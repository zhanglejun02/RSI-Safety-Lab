from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from rsi_lab.contracts import (
    AttemptSpec, CandidateBinding, CandidateOrigin, Checkpoint, ComponentRefs,
    CursorFamily, ForkPoint, PairAnchor, PairingMode, QProgram, RandomPlan,
    SProgram, SnapshotManifest, TaskCursor, Version,
)
from rsi_lab.engine.runner import PreparationRunner
from rsi_lab.engine.state import StateStore
from rsi_lab.errors import ConflictError
from rsi_lab.frozen import fixture_bytes
from rsi_lab.infra.artifacts import ArtifactStore
from rsi_lab.infra.journal import Journal
from rsi_lab.infra.models import FakeModelClient
from rsi_lab.infra.snapshots import backup_sqlite
from rsi_lab.interventions.context import ContextKind, ContextPatch, ContextReceipt, MemoryState, frozen_memory
from rsi_lab.interventions.validation import VerificationPatch
from rsi_lab.oracles.workflow import evaluate
from rsi_lab.preparation import PreparationState, ResumeRequest
from rsi_lab.serialization import decode_json
from rsi_lab.subjects.fake import FakeSubject
from rsi_lab.subjects.verification import VerificationReceipt, VerificationRuntime
from rsi_lab.tasks.workflow import Workflow


class RecordingModel(FakeModelClient):
    def __init__(self):
        self.requests = []

    def generate(self, request_id, context_bytes, edit_base):
        self.requests.append((request_id, context_bytes, edit_base))
        return super().generate(request_id, context_bytes, edit_base)


def make_lab(path: Path, *, budget=100):
    path.mkdir()
    store = ArtifactStore(path / "objects")
    world = Workflow(path / "founder.sqlite")
    world.initialize_suite("deployment_not_executed")
    environment = backup_sqlite(world.path, store)
    deployed = replace(Version.from_dict(decode_json(fixture_bytes("version_D.json"))), s=SProgram.LATEST_VALID)
    edit_base = replace(Version.from_dict(decode_json(fixture_bytes("version_E.json"))), s=SProgram.LATEST_VALID)
    manifest = SnapshotManifest(
        ComponentRefs(store.put_json(deployed.agent), store.put_json({"id": deployed.q}),
                      store.put_json({"id": deployed.v}), store.put_json({"id": deployed.s})),
        store.put_json(deployed), store.put_json(edit_base), store.put_json(frozen_memory()),
        store.put_json({"schema_version": 1, "entries": []}), environment,
        store.put_json(RandomPlan(19)), tuple(TaskCursor(family) for family in CursorFamily), "m2-test-v1")
    state = StateStore(path, store)
    snapshot = state.save_snapshot(manifest)
    anchor = PairAnchor("pair", "root", "history", snapshot,
                        store.put_json({"schema_version": 1, "tasks": ["ok"]}),
                        manifest.random_plan, manifest.cursors, PairingMode.PROCESS,
                        ForkPoint.ATTEMPT_START, 0)
    state.register_pair(anchor)
    state.create_branch("main", anchor.pair_id)
    journal = Journal(path / "controller.sqlite")
    journal.create_budget("branch", "main", budget)
    journal.create_budget("pair", "pair", budget)
    spec = AttemptSpec("attempt_1", "run", "pair", "main", "root", "history", "arm",
                       0, 1, store.put_json({"schema_version": 1, "mode": "prepare-only"}),
                       snapshot, CandidateOrigin.CONTROL)
    model = RecordingModel()
    verification = VerificationRuntime(store, journal, path / "verification")
    subject = FakeSubject(store, journal, verification, model)
    runner = PreparationRunner(state, store, subject)
    return SimpleNamespace(path=path, store=store, manifest=manifest, snapshot=snapshot, state=state,
                           anchor=anchor, journal=journal, spec=spec, model=model, subject=subject,
                           runner=runner)


def read(lab, cls, ref):
    return cls.from_dict(decode_json(lab.store.read_bytes(ref)))


def local(lab, result):
    from rsi_lab.contracts import ArtifactRef
    return read(lab, PreparationState, ArtifactRef.from_dict(result["cursor"]["local_state"]))


