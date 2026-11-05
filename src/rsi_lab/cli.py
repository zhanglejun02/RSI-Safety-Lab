"""CLI router. New experiments register once; legacy reproduction stays isolated."""
from importlib import import_module
import sys

# Explicit trusted modules, never executable import paths from configuration.
SUBCOMMANDS = {
    "platform": ("platform_cli", "Compose system, task, protocol and detector plugins"),
    "research": ("research_cli", "Task A collection, independent labels and training"),
    "cost-q": ("cost_q_cli", "Cost q descendants and frozen CPU scorers"),
    "q-effect": ("q_effect_cli", "Repeated q continuations and restoration"),
    "experience": ("experience_cli", "Public optimization history and editor diagnostics"),
    "optimized-q": ("optimized_q_cli", "Bounded H2 q diagnostic from an optimized program"),
    "v-offline": ("offline_v_cli", "Fixed real candidates and equal-budget v interventions"),
    "openevolve-screen": ("openevolve_screen", "Bounded native OpenEvolve suitability screen"),
    "openevolve-admission": ("openevolve_admission", "Zero-API fixed-candidate admission counterfactuals"),
    "stress-bench": ("stress_bench", "Build and validate paired development pressure tasks"),
    "stress-confirmation": ("stress_confirmation", "Register and evaluate paired confirmatory stress runs"),
    "stress-pilot": ("stress_pilot", "Bounded new-root GEPA/OpenEvolve pressure-task screen"),
    "mechanism-pilot": ("mechanism_pilot", "Bounded privacy, untrusted-feedback and cumulative-quota screen"),
    "adaptation-rl-pilot": ("adaptation_rl_pilot", "Scheduled requirement changes and trained tabular RL development runs"),
}


def main(argv=None) -> int:
    actual = list(sys.argv[1:] if argv is None else argv)
    if actual and actual[0] in SUBCOMMANDS:
        module, _ = SUBCOMMANDS[actual[0]]
        return import_module("rsi_lab.studies." + module).main(actual[1:])
    from ._legacy_cli import main as legacy_main
    return legacy_main(actual)
