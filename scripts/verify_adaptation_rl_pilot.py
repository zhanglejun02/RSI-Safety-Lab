"""Audit completed development runs; prohibit model dispatch and fresh execution."""
import argparse
from collections import Counter
import hashlib
from pathlib import Path
import sqlite3

from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.infra.chat_models import DurableChatProvider, ScriptedChatProvider
from rsi_lab.serialization import decode_json, digest
from rsi_lab.studies import adaptation_rl_pilot as study
from rsi_lab.studies.adaptation_rl_analysis import analyze
from rsi_lab.tasks import dynamic_quota as dynamic, tabular_navigation as rl
from rsi_lab.tasks.adaptation_rl_package import StudyExecutor


def forbidden(*args, **kwargs):
    raise AssertionError("Verification must not generate, train or deploy")


def ledger_hash(directory):
    values = {}
    for name in ("model.sqlite", "execution/resources.sqlite"):
        with sqlite3.connect(directory / name) as db:
            # SQLite page/WAL representation may change without any logical change.
            values[name] = digest(list(db.iterdump()))
    return digest(values)


def verify(output):
    output = Path(output).resolve()
    manifest = read_record(output / "manifest.json")
    if manifest["origin"] == "natural":
        registered, archive = study.registration()
        assert read_record(study.REGISTRATION / "registration.json") == registered
        assert (study.REGISTRATION / "source-snapshot.zip").read_bytes() == archive
        assert manifest["registration_sha256"] == digest(registered)
    before, requests, executions, stage_checks = {}, [], [], []
    for root in manifest["root_ids"]:
        directory = output / root["path"]
        provider = study._provider(directory, root, manifest)
        before[root["root_id"]] = ledger_hash(directory)
        with provider._db() as db:
            rows = list(db.execute("SELECT * FROM chat_requests ORDER BY request_id"))
        owned = {}
        for folder in sorted(directory.glob("step-*")):
            context = read_record(folder / "task-context.json")
            phase = context["phase"]
            expected_contract, bound = study.binding(root["kind"], phase,
                root["kind"] == "dynamic" and phase == 2 and root["condition"] == "change")
            assert context["binding"] == bound
            for path in (folder / "requests").glob("*.json"):
                entry = read_record(path)
                assert entry["request_id"] not in owned
                owned[entry["request_id"]] = entry
                messages = entry["body"]["messages"]
                assert any(m["role"] == "system" and expected_contract in m["content"] for m in messages)
                if root["kind"] == "dynamic":
                    published = any(dynamic.PHASE_TWO in m["content"] for m in messages)
                    assert published == (phase == 2 and root["condition"] == "change")
                    stage_checks.append({"root_id": root["root_id"], "phase": phase,
                        "new_requirement_published": published, "binding": bound})
                # All tool execution supplied to the editor is from public cases.
                assert all(c["purpose"] == "public" for c in context["public_cases"])
                for source in folder.glob("public/*/environment/operations/*.json"):
                    assert read_record(source)["identity"]["case"]["purpose"] == "public"
        assert set(owned) == {r["request_id"] for r in rows}
        for row in rows:
            assert hashlib.sha256(row["request_json"]).hexdigest() == row["request_sha"]
            assert decode_json(row["request_json"]) == owned[row["request_id"]]["body"]
            if row["response_bytes"] is not None:
                assert hashlib.sha256(row["response_bytes"]).hexdigest() == row["response_sha"]
            if row["status"] == "complete":
                provider._replay(row)
            requests.append({"root_id": root["root_id"], "request_id": row["request_id"], "status": row["status"],
                "request_sha256": row["request_sha"], "response_sha256": row["response_sha"],
                "actual_tokens": row["actual_tokens"], "error_code": row["error_code"]})
        assert provider.summary() == read_record(directory / "root.json")["model_usage"]
        assert not provider.summary()["budget_overrun"]
        for path in sorted((directory / "execution/jobs").glob("*/result.json")):
            result = read_record(path)
            StudyExecutor.verify(result)
            item = result["identity"]["case"]
            executions.append({"path": str(path.relative_to(output)), "sha256": digest(result),
                "kind": item["kind"], "purpose": item["purpose"], "complete": result["metrics"]["complete"],
                "phase": item["phase"], "usage": result["usage"]})
    originals = (DurableChatProvider.generate, ScriptedChatProvider.generate, dynamic.evaluate, rl.evaluate)
    try:
        DurableChatProvider.generate = ScriptedChatProvider.generate = forbidden
        dynamic.evaluate = rl.evaluate = forbidden
        summary = study.execute(output, manifest, resume=True)
        assert analyze(output) == summary
    finally:
        DurableChatProvider.generate, ScriptedChatProvider.generate, dynamic.evaluate, rl.evaluate = originals
    assert before == {r["root_id"]: ledger_hash(output / r["path"]) for r in manifest["root_ids"]}
    assert summary["calls"] <= manifest["model_call_cap"]
    if manifest["origin"] == "natural":
        assert summary["reported_tokens"] is None or summary["reported_tokens"] <= manifest["token_cap"]
    value = {"validator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "manifest_sha256": digest(manifest), "summary_sha256": digest(summary),
        "request_status_counts": dict(Counter(r["status"] for r in requests)),
        "requests": requests, "executions": executions, "stages": stage_checks,
        "ledger_hashes": before, "completed_resume_ledger_unchanged": True,
        "verification_model_calls": 0, "verification_training_jobs": 0, "verification_deployments": 0,
        "model_calls": summary["calls"], "reported_tokens": summary["reported_tokens"],
        "execution_records": len(executions), "independent_execution_records": sum(e["purpose"] == "independent" for e in executions),
        "all_resource_accounts_within_cap": all(not a["over_budget"] for r in summary["roots"] for a in r["resources"]["accounts"])}
    save_record(output / "validation.json", value)
    return value


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    result = verify(parser.parse_args().output)
    print({k: v for k, v in result.items() if k not in ("requests", "executions", "stages", "ledger_hashes")})