def prepare(lab, context=ContextPatch(), verification=VerificationPatch(), **kwargs):
    return lab.runner.prepare(lab.spec, context, verification, **kwargs)


def test_four_checkpoints_correspond_to_real_work_without_deployment(tmp_path):
    lab = make_lab(tmp_path / "run")
    deployment_db = lab.state.working_database("main")
    original_environment = deployment_db.read_bytes()
    first = prepare(lab, stop_after=1)
    assert first["cursor"]["checkpoint"] == "post_experience_selection"
    first_state = local(lab, first)
    context = read(lab, ContextReceipt, first_state.context_receipt)
    assert context.after_ids == ("W", "P")
    assert first_state.candidates == ()
    assert lab.model.requests == []
    assert lab.journal.summary()["total_synthetic_units"] == 0

    generated = prepare(lab, stop_after=2)
    generated_state = local(lab, generated)
    assert generated["cursor"]["checkpoint"] == "post_generation"
    assert len(lab.model.requests) == 1
    assert lab.model.requests[0][1] == lab.store.read_bytes(context.actual_context)
    candidate = read(lab, Version, generated_state.candidates[0].artifact)
    assert not candidate.agent.check_scope
    assert candidate.agent.check_confirmation and candidate.agent.check_fresh
    assert generated_state.native_plan == ()
    assert lab.journal.summary()["total_synthetic_units"] == 5

    before_verification = prepare(lab, stop_after=3)
    assert before_verification["cursor"]["checkpoint"] == "pre_verification"
    assert local(lab, before_verification).native_plan == ("ok",)
    assert local(lab, before_verification).verification_receipt is None
    assert lab.journal.exposures() == []

    finished = prepare(lab)
    prepared = local(lab, finished)
    assert finished["phase"] == "prepared"
    assert finished["cursor"]["checkpoint"] == "pre_deploy"
    receipt = read(lab, VerificationReceipt, prepared.verification_receipt)
    assert receipt.complete and len(receipt.executed) == 1
    assert receipt.executed[0].status == "pass"
    trace = decode_json(lab.store.read_bytes(receipt.executed[0].trace))
    assert trace["records"][0]["delivery"] is not None
    assert prepared.native_selected == prepared.candidates[0]
    assert lab.journal.summary()["total_synthetic_units"] == 6
    assert len(lab.journal.costs()) == 2
    assert len(lab.journal.exposures()) == 1
    snapshot = read(lab, SnapshotManifest, prepared.prepared_snapshot)
    binding = read(lab, CandidateBinding, prepared.candidate_binding)
    assert binding.base_snapshot == prepared.prepared_snapshot
    assert snapshot.deployed_version == lab.manifest.deployed_version
    assert snapshot.edit_base_version == lab.manifest.edit_base_version
    assert snapshot.environment == lab.manifest.environment
    assert lab.state.get_branch("main")["head_revision"] == 0
    assert deployment_db.read_bytes() == original_environment
    assert {c.family: c.position for c in snapshot.cursors} == {
        CursorFamily.PUBLIC_DEVELOPMENT: 1, CursorFamily.ADMISSION_PROBE: 0,
        CursorFamily.INDEPENDENT_EVALUATION: 0}
    memory = read(lab, MemoryState, snapshot.memory)
    assert memory.active_ids == ("G", "W", "P", "Z", "delta_attempt_1")
    assert memory.entries[-1].body.endswith("ok:pass")
    assert "risk" not in memory.entries[-1].body


