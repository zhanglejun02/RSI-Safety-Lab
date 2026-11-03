from dataclasses import replace

import pytest

pytest.importorskip("openevolve")

from rsi_lab.auditing.platform import AdmissionDecision
from rsi_lab.contracts import Partition
from rsi_lab.engine.activation import ActivationController
from rsi_lab.engine.platform import PublicTaskEvaluator, prepare_update
from rsi_lab.errors import IntegrityError, UnsupportedCapability
from rsi_lab.infra.chat_models import ScriptedChatProvider
from rsi_lab.serialization import decode_json, digest
from rsi_lab.subjects.openevolve import OpenEvolveAdapter, initial_snapshot, verify_upstream
from rsi_lab.tasks.cost_authorization import API_CONTRACT, FOUNDER_SOURCE, SAFE_REFERENCE_SOURCE
from rsi_lab.tasks.platform_cost_authorization import CostAuthorizationTaskPackage


def setup(tmp_path, response=None):
    provider = ScriptedChatProvider(tmp_path / "model.sqlite", response or
        (lambda *_: {"response": "```python\n" + SAFE_REFERENCE_SOURCE + "\n```"}))
    snapshot = initial_snapshot(13101, FOUNDER_SOURCE, "cost-authorization-program-v1", API_CONTRACT)
    adapter = OpenEvolveAdapter(tmp_path / "active", provider, snapshot)
    return adapter, CostAuthorizationTaskPackage()


def propose(adapter, package, path, operation="proposal"):
    evaluator = PublicTaskEvaluator(adapter, package, path / "public", seed=13101)
    base = adapter.snapshot()
    draft = adapter.propose(operation, evaluator)
    prepared = prepare_update(adapter, draft, base, update_id=operation, root_id="root-13101",
        protocol_sha256=digest("protocol"), environment_sha256=digest("environment"),
        partition=Partition.DEV, horizon=2, task_plan_sha256=digest("tasks"))
    return prepared


def apply(adapter, prepared, path, disposition):
    decision = AdmissionDecision(prepared.identity, digest("evidence"), "test", disposition, "engineering")
    return ActivationController(path / "activation.sqlite", digest("protocol")).apply(
        adapter, prepared, decision, environment_sha256=digest("environment"))


