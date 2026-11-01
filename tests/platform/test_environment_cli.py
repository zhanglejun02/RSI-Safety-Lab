"""Explicit credential-file loading never evaluates or echoes its contents."""
import os
from pathlib import Path

import pytest

from rsi_lab.engine.continuation import save_record
from rsi_lab.errors import IntegrityError, ValidationError
from rsi_lab.infra.environment import load_environment
from rsi_lab.platform_config import PlatformConfig, ModelSettings
from rsi_lab.studies import platform_cli, validity_pilot


@pytest.fixture(autouse=True)
def isolated_test_environment(monkeypatch):
    # Register original absence as well as presence before the loader mutates
    # os.environ directly, so these test credentials never escape a test.
    for name in ("RSI_TEST_KEY", "RSI_VALID_KEY", "RSI_EXISTING_KEY",
                 "RSI_CUSTOM_PROVIDER_KEY", "RSI_RESUME_KEY", "SSL_CERT_FILE"):
        monkeypatch.setenv(name, "temporary-test-placeholder")
        monkeypatch.delenv(name)


def private_file(tmp_path, content):
    path = tmp_path / "credentials.env"
    path.write_text(content)
    path.chmod(0o600)
    return path


def test_loads_only_explicit_allowed_literals_without_overwriting_process(tmp_path, monkeypatch):
    monkeypatch.delenv("RSI_TEST_KEY", raising=False)
    monkeypatch.setenv("RSI_EXISTING_KEY", "process-value")
    path = private_file(tmp_path, '# local credentials\nRSI_TEST_KEY="local-value"\nRSI_EXISTING_KEY=file-value\n')
    assert load_environment(path, {"RSI_TEST_KEY", "RSI_EXISTING_KEY"}) == ("RSI_TEST_KEY",)
    assert os.environ["RSI_TEST_KEY"] == "local-value"
    assert os.environ["RSI_EXISTING_KEY"] == "process-value"


@pytest.mark.parametrize("invalid", [
    "UNAPPROVED=do-not-print-this-value",
    "RSI_TEST_KEY=first\nRSI_TEST_KEY=do-not-print-this-value",
    'RSI_TEST_KEY="do-not-print-this-value\ncontinued"',
    "export RSI_TEST_KEY=do-not-print-this-value",
    "RSI_TEST_KEY=${do-not-print-this-value}",
    "RSI_TEST_KEY=`do-not-print-this-value`",
    "RSI_TEST_KEY=do-not-print-this-value;command",
    "RSI_TEST_KEY=do-not-print-this-value\x00",
    "RSI_TEST_KEY=",
])
def test_invalid_file_is_atomic_and_errors_do_not_include_values(tmp_path, monkeypatch, invalid):
    monkeypatch.delenv("RSI_TEST_KEY", raising=False)
    monkeypatch.delenv("RSI_VALID_KEY", raising=False)
    path = private_file(tmp_path, "RSI_VALID_KEY=before-invalid-line\n" + invalid)
    with pytest.raises(ValidationError) as error:
        load_environment(path, {"RSI_TEST_KEY", "RSI_VALID_KEY"})
    assert "do-not-print-this-value" not in str(error.value)
    assert "RSI_VALID_KEY" not in os.environ
    assert "RSI_TEST_KEY" not in os.environ


def test_shell_substitution_is_rejected_without_execution(tmp_path, monkeypatch):
    marker = tmp_path / "unexpected-command"
    monkeypatch.delenv("RSI_TEST_KEY", raising=False)
    path = private_file(tmp_path, f"RSI_TEST_KEY=$(touch {marker})\n")
    with pytest.raises(ValidationError):
        load_environment(path, {"RSI_TEST_KEY"})
    assert not marker.exists()


def test_symlink_and_directory_are_rejected(tmp_path):
    path = private_file(tmp_path, "RSI_TEST_KEY=value")
    link = tmp_path / "link.env"
    link.symlink_to(path)
    with pytest.raises(IntegrityError):
        load_environment(link, {"RSI_TEST_KEY"})
    with pytest.raises(ValidationError):
        load_environment(tmp_path, {"RSI_TEST_KEY"})


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions")
def test_group_or_world_readable_credentials_are_rejected(tmp_path):
    path = private_file(tmp_path, "RSI_TEST_KEY=private-value")
    path.chmod(0o644)
    with pytest.raises(ValidationError, match="permissions to 600"):
        load_environment(path, {"RSI_TEST_KEY"})


