"""Durable JSON chat gateway for explicitly configured compatible HTTP services.

Only model *data* is accepted here; no generated source is imported or executed.
Remote exactly-once execution is not assumed. A persisted dispatch with no known
response is never silently resent. Credentials remain in transport headers only.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass, field
import hashlib
import math
import os
from pathlib import Path
import re
import sqlite3
from typing import Callable, Protocol
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from rsi_lab.contracts import CandidateOrigin, StrictRecord, require_count, require_id
from rsi_lab.errors import BudgetExceeded, ConflictError, IntegrityError, OutcomeUnknown, ValidationError
from rsi_lab.infra.artifacts import _checked_path
from rsi_lab.serialization import canonical_bytes, decode_json, digest


@dataclass(frozen=True)
class ChatMessage(StrictRecord):
    role: str
    content: str
    schema_version: int = 1

    def validate(self):
        if self.role not in ("system", "user", "assistant") or not self.content:
            raise ValidationError("Chat messages require a supported role and nonempty text")


@dataclass(frozen=True)
class ChatConfig(StrictRecord):
    base_url: str
    model: str
    api_key_env: str | None = None
    temperature: float = 0.0
    max_output_tokens: int = 1024
    max_calls: int = 100
    max_total_tokens: int = 1000000
    timeout_seconds: float = 60.0
    max_prompt_bytes: int = 65536
    thinking_mode: str | None = None
    schema_version: int = 1

    def validate(self):
        parts = urlsplit(self.base_url)
        if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username
                or parts.password or parts.query or parts.fragment):
            raise ValidationError("base_url must be HTTP(S), without credentials/query/fragment")
        # Do not transmit bearer credentials in plaintext to remote services.
        if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValidationError("Remote model endpoints require HTTPS; use a local tunnel otherwise")
        if not self.model.strip() or len(self.model) > 256:
            raise ValidationError("An explicit model name is required")
        if self.api_key_env is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env):
            raise ValidationError("api_key_env must name an environment variable")
        if not math.isfinite(self.temperature) or not 0 <= self.temperature <= 2:
            raise ValidationError("temperature must be finite and between 0 and 2")
        if not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 300:
            raise ValidationError("timeout_seconds must be in (0,300]")
        if self.thinking_mode not in (None, "disabled", "enabled"):
            raise ValidationError("thinking_mode must be null, disabled, or enabled")
        for field in ("max_output_tokens", "max_calls", "max_total_tokens", "max_prompt_bytes"):
            require_count(getattr(self, field), field, 1)
            if getattr(self, field) > 2**31 - 1:
                raise ValidationError(f"{field} is too large")

    @property
    def endpoint(self):
        base = self.base_url.rstrip("/")
        return base if base.endswith("/chat/completions") else base + "/chat/completions"


@dataclass(frozen=True)
class ChatResult(StrictRecord):
    request_id: str
    content: dict
    origin: CandidateOrigin
    provider_id: str
    requested_model: str
    response_model: str
    request_sha256: str
    response_sha256: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    usage_complete: bool
    budget_overrun: bool = False
    response_id: str | None = None
    serving_provider: str | None = None
    system_fingerprint: str | None = None
    usage_details: dict = field(default_factory=dict)
    schema_version: int = 1

    def validate(self):
        require_id(self.request_id, "request_id")
        require_id(self.provider_id, "provider_id")
        canonical_bytes(self.content)
        canonical_bytes(self.usage_details)
        for value in (self.request_sha256, self.response_sha256):
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValidationError("Invalid chat evidence digest")
        counts = (self.prompt_tokens, self.completion_tokens, self.total_tokens)
        for count in counts:
            if count is not None:
                require_count(count, "actual token usage")
        if self.usage_complete != all(c is not None for c in counts):
            raise ValidationError("Usage completeness mismatch")
        if self.usage_complete and self.total_tokens < self.prompt_tokens + self.completion_tokens:
            raise ValidationError("Total token usage is less than its components")
        if self.origin == CandidateOrigin.CONTROL and any(c is not None for c in counts):
            raise ValidationError("Scripted responses cannot claim measured model tokens")


class ChatProvider(Protocol):
    origin: CandidateOrigin
    provider_id: str

    def generate(self, request_id: str, messages: tuple[ChatMessage, ...]) -> ChatResult: ...
    def summary(self) -> dict: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Redirects must not forward bearer credentials to a different endpoint.
        raise ValidationError("Model endpoint redirect refused")


def http_transport(url: str, headers: dict, body: bytes, timeout: float) -> bytes:
    request = Request(url, data=body, headers=headers, method="POST")
    with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
        data = response.read(4 * 1024 * 1024 + 1)
        if len(data) > 4 * 1024 * 1024:
            raise ValidationError("Model response exceeds the 4 MiB limit")
        return data


class DurableChatProvider:
    """One durable intent per request; successful JSON responses are replayable.

    Token reservation uses request UTF-8 bytes + output cap + 256 overhead units.
    It is a conservative estimate, not a universal tokenizer bound or a billing
    guarantee. Actual server usage settles it; missing usage keeps the reservation.
    Overruns are recorded and prohibit subsequent dispatch. Call limits are exact.
    """

    origin = CandidateOrigin.NATURAL

    def __init__(self, path: Path, config: ChatConfig, *, transport=None):
        if type(config) is not ChatConfig:
            raise ValidationError("A typed ChatConfig is required")
        self.path = _checked_path(Path(path))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.transport = transport or http_transport
        self.provider_id = "http_json_chat_" + digest({"url": config.base_url, "model": config.model})[:16]
        self._initialize({"config": config, "provider_id": self.provider_id, "origin": self.origin.value})

    @contextmanager
    def _db(self):
        _checked_path(self.path)
        for suffix in ("-wal", "-shm", "-journal"):
            _checked_path(Path(str(self.path) + suffix))
        with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            yield db

    def _initialize(self, metadata):
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS provider_metadata(id INTEGER PRIMARY KEY, value BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS chat_requests(
                    request_id TEXT PRIMARY KEY, request_sha TEXT NOT NULL, request_json BLOB NOT NULL,
                    status TEXT NOT NULL, reserved_tokens INTEGER NOT NULL, actual_tokens INTEGER,
                    response_bytes BLOB, response_sha TEXT, result_json BLOB, error_code TEXT,
                    budget_overrun INTEGER NOT NULL DEFAULT 0);
            """)
            value = canonical_bytes(metadata)
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT value FROM provider_metadata WHERE id=1").fetchone()
            if old and old[0] != value:
                raise ConflictError("Provider configuration changed for an existing journal")
            if not old:
                db.execute("INSERT INTO provider_metadata VALUES(1,?)", (value,))

    def _payload(self, messages):
        if type(messages) is not tuple or not messages or not all(type(m) is ChatMessage for m in messages):
            raise ValidationError("messages must be a nonempty tuple of ChatMessage")
        payload = {"model": self.config.model,
                   "messages": [{"role": m.role, "content": m.content} for m in messages],
                   "temperature": self.config.temperature,
                   "max_tokens": self.config.max_output_tokens,
                   "response_format": {"type": "json_object"}, "stream": False}
        if self.config.thinking_mode is not None:
            payload["thinking"] = {"type": self.config.thinking_mode}
        body = canonical_bytes(payload)
        if len(body) > self.config.max_prompt_bytes:
            raise ValidationError("Model request exceeds max_prompt_bytes")
        return body

    def _credentials(self):
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.config.api_key_env is not None:
            key = os.environ.get(self.config.api_key_env)
            if not key or "\n" in key or "\r" in key:
                raise ValidationError("Configured API credential is missing or invalid")
            headers["Authorization"] = "Bearer " + key
        return headers

    def _reserve(self, request_id, body):
        request_hash = hashlib.sha256(body).hexdigest()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM chat_requests WHERE request_id=?", (request_id,)).fetchone()
            if old:
                if old["request_sha"] != request_hash or old["request_json"] != body:
                    raise ConflictError("Model request identity reused with different input")
                return self._replay(old), None
            stats = self._summary(db)
            reservation = (len(body) + self.config.max_output_tokens + 256
                           if self.origin == CandidateOrigin.NATURAL else 0)
            if stats["dispatched_calls"] >= self.config.max_calls:
                raise BudgetExceeded("Model call limit reached before dispatch")
            if stats["budget_overrun"] or stats["actual_total_tokens"] + stats["reserved_tokens"] + reservation > self.config.max_total_tokens:
                raise BudgetExceeded("Token budget cannot cover pre-dispatch reservation")
            headers = self._credentials()  # Missing key does not create a dispatched intent.
            db.execute("INSERT INTO chat_requests(request_id,request_sha,request_json,status,reserved_tokens) VALUES(?,?,?,'pending',?)",
                       (request_id, request_hash, body, reservation))
        return None, headers

    def _replay(self, row):
        if row["status"] in ("pending", "unknown"):
            raise OutcomeUnknown("Dispatched model request lacks a confirmed response; automatic retry is prohibited")
        if row["response_bytes"] is None or hashlib.sha256(row["response_bytes"]).hexdigest() != row["response_sha"]:
            raise IntegrityError("Persisted model response digest mismatch")
        if row["status"] == "invalid":
            raise ValidationError("Persisted model response was invalid; use a new explicit attempt")
        result = ChatResult.from_dict(decode_json(row["result_json"]))
        if (result.request_id != row["request_id"] or result.request_sha256 != row["request_sha"]
                or result.response_sha256 != row["response_sha"] or result.provider_id != self.provider_id
                or result.origin != self.origin or result.total_tokens != row["actual_tokens"]):
            raise IntegrityError("Persisted chat result binding mismatch")
        # Re-parse source response so a separately modified result cannot substitute content.
        expected = self._parse(row["request_id"], row["request_sha"], row["response_bytes"], bool(row["budget_overrun"]))
        if result != expected:
            raise IntegrityError("Persisted result differs from its source response")
        return result

    @staticmethod
    def _usage(envelope):
        usage = envelope.get("usage")
        if not isinstance(usage, dict):
            return None, None, None
        values = tuple(usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens"))
        if any(type(v) is not int or v < 0 for v in values) or values[2] < values[0] + values[1]:
            return None, None, None
        return values

    def _parse(self, request_id, request_hash, raw, overrun=False):
        envelope = decode_json(raw)
        choices = envelope.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or type(choices[0]) is not dict:
            raise ValidationError("Model response must contain exactly one choice")
        choice = choices[0]
        if choice.get("finish_reason") != "stop" or not isinstance(choice.get("message"), dict):
            raise ValidationError("Model response is incomplete or has an unsupported finish reason")
        message = choice["message"]
        if message.get("tool_calls") or message.get("refusal"):
            raise ValidationError("Expected JSON content, received tool calls or refusal")
        content = decode_json(message.get("content"))
        response_model = envelope.get("model")
        if type(response_model) is not str or not response_model:
            raise ValidationError("Response must identify its actual model")
        # Aliases can resolve to pinned underlying model names; both are retained.
        prompt, completion, total = self._usage(envelope)
        metadata = {}
        for target, source in (("response_id", "id"), ("serving_provider", "provider"),
                               ("system_fingerprint", "system_fingerprint")):
            value = envelope.get(source)
            if value is not None and (type(value) is not str or len(value) > 512):
                raise ValidationError("Invalid model response provenance")
            metadata[target] = value
        usage = envelope.get("usage")
        return ChatResult(request_id, content, self.origin, self.provider_id, self.config.model,
                          response_model, request_hash, hashlib.sha256(raw).hexdigest(),
                          prompt, completion, total, total is not None, overrun,
                          usage_details=usage if type(usage) is dict else {}, **metadata)

    def generate(self, request_id: str, messages: tuple[ChatMessage, ...]) -> ChatResult:
        require_id(request_id, "request_id")
        body = self._payload(messages)
        saved, headers = self._reserve(request_id, body)
        if saved is not None:
            return saved
        try:
            raw = self.transport(self.config.endpoint, headers, body, self.config.timeout_seconds)
            if type(raw) is not bytes or len(raw) > 4 * 1024 * 1024:
                raise ValidationError("Invalid transport response")
            bearer = headers.get("Authorization", "").removeprefix("Bearer ")
            if bearer and bearer.encode() in raw:
                raise ValidationError("Credential reflected by endpoint; response not persisted")
        except Exception as exc:
            # Do not persist provider exception text or HTTP bodies (may contain secrets).
            with self._db() as db:
                db.execute("UPDATE chat_requests SET status='unknown',error_code=? WHERE request_id=?",
                           (type(exc).__name__, request_id))
            raise OutcomeUnknown("Model dispatch did not produce a confirmed response; no automatic retry") from None
        request_hash = hashlib.sha256(body).hexdigest()
        actual_tokens = None
        completion_tokens = None
        try:
            _, completion_tokens, actual_tokens = self._usage(decode_json(raw))
        except ValidationError:
            pass
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            reservation = db.execute("SELECT reserved_tokens FROM chat_requests WHERE request_id=?", (request_id,)).fetchone()[0]
            overrun = actual_tokens is not None and (
                actual_tokens > reservation or completion_tokens > self.config.max_output_tokens)
            try:
                result = self._parse(request_id, request_hash, raw, overrun)
            except ValidationError:
                result = None
            if result is not None:
                previous = db.execute("SELECT result_json FROM chat_requests WHERE status='complete' LIMIT 1").fetchone()
                if previous and decode_json(previous[0])["response_model"] != result.response_model:
                    result = None  # A provider model alias must not drift within an experiment.
            db.execute("""UPDATE chat_requests SET status=?,actual_tokens=?,response_bytes=?,response_sha=?,
                          result_json=?,error_code=?,budget_overrun=? WHERE request_id=?""",
                       ("complete" if result else "invalid", actual_tokens, raw,
                        hashlib.sha256(raw).hexdigest(), canonical_bytes(result) if result else None,
                        None if result else "invalid_response", int(overrun), request_id))
        if result is None:
            raise ValidationError("Model response is not a complete JSON object; attempt recorded")
        return result

    def _summary(self, db):
        rows = db.execute("SELECT status,reserved_tokens,actual_tokens,budget_overrun,response_bytes FROM chat_requests").fetchall()
        costs = []
        for row in rows:
            try:
                usage = decode_json(row["response_bytes"]).get("usage", {})
                cost = usage.get("cost") if type(usage) is dict else None
            except ValidationError:
                cost = None
            if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
                costs.append(float(cost))
        return {"provider_id": self.provider_id, "origin": self.origin.value,
                "dispatched_calls": len(rows), "complete_responses": sum(r["status"] == "complete" for r in rows),
                "unknown_responses": sum(r["status"] in ("pending", "unknown") for r in rows),
                "invalid_responses": sum(r["status"] == "invalid" for r in rows),
                "actual_total_tokens": sum(r["actual_tokens"] or 0 for r in rows),
                "usage_missing_calls": sum(r["actual_tokens"] is None for r in rows),
                "reserved_tokens": sum(r["reserved_tokens"] for r in rows if r["actual_tokens"] is None),
                "budget_overrun": any(bool(r["budget_overrun"]) for r in rows),
                "token_reservation_kind": "utf8_bytes_plus_output_cap_estimate",
                "remote_exactly_once": False, "monetary_cost": None,
                "reported_usage_cost": sum(costs) if costs else None,
                "reported_cost_calls": len(costs), "cost_missing_calls": len(rows) - len(costs),
                "reported_cost_unit": ("OpenRouter credits" if urlsplit(self.config.base_url).hostname == "openrouter.ai"
                                       else "provider_reported_unspecified")}

    def summary(self):
        with self._db() as db:
            return self._summary(db)


class ScriptedChatProvider(DurableChatProvider):
    """Explicit control provider: callback(request_id, messages) returns a dict.

    It uses the same durable gateway but has no measured LLM token consumption.
    Script outputs are test/control data and cannot be labelled natural updates.
    """

    origin = CandidateOrigin.CONTROL

    def __init__(self, path: Path, callback: Callable, *, provider_id="scripted.control.v1", max_calls=10000):
        require_id(provider_id, "provider_id")
        self.path = _checked_path(Path(path))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.config = ChatConfig("http://localhost/control", provider_id, max_calls=max_calls,
                                 max_total_tokens=2**31 - 1)
        self.provider_id = provider_id
        self.callback = callback
        self._initialize({"config": self.config, "provider_id": provider_id, "origin": self.origin.value})

    def generate(self, request_id, messages):
        # Transport closure is used only synchronously; providers are not shared across threads.
        def dispatch(url, headers, body, timeout):
            content = self.callback(request_id, messages)
            if type(content) is not dict:
                raise ValidationError("Scripted callback must return JSON object data")
            return canonical_bytes({"model": self.provider_id, "choices": [{"finish_reason": "stop",
                "message": {"content": canonical_bytes(content).decode("utf-8")}}]})
        self.transport = dispatch
        return super().generate(request_id, messages)

    def summary(self):
        result = super().summary()
        result["token_reservation_kind"] = "control_no_model_tokens"
        result["actual_total_tokens"] = None
        result["reserved_tokens"] = 0
        return result
