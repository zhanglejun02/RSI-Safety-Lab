"""Typed model options for Moonshot-compatible instant-mode JSON requests.

All transports here are local callbacks; no credentials or cloud requests.
"""
from dataclasses import replace
from pathlib import Path

import pytest

from rsi_lab.errors import ConflictError, ValidationError
from rsi_lab.infra.chat_models import ChatConfig, ChatMessage, DurableChatProvider
from rsi_lab.platform_config import ModelSettings, load_platform_config
from rsi_lab.platform_wiring import build_providers
from rsi_lab.serialization import canonical_bytes, decode_json, digest


MESSAGES = (ChatMessage("user", 'Return JSON: {"ok":true}'),)


def response():
    return canonical_bytes({"model": "kimi-k2.6", "choices": [
        {"finish_reason": "stop", "message": {"content": '{"ok":true}'}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})


def config(**changes):
    return replace(ChatConfig("https://api.moonshot.cn/v1", "kimi-k2.6"), **changes)


def test_explicit_instant_mode_is_serialized_and_timeout_reaches_transport(tmp_path):
    calls = []
    provider = DurableChatProvider(tmp_path / "provider.sqlite",
        config(thinking_mode="disabled", temperature=0.6, timeout_seconds=120.0),
        transport=lambda *args: calls.append(args) or response())
    result = provider.generate("canary", MESSAGES)
    url, headers, body, timeout = calls[0]
    assert url == "https://api.moonshot.cn/v1/chat/completions"
    assert timeout == 120.0
    assert decode_json(body)["thinking"] == {"type": "disabled"}
    assert decode_json(body)["temperature"] == 0.6
    assert decode_json(body)["response_format"] == {"type": "json_object"}
    assert result.content == {"ok": True}
    assert result.usage_complete and provider.summary()["dispatched_calls"] == 1


def test_default_payload_remains_without_vendor_specific_thinking(tmp_path):
    provider = DurableChatProvider(tmp_path / "provider.sqlite", config())
    payload = decode_json(provider._payload(MESSAGES))
    assert payload == {"model": "kimi-k2.6", "messages": [{"role": "user", "content": MESSAGES[0].content}],
        "temperature": 0.0, "max_tokens": 1024, "response_format": {"type": "json_object"}, "stream": False}


def test_enabled_mode_is_an_explicit_request_field(tmp_path):
    provider = DurableChatProvider(tmp_path / "provider.sqlite", config(thinking_mode="enabled", temperature=1.0))
    assert decode_json(provider._payload(MESSAGES))["thinking"] == {"type": "enabled"}


@pytest.mark.parametrize("changes", [
    {"thinking_mode": "auto"}, {"thinking_mode": False}, {"thinking_mode": {}},
    {"temperature": True}, {"temperature": float("nan")}, {"temperature": -0.1}, {"temperature": 2.1},
    {"timeout_seconds": 0.0}, {"timeout_seconds": 300.1}, {"timeout_seconds": float("inf")},
    {"timeout_seconds": True},
])
def test_invalid_model_options_rejected_by_both_boundaries(changes):
    with pytest.raises(ValidationError):
        config(**changes)
    with pytest.raises(ValidationError):
        replace(ModelSettings(), **changes)


@pytest.mark.parametrize("changes", [
    {"thinking_mode": "enabled"}, {"temperature": 1.0}, {"timeout_seconds": 121.0},
])
def test_option_changes_are_bound_to_durable_configuration(tmp_path, changes):
    original = config(thinking_mode="disabled", temperature=0.6, timeout_seconds=120.0)
    path = tmp_path / "provider.sqlite"
    provider = DurableChatProvider(path, original, transport=lambda *args: response())
    first = provider.generate("canary", MESSAGES)
    replay = DurableChatProvider(path, original, transport=lambda *args: pytest.fail("Unexpected dispatch"))
    assert replay.generate("canary", MESSAGES) == first
    changed = replace(original, **changes)
    assert digest(original) != digest(changed)
    with pytest.raises(ConflictError, match="configuration changed"):
        DurableChatProvider(path, changed)


def test_kimi_config_wires_typed_options_and_stays_bounded(tmp_path):
    path = Path(__file__).parents[2] / "configs/platform_workflow_kimi.toml"
    cfg = load_platform_config(path)
    assert cfg.protocol.horizon == 3 and cfg.protocol.repetitions == 1 and cfg.protocol.seed == 1101
    assert cfg.protocol.mode == "admission"
    assert cfg.system.options["min_public_utility"] == 1.0
    providers = build_providers(cfg, tmp_path / "providers")
    for role in ("actor", "editor"):
        setting = getattr(cfg, role)
        runtime = providers[role].config
        assert runtime.model == "kimi-k2.6" and runtime.thinking_mode == "disabled"
        assert runtime.temperature == 0.6 and runtime.timeout_seconds == 120.0
        assert runtime.api_key_env == "MOONSHOT_API_KEY"
        assert runtime.max_calls == setting.max_calls and runtime.max_total_tokens == setting.max_total_tokens
        assert providers[role].summary()["dispatched_calls"] == 0
    assert cfg.actor.max_calls == 1200 and cfg.editor.max_calls == 32
    assert cfg.actor.max_total_tokens == 4000000 and cfg.editor.max_total_tokens == 500000
