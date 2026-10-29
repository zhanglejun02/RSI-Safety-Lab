"""Closed-loop deployment facts, resource uncertainty and process recovery."""

from dataclasses import replace
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys

import pytest

from rsi_lab.config import LabConfig
from rsi_lab.contracts import ArtifactRef, CursorFamily, TransferProgram, Version
from rsi_lab.engine.commit import LifecycleController
from rsi_lab.engine.execution import DeploymentRuntime
from rsi_lab.engine.state import StateStore
from rsi_lab.errors import PermissionDenied, ValidationError
from rsi_lab.infra.artifacts import ArtifactStore
from rsi_lab.infra.journal import Journal
from rsi_lab.infra.models import DurableFakeModelClient, FakeModelClient
from rsi_lab.infra.snapshots import backup_sqlite
from rsi_lab.serialization import decode_json
from rsi_lab.studies.closed_loop import create_run, resume_run
from rsi_lab.tasks.workflow import Workflow


def config(**changes):
    return replace(LabConfig(stage="M3", study="m3_smoke", subject="fake"), **changes)


def handles(path):
    store = ArtifactStore(path / "objects")
    return store, StateStore(path, store), Journal(path / "controller.sqlite")


def semantic_evidence(value, store):
    """Resolve actual immutable evidence, excluding only measured wall time.

    Validation timings legitimately alter archive/snapshot hashes across runs.
    Candidate bytes, environment bytes, logs, decisions and costs remain compared.
    """
    if isinstance(value, list):
        return [semantic_evidence(item, store) for item in value]
    if isinstance(value, dict):
        if set(value) == {"sha256", "size_bytes", "media_type", "schema_version"}:
            ref = ArtifactRef.from_dict(value)
            content = store.read_bytes(ref)
            if ref.media_type == "application/json":
                return semantic_evidence(decode_json(content), store)
            return value
        return {
            key: semantic_evidence(item, store)
            for key, item in value.items()
            if key != "wall_seconds"
        }
    return value


@pytest.fixture
def generate_unsafe(monkeypatch):
    # Explicit control response; the real generation/verification/deployment
    # path and accounting remain active, and no oracle labels enter generation.
    original = FakeModelClient.generate

    def unsafe(model, request_id, context_bytes, edit_base):
        response = original(model, request_id, context_bytes, edit_base)
        return replace(
            response,
            version=replace(response.version, agent=TransferProgram(False, True, True)),
        )

    monkeypatch.setattr(FakeModelClient, "generate", unsafe)


def test_safe_two_attempt_loop_installs_actual_environment_and_resumes(tmp_path):
    path = tmp_path / "safe"
    result = create_run(config(), path)
    assert result["accounting"]["total_synthetic_units"] == 20
    assert result["accounting"]["outstanding_reserved_units"] == 0
    assert not result["paused"]
    assert len(result["rounds"]) == 2
    assert all(
        r["decision"]["verdict"] == "accept"
        and r["public_evaluation"]["complete"]
        and r["public_evaluation"]["risk"] == 0
        for r in result["rounds"]
    )
    store, state, journal = handles(path)
    branch = state.get_branch("main")
    assert branch["head_revision"] == 2
    head = state.load_snapshot(ArtifactRef.from_dict(branch["head"]))
    assert head.environment == backup_sqlite(state.working_database("main"), store)
    assert {c.family: c.position for c in head.cursors} == {
        CursorFamily.PUBLIC_DEVELOPMENT: 10,
        CursorFamily.ADMISSION_PROBE: 0,
        CursorFamily.INDEPENDENT_EVALUATION: 0,
    }
    with sqlite3.connect(path / "provider.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM responses").fetchone()[0] == 2
    before = journal.costs()
    assert resume_run(path) == result
    assert journal.costs() == before


