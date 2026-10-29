from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from rsi_lab.errors import ConflictError, IntegrityError, ValidationError
from rsi_lab.infra.artifacts import ArtifactStore
from rsi_lab.infra.snapshots import SQLITE_MEDIA_TYPE, backup_sqlite, restore_sqlite


def create_database(path):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE facts(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO facts VALUES (1, 'initial')")
    connection.commit()
    return connection


def read_facts(path):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("SELECT id, value FROM facts ORDER BY id").fetchall()


def test_backup_includes_committed_but_uncheckpointed_wal(tmp_path):
    source = tmp_path / "live.sqlite"
    store = ArtifactStore(tmp_path / "objects")
    with closing(create_database(source)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO facts VALUES (2, 'in wal')")
        writer.commit()
        assert Path(str(source) + "-wal").stat().st_size > 0
        # Prove that copying just the live main file loses this committed row.
        raw_copy = tmp_path / "wrong-copy.sqlite"
        raw_copy.write_bytes(source.read_bytes())
        assert read_facts(raw_copy) == [(1, "initial")]
        reference = backup_sqlite(source, store)
        restored = restore_sqlite(reference, store, tmp_path / "restored.sqlite")
        assert read_facts(restored) == [(1, "initial"), (2, "in wal")]
        assert not Path(str(restored) + "-wal").exists()


def test_restored_branches_are_writable_and_independent(tmp_path):
    store = ArtifactStore(tmp_path / "objects")
    source = tmp_path / "source.sqlite"
    with closing(create_database(source)) as connection:
        reference = backup_sqlite(connection, store)
        # A caller-owned connection remains usable after backup.
        assert connection.execute("SELECT COUNT(*) FROM facts").fetchone() == (1,)
    left = restore_sqlite(reference, store, tmp_path / "L" / "world.sqlite")
    right = restore_sqlite(reference, store, tmp_path / "R" / "world.sqlite")
    with closing(sqlite3.connect(left)) as connection:
        connection.execute("UPDATE facts SET value='left-only' WHERE id=1")
        connection.commit()
    assert read_facts(left) == [(1, "left-only")]
    assert read_facts(right) == [(1, "initial")]
    assert read_facts(source) == [(1, "initial")]
    assert store.read_bytes(reference) == right.read_bytes()
    assert left.stat().st_ino != right.stat().st_ino


def test_open_source_transaction_is_rejected_without_hanging(tmp_path):
    store = ArtifactStore(tmp_path / "objects")
    with closing(create_database(tmp_path / "source.sqlite")) as connection:
        connection.execute("INSERT INTO facts VALUES (2, 'uncommitted')")
        with pytest.raises(ValidationError, match="transaction"):
            backup_sqlite(connection, store)
        connection.rollback()
        assert backup_sqlite(connection, store).size_bytes > 0


def test_restore_refuses_existing_file_or_stray_wal(tmp_path):
    store = ArtifactStore(tmp_path / "objects")
    with closing(create_database(tmp_path / "source.sqlite")) as connection:
        reference = backup_sqlite(connection, store)
    target = tmp_path / "restored.sqlite"
    target.write_bytes(b"must survive")
    with pytest.raises(ConflictError, match="overwrite"):
        restore_sqlite(reference, store, target)
    assert target.read_bytes() == b"must survive"
    target.unlink()
    Path(str(target) + "-wal").write_bytes(b"existing journal")
    with pytest.raises(ConflictError, match="sidecar"):
        restore_sqlite(reference, store, target)


def test_restore_rejects_wrong_media_type_or_invalid_sqlite(tmp_path):
    store = ArtifactStore(tmp_path / "objects")
    with pytest.raises(ValidationError, match="SQLite artifact"):
        restore_sqlite(store.put_bytes(b"text"), store, tmp_path / "wrong.sqlite")
    with pytest.raises(IntegrityError, match="not a SQLite"):
        restore_sqlite(store.put_bytes(b"text", SQLITE_MEDIA_TYPE), store, tmp_path / "invalid.sqlite")
    with pytest.raises(IntegrityError, match="Invalid SQLite"):
        restore_sqlite(store.put_bytes(b"SQLite format 3\x00" + b"x" * 100, SQLITE_MEDIA_TYPE), store, tmp_path / "broken.sqlite")
    assert not (tmp_path / "invalid.sqlite").exists()
    assert not (tmp_path / "broken.sqlite").exists()


def test_snapshot_and_restore_reject_path_symlinks(tmp_path):
    store = ArtifactStore(tmp_path / "objects")
    source = tmp_path / "source.sqlite"
    with closing(create_database(source)) as connection:
        reference = backup_sqlite(connection, store)
    alias = tmp_path / "alias.sqlite"
    alias.symlink_to(source)
    with pytest.raises(IntegrityError, match="Symbolic"):
        backup_sqlite(alias, store)
    with pytest.raises(IntegrityError, match="Symbolic"):
        restore_sqlite(reference, store, alias)


def test_invalid_source_database_raises_integrity_error(tmp_path):
    source = tmp_path / "broken.sqlite"
    source.write_bytes(b"not sqlite")
    with pytest.raises(IntegrityError, match="backup failed"):
        backup_sqlite(source, ArtifactStore(tmp_path / "objects"))
