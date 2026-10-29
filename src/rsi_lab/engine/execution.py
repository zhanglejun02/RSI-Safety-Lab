"""Execute the actual deployed version, with durable effects and pause provenance."""

from dataclasses import replace
from pathlib import Path

from rsi_lab.contracts import (
    ArtifactRef,
    CursorFamily,
    SnapshotManifest,
    TaskCursor,
    Version,
)
from rsi_lab.errors import (
    BudgetExceeded,
    ConflictError,
    PermissionDenied,
    ValidationError,
)
from rsi_lab.infra.snapshots import backup_sqlite
from rsi_lab.oracles.workflow import evaluate
from rsi_lab.serialization import canonical_bytes, decode_json, digest
from rsi_lab.tasks.workflow import Workflow
from rsi_lab.workflow_contracts import Evaluation


class DeploymentRuntime:
    def __init__(self, state, store, journal, controller, *, fault_hook=None):
        self.state, self.store, self.journal, self.controller = (
            state,
            store,
            journal,
            controller,
        )
        self.fault_hook = fault_hook or (lambda point: None)
        with state._db(True) as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS deployed_suites(
                    suite_id TEXT PRIMARY KEY, request_json BLOB NOT NULL, base_snapshot BLOB NOT NULL,
                    result_json BLOB, environment_json BLOB, cursors_json BLOB, installed INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS branch_pauses(
                    branch_id TEXT PRIMARY KEY, suite_id TEXT NOT NULL, operation_id TEXT NOT NULL,
                    deployed_version_json BLOB NOT NULL, trace_json BLOB NOT NULL);
            """
            )

    def is_paused(self, branch_id):
        with self.state._db() as db:
            return (
                db.execute(
                    "SELECT 1 FROM branch_pauses WHERE branch_id=?", (branch_id,)
                ).fetchone()
                is not None
            )

    def run_suite(
        self,
        branch_id,
        attempt_id,
        *,
        purpose="discovery",
        cursor_family=CursorFamily.PUBLIC_DEVELOPMENT,
        namespace=None,
        pause_on_violation=True,
        guard_mode="observe",
    ):
        if purpose not in ("discovery", "online_audit") or guard_mode not in (
            "observe",
            "enforce",
        ):
            raise ValidationError("Unsupported deployed suite protocol")
        expected_family = {
            "discovery": CursorFamily.PUBLIC_DEVELOPMENT,
            "online_audit": CursorFamily.ADMISSION_PROBE,
        }[purpose]
        if cursor_family != expected_family:
            raise ValidationError("Deployed suite purpose/cursor mismatch")
        branch = self.controller.authorize_runtime(branch_id)
        if branch["role"] != ("probe" if purpose == "online_audit" else "main"):
            raise PermissionDenied(
                "Branch role cannot produce requested deployment evidence"
            )
        if purpose == "online_audit" and pause_on_violation:
            raise ValidationError(
                "Probe fixture must measure complete suites without main pause policy"
            )
        namespace = namespace or f"{attempt_id}/deployment"
        suite_id = f"{branch_id}/{namespace}"
        request = {
            "branch_id": branch_id,
            "attempt_id": attempt_id,
            "purpose": purpose,
            "cursor_family": cursor_family.value,
            "namespace": namespace,
            "guard_mode": guard_mode,
            "pause_on_violation": pause_on_violation,
        }
        with self.state._db(True) as db:
            row = db.execute(
                "SELECT * FROM deployed_suites WHERE suite_id=?", (suite_id,)
            ).fetchone()
            if row:
                if row["request_json"] != canonical_bytes(request):
                    raise ConflictError("Deployment suite identity reused")
            else:
                if self.is_paused(branch_id):
                    raise PermissionDenied(
                        f"Branch {branch_id} paused after confirmed public deployment violation"
                    )
                db.execute(
                    "INSERT INTO deployed_suites(suite_id,request_json,base_snapshot) VALUES (?,?,?)",
                    (
                        suite_id,
                        canonical_bytes(request),
                        canonical_bytes(branch["head"]),
                    ),
                )
                row = db.execute(
                    "SELECT * FROM deployed_suites WHERE suite_id=?", (suite_id,)
                ).fetchone()
        if row["result_json"]:
            return self._finish(suite_id, row)
        base_ref = ArtifactRef.from_dict(decode_json(row["base_snapshot"]))
        base = SnapshotManifest.from_dict(decode_json(self.store.read_bytes(base_ref)))
        version = Version.from_dict(
            decode_json(self.store.read_bytes(base.deployed_version))
        )
        world = Workflow(self.state.working_database(branch_id))
        tasks = world.initialize_suite(namespace)
        for task in tasks:
            operation = f"{attempt_id}/deployment/{task.task_id}"
            # A durable incident blocks the next external task, including within this suite.
            if pause_on_violation and self.is_paused(branch_id):
                break
            try:
                record = self.journal.reserve(
                    operation,
                    "branch",
                    branch_id,
                    1,
                    purpose,
                    {
                        "suite_id": suite_id,
                        "task": task.to_dict(),
                        "version": base.deployed_version.to_dict(),
                        "guard_mode": guard_mode,
                    },
                    attempt_id=attempt_id,
                )
            except BudgetExceeded:
                break
            if record["status"] == "settled":
                traces = tuple(
                    r for r in world.records(namespace) if r.effect_id == operation
                )
                self._maybe_pause(
                    branch_id,
                    suite_id,
                    operation,
                    base.deployed_version,
                    traces,
                    task,
                    pause_on_violation,
                )
                continue
            if record["status"] in ("outcome_unknown", "released"):
                break
            self.journal.record_exposure(
                operation + "/exposure",
                operation,
                cursor_family.value,
                task.task_id,
                branch_id=branch_id,
            )
            try:
                world.execute(
                    version.agent,
                    task,
                    operation,
                    guard_mode=guard_mode,
                    purpose="execution",
                )
            except Exception as exc:
                self.journal.mark_unknown(operation, f"{type(exc).__name__}: {exc}")
                # An unavailable response does not erase confirmed local effects.
                # Preserve the cost reservation, while pausing on durable harm.
                traces = tuple(
                    r for r in world.records(namespace) if r.effect_id == operation
                )
                self._maybe_pause(
                    branch_id,
                    suite_id,
                    operation,
                    base.deployed_version,
                    traces,
                    task,
                    pause_on_violation,
                )
                break
            self.fault_hook("after_deployment_effect")
            traces = tuple(
                r for r in world.records(namespace) if r.effect_id == operation
            )
            trace_ref = self.store.put_json(
                {
                    "records": traces,
                    "deployed_version": base.deployed_version,
                    "purpose": purpose,
                }
            )
            self.journal.settle(operation, 1, {"trace": trace_ref.to_dict()})
            self._maybe_pause(
                branch_id,
                suite_id,
                operation,
                base.deployed_version,
                traces,
                task,
                pause_on_violation,
            )
        traces = world.records(namespace)
        evaluation = evaluate(traces, tasks)
        environment = backup_sqlite(world.path, self.store)
        cursors = list(base.cursors)
        target_namespace = (
            f"{attempt_id}/deployment" if purpose == "online_audit" else "main"
        )
        count = sum(1 for record in traces if record.task in tasks)
        for i, c in enumerate(cursors):
            if c.family == cursor_family and c.namespace == target_namespace:
                cursors[i] = replace(c, position=c.position + count)
                break
        else:
            cursors.append(
                TaskCursor(cursor_family, position=count, namespace=target_namespace)
            )
        with self.state._db(True) as db:
            db.execute(
                "UPDATE deployed_suites SET result_json=?,environment_json=?,cursors_json=? WHERE suite_id=?",
                (
                    canonical_bytes(evaluation),
                    canonical_bytes(environment),
                    canonical_bytes({"cursors": tuple(cursors)}),
                    suite_id,
                ),
            )
            row = db.execute(
                "SELECT * FROM deployed_suites WHERE suite_id=?", (suite_id,)
            ).fetchone()
        return self._finish(suite_id, row)

    def _maybe_pause(
        self, branch_id, suite_id, operation, deployed_version, traces, task, enabled
    ):
        if enabled and evaluate(traces, (task,)).actual_violations:
            trace_ref = self.store.put_json(
                {
                    "records": traces,
                    "deployed_version": deployed_version,
                    "purpose": "discovery",
                }
            )
            with self.state._db(True) as db:
                existing = db.execute(
                    "SELECT 1 FROM branch_pauses WHERE branch_id=?", (branch_id,)
                ).fetchone()
                if not existing:
                    db.execute(
                        "INSERT INTO branch_pauses VALUES (?,?,?,?,?)",
                        (
                            branch_id,
                            suite_id,
                            operation,
                            canonical_bytes(deployed_version),
                            canonical_bytes(trace_ref),
                        ),
                    )
                    self.state._event(
                        db,
                        "unsafe_current_pause",
                        branch_id,
                        {
                            "suite_id": suite_id,
                            "operation_id": operation,
                            "deployed_version": deployed_version,
                            "trace": trace_ref,
                            "purpose": "public_development",
                        },
                    )

    def _finish(self, suite_id, row):
        if not row["installed"]:
            request = decode_json(row["request_json"])
            environment = ArtifactRef.from_dict(decode_json(row["environment_json"]))
            cursors = tuple(
                TaskCursor.from_dict(c)
                for c in decode_json(row["cursors_json"])["cursors"]
            )
            self.controller.record_runtime_snapshot(
                request["branch_id"], environment, cursors
            )
            with self.state._db(True) as db:
                db.execute(
                    "UPDATE deployed_suites SET installed=1 WHERE suite_id=?",
                    (suite_id,),
                )
        return Evaluation.from_dict(decode_json(row["result_json"]))
