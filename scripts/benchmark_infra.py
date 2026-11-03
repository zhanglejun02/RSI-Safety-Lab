"""CPU-only infra benchmark; synthetic journals, no model transport or API calls.

Run the same script with PYTHONPATH pointing to each source snapshot. Timings
measure local operations, not remote latency, token savings or scientific gains.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import platform
import statistics
import tempfile
import time
from unittest.mock import patch

from rsi_lab.frozen import implementation_fingerprint
from rsi_lab.infra import chat_models
from rsi_lab.serialization import canonical_bytes, digest
from rsi_lab.tasks import cost_program, retail_workflow as retail


def measure(samples, operation):
    results = [operation() for _ in range(samples)]
    evidence = [row.pop("seconds") for row in results]
    if any(row != results[0] for row in results):
        raise AssertionError("Deterministic benchmark evidence changed between samples")
    return {**results[0], "seconds": evidence, "median_seconds": statistics.median(evidence),
            "min_seconds": min(evidence), "max_seconds": max(evidence)}


@contextmanager
def counted(module, name):
    original, counts = getattr(module, name), {"calls": 0}

    def wrapped(*args, **kwargs):
        counts["calls"] += 1
        return original(*args, **kwargs)

    with patch.object(module, name, wrapped):
        yield counts


def retail_projections(repeats):
    with tempfile.TemporaryDirectory(prefix="rsi-retail-benchmark-") as directory:
        task = retail.RetailTaskPackage()
        step = task.public_suite(15001).rounds[0]
        env = task.create_environment(Path(directory))
        env.prepare(step)
        with counted(retail, "verify_result") as count:
            start = time.perf_counter()
            for public in env.public_tasks(step):
                env.tools(public.task_id, "benchmark").call(
                    "execute_program", {"source": retail.SAFE_SOURCE}, public.task_id)
            hashes = []
            for _ in range(repeats):
                values = (env.evaluate(step), env.synthetic_cost(step), env.public_trajectories(step),
                          env.traces(step), env.snapshot())
                hashes.append(digest(values))
            elapsed = time.perf_counter() - start
        assert len(set(hashes)) == 1
        # A restored environment must independently verify durable evidence.
        restored = task.create_environment(Path(directory))
        assert digest(restored.snapshot()) == digest(env.snapshot())
        return {"seconds": elapsed, "oracle_replays": count["calls"], "output_sha256": hashes[0],
                "executions": len(step.streams), "projection_cycles": repeats}


def program_execution(repeats):
    cached = getattr(cost_program, "_validated_ast", None)
    if cached is not None:
        cached.cache_clear()
    suite = retail.make_suite(15002, "independent", composed=True)
    with counted(cost_program.ast, "parse") as count:
        start = time.perf_counter()
        runs = [retail.evaluate_source(retail.SAFE_SOURCE, suite) for _ in range(repeats)]
        elapsed = time.perf_counter() - start
    assert all(run == runs[0] for run in runs)
    assert retail.verify_result(runs[0], suite)
    return {"seconds": elapsed, "ast_parses": count["calls"], "output_sha256": digest(runs),
            "requests": repeats * sum(len(s["steps"]) for s in suite)}


def seed_synthetic_history(provider, rows):
    """Unmeasured, artificial completed rows; never reported as real model calls."""
    body = provider._payload((chat_models.ChatMessage("user", "synthetic fixture"),))
    request_hash = hashlib.sha256(body).hexdigest()
    raw = canonical_bytes({"model": "local-fixture", "choices": [{"finish_reason": "stop",
        "message": {"content": json.dumps({"text": "x" * 16384})}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30, "cost": 0.001}})
    with provider._db() as db:
        for index in range(rows):
            request_id = "history/" + str(index)
            result = provider._parse(request_id, request_hash, raw)
            db.execute("""INSERT INTO chat_requests(
                request_id, request_sha, request_json, status, reserved_tokens,
                actual_tokens, response_bytes, response_sha, result_json)
                VALUES(?,?,?,'complete',?,30,?,?,?)""",
                (request_id, request_hash, body, len(body) + provider.config.max_output_tokens + 256,
                 raw, hashlib.sha256(raw).hexdigest(), canonical_bytes(result)))


def budget_reservation(history_rows, repeats):
    def forbidden(*args):
        raise AssertionError("Benchmark must never invoke a transport")

    with tempfile.TemporaryDirectory(prefix="rsi-budget-benchmark-") as directory:
        provider = chat_models.DurableChatProvider(Path(directory) / "models.sqlite",
            chat_models.ChatConfig("http://localhost/fixture", "local-fixture", max_calls=10000,
                                   max_total_tokens=10000000), transport=forbidden)
        seed_synthetic_history(provider, history_rows)
        body = provider._payload((chat_models.ChatMessage("user", "synthetic reservation"),))
        with counted(chat_models, "decode_json") as count:
            start = time.perf_counter()
            for index in range(repeats):
                saved, _ = provider._reserve("reservation/" + str(index), body)
                assert saved is None
            elapsed = time.perf_counter() - start
        summary = provider.summary()
        assert summary["dispatched_calls"] == history_rows + repeats
        assert summary["actual_total_tokens"] == history_rows * 30
        assert summary["unknown_responses"] == repeats
        assert summary["reported_cost_calls"] == history_rows
        return {"seconds": elapsed, "response_decodes_during_reservation": count["calls"],
                "output_sha256": digest(summary), "synthetic_history_rows": history_rows,
                "reservations": repeats, "transport_calls": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.samples <= 20:
        parser.error("samples must be between 1 and 20")
    result = {"implementation_sha256": implementation_fingerprint(),
        "python": platform.python_version(), "machine": platform.machine(),
        "samples": args.samples, "model_transport_calls": 0,
        "cases": {"retail_projections": measure(args.samples, lambda: retail_projections(10)),
                  "program_execution": measure(args.samples, lambda: program_execution(20)),
                  **{"budget_" + str(rows): measure(args.samples, lambda rows=rows: budget_reservation(rows, 10))
                     for rows in (50, 500)}}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print(json.dumps({name: {k: v for k, v in row.items() if k != "seconds"}
                      for name, row in result["cases"].items()}, indent=2))


if __name__ == "__main__":
    main()
