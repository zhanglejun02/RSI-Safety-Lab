"""Repeated interventions exercise actual native source proposals and replay."""
from dataclasses import replace
import sqlite3

import pytest

from rsi_lab.api import SystemSnapshot
from rsi_lab.contracts import Partition
from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.errors import IntegrityError, OutcomeUnknown, ValidationError
from rsi_lab.infra import chat_models
from rsi_lab.learning.contracts import CandidateRiskLabel
from rsi_lab.serialization import canonical_bytes, decode_json, digest
from rsi_lab.studies import q_effect as study
from rsi_lab.subjects.cost_q import (CONTROL_CACHE_SOURCE, ExperienceSelector,
                                    native_valid)
from rsi_lab.tasks.cost_authorization import FOUNDER_SOURCE, SAFE_REFERENCE_SOURCE


def chat_rows(directory):
    records = {}
    for path in sorted(directory.rglob("*.sqlite")):
        with sqlite3.connect(path) as db:
            if db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='chat_requests'").fetchone():
                records[str(path.relative_to(directory))] = db.execute(
                    "SELECT request_id, request_sha, status, response_sha FROM chat_requests ORDER BY request_id").fetchall()
    return records


@pytest.fixture(scope="module")
def effect_run(tmp_path_factory):
    output = tmp_path_factory.mktemp("q-effect-study") / "run"
    config = study.QEffectConfig(seeds=(29101,), repetitions=2, horizon=3, parallel_jobs=1)
    events = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(study, "implementation_fingerprint", lambda: "b" * 64)
        patch.setattr(chat_models, "http_transport", lambda *a, **k: pytest.fail("No network in controls"))
        original = study._run_slot

        def checked_slot(output, config, row, job, root, protocol):
            frozen = read_record(output / "candidates-frozen.json")
            assert set(frozen["root_metadata"]) == {"q-effect-scripted-29101"}
            assert frozen["interventions_sha256"][row["root_id"]] == digest(root["interventions"].arms)
            assert (output / "roots" / row["root_id"] / "prepared.json").exists()
            assert (output / "roots" / row["root_id"] / "founder-recheck.json").exists()
            events.append((job["replicate"], job["arm"]))
            return original(output, config, row, job, root, protocol)

        patch.setattr(study, "_run_slot", checked_slot)
        summary = study.create_run(config, output)
        # This setup-only assertion must not leak into later fault runs, which
        # deliberately register a different root while this fixture is alive.
        patch.setattr(study, "_run_slot", original)
        yield output, config, summary, events


def test_registered_four_arm_grid_restores_control_risk(effect_run):
    output, config, summary, events = effect_run
    assert summary["status"] == "complete" and summary["origin"] == "control"
    assert summary["registered_roots"] == 1 and len(summary["samples"]) == 8
    assert summary["usage"]["model_requests"] == 0
    assert summary["usage"]["scripted_control_requests"] == summary["max_editor_requests"] == 25
    assert summary["usage"]["unknown_responses"] == summary["usage"]["invalid_responses"] == 0
    manifest = read_record(output / "manifest.json")
    assert events == [(j["replicate"], j["arm"]) for j in manifest["submission_order"]]
    for row in summary["samples"]:
        assert row["complete"] and row["utility"] == 1
        assert row["risk"] == (1 / 3 if row["arm"] == "new" else 0)
        assert row["actual_violations"] == (3 if row["arm"] == "new" else 0)
        assert row["accepted_updates"] == 1
        assert [x["accepted"] for x in row["attempts"]] == [True, False, False]
        assert [x["risk"] for x in row["attempts"]] == ([0, 0, 1] if row["arm"] == "new" else [0, 0, 0])


def test_q_and_editor_inputs_stay_fixed_while_executed_source_really_changes(effect_run):
    output, _, summary, _ = effect_run
    root = output / "roots/q-effect-scripted-29101"
    intervention = read_record(root / "interventions.json")
    initial = {arm: SystemSnapshot.from_dict(value) for arm, value in intervention["arms"].items()}
    assert {s.version.component("policy")["source"] for s in initial.values()} == {FOUNDER_SOURCE}
    assert len({s.state_json for s in initial.values()}) == 1
    for row in summary["samples"]:
        arm, rep = row["arm"], row["replicate"]
        source = CONTROL_CACHE_SOURCE if arm == "new" else SAFE_REFERENCE_SOURCE
        assert row["final_source_sha256"] == digest(source)
        selector = ExperienceSelector.from_dict(initial[arm].version.component("experience_selector"))
        selected = selector.select(initial[arm].version.component("experience"))
        assert row["selected_experience_sha256"] == digest(selected)
        for attempt in range(1, 4):
            record = read_record(root / "repetitions" / f"{rep:03d}" / arm / "continuation" / f"attempt-{attempt:04d}.json")
            components = {c["name"]: decode_json(c["content_json"]) for c in record["active_version"]["components"]}
            assert components["experience_selector"] == selector.to_dict()
            assert components["experience"] == initial[arm].version.component("experience")
            assert components["policy"]["source"] == source
            native = decode_json(record["prepared"]["draft"]["native_evidence_json"])
            assert digest(native["selected_experience"]) == digest(selected)
            assert native["public_trajectory_rows_supplied"] > 0


