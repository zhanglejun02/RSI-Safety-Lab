"""Trainable bounded baseline with frozen, non-executable JSON model artifacts.

The encoder is fixed, order-sensitive signed hashing, not a pretrained language model. Four
logistic heads fit continuous empirical risk/utility using root-weighted squared
error. Calibration radii are held-out empirical residual summaries; they are
not guarantees under distribution shift or dependent repeated deployment.
"""

from dataclasses import dataclass, replace
import hashlib
import math
from pathlib import Path
import re
import tempfile

from rsi_lab.contracts import Partition, StrictRecord, require_count
from rsi_lab.errors import ConflictError, PermissionDenied, ValidationError
from rsi_lab.infra.artifacts import _checked_path
from rsi_lab.serialization import canonical_bytes, decode_json, digest
from .contracts import AuditFeatures, FEATURE_VERSION, RiskPrediction, TrainingExample, UpdateBinding, bounded, require_sha
from .data import load_examples, load_public_features, validate_bindings

ENCODER = "signed-diff-position64-ngram64-state32-and-public-counts-v2"
DIFF_POSITION_DIM = 64
DIFF_NGRAM_DIM = 64
STATE_DIM = 32
DIMENSIONS = DIFF_POSITION_DIM + DIFF_NGRAM_DIM + STATE_DIM + 16


def _hash_text_items(items, dimensions):
    vector = [0.0] * dimensions
    for item in items:
        # Canonical tuple encoding avoids ambiguities between positions/tokens.
        raw = hashlib.sha256(canonical_bytes(item)).digest()
        vector[int.from_bytes(raw[:4], "big") % dimensions] += 1.0 if raw[4] & 1 else -1.0
    norm = math.sqrt(sum(x*x for x in vector)) or 1.0
    return tuple(x / norm for x in vector)


def encode(features: AuditFeatures) -> tuple[float, ...]:
    if type(features) is not AuditFeatures:
        raise ValidationError("RiskScorer accepts AuditFeatures only")
    # A token bag erases q.order permutations. Position-token features retain
    # syntax/order, while lexical bigrams/trigrams retain local order even when
    # punctuation separates JSON array entries. No task-specific IDs or labels
    # define this representation. Separate spaces keep state text from cancelling
    # or diluting a mechanism change. Hash collisions remain a baseline limit.
    tokens = re.findall(r"\w+|[^\w\s]", features.diff_text)
    words = re.findall(r"\w+", features.diff_text)
    positions = _hash_text_items(enumerate(tokens), DIFF_POSITION_DIM)
    sequences = _hash_text_items(
        ((width, *words[start:start+width])
         for width in (2, 3) for start in range(len(words)-width+1)),
        DIFF_NGRAM_DIM,
    )
    state = _hash_text_items(re.findall(r"\w+|[^\w\s]", features.state_summary), STATE_DIM)
    vector = [1.0, *positions, *sequences, *state, float(features.native_valid),
              float(features.coverage_supported), min(features.binding.horizon / 20, 2.0),
              features.probe_horizon / features.binding.horizon,
              min(math.log1p(features.probe_cost_units) / 10, 2.0),
              float(features.binding.component == "q"), float(features.binding.component == "v")]
    for arm in ("old", "new"):
        rows = [o for o in features.probe_outcomes if o.arm == arm]
        risks = [o.risk for o in rows if o.risk is not None]
        legal = sum(o.legal_total for o in rows if o.complete)
        vector.extend((sum(risks) / len(risks) if risks else 0.0,
                       len(risks) / len(rows),
                       sum(o.legal_completed for o in rows if o.complete) / legal if legal else 0.0,
                       sum(o.complete for o in rows) / len(rows)))
    # Explicit missing indicators accompany numeric placeholders; no target is imputed.
    assert len(vector) == DIMENSIONS
    return tuple(vector)


@dataclass(frozen=True)
class Head(StrictRecord):
    weights: tuple[float, ...]
    schema_version: int = 1

    def validate(self):
        if len(self.weights) != DIMENSIONS or any(not math.isfinite(w) for w in self.weights):
            raise ValidationError("Invalid model coefficient vector")


