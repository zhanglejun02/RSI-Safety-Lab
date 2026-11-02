"""Zero-API public test-minimization feasibility check, not a safety algorithm.

No platform changes or independent risk labels. Search is exact on the frozen
public outcome matrix; compare equally optimized plans with boundary tests kept.
Historical public failure/repair episodes are mandatory regression constraints,
not held-out risks or extra independent roots. Source programs use the existing
bounded interpreter, never Python exec.
"""
import argparse
from collections import Counter
from fractions import Fraction
import hashlib
from pathlib import Path

from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.errors import ConflictError, IntegrityError, ValidationError
from rsi_lab.frozen import implementation_fingerprint
from rsi_lab.serialization import digest
from rsi_lab.studies.offline_v import import_candidates, make_protocol
from rsi_lab.subjects.optimization_memory import OptimizationEpisode
from rsi_lab.tasks import cost_authorization as task

REVISION = "public-v-objective-preflight-v1"
CORE = (0,)
BOUNDARY = (0, 18, 24)  # Development-defined ordinary / cross / revoke slots.
OLD = (*range(7), 18)
MAX_TESTS = 8


def load_public_inputs(source_run, history_record):
    imported = import_candidates(source_run)  # Does not read source risk summaries.
    candidates = [{"id": c["candidate_id"], "group": c["history_group"],
        "source": c["source"], "source_sha256": c["source_sha256"],
        "native_eligible": c["original_native_eligible"], "kind": "historical_candidate",
        "provenance_sha256": c["proposal_sha256"]} for c in imported["candidates"]]
    history = read_record(history_record)
    for data in history["episodes"]:
        episode = OptimizationEpisode.from_dict(data)
        for role in ("before", "candidate", "repair"):
            value = getattr(episode, role)
            if value is None:
                continue
            candidates.append({"id": episode.episode_id + "/" + role,
                "group": "known-public-history", "source": value.source,
                "source_sha256": digest(value.source), "native_eligible": None,
                "kind": "known_public_regression", "provenance_sha256": digest(value)})
    return {"source_manifest_sha256": imported["source_manifest_sha256"],
            "history_sha256": digest(history), "candidates": candidates}


def deduplicate(rows):
    unique = {}
    for row in rows:
        key = (row["group"], row["source_sha256"])
        if key in unique and row["tests"] != unique[key]["tests"]:
            raise IntegrityError("Identical public program/parameters produced different evidence")
        unique.setdefault(key, row)
    return list(unique.values())


def _reference(row):
    return all(t["pass"] for t in row["tests"]) if all(t["complete"] for t in row["tests"]) else None


def _costs(rows):
    """Equal history weight, deduplicated programs within history; exact rationals."""
    groups = Counter(r["group"] for r in rows)
    if not rows:
        raise ValidationError("No public optimization population")
    return [sum((Fraction(r["tests"][t]["cost"], len(groups) * groups[r["group"]])
                 for r in rows), Fraction()) for t in range(len(rows[0]["tests"]))]


def minimize(objective_rows, constraint_rows, required=CORE, max_tests=MAX_TESTS):
    """Exact finite weighted set cover; exhaustive DP, not a new audit method."""
    if any(_reference(r) is None for r in objective_rows + constraint_rows):
        return None  # Missing reference is not a free pass or zero error.
    costs = _costs(objective_rows)
    n = len(costs)
    required = tuple(sorted(required))
    if len(set(required)) != len(required) or not required or not set(required) <= set(range(n)) or len(required) > max_tests:
        raise ValidationError("Invalid required public tests")
    negative = [r for r in constraint_rows if not _reference(r)]
    masks = [sum(1 << i for i, row in enumerate(negative) if not row["tests"][t]["pass"]) for t in range(n)]
    target = (1 << len(negative)) - 1
    covered = 0
    for t in required:
        covered |= masks[t]
    states = {(covered, len(required)): (sum((costs[t] for t in required), Fraction()), required)}
    for t in range(n):
        if t in required:
            continue
        for (mask, size), (cost, plan) in tuple(states.items()):
            if size == max_tests or mask | masks[t] == mask:
                continue
            key = (mask | masks[t], size + 1)
            value = (cost + costs[t], tuple(sorted((*plan, t))))
            if key not in states or value < states[key]:
                states[key] = value
    feasible = [(cost, size, plan) for (mask, size), (cost, plan) in states.items() if mask == target]
    if not feasible:
        return None
    cost, _, plan = min(feasible)
    # No arbitrary update on exact ties if the actual old plan is feasible.
    if len(OLD) <= max_tests and set(required) <= set(OLD) and set(OLD) <= set(range(n)):
        old_mask = 0
        for t in OLD:
            old_mask |= masks[t]
        if old_mask == target and sum((costs[t] for t in OLD), Fraction()) == cost:
            plan = OLD
    return list(plan)


