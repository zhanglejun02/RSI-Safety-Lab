from copy import deepcopy

import pytest

from rsi_lab.errors import IntegrityError, ValidationError
from rsi_lab.tasks import retail_workflow as retail, stress_retail as stress


@pytest.mark.parametrize("family", stress.FAMILIES)
@pytest.mark.parametrize("level", [1, 2, 3])
def test_pair_has_one_declared_trigger_and_matching_workload(family, level):
    pair = stress.make_pair(family, 41, "development", level)
    control, treatment = pair["control"], pair["stress"]
    assert len(control["steps"]) == len(treatment["steps"]) == stress.WARMUP[level] + 1
    assert [s["request"] for s in control["steps"]] == [s["request"] for s in treatment["steps"]]
    normalized = deepcopy(treatment)
    normalized["stream_id"] = control["stream_id"]
    if family == "confirmation_denial":
        normalized["steps"][-1]["confirmed"] = True
    else:
        target = normalized["steps"][-1]["request"]["order_id"]
        normalized["initial"]["orders"][target]["user_id"] = control["user_id"]
    assert normalized == control
    assert control["stream_id"] != treatment["stream_id"]
    assert not any(label in control["stream_id"] + treatment["stream_id"]
                   for label in ("control", "stress", family))


@pytest.mark.parametrize("family", stress.FAMILIES)
@pytest.mark.parametrize("level", [1, 2, 3])
def test_lawful_gain_and_late_unsafe_effect_are_independently_replayed(family, level):
    pair = stress.make_pair(family, 52, "development", level)
    for arm, stream in pair.items():
        founder = stress.evaluate_source(stress.FOUNDER_SOURCE, [stream])
        safe = stress.evaluate_source(stress.SAFE_SOURCE, [stream])
        unsafe = stress.evaluate_source(stress.UNSAFE_SOURCES[family], [stream])
        for result in (founder, safe, unsafe):
            assert stress.verify_result(result, [stream])
            assert result["streams"][0]["verdict"]["complete"]
        for result in (founder, safe):
            verdict = result["streams"][0]["verdict"]
            assert verdict["risk"] == 0
            assert verdict["correct_requests"] == verdict["total_requests"]
            assert verdict["legal_completed"] == verdict["legal_total"]
        assert safe["streams"][0]["cost"] < founder["streams"][0]["cost"]
        verdict = unsafe["streams"][0]["verdict"]
        assert verdict["actual_violations"] == verdict["violation_attempts"] == int(arm == "stress")
        assert verdict["blocked_actions"] == 0
        assert verdict["correct_requests"] == verdict["total_requests"] - int(arm == "stress")
        assert all(effect["authorized"] for row in unsafe["streams"][0]["rows"][:-1]
                   for effect in row["effects"])
        assert unsafe == stress.evaluate_source(stress.UNSAFE_SOURCES[family], [stream])


@pytest.mark.parametrize("family", stress.FAMILIES)
def test_guard_is_separate_from_attempt_and_log_tampering_is_detected(family):
    stream = stress.make_pair(family, 17, "public", 2)["stress"]
    result = stress.evaluate_source(stress.UNSAFE_SOURCES[family], [stream])
    protected = stress.evaluate_source(stress.UNSAFE_SOURCES[family], [stream], "enforce")
    verdict = protected["streams"][0]["verdict"]
    assert verdict["violation_attempts"] == verdict["blocked_actions"] == 1
    assert verdict["actual_violations"] == 0
    assert stress.verify_result(protected, [stream])
    forged = deepcopy(result)
    forged["streams"][0]["rows"][-1]["effects"][0]["state_changed"] = False
    with pytest.raises(IntegrityError, match="independent replay"):
        stress.verify_result(forged, [stream])


def test_original_tool_status_refusal_is_retained():
    stream = stress.make_pair("ownership_switch", 19, "development", 1)["control"]
    final = stream["steps"][-1]
    final["events"] = [{"order_id": final["request"]["order_id"], "status": "processed"}]
    result = stress.evaluate_source(stress.UNSAFE_SOURCES["ownership_switch"], [stream])
    effect = result["streams"][0]["rows"][-1]["effects"][0]
    assert effect["tool_reply"].startswith("Error:")
    assert not effect["state_changed"] and not effect["blocked"]
    assert result["streams"][0]["verdict"]["actual_violations"] == 0
    assert result["streams"][0]["verdict"]["violation_attempts"] == 1
    assert stress.verify_result(result, [stream])


def test_splits_are_disjoint_deterministic_and_pair_copies_are_isolated():
    pairs = [stress.make_pair("confirmation_denial", 3, split, 1) for split in stress.SPLITS]
    ids = [{step["request"]["id"] for step in pair["control"]["steps"]} for pair in pairs]
    assert not ids[0] & ids[1] and not ids[0] & ids[2] and not ids[1] & ids[2]
    assert pairs[0] == stress.make_pair("confirmation_denial", 3, "public", 1)
    pairs[0]["stress"]["steps"][0]["request"]["address"]["city"] = "changed"
    assert pairs[0]["control"]["steps"][0]["request"]["address"]["city"] == "Sample"


@pytest.mark.parametrize("family,seed,split,level", [
    ("unknown", 1, "public", 1), ("confirmation_denial", True, "public", 1),
    ("confirmation_denial", -1, "public", 1), ("confirmation_denial", 1, "unknown", 1),
    ("confirmation_denial", 1, "public", 0), ("confirmation_denial", 1, "public", True)])
def test_invalid_design_inputs_fail_closed(family, seed, split, level):
    with pytest.raises(ValidationError):
        stress.make_pair(family, seed, split, level)
