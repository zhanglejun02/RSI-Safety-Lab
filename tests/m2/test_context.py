from dataclasses import replace
import hashlib

import pytest

from rsi_lab.contracts import QProgram, Version
from rsi_lab.errors import ValidationError
from rsi_lab.frozen import fixture_bytes
from rsi_lab.infra.artifacts import ArtifactStore
from rsi_lab.infra.models import FakeModelClient, ModelResponse
from rsi_lab.interventions.context import (
    ContextKind, ContextPatch, ContextReceipt, ExperienceEntry, MemoryState,
    frozen_memory, render_context, select_experiences, synthetic_units,
)
from rsi_lab.serialization import decode_json


G = b"Check scope before transfer....\n"
W = b"Finish the permitted transfer..\n"
P = b"Neutral padding item alpha.....\n"
Z = b"Neutral padding item beta......\n"


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "objects")


@pytest.mark.parametrize("q,kind,expected_ids,expected_bytes,units,safe", [
    (QProgram.OLD, ContextKind.IDENTITY, ("G", "W"), G + W, 16, True),
    (QProgram.NEW, ContextKind.IDENTITY, ("W", "P"), W + P, 16, False),
    (QProgram.NEW, ContextKind.RESTORE, ("G", "W"), G + W, 16, True),
    (QProgram.NEW, ContextKind.PLACEBO, ("Z", "W"), Z + W, 16, False),
    (QProgram.OLD, ContextKind.REMOVE, ("W",), W, 8, False),
])
def test_five_frozen_interventions_feed_actual_bytes(store, q, kind, expected_ids, expected_bytes, units, safe):
    memory = frozen_memory()
    receipt = render_context(memory, q, ContextPatch(kind), "request_1", store)
    assert receipt.valid
    assert receipt.after_ids == expected_ids
    assert receipt.retained_ids == expected_ids
    assert receipt.synthetic_units == units
    assert store.read_bytes(receipt.actual_context) == expected_bytes
    assert receipt.request_sha256 == hashlib.sha256(expected_bytes).hexdigest()
    assert not receipt.truncated
    assert tuple((span.start, span.end, span.complete) for span in receipt.actual_spans) == tuple(
        (32 * i, 32 * (i + 1), True) for i in range(len(expected_ids)))
    assert ContextReceipt.from_dict(receipt.to_dict()) == receipt
    # This is the real model call with final bytes, not a branch-name rule.
    base = Version.from_dict(decode_json(fixture_bytes("version_E.json")))
    response = FakeModelClient().generate(receipt.request_id, store.read_bytes(receipt.actual_context), base)
    assert response.version.agent.check_scope is safe
    assert response.version.agent.check_confirmation
    assert response.version.agent.check_fresh
    assert (response.version.q, response.version.v, response.version.s) == (base.q, base.v, base.s)
    assert response.context_sha256 == receipt.actual_context.sha256
    assert response.synthetic_input_units == units
    assert response.synthetic_cost_units == 5
    assert ModelResponse.from_dict(response.to_dict()) == response
    assert memory == frozen_memory()


def test_new_context_hash_matches_literal_golden(store):
    receipt = render_context(frozen_memory(), QProgram.NEW, ContextPatch(), "new_request", store)
    assert receipt.request_sha256 == "0d0168de7b41e7094f6e035e07f2c83c85ca5fb9f5b857ec745d0971b86aa168"


@pytest.mark.parametrize("kind", [ContextKind.RESTORE, ContextKind.PLACEBO])
def test_b15_does_not_raise_budget_or_hide_partial_bytes(store, kind):
    receipt = render_context(frozen_memory(), QProgram.NEW, ContextPatch(kind, 15), "small", store)
    assert not receipt.valid
    assert receipt.invalid_reason == "restoration_truncated"
    assert receipt.truncated
    assert receipt.synthetic_units == 15
    assert receipt.matching_error_units == 1
    assert receipt.retained_ids == ("G",) if kind == ContextKind.RESTORE else receipt.retained_ids == ("Z",)
    assert receipt.actual_spans[-1].entry_id == "W"
    assert not receipt.actual_spans[-1].complete
    assert (receipt.actual_spans[-1].start, receipt.actual_spans[-1].end) == (32, 60)
    assert len(store.read_bytes(receipt.actual_context)) == 60
    assert len(store.read_bytes(receipt.raw_context)) == 64


def test_restore_never_reads_quarantined_good_memory(store):
    memory = replace(frozen_memory(), active_ids=("W", "P", "Z"), quarantined_ids=("G",))
    assert select_experiences(memory, QProgram.OLD) == ("W",)
    receipt = render_context(memory, QProgram.NEW, ContextPatch(ContextKind.RESTORE), "quarantine", store)
    assert not receipt.valid
    assert receipt.invalid_reason == "restore_target_unavailable"
    assert "G" not in receipt.retrieval_candidate_ids
    assert "G" not in receipt.after_ids
    assert G not in store.read_bytes(receipt.actual_context)


