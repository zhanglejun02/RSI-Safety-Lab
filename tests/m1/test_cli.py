import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from rsi_lab.config import LabConfig, PairingConfig, ResourcesConfig, load_config
from rsi_lab.contracts import ForkPoint, PairingMode, Partition
from rsi_lab.engine.demo import create_run, inspect_run, resume_run
from rsi_lab.errors import ConflictError, IntegrityError, UnsupportedCapability, ValidationError
from rsi_lab.frozen import fixture_bytes, provenance
from rsi_lab.serialization import canonical_bytes
from rsi_lab.wiring import preflight


ROOT = Path(__file__).resolve().parents[2]


def cli(*args):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    return subprocess.run([sys.executable, "-m", "rsi_lab", *map(str, args)],
                          env=env, cwd=ROOT, text=True, capture_output=True, timeout=30)


def test_config_roundtrip_and_closed_input(tmp_path):
    config = load_config(ROOT / "configs/m1_state_demo.toml")
    assert LabConfig.from_dict(config.to_dict()) == config
    bad = tmp_path / "bad.toml"
    bad.write_text('schema_version = 1\nsubject = "os.system"\n')
    with pytest.raises(UnsupportedCapability):
        preflight(load_config(bad))
    bad.write_text('schema_version = 1\nunknown = "value"\n')
    with pytest.raises(ValidationError):
        load_config(bad)
    bad.write_text('schema_version = true\n')
    with pytest.raises(ValidationError):
        load_config(bad)


def test_capability_preflight_never_silently_falls_back():
    with pytest.raises(UnsupportedCapability, match="isolation"):
        preflight(replace(LabConfig(), arbitrary_code_execution=True))
    with pytest.raises(UnsupportedCapability, match="sealed"):
        preflight(replace(LabConfig(), partition=Partition.SEALED))
    with pytest.raises(ValidationError, match="pre_verification"):
        PairingConfig(PairingMode.FIXED_CANDIDATE, ForkPoint.PRE_DEPLOY, True)
    with pytest.raises(UnsupportedCapability, match="M2"):
        preflight(replace(LabConfig(), pairing=PairingConfig(PairingMode.FIXED_CANDIDATE, ForkPoint.PRE_VERIFICATION)))


def test_real_cli_stop_and_cross_process_resume(tmp_path):
    run = tmp_path / "run"
    created = cli("run", "--config", ROOT / "configs/m1_state_demo.toml", "--output", run, "--stop-after", 2)
    assert created.returncode == 0, created.stderr
    initial = json.loads(created.stdout)
    assert initial["subject_executed"] is False
    assert initial["risk"] is None
    assert initial["accounting"]["total_synthetic_units"] == 0
    assert all(a["phase"] == "preparing" for a in initial["state"]["attempts"])
    resumed = cli("resume", "--run", run)
    assert resumed.returncode == 0, resumed.stderr
    result = json.loads(resumed.stdout)
    assert all(a["phase"] == "prepared" for a in result["state"]["attempts"])
    assert result["run_id"] == initial["run_id"]
    again = cli("resume", "--run", run)
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout) == result
    with sqlite3.connect(run / "controller.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 8
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 8
        assert conn.execute("SELECT COUNT(*) FROM cost_events").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM exposure_events").fetchone()[0] == 0
    inspected = cli("inspect", "--run", run)
    assert inspected.returncode == 0, inspected.stderr
    assert json.loads(inspected.stdout)["risk"] is None
    conflict = cli("run", "--config", ROOT / "configs/m1_state_demo.toml", "--output", run)
    assert conflict.returncode == 4
    assert "use resume" in conflict.stderr


def test_resume_rejects_changed_config_and_protocol(tmp_path):
    run = tmp_path / "run"
    create_run(LabConfig(), run, stop_after=1)
    raw = json.loads((run / "resolved_config.json").read_text())
    raw["random"]["master_seed"] += 1
    (run / "resolved_config.json").write_bytes(canonical_bytes(raw))
    with pytest.raises(IntegrityError, match="configuration digest"):
        resume_run(run)


def test_run_refuses_unknown_manifest_fields(tmp_path):
    run = tmp_path / "run"
    create_run(LabConfig(), run)
    raw = json.loads((run / "run_manifest.json").read_text())
    raw["labels"] = "do not import"
    (run / "run_manifest.json").write_bytes(canonical_bytes(raw))
    with pytest.raises(ValidationError, match="manifest schema"):
        inspect_run(run)


def test_doctor_does_not_claim_missing_science():
    result = cli("doctor")
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert not report["arbitrary_code_execution"]
    assert not report["hard_isolation_verified"]
    assert not report["gpu_required"]
    assert "real_model_adapter" in report["not_implemented"]
    assert "FakeSubject" in report["implemented"]


def test_package_fixture_bytes_match_independent_golden():
    for name in ("version_D.json", "version_E.json", "context_entries.json"):
        assert fixture_bytes(name) == (ROOT / "tests/golden" / name).read_bytes()
    assert provenance() == json.loads((ROOT / "docs/specs/source_manifest.json").read_text())


def test_no_hidden_creation_on_inspect(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(ValidationError):
        inspect_run(missing)
    assert not missing.exists()


def test_inspect_does_not_recreate_missing_objects(tmp_path):
    run = tmp_path / "run"
    create_run(LabConfig(), run)
    (run / "objects").rename(run / "objects-backup")
    with pytest.raises(IntegrityError, match="object directory"):
        inspect_run(run)
    assert not (run / "objects").exists()


def test_config_rejects_budget_overflow_before_run_creation():
    with pytest.raises(ValidationError, match="64-bit"):
        ResourcesConfig(2**63)


def test_resume_refuses_changed_implementation_fingerprint(tmp_path):
    run = tmp_path / "run"
    create_run(LabConfig(), run)
    path = run / "run_manifest.json"
    manifest = json.loads(path.read_text())
    manifest["implementation_sha256"] = "0" * 64
    path.write_bytes(canonical_bytes(manifest))
    with pytest.raises(IntegrityError, match="Implementation"):
        resume_run(run)

