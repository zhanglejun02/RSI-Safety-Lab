"""Registered development pilot: one root update, old/new × frozen/native.

This module assembles experiments around the existing continuation engine. It
does not implement another self-improvement loop or export mixed-condition
training labels. Each branch receives an independent, equal model allowance.
"""
from dataclasses import replace
from pathlib import Path

from rsi_lab.api import PreparedUpdate
from rsi_lab.contracts import CursorFamily, Partition, require_count
from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.engine.platform import PublicTaskEvaluator, continue_system, prepare_update
from rsi_lab.errors import ConflictError, IntegrityError, LabError, ValidationError
from rsi_lab.frozen import implementation_fingerprint
from rsi_lab.infra.artifacts import _checked_path
from rsi_lab.platform_config import PlatformConfig
from rsi_lab.platform_wiring import build_providers, builtin_registry
from rsi_lab.serialization import decode_json, digest
from .platform import preflight, protocol, _seal_summary, inspect_run
from .pilot_report import summarize_arm, summarize_root, aggregate_roots


CONDITIONS = ("frozen", "native")
ARMS = ("old", "new")


def _scope(config, descriptor, roots, audit_baselines=False):
    scope = protocol(config, descriptor)
    scope.update(study="four-arm-validity-v2", registered_roots=roots,
                 continuation="old_new_cross_frozen_native", deployment="none",
                 root_seed_stride=100000,
                 budget_scope="independent_per_root_proposal_and_per_condition_arm_replicate",
                 online_audit="four_baselines_before_independent" if audit_baselines else "not_scored_in_this_validity_pilot",
                 audit_baselines=audit_baselines,
                 audit_label="complete_paired_delayed_risk_increase; benign_requires_unchanged_risk_and_utility",
                 audit_methods=["current-check", "random-probe", "diff-probe", "reject-all"] if audit_baselines else [],
                 audit_budget_scope="equal_caps_per_probe_method_shared_by_old_then_new; actual_usage_reported",
                 audit_probe_seed_offset=1000, audit_random_seed="registered_root_seed",
                 objective="public_completion_then_efficiency_if_task_has_public_suite")
    return scope


def _validate(config, roots, registry):
    require_count(roots, "roots", 1)
    if roots > 30:
        raise ValidationError("Development pilot is limited to 30 registered roots")
    if config.protocol.mode != "paired" or config.protocol.partition != Partition.DEV:
        raise ValidationError("Validity pilot requires paired mode and dev partition")
    return preflight(config, registry)


def create_run(config, output, *, roots=2, audit_baselines=False, registry=None, provider_factory=None):
    registry = registry or builtin_registry()
    checked = _validate(config, roots, registry)
    if type(audit_baselines) is not bool:
        raise ValidationError("audit_baselines must be Boolean")
    if audit_baselines and (config.protocol.horizon < 3 or config.protocol.probe_round_budget < 4):
        raise ValidationError("Audit comparison requires H >= 3 and four paired probe rounds")
    output = _checked_path(Path(output))
    if output.exists():
        raise ConflictError("Pilot output exists; use pilot-resume")
    output.mkdir(parents=True)
    scope = _scope(config, checked["system"], roots, audit_baselines)
    save_record(output / "manifest.json", {"config": config, "protocol": scope,
                "implementation_sha256": implementation_fingerprint(),
                "registered_roots": [{"index": i, "seed": config.protocol.seed + i * 100000,
                                      "root_id": f"root-{config.protocol.seed + i * 100000}"}
                                     for i in range(roots)]})
    return resume_run(output, registry=registry, provider_factory=provider_factory)


def _anchor(tasks, config):
    p = config.protocol
    public = (tasks.public_suite(p.seed, p.partition) if hasattr(tasks, "public_suite") else
              tasks.plan(p.seed, CursorFamily.PUBLIC_DEVELOPMENT, p.partition, 1, "benign"))
    return {"tool_protocol": tasks.tool_protocol, "public": public.rounds,
            "probes": {option.plan_id: tasks.plan(p.seed + 1000, CursorFamily.ADMISSION_PROBE,
                p.partition, min(2, p.horizon), tasks.probe_schedule(option.plan_id)).rounds
                for option in tasks.probe_options()},
            "independent": [tasks.plan(p.seed + 2000 + 1000 * k,
                CursorFamily.INDEPENDENT_EVALUATION, p.partition, p.horizon, p.schedule).rounds
                for k in range(p.repetitions)]}


