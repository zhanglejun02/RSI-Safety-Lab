"""Immutable component edits, independent of fixtures and subject algorithms."""
from dataclasses import replace

from rsi_lab.errors import ValidationError
from rsi_lab.serialization import json_text
from .contracts import SystemVersion


def replace_component(version: SystemVersion, name: str, value) -> SystemVersion:
    """Replace an existing component, preserving order, roles and revision.

    A misspelled component must fail instead of silently producing a no-op.
    Adding/removing components remains an explicit SystemVersion construction.
    """
    if name not in {component.name for component in version.components}:
        raise ValidationError(f"Missing component: {name}")
    content = json_text(value)
    return replace(version, components=tuple(
        replace(component, content_json=content) if component.name == name else component
        for component in version.components))
