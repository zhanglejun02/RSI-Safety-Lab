"""Application-level allowlists for trusted fixture code, not an OS sandbox."""

from dataclasses import dataclass, field

from rsi_lab.contracts import ArtifactRef
from rsi_lab.errors import PermissionDenied, ValidationError
from rsi_lab.ports import ArtifactReader


@dataclass(frozen=True, init=False)
class ArtifactView:
    _reader: ArtifactReader = field(repr=False)
    allowed: frozenset[ArtifactRef]

    def __init__(self, reader: ArtifactReader, allowed: frozenset[ArtifactRef]):
        if type(allowed) is not frozenset or any(type(ref) is not ArtifactRef for ref in allowed):
            raise ValidationError("ArtifactView requires a frozen set of ArtifactRef values")
        if not callable(getattr(reader, "read_bytes", None)):
            raise ValidationError("ArtifactView requires an artifact reader")
        object.__setattr__(self, "_reader", reader)
        object.__setattr__(self, "allowed", allowed)

    def read_bytes(self, ref: ArtifactRef) -> bytes:
        if type(ref) is not ArtifactRef:
            raise ValidationError("ArtifactView requires a typed ArtifactRef")
        if ref not in self.allowed:
            raise PermissionDenied(f"Artifact is outside this view: {ref.sha256}")
        return self._reader.read_bytes(ref)

