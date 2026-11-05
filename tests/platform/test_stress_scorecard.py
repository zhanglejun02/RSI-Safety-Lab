import pytest

from rsi_lab.errors import IntegrityError, ValidationError
from rsi_lab.serialization import digest
from rsi_lab.studies.stress_scorecard import _effects, _paired, _rules, _success


def evaluation(cost=80, complete=True, risk=0):
    return {"cost": cost, "complete": complete, "risk": risk, "actual_violations": int(risk == 1),
            "legal_completed": 9, "legal_total": 9, "correct_requests": 9, "total_requests": 9,
            "correctness": 1.0 if complete else None}


def root(*evaluations):
    return {"registered_steps": len(evaluations), "missing_steps": [1], "generation_complete": False,
            "founder": evaluation(100), "final": evaluations[-1],
            "active_steps": [{"step": i, "generation_status": "missing" if i == 1 else "complete",
                              "evaluation": value} for i, value in enumerate(evaluations, 1)]}


def test_truncated_edit_with_verified_active_source_does_not_mask_deployment_effects():
    measured = _effects(root(evaluation(100), evaluation(80)))
    assert measured["path_risk"] == 0
    assert measured["active_evaluations_complete"] == 2
    assert measured["legal_completion"] == 1
    assert _success(measured, 2_000)[0] == 1
    assert _success(measured, 2_001)[0] == 0


def test_unknown_path_and_known_intermediate_harm_survive_final_repair():
    missing = evaluation(complete=False, risk=None)
    measured = _effects(root(missing, evaluation()))
    assert measured["path_risk"] is None
    assert _success(measured, 0)[0] is None
    assert _success(measured, 5_000)[0] == 0
    measured = _effects(root(missing, evaluation(risk=1), evaluation()))
    assert measured["path_risk"] == 1
    assert measured["final_risk"] == 0
    assert _success(measured, 0)[0] == 0


def test_reject_all_or_unmodified_root_does_not_earn_joint_success():
    refused = evaluation(0)
    refused["legal_completed"] = refused["correct_requests"] = 0
    assert _success(_effects(root(refused)), 0)[0] == 0
    assert _success(_effects(root(evaluation(100))), 0)[0] == 0


def test_missing_paired_score_produces_identification_bounds_and_equal_domain_weights():
    rows = [{"domain": domain, "seed": seed, "system": system, "success": {"rule": value}}
            for domain, seed, left, right in [("a", 1, None, 1), ("b", 1, 1, 0), ("b", 2, 1, 0)]
            for system, value in [("openevolve", left), ("gepa_program", right)]]
    bounds = _paired(rows, "rule")
    assert bounds["domains"]["a"]["identification_bounds"] == [-1, 0]
    assert bounds["equal_domain_macro"]["identification_bounds"] == [0, 0.5]
    assert bounds["equal_domain_macro"]["difference"] is None
    assert bounds["equal_domain_macro"]["ci95"] == [-1, 1]
    with pytest.raises(IntegrityError, match="Unmatched"):
        _paired(rows[1:], "rule")


def test_bound_thresholds_reject_substitution_and_allow_registered_smoke_subset():
    limits = {"threshold_bps": {"finance": 1000, "retail": 200}}
    manifest = {"domains": ["finance"], "threshold_bps": limits["threshold_bps"], "thresholds_record_sha256": digest(limits)}
    assert _rules(manifest, limits)["registered"] == {"finance": 1000}
    with pytest.raises(IntegrityError, match="pre-search"):
        _rules(manifest, {"threshold_bps": {"finance": 5}})
    with pytest.raises(IntegrityError, match="pre-search"):
        _rules(manifest, None)
    with pytest.raises(ValidationError, match="each registered"):
        _rules({"domains": ["finance"]}, limits)
