"""One durable attempt loop shared by platform runs and the bounded Task A study.

Drivers own task/system details; the loop owns ordered attempts, checksummed
checkpoints and explicit missing slots. An independent stream is never supplied
as feedback by this service. Persistent drivers must use idempotent operations.
"""
from pathlib import Path
from typing import Protocol
from rsi_lab.errors import ConflictError, IntegrityError, LabError
from rsi_lab.infra.artifacts import _checked_path, _publish_bytes
from rsi_lab.serialization import canonical_bytes, decode_json, digest


def save_record(path, value):
    path = _checked_path(Path(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {"record": value, "sha256": digest(value)}
    data = canonical_bytes(envelope)
    if not _publish_bytes(data, path) and path.read_bytes() != data:
        raise ConflictError(f"Immutable continuation record changed: {path.name}")


def read_record(path):
    value = decode_json(_checked_path(Path(path)).read_bytes())
    if set(value) != {"record", "sha256"} or digest(value["record"]) != value["sha256"]:
        raise IntegrityError("Continuation record checksum mismatch")
    return value["record"]


class AttemptDriver(Protocol):
    def execute(self, index: int) -> dict: ...
    def missing(self, index: int, reason: str) -> dict: ...
    def restore(self, index: int, record: dict): ...


class ContinuationService:
    """No fixture/system/task imports; index counts attempts including rejections."""
    def run(self, driver: AttemptDriver, horizon: int, directory: Path, *, identity: dict):
        if type(horizon) is not int or horizon < 1:
            from rsi_lab.errors import ValidationError
            raise ValidationError("Continuation requires a positive registered horizon")
        directory = _checked_path(Path(directory))
        directory.mkdir(parents=True, exist_ok=True)
        save_record(directory / "continuation.json", {"identity": identity, "attempts": list(range(1, horizon + 1))})
        rows, stopped = [], None
        for index in range(1, horizon + 1):
            path = directory / f"attempt-{index:04}.json"
            if path.exists():
                record = read_record(path)
                driver.restore(index, record)
            else:
                try:
                    record = driver.missing(index, stopped) if stopped else driver.execute(index)
                except (IntegrityError, ConflictError):
                    raise
                except LabError as exc:
                    record = driver.missing(index, type(exc).__name__)
                # The same JSON projection is returned on first execution and replay.
                record = decode_json(canonical_bytes(record))
                save_record(path, record)
            stopped = record.get("missing_reason")
            rows.append(record)
        return tuple(rows)
