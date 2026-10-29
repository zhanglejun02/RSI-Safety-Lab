"""Canonical JSON v1: UTF-8, sorted keys, ordered arrays, no NaN or duplicates."""

import dataclasses
import hashlib
import json
import math
from enum import Enum

from .errors import ValidationError


def primitive(value):
    if isinstance(value, Enum):
        return primitive(value.value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: primitive(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) in (list, tuple):
        return [primitive(v) for v in value]
    if type(value) is dict and all(type(k) is str for k in value):
        return {k: primitive(v) for k, v in value.items()}
    raise ValidationError(f"Not canonical JSON data: {type(value).__name__}")


def canonical_bytes(value) -> bytes:
    try:
        return json.dumps(primitive(value), sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise ValidationError(f"Invalid canonical JSON: {exc}") from exc


def digest(value) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValidationError(f"Non-finite JSON number: {value}")


def decode_json(data: bytes | str) -> dict:
    try:
        if type(data) is bytes:
            data = data.decode("utf-8")
        elif type(data) is not str:
            raise ValidationError("JSON input must be UTF-8 bytes or text")
        value = json.loads(data, object_pairs_hook=_unique_object,
                           parse_constant=_invalid_constant)
        if type(value) is not dict:
            raise ValidationError("Expected a JSON object")
        canonical_bytes(value)  # Also rejects overflow to infinity and invalid UTF-8.
        return value
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValidationError(f"Invalid JSON: {exc}") from exc

