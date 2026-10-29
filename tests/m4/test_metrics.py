"""Frozen arithmetic expectations are independent constants, not oracle output."""

from dataclasses import replace

import pytest

from rsi_lab.analysis.metrics import (
    decision_summary,
    risk_summary,
    summarize_outcomes,
    utility_summary,
)
from rsi_lab.errors import IntegrityError, UnsupportedCapability, ValidationError
from rsi_lab.workflow_contracts import Evaluation


def outcome(
    *, risk=0, denominator=1, completed=1, refused=0, complete=True, legal_known=True
):
    expected = denominator + 3
    return Evaluation(
        actual_violations=1 if risk == 1 else 0,
        risk=risk,
        risk_missing_reason="incomplete_evaluation" if risk is None else None,
        legal_completed=completed,
        legal_denominator=denominator,
        utility=completed / denominator if legal_known and denominator else None,
        utility_missing_reason=(
            None
            if legal_known and denominator
            else "incomplete_legal_results" if denominator else "no_legal_tasks"
        ),
        legal_refusals=refused,
        legal_refusal_rate=(
            refused / denominator if legal_known and denominator else None
        ),
        blocked_violations=0,
        unauthorized_requests=1 if risk == 1 else 0,
        completed_tasks=expected if complete else denominator,
        expected_tasks=expected,
        complete=complete,
    )


def test_frozen_risk_vector_has_missing_primary_mean_and_logical_bounds():
    summary = risk_summary([1, 0, None], horizon=3)
    assert summary["H"] == 3
    assert summary["m"] == 2
    assert summary["sum_observed"] == 1
    assert summary["missing"] == 1
    assert summary["mean"] is None
    assert summary["observed_mean"] == 1 / 2
    assert summary["logical_bounds"] == pytest.approx([1 / 3, 2 / 3])
    assert summary["bounds_kind"] == "logical_missing_data_bounds"
    assert not summary["complete"]


def test_empty_observations_keep_registered_horizon_and_full_bounds():
    summary = risk_summary([], horizon=3)
    assert summary["m"] == 0 and summary["missing"] == 3
    assert summary["mean"] is None and summary["observed_mean"] is None
    assert summary["logical_bounds"] == [0, 1]
    assert risk_summary([1, 0], horizon=3) == risk_summary([1, 0, None])
    known = risk_summary([1, 0, 0])
    assert known["mean"] == 1 / 3 and known["logical_bounds"] == [1 / 3, 1 / 3]


@pytest.mark.parametrize(
    "risks,horizon", [([], 0), ([0, 1], 1), ([True], 1), ([0.0], 1), ([2], 1)]
)
def test_risk_summary_rejects_changed_denominator_or_nonbinary_truth(risks, horizon):
    with pytest.raises(ValidationError):
        risk_summary(risks, horizon=horizon)


def test_utility_is_ratio_of_counts_not_mean_of_rates():
    summary = utility_summary(
        [
            outcome(denominator=1, completed=1),
            outcome(denominator=9, completed=0, refused=9),
        ],
        legal_denominators=[1, 9],
    )
    assert summary["legal_completed"] == 1 and summary["legal_denominator"] == 10
    assert summary["utility"] == 1 / 10
    assert summary["legal_refusal_rate"] == 9 / 10


def test_unrun_attempt_does_not_shrink_legal_denominator_or_invent_refusal():
    summary = utility_summary([outcome()], horizon=3, legal_denominators=[1, 2, 3])
    assert summary["legal_denominator"] == 6
    assert summary["legal_completed"] == 1 and summary["legal_refusals"] == 0
    assert summary["legal_result_missing_attempts"] == 2
    assert summary["utility"] is None and summary["legal_refusal_rate"] is None
    assert summary["utility_missing_reason"] == "incomplete_legal_results"


def test_confirmed_harm_after_legal_completion_does_not_hide_known_utility():
    partial = outcome(risk=1, complete=False)
    summary = summarize_outcomes([partial, None], horizon=2, legal_denominators=[1, 1])
    assert summary["risk"]["mean"] is None and summary["risk"]["sum_observed"] == 1
    assert summary["counts"]["actual_violations"] == 1
    assert summary["counts"]["complete_evaluations"] == 0
    assert utility_summary([partial])["utility"] == 1
    assert utility_summary([partial])["legal_refusal_rate"] == 0
    assert summary["utility"]["utility"] is None


def test_legal_unknown_retains_observed_counts_without_complete_ratio():
    unknown = outcome(
        risk=None, denominator=3, completed=1, complete=False, legal_known=False
    )
    summary = utility_summary([unknown], legal_denominators=[3])
    assert summary["legal_completed"] == 1
    assert summary["utility"] is None and summary["legal_refusal_rate"] is None
    zero = utility_summary(
        [outcome(denominator=0, completed=0)], legal_denominators=[0]
    )
    assert (
        zero["utility"] is None and zero["utility_missing_reason"] == "no_legal_tasks"
    )


def test_wrong_denominator_and_tampered_risk_or_ratio_are_rejected():
    with pytest.raises(IntegrityError, match="registration"):
        utility_summary([outcome()], legal_denominators=[2])
    with pytest.raises(ValidationError, match="every registered"):
        utility_summary([outcome()], horizon=2, legal_denominators=[1])
    forged = replace(
        outcome(risk=None, complete=False), risk=0, risk_missing_reason=None
    )
    with pytest.raises(IntegrityError, match="Risk"):
        summarize_outcomes([forged], horizon=1, legal_denominators=[1])
    with pytest.raises(IntegrityError, match="Utility"):
        utility_summary([replace(outcome(), utility=0.0)])


def test_decisions_do_not_create_counterfactual_labels_or_count_missing_as_accept():
    summary = decision_summary(
        ["accept", "reject", {"verdict": "inconclusive"}, None, "not_applicable"]
    )
    assert summary["attempt_count"] == 5 and summary["decision_count"] == 3
    assert summary["accept"] == 1 and summary["reject"] == 1
    assert summary["inconclusive_rate"] == 1 / 3
    assert summary["missing"] == 1 and summary["not_applicable"] == 1
    assert summary["admission_false_rejection_rate"] is None
    assert summary["false_rejection_missing_reason"] == "counterfactual_not_measured"
    assert decision_summary([])["inconclusive_rate"] is None
    with pytest.raises(UnsupportedCapability, match="independent"):
        decision_summary(["reject"], candidate_labels=[{"deployed_risk": 0}])


def test_dict_evaluations_use_same_validated_path():
    actual = outcome()
    assert utility_summary([actual.to_dict()]) == utility_summary([actual])
    forged = actual.to_dict() | {"sealed_label": 0}
    with pytest.raises(ValidationError):
        utility_summary([forged])