def test_fresh_public_gain_and_independent_cost_have_separate_measured_evidence(effect_run):
    output, _, summary, _ = effect_run
    root = output / "roots/q-effect-scripted-29101"
    baseline = read_record(root / "founder-recheck.json")
    assert native_valid(baseline)
    baseline_cost = baseline["synthetic_units"]
    for row in summary["samples"]:
        slot = root / "repetitions" / f'{row["replicate"]:03d}' / row["arm"]
        fresh_paths = list((slot / "fresh-public").glob("*/evaluation.json"))
        assert len(fresh_paths) == 1
        fresh = read_record(fresh_paths[0])
        assert native_valid(fresh) and row["fresh_public_valid"]
        assert row["public_cost"] == fresh["synthetic_units"] < baseline_cost
        assert row["founder_fresh_public_cost"] == baseline_cost
        assert row["fresh_public_gain_fraction"] == 1 - row["public_cost"] / baseline_cost
        assert row["fresh_public_sha256"] == digest(fresh)
        records = [read_record(path) for path in (slot / "continuation/environment").glob("operations/*.json")]
        assert len(records) == 9
        assert row["evaluation_cost"] == sum(record["result"]["cost"] for record in records)
        assert row["work"]["synthetic_service_units"] > row["evaluation_cost"]
        assert row["work"]["executed_requests"] > sum(record["result"]["total_requests"] for record in records)


def test_old_restore_sham_share_initial_prompt_but_never_share_request_identity(effect_run):
    output, _, _, _ = effect_run
    root = output / "roots/q-effect-scripted-29101/repetitions"
    first_requests = []
    for rep in range(2):
        for arm in ("old", "restore", "sham"):
            with sqlite3.connect(root / f"{rep:03d}" / arm / "editor.sqlite") as db:
                request_id, body = db.execute("SELECT request_id, request_json FROM chat_requests ORDER BY rowid LIMIT 1").fetchone()
            first_requests.append((request_id, decode_json(body)))
    assert len({request_id for request_id, _ in first_requests}) == 6
    assert len({digest(payload) for _, payload in first_requests}) == 1
    assert decode_json(first_requests[0][1]["messages"][-1]["content"])["operation"] == "improve_agent"


def test_repeat_labels_keep_dev_root_and_grid_without_training_artifacts(effect_run):
    output, _, summary, _ = effect_run
    label = CandidateRiskLabel.from_dict(read_record(output / "roots/q-effect-scripted-29101/label.json"))
    assert label.replicates == 2 and label.binding.horizon == 3
    assert label.binding.partition == Partition.DEV
    assert label.targets() == (0.0, 1 / 3, 1.0, 1.0)
    assert {(o.arm, o.replicate, o.attempt) for o in label.outcomes} == {
        (arm, rep, attempt) for arm in ("old", "new") for rep in range(2) for attempt in range(1, 4)}
    assert not (output / "datasets").exists()
    assert not (output / "models").exists()
    assert not (output / "models-frozen.json").exists()
    assert not list(output.rglob("features.json"))
    assert summary["analysis"]["scope"] == "development_exploration_not_final_evaluation_or_safety_guarantee"


def test_completed_resume_rechecks_without_provider_dispatch_or_result_changes(effect_run, monkeypatch):
    output, _, summary, _ = effect_run
    before = chat_rows(output)
    summary_bytes, report_bytes = (output / "summary.json").read_bytes(), (output / "report.md").read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail("Completed replay must never call provider.generate")
    monkeypatch.setattr(chat_models.ScriptedChatProvider, "generate", forbidden)
    monkeypatch.setattr(chat_models.DurableChatProvider, "generate", forbidden)
    replayed = study.resume_run(output)
    assert canonical_bytes(replayed) == canonical_bytes(summary)
    assert chat_rows(output) == before
    assert (output / "summary.json").read_bytes() == summary_bytes
    assert (output / "report.md").read_bytes() == report_bytes


def test_resume_rejects_changed_implementation(effect_run, monkeypatch):
    output, _, _, _ = effect_run
    monkeypatch.setattr(study, "implementation_fingerprint", lambda: "c" * 64)
    with pytest.raises(IntegrityError, match="Protocol/implementation changed"):
        study.resume_run(output)


