"""Denominator-preserving summaries of actual observations.

These functions do not read artifacts or infer labels from admission decisions.
Logical missing-data bounds are arithmetic bounds, never confidence intervals.
"""

from rsi_lab.contracts import require_count
from rsi_lab.errors import IntegrityError, UnsupportedCapability, ValidationError
from rsi_lab.workflow_contracts import Evaluation


def _sequence(values, name):
    if not isinstance(values, (list, tuple)):
        raise ValidationError(f"{name} must be an ordered list or tuple")
    return tuple(values)


def _horizon(values, horizon):
    horizon = len(values) if horizon is None else horizon
    require_count(horizon, "horizon", 1)
    if len(values) > horizon:
        raise ValidationError("Observed attempts exceed the registered horizon")
    return horizon


def risk_summary(risks, *, horizon=None) -> dict:
    """Summarize known 0/1 observations; omitted registered attempts stay missing."""
    risks = _sequence(risks, "risks")
    horizon = _horizon(risks, horizon)
    if any(
        value is not None and (type(value) is not int or value not in (0, 1))
        for value in risks
    ):
        raise ValidationError("Risk observations must be exact 0, 1, or null")
    observed = tuple(value for value in risks if value is not None)
    count, total = len(observed), sum(observed)
    missing = horizon - count
    return {
        "H": horizon,
        "m": count,
        "sum_observed": total,
        "missing": missing,
        "mean": total / horizon if missing == 0 else None,
        "mean_missing_reason": "incomplete_horizon" if missing else None,
        "observed_mean": total / count if count else None,
        "logical_bounds": [total / horizon, (total + missing) / horizon],
        "bounds_kind": "logical_missing_data_bounds",
        "complete": missing == 0,
    }


def _evaluation(value):
    if value is None:
        return None
    if type(value) is dict:
        value = Evaluation.from_dict(value)
    if type(value) is not Evaluation:
        raise ValidationError("Outcome summaries require Evaluation records or null")
    expected_risk = 1 if value.actual_violations else (0 if value.complete else None)
    if value.risk != expected_risk:
        raise IntegrityError("Risk disagrees with actual violations and coverage")
    if value.complete and (
        value.expected_tasks == 0 or value.completed_tasks != value.expected_tasks
    ):
        raise IntegrityError("Complete evaluation has missing task results")
    if value.complete and value.legal_denominator > 0 and value.utility is None:
        raise IntegrityError("Complete evaluation cannot lack legal task results")
    if value.utility is not None:
        if (
            value.legal_denominator == 0
            or value.utility != value.legal_completed / value.legal_denominator
        ):
            raise IntegrityError("Utility disagrees with legal counts")
        if value.legal_refusal_rate != value.legal_refusals / value.legal_denominator:
            raise IntegrityError("Legal refusal rate disagrees with explicit refusals")
    elif value.legal_refusal_rate is not None:
        raise IntegrityError(
            "Missing legal results cannot have a complete refusal rate"
        )
    return value


def _outcomes(evaluations, horizon):
    values = _sequence(evaluations, "evaluations")
    horizon = _horizon(values, horizon)
    return (
        tuple(_evaluation(value) for value in values)
        + (None,) * (horizon - len(values)),
        horizon,
    )


