"""Real public execution and effect checks for the small history diagnostic."""
from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from rsi_lab.contracts import CursorFamily, Partition
from rsi_lab.engine.continuation import read_record
from rsi_lab.errors import IntegrityError, OutcomeUnknown, ValidationError
from rsi_lab.infra.chat_models import DurableChatProvider, ScriptedChatProvider
from rsi_lab.serialization import decode_json, digest
from rsi_lab.studies import experience_diagnostic as study
from rsi_lab.subjects.optimization_memory import OptimizationEpisode
from rsi_lab.tasks.cost_authorization import API_CONTRACT, FOUNDER_SOURCE, UNSAFE_REFERENCE_SOURCE


def small_config():
    return study.ExperienceDiagnosticConfig(blocks=(18101,), repeats=2, history_seed=18001, parallel_jobs=1)


def journals(output):
    result = {}
    for path in sorted(Path(output).rglob("*.sqlite")):
        if path.name not in ("root.sqlite", "editor.sqlite"):
            continue
        with sqlite3.connect(path) as db:
            result[str(path.relative_to(output))] = db.execute(
                "SELECT request_id,request_sha,status,response_sha FROM chat_requests ORDER BY request_id").fetchall()
    return result


def payload(path):
    with sqlite3.connect(path) as db:
        identifier, sha, raw = db.execute("SELECT request_id,request_sha,request_json FROM chat_requests").fetchone()
    return identifier, sha, decode_json(decode_json(raw)["messages"][-1]["content"])


@pytest.fixture(scope="module")
def small_run(tmp_path_factory):
    output = tmp_path_factory.mktemp("experience-diagnostic") / "run"
    config = small_config()
    events = []
    with pytest.MonkeyPatch.context() as provenance:
        provenance.setattr(study, "implementation_fingerprint", lambda: "f" * 64)
        # Setup hooks must not survive the yield: replay tests must exercise the
        # real functions without a wrapper that mutates captured event state.
        with pytest.MonkeyPatch.context() as setup:
            original_slot, original_score = study._slot, study._score_source

            def checked_slot(out, conf, manifest, history, job):
                frozen = read_record(out / "history-frozen.json")
                assert frozen["available"] and frozen["collection_sha256"] == digest(history)
                events.append("slot")
                return original_slot(out, conf, manifest, history, job)

            def checked_score(tasks, source, location, seed):
                assert (location.parent / "native-frozen.json").exists()
                events.append("effect")
                return original_score(tasks, source, location, seed)

            setup.setattr(study, "_slot", checked_slot)
            setup.setattr(study, "_score_source", checked_score)
            result = study.create_run(config, output)
        yield output, config, result, tuple(events)


def test_collected_real_public_failure_repair_and_unrelated_cost_history(small_run):
    output, config, summary, events = small_run
    assert summary["status"] == "complete"
    assert summary["origin"] == "scripted_control"
    assert summary["history_available"]
    assert summary["history_statuses"]["relevant"]["observed_public_failure"]
    assert summary["history_statuses"]["relevant"]["verified_repair"]
    assert summary["history_statuses"]["placebo"]["public_cost_improvement"]
    assert not summary["history_statuses"]["placebo"]["verified_repair"]
    collection = read_record(output / "history/collection.json")
    for raw in collection["episodes"]:
        episode = OptimizationEpisode.from_dict(raw)
        for execution in (episode.before, episode.candidate, episode.repair):
            if execution is None:
                continue
            plan = decode_json(execution.plan_json)
            assert plan["family"] == CursorFamily.PUBLIC_DEVELOPMENT.value
            assert plan["partition"] == Partition.DEV.value
            assert execution.source_provenance["source_sha256"] == digest(execution.source)
            assert execution.evaluation["public_trajectories"]
    assert len(summary["rows"]) == 12
    assert events.count("slot") == 12 and events.count("effect") == 24
    assert summary["usage"]["scripted_control_requests"] == 14
    assert summary["usage"]["unknown_responses"] == 0
    assert summary["max_editor_requests"] == config.max_calls == 16


def test_related_memory_changes_actual_program_and_effects_not_only_keywords(small_run):
    output, _, summary, _ = small_run
    for row in summary["rows"]:
        assert row["complete"] and row["candidate_valid"]
        if row["arm"] == "relevant":
            assert row["candidate_risk"] == 0
            assert row["version_calls"] > 0
        else:
            assert row["candidate_risk"] == 1
            assert row["candidate_actual_violations"] == 3
            assert row["version_calls"] == 0
        directory = output / "diagnostic" / str(row["block"]) / row["mode"] / row["arm"] / str(row["replicate"])
        saved = read_record(directory / "activation.json")
        frozen = read_record(directory / "native-frozen.json")
        if row["native_accepted"]:
            assert saved["activation"]["after_sha256"] == frozen["selected_snapshot_sha256"]
        else:
            assert saved["activation"] is None
        effect = read_record(directory / "candidate-effect/result.json")
        assert effect["evaluation"]["actual_violations"] == row["candidate_actual_violations"]
        assert effect["source_sha256"] == row["proposed_source_sha256"]
    assert len({row["selected_context_bytes"] for row in summary["rows"]}) == 1


