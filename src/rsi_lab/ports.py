"""Small storage ports used by the current kernel, independent of suppliers."""

from typing import Protocol

from .contracts import ArtifactRef


class ArtifactReader(Protocol):
    def read_bytes(self, ref: ArtifactRef) -> bytes: ...


class ArtifactRepository(ArtifactReader, Protocol):
    def put_bytes(self, data: bytes, media_type: str = "application/octet-stream") -> ArtifactRef: ...




class SubjectAdapter(Protocol):
    """Preparation capability only; authorized staging is a separate contract."""
    def capabilities(self) -> "Capabilities": ...
    def advance(self, request: "ResumeRequest") -> "CheckpointResult": ...


from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .contracts import Capabilities
    from .preparation import ResumeRequest, CheckpointResult
