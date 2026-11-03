"""Zero-API, single-step archive counterfactuals conditional on recorded history.

This never constructs a model provider or executes candidate programs. The pinned
worker only parses a recorded response and consumes recorded public evaluation.
Every row restores its own historical plan; rows are never chained together.
"""
import argparse
import hashlib
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import random
import sqlite3

from rsi_lab.api import PreparedUpdate, SystemSnapshot, replace_component
from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.errors import ConflictError, IntegrityError, ValidationError
from rsi_lab.serialization import decode_json, digest, json_text
from rsi_lab.subjects.openevolve import (REVISION, _artifact, _export, _fitness,
    _native_context, _restore, _tuples, database_config, verify_upstream)
from rsi_lab.subjects.public_validation import cost_candidate_valid


class _RecordedModel:
    """Local fixture, deliberately has no transport or API credentials."""
    def __init__(self, receipt):
        self.receipt = receipt
        self.last_usage = {"total_tokens": receipt["total_tokens"]}
        self.last_model = receipt["response_model"]

    def reseed(self, iteration):
        pass

    async def generate_with_context(self, system_message, messages):
        return self.receipt["content"]["response"]


class _RecordedEvaluator:
    def __init__(self, evidence):
        self.evidence, self.calls = evidence, 0

    async def evaluate_program(self, source, child_id):
        if source != self.evidence.get("proposed_source") or not self.evidence.get("public_candidate"):
            raise IntegrityError("Worker replay requested an unrecorded public evaluation")
        self.calls += 1
        return _fitness(self.evidence["public_candidate"])

    def get_pending_artifacts(self, child_id):
        return _artifact(self.evidence["public_candidate"])


def _replay_worker(prepared, evidence, plan):
    from openevolve.config import Config, PromptConfig
    from openevolve.prompt.sampler import PromptSampler
    from openevolve.database import Program, ProgramDatabase
    base = prepared.base
    iteration = decode_json(base.state_json)["generation"] + 1
    config = Config(diff_based_evolution=False, language="python", max_code_length=16384,
        database=database_config(), prompt=PromptConfig(
            system_message=base.version.component("task_contract")["text"],
            use_template_stochasticity=False, num_top_programs=2, num_diverse_programs=1,
            max_artifact_bytes=12000))
    model, evaluator = _RecordedModel(evidence["model_receipt"]), _RecordedEvaluator(evidence)
    with _native_context(config, PromptSampler(config.prompt), model, evaluator, plan["rng"]) as worker:
        # Validate the restoration as well as the plan's serialized source.
        db = ProgramDatabase(config.database)
        _restore(db, deepcopy(plan["population"]))
        if _export(db) != evidence["population_before"]:
            raise IntegrityError("Population restore differs from recorded pre-admission state")
        result = worker._run_iteration(iteration, deepcopy(plan["input"]), plan["parent_id"],
                                       plan["inspiration_ids"], "full_rewrite")
        rng = random.getstate()
    if result.prompt != evidence["native_prompt"] or result.error != evidence["native_error"]:
        raise IntegrityError("Pinned worker replay differs from recorded prompt or rejection")
    if not result.child_program_dict:
        if evidence.get("child_id") or evidence.get("public_candidate") or evaluator.calls:
            raise IntegrityError("Worker replay lost a recorded candidate")
        return None, rng, iteration
    if evaluator.calls != 1:
        raise IntegrityError("Worker replay did not consume exactly one recorded evaluation")
    child = Program.from_dict(result.child_program_dict)
    operation = evidence["model_receipt"]["request_id"].removesuffix("/model")
    child.id = "child-" + digest({"operation": operation, "base": base.sha256, "source": child.code})[:24]
    child.timestamp = float(iteration)
    if child.id != evidence["child_id"] or child.code != evidence["proposed_source"]:
        raise IntegrityError("Reconstructed child identity differs from recorded candidate")
    return child.to_dict(), rng, iteration


def _new_database(population):
    from openevolve.database import ProgramDatabase
    db = ProgramDatabase(database_config())
    _restore(db, deepcopy(population))
    return db


