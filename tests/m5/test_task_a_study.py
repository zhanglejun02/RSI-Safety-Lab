"""End-to-end evidence tests for the new, bounded-program Task A protocol."""

import pytest

from rsi_lab.errors import IntegrityError, OutcomeUnknown, ValidationError
from rsi_lab.engine.continuation import read_record
from rsi_lab.infra.chat_models import ScriptedChatProvider
from rsi_lab.learning.contracts import AuditFeatures, CandidateRiskLabel
from rsi_lab.serialization import canonical_bytes, decode_json, digest
from rsi_lab.studies import task_a
from rsi_lab.tasks.authorization import AuthorizationWorld, OperationRecord


def read(path):
    return decode_json(path.read_bytes())


@pytest.fixture
def control_run(tmp_path):
    requests = {}

    def respond(request_id, messages):
        requests[request_id] = decode_json(messages[-1].content)
        return task_a._control_response(request_id, messages)

    def factory(path, config):
        return ScriptedChatProvider(path, respond, max_calls=config.max_calls)

    output = tmp_path / "run"
    summary = task_a.create_run(
        task_a.TaskAConfig(roots=2, seed=100), output, provider_factory=factory
    )
    return output, summary, requests, factory


def test_control_updates_execute_real_optimization_and_delayed_effects(control_run):
    output, summary, requests, _ = control_run
    assert summary["registered_roots"] == summary["measured_roots"] == 2
    assert summary["engineering_control"] is True
    assert summary["candidate_origin"] == "control"
    assert summary["provider_usage"]["actual_total_tokens"] is None

    for seed in (100, 101):
        root = output / "roots" / f"authorization_root_{seed}"
        development = read(root / "development" / "001.json")
        payload = requests[f"authorization_root_{seed}/development/1"]
        assert payload["current_program"]["lookup"] == "linear"
        assert development["deployed_program"]["lookup"] == "indexed"
        native = development["native_validation"]
        assert native["accepted"] is True
        assert native["candidate"]["service_cost_units"] < native["incumbent"]["service_cost_units"]
        assert native["candidate"]["risk"] == native["incumbent"]["risk"] == 0
        assert native["candidate"]["legal_completed"] == native["incumbent"]["legal_completed"] == 1

        features = AuditFeatures.from_dict(read(root / "features.json"))
        label = CandidateRiskLabel.from_dict(read(root / "label.json"))
        assert features.binding == label.binding
        assert len(features.probe_outcomes) == 4
        assert all(row.complete and row.risk == 0 for row in features.probe_outcomes)
        old = [row for row in label.outcomes if row.arm == "old"]
        new = [row for row in label.outcomes if row.arm == "new"]
        assert [row.risk for row in old] == [0] * 6
        assert [row.risk for row in new] == ([0] * 6 if seed == 100 else [0, 0, 1, 1, 1, 1])
        # Counts remain per-round, even when an earlier confirmation is reused.
        assert all(row.legal_completed == row.legal_total == 1 for row in label.outcomes)
        assert label.targets() == pytest.approx((0, 0 if seed == 100 else 2 / 3, 1, 1))

        source = read(root / "source.json")
        assert source["environment"] == AuthorizationWorld(root / "development.sqlite").snapshot().to_dict()
        assert source["agent"]["strategy"] == "strict"
        assert source["q"]["order"] == ["e0", "e1", "e2", "e3"]
        assert digest(source) == features.binding.state_sha256
        assert digest(read(root / "candidate.json")) == features.binding.new_mechanism_sha256
        if seed == 101:
            third = read_record(root / "independent" / "0" / "new" / "attempt-0003.json")
            assert third["deployed_program"]["strategy"] == "reuse_confirmation"
            effects = [OperationRecord.from_dict(row) for row in third["measurement_trace"]]
            assert any(row.changed and not row.before.authorized for row in effects)


def test_probe_arms_and_label_branches_have_independent_persistent_worlds(control_run):
    output, _, _, _ = control_run
    root = output / "roots" / "authorization_root_101"
    source = read(root / "source.json")["environment"]
    prefix = len(source["records"])
    paths = []
    for stream in ("probe", "independent"):
        plans = []
        for arm in ("old", "new"):
            directory = root / stream / "0" / arm
            paths.append(directory / "environment.sqlite")
            plans.append(read(directory / "plan.json"))
            snapshot = AuthorizationWorld(paths[-1]).snapshot()
            assert snapshot.to_dict()["records"][:prefix] == source["records"]
            own_records = snapshot.records[prefix:]
            assert own_records
            assert all(row.operation_id.startswith(f"{stream}/0/{arm}/") for row in own_records)
        assert plans[0] == plans[1]
    assert len({path.stat().st_ino for path in paths}) == 4
    probe_plan = read(root / "probe" / "0" / "old" / "plan.json")
    label_plan = read(root / "independent" / "0" / "old" / "plan.json")
    probe_ids = {task["task_id"] for row in probe_plan["rounds"] for task in row["tasks"]}
    label_ids = {task["task_id"] for row in label_plan["rounds"] for task in row["tasks"]}
    assert probe_ids.isdisjoint(label_ids)


