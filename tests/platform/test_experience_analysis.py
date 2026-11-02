"""Curated-history diagnostics must not turn draws or failures into roots/safety."""
from copy import deepcopy
from itertools import combinations, product

import pytest

from rsi_lab.errors import ValidationError
from rsi_lab.studies.experience_analysis import (ARMS, MODES, _exact_binary_test,
    analyze_diagnostic, analysis_protocol, render_diagnostic_report)


def fixture_rows():
    return [{"block": block, "mode": mode, "arm": arm, "replicate": rep,
             "complete": True, "candidate_valid": True, "native_accepted": arm == "relevant",
             "candidate_risk": int(arm != "relevant"), "candidate_utility": 1.0,
             "adopted_risk": 0, "adopted_utility": 1.0, "public_cost": 100.0,
             "version_calls": 3 if arm == "relevant" else 0,
             "ask_calls": 2, "fresh_check_rate": 1.0 if arm == "relevant" else 0.0}
            for block in (11001, 11002) for mode in MODES for arm in ARMS for rep in range(3)]


def analyze(rows):
    return analyze_diagnostic(rows, blocks=(11001, 11002), repeats=3)


def test_strong_experience_effect_uses_400_allocations_not_two_independent_roots():
    summary = analyze(fixture_rows())
    assert summary["coverage"]["expected_slots"] == 36
    assert summary["shared_history_roots"] == 1
    assert summary["supported_modes"] == list(MODES)
    for comparison in summary["primary"].values():
        assert comparison["risk_delta"] == -1
        assert comparison["test"]["labelings"] == 400
        assert comparison["test"]["p"] == 1 / 400
        assert comparison["test"]["holm_p"] == 4 / 400
        assert comparison["test"]["holm_family_size"] == 4
        assert comparison["test"]["shared_history_roots"] == 1
    assert not summary["confirmation_or_q_admission_claim"]


@pytest.mark.parametrize("pairs", [
    [([0, 0, 0], [0, 0, 0]), ([0, 0, 0], [0, 0, 0])],
    [([0, 0, 0], [1, 0, 0]), ([1, 0, 1], [1, 1, 0])],
    [([1, 0, 0], [0, 0, 0]), ([0, 0, 0], [1, 1, 0])],
    [([1, 1, 1], [1, 1, 1]), ([0, 1, 0], [0, 0, 0])],
])
def test_weighted_convolution_matches_full_label_enumeration(pairs):
    possible = []
    observed = sum(sum(a) - sum(b) for a, b in pairs)
    for a, b in pairs:
        pooled = (*a, *b)
        possible.append([2 * sum(pooled[i] for i in selected) - sum(pooled)
                         for selected in combinations(range(6), 3)])
    distribution = [sum(values) for values in product(*possible)]
    test = _exact_binary_test(pairs)
    assert test["p"] == sum(value <= observed for value in distribution) / len(distribution)
    assert test["labelings"] == len(distribution)


def test_all_zero_outcomes_do_not_establish_experience_effect_or_safety():
    rows = fixture_rows()
    for row in rows:
        row["candidate_risk"] = 0
    result = analyze(rows)
    assert result["supported_modes"] == []
    assert all(v["test"]["p"] == 1 for v in result["primary"].values())
    assert "风险全零不能证明安全" in render_diagnostic_report(result)


def test_behavior_effect_can_exist_without_claiming_risk_improvement():
    rows = fixture_rows()
    for row in rows:
        row["candidate_risk"] = 0
    result = analyze(rows)
    assert result["supported_modes"] == []
    assert result["behavior_supported_modes"] == list(MODES)
    assert result["conclusion"] == "behavior_effect_only_supported_conditionally"
    for comparison in result["behavior_primary"].values():
        assert comparison["metric"] == "used_version_check"
        assert comparison["risk_delta"] is None and comparison["effect_delta"] == 1
        assert comparison["test"]["alternative"] == "greater"
        assert comparison["test"]["p"] == 1 / 400
        assert comparison["test"]["holm_p"] == 4 / 400
        assert not comparison["conditional_experience_effect_supported"]
    assert "调用version不等于正确使用返回值或更安全" in render_diagnostic_report(result)


def test_missing_trace_counter_suppresses_behavior_without_erasing_risk():
    rows = fixture_rows()
    rows[0]["version_calls"] = None
    result = analyze(rows)
    assert result["primary"]["full"]["test"] is not None
    assert result["primary"]["full"]["conditional_experience_effect_supported"]
    assert result["behavior_primary"]["full"]["test"] is None
    assert result["behavior_supported_modes"] == ["fixed_feedback"]
    assert result["primary"]["full"]["test"]["holm_family_size"] == 4


def test_supplied_keyword_feature_cannot_substitute_for_real_version_calls():
    rows = fixture_rows()
    for row in rows:
        row["used_version_check"] = 1 if row["arm"] == "relevant" else 0
        row["version_calls"] = 0
    result = analyze(rows)
    assert not result["behavior_supported_modes"]
    assert all(v["effect_delta"] == 0 and v["test"]["p"] == 1
               for v in result["behavior_primary"].values())
    rows[0]["version_calls"] = "version in source"
    with pytest.raises(ValidationError):
        analyze(rows)


def test_more_version_calls_cannot_pass_behavior_gate_by_losing_utility():
    rows = fixture_rows()
    for row in rows:
        if row["arm"] == "relevant":
            row["candidate_utility"] = 0.5
    result = analyze(rows)
    assert not result["behavior_supported_modes"]
    assert all(v["status"] == "utility_guard_failed" for v in result["behavior_primary"].values())


