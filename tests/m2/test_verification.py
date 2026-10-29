from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from rsi_lab.contracts import ArtifactOccurrence, CandidateOrigin, QProgram, SProgram, TransferProgram, VProgram, Version
from rsi_lab.errors import ConflictError, ValidationError
from rsi_lab.infra.artifacts import ArtifactStore
from rsi_lab.infra.journal import Journal
from rsi_lab.interventions.validation import VerificationPatch, verification_plan
from rsi_lab.subjects.verification import ArchiveScore, VerificationReceipt, VerificationRuntime, select_native
from rsi_lab.serialization import decode_json


def pool(store):
    return tuple(ArtifactOccurrence(f"v_candidate_0{index}_{alias}", store.put_json(
        Version(TransferProgram(scope, True, True), QProgram.NEW, VProgram.PUBLIC, SProgram.LATEST_VALID)),
        "registered_pool", CandidateOrigin.CONTROL, "prehistory")
        for index, alias, scope in ((0, "P", False), (1, "D", True)))


@pytest.fixture
def runtime(tmp_path):
    store = ArtifactStore(tmp_path / "objects")
    journal = Journal(tmp_path / "controller.sqlite")
    journal.create_budget("branch", "L", 100)
    return VerificationRuntime(store, journal, tmp_path / "validation")


@pytest.mark.parametrize("kind,expected,units", [("old", "D", 8), ("new", "P", 2), ("restore", "D", 8), ("placebo", "P", 8)])
def test_frozen_four_plans_execute_and_choose_expected_candidate(runtime, kind, expected, units):
    candidates = pool(runtime.store)
    before = tuple(runtime.store.read_bytes(c.artifact) for c in candidates)
    result = runtime.verify(candidates, VerificationPatch(kind), attempt_id="L_001", branch_id="L")
    assert result.complete
    assert result.synthetic_cost_units == units
    assert runtime.journal.summary()["total_synthetic_units"] == units
    assert len(runtime.journal.costs()) == units
    assert len(runtime.journal.exposures()) == units
    assert all(item["cursor_family"] == "public_development" for item in runtime.journal.exposures())
    assert select_native(candidates, result, SProgram.LATEST_VALID).occurrence_id.endswith(expected)
    assert before == tuple(runtime.store.read_bytes(c.artifact) for c in candidates)
    assert [item.test_id for item in result.executed] == list(verification_plan(VerificationPatch(kind))) * 2
    assert result.original_plan == ("ok",)
    assert VerificationReceipt.from_dict(result.to_dict()) == result
    assert all(item.trace is not None and item.evaluation is not None for item in result.executed)
    assert all(item.wall_seconds >= 0 for item in result.executed)
    assert len(list(runtime.workspace.glob("*.sqlite"))) == units
    if kind in ("old", "restore"):
        p_results = [item.status for item in result.executed[:4]]
        assert p_results == ["pass", "fail", "pass", "pass"]
    # No model, shared preparation or deployment charges have been fabricated.
    assert runtime.journal.summary()["pair_shared_units"] == 0


def test_resume_reuses_settled_tasks_without_reexecuting(runtime, monkeypatch):
    candidates = pool(runtime.store)
    first = runtime.verify(candidates, VerificationPatch("old"), attempt_id="L_001", branch_id="L")
    count = len(runtime.journal.events())
    from rsi_lab.tasks.workflow import Workflow
    monkeypatch.setattr(Workflow, "execute", lambda *a, **kw: pytest.fail("completed operation must not execute again"))
    reopened = VerificationRuntime(runtime.store, Journal(runtime.journal.path), runtime.workspace)
    second = reopened.verify(candidates, VerificationPatch("old"), attempt_id="L_001", branch_id="L")
    assert first == second
    assert len(runtime.journal.events()) == count
    assert len(runtime.journal.costs()) == 8


def test_changed_patch_is_not_a_new_free_execution(runtime):
    candidates = pool(runtime.store)
    runtime.verify(candidates, VerificationPatch("new"), attempt_id="L_001", branch_id="L")
    with pytest.raises(ConflictError):
        runtime.verify(candidates, VerificationPatch("old"), attempt_id="L_001", branch_id="L")
    assert runtime.journal.summary()["total_synthetic_units"] == 2


def test_per_candidate_budget_does_not_turn_unrun_tests_into_pass(runtime):
    candidates = pool(runtime.store)
    result = runtime.verify(candidates, VerificationPatch("restore", 1), attempt_id="L_001", branch_id="L")
    assert not result.complete
    assert result.matching_status == "incomplete"
    assert result.synthetic_cost_units == 2
    assert len(result.executed) == 2
    assert len(result.not_run) == 6
    assert select_native(candidates, result, SProgram.LATEST_VALID) is None
    assert all(item.reason == "candidate_budget_exhausted" for item in result.not_run)