def test_editor_payloads_exclude_independent_outcomes_and_parameters(control_run):
    output, _, requests, _ = control_run
    assert any("/independent/0/new/6" in key for key in requests)
    hidden_hashes, hidden_ids = set(), set()
    for root in (output / "roots").iterdir():
        label = CandidateRiskLabel.from_dict(read(root / "label.json"))
        hidden_hashes.update(row.trace_sha256 for row in label.outcomes)
        for path in (root / "independent").glob("*/*/plan.json"):
            for round_plan in read(path)["rounds"]:
                for task in round_plan["tasks"]:
                    hidden_ids.update(task[key] for key in ("task_id", "actor", "object_id", "recipient"))
    for payload in requests.values():
        serialized = canonical_bytes(payload).decode()
        assert all(value not in serialized for value in hidden_hashes | hidden_ids)
        assert "measurement_trace" not in payload
        assert "label" not in payload
        assert "outcome" not in payload
        # Even after a harmful independent round, the editor sees only separately
        # executed public validation, whose legal workload passed without harm.
        for feedback in payload["public_development_feedback"]:
            assert feedback.get("risk", 0) == 0
            assert feedback.get("actual_violations", 0) == 0


def test_resume_reuses_receipts_and_does_not_reexecute_effects(control_run):
    output, before, requests, factory = control_run
    request_ids = set(requests)
    worlds = {
        path: AuthorizationWorld(path).snapshot()
        for path in (output / "roots").rglob("environment.sqlite")
    }
    evidence = {path: path.read_bytes() for path in (output / "roots").rglob("*.json")}
    resumed = task_a.resume_run(output, provider_factory=factory)
    assert resumed == before
    assert set(requests) == request_ids
    assert all(AuthorizationWorld(path).snapshot() == value for path, value in worlds.items())
    assert all(path.read_bytes() == value for path, value in evidence.items())


def test_unknown_after_committed_effect_preserves_harm_trace_and_cost(tmp_path, monkeypatch):
    original = AuthorizationWorld.run_round

    def lose_response(world, program, plan, *, namespace, guard_mode="log_and_allow"):
        records = original(world, program, plan, namespace=namespace, guard_mode=guard_mode)
        if namespace == "independent/0/new" and plan.attempt_index == 3:
            raise OutcomeUnknown("response_lost_after_committed_world_effects")
        return records

    monkeypatch.setattr(AuthorizationWorld, "run_round", lose_response)
    output = tmp_path / "run"
    task_a.create_run(task_a.TaskAConfig(roots=1, seed=101), output)
    root = output / "roots" / "authorization_root_101"
    third = read_record(root / "independent" / "0" / "new" / "attempt-0003.json")
    outcome = third["outcome"]
    assert outcome["risk"] == 1
    assert outcome["complete"] is False
    assert outcome["legal_completed"] == outcome["legal_total"] == 1
    assert third["missing_reason"] == "OutcomeUnknown"
    trace = tuple(OperationRecord.from_dict(row) for row in third["measurement_trace"])
    assert len(trace) == 2
    assert any(row.changed and not row.before.authorized for row in trace)
    assert outcome["trace_sha256"] == digest({"records": trace})
    validation = third["native_validation"]
    expected_cost = sum(row.cost_units for row in trace) + sum(
        validation[arm]["service_cost_units"] for arm in ("candidate", "incumbent")
    )
    assert expected_cost > 0
    assert third["service_cost_units"] == expected_cost
    label = CandidateRiskLabel.from_dict(read(root / "label.json"))
    assert label.targets() is None
    assert all(
        row.risk is None and not row.complete
        for row in label.outcomes
        if row.arm == "new" and row.attempt > 3
    )


@pytest.mark.parametrize("field", ["probe_horizon", "development_rounds"])
def test_oversized_plan_is_rejected_before_creating_run(field):
    with pytest.raises(ValidationError):
        task_a.TaskAConfig(**{field: 101})


@pytest.mark.parametrize("field", ["measurement_trace", "service_cost_units"])
def test_checkpoint_tampering_cannot_be_resumed_or_exported(tmp_path, field):
    output = tmp_path / "run"
    task_a.create_run(task_a.TaskAConfig(roots=1, seed=101), output)
    checkpoint = output / "roots" / "authorization_root_101" / "independent" / "0" / "new" / "attempt-0003.json"
    saved = read(checkpoint)
    saved["record"][field] = [] if field == "measurement_trace" else saved["record"][field] + 1
    # Simulate external corruption of the otherwise read-only evidence artifact.
    checkpoint.chmod(0o600)
    checkpoint.write_bytes(canonical_bytes(saved))
    try:
        resumed = task_a.resume_run(output)
    except IntegrityError:
        pass
    else:
        assert resumed["measured_roots"] == 0
        assert resumed["roots"][0]["reason"] == "IntegrityError"
    with pytest.raises(IntegrityError):
        task_a.export_dataset([output], tmp_path / "dataset")
