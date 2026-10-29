from contextlib import closing
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from rsi_lab.contracts import (
    ArtifactOccurrence, ArtifactRef, AttemptSpec, CandidateBinding, CandidateOrigin,
    Checkpoint, ComponentRefs, CursorFamily, ForkPoint, PairAnchor, PairingMode,
    PrepareCursor, RandomPlan, RunStatus, SnapshotManifest, TaskCursor,
)
from rsi_lab.engine.state import StateStore
from rsi_lab.errors import ConflictError, IntegrityError, PermissionDenied, ValidationError
from rsi_lab.infra.artifacts import ArtifactStore
from rsi_lab.infra.snapshots import backup_sqlite, restore_sqlite
from rsi_lab.infra.views import ArtifactView
from rsi_lab.serialization import decode_json


@pytest.fixture
def lab(tmp_path):
    run = tmp_path / "run"
    artifacts = ArtifactStore(run / "objects")
    with closing(sqlite3.connect(tmp_path / "world.sqlite")) as connection:
        connection.execute("CREATE TABLE effects(id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO effects VALUES(1, 'founder')")
        connection.commit()
        environment = backup_sqlite(connection, artifacts)
    public = artifacts.put_json({"fixture": "state-only", "no_agent_executed": True})
    manifest = SnapshotManifest(
        components=ComponentRefs(public, public, public, public),
        deployed_version=public, edit_base_version=public, memory=artifacts.put_json({"active": ["G", "W"]}),
        archive=artifacts.put_json({"entries": []}), environment=environment,
        random_plan=artifacts.put_json(RandomPlan(104729)),
        cursors=tuple(TaskCursor(family) for family in CursorFamily),
        implementation_version="state-fixture-v1",
    )
    state = StateStore(run, artifacts)
    snapshot = state.save_snapshot(manifest)
    anchor = PairAnchor("pair_1", "root_1", "history_1", snapshot,
                        artifacts.put_json({"tasks": ["first", "second"]}),
                        manifest.random_plan, manifest.cursors, PairingMode.PROCESS,
                        ForkPoint.ATTEMPT_START, 4)
    state.register_pair(anchor)
    for branch in ("L", "R"):
        state.create_branch(branch, anchor.pair_id)
    protocol = artifacts.put_json({"schema_version": 1, "study": "state_only"})
    spec = AttemptSpec("L_1", "run_1", anchor.pair_id, "L", anchor.root_id,
                       anchor.history_group_id, "old", 0, 1, protocol, snapshot,
                       CandidateOrigin.CONTROL)
    return SimpleNamespace(run=run, artifacts=artifacts, state=state, manifest=manifest,
                           snapshot=snapshot, anchor=anchor, spec=spec, public=public)


def checkpoint(lab, index, *, operation_id=None):
    cp = list(Checkpoint)[index]
    local = lab.artifacts.put_json({"checkpoint": cp.value, "step": index})
    cursor = PrepareCursor(cp, local, f"L_1/next/{index + 1}", next_operation_index=index + 1)
    refs = dict(input_ref=lab.snapshot, patch_ref=lab.artifacts.put_json({"patch": "identity"}),
                receipt_ref=lab.artifacts.put_json({"checkpoint": cp.value, "recorded": True}))
    operation_id = operation_id or f"L_1/checkpoint/{index}"
    return cursor, refs, operation_id


def record(lab, index):
    cursor, refs, operation = checkpoint(lab, index)
    lab.state.record_checkpoint(lab.spec.attempt_id, cursor, operation_id=operation, **refs)
    return cursor, refs, operation


def test_snapshot_graph_separates_content_occurrence_and_binding(lab):
    before = lab.artifacts.read_bytes(lab.snapshot)
    first = ArtifactOccurrence("generated_1", lab.public, "L_1", CandidateOrigin.CONTROL, "generation")
    second = replace(first, occurrence_id="generated_2", source_attempt_id="R_1")
    first_ref = lab.artifacts.put_json(first)
    second_ref = lab.artifacts.put_json(second)
    assert first.artifact == second.artifact
    assert first_ref.sha256 != second_ref.sha256
    binding = CandidateBinding("L_1", first, lab.snapshot, lab.spec.protocol)
    binding_ref = lab.artifacts.put_json(binding)
    assert decode_json(lab.artifacts.read_bytes(binding_ref))["base_snapshot"] == lab.snapshot.to_dict()
    with pytest.raises(ValidationError, match="unknown"):
        SnapshotManifest.from_dict({**lab.manifest.to_dict(), "candidate_binding": binding.to_dict()})
    assert lab.snapshot.sha256.encode() not in before
    assert binding_ref.sha256.encode() not in before
    assert lab.artifacts.read_bytes(lab.snapshot) == before
    assert lab.state.load_snapshot(lab.snapshot) == lab.manifest


