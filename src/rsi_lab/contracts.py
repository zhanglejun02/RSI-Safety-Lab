"""Versioned boundary records. Type hints are checked at construction and decoding.

Content identities never convey authority. Candidate provenance is a separate
record, and bindings are never embedded into the snapshot they reference.
"""

from __future__ import annotations

import re
import types
from dataclasses import MISSING, dataclass, fields
from enum import Enum
from typing import Union, get_args, get_origin, get_type_hints

from .errors import ValidationError
from .serialization import primitive


def _convert(value, expected, path: str, decoding: bool):
    origin, args = get_origin(expected), get_args(expected)
    if origin in (Union, types.UnionType):
        for alternative in args:
            try:
                return _convert(value, alternative, path, decoding)
            except ValidationError:
                continue
        raise ValidationError(f"{path}: does not match {expected}")
    if expected is type(None):
        if value is not None:
            raise ValidationError(f"{path}: expected null")
        return None
    if origin is tuple:
        if type(value) not in ((list, tuple) if decoding else (tuple,)):
            raise ValidationError(f"{path}: expected ordered array")
        if len(args) != 2 or args[1] is not Ellipsis:
            raise ValidationError(f"{path}: unsupported tuple schema")
        return tuple(_convert(v, args[0], f"{path}[{i}]", decoding) for i, v in enumerate(value))
    if isinstance(expected, type) and issubclass(expected, StrictRecord):
        if decoding and type(value) is dict:
            return expected.from_dict(value)
        if type(value) is not expected:
            raise ValidationError(f"{path}: expected {expected.__name__}")
        return value
    if isinstance(expected, type) and issubclass(expected, Enum):
        if type(value) is expected:
            return value
        if decoding and type(value) is str:
            try:
                return expected(value)
            except ValueError:
                pass
        raise ValidationError(f"{path}: unknown {expected.__name__}: {value!r}")
    if type(value) is not expected:
        raise ValidationError(f"{path}: expected {expected.__name__}, got {type(value).__name__}")
    return value


class StrictRecord:
    def __post_init__(self):
        hints = get_type_hints(type(self))
        for field in fields(self):
            _convert(getattr(self, field.name), hints[field.name], field.name, False)
        if hasattr(self, "schema_version") and self.schema_version != 1:
            raise ValidationError(f"Unsupported schema_version: {self.schema_version}")
        self.validate()

    def validate(self):
        """Subclasses add semantic checks after recursive field type checking."""

    def to_dict(self) -> dict:
        return primitive(self)

    @classmethod
    def from_dict(cls, value: dict):
        if type(value) is not dict:
            raise ValidationError(f"{cls.__name__}: expected object")
        spec = {f.name: f for f in fields(cls)}
        extra = set(value) - set(spec)
        required = {f.name for f in spec.values() if f.default is MISSING and f.default_factory is MISSING}
        if "schema_version" in spec:
            required.add("schema_version")
        if extra or required - set(value):
            raise ValidationError(f"{cls.__name__}: unknown={sorted(extra)}, missing={sorted(required - set(value))}")
        hints = get_type_hints(cls)
        return cls(**{k: _convert(v, hints[k], f"{cls.__name__}.{k}", True) for k, v in value.items()})


def require_id(value: str, name: str = "id"):
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}", value):
        raise ValidationError(f"{name}: invalid identity {value!r}")


def require_count(value: int, name: str, minimum: int = 0):
    if type(value) is not int or value < minimum:
        raise ValidationError(f"{name}: must be an integer >= {minimum}")


class Checkpoint(str, Enum):
    POST_EXPERIENCE_SELECTION = "post_experience_selection"
    POST_GENERATION = "post_generation"
    PRE_VERIFICATION = "pre_verification"
    PRE_DEPLOY = "pre_deploy"


class ForkPoint(str, Enum):
    ATTEMPT_START = "attempt_start"
    PRE_VERIFICATION = "pre_verification"
    PRE_DEPLOY = "pre_deploy"


class PairingMode(str, Enum):
    PROCESS = "process"
    FIXED_CANDIDATE = "fixed_candidate"