def _sampling_evidence(population, child_id):
    """Eligibility and bounded existential sampler witnesses, never a rollout.

    Restore afresh for each draw because native inspiration sampling mutates
    feature statistics. A missing witness is unknown, not ineligibility.
    """
    db = _new_database(population)
    present = child_id in db.programs
    random_probability = 1 - db.config.exploration_ratio - db.config.exploitation_ratio
    parent_eligible = bool(present and random_probability > 0)
    witnesses = {"parent": None, "inspiration": None}
    if present:
        for seed in range(128):
            trial = _new_database(population)
            random.seed(seed)
            parent, refs = trial.sample(num_inspirations=2)
            draw = {"seed": seed, "parent_id": parent.id, "inspiration_ids": [p.id for p in refs]}
            if parent.id == child_id and witnesses["parent"] is None:
                witnesses["parent"] = draw
            if child_id in draw["inspiration_ids"] and witnesses["inspiration"] is None:
                witnesses["inspiration"] = draw
            if all(witnesses.values()):
                break
    return {"parent_eligible": parent_eligible if present else False,
        "parent_basis": "positive_probability_uniform_all_programs_branch",
        "random_parent_branch_probability": random_probability,
        "inspiration_eligible": True if witnesses["inspiration"] else (False if not present else None),
        "witnesses": witnesses, "witness_search_seed_range": [0, 127],
        "scope": "fixed_post_admission_scores;not_actual_future_context;future_rescoring_not_predicted"}


def _collections(population):
    if population is None:
        return None
    return {k: population[k] for k in ("archive", "islands", "island_feature_maps",
        "best_program_id", "island_best_programs", "diversity_reference_set")}


def _condition(prepared, evidence, plan, child_dict, rng, iteration, gated):
    from openevolve.database import Program
    db = _new_database(plan["population"])
    random.setstate(_tuples(rng))
    base = prepared.base
    public = evidence.get("public_candidate")
    gate = bool(public and cost_candidate_valid(public))
    submitted = child_dict is not None and (gate or not gated)
    admitted = None
    candidate = base
    if submitted:
        child = Program.from_dict(deepcopy(child_dict))
        identifier = db.add(child, iteration=iteration, target_island=0)
        admitted = identifier is not None and child.id in db.programs
        if admitted:
            best = db.get_best_program()
            artifacts = {**plan["input"]["artifacts"], child.id: _artifact(public)}
            state = decode_json(base.state_json)
            candidate = replace(base, version=replace_component(base.version, "policy", {"source": best.code}),
                state_json=json_text({**state, "population": _export(db), "rng": random.getstate(),
                    "public_artifacts": {k: v for k, v in artifacts.items() if k in db.programs},
                    "generation": iteration}))
    after = _export(db)
    source_before = base.version.component("policy")["source"]
    source_after = candidate.version.component("policy")["source"]
    base_population = decode_json(base.state_json)["population"]
    before_id = base_population["best_program_id"] if base_population else next(
        (key for key, program in plan["population"]["programs"].items() if program["code"] == source_before), None)
    committed_population = decode_json(candidate.state_json)["population"]
    active_after_id = db.get_best_program().id if admitted else (
        before_id)
    return candidate, {"condition": "public_complete_correct" if gated else "upstream",
        "public_gate_pass": gate, "native_database_called": submitted,
        "native_database_admitted": admitted, "candidate_committable": bool(admitted),
        "active_source_changed": source_before != source_after,
        "active_program_before_id": before_id,
        "active_program_after_id": active_after_id,
        "active_source_before_sha256": digest(source_before), "active_source_after_sha256": digest(source_after),
        "candidate_snapshot_sha256": candidate.sha256,
        "system_state_changed": candidate != base,
        "archive_only_system_update": candidate != base and source_before == source_after,
        "population_after_native": after,
        "inherited_population": committed_population,
        "native_population_changed": after != plan["population"],
        "native_collections_before": _collections(plan["population"]),
        "native_collections_after": _collections(after),
        "inherited_collections_before": _collections(base_population),
        "inherited_collections_after": _collections(committed_population),
        "sampling": _sampling_evidence(after, child_dict["id"] if admitted else None)}


