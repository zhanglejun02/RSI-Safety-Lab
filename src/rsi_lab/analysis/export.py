"""Read-only projections of durable facts, with separate feature and analysis views.

Export does not execute a subject, recompute labels, or treat a decision as a
counterfactual. Training reads only explicitly permitted preparation receipts.
"""

from collections import defaultdict
from pathlib import Path
import hashlib
import sqlite3

from rsi_lab import __version__
from rsi_lab.contracts import (
    ArtifactRef,
    AttemptSpec,
    PairAnchor,
    PrepareCursor,
    SnapshotManifest,
    Partition,
)
from rsi_lab.errors import (
    ConflictError,
    IntegrityError,
    PermissionDenied,
    UnsupportedCapability,
    ValidationError,
)
from rsi_lab.infra.artifacts import _checked_path, _publish_bytes
from rsi_lab.interventions.context import ContextReceipt
from rsi_lab.lifecycle import AuditDecision, CommitReceipt
from rsi_lab.preparation import PreparationState
from rsi_lab.serialization import canonical_bytes, decode_json, primitive
from rsi_lab.subjects.verification import VerificationReceipt
from rsi_lab.workflow_contracts import Evaluation, OperationRecord
from rsi_lab.oracles.workflow import evaluate
from rsi_lab.tasks.workflow import Workflow
from .access import ExportAccess
from .metrics import summarize_outcomes, decision_summary
from .partitions import PartitionRecord, validate_assignments, group_fingerprints

FEATURE_SCHEMA = "preparation_public_v1"
EXPORT_SCHEMA = "rsi_export_v1"


def _read(access, cls, ref):
    access.allow_artifacts((ref,))
    return cls.from_dict(access.read_json(ref))


def _metadata(run):
    run = _checked_path(Path(run))
    manifest = decode_json(_checked_path(run / "run_manifest.json").read_bytes())
    config = decode_json(_checked_path(run / "resolved_config.json").read_bytes())
    if manifest.get("stage") not in ("M2", "M3", "M4"):
        raise UnsupportedCapability("Research export requires an M2, M3 or M4 run")
    if manifest.get("schema_version") != 1 or config.get("schema_version") != 1:
        raise ValidationError("Unsupported stored schema")
    if manifest.get("arbitrary_code_execution") or manifest.get(
        "hard_isolation_verified"
    ):
        raise UnsupportedCapability(
            "Exporter only supports registered trusted fixture provenance"
        )
    return run, manifest, config


def _features(access, prepared):
    if prepared is None:
        return None
    context = None
    verification = None
    if prepared.context_receipt:
        receipt = _read(access, ContextReceipt, prepared.context_receipt)
        context = {
            "q": receipt.q.value,
            "patch": receipt.patch.kind.value,
            "retained_ids": receipt.retained_ids,
            "actual_bytes": receipt.actual_context.size_bytes,
            "synthetic_units": receipt.synthetic_units,
            "budget_units": receipt.budget_units,
            "truncated": receipt.truncated,
            "valid": receipt.valid,
            "invalid_reason": receipt.invalid_reason,
            "matching_error_units": receipt.matching_error_units,
        }
    if prepared.verification_receipt:
        receipt = _read(access, VerificationReceipt, prepared.verification_receipt)
        verification = {
            "requested_plan": receipt.requested_plan,
            "complete": receipt.complete,
            "matching_status": receipt.matching_status,
            "synthetic_cost_units": receipt.synthetic_cost_units,
            "executed": tuple(
                {
                    "candidate_occurrence_id": t.candidate_occurrence_id,
                    "test_id": t.test_id,
                    "status": t.status,
                    "synthetic_cost_units": t.synthetic_cost_units,
                }
                for t in receipt.executed
            ),
            "not_run": tuple(
                {
                    "candidate_occurrence_id": t.candidate_occurrence_id,
                    "test_id": t.test_id,
                    "reason": t.reason,
                }
                for t in receipt.not_run
            ),
        }
    return {"context": context, "verification": verification}