class CandidateOrigin(str, Enum):
    CONTROL = "control"
    STRESS = "stress"
    NATURAL = "natural"


class CursorFamily(str, Enum):
    PUBLIC_DEVELOPMENT = "public_development"
    ADMISSION_PROBE = "admission_probe"
    INDEPENDENT_EVALUATION = "independent_evaluation"


class Partition(str, Enum):
    TRAIN = "train"
    DEV = "dev"
    CALIBRATION = "calibration"
    SEALED = "sealed"


class Phase(str, Enum):
    CREATED = "created"
    PREPARING = "preparing"
    PREPARED = "prepared"
    AUDITED = "audited"
    RESOLVED = "resolved"
    MATERIALIZED = "materialized"
    COMMITTED = "committed"


class RunStatus(str, Enum):
    ACTIVE = "active"
    COMPLETE = "complete"
    TIMEOUT = "timeout"
    CRASHED = "crashed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    INVALID = "invalid"
    OUTCOME_UNKNOWN = "outcome_unknown"


class Coupling(str, Enum):
    PAIRED_BY_POSITION = "paired_by_position"
    INDEPENDENT_REQUESTS = "independent_requests"


@dataclass(frozen=True)
class ArtifactRef(StrictRecord):
    sha256: str
    size_bytes: int
    media_type: str = "application/octet-stream"
    schema_version: int = 1

    def validate(self):
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ValidationError("ArtifactRef.sha256: expected lowercase SHA-256")
        require_count(self.size_bytes, "size_bytes")
        if not self.media_type or "/" not in self.media_type:
            raise ValidationError("Invalid media_type")


@dataclass(frozen=True)
class ArtifactOccurrence(StrictRecord):
    occurrence_id: str
    artifact: ArtifactRef
    source_attempt_id: str
    origin: CandidateOrigin
    purpose: str
    schema_version: int = 1

    def validate(self):
        for key in ("occurrence_id", "source_attempt_id", "purpose"):
            require_id(getattr(self, key), key)


@dataclass(frozen=True)
class TaskCursor(StrictRecord):
    family: CursorFamily
    position: int = 0
    partition: Partition = Partition.DEV
    namespace: str = "main"
    schema_version: int = 1

    def validate(self):
        require_count(self.position, "position")
        require_id(self.namespace, "namespace")
        if self.family != CursorFamily.INDEPENDENT_EVALUATION and self.partition == Partition.SEALED:
            raise ValidationError("Sealed partition is exclusive to independent evaluation")


@dataclass(frozen=True)
class RandomPlan(StrictRecord):
    master_seed: int
    generation: Coupling = Coupling.INDEPENDENT_REQUESTS
    task: Coupling = Coupling.PAIRED_BY_POSITION
    q: Coupling = Coupling.PAIRED_BY_POSITION
    v: Coupling = Coupling.PAIRED_BY_POSITION
    algorithm: str = "sha256_position_v1"
    schema_version: int = 1

    def validate(self):
        require_count(self.master_seed, "master_seed")
        if self.algorithm != "sha256_position_v1":
            raise ValidationError("Unknown random derivation algorithm")


@dataclass(frozen=True)
class ComponentRefs(StrictRecord):
    agent: ArtifactRef
    q: ArtifactRef
    v: ArtifactRef
    s: ArtifactRef
    schema_version: int = 1


@dataclass(frozen=True)
class SnapshotManifest(StrictRecord):
    components: ComponentRefs
    deployed_version: ArtifactRef
    edit_base_version: ArtifactRef
    memory: ArtifactRef
    archive: ArtifactRef
    environment: ArtifactRef
    random_plan: ArtifactRef
    cursors: tuple[TaskCursor, ...]
    implementation_version: str
    schema_version: int = 1

    def validate(self):
        if not self.implementation_version:
            raise ValidationError("Snapshot requires implementation version")
        keys = [(c.family, c.partition, c.namespace) for c in self.cursors]
        if len(keys) != len(set(keys)):
            raise ValidationError("Duplicate task cursor")
        if {c.family for c in self.cursors} != set(CursorFamily):
            raise ValidationError("Snapshot requires all three cursor families")


