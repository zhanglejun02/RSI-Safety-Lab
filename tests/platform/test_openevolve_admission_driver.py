"""The study's platform condition must also govern real continuation commits."""
import pytest

pytest.importorskip("openevolve")

from rsi_lab.contracts import CursorFamily, Partition
from rsi_lab.engine.continuation import read_record
from rsi_lab.engine.platform import continue_system
from rsi_lab.errors import ConflictError
from rsi_lab.infra.chat_models import ScriptedChatProvider
from rsi_lab.serialization import decode_json, digest
from rsi_lab.studies.openevolve_admission_driver import continue_admission_system
from rsi_lab.subjects.openevolve import OpenEvolveAdapter, initial_snapshot
from rsi_lab.tasks.cost_authorization import API_CONTRACT, FOUNDER_SOURCE
from rsi_lab.tasks.platform_cost_authorization import CostAuthorizationTaskPackage


@pytest.mark.parametrize("condition,accepted", [("upstream", True), ("public_complete_correct", False)])
def test_continuation_uses_platform_decision_and_replays_without_dispatch(tmp_path, condition, accepted):
    provider = ScriptedChatProvider(tmp_path / "model.sqlite", lambda *_: {
        "response": "```python\ndef handle(request, cache, api):\n    return cache\n```"})
    package = CostAuthorizationTaskPackage()
    base = initial_snapshot(13101, FOUNDER_SOURCE, package.tool_protocol, API_CONTRACT,
                            admission_condition=condition)
    adapter = OpenEvolveAdapter(tmp_path / "system", provider, base)
    source_environment = package.create_environment(tmp_path / "origin").snapshot()
    options = dict(seed=13101, family=CursorFamily.INDEPENDENT_EVALUATION,
        partition=Partition.DEV, horizon=1, schedule="mixed", root_id="root",
        protocol_sha256=digest({"condition": condition}),
        arm="old" if condition == "upstream" else "new", stream="independent")
    result = continue_admission_system(adapter, package, source_environment, tmp_path / "branch", **options)
    row = read_record(tmp_path / "branch" / "attempt-0001.json")
    assert row["missing_reason"] is None
    assert row["prepared"]["draft"]["native_valid"] is True
    assert row["decision"]["disposition"] == ("accept" if accepted else "reject")
    assert (row["activation"] is not None) == accepted
    assert (adapter.snapshot() != base) == accepted
    # The zero-score child can alter inheritance while the executor stays fixed.
    assert adapter.snapshot().version == base.version
    if accepted:
        assert len(decode_json(adapter.snapshot().state_json)["population"]["programs"]) == 2
    else:
        assert decode_json(adapter.snapshot().state_json)["population"] is None
    provider.callback = lambda *_: pytest.fail("Recovery must not generate a new descendant")
    assert continue_admission_system(adapter, package, source_environment, tmp_path / "branch", **options) == result
    assert provider.summary()["dispatched_calls"] == 1
    with pytest.raises(ConflictError, match="Immutable continuation record changed"):
        continue_system(adapter, package, source_environment, tmp_path / "branch", **options)
