"""Independent source reexecution and forbidden-model replay of a frozen run."""
import argparse
from pathlib import Path
import sqlite3
from unittest.mock import patch

from rsi_lab.api import PreparedUpdate, SystemSnapshot, SystemVersion
from rsi_lab.contracts import CursorFamily, Partition
from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.errors import IntegrityError
from rsi_lab.frozen import implementation_fingerprint
from rsi_lab.infra.chat_models import DurableChatProvider, ScriptedChatProvider
from rsi_lab.serialization import decode_json, digest
from rsi_lab.studies.optimized_q import resume_run, _slot_path
from rsi_lab.tasks.cost_authorization import evaluate_source
from rsi_lab.tasks.platform_cost_authorization import CostAuthorizationTaskPackage


def verify(run):
    run = Path(run)
    manifest = read_record(run / "manifest.json")
    if manifest["scope"]["implementation_sha256"] != implementation_fingerprint():
        raise IntegrityError("Use the archived matching wheel for verification")
    summary = read_record(run / "summary.json")
    tasks = CostAuthorizationTaskPackage()
    counts = {"public_programs": 0, "public_requests": 0, "adopted_programs": 0,
              "adopted_requests": 0, "adopted_violations": 0, "candidate_programs": 0,
              "candidate_requests": 0, "candidate_violations": 0, "baseline_programs": 0,
              "baseline_requests": 0, "missing_attempts": 0}

    def check(actual, expected):
        if any(value != expected.get(key) for key, value in actual.items()):
            raise IntegrityError("Stored outcome differs from actual source reexecution")

    def public(source, seed, expected):
        result = evaluate_source(source, tasks.public_suite(seed).rounds[0].streams, guard_mode="log_and_allow")
        check({"complete": result["complete"], "risk": int(result["violations"] > 0) if result["complete"] else None,
            "synthetic_units": result["cost"], "legal_completed": result["legal_completed"],
            "legal_total": result["legal_total"]}, expected)
        check({"correct_requests": result["correct_requests"], "total_requests": result["total_requests"]}, expected["feedback"])
        counts["public_programs"] += 1
        counts["public_requests"] += result["total_requests"]

    def independent(source, step, expected, category):
        result = evaluate_source(source, step.streams, guard_mode="log_and_allow")
        check({"complete": result["complete"], "risk": int(result["violations"] > 0) if result["complete"] else None,
            "actual_violations": result["violations"], "utility": result["utility"],
            "legal_completed": result["legal_completed"], "legal_denominator": result["legal_total"]}, expected)
        counts[category + "_programs"] += 1
        counts[category + "_requests"] += result["total_requests"]
        if category != "baseline": counts[category + "_violations"] += result["violations"]
        return result

    imported = manifest["imported"]
    initial = SystemSnapshot.from_dict(imported["checkpoint"])
    previous = SystemSnapshot.from_dict(imported["previous"])
    if "public_history" in imported and digest(imported["public_history"]) != imported["history_sha256"]:
        raise IntegrityError("Public history provenance changed")
    precheck = read_record(run / "checkpoint-recheck/result.json") if (run / "checkpoint-recheck/result.json").exists() else None
    if precheck:
        public(previous.version.component("policy")["source"], manifest["config"]["checkpoint_recheck_seed"], precheck["before"])
        public(initial.version.component("policy")["source"], manifest["config"]["checkpoint_recheck_seed"], precheck["after"])
    for sample in summary["samples"]:
        slot = _slot_path(run, sample)
        initial_arm = SystemSnapshot.from_dict(manifest["arms"][sample["arm"]])
        plan = tasks.plan(sample["block"] + 40000, CursorFamily.INDEPENDENT_EVALUATION, Partition.DEV, 2, "revocation")
        for step in plan.rounds:
            record = read_record(slot / "continuation" / f"attempt-{step.attempt:04d}.json")
            version = SystemVersion.from_dict(record["active_version"])
            if version.component("experience_selector") != initial_arm.version.component("experience_selector"):
                raise IntegrityError("Executed program changed q")
            if record["outcome"]["complete"]:
                independent(version.component("policy")["source"], step, record["evaluation"], "adopted")
            else:
                counts["missing_attempts"] += 1
            if record["prepared"]:
                prepared = PreparedUpdate.from_dict(record["prepared"])
                native = decode_json(prepared.draft.native_evidence_json)
                public(prepared.base.version.component("policy")["source"], sample["block"] + 50000 + step.attempt, native["incumbent"])
                if "proposal_error" not in native:
                    source = prepared.draft.candidate.version.component("policy")["source"]
                    public(source, sample["block"] + 50000 + step.attempt, native["candidate"])
                    effect = read_record(slot / f"candidate-effect-{step.attempt}/result.json")
                    cp = tasks.plan(sample["block"] + 80000, CursorFamily.INDEPENDENT_EVALUATION, Partition.DEV, 1, "revocation")
                    if effect["source_sha256"] != digest(source) or effect["plan_sha256"] != digest(cp):
                        raise IntegrityError("Auxiliary proposal effect not bound to source and plan")
                    replayed = independent(source, cp.rounds[0], effect["evaluation"], "candidate")
                    check({name: replayed["call_counts"].get(name, 0) for name in ("version", "ask", "now", "execute")}, effect["call_counts"])
        fresh_path = slot / "fresh-public/result.json"
        if fresh_path.exists():
            fresh = read_record(fresh_path)
            final = SystemSnapshot.from_dict(read_record(slot / "final-snapshot.json"))
            public(initial_arm.version.component("policy")["source"], sample["block"] + 60000, fresh["before"])
            public(final.version.component("policy")["source"], sample["block"] + 60000, fresh["after"])
    for block in summary["baseline"]:
        plan = tasks.plan(int(block) + 40000, CursorFamily.INDEPENDENT_EVALUATION, Partition.DEV, 2, "revocation")
        for step in plan.rounds:
            record = read_record(run / "baseline" / block / "continuation" / f"attempt-{step.attempt:04d}.json")
            independent(initial.version.component("policy")["source"], step, record["evaluation"], "baseline")
    before = {name: (run / name).read_bytes() for name in ("summary.json", "report.md")}
    def requests():
        with sqlite3.connect(f"file:{run / 'models/root.sqlite'}?mode=ro", uri=True) as db:
            return db.execute("SELECT request_id,request_sha,status,response_sha,actual_tokens FROM chat_requests ORDER BY request_id").fetchall()
    original_requests = requests()
    def forbidden(*args, **kwargs): raise AssertionError("Offline replay attempted a model call")
    with patch.object(DurableChatProvider, "generate", forbidden), patch.object(ScriptedChatProvider, "generate", forbidden):
        resume_run(run)
    if requests() != original_requests or any((run / name).read_bytes() != data for name, data in before.items()):
        raise IntegrityError("Offline resume changed model journal or report")
    return {**counts, "implementation_sha256": implementation_fingerprint(),
        "summary_sha256": digest(summary), "model_generation_forbidden": True,
        "summary_report_and_requests_unchanged": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.run)
    save_record(args.output, result)
    print(result)
