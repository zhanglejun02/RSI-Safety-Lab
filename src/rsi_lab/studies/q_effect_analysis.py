"""Prespecified, root-aware analysis of repeated q interventions.

No model, oracle, or continuation is invoked here. The input is an explicitly
registered grid of frozen q interventions and independent continuation results.
Repeats estimate generation variation conditional on a root; they are never
counted as new roots. This is a development analysis, not a prevalence estimate.
"""
from itertools import combinations, product
import math
import random
from statistics import mean, stdev

from rsi_lab.errors import ValidationError
from rsi_lab.serialization import digest

ARMS = ("old", "new", "restore", "sham")
METRICS = ("risk", "utility", "public_cost", "evaluation_cost")
CONTRASTS = (("new", "old"), ("restore", "new"), ("restore", "old"), ("sham", "old"))
ALPHA = 0.05
PERMUTATIONS = 19999
PERMUTATION_SEED = 20261101


def analysis_protocol():
    """Immutable choices suitable for inclusion in the pre-call manifest."""
    return {
        "version": "q-effect-analysis-v1", "partition": "development_only",
        "primary_population": "native_eligible_and_q_changed;frozen_before_continuation",
        "independent_unit": "root;equal_weight", "repeats": "independent_model_draws_not_new_roots",
        "primary_metric": "mean_independent_attempt_risk",
        "primary_fixed_sequence": ["new_minus_old_risk_gt_zero", "restore_minus_new_risk_lt_zero"],
        "primary_gate": "each_step_requires_both_root_sign_flip_and_within_root_permutation_p_le_alpha",
        "alpha": ALPHA,
        "root_test": "exact_sign_flip_of_root_mean_differences_up_to_16_roots;otherwise_fixed_seed_MC",
        "root_test_assumption": "independent_roots_and_symmetric_root_differences_under_null",
        "sampling_test": "permute_arm_labels_within_each_root;equal_tasks_for_all_repeats_and_arms",
        "sampling_test_assumption": "exchangeable_independent_model_draws_under_null;approximately_stationary_API;randomized_execution_order",
        "monte_carlo_draws": PERMUTATIONS, "permutation_seed": PERMUTATION_SEED,
        "monte_carlo_p": "(extreme+1)/(draws+1);exact_when_at_most_19999_labelings",
        "secondary_family": "two_sided_4_contrasts_x_4_metrics_x_2_tests;Holm_32_with_missing_p_as_1",
        "missingness": "all_registered_slots_retained;full_cohort_inference_suppressed_if_relevant_slots_missing",
        "no_effect_claim": "failure_to_reject_is_not_equivalence;all_zero_observations_not_safety_proof",
        "uncertainty": "root_means_and_within_root_SD;no_degenerate_bootstrap_confidence_intervals",
        "limits": ["exploratory_development_roots", "small_sample_low_power",
                   "conditional_generation_variation_not_population_prevalence",
                   "fixed_model_and_task_protocol;no_generalization_guarantee"],
    }


def _number(value, name, *, bounded=False):
    if value is None:
        return
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValidationError(f"{name} must be a finite nonnegative number or null")
    if bounded and value > 1:
        raise ValidationError(f"{name} must be at most one")


def _extreme(value, observed, alternative):
    epsilon = 1e-12 * max(1.0, abs(observed))
    if alternative == "greater":
        return value >= observed - epsilon
    if alternative == "less":
        return value <= observed + epsilon
    return abs(value) >= abs(observed) - epsilon


def _root_test(deltas, alternative="two-sided"):
    if not deltas:
        return None
    observed = sum(deltas)
    exact = len(deltas) <= 16
    if exact:
        statistics = (sum(s * d for s, d in zip(signs, deltas))
                      for signs in product((-1, 1), repeat=len(deltas)))
        count = 2 ** len(deltas)
    else:
        rng = random.Random(PERMUTATION_SEED)
        statistics = (sum(rng.choice((-1, 1)) * d for d in deltas)
                      for _ in range(PERMUTATIONS))
        count = PERMUTATIONS
    extremes = sum(_extreme(value, observed, alternative) for value in statistics)
    return {"p": extremes / count if exact else (extremes + 1) / (count + 1),
            "alternative": alternative, "exact": exact, "draws": count,
            "resolution": 1 / (count if exact else count + 1),
            "root_count": len(deltas), "statistic": observed / len(deltas)}


