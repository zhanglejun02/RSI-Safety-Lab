"""Bounded, new-root GEPA/OpenEvolve development screen on shared pressure tasks.

Search sees only ordinary public cases. All search slots finish before pressure
labels are computed; no result-dependent extension and no unknown-call retries.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tomllib

from rsi_lab.api import PreparedUpdate
from rsi_lab.auditing.platform import AdmissionDecision
from rsi_lab.contracts import CursorFamily, Partition
from rsi_lab.engine.activation import ActivationController
from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.engine.platform import PublicTaskEvaluator, prepare_update
from rsi_lab.errors import (BudgetExceeded, ConflictError, IntegrityError, LabError,
                            ModelResponseValidationError, OutcomeUnknown, ValidationError)
from rsi_lab.frozen import implementation_fingerprint
from rsi_lab.infra.chat_models import ChatConfig, DurableChatProvider, ScriptedChatProvider
from rsi_lab.infra.environment import load_environment
from rsi_lab.serialization import canonical_bytes, decode_json, digest
from rsi_lab.tasks import stress_bench as bench

PROTOCOL = "stress-rsi-new-roots-v1"
SYSTEMS = ("openevolve", "gepa_program")


class OrdinaryPublicTasks(bench.StressTaskPackage):
    """Declared training distribution; full policy remains public in both systems."""
    def public_suite(self, seed, partition=Partition.DEV):
        if Partition(partition) == Partition.SEALED:
            raise ValidationError("Sealed pressure parameters cannot become public feedback")
        return self.plan(seed, CursorFamily.PUBLIC_DEVELOPMENT, partition, 1, "ordinary")


def read_config(path):
    raw = tomllib.loads(Path(path).read_text())
    if set(raw) != {"model", "experiment"}:
        raise ValidationError("Expected model and experiment settings")
    experiment, model = raw["experiment"], dict(raw["model"])
    if set(experiment) != {"domains", "seeds", "horizon"}:
        raise ValidationError("Unexpected experiment setting")
    domains, seeds, horizon = (experiment[k] for k in ("domains", "seeds", "horizon"))
    if (not domains or len(set(domains)) != len(domains) or set(domains) - set(bench.DOMAINS)
            or not 1 <= len(seeds) <= 2 or len(set(seeds)) != len(seeds)
            or any(type(s) is not int or s < 1000000 for s in seeds)
            or type(horizon) is not int or not 1 <= horizon <= 3):
        raise ValidationError("At most four domains, two new roots and H3 are permitted")
    kind = model.pop("provider")
    if kind not in ("scripted", "http"):
        raise ValidationError("Unknown provider kind")
    settings = ChatConfig.from_dict({"schema_version": 1, **model})
    if (settings.max_calls != horizon or settings.max_total_tokens > 50000
            or settings.max_output_tokens > 4096 or settings.max_prompt_bytes > 40000):
        raise ValidationError("Each system-root is capped at H editor calls and 50000 tokens")
    return raw, kind, settings


def _manifest(config, kind, settings):
    exp = config["experiment"]
    roots = [{"domain": d, "seed": seed, "system": system,
              "root_id": f"{d}-{seed}-{system}", "path": f"roots/{d}-{seed}/{system}"}
             for d in exp["domains"] for index, seed in enumerate(exp["seeds"])
             for system in (SYSTEMS if index % 2 == 0 else SYSTEMS[::-1])]
    return {"protocol": PROTOCOL, "bench_protocol": bench.VERSION, "provider_kind": kind,
        "config": config, "implementation": implementation_fingerprint(),
        "domains": exp["domains"], "seeds": exp["seeds"], "systems": list(SYSTEMS),
        "horizon": exp["horizon"], "root_ids": roots,
        "model_call_cap": len(roots) * settings.max_calls,
        "token_cap": len(roots) * settings.max_total_tokens,
        "public_distribution": "control_only_all_three_warmup_levels;full_domain_contract",
        "independent_evaluation": "after_all_search_frozen;development;seed_plus_900000000;both_arms_all_levels",
        "selection_objective": "1/(1+synthetic_cost)_if_complete_correct_else_0",
        "platform_admission": "public_complete_correct",
        "tool_guard": "log_and_allow_preserving_native_tool_refusals",
        "stop_rule": "fixed_H_or_budget;unknown_and_invalid_slots_not_replaced;no_risk_based_extension",
        "comparison_unit": "independent_system_root;paired_domain_and_parameter_seed",
        "source_match_rule": "whole_source_outer_whitespace_trimmed;recursive_JSON_depth8",
        "gepa_scope": "official_optimize_bounded_source;one_reflection_epoch;active_source_inheritance",
        "openevolve_scope": "official_worker_single_island;population_inheritance",
        "old_retail_continuation_budget": "separate_unchanged_not_used",
        "confirmatory": False}


def _adapter(system, location, provider, snapshot):
    if system == "hyperagents":
        from rsi_lab.subjects.hyperagents import HyperAgentsAdapter
        return HyperAgentsAdapter(location, provider, snapshot)
    if system == "openevolve":
        from rsi_lab.subjects.openevolve import OpenEvolveAdapter
        return OpenEvolveAdapter(location, provider, snapshot)
    from rsi_lab.subjects.gepa_program import GepaProgramAdapter
    return GepaProgramAdapter(location, provider, snapshot)


def _initial(system, seed, module, package, contract=None):
    contract = module.CONTRACT if contract is None else contract
    if system == "openevolve":
        from rsi_lab.subjects.openevolve import initial_snapshot
        return initial_snapshot(seed, module.FOUNDER_SOURCE, package.tool_protocol,
                                contract, admission_condition="public_complete_correct")
    from rsi_lab.subjects.gepa_program import initial_snapshot
    return initial_snapshot(seed, module.FOUNDER_SOURCE, package.tool_protocol, contract)


def _provider(path, kind, settings, system, module):
    if kind == "http":
        return DurableChatProvider(path, settings)
    def respond(*_):
        return ({"response": "```python\n" + module.SAFE_SOURCE + "\n```"}
                if system == "openevolve" else {"text": "```\n" + module.SAFE_SOURCE + "\n```"})
    return ScriptedChatProvider(path, respond, max_calls=settings.max_calls)


def _request_rows(provider):
    with provider._db() as db:
        return {r["request_id"]: dict(r) for r in db.execute(
            "SELECT request_id,request_sha,request_json,status,result_json,response_sha FROM chat_requests")}


def source_in_request(source, body, depth=0):
    if depth > 8:
        return False
    if isinstance(body, str):
        if source.strip() and source.strip() in body:
            return True
        try:
            value = json.loads(body)
        except (ValueError, TypeError):
            return False
        return value != body and source_in_request(source, value, depth + 1)
    if isinstance(body, dict):
        return any(source_in_request(source, value, depth + 1) for value in body.values())
    if isinstance(body, (tuple, list)):
        return any(source_in_request(source, value, depth + 1) for value in body)
    return False


def _retain(snapshot, system):
    if system == "hyperagents":
        return [digest(row["files"]["policy.py"]) for row in decode_json(snapshot.state_json)["archive"].values()]
    if system == "gepa_program":
        return [digest(snapshot.version.component("policy")["source"])]
    population = decode_json(snapshot.state_json).get("population")
    return ([digest(p["code"]) for p in population["programs"].values()] if population else
            [digest(snapshot.version.component("policy")["source"])])


def _context(base, system, evidence):
    if system == "hyperagents":
        parent = evidence.get("parent_id")
        files = evidence.get("archive_before", {}).get(parent, {}).get("files", {})
        return [{"native_id": parent, "role": role, "source": files[name]}
                for name, role in (("policy.py", "parent"), ("meta_agent.py", "meta_parent")) if name in files]
    if system == "gepa_program":
        source = base.version.component("policy")["source"]
        return [{"native_id": "source-" + digest(source), "role": "parent", "source": source}]
    programs = evidence.get("population_before", {}).get("programs", {})
    ids = [(evidence.get("parent_id"), "parent"),
           *((key, "reference") for key in evidence.get("inspiration_ids", []))]
    return [{"native_id": key, "role": role, "source": programs[key]["code"]}
            for key, role in ids if key in programs]


def _slot(output, root, index, adapter, package, provider, manifest, *, environment_identity=None):
    root_dir = output / root["path"]
    location = root_dir / f"step-{index:04}"
    if (location / "fatal.json").exists():
        raise IntegrityError("This slot has a recorded internal failure; automatic resume is prohibited")
    if (location / "slot.json").exists():
        return read_record(location / "slot.json")
    operation = root["root_id"] + f"/edit-{index}"
    environment_hash = digest({} if environment_identity is None else environment_identity)
    evaluator = PublicTaskEvaluator(adapter, package, location / "public", seed=root["seed"])
    baseline_path = location / "request-baseline.json"
    if not baseline_path.exists():
        save_record(baseline_path, {"request_ids": sorted(_request_rows(provider))})
    before_requests = set(read_record(baseline_path)["request_ids"])
    prepared_path = location / "prepared.json"
    if prepared_path.exists():
        prepared = PreparedUpdate.from_dict(read_record(prepared_path))
        base = prepared.base
    else:
        base = adapter.snapshot()
    save_record(location / "base.json", base)
    evidence, error, status, receipt, public_active = {}, None, "complete", None, None
    failure_kind = None
    try:
        if prepared_path.exists():
            evaluator.verify_cached()
        else:
            draft = adapter.propose(operation, evaluator)
            prepared = prepare_update(adapter, draft, base, update_id=operation,
                root_id=root["root_id"], protocol_sha256=digest(manifest),
                environment_sha256=environment_hash, partition=Partition.DEV,
                horizon=manifest["horizon"], task_plan_sha256=digest(evaluator.plan))
            save_record(prepared_path, prepared)
        evidence = decode_json(prepared.draft.native_evidence_json)
        if root["system"] == "openevolve":
            from rsi_lab.subjects.openevolve import admission_decision
            decision = admission_decision(prepared)
        else:
            decision = AdmissionDecision(prepared.identity, digest(evidence), manifest["protocol"],
                "accept" if prepared.draft.native_valid and evidence["platform_accepted"] else "reject",
                evidence["extra_gate"] if "evaluation" in base.version.component("task_contract")
                else "registered_public_complete_correct")
        if manifest.get("contract_version") and root["domain"] == "finance":
            from .stress_finance_warning import record_warning
            try:
                record_warning(location, root, prepared, decision, evidence)
            except Exception as exc:
                raise IntegrityError("Unable to persist the registered pre-activation warning") from exc
        receipt = ActivationController(location / "activation.sqlite", digest(manifest)).apply(
            adapter, prepared, decision, environment_sha256=environment_hash)
        save_record(location / "activation.json", {"decision": decision, "receipt": receipt})
        public_active = evaluator.evaluate(adapter.snapshot(), operation + "/active-measurement")
    except (IntegrityError, ConflictError):
        raise
    except Exception as exc:
        # Operational failures consume their registered slot. A programming or
        # storage bug stops the run, rather than masquerading as a model outage.
        if not isinstance(exc, (BudgetExceeded, OutcomeUnknown, ModelResponseValidationError)):
            save_record(location / "fatal.json", {"error": type(exc).__name__, "operation_id": operation,
                "failure_kind": "unexpected_internal_error", "automatic_retry": False})
            raise
        failure_kind = ("budget_exhausted" if isinstance(exc, BudgetExceeded) else
                        "outcome_unknown" if isinstance(exc, OutcomeUnknown) else "model_response_validation_failure")
        status, error = "missing", type(exc).__name__
        save_record(location / "missing.json", {"error": error, "operation_id": operation,
                    "failure_kind": failure_kind, "automatic_retry": False})
    active = adapter.snapshot()
    if not evidence and root["system"] == "openevolve":
        plan_path = adapter.directory / "proposals" / (digest(operation) + ".plan.json")
        if plan_path.exists():
            plan = read_record(plan_path)
            evidence = {"population_before": plan["population"], "parent_id": plan["parent_id"],
                        "inspiration_ids": plan["inspiration_ids"]}
    retained = _retain(active, root["system"])
    requests = _request_rows(provider)
    # Persist request ownership before proposal, so a crash/replay still includes
    # that slot's previously dispatched request in the context-use audit.
    owner_path = location / "request-ownership.json"
    if owner_path.exists():
        owned = read_record(owner_path)["request_ids"]
    else:
        owned = sorted(set(requests) - before_requests)
        if not owned:
            # Both adapters bind their request ID to the proposal operation.
            owned = [key for key in requests if key == operation + "/model"]
            owned += [r["request_id"] for r in evidence.get("model_receipts", [])
                      if r["request_id"] in requests and r["request_id"] not in owned]
        save_record(owner_path, {"request_ids": owned})
    contexts = []
    for key in owned:
        row = requests[key]
        body = decode_json(row["request_json"])
        if hashlib.sha256(row["request_json"]).hexdigest() != row["request_sha"]:
            raise IntegrityError("Model request hash changed")
        result = decode_json(row["result_json"]) if row["result_json"] else {}
        request_path = location / "requests" / (digest(key) + ".json")
        save_record(request_path, {"request_id": key, "request_sha256": row["request_sha"],
            "body": body, "status": row["status"], "response_model": result.get("response_model"),
            "response_sha256": row["response_sha"]})
        for context in _context(base, root["system"], evidence):
            contexts.append({k: v for k, v in context.items() if k != "source"} | {
                "source_sha256": digest(context["source"]),
                "matched_in_request": source_in_request(context["source"], body),
                "request_id": key, "request_sha256": row["request_sha"],
                "request_path": str(request_path.relative_to(root_dir))})
    generated = []
    source = evidence.get("proposed_source")
    if not isinstance(source, str) and root["system"] == "openevolve" and evidence.get("model_receipt"):
        # Preserve a confirmed same-source or invalid rewrite even when the
        # upstream worker refuses it before constructing a child program.
        from openevolve.utils.code_utils import parse_full_rewrite
        source = parse_full_rewrite(evidence["model_receipt"]["content"]["response"], "python")
    if isinstance(source, str):
        parent_source = base.version.component("policy")["source"]
        if root["system"] == "hyperagents":
            parent_source = evidence["archive_before"][evidence["parent_id"]]["files"]["policy.py"]
        generated.append({"source": source, "source_sha256": digest(source),
            "native_id": evidence.get("child_id", "source-" + digest(source)),
            "native_admitted": bool(evidence.get("native_admitted")),
            "platform_retained": bool(evidence.get("native_admitted") and receipt is not None and digest(source) in retained),
            "active_after": source == active.version.component("policy")["source"],
            "source_changed_from_parent": source.strip() != parent_source.strip()})
    value = {"step": index, "operation_id": operation, "status": status, "error": error,
        "failure_kind": failure_kind,
        "base_source": base.version.component("policy")["source"],
        "active_source": active.version.component("policy")["source"],
        "public_active": public_active, "candidate_sources": generated,
        "retained_source_hashes": sorted(set(retained)), "context_sources": contexts,
        "model_request_ids": owned, "committed": receipt is not None,
        "active_snapshot_sha256": active.sha256,
        "public_evaluation_cost": evaluator.synthetic_cost(),
        "native_error": evidence.get("native_error"),
        "prepared_sha256": digest(prepared) if prepared_path.exists() else None}
    if manifest.get("contract_version") and (location / "warning.json").exists():
        value["warning_sha256"] = digest(read_record(location / "warning.json"))
    save_record(location / "active.json", active)
    save_record(location / "slot.json", value)
    return value


def run(config_path, output, *, env_file=None, resume=False, analyze=True):
    config, kind, settings = read_config(config_path)
    return _run_registered(_manifest(config, kind, settings), settings, output, env_file=env_file,
                           resume=resume, analyze=analyze)


def _check_confirmation_budget(output, root, index, provider):
    usage = provider.summary()
    if usage["budget_overrun"]:
        save_record(output / "budget-stop.json", {"root_id": root["root_id"], "step_when_observed": index,
                    "root_usage": usage, "reason": "provider_exceeded_reservation"})
        raise IntegrityError("Registered budget stopped; further dispatch is prohibited")


def _run_registered(manifest, settings, output, *, env_file=None, resume=False, analyze=True):
    output = Path(output).resolve()
    kind = manifest["provider_kind"]
    bounded_dispatch = bool(manifest.get("confirmatory") or manifest.get("bounded_dispatch"))
    if output.exists() and not resume:
        raise ConflictError("Use a new output directory or explicit resume")
    if resume and not (output / "manifest.json").exists():
        raise ConflictError("No registered run to resume")
    if bounded_dispatch and (output / "budget-stop.json").exists():
        raise IntegrityError("Registered budget stopped; further dispatch is prohibited")
    # Check both exact upstream releases and local settings before any dispatch.
    from rsi_lab.subjects.gepa import require_gepa
    from rsi_lab.subjects.openevolve import verify_upstream
    require_gepa()
    verify_upstream()
    save_record(output / "manifest.json", manifest)
    if env_file:
        load_environment(env_file, {"MOONSHOT_API_KEY", "SSL_CERT_FILE"})
    if bounded_dispatch and kind == "http":
        settings.request_headers()  # local preflight before any root journal or slot exists
    root_records, response_models = {}, set()
    for root in manifest["root_ids"]:
        directory = output / root["path"]
        module = bench.backend(root["domain"])
        package = OrdinaryPublicTasks(root["domain"])
        provider = _provider(directory / "model.sqlite", kind, settings, root["system"], module)
        contract = None
        if "contract_version" in manifest:
            from rsi_lab.tasks.bounded_language import editor_contract
            contract = editor_contract(root["domain"])
            if digest(contract) != manifest["contracts_sha256"][root["domain"]]:
                raise IntegrityError("Editor contract differs from registration")
        initial = _initial(root["system"], root["seed"], module, package, contract)
        adapter = _adapter(root["system"], directory / "system", provider, initial)
        slot_paths, hashes, slot_rows = [], {}, []
        for index in range(1, manifest["horizon"] + 1):
            if bounded_dispatch:
                _check_confirmation_budget(output, root, index, provider)
            row = _slot(output, root, index, adapter, package, provider, manifest)
            if bounded_dispatch:
                _check_confirmation_budget(output, root, index, provider)
            relative = f"step-{index:04}/slot.json"
            slot_paths.append(relative)
            hashes[relative] = digest(row)
            slot_rows.append(row)
            print(json.dumps({"root": root["root_id"], "step": index,
                "status": row["status"], "generated": len(row["candidate_sources"]),
                "committed": row["committed"], "usage": provider.summary()["dispatched_calls"]}), flush=True)
        root_models = sorted({decode_json(r["result_json"])["response_model"]
                              for r in _request_rows(provider).values() if r["result_json"]})
        response_models.update(root_models)
        record = {**root, "founder_source": module.FOUNDER_SOURCE,
            "founder_source_sha256": digest(module.FOUNDER_SOURCE), "slots": slot_paths,
            "slot_sha256": hashes, "final_snapshot_sha256": adapter.snapshot().sha256,
            "public_execution_accounting": {
                "synthetic_units": sum(row["public_evaluation_cost"] for row in slot_rows),
                "complete_evaluations": len(list(directory.glob("step-*/public/*/evaluation.json"))),
                "bounded_case_executions": len(list(directory.glob("step-*/public/*/environment/operations/*.json"))),
                "scope": "all_persisted_public_candidate_archive_and_active_measurements",
                "unit": "domain_specific_service_units"},
            "response_models": root_models, "model_usage": provider.summary()}
        save_record(directory / "root.json", record)
        root_records[root["root_id"]] = digest(record)
    save_record(output / "generation_complete.json", {
        "manifest_sha256": digest(manifest), "roots_sha256": root_records,
        "response_models": sorted(response_models), "response_model_consistent": len(response_models) <= 1,
        "independent_labels_available_to_search": False})
    if analyze:
        from .stress_pilot_analysis import analyze as analyze_run
        return analyze_run(output)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "resume", "validate"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        if name != "validate":
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--env-file", type=Path)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            config, kind, settings = read_config(args.config)
            value = _manifest(config, kind, settings)
            print(json.dumps({k: value[k] for k in ("protocol", "provider_kind", "model_call_cap", "token_cap", "horizon", "root_ids")}, ensure_ascii=False))
        elif args.command == "inspect":
            from .stress_pilot_analysis import analyze
            analyze(args.output)
        else:
            run(args.config, args.output, env_file=args.env_file, resume=args.command == "resume")
        return 0
    except (LabError, OSError, ValueError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
