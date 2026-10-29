"""Gateway integration: real local HTTP, durable ambiguity and honest accounting."""

from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sqlite3
from threading import Thread

import pytest

from rsi_lab.contracts import CandidateOrigin
from rsi_lab.errors import BudgetExceeded, ConflictError, IntegrityError, OutcomeUnknown, ValidationError
from rsi_lab.infra.chat_models import ChatConfig, ChatMessage, DurableChatProvider, ScriptedChatProvider
from rsi_lab.serialization import canonical_bytes, decode_json


MESSAGES = (ChatMessage("system", "Return JSON only."), ChatMessage("user", "Improve the permitted program."))


def response(content=None, *, tokens=30, model="local-model", finish="stop", usage=True):
    value = {"model": model, "choices": [{"finish_reason": finish,
             "message": {"content": canonical_bytes(content or {"strategy": "strict"}).decode()}}]}
    if usage:
        value["usage"] = {"prompt_tokens": 20, "completion_tokens": tokens - 20, "total_tokens": tokens}
    return canonical_bytes(value)


@contextmanager
def local_server(raw):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, dict(self.headers), decode_json(body)))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    except PermissionError:
        pytest.skip("Execution sandbox forbids a local HTTP listener; run this integration test on the validation host")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def config(**changes):
    return replace(ChatConfig("http://127.0.0.1:8000/v1", "local-model", max_output_tokens=64), **changes)


def test_real_local_http_json_usage_and_replay_without_credentials(tmp_path, monkeypatch):
    secret = "test-only-credential-never-written-to-disk"
    monkeypatch.setenv("RSI_TEST_KEY", secret)
    with local_server(response()) as (url, calls):
        cfg = config(base_url=url, api_key_env="RSI_TEST_KEY")
        path = tmp_path / "provider.sqlite"
        client = DurableChatProvider(path, cfg)
        first = client.generate("request/1", MESSAGES)
        assert first.origin == CandidateOrigin.NATURAL
        assert first.content == {"strategy": "strict"}
        assert (first.prompt_tokens, first.completion_tokens, first.total_tokens) == (20, 10, 30)
        assert client.summary()["actual_total_tokens"] == 30
        assert calls[0][0] == "/v1/chat/completions"
        assert calls[0][1]["Authorization"] == "Bearer " + secret
        assert calls[0][2]["max_tokens"] == 64
        assert calls[0][2]["response_format"] == {"type": "json_object"}
        monkeypatch.delenv("RSI_TEST_KEY")
        restored = DurableChatProvider(path, cfg)
        assert restored.generate("request/1", MESSAGES) == first
        assert len(calls) == 1
        assert secret.encode() not in path.read_bytes()
        with pytest.raises(ValidationError, match="credential"):
            restored.generate("request/2", MESSAGES)
        assert restored.summary()["dispatched_calls"] == 1


def test_unknown_outcome_retains_reservation_and_never_retries(tmp_path):
    calls = []

    def timed_out(*args):
        calls.append(1)
        raise TimeoutError("untrusted provider text with sensitive bytes")

    path = tmp_path / "provider.sqlite"
    first = DurableChatProvider(path, config(), transport=timed_out)
    with pytest.raises(OutcomeUnknown) as exc:
        first.generate("r1", MESSAGES)
    assert "sensitive" not in str(exc.value)
    summary = first.summary()
    assert summary["unknown_responses"] == 1 and summary["reserved_tokens"] > 0
    second = DurableChatProvider(path, config(), transport=lambda *args: calls.append(2))
    with pytest.raises(OutcomeUnknown):
        second.generate("r1", MESSAGES)
    assert calls == [1]
    assert second.summary() == summary
    assert b"sensitive" not in path.read_bytes()


