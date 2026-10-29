"""Portable diagnostics tests use mocked hardware; they do not validate a GPU."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def diagnostic():
    path = Path(__file__).resolve().parents[2] / "scripts" / "gpu_preflight.py"
    spec = importlib.util.spec_from_file_location("gpu_preflight_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_torch_is_explicit_and_not_an_inference_claim(diagnostic, monkeypatch):
    def missing(name):
        raise ModuleNotFoundError("No module named 'torch'", name="torch")
    monkeypatch.setattr(diagnostic.importlib, "import_module", missing)
    monkeypatch.setattr(diagnostic, "nvidia_diagnostics", lambda: {"status": "not_installed", "devices": []})
    report = diagnostic.collect_report()
    assert report["torch"]["status"] == "not_installed"
    assert report["cuda_ready_for_runtime_setup"] is False
    assert all(value is False for value in report["validation"].values())


def test_cpu_torch_and_broken_import_are_distinguished(diagnostic, monkeypatch):
    cpu_torch = SimpleNamespace(__version__="test.cpu", version=SimpleNamespace(cuda=None),
                                cuda=SimpleNamespace(is_available=lambda: False))
    monkeypatch.setattr(diagnostic.importlib, "import_module", lambda name: cpu_torch)
    assert diagnostic.torch_diagnostics()["status"] == "cuda_unavailable"
    def broken(name):
        raise ModuleNotFoundError("Missing torch dependency", name="numpy")
    monkeypatch.setattr(diagnostic.importlib, "import_module", broken)
    assert diagnostic.torch_diagnostics()["status"] == "import_error"


def test_cuda_properties_are_reported_without_tensor_or_model_api(diagnostic, monkeypatch):
    fake_torch = SimpleNamespace(
        __version__="test.cuda", version=SimpleNamespace(cuda="test-version"),
        cuda=SimpleNamespace(is_available=lambda: True, device_count=lambda: 1,
                             get_device_properties=lambda index: SimpleNamespace(
                                 name="Mock CUDA Device", total_memory=123456789, major=9, minor=0)),
    )
    monkeypatch.setattr(diagnostic.importlib, "import_module", lambda name: fake_torch)
    result = diagnostic.torch_diagnostics()
    assert result["status"] == "available"
    assert result["device_count"] == 1
    assert result["devices"][0]["total_memory_bytes"] == 123456789
    assert result["devices"][0]["compute_capability"] == [9, 0]


def test_failed_device_query_does_not_report_cuda_ready(diagnostic, monkeypatch):
    def failed(index):
        raise RuntimeError("driver initialization failed")
    fake_torch = SimpleNamespace(
        __version__="test.cuda", version=SimpleNamespace(cuda="test-version"),
        cuda=SimpleNamespace(is_available=lambda: True, device_count=lambda: 1, get_device_properties=failed),
    )
    monkeypatch.setattr(diagnostic.importlib, "import_module", lambda name: fake_torch)
    result = diagnostic.torch_diagnostics()
    assert result["status"] == "query_error"
    assert result["cuda_available"] is False


def test_nvidia_smi_is_bounded_and_read_only(diagnostic, monkeypatch):
    monkeypatch.setattr(diagnostic.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    def run(command, **kwargs):
        assert command[0] == "/usr/bin/nvidia-smi"
        assert command[1].startswith("--query-gpu=")
        assert kwargs["timeout"] == 10
        assert "shell" not in kwargs
        return SimpleNamespace(returncode=0, stdout="0, Mock GPU, test-driver, 8192, 4096\n", stderr="")
    monkeypatch.setattr(diagnostic.subprocess, "run", run)
    result = diagnostic.nvidia_diagnostics()
    assert result["status"] == "available"
    assert result["devices"][0]["free_memory_mib"] == 4096


def test_require_cuda_retains_failure_report_and_refuses_overwrite(diagnostic, monkeypatch, tmp_path, capsys):
    report = {"cuda_ready_for_runtime_setup": False, "validation": {"inference_run": False}}
    monkeypatch.setattr(diagnostic, "collect_report", lambda: report)
    output = tmp_path / "diagnostic.json"
    assert diagnostic.main(["--require-cuda", "--output", str(output)]) == 2
    assert json.loads(output.read_text()) == report
    original = output.read_bytes()
    assert diagnostic.main(["--output", str(output)]) == 1
    assert output.read_bytes() == original
    assert "Output already exists" in capsys.readouterr().err


def test_optional_cuda_and_success_exit_codes(diagnostic, monkeypatch, capsys):
    monkeypatch.setattr(diagnostic, "collect_report", lambda: {"cuda_ready_for_runtime_setup": False})
    assert diagnostic.main([]) == 0
    monkeypatch.setattr(diagnostic, "collect_report", lambda: {"cuda_ready_for_runtime_setup": True})
    assert diagnostic.main(["--require-cuda"]) == 0
    capsys.readouterr()
