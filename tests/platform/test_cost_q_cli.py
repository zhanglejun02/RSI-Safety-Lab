from rsi_lab.studies import cost_q_cli
from rsi_lab.engine.continuation import save_record
from rsi_lab.studies.cost_q import CostQConfig


def test_explicit_env_allowlist_supports_private_key_and_tls_file(tmp_path, monkeypatch, capsys):
    config = tmp_path / 'study.toml'
    config.write_text('[model]\napi_key_env = "RSI_TEST_COST_KEY"\n')
    env = tmp_path / '.env'
    env.write_text('RSI_TEST_COST_KEY=unit-test-secret\nSSL_CERT_FILE=/tmp/unit-ca.pem\n')
    env.chmod(0o600)
    monkeypatch.delenv('RSI_TEST_COST_KEY', raising=False)
    monkeypatch.delenv('SSL_CERT_FILE', raising=False)
    seen = {}
    def create(settings, output):
        import os
        seen['loaded'] = bool(os.environ.get('RSI_TEST_COST_KEY'))
        return {'complete': True}
    monkeypatch.setattr(cost_q_cli, 'create_run', create)
    assert cost_q_cli.main(['run', '--config', str(config), '--output', str(tmp_path/'out'), '--env-file', str(env)]) == 0
    assert seen['loaded']
    assert 'unit-test-secret' not in capsys.readouterr().out


def test_resume_resolves_credential_name_from_registered_model(tmp_path, monkeypatch):
    run = tmp_path / 'run'
    save_record(run/'manifest.json', {'config': CostQConfig().to_dict()})
    env = tmp_path/'.env'
    env.write_text('OPENROUTER_API_KEY=unit-resume-secret\n')
    env.chmod(0o600)
    monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
    monkeypatch.setattr(cost_q_cli, 'resume_run', lambda output: {'complete': True})
    assert cost_q_cli.main(['resume', '--run', str(run), '--env-file', str(env)]) == 0
