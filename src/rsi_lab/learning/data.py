"""Purpose-limited offline dataset joins, with integrity and lineage checks."""

from pathlib import Path

from rsi_lab.analysis.partitions import PartitionRecord, validate_assignments, group_fingerprints
from rsi_lab.contracts import Partition
from rsi_lab.errors import ConflictError, IntegrityError, PermissionDenied, ValidationError
from rsi_lab.infra.artifacts import _checked_path
from rsi_lab.serialization import canonical_bytes, decode_json, digest
from .contracts import AuditFeatures, CandidateRiskLabel, TrainingExample, UpdateBinding

ALLOWED = {"train": {Partition.TRAIN}, "calibration": {Partition.CALIBRATION},
           "development": {Partition.DEV}, "evaluation": {Partition.SEALED}}


def partition_record(binding):
    return PartitionRecord(binding.update_id, binding.root_id, binding.history_group_id,
                           binding.state_sha256, binding.new_mechanism_sha256,
                           binding.protocol_sha256, declared_partition=binding.partition)


def validate_bindings(bindings):
    records = tuple(partition_record(b) for b in bindings)
    validate_assignments(records)
    return group_fingerprints(records)


def write_dataset(output: Path, examples: tuple[TrainingExample, ...]) -> dict:
    """Write joined evidence and targets separately, preserving all missing rows.

    This is a trusted offline packaging operation, not an online scorer API.
    The manifest exposes partition metadata so loaders can reject/skip a sealed
    source before accessing its feature or label file.
    """
    if type(examples) is not tuple or any(type(e) is not TrainingExample for e in examples):
        raise ValidationError("Dataset requires typed ordered training examples")
    if not examples:
        raise ValidationError("Cannot write an empty dataset")
    groups = validate_bindings(tuple(e.features.binding for e in examples))
    output = _checked_path(Path(output))
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ConflictError("Dataset output exists") from exc
    entries = []
    for i, example in enumerate(examples):
        binding = example.features.binding
        row = {"binding": binding.to_dict(), "group_id": groups[binding.update_id]}
        for kind, record in (("features", example.features), ("label", example.label)):
            name = f"{i:06d}.{kind}.json"
            (output / name).write_bytes(canonical_bytes(record))
            row[kind] = {"file": name, "sha256": digest(record)}
        entries.append(row)
    manifest = {"schema_version": 1, "format": "update-risk-dataset-v1",
                "entries": entries, "rows": len(entries),
                "supervised_rows": sum(e.label.targets() is not None for e in examples),
                "missing_rows": sum(e.label.targets() is None for e in examples)}
    (output / "manifest.json").write_bytes(canonical_bytes(manifest))
    return manifest


def _manifest(path, read):
    root = _checked_path(Path(path))
    manifest = decode_json(read(_checked_path(root / "manifest.json")))
    if set(manifest) != {"schema_version", "format", "entries", "rows", "supervised_rows", "missing_rows"}:
        raise IntegrityError("Unknown dataset manifest fields")
    if manifest["schema_version"] != 1 or manifest["format"] != "update-risk-dataset-v1":
        raise IntegrityError("Unsupported dataset manifest")
    entries = manifest["entries"]
    if not isinstance(entries, list) or manifest["rows"] != len(entries):
        raise IntegrityError("Dataset row count mismatch")
    bindings = []
    for index, entry in enumerate(entries):
        if type(entry) is not dict or set(entry) != {"binding", "group_id", "features", "label"}:
            raise IntegrityError("Unknown dataset entry")
        bindings.append(UpdateBinding.from_dict(entry["binding"]))
        for kind in ("features", "label"):
            ref = entry[kind]
            if type(ref) is not dict or set(ref) != {"file", "sha256"} or ref["file"] != f"{index:06d}.{kind}.json":
                raise IntegrityError("Dataset paths must match registered flat layout")
    groups = validate_bindings(tuple(bindings))
    if any(entry["group_id"] != groups[binding.update_id]
           for entry, binding in zip(entries, bindings)):
        raise IntegrityError("Dataset lineage group mismatch")
    return root, entries, bindings


def load_examples(path: Path, *, purpose: str, file_reader=None) -> tuple[TrainingExample, ...]:
    if purpose not in ALLOWED:
        raise PermissionDenied("Unknown dataset purpose")
    read = file_reader or Path.read_bytes
    root, entries, bindings = _manifest(path, read)
    selected = []
    for entry, binding in zip(entries, bindings):
        if binding.partition not in ALLOWED[purpose]:
            continue  # No feature/label file from another partition is opened.
        values = []
        for kind, cls in (("features", AuditFeatures), ("label", CandidateRiskLabel)):
            payload = decode_json(read(_checked_path(root / entry[kind]["file"])))
            if digest(payload) != entry[kind]["sha256"]:
                raise IntegrityError("Dataset artifact digest mismatch")
            value = cls.from_dict(payload)
            if value.binding != binding:
                raise IntegrityError("Dataset source partition or update binding changed")
            values.append(value)
        selected.append(TrainingExample(*values))
    if not selected:
        raise PermissionDenied(f"Dataset has no records for {purpose}")
    return tuple(selected)


def load_public_features(path: Path, *, partition: Partition, file_reader=None) -> tuple[AuditFeatures, ...]:
    """Blind evaluation input: never reads a label, including sealed labels."""
    if type(partition) is not Partition:
        raise ValidationError("Expected declared partition")
    read = file_reader or Path.read_bytes
    root, entries, bindings = _manifest(path, read)
    result = []
    for entry, binding in zip(entries, bindings):
        if binding.partition != partition:
            continue
        payload = decode_json(read(_checked_path(root / entry["features"]["file"])))
        if digest(payload) != entry["features"]["sha256"]:
            raise IntegrityError("Dataset feature digest mismatch")
        features = AuditFeatures.from_dict(payload)
        if features.binding != binding:
            raise IntegrityError("Dataset source feature binding changed")
        result.append(features)
    return tuple(result)


build_dataset = write_dataset
