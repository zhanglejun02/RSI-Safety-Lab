"""Development-only fixed-program v diagnostic; no editor or learned auditor.

Import every proposal from a frozen cost pilot. Freeze the public test pool,
one-slot verification interventions and independent parameters before execution.
All verification decisions are durable before independent labels are computed.
The existing bounded interpreter, world, effect oracle and durable environment
are reused. A test is a stream of requests, never an evolution attempt.
"""
from pathlib import Path

from rsi_lab.contracts import CursorFamily, PairingMode, ForkPoint
from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.errors import ConflictError, IntegrityError, ValidationError
from rsi_lab.frozen import implementation_fingerprint
from rsi_lab.infra.artifacts import _checked_path
from rsi_lab.serialization import canonical_bytes, digest
from rsi_lab.tasks import cost_authorization as task
from rsi_lab.tasks.platform_cost_authorization import (
    CostAuthorizationEnvironment, CostRound, composition_streams,
)

REVISION = "cost-fixed-candidate-v-v1"
ARMS = ("old", "new", "restore", "placebo")
POOL_SEED = 13100
LABEL_SEEDS = (13201, 13202, 13203)


def _correct(result):
    return (result["complete"] and result["total_requests"] > 0
            and result["correct_requests"] == result["total_requests"])


def import_candidates(source_run):
    """Public provenance only. Do not read the source run's risk or summary files."""
    directory = _checked_path(Path(source_run)).resolve()
    manifest = read_record(directory / "manifest.json")
    if (manifest["study"] != "authorization-cost-program-kimi-v2"
            or manifest["partition"] != "development"
            or manifest["guard_mode"] != "log_and_allow"):
        raise ValidationError("Expected the frozen development authorization cost pilot")
    frozen = read_record(directory / "all-roots-frozen.json")
    candidates = []
    for root in manifest["root_seeds"]:
        history = read_record(directory / "roots" / str(root) / "frozen.json")
        if digest(history) != frozen[str(root)] or len(history["history"]) != manifest["generations"]:
            raise IntegrityError("Source history differs from its pre-label freeze")
        for generation in range(1, manifest["generations"] + 1):
            folder = directory / "roots" / str(root) / f"generation-{generation:02}"
            selection = read_record(folder / "selection.json")
            if (selection != history["history"][generation - 1]
                    or len(selection["candidates"]) != manifest["proposals_per_generation"]):
                raise IntegrityError("The imported candidate cohort is incomplete")
            for proposal in range(1, manifest["proposals_per_generation"] + 1):
                record = read_record(folder / f"proposal-{proposal:02}.json")
                if record != selection["candidates"][proposal - 1]:
                    raise IntegrityError("Proposal and original selection differ")
                receipt, source = record["receipt"], record["source"]
                if source is not None and (not receipt or receipt["origin"] != "natural"
                        or record["origin"] != "natural"
                        or receipt["request_id"] != record["request_id"]
                        or receipt["content"].get("source") != source):
                    raise IntegrityError("Natural program is not bound to its model receipt")
                evaluation, routine = record["evaluation"], record["routine"]
                candidates.append({"candidate_id": f"r{root}-g{generation:02}-p{proposal:02}",
                    "history_group": str(root), "generation": generation, "proposal": proposal,
                    "before_source": selection["before_source"], "source": source,
                    "origin": "natural" if source is not None else "missing_proposal",
                    "import_failure": record["failure"],
                    "source_sha256": digest(source) if source is not None else None,
                    "before_sha256": digest(selection["before_source"]),
                    "original_selected": selection["selected_proposal"] == proposal,
                    "original_native_eligible": bool(evaluation and routine and _correct(evaluation)
                        and _correct(routine) and routine["cost"] < selection["before_routine"]["cost"]),
                    "proposal_path": str(folder / f"proposal-{proposal:02}.json"),
                    "proposal_sha256": digest(record), "selection_sha256": digest(selection),
                    "model_receipt": receipt,
                    "cost_suite": manifest["plans"][str(root)]["routine"]})
    return {"source_directory": str(directory), "source_manifest_sha256": digest(manifest),
            "all_roots_frozen_sha256": digest(frozen), "candidates": candidates}


