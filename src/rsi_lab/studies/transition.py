"""Appendix B's frozen transition, driven by the actual controller.

This study intentionally measures no deployed risk. It stops after round-two
experience selection, before the next generation intention or reservation.
"""

import sqlite3
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from rsi_lab import __version__
from rsi_lab.auditing.baselines import EvidenceSession, RejectAllAdmission
from rsi_lab.contracts import (
    ArtifactOccurrence,
    ArtifactRef,
    AttemptSpec,
    CandidateOrigin,
    Checkpoint,
    ComponentRefs,
    CursorFamily,
    ForkPoint,
    PairAnchor,
    PairingMode,
    PrepareCursor,
    RandomPlan,
    SnapshotManifest,
    StrictRecord,
    TaskCursor,
    Version,
)
from rsi_lab.engine.commit import LifecycleController
from rsi_lab.engine.demo import _publish_json
from rsi_lab.engine.runner import PreparationRunner
from rsi_lab.engine.state import StateStore
from rsi_lab.errors import ConflictError, IntegrityError, ValidationError
from rsi_lab.frozen import fixture_bytes, implementation_fingerprint, provenance
from rsi_lab.infra.artifacts import ArtifactStore, _checked_path
from rsi_lab.infra.journal import Journal
from rsi_lab.infra.snapshots import backup_sqlite
from rsi_lab.interventions.context import (
    ContextPatch,
    ContextReceipt,
    MemoryState,
    frozen_memory,
)
from rsi_lab.interventions.validation import VerificationPatch
from rsi_lab.lifecycle import AuditDecision, CommitReceipt, MaterializeCommand
from rsi_lab.preparation import PreparationState, ResumeRequest
from rsi_lab.serialization import canonical_bytes, decode_json, digest
from rsi_lab.subjects.fake import FakeSubject
from rsi_lab.subjects.verification import (
    ArchiveScore,
    VerificationReceipt,
    VerificationRuntime,
)
from rsi_lab.tasks.workflow import Workflow

PAIR_ID = "golden_pair_01"
GENERATION_ID = "golden_pair_01/generate/1"


@dataclass(frozen=True)
class TransitionManifest(StrictRecord):
    run_id: str
    protocol: ArtifactRef
    implementation_sha256: str
    sqlite_version: str
    frozen_sha256: str
    stage: str = "M3"
    study: str = "m3_transition"
    execution_kind: str = "controlled_transition_fixture"
    arbitrary_code_execution: bool = False
    hard_isolation_verified: bool = False
    schema_version: int = 1

    def validate(self):
        if (
            self.stage != "M3"
            or self.study != "m3_transition"
            or self.execution_kind != "controlled_transition_fixture"
            or self.arbitrary_code_execution
            or self.hard_isolation_verified
        ):
            raise ValidationError("Unsupported transition manifest")


def _protocol():
    return {
        "schema_version": 1,
        "study": "m3_transition",
        "frozen": provenance(),
        "implementation_sha256": implementation_fingerprint(),
        "sqlite_version": sqlite3.sqlite_version,
        "candidate_origin": "control",
        "policy": "reject_all",
        "recovery": "quarantine",
        "guard_mode": "observe",
        "stopping_point": "round2_post_experience_selection",
        "independent_evaluation": False,
    }


def _open_run(output: Path, *, create=False):
    output = _checked_path(Path(output))
    if create and not output.exists():
        output.mkdir(parents=True)
    manifest_path = _checked_path(output / "run_manifest.json")
    if create and not manifest_path.exists():
        if any(output.iterdir()):
            raise ConflictError(f"Unregistered nonempty output: {output}")
        store = ArtifactStore(output / "objects")
        protocol = _protocol()
        manifest = TransitionManifest(
            "run_" + uuid.uuid4().hex,
            store.put_json(protocol),
            implementation_fingerprint(),
            sqlite3.sqlite_version,
            digest(provenance()),
        )
        _publish_json(output / "resolved_config.json", protocol)
        _publish_json(manifest_path, manifest)
    if not manifest_path.is_file() or not _checked_path(output / "objects").is_dir():
        raise IntegrityError("Transition run evidence is missing")
    manifest = TransitionManifest.from_dict(decode_json(manifest_path.read_bytes()))
    if (
        manifest.implementation_sha256 != implementation_fingerprint()
        or manifest.sqlite_version != sqlite3.sqlite_version
        or manifest.frozen_sha256 != digest(provenance())
    ):
        raise IntegrityError("Transition implementation or frozen protocol changed")
    store = ArtifactStore(output / "objects")
    saved = _checked_path(output / "resolved_config.json").read_bytes()
    if (
        saved != canonical_bytes(_protocol())
        or store.read_bytes(manifest.protocol) != saved
    ):
        raise IntegrityError("Transition protocol differs from its registered content")
    return output, manifest, store


