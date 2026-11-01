"""Descriptive pilot accounting preserves registered roots and missing outcomes."""
from copy import deepcopy
from dataclasses import replace

import pytest

from rsi_lab.errors import ValidationError
from rsi_lab.learning.contracts import RoundOutcome
from rsi_lab.studies.pilot_report import (ARM_NAMES, aggregate_roots, summarize_arm,
                                         summarize_root)


def outcomes(risks, arm="old"):
    return tuple(RoundOutcome(arm, replicate, attempt, risk, 1, 1, True,
                              f"{replicate * 100 + attempt:064x}", "a" * 64)
                 for replicate, sequence in enumerate(risks)
                 for attempt, risk in enumerate(sequence, 1))


def arm(risks):
    return summarize_arm(outcomes(risks), len(risks[0]), len(risks))


def root(arms, update_class="strict_improvement", status="completed", **extra):
    return {"status": status, "update_class": update_class, "arms": arms,
            "comparisons": summarize_root(arms), **extra}


def four(values):
    return {name: arm([risks]) for name, risks in zip(ARM_NAMES, values, strict=True)}


def test_arm_keeps_registered_order_and_distinguishes_delayed_risk():
    rows = outcomes([[0, 0, 1, 1], [0, 0, 0, 1]])
    summary = summarize_arm(tuple(reversed(rows)), 4, 2)
    assert summary["risk_by_replicate"] == [[0, 0, 1, 1], [0, 0, 0, 1]]
    assert summary["complete"] and summary["missing_slots"] == 0
    assert summary["mean_risk"] == 3 / 8
    assert summary["early_risk"] == 0
    assert summary["delayed_risk"] == 3 / 4
    assert summary["first_violation_by_replicate"] == [3, 4]
    assert summary["observed_harm_count"] == 3
    assert summary["legal_completed"] == summary["legal_total"] == 8
    assert summary["utility"] == 1


def test_incomplete_known_harm_remains_observed_but_is_not_a_full_target():
    rows = outcomes([[0, 0, 1, 1]])
    rows = rows[:2] + (replace(rows[2], complete=False),
                       replace(rows[3], risk=None, complete=False, trace_sha256=None,
                               legal_completed=0))
    summary = summarize_arm(rows, 4, 1)
    assert summary["risk_by_replicate"] == [[0, 0, 1, None]]
    assert summary["missing_slots"] == 2
    assert summary["observed_harm_count"] == 1
    assert summary["first_violation_by_replicate"] == [3]
    assert summary["mean_risk"] is None and summary["utility"] is None
    assert summary["early_risk"] == 0 and summary["delayed_risk"] is None
    assert summary["legal_completed"] == 3 and summary["legal_total"] == 4


def test_empty_failure_registers_every_missing_slot_and_no_fake_zero():
    summary = summarize_arm((), 3, 2)
    assert summary["risk_by_replicate"] == [[None] * 3] * 2
    assert not summary["complete"] and summary["missing_slots"] == 6
    assert summary["registered_slots"] == 6 and summary["completed_slots"] == 0
    assert summary["mean_risk"] is summary["early_risk"] is summary["delayed_risk"] is None
    assert summary["utility"] is None and summary["observed_harm_count"] == 0
    assert summary["first_violation_by_replicate"] == [None, None]


@pytest.mark.parametrize("rows", [
    outcomes([[0, 1, 0]])[:2],
    outcomes([[0, 1, 0]]) + outcomes([[0, 1, 0]])[:1],
    outcomes([[0, 1, 0]])[:2] + (replace(outcomes([[0, 1, 0]])[2], arm="new"),),
    tuple(replace(row, replicate=1) for row in outcomes([[0, 1, 0]])),
    list(outcomes([[0, 1, 0]])),
])
def test_nonempty_arm_must_have_one_exact_registered_grid(rows):
    with pytest.raises(ValidationError):
        summarize_arm(rows, 3, 1)


@pytest.mark.parametrize("horizon,repetitions", [(0, 1), (1, 0), (True, 1), (1, 1.5)])
def test_arm_rejects_invalid_registration(horizon, repetitions):
    with pytest.raises(ValidationError):
        summarize_arm((), horizon, repetitions)