def test_crash_after_intent_is_not_assumed_free_or_automatically_retried(tmp_path):
    path = tmp_path / "provider.sqlite"

    def crash(*args):
        raise SystemExit(91)

    provider = DurableChatProvider(path, config(), transport=crash)
    with pytest.raises(SystemExit):
        provider.generate("r1", MESSAGES)
    recovered = DurableChatProvider(path, config(), transport=lambda *args: pytest.fail("duplicate dispatch"))
    with pytest.raises(OutcomeUnknown):
        recovered.generate("r1", MESSAGES)
    assert recovered.summary()["dispatched_calls"] == 1
    assert recovered.summary()["reserved_tokens"] > 0


def test_changed_identity_or_provider_config_is_rejected(tmp_path):
    path = tmp_path / "provider.sqlite"
    provider = DurableChatProvider(path, config(), transport=lambda *args: response())
    provider.generate("r1", MESSAGES)
    with pytest.raises(ConflictError):
        provider.generate("r1", (ChatMessage("user", "Different request"),))
    with pytest.raises(ConflictError):
        DurableChatProvider(path, config(model="another-model"))
    assert provider.summary()["dispatched_calls"] == 1


def test_call_and_token_limits_checked_before_transport(tmp_path):
    calls = []
    client = DurableChatProvider(tmp_path / "calls.sqlite", config(max_calls=1),
                                 transport=lambda *args: calls.append(1) or response())
    first = client.generate("r1", MESSAGES)
    with pytest.raises(BudgetExceeded, match="call limit"):
        client.generate("r2", MESSAGES)
    assert client.generate("r1", MESSAGES) == first
    tokens = DurableChatProvider(tmp_path / "tokens.sqlite", config(max_total_tokens=1),
                                 transport=lambda *args: pytest.fail("over-budget dispatch"))
    with pytest.raises(BudgetExceeded, match="reservation"):
        tokens.generate("r1", MESSAGES)
    assert tokens.summary()["dispatched_calls"] == 0
    assert calls == [1]


def test_missing_usage_is_not_reported_as_zero_cost(tmp_path):
    cfg = config(max_total_tokens=1000)
    client = DurableChatProvider(tmp_path / "provider.sqlite", cfg, transport=lambda *args: response(usage=False))
    result = client.generate("r1", MESSAGES)
    assert not result.usage_complete and result.total_tokens is None
    assert client.summary()["usage_missing_calls"] == 1
    assert client.summary()["reserved_tokens"] > 0
    with pytest.raises(BudgetExceeded):
        client.generate("r2", MESSAGES)


def test_provider_usage_overrun_is_recorded_and_stops_further_dispatch(tmp_path):
    client = DurableChatProvider(tmp_path / "provider.sqlite", config(), transport=lambda *args: response(tokens=5000))
    result = client.generate("r1", MESSAGES)
    assert result.budget_overrun
    assert client.summary()["actual_total_tokens"] == 5000
    assert client.summary()["reserved_tokens"] == 0
    with pytest.raises(BudgetExceeded):
        client.generate("r2", MESSAGES)


