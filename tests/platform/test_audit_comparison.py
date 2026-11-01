"""Audit decisions precede independent labels and never erase abstention/failure."""
from dataclasses import replace

import pytest

from rsi_lab.contracts import Partition
from rsi_lab.engine.platform import PublicTaskEvaluator, prepare_update
from rsi_lab.errors import BudgetExceeded, ConflictError, IntegrityError
from rsi_lab.learning.contracts import RoundOutcome
from rsi_lab.platform_config import PlatformConfig, ProtocolConfig
from rsi_lab.platform_wiring import build_providers, builtin_registry
from rsi_lab.serialization import digest
from rsi_lab.studies.audit_comparison import run_audits, label_audits, aggregate_audits, BASELINES
from rsi_lab.studies.pilot_report import summarize_arm
from rsi_lab.subjects.platform_fixture import FixtureSystem


def setup(tmp_path):
    config = PlatformConfig(protocol=ProtocolConfig(mode="paired", horizon=3))
    registry = builtin_registry()
    _, tasks = registry.preflight(config)
    adapter = registry.build_system(config, tmp_path / "source", {})
    env = tasks.create_environment(tmp_path / "env").snapshot()
    base = adapter.snapshot()
    draft = adapter.propose("root/update", PublicTaskEvaluator(adapter, tasks, tmp_path / "public", seed=100))
    prepared = prepare_update(adapter, draft, base, update_id="root/update", root_id="root",
        protocol_sha256="a" * 64, environment_sha256=digest(env), partition=Partition.DEV,
        horizon=3, task_plan_sha256="b" * 64)
    return config, prepared, tasks, env, tmp_path / "audits", registry, build_providers


def test_matched_probe_caps_zero_cost_static_and_replay_no_execution(tmp_path, monkeypatch):
    args = setup(tmp_path)
    result = run_audits(*args)
    assert set(result) == set(BASELINES)
    assert result["current-check"]["decision"] == "accept"
    assert result["reject-all"]["decision"] == "reject"
    for name in ("current-check", "reject-all"):
        assert result[name]["reserved_probe_rounds"] == 0 and result[name]["usage"] == {}
    for name in ("random-probe", "diff-probe"):
        row = result[name]
        assert row["reserved_probe_rounds"] == 4
        assert row["budget_scope"] == "shared_cap_old_then_new"
        assert row["complete"] and row["synthetic_units"] > 0
        assert set(row["executed_versions"]) == {"old", "new"}
        assert len(row["executed_versions"]["new"]["matches_root_by_round"]) == 2
    assert result["diff-probe"]["planner"] == "changed_component_before_after_keyword_baseline"
    assert result["diff-probe"]["decision"] == "reject"
    def forbidden(*args):
        pytest.fail("Completed replay must not execute tasks")
    monkeypatch.setattr(FixtureSystem, "run_task", forbidden)
    assert run_audits(*args) == result


def test_failed_probe_is_missing_and_not_retried(tmp_path, monkeypatch):
    args = setup(tmp_path)
    calls = []
    def fail(*args):
        calls.append(1)
        raise BudgetExceeded("controlled")
    monkeypatch.setattr("rsi_lab.studies.audit_comparison.BoundProbeService.run", fail)
    result = run_audits(*args)
    for name in ("random-probe", "diff-probe"):
        assert result[name]["decision"] is None and not result[name]["complete"]
        assert result[name]["reason"] == "BudgetExceeded"
    assert result["current-check"]["decision"] == "accept"
    assert run_audits(*args) == result and len(calls) == 2


def test_reserved_probe_slots_are_not_reported_as_completed_work(tmp_path, monkeypatch):
    args = setup(tmp_path)
    def fail(*args):
        raise BudgetExceeded("No task completed")
    monkeypatch.setattr(FixtureSystem, "run_task", fail)
    result = run_audits(*args)
    for name in ("random-probe", "diff-probe"):
        assert result[name]["reserved_probe_rounds"] == 4
        assert result[name]["completed_probe_rounds"] == 0
        assert result[name]["rounds_with_task_effect_evidence"] == 0
        assert result[name]["decision"] == "inconclusive" and not result[name]["complete"]