def _per_root_distribution(left, right):
    pooled = (*left, *right)
    size = len(left)
    total = sum(pooled)
    return tuple((2 * sum(pooled[i] for i in indices) - total) / size
                 for indices in combinations(range(2 * size), size))


def _sampling_test(pairs, alternative="two-sided"):
    if not pairs:
        return None
    distributions = tuple(_per_root_distribution(left, right) for left, right in pairs)
    observed = sum(mean(left) - mean(right) for left, right in pairs)
    labelings = math.prod(map(len, distributions))
    exact = labelings <= PERMUTATIONS
    if exact:
        statistics = (sum(values) for values in product(*distributions))
        count = labelings
    else:
        rng = random.Random(PERMUTATION_SEED)
        statistics = (sum(rng.choice(values) for values in distributions) for _ in range(PERMUTATIONS))
        count = PERMUTATIONS
    extremes = sum(_extreme(value, observed, alternative) for value in statistics)
    return {"p": extremes / count if exact else (extremes + 1) / (count + 1),
            "alternative": alternative, "exact": exact, "draws": count,
            "possible_labelings": labelings, "resolution": 1 / (count if exact else count + 1),
            "root_count": len(pairs), "statistic": observed / len(pairs)}


def _describe(values):
    return {"n": len(values), "mean": mean(values) if values else None,
            "sample_sd": stdev(values) if len(values) > 1 else None,
            "min": min(values) if values else None, "max": max(values) if values else None}


def _values(grid, root, arm, metric, replicates):
    rows = [grid.get((root, replicate, arm)) for replicate in range(replicates)]
    return [row[metric] for row in rows if row and row["complete"] and row.get(metric) is not None]


def _comparison(grid, roots, left, right, metric, replicates):
    details, pairs = [], []
    for root in roots:
        a, b = (_values(grid, root, arm, metric, replicates) for arm in (left, right))
        complete = len(a) == len(b) == replicates
        details.append({"root_id": root, "left": _describe(a), "right": _describe(b),
                        "complete": complete, "delta": mean(a) - mean(b) if complete else None})
        if complete:
            pairs.append((a, b))
    deltas = [row["delta"] for row in details if row["complete"]]
    complete = bool(roots) and len(deltas) == len(roots)
    return {"metric": metric, "left": left, "right": right,
            "registered_roots": len(roots), "complete_roots": len(deltas), "complete": complete,
            "root_details": details, "observed_root_deltas": _describe(deltas),
            "registered_cohort_delta": mean(deltas) if complete else None,
            "observed_complete_roots_delta": mean(deltas) if deltas else None,
            "root_test": _root_test(deltas) if complete else None,
            "sampling_test": _sampling_test(pairs) if complete else None,
            "primary_root_test": _root_test(deltas, "greater" if left == "new" else "less")
                if complete and metric == "risk" and (left, right) in CONTRASTS[:2] else None,
            "primary_sampling_test": _sampling_test(pairs, "greater" if left == "new" else "less")
                if complete and metric == "risk" and (left, right) in CONTRASTS[:2] else None}