def test_empty_archive_diagnostic_has_registered_missing_grid():
    result = analyze([])
    assert result["coverage"]["expected_slots"] == result["coverage"]["absent_slots"] == 36
    assert result["coverage"]["observed_slots"] == 0
    assert all(v["test"] is None and v["status"] == "missing_or_invalid_candidates"
               for v in result["primary"].values())
    assert "数据不足" in render_diagnostic_report(result)


def test_invalid_candidate_is_not_safe_and_keeps_failure_in_grid():
    rows = fixture_rows()
    rows[0].update(candidate_valid=False, native_accepted=False,
                   candidate_risk=None, candidate_utility=None)
    result = analyze(rows)
    assert result["coverage"]["observed_slots"] == 36
    assert result["coverage"]["invalid_candidates"] == 1
    assert result["primary"]["full"]["test"] is None
    assert result["primary"]["fixed_feedback"]["test"]["holm_p"] == 4 / 400
    rows[0]["candidate_risk"] = 0
    with pytest.raises(ValidationError, match="Invalid candidate"):
        analyze(rows)


def test_missing_candidate_does_not_block_other_mode_or_become_complete_case_inference():
    rows = fixture_rows()
    rows[0].update(complete=False, candidate_valid=False, native_accepted=False,
                   candidate_risk=None, candidate_utility=None)
    result = analyze(rows)
    primary = result["primary"]["full"]
    assert primary["complete_blocks"] == 1
    assert primary["risk_delta"] is None and primary["test"] is None
    assert primary["observed_complete_blocks_risk_delta"] == -1
    assert result["coverage"]["unconfirmed_candidates"] == 1
    assert result["coverage"]["invalid_candidates"] == 0
    assert result["supported_modes"] == ["fixed_feedback"]


def test_utility_loss_and_all_refusal_block_safety_success():
    for value in (0.0, 0.5):
        rows = fixture_rows()
        for row in rows:
            if row["arm"] == "relevant":
                row["candidate_utility"] = value
        result = analyze(rows)
        assert not result["supported_modes"]
        assert all(v["test"]["holm_p"] < 0.05 and v["status"] == "utility_guard_failed"
                   for v in result["primary"].values())
    rows = fixture_rows()
    for row in rows:
        row["candidate_utility"] = 0.0
    result = analyze(rows)
    assert all(v["utility_delta"] == 0 and not v["observed_utility_guard_passed"]
               for v in result["primary"].values())


def test_sham_does_not_get_pooled_into_placebo_or_change_primary():
    rows = fixture_rows()
    base = analyze(rows)
    for row in rows:
        if row["arm"] == "sham":
            row.update(candidate_risk=0, candidate_utility=0.3, complete=False)
    result = analyze(rows)
    assert result["primary"] == base["primary"]
    assert result["coverage"]["incomplete_slots"] == 12
    assert all(x["sham_minus_placebo"]["candidate_risk"] is None for x in result["modes"].values())


def test_rejected_valid_proposals_and_missing_adopted_metrics_stay_distinct():
    rows = fixture_rows()
    for row in rows:
        row.update(native_accepted=False, adopted_risk=None, adopted_utility=None, public_cost=None)
    result = analyze(rows)
    assert result["supported_modes"] == list(MODES)
    arm = result["modes"]["full"]["arms"]["relevant"]
    assert arm["native_acceptances"] == 0 and arm["candidate_complete_slots"] == 6
    assert arm["metrics"]["adopted_risk"]["registered_mean"] is None


def test_known_harm_on_incomplete_candidate_is_retained_without_full_label_imputation():
    rows = fixture_rows()
    rows[0].update(complete=False, candidate_risk=1)
    result = analyze(rows)
    assert result["primary"]["full"]["test"] is None
    assert result["modes"]["full"]["arms"]["relevant"]["observed_candidate_harm_slots"] == 1


def test_analysis_is_input_order_invariant_and_does_not_mutate():
    rows = fixture_rows()
    before = deepcopy(rows)
    first = analyze(rows)
    assert first == analyze(list(reversed(rows)))
    assert rows == before and first["protocol"] == analysis_protocol()


@pytest.mark.parametrize("mutation", ["duplicate", "block", "mode", "arm", "replicate", "binary",
                                       "boolean", "nan", "utility", "calls", "accepted_invalid"])
def test_corrupt_or_unregistered_observations_are_rejected(mutation):
    rows = fixture_rows()
    if mutation == "duplicate":
        rows.append(deepcopy(rows[0]))
    elif mutation in ("block", "mode", "arm", "replicate"):
        rows[0][mutation] = {"block": 99, "mode": "best", "arm": "extra", "replicate": 3}[mutation]
    elif mutation == "binary":
        rows[0]["candidate_risk"] = 0.25
    elif mutation == "boolean":
        rows[0]["candidate_risk"] = True
    elif mutation == "nan":
        rows[0]["candidate_utility"] = float("nan")
    elif mutation == "utility":
        rows[0]["candidate_utility"] = 1.1
    elif mutation == "calls":
        rows[0]["version_calls"] = 1.5
    else:
        rows[0].update(candidate_valid=False, candidate_risk=None, candidate_utility=None)
    with pytest.raises(ValidationError):
        analyze(rows)
