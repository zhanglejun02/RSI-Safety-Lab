from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from rsi_lab.contracts import (
    ArtifactRef, Checkpoint, Coupling, CursorFamily, ForkPoint,
    PairAnchor, PairingMode, Partition, PrepareCursor, RandomPlan, TaskCursor,
    TransferProgram, Version,
)
from rsi_lab.errors import ValidationError
from rsi_lab.randomness import derive_seed
from rsi_lab.serialization import canonical_bytes, decode_json


GOLDEN = Path(__file__).resolve().parents[1] / "golden"
REF = ArtifactRef("a" * 64, 1)


@pytest.mark.parametrize("data", [
    '{"x":1,"x":2}', '{"nested":{"x":1,"x":2}}', '{"x":NaN}',
    '{"x":Infinity}', '{"x":-Infinity}', '{"x":1e9999}', '[]', 'null',
    b'{"x":"\xff"}', '{"x":"\\ud800"}', '{"x":true}'.encode("utf-16"),
])
def test_json_input_rejects_ambiguous_or_noncanonical_values(data):
    with pytest.raises(ValidationError):
        decode_json(data)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), {1: "x"}, object(), {1, 2}])
def test_canonical_encoder_rejects_non_json_types(value):
    with pytest.raises(ValidationError):
        canonical_bytes(value)


def test_utf8_sorted_objects_preserve_array_order():
    assert canonical_bytes({"b": [2, 1], "a": "确认"}) == '{"a":"确认","b":[2,1]}'.encode()
    assert decode_json(canonical_bytes({"x": None, "y": True})) == {"x": None, "y": True}


@pytest.mark.parametrize("name", ["P", "N", "D", "E"])
def test_frozen_versions_round_trip_without_changing_bytes_or_hashes(name):
    raw = (GOLDEN / f"version_{name}.json").read_bytes()
    version = Version.from_dict(decode_json(raw))
    expected = json.loads((GOLDEN / "version_sha256.json").read_text())
    assert canonical_bytes(version) == raw
    assert hashlib.sha256(canonical_bytes(version)).hexdigest() == expected[name]


def test_strict_nested_program_types_and_unknown_fields():
    value = decode_json((GOLDEN / "version_D.json").read_bytes())
    for invalid in (1, 0, "true", None):
        altered = {**value, "agent": {**value["agent"], "check_scope": invalid}}
        with pytest.raises(ValidationError):
            Version.from_dict(altered)
    with pytest.raises(ValidationError, match="unknown"):
        Version.from_dict({**value, "agent": {**value["agent"], "exec": "anything"}})
    with pytest.raises(ValidationError):
        Version.from_dict({**value, "q": "q.unregistered.v1"})
    with pytest.raises(ValidationError):
        Version.from_dict({**value, "schema_version": True})
    with pytest.raises(ValidationError, match="missing"):
        Version.from_dict({key: val for key, val in value.items() if key != "schema_version"})
    with pytest.raises(ValidationError):
        TransferProgram(check_scope=True, check_confirmation=True, check_fresh=1)


def test_reference_validation_disallows_path_tokens_and_boolean_sizes():
    for digest in ("../file", "A" * 64, "a" * 63):
        with pytest.raises(ValidationError):
            ArtifactRef(digest, 1)
    for size in (True, -1, 1.0):
        with pytest.raises(ValidationError):
            ArtifactRef("a" * 64, size)


def test_cursor_scope_and_occurrence_constraints():
    with pytest.raises(ValidationError, match="Sealed"):
        TaskCursor(CursorFamily.ADMISSION_PROBE, partition=Partition.SEALED)
    with pytest.raises(ValidationError, match="one occurrence"):
        PrepareCursor(Checkpoint.POST_GENERATION, REF, "next", occurrence_index=1)
    with pytest.raises(ValidationError):
        TaskCursor(CursorFamily.PUBLIC_DEVELOPMENT, position=True)


def seed(plan, stream, arm="L", position="task/1"):
    return derive_seed(plan, root_id="root", pair_id="pair", replicate=0,
                       attempt_index=1, stream=stream, semantic_position=position, arm=arm)


def test_random_streams_pair_by_position_without_shared_mutable_rng():
    plan = RandomPlan(104729)
    before = {stream: seed(plan, stream) for stream in ("generation", "task", "q", "v")}
    assert len(set(before.values())) == 4
    for index in range(20):
        seed(plan, "q", position=f"extra/{index}")
    assert before == {stream: seed(plan, stream) for stream in before}
    assert seed(plan, "q", "L") == seed(plan, "q", "R")
    assert seed(plan, "task", "L") == seed(plan, "task", "R")
    assert seed(plan, "generation", "L") != seed(plan, "generation", "R")
    independent_q = replace(plan, q=Coupling.INDEPENDENT_REQUESTS)
    assert seed(independent_q, "q", "L") != seed(independent_q, "q", "R")
    assert seed(plan, "task", position="task/1") != seed(plan, "task", position="task/2")


def test_random_derivation_rejects_unknown_stream_and_nonstring_identity():
    with pytest.raises(ValidationError, match="stream"):
        seed(RandomPlan(1), "secret")
    with pytest.raises(ValidationError, match="arm"):
        seed(RandomPlan(1), "q", arm=123)
    with pytest.raises(ValidationError):
        RandomPlan(True)


def test_pairing_mode_checks_legal_fork_position():
    kwargs = dict(pair_id="pair", root_id="root", history_group_id="history",
                  snapshot=REF, task_plan=REF, random_plan=REF, cursors=(), round_index=4)
    PairAnchor(**kwargs, pairing_mode=PairingMode.FIXED_CANDIDATE, fork_at=ForkPoint.PRE_VERIFICATION)
    with pytest.raises(ValidationError):
        PairAnchor(**kwargs, pairing_mode=PairingMode.FIXED_CANDIDATE, fork_at=ForkPoint.ATTEMPT_START)
    with pytest.raises(ValidationError):
        PairAnchor(**kwargs, pairing_mode=PairingMode.PROCESS, fork_at=ForkPoint.PRE_DEPLOY)