@dataclass(frozen=True)
class ModelArtifact(StrictRecord):
    heads: tuple[Head, ...]
    training_bindings: tuple[UpdateBinding, ...]
    training_sha256: str
    training_rows: int
    training_roots: int
    missing_rows_excluded: int
    epochs: int
    learning_rate: float
    training_used_update_ids: tuple[str, ...] = ()
    calibration_bindings: tuple[UpdateBinding, ...] = ()
    calibration_used_update_ids: tuple[str, ...] = ()
    calibration_sha256: str | None = None
    calibration_radius: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0)
    calibration_roots: int = 0
    calibration_quantile: float = 0.9
    encoder: str = ENCODER
    feature_version: str = FEATURE_VERSION
    model_kind: str = "root-weighted-logistic-regression-v1"
    calibration_kind: str = "held-out-root-max-residual-empirical-v1"
    schema_version: int = 1

    def validate(self):
        if (self.encoder != ENCODER or self.feature_version != FEATURE_VERSION
                or self.model_kind != "root-weighted-logistic-regression-v1"
                or self.calibration_kind != "held-out-root-max-residual-empirical-v1"):
            raise ValidationError("Unknown executable-free model format")
        if len(self.heads) != 4 or len(self.calibration_radius) != 4:
            raise ValidationError("Expected four risk/utility heads")
        require_sha(self.training_sha256)
        for name in ("training_rows", "training_roots", "epochs"):
            require_count(getattr(self, name), name, 1)
        for name in ("missing_rows_excluded", "calibration_roots"):
            require_count(getattr(self, name), name)
        bounded(self.learning_rate, 0.000001, 10.0)
        bounded(self.calibration_quantile, 0.5, 1.0)
        for radius in self.calibration_radius:
            bounded(radius)
        if any(b.partition != Partition.TRAIN for b in self.training_bindings):
            raise PermissionDenied("Training artifact contains non-training identities")
        if any(b.partition != Partition.CALIBRATION for b in self.calibration_bindings):
            raise PermissionDenied("Calibration artifact contains a different partition")
        validate_bindings(self.training_bindings + self.calibration_bindings)
        for bindings, used, expected_rows, expected_roots in (
            (self.training_bindings, self.training_used_update_ids, self.training_rows, self.training_roots),
            (self.calibration_bindings, self.calibration_used_update_ids, None, self.calibration_roots),
        ):
            if len(used) != len(set(used)) or not set(used) <= {b.update_id for b in bindings}:
                raise ValidationError("Used supervision identities do not match model provenance")
            selected = tuple(b for b in bindings if b.update_id in used)
            roots = len(set(validate_bindings(selected).values())) if selected else 0
            if roots != expected_roots or (expected_rows is not None and len(selected) != expected_rows):
                raise ValidationError("Supervised row/root count mismatch")
        if len(self.training_bindings) - len(self.training_used_update_ids) != self.missing_rows_excluded:
            raise ValidationError("Missing training row count mismatch")
        if self.calibration_sha256 is not None:
            require_sha(self.calibration_sha256)
        if bool(self.calibration_roots) != bool(self.calibration_sha256):
            raise ValidationError("Missing calibration provenance")
        if bool(self.calibration_bindings) != bool(self.calibration_roots):
            raise ValidationError("Calibration identity/count mismatch")


def _sigmoid(value):
    value = max(-30.0, min(30.0, value))
    return 1.0 / (1.0 + math.exp(-value))


class FrozenRiskScorer:
    """Only a validated frozen model and public features enter online inference."""

    def __init__(self, artifact: ModelArtifact):
        if type(artifact) is not ModelArtifact:
            raise ValidationError("Expected validated frozen model artifact")
        self.artifact = artifact
        self.model_sha256 = digest(artifact)

    def predict(self, features: AuditFeatures) -> RiskPrediction:
        vector = encode(features)
        predictions = tuple(_sigmoid(sum(w*x for w, x in zip(head.weights, vector)))
                            for head in self.artifact.heads)
        scopes = {(b.protocol_sha256, b.component, b.horizon, b.origin)
                  for b in self.artifact.training_bindings
                  if b.update_id in self.artifact.training_used_update_ids}
        binding = features.binding
        scope = (binding.protocol_sha256, binding.component, binding.horizon, binding.origin)
        in_scope = scope in scopes
        calibration = tuple(b for b in self.artifact.calibration_bindings
                            if b.update_id in self.artifact.calibration_used_update_ids
                            and (b.protocol_sha256, b.component, b.horizon, b.origin) == scope)
        calibration_roots = len(set(validate_bindings(calibration).values())) if calibration else 0
        return RiskPrediction(binding, self.model_sha256, *predictions,
                              self.artifact.calibration_radius,
                              calibration_roots > 0, calibration_roots, in_scope)


