"""Controller-authorized recovery, verified materialization and atomic commit.

Only trusted assembly code creates this controller. Its private branch allowlist
and persisted branch role constrain every operation; a worker cannot supply a
role inside a command to acquire authority. This is not hostile-process isolation.
"""

import os
import sqlite3
import uuid
from contextlib import closing
from dataclasses import replace

from rsi_lab.contracts import (
    ArtifactRef,
    ArtifactOccurrence,
    AttemptSpec,
    CandidateBinding,
    CursorFamily,
    PrepareCursor,
    SnapshotManifest,
    TaskCursor,
    Version,
    ComponentRefs,
)
from rsi_lab.errors import (
    ConflictError,
    IntegrityError,
    PermissionDenied,
    ValidationError,
)
from rsi_lab.infra.artifacts import _fsync_directory
from rsi_lab.infra.snapshots import backup_sqlite, restore_sqlite
from rsi_lab.interventions.context import MemoryState
from rsi_lab.lifecycle import (
    AdmissionView,
    AuditDecision,
    MemoryPlan,
    Resolution,
    MaterializeCommand,
    MaterializationReceipt,
    CommitReceipt,
)
from rsi_lab.preparation import PreparationState
from rsi_lab.serialization import canonical_bytes, decode_json
from rsi_lab.subjects.materialize import SnapshotMaterializer, verify_staging
from rsi_lab.subjects.verification import (
    ArchiveScore,
    VerificationReceipt,
    select_native,
)


