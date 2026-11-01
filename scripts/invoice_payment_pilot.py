"""Separate invoice-payment migration using the same bounded experiment loop.

The reference authorization study must finish before natural financial edits
begin. Its outcomes are provenance only, never editor input. This is a new
optimization on a different task, not unchanged-source or benchmark transfer.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

from rsi_lab.engine.continuation import read_record
from rsi_lab.errors import IntegrityError, LabError, ValidationError
from rsi_lab.serialization import digest
from rsi_lab.tasks import invoice_payment


def load_runner(reference_run=None):
    path = Path(__file__).with_name("authorization_cost_pilot.py")
    spec = importlib.util.spec_from_file_location("invoice_cost_experiment_loop", path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    runner.task = invoice_payment
    runner.ROOT_SEEDS = (6101, 6102)
    runner.GENERATIONS = 4
    runner.ROOT_PARALLELISM = 2
    runner.FULL_GATE_FROM = 4
    base_manifest = runner._manifest

    def manifest():
        result = base_manifest()
        reference = None
        if reference_run is not None:
            root = Path(reference_run)
            summary = read_record(root / "summary.json")
            frozen = read_record(root / "all-roots-frozen.json")
            if summary.get("phase") != "evaluated" or summary["registered_roots"] != len(frozen):
                raise IntegrityError("Authorization reference is not a completed registered study")
            for seed, checksum in frozen.items():
                if digest(read_record(root / "roots" / seed / "frozen.json")) != checksum:
                    raise IntegrityError("Authorization reference history differs from its frozen commitment")
            reference = {"manifest_sha256": digest(read_record(root / "manifest.json")),
                         "summary_sha256": digest(summary), "frozen_sha256": digest(frozen)}
        result.update(study="invoice-payment-cache-migration-kimi-v1", reference_authorization_study=reference,
            adaptation_notes=["New bounded policy optimization on typed invoice, PO, receipt, approval and payment-ledger records.",
                "Mechanism migration, not unchanged-source transfer or AgentDojo/financial-audit benchmark reproduction.",
                "No prompt injection; main independent streams change approval state, binding or expiry.",
                "Two development roots/four generations/two proposals; frozen endpoint testing, no admission model."],
            limitations_of_transfer="Only approval-cache validity is the shared mechanism; new source is generated for financial APIs.")
        result["sources"][Path(__file__).name] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        return result

    runner._manifest = manifest
    return runner


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference-run", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--phase", choices=("controls", "optimize", "evaluate", "all"), default="all")
    args = parser.parse_args()
    try:
        if args.phase != "controls" and args.reference_run is None:
            raise ValidationError("Natural invoice experiments require a completed --reference-run")
        result = load_runner(args.reference_run).run(args.output, phase=args.phase, env_file=args.env_file)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (LabError, OSError) as exc:
        print(type(exc).__name__ + ": " + str(exc))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
