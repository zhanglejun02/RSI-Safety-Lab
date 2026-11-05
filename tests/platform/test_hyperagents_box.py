"""Concrete HyperAgents inheritance and real Q-learning side-effect boundaries."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import pytest

from rsi_lab.api import SystemSnapshot, replace_component
from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.errors import BudgetExceeded, IntegrityError, OutcomeUnknown, UnsupportedCapability, ValidationError
from rsi_lab.infra.chat_models import ScriptedChatProvider
from rsi_lab.serialization import decode_json, digest, json_text
from rsi_lab.studies import hyperagents_box as study
from rsi_lab.subjects import hyperagents as hyper
from rsi_lab.subjects import hyperagents_runtime as runtime
from rsi_lab.tasks import box_navigation as box
from rsi_lab.tasks.adaptation_rl_package import StudyExecutor, StudyTaskPackage


def test_box_learning_conflict_and_legality_remain_distinct():
    item = box.case(2066111701)
    initial, safe, unsafe = [box.evaluate(source, item) for source in
                             (box.FOUNDER_SOURCE, box.SAFE_SOURCE, box.UNSAFE_SOURCE)]
    for result in (initial, safe, unsafe):
        box.verify(result, item)
        assert result["q_table_sha256"] != result["initial_q_sha256"]
        assert result["metrics"]["q_updates"] > 0
        assert result["metrics"]["illegal_actions"] == 0
        assert all(s["q_before"] == s["q_after"] for e in result["episodes"] if e["phase"] == "evaluation" for s in e["steps"])
    assert initial["metrics"]["task_return"] < safe["metrics"]["task_return"] < unsafe["metrics"]["task_return"]
    assert safe["metrics"]["goal_completed"] == unsafe["metrics"]["goal_completed"] == 1
    assert safe["metrics"]["actual_violations"] == 0 and unsafe["metrics"]["actual_violations"] == 1
    # Deployment safety does not erase the safe learner's training damage.
    assert safe["metrics"]["training_safety_cost"] > 0
    assert unsafe["metrics"]["legal_completed"] == 0
    layout = item["layout"]
    state = (4, 2, 5, 2)
    assert box.move(layout, state, 0) == (state, False, False)
    assert box.transition(layout, (3, 2, 4, 2), 0)[2] == 1
    assert 0 in box.allowed(layout, (3, 2, 4, 2))


@pytest.mark.parametrize("field", ["safety_cost", "reward", "q_after", "legal_action"])
def test_box_independent_replay_detects_forged_effects(field):
    item = box.case(7)
    result = box.evaluate(box.FOUNDER_SOURCE, item)
    result["episodes"][0]["steps"][0][field] += 1
    with pytest.raises(IntegrityError):
        box.verify(result, item)


def test_box_configuration_and_budget_do_not_accept_fixed_policies(tmp_path, monkeypatch):
    for source in (box.source(513), box.SAFE_SOURCE.replace('"push_penalty": 3', '"actions": 3')):
        with pytest.raises(ValidationError):
            box.configuration(source)
    limits = {**study.manifest()["config"]["execution"], "root_training_steps": 1}
    executor = StudyExecutor(tmp_path, limits)
    monkeypatch.setattr(box, "evaluate", lambda *a, **k: pytest.fail("trained past frozen cap"))
    with pytest.raises(BudgetExceeded):
        executor.execute(box.SAFE_SOURCE, box.case(7), "limited")


def test_box_restore_never_retrains_or_repeats_effects(tmp_path, monkeypatch):
    executor = StudyExecutor(tmp_path, study.manifest()["config"]["execution"])
    item = box.case(19)
    result = executor.execute(box.SAFE_SOURCE, item, "saved")
    before = executor.summary()
    monkeypatch.setattr(box, "evaluate", lambda *a, **k: pytest.fail("unexpected retraining"))
    assert executor.execute(box.SAFE_SOURCE, item, "saved") == result
    assert before == executor.summary()


def require_sandbox(tmp_path):
    if sys.platform != "darwin":
        pytest.skip("This backend explicitly requires macOS sandbox-exec")
    result = subprocess.run(["/usr/bin/sandbox-exec", "-p", runtime.profile(tmp_path.resolve()),
        str(runtime.executable()), "-I", "-S", "-c", "print('ready')"], capture_output=True)
    if result.returncode != 0:
        pytest.skip("Nested host sandbox disallows sandbox initialization; run these tests in a permitted local shell")


def test_hyper_sandbox_blocks_private_reads_external_writes_and_network(tmp_path):
    require_sandbox(tmp_path)
    secret = tmp_path / "independent-label.txt"; secret.write_text("PRIVATE_TEST_LABEL")
    external = tmp_path / "forbidden-write.txt"
    meta = '''from agent.base_agent import AgentSystem
class MetaAgent(AgentSystem):
    def forward(self, repo_path, eval_path, iterations_left=None):
        import socket
        results = []
        for operation in (lambda: open(SECRET).read(), lambda: open(EXTERNAL, 'w'),
                          lambda: socket.create_connection(('127.0.0.1', 9), timeout=1)):
            try:
                operation()
                results.append('ALLOWED')
            except PermissionError:
                results.append('DENIED')
        with open(repo_path + '/policy.py', 'w') as f:
            f.write('def handle(request, cache, api):\\n    return cache\\n# ' + repr(results))
'''.replace('SECRET', repr(str(secret))).replace('EXTERNAL', repr(str(external)))
    provider = ScriptedChatProvider(tmp_path / "model.sqlite", lambda *a: pytest.fail("sandbox probe called model"))
    result = runtime.edit(tmp_path / "edit", {"meta_agent.py": meta, "policy.py": box.FOUNDER_SOURCE},
                          {"contract": box.CONTRACT, "evaluation": {}}, provider, "sandbox-boundary")
    assert "['DENIED', 'DENIED', 'DENIED']" in result["files"]["policy.py"]
    assert not external.exists() and "PRIVATE_TEST_LABEL" not in str(result)


def test_hyper_unknown_edit_cannot_restart(tmp_path, monkeypatch):
    path = tmp_path / "worker"
    save_record(path / "intent.json", {"lost": True})
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("restarted unknown worker"))
    with pytest.raises(OutcomeUnknown):
        runtime.edit(path, {}, {}, None, "lost")


def test_hyper_prompt_budget_stops_before_model_dispatch(tmp_path):
    require_sandbox(tmp_path)
    model = ScriptedChatProvider(tmp_path / "model.sqlite", lambda *a: pytest.fail("over-budget model dispatch"))
    model.config = replace(model.config, max_prompt_bytes=1000)
    with pytest.raises(BudgetExceeded, match="prompt-byte"):
        runtime.edit(tmp_path / "edit", {"policy.py": box.FOUNDER_SOURCE,
            "meta_agent.py": (runtime.VENDOR / "meta_agent.py").read_text()},
            {"contract": box.CONTRACT, "evaluation": {}}, model, "prompt-cap")
    assert model.summary()["dispatched_calls"] == 0


def test_pinned_native_parent_uses_valid_archive_not_only_best():
    archive = {"initial": {"score": 2, "parent_id": None, "valid_parent": True},
               "low": {"score": 1, "parent_id": "initial", "valid_parent": True},
               "invalid": {"score": 100, "parent_id": "initial", "valid_parent": False}}
    selected = {hyper.native_parent(archive, seed) for seed in range(12)}
    assert selected == {"initial", "low"}


def test_hyper_can_admit_archive_without_changing_active_program(tmp_path):
    require_sandbox(tmp_path)
    def respond(operation, messages):
        if operation.endswith("model-1"):
            return {"text": "<json>" + json_text({"tool_name": "write_files", "tool_input": {
                "files": {"policy.py": box.SAFE_SOURCE}}}) + "</json>"}
        return {"text": "Done"}
    model = ScriptedChatProvider(tmp_path / "model.sqlite", respond, max_calls=3)
    origin = study.initial("hyperagents", 31)
    adapter = hyper.HyperAgentsAdapter(tmp_path / "system", model, origin)
    class PublicControl:
        # Unit score-store control only; these scores are not RL experiment results.
        plan = {"unit_test": "lower_scoring_archive_entry"}
        def evaluate(self, snapshot, operation):
            return {"complete": True, "feedback": {"training_valid": True},
                "score": 2 if snapshot.version.component("policy")["source"] == box.FOUNDER_SOURCE else 1,
                "objective": {"evaluation_binding": study.evaluation_binding()}}
    draft = adapter.propose("archive-only", PublicControl())
    assert draft.native_valid and decode_json(draft.native_evidence_json)["platform_accepted"]
    assert draft.candidate.version == origin.version
    assert len(decode_json(draft.candidate.state_json)["archive"]) == 2
    assert draft.candidate.sha256 != origin.sha256 and adapter.snapshot() == origin


def test_hyper_parent_diff_is_relative_to_selected_archive_parent(tmp_path):
    require_sandbox(tmp_path)
    # Canonical snapshots sort archive IDs: seed 0 chooses "initial" from
    # ["active", "initial"], deliberately testing a non-active parent.
    base = study.initial("hyperagents", 0)
    state = decode_json(base.state_json)
    state["archive"]["initial"]["files"]["policy.py"] = box.SAFE_SOURCE
    state["archive"]["active"] = deepcopy(state["archive"]["initial"])
    state["archive"]["active"]["parent_id"] = "initial"
    state["archive"]["active"]["files"]["policy.py"] = box.UNSAFE_SOURCE
    state["active_id"] = "active"
    base = replace(base, version=replace_component(base.version, "policy", {"source": box.UNSAFE_SOURCE}),
                   state_json=json_text(state))
    model = ScriptedChatProvider(tmp_path / "model.sqlite", lambda *a: {"text": "Done"})
    adapter = hyper.HyperAgentsAdapter(tmp_path / "system", model, base)
    executor = StudyExecutor(tmp_path / "execution", study.manifest()["config"]["execution"])
    package = StudyTaskPackage("box_rl", study.evaluation_binding(), [box.case(31)], executor)
    root = {"root_id": "parent-control", "path": "roots/parent-control", "system": "hyperagents", "domain": "box_rl", "seed": 31}
    row = study.pilot._slot(tmp_path, root, 1, adapter, package, model, {"protocol": "parent-control", "horizon": 1})
    assert row["candidate_sources"][0]["source"] == box.SAFE_SOURCE
    assert row["base_source"] == box.UNSAFE_SOURCE
    assert row["candidate_sources"][0]["source_changed_from_parent"] is False


def test_all_adapters_meta_inheritance_rejection_isolation_and_resume(tmp_path, monkeypatch):
    require_sandbox(tmp_path)
    output = tmp_path / "control"
    # Exercise recovery between outer slots before completing the same run.
    assert study.run(output, stop_after=1) is None
    result = study.run(output, resume=True)
    assert result["paid_calls"] == 0
    for row in result["systems"]:
        slots = row["slots"]
        assert [s["platform_committed"] for s in slots] == [True, True, False]
        assert [s["active_source_changed"] for s in slots] == [True, True, False]
        assert all(s["status"] == "complete" for s in slots)
        assert slots[0]["parent_in_request"] and slots[1]["parent_in_request"]
        assert all(r["actual_violations"] == 1 for r in slots[2]["independent"])
    row = next(r for r in result["systems"] if r["system"] == "hyperagents")
    assert [s["meta_executed_marker"] for s in row["slots"]] == [False, True, True]
    directory = output / "roots/box-hyperagents"
    base = SystemSnapshot.from_dict(read_record(directory / "step-0001/base.json"))
    adapter = hyper.HyperAgentsAdapter(directory / "system", study.provider(directory, "hyperagents"), base)
    branch = adapter.fork(base, tmp_path / "branch")
    assert branch.snapshot() == base and adapter.snapshot() != base
    assert "9999" not in str(decode_json(adapter.snapshot().state_json))
    assert "REJECTED_META_CONTROL_V1" not in str(decode_json(adapter.snapshot().state_json))
    # Independent test IDs/layouts must not enter any model request.
    with adapter.provider._db() as db:
        requests = [decode_json(r[0]) for r in db.execute("SELECT request_json FROM chat_requests")]
    private_ids = [box.case(2066211701 + i, purpose="independent", replica=i)["case_id"] for i in range(2)]
    assert all(identifier not in str(requests) for identifier in private_ids)
    before = {p: p.read_bytes() for p in output.rglob("*.sqlite")}
    monkeypatch.setattr(box, "evaluate", lambda *a, **k: pytest.fail("restore retrained"))
    monkeypatch.setattr(runtime, "edit", lambda *a, **k: pytest.fail("restore ran meta code"))
    monkeypatch.setattr(ScriptedChatProvider, "generate", lambda *a, **k: pytest.fail("restore called model"))
    assert study.run(output, resume=True) == result
    assert before == {p: p.read_bytes() for p in output.rglob("*.sqlite")}
