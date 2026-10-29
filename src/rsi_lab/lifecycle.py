"""Explicit admission, recovery and commit records. No generic state/read handles."""

from dataclasses import dataclass

from .contracts import (
    ArtifactOccurrence,
    ArtifactRef,
    CandidateBinding,
    StrictRecord,
    require_count,
    require_id,
)
from .errors import ValidationError


@dataclass(frozen=True)
class AdmissionView(StrictRecord):
    binding: CandidateBinding
    native_valid: bool
    context_receipt: ArtifactRef | None
    verification_receipt: ArtifactRef | None
    schema_version: int = 1


@dataclass(frozen=True)
class AuditDecision(StrictRecord):
    binding: CandidateBinding
    policy_version: str
    verdict: str
    evidence: ArtifactRef
    schema_version: int = 1

    def validate(self):
        require_id(self.policy_version, "policy_version")
        if self.verdict not in ("accept", "reject", "inconclusive"):
            raise ValidationError("Unknown admission verdict")


@dataclass(frozen=True)
class MemoryPlan(StrictRecord):
    mode: str
    added_ids: tuple[str, ...]
    active_ids: tuple[str, ...]
    quarantined_ids: tuple[str, ...]
    schema_version: int = 1

    def validate(self):
        if self.mode not in ("retain", "discard", "quarantine"):
            raise ValidationError("Unknown memory recovery policy")
        for ids in (self.added_ids, self.active_ids, self.quarantined_ids):
            if len(ids) != len(set(ids)):
                raise ValidationError("Duplicate memory index")
        if set(self.active_ids) & set(self.quarantined_ids):
            raise ValidationError("Active and quarantined memory overlap")


@dataclass(frozen=True)
class Resolution(StrictRecord):
    attempt_id: str
    verdict: str
    deployed_version: ArtifactRef
    next_edit_base: ArtifactRef
    memory_plan: MemoryPlan
    schema_version: int = 1

    def validate(self):
        require_id(self.attempt_id)
        if self.verdict not in ("accept", "reject", "inconclusive", "not_applicable"):
            raise ValidationError("Unknown resolution")


@dataclass(frozen=True)
class MaterializeCommand(StrictRecord):
    command_id: str
    attempt_id: str
    target_branch_id: str
    expected_head_revision: int
    source_snapshot: ArtifactRef
    approved_candidate: ArtifactOccurrence | None
    target_manifest: ArtifactRef
    resolution: ArtifactRef
    schema_version: int = 1

    def validate(self):
        for value in (self.command_id, self.attempt_id, self.target_branch_id):
            require_id(value)
        require_count(self.expected_head_revision, "expected_head_revision")


@dataclass(frozen=True)
class MaterializationReceipt(StrictRecord):
    command_id: str
    manifest: ArtifactRef
    environment: ArtifactRef
    schema_version: int = 1


@dataclass(frozen=True)
class CommitReceipt(StrictRecord):
    command_id: str
    attempt_id: str
    branch_id: str
    head_revision: int
    snapshot: ArtifactRef
    schema_version: int = 1

    def validate(self):
        require_count(self.head_revision, "head_revision", 1)


@dataclass(frozen=True)
class ProbeRound(StrictRecord):
    replicate: int
    attempt_index: int
    attempt_id: str
    deployed_version: ArtifactRef
    actual_violations: int
    complete: bool
    missing_reason: str | None = None
    schema_version: int = 1

    def validate(self):
        require_count(self.replicate, "replicate")
        require_count(self.attempt_index, "attempt_index", 1)
        require_count(self.actual_violations, "actual_violations")
        if self.complete != (self.missing_reason is None):
            raise ValidationError("Probe coverage requires explicit missing reason")


@dataclass(frozen=True)
class ProbeEvidence(StrictRecord):
    rounds: tuple[ProbeRound, ...]
    synthetic_cost_units: int
    complete: bool
    missing_reason: str | None = None
    schema_version: int = 1

    def validate(self):
        require_count(self.synthetic_cost_units, "synthetic_cost_units")
        keys = [(r.replicate, r.attempt_index) for r in self.rounds]
        if len(keys) != len(set(keys)):
            raise ValidationError("Duplicate probe round")
        if self.complete and (
            set(keys) != {(0, 1), (0, 2), (1, 1), (1, 2)}
            or not all(r.complete for r in self.rounds)
        ):
            raise ValidationError(
                "Complete evidence requires both two-attempt branches"
            )
        if self.complete != (self.missing_reason is None):
            raise ValidationError("Incomplete probe evidence requires a reason")
