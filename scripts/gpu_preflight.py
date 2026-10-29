#!/usr/bin/env python3
"""Read-only host/GPU diagnostics; does not load a model or run inference.

Only Python's standard library is required. An already installed torch package
is queried if available. nvidia-smi, when present, supplies separate driver and
hardware evidence. CUDA readiness requires PyTorch to see at least one device;
neither that check nor nvidia-smi constitutes a model-performance validation.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import importlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys


def installed_versions() -> dict[str, str | None]:
    result = {}
    for package in ("torch", "transformers", "vllm", "accelerate"):
        try:
            result[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            result[package] = None
    return result


def nvidia_diagnostics() -> dict:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return {"status": "not_installed", "devices": [], "error": None}
    try:
        result = subprocess.run(
            [executable, "--query-gpu=index,name,driver_version,memory.total,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode != 0:
            return {"status": "error", "devices": [],
                    "error": result.stderr.strip() or f"nvidia-smi exit code {result.returncode}"}
        devices = []
        for row in csv.reader(result.stdout.splitlines()):
            if not row:
                continue
            fields = [value.strip() for value in row]
            if len(fields) != 5:
                raise ValueError("unexpected nvidia-smi CSV field count")
            index, name, driver, total, free = fields
            devices.append({"index": int(index), "name": name, "driver_version": driver,
                            "total_memory_mib": int(total), "free_memory_mib": int(free)})
        return {"status": "available" if devices else "no_devices", "devices": devices, "error": None}
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"status": "error", "devices": [], "error": f"{type(exc).__name__}: {exc}"}


def torch_diagnostics() -> dict:
    result = {"status": "not_installed", "version": None, "compiled_cuda_version": None,
              "cuda_available": False, "device_count": 0, "devices": [], "error": None}
    try:
        torch = importlib.import_module("torch")
    except ModuleNotFoundError as exc:
        # Missing torch differs from an installed torch with a broken dependency.
        if exc.name != "torch":
            result.update(status="import_error", error=f"{type(exc).__name__}: {exc}")
        return result
    except Exception as exc:
        result.update(status="import_error", error=f"{type(exc).__name__}: {exc}")
        return result
    try:
        result["version"] = str(torch.__version__)
        result["compiled_cuda_version"] = torch.version.cuda
        result["cuda_available"] = bool(torch.cuda.is_available())
        if not result["cuda_available"]:
            result["status"] = "cuda_unavailable"
            return result
        result["device_count"] = int(torch.cuda.device_count())
        for index in range(result["device_count"]):
            properties = torch.cuda.get_device_properties(index)
            result["devices"].append({"index": index, "name": properties.name,
                                      "total_memory_bytes": int(properties.total_memory),
                                      "compute_capability": [int(properties.major), int(properties.minor)]})
        result["cuda_available"] = result["device_count"] > 0
        result["status"] = "available" if result["cuda_available"] else "cuda_unavailable"
    except Exception as exc:
        result.update(status="query_error", cuda_available=False,
                      error=f"{type(exc).__name__}: {exc}")
    return result


def collect_report() -> dict:
    torch = torch_diagnostics()
    return {
        "schema_version": 1,
        "diagnostic": "rsi_lab_gpu_preflight_v1",
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "host": {"system": platform.system(), "release": platform.release(),
                 "machine": platform.machine(), "python": platform.python_version(),
                 "cpu_count": os.cpu_count()},
        "installed_packages": installed_versions(),
        "nvidia_smi": nvidia_diagnostics(),
        "torch": torch,
        "cuda_ready_for_runtime_setup": torch["cuda_available"],
        "validation": {"model_loaded": False, "inference_run": False,
                       "task_correctness_checked": False, "throughput_measured": False,
                       "hard_isolation_verified": False},
        "next_step": ("M1 does not require a GPU. Complete the M2/M3 CPU fixture gates "
                      "before a separately implemented and validated fixed-LLM pilot."),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write JSON to a new file; existing paths are rejected")
    parser.add_argument("--require-cuda", action="store_true",
                        help="Exit 2 unless installed PyTorch can query a CUDA device")
    args = parser.parse_args(argv)
    if args.output is not None and (args.output.exists() or args.output.is_symlink()):
        print(f"Output already exists; choose a new path: {args.output}", file=sys.stderr)
        return 1
    report = collect_report()
    serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output is not None:
        try:
            # Exclusive creation also protects against an output appearing after preflight.
            with args.output.open("x", encoding="utf-8") as destination:
                destination.write(serialized)
                destination.flush()
                os.fsync(destination.fileno())
        except OSError as exc:
            print(f"Cannot write diagnostic output {args.output}: {exc}", file=sys.stderr)
            return 1
    print(serialized, end="")
    if args.require_cuda and not report["cuda_ready_for_runtime_setup"]:
        print("CUDA requirement failed: installed PyTorch did not expose a queryable CUDA device. "
              "The JSON report is available on stdout; M1 CPU work is unaffected.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
