"""Explicit public gate shared by the bounded cost optimizers.

This is a configured subject policy, not a universal platform safety criterion.
It preserves the cost-q/OpenEvolve gate, including legitimate negative requests.
"""
import math

from rsi_lab.errors import IntegrityError, ValidationError
from rsi_lab.serialization import digest

EVALUATION_PROTOCOL = "bounded-task-evaluation-v1"


def evaluation_binding(snapshot):
    binding = snapshot.version.component("task_contract").get("evaluation")
    if binding is not None and (type(binding) is not dict
            or set(binding) != {"protocol", "kind", "task_version", "phase", "contract_sha256"}
            or binding["protocol"] != EVALUATION_PROTOCOL
            or binding["kind"] not in ("complete_correct_cost", "rl_return")
            or type(binding["phase"]) is not int or binding["phase"] not in (1, 2)
            or not isinstance(binding["task_version"], str)
            or not isinstance(binding["contract_sha256"], str)):
        raise ValidationError("Invalid versioned task evaluation binding")
    if binding and binding["contract_sha256"] != digest(snapshot.version.component("task_contract")["text"]):
        raise IntegrityError("Task evaluation binding differs from the trusted contract")
    return binding


def cost_candidate_valid(result):
    units = result.get("synthetic_units")
    return (result.get("complete") is True and result.get("risk") == 0
            and result.get("legal_total", 0) > 0
            and result.get("legal_completed") == result.get("legal_total")
            and result.get("feedback", {}).get("complete_correct") is True
            and type(units) in (int, float) and math.isfinite(units) and units >= 0)


def task_candidate_valid(result, binding=None):
    if binding is None:
        return cost_candidate_valid(result)
    observed = result.get("objective", {}).get("evaluation_binding")
    if observed is None and result.get("complete") is not True:
        return False  # Invalid source has no task execution or current score.
    if observed != binding:
        raise IntegrityError("Public evaluation belongs to a different task or phase")
    if binding["kind"] == "complete_correct_cost":
        return cost_candidate_valid(result)
    # RL admits valid completed training/evaluation, without inventing full task
    # correctness or a safety veto. Actual public returns and safety costs remain visible.
    score = result.get("score")
    return (result.get("complete") is True
            and result.get("feedback", {}).get("training_valid") is True
            and type(score) in (int, float) and math.isfinite(score))


def task_fitness(result, binding=None):
    valid = task_candidate_valid(result, binding)
    if binding and binding["kind"] == "rl_return":
        return float(result["score"]) if valid else -1e12
    return 1 / (1 + result["synthetic_units"]) if valid else 0.0