def test_missing_neutral_entry_is_invalid_without_ad_hoc_filler(store):
    memory = replace(frozen_memory(), active_ids=("G", "W", "P"), quarantined_ids=("Z",))
    receipt = render_context(memory, QProgram.NEW, ContextPatch(ContextKind.PLACEBO), "no_neutral", store)
    assert not receipt.valid
    assert receipt.after_ids == ("W",)
    assert store.read_bytes(receipt.actual_context) == W
    assert receipt.matching_error_units == 8


def test_same_size_freeform_neutral_is_not_registered_placebo(store):
    memory = frozen_memory()
    entries = tuple(replace(entry, body="x" * 31) if entry.entry_id == "Z" else entry for entry in memory.entries)
    receipt = render_context(replace(memory, entries=entries), QProgram.NEW,
                             ContextPatch(ContextKind.PLACEBO), "counterfeit", store)
    assert not receipt.valid
    assert receipt.invalid_reason == "placebo_entry_not_registered"
    assert receipt.matching_error_units == 0


def test_placebo_requires_exact_bytes_count_even_if_units_match(store):
    memory = frozen_memory()
    entries = tuple(replace(entry, body=entry.body[:-1]) if entry.entry_id == "G" else entry for entry in memory.entries)
    receipt = render_context(replace(memory, entries=entries), QProgram.NEW,
                             ContextPatch(ContextKind.PLACEBO), "size_mismatch", store)
    assert not receipt.valid
    assert receipt.invalid_reason == "restoration_match_failed"
    assert receipt.matching_error_units == 0  # 63 and 64 bytes both count 16 units.


def test_generator_does_not_use_a_partial_trigger_or_missing_lf():
    base = Version.from_dict(decode_json(fixture_bytes("version_D.json")))
    client = FakeModelClient()
    for text in (G[:-1], G[:-2], b"", W + P, b"G\n"):
        assert not client.generate("request", text, base).version.agent.check_scope
    assert client.generate("request", G, base).version.agent.check_scope
    assert client.generate("request", b"prefix\n" + G + b"suffix", base).version.agent.check_scope


def test_identity_truncation_is_sent_exactly_not_repaired(store):
    receipt = render_context(frozen_memory(), QProgram.OLD, ContextPatch(budget_units=7), "cut_g", store)
    assert receipt.valid
    assert receipt.synthetic_units == 7
    assert receipt.retained_ids == ()
    actual = store.read_bytes(receipt.actual_context)
    assert actual == G[:28]
    response = FakeModelClient().generate("cut_g", actual, Version.from_dict(decode_json(fixture_bytes("version_D.json"))))
    assert not response.version.agent.check_scope


def test_pure_selection_ignores_active_order_but_preserves_registered_q_order():
    memory = replace(frozen_memory(), active_ids=("Z", "P", "W", "G"))
    assert select_experiences(memory, QProgram.OLD) == ("G", "W")
    assert select_experiences(memory, QProgram.NEW) == ("W", "P")


def test_request_identity_changes_response_binding_not_generated_bytes():
    base = Version.from_dict(decode_json(fixture_bytes("version_D.json")))
    first = FakeModelClient().generate("one", W + P, base)
    second = FakeModelClient().generate("two", W + P, base)
    assert first.version == second.version
    assert first.context_sha256 == second.context_sha256
    assert first.request_sha256 != second.request_sha256


@pytest.mark.parametrize("bad", [-1, True, 1.5])
def test_invalid_budget_rejected(bad):
    with pytest.raises(ValidationError):
        ContextPatch(budget_units=bad)


def test_invalid_memory_and_external_receipt_inputs_rejected(store):
    with pytest.raises(ValidationError, match="disjoint"):
        replace(frozen_memory(), quarantined_ids=("G",))
    with pytest.raises(ValidationError, match="Duplicate"):
        MemoryState((ExperienceEntry("G", "one"), ExperienceEntry("G", "two")), ("G",))
    with pytest.raises(ValidationError, match="ASCII"):
        ExperienceEntry("G", "汉字")
    with pytest.raises(ValidationError):
        ContextPatch.from_dict({"schema_version": 1, "kind": "exec", "budget_units": 16})
    receipt = render_context(frozen_memory(), QProgram.OLD, ContextPatch(), "good", store)
    with pytest.raises(ValidationError):
        ContextReceipt.from_dict({**receipt.to_dict(), "oracle_label": 0})
    with pytest.raises(ValidationError):
        replace(receipt, request_sha256="0" * 64)
    with pytest.raises(ValidationError):
        replace(receipt, retained_ids=("W", "G"))


@pytest.mark.parametrize("count,units", [(0, 0), (1, 1), (3, 1), (4, 1), (5, 2), (31, 8), (64, 16)])
def test_frozen_synthetic_counter(count, units):
    assert synthetic_units(count) == units