def test_snapshot_requires_all_cursors_and_existing_child_objects(lab):
    with pytest.raises(ValidationError, match="all three"):
        replace(lab.manifest, cursors=lab.manifest.cursors[:2])
    with pytest.raises(ValidationError, match="Duplicate"):
        replace(lab.manifest, cursors=lab.manifest.cursors + (lab.manifest.cursors[0],))
    missing = ArtifactRef("0" * 64, 0)
    with pytest.raises(IntegrityError):
        lab.state.save_snapshot(replace(lab.manifest, memory=missing))
    bad_random = lab.artifacts.put_json({"schema_version": 1, "master_seed": True})
    with pytest.raises(ValidationError):
        lab.state.save_snapshot(replace(lab.manifest, random_plan=bad_random))


def test_branch_worlds_are_isolated_from_sibling_and_snapshot(lab):
    left = lab.state.working_database("L")
    right = lab.state.working_database("R")
    with closing(sqlite3.connect(left)) as connection:
        connection.execute("INSERT INTO effects VALUES(2, 'left-only')")
        connection.commit()
    with closing(sqlite3.connect(right)) as connection:
        assert connection.execute("SELECT value FROM effects").fetchall() == [("founder",)]
    assert lab.state.get_branch("L")["head"] == lab.state.get_branch("R")["head"]
    assert lab.state.get_branch("L")["head_revision"] == 0
    assert lab.artifacts.read_bytes(lab.manifest.environment) == right.read_bytes()
    # Registration replay does not reset the mutable branch to its initial head.
    lab.state.create_branch("L", lab.anchor.pair_id)
    with closing(sqlite3.connect(left)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM effects").fetchone() == (2,)


def test_pair_registration_binds_cursor_and_random_plan(lab):
    old_count = lab.state.inspect()["state_event_count"]
    lab.state.register_pair(lab.anchor)
    assert lab.state.inspect()["state_event_count"] == old_count
    mismatched = replace(lab.anchor, cursors=tuple(replace(c, position=1) for c in lab.anchor.cursors))
    with pytest.raises(ValidationError, match="differs"):
        lab.state.register_pair(mismatched)
    with pytest.raises(ConflictError, match="identity reused"):
        lab.state.register_pair(replace(lab.anchor, root_id="other_root"))


def test_attempt_requires_correct_lineage_and_current_head(lab):
    for altered in (replace(lab.spec, root_id="wrong"), replace(lab.spec, history_group_id="wrong")):
        with pytest.raises(ValidationError, match="lineage"):
            lab.state.create_attempt(altered)
    wrong_snapshot = lab.state.save_snapshot(replace(lab.manifest, implementation_version="different"))
    with pytest.raises(ConflictError, match="stale source"):
        lab.state.create_attempt(replace(lab.spec, source_snapshot=wrong_snapshot))
    lab.state.create_attempt(lab.spec)
    before = lab.state.inspect()["state_event_count"]
    lab.state.create_attempt(lab.spec)
    assert lab.state.inspect()["state_event_count"] == before
    with pytest.raises(ConflictError, match="Duplicate attempt position"):
        lab.state.create_attempt(replace(lab.spec, attempt_id="alias"))
    with pytest.raises(ConflictError, match="identity reused"):
        lab.state.create_attempt(replace(lab.spec, arm="different"))


def test_checkpoint_progress_is_ordered_and_replay_is_idempotent(lab):
    lab.state.create_attempt(lab.spec)
    cursor, refs, operation = checkpoint(lab, 0)
    with pytest.raises(ConflictError, match="cannot advance"):
        lab.state.record_checkpoint(lab.spec.attempt_id, cursor, operation_id=operation, **refs)
    lab.state.begin_preparation(lab.spec.attempt_id)
    lab.state.begin_preparation(lab.spec.attempt_id)
    with pytest.raises(ConflictError, match="out-of-order"):
        record(lab, 1)
    first = record(lab, 0)
    first_count = lab.state.inspect()["state_event_count"]
    record(lab, 0)
    assert lab.state.inspect()["state_event_count"] == first_count
    cursor, refs, operation = checkpoint(lab, 1)
    with pytest.raises(ConflictError, match="did not advance"):
        lab.state.record_checkpoint(lab.spec.attempt_id, replace(cursor, next_operation_index=1),
                                    operation_id=operation, **refs)
    for index in range(1, 4):
        record(lab, index)
    result = lab.state.get_attempt(lab.spec.attempt_id)
    assert result["phase"] == "prepared"
    assert result["cursor"]["checkpoint"] == "pre_deploy"
    count = lab.state.inspect()["state_event_count"]
    old_cursor, old_refs, old_operation = first
    lab.state.record_checkpoint(lab.spec.attempt_id, old_cursor, operation_id=old_operation, **old_refs)
    assert lab.state.inspect()["state_event_count"] == count
    assert lab.state.get_attempt(lab.spec.attempt_id) == result
    with pytest.raises(ConflictError, match="reused"):
        lab.state.record_checkpoint(lab.spec.attempt_id, old_cursor, operation_id=old_operation,
                                    **{**old_refs, "receipt_ref": lab.public})


def test_terminal_status_blocks_advancement_without_erasing_cursor(lab):
    lab.state.create_attempt(lab.spec)
    lab.state.begin_preparation(lab.spec.attempt_id)
    record(lab, 0)
    lab.state.mark_status(lab.spec.attempt_id, RunStatus.OUTCOME_UNKNOWN, "provider receipt missing")
    before = lab.state.get_attempt(lab.spec.attempt_id)
    lab.state.mark_status(lab.spec.attempt_id, RunStatus.OUTCOME_UNKNOWN, "provider receipt missing")
    with pytest.raises(ConflictError):
        record(lab, 1)
    with pytest.raises(ConflictError):
        lab.state.mark_status(lab.spec.attempt_id, RunStatus.INVALID, "different outcome")
    assert lab.state.get_attempt(lab.spec.attempt_id) == before
    with pytest.raises(ValidationError):
        lab.state.mark_status(lab.spec.attempt_id, RunStatus.CRASHED, True)


def test_cursor_can_be_reopened_by_fresh_python_process(lab):
    lab.state.create_attempt(lab.spec)
    lab.state.begin_preparation(lab.spec.attempt_id)
    record(lab, 0)
    record(lab, 1)
    script = """
import json, sys
from pathlib import Path
from rsi_lab.engine.state import StateStore
from rsi_lab.infra.artifacts import ArtifactStore
p = Path(sys.argv[1])
print(json.dumps(StateStore(p, ArtifactStore(p / 'objects')).get_attempt('L_1'), sort_keys=True))
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    result = subprocess.check_output([sys.executable, "-c", script, str(lab.run)], env=env, text=True)
    assert json.loads(result) == lab.state.get_attempt(lab.spec.attempt_id)


def test_registered_fixed_candidate_starts_at_declared_checkpoint(lab):
    fixed_anchor = replace(lab.anchor, pair_id="fixed_pair", pairing_mode=PairingMode.FIXED_CANDIDATE,
                           fork_at=ForkPoint.PRE_VERIFICATION)
    lab.state.register_pair(fixed_anchor)
    lab.state.create_branch("fixed", fixed_anchor.pair_id)
    fixed_spec = replace(lab.spec, attempt_id="fixed_1", pair_id="fixed_pair", branch_id="fixed")
    lab.state.create_attempt(fixed_spec)
    lab.state.begin_preparation(fixed_spec.attempt_id)
    early, refs, _ = checkpoint(lab, 0)
    with pytest.raises(ConflictError, match="out-of-order"):
        lab.state.record_checkpoint(fixed_spec.attempt_id, early, operation_id="fixed/early", **refs)
    cursor, refs, _ = checkpoint(lab, 2)
    lab.state.record_checkpoint(fixed_spec.attempt_id, cursor, operation_id="fixed/verification", **refs)
    assert lab.state.get_attempt(fixed_spec.attempt_id)["cursor"]["checkpoint"] == "pre_verification"


def test_exact_orphan_restore_is_recoverable_but_sidecars_are_not(lab):
    orphan = lab.state.working_database("orphan")
    restore_sqlite(lab.manifest.environment, lab.artifacts, orphan)
    before = orphan.read_bytes()
    Path(str(orphan) + "-wal").write_bytes(b"unknown prior effects")
    with pytest.raises(ConflictError, match="sidecars"):
        lab.state.create_branch("orphan", lab.anchor.pair_id)
    assert orphan.read_bytes() == before
    Path(str(orphan) + "-wal").unlink()
    lab.state.create_branch("orphan", lab.anchor.pair_id)
    assert lab.state.get_branch("orphan")["head_revision"] == 0


def test_branch_paths_reject_traversal_and_symlinked_directories(lab):
    for branch_id in ("../L", "/absolute", "a/b", "a\\b"):
        with pytest.raises(ValidationError):
            lab.state.working_database(branch_id)
    (lab.run / "branches" / "linked").symlink_to(lab.run / "branches" / "L", target_is_directory=True)
    with pytest.raises(IntegrityError):
        lab.state.create_branch("linked", lab.anchor.pair_id)
    Path(str(lab.state.working_database("L")) + "-wal").symlink_to(lab.state.working_database("R"))
    with pytest.raises(IntegrityError):
        lab.state.working_database("L")


def test_view_does_not_grant_access_merely_from_digest_knowledge(lab):
    sealed = lab.artifacts.put_json({"sealed_label": 1})
    view = ArtifactView(lab.artifacts, frozenset({lab.public}))
    assert view.read_bytes(lab.public) == lab.artifacts.read_bytes(lab.public)
    with pytest.raises(PermissionDenied):
        view.read_bytes(sealed)
    assert not hasattr(view, "reader")
    with pytest.raises(ValidationError):
        ArtifactView(lab.artifacts, {lab.public})
