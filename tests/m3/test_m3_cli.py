"""Real CLI coverage for the M3 dispatcher and its registered protocols."""

import json
import os
from pathlib import Path
import subprocess
import sys
from dataclasses import replace

import pytest

from rsi_lab.config import load_config
from rsi_lab.contracts import CandidateOrigin, Partition
from rsi_lab.errors import UnsupportedCapability
from rsi_lab.wiring import preflight

ROOT = Path(__file__).resolve().parents[2]


def cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "rsi_lab", *map(str, args)],
        cwd=ROOT,
        env=dict(os.environ, PYTHONPATH=str(ROOT / "src")),
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize(
    "study,total", [("m3_smoke", 20), ("m3_probe_admission", 100), ("m3_transition", 7)]
)
def test_m3_cli_run_resume_inspect(study, total, tmp_path):
    config = ROOT / "configs" / f"{study}.toml"
    valid = cli("validate", "--config", config)
    assert valid.returncode == 0, valid.stderr
    output = tmp_path / study
    created = cli("run", "--config", config, "--output", output)
    assert created.returncode == 0, created.stderr
    result = json.loads(created.stdout)
    if study == "m3_transition":
        assert result == json.loads(
            (ROOT / "tests/golden/transition_v1.expected.json").read_text()
        )
    else:
        assert result["accounting"]["total_synthetic_units"] == total
        assert result["accounting"]["outstanding_reserved_units"] == 0
        assert result["independent_evaluation"] is None
        assert len(result["rounds"]) == 2
        assert all(r["public_evaluation"]["risk"] == 0 for r in result["rounds"])
    for operation in ("resume", "inspect"):
        replay = cli(operation, "--run", output)
        assert replay.returncode == 0, replay.stderr
        assert json.loads(replay.stdout) == result
    assert cli("run", "--config", config, "--output", output).returncode == 4


def test_m3_preflight_and_checkpoint_flag_fail_before_creating_output(tmp_path):
    config = ROOT / "configs/m3_smoke.toml"
    output = tmp_path / "must_not_exist"
    rejected = cli("run", "--config", config, "--output", output, "--stop-after", 2)
    assert rejected.returncode == 2
    assert not output.exists()
    original = load_config(config)
    for changes in (
        {"partition": Partition.SEALED},
        {"candidate_origin": CandidateOrigin.NATURAL},
        {"arbitrary_code_execution": True},
        {"study": "not_registered"},
    ):
        with pytest.raises(UnsupportedCapability):
            preflight(replace(original, **changes))
    frozen = load_config(ROOT / "configs/m3_transition.toml")
    with pytest.raises(UnsupportedCapability, match="observe"):
        preflight(replace(original, admission_policy="probe", guard_mode="enforce"))
    for changes in (
        {"guard_mode": "enforce"},
        {"admission_policy": "native"},
        {"context_budget_units": 0},
        {"horizon_attempts": 3},
    ):
        with pytest.raises(UnsupportedCapability, match="exact frozen"):
            preflight(replace(frozen, **changes))
