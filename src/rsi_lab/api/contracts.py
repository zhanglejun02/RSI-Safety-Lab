"""Platform v1 wire contracts; independent of any particular RSI or task.

These records are NOT the frozen P0 CandidateBinding. Identical JSON is not an
activation capability. Only the controller issues activation commands.
"""
from dataclasses import dataclass
from rsi_lab.contracts import StrictRecord, CandidateOrigin, Partition, require_id, require_count
from rsi_lab.errors import ValidationError, UnsupportedCapability
from rsi_lab.learning.contracts import require_sha
from rsi_lab.serialization import decode_json, digest

API_VERSION = "rsi-platform-v1"


@dataclass(frozen=True)
class Component(StrictRecord):
    name: str
    content_json: str
    semantic_role: str | None = None
    schema_version: int = 1

    def validate(self):
        require_id(self.name)
        decode_json(self.content_json)
        if len(self.content_json.encode()) > 262144:
            raise ValidationError("Component exceeds 256 KiB")
        if self.semantic_role not in (None, "agent", "q", "v", "s", "memory", "optimizer"):
            raise ValidationError("Unknown optional semantic role")


@dataclass(frozen=True)
class SystemVersion(StrictRecord):
    system_id: str
    components: tuple[Component, ...]
    upstream_revision: str
    schema_version: int = 1

    def validate(self):
        require_id(self.system_id)
        if not self.components or len({c.name for c in self.components}) != len(self.components):
            raise ValidationError("Version requires unique named components")
        if not self.upstream_revision:
            raise ValidationError("Version must identify its implementation")

    @property
    def sha256(self):
        return digest(self)

    def component(self, name):
        for c in self.components:
            if c.name == name:
                return decode_json(c.content_json)
        raise ValidationError(f"Missing component: {name}")


@dataclass(frozen=True)
class SystemSnapshot(StrictRecord):
    version: SystemVersion
    state_json: str = "{}"
    schema_version: int = 1

    def validate(self):
        decode_json(self.state_json)

    @property
    def sha256(self):
        return digest(self)


@dataclass(frozen=True)
class Capabilities(StrictRecord):
    execute: bool = True
    snapshot_restore: bool = False
    propose: bool = False
    pre_activation: bool = False
    reconcile_activation: bool = False
    tool_protocols: tuple[str, ...] = ()
    mutable_components: tuple[str, ...] = ()
    trusted_python: bool = True
    schema_version: int = 1

    def require(self, mode, tool_protocol):
        required = {"behavior": ("execute",),
                    "paired": ("execute", "snapshot_restore", "propose", "pre_activation", "reconcile_activation"),
                    "admission": ("execute", "snapshot_restore", "propose", "pre_activation", "reconcile_activation")}
        if mode not in required:
            raise ValidationError("Unknown experiment mode")
        missing = [name for name in required[mode] if not getattr(self, name)]
        if tool_protocol not in self.tool_protocols:
            missing.append(f"tool protocol {tool_protocol}")
        if missing:
            raise UnsupportedCapability("System cannot run requested protocol: " + ", ".join(missing))


@dataclass(frozen=True)
class SystemDescriptor(StrictRecord):
    system_id: str
    display_name: str
    capabilities: Capabilities
    upstream_url: str
    upstream_revision: str
    api_version: str = API_VERSION
    schema_version: int = 1

    def validate(self):
        require_id(self.system_id)
        if self.api_version != API_VERSION:
            raise UnsupportedCapability("System adapter API version mismatch")


@dataclass(frozen=True)
class PublicTask(StrictRecord):
    task_id: str
    request_json: str
    tool_protocol: str
    schema_version: int = 1

    def validate(self):
        require_id(self.task_id)
        decode_json(self.request_json)


@dataclass(frozen=True)
class ExecutionReceipt(StrictRecord):
    task_id: str
    version_sha256: str
    complete: bool
    details_json: str = "{}"
    schema_version: int = 1

    def validate(self):
        require_id(self.task_id)
        require_sha(self.version_sha256)
        decode_json(self.details_json)


@dataclass(frozen=True)
class CandidateDraft(StrictRecord):
    base_sha256: str
    candidate: SystemSnapshot
    native_valid: bool
    native_evidence_json: str
    origin: CandidateOrigin
    schema_version: int = 1

    def validate(self):
        require_sha(self.base_sha256)
        decode_json(self.native_evidence_json)


@dataclass(frozen=True)
class UpdateIdentity(StrictRecord):
    update_id: str
    root_id: str
    history_group_id: str
    protocol_sha256: str
    base_sha256: str
    candidate_sha256: str
    environment_sha256: str
    partition: Partition
    origin: CandidateOrigin
    horizon: int
    task_plan_sha256: str
    schema_version: int = 1

    def validate(self):
        for x in (self.update_id, self.root_id, self.history_group_id):
            require_id(x)
        for x in (self.protocol_sha256, self.base_sha256, self.candidate_sha256, self.environment_sha256, self.task_plan_sha256):
            require_sha(x)
        require_count(self.horizon, "horizon", 1)


@dataclass(frozen=True)
class PreparedUpdate(StrictRecord):
    identity: UpdateIdentity
    base: SystemSnapshot
    draft: CandidateDraft
    schema_version: int = 1

    def validate(self):
        if (self.base.sha256 != self.identity.base_sha256 or
                self.draft.base_sha256 != self.base.sha256 or
                self.draft.candidate.sha256 != self.identity.candidate_sha256 or
                self.draft.origin != self.identity.origin or
                self.base.version.system_id != self.draft.candidate.version.system_id):
            raise ValidationError("Prepared update identity/content mismatch")


@dataclass(frozen=True)
class ActivationCommand(StrictRecord):
    operation_id: str
    identity: UpdateIdentity
    decision_sha256: str
    candidate: SystemSnapshot
    schema_version: int = 1

    def validate(self):
        require_id(self.operation_id)
        require_sha(self.decision_sha256)
        if self.candidate.sha256 != self.identity.candidate_sha256:
            raise ValidationError("Activation target differs from audited candidate")


@dataclass(frozen=True)
class ActivationReceipt(StrictRecord):
    operation_id: str
    command_sha256: str
    before_sha256: str
    after_sha256: str
    schema_version: int = 1

    def validate(self):
        require_id(self.operation_id)
        for x in (self.command_sha256, self.before_sha256, self.after_sha256):
            require_sha(x)


@dataclass(frozen=True)
class CostUsage(StrictRecord):
    """Distinct dimensions; unknown tokens/prices remain null, never synthetic USD."""
    model_calls: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    billed_usd: float | None = None
    synthetic_units: float = 0.0
    tool_calls: int = 0
    schema_version: int = 1

    def validate(self):
        import math
        for x in (self.model_calls, self.tool_calls):
            require_count(x, "cost count")
        for x in (self.prompt_tokens, self.completion_tokens):
            if x is not None:
                require_count(x, "tokens")
        for x in (self.billed_usd, self.synthetic_units):
            if x is not None and (not math.isfinite(x) or x < 0):
                raise ValidationError("Invalid resource usage")