def _binding(spec, prepared):
    if (
        prepared is None
        or prepared.native_selected is None
        or prepared.prepared_snapshot is None
    ):
        return None
    occurrence = prepared.native_selected
    # Identity hashes are not general ArtifactRef read capabilities.
    return {
        "target_attempt_id": spec.attempt_id,
        "candidate_occurrence_id": occurrence.occurrence_id,
        "candidate_sha256": occurrence.artifact.sha256,
        "prepared_snapshot_sha256": prepared.prepared_snapshot.sha256,
        "protocol_sha256": spec.protocol.sha256,
    }


def _plans(db, tables, branches, anchors, config, manifest):
    if "s0_branches" in tables:
        return [
            decode_json(r[0])
            for r in db.execute(
                "SELECT metadata_json FROM s0_branches ORDER BY branch_id"
            )
        ]
    plans = []
    for branch in branches.values():
        if branch["role"] != "main":
            continue
        anchor = anchors[branch["pair_id"]]
        horizon = 1 if manifest["stage"] == "M2" else config.get("horizon_attempts", 2)
        plans.append(
            {
                "branch_id": branch["branch_id"],
                "arm": branch["branch_id"],
                "pair_id": branch["pair_id"],
                "root_id": anchor.root_id,
                "history_group_id": anchor.history_group_id,
                "replicate": 0,
                "registered_horizon": horizon,
                "expected_legal_per_attempt": 1,
                "partition": config.get("partition", "dev"),
            }
        )
    return plans


def _load_attempts(db, access, partition, run_id, training):
    output = []
    partition_records = []
    rows = db.execute(
        "SELECT a.* FROM attempts a JOIN branches b USING(branch_id) WHERE b.role='main' ORDER BY a.branch_id,a.attempt_index"
    )
    for row in rows:
        spec = AttemptSpec.from_dict(decode_json(row["spec_json"]))
        prepared = None
        if row["cursor_json"]:
            cursor = PrepareCursor.from_dict(decode_json(row["cursor_json"]))
            prepared = _read(access, PreparationState, cursor.local_state)
            if prepared.source_snapshot != spec.source_snapshot:
                raise IntegrityError("Export preparation belongs to another source")
        binding = _binding(spec, prepared)
        features = _features(access, prepared)
        identity = {
            "run_id": run_id,
            "attempt_id": spec.attempt_id,
            "target_attempt_id": spec.attempt_id,
            "branch_id": spec.branch_id,
            "pair_id": spec.pair_id,
            "root_id": spec.root_id,
            "history_group_id": spec.history_group_id,
            "arm": spec.arm,
            "replicate": spec.replicate,
            "attempt_index": spec.attempt_index,
            "candidate_origin": spec.candidate_origin.value,
            "partition": partition,
            "phase": row["phase"],
            "status": row["status"],
            "status_reason": row["status_reason"],
        }
        candidate = (
            prepared.native_selected.artifact.sha256
            if prepared and prepared.native_selected
            else None
        )
        record = PartitionRecord(
            f"{run_id}/{spec.attempt_id}",
            spec.root_id,
            spec.history_group_id,
            complete_state_sha256=spec.source_snapshot.sha256,
            candidate_sha256=candidate,
            protocol_sha256=spec.protocol.sha256,
            shared_prefix_ids=(
                (prepared.shared_cost_ref,)
                if prepared and prepared.shared_cost_ref
                else ()
            ),
            declared_partition=Partition(partition),
        )
        partition_records.append(record)
        identity["partition_identity"] = {
            "complete_state_sha256": record.complete_state_sha256,
            "candidate_sha256": record.candidate_sha256,
            "protocol_sha256": record.protocol_sha256,
            "shared_prefix_ids": record.shared_prefix_ids,
            "template_id": "transfer-v1",
        }
        if training:
            output.append(
                {
                    **identity,
                    "feature_schema": FEATURE_SCHEMA,
                    "features": features,
                    "candidate_binding": binding,
                    "candidate_label": None,
                    "label_missing_reason": (
                        "counterfactual_not_measured"
                        if binding
                        else "candidate_not_available"
                    ),
                    "supervised": False,
                    "independent_repetitions": 0,
                    "missing": True,
                }
            )
        else:
            source = _read(access, SnapshotManifest, spec.source_snapshot)
            output.append(
                {
                    **identity,
                    "intervention": spec.intervention,
                    "protocol": spec.protocol,
                    "source_snapshot": spec.source_snapshot,
                    "prepared_snapshot": (
                        prepared.prepared_snapshot if prepared else None
                    ),
                    "proposed_candidates": prepared.candidates if prepared else (),
                    "native_selected": prepared.native_selected if prepared else None,
                    "deployed_version": source.deployed_version,
                    "next_edit_base": source.edit_base_version,
                    "context_receipt": prepared.context_receipt if prepared else None,
                    "verification_receipt": (
                        prepared.verification_receipt if prepared else None
                    ),
                    "shared_cost_ref": prepared.shared_cost_ref if prepared else None,
                    "candidate_binding": binding,
                    "feature_schema": FEATURE_SCHEMA,
                    "features": features,
                }
            )
    validate_assignments(partition_records)
    groups = group_fingerprints(partition_records)
    for row in output:
        row["partition_unit"] = groups[f'{run_id}/{row["attempt_id"]}']
    return output


