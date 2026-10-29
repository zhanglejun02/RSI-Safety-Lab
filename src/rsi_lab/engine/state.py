"""Persistent M1 state machine through preparation.

All writes come from the trusted controller. This module deliberately exposes no
audit/resolve/deploy shortcut before the M3 command authorization implementation.
Mutable branch databases are separate from controller.sqlite and immutable objects.
"""

import hashlib
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import fields, is_dataclass
from pathlib import Path

from rsi_lab.contracts import (
    ArtifactRef, AttemptSpec, Checkpoint, ForkPoint, PairAnchor, Phase,
    PrepareCursor, RandomPlan, RunStatus, SnapshotManifest, require_id,
)
from rsi_lab.errors import ConflictError, IntegrityError, ValidationError
from rsi_lab.infra.artifacts import ArtifactStore, _checked_path
from rsi_lab.infra.snapshots import restore_sqlite
from rsi_lab.serialization import canonical_bytes, decode_json


def artifact_refs(value):
    """Walk typed manifests only; never infer authority from embedded hashes."""
    if isinstance(value, ArtifactRef):
        yield value
    elif is_dataclass(value):
        for field in fields(value):
            yield from artifact_refs(getattr(value, field.name))
    elif isinstance(value, tuple):
        for item in value:
            yield from artifact_refs(item)


