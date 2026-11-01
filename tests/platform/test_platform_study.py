"""Whole-platform composition tests, including actual controlled deployment.

The source fingerprint is held constant only inside these tests to avoid unrelated
concurrent source edits invalidating temporary runs. Production resume separately
checks its actual source fingerprint, state bindings and immutable evidence.
"""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from rsi_lab.api import Capabilities, SystemDescriptor
from rsi_lab.api.registry import PluginRegistry
from rsi_lab.contracts import Partition
from rsi_lab.engine.continuation import read_record
from rsi_lab.errors import IntegrityError, UnsupportedCapability, ValidationError
from rsi_lab.learning.contracts import AuditFeatures, RiskPrediction, CandidateRiskLabel
from rsi_lab.platform_config import (PlatformConfig, PluginSpec, ProtocolConfig,
                                    DetectorConfig, load_platform_config)
from rsi_lab.platform_wiring import build_providers, builtin_registry
from rsi_lab.studies import platform
from rsi_lab.tasks.platform_authorization import AuthorizationTaskPackage


@pytest.fixture(autouse=True)
def stable_source_fingerprint(monkeypatch):
    monkeypatch.setattr(platform, "implementation_fingerprint", lambda: "1" * 64)


def test_rule_rejects_and_report_matches_actual_deployment(tmp_path):
    output = tmp_path / "run"
    result = platform.create_run(PlatformConfig(), output)
    assert result["decision"] == "reject" and not result["activated"]
    assert result["deployment"]["risk_by_round"] == [0] * 6
    assert result["independent"]["old"]["risk_by_round"] == [0] * 6
    assert result["independent"]["new"]["risk_by_round"] == [0, 0, 1, 1, 1, 1]
    assert result["proposed_version_sha256"] != result["deployed_version_sha256"]
    assert set(result["deployment"]["versions"]) == {result["deployed_version_sha256"]}
    assert read_record(output / "activation.json")["receipt"] is None
    assert platform.inspect_run(output)["active_snapshot_sha256"] == result["active_snapshot_sha256"]


def test_native_baseline_accepts_and_actual_deployment_is_unsafe(tmp_path):
    config = PlatformConfig(detector=DetectorConfig(name="none", policy="native-only"))
    result = platform.create_run(config, tmp_path / "run")
    assert result["decision"] == "accept" and result["activated"]
    assert result["deployment"]["risk_by_round"] == [0, 0, 1, 1, 1, 1]
    assert result["proposed_version_sha256"] == result["deployed_version_sha256"]
    assert set(result["deployment"]["versions"]) == {result["deployed_version_sha256"]}


def test_bounded_q_update_drives_real_descendants_and_exports_supervision(tmp_path):
    config = PlatformConfig(system=PluginSpec("bounded-task-a"),
        protocol=ProtocolConfig(partition=Partition.TRAIN))
    output = tmp_path / "run"
    result = platform.create_run(config, output)
    features = AuditFeatures.from_dict(read_record(output / "features.json"))
    label = CandidateRiskLabel.from_dict(read_record(output / "label.json"))
    assert features.binding.component == "q"
    assert label.targets() == (0.0, 4 / 6, 1.0, 1.0)
    assert result["decision"] == "reject"
    assert result["usage"]["models"]["editor"]["dispatched_calls"] > 1
    assert features.probe_stream_id != label.evaluation_stream_id
    assert not ({r.trace_sha256 for r in features.probe_outcomes} &
                {r.trace_sha256 for r in label.outcomes})
    platform.export_dataset([output], tmp_path / "dataset")
    assert (tmp_path / "dataset" / "manifest.json").exists()


def test_gepa_actual_upstream_end_to_end_and_resume_no_new_calls(tmp_path):
    pytest.importorskip("gepa")
    config = load_platform_config(Path(__file__).parents[2] / "configs/platform_gepa_control.toml")
    output = tmp_path / "run"
    result = platform.create_run(config, output)
    assert result["origin"] == "control" and result["engineering_control"]
    assert result["decision"] == "reject" and not result["activated"]
    assert result["deployment"]["risk_by_round"] == [0] * 6
    # Both native branches can independently rediscover the scripted unsafe
    # mutation. Zero paired delta is retained; it is not converted to D evidence.
    assert result["label_targets"][:2] == (4 / 6, 4 / 6)
    evidence = read_record(output / "prepared.json")["draft"]["native_evidence_json"]
    assert '"optimizer":"gepa.optimize"' in evidence
    providers = build_providers(config, output / "models")
    before = {k: p.summary()["dispatched_calls"] for k, p in providers.items()}
    assert before["editor"] > 0 and before["actor"] > 0
    resumed = platform.resume_run(output)
    assert resumed["active_snapshot_sha256"] == result["active_snapshot_sha256"]
    assert {k: p.summary()["dispatched_calls"] for k, p in providers.items()} == before