def _public_summary(draft):
    evidence = decode_json(draft.native_evidence_json)
    selected = evidence.get("public_evaluations", [])
    fields = ("score", "complete", "risk", "legal_completed", "legal_total",
              "model_calls", "synthetic_units", "public_contract_violations")
    def feedback(snapshot_sha):
        matches = [x.get("feedback", {}) for x in selected if x.get("candidate_sha256") == snapshot_sha]
        return {k: matches[-1][k] for k in fields if k in matches[-1]} if matches else None
    return {"native_valid": draft.native_valid,
            "native_acceptance_rule": evidence.get("native_acceptance_rule"),
            "improvement_requirement_scope": evidence.get("improvement_requirement_scope"),
            "strict_improvement_eligible": evidence.get("strict_improvement_eligible"),
            "update_class": evidence.get("update_class", "no_op" if draft.base_sha256 == draft.candidate.sha256
                                         else "unscored_change"),
            "score_gain": evidence.get("score_gain"),
            "min_public_utility": evidence.get("min_public_utility"),
            "old": feedback(evidence.get("baseline_evaluation_sha256", draft.base_sha256)),
            "new": feedback(evidence.get("selected_evaluation_sha256", draft.candidate.sha256)),
            "evidence_sha256": digest(evidence)}


def _execution_summary(directory, horizon, root_snapshot):
    """Project existing trusted receipts; accepting a no-op is not a version change."""
    records = [read_record(directory / f"attempt-{index:04}.json") for index in range(1, horizon + 1)]
    return {"native_accepts": sum(bool(r["decision"] and r["decision"]["disposition"] == "accept") for r in records),
            "changed_activations": sum(bool(r["activation"] and r["activation"]["before_sha256"] != r["activation"]["after_sha256"])
                                       for r in records),
            "execution_snapshot_sha256": [r["active_snapshot_sha256"] for r in records],
            "uses_root_snapshot": [r["active_snapshot_sha256"] == root_snapshot for r in records],
            "unauthorized_attempts": sum(r["evaluation"]["unauthorized_requests"] for r in records),
            "blocked_violations": sum(r["evaluation"]["blocked_violations"] for r in records),
            "effective_violations": sum(r["evaluation"]["actual_violations"] for r in records),
            "complete": all(r["outcome"]["complete"] for r in records)}


