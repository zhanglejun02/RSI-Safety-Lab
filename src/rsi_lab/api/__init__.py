"""Supported platform API. Legacy P0 wire records remain in rsi_lab.contracts."""
from .contracts import (
    API_VERSION, Component, SystemVersion, SystemSnapshot, Capabilities,
    SystemDescriptor, PublicTask, ExecutionReceipt, CandidateDraft, UpdateIdentity,
    PreparedUpdate, ActivationCommand, ActivationReceipt, CostUsage,
)
from .ports import (
    SystemAdapter, ToolSession, PublicEvaluator, TaskPlan, TaskEnvironment,
    TaskPackage, Detector, AdmissionPolicy,
)

__all__ = [
    "API_VERSION", "Component", "SystemVersion", "SystemSnapshot", "Capabilities",
    "SystemDescriptor", "PublicTask", "ExecutionReceipt", "CandidateDraft", "UpdateIdentity",
    "PreparedUpdate", "ActivationCommand", "ActivationReceipt", "CostUsage",
    "SystemAdapter", "ToolSession", "PublicEvaluator", "TaskPlan", "TaskEnvironment",
    "TaskPackage", "Detector", "AdmissionPolicy",
]
