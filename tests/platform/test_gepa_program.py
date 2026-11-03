"""Real pinned GEPA core, control reflection only; never paid model calls."""
from dataclasses import replace

import pytest
pytest.importorskip("gepa")

from rsi_lab.api import PublicTask
from rsi_lab.auditing.platform import AdmissionDecision
from rsi_lab.contracts import CandidateOrigin, Partition
from rsi_lab.engine.activation import ActivationController
from rsi_lab.engine.platform import PublicTaskEvaluator, prepare_update
from rsi_lab.errors import ConflictError, IntegrityError, OutcomeUnknown, ValidationError
from rsi_lab.infra.chat_models import ScriptedChatProvider
from rsi_lab.serialization import decode_json, digest
from rsi_lab.subjects.gepa_program import GepaProgramAdapter, initial_snapshot, _reflection_example, _fitness
from rsi_lab.tasks import stress_bench as bench


def setup(tmp_path, domain="authorization", source=None, callback=None):
    module = bench.backend(domain)
    package = bench.StressTaskPackage(domain, levels=(1,))
    provider = ScriptedChatProvider(tmp_path / "model.sqlite", callback or (lambda *_:
        {"text": "```python\n" + (module.SAFE_SOURCE if source is None else source) + "\n```"}))
    base = initial_snapshot(17, module.FOUNDER_SOURCE, package.tool_protocol, module.CONTRACT)
    adapter = GepaProgramAdapter(tmp_path / "system", provider, base)
    evaluator = PublicTaskEvaluator(adapter, package, tmp_path / "public", seed=17)
    return module, package, provider, base, adapter, evaluator


def apply(tmp_path, adapter, base, draft, evaluator, accept):
    prepared = prepare_update(adapter, draft, base, update_id="update", root_id="root",
        protocol_sha256=digest("test-gepa-program"), environment_sha256=digest({}),
        partition=Partition.DEV, horizon=3, task_plan_sha256=digest(evaluator.plan))
    decision = AdmissionDecision(prepared.identity, digest(draft.native_evidence_json),
        "test-gepa-program", "accept" if accept else "reject", "declared_public_gate")
    return ActivationController(tmp_path / "activation.sqlite", prepared.identity.protocol_sha256).apply(
        adapter, prepared, decision, environment_sha256=digest({}))


@pytest.mark.parametrize("domain", bench.DOMAINS)
def test_native_gepa_source_selection_activation_replay_and_isolation(tmp_path, domain, monkeypatch):
    module, package, provider, base, adapter, evaluator = setup(tmp_path, domain)
    draft = adapter.propose("update", evaluator)
    evidence = decode_json(draft.native_evidence_json)
    assert draft.origin is CandidateOrigin.CONTROL and draft.native_valid
    assert evidence["optimizer"] == "gepa.optimize" and evidence["metric_calls"] == 4
    assert evidence["native_admitted"] and evidence["platform_accepted"] and evidence["source_changed"]
    assert evidence["pool_inheritance"] == "activated_source_only"
    assert evidence["proposed_source"] == module.SAFE_SOURCE.strip()
    assert len(evidence["native_pool"]) == 2 and len(evidence["reflection_inputs"]) == 1
    assert evidence["reflection_inputs"][0]["parent_source_present"]
    assert evidence["reflection_inputs"][0]["parent_source_sha256"] == digest(module.FOUNDER_SOURCE)
    assert adapter.snapshot() == base
    assert provider.summary()["dispatched_calls"] == 1
    with monkeypatch.context() as patch:
        patch.setattr(provider, "generate", lambda *_: pytest.fail("restored proposal generated again"))
        restored = GepaProgramAdapter(tmp_path / "system", provider, base)
        assert restored.propose("update", evaluator) == draft
    untouched = adapter.fork(base, tmp_path / "untouched")
    assert apply(tmp_path, adapter, base, draft, evaluator, True)
    assert adapter.snapshot() == draft.candidate and untouched.snapshot() == base
    assert evaluator.verify_cached() == 4
    changed_plan = PublicTaskEvaluator(untouched, package, tmp_path / "different-public", seed=18)
    with pytest.raises(ConflictError, match="different inputs"):
        GepaProgramAdapter(tmp_path / "system", provider, base).propose("update", changed_plan)


def test_native_rejection_keeps_generated_source_evidence_but_not_inherited(tmp_path):
    _, _, provider, base, adapter, evaluator = setup(tmp_path, source=bench.REJECT_SOURCE)
    draft = adapter.propose("update", evaluator)
    evidence = decode_json(draft.native_evidence_json)
    assert not draft.native_valid and draft.candidate == base
    assert evidence["proposed_source"] == bench.REJECT_SOURCE.strip()
    assert not evidence["native_admitted"] and not evidence["platform_accepted"]
    assert len(evidence["native_pool"]) == 1 and evidence["metric_calls"] == 3
    assert evidence["public_evaluations"][2]["combined_score"] == 0
    assert apply(tmp_path, adapter, base, draft, evaluator, False) is None
    assert adapter.snapshot() == base and provider.summary()["dispatched_calls"] == 1


