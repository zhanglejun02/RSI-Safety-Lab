from dataclasses import replace

import pytest

from rsi_lab.analysis.partitions import (
    PartitionRecord,
    assign_partitions,
    clone_fingerprint,
    group_fingerprints,
    unit_fingerprint,
    validate_assignments,
)
from rsi_lab.contracts import Partition
from rsi_lab.errors import ConflictError, ValidationError


def sample(identity, **changes):
    return replace(
        PartitionRecord(identity, "root_" + identity, "history_" + identity), **changes
    )


def test_history_prefix_and_root_members_share_declared_partition():
    a = sample("a", declared_partition=Partition.DEV)
    b = sample(
        "b", history_group_id=a.history_group_id, declared_partition=Partition.DEV
    )
    c = sample(
        "c",
        root_id=b.root_id,
        declared_partition=Partition.DEV,
        shared_prefix_ids=("prefix",),
    )
    d = sample("d", shared_prefix_ids=("prefix",), declared_partition=Partition.DEV)
    assert set(validate_assignments([a, b, c, d]).values()) == {Partition.DEV}
    assert len(set(group_fingerprints([a, b, c, d]).values())) == 1
    with pytest.raises(ConflictError, match="crosses"):
        validate_assignments([a, b, c, replace(d, declared_partition=Partition.SEALED)])


@pytest.mark.parametrize("key", ["root_id", "history_group_id", "shared_prefix_ids"])
def test_related_samples_cannot_be_declared_in_train_and_sealed(key):
    shared = ("source_1",) if key == "shared_prefix_ids" else "shared"
    a = sample("a", **{key: shared}, declared_partition=Partition.TRAIN)
    b = sample("b", **{key: shared}, declared_partition=Partition.SEALED)
    with pytest.raises(ConflictError):
        validate_assignments([a, b])


def test_exact_full_state_candidate_protocol_clone_cannot_cross_partition():
    fingerprints = dict(
        complete_state_sha256="a" * 64,
        candidate_sha256="b" * 64,
        protocol_sha256="c" * 64,
    )
    a = sample("a", **fingerprints, declared_partition=Partition.TRAIN)
    b = sample("b", **fingerprints, declared_partition=Partition.DEV)
    assert clone_fingerprint(a) == clone_fingerprint(b)
    with pytest.raises(ConflictError):
        validate_assignments([a, b])
    assert validate_assignments([a, replace(b, protocol_sha256="d" * 64)]) == {
        "a": Partition.TRAIN,
        "b": Partition.DEV,
    }


def test_public_founder_candidate_or_template_alone_does_not_merge_roots():
    a = sample(
        "a",
        public_founder_sha256="a" * 64,
        candidate_sha256="b" * 64,
        template_id="same_template",
        declared_partition=Partition.TRAIN,
    )
    b = sample(
        "b",
        public_founder_sha256="a" * 64,
        candidate_sha256="b" * 64,
        template_id="same_template",
        declared_partition=Partition.SEALED,
    )
    assert clone_fingerprint(a) is None
    assert unit_fingerprint(a) != unit_fingerprint(b)
    assert validate_assignments([a, b]) == {"a": Partition.TRAIN, "b": Partition.SEALED}


def test_assignment_stable_under_ordering_and_unrelated_dataset_growth():
    a = sample("a")
    b = sample("b", history_group_id=a.history_group_id)
    initial = assign_partitions([a, b])
    assert initial["a"] == initial["b"]
    assert assign_partitions([b, a]) == initial
    more = assign_partitions([sample("independent"), a, b])
    assert {key: more[key] for key in initial} == initial
    with pytest.raises(ValidationError, match="declaration"):
        validate_assignments([a, b])


def test_existing_declaration_wins_and_later_clone_bridge_fails_visibly():
    a = sample("a", declared_partition=Partition.TRAIN, shared_prefix_ids=("left",))
    b = sample("b", declared_partition=Partition.SEALED, shared_prefix_ids=("right",))
    assert assign_partitions([a, b]) == {"a": Partition.TRAIN, "b": Partition.SEALED}
    bridge = sample("bridge", shared_prefix_ids=("left", "right"))
    with pytest.raises(ConflictError):
        assign_partitions([a, b, bridge])
    assert (
        assign_partitions([a, sample("c", shared_prefix_ids=("left",))])["c"]
        == Partition.TRAIN
    )


def test_partition_records_reject_duplicate_samples_and_invalid_fingerprints():
    a = sample("a")
    with pytest.raises(ValidationError, match="Duplicate partition"):
        assign_partitions([a, a])
    with pytest.raises(ValidationError, match="Invalid"):
        sample("a", complete_state_sha256="not_a_full_hash")
    with pytest.raises(ValidationError, match="Duplicate shared"):
        sample("a", shared_prefix_ids=("source", "source"))
    assert validate_assignments([]) == {}
    assert assign_partitions([]) == {}
