"""S0 assembly exercises actual shared lifecycle and preregistered denominators."""

from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from rsi_lab.config import load_config, ResourcesConfig
from rsi_lab.contracts import ArtifactRef, PrepareCursor, Version
from rsi_lab.errors import ConflictError, IntegrityError
from rsi_lab.infra.artifacts import ArtifactStore
from rsi_lab.interventions.context import ContextReceipt
from rsi_lab.preparation import PreparationState
from rsi_lab.serialization import canonical_bytes, decode_json
from rsi_lab.studies.s0 import create_run, inspect_run, resume_run
from rsi_lab.subjects.verification import VerificationReceipt

ROOT = Path(__file__).resolve().parents[2]


def config(name, **changes):
    return replace(load_config(ROOT / "configs" / f"s0_{name}.toml"), **changes)


def prepared(path, attempt):
    store = ArtifactStore(path / "objects")
    with sqlite3.connect(path / "controller.sqlite") as db:
        row = db.execute(
            "SELECT cursor_json FROM attempts WHERE attempt_id=?", (attempt,)
        ).fetchone()
    cursor = PrepareCursor.from_dict(decode_json(row[0]))
    return store, PreparationState.from_dict(
        decode_json(store.read_bytes(cursor.local_state))
    )


@pytest.mark.parametrize(
    "name,total,arms,paused",
    [
        ("smoke", 30, 1, 0),
        ("q_pairs", 84, 5, 3),
        ("v_fixed_pool", 90, 4, 2),
        ("probe_admission", 150, 1, 0),
    ],
)
def test_registered_s0_examples_complete_shared_lifecycle(
    name, total, arms, paused, tmp_path
):
    path = tmp_path / name
    result = create_run(config(name), path)
    assert result["stage"] == "M4"
    assert result["registered_horizon_per_branch"] == 3
    assert len(result["plans"]) == arms and len(result["rounds"]) == 3 * arms
    assert result["accounting"]["total_synthetic_units"] == total
    assert result["accounting"]["outstanding_reserved_units"] == 0
    assert len(result["paused_branches"]) == paused
    assert result["candidate_labels"] is None
    assert result["candidate_label_missing_reason"] == "counterfactual_not_measured"
    assert all(r["expected_legal_denominator"] == 1 for r in result["rounds"])
    missing = [r for r in result["rounds"] if r["status"] == "not_run"]
    assert len(missing) == 2 * paused
    assert all(
        r["public_evaluation"] is None and r["reason"] == "unsafe_current_pause"
        for r in missing
    )
    actual = [r for r in result["rounds"] if r["status"] == "committed"]
    assert len(actual) == 3 * arms - 2 * paused
    assert all(r["decision"]["verdict"] == "accept" for r in actual)
    assert all(r["commit"]["attempt_id"] == r["attempt_id"] for r in actual)
    with sqlite3.connect(path / "controller.sqlite") as db:
        assert db.execute(
            'SELECT COUNT(*) FROM commits c JOIN attempts a ON a.attempt_id=c.attempt_id JOIN branches b ON b.branch_id=a.branch_id WHERE b.role="main"'
        ).fetchone()[0] == len(actual)
        events = db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0]
    assert inspect_run(path) == result
    with sqlite3.connect(path / "controller.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM state_events").fetchone()[0] == events
    assert resume_run(path) == result
    with pytest.raises(ConflictError, match="resume"):
        create_run(config(name), path)


def test_q_pairs_bind_distinct_contexts_and_freeze_mechanisms(tmp_path):
    path = tmp_path / "q"
    result = create_run(config("q_pairs"), path)
    expected = {
        "Old": ("G", "W"),
        "New": ("W", "P"),
        "Restore": ("G", "W"),
        "Placebo": ("Z", "W"),
        "Remove": ("W",),
    }
    for plan in result["plans"]:
        store, p = prepared(path, f'{plan["branch_id"]}_001')
        context = ContextReceipt.from_dict(
            decode_json(store.read_bytes(p.context_receipt))
        )
        # The generator's actual context comes from the bound receipt.
        assert tuple(context.retained_ids) == expected[plan["arm"]]
        version = Version.from_dict(
            decode_json(store.read_bytes(p.candidates[0].artifact))
        )
        assert version.s.value == "s.archive_score.v1"
        assert version.q.value == (
            "q.old.v1" if plan["arm"] in ("Old", "Remove") else "q.new.v1"
        )
        assert version.agent.check_scope == (plan["arm"] in ("Old", "Restore"))
    # Both safe arms really run three attempts; archive selection can retain the
    # first occurrence while later generation still occurs and is charged.
    assert (
        len(
            [
                c
                for c in result["accounting"]["cost_events"]
                if c["operation_id"].endswith("/generate")
            ]
        )
        == 9
    )


