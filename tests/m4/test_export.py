"""End-to-end exports preserve measured facts, identities and missing outcomes."""

import hashlib
import json
from pathlib import Path
import sqlite3
from dataclasses import replace

import pytest

from rsi_lab.analysis.export import export_run
from rsi_lab.config import load_config
from rsi_lab.errors import ConflictError, IntegrityError
from rsi_lab.serialization import canonical_bytes, decode_json
from rsi_lab.studies.s0 import create_run
from rsi_lab.studies.transition import run_transition

ROOT = Path(__file__).resolve().parents[2]


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def hashes(root):
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file() and not p.name.endswith(("-wal", "-shm"))
    }


def test_paired_export_keeps_different_candidates_missing_horizon_and_costs(tmp_path):
    run = tmp_path / "run"
    create_run(load_config(ROOT / "configs/s0_q_pairs.toml"), run)
    before = hashes(run)
    output = tmp_path / "analysis"
    manifest = export_run(run, output, view="analysis")
    assert hashes(run) == before
    for name, info in manifest["files"].items():
        data = (output / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == info["sha256"]
        assert len(data) == info["size_bytes"]
    attempts = lines(output / "attempts.jsonl")
    assert len(attempts) == 9
    assert all(a["context_receipt"] and a["verification_receipt"] for a in attempts)
    first = {a["arm"]: a for a in attempts if a["attempt_index"] == 1}
    assert (
        first["Old"]["proposed_candidates"][0]["artifact"]
        != first["New"]["proposed_candidates"][0]["artifact"]
    )
    assert set(first) == {"Old", "New", "Restore", "Placebo", "Remove"}
    summary = json.loads((output / "paired_summary.json").read_text())
    assert summary["registered_attempts"] == 15 and summary["independent_roots"] == 1
    assert summary["resources"]["total_synthetic_units"] == 84
    assert summary["resources"]["shared_units"] == 0
    assert (
        summary["complete_horizon_arms"] == 2
        and summary["incomplete_horizon_arms"] == 3
    )
    arms = {a["arm"]: a for a in summary["arms"]}
    assert arms["Old"]["metrics"]["risk"]["mean"] == 0
    bad = arms["New"]["metrics"]
    assert (
        bad["risk"]["mean"] is None
        and bad["risk"]["m"] == 1
        and bad["risk"]["missing"] == 2
    )
    assert bad["risk"]["logical_bounds"] == [1 / 3, 1]
    assert (
        bad["utility"]["legal_denominator"] == 3 and bad["utility"]["utility"] is None
    )
    assert summary["admission_false_rejection_rate"] is None
    evaluations = lines(output / "evaluations.jsonl")
    assert len(evaluations) == 15
    assert all(e["continuation_horizon"] == 3 for e in evaluations)
    assert sum(e["evaluation"] is None for e in evaluations) == 6
    assert all(
        c["risk_mean_difference"] is None or c["arm"] == "Restore"
        for c in summary["pairs"][0]["comparisons"]
    )
    repeat = export_run(run, tmp_path / "same", view="analysis")
    assert repeat == manifest
    with pytest.raises(ConflictError):
        export_run(run, output, view="analysis")


def test_training_is_features_with_explicit_unmeasured_counterfactual(tmp_path):
    run = tmp_path / "run"
    cfg = replace(
        load_config(ROOT / "configs/s0_smoke.toml"),
        admission_policy="reject_all",
        context_budget_units=0,
    )
    create_run(cfg, run)
    output = tmp_path / "training"
    manifest = export_run(run, output, view="training")
    rows = lines(output / "examples.jsonl")
    assert len(rows) == 3 and manifest["supervision_rows"] == 0
    assert all(r["candidate_label"] is None and not r["supervised"] for r in rows)
    assert all(r["label_missing_reason"] == "counterfactual_not_measured" for r in rows)
    assert all(
        r["continuation"]["horizon"] == 3 and r["candidate_binding"] for r in rows
    )
    assert all("decision" not in r and "verdict" not in r for r in rows)
    assert manifest["allowed_output_artifact_refs"] == []


def test_transition_export_preserves_four_pointers_and_shared_charge_once(tmp_path):
    run = tmp_path / "run"
    run_transition(run)
    output = tmp_path / "analysis"
    export_run(run, output, view="analysis")
    first = [r for r in lines(output / "attempts.jsonl") if r["attempt_index"] == 1]
    assert len(first) == 2
    for r in first:
        pointers = [
            r["proposed_candidates"][0]["artifact"]["sha256"],
            r["native_selected"]["artifact"]["sha256"],
            r["deployed_version"]["sha256"],
            r["next_edit_base"]["sha256"],
        ]
        assert len(set(pointers)) == 4
    costs = lines(output / "costs.jsonl")
    assert len(costs) == 3 and sum(c["synthetic_cost_units"] for c in costs) == 7
    s = json.loads((output / "paired_summary.json").read_text())
    assert s["resources"]["shared_units"] == 5 and s["resources"]["arm_units"] == 2
    assert all(
        a["shared_cost_refs"] == ["golden_pair_01/generate/1"] for a in s["arms"]
    )
    assert all(r["evaluation"] is None for r in lines(output / "evaluations.jsonl"))


@pytest.mark.parametrize(
    "tamper", ["charge_owner", "deployed_target", "missing_to_zero"]
)
def test_export_rejects_corrupt_fact_bindings(tmp_path, tamper):
    run = tmp_path / "run"
    config = load_config(ROOT / "configs/s0_smoke.toml")
    if tamper == "missing_to_zero":
        config = replace(
            config, resources=replace(config.resources, synthetic_units_per_branch=6)
        )
    create_run(config, run)
    with sqlite3.connect(run / "controller.sqlite") as db:
        if tamper == "charge_owner":
            for (name,) in db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='cost_events'"
            ).fetchall():
                db.execute(f'DROP TRIGGER "{name}"')
            db.execute(
                "UPDATE cost_events SET owner_kind='pair',owner_id='forged_pair' WHERE operation_id=(SELECT MIN(operation_id) FROM cost_events)"
            )
        elif tamper == "deployed_target":
            suite, data = db.execute(
                "SELECT suite_id,request_json FROM deployed_suites LIMIT 1"
            ).fetchone()
            request = decode_json(data)
            request["branch_id"] = "other_branch"
            db.execute(
                "UPDATE deployed_suites SET request_json=? WHERE suite_id=?",
                (canonical_bytes(request), suite),
            )
        else:
            suite, data = db.execute(
                "SELECT suite_id,result_json FROM deployed_suites LIMIT 1"
            ).fetchone()
            result = decode_json(data)
            assert result["risk"] is None
            result["risk"] = 0
            result["risk_missing_reason"] = None
            db.execute(
                "UPDATE deployed_suites SET result_json=? WHERE suite_id=?",
                (canonical_bytes(result), suite),
            )
    with pytest.raises(IntegrityError):
        export_run(run, tmp_path / "bad", view="analysis")
    assert not (tmp_path / "bad").exists()


