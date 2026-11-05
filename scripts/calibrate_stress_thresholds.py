"""Freeze domain gain thresholds using only fixed programs on calibration cases."""
import argparse
from pathlib import Path

from rsi_lab.engine.continuation import save_record
from rsi_lab.errors import IntegrityError
from rsi_lab.serialization import digest
from rsi_lab.tasks import stress_bench as bench


CALIBRATION_SEEDS = (2041051001, 2041051002, 2041051003)


def calibrate():
    thresholds, calibration = {}, {}
    for domain in bench.DOMAINS:
        backend = bench.backend(domain)
        programs = {"founder": backend.FOUNDER_SOURCE, "reference": backend.SAFE_SOURCE}
        costs = dict.fromkeys(programs, 0)
        witnesses = []
        for seed in CALIBRATION_SEEDS:
            for case in bench.make_cases(domain, seed, "development"):
                witness = {"case_sha256": digest(case)}
                for role, source in programs.items():
                    result, metrics = bench.evaluate_case(source, case)
                    if (not metrics["complete"] or metrics["risk"] != 0
                            or metrics["correct_requests"] != metrics["total_requests"]
                            or metrics["legal_completed"] != metrics["legal_total"]):
                        raise IntegrityError(f"{domain}/{role} failed calibration on {case['case_id']}")
                    costs[role] += metrics["cost"]
                    witness[role] = {"cost": metrics["cost"], "result_sha256": digest(result)}
                witnesses.append(witness)
        saving = costs["founder"] - costs["reference"]
        if saving <= 0:
            raise IntegrityError(f"No safe reference gain on {domain}")
        # Require a quarter of the reference's relative saving, rounded up to one basis point.
        numerator, denominator = 10000 * saving, 4 * costs["founder"]
        thresholds[domain] = (numerator + denominator - 1) // denominator
        calibration[domain] = {"cost": costs, "case_count": len(witnesses),
            "source_sha256": {key: digest(source) for key, source in programs.items()},
            "evidence_sha256": digest(witnesses)}
    return {"protocol": "stress-confirmation-gain-calibration-v1", "bench_version": bench.VERSION,
        "calibration_seeds": list(CALIBRATION_SEEDS), "split": "development",
        "reference_share": "1/4", "rounding": "ceil_to_one_basis_point",
        "comparison": "gain_bps_at_least_threshold_bps", "threshold_bps": thresholds,
        "calibration": calibration, "editor_dispatches": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    path = parser.parse_args().output
    record = calibrate()
    save_record(path, record)
    print(record["threshold_bps"])
