"""Read-only accounting for direct history edits and diagnostic activations.

These operations do not create continuation ``attempt-*.json`` records. Their
counts must come from their own durable artifacts, not the continuation ledger.
Unknown responses and missing activation/effect receipts remain explicit.
"""
from contextlib import closing
from pathlib import Path
import sqlite3

from rsi_lab.api import PreparedUpdate
from rsi_lab.engine.continuation import read_record
from rsi_lab.errors import IntegrityError
from rsi_lab.serialization import decode_json, digest
from .cost_q import _ledger


def _editor_requests(directory):
    counts = {"dispatched_requests": 0, "model_requests": 0, "scripted_control_requests": 0,
              "complete_responses": 0, "invalid_responses": 0, "unknown_responses": 0}
    seen = set()
    for path in sorted(Path(directory).rglob("*.sqlite")):
        if path.name not in ("editor.sqlite", "root.sqlite"):
            continue
        identity = (path.stat().st_dev, path.stat().st_ino)
        if identity in seen:
            continue
        seen.add(identity)
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            metadata = decode_json(db.execute("SELECT value FROM provider_metadata WHERE id=1").fetchone()[0])
            statuses = [row[0] for row in db.execute("SELECT status FROM chat_requests")]
        if metadata["origin"] not in ("natural", "control") or any(
                status not in ("complete", "invalid", "pending", "unknown") for status in statuses):
            raise IntegrityError("Unrecognized durable editor origin or request state")
        counts["dispatched_requests"] += len(statuses)
        counts["model_requests" if metadata["origin"] == "natural" else "scripted_control_requests"] += len(statuses)
        counts["complete_responses"] += statuses.count("complete")
        counts["invalid_responses"] += statuses.count("invalid")
        counts["unknown_responses"] += sum(status in ("pending", "unknown") for status in statuses)
    return counts


def execution_work(directory):
    """Physical task work only; direct edit counts use their own receipts."""
    execution = _ledger(directory)
    for key in ("attempts", "completed_attempts", "native_acceptances", "actual_source_changes"):
        execution.pop(key)
    return execution


def experience_work(output, rows):
    """Return separate physical execution, diagnostic and history accounting.

    ``rows`` identifies completed reporting slots, not model calls. Every
    physical model journal is counted once in its scope, including failed or
    unknown calls without a proposal. A native acceptance is distinct from a
    confirmed activation and from an actual policy source change.
    """
    output = Path(output)
    manifest = read_record(output / "manifest.json")
    execution = execution_work(output)
    registered = {tuple(job[key] for key in ("block", "mode", "arm", "replicate"))
                  for job in manifest["submission_order"]}
    observed = [tuple(row[key] for key in ("block", "mode", "arm", "replicate")) for row in rows]
    if len(set(observed)) != len(observed) or not set(observed) <= registered:
        raise IntegrityError("Diagnostic accounting contains duplicate or unregistered slots")
    diagnostic = {"registered_slots": len(registered), "observed_slots": len(observed),
        "unreported_slots": len(registered) - len(observed), "prepared_proposals": 0,
        "proposal_failure_records": 0, "executable_proposals": 0,
        "native_acceptances": 0, "native_rejections": 0,
        "confirmed_activations": 0, "activated_source_changes": 0,
        "missing_activation_records": 0, "unknown_activation_outcomes": 0,
        "complete_candidate_evaluations": 0, "incomplete_candidate_evaluations": 0,
        "missing_candidate_evaluations": 0, "complete_adopted_evaluations": 0,
        "incomplete_adopted_evaluations": 0, "missing_adopted_evaluations": 0,
        "editor_requests": _editor_requests(output / "diagnostic")}
    for block, mode, arm, replicate in observed:
        location = output / "diagnostic" / str(block) / mode / arm / str(replicate)
        prepared_path = location / "prepared.json"
        diagnostic["proposal_failure_records"] += (location / "proposal-failure.json").exists()
        prepared = PreparedUpdate.from_dict(read_record(prepared_path)) if prepared_path.exists() else None
        if prepared:
            diagnostic["prepared_proposals"] += 1
            diagnostic["native_acceptances" if prepared.draft.native_valid else "native_rejections"] += 1
            activation_path = location / "activation.json"
            if not activation_path.exists():
                diagnostic["missing_activation_records"] += 1
                diagnostic["unknown_activation_outcomes"] += bool(prepared.draft.native_valid)
            else:
                record = read_record(activation_path)
                receipt = record["activation"]
                accepted = record["decision"]["disposition"] == "accept"
                if accepted != prepared.draft.native_valid:
                    raise IntegrityError("Native diagnostic decision disagrees with prepared validation")
                if receipt is not None:
                    if (not accepted or receipt["before_sha256"] != prepared.base.sha256 or
                            receipt["after_sha256"] != prepared.draft.candidate.sha256):
                        raise IntegrityError("Diagnostic activation is not bound to its prepared snapshots")
                    frozen = read_record(location / "native-frozen.json")
                    if frozen["selected_snapshot_sha256"] != receipt["after_sha256"]:
                        raise IntegrityError("Confirmed activation differs from actual frozen selection")
                    diagnostic["confirmed_activations"] += 1
                    diagnostic["activated_source_changes"] += (
                        prepared.base.version.component("policy")["source"] !=
                        prepared.draft.candidate.version.component("policy")["source"])
                elif accepted:
                    diagnostic["unknown_activation_outcomes"] += 1
        for name in ("candidate", "adopted"):
            path = location / f"{name}-effect/result.json"
            if not path.exists():
                diagnostic[f"missing_{name}_evaluations"] += 1
                continue
            effect = read_record(path)
            if name == "candidate":
                diagnostic["executable_proposals"] += 1
            diagnostic[f"{'complete' if effect['evaluation']['complete'] else 'incomplete'}_{name}_evaluations"] += 1
    edits = [read_record(path) for path in sorted((output / "history").glob("*/edit-*.json"))]
    collection_path = output / "history/collection.json"
    collection = read_record(collection_path) if collection_path.exists() else None
    history = {"editor_requests": _editor_requests(output / "history"), "edit_records": len(edits),
        "returned_executable_edits": sum(record.get("execution") is not None for record in edits),
        "edit_failure_records": sum(record.get("execution") is None for record in edits),
        "unknown_edit_records": sum(record.get("error") == "OutcomeUnknown" for record in edits),
        "collection_available": collection["available"] if collection else None,
        "verified_repairs": sum(status["verified_repair"] for status in collection["statuses"].values()) if collection else None,
        "verified_cost_improvements": sum(status["public_cost_improvement"] for status in collection["statuses"].values()) if collection else None}
    return {"schema": "direct_experience_work_v1", "execution": execution,
            "diagnostic": diagnostic, "history": history,
            "accounting": "physical_journals_once;history_edits_are_not_diagnostic_activations"}


def accounting_correction(output):
    """Build a separate correction tied to the unchanged original summary."""
    output = Path(output)
    summary = read_record(output / "summary.json")
    return {"kind": "experience_accounting_correction_v1", "original_summary_sha256": digest(summary),
            "protocol_sha256": summary["protocol_sha256"],
            "reason": "direct_prepare_activate_does_not_emit_continuation_attempt_records",
            "original_summary_preserved": True, "work": experience_work(output, summary["rows"])}
