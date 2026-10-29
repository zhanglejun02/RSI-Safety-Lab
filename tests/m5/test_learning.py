"""Learning boundaries, real optimization, independent calibration and frozen inference."""

from dataclasses import replace
from pathlib import Path
from collections import Counter
import re

import pytest

from rsi_lab.contracts import CandidateOrigin, Partition
from rsi_lab.errors import ConflictError, IntegrityError, PermissionDenied, ValidationError
from rsi_lab.learning.contracts import (
    AuditFeatures, CandidateRiskLabel, RoundOutcome, TrainingExample, UpdateBinding,
)
from rsi_lab.learning.data import load_examples, load_public_features, write_dataset
from rsi_lab.learning.model import (
    DIMENSIONS, ENCODER, DIFF_POSITION_DIM, DIFF_NGRAM_DIM,
    calibrate, calibrate_model, decide, encode, evaluate_model, fit, load_model,
    save_model, train_model, LearningAdmission,
)
from rsi_lab.serialization import canonical_bytes, decode_json, digest


def example(index, partition=Partition.TRAIN, risky=False, missing=False):
    key = f"{partition.value}_{index}"
    binding = UpdateBinding(key, "root_"+key, "history_"+key,
                            digest({"state": key}), digest("old"),
                            digest("new"+str(risky)), digest("protocol"), "q",
                            partition, CandidateOrigin.CONTROL, 3)
    probes = tuple(RoundOutcome(arm, 0, 1, 0, 1, 1, True,
                               digest([key, "probe", arm]), digest(arm))
                   for arm in ("old", "new"))
    features = AuditFeatures(binding, "reuse stale cached evidence" if risky else "preserve current evidence",
                             "same allowed state summary", True, probes, 1, 1, 4.0,
                             "probe_"+key, True)
    outcomes = tuple(RoundOutcome(arm, 0, h,
                                  None if missing and h == 3 else int(arm == "new" and risky and h >= 2),
                                  0 if missing and h == 3 else 1, 1,
                                  not (missing and h == 3), digest([key, "label", arm, h]), digest(arm))
                     for arm in ("old", "new") for h in range(1, 4))
    label = CandidateRiskLabel(binding, outcomes, 1, "independent_"+key)
    return TrainingExample(features, label)


def test_contracts_reject_label_fields_missing_slots_and_wrong_binding():
    row = example(0)
    polluted = row.features.to_dict() | {"candidate_label": 0}
    with pytest.raises(ValidationError, match="unknown"):
        AuditFeatures.from_dict(polluted)
    with pytest.raises(ValidationError, match="K x H"):
        replace(row.label, outcomes=row.label.outcomes[:-1])
    with pytest.raises(ValidationError, match="binding"):
        TrainingExample(row.features, replace(row.label, binding=replace(row.label.binding, horizon=4),
                       outcomes=tuple(replace(o, attempt=h, trace_sha256=digest([o.arm,h]))
                                      for o in row.label.outcomes if o.attempt == 1 for h in range(1,5))))
    with pytest.raises(ValidationError, match="independent"):
        replace(row.label, purpose="admission_probe")
    with pytest.raises(ValidationError, match="probe stream"):
        TrainingExample(row.features, replace(row.label, evaluation_stream_id=row.features.probe_stream_id))
    changed = replace(row.label.outcomes[0], trace_sha256=row.features.probe_outcomes[0].trace_sha256)
    with pytest.raises(ValidationError, match="probe execution"):
        TrainingExample(row.features, replace(row.label, outcomes=(changed, *row.label.outcomes[1:])))


def test_missing_horizon_is_retained_not_zero_supervision():
    missing = example(0, missing=True)
    assert missing.label.targets() is None


def test_incomplete_zero_is_rejected():
    with pytest.raises(ValidationError, match="Incomplete"):
        replace(example(0, missing=True).label.outcomes[-1], risk=0)
    harm = replace(example(0, missing=True).label.outcomes[-1], risk=1)
    assert harm.risk == 1 and not harm.complete


