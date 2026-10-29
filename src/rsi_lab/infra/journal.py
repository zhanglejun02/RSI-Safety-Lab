"""Controller-owned, durable synthetic resource and task-exposure ledger.

An operation is reserved before dispatch. A missing response is *unknown*, not
free. Only a final receipt can settle its consumption; only explicit evidence
of non-execution can cancel it. This is accounting, not an exactly-once remote
execution guarantee. A real retry must have a new operation identity.

Amounts in this M1 backend are synthetic cost units, never currency or tokens.
Actual consumption exceeding a reservation is retained and blocks later work;
an inaccurate estimate must not erase an already incurred charge.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from rsi_lab.errors import BudgetExceeded, ConflictError, ValidationError
from rsi_lab.infra.artifacts import _checked_path
from rsi_lab.serialization import canonical_bytes, decode_json


OWNER_KINDS = frozenset({"pair", "branch"})
PURPOSES = frozenset(
    {"discovery", "online_audit", "long_label", "final_eval", "training"}
)
CURSOR_FAMILIES = frozenset(
    {"public_development", "admission_probe", "independent_evaluation"}
)
PARTITIONS = frozenset({"train", "dev", "calibration", "sealed"})


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValidationError(f"{name} must be nonempty text without NUL")
    return value


def _units(value: Any, name: str) -> int:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValidationError(f"{name} must be a nonnegative signed 64-bit integer")
    return value


def _choice(value: Any, name: str, choices: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ValidationError(f"unsupported {name}: {value!r}; expected {sorted(choices)}")
    return value


def _json(value: Any, name: str) -> str:
    if not isinstance(value, dict):
        raise ValidationError(f"{name} must be a JSON object")
    return canonical_bytes(value).decode("utf-8")


class Journal:
    """Append-preserving accounting with serialized budget admission.

    Connections and transactions are short lived, so caller-side execution or
    network waits never hold the ledger's write lock. Passing the same identity
    and same payload is idempotent; a conflicting payload always raises.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = _checked_path(Path(path))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            metadata_exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='resource_journal_metadata'"
            ).fetchone()
            if metadata_exists is not None:
                version = db.execute(
                    "SELECT schema_version FROM resource_journal_metadata WHERE singleton=1"
                ).fetchone()
                if version is None or version[0] != 1:
                    raise ValidationError(f"unsupported resource journal schema_version={None if version is None else version[0]}")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS resource_journal_metadata (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    schema_version INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO resource_journal_metadata VALUES (1, 1);
                CREATE TABLE IF NOT EXISTS budgets (
                    owner_kind TEXT NOT NULL CHECK(owner_kind IN ('pair', 'branch')),
                    owner_id TEXT NOT NULL,
                    limit_units INTEGER NOT NULL CHECK(limit_units >= 0),
                    PRIMARY KEY(owner_kind, owner_id)
                );
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    owner_kind TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    attempt_id TEXT,
                    purpose TEXT NOT NULL CHECK(purpose IN
                        ('discovery', 'online_audit', 'long_label', 'final_eval', 'training')),
                    reserved_units INTEGER NOT NULL CHECK(reserved_units >= 0),
                    request_json TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                        ('reserved', 'outcome_unknown', 'settled', 'released')),
                    actual_units INTEGER,
                    receipt_json TEXT,
                    receipt_sha256 TEXT,
                    unknown_reason TEXT,
                    release_reason TEXT,
                    FOREIGN KEY(owner_kind, owner_id) REFERENCES budgets(owner_kind, owner_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_index INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT REFERENCES operations(operation_id),
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cost_events (
                    operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id),
                    owner_kind TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    attempt_id TEXT,
                    purpose TEXT NOT NULL,
                    event_type TEXT NOT NULL CHECK(event_type = 'settled_charge'),
                    synthetic_cost_units INTEGER NOT NULL CHECK(synthetic_cost_units > 0),
                    monetary_amount TEXT CHECK(monetary_amount IS NULL),
                    receipt_sha256 TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS exposure_events (
                    exposure_id TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
                    cursor_family TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    branch_id TEXT NOT NULL,
                    partition TEXT
                );
                CREATE INDEX IF NOT EXISTS operations_owner ON operations(owner_kind, owner_id);
                CREATE INDEX IF NOT EXISTS exposures_operation ON exposure_events(operation_id);
                """
            )
            version = db.execute(
                "SELECT schema_version FROM resource_journal_metadata WHERE singleton=1"
            ).fetchone()[0]
            if version != 1:
                raise ValidationError(f"unsupported resource journal schema_version={version}")
            for table in ("events", "cost_events", "exposure_events"):
                for action in ("UPDATE", "DELETE"):
                    db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action.lower()} "
                        f"BEFORE {action} ON {table} BEGIN "
                        "SELECT RAISE(ABORT, 'ledger evidence is append-only'); END"
                    )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # Recheck on every open, including SQLite's implicit sidecar paths.
        # These checks prevent obvious substitutions in trusted_fixture mode;
        # they do not claim race-proof filesystem or OS process isolation.
        _checked_path(self.path)
        for suffix in ("-wal", "-shm", "-journal"):
            _checked_path(Path(str(self.path) + suffix))
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()

    @staticmethod
    def _event(db: sqlite3.Connection, operation_id: str | None, kind: str,
               payload: dict[str, Any]) -> None:
        db.execute(
            "INSERT INTO events(operation_id, event_type, payload_json) VALUES (?, ?, ?)",
            (operation_id, kind, _json(payload, "event payload")),
        )

    @staticmethod
    def _operation(db: sqlite3.Connection, operation_id: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if row is None:
            raise ValidationError(f"unknown operation_id: {operation_id}")
        return row

    @staticmethod
    def _as_operation(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["request"] = decode_json(result.pop("request_json").encode())
        receipt = result.pop("receipt_json")
        result["receipt"] = None if receipt is None else decode_json(receipt.encode())
        result["outstanding_reserved_units"] = (
            result["reserved_units"]
            if result["status"] in ("reserved", "outcome_unknown") else 0
        )
        return result

    @staticmethod
    def _usage(db: sqlite3.Connection, owner_kind: str, owner_id: str) -> tuple[int, int]:
        # Confirmed overruns can push an account beyond SQLite's integer SUM
        # range. Python integers preserve all incurred consumption in that case.
        rows = db.execute(
            "SELECT status, actual_units, reserved_units FROM operations WHERE owner_kind=? AND owner_id=?",
            (owner_kind, owner_id),
        ).fetchall()
        return (sum(row["actual_units"] for row in rows if row["status"] == "settled"),
                sum(row["reserved_units"] for row in rows
                    if row["status"] in ("reserved", "outcome_unknown")))

    def create_budget(self, owner_kind: str, owner_id: str, limit_units: int) -> dict[str, Any]:
        _choice(owner_kind, "owner_kind", OWNER_KINDS)
        _text(owner_id, "owner_id")
        _units(limit_units, "limit_units")
        with self._write() as db:
            row = db.execute(
                "SELECT * FROM budgets WHERE owner_kind=? AND owner_id=?", (owner_kind, owner_id)
            ).fetchone()
            if row is not None:
                if row["limit_units"] != limit_units:
                    raise ConflictError(f"budget {owner_kind}/{owner_id} already has a different limit")
                return dict(row)
            db.execute("INSERT INTO budgets VALUES (?, ?, ?)", (owner_kind, owner_id, limit_units))
            payload = {"owner_kind": owner_kind, "owner_id": owner_id, "limit_units": limit_units}
            self._event(db, None, "budget_created", payload)
            return payload

    def reserve(self, operation_id: str, owner_kind: str, owner_id: str,
                amount: int, purpose: str, request: dict[str, Any], *,
                attempt_id: str | None = None) -> dict[str, Any]:
        _text(operation_id, "operation_id")
        _choice(owner_kind, "owner_kind", OWNER_KINDS)
        _text(owner_id, "owner_id")
        _units(amount, "amount")
        _choice(purpose, "purpose", PURPOSES)
        if attempt_id is not None:
            _text(attempt_id, "attempt_id")
        request_json = _json(request, "request")
        request_sha256 = hashlib.sha256(request_json.encode()).hexdigest()
        with self._write() as db:
            row = db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row is not None:
                expected = (owner_kind, owner_id, attempt_id, amount, purpose, request_json)
                actual = tuple(row[k] for k in (
                    "owner_kind", "owner_id", "attempt_id", "reserved_units", "purpose", "request_json"
                ))
                if actual != expected:
                    raise ConflictError(f"operation {operation_id}: conflicting reservation replay")
                return self._as_operation(row)
            budget = db.execute(
                "SELECT limit_units FROM budgets WHERE owner_kind=? AND owner_id=?", (owner_kind, owner_id)
            ).fetchone()
            if budget is None:
                raise ValidationError(f"operation {operation_id}: budget {owner_kind}/{owner_id} does not exist")
            spent, outstanding = self._usage(db, owner_kind, owner_id)
            if spent + outstanding + amount > budget[0]:
                raise BudgetExceeded(
                    f"operation {operation_id}: needs {amount} synthetic units; "
                    f"{owner_kind}/{owner_id} limit={budget[0]}, spent={spent}, reserved={outstanding}"
                )
            db.execute(
                """INSERT INTO operations(operation_id, owner_kind, owner_id, attempt_id, purpose,
                    reserved_units, request_json, request_sha256, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'reserved')""",
                (operation_id, owner_kind, owner_id, attempt_id, purpose, amount, request_json, request_sha256),
            )
            self._event(db, operation_id, "reserve", {"synthetic_cost_units": amount,
                        "request_sha256": request_sha256})
            return self._as_operation(self._operation(db, operation_id))

    def settle(self, operation_id: str, actual_units: int, receipt: dict[str, Any]) -> dict[str, Any]:
        _text(operation_id, "operation_id")
        _units(actual_units, "actual_units")
        receipt_json = _json(receipt, "receipt")
        receipt_sha256 = hashlib.sha256(receipt_json.encode()).hexdigest()
        with self._write() as db:
            row = self._operation(db, operation_id)
            if row["status"] == "settled":
                if row["actual_units"] != actual_units or row["receipt_json"] != receipt_json:
                    raise ConflictError(f"operation {operation_id}: conflicting final receipt")
                return self._as_operation(row)
            if row["status"] == "released":
                raise ConflictError(f"operation {operation_id}: cannot settle after confirmed non-execution")
            db.execute(
                "UPDATE operations SET status='settled', actual_units=?, receipt_json=?, "
                "receipt_sha256=? WHERE operation_id=?",
                (actual_units, receipt_json, receipt_sha256, operation_id),
            )
            if actual_units > 0:
                db.execute(
                    "INSERT INTO cost_events VALUES (?, ?, ?, ?, ?, 'settled_charge', ?, NULL, ?)",
                    (operation_id, row["owner_kind"], row["owner_id"], row["attempt_id"],
                     row["purpose"], actual_units, receipt_sha256),
                )
            self._event(db, operation_id, "settle", {"synthetic_cost_units": actual_units,
                        "receipt_sha256": receipt_sha256})
            remainder = row["reserved_units"] - actual_units
            if remainder > 0:
                self._event(db, operation_id, "release_remainder", {"synthetic_cost_units": remainder,
                            "reason": "final_receipt_confirms_consumption"})
            elif remainder < 0:
                self._event(db, operation_id, "reservation_overrun", {
                    "synthetic_cost_units": -remainder,
                    "reason": "actual_consumption_retained",
                })
            return self._as_operation(self._operation(db, operation_id))

    def mark_unknown(self, operation_id: str, reason: str) -> dict[str, Any]:
        _text(operation_id, "operation_id")
        _text(reason, "reason")
        with self._write() as db:
            row = self._operation(db, operation_id)
            if row["status"] == "outcome_unknown":
                if row["unknown_reason"] != reason:
                    raise ConflictError(f"operation {operation_id}: conflicting unknown-result reason")
                return self._as_operation(row)
            if row["status"] != "reserved":
                raise ConflictError(f"operation {operation_id}: completed outcome cannot become unknown")
            db.execute("UPDATE operations SET status='outcome_unknown', unknown_reason=? WHERE operation_id=?",
                       (reason, operation_id))
            self._event(db, operation_id, "outcome_unknown", {"reason": reason})
            return self._as_operation(self._operation(db, operation_id))

    def release(self, operation_id: str, reason: str, *, known_not_executed: bool = False) -> dict[str, Any]:
        _text(operation_id, "operation_id")
        _text(reason, "reason")
        if known_not_executed is not True:
            raise ValidationError(f"operation {operation_id}: releasing requires confirmed non-execution")
        with self._write() as db:
            row = self._operation(db, operation_id)
            if row["status"] == "released":
                if row["release_reason"] != reason:
                    raise ConflictError(f"operation {operation_id}: conflicting release evidence")
                return self._as_operation(row)
            if row["status"] == "settled":
                raise ConflictError(f"operation {operation_id}: incurred consumption cannot be released")
            exposed = db.execute(
                "SELECT 1 FROM exposure_events WHERE operation_id=? LIMIT 1", (operation_id,)
            ).fetchone()
            if exposed is not None:
                raise ConflictError(f"operation {operation_id}: exposure proves dispatch; settle a receipt instead")
            db.execute("UPDATE operations SET status='released', release_reason=? WHERE operation_id=?",
                       (reason, operation_id))
            self._event(db, operation_id, "release", {"synthetic_cost_units": row["reserved_units"],
                        "reason": reason, "known_not_executed": True})
            return self._as_operation(self._operation(db, operation_id))

    def record_exposure(self, exposure_id: str, operation_id: str, cursor_family: str,
                        task_id: str, *, branch_id: str, partition: str | None = None) -> dict[str, Any]:
        for name, value in (("exposure_id", exposure_id), ("operation_id", operation_id),
                            ("task_id", task_id), ("branch_id", branch_id)):
            _text(value, name)
        _choice(cursor_family, "cursor_family", CURSOR_FAMILIES)
        if cursor_family == "independent_evaluation":
            _choice(partition, "partition", PARTITIONS)
        elif partition is not None:
            raise ValidationError("only independent_evaluation exposures may carry a partition")
        payload = {"exposure_id": exposure_id, "operation_id": operation_id,
                   "cursor_family": cursor_family, "task_id": task_id,
                   "branch_id": branch_id, "partition": partition}
        with self._write() as db:
            operation = self._operation(db, operation_id)
            row = db.execute("SELECT * FROM exposure_events WHERE exposure_id=?", (exposure_id,)).fetchone()
            if row is not None:
                if dict(row) != payload:
                    raise ConflictError(f"exposure {exposure_id}: conflicting replay")
                return dict(row)
            if operation["status"] == "released":
                raise ConflictError(f"operation {operation_id}: cannot expose a confirmed unexecuted operation")
            if operation["owner_kind"] == "branch" and operation["owner_id"] != branch_id:
                raise ConflictError(f"operation {operation_id}: exposure branch differs from budget owner")
            expected_purpose = {"public_development": {"discovery"},
                                "admission_probe": {"online_audit"},
                                "independent_evaluation": {"long_label", "final_eval"}}[cursor_family]
            if operation["purpose"] not in expected_purpose:
                raise ValidationError(f"operation {operation_id}: purpose incompatible with cursor family")
            if cursor_family == "independent_evaluation":
                if (partition == "sealed") != (operation["purpose"] == "final_eval"):
                    raise ValidationError(f"operation {operation_id}: sealed partition requires final_eval purpose")
            db.execute("INSERT INTO exposure_events VALUES (?, ?, ?, ?, ?, ?)", tuple(payload.values()))
            self._event(db, operation_id, "exposure", payload)
            return payload

    def get_operation(self, operation_id: str) -> dict[str, Any]:
        _text(operation_id, "operation_id")
        with self._connect() as db:
            return self._as_operation(self._operation(db, operation_id))

    def events(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            result = []
            for row in db.execute("SELECT * FROM events ORDER BY event_index"):
                item = dict(row)
                item["payload"] = decode_json(item.pop("payload_json").encode())
                result.append(item)
            return result

    def costs(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM cost_events ORDER BY operation_id")]

    def exposures(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM exposure_events ORDER BY exposure_id")]

    def summary(self, owner_kind: str | None = None, owner_id: str | None = None) -> dict[str, Any]:
        if (owner_kind is None) != (owner_id is None):
            raise ValidationError("summary requires both owner_kind and owner_id, or neither")
        if owner_kind is not None:
            _choice(owner_kind, "owner_kind", OWNER_KINDS)
            _text(owner_id, "owner_id")
        with self._connect() as db:
            # One read transaction makes totals internally consistent during concurrent writes.
            db.execute("BEGIN")
            query = "SELECT * FROM budgets"
            args: tuple[str, ...] = ()
            if owner_kind is not None:
                query += " WHERE owner_kind=? AND owner_id=?"
                args = (owner_kind, owner_id)
            rows = db.execute(query + " ORDER BY owner_kind, owner_id", args).fetchall()
            if owner_kind is not None and not rows:
                raise ValidationError(f"unknown budget {owner_kind}/{owner_id}")
            accounts = []
            for row in rows:
                spent, outstanding = self._usage(db, row["owner_kind"], row["owner_id"])
                accounts.append({**dict(row), "settled_units": spent,
                                 "outstanding_reserved_units": outstanding,
                                 "available_units": max(0, row["limit_units"] - spent - outstanding),
                                 "over_budget": spent + outstanding > row["limit_units"]})
            suffix = "" if owner_kind is None else " WHERE owner_kind=? AND owner_id=?"
            cost_rows = db.execute("SELECT COUNT(*) FROM cost_events" + suffix, args).fetchone()[0]
            operations = db.execute("SELECT status FROM operations" + suffix, args).fetchall()
            return {"schema_version": 1, "unit": "synthetic_cost_units", "monetary_amount": None,
                    "settled_charge_rows": cost_rows,
                    "total_synthetic_units": sum(a["settled_units"] for a in accounts),
                    "pair_shared_units": sum(a["settled_units"] for a in accounts if a["owner_kind"] == "pair"),
                    "branch_units": sum(a["settled_units"] for a in accounts if a["owner_kind"] == "branch"),
                    "outstanding_reserved_units": sum(a["outstanding_reserved_units"] for a in accounts),
                    "operation_count": len(operations),
                    "unknown_operations": sum(row[0] == "outcome_unknown" for row in operations),
                    "accounts": accounts}
