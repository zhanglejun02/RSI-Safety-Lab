"""Legacy Task A uses the platform's durable loop without weakening evidence."""

from dataclasses import replace

import pytest

from rsi_lab.engine.continuation import ContinuationService, read_record
from rsi_lab.errors import ConflictError
from rsi_lab.infra.chat_models import ScriptedChatProvider
from rsi_lab.studies import task_a
from rsi_lab.tasks.authorization import AgentProgram, AuthorizationWorld


def test_task_a_uses_common_continuation_and_restores_completed_attempts(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    config = task_a.TaskAConfig(roots=1, seed=101)
    source = AuthorizationWorld(root / "source.sqlite").snapshot()
    requests = []

    def respond(request_id, messages):
        requests.append(request_id)
        return task_a._control_response(request_id, messages)

    provider = ScriptedChatProvider(root / "provider.sqlite", respond)
    selector = task_a.ExperienceSelector(("e2", "e1", "e0", "e3"))
    calls = []
    original = ContinuationService.run

    def run(service, driver, horizon, directory, *, identity):
        calls.append(identity["stream"])
        return original(service, driver, horizon, directory, identity=identity)

    monkeypatch.setattr(ContinuationService, "run", run)
    arguments = (root, config, provider, source, selector, task_a._memory(True), AgentProgram(), [])
    keywords = dict(stream="independent", replicate=0, arm="new", seed=50101, horizon=4)
    first = task_a._continuation(*arguments, **keywords)
    directory = root / "independent" / "0" / "new"
    assert [row.risk for row in first[0]] == [0, 0, 1, 1]
    assert read_record(directory / "attempt-0003.json")["outcome"]["risk"] == 1
    before = AuthorizationWorld(directory / "environment.sqlite").snapshot()
    assert len(requests) == 4
    assert task_a._continuation(*arguments, **keywords) == first
    assert calls == ["independent", "independent"]
    assert len(requests) == 4
    assert AuthorizationWorld(directory / "environment.sqlite").snapshot() == before
    # A changed update must not replay the previous continuation under its identity.
    with pytest.raises(ConflictError, match="Immutable continuation"):
        task_a._continuation(*arguments[:4], replace(selector, limit=1), *arguments[5:], **keywords)
