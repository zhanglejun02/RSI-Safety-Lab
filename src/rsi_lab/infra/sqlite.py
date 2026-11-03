"""Shared durable write transaction for local state, activation and task effects.

Not used by the model journal, which deliberately controls its own transaction
boundaries around requests. Do not generalize different persistence semantics.
"""
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3

from .artifacts import _checked_path


@contextmanager
def durable_transaction(path):
    path = _checked_path(Path(path))
    for suffix in ("-wal", "-shm", "-journal"):
        _checked_path(Path(str(path) + suffix))
    with closing(sqlite3.connect(path, timeout=30)) as db, db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        yield db