@pytest.mark.parametrize("raw", [
    b"not json",
    canonical_bytes({"model": "local-model", "choices": [{"finish_reason": "stop", "message": {"content": "[]"}}]}),
    response(finish="length"),
    canonical_bytes({"model": "local-model", "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        "choices": [{"finish_reason": "stop", "message": {"content": '{"x":1,"x":2}'}}]}),
])
def test_invalid_or_truncated_response_is_terminal_and_not_resent(tmp_path, raw):
    calls = []
    provider = DurableChatProvider(tmp_path / "provider.sqlite", config(), transport=lambda *args: calls.append(1) or raw)
    with pytest.raises(ValidationError):
        provider.generate("r1", MESSAGES)
    with pytest.raises(ValidationError):
        provider.generate("r1", MESSAGES)
    assert calls == [1]
    assert provider.summary()["invalid_responses"] == 1
    if b'"usage"' in raw:
        assert provider.summary()["actual_total_tokens"] == 30


def test_response_model_alias_cannot_silently_change_between_calls(tmp_path):
    outputs = iter((response(model="pinned-version-1"), response(model="pinned-version-2")))
    provider = DurableChatProvider(tmp_path / "provider.sqlite", config(), transport=lambda *args: next(outputs))
    first = provider.generate("r1", MESSAGES)
    assert first.requested_model == "local-model" and first.response_model == "pinned-version-1"
    with pytest.raises(ValidationError):
        provider.generate("r2", MESSAGES)
    assert provider.summary()["actual_total_tokens"] == 60


def test_scripted_control_is_durable_without_fake_usage(tmp_path):
    calls = []
    path = tmp_path / "control.sqlite"
    provider = ScriptedChatProvider(path, lambda rid, messages: calls.append(rid) or {"program": "safe"})
    first = provider.generate("r1", MESSAGES)
    assert first.origin == CandidateOrigin.CONTROL
    assert first.total_tokens is None and not first.usage_complete
    restored = ScriptedChatProvider(path, lambda *args: pytest.fail("duplicate control execution"))
    assert restored.generate("r1", MESSAGES) == first
    assert restored.summary()["actual_total_tokens"] is None
    assert restored.summary()["reserved_tokens"] == 0
    assert calls == ["r1"]


def test_tampered_cached_result_does_not_override_source_response(tmp_path):
    path = tmp_path / "provider.sqlite"
    provider = DurableChatProvider(path, config(), transport=lambda *args: response())
    result = provider.generate("r1", MESSAGES)
    forged = result.to_dict()
    forged["content"] = {"strategy": "unsafe"}
    with sqlite3.connect(path) as db:
        db.execute("UPDATE chat_requests SET result_json=?", (canonical_bytes(forged),))
    with pytest.raises(IntegrityError):
        provider.generate("r1", MESSAGES)


@pytest.mark.parametrize("url", ["https://key:password@example.org/v1", "https://example.org/v1?key=secret", "file:///tmp/data"])
def test_endpoint_configuration_never_accepts_embedded_credentials(url):
    with pytest.raises(ValidationError):
        config(base_url=url)


def test_invalid_input_is_rejected_before_creating_any_intent(tmp_path):
    provider = DurableChatProvider(tmp_path / "provider.sqlite", config(max_prompt_bytes=10),
                                   transport=lambda *args: pytest.fail("invalid dispatch"))
    with pytest.raises(ValidationError):
        provider.generate("r1", MESSAGES)
    assert provider.summary()["dispatched_calls"] == 0


def test_openrouter_configuration_preserves_reported_cost_and_provenance(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_TEST_KEY", "dummy-only")
    payload = decode_json(response(model="vendor/model-v1"))
    payload.update(id="gen-test", provider="ReportedProvider", system_fingerprint="revision-1")
    payload["usage"]["cost"] = 0.000123
    payload["usage"]["completion_tokens_details"] = {"reasoning_tokens": 4}
    calls = []
    provider = DurableChatProvider(tmp_path / "provider.sqlite",
        config(base_url="https://openrouter.ai/api/v1", model="vendor/model-v1", api_key_env="OPENROUTER_TEST_KEY"),
        transport=lambda *args: calls.append(args) or canonical_bytes(payload))
    result = provider.generate("r1", MESSAGES)
    assert calls[0][0] == "https://openrouter.ai/api/v1/chat/completions"
    assert result.response_id == "gen-test" and result.serving_provider == "ReportedProvider"
    assert result.system_fingerprint == "revision-1"
    assert result.usage_details["completion_tokens_details"]["reasoning_tokens"] == 4
    assert provider.summary()["reported_usage_cost"] == 0.000123
    assert provider.summary()["reported_cost_unit"] == "OpenRouter credits"
    assert provider.generate("r1", MESSAGES) == result


def test_reflected_credential_is_never_persisted(tmp_path, monkeypatch):
    secret = "dummy-reflected-secret"
    monkeypatch.setenv("RSI_TEST_KEY", secret)
    path = tmp_path / "provider.sqlite"
    provider = DurableChatProvider(path, config(api_key_env="RSI_TEST_KEY"),
                                   transport=lambda *args: response({"error": secret}))
    with pytest.raises(OutcomeUnknown):
        provider.generate("r1", MESSAGES)
    assert secret.encode() not in path.read_bytes()
    assert provider.summary()["unknown_responses"] == 1
