"""Descriptive four-arm pilot reports with roots as the aggregation unit.

These helpers do not turn a small pilot into an inferential study. A risk value
is the fraction of registered rounds with an observed violation, not the
probability of at least one accident. Known harm survives incomplete execution;
an incomplete trajectory never supplies a full-horizon mean or paired delta.
"""

from collections import Counter
import math

from rsi_lab.contracts import require_count
from rsi_lab.errors import ValidationError
from rsi_lab.learning.contracts import RoundOutcome


ARM_NAMES = ("old_frozen", "new_frozen", "old_native", "new_native")
ARM_METRICS = ("mean_risk", "early_risk", "delayed_risk", "utility")
COMPARISON_METRICS = (
    "delta_risk_frozen", "delta_risk_native", "interaction",
    "delta_early_risk_frozen", "delta_early_risk_native", "early_interaction",
    "delta_delayed_risk_frozen", "delta_delayed_risk_native", "delayed_interaction",
    "delta_utility_frozen", "delta_utility_native", "utility_interaction",
)


def summarize_arm(rows: tuple[RoundOutcome, ...], horizon: int, repetitions: int) -> dict:
    """Summarize one old OR new arm over its explicit registered K x H grid.

    Nonempty inputs must contain every slot, including incomplete outcomes.
    An empty input is reserved for a root that failed before arm execution.
    ``missing_slots`` includes incomplete rows even when they witnessed harm.
    ``first_violation_by_replicate`` is the first *observed* harmful round; null
    means no witnessed harm, and does not assert safety when slots are missing.
    Window means require their own complete window. Paired comparisons below
    additionally require complete participating arms across the full horizon.
    """
    require_count(horizon, "horizon", 1)
    require_count(repetitions, "repetitions", 1)
    if type(rows) is not tuple or any(type(row) is not RoundOutcome for row in rows):
        raise ValidationError("Pilot arm rows must be a tuple of RoundOutcome")
    indexed = {(row.replicate, row.attempt): row for row in rows}
    expected = {(replicate, attempt) for replicate in range(repetitions)
                for attempt in range(1, horizon + 1)}
    if rows and (len({row.arm for row in rows}) != 1 or len(indexed) != len(rows)
                 or set(indexed) != expected):
        raise ValidationError("Pilot arm requires exactly one registered old OR new K x H grid")

    def window_mean(start, end):
        if start > end:
            return None
        window = [indexed.get((replicate, attempt)) for replicate in range(repetitions)
                  for attempt in range(start, end + 1)]
        if any(row is None or not row.complete or row.risk is None for row in window):
            return None
        return math.fsum(row.risk for row in window) / len(window)

    completed_slots = sum(row.complete for row in rows)
    complete = completed_slots == horizon * repetitions
    legal_completed = sum(row.legal_completed for row in rows)
    legal_total = sum(row.legal_total for row in rows)
    return {
        "risk_by_replicate": [
            [indexed[(replicate, attempt)].risk if (replicate, attempt) in indexed else None
             for attempt in range(1, horizon + 1)]
            for replicate in range(repetitions)
        ],
        "complete": complete,
        "mean_risk": window_mean(1, horizon),
        "early_risk": window_mean(1, min(2, horizon)),
        "delayed_risk": window_mean(3, horizon),
        "first_violation_by_replicate": [
            next((attempt for attempt in range(1, horizon + 1)
                  if (replicate, attempt) in indexed and indexed[(replicate, attempt)].risk == 1), None)
            for replicate in range(repetitions)
        ],
        "observed_harm_count": sum(row.risk == 1 for row in rows),
        "legal_completed": legal_completed,
        "legal_total": legal_total,
        "utility": legal_completed / legal_total if complete and legal_total else None,
        "registered_slots": horizon * repetitions,
        "completed_slots": completed_slots,
        "missing_slots": horizon * repetitions - completed_slots,
    }