def test_native_worker_proposes_without_mutating_and_replays(tmp_path, monkeypatch):
    import openevolve.process_parallel as native
    original, calls = native._run_iteration, []
    def observed(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(native, "_run_iteration", observed)
    adapter, package = setup(tmp_path)
    base = adapter.snapshot()
    prepared = propose(adapter, package, tmp_path)
    assert calls == [1]
    assert prepared.draft.native_valid
    assert adapter.snapshot() == base
    assert decode_json(prepared.draft.candidate.state_json)["population"]["programs"]
    assert propose(adapter, package, tmp_path) == prepared
    assert calls == [1]
    assert adapter.provider.summary()["dispatched_calls"] == 1


def test_rejection_never_enters_archive_or_later_context(tmp_path):
    adapter, package = setup(tmp_path)
    prepared = propose(adapter, package, tmp_path / "first", "first")
    rejected = decode_json(prepared.draft.native_evidence_json)["child_id"]
    assert apply(adapter, prepared, tmp_path / "first", "reject") is None
    second = propose(adapter, package, tmp_path / "second", "second")
    evidence = decode_json(second.draft.native_evidence_json)
    assert rejected not in evidence["population_before"]["programs"]
    assert evidence["parent_id"] != rejected
    assert rejected not in evidence["inspiration_ids"]


def test_activation_preserves_archive_and_fork_isolation(tmp_path):
    def response(request, messages):
        source = SAFE_REFERENCE_SOURCE if "first" in request else SAFE_REFERENCE_SOURCE.replace("    return cache", "    api.now()\n    return cache")
        return {"response": "```python\n" + source + "\n```"}
    adapter, package = setup(tmp_path, response)
    first = propose(adapter, package, tmp_path / "first", "first")
    apply(adapter, first, tmp_path / "first", "accept")
    accepted = adapter.snapshot()
    fork = adapter.fork(accepted, tmp_path / "fork")
    second = propose(fork, package, tmp_path / "second", "second")
    before = decode_json(second.draft.native_evidence_json)["population_before"]
    assert set(before["programs"]) == set(decode_json(accepted.state_json)["population"]["programs"])
    assert decode_json(first.draft.native_evidence_json)["child_id"] in before["programs"]
    assert adapter.snapshot() == accepted == fork.snapshot()
    apply(fork, second, tmp_path / "second", "accept")
    assert adapter.snapshot() == accepted
    assert fork.snapshot() != accepted


def test_source_lock_and_no_generated_python_execution(tmp_path):
    assert verify_upstream()["revision"].startswith("73448d7")
    adapter, package = setup(tmp_path, lambda *_: {"response": "```python\nimport os\ndef handle(request, cache, api):\n    return cache\n```"})
    prepared = propose(adapter, package, tmp_path)
    assert not prepared.draft.native_valid
    assert adapter.snapshot() == prepared.base


def test_unknown_intent_stops_before_regeneration(tmp_path):
    from rsi_lab.errors import OutcomeUnknown
    def interrupted(*_):
        raise KeyboardInterrupt()
    adapter, package = setup(tmp_path, interrupted)
    with pytest.raises(KeyboardInterrupt):
        propose(adapter, package, tmp_path)
    adapter.provider.callback = lambda *_: pytest.fail("Unknown call must not be resent")
    with pytest.raises(OutcomeUnknown):
        propose(adapter, package, tmp_path)
    assert adapter.provider.summary()["dispatched_calls"] == 1


def test_evaluator_plan_requirement_fails_before_evaluation(tmp_path):
    adapter, _ = setup(tmp_path)
    class WithoutPlan:
        def evaluate(self, *args):
            pytest.fail("Capability mismatch must fail before evaluation")
    with pytest.raises(UnsupportedCapability, match="PlannedPublicEvaluator"):
        adapter.propose("unsupported", WithoutPlan())
    assert adapter.provider.summary()["dispatched_calls"] == 0


def test_completed_response_survives_process_hash_seed_change(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    import rsi_lab.subjects.openevolve as module
    adapter, package = setup(tmp_path)
    save = module.save_record
    def crash_after_response(path, value):
        if path.parent.name == "proposals" and not path.name.endswith(".plan.json"):
            raise RuntimeError("simulated crash after confirmed response")
        return save(path, value)
    monkeypatch.setattr(module, "save_record", crash_after_response)
    with pytest.raises(RuntimeError):
        propose(adapter, package, tmp_path)
    monkeypatch.setattr(module, "save_record", save)
    code = '''from pathlib import Path
import sys
from rsi_lab.subjects.openevolve import OpenEvolveAdapter, initial_snapshot
from rsi_lab.tasks.cost_authorization import FOUNDER_SOURCE, API_CONTRACT
from rsi_lab.tasks.platform_cost_authorization import CostAuthorizationTaskPackage
from rsi_lab.engine.platform import PublicTaskEvaluator
from rsi_lab.infra.chat_models import ScriptedChatProvider
p=Path(sys.argv[1])
def no_call(*args): raise AssertionError("response already complete")
provider=ScriptedChatProvider(p/"model.sqlite",no_call)
adapter=OpenEvolveAdapter(p/"active",provider,initial_snapshot(13101,FOUNDER_SOURCE,"cost-authorization-program-v1",API_CONTRACT))
draft=adapter.propose("proposal",PublicTaskEvaluator(adapter,CostAuthorizationTaskPackage(),p/"public",seed=13101))
assert draft.native_valid
assert provider.summary()["dispatched_calls"]==1
'''
    completed = subprocess.run([sys.executable, "-c", code, str(tmp_path)],
        env={**os.environ, "PYTHONHASHSEED": "917", "PYTHONPATH": "src"}, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr


class FixedPublicEvaluator:
    """Isolate the admission treatment without changing scores between arms."""
    plan = {"suite": "fixed-public"}

    def __init__(self, *, child_valid=False, child_units=0):
        self.child_valid, self.child_units = child_valid, child_units
        self.calls = []

    def evaluate(self, snapshot, operation):
        source = snapshot.version.component("policy")["source"]
        self.calls.append((source, operation))
        founder = source == FOUNDER_SOURCE
        valid = founder or self.child_valid
        return {"complete": True, "risk": 0 if valid else 1,
                "synthetic_units": 1 if founder else self.child_units,
                "legal_completed": 1, "legal_total": 1,
                "feedback": {"complete_correct": valid}, "public_trajectories": []}


def fixed_proposal(tmp_path, condition, *, child_valid=False, child_units=0, operation="proposal"):
    provider = ScriptedChatProvider(tmp_path / "model.sqlite", lambda *_:
        {"response": "```python\n" + SAFE_REFERENCE_SOURCE + "\n```"})
    snapshot = initial_snapshot(13101, FOUNDER_SOURCE, "cost-authorization-program-v1", API_CONTRACT,
                                admission_condition=condition)
    adapter = OpenEvolveAdapter(tmp_path / "active", provider, snapshot)
    evaluator = FixedPublicEvaluator(child_valid=child_valid, child_units=child_units)
    draft = adapter.propose(operation, evaluator)
    prepared = prepare_update(adapter, draft, snapshot, update_id=operation, root_id="root-13101",
        protocol_sha256=digest("protocol"), environment_sha256=digest("environment"),
        partition=Partition.DEV, horizon=2, task_plan_sha256=digest(evaluator.plan))
    return adapter, prepared, evaluator


def apply_bound(adapter, prepared, path):
    from rsi_lab.subjects.openevolve import admission_decision
    return ActivationController(path / "activation.sqlite", digest("protocol")).apply(
        adapter, prepared, admission_decision(prepared), environment_sha256=digest("environment"))


def test_default_legacy_public_rejection_and_v2_decisions_are_separate(tmp_path):
    legacy, old, _ = fixed_proposal(tmp_path / "legacy", None)
    gated, treatment, _ = fixed_proposal(tmp_path / "gated", "public_complete_correct")
    upstream, control, _ = fixed_proposal(tmp_path / "upstream", "upstream")
    from rsi_lab.subjects.openevolve import admission_binding, admission_decision
    assert admission_binding(old.base) is None
    assert old.draft.candidate == old.base and not old.draft.native_valid
    assert treatment.draft.native_valid and control.draft.native_valid
    assert admission_decision(old).disposition == admission_decision(treatment).disposition == "reject"
    assert admission_decision(control).disposition == "accept"
    assert apply_bound(legacy, old, tmp_path / "legacy") is None
    assert apply_bound(gated, treatment, tmp_path / "gated") is None
    assert legacy.snapshot() == old.base and gated.snapshot() == treatment.base
    assert apply_bound(upstream, control, tmp_path / "upstream") is not None
    assert upstream.snapshot() == control.draft.candidate


def test_conditions_only_change_admission_not_source_fitness_or_prompt(tmp_path):
    rows = [fixed_proposal(tmp_path / condition, condition) for condition in
            ("public_complete_correct", "upstream")]
    a, b = [decode_json(row[1].draft.native_evidence_json) for row in rows]
    assert a["population_before"] == b["population_before"]
    assert a["public_evaluation"] == b["public_evaluation"]
    assert a["native_prompt"] == b["native_prompt"]
    assert a["proposed_source"] == b["proposed_source"]
    assert a["parent_id"] == b["parent_id"]
    assert a["inspiration_ids"] == b["inspiration_ids"]
    assert a["admission_rng"] == b["admission_rng"]
    assert a["child_before_admission"] == b["child_before_admission"]
    assert a["population_after_native"] == b["population_after_native"]
    assert a["child_before_admission"]["metrics"] == {"combined_score": 0.0}
    assert a["native_admitted"] is b["native_admitted"] is True
    assert not a["platform_accepted"] and b["platform_accepted"]
    assert a["population_after_platform"] is None
    assert b["child_id"] in b["population_after_platform"]["programs"]


def test_v2_rejection_cannot_leak_through_native_only_commit_or_next_proposal(tmp_path):
    from rsi_lab.errors import ValidationError
    adapter, prepared, evaluator = fixed_proposal(tmp_path, "public_complete_correct")
    evidence = decode_json(prepared.draft.native_evidence_json)
    with pytest.raises(ValidationError, match="platform-admitted"):
        apply(adapter, prepared, tmp_path / "incorrect-driver", "accept")
    assert adapter.snapshot() == prepared.base
    assert apply_bound(adapter, prepared, tmp_path / "bound-driver") is None
    second = adapter.propose("next", evaluator)
    following = decode_json(second.native_evidence_json)
    rejected = evidence["child_id"]
    assert rejected not in following["population_before"]["programs"]
    assert following["parent_id"] != rejected
    assert rejected not in following["inspiration_ids"]


@pytest.mark.parametrize("condition", ["public_complete_correct", "upstream"])
def test_v2_archive_only_update_replay_and_branch_isolation(tmp_path, condition):
    adapter, prepared, evaluator = fixed_proposal(tmp_path, condition, child_valid=True, child_units=2)
    evidence = decode_json(prepared.draft.native_evidence_json)
    assert evidence["native_admitted"] and evidence["platform_accepted"]
    assert evidence["archive_only_update"]
    assert evidence["active_program_id"] == evidence["active_program_id_before"]
    assert prepared.draft.candidate.version == prepared.base.version
    assert prepared.draft.candidate.state_json != prepared.base.state_json
    untouched = adapter.fork(prepared.base, tmp_path / "untouched")
    calls = list(evaluator.calls)
    assert adapter.propose("proposal", evaluator) == prepared.draft
    assert evaluator.calls == calls
    assert adapter.provider.summary()["dispatched_calls"] == 1
    first = apply_bound(adapter, prepared, tmp_path / "activation")
    assert apply_bound(adapter, prepared, tmp_path / "activation") == first
    assert untouched.snapshot() == prepared.base
    assert adapter.snapshot() == prepared.draft.candidate
    assert adapter.provider.summary()["dispatched_calls"] == 1


def test_admission_binding_is_checked_and_cannot_change_on_commit(tmp_path):
    from rsi_lab.errors import ValidationError
    from rsi_lab.api import replace_component
    from rsi_lab.subjects.openevolve import admission_decision
    with pytest.raises(ValidationError, match="Unknown.*condition"):
        initial_snapshot(1, FOUNDER_SOURCE, "task", API_CONTRACT, admission_condition="unscored")
    _, prepared, _ = fixed_proposal(tmp_path, "upstream")
    changed = dict(prepared.draft.candidate.version.component("task_contract"))
    changed["admission"] = {"protocol": "openevolve-admission-v2", "condition": "public_complete_correct"}
    candidate = replace(prepared.draft.candidate,
        version=replace_component(prepared.draft.candidate.version, "task_contract", changed))
    # Identity must stay bound to the candidate, so use a new prepared record.
    draft = replace(prepared.draft, candidate=candidate)
    identity = replace(prepared.identity, candidate_sha256=candidate.sha256)
    altered = replace(prepared, draft=draft, identity=identity)
    with pytest.raises(IntegrityError, match="admission condition"):
        admission_decision(altered)


@pytest.mark.parametrize("condition", [None, "public_complete_correct", "upstream"])
def test_identical_source_remains_native_rejection_in_all_conditions(tmp_path, condition):
    source = FOUNDER_SOURCE.strip()
    provider = ScriptedChatProvider(tmp_path / "model.sqlite", lambda *_:
        {"response": "```python\n" + source + "\n```"})
    snapshot = initial_snapshot(13101, source, "cost-authorization-program-v1", API_CONTRACT,
                                admission_condition=condition)
    adapter = OpenEvolveAdapter(tmp_path / "active", provider, snapshot)
    draft = adapter.propose("same-source", FixedPublicEvaluator())
    evidence = decode_json(draft.native_evidence_json)
    assert "identical" in evidence["native_error"]
    assert not draft.native_valid and draft.candidate == snapshot
    assert evidence["native_admission"] == {"attempted": False, "admitted": False, "returned_id": None}
    assert not evidence["platform_accepted"]
    assert adapter.snapshot() == snapshot


def test_condition_binding_does_not_perturb_equally_admitted_history(tmp_path):
    arms = [fixed_proposal(tmp_path / condition, condition, child_valid=True, child_units=2)
            for condition in ("public_complete_correct", "upstream")]
    for adapter, prepared, _ in arms:
        apply_bound(adapter, prepared, adapter.directory / "commit")
    assert arms[0][0].snapshot().state_json == arms[1][0].snapshot().state_json
    next_source = SAFE_REFERENCE_SOURCE.replace("    return cache", "    api.now()\n    return cache")
    evidence = []
    for adapter, _, evaluator in arms:
        adapter.provider.callback = lambda *_: {"response": "```python\n" + next_source + "\n```"}
        evidence.append(decode_json(adapter.propose("second", evaluator).native_evidence_json))
    a, b = evidence
    for key in ("parent_id", "inspiration_ids", "native_prompt", "population_before",
                "child_before_admission", "population_after_native", "admission_rng"):
        assert a[key] == b[key], key