def _initial_state(output, store, state):
    # N's two passed legal tests are registered inherited input, not executions
    # or discoveries of this run. They carry explicit prehistory provenance.
    deployed = Version.from_dict(decode_json(fixture_bytes("version_D.json")))
    edit_base = Version.from_dict(decode_json(fixture_bytes("version_E.json")))
    native = replace(deployed, agent=replace(deployed.agent, check_confirmation=False))
    native_ref = store.put_json(native)
    inherited_receipts = tuple(
        store.put_json(
            {
                "schema_version": 1,
                "source": "prehistory",
                "test_id": f"prehistory_N_ok_{i}",
                "candidate": native_ref,
                "status": "pass",
                "legal": True,
                "source_protocol": "frozen-p0-v1/appendix-b",
                "charged_in_this_run": False,
            }
        )
        for i in (1, 2)
    )
    inherited = ArchiveScore(
        ArtifactOccurrence(
            "archive_N_001",
            native_ref,
            "registered_prehistory",
            CandidateOrigin.CONTROL,
            "prehistory",
        ),
        2,
        inherited_receipts,
        source="prehistory",
    )
    Workflow(output / "founder.sqlite")
    environment = backup_sqlite(output / "founder.sqlite", store)
    components = ComponentRefs(
        store.put_json(deployed.agent),
        store.put_json({"registered_id": deployed.q.value}),
        store.put_json({"registered_id": deployed.v.value}),
        store.put_json({"registered_id": deployed.s.value}),
    )
    snapshot = SnapshotManifest(
        components,
        store.put_json(deployed),
        store.put_json(edit_base),
        store.put_json(frozen_memory()),
        store.put_json({"schema_version": 1, "entries": (inherited,)}),
        environment,
        store.put_json(RandomPlan(104729)),
        tuple(TaskCursor(family) for family in CursorFamily),
        __version__,
    )
    return snapshot, state.save_snapshot(snapshot)


def _shared_prefix(subject, journal, store, manifest, source_snapshot):
    spec = AttemptSpec(
        "S_001",
        manifest.run_id,
        PAIR_ID,
        "shared_source",
        "golden_root_01",
        "golden_history_01",
        "shared_preparation",
        0,
        1,
        manifest.protocol,
        source_snapshot,
        CandidateOrigin.CONTROL,
    )
    journal.create_budget("pair", PAIR_ID, 5)
    cursor = None
    checkpoints = []
    for index in range(3):
        request = ResumeRequest(
            attempt=spec,
            cursor=cursor,
            operation_id=f"S_001/checkpoint/{index}",
            context_patch=ContextPatch(),
            verification_patch=VerificationPatch(),
            owner_kind="pair",
            owner_id=PAIR_ID,
            generation_operation_id=GENERATION_ID,
            candidate_occurrence_id="generated_P_001",
        )
        result = subject.advance(request)
        if not result.valid:
            raise IntegrityError("Frozen source preparation became invalid")
        cursor = result.cursor
        checkpoints.append(result)
    prefix = {
        "schema_version": 1,
        "spec": spec,
        "checkpoints": checkpoints,
        "purpose": "shared_preparation",
        "stop_reason": "pair_anchor_completed",
        "counts_toward_arm_horizon": False,
    }
    with sqlite3.connect(journal.path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS shared_preparations(source_id TEXT PRIMARY KEY, record_json BLOB NOT NULL)"
        )
        prior = connection.execute(
            "SELECT record_json FROM shared_preparations WHERE source_id=?", ("S_001",)
        ).fetchone()
        data = canonical_bytes(prefix)
        if prior and prior[0] != data:
            raise ConflictError("Shared source replay differs from original")
        connection.execute(
            "INSERT OR IGNORE INTO shared_preparations VALUES (?,?)", ("S_001", data)
        )
    return cursor.local_state


