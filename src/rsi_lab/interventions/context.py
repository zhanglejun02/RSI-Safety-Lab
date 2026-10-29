"""Frozen q interventions and byte-level receipts, independent of the generator.

Only the current arm's active memory can supply a replacement. Rendering and
truncation happen before generation; the model receives the saved actual bytes.
"""

from dataclasses import dataclass
from enum import Enum

from rsi_lab.contracts import ArtifactRef, QProgram, StrictRecord, require_count, require_id
from rsi_lab.errors import ValidationError
from rsi_lab.frozen import fixture_bytes
from rsi_lab.infra.artifacts import ArtifactStore
from rsi_lab.serialization import decode_json


class ContextKind(str, Enum):
    IDENTITY = "identity"
    RESTORE = "restore"
    PLACEBO = "placebo"
    REMOVE = "remove"


@dataclass(frozen=True)
class ExperienceEntry(StrictRecord):
    entry_id: str
    body: str
    schema_version: int = 1

    def validate(self):
        require_id(self.entry_id, "entry_id")
        if not self.body.isascii() or "\n" in self.body or "\r" in self.body:
            raise ValidationError("ascii_entries_newline_v1 requires single-line ASCII entry bodies")


@dataclass(frozen=True)
class MemoryState(StrictRecord):
    entries: tuple[ExperienceEntry, ...]
    active_ids: tuple[str, ...]
    quarantined_ids: tuple[str, ...] = ()
    schema_version: int = 1

    def validate(self):
        ids = [entry.entry_id for entry in self.entries]
        if len(ids) != len(set(ids)):
            raise ValidationError("Duplicate experience ID")
        for name, selection in (("active", self.active_ids), ("quarantined", self.quarantined_ids)):
            if len(selection) != len(set(selection)) or not set(selection) <= set(ids):
                raise ValidationError(f"Invalid {name} memory index")
        if set(self.active_ids) & set(self.quarantined_ids):
            raise ValidationError("Active and quarantined memory indices must be disjoint")


@dataclass(frozen=True)
class ContextPatch(StrictRecord):
    kind: ContextKind = ContextKind.IDENTITY
    budget_units: int = 16
    schema_version: int = 1

    def validate(self):
        require_count(self.budget_units, "budget_units")


@dataclass(frozen=True)
class ContextSpan(StrictRecord):
    entry_id: str
    start: int
    end: int
    complete: bool
    schema_version: int = 1

    def validate(self):
        require_id(self.entry_id, "entry_id")
        require_count(self.start, "start")
        require_count(self.end, "end")
        if self.end <= self.start:
            raise ValidationError("Context spans must contain at least one byte")


@dataclass(frozen=True)
class ContextReceipt(StrictRecord):
    request_id: str
    q: QProgram
    patch: ContextPatch
    retrieval_candidate_ids: tuple[str, ...]
    before_ids: tuple[str, ...]
    after_ids: tuple[str, ...]
    retained_ids: tuple[str, ...]
    raw_spans: tuple[ContextSpan, ...]
    actual_spans: tuple[ContextSpan, ...]
    raw_context: ArtifactRef
    actual_context: ArtifactRef
    request_sha256: str
    synthetic_units: int
    budget_units: int
    truncated: bool
    valid: bool
    invalid_reason: str | None
    matching_error_units: int | None
    renderer_id: str = "ascii_entries_newline_v1"
    tokenizer_id: str = "synthetic_bytes4_v1"
    matching_tolerance_units: int = 0
    schema_version: int = 1

    def validate(self):
        require_id(self.request_id, "request_id")
        require_count(self.synthetic_units, "synthetic_units")
        require_count(self.budget_units, "budget_units")
        if self.synthetic_units != synthetic_units(self.actual_context.size_bytes):
            raise ValidationError("Context synthetic count does not match final byte count")
        if self.actual_context.size_bytes > 4 * self.budget_units or self.budget_units != self.patch.budget_units:
            raise ValidationError("Actual context exceeds its declared budget")
        if self.request_sha256 != self.actual_context.sha256:
            raise ValidationError("Final context request hash differs from actual bytes")
        if self.truncated != (self.actual_context.size_bytes < self.raw_context.size_bytes):
            raise ValidationError("Invalid context truncation flag")
        if self.valid != (self.invalid_reason is None):
            raise ValidationError("Context validity requires an explicit invalid reason")
        if self.invalid_reason is not None and not self.invalid_reason:
            raise ValidationError("Invalid reason cannot be empty")
        if self.renderer_id != "ascii_entries_newline_v1" or self.tokenizer_id != "synthetic_bytes4_v1":
            raise ValidationError("Unregistered renderer or tokenizer")
        if self.matching_tolerance_units != 0:
            raise ValidationError("Frozen matching tolerance is zero")
        if self.retained_ids != tuple(span.entry_id for span in self.actual_spans if span.complete):
            raise ValidationError("Retained IDs must reflect complete actual spans")
        if tuple(span.entry_id for span in self.raw_spans) != self.after_ids:
            raise ValidationError("Raw spans must reflect patched ordered entries")
        if any(span.end > self.actual_context.size_bytes for span in self.actual_spans):
            raise ValidationError("Actual span exceeds final request bytes")