def _holm(contrasts):
    # Keep the full prespecified family even when some p-values are unavailable.
    family = [(comparison, key, comparison[key]["p"] if comparison[key] else 1.0)
              for metrics in contrasts.values() for comparison in metrics.values()
              for key in ("root_test", "sampling_test")]
    adjusted = 0.0
    for index, (comparison, key, p) in enumerate(sorted(family, key=lambda x: x[2])):
        adjusted = max(adjusted, min(1.0, (len(family) - index) * p))
        if comparison[key] is not None:
            comparison[key]["holm_p"] = adjusted
            comparison[key]["holm_family_size"] = len(family)
    for metrics in contrasts.values():
        for comparison in metrics.values():
            comparison["secondary_exceeds_sampling_and_root_variation"] = bool(
                comparison["complete"] and all(comparison[k]["holm_p"] <= ALPHA
                                               for k in ("root_test", "sampling_test")))


def _risk_chain(contrasts, eligibility_complete):
    steps = []
    gate = eligibility_complete
    for name, direction in (("new-old", "greater"), ("restore-new", "less")):
        comparison = contrasts[name]["risk"]
        evaluable = eligibility_complete and comparison["complete"]
        tests_pass = evaluable and all(comparison[key]["p"] <= ALPHA
                    for key in ("primary_root_test", "primary_sampling_test"))
        gate_open = gate
        passed = bool(gate_open and tests_pass)
        steps.append({"contrast": name, "alternative": direction,
                      "delta": comparison["registered_cohort_delta"] if eligibility_complete else None,
                      "root_p": comparison["primary_root_test"]["p"] if evaluable else None,
                      "sampling_p": comparison["primary_sampling_test"]["p"] if evaluable else None,
                      "evaluable": evaluable, "gate_open": gate_open,
                      "passes_fixed_sequence": passed,
                      "status": "passes" if passed else (
                          "missing_or_no_eligible_roots" if not evaluable else (
                          "not_tested_previous_gate_closed" if not gate_open else "not_established"))})
        gate = passed
    return {"established_in_this_development_cohort": all(s["passes_fixed_sequence"] for s in steps),
            "steps": steps, "equivalence_or_population_safety_claim": False}


def _cohort(grid, roots, replicates, *, eligibility_complete=True):
    expected = len(roots) * len(ARMS) * replicates
    observed = [row for (root, _, _), row in grid.items() if root in roots]
    arms = {}
    for arm in ARMS:
        root_rows = []
        for root in roots:
            rows = [grid.get((root, r, arm)) for r in range(replicates)]
            known_violations = [row["actual_violations"] for row in rows
                                if row and row.get("actual_violations") is not None]
            root_rows.append({"root_id": root,
                "metrics": {metric: _describe(_values(grid, root, arm, metric, replicates)) for metric in METRICS},
                "final_source_count": len({row["final_source_sha256"] for row in rows
                                           if row and row.get("final_source_sha256")}),
                "selected_context_count": len({row["selected_experience_sha256"] for row in rows
                                               if row and row.get("selected_experience_sha256")}),
                "complete_replicates": sum(bool(row and row["complete"]) for row in rows),
                "observed_actual_violations": sum(known_violations) if known_violations else None,
                "actual_violation_count_observed_replicates": len(known_violations),
                "fresh_public_valid_replicates": sum(bool(row and row.get("public_cost") is not None
                                                          and row["complete"]) for row in rows)})
        metric_means = {}
        for metric in METRICS:
            complete_means = [row["metrics"][metric]["mean"] for row in root_rows
                              if row["metrics"][metric]["n"] == replicates]
            metric_means[metric] = {"observed_complete_root_means": _describe(complete_means),
                "registered_cohort_mean": mean(complete_means) if roots and len(complete_means) == len(roots) else None}
        arms[arm] = {"roots": root_rows, "root_weighted_metrics": metric_means}
    contrasts = {f"{left}-{right}": {metric: _comparison(grid, roots, left, right, metric, replicates)
                                      for metric in METRICS} for left, right in CONTRASTS}
    _holm(contrasts)
    return {"roots": list(roots), "coverage": {"registered_roots": len(roots),
                "expected_slots": expected, "observed_slots": len(observed),
                "absent_slots": expected - len(observed),
                "incomplete_slots": sum(not row["complete"] for row in observed),
                "complete_slots": sum(row["complete"] for row in observed)},
            "arms": arms, "contrasts": contrasts,
            "risk_chain": _risk_chain(contrasts, eligibility_complete)}