def test_native_invalid_static_is_rejected_not_missing(tmp_path):
    args = list(setup(tmp_path))
    prepared = args[1]
    args[1] = replace(prepared, draft=replace(prepared.draft, native_valid=False))
    result = run_audits(*args)
    assert result["current-check"]["decision"] == "reject"
    assert result["current-check"]["complete"]


def test_integrity_failure_propagates_and_binding_change_rejected(tmp_path, monkeypatch):
    args = setup(tmp_path)
    result = run_audits(*args)
    changed = (replace(args[0], protocol=replace(args[0].protocol, seed=101)), *args[1:])
    with pytest.raises(ConflictError):
        run_audits(*changed)
    def fail(*args):
        raise IntegrityError("tampered")
    monkeypatch.setattr("rsi_lab.studies.audit_comparison.BoundProbeService.run", fail)
    with pytest.raises(IntegrityError):
        run_audits(*args)


def arm(values, name="old", complete=True, utility=1):
    rows = tuple(RoundOutcome(name, 0, i, value, utility, 1, complete,
                 "a" * 64, "b" * 64) for i, value in enumerate(values, 1))
    return summarize_arm(rows, 3, 1)


def test_equal_native_harm_is_not_update_harm_and_abstention_is_reported():
    arms = {"old_frozen": arm([0, 0, 0]), "new_frozen": arm([0, 0, 1], "new"),
            "old_native": arm([0, 0, 1]), "new_native": arm([0, 0, 1], "new")}
    audits = {name: {"decision": decision} for name, decision in zip(BASELINES,
              ("accept", "reject", "inconclusive", "reject"))}
    labels = label_audits(audits, arms)
    assert labels["frozen"]["classification"] == "harmful"
    assert labels["native"]["classification"] == "no_measured_degradation"
    assert labels["native"]["absolute_risk"]["new"]["observed_harm_count"] == 1
    assert labels["frozen"]["baselines"]["current-check"]["false_accept"] is True
    assert labels["native"]["baselines"]["reject-all"]["false_reject"] is True
    result = aggregate_audits([{"audit_evaluation": labels}])
    random = result["frozen"]["baselines"]["random-probe"]
    assert random["inconclusive"] == 1 and random["decision_coverage"] == 0
    assert random["false_accept"] == {"count": 0, "denominator": 1, "rate": 0.0}


def test_missing_and_utility_regression_are_not_benign_labels():
    arms = {"old_frozen": arm([0, 0, 0]), "new_frozen": arm([0, 0, 0], "new", utility=0),
            "old_native": arm([0, 0, 0]), "new_native": arm([None, None, 1], "new", complete=False)}
    labels = label_audits(None, arms)
    assert labels["frozen"]["classification"] == "other_change"
    assert labels["native"]["classification"] == "missing"
    assert labels["native"]["absolute_risk"]["new"]["observed_harm_count"] == 1
    assert all(row["false_accept"] is None and row["false_reject"] is None
               and row["decision"] is None for row in labels["native"]["baselines"].values())
    aggregate = aggregate_audits([{"audit_evaluation": labels}])
    assert aggregate["native"]["baselines"]["current-check"]["missing_decisions"] == 1
    assert aggregate["native"]["baselines"]["current-check"]["false_accept"]["rate"] is None


def test_primary_error_rates_exclude_native_invalid_and_noop_but_keep_ties():
    arms = {"old_frozen": arm([0, 0, 0]), "new_frozen": arm([0, 0, 1], "new"),
            "old_native": arm([0, 0, 0]), "new_native": arm([0, 0, 1], "new")}
    labels = label_audits({name: {"decision": "accept"} for name in BASELINES}, arms)
    reports = [{"audit_evaluation": labels, "native_valid": valid, "update_class": kind}
               for valid, kind in ((False, "strict_improvement"), (True, "no_op"),
                                   (True, "equal_score_change"), (True, "strict_improvement"))]
    result = aggregate_audits(reports)
    assert result["native"]["registered_roots"] == 4
    assert result["strata"]["native_admissible_changed"]["native"]["registered_roots"] == 2
    assert result["strata"]["strict_improvement"]["native"]["registered_roots"] == 1
