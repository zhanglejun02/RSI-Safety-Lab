"""Closed TOML schemas; configuration cannot import or execute Python."""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import (
    CandidateOrigin,
    ForkPoint,
    PairingMode,
    Partition,
    RandomPlan,
    StrictRecord,
    require_count,
)
from .errors import ValidationError


@dataclass(frozen=True)
class PairingConfig(StrictRecord):
    mode: PairingMode = PairingMode.PROCESS
    fork_at: ForkPoint = ForkPoint.ATTEMPT_START
    verification_intervention: bool = False
    schema_version: int = 1

    def validate(self):
        if (
            self.mode == PairingMode.FIXED_CANDIDATE
            and self.fork_at == ForkPoint.ATTEMPT_START
        ):
            raise ValidationError(
                "fixed_candidate requires an existing prepared candidate"
            )
        if self.mode == PairingMode.PROCESS and self.fork_at != ForkPoint.ATTEMPT_START:
            raise ValidationError("process pairing must start at attempt_start")
        if self.verification_intervention and (
            self.mode != PairingMode.FIXED_CANDIDATE
            or self.fork_at != ForkPoint.PRE_VERIFICATION
        ):
            raise ValidationError(
                "v intervention requires fixed_candidate from pre_verification"
            )


@dataclass(frozen=True)
class ResourcesConfig(StrictRecord):
    synthetic_units_per_branch: int = 100
    schema_version: int = 1

    def validate(self):
        require_count(self.synthetic_units_per_branch, "synthetic_units_per_branch")
        if self.synthetic_units_per_branch > 2**63 - 1:
            raise ValidationError(
                "synthetic_units_per_branch must fit a signed 64-bit ledger integer"
            )


@dataclass(frozen=True)
class LabConfig(StrictRecord):
    schema_version: int = 1
    study: str = "m1_state_demo"
    stage: str = "M1"
    subject: str = "state_fixture"
    runtime: str = "trusted_fixture"
    candidate_origin: CandidateOrigin = CandidateOrigin.CONTROL
    partition: Partition = Partition.DEV
    arbitrary_code_execution: bool = False
    frozen_definition_version: str = "frozen-p0-v1"
    fixture_version: str = "transfer-v1"
    context_budget_units: int = 16
    guard_mode: str = "observe"
    horizon_attempts: int = 2
    audit_budget_units: int = 40
    admission_policy: str = "native"
    memory_mode: str = "quarantine"
    task: str = "workflow"
    suite: str = "transfer-v1"
    replicates: int = 1
    continuation: str = "frozen_mechanism"
    allowed_updates: tuple[str, ...] = ("agent",)
    frozen_components: tuple[str, ...] = ("q", "v", "s")
    probe_replicates: int = 2
    probe_horizon_attempts: int = 2
    pairing: PairingConfig = field(default_factory=PairingConfig)
    random: RandomPlan = field(default_factory=lambda: RandomPlan(104729))
    resources: ResourcesConfig = field(default_factory=ResourcesConfig)

    def validate(self):
        require_count(self.context_budget_units, "context_budget_units")
        if self.guard_mode not in ("observe", "enforce"):
            raise ValidationError("Unknown guard_mode")
        require_count(self.horizon_attempts, "horizon_attempts")
        require_count(self.audit_budget_units, "audit_budget_units")
        if not 1 <= self.horizon_attempts <= 100 or self.audit_budget_units > 40:
            raise ValidationError(
                "M3 supports 1–100 attempts and at most 40 audit units"
            )
        if self.admission_policy not in ("native", "reject_all", "probe"):
            raise ValidationError("Unknown admission_policy")
        if self.memory_mode not in ("retain", "discard", "quarantine"):
            raise ValidationError("Unknown memory_mode")
        for name in ("replicates", "probe_replicates", "probe_horizon_attempts"):
            require_count(getattr(self, name), name, 1)
        if self.replicates > 100:
            raise ValidationError("At most 100 fixture root replicates are supported")


def load_config(path: Path) -> LabConfig:
    try:
        with Path(path).open("rb") as handle:
            return LabConfig.from_dict(tomllib.load(handle))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ValidationError(f"Cannot load config {path}: {exc}") from exc
