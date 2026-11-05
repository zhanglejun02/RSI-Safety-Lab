"""Scheduled rule changes and actual Q-learning, with zero HTTP calls."""
from copy import deepcopy
from dataclasses import replace

import pytest

from rsi_lab.api import SystemSnapshot, replace_component
from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.engine.platform import PublicTaskEvaluator
from rsi_lab.errors import BudgetExceeded, ConflictError, IntegrityError, OutcomeUnknown, ValidationError
from rsi_lab.infra.chat_models import ScriptedChatProvider
from rsi_lab.serialization import decode_json, digest
from rsi_lab.studies import adaptation_rl_pilot as study
from rsi_lab.subjects.public_validation import task_candidate_valid, task_fitness
from rsi_lab.tasks import dynamic_quota as dynamic, tabular_navigation as rl
from rsi_lab.tasks.adaptation_rl_package import StudyExecutor, StudyTaskPackage


def limits():
    return study.configuration(True)["config"]["execution"]


def test_dynamic_old_safe_and_break_old_controls():
    state = dynamic.initial_state()
    for slot in (1, 2):
        item = dynamic.case(17, 1, False, state, purpose="deployment", slot=slot)
        result = dynamic.evaluate(dynamic.FOUNDER_SOURCE, item)
        dynamic.verify(result, item)
        assert result["violations"] == 0 and result["correct_requests"] == result["total_requests"]
        state = result["streams"][0]["final_state"]
    assert state["projects"]["p"]["used"] == 8
    item = dynamic.case(17, 2, True, state)
    stale, safe, broken = [dynamic.evaluate(s, item) for s in
                           (dynamic.LOCAL_SOURCE, dynamic.SAFE_SOURCE, dynamic.BREAK_OLD_SOURCE)]
    assert stale["violations"] > 0
    assert safe["violations"] == 0 and safe["correct_requests"] == safe["total_requests"]
    assert dynamic.metrics(broken)["old_rule_attempts"] == 1
    assert broken["native_refusals"] == 1 and broken["violations"] == 0
    assert safe["streams"][0]["final_state"]["effects"][:len(state["effects"])] == state["effects"]
    for phase, cumulative in ((1, False), (2, True)):
        item = dynamic.case(17, phase, cumulative, state)
        founder, improved = [dynamic.evaluate(s, item) for s in (dynamic.FOUNDER_SOURCE, dynamic.SAFE_SOURCE)]
        assert improved["cost"] < founder["cost"] and improved["correct_requests"] == improved["total_requests"]


def test_future_requirement_is_not_in_phase_one_tools_or_contract():
    phase1 = dynamic.case(17, 1, False, dynamic.initial_state())
    result = dynamic.evaluate(dynamic.FOUNDER_SOURCE, phase1)
    facts = result["streams"][0]["rounds"][0]["calls"][0]["result"]
    assert "cumulative_limit" not in facts and "used" in facts
    assert dynamic.PHASE_TWO not in dynamic.contract(False)
    phase2 = dynamic.case(17, 2, True, dynamic.initial_state())
    assert phase1["case_id"] != phase2["case_id"]


def test_rl_is_learned_has_gain_and_separate_side_effects():
    item = rl.case(2026111601)
    founder, safe, unsafe = [rl.evaluate(s, item) for s in (rl.FOUNDER_SOURCE, rl.SAFE_SOURCE, rl.UNSAFE_SOURCE)]
    for result in (founder, safe, unsafe):
        rl.verify(result, item)
        assert result["initial_q_sha256"] != result["q_table_sha256"]
        assert result["metrics"]["q_updates"] > 0
        assert result["metrics"]["training_steps"] <= 128 * 24
    f, s, u = [v["metrics"] for v in (founder, safe, unsafe)]
    assert s["task_return"] > f["task_return"]
    assert s["legal_completed"] == s["goal_completed"] == 4
    assert s["training_safety_cost"] == s["actual_violations"] == 0
    assert u["task_return"] > s["task_return"] and u["goal_completed"] == 4
    assert u["legal_completed"] == 0 and u["actual_violations"] == 4 and u["training_safety_cost"] > 0
    no_conflict = rl.evaluate(rl.UNSAFE_SOURCE, rl.case(2026211602, purpose="independent", replica=1))
    assert no_conflict["metrics"]["actual_violations"] == 0


@pytest.mark.parametrize("field", ["reward", "safety_cost", "q_after"])
def test_rl_replay_rejects_changed_transitions(field):
    item = rl.case(7)
    result = rl.evaluate(rl.FOUNDER_SOURCE, item)
    result["episodes"][0]["steps"][0][field] += 1
    with pytest.raises(IntegrityError):
        rl.verify(result, item)


def test_rl_configuration_cannot_supply_actions_or_learning_state():
    for source in (rl.SAFE_SOURCE.replace('"episodes": 128', '"episodes": 129'),
                   'def handle(request, cache, api):\n    api.execute({"action": 1})\n    return cache\n'):
        with pytest.raises(ValidationError):
            rl.configuration(source)


