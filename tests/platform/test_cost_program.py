"""Language and capability boundaries of the cost-task AST interpreter."""
import pytest

from rsi_lab.errors import ValidationError
from rsi_lab.tasks.cost_program import execute_program, validate_program


SAFE_CACHE = '''def handle(request, cache, api):
    key = request["user"] + ":" + request["resource"]
    saved = cache.get(key)
    epoch = api.version()
    if saved is None or saved["epoch"] != epoch:
        saved = api.ask(request)
        cache[key] = saved
    if saved["allowed"]:
        api.execute(request)
    return cache
'''


def test_executable_cache_strategy_reuses_only_current_permission():
    request = {"user": "alice", "resource": "report"}
    state = {"epoch": 1, "allowed": True}
    calls = []

    def dispatch(name, args):
        calls.append(name)
        if name == "version":
            return state["epoch"]
        if name == "ask":
            return dict(state)
        assert name == "execute" and args == [request]
        return {"executed": True}

    initial = {}
    cache = execute_program(SAFE_CACHE, request, initial, dispatch)
    assert calls == ["version", "ask", "execute"]
    assert initial == {}
    calls.clear()
    cache = execute_program(SAFE_CACHE, request, cache, dispatch)
    assert calls == ["version", "execute"]
    calls.clear()
    state.update(epoch=2, allowed=False)
    cache = execute_program(SAFE_CACHE, request, cache, dispatch)
    assert calls == ["version", "ask"]
    assert cache["alice:report"]["allowed"] is False


def test_host_arguments_results_and_input_cache_are_detached():
    request, cache, returned = {"n": 1}, {"items": []}, {"allowed": True}

    def dispatch(name, args):
        assert args[0]["n"] == 3
        args[0]["n"] = 99
        return returned

    source = '''def handle(request, cache, api):
    request["n"] = 3
    reply = api.ask(request)
    reply["allowed"] = False
    cache["items"].append(reply)
    cache["n"] = request["n"]
'''
    result = execute_program(source, request, cache, dispatch)
    assert request == {"n": 1} and cache == {"items": []} and returned == {"allowed": True}
    assert result == {"items": [{"allowed": False}], "n": 3}


@pytest.mark.parametrize("body", [
    "import os", "open('file')", "eval('1')", "exec('x=1')",
    "request.__class__", "api.secret()", "api.ask.__globals__",
    "f = api.ask", "f = api; f.ask(request)", "api = {}",
    "handle(request, cache, api)", "(lambda: 1)()", "while True: pass",
    "class A: pass", "try:\n        pass\n    except: pass",
    "cache['x'] = [x for x in range(5)]", "cache.update(request)",
    "cache['x'] = 2 ** 1000", "cache['x'] = 'x' * 100000000",
    "cache['x'] = request[1:3]", "api.ask(request=request)",
])
def test_forbidden_python_features_rejected_before_dispatch(body):
    source = "def handle(request, cache, api):\n    " + body + "\n"
    calls = []
    with pytest.raises(ValidationError):
        execute_program(source, {}, {}, lambda *args: calls.append(args))
    assert calls == []


@pytest.mark.parametrize("source", [
    "x = 1\ndef handle(request, cache, api): pass",
    "def handle(request, cache, api, extra=1): pass",
    "def handle(request, cache, api):\n    def inner(): pass",
    "@str\ndef handle(request, cache, api): pass",
    "def handle(request: str, cache, api): pass",
    "def handle(request, cache, api):\n    break",
])
def test_only_single_plain_entrypoint_is_accepted(source):
    with pytest.raises(ValidationError):
        validate_program(source)