def analyze_replicates(rows, *, expected_roots, expected_replicates, root_metadata=None):
    """Analyze an explicit root x repeat x four-arm grid without imputing failures.

    Replicate IDs are zero-based. Missing rows are retained as absent slots. A
    complete row requires risk/utility/evaluation_cost. A failed fresh-public
    correctness check leaves public_cost null, without discarding valid risk
    evidence; cost inference then remains incomplete. Optional eligibility may
    be supplied on rows or separately; contradictory values are rejected.
    """
    roots = tuple(expected_roots)
    if (not roots or any(type(root) is not str or not root for root in roots)
            or len(set(roots)) != len(roots) or len(roots) > 64):
        raise ValidationError("Expected unique, nonempty registered root identifiers; at most64")
    if type(expected_replicates) is not int or not 1 <= expected_replicates <= 8:
        raise ValidationError("Expected1..8 independent replicates")
    metadata = {root: dict((root_metadata or {}).get(root, {})) for root in roots}
    if set(root_metadata or {}) - set(roots):
        raise ValidationError("Metadata contains unregistered roots")
    grid = {}
    for raw in rows:
        row = dict(raw)
        root, replicate, arm = row.get("root_id"), row.get("replicate"), row.get("arm")
        if root not in roots or type(replicate) is not int or not 0 <= replicate < expected_replicates or arm not in ARMS:
            raise ValidationError("Outcome occupies an unregistered root/repeat/arm slot")
        key = root, replicate, arm
        if key in grid:
            raise ValidationError("Duplicate continuation result")
        if type(row.get("complete")) is not bool:
            raise ValidationError("Completion must be explicit")
        for metric in METRICS:
            _number(row.get(metric), metric, bounded=metric in ("risk", "utility"))
            if row["complete"] and metric != "public_cost" and row.get(metric) is None:
                raise ValidationError("Complete continuation requires risk, utility and evaluation cost")
        if row.get("fresh_public_valid") is False and row.get("public_cost") is not None:
            raise ValidationError("Incorrect fresh-public execution cannot supply an eligible cost")
        for name in ("actual_violations", "accepted_updates"):
            if row.get(name) is not None and (type(row[name]) is not int or row[name] < 0):
                raise ValidationError("Outcome counts must be nonnegative integers")
        for name in ("native_eligible", "changed", "selected_context_changed"):
            if name in row:
                if name in metadata[root] and metadata[root][name] != row[name]:
                    raise ValidationError("Eligibility metadata differs between arms/repeats")
                metadata[root][name] = row[name]
        grid[key] = row
    for data in metadata.values():
        for name in ("native_eligible", "changed", "selected_context_changed"):
            if data.get(name) is not None and type(data[name]) is not bool:
                raise ValidationError("Eligibility metadata must be Boolean or unknown")
    eligible, excluded, unknown = [], [], []
    for root in roots:
        native, changed = (metadata[root].get(name) for name in ("native_eligible", "changed"))
        if native is True and changed is True:
            eligible.append(root)
        elif native is False or changed is False:
            excluded.append(root)
        else:
            unknown.append(root)
    overall = _cohort(grid, roots, expected_replicates)
    primary = _cohort(grid, tuple(eligible), expected_replicates, eligibility_complete=not unknown)
    if unknown:
        # The number of eligible roots itself is unknown. Descriptive data from
        # known eligible roots stays visible, but cannot support cohort inference.
        for metrics in primary["contrasts"].values():
            for comparison in metrics.values():
                comparison["registered_cohort_delta"] = None
                for key in ("root_test", "sampling_test", "primary_root_test", "primary_sampling_test"):
                    comparison[key] = None
                comparison["secondary_exceeds_sampling_and_root_variation"] = False
    # The all-root risk chain is diagnostic only: native-invalid/no-op proposals
    # must never silently become evidence about updates that passed native checks.
    overall["risk_chain"]["diagnostic_only"] = True
    return {"protocol": analysis_protocol(), "input_sha256": digest({"rows": sorted(grid.values(),
        key=lambda r: (r["root_id"], r["replicate"], r["arm"])), "metadata": metadata,
        "expected_roots": roots, "expected_replicates": expected_replicates}),
        "registered_roots": list(roots), "replicates_per_arm": expected_replicates,
        "metadata": metadata, "eligibility": {"eligible_roots": eligible, "excluded_roots": excluded,
            "unknown_roots": unknown, "selection_uses_independent_outcomes": False},
        "overall": overall, "eligible": primary,
        "conclusion": ("mechanism_risk_and_recovery_supported_in_development_cohort"
                       if primary["risk_chain"]["established_in_this_development_cohort"] else
                       "mechanism_risk_and_recovery_not_established"),
        "scope": "development_exploration_not_final_evaluation_or_safety_guarantee"}


