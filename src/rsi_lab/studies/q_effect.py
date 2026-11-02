"""Repeated q interventions using the existing native continuation kernel.

This is a development mechanism experiment, not a sealed admission benchmark.
All candidates/interventions are frozen before any descendant is generated.
Only model sampling varies across repetitions of a root's registered task plan.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
import random
import sqlite3
import tomllib

from rsi_lab.api import PreparedUpdate
from rsi_lab.auditing.platform import learning_binding
from rsi_lab.contracts import CursorFamily, Partition, StrictRecord, require_count
from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.engine.platform import PublicTaskEvaluator, continue_system, prepare_update
from rsi_lab.errors import ConflictError, IntegrityError, LabError, ValidationError
from rsi_lab.frozen import implementation_fingerprint
from rsi_lab.infra.artifacts import _publish_bytes
from rsi_lab.infra.chat_models import ChatConfig, DurableChatProvider, ScriptedChatProvider
from rsi_lab.learning.contracts import CandidateRiskLabel
from rsi_lab.platform_config import ModelSettings
from rsi_lab.serialization import decode_json, digest
from rsi_lab.subjects.cost_q import CostQSystemAdapter, ExperienceSelector, native_valid, scripted_cost_response
from rsi_lab.subjects.q_interventions import build_q_interventions
from rsi_lab.tasks.platform_cost_authorization import CostAuthorizationTaskPackage
from .cost_q import _bootstrap, _ledger, _static_and_plan, _usage
from .q_effect_analysis import analyze_replicates, analysis_protocol, render_report

ARMS = ("old", "new", "restore", "sham")
CORE_ARMS = {"old": "old", "new": "new", "restore": "new", "sham": "old"}


@dataclass(frozen=True)
class QEffectConfig(StrictRecord):
    seeds: tuple[int, ...] = (10101, 10102, 10103, 10104, 10105, 10106)
    repetitions: int = 3
    horizon: int = 4
    parallel_jobs: int = 3
    order_seed: int = 20261101
    model: ModelSettings = field(default_factory=ModelSettings)
    schema_version: int = 1

    @property
    def calls_per_root(self):
        return 1 + len(ARMS) * self.repetitions * self.horizon

    def validate(self):
        if not 1 <= len(self.seeds) <= 12 or len(set(self.seeds)) != len(self.seeds):
            raise ValidationError("Register 1..12 distinct development roots")
        for seed in self.seeds:
            require_count(seed, "root seed")
        require_count(self.order_seed, "order seed")
        if not 2 <= self.repetitions <= 8 or not 3 <= self.horizon <= 8:
            raise ValidationError("Repeated mechanism study requires2..8 replicates and3..8 attempts")
        if not 1 <= self.parallel_jobs <= 4:
            raise ValidationError("At most4 concurrent jobs")
        if self.model.provider == "http" and self.model.max_calls != self.calls_per_root:
            raise ValidationError("Model call cap must equal1+4*K*H per registered root")


def load_config(path):
    with Path(path).open("rb") as handle:
        raw = tomllib.load(handle)
    model = ModelSettings.from_dict({"schema_version": 1, **raw.pop("model", {})})
    return QEffectConfig.from_dict({"schema_version": 1, "model": model.to_dict(), **raw})


def _roots(config):
    return tuple({"seed": seed, "partition": Partition.DEV.value,
                  "root_id": f"q-effect-{config.model.provider}-{seed}",
                  "control_variant": "harmful" if config.model.provider == "scripted" else None}
                 for seed in config.seeds)


def _scope(config):
    return {"study": "q-repeated-intervention-v1", "config": config,
        "implementation_sha256": implementation_fingerprint(), "analysis": analysis_protocol(),
        "origin": "natural" if config.model.provider == "http" else "control",
        "partition": "development_only_no_sealed_or_training_use_this_run",
        "arms": ARMS, "core_outcome_arm_mapping": CORE_ARMS,
        "arm_provenance": {"old": "baseline", "new": "single_proposed_q_candidate",
            "restore": "controlled_context_reversion", "sham": "unchanged_baseline_independent_sampling"},
        "intervention": "restore_entire_old_selected_context_and_order;retain_new_unselected_tail",
        "sham": "exact_old_snapshot_independent_editor_requests",
        "repetitions": "independent_model_requests_same_root_task_parameters_no_seed_coupling",
        "continuation": "q_fixed;A_native_cost_selection;rejects_count_as_attempts;fresh_cache_per_stream",
        "candidate_gate": "schema_and_current_program_correct;not_demonstrated_q_improvement",
        "public_recheck": "final_A_and_founder_on_registered_fresh_public_parameters;not_editor_feedback",
        "risk_endpoint": "mean_H_round_actual_violation_indicator;missing_stays_null",
        "no_adaptive_search": "all_root_candidates_frozen_before_descendants;no_replacement_roots_or_extra_attempts",
        "budget": "same_call_caps_native_task_plans_and_horizon;actual_costs_reported_separately",
        "limits": ["small_development_cohort", "local_optimizer_not_external_RSI",
            "restore_is_context_reversion_not_single_entry_attribution", "no_new_scorer_or_probe_method_claim",
            "no_exact_API_sampling_seed_or_stationarity_guarantee", "public_cost_is_synthetic_not_dollars"]}


def create_run(config, output):
    output = Path(output)
    if output.exists():
        raise ConflictError("Run exists; use immutable resume")
    tasks, rows, scope = CostAuthorizationTaskPackage(), _roots(config), _scope(config)
    jobs = [{"root_id": row["root_id"], "replicate": rep, "arm": arm}
            for row in rows for rep in range(config.repetitions) for arm in ARMS]
    random.Random(config.order_seed).shuffle(jobs)
    anchors = {row["root_id"]: {
        "founder_public": digest(tasks.public_suite(row["seed"], Partition.DEV)),
        "independent": digest(tasks.plan(row["seed"] + 2000, CursorFamily.INDEPENDENT_EVALUATION,
            Partition.DEV, config.horizon, "delayed")),
        "native_public": [digest(tasks.public_suite(row["seed"] + 12000 + h, Partition.DEV))
                          for h in range(1, config.horizon + 1)],
        "fresh_public": digest(tasks.public_suite(row["seed"] + 30000, Partition.DEV))}
        for row in rows}
    save_record(output / "manifest.json", {"config": config, "scope": scope,
        "protocol_sha256": digest(scope), "roots": rows, "task_anchors": anchors,
        "submission_order": jobs, "submission_note": "completion_order_not_controlled;concurrent_bounded_jobs",
        "max_editor_requests": len(rows) * config.calls_per_root,
        "max_model_tokens": len(rows) * config.model.max_total_tokens if config.model.provider == "http" else None})
    return resume_run(output)


def _provider(config, path, max_calls):
    if config.model.provider == "scripted":
        return ScriptedChatProvider(path, scripted_cost_response,
            provider_id="q-effect-control-v1", max_calls=max_calls)
    setting = config.model
    return DurableChatProvider(path, ChatConfig(setting.base_url, setting.model, setting.api_key_env,
        temperature=setting.temperature, max_output_tokens=setting.max_output_tokens,
        max_calls=max_calls, max_total_tokens=setting.max_total_tokens * max_calls // config.calls_per_root,
        timeout_seconds=setting.timeout_seconds, max_prompt_bytes=98304, thinking_mode=setting.thinking_mode))


def _prepare_root(output, config, row, protocol):
    directory = output / "roots" / row["root_id"]
    failure = directory / "root-failure.json"
    if failure.exists():
        return None
    try:
        provider = _provider(config, directory / "models/root.sqlite", 1)
        tasks = CostAuthorizationTaskPackage()
        initial = _bootstrap(output, config, row, provider, tasks)
        subject = CostQSystemAdapter(directory / "system", provider, initial)
        source = tasks.create_environment(directory / "source-environment").snapshot()
        evaluator = PublicTaskEvaluator(subject, tasks, directory / "root-native", seed=row["seed"])
        path = directory / "prepared.json"
        if path.exists():
            prepared = PreparedUpdate.from_dict(read_record(path))
            evaluator.verify_cached()
        else:
            draft = subject.propose_mechanism(row["root_id"] + "/root", evaluator)
            prepared = prepare_update(subject, draft, initial, update_id=row["root_id"] + "/q-update",
                root_id=row["root_id"], protocol_sha256=protocol, environment_sha256=digest(source),
                partition=Partition.DEV, horizon=config.horizon,
                task_plan_sha256=digest(tasks.plan(row["seed"] + 2000, CursorFamily.INDEPENDENT_EVALUATION,
                    Partition.DEV, config.horizon, "delayed")))
            save_record(path, prepared)
        intervention = build_q_interventions(prepared.base, prepared.draft.candidate)
        flagged, plan = _static_and_plan(prepared)
        metadata = {"native_eligible": prepared.draft.native_valid,
            "changed": prepared.base.version.component("experience_selector") != prepared.draft.candidate.version.component("experience_selector"),
            "selected_context_changed": _selected(prepared.base) != _selected(prepared.draft.candidate),
            "intervention": intervention.metadata,
            "blind_static_decision": "reject" if flagged or not prepared.draft.native_valid else "accept",
            "blind_suggested_probe": plan}
        save_record(directory / "interventions.json", {"arms": intervention.arms, "metadata": metadata})
        # Recheck baseline before descendants, then freeze its measured cost. This
        # suite is held out from editor feedback, not a newly exposed optimizer task.
        recheck = PublicTaskEvaluator(subject, tasks, directory / "founder-recheck", seed=row["seed"] + 30000)
        baseline = recheck.evaluate(initial, row["root_id"] + "/founder-recheck")
        save_record(directory / "founder-recheck.json", baseline)
        return {"prepared": prepared, "interventions": intervention, "source": source,
                "metadata": metadata, "baseline": baseline}
    except (IntegrityError, ConflictError):
        raise
    except LabError as exc:
        save_record(failure, {"root_id": row["root_id"], "reason": type(exc).__name__,
            "missing": True, "retry": "no_retry_or_replacement"})
        return None


def _selected(snapshot):
    return ExperienceSelector.from_dict(snapshot.version.component("experience_selector")).select(
        snapshot.version.component("experience"))


def _missing_row(root_id, replicate, arm):
    return {"root_id": root_id, "replicate": replicate, "arm": arm, "complete": False,
        "risk": None, "utility": None, "public_cost": None, "evaluation_cost": None,
        "actual_violations": None, "accepted_updates": 0, "final_source_sha256": None,
        "selected_experience_sha256": None, "missing_reason": "root_preparation_failed"}


def _run_slot(output, config, row, job, root, protocol):
    root_id, arm, rep = row["root_id"], job["arm"], job["replicate"]
    if root is None:
        return _missing_row(root_id, rep, arm)
    directory = output / "roots" / root_id / "repetitions" / f"{rep:03d}" / arm
    # Independent physical journals, even for identical q, prevent response reuse.
    provider = _provider(config, directory / "editor.sqlite", config.horizon)
    initial = root["interventions"].arms[arm]
    subject = CostQSystemAdapter(directory / "system", provider, initial)
    tasks = CostAuthorizationTaskPackage()
    core = directory / "continuation"
    outcomes, units = continue_system(subject, tasks, root["source"], core,
        seed=row["seed"] + 2000, family=CursorFamily.INDEPENDENT_EVALUATION, partition=Partition.DEV,
        horizon=config.horizon, schedule="delayed", root_id=root_id, protocol_sha256=protocol,
        arm=CORE_ARMS[arm], stream="effect-" + arm, replicate=rep, update_policy="native")
    records = [read_record(core / f"attempt-{h:04d}.json") for h in range(1, config.horizon + 1)]
    selected_hash = digest(_selected(initial))
    attempts = []
    for record in records:
        native = decode_json(record["prepared"]["draft"]["native_evidence_json"]) if record["prepared"] else {}
        version = record["active_version"]
        q = next(c for c in version["components"] if c["name"] == "experience_selector")
        if decode_json(q["content_json"]) != initial.version.component("experience_selector"):
            raise IntegrityError("q changed during a native source continuation")
        if native and digest(native["selected_experience"]) != selected_hash:
            raise IntegrityError("Editor did not receive the registered q-selected context")
        proposed = native.get("candidate", {})
        incumbent = native.get("incumbent", {})
        accepted = bool(record["activation"])
        attempts.append({"attempt": record["outcome"]["attempt"], "accepted": accepted,
            "complete": record["outcome"]["complete"], "risk": record["outcome"]["risk"],
            "actual_violations": record["evaluation"]["actual_violations"],
            "selected_experience_sha256": digest(native["selected_experience"]) if native else None,
            "incumbent_public_cost": incumbent.get("synthetic_units"),
            "candidate_public_cost": proposed.get("synthetic_units"),
            "selected_public_cost": (proposed if accepted else incumbent).get("synthetic_units"),
            "executed_version_sha256": record["outcome"]["candidate_sha256"],
            "executed_source_sha256": digest(next(decode_json(c["content_json"])["source"]
                for c in version["components"] if c["name"] == "policy")),
            "public_trajectory_rows_supplied": native.get("public_trajectory_rows_supplied"),
            "missing_reason": record["missing_reason"]})
    complete = all(o.complete and o.risk is not None for o in outcomes)
    recheck = PublicTaskEvaluator(subject, tasks, directory / "fresh-public", seed=row["seed"] + 30000)
    final = subject.snapshot()
    fresh_failure = directory / "fresh-public-failure.json"
    if fresh_failure.exists():
        fresh = read_record(fresh_failure)
    else:
        try:
            fresh = recheck.evaluate(final, root_id + f"/fresh/{rep}/{arm}")
        except (IntegrityError, ConflictError):
            raise
        except LabError as exc:
            fresh = {"complete": False, "synthetic_units": None, "missing_reason": type(exc).__name__}
            save_record(fresh_failure, fresh)
    fresh_valid = native_valid(fresh)
    denominator = sum(o.legal_total for o in outcomes)
    evaluation_ledger = _ledger(core / "environment")
    summary = {"root_id": root_id, "replicate": rep, "arm": arm, "complete": complete,
        "risk": sum(o.risk for o in outcomes) / len(outcomes) if complete else None,
        "utility": sum(o.legal_completed for o in outcomes) / denominator if complete and denominator else None,
        "public_cost": fresh["synthetic_units"] if complete and fresh_valid else None,
        "evaluation_cost": evaluation_ledger["synthetic_service_units"] if complete else None,
        "actual_violations": sum(r["evaluation"]["actual_violations"] for r in records),
        "accepted_updates": sum(r["accepted"] for r in attempts),
        "final_source_sha256": digest(final.version.component("policy")["source"]),
        "selected_experience_sha256": selected_hash, "attempts": attempts,
        "fresh_public_valid": fresh_valid, "fresh_public_raw_cost": fresh["synthetic_units"],
        "founder_fresh_public_cost": root["baseline"]["synthetic_units"],
        "fresh_public_gain_fraction": (1 - fresh["synthetic_units"] / root["baseline"]["synthetic_units"])
            if complete and fresh_valid and native_valid(root["baseline"]) and root["baseline"]["synthetic_units"] else None,
        "observed_legal_completed": sum(o.legal_completed for o in outcomes),
        "observed_legal_total": denominator,
        "work": _ledger(directory), "editor": provider.summary(),
        "continuation_units_including_native": units, "fresh_public_sha256": digest(fresh)}
    save_record(directory / "outcomes.json", {"semantic_arm": arm, "core_arm": CORE_ARMS[arm], "outcomes": outcomes})
    save_record(directory / "summary.json", summary)
    return summary


def _input_provenance(output, config, roots):
    """Verify identical starting editor inputs really got independent requests."""
    result = {}
    for root in roots:
        directory = output / "roots" / root["root_id"]
        rows = []
        for rep in range(config.repetitions):
            for arm in ARMS:
                path = directory / "repetitions" / f"{rep:03d}" / arm / "editor.sqlite"
                if not path.exists():
                    continue
                with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
                    first = db.execute("SELECT request_id,request_sha,status,response_sha FROM chat_requests ORDER BY rowid LIMIT 1").fetchone()
                if first:
                    rows.append({"replicate": rep, "arm": arm, "request_id": first[0],
                        "request_sha256": first[1], "status": first[2], "response_sha256": first[3]})
        controls = [r for r in rows if r["arm"] in ("old", "restore", "sham")]
        if len({r["request_sha256"] for r in controls}) > 1:
            raise IntegrityError("Identical restored/old context received different initial editor inputs")
        if len({r["request_id"] for r in rows}) != len(rows):
            raise IntegrityError("Independent repetitions reused a model request identity")
        new_inputs = {r["request_sha256"] for r in rows if r["arm"] == "new"}
        if len(new_inputs) > 1:
            raise IntegrityError("Repetitions did not start from identical candidate editor inputs")
        result[root["root_id"]] = {"first_requests": rows,
            "old_restore_sham_input_equal": len({r["request_sha256"] for r in controls}) <= 1,
            "complete_first_request_grid": len(rows) == len(ARMS) * config.repetitions,
            "request_ids_distinct": True}
    save_record(output / "input-equivalence.json", result)
    return result


def resume_run(output):
    output = Path(output)
    manifest = read_record(output / "manifest.json")
    config = QEffectConfig.from_dict(manifest["config"])
    if digest(_scope(config)) != manifest["protocol_sha256"]:
        raise IntegrityError("Protocol/implementation changed; use archived code or register new roots")
    protocol, roots = manifest["protocol_sha256"], manifest["roots"]
    with ThreadPoolExecutor(max_workers=config.parallel_jobs) as pool:
        prepared = list(pool.map(lambda r: _prepare_root(output, config, r, protocol), roots))
    by_root = {r["root_id"]: value for r, value in zip(roots, prepared)}
    metadata = {key: value["metadata"] if value else {"native_eligible": None, "changed": None,
                "selected_context_changed": None, "missing": True} for key, value in by_root.items()}
    save_record(output / "candidates-frozen.json", {"root_metadata": metadata,
        "interventions_sha256": {key: digest(value["interventions"].arms) if value else None
                                  for key, value in by_root.items()}})
    row_index = {r["root_id"]: r for r in roots}
    with ThreadPoolExecutor(max_workers=config.parallel_jobs) as pool:
        samples = tuple(pool.map(lambda job: _run_slot(output, config, row_index[job["root_id"]], job,
                                 by_root[job["root_id"]], protocol), manifest["submission_order"]))
    input_provenance = _input_provenance(output, config, roots)
    # Original old/new repeats retain the existing update-label contract. Restore
    # and sham are separate interventions, never relabelled as extra natural roots.
    for row in roots:
        root_id = row["root_id"]
        if by_root[root_id] is None:
            continue
        from rsi_lab.learning.contracts import RoundOutcome
        outcomes = []
        for rep in range(config.repetitions):
            for arm in ("old", "new"):
                path = output / "roots" / root_id / "repetitions" / f"{rep:03d}" / arm / "outcomes.json"
                outcomes.extend(RoundOutcome.from_dict(o) for o in read_record(path)["outcomes"])
        label = CandidateRiskLabel(learning_binding(by_root[root_id]["prepared"].identity, "q"),
                                   tuple(outcomes), config.repetitions, root_id + "/repeated-independent")
        save_record(output / "roots" / root_id / "label.json", label)
    analysis = analyze_replicates(samples, expected_roots=tuple(row_index),
                                 expected_replicates=config.repetitions, root_metadata=metadata)
    summary = {"status": "complete" if all(r["complete"] for r in samples) else "complete_with_missing",
        "protocol_sha256": protocol, "origin": manifest["scope"]["origin"],
        "registered_roots": len(roots), "repetitions": config.repetitions, "horizon": config.horizon,
        "root_metadata": metadata, "samples": samples, "analysis": analysis,
        "input_equivalence": input_provenance, "total_task_work": _ledger(output),
        "usage": _usage(output, config), "max_editor_requests": manifest["max_editor_requests"],
        "limits": manifest["scope"]["limits"], "no_gpu_required": True}
    save_record(output / "summary.json", summary)
    report = render_report(analysis)
    report += ("\n## 机制输入与新参数收益复验\n\n"
        "q 的原生通过只表示格式有效且当前程序正确，不等于 q 已带来收益。"
        "下表成本降幅相对同根 founder，按各臂独立重复取均值；缺失或公开正确性失败不计作节省。"
        "增险与收益都以根为统计单位，实际越权次数是所有重复中的观测计数。\n\n"
        "| 根 | 原生通过 | q 对象变化 | 实际上下文变化 |\n| --- | --- | --- | --- |\n")
    for row in roots:
        meta = metadata[row["root_id"]]
        report += f"| {row['root_id']} | {meta['native_eligible']} | {meta['changed']} | {meta['selected_context_changed']} |\n"
    report += ("\n| 根 | 臂 | 新参数有效复验 | 对 founder 平均成本降幅 | 后代接受次数 | 实际越权次数 |\n"
               "| --- | --- | ---: | ---: | ---: | ---: |\n")
    for row in roots:
        for arm in ARMS:
            records = [r for r in samples if r["root_id"] == row["root_id"] and r["arm"] == arm]
            gains = [r["fresh_public_gain_fraction"] for r in records if r.get("fresh_public_gain_fraction") is not None]
            gain = f"{sum(gains) / len(gains):.2%}" if len(gains) == config.repetitions else "缺失"
            report += (f"| {row['root_id']} | {arm} | {len(gains)}/{config.repetitions} | {gain} | "
                f"{sum(r['accepted_updates'] for r in records)} | "
                f"{sum(r['actual_violations'] or 0 for r in records)}（已观察） |\n")
    report += ("\n\n运行来源：" + summary["origin"] + "；真实模型调用：" + str(summary["usage"]["model_requests"])
        + "；已知 token：" + str(summary["usage"]["known_tokens"]) + "。\n"
        "\n本轮没有重训评分器或产生新的审计方法成绩；独立长程轨迹不能改称在线短探针。"
        "恢复仅回退整个已选经验上下文，不修复程序，也不定位单条致因经验。\n")
    path = output / "report.md"
    data = report.encode()
    if not _publish_bytes(data, path) and path.read_bytes() != data:
        raise IntegrityError("Immutable report differs on replay")
    return summary
