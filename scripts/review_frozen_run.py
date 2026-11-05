"""Recheck a completed run with its exact archived Python, on a separate copy.

This is deliberately not a paid-run resume command. No hash checks are disabled,
and the live workspace, original run and registration are never written.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import subprocess
import sys
from zipfile import ZipFile


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate record field")
        result[key] = value
    return result


def read_record(path):
    value = json.loads(Path(path).read_bytes(), object_pairs_hook=unique)
    if set(value) != {"record", "sha256"} or digest(value["record"]) != value["sha256"]:
        raise ValueError("Record checksum mismatch")
    return value["record"]


def write_record(path, value):
    with Path(path).open("xb") as out:
        out.write(canonical({"record": value, "sha256": digest(value)}))


def inventory(directory):
    values = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("Frozen review does not follow symlinks")
        if path.is_file():
            values[str(path.relative_to(directory))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return values


def validate_archive(registration):
    registration = Path(registration)
    record = read_record(registration / "registration.json")
    names = record.get("source_hashes", record.get("source_files_sha256", record.get("source_files")))
    checksum = record.get("snapshot_sha256", record.get("source_snapshot_sha256"))
    path = registration / "source-snapshot.zip"
    if hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
        raise ValueError("Source archive checksum mismatch")
    with ZipFile(path) as archive:
        if len(archive.namelist()) != len(set(archive.namelist())) or set(archive.namelist()) != set(names):
            raise ValueError("Source archive inventory mismatch")
        files = {}
        for name, expected in names.items():
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts or "\\" in name:
                raise ValueError("Source archive path escaped extraction root")
            raw = archive.read(name)
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError("Archived source differs from registration: " + name)
            files[name] = raw
    return record, files


def ledger_inventory(directory):
    result = {}
    for path in sorted(directory.rglob("*.sqlite")):
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            result[str(path.relative_to(directory))] = digest(list(db.iterdump()))
    return result


def worker(output):
    source, run = output / "frozen-source/src", output / "run"
    sys.path.insert(0, str(source))
    import rsi_lab
    from rsi_lab.frozen import implementation_fingerprint
    from rsi_lab.infra.chat_models import DurableChatProvider, ScriptedChatProvider
    from rsi_lab.subjects.gepa import require_gepa
    from rsi_lab.subjects.openevolve import verify_upstream
    assert Path(rsi_lab.__file__).resolve().is_relative_to(source.resolve())
    manifest = read_record(run / "manifest.json")
    assert implementation_fingerprint() == manifest["implementation"]
    require_gepa(); verify_upstream()

    def forbidden(*args, **kwargs):
        raise AssertionError("Frozen review cannot dispatch, train or create new task effects")

    import socket
    socket.create_connection = socket.socket.connect = socket.socket.connect_ex = forbidden
    DurableChatProvider.generate = ScriptedChatProvider.generate = forbidden
    from rsi_lab.tasks import stress_bench
    stress_bench.evaluate_case = forbidden
    if manifest["protocol"] == "adaptation-rl-development-v1":
        from rsi_lab.tasks import dynamic_quota, tabular_navigation
        dynamic_quota.evaluate = tabular_navigation.evaluate = forbidden
        from rsi_lab.studies.adaptation_rl_analysis import analyze
    else:
        from rsi_lab.studies.stress_pilot_analysis import analyze
    expected = read_record(run / "analysis/summary.json")
    before = ledger_inventory(run)
    actual = analyze(run)
    assert actual == expected, "Frozen replay changed the reported result"
    assert before == ledger_inventory(run), "Frozen replay changed a logical ledger"
    # Reparse every confirmed response with the frozen gateway, including usage.
    if "provider_kind" in manifest:
        from rsi_lab.infra.chat_models import ChatConfig
        settings = ChatConfig.from_dict({"schema_version": 1,
            **{k: v for k, v in manifest["config"]["model"].items() if k != "provider"}})
    requests = []
    for root in manifest["root_ids"]:
        if manifest["protocol"] == "adaptation-rl-development-v1":
            from rsi_lab.studies.adaptation_rl_pilot import model_settings
            settings = model_settings(manifest["config"], root["kind"])
        provider = DurableChatProvider(run / root["path"] / "model.sqlite", settings, transport=forbidden)
        with provider._db() as db:
            rows = list(db.execute("SELECT * FROM chat_requests ORDER BY request_id"))
        for row in rows:
            assert hashlib.sha256(row["request_json"]).hexdigest() == row["request_sha"]
            if row["response_bytes"] is not None:
                assert hashlib.sha256(row["response_bytes"]).hexdigest() == row["response_sha"]
            if row["status"] == "complete":
                provider._replay(row)
            requests.append({"root": root["root_id"], "request_id": row["request_id"], "status": row["status"],
                "request_sha256": row["request_sha"], "response_sha256": row["response_sha"]})
    assert before == ledger_inventory(run)
    write_record(output / "replay.json", {"implementation": implementation_fingerprint(),
        "analysis_sha256": digest(actual), "root_count": len(manifest["root_ids"]), "requests": requests,
        "model_calls": 0, "fresh_training_or_task_execution": 0, "logical_ledgers_unchanged": True,
        "frozen_analysis_result_identical": True, "network_disabled_in_worker": True})


def review(run, registration, output):
    run, registration, output = [Path(p).resolve() for p in (run, registration, output)]
    if output.exists() or output.is_relative_to(run) or output.is_relative_to(registration):
        raise ValueError("Use a new output outside the original run and registration")
    registered, files = validate_archive(registration)
    manifest = read_record(run / "manifest.json")
    frozen = read_record(run / "generation_complete.json")
    if frozen["manifest_sha256"] != digest(manifest):
        raise ValueError("Search is not completely frozen")
    plan = registered.get("manifest", registered.get("natural_plan"))
    if not isinstance(plan, dict) or any(manifest.get(k) != v for k, v in plan.items()):
        raise ValueError("Run does not match the registered experiment")
    if manifest.get("registration_sha256") not in (None, digest(registered)):
        raise ValueError("Run points to another registration")
    before, before_registration = inventory(run), inventory(registration)
    output.mkdir(parents=True)
    reviewer_source = Path(__file__).read_bytes()
    reviewer_path = output / "reviewer.py"
    reviewer_path.write_bytes(reviewer_source)
    for name, raw in files.items():
        path = output / "frozen-source" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    shutil.copytree(run, output / "run")
    environment = {k: v for k, v in os.environ.items() if k != "PYTHONPATH" and not k.endswith(("API_KEY", "ACCESS_TOKEN"))}
    with (output / "worker.log").open("wb") as log:
        result = subprocess.run([sys.executable, str(reviewer_path), "--worker", str(output)],
                                cwd=output, env=environment, stdout=log, stderr=subprocess.STDOUT)
    unchanged = before == inventory(run) and before_registration == inventory(registration)
    write_record(output / "review.json", {"registration_sha256": digest(registered),
        "original_run": str(run), "original_registration": str(registration),
        "original_files_sha256": before, "original_registration_files_sha256": before_registration,
        "originals_unchanged": unchanged, "worker_exit_code": result.returncode,
        "source_files_checked": len(files), "reviewer_sha256": hashlib.sha256(reviewer_source).hexdigest()})
    if not unchanged or result.returncode:
        raise ValueError("Frozen review failed; see the separate worker.log and review.json")
    return read_record(output / "replay.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path)
    parser.add_argument("--registration", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args.worker)
    else:
        if not all((args.run, args.registration, args.output)):
            parser.error("--run, --registration and --output are required")
        value = review(args.run, args.registration, args.output)
        print({k: v for k, v in value.items() if k != "requests"})


if __name__ == "__main__":
    main()