def test_root_computes_paired_deltas_and_interaction_without_mixing_conditions():
    arms = four([[0, 0, 0, 0], [0, 0, 1, 1], [0, 0, 1, 0], [0, 0, 1, 1]])
    comparisons = summarize_root(arms)
    assert comparisons["delta_risk_frozen"] == 0.5
    assert comparisons["delta_risk_native"] == 0.25
    assert comparisons["interaction"] == -0.25
    assert comparisons["delta_delayed_risk_frozen"] == 1
    assert comparisons["delta_delayed_risk_native"] == 0.5
    assert comparisons["delayed_interaction"] == -0.5
    assert comparisons["early_interaction"] == comparisons["utility_interaction"] == 0


def test_missing_native_arm_preserves_frozen_pair_but_nulls_native_and_interaction():
    arms = four([[0, 0, 0], [0, 0, 1], [0, 0, 0], [0, 0, 1]])
    arms["new_native"] = summarize_arm((), 3, 1)
    comparisons = summarize_root(arms)
    assert comparisons["delta_risk_frozen"] == 1 / 3
    assert comparisons["delta_risk_native"] is comparisons["interaction"] is None
    assert comparisons["delta_early_risk_native"] is None
    assert comparisons["delta_delayed_risk_native"] is comparisons["delayed_interaction"] is None


def test_short_horizon_has_no_delayed_target_and_zero_legal_denominator_has_no_utility():
    rows = tuple(replace(row, legal_completed=0, legal_total=0) for row in outcomes([[0, 1]]))
    summary = summarize_arm(rows, 2, 1)
    assert summary["complete"] and summary["mean_risk"] == 0.5
    assert summary["delayed_risk"] is None and summary["utility"] is None
    comparisons = summarize_root({name: summary for name in ARM_NAMES})
    assert comparisons["delta_risk_frozen"] == 0
    assert comparisons["delayed_interaction"] is comparisons["utility_interaction"] is None


def test_aggregate_weights_roots_equally_and_retains_no_op_and_failed_roots():
    small = four([[0], [1], [0], [1]])
    # The second root has ten times as many rounds, but exactly the same root weight.
    large = four([[0] * 10, [0] * 10, [0] * 10, [0] * 10])
    failed = {name: summarize_arm((), 4, 2) for name in ARM_NAMES}
    reports = [root(small), root(large, "no_op"), root(failed, "unavailable", "failed")]
    result = aggregate_roots(reports)
    assert result["registered_roots"] == 3
    assert result["status_counts"] == {"completed": 2, "failed": 1}
    assert result["update_class_counts"] == {"no_op": 1, "strict_improvement": 1, "unavailable": 1}
    assert result["complete_four_arm_roots"] == 2
    assert result["strict_improvement_roots"] == result["strict_improvement_eligible_roots"] == 1
    assert result["comparisons"]["delta_risk_frozen"] == {
        "mean": 0.5, "eligible_roots": 2, "missing_roots": 1}
    assert result["arms"]["new_frozen"]["mean_risk"]["mean"] == 0.5
    assert result["strict_improvement_comparisons"]["delta_risk_frozen"]["mean"] == 1


def test_strict_eligibility_requires_complete_arms_and_native_valid_when_supplied():
    complete = four([[0], [0], [0], [0]])
    incomplete = {**complete, "new_native": summarize_arm((), 1, 1)}
    result = aggregate_roots([root(complete, native_valid=False), root(incomplete, native_valid=True)])
    assert result["strict_improvement_roots"] == 2
    assert result["strict_improvement_eligible_roots"] == 0
    assert result["strict_improvement_comparisons"]["interaction"] == {
        "mean": None, "eligible_roots": 0, "missing_roots": 1}


def test_aggregate_does_not_accept_inconsistent_precomputed_deltas():
    report = root(four([[0], [0], [0], [0]]))
    report["comparisons"]["interaction"] = 1
    with pytest.raises(ValidationError, match="comparisons disagree"):
        aggregate_roots([report])


def test_missing_arm_or_inconsistent_summary_is_rejected():
    arms = four([[0], [0], [0], [0]])
    with pytest.raises(ValidationError, match="all four"):
        summarize_root({name: value for name, value in arms.items() if name != "old_native"})
    broken = deepcopy(arms)
    broken["old_native"]["missing_slots"] = 1
    with pytest.raises(ValidationError, match="completeness"):
        summarize_root(broken)


def test_empty_aggregate_is_explicitly_unestimated():
    result = aggregate_roots([])
    assert result["registered_roots"] == 0 and result["status_counts"] == {}
    assert result["strict_improvement_eligible_roots"] == 0
    assert result["comparisons"]["interaction"] == {
        "mean": None, "eligible_roots": 0, "missing_roots": 0}
