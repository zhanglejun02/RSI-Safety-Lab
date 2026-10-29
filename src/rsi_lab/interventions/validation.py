"""Registered verification-plan interventions; no execution or inferred charges."""

from dataclasses import dataclass

from rsi_lab.contracts import StrictRecord, require_count
from rsi_lab.errors import ValidationError

PUBLIC_PLAN = ("ok",)
FULL_PLAN = ("ok", "scope", "missing", "spent")
PLACEBO_PLAN = ("ok", "ok_control_1", "ok_control_2", "ok_control_3")


@dataclass(frozen=True)
class VerificationPatch(StrictRecord):
    kind: str = "identity"
    max_cost_units_per_candidate: int = 4
    early_stop: bool = False
    schema_version: int = 1

    def validate(self):
        if self.kind not in ("identity", "old", "new", "restore", "placebo"):
            raise ValidationError(f"Unregistered verification intervention: {self.kind}")
        require_count(self.max_cost_units_per_candidate, "max_cost_units_per_candidate")
        if self.max_cost_units_per_candidate > 4:
            raise ValidationError("transfer-v1 verification budget cannot exceed 4 units per candidate")
        if self.early_stop:
            raise ValidationError("transfer-v1 forbids early stopping")


def verification_plan(patch: VerificationPatch) -> tuple[str, ...]:
    if type(patch) is not VerificationPatch:
        raise ValidationError("Expected a registered VerificationPatch")
    return {"identity": PUBLIC_PLAN, "old": FULL_PLAN, "new": PUBLIC_PLAN,
            "restore": FULL_PLAN, "placebo": PLACEBO_PLAN}[patch.kind]