def metrics(plan, rows):
    if plan is None or not rows or any(_reference(r) is None for r in rows):
        return None
    expected = [_reference(r) for r in rows]
    predicted = [all(r["tests"][t]["pass"] for t in plan) for r in rows]
    return {"programs": len(rows), "reference_pass": sum(expected), "reference_fail": len(rows) - sum(expected),
        "agreement": sum(a == b for a, b in zip(expected, predicted)) / len(rows),
        "false_accept": sum(not a and b for a, b in zip(expected, predicted)),
        "false_reject": sum(a and not b for a, b in zip(expected, predicted)),
        "mean_cost_equal_history": float(sum((_costs(rows)[t] for t in plan), Fraction())),
        "tests": len(plan), "requests": len(plan) * 3}


def analyze(matrix):
    rows = deduplicate([r for r in matrix if r["kind"] == "historical_candidate"])
    guard = deduplicate([r for r in matrix if r["kind"] == "known_public_regression"])
    if any(_reference(r) is None for r in rows + guard):
        return {"decision": "stop_missing_public_evidence", "model_calls": 0}
    roots = sorted({r["group"] for r in rows})
    folds = []
    for held_out in [None, *roots]:
        train = [r for r in rows if r["group"] != held_out]
        seen = {r["source_sha256"] for r in train}
        holdout = [r for r in rows if r["group"] == held_out and r["source_sha256"] not in seen]
        guards = [r for r in guard if r["source_sha256"] not in {r["source_sha256"] for r in holdout}]
        # Known public regressions are always retained; never hide a known lesson.
        if len(guards) != len(guard):
            holdout = [r for r in holdout if r["source_sha256"] not in {g["source_sha256"] for g in guard}]
            guards = guard
        optimized = minimize(train, train + guards)
        pinned = minimize(train, train + guards, BOUNDARY)
        plans = {"old_eight": list(OLD), "full_public_reference": list(range(30)),
                 "public_optimized": optimized, "pinned_boundary_optimized": pinned}
        table = {name: {"plan": plan, "train": metrics(plan, train),
                       "heldout": metrics(plan, holdout), "known_regression": metrics(plan, guards)}
                 for name, plan in plans.items()}
        old, opt, safe = (table[k]["train"] for k in ("old_eight", "public_optimized", "pinned_boundary_optimized"))
        saving = 1 - opt["mean_cost_equal_history"] / old["mean_cost_equal_history"] if opt and old["mean_cost_equal_history"] else None
        premium = safe["mean_cost_equal_history"] / opt["mean_cost_equal_history"] - 1 if opt and safe and opt["mean_cost_equal_history"] else None
        folds.append({"held_out_history": held_out, "table": table, "cost_saving_vs_old": saving,
            "fixed_boundary_cost_premium": premium,
            "fit_reference_has_both_classes": {_reference(r) for r in train + guards} == {False, True},
            "heldout_programs_removed_as_source_overlap": sum(r["group"] == held_out for r in rows) - len(holdout)})
    headroom = all(f["fit_reference_has_both_classes"] and f["cost_saving_vs_old"] is not None and f["cost_saving_vs_old"] >= .10
        and (f["held_out_history"] is None or f["table"]["public_optimized"]["heldout"] is not None
             and f["table"]["public_optimized"]["heldout"]["agreement"] == 1) for f in folds)
    cheap_guard = all(f["fixed_boundary_cost_premium"] is not None and f["fixed_boundary_cost_premium"] <= .05
        and (f["held_out_history"] is None or f["table"]["pinned_boundary_optimized"]["heldout"] is not None
             and f["table"]["pinned_boundary_optimized"]["heldout"]["agreement"] == 1) for f in folds)
    return {"model_calls": 0, "raw_historical_proposals": sum(r["kind"] == "historical_candidate" for r in matrix),
        "unique_history_programs": len(rows), "known_public_regression_programs": len(guard),
        "source_histories": roots, "public_headroom_gate": headroom, "cheap_fixed_boundary_gate": cheap_guard,
        "decision": "use_simple_boundary_baseline_no_paid_method_study" if headroom and cheap_guard else
                    "public_objective_feasible_protocol_only" if headroom else "stop_no_validated_public_headroom",
        "folds": folds, "independent_safety_labels_used": False,
        "limitations": ["development_feasibility_not_natural_v_risk_evidence", "full_public_reference_is_not_a_safety_oracle",
            "historical_root_folds_are_not_new_confirmation_data", "regressions_are_curated_known_public_history",
            "no_generated_descendants_no_auditor_comparison", "additive_simulated_service_cost_no_early_stop"]}


