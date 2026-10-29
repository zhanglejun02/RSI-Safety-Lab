"""Study/gateway integration with mock HTTP envelopes, never cloud model data.

Transport injection exercises the natural-provider protocol and persistence in a
temporary test directory. The responses and reported usage below are fixtures,
not empirical model results and not exported into a research dataset.
"""

from dataclasses import replace

import pytest

from rsi_lab.contracts import CandidateOrigin
from rsi_lab.errors import ValidationError
from rsi_lab.infra.chat_models import ChatConfig, DurableChatProvider
from rsi_lab.learning.contracts import AuditFeatures, CandidateRiskLabel, TrainingExample
from rsi_lab.serialization import canonical_bytes, decode_json
from rsi_lab.studies.task_a import TaskAConfig, create_run, resume_run


TEST_KEY_ENV = "RSI_STUDY_GATEWAY_TEST_KEY"
TEST_KEY = "dummy-study-key-never-a-real-credential"
PROVIDER_EXCEPTION_TEXT = "untrusted-provider-error-with-sensitive-dummy-text"


class MockHTTPTransport:
    def __init__(self, *, timeout_at=None):
        self.timeout_at = timeout_at
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        request = decode_json(body)
        payload = decode_json(request["messages"][-1]["content"])
        self.calls.append((url, headers, request, payload))
        if len(self.calls) == self.timeout_at:
            raise TimeoutError(PROVIDER_EXCEPTION_TEXT)
        if payload["operation"] == "propose_q":
            content = {"schema_version": 1, "order": ["e1", "e0", "e2", "e3"], "limit": 2}
        else:
            assert payload["operation"] == "improve_agent"
            content = {"schema_version": 1, "strategy": "strict", "lookup": "indexed"}
        return canonical_bytes({
            "id": f"mock-generation-{len(self.calls)}",
            "model": "test/immutable-model",
            "provider": "mock-transport-for-contract-testing",
            "choices": [{"finish_reason": "stop", "message": {
                "content": canonical_bytes(content).decode(),
            }}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60},
        })

    def provider(self, path, config):
        return DurableChatProvider(path, ChatConfig(
            config.base_url, config.model, config.api_key_env,
            max_calls=config.max_calls, max_total_tokens=config.max_total_tokens,
            max_output_tokens=config.max_output_tokens, timeout_seconds=config.timeout_seconds,
        ), transport=self)


@pytest.fixture
def http_config(monkeypatch):
    # Only this test-specific environment variable is read by the test gateway.
    monkeypatch.setenv(TEST_KEY_ENV, TEST_KEY)
    return TaskAConfig(provider="http", roots=1, seed=100,
                       model="test/immutable-model", base_url="http://localhost/mock/v1",
                       api_key_env=TEST_KEY_ENV, max_calls=21)


def read_records(run):
    root = run / "roots" / "authorization_root_100"
    features = AuditFeatures.from_dict(decode_json((root / "features.json").read_bytes()))
    label = CandidateRiskLabel.from_dict(decode_json((root / "label.json").read_bytes()))
    return features, label


def assert_no_sensitive_bytes(run):
    for path in run.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert TEST_KEY.encode() not in data
            assert PROVIDER_EXCEPTION_TEXT.encode() not in data


def test_complete_study_uses_http_gateway_and_resume_sends_nothing(tmp_path, monkeypatch, http_config):
    transport = MockHTTPTransport()
    run = tmp_path / "run"
    summary = create_run(http_config, run, provider_factory=transport.provider)
    expected_calls = http_config.roots * (
        http_config.development_rounds + 1 + 2 * http_config.probe_horizon
        + 2 * http_config.repetitions * http_config.horizon
    )
    assert expected_calls == 21
    assert len(transport.calls) == expected_calls
    assert summary["measured_roots"] == 1
    assert summary["roots"][0]["targets"] == [0.0, 0.0, 1.0, 1.0]
    assert summary["provider_usage"]["dispatched_calls"] == expected_calls
    assert summary["provider_usage"]["actual_total_tokens"] == expected_calls * 60
    assert summary["provider_usage"]["usage_missing_calls"] == 0
    assert summary["provider_usage"]["remote_exactly_once"] is False
    for url, headers, request, _ in transport.calls:
        assert url == "http://localhost/mock/v1/chat/completions"
        assert headers["Authorization"] == "Bearer " + TEST_KEY
        assert request["model"] == http_config.model
        assert request["max_tokens"] == http_config.max_output_tokens
        assert request["response_format"] == {"type": "json_object"}
    assert sum(call[3]["operation"] == "propose_q" for call in transport.calls) == 1

    features, label = read_records(run)
    TrainingExample(features, label)
    assert features.binding.origin == CandidateOrigin.NATURAL
    assert len(features.probe_outcomes) == 4
    assert len(label.outcomes) == 12
    assert label.targets() == (0.0, 0.0, 1.0, 1.0)
    assert features.probe_stream_id != label.evaluation_stream_id
    assert {o.trace_sha256 for o in features.probe_outcomes}.isdisjoint(
        o.trace_sha256 for o in label.outcomes)
    assert_no_sensitive_bytes(run)

    # Completed work must replay even after credentials cease to be available.
    monkeypatch.delenv(TEST_KEY_ENV)
    assert resume_run(run, provider_factory=transport.provider) == summary
    assert len(transport.calls) == expected_calls


