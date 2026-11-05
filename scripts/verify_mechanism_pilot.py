"""Verify a completed screen only from its exact matching workspace revision.

For historical runs after source changes, use scripts/review_frozen_run.py with
the corresponding run, registration and a new output directory instead.
"""
import hashlib
from pathlib import Path

from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.errors import IntegrityError
from rsi_lab.infra.chat_models import DurableChatProvider
from rsi_lab.serialization import decode_json, digest
from rsi_lab.studies import mechanism_pilot as study
from rsi_lab.studies.stress_pilot import source_in_request
from rsi_lab.tasks.bounded_language import editor_contract
from rsi_lab.tasks.mechanism_feedback import NOTE


def forbidden(*args, **kwargs):
    raise AssertionError("Verification cannot send a model request")


def ledger_digest(rows):
    return digest([{key: {"blob_sha256": hashlib.sha256(row[key]).hexdigest()}
                    if isinstance(row[key], bytes) else row[key] for key in row.keys()} for row in rows])


def verify():
    run = study.OUTPUT
    registered = read_record(study.REGISTRATION / "registration.json")
    current, snapshot = study.registration()
    if registered != current or (study.REGISTRATION / "source-snapshot.zip").read_bytes() != snapshot:
        raise IntegrityError("Workspace differs from the frozen registration. For historical runs use "
            "scripts/review_frozen_run.py --run runs/mechanism-kimi-20261105 "
            "--registration runs/mechanism-registration-20261105 --output <new-directory>; "
            "do not overwrite the historical registration.")
    frozen = read_record(run / "generation_complete.json")
    manifest = read_record(run / "manifest.json")
    assert manifest["registration_sha256"] == digest(registered)
    _, settings = study.configuration()
    request_rows, feedback_rows, ledger_hashes, usage = [], [], {}, []
    for entry in manifest["root_ids"]:
        directory = run / entry["path"]
        root = read_record(directory / "root.json")
        assert frozen["roots_sha256"][entry["root_id"]] == digest(root)
        provider = DurableChatProvider(directory / "model.sqlite", settings, transport=forbidden)
        with provider._db() as db:
            rows = list(db.execute("SELECT * FROM chat_requests ORDER BY request_id"))
        persisted = {}
        for path in directory.glob("step-*/requests/*.json"):
            record = read_record(path)
            assert record["request_id"] not in persisted
            persisted[record["request_id"]] = record
        assert set(persisted) == {row["request_id"] for row in rows}
        for row in rows:
            assert hashlib.sha256(row["request_json"]).hexdigest() == row["request_sha"]
            body = decode_json(row["request_json"])
            assert body == persisted[row["request_id"]]["body"]
            assert row["request_sha"] == persisted[row["request_id"]]["request_sha256"]
            assert any(m["role"] == "system" and editor_contract(entry["domain"]) in m["content"]
                       for m in body["messages"])
            response_reparsed = False
            if row["response_bytes"] is not None:
                assert hashlib.sha256(row["response_bytes"]).hexdigest() == row["response_sha"]
            if row["status"] == "complete":
                provider._replay(row)
                response_reparsed = True
            value = {"root_id": entry["root_id"], "request_id": row["request_id"],
                "request_sha256": row["request_sha"], "response_sha256": row["response_sha"],
                "status": row["status"], "response_reparsed": response_reparsed,
                "actual_tokens": row["actual_tokens"], "error_code": row["error_code"],
                "body_bytes": len(row["request_json"]), "full_contract_in_system_message": True}
            request_rows.append(value)
            if entry["domain"] == "feedback":
                feedback_rows.append({**value, "supplier_instruction_in_request": source_in_request(NOTE, body)})
        actual_usage = provider.summary()
        assert actual_usage == root["model_usage"]
        assert actual_usage["dispatched_calls"] <= 3 and not actual_usage["budget_overrun"]
        usage.append(actual_usage)
        ledger_hashes[entry["root_id"]] = ledger_digest(rows)

    # This path verifies the frozen lineage and replays independent effects. A
    # completed resume returns before credential loading or provider generation.
    original = DurableChatProvider.generate
    DurableChatProvider.generate = forbidden
    try:
        summary = study.run(resume=True)
    finally:
        DurableChatProvider.generate = original
    after = {}
    for entry in manifest["root_ids"]:
        provider = DurableChatProvider(run / entry["path"] / "model.sqlite", settings, transport=forbidden)
        with provider._db() as db:
            rows = list(db.execute("SELECT * FROM chat_requests ORDER BY request_id"))
        after[entry["root_id"]] = ledger_digest(rows)
    assert ledger_hashes == after
    calls = sum(item["dispatched_calls"] for item in usage)
    tokens = sum(item["actual_total_tokens"] or 0 for item in usage)
    assert calls <= manifest["model_call_cap"] and tokens <= manifest["token_cap"]
    value = {"validator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "registration_sha256": digest(registered), "frozen_generation_sha256": digest(frozen),
        "independent_analysis_sha256": digest(summary), "model_calls_during_verification": 0,
        "resume_ledger_unchanged": True, "journals_sha256": ledger_hashes,
        "dispatched_calls": calls, "reported_total_tokens": tokens,
        "request_status_counts": {s: sum(r["status"] == s for r in request_rows)
                                  for s in sorted({r["status"] for r in request_rows})},
        "requests": request_rows, "feedback_exposure": feedback_rows,
        "protected_case_records_replayed": len(list((run / "analysis/roots").glob("*/sources/*/case-*.json"))),
        "source_snapshot_verified": True, "budget_overrun": False}
    save_record(run / "validation.json", value)
    return value


if __name__ == "__main__":
    result = verify()
    print({key: value for key, value in result.items() if key not in ("requests", "feedback_exposure", "journals_sha256")})
