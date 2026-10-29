from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import sqlite3

import pytest

from rsi_lab.contracts import (
    ArtifactRef,
    AttemptSpec,
    CandidateOrigin,
    ComponentRefs,
    CursorFamily,
    ForkPoint,
    PairAnchor,
    PairingMode,
    PrepareCursor,
    RandomPlan,
    SProgram,
    SnapshotManifest,
    TaskCursor,
    Version,
)
from rsi_lab.engine.commit import LifecycleController
from rsi_lab.engine.runner import PreparationRunner
from rsi_lab.engine.state import StateStore
from rsi_lab.errors import (
    ConflictError,
    IntegrityError,
    PermissionDenied,
    ValidationError,
)
from rsi_lab.frozen import fixture_bytes
from rsi_lab.infra.artifacts import ArtifactStore
from rsi_lab.infra.journal import Journal
from rsi_lab.infra.snapshots import backup_sqlite
from rsi_lab.interventions.context import ContextPatch, MemoryState, frozen_memory
from rsi_lab.interventions.validation import VerificationPatch
from rsi_lab.lifecycle import AuditDecision, Resolution
from rsi_lab.preparation import PreparationState
from rsi_lab.serialization import canonical_bytes, decode_json
from rsi_lab.subjects.fake import FakeSubject
from rsi_lab.subjects.verification import VerificationRuntime
from rsi_lab.tasks.workflow import Workflow


@pytest.fixture
def lab(tmp_path):
    store = ArtifactStore(tmp_path / "objects")
    world = Workflow(tmp_path / "founder.sqlite")
    deployed = replace(
        Version.from_dict(decode_json(fixture_bytes("version_D.json"))),
        s=SProgram.LATEST_VALID,
    )
    edit = replace(
        Version.from_dict(decode_json(fixture_bytes("version_E.json"))),
        s=SProgram.LATEST_VALID,
    )
    components = ComponentRefs(
        store.put_json(deployed.agent),
        store.put_json({"id": deployed.q}),
        store.put_json({"id": deployed.v}),
        store.put_json({"id": deployed.s}),
    )
    manifest = SnapshotManifest(
        components,
        store.put_json(deployed),
        store.put_json(edit),
        store.put_json(frozen_memory()),
        store.put_json({"schema_version": 1, "entries": []}),
        backup_sqlite(world.path, store),
        store.put_json(RandomPlan(1)),
        tuple(TaskCursor(f) for f in CursorFamily),
        "m3-test",
    )
    state = StateStore(tmp_path, store)
    snapshot = state.save_snapshot(manifest)
    pair = PairAnchor(
        "pair",
        "root",
        "history",
        snapshot,
        store.put_json({"tasks": ["ok"]}),
        manifest.random_plan,
        manifest.cursors,
        PairingMode.PROCESS,
        ForkPoint.ATTEMPT_START,
        0,
    )
    state.register_pair(pair)
    state.create_branch("main", "pair")
    state.create_branch("probe", "pair", role="probe")
    journal = Journal(tmp_path / "controller.sqlite")
    for branch in ("main", "probe"):
        journal.create_budget("branch", branch, 100)
    spec = AttemptSpec(
        "main_1",
        "run",
        "pair",
        "main",
        "root",
        "history",
        "old",
        0,
        1,
        store.put_json({"m3": True}),
        snapshot,
        CandidateOrigin.CONTROL,
    )
    subject = FakeSubject(
        store, journal, VerificationRuntime(store, journal, tmp_path / "validation")
    )
    runner = PreparationRunner(state, store, subject)
    runner.prepare(spec, ContextPatch(), VerificationPatch())
    controller = LifecycleController(state, store, journal, ("main",))
    return SimpleNamespace(
        path=tmp_path,
        store=store,
        state=state,
        journal=journal,
        manifest=manifest,
        spec=spec,
        runner=runner,
        controller=controller,
    )