def _analysis(db, access, attempts, plans, tables, branches, anchors):
    by_id = {a["attempt_id"]: a for a in attempts}
    all_specs = {
        r["attempt_id"]: AttemptSpec.from_dict(decode_json(r["spec_json"]))
        for r in db.execute("SELECT attempt_id,spec_json FROM attempts")
    }
    decisions = []
    if "decisions" in tables:
        for row in db.execute("SELECT * FROM decisions ORDER BY attempt_id"):
            if row["attempt_id"] not in by_id:
                continue
            decision = (
                AuditDecision.from_dict(decode_json(row["decision_json"]))
                if row["decision_json"]
                else None
            )
            decisions.append(
                {
                    "attempt_id": row["attempt_id"],
                    "purpose": "admission",
                    "verdict": decision.verdict if decision else "not_applicable",
                    "decision": decision,
                    "candidate_label": None,
                    "label_missing_reason": "counterfactual_not_measured",
                }
            )
    if "commits" in tables:
        for row in db.execute("SELECT receipt_json FROM commits ORDER BY attempt_id"):
            receipt = CommitReceipt.from_dict(decode_json(row[0]))
            if receipt.attempt_id in by_id:
                command = db.execute(
                    "SELECT command_json FROM commands WHERE command_id=?",
                    (receipt.command_id,),
                ).fetchone()
                if (
                    not command
                    or decode_json(command[0])["target_manifest"]
                    != receipt.snapshot.to_dict()
                ):
                    raise IntegrityError(
                        "Commit target differs from its authorized command"
                    )
                snapshot = _read(access, SnapshotManifest, receipt.snapshot)
                by_id[receipt.attempt_id].update(
                    deployed_version=snapshot.deployed_version,
                    next_edit_base=snapshot.edit_base_version,
                    commit=receipt,
                )
    outcomes = {}
    probe_outcomes = []
    if "deployed_suites" in tables:
        for row in db.execute("SELECT * FROM deployed_suites ORDER BY suite_id"):
            request = decode_json(row["request_json"])
            spec = all_specs.get(request["attempt_id"])
            if spec is None or spec.branch_id != request["branch_id"]:
                raise IntegrityError(
                    "Deployment evidence attempt/branch binding mismatch"
                )
            outcome = (
                Evaluation.from_dict(decode_json(row["result_json"]))
                if row["result_json"]
                else None
            )
            base = _read(
                access,
                SnapshotManifest,
                ArtifactRef.from_dict(decode_json(row["base_snapshot"])),
            )
            if (
                spec.attempt_id in by_id
                and base.deployed_version != by_id[spec.attempt_id]["deployed_version"]
            ):
                raise IntegrityError(
                    "Deployment evidence differs from actual committed version"
                )
            incident = False
            if outcome is None and "branch_pauses" in tables:
                pause = db.execute(
                    "SELECT * FROM branch_pauses WHERE suite_id=?", (row["suite_id"],)
                ).fetchone()
                if pause:
                    trace_ref = ArtifactRef.from_dict(decode_json(pause["trace_json"]))
                    access.allow_artifacts((trace_ref,))
                    trace = access.read_json(trace_ref)
                    if (
                        trace.get("purpose") != "discovery"
                        or trace.get("deployed_version")
                        != base.deployed_version.to_dict()
                        or ArtifactRef.from_dict(
                            decode_json(pause["deployed_version_json"])
                        )
                        != base.deployed_version
                    ):
                        raise IntegrityError(
                            "Incident does not bind the actual deployed version"
                        )
                    records = tuple(
                        OperationRecord.from_dict(r) for r in trace["records"]
                    )
                    if not records or any(
                        r.effect_id != pause["operation_id"]
                        or r.task.namespace != request["namespace"]
                        for r in records
                    ):
                        raise IntegrityError("Incident operation provenance mismatch")
                    outcome = evaluate(
                        records,
                        Workflow._suite_tasks(request["namespace"]),
                        complete=False,
                    )
                    if outcome.risk != 1:
                        raise IntegrityError(
                            "Pause has no confirmed effective violation"
                        )
                    incident = True
            if outcome:
                summarize_outcomes([outcome], horizon=1, legal_denominators=[1])
            entry = {
                "target_attempt_id": request["attempt_id"],
                "branch_id": request["branch_id"],
                "purpose": (
                    "probe_evidence"
                    if request["purpose"] == "online_audit"
                    else "deployed_outcome"
                ),
                "cursor_family": request["cursor_family"],
                "actual_deployed_version": base.deployed_version,
                "evaluation": outcome,
                "missing_reason": (
                    (
                        "deployment_incomplete_with_confirmed_incident"
                        if incident
                        else None
                    )
                    if outcome
                    else "deployment_incomplete"
                ),
                "protocol": spec.protocol,
                "replicate": spec.replicate,
                "attempt_index": spec.attempt_index,
                "reconstructed_from_incident": incident,
            }
            if request["purpose"] == "online_audit":
                entry["continuation_horizon"] = 2
                probe_outcomes.append(entry)
            else:
                if request["attempt_id"] in outcomes:
                    raise IntegrityError(
                        "Ambiguous multiple deployment suites for one attempt"
                    )
                outcomes[request["attempt_id"]] = entry
    missing = {}
    if "s0_rounds" in tables:
        for r in db.execute(
            "SELECT branch_id,attempt_index,result_json FROM s0_rounds"
        ):
            if r["result_json"]:
                missing[(r["branch_id"], r["attempt_index"])] = decode_json(
                    r["result_json"]
                ).get("reason")
    elif "loop_rounds" in tables:
        for r in db.execute("SELECT attempt_index,result_json FROM loop_rounds"):
            if r["result_json"]:
                missing[("main", r["attempt_index"])] = decode_json(
                    r["result_json"]
                ).get("reason")
    slots = {(a["branch_id"], a["attempt_index"]): a for a in attempts}
    evaluations = []
    for plan in plans:
        for index in range(1, plan["registered_horizon"] + 1):
            attempt = slots.get((plan["branch_id"], index))
            entry = outcomes.get(attempt["attempt_id"]) if attempt else None
            evaluations.append(
                {
                    "branch_id": plan["branch_id"],
                    "arm": plan["arm"],
                    "pair_id": plan["pair_id"],
                    "root_id": plan["root_id"],
                    "history_group_id": plan["history_group_id"],
                    "attempt_index": index,
                    "target_attempt_id": attempt["attempt_id"] if attempt else None,
                    "purpose": "deployed_outcome",
                    "registered_legal_denominator": plan["expected_legal_per_attempt"],
                    "continuation_horizon": plan["registered_horizon"],
                    **(
                        entry
                        or {
                            "actual_deployed_version": None,
                            "evaluation": None,
                            "missing_reason": missing.get((plan["branch_id"], index))
                            or (attempt["status_reason"] if attempt else None)
                            or "deployment_evaluation_not_requested",
                        }
                    ),
                }
            )
    costs = [
        dict(r) for r in db.execute("SELECT * FROM cost_events ORDER BY operation_id")
    ]
    for row in costs:
        operation = db.execute(
            "SELECT * FROM operations WHERE operation_id=?", (row["operation_id"],)
        ).fetchone()
        if (
            not operation
            or operation["status"] != "settled"
            or any(
                row[key] != operation[key]
                for key in (
                    "owner_kind",
                    "owner_id",
                    "attempt_id",
                    "purpose",
                    "receipt_sha256",
                )
            )
            or row["synthetic_cost_units"] != operation["actual_units"]
        ):
            raise IntegrityError("Charge differs from its settled operation")
        row["unit"] = "synthetic_cost_unit"
        row["is_synthetic"] = True
    exposures = [
        dict(r)
        for r in db.execute("SELECT * FROM exposure_events ORDER BY exposure_id")
    ]
    outstanding = [
        dict(r)
        for r in db.execute(
            "SELECT operation_id,owner_kind,owner_id,attempt_id,purpose,status,reserved_units,unknown_reason FROM operations WHERE status IN ('reserved','outcome_unknown') ORDER BY operation_id"
        )
    ]
    summary = _paired_summary(
        plans,
        attempts,
        decisions,
        evaluations,
        costs,
        exposures,
        outstanding,
        branches,
        anchors,
    )
    return {
        "attempts.jsonl": attempts,
        "decisions.jsonl": decisions,
        "evaluations.jsonl": evaluations + probe_outcomes,
        "costs.jsonl": costs,
        "exposures.jsonl": exposures,
        "reservations.jsonl": outstanding,
        "paired_summary.json": summary,
    }


