"""Composition config for platform plugins; no executable config or stage dispatch."""
from dataclasses import dataclass, field
from pathlib import Path
import tomllib
from rsi_lab.contracts import StrictRecord, Partition, require_count
from rsi_lab.errors import ValidationError
from rsi_lab.infra.chat_models import ChatConfig
from rsi_lab.serialization import canonical_bytes


@dataclass(frozen=True)
class PluginSpec(StrictRecord):
    name: str
    options: dict = field(default_factory=dict)
    schema_version: int = 1

    def validate(self):
        if not self.name or len(self.name) > 100:
            raise ValidationError("Invalid plugin name")
        canonical_bytes(self.options)


@dataclass(frozen=True)
class ModelSettings(StrictRecord):
    provider: str = "scripted"
    model: str = "control"
    base_url: str = "https://openrouter.ai/api/v1"
    api_key_env: str = "OPENROUTER_API_KEY"
    max_calls: int = 1000
    max_total_tokens: int = 1000000
    max_output_tokens: int = 1024
    temperature: float = 0.0
    thinking_mode: str | None = None
    timeout_seconds: float = 60.0
    schema_version: int = 1

    def validate(self):
        if self.provider not in ("scripted", "http"):
            raise ValidationError("Unknown model provider")
        ChatConfig(self.base_url, self.model, self.api_key_env,
                   max_calls=self.max_calls, max_total_tokens=self.max_total_tokens,
                   max_output_tokens=self.max_output_tokens, temperature=self.temperature,
                   thinking_mode=self.thinking_mode, timeout_seconds=self.timeout_seconds)


@dataclass(frozen=True)
class ProtocolConfig(StrictRecord):
    mode: str = "admission"
    partition: Partition = Partition.DEV
    seed: int = 100
    horizon: int = 6
    repetitions: int = 1
    schedule: str = "delayed"
    probe_round_budget: int = 4
    schema_version: int = 1

    def validate(self):
        if self.mode not in ("admission", "paired", "behavior"):
            raise ValidationError("Unknown protocol mode")
        for name in ("horizon", "repetitions", "probe_round_budget"):
            require_count(getattr(self, name), name, 1)
        require_count(self.seed, "seed")
        if self.horizon > 100 or self.repetitions > 10 or self.probe_round_budget > 4:
            raise ValidationError("Protocol exceeds bounded pilot limits")
        if self.schedule not in ("delayed", "immediate", "mixed", "benign"):
            raise ValidationError("Unknown task schedule")


@dataclass(frozen=True)
class DetectorConfig(StrictRecord):
    name: str = "diff-probe"
    policy: str = "conservative"
    model_path: str | None = None
    schema_version: int = 1

    def validate(self):
        if self.name not in ("diff-probe", "random-probe", "frozen-model", "none"):
            raise ValidationError("Unknown detector")
        if self.policy not in ("conservative", "native-only", "reject-all"):
            raise ValidationError("Unknown admission policy")
        if (self.name == "frozen-model") != (self.model_path is not None):
            raise ValidationError("Frozen detector requires exactly one model_path")


@dataclass(frozen=True)
class PlatformConfig(StrictRecord):
    system: PluginSpec = field(default_factory=lambda: PluginSpec("fixture"))
    task: PluginSpec = field(default_factory=lambda: PluginSpec("authorization"))
    protocol: ProtocolConfig = field(default_factory=ProtocolConfig)
    detector: DetectorConfig = field(default_factory=DetectorConfig)
    backend: PluginSpec = field(default_factory=lambda: PluginSpec("bounded-tools"))
    editor: ModelSettings = field(default_factory=ModelSettings)
    actor: ModelSettings = field(default_factory=ModelSettings)
    schema_version: int = 1

    def validate(self):
        if self.backend.name != "bounded-tools" or self.backend.options:
            raise ValidationError("Only bounded declarative tools backend is installed; arbitrary code is unsupported")
        if self.system.name == "gepa":
            workflow = self.system.options.get("prompt_profile") == "workflow"
            if (self.task.name in ("authorization", "authorization-workflows")
                    and workflow != (self.task.name == "authorization-workflows")):
                raise ValidationError("The workflow task and GEPA workflow prompt profile must be selected together")
            if ("control_strategy" in self.system.options
                    and (self.actor.provider != "scripted" or self.editor.provider != "scripted")):
                raise ValidationError("Explicit control_strategy requires scripted actor and editor providers")


def load_platform_config(path):
    with Path(path).open("rb") as handle:
        raw = tomllib.load(handle)
    allowed = {"system", "task", "protocol", "detector", "backend", "editor", "actor"}
    if set(raw) - allowed:
        raise ValidationError("Unknown platform configuration section")
    records = {"system": PluginSpec, "task": PluginSpec, "protocol": ProtocolConfig,
               "detector": DetectorConfig, "backend": PluginSpec, "editor": ModelSettings, "actor": ModelSettings}
    return PlatformConfig(**{key: records[key].from_dict({"schema_version": 1, **value}) for key, value in raw.items()})
