"""Immutable, verified content-addressed objects for the trusted local controller.

The checks prevent accidental corruption and obvious path substitution. They are
not a replacement for OS isolation against another process running as this user.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import uuid
from typing import Any

from rsi_lab.contracts import ArtifactRef
from rsi_lab.errors import IntegrityError, ValidationError
from rsi_lab.serialization import canonical_bytes


def _checked_path(path: Path) -> Path:
    """Reject symlinks after normalizing macOS's fixed temp-directory aliases."""
    path = Path(os.path.abspath(path))
    # macOS's tempfile.gettempdir() uses /var; /var and /tmp are OS aliases for
    # /private/var and /private/tmp. Normalize only these exact system aliases,
    # not arbitrary user-created symlinks or object-store descendants.
    for alias in (Path("/var"), Path("/tmp")):
        target = Path("/private") / alias.name
        if path != alias and path.is_relative_to(alias) and alias.is_symlink() and alias.resolve() == target:
            path = target / path.relative_to(alias)
    for part in (*reversed(path.parents), path):
        if part.is_symlink():
            raise IntegrityError(f"Symbolic link is not allowed: {part}")
    return path


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_bytes(data: bytes, destination: Path, *, mode: int = 0o444) -> bool:
    """Publish durable bytes without replacing an existing directory entry.

    Return False if another writer already published this destination. Callers
    must verify that existing object before treating the operation as successful.
    """
    destination = _checked_path(destination)
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        try:
            # link() is an atomic, no-replace publication on the same filesystem.
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError:
            return False
        _fsync_directory(destination.parent)
        return True
    finally:
        temporary.unlink(missing_ok=True)


class ArtifactStore:
    """Controller-owned CAS. Knowledge of a digest is not an access capability."""

    def __init__(self, root: Path):
        self.root = _checked_path(Path(root))
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise IntegrityError(f"Artifact root is not a directory: {self.root}")

    def _path(self, digest: str, *, create: bool = False) -> Path:
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValidationError("Artifact SHA-256 must contain 64 lowercase hexadecimal characters")
        _checked_path(self.root)
        shard = _checked_path(self.root / digest[:2])
        if create:
            created = not shard.exists()
            shard.mkdir(exist_ok=True)
            if created:
                _fsync_directory(self.root)
        return _checked_path(shard / digest[2:])

    def put_bytes(self, data: bytes, media_type: str = "application/octet-stream") -> ArtifactRef:
        if not isinstance(data, bytes):
            raise ValidationError("Artifact content must be bytes")
        reference = ArtifactRef(
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            media_type=media_type,
        )
        destination = self._path(reference.sha256, create=True)
        if not _publish_bytes(data, destination):
            # Never overwrite a corrupt object, even when the requested bytes
            # could repair it. Corruption must remain observable to the caller.
            self.read_bytes(reference)
        return reference

    def put_json(self, value: Any) -> ArtifactRef:
        return self.put_bytes(canonical_bytes(value), "application/json")

    def read_bytes(self, reference: ArtifactRef) -> bytes:
        if not isinstance(reference, ArtifactRef):
            raise ValidationError("Expected an ArtifactRef, not a path or raw digest")
        path = self._path(reference.sha256)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise IntegrityError(f"Cannot read artifact {reference.sha256}: {error}") from error
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise IntegrityError(f"Artifact is not a regular file: {reference.sha256}")
            if metadata.st_size != reference.size_bytes:
                raise IntegrityError(f"Artifact size mismatch: {reference.sha256}")
            data = stream.read(reference.size_bytes + 1)
        if len(data) != reference.size_bytes or hashlib.sha256(data).hexdigest() != reference.sha256:
            raise IntegrityError(f"Artifact digest mismatch: {reference.sha256}")
        return data
