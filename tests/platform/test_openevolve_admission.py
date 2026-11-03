from copy import deepcopy
from pathlib import Path
import random

import pytest
pytest.importorskip("openevolve")

from rsi_lab.contracts import Partition
from rsi_lab.engine.continuation import read_record
from rsi_lab.engine.platform import PublicTaskEvaluator, prepare_update
from rsi_lab.errors import IntegrityError
from rsi_lab.infra.chat_models import ScriptedChatProvider
from rsi_lab.serialization import canonical_bytes, decode_json, digest
from rsi_lab.studies.openevolve_admission import analyze_proposal
from rsi_lab.subjects.openevolve import OpenEvolveAdapter, initial_snapshot
from rsi_lab.tasks.cost_authorization import API_CONTRACT, FOUNDER_SOURCE, SAFE_REFERENCE_SOURCE
from rsi_lab.tasks.platform_cost_authorization import CostAuthorizationTaskPackage


def recorded(tmp_path, source):
    provider = ScriptedChatProvider(tmp_path / "model.sqlite", lambda *_:
        {"response": "```python\n" + source + "\n```"})
    base = initial_snapshot(13101, FOUNDER_SOURCE.strip(), "cost-authorization-program-v1", API_CONTRACT)
    adapter = OpenEvolveAdapter(tmp_path / "system", provider, base)
    evaluator = PublicTaskEvaluator(adapter, CostAuthorizationTaskPackage(), tmp_path / "public", seed=13101)
    draft = adapter.propose("proposal", evaluator)
    prepared = prepare_update(adapter, draft, base, update_id="proposal", root_id="root",
        protocol_sha256=digest("protocol"), environment_sha256=digest("environment"),
        partition=Partition.DEV, horizon=3, task_plan_sha256=digest("tasks"))
    path = tmp_path / "system" / "proposals" / (digest("proposal") + ".json")
    return decode_json(canonical_bytes(prepared)), read_record(path.with_suffix(".plan.json")), read_record(path)


def test_zero_score_candidate_is_admitted_without_changing_active_source(tmp_path, monkeypatch):
    values = recorded(tmp_path, "def handle(request, cache, api):\n    api.execute(request)\n    return cache")
    def forbidden(*args, **kwargs):
        pytest.fail("offline replay must not dispatch a model or evaluate a candidate")
    monkeypatch.setattr(ScriptedChatProvider, "generate", forbidden)
    monkeypatch.setattr(PublicTaskEvaluator, "evaluate", forbidden)
    rng = random.getstate()
    row = analyze_proposal(*values)
    assert random.getstate() == rng
    assert row["original_reproduced"]
    assert row["candidate_metrics"] == {"combined_score": 0.0}
    assert row["current"]["native_database_admitted"] is None
    assert not row["current"]["system_state_changed"]
    assert row["upstream"]["native_database_admitted"] is True
    assert row["upstream"]["archive_only_system_update"]
    assert not row["upstream"]["active_source_changed"]
    assert row["upstream"]["sampling"]["parent_eligible"]
    assert row["upstream"]["sampling"]["witnesses"]["parent"]
    assert row["upstream"]["sampling"]["witnesses"]["inspiration"]
    assert row["normalized_child"]["code"] == decode_json(values[0]["draft"]["native_evidence_json"])["proposed_source"]
    # Both branches restore the same before-state; current does not inherit rescoring.
    assert row["current"]["population_after_native"] == row["population_before"]
    assert row["current"]["inherited_population"] is None


def test_successful_historical_snapshot_is_reproduced_exactly(tmp_path):
    row = analyze_proposal(*recorded(tmp_path, SAFE_REFERENCE_SOURCE))
    assert row["original_reproduced"]
    assert row["current"]["candidate_committable"]
    assert not row["conditions_differ"]
    assert row["current"]["candidate_snapshot_sha256"] == row["upstream"]["candidate_snapshot_sha256"]


def test_identical_rewrite_is_never_submitted_to_either_database(tmp_path):
    row = analyze_proposal(*recorded(tmp_path, FOUNDER_SOURCE.strip()))
    assert row["native_error"] == "Rewrite is identical to the parent program"
    assert row["normalized_child"] is None
    for arm in ("current", "upstream"):
        assert not row[arm]["native_database_called"]
        assert not row[arm]["system_state_changed"]
        assert not row[arm]["sampling"]["parent_eligible"]