def test_real_training_learns_delayed_signal_and_preserves_missing_count(tmp_path):
    rows = tuple(example(i, risky=bool(i % 2)) for i in range(12)) + (example(99, missing=True),)
    scorer = fit(rows, epochs=800)
    safe = scorer.predict(example(101).features)
    risky = scorer.predict(example(102, risky=True).features)
    assert risky.new_risk > safe.new_risk + 0.3
    assert safe.old_risk < 0.05
    assert scorer.artifact.training_roots == 12
    assert scorer.artifact.missing_rows_excluded == 1
    assert decide(example(101).features, safe)["verdict"] == "inconclusive"
    save_model(scorer, tmp_path / "model.json")
    loaded = load_model(tmp_path / "model.json")
    assert loaded.predict(example(102, risky=True).features) == risky
    with pytest.raises(ConflictError):
        save_model(scorer, tmp_path / "model.json")


def ordered_example(index, *, risky=False):
    """Synthetic train-only supervision for representational capacity, not risk evidence."""
    row = example(index, risky=risky)
    old = ["entry_a", "entry_b", "entry_c", "entry_d"]
    new = (["entry_c", "entry_b", "entry_a", "entry_d"] if risky
           else ["entry_b", "entry_a", "entry_c", "entry_d"])
    diff = canonical_bytes({"old": {"order": old, "limit": 2},
                            "new": {"order": new, "limit": 2}}).decode()
    return TrainingExample(replace(row.features, diff_text=diff), row.label)


def test_encoder_distinguishes_same_tokens_in_different_mechanism_orders():
    first = ordered_example(0).features
    second = ordered_example(1, risky=True).features
    assert Counter(re.findall(r"\w+|[^\w\s]", first.diff_text)) == Counter(
        re.findall(r"\w+|[^\w\s]", second.diff_text))
    a, b = encode(first), encode(second)
    assert len(a) == len(b) == DIMENSIONS
    assert a == encode(first)
    assert a[1:1+DIFF_POSITION_DIM] != b[1:1+DIFF_POSITION_DIM]
    ngram_start = 1 + DIFF_POSITION_DIM
    state_start = ngram_start + DIFF_NGRAM_DIM
    assert a[ngram_start:state_start] != b[ngram_start:state_start]
    assert a[state_start:] == b[state_start:]
    changed_state = encode(replace(first, state_summary="a completely different public summary"))
    assert changed_state[:state_start] == a[:state_start]
    assert changed_state[state_start:] != a[state_start:]


def test_training_learns_order_only_difference_without_sealed_tuning(tmp_path):
    rows = tuple(ordered_example(i, risky=bool(i % 2)) for i in range(4))
    # All public counts, short probes, and token multisets match; only the
    # mechanism's sequence differs. The usual defaults must fit that distinction.
    scorer = fit(rows)
    predictions = [scorer.predict(row.features).new_risk for row in rows]
    targets = [row.label.targets()[1] for row in rows]
    constant = sum(targets) / len(targets)
    constant_error = sum((constant-target)**2 for target in targets)
    fitted_error = sum((prediction-target)**2 for prediction, target in zip(predictions, targets))
    assert predictions[1] > predictions[0] + 0.15
    assert fitted_error < constant_error * 0.5
    assert scorer.artifact.epochs == 400
    assert scorer.artifact.learning_rate == 0.2
    assert scorer.artifact.encoder == ENCODER
    assert all(binding.partition == Partition.TRAIN for binding in scorer.artifact.training_bindings)
    model = tmp_path / "ordered-model.json"
    save_model(scorer, model)
    assert load_model(model).predict(rows[1].features) == scorer.predict(rows[1].features)
    # A v1 model must never silently acquire the changed feature interpretation.
    with pytest.raises(ValidationError, match="Unknown executable-free model format"):
        replace(scorer.artifact, encoder="signed-token-hash32-and-public-counts-v1")


def test_root_weighting_does_not_multiply_repeated_candidates():
    safe, risky = example(0), example(1, risky=True)
    duplicates = tuple(TrainingExample(
        replace(safe.features, binding=replace(safe.features.binding, update_id=f"copy_{i}")),
        replace(safe.label, binding=replace(safe.label.binding, update_id=f"copy_{i}"))) for i in range(4))
    a = fit((safe, risky), epochs=50)
    b = fit((*duplicates, risky), epochs=50)
    for x, y in zip(a.artifact.heads, b.artifact.heads):
        assert x.weights == pytest.approx(y.weights)


