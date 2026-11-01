"""Platform experiment assembly; all execution uses the common continuation service."""
from pathlib import Path
from rsi_lab.api import PreparedUpdate, SystemSnapshot
from rsi_lab.auditing.platform import (AdmissionPolicy, NativeOnlyPolicy, RejectAllPolicy,
    ProbeDetector, DiffProbePlanner, RandomProbePlanner, DetectionReport, AdmissionDecision,
    ProbeEvidence, audit_features, learning_binding)
from rsi_lab.contracts import CursorFamily, Partition
from rsi_lab.engine.activation import ActivationController
from rsi_lab.engine.continuation import save_record, read_record
from rsi_lab.engine.platform import (PublicTaskEvaluator, prepare_update, project_audit_view,
                                    BoundProbeService, continue_system)
from rsi_lab.errors import ConflictError, IntegrityError, ValidationError
from rsi_lab.frozen import implementation_fingerprint
from rsi_lab.infra.artifacts import _checked_path
from rsi_lab.learning.contracts import AuditFeatures, CandidateRiskLabel, TrainingExample
from rsi_lab.learning.data import write_dataset
from rsi_lab.learning.model import load_model
from rsi_lab.platform_config import PlatformConfig
from rsi_lab.platform_wiring import builtin_registry, build_providers
from rsi_lab.serialization import digest, primitive


def preflight(config, registry=None):
    if type(config) is not PlatformConfig:
        raise ValidationError("Expected platform composition config")
    registry = registry or builtin_registry()
    descriptor, tasks = registry.preflight(config)
    if (config.protocol.mode != "behavior" and config.detector.name != "none"
            and config.protocol.probe_round_budget < 2 * min(2, config.protocol.horizon)):
        raise ValidationError("Registered detector needs a paired probe budget of at least twice its short horizon")
    if config.detector.model_path:
        load_model(Path(config.detector.model_path))
    return {"valid": True, "system": descriptor, "task_protocol": tasks.tool_protocol,
            "mode": config.protocol.mode, "backend": config.backend.name,
            "arbitrary_code_execution": False, "hard_isolation_verified": False,
            "note": "Trusted Python plugins; bounded model text/tools. Validation makes no model call."}


def protocol(config, descriptor):
    return {"api": "rsi-platform-v1", "system": descriptor, "system_options": config.system.options,
            "task": config.task, "backend": config.backend, "detector": config.detector,
            "continuation": "native_updates_in_probes_and_labels_no_recursive_audit",
            "deployment": "one_audited_update_then_frozen_system_on_stateful_tasks",
            "horizon": config.protocol.horizon, "repetitions": config.protocol.repetitions,
            "schedule": config.protocol.schedule, "probe_round_budget": config.protocol.probe_round_budget,
            "editor": config.editor, "actor": config.actor,
            "implementation_sha256": implementation_fingerprint()}


def create_run(config, output, *, registry=None, provider_factory=None):
    registry = registry or builtin_registry()
    checked = preflight(config, registry)
    output = _checked_path(Path(output))
    if output.exists():
        raise ConflictError("Platform output exists; use platform resume")
    output.mkdir(parents=True)
    scope = protocol(config, checked["system"])
    model_hash = load_model(Path(config.detector.model_path)).artifact.to_dict() if config.detector.model_path else None
    save_record(output / "manifest.json", {"config": config, "protocol": scope,
                "model_artifact_sha256": digest(model_hash) if model_hash else None,
                "implementation_sha256": implementation_fingerprint()})
    return resume_run(output, registry=registry, provider_factory=provider_factory)


def _load(output, registry):
    manifest = read_record(output / "manifest.json")
    config = PlatformConfig.from_dict(manifest["config"])
    checked = preflight(config, registry)
    if (manifest["implementation_sha256"] != implementation_fingerprint()
            or digest(manifest["protocol"]) != digest(protocol(config, checked["system"]))):
        raise IntegrityError("Platform implementation or protocol changed; start a new run")
    model = load_model(Path(config.detector.model_path)) if config.detector.model_path else None
    if (digest(model.artifact.to_dict()) if model else None) != manifest["model_artifact_sha256"]:
        raise IntegrityError("Frozen model changed after run registration")
    return config, manifest, model


def task_anchor(tasks, config):
    """Registered future cursor state, not an arbitrary root-ID salt.

    Full task plans stay on the platform side. Online consumers receive only
    their digest, which conveys no handle or access to independent parameters.
    """
    p = config.protocol
    public_partition = Partition.DEV if p.partition == Partition.SEALED else p.partition
    public = (tasks.public_suite(p.seed, public_partition) if callable(getattr(tasks, "public_suite", None))
              else tasks.plan(p.seed, CursorFamily.PUBLIC_DEVELOPMENT, public_partition, 1, "benign"))
    # Exclude partition/root/history/seed/plan-ID metadata from the content
    # identity. Actual requests/events/order (including their effect targets) bind
    # the continuation; simply relabelling a partition cannot defeat clone checks.
    return {
        "tool_protocol": tasks.tool_protocol,
        "native": public.rounds,
        "probes": {option.plan_id: tasks.plan(p.seed + 1000, CursorFamily.ADMISSION_PROBE,
                       public_partition, min(2, p.horizon), tasks.probe_schedule(option.plan_id)).rounds
                   for option in tasks.probe_options()},
        "independent": tuple(tasks.plan(p.seed + 2000 + 1000 * r, CursorFamily.INDEPENDENT_EVALUATION,
                                        p.partition, p.horizon, p.schedule).rounds for r in range(p.repetitions)),
        "deployment": tasks.plan(p.seed + 3000, CursorFamily.INDEPENDENT_EVALUATION,
                                  p.partition, p.horizon, p.schedule).rounds,
    }



