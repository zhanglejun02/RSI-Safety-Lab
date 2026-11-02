from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from rsi_lab.cli import main
from rsi_lab.engine.continuation import save_record
from rsi_lab.errors import BudgetExceeded
from rsi_lab.infra.chat_models import ChatMessage
from rsi_lab.serialization import canonical_bytes
from rsi_lab.studies import optimized_q as study, optimized_q_cli as cli


def test_cli_loads_only_registered_secret_names(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(cli, "load_environment", lambda p, names: calls.append((p, names)))
    monkeypatch.setattr(cli, "create_run", lambda cfg, path: {"status": "test", "samples": ["private-large"]})
    assert main(["optimized-q", "run", "--config", "configs/optimized_q_kimi.toml", "--output", str(tmp_path),
                 "--env-file", str(tmp_path / ".env")]) == 0
    assert calls == [(tmp_path / ".env", {"MOONSHOT_API_KEY", "SSL_CERT_FILE"})]
    assert "private-large" not in capsys.readouterr().out


def test_inspect_never_starts_work_or_loads_credentials(monkeypatch, tmp_path, capsys):
    def forbidden(*args, **kwargs): pytest.fail("inspect advanced work")
    monkeypatch.setattr(cli, "load_environment", forbidden)
    monkeypatch.setattr(cli, "resume_run", forbidden)
    assert main(["optimized-q", "inspect", "--run", str(tmp_path)]) == 0
    assert '"completed_trajectories": 0' in capsys.readouterr().out


def test_resume_credential_name_is_taken_from_frozen_manifest(monkeypatch, tmp_path):
    save_record(tmp_path / "manifest.json", {"config": study.load_config("configs/optimized_q_kimi.toml")})
    calls = []
    monkeypatch.setattr(cli, "load_environment", lambda path, names: calls.append(names))
    monkeypatch.setattr(cli, "resume_run", lambda path: {"status": "test"})
    assert main(["optimized-q", "resume", "--run", str(tmp_path), "--env-file", str(tmp_path / ".env")]) == 0
    assert calls == [{"MOONSHOT_API_KEY", "SSL_CERT_FILE"}]


def test_all_provider_instances_share_exact_36_call_limit_but_not_responses(tmp_path, monkeypatch):
    config = study.load_config("configs/optimized_q_kimi.toml")
    monkeypatch.setenv(config.model.api_key_env, "test-only-not-a-key")
    providers = [study._provider(config, tmp_path) for _ in range(3)]
    physical = []
    def fake(url, headers, body, timeout):
        physical.append(body)
        return canonical_bytes({"model": config.model.model, "choices": [{"finish_reason": "stop",
            "message": {"content": '{"source":"example"}'}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}})
    for provider in providers: provider.transport = fake
    messages = (ChatMessage("user", "identical input"),)
    with ThreadPoolExecutor(max_workers=3) as pool:
        result = list(pool.map(lambda i: providers[i % 3].generate(f"separate-{i}", messages), range(36)))
    assert len(physical) == len(result) == 36
    assert len({r.request_id for r in result}) == 36
    assert len({r.request_sha256 for r in result}) == 1
    for provider in providers:
        assert provider.summary()["dispatched_calls"] == 36
        assert provider.summary()["actual_total_tokens"] == 2160
        with pytest.raises(BudgetExceeded): provider.generate("extra", messages)
    assert len(physical) == 36


def test_token_reservation_refuses_dispatch_before_budget_exhaustion(tmp_path):
    config = study.load_config("configs/optimized_q_kimi.toml")
    config = replace(config, model=replace(config.model, max_total_tokens=1000))
    provider = study._provider(config, tmp_path)
    provider.transport = lambda *a: pytest.fail("budget must reject before any API request")
    with pytest.raises(BudgetExceeded): provider.generate("not-dispatched", (ChatMessage("user", "x"),))
    assert provider.summary()["dispatched_calls"] == 0