def test_execute_only_capability_allows_behavior_but_not_admission(tmp_path):
    registry = PluginRegistry()
    def must_not_construct(*args):
        raise AssertionError("Capability failure must precede system construction")
    registry.register_system("execute-only", must_not_construct, lambda options:
        SystemDescriptor("execute-only", "Behavior-only integration",
                         Capabilities(tool_protocols=("authorization-tools-v1",)),
                         "local:test", "v1"))
    registry.register_task("authorization", lambda options: AuthorizationTaskPackage())
    config = PlatformConfig(system=PluginSpec("execute-only"), protocol=ProtocolConfig(mode="behavior"))
    assert platform.preflight(config, registry)["valid"]
    rejected = replace(config, protocol=replace(config.protocol, mode="admission"))
    with pytest.raises(UnsupportedCapability):
        platform.create_run(rejected, tmp_path / "run", registry=registry)
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("field", ["system", "task", "backend"])
def test_unknown_plugins_fail_before_output_creation(tmp_path, field):
    output = tmp_path / "run"
    with pytest.raises((ValidationError, UnsupportedCapability)):
        config = PlatformConfig(**{field: PluginSpec("not-installed")})
        platform.create_run(config, output)
    assert not output.exists()


def test_frozen_prediction_controls_actual_activation_without_label_access(tmp_path, monkeypatch):
    output = tmp_path / "run"
    calls = []
    class Scorer:
        artifact = SimpleNamespace(to_dict=lambda: {"test_frozen_artifact": "fixed"})
        def predict(self, features):
            assert type(features) is AuditFeatures
            assert not (output / "label.json").exists()
            assert not (output / "independent").exists()
            calls.append(features)
            return RiskPrediction(features.binding, "a" * 64, 0.0, 0.0, 1.0, 1.0,
                (0.0, 0.0, 0.0, 0.0), True, 10, True)
    monkeypatch.setattr(platform, "load_model", lambda _: Scorer())
    config = PlatformConfig(system=PluginSpec("fixture", {"proposed_strategy": "strict"}),
        detector=DetectorConfig("frozen-model", "conservative", "test-only-frozen-model.json"))
    result = platform.create_run(config, output)
    assert len(calls) == 1
    assert result["decision"] == "accept" and result["activated"]
    assert result["reason"] == "calibrated_risk_and_utility_requirements_met"
    assert result["proposed_version_sha256"] == result["deployed_version_sha256"]
    assert result["deployment"]["risk_by_round"] == [0] * 6


def test_resume_fingerprint_drift_fails_before_models_or_work(tmp_path, monkeypatch):
    output = tmp_path / "run"
    platform.create_run(PlatformConfig(), output)
    monkeypatch.setattr(platform, "implementation_fingerprint", lambda: "2" * 64)
    def forbidden(*args):
        raise AssertionError("No model provider should be built after protocol drift")
    with pytest.raises(IntegrityError, match="implementation or protocol changed"):
        platform.resume_run(output, provider_factory=forbidden)


def test_insufficient_probe_budget_fails_before_any_model_or_output(tmp_path):
    config = PlatformConfig(protocol=ProtocolConfig(probe_round_budget=2))
    with pytest.raises(ValidationError, match="paired probe budget"):
        platform.create_run(config, tmp_path / "run")
    assert not (tmp_path / "run").exists()


def test_completed_resume_checks_live_descendant_environment(tmp_path):
    import sqlite3
    from rsi_lab.serialization import canonical_bytes
    output = tmp_path / "run"
    platform.create_run(PlatformConfig(), output)
    world = output / "independent/0/new/environment/environment.sqlite"
    with sqlite3.connect(world) as db:
        db.execute("UPDATE auth_state SET payload=? WHERE id=1", (canonical_bytes({"grants": [], "confirmations": []}),))
    with pytest.raises(IntegrityError):
        platform.resume_run(output)
