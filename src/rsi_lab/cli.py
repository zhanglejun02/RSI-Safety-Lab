"""Offline preparation CLI. Unsupported experiments fail explicitly rather than run fake."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .config import load_config
from .studies import preparation as m2, closed_loop as m3, transition
from .serialization import decode_json
from .engine.demo import create_run, inspect_run, resume_run
from .errors import (
    BudgetExceeded,
    ConflictError,
    LabError,
    UnsupportedCapability,
    ValidationError,
)
from .serialization import digest, primitive
from .wiring import doctor_report, preflight
from .analysis.export import export_run
from .studies import s0


def main(argv=None) -> int:
    actual = list(sys.argv[1:] if argv is None else argv)
    if actual and actual[0] == "research":
        from .studies.research_cli import main as research_main
        return research_main(actual[1:])
    parser = argparse.ArgumentParser(
        prog="rsi-lab", description="RSI Safety Lab — reproducible P0 fixture kernel"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "doctor", help="Report implemented capabilities; no model calls"
    )
    subparsers.add_parser("research", help="Task A collection, independent labels and risk-model training")
    validate = subparsers.add_parser(
        "validate", help="Validate TOML and preflight capabilities"
    )
    validate.add_argument("--config", type=Path, required=True)
    run = subparsers.add_parser("run", help="Run a registered development fixture")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument(
        "--stop-after",
        type=int,
        choices=range(1, 5),
        default=4,
        help="Persist this many checkpoints per arm",
    )
    resume = subparsers.add_parser(
        "resume", help="Resume the same stored protocol and identities"
    )
    resume.add_argument("--run", type=Path, required=True)
    inspect = subparsers.add_parser("inspect", help="Read a run without advancing it")
    inspect.add_argument("--run", type=Path, required=True)
    export = subparsers.add_parser(
        "export", help="Read-only research or permitted training feature export"
    )
    export.add_argument("--run", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--view", choices=("analysis", "training"), required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            result = doctor_report()
        elif args.command == "validate":
            config = load_config(args.config)
            preflight(config)
            result = {
                "valid": True,
                "resolved_config": config,
                "config_sha256": digest(config),
            }
        elif args.command == "export":
            result = export_run(args.run, args.output, view=args.view)
        elif args.command == "run":
            config = load_config(args.config)
            preflight(config)
            if config.stage == "M4":
                if args.stop_after != 4:
                    raise ValidationError(
                        "S0 runs the full lifecycle; --stop-after applies only to M1/M2"
                    )
                result = s0.create_run(config, args.output)
            elif config.stage == "M3":
                if args.stop_after != 4:
                    raise ValidationError(
                        "M3 runs the controlled lifecycle; --stop-after only applies to M1/M2"
                    )
                if config.study == "m3_transition":
                    if args.output.exists():
                        raise ConflictError("Output exists; use resume")
                    result = transition.run_transition(args.output)
                else:
                    result = m3.create_run(config, args.output)
            else:
                run_fn = m2.create_run if config.stage == "M2" else create_run
                result = run_fn(config, args.output, stop_after=args.stop_after)
        elif args.command == "resume":
            manifest = decode_json((args.run / "run_manifest.json").read_bytes())
            if manifest.get("stage") == "M4":
                result = s0.resume_run(args.run)
            elif manifest.get("stage") == "M3":
                result = (
                    transition.run_transition
                    if manifest.get("study") == "m3_transition"
                    else m3.resume_run
                )(args.run)
            else:
                result = (
                    m2.resume_run if manifest.get("stage") == "M2" else resume_run
                )(args.run)
        else:
            manifest = decode_json((args.run / "run_manifest.json").read_bytes())
            if manifest.get("stage") == "M4":
                result = s0.inspect_run(args.run)
            elif manifest.get("stage") == "M3":
                result = (
                    transition.inspect_transition
                    if manifest.get("study") == "m3_transition"
                    else m3.inspect_run
                )(args.run)
            else:
                result = (
                    m2.inspect_run if manifest.get("stage") == "M2" else inspect_run
                )(args.run)
        print(
            json.dumps(primitive(result), ensure_ascii=False, indent=2, allow_nan=False)
        )
        return 0
    except (LabError, OSError, sqlite3.DatabaseError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        if isinstance(exc, UnsupportedCapability):
            return 3
        if isinstance(exc, ConflictError):
            return 4
        if isinstance(exc, BudgetExceeded):
            return 5
        return 2
