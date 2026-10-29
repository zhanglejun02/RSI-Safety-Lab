"""Real task execution through the offline learning and blind evaluation path.

The scripted provider is an engineering control. These small local runs check
the evidence plumbing and never stand in for natural-model paper results.
"""

from rsi_lab.contracts import CandidateOrigin, Partition
from rsi_lab.learning.contracts import FEATURE_VERSION
from rsi_lab.learning.model import (
    calibrate_model,
    evaluate_model,
    load_model,
    train_model,
)
from rsi_lab.serialization import decode_json, digest
from rsi_lab.studies.task_a import TaskAConfig, create_run, export_dataset


def test_collected_task_a_trains_calibrates_and_freezes_blind_evaluation(tmp_path):
    sizes = {Partition.TRAIN: 2, Partition.CALIBRATION: 10, Partition.SEALED: 2}
    seeds = {Partition.TRAIN: 100, Partition.CALIBRATION: 200, Partition.SEALED: 300}
    runs = []
    protocols = []
    root_sets = {}
    for partition, roots in sizes.items():
        run = tmp_path / partition.value
        config = TaskAConfig(partition=partition, roots=roots, seed=seeds[partition])
        summary = create_run(config, run)
        assert summary["engineering_control"] is True
        assert summary["candidate_origin"] == CandidateOrigin.CONTROL.value
        assert summary["registered_roots"] == summary["measured_roots"] == roots
        assert all(row["status"] == "measured" for row in summary["roots"])
        root_sets[partition] = {row["root_id"] for row in summary["roots"]}
        protocols.append(decode_json((run / "manifest.json").read_bytes())["protocol"])
        runs.append(run)

    assert protocols[0] == protocols[1] == protocols[2]
    assert sum(map(len, root_sets.values())) == len(set().union(*root_sets.values())) == 14

    # Export is a trusted offline join. Purpose-limited reads start below.
    dataset = tmp_path / "dataset"
    manifest = export_dataset(runs, dataset)
    assert manifest["rows"] == manifest["supervised_rows"] == 14
    assert manifest["missing_rows"] == 0
    by_file = {
        entry[kind]["file"]: (Partition(entry["binding"]["partition"]), kind)
        for entry in manifest["entries"]
        for kind in ("features", "label")
    }
    for entry in manifest["entries"]:
        binding = entry["binding"]
        partition = Partition(binding["partition"])
        assert binding["root_id"] in root_sets[partition]
        assert binding["protocol_sha256"] == digest(protocols[0])
        assert binding["origin"] == CandidateOrigin.CONTROL.value

    accesses = []

    def reader_for(partition):
        def read(path):
            if path.name != "manifest.json":
                selected_partition, kind = by_file[path.name]
                assert selected_partition == partition, "stage accessed a different partition"
                accesses.append((partition, kind, path.name))
            return path.read_bytes()

        return read

    trained_path = tmp_path / "trained.json"
    train_model(dataset, trained_path, epochs=100, file_reader=reader_for(Partition.TRAIN))
    trained = load_model(trained_path)
    assert trained.artifact.training_rows == trained.artifact.training_roots == 2
    assert trained.artifact.feature_version == FEATURE_VERSION
    assert trained.artifact.training_sha256
    assert trained.artifact.calibration_roots == 0
    assert {b.root_id for b in trained.artifact.training_bindings} == root_sets[Partition.TRAIN]
    assert all(b.partition == Partition.TRAIN for b in trained.artifact.training_bindings)
    assert sum(kind == "label" for _, kind, _ in accesses) == 2

    calibrated_path = tmp_path / "calibrated.json"
    calibrate_model(trained_path, dataset, calibrated_path,
                    file_reader=reader_for(Partition.CALIBRATION))
    calibrated = load_model(calibrated_path)
    assert calibrated.artifact.training_sha256 == trained.artifact.training_sha256
    assert calibrated.artifact.training_bindings == trained.artifact.training_bindings
    assert calibrated.artifact.calibration_sha256
    assert calibrated.artifact.calibration_roots == 10
    assert len(calibrated.artifact.calibration_used_update_ids) == 10
    assert {b.root_id for b in calibrated.artifact.calibration_bindings} == root_sets[Partition.CALIBRATION]
    assert all(b.partition == Partition.CALIBRATION for b in calibrated.artifact.calibration_bindings)
    assert not any(partition == Partition.SEALED for partition, _, _ in accesses)

    predictions_path = tmp_path / "blind-predictions.json"
    frozen_before_labels = []
    sealed_reader = reader_for(Partition.SEALED)

    def blind_reader(path):
        if path.name.endswith(".label.json"):
            assert predictions_path.exists(), "sealed labels opened before decisions persisted"
            frozen_bytes = predictions_path.read_bytes()
            frozen = decode_json(frozen_bytes)
            assert len(frozen["decisions"]) == sizes[Partition.SEALED]
            assert frozen["model_sha256"] == calibrated.model_sha256
            assert all(d["binding"]["partition"] == Partition.SEALED.value
                       for d in frozen["decisions"])
            assert all(d["prediction"]["calibration_roots"] == 10
                       for d in frozen["decisions"])
            frozen_before_labels.append(frozen_bytes)
        return sealed_reader(path)

    report = evaluate_model(calibrated_path, dataset, predictions_output=predictions_path,
                            file_reader=blind_reader)
    assert len(frozen_before_labels) == 2
    assert all(value == predictions_path.read_bytes() for value in frozen_before_labels)
    frozen = decode_json(predictions_path.read_bytes())
    assert report["prediction_sha256"] == digest(frozen)
    assert report["model_sha256"] == calibrated.model_sha256
    assert report["candidate_count"] == report["independent_group_count"] == 2
    assert report["missing_target_rows"] == 0
    assert sum(report["decision_counts"].values()) == 2
    assert set(report["decision_counts"]) == {"accept", "reject", "inconclusive"}
    assert report["false_acceptance_rate"] is None
    assert report["classification_missing_reason"] == "no_binary_danger_threshold_registered"
    decisions = {d["binding"]["update_id"]: d for d in frozen["decisions"]}
    for row in report["rows"]:
        assert row["verdict"] == decisions[row["update_id"]]["verdict"]
        assert row["adopted_arm"] == ("new" if row["verdict"] == "accept" else "old")
        assert row["target_complete"] is True
        assert row["selected_arm_complete"] is True
    for metric in (*report["root_weighted_mae"], report["adopted_risk"], report["adopted_utility"]):
        assert metric["total_roots"] == metric["observed_roots"] == 2
        assert metric["incomplete_roots"] == 0
        assert metric["mean"] is not None

    assert {partition: sum(p == partition and kind == "label" for p, kind, _ in accesses)
            for partition in sizes} == sizes