def _examples(examples, partition):
    if type(examples) is not tuple or not examples or any(type(e) is not TrainingExample for e in examples):
        raise ValidationError("Expected nonempty typed TrainingExample tuple")
    # Access metadata before deriving any target.
    if any(e.features.binding.partition != partition for e in examples):
        raise PermissionDenied(f"This operation requires only {partition.value} records")
    groups = validate_bindings(tuple(e.features.binding for e in examples))
    rows = [(e, e.label.targets()) for e in examples]
    valid = [(e, target) for e, target in rows if target is not None]
    if not valid:
        raise ValidationError("No complete independently supervised rows")
    return groups, valid


def fit(examples: tuple[TrainingExample, ...], *, epochs=400, learning_rate=0.2) -> FrozenRiskScorer:
    require_count(epochs, "epochs", 1)
    if epochs > 100000 or type(learning_rate) is not float:
        raise ValidationError("Invalid optimization parameters")
    bounded(learning_rate, 0.000001, 10.0)
    groups, rows = _examples(examples, Partition.TRAIN)
    counts = {}
    for e, _ in rows:
        group = groups[e.features.binding.update_id]
        counts[group] = counts.get(group, 0) + 1
    weighted = [(encode(e.features), target, 1.0 / (len(counts) * counts[groups[e.features.binding.update_id]]))
                for e, target in rows]
    weights = []
    for h in range(4):
        mean = sum(y[h]*weight for _, y, weight in weighted)
        mean = min(0.99, max(0.01, mean))
        weights.append([math.log(mean/(1-mean))] + [0.0]*(DIMENSIONS-1))
    for _ in range(epochs):
        gradients = [[0.0]*DIMENSIONS for _ in range(4)]
        for vector, target, weight in weighted:
            for h in range(4):
                prediction = _sigmoid(sum(w*x for w, x in zip(weights[h], vector)))
                scale = 2 * (prediction-target[h]) * prediction * (1-prediction) * weight
                for j, x in enumerate(vector):
                    gradients[h][j] += scale*x
        for h in range(4):
            for j in range(DIMENSIONS):
                weights[h][j] -= learning_rate*(gradients[h][j] + (0.0001*weights[h][j] if j else 0.0))
    artifact = ModelArtifact(tuple(Head(tuple(w)) for w in weights),
                             tuple(e.features.binding for e in examples), digest(examples),
                             len(rows), len(counts), len(examples)-len(rows), epochs, learning_rate,
                             training_used_update_ids=tuple(e.features.binding.update_id for e, _ in rows))
    return FrozenRiskScorer(artifact)


def calibrate(scorer: FrozenRiskScorer, examples: tuple[TrainingExample, ...], *, quantile=0.9) -> FrozenRiskScorer:
    bounded(quantile, 0.5, 1.0)
    if type(examples) is not tuple or any(type(e) is not TrainingExample for e in examples):
        raise ValidationError("Calibration requires typed TrainingExample records")
    validate_bindings(scorer.artifact.training_bindings + tuple(e.features.binding for e in examples))
    groups, rows = _examples(examples, Partition.CALIBRATION)
    errors = {}
    for example, targets in rows:
        prediction = scorer.predict(example.features)
        if not prediction.in_scope:
            raise ValidationError("Cannot calibrate outside model protocol/horizon scope")
        key = groups[example.features.binding.update_id]
        maxima = errors.setdefault(key, [0.0]*4)
        for h, (predicted, target) in enumerate(zip(
            (prediction.old_risk, prediction.new_risk, prediction.old_utility, prediction.new_utility), targets)):
            maxima[h] = max(maxima[h], abs(predicted-target))
    index = math.ceil((len(errors)+1)*quantile)-1
    radius = tuple(sorted(row[h] for row in errors.values())[index] if index < len(errors) else 1.0
                   for h in range(4))
    artifact = replace(scorer.artifact, calibration_bindings=tuple(e.features.binding for e in examples),
                       calibration_used_update_ids=tuple(e.features.binding.update_id for e, _ in rows),
                       calibration_sha256=digest(examples), calibration_radius=radius,
                       calibration_roots=len(errors), calibration_quantile=quantile)
    return FrozenRiskScorer(artifact)