class StateStore:
    def __init__(self, run_dir: Path, artifacts: ArtifactStore):
        self.run_dir = _checked_path(Path(run_dir))
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = _checked_path(self.run_dir / "controller.sqlite")
        self.artifacts = artifacts
        with self._db() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS pairs(
                    pair_id TEXT PRIMARY KEY, anchor_json BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS branches(
                    branch_id TEXT PRIMARY KEY, pair_id TEXT NOT NULL REFERENCES pairs(pair_id),
                    role TEXT NOT NULL CHECK(role IN ('main','probe','evaluation')),
                    head_json BLOB NOT NULL, head_revision INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS attempts(
                    attempt_id TEXT PRIMARY KEY, branch_id TEXT NOT NULL REFERENCES branches(branch_id),
                    spec_json BLOB NOT NULL, phase TEXT NOT NULL, status TEXT NOT NULL,
                    cursor_json BLOB, status_reason TEXT,
                    attempt_index INTEGER NOT NULL, replicate INTEGER NOT NULL,
                    UNIQUE(branch_id, replicate, attempt_index));
                CREATE TABLE IF NOT EXISTS checkpoints(
                    operation_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
                    checkpoint TEXT NOT NULL, record_json BLOB NOT NULL,
                    UNIQUE(attempt_id, checkpoint));
                CREATE TABLE IF NOT EXISTS state_events(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL, identity TEXT NOT NULL, data_json BLOB NOT NULL);
            """)

    @contextmanager
    def _db(self, write=False):
        _checked_path(self.path)
        for suffix in ("-wal", "-shm", "-journal"):
            _checked_path(Path(str(self.path) + suffix))
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _event(conn, event_type, identity, data):
        conn.execute("INSERT INTO state_events(event_type,identity,data_json) VALUES (?,?,?)",
                     (event_type, identity, canonical_bytes(data)))

    def save_snapshot(self, manifest: SnapshotManifest) -> ArtifactRef:
        for ref in artifact_refs(manifest):
            self.artifacts.read_bytes(ref)
        RandomPlan.from_dict(decode_json(self.artifacts.read_bytes(manifest.random_plan)))
        return self.artifacts.put_json(manifest.to_dict())

    def load_snapshot(self, ref: ArtifactRef) -> SnapshotManifest:
        manifest = SnapshotManifest.from_dict(decode_json(self.artifacts.read_bytes(ref)))
        for child in artifact_refs(manifest):
            self.artifacts.read_bytes(child)
        RandomPlan.from_dict(decode_json(self.artifacts.read_bytes(manifest.random_plan)))
        return manifest

    def register_pair(self, anchor: PairAnchor):
        snapshot = self.load_snapshot(anchor.snapshot)
        self.artifacts.read_bytes(anchor.task_plan)
        if anchor.cursors != snapshot.cursors or anchor.random_plan != snapshot.random_plan:
            raise ValidationError(f"Pair {anchor.pair_id}: cursor/random plan differs from snapshot")
        data = canonical_bytes(anchor)
        with self._db(True) as conn:
            row = conn.execute("SELECT anchor_json FROM pairs WHERE pair_id=?", (anchor.pair_id,)).fetchone()
            if row:
                if row[0] != data:
                    raise ConflictError(f"Pair identity reused: {anchor.pair_id}")
                return
            conn.execute("INSERT INTO pairs VALUES (?,?)", (anchor.pair_id, data))
            self._event(conn, "pair_registered", anchor.pair_id, anchor)

    def get_pair(self, pair_id: str) -> PairAnchor:
        with self._db() as conn:
            row = conn.execute("SELECT anchor_json FROM pairs WHERE pair_id=?", (pair_id,)).fetchone()
        if not row:
            raise ValidationError(f"Unknown pair: {pair_id}")
        return PairAnchor.from_dict(decode_json(row[0]))

    def working_database(self, branch_id: str) -> Path:
        if type(branch_id) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", branch_id):
            raise ValidationError("Branch IDs must be safe single path components")
        path = _checked_path(self.run_dir / "branches" / branch_id / "environment.sqlite")
        for suffix in ("-wal", "-shm", "-journal"):
            _checked_path(Path(str(path) + suffix))
        return path

    def create_branch(self, branch_id: str, pair_id: str, *, role="main"):
        if role not in ("main", "probe", "evaluation"):
            raise ValidationError(f"Unknown branch role: {role}")
        path = self.working_database(branch_id)
        anchor = self.get_pair(pair_id)
        manifest = self.load_snapshot(anchor.snapshot)
        with self._db() as conn:
            row = conn.execute("SELECT * FROM branches WHERE branch_id=?", (branch_id,)).fetchone()
        if row:
            if row["pair_id"] != pair_id or row["role"] != role:
                raise ConflictError(f"Branch identity reused: {branch_id}")
            if not path.is_file():
                raise IntegrityError(f"Missing branch environment: {branch_id}")
            return self.get_branch(branch_id)
        # Safe publication occurs before the head reference. Recover an exact orphan
        # left by interruption, but never overwrite an unrelated mutable database.
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            # A freshly restored orphan is standalone. Matching the main file
            # alone is insufficient when WAL may contain additional effects.
            if any(Path(str(path) + suffix).exists() or Path(str(path) + suffix).is_symlink()
                   for suffix in ("-wal", "-shm", "-journal")):
                raise ConflictError(f"Unregistered branch database has sidecars: {branch_id}")
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != manifest.environment.sha256:
                raise ConflictError(f"Unregistered branch database has different bytes: {branch_id}")
        else:
            restore_sqlite(manifest.environment, self.artifacts, path)
        with self._db(True) as conn:
            existing = conn.execute("SELECT pair_id,role FROM branches WHERE branch_id=?", (branch_id,)).fetchone()
            if existing:
                if tuple(existing) != (pair_id, role):
                    raise ConflictError(f"Concurrent branch registration conflict: {branch_id}")
            else:
                conn.execute("INSERT INTO branches VALUES (?,?,?,?,0)",
                             (branch_id, pair_id, role, canonical_bytes(anchor.snapshot)))
                self._event(conn, "branch_created", branch_id, {"pair_id": pair_id, "role": role})
        return self.get_branch(branch_id)

    def get_branch(self, branch_id: str) -> dict:
        with self._db() as conn:
            row = conn.execute("SELECT * FROM branches WHERE branch_id=?", (branch_id,)).fetchone()
        if not row:
            raise ValidationError(f"Unknown branch: {branch_id}")
        result = dict(row)
        result["head"] = decode_json(result.pop("head_json"))
        return result

    def create_attempt(self, spec: AttemptSpec):
        # Replaying an already recorded attempt may follow its own committed head.
        with self._db() as conn:
            prior=conn.execute("SELECT spec_json FROM attempts WHERE attempt_id=?", (spec.attempt_id,)).fetchone()
        if prior:
            if prior[0] != canonical_bytes(spec):
                raise ConflictError(f"Attempt identity reused: {spec.attempt_id}")
            return
        branch = self.get_branch(spec.branch_id)
        anchor = self.get_pair(spec.pair_id)
        if branch["pair_id"] != spec.pair_id or (spec.root_id, spec.history_group_id) != (anchor.root_id, anchor.history_group_id):
            raise ValidationError(f"Attempt {spec.attempt_id}: lineage binding mismatch")
        if ArtifactRef.from_dict(branch["head"]) != spec.source_snapshot:
            raise ConflictError(f"Attempt {spec.attempt_id}: stale source snapshot")
        self.artifacts.read_bytes(spec.protocol)
        data = canonical_bytes(spec)
        with self._db(True) as conn:
            row = conn.execute("SELECT spec_json FROM attempts WHERE attempt_id=?", (spec.attempt_id,)).fetchone()
            if row:
                if row[0] != data:
                    raise ConflictError(f"Attempt identity reused: {spec.attempt_id}")
                return
            try:
                conn.execute("INSERT INTO attempts VALUES (?,?,?,?,?,NULL,NULL,?,?)",
                             (spec.attempt_id, spec.branch_id, data, Phase.CREATED.value,
                              RunStatus.ACTIVE.value, spec.attempt_index, spec.replicate))
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"Duplicate attempt position: {spec.attempt_id}") from exc
            self._event(conn, "attempt_created", spec.attempt_id, spec)

    def bind_preparation_options(self, attempt_id: str, options: dict):
        """Freeze adapter inputs even when replaying a completed attempt."""
        data = canonical_bytes(options)
        with self._db(True) as conn:
            self._attempt_row(conn, attempt_id)
            conn.execute("CREATE TABLE IF NOT EXISTS preparation_options(attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id), options_json BLOB NOT NULL)")
            row = conn.execute("SELECT options_json FROM preparation_options WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row and row[0] != data:
                raise ConflictError(f"Attempt {attempt_id}: preparation options changed")
            conn.execute("INSERT OR IGNORE INTO preparation_options VALUES (?,?)", (attempt_id, data))

    def begin_preparation(self, attempt_id: str):
        with self._db(True) as conn:
            row = self._attempt_row(conn, attempt_id)
            if row["status"] != RunStatus.ACTIVE.value:
                raise ConflictError(f"Attempt {attempt_id}: status={row['status']}")
            if row["phase"] == Phase.PREPARING.value:
                return
            if row["phase"] != Phase.CREATED.value:
                raise ConflictError(f"Attempt {attempt_id}: cannot begin from {row['phase']}")
            conn.execute("UPDATE attempts SET phase=? WHERE attempt_id=?", (Phase.PREPARING.value, attempt_id))
            self._event(conn, "preparation_started", attempt_id, {})

    @staticmethod
    def _attempt_row(conn, attempt_id):
        row = conn.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if not row:
            raise ValidationError(f"Unknown attempt: {attempt_id}")
        return row

    def record_checkpoint(self, attempt_id: str, cursor: PrepareCursor, *, operation_id: str,
                          input_ref: ArtifactRef, patch_ref: ArtifactRef, receipt_ref: ArtifactRef):
        require_id(operation_id, "operation_id")
        for ref in (cursor.local_state, input_ref, patch_ref, receipt_ref):
            self.artifacts.read_bytes(ref)
        record = dict(cursor=cursor, input_ref=input_ref, patch_ref=patch_ref, receipt_ref=receipt_ref)
        data = canonical_bytes(record)
        with self._db(True) as conn:
            row = self._attempt_row(conn, attempt_id)
            old = conn.execute("SELECT * FROM checkpoints WHERE operation_id=?", (operation_id,)).fetchone()
            if old:
                if old["attempt_id"] != attempt_id or old["record_json"] != data:
                    raise ConflictError(f"Checkpoint operation reused: {operation_id}")
                return
            if row["phase"] != Phase.PREPARING.value or row["status"] != RunStatus.ACTIVE.value:
                raise ConflictError(f"Attempt {attempt_id} cannot advance from {row['phase']}/{row['status']}")
            spec = AttemptSpec.from_dict(decode_json(row["spec_json"]))
            anchor_row = conn.execute("SELECT anchor_json FROM pairs WHERE pair_id=?", (spec.pair_id,)).fetchone()
            anchor = PairAnchor.from_dict(decode_json(anchor_row[0]))
            order = list(Checkpoint)
            initial = {ForkPoint.ATTEMPT_START: 0, ForkPoint.PRE_VERIFICATION: 2, ForkPoint.PRE_DEPLOY: 3}[anchor.fork_at]
            options_table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='preparation_options'").fetchone()
            if options_table:
                options_row=conn.execute("SELECT options_json FROM preparation_options WHERE attempt_id=?", (attempt_id,)).fetchone()
                if options_row and decode_json(options_row[0]).get('fixed_preparation') is None:
                    initial=0
            previous = PrepareCursor.from_dict(decode_json(row["cursor_json"])) if row["cursor_json"] else None
            expected = order.index(previous.checkpoint) + 1 if previous else initial
            if order.index(cursor.checkpoint) != expected:
                raise ConflictError(f"Attempt {attempt_id}: out-of-order checkpoint {cursor.checkpoint.value}")
            if previous and cursor.next_operation_index <= previous.next_operation_index:
                raise ConflictError(f"Attempt {attempt_id}: operation position did not advance")
            conn.execute("INSERT INTO checkpoints VALUES (?,?,?,?)",
                         (operation_id, attempt_id, cursor.checkpoint.value, data))
            phase = Phase.PREPARED if cursor.checkpoint == Checkpoint.PRE_DEPLOY else Phase.PREPARING
            conn.execute("UPDATE attempts SET cursor_json=?,phase=? WHERE attempt_id=?",
                         (canonical_bytes(cursor), phase.value, attempt_id))
            self._event(conn, "checkpoint_recorded", attempt_id, {"operation_id": operation_id, **record})

    def get_attempt(self, attempt_id: str) -> dict:
        with self._db() as conn:
            row = self._attempt_row(conn, attempt_id)
        result = dict(row)
        result["spec"] = decode_json(result.pop("spec_json"))
        raw_cursor = result.pop("cursor_json")
        result["cursor"] = decode_json(raw_cursor) if raw_cursor is not None else None
        return result

    def mark_status(self, attempt_id: str, status: RunStatus, reason: str):
        if (type(status) is not RunStatus or status in (RunStatus.ACTIVE, RunStatus.COMPLETE)
                or type(reason) is not str or not reason.strip()):
            raise ValidationError("M1 terminal status requires an explicit failure status and reason")
        with self._db(True) as conn:
            row = self._attempt_row(conn, attempt_id)
            if row["status"] == status.value and row["status_reason"] == reason:
                return
            if row["status"] != RunStatus.ACTIVE.value:
                raise ConflictError(f"Attempt {attempt_id} is already terminal")
            conn.execute("UPDATE attempts SET status=?,status_reason=? WHERE attempt_id=?",
                         (status.value, reason, attempt_id))
            self._event(conn, "status_changed", attempt_id, {"status": status, "reason": reason})

    def inspect(self) -> dict:
        with self._db() as conn:
            branches = [r[0] for r in conn.execute("SELECT branch_id FROM branches ORDER BY branch_id")]
            attempts = [r[0] for r in conn.execute("SELECT attempt_id FROM attempts ORDER BY attempt_id")]
            event_count = conn.execute("SELECT COUNT(*) FROM state_events").fetchone()[0]
        return {"branches": [self.get_branch(b) for b in branches],
                "attempts": [self.get_attempt(a) for a in attempts], "state_event_count": event_count}

