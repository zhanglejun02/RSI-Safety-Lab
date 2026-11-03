from copy import deepcopy
import json
from pathlib import Path
import pytest

from rsi_lab.errors import IntegrityError, ValidationError
from rsi_lab.serialization import digest
from rsi_lab.tasks import stress_bench as tasks
from rsi_lab.studies import stress_bench as study


def test_corpus_controls_integrity_and_fixed_source_scoring(tmp_path):
    corpus, output = tmp_path / "corpus", tmp_path / "check"
    manifest = study.build(corpus, seeds=1)
    assert manifest["case_count"] == 120 and manifest["pair_count"] == 60
    assert {"tasks/fitness.py", "tasks/retail_vendor/cancel_pending_order.py",
            "tasks/retail_vendor/modify_pending_order_address.py"} <= set(manifest["implementation"])
    checked = study.check(corpus, output)
    assert checked["validation_passed"], checked["errors"]
    assert checked["http_calls"] == 0 and not checked["natural_rsi_results"]
    assert checked["executions"] == 480
    assert all(checked["safe_improvement_available"].values())
    # A submitted fixed program has no invented natural/evolution provenance.
    entries = {}
    for domain in tasks.DOMAINS:
        source = tmp_path / (domain + ".py")
        source.write_text(tasks.backend(domain).SAFE_SOURCE)
        entries[domain] = source.name
    mapping = tmp_path / "programs.json"
    mapping.write_text(json.dumps(entries))
    scored = study.score(corpus, mapping, "safe-check", tmp_path / "score")
    assert all(r["metrics"]["correctness"] == 1 and r["metrics"]["risk"] == 0 for r in scored["rows"])
    assert scored["origin"] == "external_unverified"
    path = next((corpus / "cases").glob("*.json"))
    path.chmod(0o600)
    path.unlink()
    with pytest.raises(IntegrityError, match="inventory"):
        study.load_corpus(corpus)


@pytest.mark.parametrize("domain", tasks.DOMAINS)
def test_common_task_package_keeps_labels_private_and_results_replay(tmp_path, domain, monkeypatch):
    from rsi_lab.contracts import CursorFamily, Partition
    package = tasks.StressTaskPackage(domain, levels=(1,))
    step = package.public_suite(3).rounds[0]
    env = package.create_environment(tmp_path / domain)
    env.prepare(step)
    visible = package.create_environment(tmp_path / (domain + "-fresh"))
    for case, task in zip(step.cases, env.public_tasks(step)):
        assert set(json.loads(task.request_json)) == {"requests"}
        assert task.task_id == case["case_id"]
        operation = "execute/" + task.task_id
        tool = env.tools(task.task_id, "test")
        source = tasks.backend(domain).SAFE_SOURCE
        receipt = tool.call("execute_program", {"source": source}, operation)
        assert receipt["complete"]
        assert tool.call("execute_program", {"source": source}, operation) == receipt
    measured = env.evaluate(step)
    assert measured.complete and measured.risk == 0
    assert measured.legal_completed == measured.legal_denominator
    assert not visible.snapshot()["records"]
    assert len(env.public_trajectories(step)) == len(step.cases)
    assert all(row["public_evaluation"]["correct_requests"] == row["public_evaluation"]["total_requests"] for row in env.public_trajectories(step))
    frozen = env.snapshot()
    restored = package.create_environment(tmp_path / (domain + "-restored"), frozen)
    restored.prepare(step)
    def unexpected_execution(*args, **kwargs):
        raise AssertionError("Restored operation must not execute a second time")
    with monkeypatch.context() as patch:
        patch.setattr(tasks, "evaluate_case", unexpected_execution)
        for task in restored.public_tasks(step):
            receipt = restored.tools(task.task_id, "test").call("execute_program", {"source": source}, "execute/" + task.task_id)
            assert receipt["complete"]
    assert restored.snapshot() == frozen
    other_step = package.public_suite(4).rounds[0]
    restored.prepare(other_step)
    task = restored.public_tasks(other_step)[0]
    restored.tools(task.task_id, "child").call("execute_program", {"source": source}, "child/execute")
    assert len(restored.snapshot()["records"]) == len(frozen["records"]) + 1
    assert env.snapshot() == frozen
    changed = deepcopy(frozen)
    changed["records"][0]["source_sha256"] = digest("changed source")
    with pytest.raises(IntegrityError, match="identity"):
        package.create_environment(tmp_path / (domain + "-tampered"), changed).snapshot()
    with pytest.raises(ValidationError, match="Sealed"):
        package.public_suite(3, Partition.SEALED)
    with pytest.raises(ValidationError, match="Sealed"):
        package.plan(3, CursorFamily.PUBLIC_DEVELOPMENT, Partition.SEALED, 1, "mixed")


def test_missing_risk_is_not_dropped_from_macro():
    case = {"domain": "privacy", "family": "test", "split": "development", "level": 1, "arm": "stress"}
    values = {"complete": False, "risk": None, "legal_total": 1, "total_requests": 1,
        "legal_completed": 0, "correct_requests": 0, "actual_violations": 0,
        "violation_attempts": 0, "native_tool_refusals": 0, "blocked_actions": 0, "cost": 0}
    groups, macro = study._aggregate([{"profile": "test", "case": case, "metrics": values}])
    assert groups[0]["risk_bounds"] == [0, 1]
    assert groups[0]["risk_missing"] == 1
    assert macro[0]["trajectory_risk"] is None and macro[0]["legal_completion"] is None


def test_pinned_openevolve_runs_all_domains_without_http(tmp_path):
    pytest.importorskip("openevolve")
    rows = study._smoke(tmp_path)
    assert len(rows) == 4
    assert all(r["admitted"] and r["scripted_model_calls"] == 1 and r["http_calls"] == 0 for r in rows)
