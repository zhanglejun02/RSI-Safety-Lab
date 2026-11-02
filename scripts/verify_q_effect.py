"""Offline source replay and no-model resume for a frozen q-effect run.

Use the run's archived wheel after repository sources change. No credentials or
network are needed; model generation is explicitly forbidden during resume.
"""
import argparse
from pathlib import Path
from unittest.mock import patch

from rsi_lab.contracts import CursorFamily, Partition
from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.errors import IntegrityError
from rsi_lab.frozen import implementation_fingerprint
from rsi_lab.infra.chat_models import DurableChatProvider, ScriptedChatProvider
from rsi_lab.serialization import decode_json, digest
from rsi_lab.studies.q_effect import ARMS, resume_run
from rsi_lab.tasks.cost_authorization import evaluate_source
from rsi_lab.tasks.platform_cost_authorization import CostAuthorizationTaskPackage


def verify(run):
    run = Path(run)
    manifest = read_record(run / "manifest.json")
    if manifest["scope"]["implementation_sha256"] != implementation_fingerprint():
        raise IntegrityError("Install the registered archived wheel before verification")
    config = manifest["config"]
    counts = {"replayed_attempts": 0, "replayed_requests": 0, "actual_violations": 0,
              "missing_attempts": 0, "missing_roots": 0}
    tasks = CostAuthorizationTaskPackage()
    for root in manifest["roots"]:
        directory = run / "roots" / root["root_id"]
        if (directory / "root-failure.json").exists():
            counts["missing_roots"] += 1
            continue
        interventions = read_record(directory / "interventions.json")["arms"]
        plan = tasks.plan(root["seed"] + 2000, CursorFamily.INDEPENDENT_EVALUATION,
                          Partition.DEV, config["horizon"], "delayed")
        for rep in range(config["repetitions"]):
            for arm in ARMS:
                expected_q = next(c for c in interventions[arm]["version"]["components"]
                                  if c["name"] == "experience_selector")
                for step in plan.rounds:
                    record = read_record(directory / "repetitions" / f"{rep:03d}" / arm /
                                         "continuation" / f"attempt-{step.attempt:04d}.json")
                    components = {c["name"]: c for c in record["active_version"]["components"]}
                    if components["experience_selector"] != expected_q:
                        raise IntegrityError("An executed descendant changed q")
                    if not record["outcome"]["complete"]:
                        counts["missing_attempts"] += 1
                        continue
                    result = evaluate_source(decode_json(components["policy"]["content_json"])["source"],
                                             step.streams, guard_mode="log_and_allow")
                    expected = record["evaluation"]
                    values = {"actual_violations": result["violations"],
                              "legal_completed": result["legal_completed"],
                              "legal_denominator": result["legal_total"],
                              "risk": int(result["violations"] > 0), "complete": result["complete"]}
                    if any(expected[k] != value for k, value in values.items()):
                        raise IntegrityError("Frozen descendant source does not reproduce the independent label")
                    if (record["outcome"]["risk"] != values["risk"] or
                            record["outcome"]["candidate_sha256"] != digest(record["active_version"])):
                        raise IntegrityError("Outcome is not bound to the replayed version and risk")
                    counts["replayed_attempts"] += 1
                    counts["replayed_requests"] += result["total_requests"]
                    counts["actual_violations"] += result["violations"]
    before = {name: (run / name).read_bytes() for name in ("summary.json", "report.md")}
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline verification attempted to generate a model response")
    with patch.object(DurableChatProvider, "generate", forbidden), patch.object(ScriptedChatProvider, "generate", forbidden):
        resume_run(run)
    if any((run / name).read_bytes() != raw for name, raw in before.items()):
        raise IntegrityError("No-model replay changed the frozen summary/report")
    return {**counts, "protocol_sha256": manifest["protocol_sha256"],
            "implementation_sha256": implementation_fingerprint(), "summary_and_report_byte_identical": True,
            "model_generation_forbidden": True, "summary_sha256": digest(read_record(run / "summary.json"))}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.run)
    save_record(args.output, result)
    print(result)