@dataclass(frozen=True)
class PrepareCursor(StrictRecord):
    checkpoint: Checkpoint
    local_state: ArtifactRef
    next_operation_id: str
    occurrence_index: int = 0
    next_operation_index: int = 0
    schema_version: int = 1

    def validate(self):
        require_id(self.next_operation_id, "next_operation_id")
        require_count(self.next_operation_index, "next_operation_index")
        if self.occurrence_index != 0:
            raise ValidationError("M1 supports one occurrence of each checkpoint")


@dataclass(frozen=True)
class PairAnchor(StrictRecord):
    pair_id: str
    root_id: str
    history_group_id: str
    snapshot: ArtifactRef
    task_plan: ArtifactRef
    random_plan: ArtifactRef
    cursors: tuple[TaskCursor, ...]
    pairing_mode: PairingMode
    fork_at: ForkPoint
    round_index: int
    schema_version: int = 1

    def validate(self):
        for key in ("pair_id", "root_id", "history_group_id"):
            require_id(getattr(self, key), key)
        require_count(self.round_index, "round_index")
        if self.pairing_mode == PairingMode.FIXED_CANDIDATE and self.fork_at == ForkPoint.ATTEMPT_START:
            raise ValidationError("Fixed candidate pairing requires a prepared candidate checkpoint")
        if self.pairing_mode == PairingMode.PROCESS and self.fork_at != ForkPoint.ATTEMPT_START:
            raise ValidationError("Process pairing starts at attempt_start")


@dataclass(frozen=True)
class AttemptSpec(StrictRecord):
    attempt_id: str
    run_id: str
    pair_id: str
    branch_id: str
    root_id: str
    history_group_id: str
    arm: str
    replicate: int
    attempt_index: int
    protocol: ArtifactRef
    source_snapshot: ArtifactRef
    candidate_origin: CandidateOrigin
    intervention: str = "identity"
    schema_version: int = 1

    def validate(self):
        for key in ("attempt_id", "run_id", "pair_id", "branch_id", "root_id", "history_group_id", "arm", "intervention"):
            require_id(getattr(self, key), key)
        require_count(self.replicate, "replicate")
        require_count(self.attempt_index, "attempt_index", 1)


@dataclass(frozen=True)
class CandidateBinding(StrictRecord):
    attempt_id: str
    candidate_occurrence: ArtifactOccurrence
    base_snapshot: ArtifactRef
    protocol: ArtifactRef
    schema_version: int = 1

    def validate(self):
        require_id(self.attempt_id, "attempt_id")


@dataclass(frozen=True)
class CheckpointCapability(StrictRecord):
    checkpoint: Checkpoint
    payload_schema: str
    writable_fields: tuple[str, ...]
    restorable: bool = True
    schema_version: int = 1

    def validate(self):
        require_id(self.payload_schema, 'payload_schema')
        if len(self.writable_fields) != len(set(self.writable_fields)):
            raise ValidationError('Duplicate writable checkpoint field')


@dataclass(frozen=True)
class Capabilities(StrictRecord):
    adapter_id: str
    checkpoints: tuple[Checkpoint, ...]
    pairing_modes: tuple[PairingMode, ...]
    snapshot_supported: bool
    arbitrary_code_execution: bool = False
    hard_isolation_verified: bool = False
    checkpoint_contracts: tuple[CheckpointCapability, ...] = ()
    schema_version: int = 1


class QProgram(str, Enum):
    OLD = "q.old.v1"
    NEW = "q.new.v1"


class VProgram(str, Enum):
    PUBLIC = "v.public.v1"


class SProgram(str, Enum):
    LATEST_VALID = "s.latest_valid.v1"
    ARCHIVE_SCORE = "s.archive_score.v1"


@dataclass(frozen=True)
class TransferProgram(StrictRecord):
    check_scope: bool
    check_confirmation: bool
    check_fresh: bool
    op: str = "transfer"

    def validate(self):
        if self.op != "transfer":
            raise ValidationError("Only the registered transfer declaration is supported")


@dataclass(frozen=True)
class Version(StrictRecord):
    agent: TransferProgram
    q: QProgram
    v: VProgram
    s: SProgram
    schema_version: int = 1

