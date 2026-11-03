"""Opt-in public objective shared by bounded cost tasks; not an oracle."""


def complete_correct_cost_fitness(metrics):
    rows = metrics["public_trajectories"]
    values = [r.get("public_evaluation") for r in rows]
    available = bool(values) and all(type(v) is dict for v in values)
    total = sum(v["total_requests"] for v in values) if available else 0
    correct = sum(v["correct_requests"] for v in values) if available else 0
    complete_correct = bool(available and total and metrics["complete"] and
                            all(r["execution_complete"] for r in rows) and correct == total)
    return {"score": -float(metrics["synthetic_units"]) if complete_correct else -1e12,
            "objective": {"priority": "complete_correct_then_minimum_cost",
                          "cost_measure": "synthetic_service_units", "inference_cost_reported_separately": True},
            "feedback": {**metrics["feedback"], "public_correctness": correct / total if total else None,
                         "complete_correct": complete_correct, "correct_requests": correct,
                         "total_requests": total}}