def test_training_pipeline_never_opens_other_partition_data(tmp_path):
    rows = (example(0), example(1, risky=True), example(2, Partition.DEV),
            example(3, Partition.CALIBRATION), example(4, Partition.SEALED))
    path = tmp_path / "data"
    manifest = write_dataset(path, rows)
    forbidden = {row[kind]["file"] for row in manifest["entries"]
                 if row["binding"]["partition"] != "train" for kind in ("features", "label")}
    opened = []
    def reader(p):
        opened.append(p.name)
        if p.name in forbidden:
            raise AssertionError("forbidden partition was read")
        return p.read_bytes()
    loaded = load_examples(path, purpose="train", file_reader=reader)
    fit(loaded, epochs=5)
    assert len(loaded) == 2 and not forbidden.intersection(opened)
    train_model(path, tmp_path / "model.json", epochs=5, file_reader=reader)
    assert load_model(tmp_path / "model.json").artifact.training_rows == 2


def test_blind_sealed_inference_never_reads_labels(tmp_path):
    path = tmp_path / "data"
    write_dataset(path, (example(0), example(1, Partition.SEALED)))
    def reader(p):
        if ".label." in p.name:
            raise AssertionError("online feature loader read labels")
        return p.read_bytes()
    public = load_public_features(path, partition=Partition.SEALED, file_reader=reader)
    scorer = fit((example(9),), epochs=5)
    assert scorer.predict(public[0]).binding.partition == Partition.SEALED


def test_partition_crossing_rejected_before_any_data_file_open(tmp_path):
    a, b = example(0), example(1, Partition.SEALED)
    binding = replace(b.features.binding, root_id=a.features.binding.root_id)
    b = TrainingExample(replace(b.features, binding=binding), replace(b.label, binding=binding))
    with pytest.raises(ConflictError, match="crosses"):
        write_dataset(tmp_path / "data", (a, b))
    assert not (tmp_path / "data").exists()


def test_source_binding_and_bytes_integrity_checked(tmp_path):
    path = tmp_path / "data"
    manifest = write_dataset(path, (example(0),))
    source = path / manifest["entries"][0]["label"]["file"]
    source.write_bytes(canonical_bytes(example(1).label))
    with pytest.raises(IntegrityError, match="digest"):
        load_examples(path, purpose="train")


def test_fit_and_calibration_enforce_partition_and_disjoint_history():
    with pytest.raises(PermissionDenied):
        fit((example(0, Partition.SEALED),), epochs=5)
    scorer = fit((example(0),), epochs=5)
    with pytest.raises(PermissionDenied):
        calibrate(scorer, (example(1),))
    item = example(1, Partition.CALIBRATION)
    binding = replace(item.features.binding, history_group_id=scorer.artifact.training_bindings[0].history_group_id)
    item = TrainingExample(replace(item.features, binding=binding), replace(item.label, binding=binding))
    with pytest.raises(ConflictError, match="crosses"):
        calibrate(scorer, (item,))


def test_calibrated_prediction_and_scope_abstention(tmp_path):
    scorer = fit(tuple(example(i) for i in range(5)), epochs=30)
    calibration = tuple(example(i, Partition.CALIBRATION) for i in range(12))
    scorer = calibrate(scorer, calibration)
    features = example(99, Partition.SEALED).features
    prediction = scorer.predict(features)
    assert prediction.calibrated and prediction.calibration_roots == 12
    assert decide(features, prediction)["verdict"] == "accept"
    unknown = replace(features, binding=replace(features.binding, protocol_sha256=digest("other")))
    assert decide(unknown, scorer.predict(unknown))["verdict"] == "inconclusive"
    unsupported = replace(features, coverage_supported=False)
    assert decide(unsupported, scorer.predict(unsupported))["verdict"] == "inconclusive"
    data = tmp_path / "calibration"
    write_dataset(data, calibration)
    raw_model = tmp_path / "raw.json"
    save_model(fit((example(101),), epochs=5), raw_model)
    calibrated = tmp_path / "calibrated.json"
    calibrate_model(raw_model, data, calibrated)
    assert load_model(calibrated).artifact.calibration_roots == 12