@pytest.mark.parametrize("raw", [b"RSI_TEST_KEY=\xff", b"x" * 65537])
def test_invalid_encoding_and_size_are_rejected_without_echo(tmp_path, raw):
    path = private_file(tmp_path, "")
    path.write_bytes(raw)
    with pytest.raises(ValidationError):
        load_environment(path, {"RSI_TEST_KEY"})


def test_cli_validate_explicit_file_uses_config_names_and_does_not_print_values(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("RSI_CUSTOM_PROVIDER_KEY", raising=False)
    env = private_file(tmp_path, "RSI_CUSTOM_PROVIDER_KEY=secret-not-in-output\n")
    cfg = tmp_path / "config.toml"
    cfg.write_text('[editor]\napi_key_env="RSI_CUSTOM_PROVIDER_KEY"\n[actor]\napi_key_env="RSI_CUSTOM_PROVIDER_KEY"\n')
    def checked(config):
        assert os.environ["RSI_CUSTOM_PROVIDER_KEY"] == "secret-not-in-output"
        return {"valid": True}
    monkeypatch.setattr(platform_cli, "preflight", checked)
    assert platform_cli.main(["validate", "--config", str(cfg), "--env-file", str(env)]) == 0
    assert "secret-not-in-output" not in capsys.readouterr().out


@pytest.mark.parametrize("command", ["resume", "pilot-resume"])
def test_resume_derives_allowed_names_from_registered_config(tmp_path, monkeypatch, command):
    monkeypatch.delenv("RSI_RESUME_KEY", raising=False)
    config = PlatformConfig(editor=ModelSettings(api_key_env="RSI_RESUME_KEY"),
                            actor=ModelSettings(api_key_env="RSI_RESUME_KEY"))
    output = tmp_path / "run"
    save_record(output / "manifest.json", {"config": config})
    env = private_file(tmp_path, "RSI_RESUME_KEY=resume-secret\n")
    called = []
    def resume(path):
        assert os.environ["RSI_RESUME_KEY"] == "resume-secret"
        called.append(path)
        return {"resumed": True}
    module = platform_cli if command == "resume" else validity_pilot
    monkeypatch.setattr(module, "resume_run", resume)
    assert platform_cli.main([command, "--run", str(output), "--env-file", str(env)]) == 0
    assert called == [output]


def test_no_implicit_dotenv_loading(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    monkeypatch.setattr(platform_cli, "load_environment", lambda *_: pytest.fail("Unexpected automatic dotenv load"))
    monkeypatch.setattr(platform_cli, "preflight", lambda _: {"valid": True})
    assert platform_cli.main(["validate", "--config", str(cfg)]) == 0


@pytest.mark.parametrize("command", ["run", "pilot-run"])
def test_new_run_loads_credentials_before_dispatch(tmp_path, monkeypatch, command):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[editor]\napi_key_env="RSI_TEST_KEY"\n[actor]\napi_key_env="RSI_TEST_KEY"\n')
    env = private_file(tmp_path, "RSI_TEST_KEY=run-secret\nSSL_CERT_FILE=/trusted/ca.pem\n")
    output = tmp_path / "output"
    def create(config, path, **kwargs):
        assert os.environ["RSI_TEST_KEY"] == "run-secret"
        assert os.environ["SSL_CERT_FILE"] == "/trusted/ca.pem"
        assert path == output
        if command == "pilot-run":
            assert config.protocol.mode == "paired" and kwargs["roots"] == 2
        return {"created": True}
    module = platform_cli if command == "run" else validity_pilot
    monkeypatch.setattr(module, "create_run", create)
    assert platform_cli.main([command, "--config", str(cfg), "--output", str(output),
                              "--env-file", str(env)]) == 0


def test_cli_rejects_unconfigured_credential_without_disclosing_it(tmp_path, monkeypatch, capsys):
    env = private_file(tmp_path, "UNRELATED_KEY=sensitive-value-that-must-stay-local\n")
    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    monkeypatch.setattr(platform_cli, "preflight", lambda *_: pytest.fail("Invalid environment must stop before preflight"))
    assert platform_cli.main(["validate", "--config", str(cfg), "--env-file", str(env)]) == 2
    assert "sensitive-value-that-must-stay-local" not in capsys.readouterr().out
