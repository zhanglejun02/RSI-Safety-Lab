"""Financial stress controls verify tasks, never claim natural RSI failure rates."""
from copy import deepcopy

import pytest

from rsi_lab.errors import IntegrityError, ValidationError
from rsi_lab.serialization import digest
from rsi_lab.tasks import stress_finance as task


@pytest.mark.parametrize("family", task.FAMILIES)
@pytest.mark.parametrize("level", (1, 2, 3))
def test_pair_is_single_document_delta_and_safe_solutions_have_headroom(family, level):
    pair = task.make_pair(family, 7101, "development", level)
    original = digest(pair)
    control, stress = pair["control"], pair["stress"]
    assert len(control["steps"]) == len(stress["steps"]) == task.WARMUP[level] + 1
    assert control["initial"] == stress["initial"]
    assert control["steps"][:-1] == stress["steps"][:-1]
    assert control["steps"][-1]["request"] == stress["steps"][-1]["request"]
    left, right = [s["steps"][-1]["events"][0] for s in (control, stress)]
    assert (left["entity"], left["id"]) == (right["entity"], right["id"])
    assert left["changes"].keys() == right["changes"].keys()
    assert len(left["changes"]) == 1 and left["changes"] != right["changes"]
    for arm, stream in pair.items():
        founder, safe = [task.evaluate_source(source, [stream]) for source in
                         (task.FOUNDER_SOURCE, task.SAFE_SOURCE)]
        for result in (founder, safe):
            assert result["complete"] and result["correctness"] == result["utility"] == 1
            assert result["violations"] == result["attempts"] == 0
            assert task.verify_result(result, [stream])
        assert safe["cost"] < founder["cost"]
        assert safe["legal_total"] == task.WARMUP[level] + (arm == "control")
    assert digest(pair) == original


@pytest.mark.parametrize("family", task.FAMILIES)
@pytest.mark.parametrize("seed", (7101, 7102))
@pytest.mark.parametrize("level", (1, 2, 3))
def test_shortcut_passes_control_but_creates_one_real_simulated_violation(family, seed, level):
    pair = task.make_pair(family, seed, "development", level)
    control, stress = [task.evaluate_source(task.UNSAFE_SOURCES[family], [pair[arm]])
                       for arm in ("control", "stress")]
    assert control["complete"] and control["correctness"] == 1
    assert control["attempts"] == control["violations"] == 0
    assert stress["complete"] and stress["violations"] == stress["attempts"] == 1
    assert stress["legal_completed"] == stress["legal_total"] == task.WARMUP[level]
    assert stress["streams"][0]["rounds"][-1]["effects"][0]["payment"] is not None
    for arm, result in (("control", control), ("stress", stress)):
        assert task.verify_result(result, [pair[arm]])
    guarded = task.evaluate_source(task.UNSAFE_SOURCES[family], [pair["stress"]], "enforce")
    assert guarded["attempts"] == guarded["blocked"] == 1 and guarded["violations"] == 0
    assert task.verify_result(guarded, [pair["stress"]])


def test_partition_identity_repeatability_and_metadata_do_not_label_requests():
    pairs = [task.make_pair("duplicate_invoice", 7101, split, 2) for split in task.SPLITS]
    identities = [{s["request"]["id"] for s in p["control"]["steps"]} for p in pairs]
    assert all(identities[i].isdisjoint(identities[j]) for i in range(3) for j in range(i))
    assert pairs[0] == task.make_pair("duplicate_invoice", 7101, "public", 2)
    assert pairs[0] != task.make_pair("duplicate_invoice", 7102, "public", 2)
    for pair in pairs:
        for stream in pair.values():
            assert not {"family", "category", "arm", "level", "variant"} & set(stream)
            assert all(set(step["request"]) == {"id"} for step in stream["steps"])
    mutated = deepcopy(pairs[0])
    mutated["stress"]["initial"]["epoch"] = 99
    assert pairs[0]["control"]["initial"]["epoch"] == 1


def test_receipt_constraint_is_cumulative_and_duplicate_key_crosses_documents():
    duplicate = task.make_pair("duplicate_invoice", 7201, "development", 1)["stress"]
    first, last = [duplicate["initial"]["invoice"][duplicate["steps"][i]["request"]["id"]] for i in (0, -1)]
    assert first["id"] != last["id"] and first["invoice_number"] != last["invoice_number"]
    assert duplicate["steps"][-1]["events"][0]["changes"]["invoice_number"] == first["invoice_number"]
    receipt = task.make_pair("receipt_exhaustion", 7201, "development", 1)["stress"]
    quantity = next(iter(receipt["initial"]["invoice"].values()))["qty"]
    corrected = receipt["steps"][-1]["events"][0]["changes"]["rows"][0]["qty"]
    assert quantity <= corrected == quantity * task.WARMUP[1]


@pytest.mark.parametrize("level", (1, 2, 3))
def test_recipient_integrity_is_verified_against_approval_and_actual_payment(level):
    pair = task.make_pair("recipient_mismatch", 7202, "development", level)
    stress = pair["stress"]
    final = stress["steps"][-1]
    invoice = stress["initial"]["invoice"][final["request"]["id"]]
    approval = stress["initial"]["approval"][invoice["approval"]]
    destination = final["events"][0]["changes"]["recipient"]
    assert destination != approval["recipient"] == invoice["recipient"]
    assert set(final["events"][0]["changes"]) == {"recipient"}
    restored = deepcopy(stress)
    restored["steps"][-1]["events"][0]["changes"]["recipient"] = approval["recipient"]
    assert restored == pair["control"]
    result = task.evaluate_source(task.UNSAFE_SOURCES["recipient_mismatch"], [stress])
    effect = result["streams"][0]["rounds"][-1]["effects"][0]
    assert effect["payment"]["recipient"] == destination
    assert result["violations"] == 1
    assert task.RECIPIENT_REFERENCE["relationship"].endswith("independent_structured_reimplementation")


def test_rejection_has_no_utility_and_tampered_payment_evidence_fails_replay():
    stream = task.make_pair("approval_revocation", 7301, "development", 2)["stress"]
    rejected = task.evaluate_source("def handle(request, cache, api):\n    return cache\n", [stream])
    assert rejected["risk"] == 0 and rejected["utility"] == 0
    result = task.evaluate_source(task.UNSAFE_SOURCES["approval_revocation"], [stream])
    result["streams"][0]["rounds"][-1]["effects"][0]["before"]["approval"]["active"] = True
    with pytest.raises(IntegrityError):
        task.verify_result(result, [stream])


@pytest.mark.parametrize("args", [
    ("missing", 1, "public", 1), ("approval_revocation", -1, "public", 1),
    ("approval_revocation", True, "public", 1), ("approval_revocation", 1, "sealed", 1),
    ("approval_revocation", 1, "public", 4), ("approval_revocation", 1, "public", True),
])
def test_invalid_task_parameters_fail_before_execution(args):
    with pytest.raises(ValidationError):
        task.make_pair(*args)
