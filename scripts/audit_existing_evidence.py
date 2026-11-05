"""Read-only census of current pressure/adaptation evidence and stated estimands."""
import argparse
from collections import Counter
from math import comb, log, sqrt
from pathlib import Path

from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.serialization import digest
from rsi_lab.studies.stress_scorecard import build

RUNS = ("stress-rsi-kimi-20261103", "mechanism-kimi-20261105", "adaptation-rl-kimi-20261105")


def paired_power(n, difference, discordance, alpha=.05):
    """Hypothetical iid pairs, exact two-sided McNemar; not the registered test."""
    if not 0 < discordance <= 1 or not abs(difference) <= discordance:
        raise ValueError("Impossible paired binary alternative")
    theta, power = (1 + difference / discordance) / 2, 0.0
    for k in range(n + 1):
        chance = comb(n, k) * discordance**k * (1-discordance)**(n-k)
        for left in range(k + 1):
            p_value = min(1, 2 * sum(comb(k, x) for x in range(min(left, k-left) + 1)) / 2**k)
            if p_value <= alpha:
                power += chance * comb(k, left) * theta**left * (1-theta)**(k-left)
    return power


def audit(repo, output):
    roots, references, summaries = [], {}, []
    for name in RUNS:
        directory = repo / "runs" / name
        summary = read_record(directory / "analysis/summary.json")
        manifest = read_record(directory / "manifest.json")
        freeze = read_record(directory / "generation_complete.json")
        assert freeze["manifest_sha256"] == digest(manifest)
        assert summary["manifest_sha256"] == digest(manifest)
        assert summary["generation_freeze_sha256"] == digest(freeze)
        references[name] = {"manifest": digest(manifest), "freeze": digest(freeze), "summary": digest(summary)}
        scores = {r["root_id"]: r for r in build(directory)["roots"]} if "adaptation" not in name else {}
        for root in summary["roots"]:
            artifact = read_record(directory / root["path"] / "root.json")
            assert digest(artifact) == freeze["roots_sha256"][root["root_id"]]
            candidates = root["candidates"]
            candidate_risks = [c["evaluation"]["risk"] if "evaluation" in c else c["risk"] for c in candidates]
            active = [s["evaluation"] for s in root["active_steps"]]
            online = [s.get("deployment") for s in root["active_steps"]]
            row = {"run": name, "root_id": root["root_id"], "system": root["system"],
                "domain": root.get("kind", root["domain"]), "condition": root.get("condition", "fixed"),
                "seed": root["seed"], "registered_slots": len(root["active_steps"]),
                "generation_missing_slots": root.get("missing_slots", root.get("missing_steps", [])),
                "candidates": len(candidates), "candidate_risk_unknown": candidate_risks.count(None),
                "unsafe_candidate_events": candidate_risks.count(1),
                "active_effect_complete_slots": sum(a["complete"] for a in active),
                "active_risk": 1 if any(a["actual_violations"] for a in active) or any(a and a["actual_violations"] for a in online)
                    else 0 if all(a["complete"] for a in active) else None,
                "calls": root["model_usage"]["dispatched_calls"], "tokens": root["model_usage"]["actual_total_tokens"],
                "response_complete": root["model_usage"]["complete_responses"],
                "response_invalid": root["model_usage"]["invalid_responses"],
                "response_unknown": root["model_usage"]["unknown_responses"],
                "independent_unseen_problem": "not_established;fixed_known_templates",
                "candidate_label_type": "current_candidate_effects;not_new_harm_emerging_only_in_descendants"}
            if scores:
                score = scores[root["root_id"]]
                row.update(active_success_one_percent=score["success"]["one_percent"],
                    complete_generation_sensitivity=score["complete_generation_sensitivity"]["one_percent"],
                    legacy_analysis_cost_comparison_available=root["safe_cost_comparison_available"])
            roots.append(row)
        selected = [r for r in roots if r["run"] == name]
        summaries.append({"run": name, "roots": len(selected),
            **{key: sum(r[key] for r in selected) for key in ("calls", "tokens", "candidates", "candidate_risk_unknown", "unsafe_candidate_events",
                                                            "response_complete", "response_invalid", "response_unknown")},
            "active_unsafe_roots": sum(r["active_risk"] == 1 for r in selected),
            "active_risk_unknown_roots": sum(r["active_risk"] is None for r in selected)})
    hypothetical = {"zero_events_one_sided_95_upper": {str(n): 1 - .05**(1/n) for n in (2, 4, 8, 12)},
        "assumption": "binomial illustration;not_a_pooled_deployment_prevalence_estimate",
        "registered_64_pair_hoeffding_halfwidth": sqrt(2 * log(40) / 64),
        "hypothetical_exact_mcnemar_power": [{"n": 64, "difference": .125, "discordance": d,
            "power": paired_power(64, .125, d)} for d in (.125, .5, 1)],
        "test_scope": "power_sensitivity_only;does_not_replace_registered_Hoeffding_analysis"}
    value = {"schema": "current-evidence-audit-v1", "run_references": references, "roots": roots,
        "studies": summaries, "statistical_checks": hypothetical, "pooled_prevalence": None,
        "model_calls_during_audit": 0, "training_during_audit": 0,
        "not_a_complete_inventory_of_legacy_learning_pairs": True}
    save_record(output / "evidence-census.json", value)
    lines = ["# 现有主线证据清单", "", "这是三轮当前压力／动态／RL 研究的逐根清单，不是所有旧学习标签的去重数据集；不将不同协议合成总体发生率。", "",
        "| 运行 | 根 | 调用 | token | 已解析候选／风险标签未知 | 危险候选事件 | 已测活动风险根 |",
        "| --- | --- | --- | --- | --- | --- | --- |"]
    for s in summaries:
        lines.append(f"| {s['run']} | {s['roots']} | {s['calls']} | {s['tokens']} | {s['candidates']} / {s['candidate_risk_unknown']} | "
                     f"{s['unsafe_candidate_events']} | {s['active_unsafe_roots']} |")
    lines += ["", "活动风险与候选风险分别统计；同一根中的多次事件不能直接视作独立危险机制。具体结论应依据本次输入的逐根记录。",
        "", "| 根 | 生成缺失槽 | 完整活动效果 | 至少 1% 主终点 | 生成证据完整敏感性 | 旧分析可比较标志 |",
        "| --- | --- | --- | --- | --- | --- |"]
    for r in roots:
        if r["generation_missing_slots"]:
            lines.append(f"| {r['root_id']} | {r['generation_missing_slots']} | {r['active_effect_complete_slots']}/{r['registered_slots']} | "
                         f"{r.get('active_success_one_percent')} | {r.get('complete_generation_sensitivity')} | "
                         f"{r.get('legacy_analysis_cost_comparison_available')} |")
    lines += ["", "## 功效数值的假设检查", "", "零事件独立二项示例的单侧 95% 上界为 1−0.05^(1/n)："]
    lines += [f"- 0/{n}：{100*v:.2f}%。" for n, v in hypothetical["zero_events_one_sided_95_upper"].items()]
    lines += ["", "64 对的登记 Hoeffding 半宽为 33.95 个百分点。它是保守区间界，不等同于检验的 MDE。",
              "下面仅作另一种预先指定检验的假设敏感性例子：64 个独立同分布配对、真实成功率差 12.5pp、双侧精确 McNemar α=0.05。",
              "", "| 不一致对概率 | 精确功效 |", "| --- | --- |"]
    lines += [f"| {v['discordance']} | {v['power']:.4f} |" for v in hypothetical["hypothetical_exact_mcnemar_power"]]
    lines += ["", "因此不能只由 n=64 和差值 12.5pp 给出唯一的 0.29 功效；还需要联合分布和检验。相同提示或相同成本也不能直接推出 ICC 或有效 n。",
              "二项区间依据 [NIST](https://itl.nist.gov/div898/handbook/prc/section2/prc241.htm)；精确配对检验定义依据 [statsmodels 文档](https://www.statsmodels.org/stable/generated/statsmodels.stats.contingency_tables.mcnemar.html)。本脚本用有限二项求和计算假设功效，不替换任何旧研究的登记分析。"]
    (output / "evidence-census.md").write_text("\n".join(lines) + "\n")
    return value


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    result = audit(Path(__file__).resolve().parents[1], args.output)
    print(result["studies"])
