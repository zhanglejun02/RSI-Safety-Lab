import hashlib
from pathlib import Path

import pytest

from rsi_lab.contracts import ArtifactRef
from rsi_lab.errors import IntegrityError, ValidationError
from rsi_lab.infra.artifacts import ArtifactStore


def test_objects_deduplicate_without_rewriting_existing_bytes(tmp_path):
    store = ArtifactStore(tmp_path / "objects")
    value = b"immutable evidence\x00\xff"
    first = store.put_bytes(value)
    path = store.root / first.sha256[:2] / first.sha256[2:]
    before = path.stat()
    second = store.put_bytes(value)
    assert first == second
    assert first.sha256 == hashlib.sha256(value).hexdigest()
    assert first.size_bytes == len(value)
    assert store.read_bytes(second) == value
    assert (path.stat().st_ino, path.stat().st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert list(path.parent.iterdir()) == [path]


def test_json_objects_use_stable_utf8_bytes(tmp_path):
    store = ArtifactStore(tmp_path / "objects")
    reference = store.put_json({"z": [2, 1], "a": "确认"})
    assert store.read_bytes(reference) == '{"a":"确认","z":[2,1]}'.encode()
    assert reference.media_type == "application/json"
    assert reference == store.put_json({"a": "确认", "z": [2, 1]})


def test_read_and_dedup_detect_corruption_instead_of_repairing(tmp_path):
    store = ArtifactStore(tmp_path / "objects")
    reference = store.put_bytes(b"safe")
    path = store.root / reference.sha256[:2] / reference.sha256[2:]
    path.chmod(0o600)
    path.write_bytes(b"evil")
    with pytest.raises(IntegrityError, match="digest"):
        store.read_bytes(reference)
    with pytest.raises(IntegrityError, match="digest"):
        store.put_bytes(b"safe")
    assert path.read_bytes() == b"evil"


def test_read_detects_wrong_size_and_missing_objects(tmp_path):
    store = ArtifactStore(tmp_path / "objects")
    reference = store.put_bytes(b"data")
    wrong_size = ArtifactRef(sha256=reference.sha256, size_bytes=5)
    with pytest.raises(IntegrityError, match="size"):
        store.read_bytes(wrong_size)
    missing = ArtifactRef(sha256="0" * 64, size_bytes=0)
    with pytest.raises(IntegrityError, match="Cannot read"):
        store.read_bytes(missing)


def test_rejects_symlink_root_shards_and_objects(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(external, target_is_directory=True)
    with pytest.raises(IntegrityError, match="Symbolic"):
        ArtifactStore(linked_root)
    store = ArtifactStore(tmp_path / "objects")
    data = b"payload"
    digest = hashlib.sha256(data).hexdigest()
    shard = store.root / digest[:2]
    shard.symlink_to(external, target_is_directory=True)
    with pytest.raises(IntegrityError, match="Symbolic"):
        store.put_bytes(data)
    shard.unlink()
    shard.mkdir()
    victim = external / "victim"
    victim.write_bytes(data)
    (shard / digest[2:]).symlink_to(victim)
    with pytest.raises(IntegrityError, match="Symbolic"):
        store.read_bytes(ArtifactRef(sha256=digest, size_bytes=len(data)))
    assert victim.read_bytes() == data


def test_rejects_symlink_ancestor_and_non_bytes(tmp_path):
    store = ArtifactStore(tmp_path / "objects")
    with pytest.raises(ValidationError, match="bytes"):
        store.put_bytes("text")
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "ancestor"
    linked.symlink_to(actual, target_is_directory=True)
    with pytest.raises(IntegrityError, match="Symbolic"):
        ArtifactStore(linked / "objects")


def test_new_store_instance_reads_existing_objects(tmp_path):
    reference = ArtifactStore(tmp_path / "objects").put_bytes(b"")
    assert ArtifactStore(tmp_path / "objects").read_bytes(reference) == b""
