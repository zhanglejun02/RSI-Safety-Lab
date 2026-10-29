"""Read capabilities used by exporters, including real WAL and denied routes."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3

import pytest

from rsi_lab.analysis.access import ExportAccess, ReadOnlyArtifacts, readonly_database
from rsi_lab.errors import IntegrityError, PermissionDenied, ValidationError
from rsi_lab.infra.artifacts import ArtifactStore


def test_readonly_database_preserves_committed_wal_and_refuses_mutation(tmp_path):
    path = tmp_path / "controller.sqlite"
    writer = sqlite3.connect(path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE facts(value TEXT)")
        writer.execute("INSERT INTO facts VALUES ('durable')")
        writer.commit()
        assert Path(str(path) + "-wal").stat().st_size > 0
        with readonly_database(path) as db:
            assert db.execute("SELECT value FROM facts").fetchone()[0] == "durable"
            for sql in (
                "INSERT INTO facts VALUES ('bad')",
                "DROP TABLE facts",
                "PRAGMA query_only=OFF",
                "ATTACH DATABASE ':memory:' AS extra",
            ):
                with pytest.raises(sqlite3.DatabaseError):
                    db.execute(sql)
        assert writer.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1
    finally:
        writer.close()


def test_missing_readonly_inputs_are_not_initialized(tmp_path):
    missing = tmp_path / "absent" / "controller.sqlite"
    with pytest.raises(IntegrityError):
        with readonly_database(missing):
            pass
    with pytest.raises(IntegrityError):
        ReadOnlyArtifacts(tmp_path / "absent" / "objects")
    assert not (tmp_path / "absent").exists()


def test_read_transaction_remains_consistent_while_writer_appends(tmp_path):
    path = tmp_path / "controller.sqlite"
    writer = sqlite3.connect(path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE facts(value)")
        writer.execute("INSERT INTO facts VALUES (1)")
        writer.commit()
        with readonly_database(path) as db:
            assert db.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1
            writer.execute("INSERT INTO facts VALUES (2)")
            writer.commit()
            assert db.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1
        with readonly_database(path) as db:
            assert db.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 2
    finally:
        writer.close()


def test_injected_reader_cannot_relabel_different_bytes(tmp_path):
    store = ArtifactStore(tmp_path / "objects")
    ref = store.put_bytes(b"public")

    class WrongReader:
        def read_bytes(self, reference):
            return b"secret"

    access = ExportAccess(
        tmp_path, partition="dev", view="training", artifact_reader=WrongReader()
    )
    access.allow_artifacts((ref,))
    with pytest.raises(IntegrityError, match="authorized reference"):
        access.read_bytes(ref)


def test_training_partition_rejection_happens_before_any_injected_read(tmp_path):
    def poison_open(path):
        pytest.fail("Forbidden database was opened")

    class PoisonReader:
        def read_bytes(self, ref):
            pytest.fail("Forbidden artifact was read")

    for partition in ("sealed", "calibration"):
        with pytest.raises(PermissionDenied, match=partition):
            ExportAccess(
                tmp_path / "absent",
                partition=partition,
                view="training",
                database_opener=poison_open,
                artifact_reader=PoisonReader(),
            )
    assert not (tmp_path / "absent").exists()
    with pytest.raises(ValidationError, match="exclude sealed"):
        ExportAccess(
            tmp_path,
            partition="sealed",
            view="training",
            allowed_training_partitions=frozenset({"sealed"}),
        )


def test_explicit_reader_and_opener_are_used_but_nested_ref_is_not_authorized(tmp_path):
    store = ArtifactStore(tmp_path / "objects")
    forbidden = store.put_json({"sealed_label": 1})
    public = store.put_json({"features": [1, 2], "nested": forbidden})
    calls = []

    class RestrictedReader:
        def read_bytes(self, ref):
            assert ref != forbidden, "Sealed reader must never be reached"
            calls.append(("read", ref))
            return store.read_bytes(ref)

    @contextmanager
    def fake_opener(path):
        calls.append(("database", path))
        yield "read_session"

    access = ExportAccess(
        tmp_path,
        partition="dev",
        view="training",
        database_opener=fake_opener,
        artifact_reader=RestrictedReader(),
    )
    access.allow_artifacts((public,))
    with access.database() as db:
        assert db == "read_session"
    data = access.read_json(public)
    assert data["features"] == [1, 2]
    assert access.allowed_artifacts == frozenset({public})
    with pytest.raises(PermissionDenied, match="allowlist"):
        access.read_bytes(forbidden)
    with pytest.raises(PermissionDenied, match="unapproved"):
        access.assert_output_references({"nested_payload": data})
    access.assert_output_references({"feature_ref": public})
    assert calls == [("database", tmp_path / "controller.sqlite"), ("read", public)]


def test_readonly_artifacts_verify_bytes_and_reject_sidecar_symlink(tmp_path):
    store = ArtifactStore(tmp_path / "objects")
    ref = store.put_bytes(b"original")
    reader = ReadOnlyArtifacts(tmp_path / "objects")
    assert reader.read_bytes(ref) == b"original"
    path = store._path(ref.sha256)
    path.chmod(0o600)
    path.write_bytes(b"tampered")
    with pytest.raises(IntegrityError, match="digest"):
        reader.read_bytes(ref)
    database = tmp_path / "controller.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE facts(value)")
    Path(str(database) + "-journal").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(IntegrityError, match="Symbolic link"):
        with readonly_database(database):
            pass


def test_real_training_export_uses_poisoned_database_and_artifact_ports(tmp_path):
    """Instrumentation is on export_run itself, not an unused view stand-in."""
    from rsi_lab.analysis.access import artifact_references, _read_authorizer
    from rsi_lab.analysis.export import export_run
    from rsi_lab.config import LabConfig
    from rsi_lab.contracts import ArtifactRef, PrepareCursor
    from rsi_lab.preparation import PreparationState
    from rsi_lab.serialization import decode_json
    from rsi_lab.studies.closed_loop import create_run

    run = tmp_path / "run"
    create_run(
        LabConfig(stage="M3", study="m3_smoke", subject="fake", horizon_attempts=1), run
    )
    store = ArtifactStore(run / "objects")
    manifest = decode_json((run / "run_manifest.json").read_bytes())
    allowed = {ArtifactRef.from_dict(manifest["protocol"])}
    with sqlite3.connect(run / "controller.sqlite") as db:
        db.execute("CREATE TABLE sealed_supervision(label TEXT)")
        db.execute(
            "INSERT INTO sealed_supervision VALUES ('SEALED_SENTINEL_DO_NOT_READ')"
        )
        row = db.execute(
            "SELECT cursor_json FROM attempts WHERE attempt_id='main_001'"
        ).fetchone()
    cursor = PrepareCursor.from_dict(decode_json(row[0]))
    prepared = PreparationState.from_dict(
        decode_json(store.read_bytes(cursor.local_state))
    )
    allowed.update(
        (cursor.local_state, prepared.context_receipt, prepared.verification_receipt)
    )
    forbidden = store.put_json({"sealed_label": "SEALED_SENTINEL_DO_NOT_READ"})
    for name in ("sealed_eval.sqlite", "supervision.sqlite"):
        with sqlite3.connect(run / name) as db:
            db.execute("CREATE TABLE sealed_values(value TEXT)")
            db.execute(
                "INSERT INTO sealed_values VALUES ('SEALED_SENTINEL_DO_NOT_READ')"
            )
    opened, reads, selected_tables = [], [], []

    @contextmanager
    def poison_opener(path):
        assert (
            Path(path).name == "controller.sqlite"
        ), "Exporter opened a sealed database"
        opened.append(Path(path))
        with readonly_database(path) as db:

            def authorizer(action, first, second, database, trigger):
                if action == sqlite3.SQLITE_READ:
                    assert first not in (
                        "sealed_supervision",
                        "decisions",
                        "deployed_suites",
                        "probe_sessions",
                    ), first
                    selected_tables.append(first)
                return _read_authorizer(action, first, second, database, trigger)

            db.set_authorizer(authorizer)
            yield db

    class PoisonReader:
        def read_bytes(self, reference):
            assert reference != forbidden, "Exporter read a sealed artifact"
            assert (
                reference in allowed
            ), "Exporter traversed an unapproved nested artifact"
            reads.append(reference)
            return store.read_bytes(reference)

    output = tmp_path / "training"
    result = export_run(
        run,
        output,
        view="training",
        database_opener=poison_opener,
        artifact_reader=PoisonReader(),
    )
    assert result["allowed_output_artifact_refs"] == []
    assert result["supervision_rows"] == 0
    lines = [
        decode_json(line)
        for line in (output / "examples.jsonl").read_bytes().splitlines()
    ]
    assert len(lines) == 1
    assert lines[0]["candidate_label"] is None
    assert lines[0]["label_missing_reason"] == "counterfactual_not_measured"
    assert (
        lines[0]["candidate_binding"]["candidate_sha256"]
        == prepared.native_selected.artifact.sha256
    )
    assert artifact_references(lines) == ()
    assert all("SEALED_SENTINEL" not in p.read_text() for p in output.iterdir())
    assert opened == [run / "controller.sqlite"]
    assert set(reads) == allowed
    assert "attempts" in selected_tables


def test_real_training_export_rejects_sealed_before_opening_any_port(tmp_path):
    from rsi_lab.analysis.export import export_run
    from rsi_lab.contracts import ArtifactRef
    from rsi_lab.serialization import canonical_bytes

    # A metadata-only routing fixture. No database or CAS is needed or created:
    # permission denial must happen before protocol bytes or SQLite are touched.
    run = tmp_path / "sealed_run"
    run.mkdir()
    (run / "run_manifest.json").write_bytes(
        canonical_bytes(
            {
                "schema_version": 1,
                "stage": "M4",
                "protocol": ArtifactRef("0" * 64, 0),
                "arbitrary_code_execution": False,
                "hard_isolation_verified": False,
            }
        )
    )
    (run / "resolved_config.json").write_bytes(
        canonical_bytes({"schema_version": 1, "partition": "sealed"})
    )

    def poison(path):
        pytest.fail("Opened a sealed database before partition routing")

    class PoisonReader:
        def read_bytes(self, ref):
            pytest.fail("Read sealed protocol/artifact before partition routing")

    output = tmp_path / "export"
    with pytest.raises(PermissionDenied, match="sealed"):
        export_run(
            run,
            output,
            view="training",
            database_opener=poison,
            artifact_reader=PoisonReader(),
        )
    assert not output.exists()
    assert sorted(p.name for p in run.iterdir()) == [
        "resolved_config.json",
        "run_manifest.json",
    ]


def test_mixed_branch_partition_is_rejected_before_preparation_artifact_reads(tmp_path):
    from rsi_lab.analysis.export import export_run
    from rsi_lab.config import LabConfig
    from rsi_lab.contracts import ArtifactRef
    from rsi_lab.serialization import canonical_bytes, decode_json
    from rsi_lab.studies.closed_loop import create_run

    run = tmp_path / "mixed"
    create_run(
        LabConfig(stage="M3", study="m3_smoke", subject="fake", horizon_attempts=1), run
    )
    store = ArtifactStore(run / "objects")
    manifest = decode_json((run / "run_manifest.json").read_bytes())
    protocol = ArtifactRef.from_dict(manifest["protocol"])
    with sqlite3.connect(run / "controller.sqlite") as db:
        # A malformed imported branch plan cannot lower its own partition from
        # sealed to the enclosing dev run by falling through a run-wide filter.
        db.execute(
            "CREATE TABLE s0_branches(branch_id TEXT PRIMARY KEY, metadata_json BLOB)"
        )
        db.execute(
            "INSERT INTO s0_branches VALUES (?,?)",
            ("main", canonical_bytes({"branch_id": "main", "partition": "sealed"})),
        )
    reads = []

    class PoisonReader:
        def read_bytes(self, ref):
            assert (
                ref == protocol
            ), "Read preparation artifacts before checking branch partition"
            reads.append(ref)
            return store.read_bytes(ref)

    output = tmp_path / "mixed_export"
    with pytest.raises(PermissionDenied, match="Mixed-partition"):
        export_run(run, output, view="training", artifact_reader=PoisonReader())
    assert reads == [protocol]
    assert not output.exists()
