"""Failure-first projection of public authorization request/tool evidence.

Only public task requests, public check counts and observed API calls are
projected. Protected initial state, effects, per-request oracle labels and
arbitrary metadata never become editor input. Oversized cases are sampled as
structured requests/calls; JSON documents are never sliced into opaque text.
"""
from copy import deepcopy

from rsi_lab.errors import ValidationError
from rsi_lab.serialization import canonical_bytes, digest

REQUEST_FIELDS = ("id", "token", "user", "tool", "resource", "amount")
OUTPUT_FIELDS = ("allowed", "scope", "epoch", "remaining", "expires", "blocked", "written")
API_NAMES = ("ask", "version", "now", "execute")
MAX_CASES = 6


def _sequence(value, name):
    if type(value) not in (tuple, list):
        raise ValidationError(f"Expected public {name} sequence")
    return value


def _request(value):
    if type(value) is not dict:
        raise ValidationError("Public request must be structured data")
    result = {key: deepcopy(value[key]) for key in REQUEST_FIELDS if key in value}
    if any(type(item) is not (int if key == "amount" else str) for key, item in result.items()):
        raise ValidationError("Public authorization request fields must retain scalar contract types")
    return result


def _call(value):
    if type(value) is not dict or value.get("name") not in API_NAMES:
        raise ValidationError("Expected an observed authorization API call")
    name = value["name"]
    arguments = value.get("arguments", [])
    if type(arguments) not in (list, tuple):
        raise ValidationError("Public API arguments must retain their observed argument list")
    args = [_request(arg) if type(arg) is dict else deepcopy(arg) for arg in arguments]
    if any(type(arg) not in (dict, str, int, bool, float, type(None)) for arg in args):
        raise ValidationError("Public authorization arguments cannot carry arbitrary nested metadata")
    output = value.get("output", value.get("result"))
    if type(output) is dict:
        output = {key: deepcopy(output[key]) for key in OUTPUT_FIELDS if key in output}
        for key, item in output.items():
            if key == "scope":
                valid = (type(item) is list and len(item) == 4
                         and all(type(part) is str for part in item[:3]) and type(item[3]) is int)
            else:
                valid = type(item) is (bool if key in ("allowed", "blocked", "written") else int)
            if not valid:
                raise ValidationError("Public API output does not match the scalar authorization contract")
    else:
        if type(output) not in (int, type(None)):
            raise ValidationError("Public clock/version output must be an integer or missing")
        output = deepcopy(output)
    result = {"name": name, "arguments": args, "output": output}
    if "cost" in value:
        if type(value["cost"]) not in (int, float) or value["cost"] < 0:
            raise ValidationError("Public call cost must be numeric")
        result["cost"] = value["cost"]
    canonical_bytes(result)
    return result


def _closed_case(row, index):
    if type(row) is not dict or type(row.get("task_id")) is not str or not row["task_id"]:
        raise ValidationError("Public trajectory requires its actual task identifier")
    check = row.get("public_evaluation")
    if type(check) is not dict or type(check.get("complete")) is not bool:
        raise ValidationError("Public check evidence is required; independent labels cannot substitute")
    for key in ("correct_requests", "total_requests"):
        if type(check.get(key)) is not int or check[key] < 0:
            raise ValidationError("Public checks require observed nonnegative request counts")
    if check["correct_requests"] > check["total_requests"]:
        raise ValidationError("Public correctness exceeds the executed task denominator")
    check = {key: check[key] for key in ("complete", "correct_requests", "total_requests")}
    complete = row.get("execution_complete", check["complete"])
    if type(complete) is not bool:
        raise ValidationError("Public execution completeness must be explicit")
    public_program = row.get("program_trajectory", {})
    if type(public_program) is not dict:
        raise ValidationError("Public program trajectory must be structured")
    requests = public_program.get("trajectories", row.get("trajectories", ()))
    if not requests:
        # This path also supports a public single-request API trace.
        request = row.get("request", {})
        if type(request) is dict and "requests" in request:
            requests = tuple({"request": item, "calls": [], "complete": complete}
                             for item in _sequence(request["requests"], "requested stream"))
        else:
            requests = ({"request": request, "calls": row.get("tool_calls", ()), "complete": complete},)
    cleaned = []
    for request_index, request in enumerate(_sequence(requests, "request trajectory")):
        if type(request) is not dict:
            raise ValidationError("Public request trajectory must be structured")
        calls = [_call(c) for c in _sequence(request.get("calls", request.get("tool_calls", ())), "API calls")]
        if type(request.get("complete", complete)) is not bool:
            raise ValidationError("Public request completeness must be boolean")
        cleaned.append({"request_index": request_index, "request": _request(request.get("request", {})),
            "calls": [{"call_index": i, **call} for i, call in enumerate(calls)],
            "complete": request.get("complete", complete)})
    value = {"task_id": row["task_id"], "execution_complete": complete,
             "public_evaluation": check, "program_trajectory": {"trajectories": cleaned}}
    return {"index": index, "value": value,
            "failed": not complete or not check["complete"] or check["correct_requests"] != check["total_requests"],
            "public_projection_sha256": digest(value)}