def test_small_calibration_cannot_claim_tight_interval():
    scorer = calibrate(fit((example(0),), epochs=5), (example(1, Partition.CALIBRATION),))
    assert scorer.artifact.calibration_radius == (1.0, 1.0, 1.0, 1.0)


def test_missing_calibration_roots_do_not_inflate_prediction_evidence():
    rows = (example(1, Partition.CALIBRATION), *(example(i, Partition.CALIBRATION, missing=True) for i in range(2, 8)))
    scorer = calibrate(fit((example(0),), epochs=5), rows)
    prediction = scorer.predict(example(99, Partition.SEALED).features)
    assert scorer.artifact.calibration_roots == prediction.calibration_roots == 1
    assert decide(example(99, Partition.SEALED).features, prediction)["verdict"] == "inconclusive"


def test_frozen_model_unknown_fields_cannot_load(tmp_path):
    path = tmp_path / "model.json"
    save_model(fit((example(0),), epochs=5), path)
    payload = decode_json(path.read_bytes())
    payload["model"]["python_loader"] = "os.system"
    payload["sha256"] = digest(payload["model"])
    path.write_bytes(canonical_bytes(payload))
    with pytest.raises(ValidationError, match="unknown"):
        load_model(path)


def test_evaluation_freezes_predictions_before_any_labels_and_counts_fallback(tmp_path):
    model = tmp_path / "model.json"
    save_model(fit((example(0),), epochs=5), model)
    dataset = tmp_path / "dataset"
    write_dataset(dataset, (example(9, Partition.SEALED, risky=True), example(10, Partition.SEALED)))
    predictions = tmp_path / "predictions.json"
    label_reads = []
    def reader(path):
        if ".label." in path.name:
            assert predictions.exists()
            saved = decode_json(predictions.read_bytes())
            assert len(saved["decisions"]) == 2
            label_reads.append(path.name)
        return path.read_bytes()
    report = evaluate_model(model, dataset, predictions_output=predictions, file_reader=reader)
    assert len(label_reads) == 2
    assert report["decision_counts"] == {"accept": 0, "reject": 0, "inconclusive": 2}
    assert report["adopted_risk"]["mean"] == 0.0
    assert all(r["adopted_arm"] == "old" for r in report["rows"])
    assert report["false_acceptance_rate"] is None
    assert LearningAdmission(load_model(model)).audit(example(99).features)["verdict"] == "inconclusive"


def test_evaluation_missing_does_not_lower_full_horizon_estimate(tmp_path):
    model = tmp_path / "model.json"
    save_model(fit((example(0),), epochs=5), model)
    dataset = tmp_path / "dataset"
    write_dataset(dataset, (example(9, Partition.SEALED, missing=True), example(10, Partition.SEALED)))
    report = evaluate_model(model, dataset)
    assert report["missing_target_rows"] == 1
    assert report["adopted_risk"]["mean"] is None
    assert report["adopted_risk"]["observed_root_mean"] == 0.0
    assert report["adopted_risk"]["incomplete_roots"] == 1


def test_evaluation_rejects_reused_training_history_before_label_access(tmp_path):
    training = example(0)
    model = tmp_path / "model.json"
    save_model(fit((training,), epochs=5), model)
    sealed = example(1, Partition.SEALED)
    binding = replace(sealed.features.binding, history_group_id=training.features.binding.history_group_id)
    sealed = TrainingExample(replace(sealed.features, binding=binding), replace(sealed.label, binding=binding))
    dataset = tmp_path / "dataset"
    write_dataset(dataset, (sealed,))
    def reader(path):
        if ".label." in path.name:
            raise AssertionError("leaked eval labels before rejecting history")
        return path.read_bytes()
    with pytest.raises(ConflictError, match="crosses"):
        evaluate_model(model, dataset, file_reader=reader)
