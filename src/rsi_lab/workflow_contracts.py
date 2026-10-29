"""Transfer task and trusted effect-log records shared by task and oracle code."""

from dataclasses import dataclass

from .contracts import StrictRecord, require_count, require_id
from .errors import ValidationError


@dataclass(frozen=True)
class Task(StrictRecord):
    task_id: str
    namespace: str
    actor: str
    record: str
    recipient: str
    confirmation_id: str | None
    legal: bool
    schema_version: int = 1

    def validate(self):
        for name in ("task_id", "namespace", "actor", "record", "recipient"):
            require_id(getattr(self, name), name)
        if self.confirmation_id is not None:
            require_id(self.confirmation_id, "confirmation_id")


@dataclass(frozen=True)
class AuthorizationFacts(StrictRecord):
    in_scope: bool
    has_confirmation: bool
    confirmation_spent: bool | None
    schema_version: int = 1

    def validate(self):
        if self.has_confirmation != (self.confirmation_spent is not None):
            raise ValidationError("Confirmation existence and spent fact disagree")


@dataclass(frozen=True)
class Delivery(StrictRecord):
    effect_id: str
    task: Task
    confirmation_consumed: bool
    schema_version: int = 1

    def validate(self):
        require_id(self.effect_id, "effect_id")


@dataclass(frozen=True)
class OperationRecord(StrictRecord):
    effect_id: str
    task: Task
    before: AuthorizationFacts
    guard_mode: str
    refused: bool
    refusal_reason: str | None
    blocked: bool
    changed: bool
    delivery: Delivery | None
    sequence: int
    purpose: str = "execution"
    schema_version: int = 1

    def validate(self):
        require_id(self.effect_id, "effect_id")
        require_count(self.sequence, "sequence", 1)
        if self.guard_mode not in ("observe", "enforce"):
            raise ValidationError("Unregistered guard mode")
        if self.purpose not in ("execution", "validation", "prehistory"):
            raise ValidationError("Unregistered workflow operation purpose")
        if self.refused != (self.refusal_reason is not None):
            raise ValidationError("Refusal must have an explicit reason")
        if self.refusal_reason not in (None, "scope", "missing_confirmation", "spent_confirmation"):
            raise ValidationError("Unknown refusal reason")


@dataclass(frozen=True)
class Evaluation(StrictRecord):
    actual_violations: int
    risk: int | None
    risk_missing_reason: str | None
    legal_completed: int
    legal_denominator: int
    utility: float | None
    utility_missing_reason: str | None
    legal_refusals: int
    legal_refusal_rate: float | None
    blocked_violations: int
    unauthorized_requests: int
    completed_tasks: int
    expected_tasks: int
    complete: bool
    oracle_version: str = "transfer_log_oracle_v1"
    schema_version: int = 1

    def validate(self):
        for name in ("actual_violations", "legal_completed", "legal_denominator", "legal_refusals",
                     "blocked_violations", "unauthorized_requests", "completed_tasks", "expected_tasks"):
            require_count(getattr(self, name), name)
        if self.risk not in (None, 0, 1):
            raise ValidationError("Risk must be zero, one, or unknown")
        if (self.risk is None) != (self.risk_missing_reason is not None):
            raise ValidationError("Missing risk requires a reason")
        if (self.utility is None) != (self.utility_missing_reason is not None):
            raise ValidationError("Missing utility requires a reason")
        if self.legal_completed + self.legal_refusals > self.legal_denominator:
            raise ValidationError("Legal task counts exceed preregistered denominator")
        if self.completed_tasks > self.expected_tasks:
            raise ValidationError("Task results exceed expected task count")
        for value in (self.utility, self.legal_refusal_rate):
            if value is not None and not 0 <= value <= 1:
                raise ValidationError("Legal task rates must be within [0, 1]")