@pytest.mark.parametrize("q,kind,expected_risk", [
    (QProgram.OLD, ContextKind.IDENTITY, 0),
    (QProgram.NEW, ContextKind.IDENTITY, 1),
    (QProgram.NEW, ContextKind.RESTORE, 0),
    (QProgram.NEW, ContextKind.PLACEBO, 1),
    (QProgram.OLD, ContextKind.REMOVE, 1),
])
def test_actual_q_requests_produce_independently_evaluated_candidates(tmp_path, q, kind, expected_risk):
    lab = make_lab(tmp_path / "run")
    result = prepare(lab, ContextPatch(kind), assigned_q=q)
    prepared = local(lab, result)
    candidate = read(lab, Version, prepared.candidates[0].artifact)
    assert candidate.q == q
    # Independently execute the candidate on a fresh suite. This result is never
    # fed back into the subject, its context, or the public verification archive.
    world = Workflow(tmp_path / "independent.sqlite")
    tasks = world.initialize_suite("independent_candidate_check")
    for task in tasks:
        world.execute(candidate.agent, task, f"independent/{task.task_id}")
    evaluation = evaluate(world.records("independent_candidate_check"), tasks)
    assert evaluation.risk == expected_risk
    assert evaluation.utility == 1.0
    assert lab.journal.summary()["total_synthetic_units"] == 6
    assert len(lab.model.requests) == 1


def test_resume_uses_persisted_response_and_never_repeats_generation(tmp_path):
    lab = make_lab(tmp_path / "run")
    original = prepare(lab, stop_after=2)
    candidate_ref = local(lab, original).candidates[0].artifact
    fresh_store = ArtifactStore(lab.path / "objects")
    fresh_state = StateStore(lab.path, fresh_store)
    fresh_journal = Journal(lab.path / "controller.sqlite")
    fresh_model = RecordingModel()
    runner = PreparationRunner(fresh_state, fresh_store,
        FakeSubject(fresh_store, fresh_journal,
                    VerificationRuntime(fresh_store, fresh_journal, lab.path / "verification"), fresh_model))
    result = runner.prepare(lab.spec, ContextPatch(), VerificationPatch())
    assert result["phase"] == "prepared"
    assert local(lab, result).candidates[0].artifact == candidate_ref
    assert fresh_model.requests == []
    before = (fresh_journal.summary(), fresh_state.inspect())
    repeated = runner.prepare(lab.spec, ContextPatch(), VerificationPatch())
    assert repeated == result
    assert before == (fresh_journal.summary(), fresh_state.inspect())


@pytest.mark.parametrize("kind", [ContextKind.RESTORE, ContextKind.PLACEBO])
def test_invalid_context_preserves_receipt_and_stops_before_model(tmp_path, kind):
    lab = make_lab(tmp_path / "run")
    result = prepare(lab, ContextPatch(kind, 15))
    assert result["status"] == "invalid"
    assert result["cursor"]["checkpoint"] == "post_experience_selection"
    prepared = local(lab, result)
    assert not prepared.valid and prepared.candidates == ()
    receipt = read(lab, ContextReceipt, prepared.context_receipt)
    assert receipt.actual_context.size_bytes == 60
    assert receipt.invalid_reason == "restoration_truncated"
    assert lab.model.requests == []
    assert lab.journal.costs() == []
    assert lab.journal.exposures() == []
    assert not any(event["operation_id"] == "attempt_1/generate" for event in lab.journal.events())


def test_budget_failure_retains_completed_generation_without_faking_validation(tmp_path):
    lab = make_lab(tmp_path / "run", budget=5)
    result = prepare(lab)
    assert result["status"] == "budget_exhausted"
    prepared = local(lab, result)
    receipt = read(lab, VerificationReceipt, prepared.verification_receipt)
    assert not receipt.complete
    assert receipt.executed == ()
    assert receipt.not_run[0].reason == "branch_budget_exhausted"
    assert prepared.native_selected is None
    assert prepared.candidate_binding is None
    assert lab.journal.summary()["total_synthetic_units"] == 5
    assert lab.journal.exposures() == []


def test_changed_context_patch_is_rejected_mid_preparation(tmp_path):
    lab = make_lab(tmp_path / "run")
    prepare(lab, stop_after=1)
    with pytest.raises(ConflictError, match="changed"):
        prepare(lab, ContextPatch(ContextKind.RESTORE))
    assert lab.model.requests == []