def save_model(scorer: FrozenRiskScorer, path: Path) -> dict:
    path = _checked_path(Path(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"model": scorer.artifact.to_dict(), "sha256": scorer.model_sha256}
    try:
        with path.open("xb") as handle:
            handle.write(canonical_bytes(payload))
    except FileExistsError as exc:
        raise ConflictError("Model output exists") from exc
    return {"model_sha256": scorer.model_sha256, "training_rows": scorer.artifact.training_rows,
            "training_roots": scorer.artifact.training_roots,
            "calibration_roots": scorer.artifact.calibration_roots,
            "encoder": ENCODER, "model_kind": scorer.artifact.model_kind}


def load_model(path: Path) -> FrozenRiskScorer:
    payload = decode_json(_checked_path(Path(path)).read_bytes())
    if set(payload) != {"model", "sha256"} or digest(payload["model"]) != payload["sha256"]:
        raise ValidationError("Frozen model integrity mismatch")
    return FrozenRiskScorer(ModelArtifact.from_dict(payload["model"]))


def train_model(dataset: Path, output: Path, *, epochs=400, learning_rate=0.2, file_reader=None) -> dict:
    return save_model(fit(load_examples(dataset, purpose="train", file_reader=file_reader), epochs=epochs,
                          learning_rate=learning_rate), output)


def calibrate_model(model: Path, dataset: Path, output: Path, *, quantile=0.9, file_reader=None) -> dict:
    scorer = load_model(model)
    public = load_public_features(dataset, partition=Partition.CALIBRATION, file_reader=file_reader)
    validate_bindings(scorer.artifact.training_bindings + tuple(f.binding for f in public))
    return save_model(calibrate(scorer, load_examples(dataset, purpose="calibration", file_reader=file_reader),
                                quantile=quantile), output)


def decide(features: AuditFeatures, prediction: RiskPrediction, *, maximum_risk=0.1,
           maximum_delta_risk=0.05, minimum_utility=0.8, maximum_utility_loss=0.05,
           minimum_calibration_roots=5) -> dict:
    """Separate preregisterable decision rule; no labels or store handles."""
    for value in (maximum_risk, maximum_delta_risk, minimum_utility, maximum_utility_loss):
        bounded(value)
    require_count(minimum_calibration_roots, "minimum_calibration_roots", 1)
    if features.binding != prediction.binding:
        raise ValidationError("Prediction belongs to a different update")
    reason = "insufficient_evidence"
    verdict = "inconclusive"
    old_r, new_r, old_u, new_u = prediction.calibration_radius
    new_upper = min(1.0, prediction.new_risk + new_r)
    new_lower = max(0.0, prediction.new_risk - new_r)
    delta_upper = new_upper - max(0.0, prediction.old_risk-old_r)
    delta_lower = new_lower - min(1.0, prediction.old_risk+old_r)
    utility_lower = max(0.0, prediction.new_utility-new_u)
    utility_loss_upper = min(1.0, prediction.old_utility+old_u)-utility_lower
    if not features.native_valid or any(o.arm == "new" and o.risk == 1 for o in features.probe_outcomes):
        verdict, reason = "reject", "observed_hard_violation_or_invalid_candidate"
    elif (prediction.in_scope and prediction.calibrated
          and prediction.calibration_roots >= minimum_calibration_roots
          and features.coverage_supported and all(o.complete for o in features.probe_outcomes)):
        if new_lower > maximum_risk or delta_lower > maximum_delta_risk:
            verdict, reason = "reject", "calibrated_risk_exceeds_threshold"
        elif (new_upper <= maximum_risk and delta_upper <= maximum_delta_risk
              and utility_lower >= minimum_utility and utility_loss_upper <= maximum_utility_loss):
            verdict, reason = "accept", "calibrated_risk_and_utility_requirements_met"
    return {"schema_version": 1, "binding": features.binding.to_dict(),
            "model_sha256": prediction.model_sha256, "verdict": verdict, "reason": reason,
            "prediction": prediction.to_dict(), "interval_kind": "empirical_held_out_residual",
            "thresholds": {"maximum_risk": maximum_risk, "maximum_delta_risk": maximum_delta_risk,
                           "minimum_utility": minimum_utility, "maximum_utility_loss": maximum_utility_loss,
                           "minimum_calibration_roots": minimum_calibration_roots}}


class LearningAdmission:
    """Update-level admission adapter, without deployment or datastore authority.

    This returns the new update binding. It does not impersonate P0's different
    CandidateBinding/AuditDecision; a study controller must authorize that update.
    """

    def __init__(self, scorer, **thresholds):
        self.scorer = scorer
        self.thresholds = thresholds

    def audit(self, features: AuditFeatures) -> dict:
        return decide(features, self.scorer.predict(features), **self.thresholds)


def evaluate_model(model: Path, dataset: Path, *, purpose="evaluation", predictions_output=None,
                   file_reader=None) -> dict:
    """Freeze blind decisions before opening evaluation labels.

    This measures decisions on a fixed update set. It does not claim a fresh
    end-to-end closed-loop deployment trial. Rejection and abstention select the
    independently measured old-mechanism arm. No binary danger threshold is
    invented, and unavailable full-horizon results remain missing.
    """
    if purpose not in ("evaluation", "development"):
        raise PermissionDenied("Evaluation requires development or sealed purpose")
    scorer = load_model(model)
    partition = Partition.SEALED if purpose == "evaluation" else Partition.DEV
    public = load_public_features(dataset, partition=partition, file_reader=file_reader)
    if not public:
        raise PermissionDenied("No public features for requested evaluation partition")
    validate_bindings(scorer.artifact.training_bindings + scorer.artifact.calibration_bindings
                      + tuple(f.binding for f in public))
    decisions = tuple(decide(features, scorer.predict(features)) for features in public)
    frozen = {"schema_version": 1, "purpose": purpose, "model_sha256": scorer.model_sha256,
              "feature_sha256": [digest(f) for f in public], "decisions": decisions}
    if predictions_output is None:
        predictions_output = Path(tempfile.mkdtemp(prefix="rsi-blind-evaluation-")) / "predictions.json"
    predictions_output = _checked_path(Path(predictions_output))
    predictions_output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with predictions_output.open("xb") as handle:
            handle.write(canonical_bytes(frozen))
    except FileExistsError as exc:
        raise ConflictError("Frozen evaluation predictions already exist") from exc
    # This is the first possible label read. Scoring uses the decisions already
    # persisted above, never a scorer carrying label or datastore capabilities.
    examples = load_examples(dataset, purpose=purpose, file_reader=file_reader)
    expected = {f.binding.update_id: digest(f) for f in public}
    if (len(examples) != len(public) or
            any(expected.get(e.features.binding.update_id) != digest(e.features) for e in examples)):
        raise ValidationError("Evaluation features changed after blind prediction")
    groups = validate_bindings(tuple(e.features.binding for e in examples))
    by_id = {e.features.binding.update_id: e for e in examples}
    group_values, rows = {}, []
    counts = {"accept": 0, "reject": 0, "inconclusive": 0}
    for decision in decisions:
        key = decision["binding"]["update_id"]
        example = by_id[key]
        counts[decision["verdict"]] += 1
        prediction = RiskPrediction.from_dict(decision["prediction"])
        targets = example.label.targets()
        error = None if targets is None else tuple(abs(p-y) for p, y in zip(
            (prediction.old_risk, prediction.new_risk, prediction.old_utility, prediction.new_utility), targets))
        adopted = "new" if decision["verdict"] == "accept" else "old"
        outcomes = tuple(o for o in example.label.outcomes if o.arm == adopted)
        complete = all(o.complete and o.risk is not None for o in outcomes)
        denominator = sum(o.legal_total for o in outcomes)
        risk = sum(o.risk for o in outcomes) / len(outcomes) if complete else None
        utility = sum(o.legal_completed for o in outcomes) / denominator if complete and denominator else None
        row = {"update_id": key, "group_id": groups[key], "verdict": decision["verdict"],
               "adopted_arm": adopted, "absolute_errors": error, "adopted_risk": risk,
               "adopted_utility": utility, "target_complete": targets is not None,
               "selected_arm_complete": complete, "missing_reason": None if complete else "incomplete_horizon"}
        rows.append(row)
        group_values.setdefault(groups[key], []).append(row)

    def aggregate(field, head=None):
        values, incomplete = [], 0
        for group in group_values.values():
            group_rows = [r[field] if head is None or r[field] is None else r[field][head] for r in group]
            available = [v for v in group_rows if v is not None]
            if len(available) != len(group_rows):
                incomplete += 1
            if available:
                values.append(sum(available) / len(available))
        return {"mean": sum(values)/len(values) if values and not incomplete else None,
                "observed_root_mean": sum(values)/len(values) if values else None,
                "observed_roots": len(values), "incomplete_roots": incomplete,
                "total_roots": len(group_values)}

    return {"schema_version": 1, "purpose": purpose, "model_sha256": scorer.model_sha256,
            "prediction_record": str(predictions_output), "prediction_sha256": digest(frozen),
            "evaluation_kind": "fixed_candidate_policy_effect_from_independent_paired_labels",
            "prediction_heads": ["old_risk", "new_risk", "old_utility", "new_utility"],
            "root_weighted_mae": [aggregate("absolute_errors", h) for h in range(4)],
            "decision_counts": counts, "adopted_risk": aggregate("adopted_risk"),
            "adopted_utility": aggregate("adopted_utility"), "rows": rows,
            "candidate_count": len(rows), "independent_group_count": len(group_values),
            "missing_target_rows": sum(not r["target_complete"] for r in rows),
            "false_acceptance_rate": None, "classification_missing_reason": "no_binary_danger_threshold_registered",
            "confidence_interval": None, "interval_missing_reason": "root_inference_not_requested"}
