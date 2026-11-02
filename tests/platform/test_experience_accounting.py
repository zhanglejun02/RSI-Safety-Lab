"""Direct proposal/activation work has its own receipts, including unknowns."""
from dataclasses import replace
from pathlib import Path

import pytest

from rsi_lab.api import PreparedUpdate
from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.errors import IntegrityError, OutcomeUnknown
from rsi_lab.infra.chat_models import ChatMessage, DurableChatProvider, ScriptedChatProvider
from rsi_lab.serialization import digest
from rsi_lab.studies import experience_diagnostic as study
from rsi_lab.studies.experience_accounting import accounting_correction, experience_work


@pytest.fixture(scope="module")
def control(tmp_path_factory):
    output = tmp_path_factory.mktemp("direct-work") / "run"
    config = study.ExperienceDiagnosticConfig(blocks=(19101,), repeats=2, history_seed=19001, parallel_jobs=1)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(study, "implementation_fingerprint", lambda: "9" * 64)
        summary = study.create_run(config, output)
    return output, summary


def test_counts_real_direct_edits_and_confirmed_activations(control, monkeypatch):
    output, summary = control
    def forbidden(*args, **kwargs):
        pytest.fail("Accounting must not generate a model response")
    monkeypatch.setattr(DurableChatProvider, "generate", forbidden)
    monkeypatch.setattr(ScriptedChatProvider, "generate", forbidden)
    before = {name: (output / name).read_bytes() for name in ("summary.json", "report.md")}
    result = experience_work(output, summary["rows"])
    assert summary["total_task_work"] == result
    assert all("attempts" not in row["work"] and "native_acceptances" not in row["work"]
               for row in summary["rows"])
    diagnostic, history = result["diagnostic"], result["history"]
    assert diagnostic["registered_slots"] == diagnostic["observed_slots"] == 12
    assert diagnostic["native_acceptances"] == diagnostic["confirmed_activations"] == 12
    assert diagnostic["activated_source_changes"] == diagnostic["executable_proposals"] == 12
    assert diagnostic["complete_candidate_evaluations"] == diagnostic["complete_adopted_evaluations"] == 12
    assert diagnostic["editor_requests"]["dispatched_requests"] == 12
    assert diagnostic["editor_requests"]["scripted_control_requests"] == 12
    assert diagnostic["editor_requests"]["model_requests"] == 0
    assert history["editor_requests"]["dispatched_requests"] == history["returned_executable_edits"] == 2
    assert history["verified_repairs"] == 1 and history["verified_cost_improvements"] >= 1
    assert all(key not in result["execution"] for key in
               ("attempts", "completed_attempts", "native_acceptances", "actual_source_changes"))
    assert result["execution"]["executed_requests"] > 0
    correction = accounting_correction(output)
    assert correction["original_summary_sha256"] == digest(summary)
    assert correction["work"] == result
    assert all((output / name).read_bytes() == data for name, data in before.items())


def test_unknown_dispatch_is_counted_once_without_inventing_effects(tmp_path):
    job = {"block": 19102, "mode": "full", "arm": "relevant", "replicate": 0}
    location = tmp_path / "diagnostic/19102/full/relevant/0"
    save_record(tmp_path / "manifest.json", {"submission_order": [job]})
    def unknown(*args):
        raise TimeoutError("test dispatch outcome is unknown")
    provider = ScriptedChatProvider(location / "editor.sqlite", unknown, max_calls=1)
    with pytest.raises(OutcomeUnknown):
        provider.generate("request-unknown", (ChatMessage("user", "{}"),))
    save_record(location / "proposal-failure.json", {"error": "OutcomeUnknown", "retry": False})
    result = experience_work(tmp_path, [job])
    diagnostic = result["diagnostic"]
    assert diagnostic["editor_requests"]["dispatched_requests"] == 1
    assert diagnostic["editor_requests"]["unknown_responses"] == 1
    assert diagnostic["prepared_proposals"] == diagnostic["native_rejections"] == 0
    assert diagnostic["missing_candidate_evaluations"] == diagnostic["missing_adopted_evaluations"] == 1
    assert diagnostic["complete_candidate_evaluations"] == diagnostic["complete_adopted_evaluations"] == 0
    assert result["history"]["collection_available"] is None
    assert result["history"]["verified_repairs"] is None
    assert experience_work(tmp_path, [job]) == result


def test_accepting_unchanged_source_does_not_count_as_source_change(control, tmp_path):
    output, summary = control
    row = summary["rows"][0]
    relative = Path("diagnostic") / str(row["block"]) / row["mode"] / row["arm"] / str(row["replicate"])
    original = PreparedUpdate.from_dict(read_record(output / relative / "prepared.json"))
    draft = replace(original.draft, candidate=original.base)
    prepared = replace(original, identity=replace(original.identity, candidate_sha256=original.base.sha256), draft=draft)
    saved = read_record(output / relative / "activation.json")
    saved["decision"]["identity"] = prepared.identity.to_dict()
    saved["activation"]["after_sha256"] = prepared.base.sha256
    save_record(tmp_path / "manifest.json", {"submission_order": [row]})
    save_record(tmp_path / relative / "prepared.json", prepared)
    save_record(tmp_path / relative / "activation.json", saved)
    save_record(tmp_path / relative / "native-frozen.json", {"selected_snapshot_sha256": prepared.base.sha256})
    diagnostic = experience_work(tmp_path, [row])["diagnostic"]
    assert diagnostic["native_acceptances"] == diagnostic["confirmed_activations"] == 1
    assert diagnostic["activated_source_changes"] == 0


def test_missing_activation_receipt_is_not_confirmed(control, tmp_path):
    output, summary = control
    row = summary["rows"][0]
    relative = Path("diagnostic") / str(row["block"]) / row["mode"] / row["arm"] / str(row["replicate"])
    save_record(tmp_path / "manifest.json", {"submission_order": [row]})
    save_record(tmp_path / relative / "prepared.json", read_record(output / relative / "prepared.json"))
    diagnostic = experience_work(tmp_path, [row])["diagnostic"]
    assert diagnostic["native_acceptances"] == diagnostic["unknown_activation_outcomes"] == 1
    assert diagnostic["confirmed_activations"] == diagnostic["activated_source_changes"] == 0


def test_duplicate_reporting_slots_rejected_instead_of_double_charged(control):
    output, summary = control
    with pytest.raises(IntegrityError, match="duplicate"):
        experience_work(output, [summary["rows"][0], summary["rows"][0]])
