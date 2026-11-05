"""Pre-register the paired stress study and gate its sole paid run on a separate approval."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sys
import tomllib
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.errors import ConflictError, IntegrityError, LabError, ValidationError
from rsi_lab.infra.artifacts import _checked_path, _publish_bytes
from rsi_lab.infra.chat_models import ChatConfig
from rsi_lab.serialization import digest
from rsi_lab.tasks import stress_bench as bench
from rsi_lab.tasks.bounded_language import CONTRACT_VERSION, editor_contract
from . import stress_pilot as pilot

REPO = Path(__file__).resolve().parents[3]
CONFIG = REPO / "configs/stress_confirmation_kimi.toml"
THRESHOLDS = REPO / "configs/stress_confirmation_thresholds.json"
REG = REPO / "runs/stress-readiness-20261105/registration"
OUTPUT = REPO / "runs/stress-confirmation-kimi-20261105"
PROTOCOL = "stress-rsi-paired-confirmation-v1"
DATA_SCOPE = ["synthetic_task_requests", "public_domain_tool_and_language_contract",
              "bounded_program_source", "ordinary_public_execution_traces"]
CALL_CAP = 384
TOKEN_CAP = 6400000
SEEDS = list(range(2026110501, 2026110517))


def _config(control=False):
    cfg = tomllib.loads(CONFIG.read_text())
    raw_model = dict(cfg["model"])
    provider = raw_model.pop("provider")
    settings = ChatConfig.from_dict({"schema_version": 1, **raw_model})
    expected_grid = {"domains": list(bench.DOMAINS), "seeds": SEEDS, "horizon": 3}
    if (cfg["experiment"] != expected_grid or cfg["budget"] != {"max_calls": CALL_CAP, "max_total_tokens": TOKEN_CAP}
            or provider != "http" or settings.endpoint != "https://api.moonshot.cn/v1/chat/completions"
            or settings.model != "kimi-k2.6" or settings.api_key_env != "MOONSHOT_API_KEY"
            or settings.max_calls != 3 or settings.max_total_tokens != 50000):
        raise ValidationError("Confirmation grid, destination or budget differs from the protocol")
    if control:
        cfg["experiment"] = {"domains": ["finance"], "seeds": [2066110501], "horizon": 2}
        cfg["budget"] = {"max_calls": 4, "max_total_tokens": 100000}
        cfg["model"] = {**raw_model, "provider": "scripted", "base_url": "http://localhost/control",
                        "model": "control", "api_key_env": None, "max_calls": 2}
        settings = ChatConfig.from_dict({"schema_version": 1, **{k: v for k, v in cfg["model"].items() if k != "provider"}})
    return cfg, settings


def manifest_for(control=False):
    cfg, settings = _config(control)
    manifest = pilot._manifest(cfg, cfg["model"]["provider"], settings)
    if (manifest["model_call_cap"] != cfg["budget"]["max_calls"]
            or manifest["token_cap"] != cfg["budget"]["max_total_tokens"]):
        raise ValidationError("Root allocations must sum to the global cap")
    # Commit parameter IDs only. Do not build or execute any holdout cases before search freeze.
    parameters = {f"{d}/{seed}": seed + 1200000000 for d in manifest["domains"] for seed in manifest["seeds"]}
    thresholds = read_record(THRESHOLDS)
    manifest.update(threshold_bps=thresholds["threshold_bps"], thresholds_record_sha256=digest(thresholds),
                    protocol=PROTOCOL + ("-control" if control else ""), confirmatory=not control,
                    contract_version=CONTRACT_VERSION, contracts_sha256={d: digest(editor_contract(d)) for d in manifest["domains"]},
                    holdout_parameters=parameters, holdout_parameters_sha256=digest(parameters),
                    independent_evaluation="after_all_search_frozen;registered_holdout_parameters;both_arms_all_levels",
                    admission_monitor="finance_warning_pre_activation_observation_only", threshold_file_sha256=_sha(THRESHOLDS))
    return manifest, settings


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _archive():
    source = REPO / "src/rsi_lab"
    paths = set(source.rglob("*.py")) | set((source / "data").glob("*.json"))
    paths.update({CONFIG, THRESHOLDS, REPO / "docs/specs/stress_confirmation_protocol.md",
                  REPO / "scripts/calibrate_stress_thresholds.py", REPO / "scripts/verify_stress_contracts.py",
                  REPO / "pyproject.toml",
                  source / "tasks/retail_vendor/LICENSE"})
    if not (source / "studies/stress_scorecard.py").is_file():
        raise ValidationError("Registered analysis requires the scorecard implementation")
    stream, hashes = BytesIO(), {}
    with ZipFile(stream, "w") as archive:
        for path in sorted(paths):
            name = path.relative_to(REPO).as_posix()
            raw = path.read_bytes()
            hashes[name] = hashlib.sha256(raw).hexdigest()
            info = ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, raw)
    return stream.getvalue(), hashes


def _registration():
    manifest, _ = manifest_for()
    thresholds = read_record(THRESHOLDS)["threshold_bps"]
    if set(thresholds) != set(bench.DOMAINS) or any(type(x) is not int or not 0 < x <= 10000 for x in thresholds.values()):
        raise ValidationError("Expected four positive domain thresholds in basis points")
    data, hashes = _archive()
    return {"manifest": manifest, "manifest_sha256": digest(manifest), "relative_output": str(OUTPUT.relative_to(REPO)),
            "threshold_bps": thresholds, "source_snapshot_sha256": hashlib.sha256(data).hexdigest(),
            "source_files_sha256": hashes, "dispatch_authority": "requires_separate_explicit_user_approval",
            "provider_data_scope": DATA_SCOPE}, data


def register():
    record, data = _registration()
    _checked_path(REG).mkdir(parents=True, exist_ok=True)
    snapshot = REG / "source-snapshot.zip"
    if not _publish_bytes(data, snapshot) and _sha(snapshot) != record["source_snapshot_sha256"]:
        raise ConflictError("Existing confirmation snapshot differs")
    save_record(REG / "registration.json", record)
    return record


def _verified_authority(approval):
    expected, _ = _registration()
    registered = read_record(REG / "registration.json")
    if registered != expected or _sha(REG / "source-snapshot.zip") != registered["source_snapshot_sha256"]:
        raise IntegrityError("Registration or its source inputs have changed")
    manifest = registered["manifest"]
    consent = read_record(approval)
    terms = {"registration_sha256": digest(registered), "authorized": True,
             "output": str(OUTPUT), "endpoint": manifest["config"]["model"]["base_url"],
             "model": manifest["config"]["model"]["model"], "api_key_env": "MOONSHOT_API_KEY",
             "data_scope": DATA_SCOPE, "max_calls": CALL_CAP, "max_total_tokens": TOKEN_CAP}
    if (any(consent.get(k) != v for k, v in terms.items()) or consent.get("authorized") is not True
            or not isinstance(consent.get("user_statement"), str) or not consent["user_statement"].strip()):
        raise ValidationError("A separate explicit user approval must bind this registration, destination, scope and budget")
    return registered, consent


@contextmanager
def _run_lock(directory):
    with (_checked_path(directory / "runner.lock")).open("ab") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ConflictError("Another worker owns this registered run") from exc
        yield


def run_paid(approval, *, env_file=None, resume=False):
    registration, consent = _verified_authority(approval)
    with _run_lock(REG):
        started = REG / "dispatch-started.json"
        _checked_path(OUTPUT)
        bound_manifest = {**registration["manifest"], "registration_sha256": digest(registration),
                          "approval_sha256": digest(consent)}
        manifest_path = OUTPUT / "manifest.json"
        if not resume and (OUTPUT.exists() or started.exists()):
            raise ConflictError("Registered budget already opened; use its original run and resume")
        if resume:
            if not OUTPUT.is_dir() or (started.exists() and not manifest_path.is_file()):
                raise ConflictError("Cannot resume without the original bound run")
            if not started.exists() and any(p != manifest_path for p in OUTPUT.iterdir()):
                raise IntegrityError("Unexpected run content before budget opening")
        # Persist the unique output first. If interrupted here, resume can finish opening
        # an empty/manifest-only directory; a previously opened budget needs its old manifest.
        save_record(manifest_path, bound_manifest)
        save_record(started, {"registration_sha256": digest(registration), "approval_sha256": digest(consent),
                              "output": str(OUTPUT)})
        if (OUTPUT / "generation_complete.json").exists():
            from .stress_pilot_analysis import analyze
            return analyze(OUTPUT)  # completed resume is offline
        _, settings = _config()
        return pilot._run_registered(bound_manifest, settings, OUTPUT, env_file=env_file, resume=True)


def run_control(output, resume=False):
    output = Path(output).resolve()
    if output == OUTPUT or output.is_relative_to(OUTPUT) or output.is_relative_to(REG):
        raise ValidationError("Control output must be isolated from formal output and registration")
    manifest, settings = manifest_for(control=True)
    return pilot._run_registered(manifest, settings, output, resume=resume)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("register")
    control = commands.add_parser("control")
    control.add_argument("--output", type=Path, required=True)
    control.add_argument("--resume", action="store_true")
    for name in ("run", "resume"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--approval", type=Path, required=True)
        cmd.add_argument("--env-file", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "register":
            record = register()
            print(json.dumps({"registration_sha256": digest(record), "output": str(REG / "registration.json"),
                              "calls": CALL_CAP, "tokens": TOKEN_CAP, "authorization": "pending"}))
        else:
            if args.command == "control":
                output = args.output.resolve()
                run_control(output, args.resume)
            else:
                output = OUTPUT
                run_paid(args.approval, env_file=args.env_file, resume=args.command == "resume")
            from .stress_scorecard import main as score
            score(["--run", str(output), "--output", str(output.with_name(output.name + "-scorecard")),
                   "--thresholds", str(THRESHOLDS)])
        return 0
    except (LabError, OSError, ValueError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