def test_manifest_config_change_is_not_silent_protocol_amendment(effect_run, tmp_path):
    output, _, _, _ = effect_run
    manifest = read_record(output / "manifest.json")
    manifest["config"]["repetitions"] = 3
    target = tmp_path / "tampered"
    target.mkdir()
    save_record(target / "manifest.json", manifest)
    with pytest.raises(IntegrityError, match="Protocol/implementation changed"):
        study.resume_run(target)


@pytest.mark.parametrize("fields", [{"seeds": (1, 1)}, {"repetitions": 1}, {"horizon": 2}, {"parallel_jobs": 5}])
def test_invalid_registration_cannot_start_calls(fields):
    with pytest.raises(ValidationError):
        study.QEffectConfig(**fields)


def test_http_allowance_must_equal_the_registered_repeat_grid():
    model = replace(study.ModelSettings(), provider="http", max_calls=25)
    config = study.QEffectConfig(seeds=(1,), repetitions=2, horizon=3, model=model)
    assert config.calls_per_root == 25
    with pytest.raises(ValidationError, match="call cap"):
        replace(config, model=replace(model, max_calls=24))


def test_unknown_model_and_failed_fresh_recheck_preserve_distinct_missingness_and_do_not_retry(tmp_path, monkeypatch):
    output = tmp_path / "fault-run"
    config = study.QEffectConfig(seeds=(29102,), repetitions=2, horizon=3, parallel_jobs=1)
    monkeypatch.setattr(study, "implementation_fingerprint", lambda: "d" * 64)
    response = study.scripted_cost_response
    evaluate = study.PublicTaskEvaluator.evaluate
    dispatch_failures, fresh_failures = [], []

    def fail_once(request_id, messages):
        if "/effect-new/0/new/1/propose/edit" in request_id:
            dispatch_failures.append(request_id)
            raise OutcomeUnknown("Controlled transport fault")
        return response(request_id, messages)

    def fail_fresh(self, snapshot, operation_id):
        if operation_id.endswith("/fresh/1/old"):
            fresh_failures.append(operation_id)
            raise ValidationError("Controlled local fresh-public evaluation fault")
        return evaluate(self, snapshot, operation_id)

    with monkeypatch.context() as initial_faults:
        initial_faults.setattr(study, "scripted_cost_response", fail_once)
        initial_faults.setattr(study.PublicTaskEvaluator, "evaluate", fail_fresh)
        summary = study.create_run(config, output)
    assert len(dispatch_failures) == len(fresh_failures) == 1
    assert summary["status"] == "complete_with_missing"
    assert summary["usage"]["scripted_control_requests"] == 23
    assert summary["usage"]["unknown_responses"] == 1
    rows = {(r["replicate"], r["arm"]): r for r in summary["samples"]}
    missing = rows[0, "new"]
    assert not missing["complete"]
    assert missing["risk"] is missing["utility"] is missing["evaluation_cost"] is None
    assert len(missing["attempts"]) == 3
    assert all(not r["complete"] and r["risk"] is None and r["missing_reason"] == "OutcomeUnknown"
               for r in missing["attempts"])
    assert missing["accepted_updates"] == 0
    assert all(row["complete"] for key, row in rows.items() if key != (0, "new"))
    fresh_missing = rows[1, "old"]
    assert fresh_missing["complete"] and fresh_missing["risk"] == 0 and fresh_missing["utility"] == 1
    assert fresh_missing["public_cost"] is fresh_missing["fresh_public_gain_fraction"] is None
    assert fresh_missing["evaluation_cost"] > 0 and not fresh_missing["fresh_public_valid"]
    contrasts = summary["analysis"]["eligible"]["contrasts"]
    assert contrasts["new-old"]["risk"]["root_test"] is None
    assert contrasts["new-old"]["risk"]["sampling_test"] is None
    assert contrasts["sham-old"]["risk"]["root_test"] is not None
    assert contrasts["sham-old"]["public_cost"]["root_test"] is None
    label = CandidateRiskLabel.from_dict(read_record(output / "roots/q-effect-scripted-29102/label.json"))
    assert len(label.outcomes) == 12 and label.targets() is None
    before = chat_rows(output)
    summary_bytes, report_bytes = (output / "summary.json").read_bytes(), (output / "report.md").read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("A complete or unknown model intent cannot be retried")

    def no_fresh_retry(self, snapshot, operation_id):
        if operation_id.endswith("/fresh/1/old"):
            pytest.fail("An already reported missing fresh-public recheck cannot be filled on resume")
        return evaluate(self, snapshot, operation_id)

    monkeypatch.setattr(chat_models.ScriptedChatProvider, "generate", forbidden)
    monkeypatch.setattr(chat_models.DurableChatProvider, "generate", forbidden)
    monkeypatch.setattr(study.PublicTaskEvaluator, "evaluate", no_fresh_retry)
    study.resume_run(output)
    assert chat_rows(output) == before
    assert (output / "summary.json").read_bytes() == summary_bytes
    assert (output / "report.md").read_bytes() == report_bytes
