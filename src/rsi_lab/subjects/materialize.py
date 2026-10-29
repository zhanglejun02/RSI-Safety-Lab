"""Closed snapshot materialization into staging; never changes a branch head.

The trusted controller independently checks these files before activating a
snapshot. Path checks are fixture safeguards, not an OS isolation claim.
"""

import hashlib
from pathlib import Path

from rsi_lab.contracts import SnapshotManifest
from rsi_lab.errors import IntegrityError
from rsi_lab.infra.artifacts import _checked_path, _publish_bytes
from rsi_lab.infra.snapshots import restore_sqlite
from rsi_lab.lifecycle import MaterializationReceipt
from rsi_lab.serialization import decode_json, digest


class SnapshotMaterializer:
    def __init__(self, root: Path, store):
        self.root = _checked_path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = store

    def path(self, command):
        return _checked_path(self.root / digest(command.command_id))

    def materialize(self, command):
        directory = self.path(command)
        directory.mkdir(exist_ok=True)
        expected = {"manifest.json", "environment.sqlite"}
        if not {p.name for p in directory.iterdir()} <= expected:
            raise IntegrityError("Unapproved staging files")
        manifest_bytes = self.store.read_bytes(command.target_manifest)
        manifest = SnapshotManifest.from_dict(decode_json(manifest_bytes))
        path = _checked_path(directory / "manifest.json")
        if not path.exists():
            _publish_bytes(manifest_bytes, path)
        elif path.read_bytes() != manifest_bytes:
            raise IntegrityError("Existing staged manifest differs")
        environment_path = _checked_path(directory / "environment.sqlite")
        if not environment_path.exists():
            restore_sqlite(manifest.environment, self.store, environment_path)
        return MaterializationReceipt(
            command.command_id, command.target_manifest, manifest.environment
        )


def verify_staging(materializer, command, receipt):
    directory = materializer.path(command)
    if (
        receipt.command_id != command.command_id
        or receipt.manifest != command.target_manifest
    ):
        raise IntegrityError("Materialization receipt command/manifest mismatch")
    if not directory.is_dir() or {p.name for p in directory.iterdir()} != {
        "manifest.json",
        "environment.sqlite",
    }:
        raise IntegrityError("Missing or extra staging files")
    manifest_bytes = _checked_path(directory / "manifest.json").read_bytes()
    if (
        len(manifest_bytes) != command.target_manifest.size_bytes
        or hashlib.sha256(manifest_bytes).hexdigest() != command.target_manifest.sha256
    ):
        raise IntegrityError("Actual staged manifest digest mismatch")
    manifest = SnapshotManifest.from_dict(decode_json(manifest_bytes))
    environment_bytes = _checked_path(directory / "environment.sqlite").read_bytes()
    if receipt.environment != manifest.environment or (
        len(environment_bytes) != manifest.environment.size_bytes
        or hashlib.sha256(environment_bytes).hexdigest() != manifest.environment.sha256
    ):
        raise IntegrityError("Actual staged environment digest mismatch")
    return manifest, manifest_bytes, environment_bytes
