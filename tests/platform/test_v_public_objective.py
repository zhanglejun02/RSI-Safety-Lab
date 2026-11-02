"""Feasibility search must preserve public classification and real costs."""
import importlib.util
from itertools import combinations
from pathlib import Path

import pytest

from rsi_lab.engine.continuation import read_record
from rsi_lab.infra.chat_models import DurableChatProvider, ScriptedChatProvider
from rsi_lab.tasks.cost_authorization import FOUNDER_SOURCE, SAFE_REFERENCE_SOURCE
from rsi_lab.serialization import digest


@pytest.fixture(scope="module")
def study():
    path = Path(__file__).resolve().parents[2] / "scripts/v_public_objective_preflight.py"
    spec = importlib.util.spec_from_file_location("public_v_preflight_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def row(name, passes, costs, group="root"):
    return {"id": name, "group": group, "source_sha256": digest(name), "kind": "historical_candidate",
            "native_eligible": False,
            "tests": [{"pass": p, "cost": c, "complete": True} for p, c in zip(passes, costs)]}


def test_exact_cost_search_matches_bruteforce(study):
    rows = [row("safe", [1, 1, 1, 1, 1, 1], [5, 4, 6, 4, 10, 3]),
            row("cross", [1, 0, 1, 0, 1, 1], [4, 3, 8, 5, 12, 7]),
            row("revoke", [1, 1, 0, 0, 1, 1], [4, 8, 6, 7, 9, 7], "another-root")]
    expected = []
    costs = study._costs(rows)
    for size in range(1, 5):
        for plan in combinations(range(6), size):
            if 0 in plan and study.metrics(plan, rows)["agreement"] == 1:
                expected.append((sum(costs[t] for t in plan), size, plan))
    assert study.minimize(rows, rows, max_tests=4) == list(min(expected)[2])


def test_known_public_failure_cannot_be_silently_removed(study):
    training = [row("safe", [1, 1, 1], [5, 1, 1]), row("cross", [1, 0, 1], [5, 1, 1])]
    known = row("known_revoke", [1, 1, 0], [5, 1, 1])
    assert study.minimize(training, training, max_tests=3) == [0, 1]
    assert study.minimize(training, training + [known], max_tests=3) == [0, 1, 2]
    assert study.minimize(training, training + [known], max_tests=2) is None


def test_classification_cannot_hide_behind_native_cost_rejections(study):
    rows = [row("good", [1, 1, 1], [5, 2, 3]), row("bad", [1, 0, 1], [5, 2, 3])]
    assert all(not r["native_eligible"] for r in rows)
    assert study.metrics([0], rows)["false_accept"] == 1
    assert study.minimize(rows, rows, max_tests=2) == [0, 1]
    for r in rows:
        r["native_eligible"] = True
    assert study.minimize(rows, rows, max_tests=2) == [0, 1]


def test_duplicate_programs_do_not_weight_history_more(study):
    first = row("one", [1, 1], [3, 1])
    second = row("two", [1, 0], [9, 5], "second")
    assert study._costs(study.deduplicate([first] * 12 + [second])) == study._costs([first, second])


def test_missing_public_result_prevents_selection(study):
    rows = [row("one", [1, 0], [3, 4])]
    rows[0]["tests"][1]["complete"] = False
    assert study.minimize(rows, rows) is None
    assert study.metrics([0], rows) is None
    assert study.analyze(rows)["decision"] == "stop_missing_public_evidence"


def test_whole_preflight_is_public_only_and_zero_api(study, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Public feasibility check must not call a model")
    monkeypatch.setattr(DurableChatProvider, "generate", forbidden)
    monkeypatch.setattr(ScriptedChatProvider, "generate", forbidden)
    cases = [{"id": name, "group": name, "source": source, "source_sha256": digest(source),
              "native_eligible": True, "kind": "historical_candidate"}
             for name, source in (("a", FOUNDER_SOURCE), ("b", SAFE_REFERENCE_SOURCE))]
    monkeypatch.setattr(study, "load_public_inputs", lambda *a: {"candidates": cases})
    summary = study.run("unused", "unused", tmp_path / "run")
    assert summary["model_calls"] == 0 and not summary["independent_safety_labels_used"]
    assert summary["reference_collection"]["executed_requests"] == 180
    for path in (tmp_path / "run/public").glob("*.json"):
        result = read_record(path)
        assert result["splits"] == ["public"]
    assert "independent" not in read_record(tmp_path / "run/manifest.json")["public_pools"]