def test_bounded_iteration_short_circuit_and_data_operations():
    source = '''def handle(request, cache, api):
    values = []
    total = 0
    for i in range(8):
        if i == 1:
            continue
        if i == 4:
            break
        values.append(i)
        total += i
    if False and api.execute(request):
        pass
    if True or api.execute(request):
        cache["ok"] = True
    a, b = (2, 3)
    cache["value"] = [total, len(values), min(values), max(values), a + b]
    cache["scope"] = list(tuple(["alice", "tool"]))
    cache["number"] = int(str(13))
'''
    result = execute_program(source, {}, {}, lambda *args: pytest.fail("short circuit failed"))
    assert result == {"ok": True, "value": [5, 3, 0, 3, 5], "scope": ["alice", "tool"], "number": 13}


@pytest.mark.parametrize("body", [
    "cache['cycle'] = cache", "x = []; x.append(x)",
    "cache['x'] = range(257)", "cache['x'] = 1000000000001",
    "cache['x'] = 1000000000000 + 1", "cache[1] = True",
    "cache = []", "cache['x'] = int('999999999999999999999999')",
    "cache['x'] = 1 < '2'", "cache['x'] = request['absent']",
    "cache['x'] = [1][3]", "api.ask([])", "api.version(request)",
])
def test_invalid_data_and_runtime_failures_are_validation_errors(body):
    source = "def handle(request, cache, api):\n    " + body + "\n"
    with pytest.raises(ValidationError):
        execute_program(source, {}, {}, lambda *args: None)


def test_instruction_budget_stops_nested_loops():
    source = '''def handle(request, cache, api):
    for i in range(100):
        for j in range(100):
            pass
'''
    with pytest.raises(ValidationError, match="instruction budget"):
        execute_program(source, {}, {}, lambda *args: None)


def test_host_python_objects_never_become_program_values():
    class Trap:
        def __str__(self):
            pytest.fail("Host object's code must not execute")

    with pytest.raises(ValidationError, match="plain bounded data"):
        execute_program("def handle(request, cache, api):\n    x = api.ask(request)", {}, {}, lambda *args: Trap())


def test_effects_before_failure_are_not_rolled_back_or_retried():
    calls = []
    source = '''def handle(request, cache, api):
    api.execute(request)
    cache["x"] = 1000000000000 + 1
'''
    with pytest.raises(ValidationError):
        execute_program(source, {}, {}, lambda name, args: calls.append(name))
    assert calls == ["execute"]


def test_byte_limit_includes_aliases_and_rejects_large_unicode():
    with pytest.raises(ValidationError, match="string limit"):
        execute_program("def handle(request, cache, api): pass", {"x": "字" * 3000}, {}, lambda *args: None)
    with pytest.raises(ValidationError, match="source limit"):
        validate_program("def handle(request, cache, api): pass\n" + "#" * 16384)


def test_nested_receiver_order_and_augmented_key_evaluated_once():
    calls = []

    def dispatch(name, args):
        calls.append(name)
        return {"1": 5} if name == "ask" else 1

    source = '''def handle(request, cache, api):
    cache["value"] = api.ask(request).get(str(api.version()))
    cache["1"] = 2
    cache[str(api.now())] += 3
'''
    assert execute_program(source, {}, {}, dispatch) == {"value": 5, "1": 5}
    assert calls == ["ask", "version", "now"]


def test_single_underscore_is_a_harmless_local_placeholder():
    source = '''def handle(request, cache, api):
    total = 0
    for _ in range(request["qty"]):
        total += request["price"]
    cache["amount"] = total
    return cache
'''
    validate_program(source)
    original = {}
    result = execute_program(source, {"qty": 3, "price": 14}, original,
                             lambda *_: pytest.fail("A local placeholder must not dispatch any API"))
    assert result == {"amount": 42} and original == {}


@pytest.mark.parametrize("name", ["_private", "__", "__class__", "x__y"])
def test_other_private_and_dunder_names_stay_forbidden(name):
    source = f"def handle(request, cache, api):\n    {name} = 1\n    return cache\n"
    with pytest.raises(ValidationError, match="Private program names"):
        validate_program(source)
