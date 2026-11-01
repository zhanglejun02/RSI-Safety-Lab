"""Recovery uses durable activation provenance without relaxing live-state guards."""
from dataclasses import replace

import pytest

from rsi_lab.contracts import CursorFamily, Partition
from rsi_lab.engine.activation import ActivationController
from rsi_lab.engine.platform import continue_system, execute_step, PlatformContinuationDriver
from rsi_lab.errors import ConflictError, IntegrityError
from rsi_lab.serialization import canonical_bytes
from rsi_lab.subjects.platform_fixture import FixtureSystem
from rsi_lab.tasks.platform_authorization import AuthorizationTaskPackage


def config():
    return dict(seed=32, family=CursorFamily.INDEPENDENT_EVALUATION, partition=Partition.DEV,
                horizon=2, schedule="delayed", root_id="root", protocol_sha256="d" * 64,
                arm="new", stream="independent")


def setup(tmp_path):
    pack = AuthorizationTaskPackage()
    directory = tmp_path / "continuation"
    subject = FixtureSystem(directory / "system")
    source = pack.create_environment(tmp_path / "source").snapshot()
    return pack, directory, subject, source


def test_ready_receipt_recovers_after_task_effect_without_reactivating(tmp_path, monkeypatch):
    pack, directory, subject, source = setup(tmp_path)
    original_record = PlatformContinuationDriver._record
    original_activate = FixtureSystem.activate
    activated = []
    crash = [True]

    def activate(adapter, command):
        activated.append(command.operation_id)
        return original_activate(adapter, command)

    def lose_result(driver, index, reason, **kwargs):
        if index == 1 and reason is None and crash[0]:
            crash[0] = False
            raise RuntimeError("process loss after task effect before attempt checkpoint")
        return original_record(driver, index, reason, **kwargs)

    monkeypatch.setattr(FixtureSystem, "activate", activate)
    monkeypatch.setattr(PlatformContinuationDriver, "_record", lose_result)
    with pytest.raises(RuntimeError):
        continue_system(subject, pack, source, directory, **config())
    assert (directory / "ready-0001.json").exists()
    assert not (directory / "attempt-0001.json").exists()
    assert len(activated) == 1
    before = pack.create_environment(directory / "environment", source).snapshot()
    assert before.records
    rows, cost = continue_system(subject, pack, source, directory, **config())
    assert all(row.complete for row in rows)
    assert len(activated) == 2 and len(set(activated)) == 2
    after = pack.create_environment(directory / "environment", source).snapshot()
    assert after.records[:len(before.records)] == before.records
    assert len(after.records) == 2
    assert continue_system(subject, pack, source, directory, **config()) == (rows, cost)
    assert len(activated) == 2


def test_stale_live_environment_blocks_prepared_update_on_resume(tmp_path, monkeypatch):
    pack, directory, subject, source = setup(tmp_path)
    original = ActivationController.apply

    def crash(*args, **kwargs):
        raise RuntimeError("process loss before dispatch")

    monkeypatch.setattr(ActivationController, "apply", crash)
    with pytest.raises(RuntimeError):
        continue_system(subject, pack, source, directory, **config())
    assert (directory / "prepared-0001.json").exists()
    assert not (directory / "ready-0001.json").exists()
    base = subject.snapshot()
    # An external state change must invalidate the already prepared audit binding.
    env = pack.create_environment(directory / "environment", source)
    env.prepare(pack.plan(999, horizon=1).rounds[0])
    assert env.snapshot() != source
    monkeypatch.setattr(ActivationController, "apply", original)
    with pytest.raises(ConflictError, match="environment changed"):
        continue_system(subject, pack, source, directory, **config())
    assert subject.snapshot() == base
    assert not (directory / "ready-0001.json").exists()


def test_execution_cannot_mutate_inherited_state_without_changing_version(tmp_path, monkeypatch):
    pack, directory, subject, source = setup(tmp_path)
    env = pack.create_environment(directory / "environment", source)
    original = FixtureSystem.run_task

    def mutate(adapter, task, tools, operation):
        receipt = original(adapter, task, tools, operation)
        changed = replace(adapter.snapshot(), state_json='{"memory":"unapproved"}')
        with adapter._db() as db:
            db.execute("UPDATE system_state SET body=? WHERE id=1", (canonical_bytes(changed),))
        return receipt

    monkeypatch.setattr(FixtureSystem, "run_task", mutate)
    with pytest.raises(IntegrityError, match="inherited state"):
        execute_step(subject, env, pack.plan(71, horizon=1).rounds[0], "execute")


def test_completed_continuation_verifies_real_final_state_and_source(tmp_path):
    pack, directory, subject, source = setup(tmp_path)
    continue_system(subject, pack, source, directory, **config())
    other = pack.create_environment(tmp_path / "other")
    other.prepare(pack.plan(999, horizon=1).rounds[0])
    with pytest.raises(ConflictError, match="source environment"):
        continue_system(subject, pack, other.snapshot(), directory, **config())
    changed = replace(subject.snapshot(), state_json='{"memory":"unapproved"}')
    with subject._db() as db:
        db.execute("UPDATE system_state SET body=? WHERE id=1", (canonical_bytes(changed),))
    with pytest.raises(IntegrityError, match="final active state"):
        continue_system(subject, pack, source, directory, **config())