def decision(lab, verdict="reject", controller=None, attempt="main_1"):
    controller = controller or lab.controller
    view = controller.view(attempt)
    evidence = lab.store.put_json(
        {"binding": view.binding.to_dict(), "policy_version": "test.policy.v1"}
    )
    return AuditDecision(view.binding, "test.policy.v1", verdict, evidence)


def read(lab, cls, ref):
    return cls.from_dict(decode_json(lab.store.read_bytes(ref)))


@pytest.mark.parametrize(
    "mode,added_active,quarantined",
    [("retain", True, False), ("discard", False, False), ("quarantine", False, True)],
)
def test_rejection_keeps_code_preserves_preparation_and_recovers_new_memory(
    lab, mode, added_active, quarantined
):
    before_cost = lab.journal.summary()["total_synthetic_units"]
    result = lab.controller.complete("main_1", decision(lab), memory_mode=mode)
    target = read(lab, SnapshotManifest, result.snapshot)
    memory = read(lab, MemoryState, target.memory)
    assert target.deployed_version == lab.manifest.deployed_version
    assert target.edit_base_version == lab.manifest.edit_base_version
    assert ("delta_main_1" in memory.active_ids) == added_active
    assert ("delta_main_1" in memory.quarantined_ids) == quarantined
    assert memory.active_ids[:4] == ("G", "W", "P", "Z")
    assert (
        len(memory.entries) == 5
    )  # discard excludes retrieval, never deletes evidence
    assert target.cursors[0].position == 1
    assert decode_json(lab.store.read_bytes(target.archive))["entries"]
    assert result.head_revision == 1
    assert lab.state.get_attempt("main_1")["phase"] == "committed"
    assert lab.journal.summary()["total_synthetic_units"] == before_cost == 6
    assert lab.controller.complete("main_1", decision(lab), memory_mode=mode) == result
    with pytest.raises(ConflictError, match="policy"):
        lab.controller.resolve(
            "main_1", memory_mode="discard" if mode != "discard" else "retain"
        )


def test_accept_deploys_only_bound_candidate(lab):
    view = lab.controller.view("main_1")
    result = lab.controller.complete(
        "main_1", decision(lab, "accept"), memory_mode="retain"
    )
    target = read(lab, SnapshotManifest, result.snapshot)
    assert (
        target.deployed_version
        == target.edit_base_version
        == view.binding.candidate_occurrence.artifact
    )
    assert target.deployed_version != lab.manifest.deployed_version
    version = read(lab, Version, target.deployed_version)
    assert lab.store.read_bytes(target.components.agent) == lab.store.read_bytes(
        lab.store.put_json(version.agent)
    )


def test_inconclusive_remains_distinct_and_keeps_current(lab):
    result = lab.controller.complete("main_1", decision(lab, "inconclusive"))
    assert lab.controller.get_decision("main_1").verdict == "inconclusive"
    assert (
        read(lab, SnapshotManifest, result.snapshot).deployed_version
        == lab.manifest.deployed_version
    )


def test_decision_rejects_wrong_candidate_base_protocol_or_evidence(lab):
    valid = decision(lab)
    for binding in (
        replace(valid.binding, attempt_id="unknown"),
        replace(valid.binding, base_snapshot=lab.spec.source_snapshot),
        replace(valid.binding, protocol=lab.store.put_json({"wrong": True})),
        replace(
            valid.binding,
            candidate_occurrence=replace(
                valid.binding.candidate_occurrence, occurrence_id="probe_generated"
            ),
        ),
    ):
        with pytest.raises((ConflictError, ValueError)):
            lab.controller.record_decision(replace(valid, binding=binding))
    evidence = lab.store.put_json(
        {"binding": valid.binding.to_dict(), "policy_version": "wrong"}
    )
    with pytest.raises(ConflictError, match="evidence"):
        lab.controller.record_decision(replace(valid, evidence=evidence))
    assert lab.state.get_attempt("main_1")["phase"] == "prepared"