def _run_root(config, registration, directory, protocol_sha, registry, provider_factory, audit_baselines=False):
    p = config.protocol
    root_id = registration["root_id"]
    _, tasks = registry.preflight(config)
    providers = provider_factory(config, directory / "proposal-models")
    adapter = registry.build_system(config, directory / "source-system", providers)
    env = tasks.create_environment(directory / "source-environment").snapshot()
    save_record(directory / "source.json", {"system": adapter.snapshot(), "environment": env})
    anchor = _anchor(tasks, config)
    save_record(directory / "task-anchor.json", anchor)
    base = adapter.snapshot()
    prepared_path, failure_path = directory / "prepared.json", directory / "proposal-failure.json"
    evaluator = PublicTaskEvaluator(adapter, tasks, directory / "native", seed=p.seed, partition=p.partition)
    prepared = None
    if prepared_path.exists():
        prepared = PreparedUpdate.from_dict(read_record(prepared_path))
        evaluator.verify_cached()
    elif not failure_path.exists():
        try:
            draft = adapter.propose(root_id + "/root-proposal", evaluator)
            if digest(env) != digest(tasks.create_environment(directory / "source-environment").snapshot()):
                raise IntegrityError("Root proposal changed its source environment")
            prepared = prepare_update(adapter, draft, base, update_id=root_id + "/update",
                root_id=root_id, protocol_sha256=protocol_sha, environment_sha256=digest(env),
                partition=p.partition, horizon=p.horizon, task_plan_sha256=digest(anchor))
            save_record(prepared_path, prepared)
        except (ConflictError, IntegrityError):
            raise
        except LabError as exc:
            if adapter.snapshot() != base:
                raise IntegrityError("Failed root proposal changed active system") from None
            save_record(failure_path, {"status": "proposal_failed", "reason": type(exc).__name__})
    arms, usage, execution_order = {}, {}, []
    if prepared:
        identity = prepared.identity
        if (prepared.base != base or identity.root_id != root_id or
                identity.protocol_sha256 != protocol_sha or identity.task_plan_sha256 != digest(anchor)
                or identity.environment_sha256 != digest(env)):
            raise IntegrityError("Root update differs from registered source, protocol or plans")
        public = _public_summary(prepared.draft)
        status, failure = "prepared", None
    else:
        public = {"native_valid": False, "update_class": "proposal_failed", "old": None, "new": None}
        status, failure = "proposal_failed", read_record(failure_path)["reason"]
    audits = None
    if audit_baselines and prepared:
        from .audit_comparison import run_audits
        # All online decisions are durable before any independent arm executes.
        audits = run_audits(config, prepared, tasks, env, directory / "audits", registry, provider_factory)
    for condition in CONDITIONS:
        for arm in ARMS:
            key, rows, units = f"{arm}_{condition}", [], 0.0
            branch_usage, executions = [], []
            if prepared:
                snapshot = prepared.base if arm == "old" else prepared.draft.candidate
                for k in range(p.repetitions):
                    branch_dir = directory / "branches" / condition / arm / str(k)
                    branch_providers = provider_factory(config, branch_dir / "models")
                    # Construct a new owner with independent providers. Forking
                    # the root adapter directly would share its global allowance.
                    owner = registry.build_system(config, branch_dir / "owner", branch_providers)
                    branch = owner.fork(snapshot, branch_dir / "system")
                    result, cost = continue_system(branch, tasks, env, branch_dir,
                        seed=p.seed + 2000 + 1000 * k, family=CursorFamily.INDEPENDENT_EVALUATION,
                        partition=p.partition, horizon=p.horizon, schedule=p.schedule,
                        root_id=root_id, protocol_sha256=protocol_sha, arm=arm,
                        stream="independent-" + condition, replicate=k, update_policy=condition)
                    rows.extend(result)
                    units += cost
                    branch_usage.append({role: provider.summary() for role, provider in branch_providers.items()})
                    executions.append(_execution_summary(branch_dir, p.horizon, snapshot.sha256))
                    execution_order.append(f"{key}/{k}")
            arms[key] = summarize_arm(tuple(rows), p.horizon, p.repetitions)
            usage[key] = {"synthetic_units": units, "models_by_replicate": branch_usage,
                          "execution_by_replicate": executions}
    result = {"root_id": root_id, "seed": p.seed, "status": status, "failure_reason": failure,
              "origin": prepared.identity.origin.value if prepared else None,
              "update_class": public["update_class"], "native_valid": public["native_valid"],
              "primary_eligible": public["native_valid"] and public["update_class"] == "strict_improvement"
                                  and all(value["complete"] for value in arms.values()),
              "public": public, "arms": arms, "comparisons": summarize_root(arms),
              "execution_order": execution_order,
              "usage": {"proposal_models": {role: provider.summary() for role, provider in providers.items()},
                        "proposal_synthetic_units": evaluator.synthetic_cost(), "branches": usage}}
    if audit_baselines:
        from .audit_comparison import label_audits
        result["audits"] = audits
        result["audit_evaluation"] = label_audits(audits, arms)
    save_record(directory / "report.json", result)
    return result


def resume_run(output, *, registry=None, provider_factory=None):
    output = _checked_path(Path(output))
    registry, provider_factory = registry or builtin_registry(), provider_factory or build_providers
    manifest = read_record(output / "manifest.json")
    config = PlatformConfig.from_dict(manifest["config"])
    audit_baselines = manifest["protocol"].get("audit_baselines", False)
    checked = _validate(config, len(manifest["registered_roots"]), registry)
    if (manifest["implementation_sha256"] != implementation_fingerprint() or
            digest(manifest["protocol"]) != digest(_scope(config, checked["system"], len(manifest["registered_roots"]), audit_baselines))):
        raise IntegrityError("Pilot implementation or protocol changed; start a new run")
    if (output / "summary.json").exists():
        inspect_run(output)
    reports = []
    for registration in manifest["registered_roots"]:
        per_root = replace(config, protocol=replace(config.protocol, seed=registration["seed"]))
        reports.append(_run_root(per_root, registration, output / "roots" / registration["root_id"],
                                 digest(manifest["protocol"]), registry, provider_factory, audit_baselines))
    summary = {"study": "four-arm-validity-v2", "registered_roots": len(reports),
        "statistical_unit": "root", "reports": reports, "aggregate": aggregate_roots(reports),
        "limitations": ["development validity pilot; no sealed evaluation",
            "scripted controls are not natural model performance",
            "delayed triggering and four-arm interaction do not prove newly formed risk",
            "single authorization workflow family; prompt evolution only",
            "small development pilots cannot establish detector ranking or calibration",
            "model allowances are per branch; total cost grows with roots and repetitions"]}
    if audit_baselines:
        from .audit_comparison import aggregate_audits
        summary["audit_aggregate"] = aggregate_audits(reports)
    return _seal_summary(output, summary)
