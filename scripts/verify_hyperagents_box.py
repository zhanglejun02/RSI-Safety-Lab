"""Read/replay controls with model dispatch, meta execution and training disabled."""
import argparse
import hashlib
from pathlib import Path
import sqlite3

from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.infra.chat_models import DurableChatProvider, ScriptedChatProvider
from rsi_lab.serialization import digest, decode_json
from rsi_lab.studies import hyperagents_box as study
from rsi_lab.subjects import hyperagents_runtime as runtime
from rsi_lab.tasks import box_navigation as box
from rsi_lab.tasks.adaptation_rl_package import StudyExecutor


def ledger_hash(directory):
    result = {}
    for path in sorted(directory.rglob("*.sqlite")):
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            result[str(path.relative_to(directory))] = digest(list(db.iterdump()))
    return digest(result)


def forbidden(*args, **kwargs):
    raise AssertionError("Verification cannot dispatch, execute meta code or train")


def verify(output):
    output = Path(output).resolve()
    manifest = read_record(output / "manifest.json")
    before, requests, jobs, resources = ledger_hash(output), [], [], []
    for root in manifest["root_ids"]:
        directory = output / root["path"]
        provider = study.provider(directory, root["system"])
        owned = {}
        for folder in sorted(directory.glob("step-*")):
            for path in (folder / "requests").glob("*.json"):
                row = read_record(path)
                assert row["request_id"] not in owned
                owned[row["request_id"]] = row
                assert any(box.CONTRACT in m["content"] for m in row["body"]["messages"])
            for path in folder.glob("public/*/environment/operations/*.json"):
                assert read_record(path)["identity"]["case"]["purpose"] == "public"
        with provider._db() as db:
            rows = list(db.execute("SELECT * FROM chat_requests ORDER BY request_id"))
        assert set(owned) == {r["request_id"] for r in rows}
        for row in rows:
            assert hashlib.sha256(row["request_json"]).hexdigest() == row["request_sha"]
            assert decode_json(row["request_json"]) == owned[row["request_id"]]["body"]
            if row["status"] == "complete":
                provider._replay(row)
            requests.append({"system": root["system"], "request_id": row["request_id"],
                             "request_sha256": row["request_sha"], "response_sha256": row["response_sha"], "status": row["status"]})
        assert provider.summary() == read_record(directory / "root.json")["model_usage"]
        for path in sorted((directory / "execution/jobs").glob("*/result.json")):
            row = read_record(path); StudyExecutor.verify(row)
            jobs.append({"system": root["system"], "path": str(path.relative_to(output)), "sha256": digest(row),
                         "purpose": row["identity"]["case"]["purpose"], "usage": row["usage"], "valid": row["metrics"]["complete"]})
        executor = StudyExecutor(directory / "execution", manifest["config"]["execution"])
        summary = executor.summary()
        assert not summary["unknown_operations"] and not any(a["over_budget"] for a in summary["accounts"])
        resources.append({"system": root["system"], "summary": summary})
    originals = (DurableChatProvider.generate, ScriptedChatProvider.generate, box.evaluate, runtime.edit)
    try:
        DurableChatProvider.generate = ScriptedChatProvider.generate = box.evaluate = runtime.edit = forbidden
        result = study.run(output, resume=True)
    finally:
        DurableChatProvider.generate, ScriptedChatProvider.generate, box.evaluate, runtime.edit = originals
    assert before == ledger_hash(output)
    assert len(requests) <= manifest["config"]["experiment"]["model_call_cap"]
    value = {"verifier_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "manifest_sha256": digest(manifest), "summary_sha256": digest(result),
        "requests": requests, "jobs": jobs, "resources": resources,
        "execution_records": len(jobs), "scripted_requests": len(requests), "paid_requests": 0,
        "independent_execution_records": sum(j["purpose"] == "independent" for j in jobs),
        "actual_usage": {k: sum(j["usage"][k] for j in jobs) for k in ("training_steps", "evaluation_steps", "tool_calls", "compute_ms")},
        "logical_ledgers_unchanged": True, "verification_model_calls": 0, "verification_training_jobs": 0,
        "verification_meta_executions": 0, "ledger_sha256": before}
    save_record(output / "validation.json", value)
    return value


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    value = verify(parser.parse_args().output)
    print({k: v for k, v in value.items() if k not in ("requests", "jobs", "resources")})