def test_feedback_ablation_keeps_contract_source_summary_and_selected_history(small_run):
    output, config, _, _ = small_run
    samples = {}
    for mode in study.MODES:
        path = output / "diagnostic" / str(config.blocks[0]) / mode / "relevant/0/editor.sqlite"
        _, _, samples[mode] = payload(path)
    full, fixed = samples["full"], samples["fixed_feedback"]
    assert full["public_trajectories"] and fixed["public_trajectories"] == []
    for key in ("contract", "current_source", "public_feedback", "selected_experience", "previous_public_feedback"):
        assert full[key] == fixed[key]
    assert full["contract"] == API_CONTRACT and full["current_source"] == FOUNDER_SOURCE


def test_placebo_sham_and_repeats_use_equal_payload_but_independent_requests(small_run):
    output, config, summary, _ = small_run
    for mode in study.MODES:
        records = [payload(output / "diagnostic" / str(config.blocks[0]) / mode / arm / str(rep) / "editor.sqlite")
                   for arm in ("placebo", "sham") for rep in range(config.repeats)]
        assert len({x[0] for x in records}) == 4
        assert len({x[1] for x in records}) == 1
        assert all(x[2] == records[0][2] for x in records)
    assert not summary["learning_started"] and not summary["q_optimization_started"]
    assert not (output / "models").exists()


def test_resume_forbids_model_generation_and_keeps_report_bytes(small_run, monkeypatch):
    output, _, summary, _ = small_run
    before = {p: (output / p).read_bytes() for p in ("summary.json", "report.md")}
    calls = journals(output)
    def forbidden(*args, **kwargs):
        pytest.fail("Completed diagnostic attempted another model generation")
    monkeypatch.setattr(DurableChatProvider, "generate", forbidden)
    monkeypatch.setattr(ScriptedChatProvider, "generate", forbidden)
    assert study.resume_run(output) == summary
    assert journals(output) == calls
    assert all((output / p).read_bytes() == body for p, body in before.items())


def test_source_fingerprint_change_rejected_before_replay(small_run, monkeypatch):
    output, _, _, _ = small_run
    monkeypatch.setattr(study, "implementation_fingerprint", lambda: "e" * 64)
    with pytest.raises(IntegrityError, match="protocol or source"):
        study.resume_run(output)


def test_no_verified_repair_stops_before_diagnostic_without_fabricating_history(tmp_path, monkeypatch):
    monkeypatch.setattr(study, "implementation_fingerprint", lambda: "f" * 64)
    original = study._provider
    def provider(config, path, calls):
        value = original(config, path, calls)
        callback = value.callback
        def respond(request_id, messages):
            if decode_json(messages[-1].content)["operation"] == "repair_history":
                return {"source": UNSAFE_REFERENCE_SOURCE}
            return callback(request_id, messages)
        value.callback = respond
        return value
    monkeypatch.setattr(study, "_provider", provider)
    result = study.create_run(small_config(), tmp_path / "run")
    assert result["status"] == "no_verified_history_stop"
    assert not result["history_available"] and result["rows"] == ()
    assert not result["history_statuses"]["relevant"]["verified_repair"]
    assert result["history_statuses"]["relevant"]["repair_attempted"]
    assert result["usage"]["scripted_control_requests"] == 3
    assert not (tmp_path / "run/diagnostic").exists()


def test_incorrect_executable_proposal_and_unknown_response_are_not_safe_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(study, "implementation_fingerprint", lambda: "f" * 64)
    original = study._provider
    wrong = 'def handle(request, cache, api):\n    api.execute(request)\n    return cache\n'
    def provider(config, path, calls):
        value = original(config, path, calls)
        callback = value.callback
        def respond(request_id, messages):
            if "/full/relevant/0/" in request_id:
                return {"source": wrong}
            if "/fixed_feedback/placebo/1/" in request_id:
                raise OutcomeUnknown("Synthetic lost response after dispatch")
            return callback(request_id, messages)
        value.callback = respond
        return value
    monkeypatch.setattr(study, "_provider", provider)
    output = tmp_path / "run"
    result = study.create_run(small_config(), output)
    incorrect = next(row for row in result["rows"] if (row["mode"], row["arm"], row["replicate"]) == ("full", "relevant", 0))
    assert incorrect["complete"] and incorrect["candidate_valid"]
    assert not incorrect["native_accepted"]
    assert incorrect["candidate_risk"] == 1 and incorrect["adopted_risk"] == 0
    unknown = next(row for row in result["rows"] if (row["mode"], row["arm"], row["replicate"]) == ("fixed_feedback", "placebo", 1))
    assert not unknown["complete"] and not unknown["candidate_valid"]
    assert unknown["candidate_risk"] is None and unknown["candidate_utility"] is None
    assert unknown["adopted_risk"] == 0
    assert result["status"] == "complete_with_missing"
    assert result["usage"]["unknown_responses"] == 1
    assert result["usage"]["scripted_control_requests"] == 14
    previous = journals(output)
    before = {p: (output / p).read_bytes() for p in ("summary.json", "report.md")}
    monkeypatch.setattr(study, "_provider", original)
    def forbidden(*args, **kwargs):
        pytest.fail("Frozen missing outcome was retried")
    monkeypatch.setattr(DurableChatProvider, "generate", forbidden)
    monkeypatch.setattr(ScriptedChatProvider, "generate", forbidden)
    assert study.resume_run(output) == result
    assert journals(output) == previous
    assert all((output / p).read_bytes() == body for p, body in before.items())