def test_unsafe_current_pauses_from_deployed_log_and_resume_is_read_only(
    tmp_path, generate_unsafe
):
    path = tmp_path / "unsafe"
    result = create_run(config(), path)
    assert result["paused"]
    assert result["rounds"][0]["decision"]["verdict"] == "accept"
    outcome = result["rounds"][0]["public_evaluation"]
    assert outcome["risk"] == 1 and outcome["actual_violations"] == 1
    assert outcome["completed_tasks"] == 2 and not outcome["complete"]
    assert result["rounds"][1] == {
        "attempt_index": 2,
        "status": "not_run",
        "reason": "unsafe_current_pause",
        "public_evaluation": None,
    }
    assert result["accounting"]["total_synthetic_units"] == 8
    assert result["accounting"]["outstanding_reserved_units"] == 0
    store, state, journal = handles(path)
    snapshot = state.load_snapshot(
        ArtifactRef.from_dict(state.get_branch("main")["head"])
    )
    with state._db() as db:
        pause = db.execute(
            "SELECT * FROM branch_pauses WHERE branch_id=?", ("main",)
        ).fetchone()
        assert (
            ArtifactRef.from_dict(decode_json(pause["deployed_version_json"]))
            == snapshot.deployed_version
        )
        trace = decode_json(
            store.read_bytes(ArtifactRef.from_dict(decode_json(pause["trace_json"])))
        )
    assert trace["purpose"] == "discovery"
    assert len(trace["records"]) == 1 and trace["records"][0]["changed"]
    assert trace["records"][0]["before"]["in_scope"] is False
    assert all(e["cursor_family"] == "public_development" for e in journal.exposures())
    before = journal.costs()
    assert resume_run(path) == result
    assert journal.costs() == before


def test_rejected_unsafe_candidate_does_not_pause_safe_deployed_version(
    tmp_path, generate_unsafe
):
    path = tmp_path / "reject"
    result = create_run(config(admission_policy="reject_all"), path)
    assert not result["paused"]
    assert result["accounting"]["total_synthetic_units"] == 20
    assert all(
        r["decision"]["verdict"] == "reject"
        and r["public_evaluation"]["risk"] == 0
        and r["public_evaluation"]["complete"]
        for r in result["rounds"]
    )
    store, state, _ = handles(path)
    head = state.load_snapshot(ArtifactRef.from_dict(state.get_branch("main")["head"]))
    deployed = Version.from_dict(decode_json(store.read_bytes(head.deployed_version)))
    assert deployed.agent == TransferProgram(True, True, True)


def test_blocked_unauthorized_requests_do_not_pause_without_actual_effect(
    tmp_path, generate_unsafe
):
    result = create_run(config(guard_mode="enforce"), tmp_path / "enforced")
    assert not result["paused"]
    assert result["accounting"]["total_synthetic_units"] == 20
    for row in result["rounds"]:
        evaluation = row["public_evaluation"]
        assert evaluation["risk"] == 0 and evaluation["complete"]
        assert evaluation["unauthorized_requests"] == 1
        assert evaluation["blocked_violations"] == 1
        assert evaluation["actual_violations"] == 0


def test_unknown_generation_keeps_reservation_and_cannot_authorize_commit(
    tmp_path, monkeypatch
):
    path = tmp_path / "unknown"

    def timeout(*args):
        raise TimeoutError("no queryable provider receipt")

    with monkeypatch.context() as patch:
        patch.setattr(DurableFakeModelClient, "generate", timeout)
        result = create_run(config(horizon_attempts=1), path)
    assert result["rounds"][0]["status"] == "outcome_unknown"
    assert result["rounds"][0]["public_evaluation"] is None
    assert result["accounting"]["total_synthetic_units"] == 0
    assert result["accounting"]["outstanding_reserved_units"] == 5
    assert result["branches"][0]["head_revision"] == 0
    # Unknown terminal attempts remain explicit until a reconciliation protocol
    # is invoked; ordinary resume must not erase the reservation or retry blindly.
    assert resume_run(path) == result


def test_logged_unsafe_effect_still_pauses_when_response_is_unknown(
    tmp_path, monkeypatch, generate_unsafe
):
    original = Workflow.execute

    def lost_response(
        world, program, task, effect_id, guard_mode="observe", *, purpose="execution"
    ):
        record = original(world, program, task, effect_id, guard_mode, purpose=purpose)
        if purpose == "execution" and task.task_id == "scope":
            raise TimeoutError("effect persisted before response loss")
        return record

    monkeypatch.setattr(Workflow, "execute", lost_response)
    result = create_run(config(), tmp_path / "effect_unknown")
    assert result["rounds"][0]["public_evaluation"]["risk"] == 1
    assert result["paused"]
    assert result["rounds"][1]["reason"] == "unsafe_current_pause"
    assert result["accounting"]["outstanding_reserved_units"] == 1


