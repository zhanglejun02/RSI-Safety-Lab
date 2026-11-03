"""Explicit constructor registry; third-party Python plugins are trusted code."""
from dataclasses import dataclass
from typing import Callable

from rsi_lab.contracts import require_id
from rsi_lab.errors import ConflictError, IntegrityError, UnsupportedCapability, ValidationError
from .contracts import SystemDescriptor


@dataclass(frozen=True)
class ScriptedModel:
    """Declared engineering control; identity must bind role and control options."""
    provider_id: str
    respond: Callable

    def __post_init__(self):
        require_id(self.provider_id)
        if not callable(self.respond):
            raise ValidationError("Scripted model responder must be callable")


class PluginRegistry:
    def __init__(self):
        self.systems = {}
        self.tasks = {}
        self._scripted_models = {}

    def register_system(self, name, factory, preflight, *, scripted_model=None):
        require_id(name)
        if not callable(factory) or not callable(preflight) or (scripted_model is not None and not callable(scripted_model)):
            raise ValidationError("System registration requires callable factories")
        if name in self.systems:
            raise ConflictError("System plugin already registered: " + name)
        self.systems[name] = (factory, preflight)
        if scripted_model is not None:
            self._scripted_models[name] = scripted_model

    def register_task(self, name, factory):
        require_id(name)
        if not callable(factory):
            raise ValidationError("Task registration requires a callable factory")
        if name in self.tasks:
            raise ConflictError("Task plugin already registered: " + name)
        self.tasks[name] = factory

    def preflight(self, config):
        if config.system.name not in self.systems or config.task.name not in self.tasks:
            raise UnsupportedCapability("Requested system or task plugin is not registered")
        descriptor = self.systems[config.system.name][1](config.system.options)
        if type(descriptor) is not SystemDescriptor:
            raise ValidationError("System preflight must return SystemDescriptor")
        task = self.tasks[config.task.name](config.task.options)
        descriptor.capabilities.require(config.protocol.mode, task.tool_protocol)
        return descriptor, task

    def build_system(self, config, directory, providers):
        if config.system.name not in self.systems:
            raise UnsupportedCapability("System plugin is not registered: " + config.system.name)
        factory, describe = self.systems[config.system.name]
        expected = describe(config.system.options)
        adapter = factory(directory, providers, config.system.options)
        if adapter.describe() != expected:
            raise IntegrityError("Constructed system descriptor differs from its preflight declaration")
        return adapter

    def scripted_model(self, name, role, options):
        if name not in self._scripted_models:
            raise UnsupportedCapability(
                f"System {name} has no registered scripted model; register a control or supply a provider_factory")
        result = self._scripted_models[name](role, options)
        if type(result) is not ScriptedModel:
            raise ValidationError("Scripted model factory must return ScriptedModel")
        return result