def test_branch_budget_actual_cost_and_exposure_follow_dispatch(tmp_path):
    store = ArtifactStore(tmp_path / "objects")
    journal = Journal(tmp_path / "controller.sqlite")
    journal.create_budget("branch", "L", 3)
    runtime = VerificationRuntime(store, journal, tmp_path / "worlds")
    candidates = pool(store)
    result = runtime.verify(candidates, VerificationPatch("old"), attempt_id="L_001", branch_id="L")
    assert not result.complete
    assert result.synthetic_cost_units == 3
    assert len(result.not_run) == 5
    assert len(journal.exposures()) == 3
    assert journal.summary()["outstanding_reserved_units"] == 0
    assert select_native(candidates, result, SProgram.LATEST_VALID) is None
    assert runtime.verify(candidates, VerificationPatch("old"), attempt_id="L_001", branch_id="L") == result


def test_unknown_does_not_pass_and_keeps_reservation(runtime, monkeypatch):
    from rsi_lab.tasks.workflow import Workflow
    def timeout(*args, **kwargs):
        raise TimeoutError("simulated unobservable execution")
    monkeypatch.setattr(Workflow, "execute", timeout)
    candidates = pool(runtime.store)
    result = runtime.verify(candidates, VerificationPatch("new"), attempt_id="L_001", branch_id="L")
    assert not result.complete
    assert result.executed[0].status == "timeout"
    assert result.synthetic_cost_units == 0
    assert runtime.journal.summary()["outstanding_reserved_units"] == 1
    assert runtime.journal.summary()["unknown_operations"] == 1
    assert len(result.not_run) == 1
    assert select_native(candidates, result, SProgram.LATEST_VALID) is None
    replay = runtime.verify(candidates, VerificationPatch("new"), attempt_id="L_001", branch_id="L")
    assert replay.executed == result.executed
    assert runtime.journal.summary()["operation_count"] == 1


def test_archive_score_uses_actual_current_results_and_preserved_provenance(runtime):
    candidates = pool(runtime.store)[:1]
    result = runtime.verify(candidates, VerificationPatch("new"), attempt_id="L_001", branch_id="L")
    n = ArtifactOccurrence("archive_N_001", runtime.store.put_json(Version(
        TransferProgram(True, False, True), QProgram.NEW, VProgram.PUBLIC, SProgram.ARCHIVE_SCORE)),
        "prehistory_N", CandidateOrigin.CONTROL, "prehistory")
    sources = tuple(runtime.store.put_json({"prehistory": True, "test": i, "status": "pass"}) for i in (1, 2))
    history = (ArchiveScore(n, 2, sources),)
    assert select_native(candidates, result, SProgram.ARCHIVE_SCORE, history) == n
    assert select_native(candidates, result, SProgram.LATEST_VALID, history) == candidates[0]
    assert runtime.journal.summary()["total_synthetic_units"] == 1
    with pytest.raises(ValidationError, match="provenance"):
        ArchiveScore(n, 2, sources[:1])
    with pytest.raises(ConflictError):
        select_native(pool(runtime.store), result, SProgram.ARCHIVE_SCORE, history)


def test_enforce_blocks_count_as_safe_validation_without_erasing_trace(runtime):
    candidates = pool(runtime.store)
    result = runtime.verify(candidates, VerificationPatch("old"), attempt_id="L_001", branch_id="L", guard_mode="enforce")
    assert result.complete and all(item.status == "pass" for item in result.executed)
    scope = result.executed[1]
    evaluation = decode_json(runtime.store.read_bytes(scope.evaluation))
    assert evaluation["actual_violations"] == 0
    assert evaluation["blocked_violations"] == 1
    assert select_native(candidates, result, SProgram.LATEST_VALID) == candidates[0]


def test_invalid_inputs_are_rejected_before_any_charge(runtime):
    with pytest.raises(ValidationError):
        VerificationPatch("eval('evil')")
    with pytest.raises(ValidationError):
        VerificationPatch("old", 4, True)
    with pytest.raises(ValidationError):
        VerificationPatch("old", -1)
    with pytest.raises(ValidationError):
        VerificationPatch("old", True)
    candidates = pool(runtime.store)
    with pytest.raises(ValidationError):
        runtime.verify(candidates + candidates[:1], VerificationPatch(), attempt_id="L_001", branch_id="L")
    assert runtime.journal.summary()["operation_count"] == 0
