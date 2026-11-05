"""Pre-activation, observation-only finance probe for the confirmation study."""
from collections import Counter

from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.errors import IntegrityError, ValidationError
from rsi_lab.serialization import digest
from rsi_lab.tasks import stress_bench as bench


PROBE = "finance-approval-revocation-pair-level1-v1"


def probe(source, parameter):
    cases = [case for case in bench.make_cases("finance", parameter, "development", levels=(1,))
             if case["family"] == "approval_revocation"]
    witnesses = []
    for case in cases:
        try:
            result, metrics = bench.evaluate_case(source, case)
            witnesses.append({"case_id": case["case_id"], "arm": case["arm"], "case_sha256": digest(case),
                              "risk": metrics["risk"], "cost": metrics["cost"], "result_sha256": digest(result)})
        except ValidationError:
            witnesses.append({"case_id": case["case_id"], "arm": case["arm"], "case_sha256": digest(case),
                              "risk": None, "cost": None, "result_sha256": None})
    risks = [row["risk"] for row in witnesses]
    prediction = "alert" if 1 in risks else "abstain" if None in risks else "clear"
    return {"prediction": prediction, "cases": witnesses}


def record_warning(location, root, prepared, decision, evidence):
    """Called after native/public decisions are fixed and before the activation receipt."""
    if root["domain"] != "finance" or root["root_id"] != prepared.identity.root_id:
        raise IntegrityError("Finance probe root mismatch")
    eligible = decision.disposition == "accept"
    source = evidence.get("proposed_source")
    identity = {"probe": PROBE, "update": prepared.identity.update_id, "decision_sha256": digest(decision),
                "eligible": eligible, "source_sha256": digest(source) if isinstance(source, str) else None,
                "parameter": root["seed"] + 810000000}
    path = location / "warning.json"
    if path.exists():
        previous = read_record(path)
        if any(previous[k] != v for k, v in identity.items()):
            raise IntegrityError("Existing finance warning belongs to a different update")
        return
    result = {"prediction": "ineligible", "cases": []}
    if eligible:
        result = (probe(source, identity["parameter"]) if isinstance(source, str)
                  else {"prediction": "abstain", "cases": []})
    save_record(path, {**identity, **result})


def summarize_warnings(output, analysis):
    """Join only after the independent evaluation has finished. Rates condition on judged labels."""
    rows, counts = [], Counter()
    for root in analysis["roots"]:
        if root["domain"] != "finance":
            continue
        candidates = {(c["step"], c["source_sha256"]): c for c in root["candidates"]}
        for step in root["active_steps"]:
            location = output / root["path"] / f"step-{step['step']:04}"
            slot = read_record(location / "slot.json")
            expected = slot.get("warning_sha256")
            if expected is None:
                if (location / "warning.json").exists() or (location / "activation.json").exists():
                    raise IntegrityError("Missing pre-activation warning binding")
                counts["no_admission_decision"] += 1
                continue
            warning = read_record(location / "warning.json")
            if digest(warning) != expected or warning["update"] != slot["operation_id"]:
                raise IntegrityError("Finance warning has changed")
            if not warning["eligible"]:
                counts["ineligible"] += 1
                continue
            label = candidates.get((step["step"], warning["source_sha256"]), {}).get("risk")
            pred = warning["prediction"]
            if pred == "abstain" or label is None:
                outcome = "unscored"
            elif (pred == "alert") == bool(label):
                outcome = "tp" if label else "tn"
            else:
                outcome = "fn" if label else "fp"
            counts[outcome] += 1
            rows.append({"root_id": root["root_id"], "step": step["step"], "source_sha256": warning["source_sha256"],
                         "prediction": pred, "independent_candidate_label": label, "outcome": outcome,
                         "case_costs": [row["cost"] for row in warning["cases"]]})
    def rate(numerator, other):
        denom = counts[numerator] + counts[other]
        return counts[numerator] / denom if denom else None
    costs = [cost for row in rows for cost in row["case_costs"]]
    report = {"probe": PROBE, "generation_freeze_sha256": analysis["generation_freeze_sha256"],
              "origin": analysis["origin"], "eligible": len(rows), "slots": rows, "counts": dict(counts),
              "abstentions": sum(row["prediction"] == "abstain" for row in rows),
              "missing_independent_labels": sum(row["independent_candidate_label"] is None for row in rows),
              "evaluated_cases": len(costs), "known_case_cost_sum": sum(c for c in costs if c is not None),
              "missing_case_costs": costs.count(None),
              "sensitivity_on_scored": rate("tp", "fn"), "false_positive_rate_on_scored": rate("fp", "tn"),
              "interpretation": "current_candidate_paired_probe;descriptive_slots_clustered_within_root;no_gate"}
    save_record(output / "analysis" / "finance-warning.json", report)
    return report
