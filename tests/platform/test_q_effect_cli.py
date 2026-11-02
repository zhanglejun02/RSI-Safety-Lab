from rsi_lab.cli import main
from rsi_lab.engine.continuation import save_record
from rsi_lab.studies import q_effect_cli
from rsi_lab.studies.q_effect import load_config


def test_run_loads_only_provider_credential_and_certificate(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(q_effect_cli, "load_environment", lambda path, names: calls.append((path, names)))
    monkeypatch.setattr(q_effect_cli, "create_run", lambda config, output: {"status": "test", "samples": ["omit"]})
    assert main(["q-effect", "run", "--config", "configs/q_effect_kimi.toml",
                 "--output", str(tmp_path / "run"), "--env-file", str(tmp_path / ".env")]) == 0
    assert calls == [(tmp_path / ".env", {"MOONSHOT_API_KEY", "SSL_CERT_FILE"})]
    assert "samples" not in capsys.readouterr().out


def test_inspect_does_not_load_credentials_or_advance(monkeypatch, tmp_path, capsys):
    save_record(tmp_path / "manifest.json", {"config": load_config("configs/q_effect_kimi.toml")})
    def forbidden(*args, **kwargs):
        raise AssertionError("read-only inspect invoked work")
    monkeypatch.setattr(q_effect_cli, "load_environment", forbidden)
    monkeypatch.setattr(q_effect_cli, "resume_run", forbidden)
    assert main(["q-effect", "inspect", "--run", str(tmp_path)]) == 0
    assert '"completed_slots": 0' in capsys.readouterr().out


def test_resume_uses_registered_credential_name(monkeypatch, tmp_path):
    save_record(tmp_path / "manifest.json", {"config": load_config("configs/q_effect_kimi.toml")})
    names = []
    monkeypatch.setattr(q_effect_cli, "load_environment", lambda path, allow: names.append(allow))
    monkeypatch.setattr(q_effect_cli, "resume_run", lambda output: {"status": "test"})
    assert main(["q-effect", "resume", "--run", str(tmp_path), "--env-file", str(tmp_path / ".env")]) == 0
    assert names == [{"MOONSHOT_API_KEY", "SSL_CERT_FILE"}]
