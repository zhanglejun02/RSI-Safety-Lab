"""Stable lineage grouping and leakage checks; no artifact or label access.

Declared partitions are authoritative. Incremental dataset writers should retain
those declarations, so a newly discovered clone bridge causes a visible conflict
rather than silently reassigning old samples. A public founder alone is not a
lineage, and is intentionally excluded from grouping.
"""

from dataclasses import dataclass

from rsi_lab.contracts import Partition, StrictRecord, require_id
from rsi_lab.errors import ConflictError, ValidationError
from rsi_lab.serialization import digest


def _sha(value, name):
    if value is not None and (
        len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValidationError(f"Invalid {name}")


@dataclass(frozen=True)
class PartitionRecord(StrictRecord):
    sample_id: str
    root_id: str
    history_group_id: str
    complete_state_sha256: str | None = None
    candidate_sha256: str | None = None
    protocol_sha256: str | None = None
    shared_prefix_ids: tuple[str, ...] = ()
    declared_partition: Partition | None = None
    public_founder_sha256: str | None = None
    template_id: str | None = None
    schema_version: int = 1

    def validate(self):
        for name in ("sample_id", "root_id", "history_group_id"):
            require_id(getattr(self, name), name)
        for name in (
            "complete_state_sha256",
            "candidate_sha256",
            "protocol_sha256",
            "public_founder_sha256",
        ):
            _sha(getattr(self, name), name)
        for identity in self.shared_prefix_ids:
            require_id(identity, "shared_prefix_id")
        if len(self.shared_prefix_ids) != len(set(self.shared_prefix_ids)):
            raise ValidationError("Duplicate shared prefix identity")
        if self.template_id is not None:
            require_id(self.template_id, "template_id")


def unit_fingerprint(record: PartitionRecord) -> str:
    """Fingerprint the declared lineage identity, never an inferred label."""
    if type(record) is not PartitionRecord:
        raise ValidationError("Expected a PartitionRecord")
    return digest(
        {
            "version": "lineage-unit-v1",
            "root_id": record.root_id,
            "history_group_id": record.history_group_id,
        }
    )


def clone_fingerprint(record: PartitionRecord) -> str | None:
    if type(record) is not PartitionRecord:
        raise ValidationError("Expected a PartitionRecord")
    if any(
        value is None
        for value in (
            record.complete_state_sha256,
            record.candidate_sha256,
            record.protocol_sha256,
        )
    ):
        return None
    return digest(
        {
            "version": "complete-candidate-protocol-v1",
            "state": record.complete_state_sha256,
            "candidate": record.candidate_sha256,
            "protocol": record.protocol_sha256,
        }
    )


def _groups(records):
    if not isinstance(records, (list, tuple)) or any(
        type(record) is not PartitionRecord for record in records
    ):
        raise ValidationError("Partitions require ordered PartitionRecord values")
    records = tuple(sorted(records, key=lambda record: record.sample_id))
    if len({record.sample_id for record in records}) != len(records):
        raise ValidationError("Duplicate partition sample identity")
    parents = list(range(len(records)))

    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    seen = {}
    for index, record in enumerate(records):
        keys = [("root", record.root_id), ("history", record.history_group_id)]
        keys.extend(("prefix", item) for item in record.shared_prefix_ids)
        clone = clone_fingerprint(record)
        if clone is not None:
            keys.append(("clone", clone))
        for key in keys:
            if key in seen:
                parents[find(index)] = find(seen[key])
            else:
                seen[key] = index
    groups = {}
    for index, record in enumerate(records):
        groups.setdefault(find(index), []).append(record)
    return tuple(tuple(group) for group in groups.values())


def _declared(group):
    choices = {
        record.declared_partition
        for record in group
        if record.declared_partition is not None
    }
    if len(choices) > 1:
        raise ConflictError(
            "Related lineage, prefix, or exact clone crosses declared partitions"
        )
    return next(iter(choices)) if choices else None


def validate_assignments(records) -> dict[str, Partition]:
    """Validate explicit source partitions, without silently assigning a split."""
    groups = _groups(records)
    if any(record.declared_partition is None for group in groups for record in group):
        raise ValidationError("Partition validation requires every source declaration")
    return {record.sample_id: _declared(group) for group in groups for record in group}


def group_fingerprints(records) -> dict[str, str]:
    """Identify connected units within a registered dataset, including clone ties.

    Unlike the per-record lineage fingerprint, these IDs join distinct declared
    roots linked by history or shared sources. Persist them with the dataset
    manifest; newly discovered bridges can change the connected-unit identity.
    """
    result = {}
    for group in _groups(records):
        fingerprint = digest(
            {
                "version": "connected-lineage-unit-v1",
                "roots": sorted({record.root_id for record in group}),
                "histories": sorted({record.history_group_id for record in group}),
            }
        )
        result.update({record.sample_id: fingerprint for record in group})
    return result


def assign_partitions(records, *, salt="history-split-v1") -> dict[str, Partition]:
    """Assign undeclared groups deterministically (60/20/10/10).

    Existing declarations win. Callers must persist assignments for incremental
    datasets; adding a bridge between already split groups then fails validation.
    No approximate/template deduplication or statistical independence is claimed.
    """
    require_id(salt, "split_salt")
    result = {}
    for group in _groups(records):
        partition = _declared(group)
        if partition is None:
            key = sorted({record.history_group_id for record in group})
            bucket = int(digest({"salt": salt, "history_groups": key}), 16) % 100
            partition = (
                Partition.TRAIN
                if bucket < 60
                else (
                    Partition.DEV
                    if bucket < 80
                    else Partition.CALIBRATION if bucket < 90 else Partition.SEALED
                )
            )
        result.update({record.sample_id: partition for record in group})
    return result
