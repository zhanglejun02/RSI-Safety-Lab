"""Optimizations must preserve budget boundaries, replay and execution isolation."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.errors import BudgetExceeded, ConflictError, IntegrityError, ValidationError
from rsi_lab.infra import chat_models
from rsi_lab.platform_config import ModelSettings, PlatformConfig, ProtocolConfig
from rsi_lab.serialization import canonical_bytes, digest
from rsi_lab.tasks import cost_program, retail_workflow as retail
from rsi_lab.studies import platform, platform_cli, validity_pilot


def retail_environment(directory):
    package = retail.RetailTaskPackage()
    env = package.create_environment(directory)
    step = package.public_suite(15001).rounds[0]
    env.prepare(step)
    task = env.public_tasks(step)[0]
    env.tools(task.task_id, "checks").call("execute_program", {"source": retail.SAFE_SOURCE}, "run/1")
    return package, env, step


def test_verified_retail_log_reused_locally_but_replayed_on_restore(tmp_path, monkeypatch):
    original, calls = retail.verify_result, []

    def verify(result, suite):
        calls.append(result)
        return original(result, suite)

    monkeypatch.setattr(retail, "verify_result", verify)
    package, env, step = retail_environment(tmp_path / "first")
    snapshot = env.snapshot()
    for _ in range(3):
        env.evaluate(step)
        env.synthetic_cost(step)
        env.public_trajectories(step)
        assert env.snapshot() == snapshot
    assert len(calls) == 1
    # Both recovery from disk and a new fork must earn their own validation.
    assert package.create_environment(tmp_path / "first").snapshot() == snapshot
    assert package.create_environment(tmp_path / "fork", snapshot).snapshot() == snapshot
    assert len(calls) == 3


@pytest.mark.parametrize("change", ["checksum", "effect", "source", "guard", "path", "symlink"])
def test_warm_retail_verification_still_detects_changed_files(tmp_path, change):
    _, env, _ = retail_environment(tmp_path)
    env.snapshot()  # Populate the successful-validation cache first.
    path = next((tmp_path / "operations").glob("*.json"))
    row = read_record(path)
    path.unlink()
    if change == "checksum":
        path.write_bytes(canonical_bytes({"record": row, "sha256": "0" * 64}))
    elif change == "symlink":
        target = tmp_path / "elsewhere.json"
        save_record(target, row)
        path.symlink_to(target)
    else:
        if change == "effect":
            # A recomputed envelope checksum must not conceal forged effects.
            row["result"]["streams"][0]["rows"][0]["effects"][0]["authorized"] = False
        elif change == "source":
            row["source_sha256"] = "0" * 64
        elif change == "guard":
            row["result"]["guard"] = "enforce"
        else:
            path = path.with_name("wrong-slot.json")
        save_record(path, row)
    with pytest.raises(IntegrityError):
        env.snapshot()


def test_retail_snapshot_inputs_detached_and_validation_cache_bounded(tmp_path):
    package, env, step = retail_environment(tmp_path / "first")
    snapshot = env.snapshot()
    fork = package.create_environment(tmp_path / "fork", snapshot)
    expected = deepcopy(snapshot)
    snapshot["records"][0]["result"]["streams"][0]["cost"] = -1
    assert fork.snapshot() == expected
    original = expected["records"][0]
    for index in range(257):
        fork._verify({**original, "operation_id": "validation/" + str(index)})
    assert len(fork._verified) <= 256
    # Operation identity conflicts are still rejected before running new source.
    task = env.public_tasks(step)[0]
    with pytest.raises(ConflictError):
        env.tools(task.task_id, "checks").call("execute_program", {"source": retail.FOUNDER_SOURCE}, "run/1")


def test_cached_syntax_keeps_inputs_effects_and_instruction_budgets_independent(monkeypatch):
    cost_program._validated_ast.cache_clear()
    original, parses = cost_program.ast.parse, []

    def parse(source, *args, **kwargs):
        parses.append(source)
        return original(source, *args, **kwargs)

    monkeypatch.setattr(cost_program.ast, "parse", parse)
    source = '''def handle(request, cache, api):
    cache["count"] = cache.get("count", 0) + 1
    api.execute(request)
    return cache
'''
    cache, effects = {}, []
    cost_program.validate_program(source)
    for number in range(4):
        request = {"id": number}
        assert cost_program.execute_program(source, request, cache,
            lambda name, arguments: effects.append(arguments)) == {"count": 1}
        assert request == {"id": number} and cache == {}
    assert effects == [[{"id": n}] for n in range(4)] and parses == [source]
    # Parse reuse does not reuse an outcome or a partly spent runtime budget.
    costly = 'def handle(request, cache, api):\n    for a in range(256):\n        for b in range(256):\n            pass\n'
    for _ in range(2):
        with pytest.raises(ValidationError, match="instruction budget"):
            cost_program.execute_program(costly, {}, {}, lambda *args: None)
    assert parses.count(costly) == 1


def test_syntax_cache_has_bounded_entries_and_no_validation_bypass():
    cost_program._validated_ast.cache_clear()
    for index in range(65):
        cost_program.validate_program(f"def handle(request, cache, api):\n    return cache\n# {index}")
    info = cost_program._validated_ast.cache_info()
    assert info.currsize == info.maxsize == 64
    for invalid in ([], None, "x" * (cost_program.MAX_SOURCE_BYTES + 1), "\ud800",
                    "import os\ndef handle(request, cache, api):\n    return cache\n"):
        with pytest.raises(ValidationError):
            cost_program.validate_program(invalid)
    assert cost_program._validated_ast.cache_info().currsize == 64


def model_response():
    return canonical_bytes({"model": "local-fixture", "choices": [{"finish_reason": "stop",
        "message": {"content": '{"value": 1}'}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30, "cost": 0.001}})


def test_budget_reservation_skips_reporting_and_observes_other_connections(tmp_path, monkeypatch):
    calls = []
    config = chat_models.ChatConfig("http://localhost/fixture", "local-fixture", max_calls=2)
    path = tmp_path / "model.sqlite"
    first = chat_models.DurableChatProvider(path, config, transport=lambda *args: calls.append(1) or model_response())
    second = chat_models.DurableChatProvider(path, config, transport=lambda *args: calls.append(2) or model_response())
    messages = (chat_models.ChatMessage("user", "fixture"),)
    first.generate("r1", messages)
    def forbidden(*args):
        pytest.fail("Reservation must not build the historical cost report")
    monkeypatch.setattr(second, "_summary", forbidden)
    second.generate("r2", messages)
    with pytest.raises(BudgetExceeded, match="call limit"):
        first.generate("r3", messages)
    assert calls == [1, 2]
    report = first.summary()
    assert report["actual_total_tokens"] == 60 and report["reported_usage_cost"] == 0.002
    assert report["reported_cost_calls"] == 2


@pytest.mark.parametrize("entries", [[], [
    ("complete", 100, 30, 0), ("pending", 200, None, 0), ("unknown", 300, None, 0),
    ("complete", 400, None, 0), ("invalid", 500, 40, 0), ("invalid", 600, None, 0),
    ("complete", 50, 70, 1),
]])
def test_budget_aggregate_matches_existing_report_for_all_terminal_states(tmp_path, entries):
    provider = chat_models.DurableChatProvider(tmp_path / "model.sqlite",
        chat_models.ChatConfig("http://localhost/fixture", "local-fixture"))
    with provider._db() as db:
        for index, (status, reserved, actual, overrun) in enumerate(entries):
            db.execute("""INSERT INTO chat_requests(request_id,request_sha,request_json,status,
                reserved_tokens,actual_tokens,budget_overrun) VALUES(?,?,?,?,?,?,?)""",
                (str(index), digest(str(index)), b"{}", status, reserved, actual, overrun))
        report = provider._summary(db)
        assert provider._budget_usage(db) == {key: report[key] for key in
            ("dispatched_calls", "actual_total_tokens", "reserved_tokens", "budget_overrun")}


def test_budget_preview_counts_roots_repetitions_and_methods_without_gateways(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Preview must not construct a model gateway")
    monkeypatch.setattr(chat_models.DurableChatProvider, "__init__", forbidden)
    monkeypatch.setattr(chat_models.ScriptedChatProvider, "__init__", forbidden)
    settings = PlatformConfig(protocol=ProtocolConfig(mode="paired", horizon=3, repetitions=2),
        editor=ModelSettings(provider="http", max_calls=10, max_total_tokens=2000),
        actor=ModelSettings(provider="scripted", max_calls=100, max_total_tokens=4000))
    single = platform.preflight(settings)["model_allowance"]
    assert single["provider_sets"] == 1 and single["remote_call_limit_sum"] == 10
    preview = validity_pilot.preflight_pilot(settings, roots=3, audit_baselines=True)
    assert preview["allocations_per_root"] == {"root_proposal": 1, "continuations": 8, "audit_probe_methods": 2}
    allowance = preview["model_allowance"]
    assert allowance["provider_sets"] == 33
    assert allowance["remote_call_limit_sum"] == 330 and allowance["configured_token_limit_sum"] == 66000
    assert allowance["roles"]["actor"]["remote_call_limit_sum"] == 0
    assert allowance["monetary_cost"] is None and allowance["is_expected_usage"] is False
    with pytest.raises(ValidationError, match="paired mode"):
        validity_pilot.preflight_pilot(replace(settings, protocol=replace(settings.protocol, mode="admission")))


def test_cli_pilot_preview_needs_no_key_or_output_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(validity_pilot, "create_run", lambda *args, **kwargs: pytest.fail("Preview must not run"))
    config = tmp_path / "preview.toml"
    config.write_text('[protocol]\nhorizon=3\n[editor]\nprovider="http"\nmax_calls=5\n[actor]\nprovider="http"\nmax_calls=7\n')
    assert platform_cli.main(["pilot-validate", "--config", str(config), "--roots", "2", "--audit-baselines"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["mode"] == "paired"
    assert preview["model_allowance"]["provider_sets"] == 14
    assert preview["model_allowance"]["remote_call_limit_sum"] == 168
    assert list(tmp_path.iterdir()) == [config]
    assert platform_cli.main(["pilot-validate", "--config", str(config), "--roots", "31"]) == 2
