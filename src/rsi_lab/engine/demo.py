"""Runnable state-persistence fixture, explicitly not a self-improving subject.

The fixture advances stored cursors and logs zero-cost infrastructure operations.
It never reports model, task, oracle, deployment or admission execution.
"""

import os
import sqlite3
import uuid
from pathlib import Path

from rsi_lab import __version__
from rsi_lab.config import LabConfig
from rsi_lab.contracts import (
    ArtifactRef, AttemptSpec, Checkpoint, ComponentRefs, CursorFamily, PairAnchor,
    PrepareCursor, SnapshotManifest, TaskCursor, Version, require_id,
)
from rsi_lab.engine.state import StateStore
from rsi_lab.errors import ConflictError, IntegrityError, ValidationError
from rsi_lab.frozen import fixture_bytes, implementation_fingerprint, provenance
from rsi_lab.infra.artifacts import ArtifactStore, _checked_path
from rsi_lab.infra.journal import Journal
from rsi_lab.infra.snapshots import backup_sqlite
from rsi_lab.serialization import canonical_bytes, decode_json, digest
from rsi_lab.wiring import preflight


def _publish_json(path: Path, value):
    """Publish a new file durably without replacing a previous run artifact."""
    data = canonical_bytes(value)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except FileExistsError as exc:
        raise ConflictError(f"Output already exists: {path}") from exc
    finally:
        temp.unlink(missing_ok=True)


def _protocol(config: LabConfig) -> dict:
    return {"schema_version": 1, "config": config.to_dict(), "frozen": provenance(),
            "implementation_version": __version__, "implementation_sha256": implementation_fingerprint(),
            "sqlite_version": sqlite3.sqlite_version, "execution_kind": "state_persistence_fixture"}


def create_run(config: LabConfig, output: Path, *, stop_after=4) -> dict:
    preflight(config)
    if type(stop_after) is not int or not 1 <= stop_after <= 4:
        raise ValidationError("stop_after must be one of 1, 2, 3, 4")
    output = _checked_path(Path(output))
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ConflictError(f"Output exists; use resume --run {output}") from exc
    store = ArtifactStore(output / "objects")
    protocol_ref = store.put_json(_protocol(config))
    run_id = "run_" + uuid.uuid4().hex
    _publish_json(output / "resolved_config.json", config)
    _publish_json(output / "run_manifest.json", {
        "schema_version": 1, "run_id": run_id, "stage": "M1",
        "implementation_version": __version__, "protocol": protocol_ref,
        "implementation_sha256": implementation_fingerprint(), "sqlite_version": sqlite3.sqlite_version,
        "resolved_config_sha256": digest(config), "frozen": provenance(),
        "runtime": "trusted_fixture", "arbitrary_code_execution": False,
        "hard_isolation_verified": False, "execution_kind": "state_persistence_fixture",
        "scientific_results": False,
    })
    return resume_run(output, stop_after=stop_after)


def _load_run(output: Path):
    output = _checked_path(Path(output))
    for name in ("resolved_config.json", "run_manifest.json"):
        _checked_path(output / name)
        if not (output / name).is_file():
            raise ValidationError(f"Not a resumable lab run; missing {name}: {output}")
    manifest = decode_json((output / "run_manifest.json").read_bytes())
    required = {"schema_version", "run_id", "stage", "implementation_version", "implementation_sha256", "sqlite_version", "protocol",
                "resolved_config_sha256", "frozen", "runtime", "arbitrary_code_execution",
                "hard_isolation_verified", "execution_kind", "scientific_results"}
    if set(manifest) != required or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ValidationError("Unknown or incomplete run manifest schema")
    require_id(manifest["run_id"], "run_id")
    config = LabConfig.from_dict(decode_json((output / "resolved_config.json").read_bytes()))
    preflight(config)
    if (manifest["implementation_version"] != __version__
            or manifest["implementation_sha256"] != implementation_fingerprint()
            or manifest["sqlite_version"] != sqlite3.sqlite_version
            or canonical_bytes(manifest["frozen"]) != canonical_bytes(provenance())):
        raise IntegrityError("Implementation or frozen protocol changed; this run cannot be resumed silently")
    if manifest["resolved_config_sha256"] != digest(config):
        raise IntegrityError("Resolved configuration digest mismatch")
    expected_flags = {"stage": "M1", "runtime": "trusted_fixture", "execution_kind": "state_persistence_fixture",
                      "arbitrary_code_execution": False, "hard_isolation_verified": False, "scientific_results": False}
    if any(type(manifest[k]) is not type(v) or manifest[k] != v for k, v in expected_flags.items()):
        raise IntegrityError("Run manifest capability flags do not match M1")
    objects = _checked_path(output / "objects")
    if not objects.is_dir():
        raise IntegrityError("Run object directory is missing; refusing to recreate evidence")
    store = ArtifactStore(objects)
    protocol_ref = ArtifactRef.from_dict(manifest["protocol"])
    if store.read_bytes(protocol_ref) != canonical_bytes(_protocol(config)):
        raise IntegrityError("Stored protocol differs from resolved configuration")
    return output, config, manifest, store, protocol_ref