def test_completion_checks_attempt_argument_before_recording_decision(lab):
    with pytest.raises(ConflictError, match="different attempt"):
        lab.controller.complete("another_attempt", decision(lab))
    assert lab.state.get_attempt("main_1")["phase"] == "prepared"
    assert lab.controller.get_decision("main_1") is None


@pytest.mark.parametrize("erase", [False, True])
def test_persisted_selection_must_match_original_occurrence_provenance(lab, erase):
    view = lab.controller.view("main_1")
    cursor = PrepareCursor.from_dict(lab.state.get_attempt("main_1")["cursor"])
    prepared = read(lab, PreparationState, cursor.local_state)
    occurrence = (
        None
        if erase
        else replace(prepared.native_selected, occurrence_id="probe_relabel")
    )
    binding = (
        None
        if erase
        else lab.store.put_json(replace(view.binding, candidate_occurrence=occurrence))
    )
    forged = replace(prepared, native_selected=occurrence, candidate_binding=binding)
    with lab.state._db(True) as conn:
        conn.execute(
            "UPDATE attempts SET cursor_json=? WHERE attempt_id=?",
            (
                canonical_bytes(
                    replace(cursor, local_state=lab.store.put_json(forged))
                ),
                "main_1",
            ),
        )
    with pytest.raises(IntegrityError, match="provenance"):
        lab.controller.view("main_1")


def test_role_and_explicit_allowlist_both_required(lab):
    wrong_role = LifecycleController(
        lab.state, lab.store, lab.journal, ("main",), role="probe"
    )
    with pytest.raises(PermissionDenied):
        wrong_role.view("main_1")
    wrong_branch = LifecycleController(
        lab.state, lab.store, lab.journal, ("probe",), role="main"
    )
    with pytest.raises(PermissionDenied):
        wrong_branch.view("main_1")


def test_worker_cannot_change_registered_target_or_candidate(lab):
    lab.controller.record_decision(decision(lab))
    command = lab.controller.resolve("main_1")
    for changed in (
        replace(command, target_branch_id="probe"),
        replace(command, target_manifest=lab.spec.source_snapshot),
        replace(
            command,
            approved_candidate=replace(
                command.approved_candidate, occurrence_id="different"
            ),
        ),
    ):
        with pytest.raises(PermissionDenied):
            lab.controller.materialize(changed)


@pytest.mark.parametrize("tamper", ["environment", "manifest", "extra", "symlink"])
def test_actual_staging_is_rechecked_before_commit(lab, tamper):
    lab.controller.record_decision(decision(lab))
    command = lab.controller.resolve("main_1")
    receipt = lab.controller.materialize(command)
    directory = lab.controller.materializer.path(command)
    if tamper == "extra":
        (directory / "unapproved").write_text("bad")
    elif tamper == "symlink":
        path = directory / "environment.sqlite"
        path.unlink()
        path.symlink_to(lab.path / "founder.sqlite")
    else:
        path = directory / (
            "environment.sqlite" if tamper == "environment" else "manifest.json"
        )
        path.chmod(0o600)
        path.write_bytes(b"changed")
    with pytest.raises(IntegrityError):
        lab.controller.commit(command, receipt)
    assert lab.state.get_branch("main")["head_revision"] == 0
    assert lab.state.get_attempt("main_1")["phase"] == "materialized"


def test_stale_revision_cannot_commit(lab):
    lab.controller.record_decision(decision(lab))
    command = lab.controller.resolve("main_1")
    receipt = lab.controller.materialize(command)
    with lab.state._db(True) as conn:
        conn.execute("UPDATE branches SET head_revision=1 WHERE branch_id='main'")
    with pytest.raises(ConflictError, match="Stale"):
        lab.controller.commit(command, receipt)
    assert lab.controller.get_receipt("main_1") is None