def test_stage_response_can_replay_after_missing_controller_checkpoint(tmp_path):
    lab = make_lab(tmp_path / "run")
    lab.state.create_attempt(lab.spec)
    lab.state.begin_preparation(lab.spec.attempt_id)
    request = ResumeRequest(lab.spec, None, "attempt_1/checkpoint/0", ContextPatch(),
                            VerificationPatch(), owner_id="main")
    produced = lab.subject.advance(request)
    # Simulate loss after the adapter result/operation settled, before the
    # controller recorded that result. Resume must reuse the settled stage.
    assert lab.state.get_attempt(lab.spec.attempt_id)["cursor"] is None
    completed = prepare(lab)
    assert completed["phase"] == "prepared"
    assert lab.subject.advance(request) == produced
    assert len(lab.model.requests) == 1
    assert lab.journal.summary()["total_synthetic_units"] == 6


@pytest.mark.parametrize("mutation", ["request_id", "context_sha256", "request_sha256", "synthetic_input_units", "q"])
def test_tampered_model_response_cannot_progress_or_erase_actual_cost(tmp_path, mutation):
    lab = make_lab(tmp_path / "run")

    class TamperedModel(FakeModelClient):
        def generate(self, request_id, context_bytes, edit_base):
            response = super().generate(request_id, context_bytes, edit_base)
            if mutation == "q":
                return replace(response, version=replace(response.version, q=QProgram.OLD))
            value = "wrong_request" if mutation == "request_id" else 0 if mutation == "synthetic_input_units" else "0" * 64
            return replace(response, **{mutation: value})

    lab.subject.model = TamperedModel()
    with pytest.raises(ConflictError, match="binding"):
        prepare(lab)
    attempt = lab.state.get_attempt(lab.spec.attempt_id)
    assert attempt["cursor"]["checkpoint"] == "post_experience_selection"
    assert local(lab, attempt).candidates == ()
    assert lab.journal.summary()["total_synthetic_units"] == 5
    assert lab.state.get_branch("main")["head_revision"] == 0
    assert lab.journal.exposures() == []


def test_completed_replay_rejects_different_options(tmp_path):
    lab = make_lab(tmp_path / "run")
    completed = prepare(lab)
    assert completed["phase"] == "prepared"
    with pytest.raises(ConflictError, match="changed"):
        prepare(lab, ContextPatch(ContextKind.RESTORE))
    with pytest.raises(ConflictError, match="changed"):
        prepare(lab, guard_mode="enforce")
    with pytest.raises(ConflictError, match="changed"):
        prepare(lab, assigned_q=QProgram.OLD)
    assert lab.journal.summary()["total_synthetic_units"] == 6
    assert len(lab.model.requests) == 1


def test_preexisting_unknown_generation_keeps_reservation_and_never_retries(tmp_path):
    lab = make_lab(tmp_path / "run")
    selected = prepare(lab, stop_after=1)
    context = read(lab, ContextReceipt, local(lab, selected).context_receipt)
    edit_base = read(lab, Version, lab.manifest.edit_base_version)
    lab.journal.reserve("attempt_1/generate", "branch", "main", 5, "discovery",
        {"request_id": "attempt_1/generate", "context": context.actual_context.to_dict(),
         "edit_base": edit_base.to_dict()}, attempt_id="attempt_1")
    lab.journal.mark_unknown("attempt_1/generate", "Provider accepted request but response was lost")
    result = prepare(lab)
    assert result["status"] == "outcome_unknown"
    assert lab.model.requests == []
    assert local(lab, result).candidates == ()
    assert lab.journal.summary()["outstanding_reserved_units"] == 5
    assert lab.journal.summary()["total_synthetic_units"] == 0
    assert prepare(lab) == result
    assert lab.model.requests == []


def test_generator_timeout_is_unknown_not_a_free_same_identity_retry(tmp_path):
    lab = make_lab(tmp_path / "run")

    class LostResponseModel:
        def __init__(self):
            self.calls = 0

        def generate(self, request_id, context_bytes, edit_base):
            self.calls += 1
            raise TimeoutError("response lost after dispatch")

    model = LostResponseModel()
    lab.subject.model = model
    result = prepare(lab)
    assert result["status"] == "outcome_unknown"
    assert model.calls == 1
    assert lab.journal.get_operation("attempt_1/generate")["status"] == "outcome_unknown"
    assert lab.journal.summary()["outstanding_reserved_units"] == 5
    assert lab.journal.summary()["total_synthetic_units"] == 0
    assert prepare(lab) == result
    assert model.calls == 1

