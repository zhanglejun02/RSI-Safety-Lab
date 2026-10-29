"""Independent frozen Appendix B assertions; expected bytes are never regenerated."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from rsi_lab.contracts import ArtifactRef, SnapshotManifest
from rsi_lab.errors import ConflictError, IntegrityError
from rsi_lab.infra.artifacts import ArtifactStore
from rsi_lab.interventions.context import MemoryState
from rsi_lab.serialization import decode_json
from rsi_lab.studies.transition import inspect_transition, run_transition

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = json.loads((ROOT / "tests/golden/transition_v1.expected.json").read_text())


def _assert_disk_facts(output):
    store = ArtifactStore(output / "objects")
    with sqlite3.connect(output / "controller.sqlite") as connection:
        for arm in ("L", "R"):
            raw, revision = connection.execute(
                "SELECT head_json,head_revision FROM branches WHERE branch_id=?", (arm,)
            ).fetchone()
            head = ArtifactRef.from_dict(decode_json(raw))
            state = SnapshotManifest.from_dict(decode_json(store.read_bytes(head)))
            memory = MemoryState.from_dict(decode_json(store.read_bytes(state.memory)))
            assert memory.active_ids == ("G", "W", "P", "Z")
            assert memory.quarantined_ids == (f"delta_{arm}_001",)
            assert revision == 1
            positions = {
                cursor.family.value: cursor.position for cursor in state.cursors
            }
            assert positions == {
                "public_development": 1,
                "admission_probe": 0,
                "independent_evaluation": 0,
            }
            # Stopping at selection creates no intention for next model generation.
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM operations WHERE operation_id=?",
                    (f"{arm}_002/generate",),
                ).fetchone()[0]
                == 0
            )
        generation = connection.execute(
            "SELECT owner_kind,owner_id,attempt_id,actual_units FROM operations WHERE operation_id=?",
            ("golden_pair_01/generate/1",),
        ).fetchone()
        assert generation == ("pair", "golden_pair_01", "S_001", 5)
        assert connection.execute(
            "SELECT operation_id FROM cost_events WHERE operation_id LIKE '%/verify/%' ORDER BY operation_id"
        ).fetchall() == [("L_001/verify/ok",), ("R_001/verify/ok",)]
        assert connection.execute("SELECT COUNT(*) FROM cost_events").fetchone()[0] == 3
        assert (
            connection.execute("SELECT COUNT(*) FROM exposure_events").fetchone()[0]
            == 2
        )
        prefix = decode_json(
            connection.execute(
                "SELECT record_json FROM shared_preparations WHERE source_id=?",
                ("S_001",),
            ).fetchone()[0]
        )
        assert prefix["stop_reason"] == "pair_anchor_completed"
        assert prefix["purpose"] == "shared_preparation"
        assert prefix["counts_toward_arm_horizon"] is False
        assert [row["cursor"]["checkpoint"] for row in prefix["checkpoints"]] == [
            "post_experience_selection",
            "post_generation",
            "pre_verification",
        ]
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM attempts WHERE attempt_id='S_001'"
            ).fetchone()[0]
            == 0
        )
    for database in (output / "validation").glob("*.sqlite"):
        with sqlite3.connect(database) as connection:
            # Each independent ok test makes one actual delivery, including after recovery.
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM workflow_deliveries"
                ).fetchone()[0]
                == 1
            )


def test_exact_frozen_transition_and_idempotent_replay(tmp_path):
    output = tmp_path / "transition"
    assert run_transition(output) == EXPECTED
    _assert_disk_facts(output)
    assert inspect_transition(output) == EXPECTED
    assert run_transition(output) == EXPECTED
    _assert_disk_facts(output)


@pytest.mark.parametrize("point", ["after_effect", "after_staging", "after_commit"])
def test_kill_and_new_process_recovery_matches_unchanged_golden(tmp_path, point):
    output = tmp_path / point
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    command = [sys.executable, str(ROOT / "tests/m3/crash_worker.py"), str(output)]
    crashed = subprocess.run(
        [*command, point], env=env, cwd=ROOT, capture_output=True, text=True, timeout=60
    )
    assert crashed.returncode == 86, crashed.stderr
    recovered = subprocess.run(
        command, env=env, cwd=ROOT, capture_output=True, text=True, timeout=60
    )
    assert recovered.returncode == 0, recovered.stderr
    assert json.loads(recovered.stdout) == EXPECTED
    _assert_disk_facts(output)
    again = subprocess.run(
        command, env=env, cwd=ROOT, capture_output=True, text=True, timeout=60
    )
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout) == EXPECTED


def test_transition_rejects_configuration_change(tmp_path):
    output = tmp_path / "transition"
    run_transition(output)
    path = output / "resolved_config.json"
    value = json.loads(path.read_text())
    value["recovery"] = "retain"
    path.write_text(json.dumps(value))
    with pytest.raises(IntegrityError, match="protocol"):
        run_transition(output)


def test_transition_rejects_unrelated_nonempty_output(tmp_path):
    output = tmp_path / "unrelated"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("preserve")
    with pytest.raises(ConflictError):
        run_transition(output)
    assert sentinel.read_text() == "preserve"