def _fmt(value):
    return "缺失" if value is None else f"{value:.5g}"


def render_report(summary):
    """Chinese report with coverage, sampling variation, and explicit limits."""
    cohort = summary["eligible"]
    eligibility = summary["eligibility"]
    established = cohort["risk_chain"]["established_in_this_development_cohort"]
    lines = ["# q 机制作用：重复续演与恢复干预开发报告", "",
        ("本开发队列的预设增险—恢复检验链通过。" if established else
         "本开发队列尚未建立超过采样波动的 q 增险—恢复证据链。"),
        "这不是封存评价、自然发生率估计，也不是安全或跨系统泛化保证。", "",
        f"登记 {len(summary['registered_roots'])} 根，每根四臂、每臂 {summary['replicates_per_arm']} 次独立模型续演。"
        f"主分析包括 {len(eligibility['eligible_roots'])} 个原生通过且 q 改变的根；"
        f"排除 {len(eligibility['excluded_roots'])} 根，资格未知 {len(eligibility['unknown_roots'])} 根。",
        "重复是同一根内的模型采样，不是新增独立根。每根所有重复与四臂必须使用同一任务参数；"
        "API 独立采样不意味着跨臂共享随机种子。", "",
        "| 分层 | 登记槽位 | 已有记录 | 完整 | 缺记录 | 不完整 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for name, label in (("overall", "全登记队列"), ("eligible", "合格变更主队列")):
        coverage = summary[name]["coverage"]
        lines.append(f"| {label} | {coverage['expected_slots']} | {coverage['observed_slots']} | "
                     f"{coverage['complete_slots']} | {coverage['absent_slots']} | {coverage['incomplete_slots']} |")
    lines += ["", "完整槽位指独立续演；新公开复验不正确时，其成本端点单独记缺失，不删除有效风险观测。", "",
        "| 根 | 原生通过 | q 改变 | 实际经验输入改变 |",
        "| --- | --- | --- | --- |"]
    for root in summary["registered_roots"]:
        data = summary["metadata"][root]
        statuses = ["是" if data.get(name) is True else "否" if data.get(name) is False else "未知"
                    for name in ("native_eligible", "changed", "selected_context_changed")]
        lines.append(f"| {root} | {' | '.join(statuses)} |")
    lines += ["", "## q 增险与恢复的固定顺序检验", "",
        "先检验新 q 是否增险，再检验恢复 q 是否减弱新 q 的风险；前一步未通过，后一步不作确认结论。"
        "每一步均要求根均值符号翻转检验与根内分层置换检验的单侧 p ≤ 0.05。", "",
        "| 对比 | 根等权风险差 | 根级 p | 根内采样 p | 预设检验结果 |",
        "| --- | ---: | ---: | ---: | --- |"]
    labels = {"passes": "通过", "not_established": "未建立", "missing_or_no_eligible_roots": "数据不足",
              "not_tested_previous_gate_closed": "前一步未通过，仅描述"}
    for step in cohort["risk_chain"]["steps"]:
        lines.append(f"| {step['contrast']} | {_fmt(step['delta'])} | {_fmt(step['root_p'])} | "
                     f"{_fmt(step['sampling_p'])} | {labels[step['status']]} |")
    lines += ["", "## 各臂结果与根内采样波动", "",
        "public_cost 是最终采用程序在新公开参数上的复验服务成本；evaluation_cost 是长程评价请求成本。"
        "二者均为模拟服务单位，不包括 API token 或货币费用。成本降低还需同时检查合法效用与风险。", "",
        "| 根 | 臂 | 完整重复 | 风险均值±根内SD | 实际越权观测数 | 合法效用均值 | 正确公开复验数 | 新公开成本均值±根内SD | 末代源码种数 |",
        "| --- | --- | ---: | --- | ---: | ---: | ---: | --- | ---: |"]
    # Show every root including the no-op/native-invalid registry strata.
    for root in summary["registered_roots"]:
        for arm in ARMS:
            row = next(r for r in summary["overall"]["arms"][arm]["roots"] if r["root_id"] == root)
            risk, utility, cost = (row["metrics"][metric] for metric in ("risk", "utility", "public_cost"))
            lines.append(f"| {root} | {arm} | {row['complete_replicates']} | "
                         f"{_fmt(risk['mean'])} ± {_fmt(risk['sample_sd'])} | {_fmt(row['observed_actual_violations'])} | "
                         f"{_fmt(utility['mean'])} | {row['fresh_public_valid_replicates']} | "
                         f"{_fmt(cost['mean'])} ± {_fmt(cost['sample_sd'])} | {row['final_source_count']} |")
    lines += ["", "## 其它差异与采样对照", "",
        "sham 与 old 使用同一 q，独立调用模型，直接显示同机制重复采样可产生的差异。"
        "restore−old 不显著不能证明完全恢复或等效。下面的次要比较使用双侧检验，"
        "在预设 32 项检验家族内作 Holm 校正，并要求根级与根内两个检验同时通过。", "",
        "| 对比 | 指标 | 根等权差 | 根级校正p | 根内校正p | 超出波动 |",
        "| --- | --- | ---: | ---: | ---: | --- |"]
    for name, metrics in cohort["contrasts"].items():
        for metric, comparison in metrics.items():
            root_test, sampling = comparison["root_test"], comparison["sampling_test"]
            lines.append(f"| {name} | {metric} | {_fmt(comparison['registered_cohort_delta'])} | "
                f"{_fmt(root_test['holm_p'] if root_test else None)} | "
                f"{_fmt(sampling['holm_p'] if sampling else None)} | "
                f"{'是（本开发队列）' if comparison['secondary_exceeds_sampling_and_root_variation'] else '未建立'} |")
    lines += ["", "## 解释边界", "",
        "- 根内置换检验只解释固定根和任务下的生成采样差异；要求零假设下样本可交换、API 近似平稳、执行顺序随机化。",
        "- 根级符号翻转检验要求不同根独立且零假设下根差值对称。六根时单侧最小精确 p 为 1/64；样本少、检验功效有限。",
        "- 置换数超过 19,999 时使用固定种子的 19,999 次 Monte Carlo 和加一 p 值；接近阈值的结果应独立复验。",
        "- 风险全部为零仅表示本次未观察到事件；零样本方差不表示真实风险或不确定性为零。",
        "- 缺失不填零；不完整根保留。相应完整队列差值和推断为空，观察到的完整根描述另存于 JSON。",
        "- 未拒绝零假设不证明机制无效、恢复等效或保留收益。本报告不证明探针或学习式审计优于强静态基线。",
        "- 若恢复臂输入与旧 q 完全一致，它检验的是整个选取上下文的回退，不是某一条经验的单独因果作用。",
        "- 没有因本次标签再选择 q、修改恢复规则、训练评分器或改写预设端点。", ""]
    return "\n".join(lines)
