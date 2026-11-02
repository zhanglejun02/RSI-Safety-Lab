from dataclasses import replace
import sqlite3

import pytest

from rsi_lab.api import SystemSnapshot, SystemVersion
from rsi_lab.engine.continuation import read_record
from rsi_lab.errors import IntegrityError, OutcomeUnknown, ValidationError
from rsi_lab.infra.chat_models import DurableChatProvider, ScriptedChatProvider
from rsi_lab.serialization import canonical_bytes, decode_json, digest
from rsi_lab.studies import optimized_q as study
from rsi_lab.tasks.cost_authorization import FOUNDER_SOURCE


@pytest.fixture(scope="module")
def control(tmp_path_factory):
    output = tmp_path_factory.mktemp("optimized-q") / "run"
    config = study.OptimizedQConfig(blocks=(19301,), repetitions=2, checkpoint_recheck_seed=19300, parallel_jobs=2)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(study, "implementation_fingerprint", lambda: "a" * 64)
        result = study.create_run(config, output)
        yield output, config, result


def test_actual_h2_continuations_start_from_optimized_checkpoint_and_keep_q(control):
    output, config, summary = control
    assert summary["status"] == "complete" and summary["checkpoint_public"]["eligible"]
    manifest = read_record(output / "manifest.json")
    checkpoint = SystemSnapshot.from_dict(manifest["imported"]["checkpoint"])
    assert checkpoint.version.component("policy")["source"] != FOUNDER_SOURCE
    assert len(summary["samples"]) == 6
    assert summary["usage"]["scripted_control_requests"] == 12
    assert summary["usage"]["model_requests"] == 0
    assert len(list(output.glob("models/*.sqlite"))) == 1
    for row in summary["samples"]:
        assert row["complete"] and row["utility"] == 1 and row["fresh_gain"] > 0
        assert row["risk"] == (0 if row["arm"] == "relevant" else 1)
        assert row["accepted_updates"] == 1
        assert len(row["attempts"]) == 2
        slot = study._slot_path(output, row)
        first = read_record(slot / "continuation/attempt-0001.json")
        second = read_record(slot / "continuation/attempt-0002.json")
        assert second["prepared"]["base"] == first["prepared"]["draft"]["candidate"]
        for record in (first, second):
            active = SystemVersion.from_dict(record["active_version"])
            initial = SystemSnapshot.from_dict(manifest["arms"][row["arm"]])
            assert active.component("experience_selector") == initial.version.component("experience_selector")
            assert active.component("experience") == checkpoint.version.component("experience")
        assert row["path_violations"] == sum(r["actual_violations"] for r in row["attempts"])
    assert summary["input_equivalence"]["same_input_controls_verified"]
    assert summary["baseline"]["19301"]["terminal_risk"] == 0
    assert (output / "generation-complete.json").is_file()


def test_complete_resume_no_model_or_record_change(control, monkeypatch):
    output, _, summary = control
    before = {name: (output / name).read_bytes() for name in ("summary.json", "report.md")}
    def forbidden(*args, **kwargs): pytest.fail("No model during completed replay")
    monkeypatch.setattr(DurableChatProvider, "generate", forbidden)
    monkeypatch.setattr(ScriptedChatProvider, "generate", forbidden)
    assert canonical_bytes(study.resume_run(output)) == canonical_bytes(summary)
    assert all((output / name).read_bytes() == data for name, data in before.items())


def test_checkpoint_recheck_failure_stops_without_any_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(study, "implementation_fingerprint", lambda: "b" * 64)
    original = study.import_checkpoint
    def no_gain(config):
        result = original(config)
        result["previous"] = result["checkpoint"]
        return result
    monkeypatch.setattr(study, "import_checkpoint", no_gain)
    monkeypatch.setattr(ScriptedChatProvider, "generate", lambda *a, **k: pytest.fail("No fallback or model call"))
    result = study.create_run(study.OptimizedQConfig(blocks=(19302,), repetitions=2), tmp_path / "run")
    assert result["status"] == "checkpoint_public_recheck_failed_stop"
    assert result["usage"]["scripted_control_requests"] == 0
    assert result["analysis"]["test"] is None


def test_unknown_first_attempt_stays_missing_and_is_never_redispatched(tmp_path, monkeypatch):
    monkeypatch.setattr(study, "implementation_fingerprint", lambda: "c" * 64)
    original = study._provider
    def provider(config, output):
        default = original(config, output)
        respond = default.callback
        def callback(request_id, messages):
            if request_id == "optimized-q-19303/placebo/0/new/1/propose/edit":
                raise TimeoutError("unknown")
            return respond(request_id, messages)
        default.callback = callback
        return default
    monkeypatch.setattr(study, "_provider", provider)
    config = study.OptimizedQConfig(blocks=(19303,), repetitions=2, parallel_jobs=1)
    output = tmp_path / "run"
    result = study.create_run(config, output)
    assert result["status"] == "complete_with_missing"
    missing = [r for r in result["samples"] if not r["complete"]]
    assert len(missing) == 1 and missing[0]["risk"] is None
    assert [r["missing_reason"] for r in missing[0]["attempts"]] == ["OutcomeUnknown", "OutcomeUnknown"]
    assert result["usage"]["scripted_control_requests"] == 11
    monkeypatch.setattr(ScriptedChatProvider, "generate", lambda *a, **k: pytest.fail("Unknown retried"))
    assert canonical_bytes(study.resume_run(output)) == canonical_bytes(result)


def test_fingerprint_change_rejects_resume(control, monkeypatch):
    monkeypatch.setattr(study, "implementation_fingerprint", lambda: "e" * 64)
    with pytest.raises(IntegrityError): study.resume_run(control[0])


def test_natural_budget_cannot_exceed_registered_last_diagnostic():
    config = study.load_config("configs/optimized_q_kimi.toml")
    assert config.max_calls == 36
    with pytest.raises(ValidationError): replace(config, model=replace(config.model, max_total_tokens=500001))
    with pytest.raises(ValidationError): replace(config, horizon=3)