def test_after_commit_recovery_finishes_workspace_without_recommitting(lab):
    def fault(point):
        if point == "after_commit":
            raise RuntimeError("response lost")

    broken = LifecycleController(
        lab.state, lab.store, lab.journal, ("main",), fault_hook=fault
    )
    verdict = decision(lab, "accept")
    with pytest.raises(RuntimeError):
        broken.complete("main_1", verdict)
    assert lab.state.get_attempt("main_1")["phase"] == "committed"
    recovered = LifecycleController(lab.state, lab.store, lab.journal, ("main",))
    receipt = recovered.complete("main_1", verdict)
    assert receipt.head_revision == 1
    with lab.state._db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == 1
        assert (
            conn.execute("SELECT installed FROM working_installations").fetchone()[0]
            == 1
        )


def test_old_commit_replay_preserves_later_world_effects(lab):
    verdict = decision(lab, "accept")
    result = lab.controller.complete("main_1", verdict)
    world = Workflow(lab.state.working_database("main"))
    tasks = world.initialize_suite("later")
    deployed = read(
        lab, Version, read(lab, SnapshotManifest, result.snapshot).deployed_version
    )
    world.execute(deployed.agent, tasks[0], "later/ok")
    env = backup_sqlite(world.path, lab.store)
    head = read(lab, SnapshotManifest, result.snapshot)
    cursors = tuple(
        (
            replace(c, position=c.position + 1)
            if c.family == CursorFamily.PUBLIC_DEVELOPMENT
            else c
        )
        for c in head.cursors
    )
    updated = lab.controller.record_runtime_snapshot("main", env, cursors)
    assert updated != result.snapshot
    assert lab.controller.complete("main_1", verdict) == result
    assert len(world.records("later")) == 1
    assert ArtifactRef.from_dict(lab.state.get_branch("main")["head"]) == updated
    assert lab.state.get_branch("main")["head_revision"] == 1


def test_runtime_cannot_advance_other_role_cursor_or_fake_environment(lab):
    result = lab.controller.complete("main_1", decision(lab))
    head = read(lab, SnapshotManifest, result.snapshot)
    wrong = tuple(
        replace(c, position=1) if c.family == CursorFamily.INDEPENDENT_EVALUATION else c
        for c in head.cursors
    )
    with pytest.raises(PermissionDenied):
        lab.controller.record_runtime_snapshot("main", head.environment, wrong)
    with pytest.raises(IntegrityError):
        lab.controller.record_runtime_snapshot(
            "main", lab.store.put_json({"not": "a database"}), head.cursors
        )


def test_runtime_adds_only_own_family_namespaces_and_retains_existing(lab):
    result = lab.controller.complete("main_1", decision(lab))
    head = read(lab, SnapshotManifest, result.snapshot)
    appended = head.cursors + (
        TaskCursor(CursorFamily.PUBLIC_DEVELOPMENT, 1, namespace="main_1/deployment"),
    )
    environment = backup_sqlite(lab.state.working_database("main"), lab.store)
    target = lab.controller.record_runtime_snapshot("main", environment, appended)
    assert read(lab, SnapshotManifest, target).cursors == appended
    with pytest.raises(ValidationError, match="remove"):
        lab.controller.record_runtime_snapshot("main", head.environment, head.cursors)
    with pytest.raises(PermissionDenied, match="another role"):
        lab.controller.record_runtime_snapshot(
            "main",
            head.environment,
            appended
            + (
                TaskCursor(
                    CursorFamily.ADMISSION_PROBE, 1, namespace="other/deployment"
                ),
            ),
        )


def test_not_applicable_has_no_fabricated_decision(lab):
    # A full validation rejects the generated P before native selection.
    spec = replace(lab.spec, attempt_id="none_1", branch_id="probe")
    lab.runner.prepare(spec, ContextPatch(), VerificationPatch("old"))
    controller = LifecycleController(
        lab.state, lab.store, lab.journal, ("probe",), role="probe"
    )
    assert controller.view("none_1") is None
    receipt = controller.complete_no_candidate("none_1")
    assert controller.get_decision("none_1") is None
    command = controller.get_command("none_1")
    assert read(lab, Resolution, command.resolution).verdict == "not_applicable"
    assert (
        read(lab, SnapshotManifest, receipt.snapshot).deployed_version
        == lab.manifest.deployed_version
    )