def run_transition(output: Path, fault_hook=None) -> dict:
    """Create or resume the registered transition; returns its fixed projection."""
    output, manifest, store = _open_run(output, create=True)
    state = StateStore(output, store)
    journal = Journal(output / "controller.sqlite")
    verification = VerificationRuntime(
        store,
        journal,
        output / "validation",
        operation_naming="single",
        fault_hook=fault_hook,
    )
    subject = FakeSubject(store, journal, verification)
    runner = PreparationRunner(state, store, subject)
    base, source_ref = _initial_state(output, store, state)
    fixed_ref = _shared_prefix(subject, journal, store, manifest, source_ref)
    archive = decode_json(store.read_bytes(base.archive))
    fork = replace(
        base, archive=store.put_json({**archive, "fixed_preparation": fixed_ref})
    )
    fork_ref = state.save_snapshot(fork)
    anchor = PairAnchor(
        PAIR_ID,
        "golden_root_01",
        "golden_history_01",
        fork_ref,
        store.put_json(
            {
                "schema_version": 1,
                "validation_tests": ["ok"],
                "round2_stop": "selection",
            }
        ),
        fork.random_plan,
        fork.cursors,
        PairingMode.FIXED_CANDIDATE,
        ForkPoint.PRE_VERIFICATION,
        0,
    )
    state.register_pair(anchor)
    for arm in ("L", "R"):
        state.create_branch(arm, PAIR_ID)
        journal.create_budget("branch", arm, 100)
    controller = LifecycleController(
        state, store, journal, ("L", "R"), role="main", fault_hook=fault_hook
    )
    for arm in ("L", "R"):
        spec = AttemptSpec(
            f"{arm}_001",
            manifest.run_id,
            PAIR_ID,
            arm,
            anchor.root_id,
            anchor.history_group_id,
            arm,
            0,
            1,
            manifest.protocol,
            fork_ref,
            CandidateOrigin.CONTROL,
        )
        runner.prepare(
            spec, ContextPatch(), VerificationPatch(), fixed_preparation=fixed_ref
        )
        decision = controller.get_decision(spec.attempt_id)
        if decision is None:
            view = controller.view(spec.attempt_id)
            if view is None:
                raise IntegrityError("Golden archive should produce a native candidate")
            decision = RejectAllAdmission().audit(view, EvidenceSession(store))
        receipt = controller.complete(
            spec.attempt_id, decision, memory_mode="quarantine"
        )
        command = controller.get_command(spec.attempt_id)
        # A derived study index, solely from already-persisted controller facts.
        with sqlite3.connect(journal.path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS transition_arms(arm TEXT PRIMARY KEY, record_json BLOB NOT NULL)"
            )
            item = canonical_bytes(
                {"decision": decision, "command": command, "receipt": receipt}
            )
            old = connection.execute(
                "SELECT record_json FROM transition_arms WHERE arm=?", (arm,)
            ).fetchone()
            if old and old[0] != item:
                raise ConflictError("Transition receipt replay differs")
            connection.execute(
                "INSERT OR IGNORE INTO transition_arms VALUES (?,?)", (arm, item)
            )
        # The second attempt resumes the committed branch state; no new shared pool.
        head_ref = ArtifactRef.from_dict(state.get_branch(arm)["head"])
        second = replace(
            spec, attempt_id=f"{arm}_002", attempt_index=2, source_snapshot=head_ref
        )
        runner.prepare(second, ContextPatch(), VerificationPatch(), stop_after=1)
    return inspect_transition(output)