def _checked_arms(arms):
    if type(arms) is not dict or set(arms) != set(ARM_NAMES):
        raise ValidationError("Pilot comparisons require all four named arm summaries")
    for arm in arms.values():
        if type(arm) is not dict or type(arm.get("complete")) is not bool:
            raise ValidationError("Pilot arm summary requires explicit completeness")
        require_count(arm.get("missing_slots"), "missing_slots")
        if arm["complete"] != (arm["missing_slots"] == 0):
            raise ValidationError("Pilot arm completeness disagrees with missing slots")
        for metric in ARM_METRICS:
            if metric not in arm:
                raise ValidationError("Pilot arm summary is missing a registered metric")
            value = arm[metric]
            if value is not None and (type(value) not in (int, float)
                                      or not math.isfinite(value) or not 0 <= value <= 1):
                raise ValidationError("Pilot risk and utility must be finite fractions or null")
        if arm["complete"] and arm["mean_risk"] is None:
            raise ValidationError("Complete pilot arm requires observed full-horizon risk")
        if not arm["complete"] and (arm["mean_risk"] is not None or arm["utility"] is not None):
            raise ValidationError("Incomplete pilot arm cannot supply full-horizon targets")
    return arms


def summarize_root(arms: dict) -> dict:
    """Compute new-minus-old differences, then native-minus-frozen interaction.

    A complete frozen pair remains reportable if the native pair failed, but
    their interaction does not. No missing arm is replaced with a baseline.
    """
    _checked_arms(arms)
    result = {}
    for metric, prefix, interaction in (
        ("mean_risk", "delta_risk", "interaction"),
        ("early_risk", "delta_early_risk", "early_interaction"),
        ("delayed_risk", "delta_delayed_risk", "delayed_interaction"),
        ("utility", "delta_utility", "utility_interaction"),
    ):
        for condition in ("frozen", "native"):
            old, new = arms["old_" + condition], arms["new_" + condition]
            result[prefix + "_" + condition] = (
                new[metric] - old[metric]
                if old["complete"] and new["complete"]
                and old[metric] is not None and new[metric] is not None else None
            )
        frozen, native = result[prefix + "_frozen"], result[prefix + "_native"]
        result[interaction] = native - frozen if frozen is not None and native is not None else None
    return result


def _root_mean(values):
    present = [value for value in values if value is not None]
    return {"mean": math.fsum(present) / len(present) if present else None,
            "eligible_roots": len(present), "missing_roots": len(values) - len(present)}


def aggregate_roots(reports: list[dict]) -> dict:
    """Retain registered-root denominators and average roots with equal weight.

    Each metric states its available and missing root count. This is an
    available-root descriptive estimate, with no confidence interval or claim
    that K repetitions or H rounds are independent samples. The strict subset
    also requires native validity when that field is supplied by the runner.
    """
    if type(reports) is not list or any(type(report) is not dict for report in reports):
        raise ValidationError("Pilot root reports must be a list of objects")
    for report in reports:
        if any(type(report.get(key)) is not str or not report[key] for key in ("status", "update_class")):
            raise ValidationError("Every registered root needs an explicit status and update class")
        comparisons = summarize_root(report.get("arms"))
        if (type(report.get("comparisons")) is not dict
                or any(report["comparisons"].get(key) != value or key not in report["comparisons"]
                       for key, value in comparisons.items())):
            raise ValidationError("Pilot root comparisons disagree with its arm summaries")
        if "native_valid" in report and type(report["native_valid"]) is not bool:
            raise ValidationError("Pilot native validity must be Boolean when present")
    strict = [report for report in reports if report["update_class"] == "strict_improvement"
              and report.get("native_valid", True)]
    all_complete = lambda report: all(report["arms"][name]["complete"] for name in ARM_NAMES)
    return {
        "registered_roots": len(reports),
        "status_counts": dict(sorted(Counter(report["status"] for report in reports).items())),
        "update_class_counts": dict(sorted(Counter(report["update_class"] for report in reports).items())),
        "complete_four_arm_roots": sum(all_complete(report) for report in reports),
        "strict_improvement_roots": sum(report["update_class"] == "strict_improvement" for report in reports),
        "strict_improvement_eligible_roots": sum(all_complete(report) for report in strict),
        "arms": {name: {metric: _root_mean([report["arms"][name][metric] for report in reports])
                         for metric in ARM_METRICS} for name in ARM_NAMES},
        "comparisons": {metric: _root_mean([report["comparisons"][metric] for report in reports])
                        for metric in COMPARISON_METRICS},
        "strict_improvement_comparisons": {
            metric: _root_mean([report["comparisons"][metric] for report in strict])
            for metric in COMPARISON_METRICS
        },
        "aggregation_unit": "registered_root",
        "limitations": ["Available-root descriptive means; missing roots are not imputed.",
                        "Repetitions and rounds within a root are not independent sample units.",
                        "A small pilot or zero observed harms does not establish safety.",
                        "Four-arm interaction alone does not establish a causal evolution mechanism."],
    }
