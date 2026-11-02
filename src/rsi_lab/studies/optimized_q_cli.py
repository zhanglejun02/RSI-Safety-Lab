"""Bounded optimized-checkpoint diagnostic; inspect never advances work."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

from rsi_lab.engine.continuation import read_record
from rsi_lab.errors import LabError
from rsi_lab.infra.environment import load_environment
from rsi_lab.serialization import primitive
from .optimized_q import create_run, load_config, resume_run


def main(argv=None):
    parser = argparse.ArgumentParser(prog="rsi-lab optimized-q")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--env-file", type=Path)
    resume = commands.add_parser("resume")
    resume.add_argument("--run", type=Path, required=True)
    resume.add_argument("--env-file", type=Path)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--run", type=Path, required=True)
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
            result = {"status": "in_progress", "checkpoint_frozen": (args.run / "checkpoint-frozen.json").exists(),
                "completed_trajectories": len(list(args.run.glob("trajectories/*/*/*/final-snapshot.json")))}
        compact = {k: result[k] for k in ("status", "origin", "checkpoint_public", "analysis", "usage",
            "max_editor_requests", "max_model_tokens", "completed_trajectories", "checkpoint_frozen") if k in result}
        print(json.dumps(primitive(compact), ensure_ascii=False, indent=2))
        return 0
    except (LabError, OSError, sqlite3.DatabaseError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
