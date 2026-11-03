from pathlib import Path

import pytest

from rsi_lab.engine.continuation import read_record
from rsi_lab.errors import ValidationError
from rsi_lab.infra.chat_models import ScriptedChatProvider
from rsi_lab.studies import stress_pilot as pilot


def configuration(tmp_path, horizon=2):
    template = Path("configs/stress_pilot_control.toml").read_text()
    template = template.replace('["authorization", "finance", "retail", "privacy"]', '["finance"]')
    template = template.replace('[2026110301, 2026110302]', '[2026110301]')
    template = template.replace('horizon = 3', f'horizon = {horizon}').replace('max_calls = 3', f'max_calls = {horizon}')
    path = tmp_path / "control.toml"
    path.write_text(template)
    return path


def test_two_native_systems_freeze_before_labels_and_resume_without_calls(tmp_path, monkeypatch):
    config, output = configuration(tmp_path), tmp_path / "run"
    manifest = pilot.run(config, output, analyze=False)
    assert manifest["model_call_cap"] == 4 and not (output / "analysis").exists()
    assert all(c["arm"] == "control" for c in pilot.OrdinaryPublicTasks("finance").public_suite(9).rounds[0].cases)
    for entry in manifest["root_ids"]:
        root = read_record(output / entry["path"] / "root.json")
        assert root["model_usage"]["dispatched_calls"] == 2
        assert root["public_execution_accounting"]["synthetic_units"] > 0
        slots = [read_record(output / entry["path"] / p) for p in root["slots"]]
        assert all(s["status"] == "complete" and len(s["candidate_sources"]) == 1 for s in slots)
        assert any(c["matched_in_request"] for c in slots[-1]["context_sources"])
        if entry["system"] == "openevolve":
            assert not slots[-1]["candidate_sources"][0]["native_admitted"]
            assert not slots[-1]["candidate_sources"][0]["platform_retained"]
    def forbidden(*args, **kwargs):
        raise AssertionError("Completed resume must not dispatch")
    monkeypatch.setattr(ScriptedChatProvider, "generate", forbidden)
    assert pilot.run(config, output, resume=True, analyze=False) == manifest
    from rsi_lab.studies.stress_pilot_analysis import analyze
    report = analyze(output)
    assert report["origin"] == "control" and report["ranking"] is None


def test_unknown_slots_are_not_retried_and_context_requests_survive(tmp_path, monkeypatch):
    def provider(path, kind, settings, system, module):
        def timeout(*_):
            raise TimeoutError("Simulated lost response")
        return ScriptedChatProvider(path, timeout, max_calls=settings.max_calls)
    monkeypatch.setattr(pilot, "_provider", provider)
    config, output = configuration(tmp_path), tmp_path / "unknown"
    manifest = pilot.run(config, output, analyze=False)
    for entry in manifest["root_ids"]:
        root = read_record(output / entry["path"] / "root.json")
        assert root["model_usage"]["dispatched_calls"] == root["model_usage"]["unknown_responses"] == 2
        for path in root["slots"]:
            slot = read_record(output / entry["path"] / path)
            assert slot["status"] == "missing" and len(slot["model_request_ids"]) == 1
            assert slot["base_source"] == slot["active_source"]
    assert pilot.run(config, output, resume=True, analyze=False) == manifest


def test_budget_configuration_and_actual_source_matching(tmp_path):
    config = configuration(tmp_path)
    config.write_text(config.read_text().replace('max_calls = 2', 'max_calls = 3'))
    with pytest.raises(ValidationError, match="capped"):
        pilot.read_config(config)
    source = "def handle(request, cache, api):\n    return cache\n"
    assert pilot.source_in_request(source, {"messages": [{"content": source.strip()}]})
    assert not pilot.source_in_request(source, {"content": "parent_id=source-123"})
