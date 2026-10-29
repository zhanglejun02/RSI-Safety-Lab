"""Typed, closed projections for update-level learning.

Hashes identify evidence; none is an ArtifactRef or grants read authority. These
records describe a finite continuation protocol, not universal safety labels.
"""

from dataclasses import dataclass
import math
from typing import Protocol

from rsi_lab.contracts import CandidateOrigin, Partition, StrictRecord, require_count, require_id
from rsi_lab.errors import ValidationError

FEATURE_VERSION = "audit-features-v1"


def require_sha(value: str):
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValidationError("Expected lowercase SHA-256 identity")


def bounded(value: float, low=0.0, high=1.0):
    if not math.isfinite(value) or not low <= value <= high:
        raise ValidationError(f"Expected finite value in [{low}, {high}]")


@dataclass(frozen=True)
class UpdateBinding(StrictRecord):
    update_id: str
    root_id: str
    history_group_id: str
    state_sha256: str
    old_mechanism_sha256: str
    new_mechanism_sha256: str
    protocol_sha256: str
    component: str
    partition: Partition
    origin: CandidateOrigin
    horizon: int
    schema_version: int = 1

    def validate(self):
        for value in (self.update_id, self.root_id, self.history_group_id):
            require_id(value)
        for value in (self.state_sha256, self.old_mechanism_sha256,
                      self.new_mechanism_sha256, self.protocol_sha256):
            require_sha(value)
        if self.component not in ("q", "v", "agent"):
            raise ValidationError("Unknown updated component")
        require_count(self.horizon, "horizon", 1)


@dataclass(frozen=True)
class RoundOutcome(StrictRecord):
    arm: str
    replicate: int
    attempt: int
    risk: int | None
    legal_completed: int
    legal_total: int
    complete: bool
    trace_sha256: str | None
    candidate_sha256: str
    schema_version: int = 1

    def validate(self):
        if self.arm not in ("old", "new"):
            raise ValidationError("Outcome arm must be old or new")
        require_count(self.replicate, "replicate")
        require_count(self.attempt, "attempt", 1)
        require_count(self.legal_completed, "legal_completed")
        require_count(self.legal_total, "legal_total")
        if self.legal_completed > self.legal_total or self.risk not in (0, 1, None):
            raise ValidationError("Invalid observed outcome counts")
        if self.complete and self.risk is None:
            raise ValidationError("Complete outcome requires observed risk")
        if not self.complete and self.risk == 0:
            raise ValidationError("Incomplete absence of harm cannot be encoded zero")
        if self.trace_sha256 is None:
            if self.complete or self.risk == 1:
                raise ValidationError("Observed outcomes require actual trace provenance")
        else:
            require_sha(self.trace_sha256)
        require_sha(self.candidate_sha256)


def validate_grid(outcomes, replicates, horizon):
    require_count(replicates, "replicates", 1)
    require_count(horizon, "horizon", 1)
    observed = [(o.arm, o.replicate, o.attempt) for o in outcomes]
    expected = {(a, r, h) for a in ("old", "new")
                for r in range(replicates) for h in range(1, horizon + 1)}
    if len(observed) != len(set(observed)) or set(observed) != expected:
        raise ValidationError("Every preregistered old/new K x H slot must be explicit")


@dataclass(frozen=True)
class AuditFeatures(StrictRecord):
    binding: UpdateBinding
    diff_text: str
    state_summary: str
    native_valid: bool
    probe_outcomes: tuple[RoundOutcome, ...]
    probe_horizon: int
    probe_replicates: int
    probe_cost_units: float
    probe_stream_id: str
    coverage_supported: bool
    feature_version: str = FEATURE_VERSION
    schema_version: int = 1

    def validate(self):
        if self.feature_version != FEATURE_VERSION:
            raise ValidationError("Unknown audit feature version")
        if len(self.diff_text.encode()) > 65536 or len(self.state_summary.encode()) > 32768:
            raise ValidationError("Public text projection exceeds documented byte limit")
        validate_grid(self.probe_outcomes, self.probe_replicates, self.probe_horizon)
        if self.probe_horizon > self.binding.horizon:
            raise ValidationError("Probe horizon exceeds target continuation")
        if not math.isfinite(self.probe_cost_units) or self.probe_cost_units < 0:
            raise ValidationError("Invalid actual probe cost")
        require_id(self.probe_stream_id)


@dataclass(frozen=True)
class CandidateRiskLabel(StrictRecord):
    binding: UpdateBinding
    outcomes: tuple[RoundOutcome, ...]
    replicates: int
    evaluation_stream_id: str
    purpose: str = "independent_evaluation"
    schema_version: int = 1

    def validate(self):
        if self.purpose != "independent_evaluation":
            raise ValidationError("Candidate labels require independent evaluation")
        require_id(self.evaluation_stream_id)
        validate_grid(self.outcomes, self.replicates, self.binding.horizon)

    def targets(self) -> tuple[float, ...] | None:
        """Four heads: old risk, new risk, old utility, new utility.

        Incomplete trajectories remain retained in the dataset but are not
        silently turned into full-horizon supervised regression targets.
        """
        if not all(o.complete and o.risk is not None for o in self.outcomes):
            return None
        risks, utilities = [], []
        for arm in ("old", "new"):
            rows = [o for o in self.outcomes if o.arm == arm]
            denominator = sum(o.legal_total for o in rows)
            if not denominator:
                return None
            risks.append(sum(o.risk for o in rows) / len(rows))
            utilities.append(sum(o.legal_completed for o in rows) / denominator)
        return tuple(risks + utilities)


@dataclass(frozen=True)
class TrainingExample(StrictRecord):
    features: AuditFeatures
    label: CandidateRiskLabel
    schema_version: int = 1

    def validate(self):
        if self.features.binding != self.label.binding:
            raise ValidationError("Feature/label update, state or protocol binding mismatch")
        if self.features.probe_stream_id == self.label.evaluation_stream_id:
            raise ValidationError("Long labels cannot reuse the online probe stream")
        if ({o.trace_sha256 for o in self.features.probe_outcomes if o.trace_sha256 is not None}
                & {o.trace_sha256 for o in self.label.outcomes if o.trace_sha256 is not None}):
            raise ValidationError("Long labels cannot reuse probe execution receipts")


@dataclass(frozen=True)
class RiskPrediction(StrictRecord):
    binding: UpdateBinding
    model_sha256: str
    old_risk: float
    new_risk: float
    old_utility: float
    new_utility: float
    calibration_radius: tuple[float, ...]
    calibrated: bool
    calibration_roots: int
    in_scope: bool
    schema_version: int = 1

    def validate(self):
        require_sha(self.model_sha256)
        for value in (self.old_risk, self.new_risk, self.old_utility, self.new_utility):
            bounded(value)
        if len(self.calibration_radius) != 4:
            raise ValidationError("Four prediction heads require four error radii")
        for radius in self.calibration_radius:
            bounded(radius)
        require_count(self.calibration_roots, "calibration_roots")
        if self.calibrated != (self.calibration_roots > 0):
            raise ValidationError("Calibration status disagrees with held-out roots")

    @property
    def delta_risk(self):
        return self.new_risk - self.old_risk

    @property
    def delta_utility(self):
        return self.new_utility - self.old_utility


class RiskScorer(Protocol):
    def predict(self, features: AuditFeatures) -> RiskPrediction: ...
