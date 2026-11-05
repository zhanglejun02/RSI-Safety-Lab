"""Small registered natural-mechanism screen. Reuses native adapters and protected effects."""
import argparse
import hashlib
from io import BytesIO
from pathlib import Path
import sys
import tomllib
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.errors import ConflictError, IntegrityError, LabError, ValidationError
from rsi_lab.infra.artifacts import _publish_bytes
from rsi_lab.infra.chat_models import ChatConfig
from rsi_lab.serialization import digest
from rsi_lab.tasks import stress_bench as bench
from rsi_lab.tasks.bounded_language import CONTRACT_VERSION, editor_contract
from . import stress_pilot as pilot
from .stress_confirmation import _run_lock

REPO = Path(__file__).resolve().parents[3]
CONFIG = REPO / "configs/mechanism_pilot_kimi.toml"
REGISTRATION = REPO / "runs/mechanism-registration-20261105"
OUTPUT = REPO / "runs/mechanism-kimi-20261105"
PROTOCOL = "mechanism-diversity-development-v1"


def configuration(control=False):
    config = tomllib.loads(CONFIG.read_text())
    if config["experiment"] != {"domains": ["privacy", "feedback", "quota"], "seeds": [2026111501, 2026111502], "horizon": 3}:
        raise ValidationError("Unexpected mechanism screen grid")
    model = dict(config["model"])
    if model.pop("provider") != "http":
        raise ValidationError("Registered model provider differs")
    settings = ChatConfig.from_dict({"schema_version": 1, **model})
    if (settings.endpoint != "https://api.moonshot.cn/v1/chat/completions" or settings.model != "kimi-k2.6"
            or settings.max_calls != 3 or settings.max_total_tokens != 50000 or settings.max_output_tokens != 4096
            or settings.max_prompt_bytes != 40000):
        raise ValidationError("Registered budget or destination differs")
    if control:
        config["experiment"] = {"domains": ["privacy", "feedback", "quota"], "seeds": [2066111501], "horizon": 2}
        model.update(base_url="http://localhost/control", model="control", api_key_env=None, max_calls=2)
        config["model"] = {"provider": "scripted", **model}
        settings = ChatConfig.from_dict({"schema_version": 1, **model})
    manifest = pilot._manifest(config, config["model"]["provider"], settings)
    manifest.update(protocol=PROTOCOL, confirmatory=False, bounded_dispatch=True,
                    contract_version=CONTRACT_VERSION,
                    contracts_sha256={d: digest(editor_contract(d)) for d in config["experiment"]["domains"]},
                    task_revisions={d: bench.execution_version(d) for d in config["experiment"]["domains"]},
                    feedback_threat="supplier_note_in_public_tool_reply;recipient_disagreement_in_independent_execution",
                    registration_scope="small_development_screen_not_system_ranking")
    if not control and (manifest["model_call_cap"], manifest["token_cap"]) != (36, 600000):
        raise ValidationError("Unexpected total model allowance")
    return manifest, settings


def registration():
    manifest, _ = configuration()
    paths = set((REPO / "src/rsi_lab").rglob("*.py"))
    paths.update((REPO / "src/rsi_lab/data").glob("*.json"))
    paths.update({CONFIG, REPO / "docs/specs/mechanism_pilot_protocol.md", REPO / "pyproject.toml",
                  REPO / "scripts/diagnose_stress_tasks.py", REPO / "scripts/verify_mechanism_pilot.py",
                  REPO / "src/rsi_lab/tasks/retail_vendor/LICENSE"})
    stream, hashes = BytesIO(), {}
    with ZipFile(stream, "w") as archive:
        for path in sorted(paths):
            name = str(path.relative_to(REPO))
            raw = path.read_bytes()
            hashes[name] = hashlib.sha256(raw).hexdigest()
            info = ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, raw)
    raw = stream.getvalue()
    return {"manifest": manifest, "source_hashes": hashes, "snapshot_sha256": hashlib.sha256(raw).hexdigest(),
            "output": str(OUTPUT), "authority": {"user_instruction": "用kimi的api测试吧",
                "bounded_execution_choice": "36 editor calls / 600000 tokens, stated before any dispatch",
                "endpoint": "https://api.moonshot.cn/v1", "model": "kimi-k2.6",
                "data_scope": ["synthetic tasks", "public contracts", "program source", "public execution traces"],
                "separate_from_prior_budgets": True}}, raw


def register():
    record, raw = registration()
    REGISTRATION.mkdir(parents=True, exist_ok=True)
    path = REGISTRATION / "source-snapshot.zip"
    if not _publish_bytes(raw, path) and path.read_bytes() != raw:
        raise ConflictError("Registered source snapshot differs")
    save_record(REGISTRATION / "registration.json", record)
    return record


def run(env_file=None, resume=False):
    expected, raw = registration()
    registered = read_record(REGISTRATION / "registration.json")
    if registered != expected or (REGISTRATION / "source-snapshot.zip").read_bytes() != raw:
        raise IntegrityError("Mechanism registration changed before execution")
    with _run_lock(REGISTRATION):
        manifest = {**registered["manifest"], "registration_sha256": digest(registered)}
        path, started = OUTPUT / "manifest.json", REGISTRATION / "dispatch-started.json"
        if not resume and (OUTPUT.exists() or started.exists()):
            raise ConflictError("Use the original mechanism output and resume")
        if resume and (not OUTPUT.exists() or (started.exists() and not path.exists())):
            raise ConflictError("Cannot reopen lost budget state")
        save_record(path, manifest)
        save_record(started, {"registration_sha256": digest(registered), "output": str(OUTPUT)})
        if (OUTPUT / "generation_complete.json").exists():
            from .stress_pilot_analysis import analyze
            return analyze(OUTPUT)
        _, settings = configuration()
        return pilot._run_registered(manifest, settings, OUTPUT, env_file=env_file, resume=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("register")
    for name in ("run", "resume"):
        command = commands.add_parser(name)
        command.add_argument("--env-file", type=Path)
    control = commands.add_parser("control")
    control.add_argument("--output", type=Path, required=True)
    control.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "register":
            print(digest(register()))
            return 0
        if args.command == "control":
            output = args.output.resolve()
            if output == OUTPUT or output.is_relative_to(OUTPUT) or output.is_relative_to(REGISTRATION):
                raise ValidationError("Control output overlaps the paid experiment")
            manifest, settings = configuration(control=True)
            pilot._run_registered(manifest, settings, output, resume=args.resume)
        else:
            output = OUTPUT
            run(args.env_file, args.command == "resume")
        from .stress_scorecard import main as score
        score(["--run", str(output), "--output", str(output.with_name(output.name + "-scorecard"))])
        return 0
    except (LabError, OSError, ValueError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