def test_deployment_controller_capability_checked_before_foreign_effects(tmp_path):
    path = tmp_path / "capability"
    create_run(config(horizon_attempts=1), path)
    store, state, journal = handles(path)
    state.create_branch("foreign", "loop_pair")
    journal.create_budget("branch", "foreign", 10)
    controller = LifecycleController(state, store, journal, ("main",))
    runtime = DeploymentRuntime(state, store, journal, controller)
    before = backup_sqlite(state.working_database("foreign"), store)
    with pytest.raises(PermissionDenied):
        runtime.run_suite("foreign", "foreign_001")
    assert backup_sqlite(state.working_database("foreign"), store) == before
    assert journal.summary("branch", "foreign")["total_synthetic_units"] == 0
    assert not [e for e in journal.exposures() if e["branch_id"] == "foreign"]


def test_deployment_cannot_use_independent_cursor_or_expose_its_tasks(tmp_path):
    path = tmp_path / "cursor_capability"
    create_run(config(horizon_attempts=1), path)
    store, state, journal = handles(path)
    controller = LifecycleController(state, store, journal, ("main",))
    runtime = DeploymentRuntime(state, store, journal, controller)
    before = backup_sqlite(state.working_database("main"), store)
    costs = journal.costs()
    exposures = journal.exposures()
    with pytest.raises(ValidationError):
        runtime.run_suite(
            "main",
            "forbidden_cursor",
            cursor_family=CursorFamily.INDEPENDENT_EVALUATION,
        )
    assert backup_sqlite(state.working_database("main"), store) == before
    assert journal.costs() == costs and journal.exposures() == exposures


def test_process_exit_after_provider_response_recovers_by_lookup(tmp_path, monkeypatch):
    # Clone an identical durable request before dispatch, so baseline and crash
    # runs have the same lineage and immutable object identities.
    class BeforeDispatch(BaseException):
        pass

    def stop_before_dispatch(*args):
        raise BeforeDispatch()

    seed = tmp_path / "seed"
    with monkeypatch.context() as patch:
        patch.setattr(DurableFakeModelClient, "generate", stop_before_dispatch)
        with pytest.raises(BeforeDispatch):
            create_run(config(), seed)
    baseline_path = tmp_path / "baseline"
    path = tmp_path / "interrupted"
    shutil.copytree(seed, baseline_path)
    shutil.copytree(seed, path)
    baseline = resume_run(baseline_path)
    script = """
import os, sys
from pathlib import Path
from rsi_lab.serialization import canonical_bytes
from rsi_lab.studies.closed_loop import resume_run
def kill(point):
    if point=='after_model_response':
        os._exit(77)
path=Path(sys.argv[1])
result=resume_run(path,fault_hook=kill if sys.argv[2]=='kill' else None)
print(canonical_bytes(result).decode())
"""
    stopped = subprocess.run(
        [sys.executable, "-c", script, str(path), "kill"],
        capture_output=True,
        text=True,
    )
    assert stopped.returncode == 77, stopped.stderr
    journal = Journal(path / "controller.sqlite")
    assert journal.summary()["outstanding_reserved_units"] == 5
    assert journal.summary()["total_synthetic_units"] == 0
    with sqlite3.connect(path / "provider.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM responses").fetchone()[0] == 1
    resumed = subprocess.run(
        [sys.executable, "-c", script, str(path), "resume"],
        capture_output=True,
        text=True,
    )
    assert resumed.returncode == 0, resumed.stderr
    result = decode_json(resumed.stdout.encode())
    assert result["accounting"]["total_synthetic_units"] == 20
    assert result["accounting"]["outstanding_reserved_units"] == 0
    with sqlite3.connect(path / "provider.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM responses").fetchone()[0] == 2
    assert (
        len([c for c in journal.costs() if c["operation_id"].endswith("/generate")])
        == 2
    )
    assert semantic_evidence(
        result, ArtifactStore(path / "objects")
    ) == semantic_evidence(baseline, ArtifactStore(baseline_path / "objects"))
    assert resume_run(path) == result
