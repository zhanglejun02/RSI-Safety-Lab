"""Atomic local activation for trusted, declarative adapters.

Only inherited system state lives here; proposals use separate workspaces. This
base is not an OS sandbox and is not a remote exactly-once implementation.
"""
from contextlib import contextmanager, closing
from pathlib import Path
import sqlite3
from rsi_lab.api import SystemSnapshot, ActivationCommand, ActivationReceipt
from rsi_lab.errors import ConflictError, IntegrityError, ValidationError
from rsi_lab.infra.artifacts import _checked_path
from rsi_lab.serialization import canonical_bytes, decode_json, digest


class LocalSystemAdapter:
    def __init__(self, directory: Path, initial_snapshot: SystemSnapshot):
        self.directory = _checked_path(Path(directory))
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "system.sqlite"
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS system_state(id INTEGER PRIMARY KEY CHECK(id=1), initial_hash TEXT NOT NULL, body BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS activations(operation_id TEXT PRIMARY KEY, command_hash TEXT NOT NULL, body BLOB NOT NULL);
            """)
            row = db.execute("SELECT initial_hash FROM system_state WHERE id=1").fetchone()
            if row and row[0] != initial_snapshot.sha256:
                raise ConflictError("Adapter directory initialized from a different snapshot")
            db.execute("INSERT OR IGNORE INTO system_state VALUES(1,?,?)", (initial_snapshot.sha256, canonical_bytes(initial_snapshot)))

    @contextmanager
    def _db(self):
        _checked_path(self.path)
        for suffix in ("-wal", "-shm", "-journal"):
            _checked_path(Path(str(self.path) + suffix))
        with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def _snapshot(db):
        return SystemSnapshot.from_dict(decode_json(db.execute("SELECT body FROM system_state WHERE id=1").fetchone()[0]))

    def snapshot(self):
        with self._db() as db:
            return self._snapshot(db)

    def activate(self, command: ActivationCommand):
        if type(command) is not ActivationCommand:
            raise ValidationError("Expected controller activation command")
        with self._db() as db:
            previous = db.execute("SELECT command_hash,body FROM activations WHERE operation_id=?", (command.operation_id,)).fetchone()
            if previous:
                if previous[0] != digest(command):
                    raise ConflictError("Activation operation reused with different command")
                return ActivationReceipt.from_dict(decode_json(previous[1]))
            before = self._snapshot(db)
            if before.sha256 != command.identity.base_sha256:
                raise ConflictError("Activation base is stale")
            if before.version.system_id != command.candidate.version.system_id:
                raise IntegrityError("Cannot activate another system's snapshot")
            receipt = ActivationReceipt(command.operation_id, digest(command), before.sha256, command.candidate.sha256)
            db.execute("UPDATE system_state SET body=? WHERE id=1", (canonical_bytes(command.candidate),))
            db.execute("INSERT INTO activations VALUES(?,?,?)", (command.operation_id, digest(command), canonical_bytes(receipt)))
            return receipt

    def activation_status(self, operation_id):
        with self._db() as db:
            row = db.execute("SELECT body FROM activations WHERE operation_id=?", (operation_id,)).fetchone()
            return ActivationReceipt.from_dict(decode_json(row[0])) if row else None