def _sample_indices(size, count):
    if count >= size:
        return list(range(size))
    if count == 1:
        return [size - 1]
    return sorted({i * (size - 1) // (count - 1) for i in range(count)})


def _case_variant(case, request_limit=None, call_limit=None):
    value = deepcopy(case["value"])
    all_requests = value["program_trajectory"]["trajectories"]
    selected = _sample_indices(len(all_requests), request_limit or len(all_requests))
    rows = [all_requests[i] for i in selected]
    call_counts = []
    for row in rows:
        calls = row["calls"]
        chosen = _sample_indices(len(calls), call_limit or len(calls)) if calls else []
        row["calls"] = [calls[i] for i in chosen]
        row["call_sampling"] = {"total": len(calls), "supplied": len(chosen),
                                "indices": chosen, "omitted": len(calls) - len(chosen)}
        call_counts.append(len(calls) - len(chosen))
    value["program_trajectory"]["trajectories"] = rows
    value["feedback_projection"] = {"source_index": case["index"],
        "public_projection_sha256": case["public_projection_sha256"],
        "request_count": len(all_requests), "supplied_request_count": len(rows),
        "sampled_request_indices": selected, "omitted_requests": len(all_requests) - len(rows),
        "omitted_calls_in_sampled_requests": sum(call_counts),
        "truncated": len(rows) != len(all_requests) or any(call_counts),
        "sampling": "first_last_then_evenly_spaced_requests_and_calls;no_oracle_selection"}
    return value


def _variants(case):
    # Grow a readable first/last sample to complete public evidence. Actual
    # argument/result values remain intact; if even this minimum does not fit,
    # do not silently replace it with a hash or a broken JSON string.
    variants = [_case_variant(case, requests, calls) for requests, calls in
                ((2, 2), (2, 8), (4, 8), (8, 8), (None, 8))]
    unique = {digest(value): value for value in variants}
    return sorted(unique.values(), key=lambda value: len(canonical_bytes(value)))


def project_public_feedback(evidence, max_bytes=24000):
    """Return bounded trajectories plus provenance, without modifying evidence.

    ``max_bytes`` bounds canonical UTF-8 bytes of the trajectories list, excluding
    the compact provenance object. Up to three distinct failed task IDs receive
    reserved minimum evidence before detail expansion and passing cases. Omitted
    cases and request/call truncation are explicit. Arguments/results are never
    fabricated or scalar-truncated. A failed minimum that cannot fit is an error.
    """
    if type(evidence) is not dict or type(max_bytes) is not int or not 1 <= max_bytes <= 2**20:
        raise ValidationError("Expected public evaluator evidence and a bounded byte allowance")
    raw = _sequence(evidence.get("public_trajectories", ()), "task trajectories")
    if not raw:
        raise ValidationError("Public task trajectories are required")
    raw_sha = digest(raw)
    if evidence.get("public_trajectories_sha256", raw_sha) != raw_sha:
        raise ValidationError("Public trajectory source hash mismatch")
    cases = [_closed_case(row, index) for index, row in enumerate(raw)]
    failures = [case for case in cases if case["failed"]]
    passes = [case for case in cases if not case["failed"]]
    ordered, seen = [], set()
    for case in failures:
        if case["value"]["task_id"] not in seen:
            ordered.append(case)
            seen.add(case["value"]["task_id"])
    ordered.extend(case for case in failures if case not in ordered)
    variants = {case["index"]: _variants(case) for case in cases}
    selected = []
    # Preserve distinct failed task coverage before spending bytes on full cases.
    for case in ordered[:3]:
        trial = [*selected, variants[case["index"]][0]]
        if len(canonical_bytes(trial)) <= max_bytes:
            selected = trial
    for case in ordered[3:]:
        if len(selected) >= MAX_CASES:
            break
        trial = [*selected, variants[case["index"]][0]]
        if len(canonical_bytes(trial)) <= max_bytes:
            selected = trial
    if failures and not selected:
        raise ValidationError("Byte allowance cannot retain one failed case with request/call evidence")
    # Expand selected failures first, keeping every reserved failed case.
    for index, selected_case in enumerate(tuple(selected)):
        for value in reversed(variants[selected_case["feedback_projection"]["source_index"]]):
            trial = [*selected[:index], value, *selected[index + 1:]]
            if len(canonical_bytes(trial)) <= max_bytes:
                selected = trial
                break
    for case in passes:
        if len(selected) >= MAX_CASES:
            break
        for value in reversed(variants[case["index"]]):
            if len(canonical_bytes([*selected, value])) <= max_bytes:
                selected.append(value)
                break
    if not selected:
        raise ValidationError("Byte allowance cannot retain one readable public case")
    selected_indices = [row["feedback_projection"]["source_index"] for row in selected]
    supplied_failed = [case for case in failures if case["index"] in selected_indices]
    result = {"trajectories": selected, "provenance": {
        "projection": "public_failed_cases_first_v1", "raw_source_sha256": raw_sha,
        "allowed_projection_sha256": digest([case["value"] for case in cases]),
        "projected_sha256": digest(selected), "max_bytes": max_bytes,
        "supplied_bytes": len(canonical_bytes(selected)), "budget_scope": "canonical_trajectories_only",
        "max_supplied_cases": MAX_CASES,
        "total_cases": len(cases), "supplied_cases": len(selected),
        "total_failed_cases": len(failures), "supplied_failed_cases": len(supplied_failed),
        "omitted_failed_cases": len(failures) - len(supplied_failed),
        "failed_task_ids": [case["value"]["task_id"] for case in failures],
        "supplied_failed_task_ids": [case["value"]["task_id"] for case in supplied_failed],
        "omitted_case_indices": [case["index"] for case in cases if case["index"] not in selected_indices],
        "truncated_case_indices": [row["feedback_projection"]["source_index"] for row in selected
                                   if row["feedback_projection"]["truncated"]],
        "source_field_allowlist": "task_id;public_check_counts;public_request_fields;observed_authorization_API_calls",
        "not_copied": "initial_state;effects;independent_labels;tools_schema;arbitrary_metadata"}}
    return result