def _paired_summary(
    plans,
    attempts,
    decisions,
    evaluations,
    costs,
    exposures,
    reservations,
    branches,
    anchors,
):
    verdicts = {r["attempt_id"]: r["verdict"] for r in decisions}
    arm_summaries = []
    for plan in plans:
        branch = plan["branch_id"]
        rows = [r for r in evaluations if r["branch_id"] == branch]
        items = [a for a in attempts if a["branch_id"] == branch]
        stats = summarize_outcomes(
            [r["evaluation"] for r in rows],
            horizon=plan["registered_horizon"],
            legal_denominators=[plan["expected_legal_per_attempt"]]
            * plan["registered_horizon"],
        )
        arm_summaries.append(
            {
                **plan,
                "metrics": stats,
                "decisions": decision_summary(
                    [verdicts.get(r["target_attempt_id"]) for r in rows]
                ),
                "dispatched_attempts": len(items),
                "committed_attempts": sum(a["phase"] == "committed" for a in items),
                "task_exposures": sum(e["branch_id"] == branch for e in exposures),
                "nonshared_cost_units": sum(
                    c["synthetic_cost_units"]
                    for c in costs
                    if c["owner_kind"] == "branch" and c["owner_id"] == branch
                ),
                "shared_cost_refs": sorted(
                    {a["shared_cost_ref"] for a in items if a["shared_cost_ref"]}
                ),
            }
        )
    pairs = []
    for pair_id in sorted({p["pair_id"] for p in plans}):
        arms = [r for r in arm_summaries if r["pair_id"] == pair_id]
        # Pair on the common anchor and root, never on generated candidate hash.
        baseline = next((a for a in arms if a["arm"] == "Old"), arms[0])
        comparisons = []
        for arm in arms:
            if arm is baseline:
                continue
            left = baseline["metrics"]["risk"]["mean"]
            right = arm["metrics"]["risk"]["mean"]
            comparisons.append(
                {
                    "baseline_arm": baseline["arm"],
                    "arm": arm["arm"],
                    "risk_mean_difference": (
                        None if left is None or right is None else right - left
                    ),
                    "missing_reason": (
                        "incomplete_registered_horizon"
                        if left is None or right is None
                        else None
                    ),
                }
            )
        pairs.append(
            {
                "pair_id": pair_id,
                "root_id": arms[0]["root_id"],
                "history_group_id": arms[0]["history_group_id"],
                "pairing_mode": anchors[pair_id].pairing_mode.value,
                "shared_cost_units": sum(
                    c["synthetic_cost_units"]
                    for c in costs
                    if c["owner_kind"] == "pair" and c["owner_id"] == pair_id
                ),
                "arms": [a["branch_id"] for a in arms],
                "comparisons": comparisons,
            }
        )
    return {
        "schema_version": 1,
        "statistical_unit": "root/history_group",
        "engineering_fixture": True,
        "independent_roots": len({a["partition_unit"] for a in attempts}),
        "declared_roots": len({p["root_id"] for p in plans}),
        "history_groups": len({p["history_group_id"] for p in plans}),
        "registered_attempts": sum(p["registered_horizon"] for p in plans),
        "complete_horizon_arms": sum(
            a["metrics"]["risk"]["missing"] == 0 for a in arm_summaries
        ),
        "incomplete_horizon_arms": sum(
            a["metrics"]["risk"]["missing"] > 0 for a in arm_summaries
        ),
        "arms": arm_summaries,
        "pairs": pairs,
        "resources": {
            "total_synthetic_units": sum(c["synthetic_cost_units"] for c in costs),
            "shared_units": sum(
                c["synthetic_cost_units"] for c in costs if c["owner_kind"] == "pair"
            ),
            "arm_units": sum(
                c["synthetic_cost_units"]
                for c in costs
                if c["owner_kind"] == "branch"
                and branches.get(c["owner_id"], {}).get("role") == "main"
            ),
            "audit_units": sum(
                c["synthetic_cost_units"]
                for c in costs
                if c["purpose"] == "online_audit"
            ),
            "outstanding_reserved_units": sum(
                r["reserved_units"] for r in reservations
            ),
        },
        "admission_false_rejection_rate": None,
        "false_rejection_missing_reason": "counterfactual_not_measured",
        "confidence_interval": None,
        "interval_missing_reason": "fixture_no_inferential_claim",
    }


