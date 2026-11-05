import pytest

from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.errors import ConflictError, IntegrityError, ValidationError
from rsi_lab.infra.chat_models import ScriptedChatProvider
from rsi_lab.serialization import digest
from rsi_lab.studies import stress_confirmation as study
from rsi_lab.studies import stress_pilot as pilot
from rsi_lab.studies.stress_pilot_analysis import analyze


def test_fixed_confirmation_budget_and_distinct_controls():
    paid, _ = study.manifest_for()
    control, _ = study.manifest_for(control=True)
    assert (paid["model_call_cap"], paid["token_cap"]) == (384, 6400000)
    assert len(paid["holdout_parameters"]) == 64 and len(paid["root_ids"]) == 128
    assert paid["confirmatory"] and control["provider_kind"] == "scripted" and not control["confirmatory"]
    assert not set(paid["seeds"]).intersection(control["seeds"] + [2026110301, 2026110302, 2041051001])
    assert digest(paid["holdout_parameters"]) == paid["holdout_parameters_sha256"]
    with pytest.raises(ValidationError, match="isolated"):
        study.run_control(study.OUTPUT)


def test_paid_run_requires_bound_authorization_and_original_output(tmp_path, monkeypatch):
    out = tmp_path / "only-paid"
    reg = tmp_path / "registration"
    monkeypatch.setattr(study, "REG", reg)
    monkeypatch.setattr(study, "OUTPUT", out)
    manifest, _ = study.manifest_for()
    record = {"manifest": manifest, "source_snapshot_sha256": study.hashlib.sha256(b"snapshot").hexdigest()}
    monkeypatch.setattr(study, "_registration", lambda: (record, b"snapshot"))
    save_record(reg / "registration.json", record)
    (reg / "source-snapshot.zip").write_bytes(b"snapshot")
    approval = tmp_path / "approval.json"
    terms = {"authorized": True, "registration_sha256": digest(record), "output": str(out),
             "max_calls": 384, "max_total_tokens": 6400000,
             "endpoint": "https://api.moonshot.cn/v1", "model": "kimi-k2.6", "api_key_env": "MOONSHOT_API_KEY",
             "data_scope": study.DATA_SCOPE, "user_statement": "scripted test only"}
    save_record(tmp_path / "wrong.json", {**terms, "max_calls": 48})
    with pytest.raises(ValidationError, match="approval"):
        study.run_paid(tmp_path / "wrong.json")
    assert not out.exists() and not (reg / "dispatch-started.json").exists()
    save_record(approval, terms)
    # No provider is constructed by this test.
    monkeypatch.setattr(pilot, "_run_registered", lambda *args, **kwargs: None)
    # Simulate an interruption immediately before the budget marker becomes durable.
    real_save = study.save_record
    def interrupt(path, value):
        if path.name == "dispatch-started.json":
            raise OSError("simulated power interruption")
        real_save(path, value)
    monkeypatch.setattr(study, "save_record", interrupt)
    with pytest.raises(OSError, match="interruption"):
        study.run_paid(approval)
    monkeypatch.setattr(study, "save_record", real_save)
    study.run_paid(approval, resume=True)
    with pytest.raises(ConflictError, match="budget"):
        study.run_paid(approval)
    (out / "manifest.json").unlink()  # a lost bound manifest must not reopen a paid budget
    with pytest.raises(ConflictError, match="original"):
        study.run_paid(approval, resume=True)


def test_missing_credentials_cannot_consume_formal_slots(tmp_path, monkeypatch):
    manifest, settings = study.manifest_for()
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    out = tmp_path / "formal"
    with pytest.raises(ValidationError, match="credential"):
        pilot._run_registered(manifest, settings, out)
    assert not (out / "roots").exists()
    assert not (out / "generation_complete.json").exists()


def test_known_overrun_stops_even_on_resume(tmp_path):
    from types import SimpleNamespace
    manifest, settings = study.manifest_for()
    out = tmp_path / "overrun"
    save_record(out / "manifest.json", manifest)
    provider = SimpleNamespace(summary=lambda: {"actual_total_tokens": 50001, "budget_overrun": True})
    with pytest.raises(IntegrityError, match="budget stopped"):
        pilot._check_confirmation_budget(out, manifest["root_ids"][0], 1, provider)
    assert read_record(out / "budget-stop.json")["root_usage"]["actual_total_tokens"] == 50001
    with pytest.raises(IntegrityError, match="budget stopped"):
        pilot._run_registered(manifest, settings, out, resume=True)
    assert not (out / "roots").exists()


def test_scripted_freeze_holdout_warning_and_no_repeat(tmp_path, monkeypatch):
    manifest, settings = study.manifest_for(control=True)
    output = tmp_path / "control"
    pilot._run_registered(manifest, settings, output, analyze=False)
    assert not (output / "analysis").exists()
    frozen = read_record(output / "generation_complete.json")
    summary = analyze(output)
    assert summary["origin"] == "control" and not summary["formal_holdout"]
    assert read_record(output / "analysis/holdout-access.json")["generation_freeze_sha256"] == digest(frozen)
    for directory in (output / "analysis/roots").iterdir():
        cases = read_record(directory / "cases.json")["cases"]
        assert all(c["split"] == "holdout" and c["seed"] == manifest["holdout_parameters"]["finance/2066110501"]
                   for c in cases)
    warning = read_record(output / "analysis/finance-warning.json")
    assert warning["eligible"] and warning["origin"] == "control"
    def forbidden(*args, **kwargs):
        raise AssertionError("completed resume called the editor")
    monkeypatch.setattr(ScriptedChatProvider, "generate", forbidden)
    assert study.run_control(output, resume=True) == summary
