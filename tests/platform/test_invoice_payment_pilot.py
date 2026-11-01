"""The financial migration keeps its evidence and model inputs separate."""
import importlib.util
from pathlib import Path
import sys

from rsi_lab.engine.continuation import save_record, read_record
from rsi_lab.infra.chat_models import ScriptedChatProvider
from rsi_lab.serialization import digest


def module():
    path = Path(__file__).parents[2] / "scripts/invoice_payment_pilot.py"
    spec = importlib.util.spec_from_file_location("invoice_pilot_test", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_cli_requires_completed_reference_for_natural_run(tmp_path, monkeypatch):
    runner = module()
    monkeypatch.setattr(sys, "argv", ["pilot", "--output", str(tmp_path / "trial")])
    assert runner.main() == 2
    assert not (tmp_path / "trial").exists()


def test_invoice_trial_uses_separate_records_and_never_sends_reference_labels(tmp_path, monkeypatch):
    reference = tmp_path / "reference"
    original = {"seed": 1, "history": []}
    save_record(reference / "roots/1/frozen.json", original)
    save_record(reference / "all-roots-frozen.json", {"1": digest(original)})
    save_record(reference / "summary.json", {"phase": "evaluated", "registered_roots": 1,
                                             "independent_secret": "never_send_reference_labels"})
    save_record(reference / "manifest.json", {"study": "control_reference"})
    runner = module().load_runner(reference)
    monkeypatch.setattr(runner, "ROOT_SEEDS", (9201,))
    monkeypatch.setattr(runner, "GENERATIONS", 1)

    def answer(key, messages):
        assert "never_send_reference_labels" not in messages[1].content
        assert "invoice" in messages[1].content and "receipt" in messages[1].content
        return {"source": runner.task.SAFE_REFERENCE_SOURCE}

    monkeypatch.setattr(runner, "_provider", lambda path: ScriptedChatProvider(path, answer,
        provider_id="control.invoice.editor", max_calls=2))
    output = tmp_path / "invoice"
    result = runner.run(output)
    assert result["positive_roots"] == 0
    assert result["root_summaries"][0]["replicated_selected_improvements"] == 1
    manifest = read_record(output / "manifest.json")
    assert manifest["study"] == "invoice-payment-cache-migration-kimi-v1"
    assert manifest["reference_authorization_study"]["summary_sha256"] == digest(read_record(reference / "summary.json"))
    assert "invoice_payment.py" in manifest["sources"]
    assert "invoice_payment_pilot.py" in manifest["sources"]
    assert manifest["plans"]["9201"]["routine"][0]["task_version"] == "invoice-payment-v1"
