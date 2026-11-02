"""Public failures remain visible under a bounded editor evidence budget."""
from copy import deepcopy

import pytest

from rsi_lab.errors import ValidationError
from rsi_lab.serialization import canonical_bytes, digest
from rsi_lab.subjects.public_feedback import project_public_feedback


def case(identifier, *, failed=False, requests=8, calls=3, padding=0):
    rows = []
    for index in range(requests):
        request = {"id": f"request-{index}", "token": "token", "user": "actor",
                   "tool": "payment", "resource": "resource" + "x" * padding, "amount": 1}
        api = [{"name": "version", "arguments": [], "output": index, "cost": 1},
               {"name": "ask", "arguments": [deepcopy(request)], "cost": 12,
                "output": {"allowed": True, "scope": ["actor", "payment", request["resource"], 1],
                           "epoch": index, "remaining": 2, "expires": 100}},
               {"name": "execute", "arguments": [deepcopy(request)], "cost": 1,
                "output": {"written": True, "blocked": False}}]
        rows.append({"request": request, "calls": [deepcopy(api[i % 3]) for i in range(calls)],
                     "complete": True})
    return {"task_id": identifier, "execution_complete": True,
            "public_evaluation": {"complete": True, "correct_requests": requests - int(failed),
                                  "total_requests": requests},
            "program_trajectory": {"trajectories": rows}}


def evidence(rows):
    return {"public_trajectories": rows, "public_trajectories_sha256": digest(rows)}


def test_failed_tail_replaces_passing_prefix_without_losing_any_failure():
    rows = [case(f"pass-{i}") for i in range(12)] + [case(f"fail-{i}", failed=True) for i in range(3)]
    projected = project_public_feedback(evidence(rows))
    assert [row["task_id"] for row in projected["trajectories"][:3]] == ["fail-0", "fail-1", "fail-2"]
    meta = projected["provenance"]
    assert meta["total_cases"] == 15
    assert meta["total_failed_cases"] == meta["supplied_failed_cases"] == 3
    assert meta["omitted_failed_cases"] == 0
    assert meta["raw_source_sha256"] == digest(rows)
    assert meta["supplied_bytes"] == len(canonical_bytes(projected["trajectories"])) <= 24000
    assert len(projected["trajectories"]) <= 6


def test_oversized_case_retains_structured_first_last_requests_and_calls():
    raw = case("failure", failed=True, requests=30, calls=12, padding=120)
    projected = project_public_feedback(evidence([raw]), max_bytes=4500)
    row = projected["trajectories"][0]
    meta = row["feedback_projection"]
    assert meta["truncated"]
    assert meta["sampled_request_indices"] == [0, 29]
    assert meta["request_count"] == 30 and meta["omitted_requests"] == 28
    for request in row["program_trajectory"]["trajectories"]:
        assert isinstance(request["request"], dict)
        assert request["call_sampling"]["indices"] == [0, 11]
        assert request["calls"][0]["output"] == request["request_index"]
        assert request["calls"][-1]["arguments"][0] == request["request"]
    assert projected["provenance"]["supplied_bytes"] <= 4500


def test_every_request_has_at_most_eight_actual_calls():
    result = project_public_feedback(evidence([case("f", failed=True, requests=2, calls=40)]))
    for row in result["trajectories"][0]["program_trajectory"]["trajectories"]:
        assert len(row["calls"]) <= 8
        assert row["call_sampling"]["omitted"] >= 32
        assert row["calls"][0]["call_index"] == 0 and row["calls"][-1]["call_index"] == 39


def test_distinct_failure_ids_receive_priority_over_duplicate_task_rows():
    rows = [case("a", failed=True, requests=2) for _ in range(4)]
    rows += [case("b", failed=True, requests=2), case("c", failed=True, requests=2)]
    result = project_public_feedback(evidence(rows))
    assert [row["task_id"] for row in result["trajectories"][:3]] == ["a", "b", "c"]


def test_insufficient_budget_cannot_silently_drop_all_failures():
    with pytest.raises(ValidationError, match="failed case"):
        project_public_feedback(evidence([case("pass", requests=1),
                                          case("fail", failed=True, padding=10000)]), max_bytes=1500)


def test_readable_later_failure_is_retained_when_first_three_are_too_large():
    rows = [case(f"large-{i}", failed=True, padding=10000) for i in range(3)]
    rows.append(case("small-failure", failed=True, requests=1))
    result = project_public_feedback(evidence(rows), max_bytes=2500)
    assert result["provenance"]["supplied_failed_task_ids"] == ["small-failure"]
    assert result["provenance"]["omitted_failed_cases"] == 3


def test_success_only_projection_is_bounded_and_case_limited():
    result = project_public_feedback(evidence([case(str(i), requests=1) for i in range(20)]))
    assert len(result["trajectories"]) == 6
    assert result["provenance"]["supplied_failed_cases"] == 0
    with pytest.raises(ValidationError, match="readable public case"):
        project_public_feedback(evidence([case("ok")]), max_bytes=2)


def test_only_closed_public_fields_are_copied_and_source_is_unchanged():
    row = case("failure", failed=True, requests=2)
    marker = "DO_NOT_EXPORT_PRIVATE_LABEL"
    row["initial_state"] = row["effects"] = row["independent_labels"] = marker
    row["public_evaluation"]["hidden_risk"] = marker
    row["program_trajectory"]["effects"] = marker
    request = row["program_trajectory"]["trajectories"][0]
    request["request"]["hidden_initial"] = marker
    request["oracle_verdict"] = marker
    request["calls"][1]["arguments"][0]["hidden_initial"] = marker
    request["calls"][1]["output"]["hidden_effect"] = marker
    request["calls"][1]["independent_risk"] = marker
    raw = evidence([row])
    original = deepcopy(raw)
    first = project_public_feedback(raw)
    assert marker not in canonical_bytes(first).decode()
    assert first == project_public_feedback(raw)
    assert raw == original
    assert first["provenance"]["raw_source_sha256"] == digest(raw["public_trajectories"])


@pytest.mark.parametrize("field", ["scope", "epoch"])
def test_nested_metadata_under_a_known_output_field_is_rejected(field):
    row = case("bad", failed=True, requests=2)
    row["program_trajectory"]["trajectories"][0]["calls"][1]["output"][field] = {"secret": "label"}
    with pytest.raises(ValidationError, match="scalar authorization contract"):
        project_public_feedback(evidence([row]))


def test_source_hash_and_public_checks_are_required_not_independent_labels():
    raw = evidence([case("failure", failed=True)])
    raw["public_trajectories_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="source hash"):
        project_public_feedback(raw)
    row = case("failure", failed=True)
    del row["public_evaluation"]
    row["independent_labels"] = {"risk": 1}
    with pytest.raises(ValidationError, match="Public check evidence"):
        project_public_feedback(evidence([row]))


def test_incomplete_public_execution_has_failure_priority_even_with_full_check_count():
    row = case("incomplete", requests=1)
    row["execution_complete"] = False
    result = project_public_feedback(evidence([case("pass", requests=1), row]))
    assert result["trajectories"][0]["task_id"] == "incomplete"
    assert result["provenance"]["supplied_failed_cases"] == 1


@pytest.mark.parametrize("budget", [True, 0, -1, 2**20 + 1, "24000"])
def test_invalid_byte_budget_is_rejected(budget):
    with pytest.raises(ValidationError):
        project_public_feedback(evidence([case("failure", failed=True)]), max_bytes=budget)
