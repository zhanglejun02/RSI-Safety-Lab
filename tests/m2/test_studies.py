"""End-to-end M2 tests use real CLI processes and stored public evidence."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from rsi_lab.contracts import ArtifactRef, Checkpoint
from rsi_lab.infra.artifacts import ArtifactStore
from rsi_lab.serialization import decode_json

ROOT = Path(__file__).resolve().parents[2]


def cli(*arguments):
    environment = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    return subprocess.run([sys.executable, "-m", "rsi_lab", *map(str, arguments)],
                          cwd=ROOT, env=environment, capture_output=True, text=True, timeout=60)


def success(result):
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def artifact(store, ref):
    return decode_json(store.read_bytes(ArtifactRef.from_dict(ref)))


@pytest.mark.parametrize("study,cost,arms,prefix_cost", [
    ("m2_q_pairs", 30, 5, 25),
    ("m2_shared_candidate", 7, 2, 5),
    ("m2_v_fixed_pool", 26, 4, 0),
])
def test_real_cli_studies_stop_resume_and_preserve_deployment(tmp_path, study, cost, arms, prefix_cost):
    run = tmp_path / study
    stopped = success(cli("run", "--config", ROOT / f"configs/{study}.toml", "--output", run, "--stop-after", 3))
    assert stopped["stage"] == "M2"
    assert stopped["accounting"]["total_synthetic_units"] == prefix_cost
    assert len(stopped["attempts"]) == arms
    assert all(a["phase"] == "preparing" for a in stopped["attempts"])
    assert all(a["cursor"]["checkpoint"] == "pre_verification" for a in stopped["attempts"])
    after = success(cli("resume", "--run", run))
    assert after["accounting"]["total_synthetic_units"] == cost
    assert after["accounting"]["outstanding_reserved_units"] == 0
    assert all(a["phase"] == "prepared" and a["status"] == "active" for a in after["attempts"])
    assert not after["admission_performed"]
    assert not after["deployment_performed"]
    assert all(branch["head_revision"] == 0 for branch in after["branches"])
    assert all(attempt["deployed_risk"] is None for attempt in after["attempts"])
    assert success(cli("inspect", "--run", run)) == after
    assert success(cli("resume", "--run", run)) == after
    store = ArtifactStore(run / "objects")
    with sqlite3.connect(run / "controller.sqlite") as connection:
        attempts = connection.execute("SELECT attempt_id,spec_json FROM attempts ORDER BY attempt_id").fetchall()
        assert len(attempts) == arms
        for identity, raw in attempts:
            spec = decode_json(raw)
            start = artifact(store, spec["source_snapshot"])
            attempt = next(a for a in after["attempts"] if a["attempt_id"] == identity)
            prepared = artifact(store, attempt["preparation"]["prepared_snapshot"])
            assert attempt["deployed_version"] == prepared["deployed_version"] == start["deployed_version"]
            assert attempt["next_edit_base"] == prepared["edit_base_version"] == start["edit_base_version"]
            assert prepared["environment"] == start["environment"]
            assert all(cursor["position"] == 0 for cursor in prepared["cursors"] if cursor["family"] != "public_development")
            with sqlite3.connect(run / "branches" / spec["branch_id"] / "environment.sqlite") as branch:
                assert branch.execute("SELECT COUNT(*) FROM workflow_operations").fetchone()[0] == 0
    if study == "m2_shared_candidate":
        assert after["accounting"]["pair_shared_units"] == 5
        assert after["shared_cost_ref"] == "S_001/generate"
        generated = [row for row in after["accounting"]["cost_events"] if row["operation_id"] == "S_001/generate"]
        assert len(generated) == 1 and generated[0]["owner_kind"] == "pair"
        assert generated[0]["owner_id"] == "m2_pair"
        assert after["attempts"][0]["preparation"]["candidates"] == after["attempts"][1]["preparation"]["candidates"]
        with sqlite3.connect(run / "controller.sqlite") as connection:
            source = decode_json(connection.execute("SELECT record_json FROM shared_preparations").fetchone()[0])
            assert source["stop_reason"] == "pair_anchor_completed"
            assert source["counts_toward_arm_horizon"] is False
            assert [r[0] for r in connection.execute("SELECT operation_id FROM operations WHERE reserved_units=5")] == ["S_001/generate"]
    elif study == "m2_v_fixed_pool":
        pools = [attempt["preparation"]["candidates"] for attempt in after["attempts"]]
        assert all(candidate_pool == pools[0] for candidate_pool in pools)
        selected = {a["attempt_id"]: a["preparation"]["native_selected"]["occurrence_id"] for a in after["attempts"]}
        assert selected == {"Old_001": "v_candidate_01_D", "New_001": "v_candidate_00_P",
                            "Restore_001": "v_candidate_01_D", "Placebo_001": "v_candidate_00_P"}
        with sqlite3.connect(run / "controller.sqlite") as connection:
            assert connection.execute("SELECT COUNT(*) FROM operations WHERE reserved_units=5").fetchone()[0] == 0
            assert {r[0] for r in connection.execute("SELECT checkpoint FROM checkpoints")} == {"pre_verification", "pre_deploy"}
        assert after["accounting"]["pair_shared_units"] == 0
    else:
        expected_scope = {"Old_001": True, "New_001": False, "Restore_001": True,
                          "Placebo_001": False, "Remove_001": False}
        with sqlite3.connect(run / "controller.sqlite") as connection:
            connection.row_factory = sqlite3.Row
            for attempt in after["attempts"]:
                identity = attempt["attempt_id"]
                preparation = attempt["preparation"]
                candidate = artifact(store, preparation["candidates"][0]["artifact"])
                assert candidate["agent"]["check_scope"] == expected_scope[identity]
                context = artifact(store, preparation["context_receipt"])
                actual = store.read_bytes(ArtifactRef.from_dict(context["actual_context"]))
                expected_good = b"Check scope before transfer....\n" in actual
                assert expected_good == expected_scope[identity]
                operation = connection.execute("SELECT request_json,receipt_json FROM operations WHERE operation_id=?", (f"{identity}/generate",)).fetchone()
                request, response = decode_json(operation["request_json"]), decode_json(operation["receipt_json"])["response"]
                assert request["context"] == context["actual_context"]
                assert response["context_sha256"] == context["actual_context"]["sha256"]
                assert candidate["q"] == request["edit_base"]["q"]
                assert candidate["v"] == request["edit_base"]["v"]
                assert candidate["s"] == request["edit_base"]["s"]


@pytest.mark.parametrize("replacement", [
    ('guard_mode = "observe"', 'guard_mode = "unknown"'),
    ('subject = "fake"', 'subject = "unknown"'),
    ('candidate_origin = "control"', 'candidate_origin = "natural"'),
    ('partition = "dev"', 'partition = "sealed"'),
    ('schema_version = 1', 'schema_version = true'),
    ('fork_at = "pre_verification"', 'fork_at = "pre_deploy"'),
])
def test_bad_m2_config_fails_before_output_creation(tmp_path, replacement):
    original = (ROOT / "configs/m2_v_fixed_pool.toml").read_text()
    assert replacement[0] in original
    invalid = tmp_path / "invalid.toml"
    invalid.write_text(original.replace(*replacement))
    output = tmp_path / "must-not-exist"
    result = cli("run", "--config", invalid, "--output", output)
    assert result.returncode != 0
    assert not output.exists()


def test_restore_truncation_blocks_model_dispatch_in_study(tmp_path):
    config = tmp_path / "small.toml"
    original = (ROOT / "configs/m2_q_pairs.toml").read_text()
    assert 'context_budget_units = 16' in original
    config.write_text(original.replace('context_budget_units = 16', 'context_budget_units = 15'))
    run = tmp_path / "small-run"
    result = success(cli("run", "--config", config, "--output", run))
    assert result["accounting"]["total_synthetic_units"] == 18
    for identity in ("Restore_001", "Placebo_001"):
        attempt = next(item for item in result["attempts"] if item["attempt_id"] == identity)
        assert attempt["status"] == "invalid"
        assert attempt["preparation"]["candidates"] == []
        with sqlite3.connect(run / "controller.sqlite") as connection:
            assert connection.execute("SELECT COUNT(*) FROM operations WHERE operation_id=?", (f"{identity}/generate",)).fetchone()[0] == 0


def test_each_fixed_arm_references_shared_generation(tmp_path):
    from rsi_lab.studies.preparation import create_run
    from rsi_lab.config import load_config
    report = create_run(load_config(Path('configs/m2_shared_candidate.toml')), tmp_path / 'shared')
    assert all(a['preparation'].shared_cost_ref == 'S_001/generate' for a in report['attempts'])
