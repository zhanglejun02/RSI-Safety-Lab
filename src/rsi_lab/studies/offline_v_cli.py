"""Zero-API v study; no environment file, model configuration or training path."""
import argparse
import json
from pathlib import Path
import sys

from rsi_lab.engine.continuation import read_record
from rsi_lab.errors import LabError
from .offline_v import create_run, resume_run


def main(argv=None):
    parser = argparse.ArgumentParser(prog="rsi-lab v-offline")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--source-run", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    for name in ("resume", "inspect"):
        command = commands.add_parser(name)
        command.add_argument("--run", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            result = create_run(args.source_run, args.output)
        elif args.command == "resume":
            result = resume_run(args.run)
        else:
            result = read_record(args.run / "summary.json")
        print(json.dumps({k: result[k] for k in ("status", "registered_candidates", "history_groups",
            "common_public_eligible", "arms", "mechanism_check", "model_calls")}, ensure_ascii=False, indent=2))
        return 0
    except (LabError, OSError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
