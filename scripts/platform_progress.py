"""Read live platform progress without displaying prompts, responses or secrets."""
import argparse
import json
from pathlib import Path
import sqlite3

from rsi_lab.engine.continuation import read_record
from rsi_lab.serialization import decode_json


def inspect_progress(directory):
    directory = Path(directory)
    usage = {}
    for path in sorted(directory.rglob("*.sqlite")):
        if path.parent.name != "models" and not path.parent.name.endswith("-models"):
            continue
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2) as db:
            rows = db.execute("SELECT status, COUNT(*), SUM(actual_tokens) FROM chat_requests GROUP BY status").fetchall()
            errors = db.execute("SELECT error_code, COUNT(*) FROM chat_requests WHERE error_code IS NOT NULL GROUP BY error_code").fetchall()
        usage[str(path.relative_to(directory))] = {
            "statuses": {status: count for status, count, tokens in rows},
            "known_tokens": sum(tokens or 0 for status, count, tokens in rows),
            "errors": dict(errors),
        }
    prepared, decisions, attempts = {}, {}, {}
    for path in sorted(directory.rglob("prepared.json")):
        record = read_record(path)
        evidence = decode_json(record["draft"]["native_evidence_json"])
        prepared[str(path.relative_to(directory))] = {"native_valid": record["draft"]["native_valid"],
            "update_class": evidence.get("update_class"), "score_gain": evidence.get("score_gain")}
    for path in sorted(directory.rglob("online.json")):
        record = read_record(path)
        decisions[str(path.relative_to(directory))] = record["decision"]["disposition"]
    for path in sorted(directory.rglob("attempt-*.json")):
        record = read_record(path)
        if "outcome" in record:
            attempts[str(path.relative_to(directory))] = {"complete": record["outcome"]["complete"],
                "risk": record["outcome"]["risk"], "missing_reason": record.get("missing_reason")}
    return {"live_progress_only": True, "completed_report": (directory / "summary.json").exists(),
            "models": usage, "prepared": prepared, "decisions": decisions, "attempts": attempts}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(inspect_progress(args.run), ensure_ascii=False, indent=2))
