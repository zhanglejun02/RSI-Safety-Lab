"""Composition preserves legacy tasks and makes pilot controls explicit."""
from dataclasses import replace
from pathlib import Path

import pytest

from rsi_lab.contracts import CursorFamily, Partition
from rsi_lab.engine.platform import execute_step
from rsi_lab.errors import ConflictError, ValidationError
from rsi_lab.infra.chat_models import ChatMessage
from rsi_lab.platform_config import ModelSettings, PluginSpec, load_platform_config
from rsi_lab.platform_wiring import build_providers, builtin_registry
from rsi_lab.serialization import digest
from rsi_lab.studies.platform import preflight, task_anchor
from rsi_lab.subjects.gepa import WORKFLOW_INITIAL_PROMPT, initial_gepa_snapshot
from rsi_lab.tasks.authorization_workflows import AuthorizationWorkflowTaskPackage


CONFIGS = Path(__file__).parents[2] / "configs"


@pytest.fixture
def config():
    pytest.importorskip("gepa")
    return load_platform_config(CONFIGS / "platform_workflow_control.toml")


def test_workflow_and_legacy_configs_preflight_without_model_calls(config, tmp_path):
    for name in ("platform_workflow_control.toml", "platform_workflow_http.toml", "platform_fixture.toml",
                 "platform_task_a.toml", "platform_gepa.toml", "platform_gepa_control.toml"):
        checked = preflight(load_platform_config(CONFIGS / name))
        assert checked["valid"]
    providers = build_providers(config, tmp_path / "providers")
    adapter = builtin_registry().build_system(config, tmp_path / "subject", providers)
    assert adapter.snapshot().version.component("instructions")["text"] == WORKFLOW_INITIAL_PROMPT
    assert adapter.require_strict_improvement is False
    assert adapter.min_public_utility == 1.0
    assert adapter.describe() == preflight(config)["system"]
    assert all(provider.summary()["dispatched_calls"] == 0 for provider in providers.values())


@pytest.mark.parametrize("strategy", ["safe_batch", "unsafe_cache"])
def test_scripted_selection_changes_actual_workflow_behavior(config, tmp_path, strategy):
    config = replace(config, system=replace(config.system, options={**config.system.options,
                                                                  "control_strategy": strategy}))
    registry = builtin_registry()
    _, package = registry.preflight(config)
    providers = build_providers(config, tmp_path / "providers")
    subject = registry.build_system(config, tmp_path / "subject", providers)
    response = providers["editor"].generate("control.reflection", (ChatMessage("user", "Optimize public work"),))
    prompt = response.content["text"].strip().removeprefix("```").removesuffix("```").strip()
    candidate = initial_gepa_snapshot(prompt)
    branch = subject.fork(candidate, tmp_path / "candidate")
    environment = package.create_environment(tmp_path / "environment")
    outcomes = []
    for step in package.plan(990, CursorFamily.INDEPENDENT_EVALUATION, Partition.DEV, 6, "delayed").rounds:
        execute_step(branch, environment, step, "configured.control/" + str(step.attempt_index))
        outcomes.append(environment.evaluate(step))
    assert [outcome.risk for outcome in outcomes] == ([0] * 6 if strategy == "safe_batch" else [0, 0, 1, 1, 0, 1])
    assert all(outcome.complete for outcome in outcomes)
    assert all(provider.origin.value == "control" for provider in providers.values())
    assert all(strategy in provider.provider_id for provider in providers.values())


def test_control_strategy_is_resume_bound(config, tmp_path):
    build_providers(config, tmp_path)
    other = replace(config, system=replace(config.system, options={**config.system.options,
                                                                 "control_strategy": "unsafe_cache"}))
    with pytest.raises(ConflictError):
        build_providers(other, tmp_path)


def test_workflow_options_are_validated_before_execution(config):
    for key, value in (("prompt_profile", "unknown"), ("control_strategy", "unknown"),
                       ("require_strict_improvement", 1), ("max_actor_steps", 65),
                       ("min_public_utility", -0.1), ("min_public_utility", 1.1),
                       ("min_public_utility", True), ("min_public_utility", "1")):
        with pytest.raises(ValidationError):
            invalid = replace(config, system=replace(config.system, options={**config.system.options, key: value}))
            preflight(invalid)
    with pytest.raises(ValidationError, match="scripted"):
        replace(config, actor=ModelSettings(provider="http", model="test-model"))
    with pytest.raises(ValidationError, match="selected together"):
        replace(config, task=PluginSpec("authorization"))
    with pytest.raises(ValidationError):
        preflight(replace(config, task=replace(config.task, options={"public_cases": 2})))


def test_anchor_binds_actual_public_suite_and_keeps_sealed_public_inputs_dev(config):
    package = AuthorizationWorkflowTaskPackage(public_cases=3)
    anchor = task_anchor(package, config)
    assert anchor["native"] == package.public_suite(config.protocol.seed, Partition.DEV).rounds
    assert len(anchor["native"]) == 3
    assert anchor["native"] != package.plan(config.protocol.seed, horizon=1, schedule="benign").rounds
    assert digest(task_anchor(AuthorizationWorkflowTaskPackage(public_cases=4), config)) != digest(anchor)
    sealed = replace(config, protocol=replace(config.protocol, partition=Partition.SEALED))
    assert task_anchor(package, sealed)["native"] == anchor["native"]
    assert task_anchor(package, sealed)["independent"] != anchor["independent"]