@pytest.mark.parametrize("role", ["main", "probe"])
def test_failed_validation_preserves_only_actual_exposure_cursor(
    lab, monkeypatch, role
):
    branch = "failed_" + role
    lab.state.create_branch(branch, "pair", role=role)
    lab.journal.create_budget("branch", branch, 100)
    spec = replace(lab.spec, attempt_id=branch + "_1", branch_id=branch)
    controller = LifecycleController(
        lab.state, lab.store, lab.journal, (branch,), role=role
    )
    family = (
        CursorFamily.PUBLIC_DEVELOPMENT
        if role == "main"
        else CursorFamily.ADMISSION_PROBE
    )
    purpose = "discovery" if role == "main" else "online_audit"
    original_execute = Workflow.execute

    def lose_response(
        world, program, task, effect_id, guard_mode="observe", *, purpose="execution"
    ):
        record = original_execute(
            world, program, task, effect_id, guard_mode, purpose=purpose
        )
        if purpose == "validation":
            raise TimeoutError("validation effect occurred before response was lost")
        return record

    monkeypatch.setattr(Workflow, "execute", lose_response)
    before = lab.state.load_snapshot(spec.source_snapshot)
    environment_before = backup_sqlite(lab.state.working_database(branch), lab.store)
    attempt = lab.runner.prepare(
        spec, ContextPatch(), VerificationPatch(), purpose=purpose, cursor_family=family
    )
    assert attempt["status"] == "outcome_unknown"
    prepared = read(
        lab, PreparationState, PrepareCursor.from_dict(attempt["cursor"]).local_state
    )
    assert not prepared.valid
    with pytest.raises(ConflictError):
        controller.view(spec.attempt_id)
    result = controller.record_failed_preparation(spec.attempt_id)
    target = lab.state.load_snapshot(result)
    assert replace(target, cursors=before.cursors) == before
    namespace = "main" if role == "main" else spec.attempt_id + "/validation"
    assert (
        next(
            c for c in target.cursors if c.family == family and c.namespace == namespace
        ).position
        == 1
    )
    assert (
        backup_sqlite(lab.state.working_database(branch), lab.store)
        == environment_before
    )
    assert lab.state.get_branch(branch)["head_revision"] == 0
    assert lab.journal.summary("branch", branch)["total_synthetic_units"] == 5
    assert lab.journal.summary("branch", branch)["outstanding_reserved_units"] == 1
    with lab.state._db() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM decisions WHERE attempt_id=?", (spec.attempt_id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM commands WHERE attempt_id=?", (spec.attempt_id,)
            ).fetchone()[0]
            == 0
        )
    assert controller.record_failed_preparation(spec.attempt_id) == result
    assert ArtifactRef.from_dict(lab.state.get_branch(branch)["head"]) == result
    # A later cursor update must survive replay of this old terminal attempt.
    later = tuple(
        (
            replace(c, position=c.position + 1)
            if c.family == family and c.namespace == namespace
            else c
        )
        for c in target.cursors
    )
    later_ref = controller.record_runtime_snapshot(branch, environment_before, later)
    assert controller.record_failed_preparation(spec.attempt_id) == result
    assert ArtifactRef.from_dict(lab.state.get_branch(branch)["head"]) == later_ref


def test_failed_preparation_progress_rejects_live_or_foreign_attempt(lab):
    with pytest.raises(ConflictError, match="terminal failed"):
        lab.controller.record_failed_preparation("main_1")
    probe = LifecycleController(
        lab.state, lab.store, lab.journal, ("probe",), role="probe"
    )
    with pytest.raises(PermissionDenied):
        probe.record_failed_preparation("main_1")
