"""CPU-only control-data acceptance of collection, training and blind evaluation."""

import argparse
from dataclasses import replace
import json
from pathlib import Path

from rsi_lab.contracts import Partition
from rsi_lab.learning.model import calibrate_model, evaluate_model, train_model
from rsi_lab.serialization import primitive
from rsi_lab.studies.task_a import TaskAConfig, create_run, export_dataset


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    base = TaskAConfig(roots=8, seed=100)
    training = create_run(base, args.output / "train")
    calibration = create_run(replace(base, partition=Partition.CALIBRATION, roots=10, seed=200),
                             args.output / "calibration")
    export_dataset([args.output / "train", args.output / "calibration"], args.output / "learning-data")
    trained = train_model(args.output / "learning-data", args.output / "model.json")
    calibrated = calibrate_model(args.output / "model.json", args.output / "learning-data",
                                 args.output / "calibrated-model.json")
    # Generate the engineering held-out set only after freezing the model.
    sealed = create_run(replace(base, partition=Partition.SEALED, roots=2, seed=300), args.output / "sealed")
    export_dataset([args.output / "sealed"], args.output / "evaluation-data")
    evaluation = evaluate_model(args.output / "calibrated-model.json", args.output / "evaluation-data",
                                predictions_output=args.output / "blind-decisions.json")
    summary = {"engineering_control_only": True, "cloud_calls": 0,
               "collected_roots": {"train": training["measured_roots"],
                                   "calibration": calibration["measured_roots"],
                                   "sealed": sealed["measured_roots"]},
               "training": trained, "calibration": calibrated, "evaluation": evaluation}
    text = json.dumps(primitive(summary), ensure_ascii=False, indent=2, allow_nan=False)
    (args.output / "summary.json").write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
