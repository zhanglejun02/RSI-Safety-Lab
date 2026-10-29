"""Explicit commands for the task-A supervised research protocol."""

import argparse
import json
from pathlib import Path
import sqlite3

from rsi_lab.errors import LabError
from rsi_lab.learning.contracts import AuditFeatures
from rsi_lab.learning.model import train_model, calibrate_model, load_model, decide, evaluate_model
from rsi_lab.serialization import decode_json, primitive
from . import task_a


def main(argv=None):
    parser = argparse.ArgumentParser(prog="rsi-lab research")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "collect"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        if name == "collect":
            command.add_argument("--output", type=Path, required=True)
    resume = commands.add_parser("resume")
    resume.add_argument("--run", type=Path, required=True)
    dataset = commands.add_parser("dataset")
    dataset.add_argument("--run", type=Path, action="append", required=True)
    dataset.add_argument("--output", type=Path, required=True)
    train = commands.add_parser("train")
    train.add_argument("--dataset", type=Path, required=True)
    train.add_argument("--output", type=Path, required=True)
    train.add_argument("--epochs", type=int, default=400)
    calibrate = commands.add_parser("calibrate")
    calibrate.add_argument("--model", type=Path, required=True)
    calibrate.add_argument("--dataset", type=Path, required=True)
    calibrate.add_argument("--output", type=Path, required=True)
    predict = commands.add_parser("predict")
    predict.add_argument("--model", type=Path, required=True)
    predict.add_argument("--features", type=Path, required=True)
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--model", type=Path, required=True)
    evaluate.add_argument("--dataset", type=Path, required=True)
    evaluate.add_argument("--predictions", type=Path, required=True)
    evaluate.add_argument("--purpose", choices=("evaluation", "development"), default="evaluation")
    args = parser.parse_args(argv)
    try:
        if args.command in ("collect", "validate"):
            config = task_a.load_config(args.config)
            result = (task_a.create_run(config, args.output) if args.command == "collect" else
                      {"valid": True, "config": config, "protocol": task_a.protocol(config),
                       "max_planned_model_calls": config.roots * (config.development_rounds + 1 + 2 * config.probe_horizon + 2 * config.repetitions * config.horizon),
                       "note": "No network request. Explicit max_calls may yield incomplete labels."})
        elif args.command == "resume":
            result = task_a.resume_run(args.run)
        elif args.command == "dataset":
            result = task_a.export_dataset(args.run, args.output)
        elif args.command == "train":
            result = train_model(args.dataset, args.output, epochs=args.epochs)
        elif args.command == "calibrate":
            result = calibrate_model(args.model, args.dataset, args.output)
        elif args.command == "predict":
            features = AuditFeatures.from_dict(decode_json(args.features.read_bytes()))
            model = load_model(args.model)
            prediction = model.predict(features)
            result = {"prediction": prediction, "decision": decide(features, prediction)}
        else:
            result = evaluate_model(args.model, args.dataset, purpose=args.purpose,
                                    predictions_output=args.predictions)
        print(json.dumps(primitive(result), ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (LabError, OSError, sqlite3.DatabaseError, ValueError) as exc:
        # Transport errors sanitize their diagnostics; credentials never form CLI arguments.
        print(f"{type(exc).__name__}: {exc}")
        return 2
