"""Replay the actual public history and diagnostic programs without a model.

After source changes, install the registered archived wheel before using this
script. Historical independent labels are neither opened nor used. Completed
and early-stop runs are supported; unknown proposals remain unknown.
"""
import argparse
from pathlib import Path
import sqlite3
from unittest.mock import patch

from rsi_lab.api import PreparedUpdate
from rsi_lab.contracts import CursorFamily, Partition
from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.errors import IntegrityError
from rsi_lab.frozen import implementation_fingerprint
from rsi_lab.infra.chat_models import DurableChatProvider, ScriptedChatProvider
from rsi_lab.serialization import decode_json, digest
from rsi_lab.studies.experience_diagnostic import resume_run
from rsi_lab.subjects.cost_q import ExperienceSelector, make_initial_snapshot
from rsi_lab.subjects.optimization_memory import OptimizationEpisode, PublicOptimizationExecution, episode_status
from rsi_lab.tasks.cost_authorization import evaluate_source
from rsi_lab.tasks.platform_cost_authorization import CostAuthorizationTaskPackage, CostPlan


def _check(actual, expected, message):
    if any(expected.get(key) != value for key, value in actual.items()):
        raise IntegrityError(message)


def _risk(result):
    return 1 if result["violations"] else 0 if result["complete"] else None


def _requests(run):
    """Prove no physical model journal changed during offline verification."""
    records = {}
    for path in sorted(run.rglob("*.sqlite")):
        if path.name not in ("root.sqlite", "editor.sqlite"):
            continue
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            records[str(path.relative_to(run))] = db.execute(
                "SELECT request_id,request_sha,status,response_sha FROM chat_requests ORDER BY request_id").fetchall()
    return records


def _verify_public(execution):
    plan = CostPlan.from_dict(decode_json(execution.plan_json))
    if plan.family != CursorFamily.PUBLIC_DEVELOPMENT or plan.partition != Partition.DEV:
        raise IntegrityError("History contains non-public or non-development evaluation")
    results = [evaluate_source(execution.source, step.streams, guard_mode="log_and_allow") for step in plan.rounds]
    complete = all(value["complete"] for value in results)
    violations = sum(value["violations"] for value in results)
    correct = sum(value["correct_requests"] for value in results)
    total = sum(value["total_requests"] for value in results)
    evidence = execution.evaluation
    _check({"complete": complete, "risk": 1 if violations else 0 if complete else None,
            "legal_completed": sum(value["legal_completed"] for value in results),
            "legal_total": sum(value["legal_total"] for value in results),
            "synthetic_units": sum(value["cost"] for value in results),
            "public_contract_violations": violations}, evidence,
           "Public history source does not reproduce stored public execution measurements")
    _check({"complete_correct": bool(complete and total and correct == total),
            "correct_requests": correct, "total_requests": total}, evidence["feedback"],
           "Public history correctness cannot be reproduced from its source")
    return {"requests": total, "violations": violations}


def _verify_effect(source, plan, location):
    saved = read_record(location / "result.json")
    if saved["source_sha256"] != digest(source) or saved["plan_sha256"] != digest(plan):
        raise IntegrityError("Diagnostic outcome is not bound to this source and plan")
    result = evaluate_source(source, plan.rounds[0].streams, guard_mode="log_and_allow")
    _check({"complete": result["complete"], "risk": _risk(result),
            "actual_violations": result["violations"], "legal_completed": result["legal_completed"],
            "legal_denominator": result["legal_total"], "utility": result["utility"],
            "blocked_violations": result["blocked"], "unauthorized_requests": result["attempts"]},
           saved["evaluation"], "Actual diagnostic program does not reproduce independent effects")
    _check({name: result["call_counts"].get(name, 0) for name in ("ask", "version", "now", "execute")},
           saved["call_counts"], "Actual diagnostic tool behavior differs from stored trace counters")
    if result["cost"] != saved["service_units"]:
        raise IntegrityError("Diagnostic replay service cost changed")
    return result


