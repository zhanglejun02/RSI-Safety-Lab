"""Errors surfaced at CLI and adapter boundaries."""


class LabError(Exception):
    """Expected, actionable lab error."""


class ValidationError(LabError, ValueError):
    """An input violates its declared schema."""


class IntegrityError(LabError):
    """Stored bytes or references no longer match their manifest."""


class ConflictError(LabError):
    """An identity was reused with different content or state is stale."""


class UnsupportedCapability(LabError):
    """A protocol cannot run on the declared backend."""


class BudgetExceeded(LabError):
    """Available budget cannot cover the requested reservation."""


class PermissionDenied(LabError):
    """A capability does not authorize this operation."""



class OutcomeUnknown(LabError):
    """Execution cannot safely be retried under the same operation identity."""
