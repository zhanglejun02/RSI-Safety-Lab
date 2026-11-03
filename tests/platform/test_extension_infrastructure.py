"""New adapters can use the existing engine without built-in provider branches."""
from dataclasses import replace
from pathlib import Path
import subprocess
import sqlite3
import sys

import pytest

from rsi_lab.api import Component, SystemVersion, replace_component
from rsi_lab.api.registry import ScriptedModel
from rsi_lab.errors import IntegrityError, LabError, UnsupportedCapability, ValidationError
from rsi_lab.infra.chat_models import ChatMessage
from rsi_lab.infra.sqlite import durable_transaction
from rsi_lab.platform_config import PlatformConfig, PluginSpec, ProtocolConfig, DetectorConfig
from rsi_lab.platform_wiring import builtin_registry, build_providers, fixture_descriptor
from rsi_lab.serialization import canonical_bytes, json_text
from rsi_lab.studies import platform, validity_pilot
from rsi_lab.subjects.platform_fixture import FixtureSystem


def descriptor(options):
    return replace(fixture_descriptor({}), system_id="external-demo", display_name="External control",
                   upstream_url="local:external-demo", upstream_revision="external-v1")


class ExternalControl(FixtureSystem):
    system_id, revision = "external-demo", "external-v1"

    def __init__(self, directory, provider, initial_snapshot=None):
        self.provider = provider
        super().__init__(directory, initial_snapshot)

    def describe(self):
        return descriptor({})

    def fork(self, snapshot, directory):
        return type(self)(directory, self.provider, snapshot)

    def propose(self, operation_id, evaluator):
        response = self.provider.generate(operation_id + "/model", (ChatMessage("user", "external operation"),))
        self.proposed_strategy = response.content["strategy"]
        return super().propose(operation_id, evaluator)


def registry(*, control=True):
    result = builtin_registry()
    result.register_system("external-demo",
        lambda directory, providers, options: ExternalControl(directory, providers["editor"]), descriptor,
        scripted_model=(lambda role, options: ScriptedModel("control.external." + role,
            lambda request, messages: {"strategy": "strict"})) if control else None)
    return result


def config():
    return PlatformConfig(system=PluginSpec("external-demo"), protocol=ProtocolConfig(horizon=2),
                          detector=DetectorConfig(name="none", policy="native-only"))


def test_external_registered_provider_runs_admission_and_resumes(tmp_path):
    plugins, settings = registry(), config()
    run = tmp_path / "run"
    result = platform.create_run(settings, run, registry=plugins)
    assert result["activated"] and result["decision"] == "accept"
    assert result["deployment"]["risk_by_round"] == [0, 0]
    assert result["usage"]["models"]["editor"]["dispatched_calls"] > 1
    providers = build_providers(settings, run / "models", registry=plugins)
    assert providers["editor"].provider_id == "control.external.editor"
    count = providers["editor"].summary()["dispatched_calls"]
    assert platform.resume_run(run, registry=plugins) == result
    assert providers["editor"].summary()["dispatched_calls"] == count


def test_external_registered_provider_reaches_four_arm_pilot(tmp_path):
    plugins = registry()
    settings = replace(config(), protocol=replace(config().protocol, mode="paired"))
    result = validity_pilot.create_run(settings, tmp_path / "pilot", roots=1, registry=plugins)
    assert result["registered_roots"] == 1
    assert validity_pilot.resume_run(tmp_path / "pilot", registry=plugins) == result


def test_missing_control_does_not_fall_back_or_create_model_journals(tmp_path):
    output = tmp_path / "models"
    with pytest.raises(UnsupportedCapability, match="no registered scripted model"):
        build_providers(config(), output, registry=registry(control=False))
    assert not output.exists()


def test_constructed_adapter_must_match_preflight(tmp_path):
    plugins = builtin_registry()
    plugins.register_system("incorrect", lambda directory, providers, options: FixtureSystem(directory), descriptor)
    with pytest.raises(IntegrityError, match="descriptor differs"):
        plugins.build_system(replace(config(), system=PluginSpec("incorrect")), tmp_path, {})


def test_component_edit_is_canonical_and_missing_name_is_not_noop():
    version = SystemVersion("external-demo", (Component("instructions", '{"b":2,"a":1}', "agent"),
                                              Component("settings", '{}')), "v1")
    edited = replace_component(version, "instructions", {"new": "中文"})
    expected = replace(version, components=(replace(version.components[0], content_json=json_text({"new": "中文"})), version.components[1]))
    assert canonical_bytes(edited) == canonical_bytes(expected)
    assert version.component("instructions") == {"a": 1, "b": 2}
    with pytest.raises(ValidationError, match="Missing component"):
        replace_component(version, "instruction_typo", {})
    from rsi_lab.subjects.platform_fixture import replace_component as legacy
    assert legacy is replace_component


def test_shared_write_transaction_rolls_back_and_closes(tmp_path):
    path = tmp_path / "state.sqlite"
    with durable_transaction(path) as db:
        db.execute("CREATE TABLE facts(value INTEGER)")
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert db.in_transaction
    with pytest.raises(RuntimeError):
        with durable_transaction(path) as db:
            db.execute("INSERT INTO facts VALUES(1)")
            raise RuntimeError("rollback")
    with durable_transaction(path) as db:
        assert db.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        db.execute("SELECT 1")


def test_shared_transaction_rejects_symlinked_journal(tmp_path):
    path = tmp_path / "state.sqlite"
    target = tmp_path / "target"
    target.write_text("preserve")
    Path(str(path) + "-journal").symlink_to(target)
    with pytest.raises(LabError):
        with durable_transaction(path):
            pytest.fail("Unsafe journal path must not be opened")
    assert target.read_text() == "preserve" and not path.exists()


def test_top_level_help_lists_all_registered_routes():
    from rsi_lab.cli import SUBCOMMANDS
    result = subprocess.run([sys.executable, "-m", "rsi_lab", "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert all(name in result.stdout for name in SUBCOMMANDS)


def test_new_cli_does_not_import_legacy_runner():
    code = '''import sys
from rsi_lab.cli import main
try:
    main(["openevolve-screen", "--help"])
except SystemExit as exc:
    assert exc.code == 0
assert "rsi_lab._legacy_cli" not in sys.modules
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
