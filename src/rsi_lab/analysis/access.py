"""Partition-routed, read-only capabilities for research export.

These are application boundaries for trusted exporters, not process isolation.
Partition metadata must be validated against the run protocol before constructing
an access object. Nested hashes never grant additional read capabilities.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
from typing import Iterator

from rsi_lab.contracts import ArtifactRef
from rsi_lab.errors import IntegrityError, PermissionDenied, ValidationError
from rsi_lab.infra.artifacts import _checked_path
from rsi_lab.serialization import decode_json, primitive

PARTITIONS = frozenset({"train", "dev", "calibration", "sealed"})
TRAINING_PARTITIONS = frozenset({"train", "dev"})


class ReadOnlyArtifacts:
    """Read a pre-existing CAS without constructing its writable repository."""

    def __init__(self, root: Path):
        self.root = _checked_path(Path(root))
        if not self.root.is_dir():
            raise IntegrityError(f"Artifact directory does not exist: {self.root}")

    def read_bytes(self, reference: ArtifactRef) -> bytes:
        if type(reference) is not ArtifactRef:
            raise ValidationError("Read-only artifacts require a typed ArtifactRef")
        path = _checked_path(self.root / reference.sha256[:2] / reference.sha256[2:])
        flags = (
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise IntegrityError(
                f"Cannot read artifact {reference.sha256}: {exc}"
            ) from exc
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size != reference.size_bytes
            ):
                raise IntegrityError(f"Artifact size/type mismatch: {reference.sha256}")
            data = stream.read(reference.size_bytes + 1)
        if (
            len(data) != reference.size_bytes
            or hashlib.sha256(data).hexdigest() != reference.sha256
        ):
            raise IntegrityError(f"Artifact digest mismatch: {reference.sha256}")
        return data


def _read_authorizer(action, first, second, database, trigger):
    allowed = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_TRANSACTION,
        sqlite3.SQLITE_RECURSIVE,
    }
    if action == sqlite3.SQLITE_FUNCTION and second == "load_extension":
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA:
        return (
            sqlite3.SQLITE_OK
            if first in ("table_info", "table_xinfo")
            else sqlite3.SQLITE_DENY
        )
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY


@contextmanager
def readonly_database(path: Path) -> Iterator[sqlite3.Connection]:
    """One consistent read transaction; preserve committed WAL, never initialize."""
    path = _checked_path(Path(path))
    if not path.is_file():
        raise IntegrityError(f"Export database does not exist: {path}")
    for suffix in ("-wal", "-shm", "-journal"):
        _checked_path(Path(str(path) + suffix))
    with closing(
        sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)
    ) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        db.set_authorizer(_read_authorizer)
        try:
            yield db
        finally:
            db.rollback()


def artifact_references(value) -> tuple[ArtifactRef, ...]:
    """Inspect output references without reading or authorizing their contents."""
    found = []

    def visit(item):
        if type(item) is dict:
            if "sha256" in item and "size_bytes" in item:
                found.append(ArtifactRef.from_dict(item))
                return
            for child in item.values():
                visit(child)
        elif type(item) is list:
            for child in item:
                visit(child)

    visit(primitive(value))
    return tuple(found)


class ExportAccess:
    """Explicit routing and artifact allowlist used by the real export path.

    database_opener(path) must return a connection context manager. An injected
    artifact_reader needs only read_bytes(ArtifactRef). Rejected training sources
    reach neither injected capability, including through nested references.
    """

    def __init__(
        self,
        run_dir: Path,
        *,
        partition: str,
        view: str,
        allowed_training_partitions: frozenset[str] = TRAINING_PARTITIONS,
        database_opener=None,
        artifact_reader=None,
    ):
        if partition not in PARTITIONS or view not in ("analysis", "training"):
            raise ValidationError("Unknown export partition or view")
        if type(
            allowed_training_partitions
        ) is not frozenset or not allowed_training_partitions <= (
            PARTITIONS - {"sealed"}
        ):
            raise ValidationError("Training partitions must explicitly exclude sealed")
        # This check precedes even construction of default filesystem readers.
        if view == "training" and partition not in allowed_training_partitions:
            raise PermissionDenied(f"Training export cannot read partition {partition}")
        self.run_dir = _checked_path(Path(run_dir))
        self.partition, self.view = partition, view
        self._opener = database_opener or readonly_database
        self._reader = artifact_reader
        self._allowed: set[ArtifactRef] = set()

    @contextmanager
    def database(self):
        with self._opener(self.run_dir / "controller.sqlite") as db:
            yield db

    @property
    def allowed_artifacts(self) -> frozenset[ArtifactRef]:
        return frozenset(self._allowed)

    def allow_artifacts(self, references) -> None:
        references = tuple(references)
        if any(type(ref) is not ArtifactRef for ref in references):
            raise ValidationError("Export artifact grants require typed references")
        self._allowed.update(references)

    def read_bytes(self, reference: ArtifactRef) -> bytes:
        if type(reference) is not ArtifactRef:
            raise ValidationError("Export reads require a typed ArtifactRef")
        if reference not in self._allowed:
            raise PermissionDenied(
                f"Artifact outside export allowlist: {reference.sha256}"
            )
        if self._reader is None:
            self._reader = ReadOnlyArtifacts(self.run_dir / "objects")
        data = self._reader.read_bytes(reference)
        if (
            type(data) is not bytes
            or len(data) != reference.size_bytes
            or hashlib.sha256(data).hexdigest() != reference.sha256
        ):
            raise IntegrityError(
                "Export reader returned bytes outside the authorized reference"
            )
        return data

    def read_json(self, reference: ArtifactRef) -> dict:
        return decode_json(self.read_bytes(reference))

    def assert_output_references(self, payload) -> None:
        for reference in artifact_references(payload):
            if reference not in self._allowed:
                raise PermissionDenied(
                    f"Output contains an unapproved artifact reference: {reference.sha256}"
                )