def export_run(
    run: Path,
    output: Path,
    *,
    view="analysis",
    database_opener=None,
    artifact_reader=None,
):
    """Export one immutable view without opening label stores or mutating a run."""
    if view not in ("analysis", "training"):
        raise ValidationError("Unknown export view")
    output = _checked_path(Path(output))
    if output.exists():
        raise ConflictError("Export output exists; choose a new directory")
    run, manifest, config = _metadata(run)
    partition = config.get("partition", "dev")
    access = ExportAccess(
        run,
        partition=partition,
        view=view,
        database_opener=database_opener,
        artifact_reader=artifact_reader,
    )
    protocol = ArtifactRef.from_dict(manifest["protocol"])
    access.allow_artifacts((protocol,))
    protocol_data = access.read_json(protocol)
    if ("config" in protocol_data and protocol_data["config"] != config) or (
        manifest.get("study") == "m3_transition" and protocol_data != config
    ):
        raise IntegrityError("Stored protocol and resolved configuration differ")
    if "config" in manifest:
        config_ref = ArtifactRef.from_dict(manifest["config"])
        access.allow_artifacts((config_ref,))
        if access.read_json(config_ref) != config:
            raise IntegrityError("Stored configuration reference differs")
    with access.database() as db:
        tables = {
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        branches = {
            r["branch_id"]: dict(r) for r in db.execute("SELECT * FROM branches")
        }
        anchors = {
            r["pair_id"]: PairAnchor.from_dict(decode_json(r["anchor_json"]))
            for r in db.execute("SELECT * FROM pairs")
        }
        plans = _plans(db, tables, branches, anchors, config, manifest)
        if {p["branch_id"] for p in plans} != {
            b for b, row in branches.items() if row["role"] == "main"
        } or len(plans) != len({p["branch_id"] for p in plans}):
            raise IntegrityError("Registered plans do not exactly cover main branches")
        if any(p["partition"] != partition for p in plans):
            raise PermissionDenied(
                "Mixed-partition run cannot be exported as a single partition"
            )
        if any(
            type(p["registered_horizon"]) is not int
            or p["registered_horizon"] < 1
            or p["expected_legal_per_attempt"] != 1
            for p in plans
        ):
            raise IntegrityError(
                "Invalid registered fixture horizon or legal denominator"
            )
        if manifest["stage"] == "M4" and (
            any(p["registered_horizon"] != config["horizon_attempts"] for p in plans)
            or len(plans) != config["replicates"] * len(protocol_data["arms"])
        ):
            raise IntegrityError("Registered S0 plan differs from its frozen protocol")
        attempts = _load_attempts(
            db, access, partition, manifest["run_id"], view == "training"
        )
        if view == "training":
            plan_by_branch = {p["branch_id"]: p for p in plans}
            for row in attempts:
                plan = plan_by_branch[row["branch_id"]]
                row["continuation"] = {
                    "name": config.get("continuation", "frozen_mechanism"),
                    "protocol_sha256": protocol.sha256,
                    "horizon": plan["registered_horizon"],
                    "purpose": "candidate_label",
                    "measurement_status": "not_requested",
                }
            payloads = {"examples.jsonl": attempts}
            access.assert_output_references(primitive(payloads))
        else:
            payloads = _analysis(db, access, attempts, plans, tables, branches, anchors)
    # Prepare every file before making the output visible; the manifest is last.
    encoded = {
        name: (
            b"".join(canonical_bytes(row) + b"\n" for row in value)
            if name.endswith(".jsonl")
            else canonical_bytes(value)
        )
        for name, value in payloads.items()
    }
    result = {
        "schema_version": 1,
        "export_schema": EXPORT_SCHEMA,
        "view": view,
        "run_id": manifest["run_id"],
        "source_stage": manifest["stage"],
        "source_study": config.get("study", manifest.get("study")),
        "source_implementation_sha256": manifest["implementation_sha256"],
        "exporter_version": __version__,
        "protocol_sha256": protocol.sha256,
        "partition": partition,
        "candidate_origin": config.get("candidate_origin", "control"),
        "feature_schema": FEATURE_SCHEMA if view == "training" else None,
        "candidate_label_status": "counterfactual_not_measured",
        "supervision_rows": 0,
        "allowed_output_artifact_refs": [],
        "artifact_reference_policy": "training uses identity hashes only; analysis refs stay in source CAS",
        "advanced_near_duplicate_detection": "not_implemented",
        "files": {
            name: {
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
                "rows": len(payloads[name]) if name.endswith(".jsonl") else None,
            }
            for name, data in encoded.items()
        },
    }
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ConflictError("Export output exists") from exc
    for name, data in encoded.items():
        if not _publish_bytes(data, output / name):
            raise ConflictError("Export file already exists")
    _publish_bytes(canonical_bytes(result), output / "export_manifest.json")
    return result
