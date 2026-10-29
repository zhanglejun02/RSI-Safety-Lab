"""Installed entry-point behavior, registered protocols and explicit limitations."""

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from rsi_lab.config import load_config
from rsi_lab.contracts import ForkPoint, PairingMode
from rsi_lab.errors import UnsupportedCapability, ValidationError
from rsi_lab.wiring import preflight

ROOT = Path(__file__).resolve().parents[2]


def cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "rsi_lab", *map(str, args)],
        cwd=ROOT,
        env=dict(os.environ, PYTHONPATH=str(ROOT / "src")),
        capture_output=True,
        text=True,
        timeout=90,
    )


@pytest.mark.parametrize(
    "study,units",
    [
        ("s0_smoke", 30),
        ("s0_q_pairs", 84),
        ("s0_v_fixed_pool", 90),
        ("s0_probe_admission", 150),
    ],
)
def test_all_s0_cli_views_resume_and_exports(tmp_path, study, units):
    config = ROOT / "configs" / f"{study}.toml"
    path = tmp_path / "run"
    for action, args in [
        ("validate", ("--config", config)),
        ("run", ("--config", config, "--output", path)),
    ]:
        result = cli(action, *args)
        assert result.returncode == 0, result.stderr
    initial = json.loads(result.stdout)
    assert initial["accounting"]["total_synthetic_units"] == units
    for action in ("inspect", "resume"):
        result = cli(action, "--run", path)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == initial
    for view in ("analysis", "training"):
        out = tmp_path / view
        result = cli("export", "--run", path, "--output", out, "--view", view)
        assert result.returncode == 0, result.stderr
        exported = json.loads(result.stdout)
        assert exported["source_stage"] == "M4" and exported["view"] == view
        assert exported["supervision_rows"] == 0
        assert (out / "export_manifest.json").is_file()
        assert (
            cli("export", "--run", path, "--output", out, "--view", view).returncode
            == 4
        )


def test_s0_preflight_rejects_protocol_changes_before_execution(tmp_path):
    config = load_config(ROOT / "configs/s0_probe_admission.toml")
    for values in (
        {"arbitrary_code_execution": True},
        {"probe_replicates": 3},
        {"probe_horizon_attempts": 3},
        {"frozen_components": ("q", "s")},
        {"allowed_updates": ("agent", "q")},
        {"guard_mode": "enforce"},
    ):
        with pytest.raises(UnsupportedCapability):
            preflight(replace(config, **values))
    for name in ("s0_q_pairs", "s0_v_fixed_pool"):
        with pytest.raises(UnsupportedCapability, match="native"):
            preflight(
                replace(
                    load_config(ROOT / "configs" / f"{name}.toml"),
                    admission_policy="reject_all",
                )
            )
    with pytest.raises(ValidationError):
        replace(config, horizon_attempts=0)
    with pytest.raises(ValidationError):
        replace(
            load_config(ROOT / "configs/s0_v_fixed_pool.toml"),
            pairing=replace(
                config.pairing,
                mode=PairingMode.FIXED_CANDIDATE,
                fork_at=ForkPoint.PRE_DEPLOY,
                verification_intervention=True,
            ),
        )
    out = tmp_path / "must_not_exist"
    result = cli(
        "run",
        "--config",
        ROOT / "configs/s0_smoke.toml",
        "--output",
        out,
        "--stop-after",
        2,
    )
    assert result.returncode == 2 and not out.exists()


def test_untrusted_code_hard_isolation_backend():
    pytest.skip(
        "No verified OS isolation backend is installed; arbitrary generated code is rejected by preflight"
    )
