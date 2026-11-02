from pathlib import Path
from rsi_lab.cli import main
from rsi_lab.engine.continuation import save_record
from rsi_lab.studies import experience_cli as cli
from rsi_lab.studies.experience_diagnostic import load_config


def test_run_uses_explicit_provider_credentials(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(cli, "load_environment", lambda p, names: calls.append((p, names)))
    monkeypatch.setattr(cli, "create_run", lambda cfg, path: {"status": "test", "rows": ["large"]})
    assert main(["experience", "run", "--config", "configs/experience_kimi.toml", "--output", str(tmp_path),
                 "--env-file", str(tmp_path / ".env")]) == 0
    assert calls == [(tmp_path / ".env", {"MOONSHOT_API_KEY", "SSL_CERT_FILE"})]
    assert '"rows"' not in capsys.readouterr().out


def test_inspect_does_not_advance_or_read_secret(monkeypatch, tmp_path, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("read-only inspect advanced work")
    monkeypatch.setattr(cli, "load_environment", forbidden)
    monkeypatch.setattr(cli, "resume_run", forbidden)
    assert main(["experience", "inspect", "--run", str(tmp_path)]) == 0
    assert '"completed_slots": 0' in capsys.readouterr().out


def test_resume_loads_registered_environment_name(monkeypatch, tmp_path):
    save_record(tmp_path / "manifest.json", {"config": load_config("configs/experience_kimi.toml")})
    calls = []
    monkeypatch.setattr(cli, "load_environment", lambda path, names: calls.append(names))
    monkeypatch.setattr(cli, "resume_run", lambda path: {"status": "test"})
    assert main(["experience", "resume", "--run", str(tmp_path), "--env-file", str(tmp_path / ".env")]) == 0
    assert calls == [{"MOONSHOT_API_KEY", "SSL_CERT_FILE"}]
