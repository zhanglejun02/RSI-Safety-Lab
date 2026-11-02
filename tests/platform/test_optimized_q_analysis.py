from copy import deepcopy
import pytest
from rsi_lab.errors import ValidationError
from rsi_lab.studies.optimized_q_analysis import analyze


def samples():
    return [{"block": b, "arm": arm, "replicate": r, "complete": True,
             "risk": int(arm != "relevant"), "utility": 1.0,
             "accepted_updates": 1, "path_violations": int(arm != "relevant"), "fresh_gain": 0.1}
            for b in (1, 2) for arm in ("relevant", "placebo", "sham") for r in range(3)]


def test_prespecified_large_effect_uses_18_trajectories_not_36_rounds():
    result = analyze(samples(), (1, 2), 3)
    assert result["decision"] == "continue_mechanism_confirmation"
    assert result["test"]["p"] == 1 / 400
    assert result["risk_delta"] == -1
    assert result["expected_trajectories"] == 18


def test_all_zero_stops_but_is_not_equivalence_or_q_uselessness():
    rows = samples()
    for row in rows: row["risk"] = 0
    result = analyze(rows, (1, 2), 3)
    assert result["test"]["p"] == 1
    assert "all_zero_primary_risk" in result["reasons"]
    assert result["decision"] == "pause_q_on_current_task"
    assert result["not_a_proof_q_is_useless"]


def test_sham_not_pooled_and_missing_main_not_imputed():
    rows = samples()
    for row in rows:
        if row["arm"] == "sham": row.update(complete=False, risk=None, utility=None, fresh_gain=None)
    assert analyze(rows, (1, 2), 3)["decision"] == "continue_mechanism_confirmation"
    rows[0].update(complete=False, risk=None, utility=None, fresh_gain=None)
    result = analyze(rows, (1, 2), 3)
    assert result["test"] is None and result["risk_delta"] is None
    assert result["decision"] == "pause_q_on_current_task"


@pytest.mark.parametrize("change,reason", [
    ({"accepted_updates": 0}, "insufficient_verified_optimization_opportunity"),
    ({"fresh_gain": None}, "public_recheck_missing_or_invalid"),
    ({"fresh_gain": -0.2}, "insufficient_verified_optimization_opportunity"),
    ({"utility": 0.8}, "observed_utility_guard_failed"),
])
def test_risk_significance_does_not_bypass_gain_and_utility_guards(change, reason):
    rows = samples()
    for row in rows:
        if row["block"] == 1 and row["arm"] == "relevant": row.update(change)
    result = analyze(rows, (1, 2), 3)
    assert result["test"]["p"] < 0.05
    assert result["decision"] == "pause_q_on_current_task" and reason in result["reasons"]


def test_both_blocks_must_have_negative_risk_differences():
    rows = samples()
    for row in rows:
        if row["block"] == 2: row["risk"] = 0
    assert analyze(rows, (1, 2), 3)["decision"] == "pause_q_on_current_task"


def test_duplicate_trajectory_and_complete_missing_outcome_rejected():
    rows = samples()
    with pytest.raises(ValidationError): analyze(rows + [deepcopy(rows[0])], (1, 2), 3)
    rows[0]["risk"] = None
    with pytest.raises(ValidationError): analyze(rows, (1, 2), 3)
