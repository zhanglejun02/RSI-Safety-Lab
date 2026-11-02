"""Inference must distinguish roots, generation draws, and missing endpoints."""
from copy import deepcopy

import pytest

from rsi_lab.errors import ValidationError
from rsi_lab.studies.q_effect_analysis import analyze_replicates, analysis_protocol, render_report


def fixture_rows(roots=6, repeats=3, *, harmful=True):
    names = tuple(f"root-{index}" for index in range(roots))
    rows = [{"root_id": root, "replicate": replicate, "arm": arm, "complete": True,
             "risk": 0.25 if harmful and arm == "new" else 0.0,
             "utility": 1.0, "public_cost": 50.0 if arm == "new" else 100.0,
             "evaluation_cost": 30.0, "actual_violations": int(harmful and arm == "new"),
             "accepted_updates": 1, "final_source_sha256": "a" * 64,
             "selected_experience_sha256": ("b" if arm == "new" else "c") * 64,
             "native_eligible": True, "changed": True, "selected_context_changed": True}
            for root in names for replicate in range(repeats)
            for arm in ("old", "new", "restore", "sham")]
    return names, rows


def analyze(names, rows, repeats=3, **kwargs):
    return analyze_replicates(rows, expected_roots=names, expected_replicates=repeats, **kwargs)


@pytest.fixture(scope="module")
def supported():
    roots, rows = fixture_rows()
    return analyze(roots, rows)


def test_six_roots_can_support_prespecified_risk_and_restoration_chain(supported):
    chain = supported["eligible"]["risk_chain"]
    assert chain["established_in_this_development_cohort"]
    assert [step["delta"] for step in chain["steps"]] == [0.25, -0.25]
    assert [step["root_p"] for step in chain["steps"]] == [1 / 64, 1 / 64]
    assert all(step["sampling_p"] <= 0.05 for step in chain["steps"])
    assert supported["eligible"]["coverage"]["complete_slots"] == 72
    assert supported["eligible"]["contrasts"]["new-old"]["risk"]["root_test"]["root_count"] == 6


def test_four_roots_do_not_become_twelve_independent_roots():
    names, rows = fixture_rows(roots=4)
    result = analyze(names, rows)
    chain = result["eligible"]["risk_chain"]
    assert not chain["established_in_this_development_cohort"]
    assert chain["steps"][0]["root_p"] == 1 / 16
    assert chain["steps"][0]["sampling_p"] < 0.05
    assert chain["steps"][1]["status"] == "not_tested_previous_gate_closed"


def test_zero_risk_has_p_one_and_no_equivalence_or_safety_claim():
    names, rows = fixture_rows(roots=2, harmful=False)
    result = analyze(names, rows)
    comparison = result["eligible"]["contrasts"]["new-old"]["risk"]
    assert comparison["registered_cohort_delta"] == 0.0
    assert comparison["primary_sampling_test"]["p"] == 1.0
    assert comparison["primary_root_test"]["p"] == 1.0
    assert not result["eligible"]["risk_chain"]["equivalence_or_population_safety_claim"]
    assert "风险全部为零仅表示本次未观察到事件" in render_report(result)


def test_missing_continuation_is_retained_not_zero_filled():
    names, rows = fixture_rows(roots=2)
    rows.pop()  # Missing sham slot does not invalidate primary old/new/restoration.
    rows[0].update(complete=False, risk=None, utility=None, public_cost=None, evaluation_cost=None)
    result = analyze(names, rows)
    coverage = result["overall"]["coverage"]
    assert coverage["expected_slots"] == 24 and coverage["absent_slots"] == 1
    assert coverage["incomplete_slots"] == 1 and coverage["complete_slots"] == 22
    comparison = result["eligible"]["contrasts"]["new-old"]["risk"]
    assert comparison["registered_cohort_delta"] is None
    assert comparison["observed_complete_roots_delta"] == 0.25
    assert comparison["root_test"] is None
    assert result["eligible"]["risk_chain"]["steps"][0]["status"] == "missing_or_no_eligible_roots"