def test_fixed_pool_first_round_is_shared_then_continues_by_generation(tmp_path):
    path = tmp_path / "v"
    result = create_run(config("v_fixed_pool"), path)
    pool = None
    for plan in result["plans"]:
        store, p = prepared(path, f'{plan["branch_id"]}_001')
        if pool is None:
            pool = p.candidates
        assert p.candidates == pool
        assert [c.occurrence_id for c in pool] == [
            "v_candidate_00_P",
            "v_candidate_01_D",
        ]
        receipt = VerificationReceipt.from_dict(
            decode_json(store.read_bytes(p.verification_receipt))
        )
        assert (
            receipt.synthetic_cost_units
            == {"Old": 8, "New": 2, "Restore": 8, "Placebo": 8}[plan["arm"]]
        )
        assert p.native_selected.occurrence_id == (
            "v_candidate_01_D"
            if plan["arm"] in ("Old", "Restore")
            else "v_candidate_00_P"
        )
    costs = result["accounting"]["cost_events"]
    assert not any(c["operation_id"].endswith("_001/generate") for c in costs)
    assert len([c for c in costs if c["operation_id"].endswith("/generate")]) == 4
    for arm in ("Old", "Restore"):
        store, p = prepared(path, f"r000_{arm}_002")
        assert p.candidates != pool and p.candidates[0].purpose == "generation"


def test_replicates_register_independent_roots_with_common_public_founder(tmp_path):
    result = create_run(
        config("smoke", replicates=2, horizon_attempts=1), tmp_path / "roots"
    )
    assert result["registered_root_count"] == 2
    plans = result["plans"]
    assert plans[0]["root_id"] != plans[1]["root_id"]
    assert plans[0]["history_group_id"] != plans[1]["history_group_id"]
    assert plans[0]["initial_snapshot"] == plans[1]["initial_snapshot"]
    assert result["accounting"]["total_synthetic_units"] == 20


def test_budget_exhaustion_keeps_all_registered_slots(tmp_path):
    result = create_run(
        config("smoke", resources=ResourcesConfig(5)), tmp_path / "budget"
    )
    assert len(result["rounds"]) == 3
    assert all(
        r["status"] == "budget_exhausted" and r["public_evaluation"] is None
        for r in result["rounds"]
    )
    assert all(r["decision"] is None and r["commit"] is None for r in result["rounds"])
    assert result["accounting"]["total_synthetic_units"] == 5
    assert result["branches"][0]["head_revision"] == 0


def test_horizon_preregistered_before_dispatch_and_resume_uses_same_slots(tmp_path):
    class Crash(BaseException):
        pass

    def stop(point):
        if point == "after_model_response":
            raise Crash()

    path = tmp_path / "interrupted"
    with pytest.raises(Crash):
        create_run(config("q_pairs"), path, fault_hook=stop)
    before = inspect_run(path)
    assert len(before["rounds"]) == 15
    assert all(r["status"] == "pending" for r in before["rounds"])
    assert before["accounting"]["outstanding_reserved_units"] == 5
    completed = resume_run(path)
    assert completed["accounting"]["total_synthetic_units"] == 84
    assert completed["accounting"]["outstanding_reserved_units"] == 0
    assert len(completed["rounds"]) == 15
    assert resume_run(path) == completed


def test_changed_stored_config_is_not_silently_resumed(tmp_path):
    path = tmp_path / "changed"
    create_run(config("smoke", horizon_attempts=1), path)
    saved = decode_json((path / "resolved_config.json").read_bytes())
    saved["horizon_attempts"] = 2
    (path / "resolved_config.json").write_bytes(canonical_bytes(saved))
    with pytest.raises(IntegrityError, match="changed"):
        resume_run(path)


def test_rejection_executes_old_deployment_for_each_registered_attempt(tmp_path):
    result = create_run(
        config(
            "smoke",
            horizon_attempts=2,
            admission_policy="reject_all",
            context_budget_units=0,
        ),
        tmp_path / "rejected",
    )
    assert len(result["rounds"]) == 2 and result["paused_branches"] == []
    assert result["accounting"]["total_synthetic_units"] == 20
    assert all(r["decision"]["verdict"] == "reject" for r in result["rounds"])
    assert all(
        r["public_evaluation"]["risk"] == 0 and r["public_evaluation"]["complete"]
        for r in result["rounds"]
    )
    store, p = prepared(tmp_path / "rejected", "r000_Main_001")
    candidate = Version.from_dict(
        decode_json(store.read_bytes(p.native_selected.artifact))
    )
    assert candidate.agent.check_scope is False
    # These are old-deployment outcomes, not counterfactual labels of P.
    assert result["candidate_labels"] is None