def verify(run):
    run = Path(run)
    manifest = read_record(run / "manifest.json")
    if manifest["scope"]["implementation_sha256"] != implementation_fingerprint():
        raise IntegrityError("Install the registered archived wheel before verification")
    config = manifest["config"]
    history = read_record(run / "history/collection.json")
    if read_record(run / "history-frozen.json") != {"collection_sha256": digest(history), "available": history["available"]}:
        raise IntegrityError("Collected public history differs from the frozen diagnostic input")
    counts = {"public_history_executions": 0, "public_history_requests": 0, "public_history_violations": 0,
              "diagnostic_candidate_programs": 0, "diagnostic_adopted_programs": 0,
              "diagnostic_requests": 0, "candidate_violations": 0, "adopted_violations": 0,
              "unconfirmed_or_nonexecutable_proposals": 0, "history_edit_failures": 0}
    verified = set()
    def public(execution):
        key = digest(execution)
        if key in verified:
            return
        result = _verify_public(execution)
        counts["public_history_executions"] += 1
        counts["public_history_requests"] += result["requests"]
        counts["public_history_violations"] += result["violations"]
        verified.add(key)
    episodes = tuple(OptimizationEpisode.from_dict(raw) for raw in history["episodes"])
    for name, episode in zip(("relevant", "placebo"), episodes):
        if history["statuses"][name] != episode_status(episode):
            raise IntegrityError("Historical failure/repair status changed")
        for execution in (episode.before, episode.candidate, episode.repair):
            if execution is not None:
                public(execution)
    # Rejected repair attempts remain evidence too. Deduplicate selected attempts
    # which appear both here and in the final episode, never count them twice.
    for path in sorted((run / "history").glob("*/edit-*.json")):
        record = read_record(path)
        if record.get("execution") is not None:
            execution = PublicOptimizationExecution.from_dict(record["execution"])
            if record["model_receipt"]["content"] != {"source": execution.source}:
                raise IntegrityError("History source differs from the actual editor response")
            public(execution)
        else:
            counts["history_edit_failures"] += 1
    summary = read_record(run / "summary.json")
    tasks = CostAuthorizationTaskPackage()
    for row in summary["rows"]:
        block, mode, arm, rep = (row[key] for key in ("block", "mode", "arm", "replicate"))
        location = run / "diagnostic" / str(block) / mode / arm / str(rep)
        key = history["ids"]["relevant" if arm == "relevant" else "placebo"]
        selector = ExperienceSelector((key, *(name for name in history["memory"] if name != key)),
                                      limit=1, max_context_bytes=16384)
        initial = make_initial_snapshot(block, history["memory"], selector)
        frozen = read_record(location / "native-frozen.json")
        path = location / "prepared.json"
        prepared = PreparedUpdate.from_dict(read_record(path)) if path.exists() else None
        if prepared and prepared.base != initial:
            raise IntegrityError("Diagnostic proposal did not start from the registered checkpoint")
        native = decode_json(prepared.draft.native_evidence_json) if prepared else {}
        adopted = prepared.draft.candidate if prepared and prepared.draft.native_valid else initial
        if (frozen["selected_snapshot_sha256"] != adopted.sha256 or
                frozen["prepared_sha256"] != digest(prepared) or
                digest(frozen["selected_context"]) != digest(selector.select(history["memory"]))):
            raise IntegrityError("Actual adopted program or selected experience changed")
        plan = tasks.plan(block + 20000, CursorFamily.INDEPENDENT_EVALUATION, Partition.DEV, 1, "revocation")
        if digest(plan) != manifest["task_anchors"][str(block)]["diagnostic"]:
            raise IntegrityError("Diagnostic task parameters differ from registration")
        if row["candidate_valid"]:
            source = native["model_receipt"]["content"]["source"]
            if digest(source) != row["proposed_source_sha256"]:
                raise IntegrityError("Candidate report is not tied to the model-produced source")
            result = _verify_effect(source, plan, location / "candidate-effect")
            _check({"candidate_risk": _risk(result), "candidate_utility": result["utility"],
                    "candidate_actual_violations": result["violations"]}, row,
                   "Candidate diagnostic summary differs from actual effect replay")
            counts["diagnostic_candidate_programs"] += 1
            counts["diagnostic_requests"] += result["total_requests"]
            counts["candidate_violations"] += result["violations"]
        else:
            if row["candidate_risk"] is not None or row["candidate_utility"] is not None:
                raise IntegrityError("Unknown or nonexecutable proposal was assigned a safety outcome")
            counts["unconfirmed_or_nonexecutable_proposals"] += 1
        source = adopted.version.component("policy")["source"]
        if digest(source) != row["selected_source_sha256"]:
            raise IntegrityError("Adopted source report changed")
        result = _verify_effect(source, plan, location / "adopted-effect")
        _check({"adopted_risk": _risk(result), "adopted_utility": result["utility"]}, row,
               "Adopted diagnostic summary differs from actual effect replay")
        counts["diagnostic_adopted_programs"] += 1
        counts["diagnostic_requests"] += result["total_requests"]
        counts["adopted_violations"] += result["violations"]
    before = {name: (run / name).read_bytes() for name in ("summary.json", "report.md")}
    requests = _requests(run)
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline verification attempted model generation")
    with patch.object(DurableChatProvider, "generate", forbidden), patch.object(ScriptedChatProvider, "generate", forbidden):
        resume_run(run)
    if any((run / name).read_bytes() != body for name, body in before.items()) or _requests(run) != requests:
        raise IntegrityError("No-model resume changed frozen reports or model request journals")
    return {**counts, "history_available": history["available"], "status": summary["status"],
            "protocol_sha256": manifest["protocol_sha256"], "implementation_sha256": implementation_fingerprint(),
            "summary_and_report_byte_identical": True, "model_request_journals_unchanged": True,
            "model_generation_forbidden": True, "summary_sha256": digest(summary)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.run)
    save_record(args.output, result)
    print(result)
