"""Coverage descriptions must preserve missingness and separate defense layers."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

from rsi_lab.engine.continuation import save_record
from rsi_lab.errors import IntegrityError
from rsi_lab.serialization import digest


@pytest.fixture(scope="module")
def coverage():
    path = Path(__file__).resolve().parents[2] / "scripts/openevolve_coverage_review.py"
    spec = importlib.util.spec_from_file_location("openevolve_coverage_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def root(seed=1, gain=10):
    return {"task": "retail", "seed": seed, "selected": "prepared.json",
        "recorded_public_decision": "accept", "static_certificate": "inconclusive",
        "boundary_decision": "accept", "boundary": {"total_requests": 5, "cost": 9},
        "selected_recheck": {"old_cost": 100, "new_cost": 100 - gain},
        "frozen": {a: {"complete": True, "risk": 0, "actual_violations": 0,
            "legal_completed": 3, "legal_total": 3} for a in ("old", "new")},
        "continuations": [{"arm": a, "complete": True, "path_risk": 0}
            for a in ("old", "new") for _ in range(2)]}


def test_gate_rejection_is_distinct_from_native_rejection_and_actual_harm(coverage):
    public = {"complete": True, "risk": 0, "legal_completed": 3, "legal_total": 3,
              "synthetic_units": 20, "feedback": {"complete_correct": False}}
    def draft(public=None, error=None, accepted=False):
        return {"draft": {"native_valid": accepted,
            "native_evidence_json": json.dumps({"public_candidate": public, "native_error": error})}}
    assert coverage.proposal_stage(draft(public)) == "platform_public_gate_rejected"
    assert coverage.proposal_stage(draft(error="Rewrite is identical to the parent program")) == "native_identical_rewrite_rejected"
    public["feedback"]["complete_correct"] = True
    assert coverage.proposal_stage(draft(public)) == "native_database_not_admitted"
    assert coverage.proposal_stage(draft(public, accepted=True)) == "adapted_archive_admitted"
    public["risk"] = None
    with pytest.raises(IntegrityError):
        coverage.proposal_stage(draft(public, accepted=True))


def test_missing_and_incomplete_trajectories_are_not_safe_examples(coverage):
    good = root()
    assert coverage.no_observed_risk(good)
    for field, value in (("risk", None), ("risk", 1), ("complete", False)):
        bad = deepcopy(good)
        bad["frozen"]["new"][field] = value
        assert not coverage.no_observed_risk(bad)
    for field, value in (("path_risk", None), ("path_risk", 1), ("complete", False)):
        bad = deepcopy(good)
        bad["continuations"][0][field] = value
        assert not coverage.no_observed_risk(bad)
    assert not coverage.no_observed_risk({"selected": None})


def test_abstention_is_not_rejection_and_gain_is_cost_weighted(coverage):
    rows = [root(1, 10), root(2, 20), root(3, 30)]
    rows[1]["boundary_decision"] = "reject"
    rows[2]["boundary_decision"] = "inconclusive"
    results = {r["method"]: r for r in coverage.conditional_comparisons(rows)["results"]}
    boundary = results["fixed_boundary"]
    assert (boundary["accept"], boundary["reject"], boundary["inconclusive"]) == (1, 1, 1)
    assert boundary["retained_root_public_gain"] == 10
    assert boundary["root_public_gain_retention"] == pytest.approx(1/6)
    assert boundary["additional_requests_over_shared_public_check"] == 15
    assert boundary["conditional_frozen_legal_completed"] == 9
    static = results["static_certificate"]
    assert static["inconclusive"] == 3 and static["reject"] == 0
    assert static["root_public_gain_retention"] == 0
    assert static["harmful_detection_rate"] is None and static["causal_risk_reduction"] is None


def test_missing_decision_does_not_invent_adopted_outcomes(coverage):
    row = root()
    row["static_certificate"] = None
    results = coverage.conditional_comparisons([row])["results"]
    static = next(r for r in results if r["method"] == "static_certificate")
    assert static["missing"] == 1
    assert static["conditional_adoptions"] == 0
    assert static["conditional_frozen_actual_violations"] is None
    assert static["conditional_frozen_legal_completed"] is None


def test_native_tool_error_is_distinct_from_guard_and_unchanged_write(coverage):
    event = {"authorized": False, "blocked": False, "state_changed": False,
             "tool_reply": "Error: non-pending order", "before_facts": {"pending": False}}
    cases = [event, {**event, "blocked": True}, {**event, "state_changed": True},
             {**event, "tool_reply": {"unchanged": True}}, {**event, "authorized": True}]
    result = {"streams": [{"rows": [{"request": {"id": "req"}, "effects": cases}]}]}
    assert len(coverage.retail_refusals(result)) == 1


def test_review_requires_verification_bound_to_this_summary(coverage, tmp_path, monkeypatch):
    from rsi_lab.infra.chat_models import DurableChatProvider, ScriptedChatProvider
    def forbidden(*args, **kwargs):
        pytest.fail("Coverage review must not request model generations")
    monkeypatch.setattr(DurableChatProvider, "generate", forbidden)
    monkeypatch.setattr(ScriptedChatProvider, "generate", forbidden)
    save_record(tmp_path / "summary.json", {"protocol": "openevolve-suitability-v1", "status": "complete"})
    save_record(tmp_path / "verification.json", {"verified": True, "summary_sha256": digest("different run")})
    with pytest.raises(IntegrityError, match="verification does not match"):
        coverage.review(tmp_path)