def test_conflicting_or_incomplete_checkpoint_fails_closed(tmp_path):
    prepared, plan, proposal = recorded(tmp_path, SAFE_REFERENCE_SOURCE)
    changed = deepcopy(plan)
    changed["population"]["last_iteration"] += 1
    with pytest.raises(IntegrityError, match="pre-admission inputs"):
        analyze_proposal(prepared, changed, proposal)
    changed = deepcopy(plan)
    changed["base_sha256"] = digest("another checkpoint")
    with pytest.raises(IntegrityError, match="different base"):
        analyze_proposal(prepared, changed, proposal)
    changed = deepcopy(plan)
    changed["rng"] = random.Random(0).getstate()
    # A wrong post-prompt RNG changes the full inherited snapshot, even if db admission agrees.
    with pytest.raises(IntegrityError, match="not reproduced"):
        analyze_proposal(prepared, changed, proposal)


def test_public_record_binding_rejects_changed_plan_and_scores(tmp_path):
    from rsi_lab.studies.openevolve_admission import _bind_public_records
    prepared, plan, _ = recorded(tmp_path, SAFE_REFERENCE_SOURCE)
    evidence = decode_json(prepared["draft"]["native_evidence_json"])
    _bind_public_records(prepared, plan, evidence, tmp_path / "public", read_record)
    def changed_request(path):
        value = read_record(path)
        if path.name == "request.json":
            value["plan"] = {"replaced": True}
        return value
    with pytest.raises(IntegrityError, match="different source, plan or operation"):
        _bind_public_records(prepared, plan, evidence, tmp_path / "public", changed_request)
    def changed_evaluation(path):
        value = read_record(path)
        if path.name == "evaluation.json":
            value["synthetic_units"] += 1
        return value
    with pytest.raises(IntegrityError, match="Archive score or artifact"):
        _bind_public_records(prepared, plan, evidence, tmp_path / "public", changed_evaluation)


def test_missing_prepared_record_cannot_silently_shrink_denominator(tmp_path):
    from shutil import copyfile
    from rsi_lab.engine.continuation import save_record
    from rsi_lab.studies.openevolve_admission import review
    run = tmp_path / "run"
    root = run / "roots" / "root"
    prepared, _, _ = recorded(root, SAFE_REFERENCE_SOURCE)
    prepared_path = root / "search-1" / "prepared.json"
    save_record(prepared_path, prepared)
    copyfile(root / "model.sqlite", run / "model.sqlite")
    summary = {"protocol": "openevolve-suitability-v1", "status": "complete",
               "usage": {"complete_responses": 1, "unknown_responses": 0}}
    save_record(run / "summary.json", summary)
    save_record(run / "verification.json", {"verified": True, "summary_sha256": digest(summary)})
    # Keep the completed model response and durable proposal, remove only prepared.
    prepared_path.chmod(0o600)
    prepared_path.unlink()
    with pytest.raises(IntegrityError, match="inventory mismatch.*prepared=0"):
        review(run)


def test_corrupted_proposal_is_explicitly_unanalyzable(tmp_path):
    from shutil import copyfile
    from rsi_lab.engine.continuation import save_record
    from rsi_lab.studies.openevolve_admission import review
    run = tmp_path / "run"
    root = run / "roots" / "root"
    prepared, _, _ = recorded(root, SAFE_REFERENCE_SOURCE)
    save_record(root / "search-1" / "prepared.json", prepared)
    copyfile(root / "model.sqlite", run / "model.sqlite")
    summary = {"protocol": "openevolve-suitability-v1", "status": "complete",
               "usage": {"complete_responses": 1, "unknown_responses": 0}}
    save_record(run / "summary.json", summary)
    save_record(run / "verification.json", {"verified": True, "summary_sha256": digest(summary)})
    proposal_path = root / "system" / "proposals" / (digest("proposal") + ".json")
    proposal_path.chmod(0o600)
    proposal_path.write_text('{"record": {}, "sha256": "corrupted"}')
    result = review(run)
    assert result["prepared_records"] == 1
    assert result["counts"]["unanalyzable"] == 1
    assert result["proposal_load_errors"][0]["path"].endswith(proposal_path.name)
    assert not result["inventory"]["request_ids_reconciled"]
    assert not result["continuation_decision"]["eligible_checkpoints"]
