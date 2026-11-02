"""CLI for the bounded q/descendant admission study; no hidden API defaults."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

from rsi_lab.errors import LabError
from rsi_lab.infra.environment import load_environment
from rsi_lab.serialization import primitive
from rsi_lab.engine.continuation import read_record
from .cost_q import create_run, resume_run, load_config


def main(argv=None):
    parser = argparse.ArgumentParser(prog="rsi-lab cost-q")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Register new root splits, collect, train and evaluate")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--env-file", type=Path)
    resume = commands.add_parser("resume", help="Resume immutable work without resending unknown calls")
    resume.add_argument("--run", type=Path, required=True)
    resume.add_argument("--env-file", type=Path)
    inspect = commands.add_parser("inspect", help="Read frozen summary or current completed root records")
    inspect.add_argument("--run", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if getattr(args, "env_file", None):
            settings = (load_config(args.config).model if args.command == "run" else
                        read_record(args.run / "manifest.json")["config"]["model"])
            name = settings.api_key_env if hasattr(settings, "api_key_env") else settings["api_key_env"]
            load_environment(args.env_file, {name, "SSL_CERT_FILE"})
        if args.command == "run":
            result = create_run(load_config(args.config), args.output)
        elif args.command == "resume":
            result = resume_run(args.run)
        else:
            path = args.run / "summary.json"
            result = read_record(path) if path.exists() else {
                "status": "in_progress", "manifest": read_record(args.run / "manifest.json"),
                "online_roots": len(list(args.run.glob("roots/*/online.json"))),
                "labelled_roots": len(list(args.run.glob("roots/*/label.json"))),
                "failed_roots": len(list(args.run.glob("roots/*/root-failure.json"))),
                "models_frozen": (args.run / "models-frozen.json").exists()}
        print(json.dumps(primitive(result), ensure_ascii=False, indent=2))
        return 0
    except (LabError, OSError, sqlite3.DatabaseError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
