"""Explicit, bounded development history diagnostic."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
from rsi_lab.engine.continuation import read_record
from rsi_lab.errors import LabError
from rsi_lab.infra.environment import load_environment
from rsi_lab.serialization import primitive
from .experience_diagnostic import create_run, load_config, resume_run


def main(argv=None):
    parser = argparse.ArgumentParser(prog="rsi-lab experience")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--config", required=True, type=Path)
    run.add_argument("--output", required=True, type=Path)
    run.add_argument("--env-file", type=Path)
    resume = commands.add_parser("resume")
    resume.add_argument("--run", required=True, type=Path)
    resume.add_argument("--env-file", type=Path)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--run", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if getattr(args, "env_file", None):
            name = (load_config(args.config).model.api_key_env if args.command == "run" else
                    read_record(args.run / "manifest.json")["config"]["model"]["api_key_env"])
            load_environment(args.env_file, {name, "SSL_CERT_FILE"})
        if args.command == "run":
            result = create_run(load_config(args.config), args.output)
        elif args.command == "resume":
            result = resume_run(args.run)
        elif (args.run / "summary.json").exists():
            result = read_record(args.run / "summary.json")
        else:
            result = {"status": "in_progress", "history_frozen": (args.run / "history-frozen.json").exists(),
                "completed_slots": len(list(args.run.glob("diagnostic/*/*/*/*/summary.json")))}
        print(json.dumps(primitive({k: v for k, v in result.items() if k != "rows"}), ensure_ascii=False, indent=2))
        return 0
    except (LabError, OSError, sqlite3.DatabaseError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