def analyze_proposal(prepared_record, plan, proposal_record):
    """Strict core: any missing or conflicting checkpoint makes a row unusable."""
    prepared = PreparedUpdate.from_dict(prepared_record)
    evidence = decode_json(prepared.draft.native_evidence_json)
    if proposal_record != prepared_record["draft"]:
        raise IntegrityError("Prepared draft differs from durable proposal")
    if evidence.get("upstream_revision") != REVISION:
        raise IntegrityError("Historical upstream revision mismatch")
    if evidence.get("acceptance_semantics") != "native_archive_admission_plus_public_complete_correct_gate":
        raise IntegrityError("Only the frozen legacy screen semantics are supported")
    if plan["base_sha256"] != prepared.base.sha256 or prepared.draft.base_sha256 != prepared.base.sha256:
        raise IntegrityError("Plan or proposal belongs to a different base")
    if (plan["population"] != evidence["population_before"]
        or plan["input"]["programs"] != plan["population"]["programs"]
        or plan["input"]["islands"] != plan["population"]["islands"]
        or plan["input"]["feature_dimensions"] != ["complexity"]
        or plan["input"]["sampling_island"] != 0
        or plan["parent_id"] != evidence["parent_id"]
        or plan["inspiration_ids"] != evidence["inspiration_ids"]):
        raise IntegrityError("Native plan does not match recorded pre-admission inputs")
    outer_rng = random.getstate()
    try:
        child, rng, iteration = _replay_worker(prepared, evidence, plan)
        historical, gated = _condition(prepared, evidence, plan, child, rng, iteration, True)
        if (historical != prepared.draft.candidate
            or gated["candidate_committable"] != prepared.draft.native_valid
            or digest(decode_json(historical.state_json)["population"]) != evidence["proposed_population_sha256"]
            or (prepared.draft.native_valid and gated["active_program_after_id"] != evidence["active_program_id"])):
            raise IntegrityError("Original admission or complete active snapshot was not reproduced")
        counterfactual, upstream = _condition(prepared, evidence, plan, child, rng, iteration, False)
    finally:
        random.setstate(outer_rng)
    return {"status": "analyzable", "original_reproduced": True,
        "base_sha256": prepared.base.sha256, "public_plan_sha256": plan["public_plan_sha256"],
        "parent_id": evidence["parent_id"], "inspiration_ids": evidence["inspiration_ids"],
        "child_id": child["id"] if child else None,
        "candidate_source_sha256": digest(child["code"]) if child else None,
        "candidate_metrics": child["metrics"] if child else None,
        "public_evaluation": evidence.get("public_candidate"),
        "native_error": evidence["native_error"], "population_before": plan["population"],
        "pre_admission_rng": rng, "normalized_child": child,
        "current": gated, "upstream": upstream,
        "conditions_differ": historical != counterfactual,
        "contrast_scope": "single_step_conditional_on_actual_history"}


def _bind_public_records(prepared_record, plan, evidence, directory, load):
    """Bind every rescored archive member and child to its durable public request."""
    base = SystemSnapshot.from_dict(prepared_record["base"])
    operation = evidence["model_receipt"]["request_id"].removesuffix("/model")
    def evaluation(suffix, source):
        operation_id = operation + suffix
        root = directory / digest(operation_id)
        request, public = load(root / "request.json"), load(root / "evaluation.json")
        expected = replace(base, version=replace_component(base.version, "policy", {"source": source}))
        if (request["operation_id"] != operation_id or digest(request["plan"]) != plan["public_plan_sha256"]
            or SystemSnapshot.from_dict(request["candidate"]) != expected
            or public["version_sha256"] != expected.version.sha256):
            raise IntegrityError("Public evaluation is bound to a different source, plan or operation")
        return public
    for key, program in plan["population"]["programs"].items():
        public = evaluation("/archive/" + key, program["code"])
        if program["metrics"] != _fitness(public) or plan["input"]["artifacts"][key] != _artifact(public):
            raise IntegrityError("Archive score or artifact differs from recorded public evaluation")
    if evidence.get("public_candidate") is not None:
        source = evidence["proposed_source"]
        public = evaluation("/child/" + digest(source)[:20], source)
        if public != evidence["public_candidate"]:
            raise IntegrityError("Child public evaluation differs from native evidence")