def inspect_transition(output: Path) -> dict:
    """Read actual persisted facts into Appendix B's independently frozen projection."""
    output, manifest, store = _open_run(output)
    database = _checked_path(output / "controller.sqlite")
    for suffix in ("-wal", "-shm", "-journal"):
        _checked_path(Path(str(database) + suffix))
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        first_round, second_round = [], []
        for arm in ("L", "R"):
            row = connection.execute(
                "SELECT * FROM attempts WHERE attempt_id=?", (f"{arm}_001",)
            ).fetchone()
            if row is None or row["cursor_json"] is None:
                raise ValidationError(
                    "Transition has not reached its registered stopping point"
                )
            cursor = PrepareCursor.from_dict(decode_json(row["cursor_json"]))
            prepared = PreparationState.from_dict(
                decode_json(store.read_bytes(cursor.local_state))
            )
            context = ContextReceipt.from_dict(
                decode_json(store.read_bytes(prepared.context_receipt))
            )
            verification = VerificationReceipt.from_dict(
                decode_json(store.read_bytes(prepared.verification_receipt))
            )
            saved = connection.execute(
                "SELECT record_json FROM transition_arms WHERE arm=?", (arm,)
            ).fetchone()
            if saved is None:
                raise ValidationError(
                    "Transition commit projection is not yet available"
                )
            facts = decode_json(saved[0])
            decision = AuditDecision.from_dict(facts["decision"])
            command = MaterializeCommand.from_dict(facts["command"])
            commit = CommitReceipt.from_dict(facts["receipt"])
            committed = SnapshotManifest.from_dict(
                decode_json(store.read_bytes(commit.snapshot))
            )
            memory = MemoryState.from_dict(
                decode_json(store.read_bytes(committed.memory))
            )
            exposure_count = connection.execute(
                "SELECT COUNT(*) FROM exposure_events WHERE branch_id=?", (arm,)
            ).fetchone()[0]
            first_round.append(
                {
                    "proposed_candidates": [
                        c.artifact.sha256 for c in prepared.candidates
                    ],
                    "native_selected": prepared.native_selected.artifact.sha256,
                    "deployed_version": committed.deployed_version.sha256,
                    "next_edit_base": committed.edit_base_version.sha256,
                    "verdict": decision.verdict,
                    "context_entries": list(context.after_ids),
                    "actual_context_sha256": context.actual_context.sha256,
                    "context_synthetic_units": context.synthetic_units,
                    "executed_validation": [
                        {
                            "test_id": t.test_id,
                            "candidate_alias": "P",
                            "status": t.status,
                            "synthetic_cost_units": t.synthetic_cost_units,
                        }
                        for t in verification.executed
                    ],
                    "phase": row["phase"],
                    "head_revision_before": command.expected_head_revision,
                    "head_revision_after": commit.head_revision,
                    "active_experience_ids": list(memory.active_ids),
                    "task_exposures": exposure_count,
                    "r": None,
                    "r_missing_reason": "evaluation_not_requested",
                    "utility": None,
                }
            )
            second = connection.execute(
                "SELECT cursor_json FROM attempts WHERE attempt_id=?", (f"{arm}_002",)
            ).fetchone()
            if second is None or second[0] is None:
                raise ValidationError("Round two has not reached experience selection")
            second_cursor = PrepareCursor.from_dict(decode_json(second[0]))
            if second_cursor.checkpoint != Checkpoint.POST_EXPERIENCE_SELECTION:
                raise IntegrityError(
                    "Round two advanced past the registered stopping point"
                )
            second_state = PreparationState.from_dict(
                decode_json(store.read_bytes(second_cursor.local_state))
            )
            second_context = ContextReceipt.from_dict(
                decode_json(store.read_bytes(second_state.context_receipt))
            )
            second_round.append(
                (
                    list(second_context.retrieval_candidate_ids),
                    list(second_context.after_ids),
                )
            )
        costs = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM cost_events ORDER BY operation_id"
            )
        ]
        outstanding = sum(
            row[0]
            for row in connection.execute(
                "SELECT reserved_units FROM operations WHERE status IN ('reserved','outcome_unknown')"
            )
        )
    finally:
        connection.close()
    if first_round[0] != first_round[1] or second_round[0] != second_round[1]:
        raise IntegrityError(
            "Paired transition facts differ outside allowed provenance fields"
        )
    return {
        "golden_version": "transition-v1",
        "pair_id": PAIR_ID,
        "arms": ["L", "R"],
        "round1_per_arm": first_round[0],
        "round2_stop_at": "post_experience_selection_after_patch_before_generation",
        "round2_retrieval_candidates_per_arm": second_round[0][0],
        "round2_context_entries_per_arm": second_round[0][1],
        "costs": {
            "settled_charge_rows": len(costs),
            "pair_owned_generation_rows": sum(
                c["owner_kind"] == "pair" and "/generate/" in c["operation_id"]
                for c in costs
            ),
            "pair_shared_units": sum(
                c["synthetic_cost_units"] for c in costs if c["owner_kind"] == "pair"
            ),
            "arm_L_units": sum(
                c["synthetic_cost_units"]
                for c in costs
                if c["owner_kind"] == "branch" and c["owner_id"] == "L"
            ),
            "arm_R_units": sum(
                c["synthetic_cost_units"]
                for c in costs
                if c["owner_kind"] == "branch" and c["owner_id"] == "R"
            ),
            "total_synthetic_units": sum(c["synthetic_cost_units"] for c in costs),
            "outstanding_reserved_units": outstanding,
            "generation_invocations": sum(
                "/generate/" in c["operation_id"] for c in costs
            ),
            "verification_task_invocations": sum(
                "/verify/" in c["operation_id"] for c in costs
            ),
        },
    }
