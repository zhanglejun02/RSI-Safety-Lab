from dataclasses import replace
from pathlib import Path

import pytest

from rsi_lab.engine.continuation import read_record
from rsi_lab.errors import ConflictError, OutcomeUnknown, ValidationError
from rsi_lab.infra.chat_models import ScriptedChatProvider
from rsi_lab.serialization import decode_json, digest
from rsi_lab.studies import public_q_search as study
from rsi_lab.subjects.cost_q import control_memory, control_selector, make_initial_snapshot
from rsi_lab.tasks.cost_authorization import FOUNDER_SOURCE, SAFE_REFERENCE_SOURCE
from rsi_lab.tasks.platform_cost_authorization import CostAuthorizationTaskPackage


def execute(directory, *, mode="improve", replicates=2):
    base = make_initial_snapshot(19501, control_memory(), control_selector("old"))
    selectors = {"old": control_selector("old"), "new": control_selector("harmful")}
    def provider_factory(candidate, rep):
        def reply(request_id, messages):
            assert (directory / "manifest.json").exists()
            assert (directory / "providers.json").exists()
            payload = decode_json(messages[-1].content)
            assert payload["operation"] == "improve_agent"
            if mode == "unknown" and candidate == "new" and rep == 0:
                raise OutcomeUnknown("simulated unconfirmed model response")
            source = SAFE_REFERENCE_SOURCE if mode == "tie" or candidate == "new" else FOUNDER_SOURCE
            return {"source": source}
        return ScriptedChatProvider(directory / "models" / candidate / f"{rep}.sqlite", reply,
                                    provider_id="public-q-test", max_calls=1)
    result = study.evaluate_public_q_candidates(base, selectors, baseline_id="old",
        tasks=CostAuthorizationTaskPackage(), provider_factory=provider_factory,
        directory=directory, root_id="root-public-search", seed=19501, replicates=replicates)
    return result, base


@pytest.fixture(scope="module")
def search_run(tmp_path_factory):
    directory = tmp_path_factory.mktemp("public-q-search")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(study, "implementation_fingerprint", lambda: "b" * 64)
        result, base = execute(directory)
        yield directory, result, base


def test_real_public_trial_activates_program_but_returns_only_q(search_run):
    directory, result, base = search_run
    assert result.selection.disposition == "selected"
    assert result.selection.selected_candidate_id == "new"
    assert result.candidate.version.component("policy") == base.version.component("policy")
    assert result.candidate.state_json == base.state_json
    assert result.candidate.version.component("experience") == base.version.component("experience")
    assert result.candidate.version.component("experience_selector") == control_selector("harmful").to_dict()
    assert len(result.trials) == 4
    assert sum(row.editor_calls for row in result.trials) == 4
    assert all(row.public_correct and row.complete for row in result.trials)
    for name in ("old", "new"):
        for rep in range(2):
            evidence = read_record(directory / "trials" / name / f"{rep:03d}" / "execution.json")
            assert bool(evidence["activation"]) == (name == "new")
            if name == "new":
                assert evidence["after"]["synthetic_units"] < evidence["before"]["synthetic_units"]
                assert evidence["active_snapshot"]["state_json"] != base.state_json
            assert evidence["actual_public_service_units"] >= evidence["after"]["synthetic_units"]
    assert not list(directory.rglob("label.json"))


def test_search_replays_without_model_calls_and_preserves_selection(search_run, monkeypatch):
    directory, result, _ = search_run
    before = (directory / "selection.json").read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail("immutable public search attempted a model call")
    monkeypatch.setattr(ScriptedChatProvider, "generate", forbidden)
    replay, _ = execute(directory)
    assert replay == result
    assert (directory / "selection.json").read_bytes() == before


def test_equal_real_optimizer_results_keep_baseline_and_trial_A_is_not_inherited(tmp_path, monkeypatch):
    monkeypatch.setattr(study, "implementation_fingerprint", lambda: "b" * 64)
    result, base = execute(tmp_path, mode="tie", replicates=1)
    assert result.selection.disposition == "retain_baseline"
    assert result.candidate == base
    assert all(row.successful_updates == 1 for row in result.trials)


def test_unknown_response_keeps_full_grid_and_never_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(study, "implementation_fingerprint", lambda: "b" * 64)
    result, base = execute(tmp_path, mode="unknown")
    assert result.selection.disposition == "inconclusive" and result.candidate == base
    assert len(result.trials) == 4
    missing = [row for row in result.trials if not row.complete]
    assert len(missing) == 1 and missing[0].candidate_id == "new" and missing[0].replicate == 0
    assert missing[0].after_cost is None
    assert sum(row.editor_calls for row in result.trials) == 4
    before = (tmp_path / "selection.json").read_bytes()
    # Test recovery even in the crash gap after failure publication but before
    # its redundant trial projection was persisted.
    (tmp_path / "trials/new/000/trial.json").unlink()
    monkeypatch.setattr(ScriptedChatProvider, "generate", lambda *a, **k: pytest.fail("unknown request retried"))
    replay, _ = execute(tmp_path, mode="improve")
    assert replay == result and (tmp_path / "selection.json").read_bytes() == before


def test_resume_rejects_source_change(search_run, monkeypatch):
    directory, _, _ = search_run
    monkeypatch.setattr(study, "implementation_fingerprint", lambda: "c" * 64)
    with pytest.raises(ConflictError):
        execute(directory)


def test_shared_journal_is_rejected_before_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(study, "implementation_fingerprint", lambda: "b" * 64)
    base = make_initial_snapshot(19502, control_memory(), control_selector("old"))
    provider = ScriptedChatProvider(tmp_path / "shared.sqlite", lambda *args: pytest.fail("must not dispatch"),
                                    max_calls=1)
    with pytest.raises(ValidationError, match="share"):
        study.evaluate_public_q_candidates(base, {"old": control_selector("old"), "new": control_selector("harmful")},
            baseline_id="old", tasks=CostAuthorizationTaskPackage(), provider_factory=lambda *args: provider,
            directory=tmp_path, root_id="root-public-search", seed=19502, replicates=1)