def test_export_retains_confirmed_incident_before_suite_checkpoint(
    tmp_path, monkeypatch
):
    from rsi_lab.engine.execution import DeploymentRuntime

    original = DeploymentRuntime._maybe_pause

    class Kill(BaseException):
        pass

    def after_pause(runtime, *args, **kwargs):
        original(runtime, *args, **kwargs)
        if runtime.is_paused(args[0]):
            raise Kill()

    monkeypatch.setattr(DeploymentRuntime, "_maybe_pause", after_pause)
    run = tmp_path / "run"
    with pytest.raises(Kill):
        create_run(
            replace(
                load_config(ROOT / "configs/s0_smoke.toml"), context_budget_units=0
            ),
            run,
        )
    output = tmp_path / "partial"
    export_run(run, output, view="analysis")
    evaluations = lines(output / "evaluations.jsonl")
    assert evaluations[0]["evaluation"]["risk"] == 1
    assert evaluations[0]["reconstructed_from_incident"]
    assert evaluations[0]["evaluation"]["utility"] is None
    assert all(row["evaluation"] is None for row in evaluations[1:])
    summary = json.loads((output / "paired_summary.json").read_text())
    assert summary["arms"][0]["metrics"]["risk"]["sum_observed"] == 1
    assert summary["arms"][0]["metrics"]["risk"]["mean"] is None