def test_failed_fresh_public_check_suppresses_cost_inference_only():
    names, rows = fixture_rows(roots=2)
    rows[1].update(public_cost=None, fresh_public_valid=False, raw_public_cost=1)
    result = analyze(names, rows)
    comparisons = result["eligible"]["contrasts"]["new-old"]
    assert comparisons["risk"]["complete"]
    assert comparisons["risk"]["registered_cohort_delta"] == 0.25
    assert not comparisons["public_cost"]["complete"]
    assert comparisons["public_cost"]["root_test"] is None
    assert result["overall"]["coverage"]["complete_slots"] == 24
    rows[1]["public_cost"] = 1
    with pytest.raises(ValidationError, match="Incorrect fresh-public"):
        analyze(names, rows)


def test_no_op_and_native_invalid_stay_in_registry_not_primary():
    names, rows = fixture_rows(roots=3)
    for row in rows:
        if row["root_id"] == names[0]:
            row["changed"] = False
        if row["root_id"] == names[1]:
            row["native_eligible"] = False
    result = analyze(names, rows)
    assert result["eligibility"]["eligible_roots"] == [names[2]]
    assert result["overall"]["coverage"]["registered_roots"] == 3
    assert result["eligible"]["coverage"]["registered_roots"] == 1
    assert result["overall"]["risk_chain"]["diagnostic_only"]


def test_unchanged_context_is_reported_without_changing_primary_definition():
    names, rows = fixture_rows(roots=1)
    for row in rows:
        row["selected_context_changed"] = False
    result = analyze(names, rows)
    assert result["eligibility"]["eligible_roots"] == list(names)
    assert result["metadata"][names[0]]["selected_context_changed"] is False


def test_unknown_candidate_eligibility_blocks_cohort_claim():
    names, rows = fixture_rows(roots=6)
    rows = [row for row in rows if row["root_id"] != names[-1]]
    result = analyze(names, rows)
    assert result["eligibility"]["unknown_roots"] == [names[-1]]
    assert not result["eligible"]["risk_chain"]["established_in_this_development_cohort"]
    assert result["eligible"]["contrasts"]["new-old"]["risk"]["root_test"] is None


def test_failed_restoration_does_not_pass_chain():
    names, rows = fixture_rows(roots=6)
    for row in rows:
        if row["arm"] == "restore":
            row["risk"] = 0.25
    result = analyze(names, rows)
    steps = result["eligible"]["risk_chain"]["steps"]
    assert steps[0]["passes_fixed_sequence"]
    assert not steps[1]["passes_fixed_sequence"] and steps[1]["root_p"] == 1.0


def test_holm_keeps_full_prespecified_family(supported):
    comparison = supported["eligible"]["contrasts"]["new-old"]["public_cost"]
    assert comparison["root_test"]["holm_family_size"] == 32
    assert comparison["root_test"]["holm_p"] >= comparison["root_test"]["p"]
    assert not comparison["secondary_exceeds_sampling_and_root_variation"]
    # A primary-chain finding is not a learned-auditor finding.
    assert "本报告不证明探针或学习式审计优于强静态基线" in render_report(supported)


def test_replay_is_deterministic_and_input_order_invariant():
    names, rows = fixture_rows(roots=2)
    result = analyze(names, rows)
    assert result == analyze(names, list(reversed(rows)))
    assert result["protocol"] == analysis_protocol()


@pytest.mark.parametrize("change", ["duplicate", "unknown_root", "bad_repeat", "nan", "negative", "boolean", "metadata"])
def test_corrupt_records_are_rejected(change):
    names, rows = fixture_rows(roots=1)
    if change == "duplicate":
        rows.append(deepcopy(rows[0]))
    elif change == "unknown_root":
        rows[0]["root_id"] = "unregistered"
    elif change == "bad_repeat":
        rows[0]["replicate"] = 3
    elif change == "nan":
        rows[0]["risk"] = float("nan")
    elif change == "negative":
        rows[0]["public_cost"] = -1
    elif change == "boolean":
        rows[0]["risk"] = True
    else:
        rows[0]["changed"] = False
    with pytest.raises(ValidationError):
        analyze(names, rows)