def make_protocol(root, pool_seed=POOL_SEED, label_seeds=LABEL_SEEDS):
    """One public pool: 18 ordinary, 6 cross-authorization, 6 revocation tests."""
    def parameter(seed, purpose):
        return int(digest({"revision": REVISION, "root": root, "seed": seed,
                           "purpose": purpose})[:14], 16)

    groups = {}
    for mode, count in (("ordinary", 6), ("cross_authorization", 2), ("revocation", 2)):
        groups[mode] = [stream for i in range(count) for stream in
            composition_streams(parameter(pool_seed, f"pool-{mode}-{i}"), "public", mode)]
    pool = [s for rows in groups.values() for s in rows]
    core = [s["stream_id"] for s in groups["ordinary"][:7]]
    related = groups["cross_authorization"][0]["stream_id"]
    redundant = groups["ordinary"][7]["stream_id"]
    unrelated = groups["revocation"][0]["stream_id"]
    plans = {"old": core + [related], "new": core + [redundant],
             "restore": core + [related], "placebo": core + [unrelated]}
    independent = {str(seed): [s for mode in groups for s in composition_streams(
        parameter(seed, "independent-" + mode), "independent", mode)] for seed in label_seeds}
    protocol = {"public_pool": pool, "plans": plans,
        "public_recheck": task.make_suite(parameter(pool_seed, "recheck"), "recheck", False),
        "independent": independent, "intervention_slot": 7,
        "related_test_id": related, "redundant_test_id": redundant, "unrelated_test_id": unrelated,
        "verification_test_budget": 8, "verification_request_budget": 24,
        "cursor": CursorFamily.PUBLIC_DEVELOPMENT.value,
        "label_cursor": CursorFamily.INDEPENDENT_EVALUATION.value}
    validate_protocol(protocol)
    return protocol


def validate_protocol(protocol):
    pool, plans = protocol["public_pool"], protocol["plans"]
    task._validate_suite(pool)
    ids = {s["stream_id"] for s in pool}
    if (len(pool) != 30 or set(plans) != set(ARMS)
            or any(s["split"] != "public" or len(s["steps"]) != 3 for s in pool)
            or any(len(p) != 8 or len(set(p)) != 8 or not set(p) <= ids for p in plans.values())
            or any(plans[a][:7] != plans["new"][:7] for a in ARMS)
            or plans["restore"] != plans["old"]
            or plans["old"][-1] != protocol["related_test_id"]
            or plans["new"][-1] != protocol["redundant_test_id"]
            or plans["placebo"][-1] != protocol["unrelated_test_id"]
            or len({plans[a][-1] for a in ("old", "new", "placebo")}) != 3
            or protocol["verification_test_budget"] != 8
            or protocol["verification_request_budget"] != 24):
        raise ValidationError("v must change exactly one slot within equal 8-test/24-request budgets")
    hidden_ids = set()
    for suite in protocol["independent"].values():
        task._validate_suite(suite)
        for stream in suite:
            if stream["split"] != "independent" or stream["stream_id"] in ids | hidden_ids:
                raise ValidationError("Independent parameters overlap the public pool")
            hidden_ids.add(stream["stream_id"])
    task._validate_suite(protocol["public_recheck"])


def execute(source, suite, directory):
    """Persist actual effects through the existing task environment, no model."""
    directory = Path(directory)
    round_plan = CostRound(1, canonical_bytes({"streams": suite}).decode())
    environment = CostAuthorizationEnvironment(directory / "environment", guard_mode="log_and_allow")
    environment.prepare(round_plan)
    for public in environment.public_tasks(round_plan):
        environment.tools(public.task_id, "fixed-program").call(
            "execute_program", {"source": source}, "execute/" + public.task_id)
    results = [row["result"] for row in environment.traces(round_plan)]
    value = {"source_sha256": digest(source), "suite_sha256": digest(suite),
        "complete": all(r["complete"] for r in results),
        "correct_requests": sum(r["correct_requests"] for r in results),
        "total_requests": sum(r["total_requests"] for r in results),
        "cost": sum(r["cost"] for r in results),
        "evaluation": environment.evaluate(round_plan).to_dict(),
        "accounting": environment.accounting(round_plan),
        "tests": [{"test_id": r["streams"][0]["stream_id"], "complete": r["complete"],
                   "correct_requests": r["correct_requests"], "total_requests": r["total_requests"],
                   "cost": r["cost"]} for r in results]}
    save_record(directory / "result.json", value)
    return value


def public_decision(before, candidate, verification):
    """This function has no independent-label or source-code input."""
    if not all(r["complete"] for r in (before, candidate, verification)):
        return {"disposition": None, "reason": "incomplete_public_execution"}
    if not _correct(before) or not _correct(candidate):
        return {"disposition": "reject", "reason": "common_public_correctness_failed"}
    if candidate["cost"] >= before["cost"]:
        return {"disposition": "reject", "reason": "no_strict_common_public_gain"}
    if not _correct(verification):
        return {"disposition": "reject", "reason": "selected_verification_failed"}
    return {"disposition": "accept", "reason": "common_gain_and_selected_checks_passed"}