def test_bounded_validation_failure_is_recorded_and_not_executed(tmp_path):
    _, _, provider, base, adapter, evaluator = setup(tmp_path, source="import os\n")
    draft = adapter.propose("update", evaluator)
    evidence = decode_json(draft.native_evidence_json)
    assert not draft.native_valid and draft.candidate == base
    assert evidence["proposed_source"] == "import os"
    assert evidence["public_evaluations"][2]["bounded_validation_error"]
    assert evidence["public_candidate"]["risk"] is None
    assert evaluator.verify_cached() == 2
    assert provider.summary()["dispatched_calls"] == 1


def test_platform_rejection_does_not_inherit_native_accepted_source(tmp_path):
    _, _, _, base, adapter, evaluator = setup(tmp_path)
    draft = adapter.propose("update", evaluator)
    assert draft.native_valid
    assert apply(tmp_path, adapter, base, draft, evaluator, False) is None
    assert adapter.snapshot() == base


def test_upstream_tie_acceptance_does_not_bypass_platform_public_gate(tmp_path):
    _, _, _, base, adapter, _ = setup(tmp_path)
    class IncompleteEvaluator:
        plan = {"registered": "incomplete-public-control"}
        def evaluate(self, candidate, operation_id):
            return {"complete": False, "risk": None, "synthetic_units": 0,
                "legal_completed": 0, "legal_total": 1, "public_trajectories": [],
                "feedback": {"complete_correct": False}}
    evaluator = IncompleteEvaluator()
    draft = adapter.propose("update", evaluator)
    evidence = decode_json(draft.native_evidence_json)
    assert draft.native_valid and evidence["native_admitted"]
    assert not evidence["platform_accepted"]
    with pytest.raises(IntegrityError, match="platform-admissible"):
        apply(tmp_path, adapter, base, draft, evaluator, True)
    assert adapter.snapshot() == base


def test_unknown_reflection_is_not_retried(tmp_path):
    dispatched = []
    def unknown(*args):
        dispatched.append(1)
        raise TimeoutError("unknown remote outcome")
    _, _, provider, base, adapter, evaluator = setup(tmp_path, callback=unknown)
    for _ in range(2):
        with pytest.raises(OutcomeUnknown):
            adapter.propose("update", evaluator)
    assert len(dispatched) == 1 and provider.summary()["dispatched_calls"] == 1
    assert adapter.snapshot() == base


def test_protocol_mismatch_and_bound_feedback_projection(tmp_path):
    _, _, _, _, adapter, _ = setup(tmp_path)
    with pytest.raises(ValidationError, match="protocol mismatch"):
        adapter.run_task(PublicTask("wrong", "{}", "another-protocol"), None, "execution")
    raw = {"complete": True, "risk": 0, "synthetic_units": 7, "legal_completed": 1,
        "legal_total": 1, "feedback": {"complete_correct": True},
        "public_trajectories": [{"request": "x" * 7000}, {"request": "y" * 7000}],
        "private_independent_label": "NEVER_INCLUDE"}
    projection = _reflection_example(raw)
    assert len(projection["Generated Outputs"]["public_execution"]) == 1
    assert projection["Generated Outputs"]["available_rows"] == 2
    assert "NEVER_INCLUDE" not in str(projection)
    from rsi_lab.subjects.openevolve import _fitness as oe_fitness
    assert _fitness(raw) == oe_fitness(raw)["combined_score"] == 0.125


def test_identical_rewrite_has_no_fake_state_or_source_change(tmp_path):
    source = bench.backend("authorization").FOUNDER_SOURCE.strip()
    _, _, _, base, adapter, evaluator = setup(tmp_path, source=source)
    # Normalize founder source too: byte equality is the declared identity.
    base = initial_snapshot(17, source, adapter.describe().capabilities.tool_protocols[0],
        base.version.component("task_contract")["text"])
    adapter = GepaProgramAdapter(tmp_path / "normalized", adapter.provider, base)
    evaluator = PublicTaskEvaluator(adapter, evaluator.task_package, tmp_path / "normalized-public", seed=17)
    draft = adapter.propose("update", evaluator)
    evidence = decode_json(draft.native_evidence_json)
    assert evidence["native_admitted"] and not evidence["source_changed"]
    assert not evidence["state_changed"] and draft.candidate == base
    assert evidence["proposed_source"] == source
