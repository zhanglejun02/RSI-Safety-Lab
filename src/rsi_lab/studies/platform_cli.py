"""Composition-based CLI; no stage-specific branches for new systems."""
import argparse
import json
from dataclasses import replace
from pathlib import Path
import sqlite3
from rsi_lab.errors import LabError
from rsi_lab.engine.continuation import read_record
from rsi_lab.infra.environment import load_environment
from rsi_lab.platform_config import PlatformConfig, load_platform_config
from rsi_lab.serialization import primitive
from .platform import create_run, resume_run, inspect_run, preflight, export_dataset


def main(argv=None):
    parser = argparse.ArgumentParser(prog="rsi-lab platform")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "run"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--env-file", type=Path)
        if name == "run":
            command.add_argument("--output", type=Path, required=True)
    for name in ("resume", "inspect"):
        command = commands.add_parser(name)
        command.add_argument("--run", type=Path, required=True)
        if name == "resume":
            command.add_argument("--env-file", type=Path)
    pilot = commands.add_parser("pilot-run", help="Development four-arm validity pilot (forces paired mode)")
    pilot.add_argument("--config", type=Path, required=True)
    pilot.add_argument("--output", type=Path, required=True)
    pilot.add_argument("--roots", type=int, default=2)
    pilot.add_argument("--audit-baselines", action="store_true", help="Freeze four audit baselines before independent evaluation")
    pilot.add_argument("--env-file", type=Path)
    pilot_resume = commands.add_parser("pilot-resume")
    pilot_resume.add_argument("--run", type=Path, required=True)
    pilot_resume.add_argument("--env-file", type=Path)
    dataset = commands.add_parser("dataset")
    dataset.add_argument("--run", action="append", type=Path, required=True)
    dataset.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = load_platform_config(args.config) if hasattr(args, "config") else None
        if getattr(args, "env_file", None) is not None:
            if config is None:
                config = PlatformConfig.from_dict(read_record(args.run / "manifest.json")["config"])
            allowed_names = {setting.api_key_env for setting in (config.editor, config.actor)
                             if setting.api_key_env is not None}
            load_environment(args.env_file, allowed_names | {"SSL_CERT_FILE"})
        if args.command == "pilot-run":
            from .validity_pilot import create_run as create_pilot
            config = replace(config, protocol=replace(config.protocol, mode="paired"))
            result = create_pilot(config, args.output, roots=args.roots, audit_baselines=args.audit_baselines)
        elif args.command == "pilot-resume":
            from .validity_pilot import resume_run as resume_pilot
            result = resume_pilot(args.run)
        elif args.command == "validate":
            result = preflight(config)
        elif args.command == "run":
            result = create_run(config, args.output)
        elif args.command == "resume":
            result = resume_run(args.run)
        elif args.command == "inspect":
            result = inspect_run(args.run)
        else:
            result = export_dataset(args.run, args.output)
        print(json.dumps(primitive(result), ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (LabError, OSError, sqlite3.DatabaseError) as exc:
        print(f"{type(exc).__name__}: {exc}")
        return 2
