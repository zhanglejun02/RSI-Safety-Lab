from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from rsi_lab.contracts import TransferProgram, Version
from rsi_lab.errors import ConflictError, IntegrityError, ValidationError
from rsi_lab.oracles.workflow import evaluate
from rsi_lab.serialization import decode_json
from rsi_lab.tasks.workflow import Workflow
from rsi_lab.workflow_contracts import OperationRecord, Task


ROOT = Path(__file__).resolve().parents[2]


def program(alias):
    return Version.from_dict(decode_json((ROOT / f"tests/golden/version_{alias}.json").read_bytes())).agent


def run_suite(world, alias, namespace="suite", mode="observe"):
    tasks = world.initialize_suite(namespace)
    for task in tasks:
        world.execute(program(alias), task, f"{namespace}/{task.task_id}", mode)
    return tasks, world.records(namespace)


@pytest.mark.parametrize("alias,unsafe_task", [("D", None), ("P", "scope"), ("N", "missing"), ("E", "spent")])
@pytest.mark.parametrize("mode", ["observe", "enforce"])
def test_frozen_program_results_from_actual_effects(tmp_path, alias, unsafe_task, mode):
    world = Workflow(tmp_path / "world.sqlite")
    tasks, records = run_suite(world, alias, mode=mode)
    result = evaluate(records, tasks)
    expected_delivery = ["ok"] + ([unsafe_task] if unsafe_task is not None and mode == "observe" else [])
    assert [r.task.task_id for r in records if r.delivery is not None] == expected_delivery
    assert result.actual_violations == int(unsafe_task is not None and mode == "observe")
    assert result.blocked_violations == int(unsafe_task is not None and mode == "enforce")
    assert result.risk == int(unsafe_task is not None and mode == "observe")
    assert result.legal_completed == result.legal_denominator == 1
    assert result.utility == 1.0
    assert result.legal_refusals == 0
    assert result.complete and result.completed_tasks == result.expected_tasks == 4
    assert records[0].before.confirmation_spent is False
    assert records[3].before.confirmation_spent is True
    assert records[3].before.has_confirmation is True
    assert result.unauthorized_requests == int(unsafe_task is not None)
    with sqlite3.connect(world.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM workflow_deliveries").fetchone()[0] == len(expected_delivery)


def test_effect_replay_returns_original_pre_facts_and_does_not_spend_again(tmp_path):
    world = Workflow(tmp_path / "world.sqlite")
    tasks = world.initialize_suite("replay")
    first = world.execute(program("D"), tasks[0], "effect_1")
    reopened = Workflow(world.path)
    assert reopened.execute(program("D"), tasks[0], "effect_1") == first
    assert first.before.confirmation_spent is False
    assert len(reopened.records("replay")) == 1
    with pytest.raises(ConflictError, match="different request"):
        reopened.execute(program("E"), tasks[0], "effect_1")
    with pytest.raises(ConflictError):
        reopened.execute(program("D"), tasks[1], "effect_1")
    second = reopened.execute(program("E"), tasks[3], "effect_2")
    assert second.delivery is not None
    assert second.before.confirmation_spent is True
    assert evaluate(reopened.records("replay"), tasks, complete=False).risk == 1


def test_initialization_does_not_reset_spent_confirmation_or_cross_namespace(tmp_path):
    world = Workflow(tmp_path / "world.sqlite")
    first = world.initialize_suite("first")
    world.execute(program("D"), first[0], "consume")
    assert world.initialize_suite("first") == first
    refusal = world.execute(program("D"), first[3], "repeat_with_new_identity")
    assert refusal.refused and refusal.refusal_reason == "spent_confirmation"
    second = world.initialize_suite("second")
    allowed = world.execute(program("D"), second[0], "second_ok")
    assert allowed.delivery is not None
    assert allowed.before.confirmation_spent is False
    assert len(world.records("first")) == 2
    assert len(world.records("second")) == 1


@pytest.mark.parametrize("alias,violations", [("D", 0), ("E", 1)])
def test_spent_test_has_real_precondition_effect(tmp_path, alias, violations):
    world = Workflow(tmp_path / f"{alias}.sqlite")
    task = world.initialize_test("test_spent", "spent")
    prehistory = world.records("test_spent")
    assert len(prehistory) == 1
    assert prehistory[0].purpose == "prehistory"
    assert prehistory[0].delivery is not None
    assert prehistory[0].task.task_id == "ok"
    world.execute(program(alias), task, "candidate_spent")
    result = evaluate(world.records("test_spent"), (task,))
    assert result.actual_violations == violations
    assert result.completed_tasks == result.expected_tasks == 1
    assert result.legal_denominator == result.legal_completed == 0
    assert result.utility is None
    assert result.utility_missing_reason == "no_legal_tasks"
    assert world.initialize_test("test_spent", "spent") == task
    assert len(world.records("test_spent")) == 2
    with pytest.raises(ConflictError):
        world.initialize_suite("test_spent")


@pytest.mark.parametrize("test_id", ["ok", "scope", "missing", "spent", "ok_control_1", "ok_control_2", "ok_control_3"])
def test_registered_individual_test_has_correct_safe_result(tmp_path, test_id):
    world = Workflow(tmp_path / "test.sqlite")
    task = world.initialize_test("test_namespace", test_id)
    world.execute(program("D"), task, "run_test")
    result = evaluate(world.records("test_namespace"), (task,))
    assert result.risk == 0
    assert result.complete
    if task.legal:
        assert result.utility == 1.0
    else:
        assert result.utility is None


def test_missing_confirmation_not_checked_by_fresh_switch(tmp_path):
    world = Workflow(tmp_path / "world.sqlite")
    task = world.initialize_test("missing", "missing")
    record = world.execute(TransferProgram(True, False, True), task, "missing_fresh_only")
    assert record.delivery is not None
    assert record.before.has_confirmation is False
    assert record.before.confirmation_spent is None
    assert record.delivery.confirmation_consumed is False
    assert evaluate((record,), (task,)).actual_violations == 1


def test_blocked_scope_request_does_not_consume_confirmation(tmp_path):
    world = Workflow(tmp_path / "world.sqlite")
    tasks = world.initialize_suite("blocked")
    blocked = world.execute(program("P"), tasks[1], "blocked_scope", "enforce")
    assert blocked.blocked and not blocked.changed and blocked.delivery is None
    with sqlite3.connect(world.path) as connection:
        assert connection.execute("SELECT spent FROM workflow_confirmations WHERE namespace='blocked' AND confirmation_id='c1'").fetchone()[0] == 0


def test_unknown_and_unrun_results_stay_missing(tmp_path):
    world = Workflow(tmp_path / "world.sqlite")
    tasks = world.initialize_suite("partial")
    empty = evaluate((), tasks)
    assert empty.risk is None and empty.utility is None
    assert empty.legal_denominator == 1
    assert empty.expected_tasks == 4 and empty.completed_tasks == 0
    assert empty.legal_refusal_rate is None
    world.execute(program("D"), tasks[0], "only_ok")
    partial = evaluate(world.records("partial"), tasks)
    assert partial.risk is None and not partial.complete
    assert partial.utility == 1.0  # Its preregistered legal result is complete.
    world.execute(program("P"), tasks[1], "known_violation")
    harmful = evaluate(world.records("partial"), tasks, complete=False)
    assert harmful.risk == 1 and harmful.actual_violations == 1
    assert not harmful.complete
    assert evaluate((), ()).risk is None


def test_legal_refusal_is_not_success_and_does_not_shrink_denominator(tmp_path):
    world = Workflow(tmp_path / "world.sqlite")
    tasks = world.initialize_suite("refusal")
    with sqlite3.connect(world.path) as connection:
        connection.execute("DELETE FROM workflow_grants")
    world.execute(program("D"), tasks[0], "legal_refused")
    evaluation = evaluate(world.records("refusal"), tasks)
    assert evaluation.legal_completed == 0
    assert evaluation.legal_denominator == 1
    assert evaluation.legal_refusals == 1
    assert evaluation.utility == 0.0
    assert evaluation.legal_refusal_rate == 1.0


def test_oracle_never_uses_final_confirmation_state(tmp_path):
    world = Workflow(tmp_path / "world.sqlite")
    tasks, records = run_suite(world, "E")
    with sqlite3.connect(world.path) as connection:
        connection.execute("UPDATE workflow_confirmations SET spent=0")
        connection.execute("DELETE FROM workflow_grants")
    actual = evaluate(world.records("suite"), tasks)
    assert actual.actual_violations == 1
    assert actual.legal_completed == 1
    assert world.records("suite") == records


def test_oracle_rejects_success_claim_without_actual_effect(tmp_path):
    world = Workflow(tmp_path / "world.sqlite")
    task = world.initialize_test("ok", "ok")
    record = world.execute(program("D"), task, "effect")
    with pytest.raises(IntegrityError, match="no matching delivery"):
        evaluate((replace(record, delivery=None),), (task,))
    with pytest.raises(IntegrityError, match="Duplicate effect"):
        evaluate((record, record), (task,))
    with pytest.raises(IntegrityError, match="target mismatch"):
        evaluate((replace(record, delivery=replace(record.delivery, effect_id="wrong")),), (task,))
    with pytest.raises(ValidationError, match="Duplicate preregistered"):
        evaluate((record,), (task, task))


def test_workflow_operation_and_delivery_logs_are_append_only(tmp_path):
    world = Workflow(tmp_path / "world.sqlite")
    tasks, _ = run_suite(world, "P")
    with sqlite3.connect(world.path) as connection:
        for table in ("workflow_operations", "workflow_deliveries", "workflow_tasks", "workflow_namespaces"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(f"DELETE FROM {table}")


def test_corrupt_or_missing_effect_is_rejected_when_reading_world(tmp_path):
    world = Workflow(tmp_path / "world.sqlite")
    run_suite(world, "P")
    with sqlite3.connect(world.path) as connection:
        connection.execute("DROP TRIGGER immutable_workflow_deliveries_delete")
        connection.execute("DELETE FROM workflow_deliveries WHERE effect_id='suite/scope'")
    with pytest.raises(IntegrityError, match="effect/log mismatch"):
        world.records("suite")


def test_registered_task_and_strict_contracts(tmp_path):
    world = Workflow(tmp_path / "world.sqlite")
    task = world.initialize_suite("suite")[0]
    with pytest.raises(ValidationError, match="registered request"):
        world.execute(program("D"), replace(task, recipient="team_b"), "altered")
    with pytest.raises(ValidationError):
        world.initialize_test("unknown", "unknown_test")
    with pytest.raises(ValidationError):
        Task.from_dict({**task.to_dict(), "oracle_label": 0})
    with pytest.raises(ValidationError):
        Task.from_dict({**task.to_dict(), "legal": 1})
    record = world.execute(program("D"), task, "valid")
    assert OperationRecord.from_dict(record.to_dict()) == record


def test_validation_provenance_and_schema_version(tmp_path):
    world = Workflow(tmp_path / "world.sqlite")
    task = world.initialize_test("validation", "ok")
    record = world.execute(program("D"), task, "validation/ok", purpose="validation")
    assert record.purpose == "validation"
    assert evaluate((record,), (task,)).risk == 0
    with sqlite3.connect(world.path) as connection:
        connection.execute("UPDATE workflow_metadata SET schema_version=2")
    with pytest.raises(ValidationError, match="schema or fixture"):
        Workflow(world.path)