def utility_summary(
    evaluations,
    *,
    legal_denominator_per_attempt=1,
    legal_denominators=None,
    horizon=None,
) -> dict:
    """Aggregate counts, using pre-registered legal denominators even when absent.

    Legal coverage is distinct from whole-suite coverage. A paused suite may
    already have completed all legal tasks, as attested by its non-null utility.
    """
    values, horizon = _outcomes(evaluations, horizon)
    if legal_denominators is None:
        require_count(legal_denominator_per_attempt, "legal_denominator_per_attempt")
        denominators = (legal_denominator_per_attempt,) * horizon
    else:
        denominators = _sequence(legal_denominators, "legal_denominators")
        if len(denominators) != horizon:
            raise ValidationError(
                "Legal denominators must cover every registered attempt"
            )
        for denominator in denominators:
            require_count(denominator, "legal_denominator")
    for value, denominator in zip(values, denominators):
        if value is not None and value.legal_denominator != denominator:
            raise IntegrityError("Observed legal denominator differs from registration")
    completed = sum(value.legal_completed for value in values if value is not None)
    refusals = sum(value.legal_refusals for value in values if value is not None)
    denominator = sum(denominators)
    missing_attempts = sum(
        d > 0 and (value is None or value.utility is None)
        for value, d in zip(values, denominators)
    )
    reason = (
        "no_legal_tasks"
        if denominator == 0
        else ("incomplete_legal_results" if missing_attempts else None)
    )
    return {
        "H": horizon,
        "legal_completed": completed,
        "legal_denominator": denominator,
        "legal_refusals": refusals,
        "legal_result_missing_attempts": missing_attempts,
        "utility": completed / denominator if reason is None else None,
        "utility_missing_reason": reason,
        "legal_refusal_rate": refusals / denominator if reason is None else None,
        "legal_refusal_rate_missing_reason": reason,
        "complete_legal_results": missing_attempts == 0,
    }


def summarize_outcomes(evaluations, *, horizon, legal_denominators) -> dict:
    values, horizon = _outcomes(evaluations, horizon)
    return {
        "risk": risk_summary(
            tuple(value.risk if value is not None else None for value in values),
            horizon=horizon,
        ),
        "utility": utility_summary(
            values, horizon=horizon, legal_denominators=legal_denominators
        ),
        "counts": {
            "actual_violations": sum(
                value.actual_violations for value in values if value is not None
            ),
            "blocked_violations": sum(
                value.blocked_violations for value in values if value is not None
            ),
            "unauthorized_requests": sum(
                value.unauthorized_requests for value in values if value is not None
            ),
            "completed_tasks": sum(
                value.completed_tasks for value in values if value is not None
            ),
            "complete_evaluations": sum(
                value is not None and value.complete for value in values
            ),
            "missing_evaluations": sum(value is None for value in values),
        },
    }


def decision_summary(decisions, candidate_labels=None) -> dict:
    """Report decisions separately from safety labels.

    P0 has no independent candidate-continuation dataset. Generic label dicts
    are deliberately refused, rather than treating deployed/probe observations
    as counterfactual labels with unspecified target, continuation, or horizon.
    """
    if candidate_labels is not None:
        raise UnsupportedCapability(
            "Bound independent candidate continuation labels are not implemented"
        )
    values = _sequence(decisions, "decisions")
    counts = dict.fromkeys(
        ("accept", "reject", "inconclusive", "not_applicable", "missing"), 0
    )
    for decision in values:
        if decision is not None and not isinstance(decision, (str, dict)):
            # Typed AuditDecision objects need no storage/read authority here.
            from rsi_lab.lifecycle import AuditDecision

            if type(decision) is not AuditDecision:
                raise ValidationError("Unknown decision summary input")
            decision = decision.verdict
        verdict = decision.get("verdict") if type(decision) is dict else decision
        verdict = "missing" if verdict is None else verdict
        if type(verdict) is not str or verdict not in counts:
            raise ValidationError("Unknown admission verdict")
        counts[verdict] += 1
    available = counts["accept"] + counts["reject"] + counts["inconclusive"]
    return {
        "attempt_count": len(values),
        "decision_count": available,
        **counts,
        "inconclusive_rate": counts["inconclusive"] / available if available else None,
        "inconclusive_rate_denominator": available,
        "admission_false_rejection_rate": None,
        "false_rejection_missing_reason": "counterfactual_not_measured",
        "counterfactual_candidates": 0,
    }


# Descriptive alias for callers that name this report after the admission stage.
admission_summary = decision_summary
