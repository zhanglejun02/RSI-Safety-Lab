"""Actual platform evidence reaches training without weakening clone separation."""
from dataclasses import replace

import pytest

from rsi_lab.api import PreparedUpdate
from rsi_lab.auditing.platform import learning_binding
from rsi_lab.contracts import Partition
from rsi_lab.engine.continuation import read_record
from rsi_lab.errors import ConflictError
from rsi_lab.learning.contracts import AuditFeatures, CandidateRiskLabel, TrainingExample
from rsi_lab.learning.data import load_examples, validate_bindings, write_dataset
from rsi_lab.learning.model import calibrate_model, load_model, train_model
from rsi_lab.platform_config import PlatformConfig, ProtocolConfig
from rsi_lab.studies import platform


@pytest.fixture
def collected(tmp_path, monkeypatch):
    # Prevent unrelated parallel source edits from changing this test's protocol
    # between its partitions. Production fingerprint rejection has separate tests.
    monkeypatch.setattr(platform, "implementation_fingerprint", lambda: "1" * 64)
    runs, prepared, examples = [], [], []
    for partition, seed in ((Partition.TRAIN, 110), (Partition.CALIBRATION, 210)):
        run = tmp_path / partition.value
        config = PlatformConfig(protocol=ProtocolConfig(
            partition=partition, seed=seed, horizon=3))
        result = platform.create_run(config, run)
        assert result["engineering_control"] is True
        assert result["label_targets"] == (0.0, 1 / 3, 1.0, 1.0)
        runs.append(run)
        prepared.append(PreparedUpdate.from_dict(read_record(run / "prepared.json")))
        examples.append(TrainingExample(
            AuditFeatures.from_dict(read_record(run / "features.json")),
            CandidateRiskLabel.from_dict(read_record(run / "label.json"))))
    return runs, prepared, examples


def test_platform_actual_train_calibration_pipeline_uses_future_task_anchor(tmp_path, collected):
    runs, prepared, examples = collected
    train, calibration = (row.features.binding for row in examples)
    # Empty current worlds and identical system updates are valid across different
    # future task parameters; root labels alone must never provide the separation.
    assert prepared[0].base == prepared[1].base
    assert prepared[0].draft.candidate == prepared[1].draft.candidate
    assert prepared[0].identity.environment_sha256 == prepared[1].identity.environment_sha256
    assert prepared[0].identity.task_plan_sha256 != prepared[1].identity.task_plan_sha256
    assert train.state_sha256 != calibration.state_sha256
    assert train.protocol_sha256 == calibration.protocol_sha256

    dataset = tmp_path / "dataset"
    manifest = platform.export_dataset(runs, dataset)
    assert manifest["rows"] == manifest["supervised_rows"] == 2
    assert len(load_examples(dataset, purpose="train")) == 1
    assert len(load_examples(dataset, purpose="calibration")) == 1
    trained_path = tmp_path / "trained.json"
    calibrated_path = tmp_path / "calibrated.json"
    train_model(dataset, trained_path, epochs=20)
    calibrate_model(trained_path, dataset, calibrated_path)
    trained, calibrated = load_model(trained_path), load_model(calibrated_path)
    assert trained.artifact.training_roots == 1
    assert calibrated.artifact.training_bindings == trained.artifact.training_bindings
    assert calibrated.artifact.calibration_roots == 1
    assert calibrated.artifact.calibration_bindings == (calibration,)
    prediction = calibrated.predict(examples[1].features)
    assert prediction.binding == calibration and prediction.in_scope
    # This checks plumbing only. One calibration root cannot claim tight bounds.
    assert prediction.calibration_radius == (1.0, 1.0, 1.0, 1.0)


def test_relabelling_same_world_and_future_plan_still_rejects_exact_clone(tmp_path, collected):
    _, prepared, examples = collected
    original = prepared[0].identity
    renamed = replace(original, update_id="renamed-update", root_id="renamed-root",
                      history_group_id="renamed-history", partition=Partition.CALIBRATION)
    before, after = learning_binding(original, "agent"), learning_binding(renamed, "agent")
    assert original.task_plan_sha256 == renamed.task_plan_sha256
    assert before.state_sha256 == after.state_sha256
    assert before.new_mechanism_sha256 == after.new_mechanism_sha256
    with pytest.raises(ConflictError, match="exact clone"):
        validate_bindings((before, after))
    example = examples[0]
    clone = TrainingExample(replace(example.features, binding=after),
                           replace(example.label, binding=after))
    with pytest.raises(ConflictError, match="exact clone"):
        write_dataset(tmp_path / "must-not-exist", (example, clone))
    assert not (tmp_path / "must-not-exist").exists()
