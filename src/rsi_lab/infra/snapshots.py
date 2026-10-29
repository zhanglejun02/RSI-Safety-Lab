"""Consistent SQLite backup artifacts and independent writable restoration."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile

from rsi_lab.contracts import ArtifactRef
from rsi_lab.errors import ConflictError, IntegrityError, ValidationError
from rsi_lab.infra.artifacts import ArtifactStore, _checked_path, _publish_bytes


SQLITE_MEDIA_TYPE = "application/vnd.sqlite3"


def _integrity_check(connection: sqlite3.Connection) -> None:
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    if rows != [("ok",)]:
        raise IntegrityError(f"SQLite integrity check failed: {rows!r}")


def _check_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        _checked_path(Path(str(path) + suffix))


def backup_sqlite(source: sqlite3.Connection | Path, store: ArtifactStore) -> ArtifactRef:
    """Capture a committed SQLite view, including committed frames in WAL.

    A caller-owned connection must have no active transaction. Its ownership
    remains with the caller. Path inputs are opened read-only and closed here.
    The resulting artifact is a standalone database, not a copy of a live file.
    """
    owns_connection = not isinstance(source, sqlite3.Connection)
    if owns_connection:
        path = _checked_path(Path(source))
        if not path.is_file():
            raise IntegrityError(f"SQLite source does not exist: {path}")
        _check_sidecars(path)
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    else:
        connection = source
    try:
        if connection.in_transaction:
            raise ValidationError("Commit or roll back the source transaction before taking a snapshot")
        with tempfile.TemporaryDirectory(prefix="rsi-sqlite-backup-") as temporary:
            target = Path(temporary) / "snapshot.sqlite"
            with closing(sqlite3.connect(target)) as backup:
                try:
                    connection.backup(backup)
                    backup.execute("PRAGMA journal_mode=DELETE")
                    _integrity_check(backup)
                except sqlite3.DatabaseError as error:
                    raise IntegrityError(f"SQLite backup failed: {error}") from error
            return store.put_bytes(target.read_bytes(), SQLITE_MEDIA_TYPE)
    finally:
        if owns_connection:
            connection.close()


def restore_sqlite(reference: ArtifactRef, store: ArtifactStore, destination: Path) -> Path:
    """Restore verified bytes to a new, independent writable database file."""
    if reference.media_type != SQLITE_MEDIA_TYPE:
        raise ValidationError(f"Expected a SQLite artifact, got {reference.media_type!r}")
    destination = _checked_path(Path(destination))
    _check_sidecars(destination)
    if destination.exists() or any(Path(str(destination) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ConflictError(f"Refusing to overwrite an existing SQLite database or sidecar: {destination}")
    data = store.read_bytes(reference)
    if not data.startswith(b"SQLite format 3\x00"):
        raise IntegrityError(f"Artifact is not a SQLite database: {reference.sha256}")
    with tempfile.TemporaryDirectory(prefix="rsi-sqlite-verify-") as temporary:
        validation_path = Path(temporary) / "database.sqlite"
        validation_path.write_bytes(data)
        connection = sqlite3.connect(validation_path.as_uri() + "?mode=ro", uri=True)
        try:
            _integrity_check(connection)
        except sqlite3.DatabaseError as error:
            raise IntegrityError(f"Invalid SQLite artifact: {reference.sha256}") from error
        finally:
            connection.close()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not _publish_bytes(data, destination, mode=0o600):
        raise ConflictError(f"SQLite restore destination already exists: {destination}")
    return destination
