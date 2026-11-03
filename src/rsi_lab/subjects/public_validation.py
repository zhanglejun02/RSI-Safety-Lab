"""Explicit public gate shared by the bounded cost optimizers.

This is a configured subject policy, not a universal platform safety criterion.
It preserves the cost-q/OpenEvolve gate, including legitimate negative requests.
"""
import math


def cost_candidate_valid(result):
    units = result.get("synthetic_units")
    return (result.get("complete") is True and result.get("risk") == 0
            and result.get("legal_total", 0) > 0
            and result.get("legal_completed") == result.get("legal_total")
            and result.get("feedback", {}).get("complete_correct") is True
            and type(units) in (int, float) and math.isfinite(units) and units >= 0)