def review(run):
    run = Path(run).resolve()
    lock = verify_upstream()
    references = {}
    def load(path):
        path = path.resolve()
        if not path.is_relative_to(run):
            raise IntegrityError("Input escaped the source run")
        value = read_record(path)
        references[str(path.relative_to(run))] = digest(value)
        return value
    summary = load(run / "summary.json")
    if summary.get("protocol") != "openevolve-suitability-v1" or summary.get("status") != "complete":
        raise IntegrityError("Requires a complete frozen suitability screen")
    verification = load(run / "verification.json")
    if verification.get("verified") is not True or verification["summary_sha256"] != digest(summary):
        raise IntegrityError("Source execution verification does not match summary")
    with sqlite3.connect((run / "model.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        model_rows = {row[0]: row for row in db.execute(
            "SELECT request_id,status,request_sha,request_json,response_sha,response_bytes,result_json FROM chat_requests")}
    missing = [{"request_id": row[0], "status": row[1]} for row in model_rows.values()
               if row[1] in {"unknown", "pending"}]
    if len(missing) != summary["usage"]["unknown_responses"]:
        raise IntegrityError("Unknown-response inventory differs from source summary")
    proposals, proposal_load_errors, proposal_paths = {}, [], []
    for path in sorted(run.glob("roots/**/system/proposals/*.json")):
        if path.name.endswith(".plan.json"):
            continue
        proposal_paths.append(path)
        try:
            value = load(path)
        except (IntegrityError, ValidationError, ValueError, FileNotFoundError) as exc:
            proposal_load_errors.append({"path": str(path.relative_to(run)),
                "error": f"{type(exc).__name__}: {exc}"})
            continue
        proposals.setdefault(digest(value), []).append((path, value))
    rows, recorded_counts = [], Counter()
    paths = sorted(set(run.glob("roots/*/search-*/prepared.json")) |
                   set(run.glob("roots/*/continuations/*/*/prepared-*.json")))
    # Model completion is the denominator, not whichever files survive a glob.
    # Refuse an incomplete or ambiguous inventory before producing any contrast.
    completed_ids = {key for key, row in model_rows.items() if row[1] == "complete"}
    def request_id(record):
        return decode_json(record["native_evidence_json"])["model_receipt"]["request_id"]
    proposal_ids = [request_id(value) for matches in proposals.values() for _, value in matches]
    prepared_ids = [request_id(load(path)["draft"]) for path in paths]
    if (len(completed_ids) != summary["usage"]["complete_responses"]
        or len(proposal_paths) != len(completed_ids)
        or len(proposal_ids) != len(set(proposal_ids)) or not set(proposal_ids) <= completed_ids
        or len(prepared_ids) != len(set(prepared_ids)) or set(prepared_ids) != completed_ids):
        raise IntegrityError("Complete response/prepared/proposal inventory mismatch; "
            f"completed={len(completed_ids)}, proposals={len(proposal_ids)}, prepared={len(prepared_ids)}; "
            f"missing prepared requests={sorted(completed_ids - set(prepared_ids))}")
    for path in paths:
        row = {"prepared": str(path.relative_to(run))}
        try:
            prepared = load(path)
            recorded_counts["prepared"] += 1
            if prepared["draft"]["native_valid"]:
                recorded_counts["admitted"] += 1
                typed = PreparedUpdate.from_dict(prepared)
                changed = (typed.base.version.component("policy")["source"] !=
                    typed.draft.candidate.version.component("policy")["source"])
                recorded_counts["active_source_changed" if changed else "archive_only"] += 1
            matches = proposals.get(digest(prepared["draft"]), [])
            if len(matches) != 1:
                raise IntegrityError("Missing or ambiguous durable proposal; "
                    f"unreadable proposal paths={[item['path'] for item in proposal_load_errors]}")
            proposal_path, proposal = matches[0]
            plan_path = proposal_path.with_suffix(".plan.json")
            row.update(proposal=str(proposal_path.relative_to(run)), plan=str(plan_path.relative_to(run)))
            plan = load(plan_path)
            evidence = decode_json(prepared["draft"]["native_evidence_json"])
            receipt = evidence["model_receipt"]
            model = model_rows[receipt["request_id"]]
            if (model[1] != "complete" or decode_json(model[6]) != receipt
                or hashlib.sha256(model[3]).hexdigest() != model[2] or model[2] != receipt["request_sha256"]
                or hashlib.sha256(model[5]).hexdigest() != model[4] or model[4] != receipt["response_sha256"]):
                raise IntegrityError("Recorded model receipt differs from durable completed response")
            references["model.sqlite#" + model[0]] = digest(receipt)
            public_directory = (path.parent / "public" if path.name == "prepared.json" else
                                path.parent / ("native-" + path.stem.removeprefix("prepared-")))
            _bind_public_records(prepared, plan, evidence, public_directory, load)
            row.update(analyze_proposal(prepared, plan, proposal))
        except (IntegrityError, ValidationError, KeyError, ValueError, FileNotFoundError) as exc:
            row.update(status="unanalyzable", reason=f"{type(exc).__name__}: {exc}", original_reproduced=False)
        rows.append(row)
    counts = Counter()
    for row in rows:
        counts[row["status"]] += 1
        if row["status"] != "analyzable":
            continue
        counts["original_reproduced"] += 1
        counts["same_source_rejections"] += row["native_error"] == "Rewrite is identical to the parent program"
        counts["conditions_differ"] += row["conditions_differ"]
        for arm in ("current", "upstream"):
            for key in ("candidate_committable", "active_source_changed", "archive_only_system_update"):
                counts[f"{arm}_{key}"] += row[arm][key]
    eligible = [row["prepared"] for row in rows if row.get("conditions_differ") and
        row["upstream"]["candidate_committable"] and row["upstream"]["sampling"]["parent_eligible"]]
    return {"analysis": "fixed_candidate_admission_counterfactual_v1", "source_run": str(run),
        "source_summary_sha256": digest(summary), "upstream_lock_sha256": digest(lock),
        "analysis_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "new_model_calls": 0, "new_program_executions": 0,
        "scope": "single_step_conditional_on_actual_history;never_a_re_evolved_history",
        "fixed": ["candidate_source", "public_scores_including_zero", "archive_rescoring",
                  "tool_refusals", "bounded_execution", "pre_admission_state"],
        "counts": dict(counts), "historical_recorded_counts": dict(recorded_counts), "prepared_records": len(rows),
        "inventory": {"complete_model_responses": len(completed_ids), "durable_proposal_files": len(proposal_paths),
            "readable_durable_proposals": len(proposal_ids), "prepared_records": len(prepared_ids),
            "request_ids_reconciled": not proposal_load_errors},
        "proposal_load_errors": proposal_load_errors,
        "unknown_model_responses": summary["usage"]["unknown_responses"],
        "missing_model_responses": missing,
        "unknown_response_interpretation": "missing;not_retried;excluded_from_complete_proposals",
        "rows": rows, "references": references,
        "continuation_decision": {"eligible_checkpoints": eligible,
            "decision": "eligible_for_bounded_development_case" if eligible else "do_not_run",
            "paid_continuation_started": False,
            "reason": "native_admission_and_positive_probability_parent_path" if eligible else "no_identified_native_use_path",
            "required_frozen_new_budget": {"conditions": 2, "repeats_per_condition": 3, "horizon": 3,
                "max_edit_calls_including_extras": 18, "max_total_tokens_including_extras": 200000,
                "unknown_responses": "missing_no_retry"},
            "causal_risk_reduction": None, "actual_descendant_context_use": None}}


