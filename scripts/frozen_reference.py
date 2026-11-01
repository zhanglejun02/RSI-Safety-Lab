"""Add matched frozen controls to one completed platform run without reoptimizing.

The native observations remain the original run's registered evidence. This
post-run reference is descriptive, not a prospectively balanced four-arm trial.
Only the installed authorization task snapshots are currently supported.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

from rsi_lab.api import PreparedUpdate, SystemSnapshot
from rsi_lab.contracts import CursorFamily
from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.engine.platform import continue_system
from rsi_lab.errors import ConflictError, IntegrityError, LabError, ValidationError
from rsi_lab.frozen import implementation_fingerprint
from rsi_lab.infra.artifacts import _checked_path
from rsi_lab.infra.environment import load_environment
from rsi_lab.learning.contracts import RoundOutcome
from rsi_lab.platform_config import PlatformConfig
from rsi_lab.platform_wiring import builtin_registry, build_providers
from rsi_lab.serialization import decode_json, digest, primitive
from rsi_lab.studies.pilot_report import summarize_arm, summarize_root
from rsi_lab.studies.platform import inspect_run, protocol, task_anchor, _seal_summary
from rsi_lab.tasks.authorization import WorldSnapshot


def script_fingerprint():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _source(source_run, registry):
    summary = inspect_run(source_run)
    manifest = read_record(source_run / "manifest.json")
    config = PlatformConfig.from_dict(manifest["config"])
    if config.protocol.mode not in ("paired", "admission") or summary.get("mode") != config.protocol.mode:
        raise ValidationError("Reference requires a completed normal paired/admission platform run")
    if config.task.name not in ("authorization", "authorization-workflows"):
        raise ValidationError("Reference snapshot decoding supports the authorization task packages only")
    descriptor, tasks = registry.preflight(config)
    if (manifest["implementation_sha256"] != implementation_fingerprint()
            or digest(manifest["protocol"]) != digest(protocol(config, descriptor))):
        raise IntegrityError("Source implementation or protocol differs from the current engine")
    prepared = PreparedUpdate.from_dict(read_record(source_run / "prepared.json"))
    root_source = read_record(source_run / "source.json")
    anchor = read_record(source_run / "task-anchor.json")
    p, identity = config.protocol, prepared.identity
    if (digest(anchor) != digest(task_anchor(tasks, config))
            or identity.task_plan_sha256 != digest(anchor)
            or identity.protocol_sha256 != digest(manifest["protocol"])
            or identity.root_id != f"root-{p.seed}" or identity.horizon != p.horizon
            or identity.partition != p.partition
            or SystemSnapshot.from_dict(root_source["system"]) != prepared.base
            or root_source["environment_sha256"] != identity.environment_sha256):
        raise IntegrityError("Root candidate does not bind the registered source and task plans")
    rows, receipts, source_environment = {"old": [], "new": []}, {}, None
    for arm, snapshot in (("old", prepared.base), ("new", prepared.draft.candidate)):
        for k in range(p.repetitions):
            relative = Path("independent") / str(k) / arm
            branch_source = read_record(source_run / relative / "source.json")
            environment = WorldSnapshot.from_dict(branch_source["environment"])
            if (SystemSnapshot.from_dict(branch_source["system"]) != snapshot
                    or digest(environment) != identity.environment_sha256):
                raise IntegrityError("Native branch initial snapshot differs from the root candidate or environment")
            source_environment = environment
            plan = tasks.plan(p.seed + 2000 + 1000 * k, CursorFamily.INDEPENDENT_EVALUATION,
                              p.partition, p.horizon, p.schedule)
            record = read_record(source_run / relative / "continuation.json")
            expected = {"source_sha256": digest(branch_source), "plan": plan,
                "root": identity.root_id, "protocol": identity.protocol_sha256,
                "arm": arm, "stream": "independent", "replicate": k, "update_policy": "native"}
            if (digest(record) != digest({"identity": expected, "attempts": list(range(1, p.horizon + 1))})
                    or digest(plan.rounds) != digest(anchor["independent"][k])):
                raise IntegrityError("Native branch task plan is not the matched independent plan")
            for i in range(1, p.horizon + 1):
                receipt_name = relative / f"attempt-{i:04}.json"
                attempt = read_record(source_run / receipt_name)
                row = RoundOutcome.from_dict(attempt["outcome"])
                expected_trace = (digest({"stream": "independent", "records": attempt["trace"]})
                                  if attempt["trace"] else None)
                if (row.arm != arm or row.replicate != k or row.attempt != i
                        or row.candidate_sha256 != digest(attempt["active_version"])
                        or row.trace_sha256 != expected_trace):
                    raise IntegrityError("Native outcome does not match its registered arm and execution evidence")
                rows[arm].append(row)
                receipts[str(receipt_name)] = attempt
    return config, manifest, summary, prepared, tasks, source_environment, rows, receipts


def run_reference(source_run, output, *, env_file=None, registry=None, provider_factory=None):
    source_run, output = _checked_path(Path(source_run)), _checked_path(Path(output))
    if source_run == output or source_run.is_relative_to(output) or output.is_relative_to(source_run):
        raise ValidationError("Reference output must be separate from the immutable source run")
    registry, provider_factory = registry or builtin_registry(), provider_factory or build_providers
    config, manifest, summary, prepared, tasks, environment, native_rows, receipts = _source(source_run, registry)
    scope = {"study": "posthoc-frozen-reference-v1", "source_protocol_sha256": prepared.identity.protocol_sha256,
             "continuation": "frozen", "task_plan_sha256": prepared.identity.task_plan_sha256,
             "implementation_sha256": implementation_fingerprint(), "script_sha256": script_fingerprint()}
    binding = {"config": config, "protocol": scope, "source_run": str(source_run),
        "source_manifest_sha256": digest(manifest), "source_prepared_sha256": digest(prepared),
        "source_summary_sha256": digest(summary),
        "source_evidence_manifest_sha256": digest(read_record(source_run / "evidence_manifest.json")),
        "source_environment_sha256": digest(environment),
        "native_receipt_sha256": {name: digest(value) for name, value in receipts.items()}}
    if output.exists():
        if not (output / "manifest.json").exists():
            raise ConflictError("Existing reference output has no registered manifest")
        if digest(read_record(output / "manifest.json")) != digest(binding):
            raise IntegrityError("Reference script, source, engine or configuration changed")
        if (output / "summary.json").exists():
            inspect_run(output)
    else:
        output.mkdir(parents=True)
    save_record(output / "manifest.json", binding)
    if env_file is not None:
        load_environment(env_file, {config.actor.api_key_env, config.editor.api_key_env, "SSL_CERT_FILE"} - {None})
    # Copy only checksummed JSON receipts, never source databases or credentials.
    for name, receipt in receipts.items():
        save_record(output / "native-evidence" / name, receipt)
    p = config.protocol
    arms, usage = {}, {}
    for arm, snapshot in (("old", prepared.base), ("new", prepared.draft.candidate)):
        frozen_rows, synthetic_units, models = [], 0.0, []
        for k in range(p.repetitions):
            branch_dir = output / "frozen" / str(k) / arm
            providers = provider_factory(config, branch_dir / "models")
            owner = registry.build_system(config, branch_dir / "owner", providers)
            branch = owner.fork(snapshot, branch_dir / "system")
            outcomes, units = continue_system(branch, tasks, environment, branch_dir,
                seed=p.seed + 2000 + 1000 * k, family=CursorFamily.INDEPENDENT_EVALUATION,
                partition=p.partition, horizon=p.horizon, schedule=p.schedule,
                root_id=prepared.identity.root_id, protocol_sha256=digest(scope), arm=arm,
                stream="frozen-reference", replicate=k, update_policy="frozen")
            frozen_rows.extend(outcomes)
            synthetic_units += units
            models.append({role: provider.summary() for role, provider in providers.items()})
        arms[arm + "_frozen"] = summarize_arm(tuple(frozen_rows), p.horizon, p.repetitions)
        arms[arm + "_native"] = summarize_arm(tuple(native_rows[arm]), p.horizon, p.repetitions)
        usage[arm] = {"synthetic_units": synthetic_units, "models_by_replicate": models}
    # Verify the original JSON evidence has not changed while the extra calls ran.
    if digest(inspect_run(source_run)) != binding["source_summary_sha256"]:
        raise IntegrityError("Source evidence changed during the frozen reference")
    evidence = decode_json(prepared.draft.native_evidence_json)
    update_class = evidence.get("update_class", "no_op" if prepared.base == prepared.draft.candidate else "unscored_change")
    result = {"study": scope["study"], "root_id": prepared.identity.root_id,
        "origin": prepared.identity.origin.value, "update_class": update_class,
        "native_valid": prepared.draft.native_valid,
        "primary_eligible": prepared.draft.native_valid and update_class == "strict_improvement"
                            and all(a["complete"] for a in arms.values()),
        "arms": arms, "comparisons": summarize_root(arms),
        "usage": {"new_frozen_models": usage, "original_run_shared_usage": summary["usage"]},
        "source_prepared_sha256": digest(prepared), "source_protocol_sha256": prepared.identity.protocol_sha256,
        "frozen_protocol_sha256": digest(scope), "source_unchanged": True,
        "limitations": ["Post-run descriptive reference using the same registered candidate, not a new optimization.",
            "Native outcomes retain the source run's shared model budget and any missing results.",
            "Frozen controls use independent per-arm model budgets; this is not a prospectively balanced four-arm trial.",
            "Source JSON receipts are verified; this script does not reopen or repair native SQLite state.",
            "No confidence or causal claim follows from one root or an incomplete native pair."]}
    return _seal_summary(output, result)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args(argv)
    try:
        result = run_reference(args.source_run, args.output, env_file=args.env_file)
        print(json.dumps(primitive(result), ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (LabError, OSError, sqlite3.DatabaseError) as exc:
        print(f"{type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