def _initialize_state(output, config, manifest, store, protocol_ref):
    state = StateStore(output, store)
    journal = Journal(output / "controller.sqlite")
    # Initialization is a deterministic fixture artifact, not a simulated task.
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE fixture_state(key TEXT PRIMARY KEY, value INTEGER NOT NULL)")
        connection.execute("INSERT INTO fixture_state VALUES ('counter', 0)")
        connection.commit()
        environment_ref = backup_sqlite(connection, store)
    finally:
        connection.close()
    deployed = Version.from_dict(decode_json(fixture_bytes("version_D.json")))
    edit_base = Version.from_dict(decode_json(fixture_bytes("version_E.json")))
    components = ComponentRefs(
        agent=store.put_json(deployed.agent.to_dict()),
        q=store.put_json({"registered_id": deployed.q.value}),
        v=store.put_json({"registered_id": deployed.v.value}),
        s=store.put_json({"registered_id": deployed.s.value}),
    )
    memory_ref = store.put_json({"schema_version": 1,
                               "entries": decode_json(fixture_bytes("context_entries.json")),
                               "active_index": ["G", "W", "P", "Z"], "quarantined": []})
    cursors = tuple(TaskCursor(family) for family in CursorFamily)
    random_ref = store.put_json(config.random.to_dict())
    snapshot = SnapshotManifest(
        components=components, deployed_version=store.put_json(deployed.to_dict()),
        edit_base_version=store.put_json(edit_base.to_dict()), memory=memory_ref,
        archive=store.put_json({"schema_version": 1, "entries": []}), environment=environment_ref,
        random_plan=random_ref, cursors=cursors, implementation_version=__version__,
    )
    snapshot_ref = state.save_snapshot(snapshot)
    anchor = PairAnchor(
        pair_id="m1_pair", root_id="m1_root", history_group_id="m1_history",
        snapshot=snapshot_ref, task_plan=store.put_json({"schema_version": 1, "tasks": [], "purpose": "no_task_execution"}),
        random_plan=random_ref, cursors=cursors, pairing_mode=config.pairing.mode,
        fork_at=config.pairing.fork_at, round_index=0,
    )
    state.register_pair(anchor)
    journal.create_budget("pair", anchor.pair_id, 0)
    for arm in ("L", "R"):
        state.create_branch(arm, anchor.pair_id)
        journal.create_budget("branch", arm, config.resources.synthetic_units_per_branch)
        state.create_attempt(AttemptSpec(
            attempt_id=f"{arm}_001", run_id=manifest["run_id"], pair_id=anchor.pair_id,
            branch_id=arm, root_id=anchor.root_id, history_group_id=anchor.history_group_id,
            arm=arm, replicate=0, attempt_index=1, protocol=protocol_ref,
            source_snapshot=snapshot_ref, candidate_origin=config.candidate_origin,
        ))
    return state, journal


def resume_run(output: Path, *, stop_after=4) -> dict:
    if type(stop_after) is not int or not 1 <= stop_after <= 4:
        raise ValidationError("stop_after must be one of 1, 2, 3, 4")
    output, config, manifest, store, protocol_ref = _load_run(output)
    state, journal = _initialize_state(output, config, manifest, store, protocol_ref)
    for arm in ("L", "R"):
        attempt_id = f"{arm}_001"
        attempt = state.get_attempt(attempt_id)
        if attempt["phase"] == "created":
            state.begin_preparation(attempt_id)
        for index, checkpoint in enumerate(list(Checkpoint)[:stop_after]):
            operation_id = f"{attempt_id}/state_checkpoint/{index}"
            payload = {"schema_version": 1, "checkpoint": checkpoint.value,
                       "fixture_only": True, "subject_executed": False}
            local_ref = store.put_json(payload)
            patch_ref = store.put_json({"schema_version": 1, "kind": "identity_state_fixture"})
            receipt_ref = store.put_json({**payload, "operation_id": operation_id})
            journal.reserve(operation_id, "branch", arm, 0, "discovery", payload, attempt_id=attempt_id)
            cursor = PrepareCursor(checkpoint, local_ref, f"{attempt_id}/state_checkpoint/{index + 1}",
                                   next_operation_index=index + 1)
            state.record_checkpoint(attempt_id, cursor, operation_id=operation_id,
                                    input_ref=protocol_ref, patch_ref=patch_ref, receipt_ref=receipt_ref)
            journal.settle(operation_id, 0, {"receipt": receipt_ref.to_dict()})
    return {"run_id": manifest["run_id"], "stage": "M1", "execution_kind": "state_persistence_fixture",
            "subject_executed": False, "risk": None, "risk_missing_reason": "evaluation_not_implemented",
            "state": state.inspect(), "accounting": journal.summary()}


def inspect_run(output: Path) -> dict:
    """Read-only inspection; do not create or repair controller tables."""
    output, config, manifest, store, protocol_ref = _load_run(output)
    database = _checked_path(output / "controller.sqlite")
    for suffix in ("-wal", "-shm", "-journal"):
        _checked_path(Path(str(database) + suffix))
    if not database.is_file():
        raise ValidationError("Run initialization is incomplete; use resume")
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        branches = [dict(row) for row in connection.execute(
            "SELECT branch_id,pair_id,role,head_revision FROM branches ORDER BY branch_id")]
        attempts = []
        for row in connection.execute("SELECT attempt_id,phase,status,cursor_json FROM attempts ORDER BY attempt_id"):
            item = dict(row)
            raw = item.pop("cursor_json")
            item["cursor"] = decode_json(raw) if raw is not None else None
            attempts.append(item)
        costs = connection.execute("SELECT COUNT(*),COALESCE(SUM(synthetic_cost_units),0) FROM cost_events").fetchone()
        reservations = connection.execute("SELECT COALESCE(SUM(reserved_units),0) FROM operations WHERE status IN ('reserved','outcome_unknown')").fetchone()[0]
    finally:
        connection.close()
    return {"manifest": manifest, "branches": branches, "attempts": attempts,
            "accounting": {"settled_charge_rows": costs[0], "total_synthetic_units": costs[1],
                           "outstanding_reserved_units": reservations},
            "risk": None, "risk_missing_reason": "evaluation_not_implemented"}