def synthetic_units(byte_count: int) -> int:
    require_count(byte_count, "byte_count")
    return (byte_count + 3) // 4


def frozen_memory() -> MemoryState:
    entries = decode_json(fixture_bytes("context_entries.json"))
    return MemoryState(tuple(ExperienceEntry(key, entries[key]) for key in ("G", "W", "P", "Z")),
                       ("G", "W", "P", "Z"))


def select_experiences(memory: MemoryState, q: QProgram) -> tuple[str, ...]:
    """Pure selection on one active index; missing required entries are not fetched."""
    if type(memory) is not MemoryState or type(q) is not QProgram:
        raise ValidationError("q selection requires typed current memory and a registered QProgram")
    requested = ("G", "W") if q == QProgram.OLD else ("W", "P")
    return tuple(key for key in requested if key in memory.active_ids)


def _render(ids, entries):
    output = bytearray()
    spans = []
    for key in ids:
        start = len(output)
        output.extend(entries[key].body.encode("ascii") + b"\n")
        spans.append(ContextSpan(key, start, len(output), True))
    return bytes(output), tuple(spans)


def render_context(memory: MemoryState, q: QProgram, patch: ContextPatch,
                   request_id: str, store: ArtifactStore) -> ContextReceipt:
    if type(memory) is not MemoryState or type(q) is not QProgram or type(patch) is not ContextPatch:
        raise ValidationError("render_context requires typed memory, q and patch")
    require_id(request_id, "request_id")
    # Construct this restricted lookup first; quarantined entries cannot be read
    # by either q or a restorative intervention.
    active = {entry.entry_id: entry for entry in memory.entries if entry.entry_id in memory.active_ids}
    before = select_experiences(memory, q)
    expected_before = ("G", "W") if q == QProgram.OLD else ("W", "P")
    reason = None if before == expected_before else "selected_entry_unavailable"
    after = before
    target_ids = select_experiences(memory, QProgram.OLD)
    target_bytes = None
    if patch.kind in (ContextKind.RESTORE, ContextKind.PLACEBO):
        if target_ids != ("G", "W"):
            reason = "restore_target_unavailable"
        else:
            target_bytes, _ = _render(target_ids, active)
        if patch.kind == ContextKind.RESTORE:
            after = target_ids
        else:
            required = ("Z", "W")
            after = tuple(key for key in required if key in active)
            if after != required:
                reason = "placebo_entry_unavailable"
            elif active["Z"].body != decode_json(fixture_bytes("context_entries.json"))["Z"]:
                reason = "placebo_entry_not_registered"
    elif patch.kind == ContextKind.REMOVE:
        after = tuple(key for key in before if key != "G")
    raw, raw_spans = _render(after, active)
    actual = raw[:4 * patch.budget_units]
    actual_spans = tuple(ContextSpan(span.entry_id, span.start, min(span.end, len(actual)),
                                    span.end <= len(actual))
                         for span in raw_spans if span.start < len(actual))
    matching_error = None
    if patch.kind in (ContextKind.RESTORE, ContextKind.PLACEBO) and target_bytes is not None:
        matching_error = abs(synthetic_units(len(actual)) - synthetic_units(len(target_bytes)))
        if len(actual) != len(raw):
            reason = "restoration_truncated"
        elif len(after) != len(target_ids) or len(actual) != len(target_bytes) or matching_error != 0:
            reason = "restoration_match_failed"
        elif patch.kind == ContextKind.RESTORE and actual != target_bytes:
            reason = "restore_bytes_changed"
    raw_ref = store.put_bytes(raw, "text/plain; charset=us-ascii")
    actual_ref = store.put_bytes(actual, "text/plain; charset=us-ascii")
    return ContextReceipt(
        request_id=request_id, q=q, patch=patch, retrieval_candidate_ids=memory.active_ids,
        before_ids=before, after_ids=after,
        retained_ids=tuple(span.entry_id for span in actual_spans if span.complete),
        raw_spans=raw_spans, actual_spans=actual_spans, raw_context=raw_ref, actual_context=actual_ref,
        request_sha256=actual_ref.sha256, synthetic_units=synthetic_units(len(actual)),
        budget_units=patch.budget_units, truncated=len(actual) < len(raw), valid=reason is None,
        invalid_reason=reason, matching_error_units=matching_error,
    )
