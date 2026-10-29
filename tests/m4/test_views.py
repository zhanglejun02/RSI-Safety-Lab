"""Admission and pause cannot gain sealed inputs through nested fields or stores."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import shutil
import sqlite3

import pytest

from rsi_lab.config import load_config
from rsi_lab.engine.commit import LifecycleController
from rsi_lab.engine.state import StateStore
from rsi_lab.errors import ValidationError
from rsi_lab.infra.artifacts import ArtifactStore
from rsi_lab.infra.journal import Journal
from rsi_lab.lifecycle import AdmissionView
from rsi_lab.serialization import canonical_bytes
from rsi_lab.studies.s0 import create_run, resume_run

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "path",
    [
        (),
        ("binding",),
        ("binding", "candidate_occurrence"),
        ("binding", "candidate_occurrence", "artifact"),
        ("context_receipt",),
        ("verification_receipt",),
    ],
)
def test_admission_view_rejects_nested_sealed_fields(tmp_path, path):
    run = tmp_path / "run"
    result = create_run(
        replace(load_config(ROOT / "configs/s0_smoke.toml"), horizon_attempts=1), run
    )
    store = ArtifactStore(run / "objects")
    state = StateStore(run, store)
    journal = Journal(run / "controller.sqlite")
    branch = result["plans"][0]["branch_id"]
    controller = LifecycleController(state, store, journal, (branch,))
    view = controller.view(f"{branch}_001")
    assert AdmissionView.from_dict(view.to_dict()) == view
    assert all(
        not hasattr(view, name)
        for name in ("store", "read_bytes", "state", "controller")
    )
    contaminated = deepcopy(view.to_dict())
    target = contaminated
    for key in path:
        target = target[key]
    target["sealed_label"] = "SEALED_SENTINEL_DO_NOT_READ"
    with pytest.raises(ValidationError):
        AdmissionView.from_dict(contaminated)
    assert b"SEALED_SENTINEL" not in canonical_bytes(view)


def test_sealed_data_cannot_change_admission_or_pause(tmp_path, monkeypatch):
    class StopAfterResponse(BaseException):
        pass

    def stop(point):
        if point == "after_model_response":
            raise StopAfterResponse()

    # Both executions begin with the same registered lineage and stored provider
    # response. Only inaccessible label stores differ between the two copies.
    seed = tmp_path / "seed"
    config = replace(
        load_config(ROOT / "configs/s0_smoke.toml"), context_budget_units=0
    )
    with pytest.raises(StopAfterResponse):
        create_run(config, seed, fault_hook=stop)
    paths = [tmp_path / "claims_safe", tmp_path / "claims_harmful"]
    forbidden = []
    for path, label in zip(paths, (0, 1)):
        shutil.copytree(seed, path)
        for name in ("sealed_eval.sqlite", "controller.sqlite"):
            with sqlite3.connect(path / name) as db:
                db.execute("CREATE TABLE sealed_supervision(risk INTEGER)")
                db.execute("INSERT INTO sealed_supervision VALUES (?)", (label,))
        forbidden.append(
            ArtifactStore(path / "objects").put_json({"sealed_label": label})
        )
    original_connect = sqlite3.connect
    opened = []

    def guarded_connect(database, *args, **kwargs):
        assert "sealed_eval.sqlite" not in str(database), "Read sealed database"
        opened.append(str(database))
        db = original_connect(database, *args, **kwargs)

        def authorizer(action, first, second, database_name, trigger):
            if action == sqlite3.SQLITE_READ:
                assert first != "sealed_supervision", "Read sealed table"
            return sqlite3.SQLITE_OK

        db.set_authorizer(authorizer)
        return db

    original_read = ArtifactStore.read_bytes

    def guarded_read(store, reference):
        assert reference not in forbidden, "Read sealed artifact"
        return original_read(store, reference)

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", guarded_connect)
        patch.setattr(ArtifactStore, "read_bytes", guarded_read)
        results = [resume_run(path) for path in paths]

    def measured(result):
        return {
            "rounds": [
                {
                    "branch_id": row["branch_id"],
                    "attempt_index": row["attempt_index"],
                    "status": row["status"],
                    "reason": row["reason"],
                    "verdict": row["decision"]["verdict"] if row["decision"] else None,
                    "public_evaluation": row["public_evaluation"],
                }
                for row in result["rounds"]
            ],
            "paused_branches": result["paused_branches"],
            "accounting": result["accounting"],
        }

    assert measured(results[0]) == measured(results[1])
    assert results[0]["rounds"][0]["decision"]["verdict"] == "accept"
    assert results[0]["rounds"][0]["public_evaluation"]["risk"] == 1
    assert results[0]["paused_branches"]
    assert all(
        row["reason"] == "unsafe_current_pause" for row in results[0]["rounds"][1:]
    )
    assert opened and all("sealed_eval.sqlite" not in path for path in opened)
