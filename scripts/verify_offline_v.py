"""Replay frozen v evidence from source, not predicted risks or saved labels.

Use the run's frozen wheel if the workspace implementation has changed.
No imports from the original model run or model requests are needed.
"""
import argparse
from pathlib import Path
from unittest.mock import patch

from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.errors import IntegrityError
from rsi_lab.frozen import implementation_fingerprint
from rsi_lab.infra.chat_models import DurableChatProvider, ScriptedChatProvider
from rsi_lab.serialization import canonical_bytes, digest
from rsi_lab.studies.offline_v import ARMS, resume_run
from rsi_lab.tasks import cost_authorization as task


def verify(run):
    run = Path(run)
    manifest = read_record(run / "manifest.json")
    if manifest["implementation_sha256"] != implementation_fingerprint():
        raise IntegrityError("Install the frozen run's implementation before replay")
    counts = {"program_suite_executions": 0, "streams": 0, "requests": 0,
              "verification_requests": 0, "independent_requests": 0}

    def check(actual, expected):
        if canonical_bytes(actual) != canonical_bytes(expected):
            raise IntegrityError("Source replay differs from persisted v evidence")

    def execute(source, suite, directory, phase):
        # Direct interpreter execution provides a second path around the study driver.
        result = task.evaluate_source(source, suite, guard_mode="log_and_allow")
        saved = read_record(directory / "result.json")
        check({k: result[k] for k in ("source_sha256", "suite_sha256", "complete", "correct_requests", "total_requests", "cost")},
              {k: saved[k] for k in ("source_sha256", "suite_sha256", "complete", "correct_requests", "total_requests", "cost")})
        check({"actual_violations": result["violations"], "legal_completed": result["legal_completed"],
               "legal_denominator": result["legal_total"], "utility": result["utility"],
               "unauthorized_requests": result["attempts"], "blocked_violations": result["blocked"],
               "complete": result["complete"]},
              {k: saved["evaluation"][k] for k in ("actual_violations", "legal_completed", "legal_denominator", "utility",
                                                  "unauthorized_requests", "blocked_violations", "complete")})
        for stream, actual in zip(suite, result["streams"]):
            slot = digest({"namespace": "fixed-program", "task": stream["stream_id"]})
            receipt = read_record(directory / "environment/operations" / (slot + ".json"))
            check(receipt["stream"], stream)
            check(receipt["source_sha256"], digest(source))
            task.verify_result(receipt["result"], [stream])
            check(receipt["result"]["streams"], [actual])
        counts["program_suite_executions"] += 1
        counts["streams"] += len(suite)
        counts["requests"] += result["total_requests"]
        if phase in ("verification", "independent"):
            counts[phase + "_requests"] += result["total_requests"]

    for case in manifest["imported"]["candidates"]:
        if case["source"] is None:
            continue
        folder = run / "candidates" / case["candidate_id"]
        protocol = manifest["protocols"][case["history_group"]]
        for key, field in (("before", "before_source"), ("candidate", "source")):
            execute(case[field], case["cost_suite"], folder / "public" / key, "public")
            execute(case[field], protocol["public_recheck"], folder / "fresh" / key, "fresh")
            for block, suite in protocol["independent"].items():
                execute(case[field], suite, folder / "independent" / block / key, "independent")
        pool = {s["stream_id"]: s for s in protocol["public_pool"]}
        for arm in ARMS:
            execute(case["source"], [pool[t] for t in protocol["plans"][arm]], folder / "verification" / arm, "verification")
    saved = {name: (run / name).read_bytes() for name in ("summary.json", "report.md", "decisions-frozen.json")}
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline diagnostic attempted model generation")
    with patch.object(DurableChatProvider, "generate", forbidden), patch.object(ScriptedChatProvider, "generate", forbidden):
        resume_run(run)
    if any((run / name).read_bytes() != content for name, content in saved.items()):
        raise IntegrityError("Completed offline replay changed frozen outputs")
    return {**counts, "model_generation_forbidden": True, "decisions_summary_report_unchanged": True,
            "implementation_sha256": implementation_fingerprint(), "manifest_sha256": digest(manifest),
            "summary_sha256": digest(read_record(run / "summary.json"))}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.run)
    save_record(args.output, result)
    print(result)
