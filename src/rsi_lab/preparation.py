"""Typed adapter boundary for resumable preparation, without deployment authority."""
from dataclasses import dataclass

from .contracts import (ArtifactOccurrence, ArtifactRef, AttemptSpec, Capabilities,
                        PrepareCursor, StrictRecord, QProgram, CursorFamily)
from .interventions.context import ContextPatch
from .interventions.validation import VerificationPatch


@dataclass(frozen=True)
class ResumeRequest(StrictRecord):
    attempt: AttemptSpec
    cursor: PrepareCursor | None
    operation_id: str
    context_patch: ContextPatch
    verification_patch: VerificationPatch
    guard_mode: str = "observe"
    fixed_preparation: ArtifactRef | None = None
    owner_kind: str = "branch"
    owner_id: str = "main"
    assigned_q: QProgram | None = None
    purpose: str = "discovery"
    cursor_family: CursorFamily = CursorFamily.PUBLIC_DEVELOPMENT
    generation_operation_id: str | None = None
    candidate_occurrence_id: str | None = None
    schema_version: int = 1

    def validate(self):
        from .contracts import require_id
        from .errors import ValidationError
        require_id(self.operation_id, "operation_id")
        require_id(self.owner_id, "owner_id")
        if self.purpose not in ('discovery','online_audit'):
            raise ValidationError('Preparation cannot consume label/final evaluation purposes')
        if (self.purpose == 'online_audit') != (self.cursor_family == CursorFamily.ADMISSION_PROBE):
            raise ValidationError('Preparation purpose/cursor mismatch')
        if self.cursor_family == CursorFamily.INDEPENDENT_EVALUATION:
            raise ValidationError('Independent labels cannot become preparation feedback')
        for value in (self.generation_operation_id,self.candidate_occurrence_id):
            if value is not None:
                require_id(value)
        if self.guard_mode not in ("observe", "enforce") or self.owner_kind not in ("branch", "pair"):
            raise ValidationError("Unsupported preparation mode or cost owner")
        if self.owner_id != (self.attempt.pair_id if self.owner_kind == "pair" else self.attempt.branch_id):
            raise ValidationError("Preparation cost owner does not match attempt")


@dataclass(frozen=True)
class PreparationState(StrictRecord):
    source_snapshot: ArtifactRef
    context_patch: ContextPatch
    verification_patch: VerificationPatch
    assigned_q: QProgram | None = None
    context_receipt: ArtifactRef | None = None
    candidates: tuple[ArtifactOccurrence, ...] = ()
    verification_receipt: ArtifactRef | None = None
    native_selected: ArtifactOccurrence | None = None
    prepared_snapshot: ArtifactRef | None = None
    candidate_binding: ArtifactRef | None = None
    native_plan: tuple[str, ...] = ()
    shared_cost_ref: str | None = None
    valid: bool = True
    missing_reason: str | None = None
    schema_version: int = 1


@dataclass(frozen=True)
class CheckpointResult(StrictRecord):
    cursor: PrepareCursor
    receipt: ArtifactRef
    valid: bool
    missing_reason: str | None = None
    schema_version: int = 1
