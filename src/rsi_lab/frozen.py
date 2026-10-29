"""Installed-package provenance, copied from independently preserved source inputs."""

import hashlib
from importlib.resources import files

from .errors import IntegrityError, ValidationError
from .serialization import decode_json, digest


def provenance() -> dict:
    return decode_json(files("rsi_lab").joinpath("data/provenance.json").read_bytes())


def implementation_fingerprint() -> str:
    """Fingerprint installed Python sources even when an editable tree has no commit."""
    from pathlib import Path
    root = Path(__file__).parent
    sources = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in sorted(root.rglob("*.py"))}
    return digest(sources)


def fixture_bytes(name: str) -> bytes:
    if name not in ("version_D.json", "version_E.json", "context_entries.json"):
        raise ValidationError(f"Unregistered packaged fixture: {name}")
    data = files("rsi_lab").joinpath(f"data/{name}").read_bytes()
    expected = provenance()["frozen_files"][f"tests/golden/{name}"]
    if hashlib.sha256(data).hexdigest() != expected:
        raise IntegrityError(f"Packaged fixture digest mismatch: {name}")
    return data