def render(summary):
    if "folds" not in summary:
        return "# v 公开优化目标检查\n\n公共证据缺失，停止。\n"
    lines = ["# v 公开优化目标检查", "", f"决策：`{summary['decision']}`。模型调用0；没有运行独立风险任务。",
        "", f"{summary['raw_historical_proposals']}个历史提案去重为{summary['unique_history_programs']}个同根程序；"
        f"{summary['known_public_regression_programs']}个已知公开历史程序作为强制回归约束。它们不是额外独立根。",
        "", "| 留出根 | 方案 | 测试序号 | 训练平均成本 | 训练一致率 | 留出公开一致率 | 已知回归一致率 |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: |"]
    for fold in summary["folds"]:
        for name, row in fold["table"].items():
            def value(part, key):
                return row[part][key] if row[part] else None
            lines.append(f"| {fold['held_out_history'] or '全开发集'} | {name} | {row['plan']} | "
                f"{value('train','mean_cost_equal_history')} | {value('train','agreement')} | "
                f"{value('heldout','agreement')} | {value('known_regression','agreement')} |")
    lines += ["", "一致率对照完整30题公开测试，分别计算验证判别，不由原生成本门槛遮蔽错误。",
        "成本按历史等权、同根源码去重计算；全参考矩阵的收集成本单列，不能当免费监督。",
        "核心功能测试0固定；v最多8题，可减少实际题数；固定安全基线强制保留0/18/24并使用同一优化器补充。",
        "各v改变的是测试选择，源码、参数和测试实现固定；没有奖励接受数量。",
        "跨授权/撤销测试来自既有开发知识。保留已公开发现的失败历史，不能为了使优化删边界而从档案中拿走它。",
        "", "10%相对旧v省成本、固定边界≤5%额外成本均为事前探索门槛，不是统计显著性或安全阈值。",
        "这一结果只说明公开目标的可行性及简单基线的成本；不能证明自然更新增险或后代探针有效。", ""]
    return "\n".join(lines)


def run(source_run, history_record, output):
    output = Path(output)
    if output.exists():
        raise ConflictError("Use a new output directory; this preflight does not overwrite past results")
    imported = load_public_inputs(source_run, history_record)
    pools = {c["group"]: make_protocol(c["group"])["public_pool"] for c in imported["candidates"]}
    manifest = {"revision": REVISION, "implementation_sha256": implementation_fingerprint(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "imported": imported,
        "protocol_document_sha256": hashlib.sha256((Path(__file__).resolve().parents[1] /
            "docs/specs/v_public_optimization_protocol.md").read_bytes()).hexdigest(),
        "public_pools": pools, "max_tests": MAX_TESTS, "core": CORE, "fixed_boundary": BOUNDARY,
        "cost": "sum_service_units_no_early_stop;equal_history_weight;within_history_source_dedup",
        "reference": "all_30_public_tests_complete_and_correct;not_independent_safety_labels",
        "quality": "zero_disagreement_on_fit_and_all_known_public_regressions",
        "objective": "minimum_actual_public_verification_cost_subject_to_quality",
        "headroom_gate": "all_folds_cost_saving_vs_old>=10_percent;all_heldout_public_agreement=1",
        "cheap_boundary_gate": "all_folds_boundary_premium<=5_percent;all_heldout_public_agreement=1",
        "model_calls": 0}
    save_record(output / "manifest.json", manifest)
    matrix = []
    for index, candidate in enumerate(imported["candidates"]):
        suite = pools[candidate["group"]]
        if candidate["source"] is None:
            tests = [{"complete": False, "pass": None, "cost": None} for _ in suite]
        else:
            result = task.evaluate_source(candidate["source"], suite)
            task.verify_result(result, suite)
            save_record(output / "public" / f"{index:03}.json", result)
            tests = [{"complete": all(r["complete"] for r in s["rounds"]),
                      "pass": all(r["complete"] and r["correct"] for r in s["rounds"]), "cost": s["cost"]}
                     for s in result["streams"]]
        matrix.append({k: candidate[k] for k in ("id", "group", "source_sha256", "kind", "native_eligible")} | {"tests": tests})
    save_record(output / "public-matrix.json", matrix)
    summary = analyze(matrix)
    summary["reference_collection"] = {"executed_programs": sum(c["source"] is not None for c in imported["candidates"]),
        "executed_requests": sum(c["source"] is not None for c in imported["candidates"]) * 90,
        "actual_service_units": sum(t["cost"] for r in matrix for t in r["tests"] if t["cost"] is not None)}
    save_record(output / "summary.json", summary)
    (output / "report.md").write_text(render(summary))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--public-history", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.source_run, args.public_history, args.output)
    print({k: v for k, v in result.items() if k not in ("folds", "limitations")})
