import pytest

from rsi_lab.errors import ValidationError
from rsi_lab.studies import stress_pilot as pilot
from rsi_lab.tasks import stress_bench as bench
from rsi_lab.tasks.bounded_language import LANGUAGE_GUIDE, editor_contract
from rsi_lab.tasks.cost_program import execute_program


@pytest.mark.parametrize("system", pilot.SYSTEMS)
def test_contract_is_versioned_without_changing_pilot_defaults(system):
    module = bench.backend("privacy")
    package = pilot.OrdinaryPublicTasks("privacy")
    old = pilot._initial(system, 715, module, package)
    new = pilot._initial(system, 715, module, package, editor_contract("privacy"))
    old_text = old.version.component("task_contract")["text"]
    new_text = new.version.component("task_contract")["text"]
    assert old_text == module.CONTRACT and new_text == old_text.rstrip() + "\n\n" + LANGUAGE_GUIDE
    assert old.version.component("policy") == new.version.component("policy")
    assert old.state_json == new.state_json


@pytest.mark.parametrize(("body", "notice"), [
    ("out = [x for x in request]", "No comprehensions"),
    ("out = set()", "set()"),
    ("del cache['old']", "del"),
    ("cache[('a', 'b')] = 1", "tuple keys fail at runtime"),
    ("cache['nested'] = cache", "Data must be acyclic"),
])
def test_disclosed_restrictions_match_real_failures(body, notice):
    assert notice in LANGUAGE_GUIDE
    with pytest.raises(ValidationError):
        execute_program("def handle(request, cache, api):\n    " + body, {}, {}, lambda *_: None)


def test_explicit_loop_string_keys_and_cache_replacement_run():
    program = """def handle(request, cache, api):
    seen = []
    for value in request["rows"]:
        seen.append(value)
    updated = {}
    for key in cache:
        if key != "old":
            updated[key] = cache[key]
    updated["rows"] = seen
    cache = updated
    return cache
"""
    cache = execute_program(program, {"rows": [1, 2]}, {"keep": 1, "old": 7}, lambda *_: None)
    assert cache == {"keep": 1, "rows": [1, 2]}