class LifecycleController:
    def __init__(
        self, state, store, journal, allowed_branch_ids, role="main", fault_hook=None
    ):
        if (
            role not in ("main", "probe", "evaluation")
            or type(allowed_branch_ids) is not tuple
        ):
            raise ValidationError(
                "Controller requires an explicit branch capability tuple and registered role"
            )
        self.state, self.store, self.journal = state, store, journal
        self._allowed = frozenset(allowed_branch_ids)
        self._role = role
        self._fault = fault_hook or (lambda point: None)
        self.materializer = SnapshotMaterializer(state.run_dir / "staging", store)
        self._trusted = SnapshotMaterializer(state.run_dir / "trusted_staging", store)
        with self.state._db(True) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS decisions(attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id), decision_json BLOB);
                CREATE TABLE IF NOT EXISTS commands(command_id TEXT PRIMARY KEY, attempt_id TEXT UNIQUE NOT NULL REFERENCES attempts(attempt_id), command_json BLOB NOT NULL, role TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS materializations(command_id TEXT PRIMARY KEY REFERENCES commands(command_id), receipt_json BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS commits(command_id TEXT PRIMARY KEY REFERENCES commands(command_id), attempt_id TEXT UNIQUE NOT NULL, receipt_json BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS working_installations(branch_id TEXT PRIMARY KEY REFERENCES branches(branch_id), snapshot_json BLOB NOT NULL, installed INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS failed_preparation_progress(attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id), source_snapshot_json BLOB NOT NULL, exposure_json BLOB NOT NULL, snapshot_json BLOB NOT NULL);
            """
            )

    def _authorize(self, branch_id):
        if branch_id not in self._allowed:
            raise PermissionDenied(
                f"Controller has no capability for branch {branch_id}"
            )
        branch = self.state.get_branch(branch_id)
        if branch["role"] != self._role:
            raise PermissionDenied(
                f'Controller role cannot modify {branch["role"]} branch {branch_id}'
            )
        return branch

    def authorize_runtime(self, branch_id):
        """Authorize dispatch and finish any durable commit's pending install."""
        self._authorize(branch_id)
        self._install_pending(branch_id)
        return self._authorize(branch_id)

    def _read(self, cls, ref):
        return cls.from_dict(decode_json(self.store.read_bytes(ref)))

    def _prepared(self, attempt_id):
        attempt = self.state.get_attempt(attempt_id)
        spec = AttemptSpec.from_dict(attempt["spec"])
        self._authorize(spec.branch_id)
        if attempt["phase"] not in (
            "prepared",
            "audited",
            "resolved",
            "materialized",
            "committed",
        ) or attempt["status"] not in ("active", "complete"):
            raise ConflictError(f"Attempt {attempt_id} is not ready for admission")
        cursor = PrepareCursor.from_dict(attempt["cursor"])
        prepared = self._read(PreparationState, cursor.local_state)
        if not prepared.valid or prepared.prepared_snapshot is None:
            raise ConflictError(f"Attempt {attempt_id} has no valid prepared snapshot")
        if prepared.source_snapshot != spec.source_snapshot:
            raise ConflictError("Prepared source differs from attempt source")
        self.state.load_snapshot(prepared.prepared_snapshot)
        return attempt, spec, prepared

    def view(self, attempt_id):
        _, spec, prepared = self._prepared(attempt_id)
        if prepared.verification_receipt is None:
            raise IntegrityError("Admission requires actual native verification")
        receipt = self._read(VerificationReceipt, prepared.verification_receipt)
        if (
            receipt.attempt_id != spec.attempt_id
            or receipt.branch_id != spec.branch_id
            or receipt.candidate_occurrences != prepared.candidates
            or not receipt.complete
        ):
            raise IntegrityError("Native verification provenance mismatch")
        source = self.state.load_snapshot(spec.source_snapshot)
        history = tuple(
            ArchiveScore.from_dict(entry)
            for entry in decode_json(self.store.read_bytes(source.archive)).get(
                "entries", ()
            )
        )
        selector = self._read(Version, source.edit_base_version).s
        if (
            select_native(prepared.candidates, receipt, selector, history)
            != prepared.native_selected
        ):
            raise IntegrityError("Native selection occurrence provenance mismatch")
        if prepared.native_selected is None:
            if prepared.candidate_binding is not None:
                raise IntegrityError("No-selection state has a candidate binding")
            return None
        expected = CandidateBinding(
            spec.attempt_id,
            prepared.native_selected,
            prepared.prepared_snapshot,
            spec.protocol,
        )
        if (
            prepared.candidate_binding is None
            or self._read(CandidateBinding, prepared.candidate_binding) != expected
        ):
            raise ConflictError("Prepared candidate binding mismatch")
        self.store.read_bytes(prepared.native_selected.artifact)
        return AdmissionView(
            expected, True, prepared.context_receipt, prepared.verification_receipt
        )

    def record_decision(self, decision):
        if type(decision) is not AuditDecision:
            raise ValidationError("Expected a typed AuditDecision")
        view = self.view(decision.binding.attempt_id)
        if view is None or view.binding != decision.binding:
            raise ConflictError(
                "Admission decision candidate/base/protocol binding mismatch"
            )
        evidence = decode_json(self.store.read_bytes(decision.evidence))
        if (
            type(evidence) is not dict
            or evidence.get("binding") != decision.binding.to_dict()
            or evidence.get("policy_version") != decision.policy_version
        ):
            raise ConflictError(
                "Admission evidence is bound to a different candidate or policy"
            )
        data = canonical_bytes(decision)
        with self.state._db(True) as conn:
            old = conn.execute(
                "SELECT decision_json FROM decisions WHERE attempt_id=?",
                (decision.binding.attempt_id,),
            ).fetchone()
            if old:
                if old[0] != data:
                    raise ConflictError("A different decision is already recorded")
                return
            row = self.state._attempt_row(conn, decision.binding.attempt_id)
            if row["phase"] != "prepared":
                raise ConflictError("Decision cannot be introduced after resolution")
            conn.execute(
                "INSERT INTO decisions VALUES (?,?)",
                (decision.binding.attempt_id, data),
            )
            conn.execute(
                "UPDATE attempts SET phase='audited' WHERE attempt_id=?",
                (decision.binding.attempt_id,),
            )
            self.state._event(
                conn, "admission_decision", decision.binding.attempt_id, decision
            )

    def get_decision(self, attempt_id):
        self._prepared(attempt_id)
        with self.state._db() as conn:
            row = conn.execute(
                "SELECT decision_json FROM decisions WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
        return self._decode(AuditDecision, row[0]) if row and row[0] else None

    @staticmethod
    def _decode(cls, data):
        return cls.from_dict(decode_json(data))

    def get_command(self, attempt_id):
        self._prepared(attempt_id)
        with self.state._db() as conn:
            row = conn.execute(
                "SELECT command_json FROM commands WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
        if not row:
            raise ValidationError("Attempt has no authorized command")
        return self._decode(MaterializeCommand, row[0])

    def get_receipt(self, attempt_id):
        self._prepared(attempt_id)
        with self.state._db() as conn:
            row = conn.execute(
                "SELECT receipt_json FROM commits WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
        return self._decode(CommitReceipt, row[0]) if row else None

    def resolve(self, attempt_id, memory_mode="quarantine"):
        attempt, spec, prepared = self._prepared(attempt_id)
        if memory_mode not in ("retain", "discard", "quarantine"):
            raise ValidationError("Unknown experience recovery mode")
        with self.state._db() as conn:
            old = conn.execute(
                "SELECT command_json FROM commands WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
        if old:
            command = self._decode(MaterializeCommand, old[0])
            if (
                self._read(Resolution, command.resolution).memory_plan.mode
                != memory_mode
            ):
                raise ConflictError("Recovery policy cannot change after resolution")
            return command
        decision = self.get_decision(attempt_id)
        if decision is None and prepared.native_selected is not None:
            raise ConflictError("Cannot resolve an unaudited candidate")
        if attempt["phase"] != "audited":
            raise ConflictError("Resolution requires an audited attempt")
        verdict = decision.verdict if decision else "not_applicable"
        original = self.state.load_snapshot(spec.source_snapshot)
        target = self.state.load_snapshot(prepared.prepared_snapshot)
        before = self._read(MemoryState, original.memory)
        after = self._read(MemoryState, target.memory)
        before_entries = {entry.entry_id: entry for entry in before.entries}
        after_entries = {entry.entry_id: entry for entry in after.entries}
        if any(
            after_entries.get(key) != entry for key, entry in before_entries.items()
        ):
            raise IntegrityError("Preparation changed inherited experience bytes")
        added = tuple(
            entry.entry_id
            for entry in after.entries
            if entry.entry_id not in before_entries
        )
        added_set = set(added)
        active = tuple(item for item in after.active_ids if item not in added_set)
        quarantined = tuple(
            item for item in after.quarantined_ids if item not in added_set
        )
        if active != before.active_ids or quarantined != before.quarantined_ids:
            raise IntegrityError("Preparation changed inherited experience visibility")
        if memory_mode == "retain":
            active = active + tuple(
                item for item in after.active_ids if item in added_set
            )
            quarantined = quarantined + tuple(
                item for item in after.quarantined_ids if item in added_set
            )
        elif memory_mode == "quarantine":
            quarantined += added
        memory_plan = MemoryPlan(memory_mode, added, active, quarantined)
        memory_ref = self.store.put_json(
            replace(after, active_ids=active, quarantined_ids=quarantined)
        )
        deployed, edit_base = original.deployed_version, original.edit_base_version
        components = original.components
        if verdict == "accept":
            deployed = edit_base = prepared.native_selected.artifact
            version = self._read(Version, deployed)
            components = ComponentRefs(
                self.store.put_json(version.agent),
                self.store.put_json({"registered_id": version.q.value}),
                self.store.put_json({"registered_id": version.v.value}),
                self.store.put_json({"registered_id": version.s.value}),
            )
        target = replace(
            target,
            deployed_version=deployed,
            edit_base_version=edit_base,
            memory=memory_ref,
            components=components,
        )
        target_ref = self.state.save_snapshot(target)
        resolution = Resolution(attempt_id, verdict, deployed, edit_base, memory_plan)
        resolution_ref = self.store.put_json(resolution)
        branch = self._authorize(spec.branch_id)
        if ArtifactRef.from_dict(branch["head"]) != spec.source_snapshot:
            raise ConflictError("Attempt source is no longer branch head")
        command = MaterializeCommand(
            f"{attempt_id}/materialize",
            attempt_id,
            spec.branch_id,
            branch["head_revision"],
            prepared.prepared_snapshot,
            prepared.native_selected,
            target_ref,
            resolution_ref,
        )
        with self.state._db(True) as conn:
            row = self.state._attempt_row(conn, attempt_id)
            if row["phase"] != "audited":
                raise ConflictError("Concurrent resolution changed attempt")
            conn.execute(
                "INSERT INTO commands VALUES (?,?,?,?)",
                (command.command_id, attempt_id, canonical_bytes(command), self._role),
            )
            conn.execute(
                "UPDATE attempts SET phase='resolved' WHERE attempt_id=?", (attempt_id,)
            )
            self.state._event(conn, "state_resolved", attempt_id, command)
        return command

    def _registered(self, command):
        if type(command) is not MaterializeCommand:
            raise ValidationError("Expected a controller MaterializeCommand")
        self._authorize(command.target_branch_id)
        with self.state._db() as conn:
            row = conn.execute(
                "SELECT command_json,role FROM commands WHERE command_id=?",
                (command.command_id,),
            ).fetchone()
        if not row or row[0] != canonical_bytes(command) or row[1] != self._role:
            raise PermissionDenied(
                "Command is not authorized for this controller capability"
            )
        _, spec, prepared = self._prepared(command.attempt_id)
        if (
            spec.branch_id != command.target_branch_id
            or prepared.prepared_snapshot != command.source_snapshot
            or prepared.native_selected != command.approved_candidate
        ):
            raise ConflictError("Materialize command provenance mismatch")
        return spec

    def materialize(self, command):
        self._registered(command)
        operation_id = command.command_id
        self.journal.reserve(
            operation_id,
            "branch",
            command.target_branch_id,
            0,
            "online_audit" if self._role == "probe" else "discovery",
            command.to_dict(),
            attempt_id=command.attempt_id,
        )
        receipt = self.materializer.materialize(command)
        verify_staging(self.materializer, command, receipt)
        # Hand exact independently verified bytes into controller-owned staging.
        trusted_receipt = self._trusted.materialize(command)
        verify_staging(self._trusted, command, trusted_receipt)
        with self.state._db(True) as conn:
            old = conn.execute(
                "SELECT receipt_json FROM materializations WHERE command_id=?",
                (command.command_id,),
            ).fetchone()
            if old and old[0] != canonical_bytes(receipt):
                raise ConflictError("Materialization replay differs")
            conn.execute(
                "INSERT OR IGNORE INTO materializations VALUES (?,?)",
                (command.command_id, canonical_bytes(receipt)),
            )
            row = self.state._attempt_row(conn, command.attempt_id)
            if row["phase"] == "resolved":
                conn.execute(
                    "UPDATE attempts SET phase='materialized' WHERE attempt_id=?",
                    (command.attempt_id,),
                )
                self.state._event(conn, "materialized", command.attempt_id, receipt)
            elif row["phase"] not in ("materialized", "committed"):
                raise ConflictError("Invalid materialization phase")
        self.journal.settle(operation_id, 0, receipt.to_dict())
        self._fault("after_staging")
        return receipt

    def commit(self, command, receipt):
        spec = self._registered(command)
        existing = self.get_receipt(command.attempt_id)
        if existing:
            with self.state._db() as conn:
                previous = conn.execute(
                    "SELECT receipt_json FROM materializations WHERE command_id=?",
                    (command.command_id,),
                ).fetchone()
            if (
                type(receipt) is not MaterializationReceipt
                or not previous
                or previous[0] != canonical_bytes(receipt)
            ):
                raise ConflictError("Committed materialization replay differs")
            self._install_pending(command.target_branch_id)
            return existing
        if type(receipt) is not MaterializationReceipt:
            raise ValidationError("Expected an actual materialization receipt")
        verify_staging(self.materializer, command, receipt)
        verify_staging(self._trusted, command, receipt)
        self.state.load_snapshot(command.target_manifest)
        result = CommitReceipt(
            command.command_id,
            command.attempt_id,
            command.target_branch_id,
            command.expected_head_revision + 1,
            command.target_manifest,
        )
        with self.state._db(True) as conn:
            persisted = conn.execute(
                "SELECT receipt_json FROM materializations WHERE command_id=?",
                (command.command_id,),
            ).fetchone()
            if not persisted or persisted[0] != canonical_bytes(receipt):
                raise PermissionDenied(
                    "Materialization was not independently registered"
                )
            branch = conn.execute(
                "SELECT * FROM branches WHERE branch_id=?", (command.target_branch_id,)
            ).fetchone()
            if (
                branch["head_revision"] != command.expected_head_revision
                or self._decode(ArtifactRef, branch["head_json"])
                != spec.source_snapshot
            ):
                raise ConflictError("Stale head revision or source snapshot")
            row = self.state._attempt_row(conn, command.attempt_id)
            if row["phase"] != "materialized":
                raise ConflictError("Commit requires materialized attempt")
            conn.execute(
                "UPDATE branches SET head_json=?,head_revision=? WHERE branch_id=?",
                (
                    canonical_bytes(command.target_manifest),
                    result.head_revision,
                    command.target_branch_id,
                ),
            )
            conn.execute(
                "UPDATE attempts SET phase='committed',status='complete' WHERE attempt_id=?",
                (command.attempt_id,),
            )
            conn.execute(
                "INSERT INTO commits VALUES (?,?,?)",
                (command.command_id, command.attempt_id, canonical_bytes(result)),
            )
            conn.execute(
                "INSERT INTO working_installations VALUES (?,?,0) ON CONFLICT(branch_id) DO UPDATE SET snapshot_json=excluded.snapshot_json, installed=0",
                (command.target_branch_id, canonical_bytes(command.target_manifest)),
            )
            self.state._event(conn, "committed", command.attempt_id, result)
        self._fault("after_commit")
        self._install_pending(command.target_branch_id)
        return result

    def _install_pending(self, branch_id):
        self._authorize(branch_id)
        with self.state._db() as conn:
            row = conn.execute(
                "SELECT snapshot_json,installed FROM working_installations WHERE branch_id=?",
                (branch_id,),
            ).fetchone()
        if not row or row["installed"]:
            return
        ref = self._decode(ArtifactRef, row["snapshot_json"])
        branch = self.state.get_branch(branch_id)
        if ArtifactRef.from_dict(branch["head"]) != ref:
            raise ConflictError(
                "Pending workspace installation does not match branch head"
            )
        snapshot = self.state.load_snapshot(ref)
        destination = self.state.working_database(branch_id)
        temporary = destination.with_name(f".install-{uuid.uuid4().hex}.sqlite")
        try:
            restore_sqlite(snapshot.environment, self.store, temporary)
            if destination.exists():
                # No task dispatch occurs while installation is pending. Close the
                # old SQLite journal before the standalone file is replaced.
                with closing(sqlite3.connect(destination)) as conn:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    conn.execute("PRAGMA journal_mode=DELETE")
            os.replace(temporary, destination)
            _fsync_directory(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)
        with self.state._db(True) as conn:
            conn.execute(
                "UPDATE working_installations SET installed=1 WHERE branch_id=? AND snapshot_json=?",
                (branch_id, canonical_bytes(ref)),
            )
            self.state._event(conn, "workspace_installed", branch_id, {"snapshot": ref})

    def complete(self, attempt_id, decision, memory_mode="quarantine"):
        if (
            type(decision) is not AuditDecision
            or decision.binding.attempt_id != attempt_id
        ):
            raise ConflictError("Completion decision belongs to a different attempt")
        self.record_decision(decision)
        command = self.resolve(attempt_id, memory_mode)
        existing = self.get_receipt(attempt_id)
        if existing:
            self._install_pending(command.target_branch_id)
            return existing
        return self.commit(command, self.materialize(command))

    def complete_no_candidate(self, attempt_id, memory_mode="quarantine"):
        if self.view(attempt_id) is not None:
            raise ConflictError(
                "not_applicable is only valid when native selection is empty"
            )
        with self.state._db(True) as conn:
            row = self.state._attempt_row(conn, attempt_id)
            if row["phase"] == "prepared":
                conn.execute("INSERT INTO decisions VALUES (?,NULL)", (attempt_id,))
                conn.execute(
                    "UPDATE attempts SET phase='audited' WHERE attempt_id=?",
                    (attempt_id,),
                )
                self.state._event(conn, "admission_not_applicable", attempt_id, {})
        command = self.resolve(attempt_id, memory_mode)
        existing = self.get_receipt(attempt_id)
        if existing:
            self._install_pending(command.target_branch_id)
            return existing
        return self.commit(command, self.materialize(command))

    def record_failed_preparation(self, attempt_id):
        """Keep dispatched validation exposures without admitting a failed candidate.

        This closes only the task cursor side of a terminal preparation. All
        artifacts and reservations remain in their existing stores; no code,
        memory, archive, environment or candidate selection is activated.
        """
        attempt = self.state.get_attempt(attempt_id)
        spec = AttemptSpec.from_dict(attempt["spec"])
        branch = self.authorize_runtime(spec.branch_id)
        if attempt["phase"] not in ("created", "preparing", "prepared") or attempt[
            "status"
        ] not in (
            "invalid",
            "outcome_unknown",
            "budget_exhausted",
            "timeout",
            "crashed",
        ):
            raise ConflictError("Cursor recovery requires terminal failed preparation")
        if self._role not in ("main", "probe"):
            raise PermissionDenied(
                "Failed validation recovery requires a preparation role"
            )
        with self.state._db() as conn:
            saved = conn.execute(
                "SELECT * FROM failed_preparation_progress WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
        if saved:
            if (
                self._decode(ArtifactRef, saved["source_snapshot_json"])
                != spec.source_snapshot
            ):
                raise ConflictError("Failed preparation recovery source changed")
            return self._decode(ArtifactRef, saved["snapshot_json"])
        source = self.state.load_snapshot(spec.source_snapshot)
        prepared = None
        if attempt["cursor"] is not None:
            cursor = PrepareCursor.from_dict(attempt["cursor"])
            prepared = self._read(PreparationState, cursor.local_state)
            if prepared.source_snapshot != spec.source_snapshot:
                raise ConflictError("Failed preparation source differs from attempt")
        family = (
            CursorFamily.PUBLIC_DEVELOPMENT
            if self._role == "main"
            else CursorFamily.ADMISSION_PROBE
        )
        purpose = "discovery" if self._role == "main" else "online_audit"
        exposures = []
        seen = set()
        for exposure in self.journal.exposures():
            if not exposure["operation_id"].startswith(f"{attempt_id}/verify/"):
                continue
            operation = self.journal.get_operation(exposure["operation_id"])
            request = operation["request"]
            # Deployment or other operations of this attempt do not contribute
            # to the validation cursor. Their own runtime records their facts.
            if (
                operation["attempt_id"] != attempt_id
                or operation["owner_kind"] != "branch"
                or operation["owner_id"] != spec.branch_id
                or exposure["branch_id"] != spec.branch_id
                or operation["purpose"] != purpose
                or exposure["cursor_family"] != family.value
                or exposure["partition"] is not None
                or request.get("purpose") != purpose
                or request.get("cursor_family") != family.value
                or request.get("test_id") != exposure["task_id"]
                or exposure["exposure_id"] != operation["operation_id"] + "/exposure"
                or operation["operation_id"] in seen
                or operation["status"] == "released"
            ):
                raise IntegrityError("Failed validation exposure provenance mismatch")
            candidate = ArtifactOccurrence.from_dict(
                request.get("candidate_occurrence", {})
            )
            if prepared is None or candidate not in prepared.candidates:
                raise IntegrityError("Failed validation candidate provenance mismatch")
            seen.add(operation["operation_id"])
            exposures.append(exposure)
        cursors = list(source.cursors)
        namespace = "main" if self._role == "main" else f"{attempt_id}/validation"
        if exposures:
            for index, cursor in enumerate(cursors):
                if cursor.family == family and cursor.namespace == namespace:
                    cursors[index] = replace(
                        cursor, position=cursor.position + len(exposures)
                    )
                    break
            else:
                cursors.append(
                    TaskCursor(family, position=len(exposures), namespace=namespace)
                )
        target = self.state.save_snapshot(replace(source, cursors=tuple(cursors)))
        with self.state._db(True) as conn:
            saved = conn.execute(
                "SELECT snapshot_json FROM failed_preparation_progress WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if saved:
                return self._decode(ArtifactRef, saved[0])
            current = conn.execute(
                "SELECT head_json,head_revision FROM branches WHERE branch_id=?",
                (spec.branch_id,),
            ).fetchone()
            if (
                current["head_revision"] != branch["head_revision"]
                or self._decode(ArtifactRef, current["head_json"])
                != spec.source_snapshot
            ):
                raise ConflictError(
                    "Failed preparation source is no longer branch head"
                )
            row = self.state._attempt_row(conn, attempt_id)
            if row["status"] != attempt["status"] or row["phase"] != attempt["phase"]:
                raise ConflictError("Failed preparation status changed during recovery")
            conn.execute(
                "UPDATE branches SET head_json=? WHERE branch_id=?",
                (canonical_bytes(target), spec.branch_id),
            )
            conn.execute(
                "INSERT INTO failed_preparation_progress VALUES (?,?,?,?)",
                (
                    attempt_id,
                    canonical_bytes(spec.source_snapshot),
                    canonical_bytes(exposures),
                    canonical_bytes(target),
                ),
            )
            self.state._event(
                conn,
                "failed_preparation_progress",
                attempt_id,
                {"snapshot": target, "validation_exposures": len(exposures)},
            )
        return target

    def record_runtime_snapshot(self, branch_id, environment_ref, cursors):
        branch = self._authorize(branch_id)
        self._install_pending(branch_id)
        original_ref = ArtifactRef.from_dict(branch["head"])
        original = self.state.load_snapshot(original_ref)
        if type(cursors) is not tuple:
            raise ValidationError("Runtime cursors must be an ordered tuple")
        allowed_family = {
            "main": CursorFamily.PUBLIC_DEVELOPMENT,
            "probe": CursorFamily.ADMISSION_PROBE,
            "evaluation": CursorFamily.INDEPENDENT_EVALUATION,
        }[self._role]
        if len(cursors) < len(original.cursors):
            raise ValidationError("Runtime cannot remove cursor namespaces")
        for before, after in zip(original.cursors, cursors):
            if (
                replace(before, position=after.position) != after
                or after.position < before.position
            ):
                raise ValidationError(
                    "Runtime cursor identity changed or moved backwards"
                )
            if after.family != allowed_family and after.position != before.position:
                raise PermissionDenied("Runtime cannot advance another role cursor")
        for added in cursors[len(original.cursors) :]:
            if added.family != allowed_family:
                raise PermissionDenied("Runtime cannot add another role cursor")
        actual = backup_sqlite(self.state.working_database(branch_id), self.store)
        if actual != environment_ref:
            raise IntegrityError("Runtime snapshot is not the actual branch database")
        target = self.state.save_snapshot(
            replace(original, environment=environment_ref, cursors=cursors)
        )
        with self.state._db(True) as conn:
            row = conn.execute(
                "SELECT head_json,head_revision FROM branches WHERE branch_id=?",
                (branch_id,),
            ).fetchone()
            if (
                row["head_revision"] != branch["head_revision"]
                or self._decode(ArtifactRef, row["head_json"]) != original_ref
            ):
                raise ConflictError("Concurrent runtime update changed branch head")
            if target != original_ref:
                conn.execute(
                    "UPDATE branches SET head_json=? WHERE branch_id=?",
                    (canonical_bytes(target), branch_id),
                )
                conn.execute(
                    "INSERT INTO working_installations VALUES (?,?,1) ON CONFLICT(branch_id) DO UPDATE SET snapshot_json=excluded.snapshot_json,installed=1",
                    (branch_id, canonical_bytes(target)),
                )
                self.state._event(
                    conn, "runtime_snapshot", branch_id, {"snapshot": target}
                )
        return target
