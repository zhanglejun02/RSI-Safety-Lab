from dataclasses import replace

import pytest

from rsi_lab.errors import BudgetExceeded, ConflictError, ValidationError
from rsi_lab.serialization import decode_json
from rsi_lab.tasks.platform_authorization import AuthorizationTaskPackage


def perform(env, round_plan, namespace, *, strict):
    env.prepare(round_plan)
    for task in env.public_tasks(round_plan):
        tools = env.tools(task.task_id, namespace)
        facts = tools.call('query_authorization', {}, 'query')
        permitted = facts['grant_active'] and facts['confirmation_exists']
        if strict:
            permitted = permitted and facts['confirmation_matches'] and not facts['confirmation_spent'] and not facts['confirmation_revoked']
        tools.call('transfer' if permitted else 'refuse', {}, 'action')
    return env.evaluate(round_plan)


def test_tools_actual_effects_are_independent_of_policy_and_legality(tmp_path):
    pack = AuthorizationTaskPackage()
    plan = pack.plan(89, horizon=6)
    old = pack.create_environment(tmp_path / 'old')
    new = pack.create_environment(tmp_path / 'new')
    safe = [perform(old, r, f'r{r.attempt_index}', strict=True) for r in plan.rounds]
    unsafe = [perform(new, r, f'r{r.attempt_index}', strict=False) for r in plan.rounds]
    assert [x.risk for x in safe] == [0] * 6
    assert [x.risk for x in unsafe] == [0, 0, 1, 1, 1, 1]
    assert all(x.utility == 1.0 for x in safe + unsafe)
    assert [old.synthetic_cost(r) for r in plan.rounds] == [new.synthetic_cost(r) for r in plan.rounds]
    assert all('legal' not in decode_json(t.request_json) for r in plan.rounds for t in new.public_tasks(r))
    # Changing the utility annotation cannot change actual authorization facts.
    alternate = replace(plan.rounds[-1], tasks=tuple(replace(t, legal=not t.legal) for t in plan.rounds[-1].tasks))
    with pytest.raises(ConflictError):
        new.prepare(alternate)


def test_query_replay_and_effect_replay_after_resume(tmp_path):
    pack = AuthorizationTaskPackage()
    plan = pack.plan(2, horizon=1)
    env = pack.create_environment(tmp_path)
    env.prepare(plan.rounds[0])
    task = env.public_tasks(plan.rounds[0])[0]
    tools = env.tools(task.task_id, 'task')
    before = tools.call('query_authorization', {}, 'query')
    assert before['confirmation_spent'] is False
    effect = tools.call('transfer', {}, 'action')
    env = pack.create_environment(tmp_path)
    env.prepare(plan.rounds[0])
    tools = env.tools(task.task_id, 'task')
    assert tools.call('query_authorization', {}, 'query') == before
    assert tools.call('transfer', {}, 'action') == effect
    assert len(env.traces(plan.rounds[0])) == 1
    with pytest.raises(ConflictError):
        tools.call('refuse', {}, 'action')
    with pytest.raises(ConflictError):
        tools.call('transfer', {}, 'another_action')


def test_branch_isolation_and_budget(tmp_path):
    pack = AuthorizationTaskPackage(max_tool_calls=1)
    plan = pack.plan(3, horizon=1)
    env = pack.create_environment(tmp_path / 'parent')
    env.prepare(plan.rounds[0])
    before = env.snapshot()
    child = pack.create_environment(tmp_path / 'child', before)
    child.prepare(plan.rounds[0])
    task = child.public_tasks(plan.rounds[0])[0]
    tools = child.tools(task.task_id, 'task')
    tools.call('query_authorization', {}, 'query')
    with pytest.raises(BudgetExceeded):
        tools.call('transfer', {}, 'action')
    assert env.snapshot() == before
    assert child.evaluate(plan.rounds[0]).risk is None
    with pytest.raises(ValidationError):
        tools.call('transfer', {'recipient': 'other'}, 'bad')


def test_enforcement_and_incomplete_harm(tmp_path):
    pack = AuthorizationTaskPackage(guard_mode='enforce')
    plan = pack.plan(3, horizon=3)
    env = pack.create_environment(tmp_path)
    results = [perform(env, r, f'r{r.attempt_index}', strict=False) for r in plan.rounds]
    assert results[-1].risk == 0 and results[-1].blocked_violations == 1
    assert env.evaluate(plan.rounds[-1], complete=False).risk is None


def test_crash_after_effect_retains_intent_and_replays_same_action(tmp_path, monkeypatch):
    pack = AuthorizationTaskPackage()
    plan = pack.plan(4, horizon=1)
    env = pack.create_environment(tmp_path)
    env.prepare(plan.rounds[0])
    task = env.public_tasks(plan.rounds[0])[0]
    tools = env.tools(task.task_id, 'task')
    real = env._world.act
    def after_effect(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError('crash after persisted effect')
    monkeypatch.setattr(env._world, 'act', after_effect)
    with pytest.raises(RuntimeError):
        tools.call('transfer', {}, 'action')
    with pytest.raises(ConflictError):
        tools.call('refuse', {}, 'other')
    monkeypatch.setattr(env._world, 'act', real)
    assert tools.call('transfer', {}, 'action')['changed']
    assert len(env.traces(plan.rounds[0])) == 1


def test_environment_reopen_requires_original_source_and_configuration(tmp_path):
    pack = AuthorizationTaskPackage()
    plan = pack.plan(5, horizon=1)
    original = pack.create_environment(tmp_path / 'source')
    original.prepare(plan.rounds[0])
    source = original.snapshot()
    branch = pack.create_environment(tmp_path / 'branch', source)
    assert pack.create_environment(tmp_path / 'branch', source).snapshot() == branch.snapshot()
    with pytest.raises(ConflictError):
        pack.create_environment(tmp_path / 'branch')
    with pytest.raises(ConflictError):
        AuthorizationTaskPackage(guard_mode='enforce').create_environment(tmp_path / 'branch', source)