def test_independent_timeout_retains_missing_slots_and_never_retries(tmp_path, monkeypatch, http_config):
    # Four development calls, q proposal and four probe calls precede the first label call.
    transport = MockHTTPTransport(timeout_at=10)
    run = tmp_path / "run"
    summary = create_run(http_config, run, provider_factory=transport.provider)
    features, label = read_records(run)
    assert all(o.complete for o in features.probe_outcomes)
    old = [o for o in label.outcomes if o.arm == "old"]
    new = [o for o in label.outcomes if o.arm == "new"]
    assert len(old) == len(new) == http_config.horizon
    assert all(o.risk is None and not o.complete for o in old)
    assert all(o.complete and o.risk == 0 for o in new)
    assert label.targets() is None
    assert summary["roots"][0]["targets"] is None
    assert summary["registered_roots"] == 1
    assert summary["provider_usage"]["unknown_responses"] == 1
    assert summary["provider_usage"]["reserved_tokens"] > 0
    assert summary["provider_usage"]["actual_total_tokens"] == 15 * 60
    assert len(transport.calls) == 16
    assert_no_sensitive_bytes(run)

    monkeypatch.delenv(TEST_KEY_ENV)
    assert resume_run(run, provider_factory=transport.provider) == summary
    assert len(transport.calls) == 16


def test_call_limit_stops_before_dispatch_and_retains_label_denominator(tmp_path, http_config):
    transport = MockHTTPTransport()
    run = tmp_path / "run"
    summary = create_run(replace(http_config, max_calls=10), run, provider_factory=transport.provider)
    _, label = read_records(run)
    assert len(transport.calls) == 10
    assert summary["provider_usage"]["dispatched_calls"] == 10
    assert summary["provider_usage"]["actual_total_tokens"] == 600
    assert summary["provider_usage"]["unknown_responses"] == 0
    assert len(label.outcomes) == 2 * http_config.horizon
    assert sum(o.complete for o in label.outcomes) == 1
    assert all(o.risk is None for o in label.outcomes if not o.complete)
    assert label.targets() is None
    assert resume_run(run, provider_factory=transport.provider) == summary
    assert len(transport.calls) == 10


def test_token_reservation_blocks_first_dispatch_without_losing_root(tmp_path, http_config):
    transport = MockHTTPTransport()
    run = tmp_path / "run"
    summary = create_run(replace(http_config, max_total_tokens=1), run, provider_factory=transport.provider)
    assert not transport.calls
    assert summary["registered_roots"] == 1
    assert summary["measured_roots"] == 0
    assert summary["roots"] == [{"root_id": "authorization_root_100", "status": "unmeasured",
                                 "reason": "BudgetExceeded"}]
    assert summary["provider_usage"]["dispatched_calls"] == 0
    assert resume_run(run, provider_factory=transport.provider) == summary
    assert not transport.calls


@pytest.mark.parametrize("url", [
    "https://user:dummy-url-secret@example.org/v1",
    "https://example.org/v1?key=dummy-url-secret",
    "file:///tmp/model-endpoint",
])
def test_invalid_endpoint_is_rejected_before_manifest_is_written(tmp_path, http_config, url):
    run = tmp_path / "run"
    def forbidden_provider(*args):
        pytest.fail("Invalid HTTP configuration reached provider construction")

    with pytest.raises(ValidationError):
        create_run(replace(http_config, base_url=url), run, provider_factory=forbidden_provider)
    assert not (run / "manifest.json").exists()
    assert not run.exists()


def test_model_environment_must_not_alias_credential_environment(tmp_path, http_config):
    run = tmp_path / "run"
    def forbidden_provider(*args):
        pytest.fail("Credential/model environment alias reached provider construction")

    with pytest.raises(ValidationError):
        create_run(replace(http_config, model="", model_env=TEST_KEY_ENV), run,
                   provider_factory=forbidden_provider)
    assert not run.exists()
