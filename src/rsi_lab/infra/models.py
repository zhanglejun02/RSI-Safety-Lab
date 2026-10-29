"""Deterministic registered fixture model, with no access to memory or labels."""

from dataclasses import dataclass, replace
from contextlib import closing, contextmanager
from pathlib import Path
import hashlib
import sqlite3

from rsi_lab.contracts import (
    StrictRecord,
    TransferProgram,
    Version,
    require_count,
    require_id,
)
from rsi_lab.errors import ConflictError, ValidationError
from rsi_lab.infra.artifacts import _checked_path
from rsi_lab.frozen import fixture_bytes
from rsi_lab.interventions.context import synthetic_units
from rsi_lab.serialization import canonical_bytes, decode_json, digest


@dataclass(frozen=True)
class ModelResponse(StrictRecord):
    version: Version
    request_id: str
    context_sha256: str
    synthetic_input_units: int
    request_sha256: str
    synthetic_cost_units: int = 5
    model_id: str = "fake_context_bytes_v1"
    schema_version: int = 1

    def validate(self):
        require_id(self.request_id, "request_id")
        require_count(self.synthetic_input_units, "synthetic_input_units")
        for key in ("context_sha256", "request_sha256"):
            value = getattr(self, key)
            if len(value) != 64 or any(
                char not in "0123456789abcdef" for char in value
            ):
                raise ValidationError(f"Invalid {key}")
        if self.synthetic_cost_units != 5 or self.model_id != "fake_context_bytes_v1":
            raise ValidationError("Unregistered model or synthetic cost schedule")


class FakeModelClient:
    """The actual request body is the sole experience input to generation.

    The caller must validate ContextReceipt before dispatch and journal this request
    before calling. This pure fixture response is not a model-provider API response.
    """

    def generate(
        self, request_id: str, context_bytes: bytes, edit_base: Version
    ) -> ModelResponse:
        require_id(request_id, "request_id")
        if type(context_bytes) is not bytes or type(edit_base) is not Version:
            raise ValidationError(
                "Generation requires actual context bytes and a typed edit base"
            )
        trigger = (
            decode_json(fixture_bytes("context_entries.json"))["G"].encode("ascii")
            + b"\n"
        )
        agent = TransferProgram(
            check_scope=trigger in context_bytes,
            check_confirmation=True,
            check_fresh=True,
        )
        context_sha256 = hashlib.sha256(context_bytes).hexdigest()
        return ModelResponse(
            version=replace(edit_base, agent=agent),
            request_id=request_id,
            context_sha256=context_sha256,
            synthetic_input_units=synthetic_units(len(context_bytes)),
            request_sha256=digest(
                {
                    "request_id": request_id,
                    "context_sha256": context_sha256,
                    "edit_base": edit_base.to_dict(),
                }
            ),
        )


class DurableFakeModelClient(FakeModelClient):
    """Local queryable fixture provider for response-loss recovery tests.

    The provider owns its durable response journal independently of the lab's cost
    ledger. This is a fixture capability, not a remote exactly-once guarantee.
    """

    def __init__(self, path: Path, *, fault_hook=None):
        self.path = _checked_path(Path(path))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fault_hook = fault_hook or (lambda point: None)
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS responses(request_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, response_json BLOB NOT NULL)"
            )

    @contextmanager
    def _db(self):
        _checked_path(self.path)
        for suffix in ("-wal", "-shm", "-journal"):
            _checked_path(Path(str(self.path) + suffix))
        with closing(sqlite3.connect(self.path)) as db, db:
            yield db

    def lookup(self, request_id: str) -> ModelResponse | None:
        with self._db() as db:
            row = db.execute(
                "SELECT response_json FROM responses WHERE request_id=?", (request_id,)
            ).fetchone()
        return ModelResponse.from_dict(decode_json(row[0])) if row else None

    def generate(
        self, request_id: str, context_bytes: bytes, edit_base: Version
    ) -> ModelResponse:
        expected = digest(
            {
                "request_id": request_id,
                "context_sha256": hashlib.sha256(context_bytes).hexdigest(),
                "edit_base": edit_base.to_dict(),
            }
        )
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT request_hash,response_json FROM responses WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row:
                if row[0] != expected:
                    raise ConflictError("Provider request identity reused")
                return ModelResponse.from_dict(decode_json(row[1]))
            response = super().generate(request_id, context_bytes, edit_base)
            db.execute(
                "INSERT INTO responses VALUES (?,?,?)",
                (request_id, expected, canonical_bytes(response)),
            )
        self.fault_hook("after_model_response")
        return response
