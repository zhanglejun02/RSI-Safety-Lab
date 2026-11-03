from copy import deepcopy
import pytest
from rsi_lab.tasks import stress_authorization as cases
from rsi_lab.errors import IntegrityError


@pytest.mark.parametrize("family", cases.FAMILIES)
@pytest.mark.parametrize("level", (1, 2, 3))
def test_safe_solution_and_paired_shortcut_failure(family, level):
    pair = cases.make_pair(family, 7, "development", level)
    assert [s["request"] for s in pair["control"]["steps"]] == [s["request"] for s in pair["stress"]["steps"]]
    for arm, stream in pair.items():
        safe = cases.evaluate_source(cases.SAFE_SOURCE, [stream])
        assert safe["complete"] and safe["risk"] == 0
        assert safe["correct_requests"] == safe["total_requests"]
        assert cases.verify_result(safe, [stream])
        shortcut = cases.evaluate_source(cases.UNSAFE_SOURCES[family], [stream])
        assert shortcut["complete"] and shortcut["risk"] == int(arm == "stress")
        if arm == "control":
            assert shortcut["correct_requests"] == shortcut["total_requests"]
            founder = cases.evaluate_source(cases.FOUNDER_SOURCE, [stream])
            assert safe["cost"] < founder["cost"]
        changed = deepcopy(safe)
        changed["violations"] += 1
        with pytest.raises(IntegrityError):
            cases.verify_result(changed, [stream])


def test_determinism_and_parameter_split():
    a = cases.make_pair(cases.FAMILIES[0], 7, "public", 1)
    assert a == cases.make_pair(cases.FAMILIES[0], 7, "public", 1)
    assert a["control"]["stream_id"] != cases.make_pair(cases.FAMILIES[0], 7, "holdout", 1)["control"]["stream_id"]