def test_durable_training_resume_and_incomplete_intent(tmp_path, monkeypatch):
    executor = StudyExecutor(tmp_path, limits())
    item = rl.case(31)
    first = executor.execute(rl.SAFE_SOURCE, item, "one")
    summary = executor.summary()
    monkeypatch.setattr(rl, "evaluate", lambda *a, **k: pytest.fail("retrained cached policy"))
    assert executor.execute(rl.SAFE_SOURCE, item, "one") == first
    assert executor.summary() == summary
    with pytest.raises(ConflictError):
        executor.execute(rl.UNSAFE_SOURCE, item, "one")
    identity = {"source": rl.SAFE_SOURCE, "source_sha256": digest(rl.SAFE_SOURCE), "case": item,
                "operation": "lost", "limits_sha256": digest(executor.limits)}
    save_record(tmp_path / "jobs" / digest("lost") / "intent.json", identity)
    with pytest.raises(OutcomeUnknown):
        executor.execute(rl.SAFE_SOURCE, item, "lost")


def test_budget_preflight_prevents_training(tmp_path, monkeypatch):
    cap = {**limits(), "root_training_steps": 1}
    executor = StudyExecutor(tmp_path, cap)
    monkeypatch.setattr(rl, "evaluate", lambda *a, **k: pytest.fail("trained past cap"))
    with pytest.raises(BudgetExceeded):
        executor.execute(rl.SAFE_SOURCE, rl.case(7), "limited")


def test_bound_rl_fitness_is_reward_not_service_cost():
    _, bound = study.binding("rl", 1)
    public = {"complete": True, "risk": 1, "legal_completed": 0, "legal_total": 4,
              "synthetic_units": 1000, "score": 15.75, "feedback": {"training_valid": True},
              "objective": {"evaluation_binding": bound}}
    assert task_candidate_valid(public, bound) and task_fitness(public, bound) == 15.75
    assert not task_candidate_valid(public)  # Legacy gate has not been relaxed.
    with pytest.raises(IntegrityError):
        task_fitness(public, {**bound, "phase": 2})


@pytest.mark.parametrize("system", ["openevolve", "gepa_program"])
def test_actual_rl_adapters_isolation_rejection_and_followup(tmp_path, system):
    manifest = study.configuration(True)
    root = next(r for r in manifest["root_ids"] if r["kind"] == "rl" and r["system"] == system)
    result = study._root(tmp_path, root, manifest)
    directory = tmp_path / root["path"]
    slots = [read_record(directory / p) for p in result["slots"]]
    assert all(s["status"] == "complete" for s in slots)
    assert slots[0]["committed"] and slots[1]["committed"]
    assert not slots[2]["committed"] and slots[2]["active_source"] == slots[1]["active_source"]
    assert slots[1]["public_active"]["score"] > slots[0]["public_active"]["score"]
    assert slots[1]["public_active"]["risk"] == 1
    base = SystemSnapshot.from_dict(read_record(directory / "step-0001/base.json"))
    provider = study._provider(directory, root, manifest)
    adapter = study.pilot._adapter(system, directory / "system-phase-1", provider, base)
    isolated = adapter.fork(base, tmp_path / "fork")
    assert isolated.snapshot() == base and adapter.snapshot() != base
    assert "q_table" not in str(adapter.snapshot().to_dict())


@pytest.mark.parametrize("system", ["openevolve", "gepa_program"])
@pytest.mark.parametrize("boundary", [2, 3])
def test_phase_boundary_resume_does_not_repeat_event_or_effects(tmp_path, monkeypatch, system, boundary):
    manifest = study.configuration(True)
    root = next(r for r in manifest["root_ids"] if r["kind"] == "dynamic" and r["condition"] == "change" and r["system"] == system)
    assert study._root(tmp_path, root, manifest, stop_after=boundary) is None
    directory = tmp_path / root["path"]
    phase_event = directory / "phase-event.json"
    if boundary == 2:
        assert not phase_event.exists()
    before = {str(p): p.read_bytes() for p in directory.glob("execution/jobs/*/result.json")}
    result = study._root(tmp_path, root, manifest)
    assert len(result["phase_events"]) == 1
    event = read_record(phase_event)
    assert event["environment_before"] == event["environment_after"]
    assert event["environment_before"]["projects"]["p"]["used"] == 8
    assert all(__import__("pathlib").Path(p).read_bytes() == raw for p, raw in before.items())
    extensions = [read_record(directory / p) for p in result["study_slots"]]
    assert extensions[2]["deployment"]["metrics"]["actual_violations"] == 1
    assert extensions[3]["deployment"]["metrics"]["actual_violations"] == 0
    count = len(list(directory.glob("execution/jobs/*/result.json")))
    monkeypatch.setattr(ScriptedChatProvider, "generate", lambda *a, **k: pytest.fail("repeated edit"))
    monkeypatch.setattr(dynamic, "evaluate", lambda *a, **k: pytest.fail("repeated deployment"))
    assert study._root(tmp_path, root, manifest) == result
    assert len(list(directory.glob("execution/jobs/*/result.json"))) == count
    for index in (1, 2, 3, 4):
        requests = [read_record(p) for p in (directory / f"step-{index:04}/requests").glob("*.json")]
        assert requests
        text = str(requests)
        assert (dynamic.PHASE_TWO in text.replace("\\n", "\n")) == (index >= 3)
        assert "purpose': 'independent'" not in text


def test_analysis_requires_generation_freeze(tmp_path):
    from rsi_lab.studies.adaptation_rl_analysis import analyze
    save_record(tmp_path / "manifest.json", study.configuration(True))
    with pytest.raises(FileNotFoundError):
        analyze(tmp_path)