def _summarize(rows):
    return {"risk_by_round": [r.risk for r in rows], "complete": all(r.complete for r in rows),
            "mean_risk": sum(r.risk for r in rows) / len(rows) if rows and all(r.complete for r in rows) else None,
            "legal_completed": sum(r.legal_completed for r in rows), "legal_total": sum(r.legal_total for r in rows),
            "versions": [r.candidate_sha256 for r in rows]}


def _seal_summary(output, summary):
    # Immutable JSON receipts are checked independently of mutable SQLite journals.
    hashes = {str(path.relative_to(output)): digest(read_record(path)) for path in sorted(output.rglob("*.json"))
              if path.name not in ("evidence_manifest.json", "summary.json")}
    save_record(output / "evidence_manifest.json", hashes)
    save_record(output / "summary.json", summary)
    return summary


def inspect_run(output):
    output = _checked_path(Path(output))
    summary = read_record(output / "summary.json")
    for relative, expected in read_record(output / "evidence_manifest.json").items():
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or digest(read_record(output / path)) != expected:
            raise IntegrityError("Platform evidence changed")
    return summary


def resume_run(output, *, registry=None, provider_factory=None):
    output = _checked_path(Path(output))
    registry = registry or builtin_registry()
    config, manifest, scorer = _load(output, registry)
    previous_summary = inspect_run(output) if (output / "summary.json").exists() else None
    providers = (provider_factory or build_providers)(config, output / "models")
    adapter = registry.build_system(config, output / "main" / "system", providers)
    if previous_summary and adapter.snapshot().sha256 != previous_summary["active_snapshot_sha256"]:
        raise IntegrityError("Actual active state differs from completed report")
    # Replay the common service even on completed runs: cached steps verify live
    # branch system/environment receipts without issuing new model requests.
    _, tasks = registry.preflight(config)
    source_env = tasks.create_environment(output / "source-environment").snapshot()
    p = config.protocol
    protocol_sha = digest(manifest["protocol"])
    root_id = f"root-{p.seed}"
    source_path = output / "source.json"
    if not source_path.exists():
        save_record(source_path, {"system": adapter.snapshot(), "environment_sha256": digest(source_env)})
    source = read_record(source_path)
    base = SystemSnapshot.from_dict(source["system"])
    if source["environment_sha256"] != digest(source_env):
        raise IntegrityError("Initial environment changed")
    if p.mode == "behavior":
        behavior, units = continue_system(adapter, tasks, source_env, output / "behavior", seed=p.seed + 1000,
            family=CursorFamily.INDEPENDENT_EVALUATION, partition=p.partition, horizon=p.horizon,
            schedule=p.schedule, root_id=root_id, protocol_sha256=protocol_sha, arm="old", stream="behavior", update_policy="frozen")
        return _seal_summary(output, {"mode": "behavior", "system": config.system.name,
            "behavior": _summarize(behavior), "active_snapshot_sha256": adapter.snapshot().sha256,
            "usage": {"synthetic_units": units, "models": {k: v.summary() for k, v in providers.items()}}})
    anchor = task_anchor(tasks, config)
    save_record(output / "task-anchor.json", anchor)
    prepared_path = output / "prepared.json"
    if prepared_path.exists():
        prepared = PreparedUpdate.from_dict(read_record(prepared_path))
        PublicTaskEvaluator(adapter, tasks, output / "native", seed=p.seed,
                            partition=p.partition).verify_cached()
    else:
        evaluator = PublicTaskEvaluator(adapter, tasks, output / "native", seed=p.seed, partition=p.partition)
        draft = adapter.propose(root_id + "/root-proposal", evaluator)
        prepared = prepare_update(adapter, draft, base, update_id=root_id + "/update", root_id=root_id,
            protocol_sha256=protocol_sha, environment_sha256=digest(source_env), partition=p.partition, horizon=p.horizon,
            task_plan_sha256=digest(anchor))
        save_record(prepared_path, prepared)
    if prepared.identity.task_plan_sha256 != digest(anchor):
        raise IntegrityError("Prepared update task cursor differs from registered plans")
    view = project_audit_view(prepared, probe_options=tasks.probe_options())
    save_record(output / "audit_view.json", view)
    online_path = output / "online.json"
    if online_path.exists():
        online = read_record(online_path)
        report = DetectionReport.from_dict(online["report"])
        decision = AdmissionDecision.from_dict(online["decision"])
        evidence = ProbeEvidence.from_dict(online["evidence"]) if online["evidence"] else None
    else:
        if config.detector.name == "none":
            evidence = None
            report = DetectionReport(view.identity, "static-native-only-v1", digest(view), False, True, False,
                                     ("no_probe_requested",))
        else:
            planner = RandomProbePlanner(p.seed) if config.detector.name == "random-probe" else DiffProbePlanner()
            probes = BoundProbeService(adapter, tasks, source_env, prepared, output / "probes",
                                        seed=p.seed + 1000, max_rounds=p.probe_round_budget)
            report, evidence = ProbeDetector(planner, scorer).detect(view, probes)
        policies = {"conservative": AdmissionPolicy, "native-only": NativeOnlyPolicy, "reject-all": RejectAllPolicy}
        decision = policies[config.detector.policy]().decide(view, report, evidence)
        save_record(online_path, {"report": report, "decision": decision, "evidence": evidence})
    features = audit_features(view, evidence) if evidence and view.semantic_component else None
    if features:
        save_record(output / "features.json", features)
    # Online prediction and decision are durable before independent evaluation begins.
    # Actual gate controls the main system; descendant branches cannot change it.
    activation = None
    if p.mode == "admission":
        controller = ActivationController(output / "controller.sqlite", protocol_sha)
        activation = controller.apply(adapter, prepared, decision, environment_sha256=digest(source_env))
        save_record(output / "activation.json", {"receipt": activation, "active": adapter.snapshot()})
    rows, label_units = [], 0.0
    for replicate in range(p.repetitions):
        for arm, snapshot in (("old", prepared.base), ("new", prepared.draft.candidate)):
            directory = output / "independent" / str(replicate) / arm
            branch = adapter.fork(snapshot, directory / "system")
            outcomes, cost = continue_system(branch, tasks, source_env, directory, seed=p.seed + 2000 + 1000 * replicate,
                family=CursorFamily.INDEPENDENT_EVALUATION, partition=p.partition, horizon=p.horizon,
                schedule=p.schedule, root_id=root_id, protocol_sha256=protocol_sha, arm=arm,
                stream="independent", replicate=replicate)
            rows.extend(outcomes)
            label_units += cost
    label = None
    if features:
        label = CandidateRiskLabel(features.binding, tuple(rows), p.repetitions, root_id + "/independent")
        TrainingExample(features, label)
        save_record(output / "label.json", label)
    deployment, deploy_units = (), 0.0
    if p.mode == "admission":
        deployment, deploy_units = continue_system(adapter, tasks, source_env, output / "deployment",
            seed=p.seed + 3000, family=CursorFamily.INDEPENDENT_EVALUATION, partition=p.partition,
            horizon=p.horizon, schedule=p.schedule, root_id=root_id, protocol_sha256=protocol_sha,
            arm="new" if decision.disposition == "accept" else "old", stream="deployment", update_policy="frozen")
    native_units = PublicTaskEvaluator(adapter, tasks, output / "native", seed=p.seed, partition=p.partition).synthetic_cost()
    return _seal_summary(output, {"mode": p.mode, "system": config.system.name,
        "origin": prepared.identity.origin.value, "engineering_control": prepared.identity.origin.value == "control",
        "decision": decision.disposition, "reason": decision.reason,
        "activated": activation is not None, "active_snapshot_sha256": adapter.snapshot().sha256,
        "proposed_version_sha256": prepared.draft.candidate.version.sha256,
        "deployed_version_sha256": adapter.snapshot().version.sha256,
        "independent": {arm: _summarize([r for r in rows if r.arm == arm]) for arm in ("old", "new")},
        "label_targets": label.targets() if label else None,
        "deployment": _summarize(deployment) if deployment else None,
        "usage": {"native_synthetic_units": native_units, "probe_synthetic_units": evidence.synthetic_units if evidence else 0.0,
                  "label_synthetic_units": label_units, "deployment_synthetic_units": deploy_units,
                  "models": {k: v.summary() for k, v in providers.items()}},
        "limitations": ["one authorization family", "trusted Python adapters with bounded model tools",
                        "single update admission; deployment system frozen afterwards",
                        "delayed trigger does not establish emergent risk", "control is not natural safety performance"]})


def export_dataset(runs, output):
    examples = []
    for path in runs:
        path = _checked_path(Path(path))
        inspect_run(path)
        manifest = read_record(path / "manifest.json")
        if not (path / "features.json").exists() or not (path / "label.json").exists():
            raise ValidationError("Run has no supported paired supervision projection")
        features = AuditFeatures.from_dict(read_record(path / "features.json"))
        label = CandidateRiskLabel.from_dict(read_record(path / "label.json"))
        prepared = PreparedUpdate.from_dict(read_record(path / "prepared.json"))
        if (features.binding != learning_binding(prepared.identity, features.binding.component)
                or prepared.identity.protocol_sha256 != digest(manifest["protocol"])
                or prepared.identity.partition.value != manifest["config"]["protocol"]["partition"]):
            raise IntegrityError("Dataset source identity mismatch")
        examples.append(TrainingExample(features, label))
    return write_dataset(output, tuple(examples))
