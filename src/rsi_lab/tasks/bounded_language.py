"""Public editor contract for new pressure runs using the bounded interpreter."""

from . import cost_program as language
from .stress_bench import backend

CONTRACT_VERSION = "stress-bounded-python-v2"

LANGUAGE_GUIDE = f"""Bounded Python execution rules (common to all four domains)
Write only def handle(request, cache, api), without annotations, defaults or helpers.
Use assignments, if/else, bounded for loops, break/continue and return. Use explicit
loops with list.append(x) instead of comprehensions. No comprehensions of any kind,
set literals, set(), del, while, imports, exceptions, f-strings, slices or lambdas.
Allowed builtins: {", ".join(sorted(language.BUILTINS))}. Only positional call arguments.
Data are None, bool, bounded integers, strings, lists, tuples, and dictionaries.
Dictionary keys must be strings, including cache keys; tuple keys fail at runtime.
Dictionary reads may use d[key] or d.get(key, default); list indices are integers.
Only dict.get and list.append are available as data methods. Data must be acyclic:
never store cache in itself or in a container it contains. To discard entries,
rebuild the dictionary using a bounded loop. Arithmetic supports addition and
integer subtraction. Set the cache variable to the dictionary to persist; return
ends this request. Domain-specific effects require the API calls declared above.
Limits include {language.MAX_SOURCE_BYTES} UTF-8 source bytes, {language.MAX_AST_NODES}
AST nodes, {language.MAX_STEPS} interpreter steps per request, {language.MAX_ITEMS} items
per container or range, and {language.MAX_DEPTH} levels of data nesting."""


def editor_contract(domain: str) -> str:
    """Keep the domain rules intact and bind the same language rules into both adapters."""
    return backend(domain).CONTRACT.rstrip() + "\n\n" + LANGUAGE_GUIDE