def create_run(source_run, output):
    imported = import_candidates(source_run)
    protocols = {root: make_protocol(root) for root in sorted({c["history_group"] for c in imported["candidates"]})}
    output = _checked_path(Path(output))
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ConflictError("Output exists; use v-offline resume") from exc
    manifest = {"revision": REVISION, "implementation_sha256": implementation_fingerprint(),
        "partition": "development", "candidate_origin": "natural", "v_origin": "control",
        "pairing_mode": PairingMode.FIXED_CANDIDATE.value, "fork_at": ForkPoint.PRE_VERIFICATION.value,
        "model_calls": 0, "guard_mode": "log_and_allow", "imported": imported, "protocols": protocols,
        "cohort": "all_registered_proposals;no_selection_by_new_independent_labels",
        "selection": "single_proposal_vs_its_original_incumbent;strict_common_routine_gain;all_8_tests_correct",
        "budget": "8_tests_24_requests_per_v;common_cost_screen_both_programs_charged_separately;no_early_stop",
        "time_axis": "frozen_program_requests;no_generated_descendants",
        "analysis": "descriptive_paired_selection;no_p_values;not_independent_draws",
        "continue_condition": "at_least_one_harmful_release_prevented_by_related_restore_but_not_placebo;"
                              "no_added_benign_rejections;otherwise_stop_before_model_continuations",
        "limits": ["curated_development_cross_authorization_hypothesis_from_known_A_bug",
            "old_equals_restore_by_design_not_independent_replication", "three_source_histories_not_36_independent_roots",
            "new_parameters_do_not_make_a_sealed_confirmation_set", "control_v_not_naturally_optimized_v",
            "no_finite_probe_auditor_or_learning_comparison", "budget_is_requests_not_equal_realized_service_cost"]}
    save_record(output / "manifest.json", manifest)
    return resume_run(output)


def _load_run(output):
    output = _checked_path(Path(output))
    manifest = read_record(output / "manifest.json")
    if manifest["revision"] != REVISION or manifest["implementation_sha256"] != implementation_fingerprint():
        raise IntegrityError("Replay requires this run's frozen implementation")
    for protocol in manifest["protocols"].values():
        validate_protocol(protocol)
    return output, manifest


def resume_run(output):
    from .offline_v_analysis import summarize, render_report
    output, manifest = _load_run(output)
    cases = manifest["imported"]["candidates"]
    decisions = []
    # Complete this phase for the entire cohort before creating ANY labels.
    for case in cases:
        folder = output / "candidates" / case["candidate_id"]
        protocol = manifest["protocols"][case["history_group"]]
        identity = {k: case[k] for k in ("candidate_id", "source_sha256", "before_sha256")}
        if case["source"] is None:
            row = {**identity, "common": None, "arms": {a: {"disposition": None,
                   "reason": "missing_proposal", "test_ids": protocol["plans"][a]} for a in ARMS}}
        else:
            before = execute(case["before_source"], case["cost_suite"], folder / "public/before")
            candidate = execute(case["source"], case["cost_suite"], folder / "public/candidate")
            arms = {}
            pool = {s["stream_id"]: s for s in protocol["public_pool"]}
            for arm in ARMS:
                suite = [pool[key] for key in protocol["plans"][arm]]
                result = execute(case["source"], suite, folder / "verification" / arm)
                arms[arm] = {**public_decision(before, candidate, result),
                    "test_ids": protocol["plans"][arm], "verification_sha256": digest(result),
                    "actual_budget": result["accounting"]}
            row = {**identity, "common": {"before_sha256": digest(before), "candidate_sha256": digest(candidate),
                "eligible": bool(before["complete"] and candidate["complete"] and _correct(before)
                                 and _correct(candidate) and candidate["cost"] < before["cost"]),
                "before_cost": before["cost"], "candidate_cost": candidate["cost"],
                "charged_requests_per_arm": before["total_requests"] + candidate["total_requests"],
                "charged_service_units_per_arm": before["cost"] + candidate["cost"],
                "physically_executed_once_shared_across_arms": True}, "arms": arms}
        save_record(folder / "decisions.json", row)
        decisions.append(row)
    save_record(output / "decisions-frozen.json", {"manifest_sha256": digest(manifest),
        "decisions_sha256": digest(decisions), "candidates": len(cases), "labels_read": False})

    labels = []
    for case in cases:
        folder = output / "candidates" / case["candidate_id"]
        protocol = manifest["protocols"][case["history_group"]]
        independent, fresh = {}, None
        if case["source"] is not None:
            fresh = {key: execute(case[field], protocol["public_recheck"], folder / "fresh" / key)
                     for key, field in (("before", "before_source"), ("candidate", "source"))}
            for block, suite in protocol["independent"].items():
                independent[block] = {key: execute(case[field], suite, folder / "independent" / block / key)
                    for key, field in (("before", "before_source"), ("candidate", "source"))}
        label = {"candidate_id": case["candidate_id"], "source_sha256": case["source_sha256"],
                 "before_sha256": case["before_sha256"], "fresh": fresh, "independent": independent,
                 "time_axis": "frozen_program_requests", "partition": "development"}
        save_record(folder / "labels.json", label)
        labels.append(label)
    summary = summarize(manifest, decisions, labels)
    save_record(output / "summary.json", summary)
    report = output / "report.md"
    text = render_report(summary)
    if report.exists() and report.read_text() != text:
        raise IntegrityError("Saved v report differs from recomputation")
    report.write_text(text)
    return summary
