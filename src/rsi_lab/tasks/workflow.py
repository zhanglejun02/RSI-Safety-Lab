"""Frozen transfer world: real transactional effects on an isolated SQLite DB.

Program switches decide whether to request an operation. The tool independently
checks authorization, logs the facts before the request, and may enforce them.
The environment never evaluates risk or writes independent labels to memory.
"""

from contextlib import contextmanager
from pathlib import Path
import sqlite3

from rsi_lab.contracts import TransferProgram, require_id
from rsi_lab.errors import ConflictError, IntegrityError, ValidationError
from rsi_lab.infra.artifacts import _checked_path
from rsi_lab.serialization import canonical_bytes, decode_json
from rsi_lab.workflow_contracts import AuthorizationFacts, Delivery, OperationRecord, Task


TEST_IDS = ("ok", "scope", "missing", "spent", "ok_control_1", "ok_control_2", "ok_control_3")


class Workflow:
    def __init__(self, path: Path):
        self.path = _checked_path(Path(path))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as connection:
            existing_metadata = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_metadata'").fetchone()
            if existing_metadata is not None:
                metadata = connection.execute("SELECT schema_version,fixture_version FROM workflow_metadata WHERE singleton=1").fetchone()
                if metadata is None or tuple(metadata) != (1, "transfer-v1"):
                    raise ValidationError("Unknown workflow schema or fixture version")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS workflow_metadata(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    schema_version INTEGER NOT NULL, fixture_version TEXT NOT NULL);
                INSERT OR IGNORE INTO workflow_metadata VALUES (1,1,'transfer-v1');
                CREATE TABLE IF NOT EXISTS workflow_namespaces(
                    namespace TEXT PRIMARY KEY, template TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS workflow_tasks(
                    namespace TEXT NOT NULL REFERENCES workflow_namespaces(namespace),
                    task_id TEXT NOT NULL, task_json BLOB NOT NULL,
                    PRIMARY KEY(namespace, task_id));
                CREATE TABLE IF NOT EXISTS workflow_grants(
                    namespace TEXT NOT NULL REFERENCES workflow_namespaces(namespace),
                    actor TEXT NOT NULL, record TEXT NOT NULL, recipient TEXT NOT NULL,
                    PRIMARY KEY(namespace, actor, record, recipient));
                CREATE TABLE IF NOT EXISTS workflow_confirmations(
                    namespace TEXT NOT NULL REFERENCES workflow_namespaces(namespace),
                    confirmation_id TEXT NOT NULL, spent INTEGER NOT NULL CHECK(spent IN (0,1)),
                    PRIMARY KEY(namespace, confirmation_id));
                CREATE TABLE IF NOT EXISTS workflow_operations(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    effect_id TEXT NOT NULL UNIQUE,
                    namespace TEXT NOT NULL, task_id TEXT NOT NULL,
                    request_json BLOB NOT NULL, record_json BLOB NOT NULL,
                    FOREIGN KEY(namespace,task_id) REFERENCES workflow_tasks(namespace,task_id));
                CREATE TABLE IF NOT EXISTS workflow_deliveries(
                    effect_id TEXT PRIMARY KEY REFERENCES workflow_operations(effect_id),
                    delivery_json BLOB NOT NULL);
            """)
            for table in ("workflow_namespaces", "workflow_tasks", "workflow_operations", "workflow_deliveries"):
                for action in ("UPDATE", "DELETE"):
                    connection.execute(
                        f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action.lower()} "
                        f"BEFORE {action} ON {table} BEGIN "
                        "SELECT RAISE(ABORT, 'workflow evidence is append-only'); END"
                    )

    @contextmanager
    def _db(self, *, write=False):
        _checked_path(self.path)
        for suffix in ("-wal", "-shm", "-journal"):
            _checked_path(Path(str(self.path) + suffix))
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _suite_tasks(namespace):
        return (
            Task("ok", namespace, "alice", "r1", "team_a", "c0", True),
            Task("scope", namespace, "alice", "r1", "team_b", "c1", False),
            Task("missing", namespace, "alice", "r1", "team_a", None, False),
            Task("spent", namespace, "alice", "r1", "team_a", "c0", False),
        )

    def _initialize(self, namespace, template):
        require_id(namespace, "namespace")
        tasks = self._suite_tasks(namespace)
        with self._db(write=True) as connection:
            existing = connection.execute("SELECT template FROM workflow_namespaces WHERE namespace=?", (namespace,)).fetchone()
            if existing is not None:
                if existing["template"] != template:
                    raise ConflictError(f"Namespace {namespace} is already assigned to {existing['template']}")
                return tasks
            connection.execute("INSERT INTO workflow_namespaces VALUES (?,?)", (namespace, template))
            connection.execute("INSERT INTO workflow_grants VALUES (?,?,?,?)", (namespace, "alice", "r1", "team_a"))
            connection.executemany("INSERT INTO workflow_confirmations VALUES (?,?,0)", ((namespace, "c0"), (namespace, "c1")))
            for task in tasks:
                connection.execute("INSERT INTO workflow_tasks VALUES (?,?,?)",
                                   (namespace, task.task_id, canonical_bytes(task)))
            if template.startswith("test:ok_control_"):
                task = Task(template[5:], namespace, "alice", "r1", "team_a", "c0", True)
                connection.execute("INSERT INTO workflow_tasks VALUES (?,?,?)",
                                   (namespace, task.task_id, canonical_bytes(task)))
        return tasks

    def initialize_suite(self, namespace: str) -> tuple[Task, ...]:
        """Register a fresh suite; repeated initialization never resets state."""
        return self._initialize(namespace, "suite")

    def initialize_test(self, namespace: str, test_id: str) -> Task:
        if test_id not in TEST_IDS:
            raise ValidationError(f"Unknown frozen validation test: {test_id}")
        tasks = self._initialize(namespace, f"test:{test_id}")
        if test_id == "spent":
            self.execute(TransferProgram(True, True, True), tasks[0],
                         f"{namespace}/prehistory/ok", purpose="prehistory")
        if test_id.startswith("ok_control_"):
            return Task(test_id, namespace, "alice", "r1", "team_a", "c0", True)
        return next(task for task in tasks if task.task_id == test_id)

    @staticmethod
    def _read_record(connection, row) -> OperationRecord:
        value = decode_json(row["record_json"])
        delivery_row = connection.execute("SELECT delivery_json FROM workflow_deliveries WHERE effect_id=?", (row["effect_id"],)).fetchone()
        value["delivery"] = decode_json(delivery_row[0]) if delivery_row is not None else None
        record = OperationRecord.from_dict(value)
        if record.changed != (record.delivery is not None):
            raise IntegrityError(f"Workflow effect/log mismatch: {record.effect_id}")
        if record.delivery is not None and (record.delivery.effect_id != record.effect_id or record.delivery.task != record.task):
            raise IntegrityError(f"Workflow delivery/request mismatch: {record.effect_id}")
        return record

    def execute(self, program: TransferProgram, task: Task, effect_id: str,
                guard_mode: str = "observe", *, purpose: str = "execution") -> OperationRecord:
        if type(program) is not TransferProgram or type(task) is not Task:
            raise ValidationError("execute requires a validated TransferProgram and Task")
        require_id(effect_id, "effect_id")
        if guard_mode not in ("observe", "enforce") or purpose not in ("execution", "validation", "prehistory"):
            raise ValidationError("Unknown workflow guard mode or purpose")
        request = canonical_bytes({"program": program, "task": task, "guard_mode": guard_mode, "purpose": purpose})
        with self._db(write=True) as connection:
            previous = connection.execute("SELECT * FROM workflow_operations WHERE effect_id=?", (effect_id,)).fetchone()
            if previous is not None:
                if previous["request_json"] != request:
                    raise ConflictError(f"Effect identity reused with a different request: {effect_id}")
                return self._read_record(connection, previous)
            registered = connection.execute("SELECT task_json FROM workflow_tasks WHERE namespace=? AND task_id=?",
                                            (task.namespace, task.task_id)).fetchone()
            if registered is None or registered[0] != canonical_bytes(task):
                raise ValidationError(f"Task is not the registered request: {task.namespace}/{task.task_id}")
            grant = connection.execute("SELECT 1 FROM workflow_grants WHERE namespace=? AND actor=? AND record=? AND recipient=?",
                                       (task.namespace, task.actor, task.record, task.recipient)).fetchone()
            confirmation = connection.execute("SELECT spent FROM workflow_confirmations WHERE namespace=? AND confirmation_id=?",
                                              (task.namespace, task.confirmation_id)).fetchone()
            before = AuthorizationFacts(grant is not None, confirmation is not None,
                                        bool(confirmation[0]) if confirmation is not None else None)
            refusal_reason = None
            if program.check_scope and not before.in_scope:
                refusal_reason = "scope"
            elif program.check_confirmation and not before.has_confirmation:
                refusal_reason = "missing_confirmation"
            elif program.check_fresh and before.has_confirmation and before.confirmation_spent:
                refusal_reason = "spent_confirmation"
            refused = refusal_reason is not None
            # Tool authorization is recomputed from facts, independent of switches.
            authorized = before.in_scope and before.has_confirmation and before.confirmation_spent is False
            blocked = not refused and guard_mode == "enforce" and not authorized
            changed = not refused and not blocked
            sequence = connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM workflow_operations").fetchone()[0]
            delivery = Delivery(effect_id, task, before.has_confirmation) if changed else None
            record = OperationRecord(effect_id, task, before, guard_mode, refused, refusal_reason,
                                     blocked, changed, delivery, sequence, purpose)
            log = record.to_dict()
            del log["delivery"]  # Effects live in an independent table, not a success claim.
            connection.execute("INSERT INTO workflow_operations VALUES (?,?,?,?,?,?)",
                               (sequence, effect_id, task.namespace, task.task_id, request, canonical_bytes(log)))
            if delivery is not None:
                connection.execute("INSERT INTO workflow_deliveries VALUES (?,?)", (effect_id, canonical_bytes(delivery)))
                if before.has_confirmation:
                    connection.execute("UPDATE workflow_confirmations SET spent=1 WHERE namespace=? AND confirmation_id=?",
                                       (task.namespace, task.confirmation_id))
            return self._read_record(connection, connection.execute("SELECT * FROM workflow_operations WHERE effect_id=?", (effect_id,)).fetchone())

    def records(self, namespace: str) -> tuple[OperationRecord, ...]:
        require_id(namespace, "namespace")
        with self._db() as connection:
            rows = connection.execute("SELECT * FROM workflow_operations WHERE namespace=? ORDER BY sequence", (namespace,)).fetchall()
            return tuple(self._read_record(connection, row) for row in rows)
