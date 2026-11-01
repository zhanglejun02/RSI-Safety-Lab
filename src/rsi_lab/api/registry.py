"""Explicit constructor registry; third-party Python plugins are trusted code."""
from rsi_lab.errors import ConflictError, UnsupportedCapability


class PluginRegistry:
    def __init__(self):
        self.systems = {}
        self.tasks = {}

    def register_system(self, name, factory, preflight):
        if name in self.systems:
            raise ConflictError("System plugin already registered: " + name)
        self.systems[name] = (factory, preflight)

    def register_task(self, name, factory):
        if name in self.tasks:
            raise ConflictError("Task plugin already registered: " + name)
        self.tasks[name] = factory

    def preflight(self, config):
        if config.system.name not in self.systems or config.task.name not in self.tasks:
            raise UnsupportedCapability("Requested system or task plugin is not registered")
        descriptor = self.systems[config.system.name][1](config.system.options)
        task = self.tasks[config.task.name](config.task.options)
        descriptor.capabilities.require(config.protocol.mode, task.tool_protocol)
        return descriptor, task

    def build_system(self, config, directory, providers):
        return self.systems[config.system.name][0](directory, providers, config.system.options)
