"""Bounded repeated mechanism experiment, with explicit credential loading."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

from rsi_lab.engine.continuation import read_record
from rsi_lab.errors import LabError
from rsi_lab.infra.environment import load_environment
from rsi_lab.serialization import primitive
from .q_effect import create_run, load_config, resume_run


def main(argv=None):
    parser = argparse.ArgumentParser(prog="rsi-lab q-effect")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Register q interventions and repeated native continuations")
    run.add_argument("--config", required=True, type=Path)
    run.add_argument("--output", required=True, type=Path)
    run.add_argument("--env-file", type=Path)
    resume = commands.add_parser("resume", help="Replay/resume registered work; never retry unknown calls")
    resume.add_argument("--run", required=True, type=Path)
    resume.add_argument("--env-file", type=Path)
    inspect = commands.add_parser("inspect", help="Read compact progress/summary without advancing")
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
        else:
            path = args.run / "summary.json"
            result = read_record(path) if path.exists() else {
                "status": "in_progress", "manifest": read_record(args.run / "manifest.json"),
                "candidates_frozen": (args.run / "candidates-frozen.json").exists(),
                "completed_slots": len(list(args.run.glob("roots/*/repetitions/*/*/summary.json"))),
                "failed_roots": len(list(args.run.glob("roots/*/root-failure.json")))}
        # Full per-attempt data stays in the immutable summary, not terminal output.
        compact = {key: value for key, value in result.items() if key not in ("samples", "root_metadata")}
        print(json.dumps(primitive(compact), ensure_ascii=False, indent=2))
        return 0
    except (LabError, OSError, sqlite3.DatabaseError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