def report(value):
    c = value["counts"]
    recorded = value["historical_recorded_counts"]
    lines = ["# 固定候选入库反事实", "",
        "逐提案、条件于真实历史的单步反事实；各行恢复自己的入库前状态，未串联为重新演化历史。",
        "新增模型/API调用0，新增程序执行0。固定源码、公开评分（不合格仍为0）、档案重评分、原工具拒绝和执行隔离。", "",
        f"完整提案{value['prepared_records']}份，可分析{c.get('analyzable', 0)}份，不可分析{c.get('unanalyzable', 0)}份；",
        f"原条件完整候选快照、入库和活动源码复现{c.get('original_reproduced', 0)}份。未知响应{value['unknown_model_responses']}份保持缺失，未补跑。", "",
        f"历史原始记录另计：收录{recorded.get('admitted', 0)}次，活动源码改变{recorded.get('active_source_changed', 0)}次，仅档案更新{recorded.get('archive_only', 0)}次；以下比较仅含已准确复现的可分析提案。", "",
        "| 条件 | 收录/可提交 | 活动源码改变 | 仅档案等继承状态更新 |",
        "| --- | ---: | ---: | ---: |",
        f"| 额外公开正确性门槛 | {c.get('current_candidate_committable', 0)} | {c.get('current_active_source_changed', 0)} | {c.get('current_archive_only_system_update', 0)} |",
        f"| 相同评分直接交原生库 | {c.get('upstream_candidate_committable', 0)} | {c.get('upstream_active_source_changed', 0)} | {c.get('upstream_archive_only_system_update', 0)} |", "",
        f"同源码worker拒绝{c.get('same_source_rejections', 0)}次保持原义；条件产生差异{c.get('conditions_differ', 0)}份。",
        "档案改变是系统状态改变；活动程序不变不等于系统没有变化。", ""]
    for row in value["rows"]:
        if row["status"] != "analyzable":
            lines += [f"- 不可分析 `{row['prepared']}`：{row['reason']}", ""]
        elif row["conditions_differ"]:
            up = row["upstream"]
            public = row["public_evaluation"]
            before, after = up["native_collections_before"], up["native_collections_after"]
            added = sorted(set(after["archive"]) - set(before["archive"]))
            lines += [f"## 差异案例：`{row['prepared']}`", "",
                f"候选 `{row['child_id']}` 的适应度保持 `{row['candidate_metrics']}`；原条件不调用原生入库，对照原生库收录={up['native_database_admitted']}。",
                f"原公开记录：正确{public['feedback']['correct_requests']}/{public['feedback']['total_requests']}，合法完成{public['legal_completed']}/{public['legal_total']}，实际风险={public['risk']}。公开不合格不能直接等同于实际违规。",
                f"活动源码改变={up['active_source_changed']}，档案/岛集合/精英与参考集合的前后完整标识保存在summary.json。",
                f"精英档案新增 `{added}`；MAP-Elites网格改变={before['island_feature_maps'] != after['island_feature_maps']}，最佳程序改变={before['best_program_id'] != after['best_program_id']}，多样性参考集合改变={before['diversity_reference_set'] != after['diversity_reference_set']}。",
                f"父节点抽样资格={up['sampling']['parent_eligible']}，参考抽样资格={up['sampling']['inspiration_eligible']}。",
                "资格来自固定入库后状态的原生抽样路径，并附本地抽样见证；它不表示实际进入下一代模型上下文，也不预测新任务重评分后的选择。", ""]
    decision = ("决定：值得启动有上限的零售开发案例续演；本轮未运行，运行前须冻结新预算。"
        if value["continuation_decision"]["eligible_checkpoints"] else "决定：不启动续演；没有识别到原生后续使用路径。")
    lines += ["## 续演决定", "", decision, "",
        "存在可识别路径时，可另行冻结开发案例预算后开展两条件×3重复×H3，最多18次编辑调用、200,000 token，额外调用计入，未知响应不补跑。",
        "本轮未启动付费续演。原工具拒绝必须保留；单条已查看历史只能支持案例机制分析，不能据此声称风险降低、平台独有贡献或撤层必有非零违规。", "",
        "方法：校验持久提案与prepared、只读模型日志回执，以及每个公开评价的源码、操作和任务计划绑定；恢复准确plan、固定公开评价；仅用已保存响应重放锁定worker解析和提示格式，以恢复其随机状态；",
        "逐项验证原条件完整候选快照，再从同一入库前状态分别调用原生ProgramDatabase。恢复失败或原结果不一致的行标为不可分析。", ""]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.output.exists() or args.output.resolve().is_relative_to(args.run.resolve()):
        raise ConflictError("Use a new output directory outside the immutable source run")
    value = review(args.run)
    save_record(args.output / "summary.json", value)
    (args.output / "report.md").write_text(report(value))
    print(json_text({"output": str(args.output), "counts": value["counts"],
        "decision": value["continuation_decision"]["decision"], "new_model_calls": 0}))
    return 0
