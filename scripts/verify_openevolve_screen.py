"""No-network replay of every recorded program execution and model receipt."""
import argparse
import json
from pathlib import Path
import re

from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.errors import IntegrityError
from rsi_lab.frozen import implementation_fingerprint
from rsi_lab.serialization import decode_json, digest
from rsi_lab.subjects.openevolve import verify_upstream
from rsi_lab.studies.openevolve_screen import _provider, _score, _paired_test, execution_accounting, _report, verify_implementation
from rsi_lab.tasks import cost_authorization as auth, retail_workflow as retail


def verify(output):
    output = Path(output)
    manifest = read_record(output / "manifest.json")
    summary = read_record(output / "summary.json")
    verify_implementation(output, manifest)
    if manifest["upstream_lock_sha256"] != digest(verify_upstream()):
        raise IntegrityError("Native OpenEvolve source lock changed")
    sources = {}
    for path in output.glob("roots/**/request.json"):
        request = read_record(path)
        if "candidate" in request:
            policy = next(c for c in request["candidate"]["version"]["components"] if c["name"] == "policy")
            source = decode_json(policy["content_json"])["source"]
        elif type(request.get("source")) is str:
            source = request["source"]
        else:
            continue
        sources[digest(source)] = source
    counts = {"executions": 0, "requests": 0, "model_receipts": 0, "continuation_attempts": 0}
    def replay(result, suite, name):
        source = sources[result["source_sha256"]]
        if name == "authorization":
            measured = auth.evaluate_source(source, suite, result["guard_mode"])
            auth.verify_result(result, suite)
        else:
            measured = retail.evaluate_source(source, suite, result["guard"])
            retail.verify_result(result, suite)
        if measured != result:
            raise IntegrityError("Frozen source replay differs from persisted execution")
        counts["executions"] += 1
        counts["requests"] += _score(result, name)["total_requests"]
    for path in output.glob("roots/**/operations/*.json"):
        record = read_record(path)
        if "result" in record and "stream" in record:
            name = "retail" if record["stream"]["stream_id"].startswith("retail-") else "authorization"
            if record["source_sha256"] != record["result"]["source_sha256"]:
                raise IntegrityError("Execution record source changed")
            replay(record["result"], [record["stream"]], name)
    for path in output.glob("roots/**/result.json"):
        if (path.parent / "request.json").exists():
            request = read_record(path.parent / "request.json")
            if "purpose" in request:
                replay(read_record(path), request["suite"], request["task"])
    for path in output.glob("roots/**/attempt-*.json"):
        row = read_record(path)
        if digest(row["active_version"]) != row["outcome"]["candidate_sha256"]:
            raise IntegrityError("Executed version differs from outcome")
        name = "retail" if "/retail-" in path.as_posix() else "authorization"
        scores = [_score(t["result"], name) for t in row["trace"]]
        for key in ("actual_violations", "legal_completed"):
            if sum(s[key] for s in scores) != row["evaluation"][key]:
                raise IntegrityError("Continuation outcome differs from operation effects")
        counts["continuation_attempts"] += 1
    provider = _provider(output, manifest["config"])
    with provider._db() as db:
        for row in db.execute("SELECT * FROM chat_requests WHERE status='complete'"):
            provider._replay(row)
            counts["model_receipts"] += 1
    if provider.summary() != summary["usage"]:
        raise IntegrityError("Usage summary changed")
    if execution_accounting(output) != summary["execution_accounting"]:
        raise IntegrityError("Execution accounting differs")
    for task in ("authorization", "retail"):
        if _paired_test([r for r in summary["roots"] if r["task"] == task]) != summary["repetition_analysis"][task]:
            raise IntegrityError("Repeat statistics differ")
    def normalize_report(text):
        # Canonical persistence sorts mapping keys; Markdown JSON blocks may retain
        # their original insertion order. Compare data, preserving all prose/values.
        return re.sub(r"```json\n(.*?)\n```", lambda m: "```json\n" +
                      json.dumps(json.loads(m[1]), sort_keys=True) + "\n```", text, flags=re.S)
    if normalize_report((output / "report.md").read_text()) != normalize_report(_report(summary)):
        raise IntegrityError("Report differs from summary")
    result = {"verified": True, "network_calls": 0, "model_generation_calls": 0,
        "implementation_sha256": implementation_fingerprint(), "summary_sha256": digest(summary), **counts}
    save_record(output / "verification.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    print(__import__("json").dumps(verify(args.run), indent=2))
