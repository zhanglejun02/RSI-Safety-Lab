"""The frozen-reference script adds real matched continuations without a new proposal."""
from dataclasses import replace
import importlib.util
from pathlib import Path

import pytest

from rsi_lab.engine.continuation import read_record
from rsi_lab.errors import BudgetExceeded, IntegrityError, ValidationError
from rsi_lab.platform_config import PlatformConfig, ProtocolConfig
from rsi_lab.platform_wiring import builtin_registry, fixture_descriptor
from rsi_lab.serialization import canonical_bytes, decode_json, digest
from rsi_lab.studies.platform import create_run, inspect_run
from rsi_lab.subjects.platform_fixture import FixtureSystem


@pytest.fixture
def script():
    path = Path(__file__).parents[2] / "scripts/frozen_reference.py"
    spec = importlib.util.spec_from_file_location("frozen_reference_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def config():
    return PlatformConfig(protocol=ProtocolConfig(mode="paired", horizon=3))


def files(directory):
    return {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()}


def registry_with(subject):
    registry = builtin_registry()
    registry.systems["fixture"] = (lambda directory, providers, options: subject(directory, **options), fixture_descriptor)
    return registry


def test_matched_frozen_reference_preserves_source_and_resumes_without_execution(tmp_path, script, monkeypatch):
    source, output = tmp_path / "source", tmp_path / "reference"
    original = create_run(config(), source)
    source_bytes = files(source)
    calls = []
    run_task = FixtureSystem.run_task
    def counted(self, *args, **kwargs):
        calls.append(args[0].task_id)
        return run_task(self, *args, **kwargs)
    monkeypatch.setattr(FixtureSystem, "run_task", counted)
    monkeypatch.setattr(FixtureSystem, "propose", lambda *_: pytest.fail("Frozen reference cannot reoptimize"))
    result = script.run_reference(source, output)
    assert result["source_unchanged"] and files(source) == source_bytes
    assert result["arms"]["old_frozen"]["mean_risk"] == 0
    assert result["arms"]["new_frozen"]["risk_by_replicate"] == [[0, 0, 1]]
    assert result["arms"]["new_native"]["risk_by_replicate"][0] == original["independent"]["new"]["risk_by_round"]
    assert result["usage"]["original_run_shared_usage"] == original["usage"]
    assert len(calls) == 8  # One request in rounds 1/2, two in round 3, for each arm.
    prepared = read_record(source / "prepared.json")
    for arm in ("old", "new"):
        frozen = output / "frozen/0" / arm
        native = source / "independent/0" / arm
        assert not list(frozen.glob("prepared-*.json"))
        assert read_record(frozen / "source.json") == read_record(native / "source.json")
        assert read_record(frozen / "continuation.json")["identity"]["plan"] == read_record(native / "continuation.json")["identity"]["plan"]
        for i in range(1, 4):
            record = read_record(frozen / f"attempt-{i:04}.json")
            snapshot = prepared["base"] if arm == "old" else prepared["draft"]["candidate"]
            assert record["active_snapshot_sha256"] == digest(snapshot)
            assert record["activation"] is None and record["prepared"] is None
    assert script.run_reference(source, output) == result
    assert inspect_run(output) == result
    assert len(calls) == 8 and files(source) == source_bytes


def test_same_score_native_invalid_root_stays_nonprimary(tmp_path, script):
    class EqualFixture(FixtureSystem):
        def propose(self, operation_id, evaluator):
            draft = super().propose(operation_id, evaluator)
            return replace(draft, native_valid=False, native_evidence_json=canonical_bytes({
                "update_class": "equal_score_change", "score_gain": 0.0}).decode())
    registry = registry_with(EqualFixture)
    source = tmp_path / "source"
    create_run(config(), source, registry=registry)
    result = script.run_reference(source, tmp_path / "reference", registry=registry)
    assert result["update_class"] == "equal_score_change" and not result["native_valid"]
    assert not result["primary_eligible"]


def test_native_missing_results_remain_missing_without_repair(tmp_path, script):
    class MissingFixture(FixtureSystem):
        def run_task(self, task, tools, operation_id):
            if "/independent/0/old/" in operation_id:
                raise BudgetExceeded("Deliberate original native interruption")
            return super().run_task(task, tools, operation_id)
    registry = registry_with(MissingFixture)
    source = tmp_path / "source"
    create_run(config(), source, registry=registry)
    result = script.run_reference(source, tmp_path / "reference", registry=registry)
    assert result["arms"]["old_native"]["missing_slots"] == 3
    assert result["arms"]["old_native"]["mean_risk"] is None
    assert result["comparisons"]["delta_risk_native"] is None
    assert result["comparisons"]["interaction"] is None
    assert result["arms"]["old_frozen"]["complete"]


def test_separate_output_and_script_engine_bindings_fail_before_execution(tmp_path, script, monkeypatch):
    source, output = tmp_path / "source", tmp_path / "reference"
    create_run(config(), source)
    for invalid in (source, source / "child", tmp_path):
        with pytest.raises(ValidationError, match="separate"):
            script.run_reference(source, invalid)
    script.run_reference(source, output)
    monkeypatch.setattr(script, "script_fingerprint", lambda: "0" * 64)
    with pytest.raises(IntegrityError, match="script, source, engine"):
        script.run_reference(source, output)
    monkeypatch.setattr(script, "implementation_fingerprint", lambda: "0" * 64)
    with pytest.raises(IntegrityError, match="current engine"):
        script.run_reference(source, tmp_path / "new-reference")
    assert not (tmp_path / "new-reference").exists()


def test_native_branch_snapshot_mismatch_is_not_hidden_by_valid_checksums(tmp_path, script):
    source = tmp_path / "source"
    create_run(config(), source)
    relative = "independent/0/old/source.json"
    wrong = read_record(source / relative)
    wrong["system"] = read_record(source / "prepared.json")["draft"]["candidate"]
    def overwrite(path, record):
        path.chmod(0o600)
        path.write_bytes(canonical_bytes({"record": record, "sha256": digest(record)}))
    overwrite(source / relative, wrong)
    evidence = read_record(source / "evidence_manifest.json")
    evidence[relative] = digest(wrong)
    overwrite(source / "evidence_manifest.json", evidence)
    with pytest.raises(IntegrityError, match="initial snapshot"):
        script.run_reference(source, tmp_path / "reference")
    assert not (tmp_path / "reference").exists()
