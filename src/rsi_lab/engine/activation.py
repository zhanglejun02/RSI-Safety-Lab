"""Platform activation coordinator, with persisted intent and query-only recovery."""
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
from rsi_lab.api import ActivationCommand, ActivationReceipt, PreparedUpdate
from rsi_lab.auditing.platform import AdmissionDecision
from rsi_lab.errors import ConflictError, IntegrityError, OutcomeUnknown, UnsupportedCapability, ValidationError
from rsi_lab.infra.artifacts import _checked_path
from rsi_lab.learning.contracts import require_sha
from rsi_lab.serialization import canonical_bytes, decode_json, digest


class ActivationController:
    def __init__(self, path: Path, protocol_sha256: str):
        require_sha(protocol_sha256)
        self.path = _checked_path(Path(path))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.protocol_sha256 = protocol_sha256
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS intents(operation TEXT PRIMARY KEY, request_hash TEXT NOT NULL, command BLOB NOT NULL, receipt BLOB)")
            db.execute("CREATE TABLE IF NOT EXISTS decisions(update_id TEXT PRIMARY KEY, body BLOB NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS protocol(id INTEGER PRIMARY KEY, hash TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO protocol VALUES(1,?)", (protocol_sha256,))
            if db.execute("SELECT hash FROM protocol WHERE id=1").fetchone()[0] != protocol_sha256:
                raise ConflictError("Controller protocol changed")

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
    def _check_adapter(adapter, prepared):
        descriptor = adapter.describe()
        capabilities = descriptor.capabilities
        if not (capabilities.snapshot_restore and capabilities.pre_activation
                and capabilities.reconcile_activation):
            raise UnsupportedCapability("Activation requires snapshot, pre-activation and reconciliation capabilities")
        for snapshot in (prepared.base, prepared.draft.candidate):
            if (snapshot.version.system_id != descriptor.system_id or
                    snapshot.version.upstream_revision != descriptor.upstream_revision):
                raise IntegrityError("Candidate or base does not match the registered adapter implementation")
        before = {c.name: c for c in prepared.base.version.components}
        after = {c.name: c for c in prepared.draft.candidate.version.components}
        changed = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
        if changed - set(capabilities.mutable_components):
            raise UnsupportedCapability("Update changes undeclared components: " + ", ".join(sorted(changed - set(capabilities.mutable_components))))

    def apply(self, adapter, prepared, decision, *, environment_sha256):
        if type(prepared) is not PreparedUpdate or type(decision) is not AdmissionDecision:
            raise ValidationError("Activation requires a typed prepared update and admission decision")
        if decision.identity != prepared.identity or decision.identity.protocol_sha256 != self.protocol_sha256:
            raise ValidationError("Decision is not bound to this candidate and protocol")
        if environment_sha256 != prepared.identity.environment_sha256:
            raise ConflictError("Audited environment changed")
        self._check_adapter(adapter, prepared)
        if decision.disposition == "accept" and not prepared.draft.native_valid:
            raise ValidationError("Cannot activate a candidate that failed native validation")
        command = ActivationCommand(prepared.identity.update_id + "/activate", prepared.identity,
                                    digest(decision), prepared.draft.candidate)
        request_hash = digest(command)
        with self._db() as db:
            previous = db.execute("SELECT body FROM decisions WHERE update_id=?", (prepared.identity.update_id,)).fetchone()
            if previous and previous[0] != canonical_bytes(decision):
                raise ConflictError("A different admission decision is already recorded for this update")
            row = db.execute("SELECT request_hash,receipt FROM intents WHERE operation=?", (command.operation_id,)).fetchone()
            if row:
                if row[0] != request_hash:
                    raise ConflictError("Activation intent identity reused")
            elif adapter.snapshot().sha256 != prepared.base.sha256:
                raise ConflictError("Audited system state changed before activation")
            db.execute("INSERT OR IGNORE INTO decisions VALUES(?,?)", (prepared.identity.update_id, canonical_bytes(decision)))
            # Rejection never calls activation or incorporates proposed inheritance state.
            if decision.disposition != "accept":
                return None
            if not row:
                db.execute("INSERT INTO intents VALUES(?,?,?,NULL)", (command.operation_id, request_hash, canonical_bytes(command)))
        # Intent is durable before any adapter side effect. Never replay an unknown call.
        if row:
            receipt = ActivationReceipt.from_dict(decode_json(row[1])) if row[1] else adapter.activation_status(command.operation_id)
            if receipt is None:
                raise OutcomeUnknown("Activation intent exists without a known receipt; reconcile, do not resend")
        else:
            receipt = adapter.activate(command)
        if (type(receipt) is not ActivationReceipt or receipt.operation_id != command.operation_id or
                receipt.command_sha256 != request_hash or receipt.before_sha256 != prepared.base.sha256 or
                receipt.after_sha256 != prepared.draft.candidate.sha256):
            raise IntegrityError("Adapter activation receipt differs from authorization")
        # Historical success is not permission to rewind a newer deployed version.
        if adapter.snapshot().sha256 != receipt.after_sha256:
            raise ConflictError("Receipt is historical; actual active system has advanced")
        with self._db() as db:
            db.execute("UPDATE intents SET receipt=? WHERE operation=?", (canonical_bytes(receipt), command.operation_id))
        return receipt
