"""Actual isolated validation, durable receipts and pure native selection.

The runtime owns task dispatch and charges. Interventions describe plans only.
Every test uses a separate writable world, including repeated legal controls.
No validation action deploys a candidate or touches a branch's deployment world.
"""

from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from rsi_lab.contracts import (
    ArtifactOccurrence, ArtifactRef, CursorFamily, SProgram, StrictRecord, Version,
    require_count, require_id,
)
from rsi_lab.errors import BudgetExceeded, ConflictError, ValidationError
from rsi_lab.infra.artifacts import ArtifactStore, _checked_path
from rsi_lab.infra.journal import Journal
from rsi_lab.interventions.validation import PUBLIC_PLAN, VerificationPatch, verification_plan
from rsi_lab.oracles.workflow import evaluate
from rsi_lab.serialization import canonical_bytes, decode_json, digest
from rsi_lab.tasks.workflow import Workflow

TEST_IMPLEMENTATION = "transfer-validation-v1"


@dataclass(frozen=True)
class TestExecution(StrictRecord):
    candidate_occurrence_id: str
    candidate: ArtifactRef
    test_id: str
    operation_id: str
    status: str
    trace: ArtifactRef | None
    evaluation: ArtifactRef | None
    synthetic_cost_units: int
    wall_seconds: float
    reason: str | None = None
    implementation_version: str = TEST_IMPLEMENTATION
    schema_version: int = 1

    def validate(self):
        for name in ("candidate_occurrence_id", "test_id", "operation_id"):
            require_id(getattr(self, name), name)
        if self.status not in ("pass", "fail", "timeout", "error"):
            raise ValidationError("Unknown actual test status")
        if self.synthetic_cost_units not in (0, 1):
            raise ValidationError("A test consumes one unit when confirmed complete")
        if self.wall_seconds < 0 or self.wall_seconds != self.wall_seconds or self.wall_seconds == float("inf"):
            raise ValidationError("Invalid wall time")
        if self.status in ("pass", "fail") and (self.trace is None or self.evaluation is None or self.synthetic_cost_units != 1):
            raise ValidationError("Completed test requires actual trace, evaluation and settled cost")
        if self.status in ("timeout", "error") and not self.reason:
            raise ValidationError("Incomplete test requires a reason")
        if self.implementation_version != TEST_IMPLEMENTATION:
            raise ValidationError("Unknown test implementation")


@dataclass(frozen=True)
class SkippedTest(StrictRecord):
    candidate_occurrence_id: str
    test_id: str
    reason: str
    schema_version: int = 1

    def validate(self):
        for key in ("candidate_occurrence_id", "test_id"):
            require_id(getattr(self, key), key)
        if not self.reason:
            raise ValidationError("Unexecuted test requires a reason")


@dataclass(frozen=True)
class VerificationReceipt(StrictRecord):
    attempt_id: str
    branch_id: str
    candidate_occurrences: tuple[ArtifactOccurrence, ...]
    patch: VerificationPatch
    original_plan: tuple[str, ...]
    requested_plan: tuple[str, ...]
    executed: tuple[TestExecution, ...]
    not_run: tuple[SkippedTest, ...]
    complete: bool
    synthetic_cost_units: int
    matching_status: str
    schema_version: int = 1

    def validate(self):
        require_id(self.attempt_id, "attempt_id")
        require_id(self.branch_id, "branch_id")
        if self.original_plan != PUBLIC_PLAN or self.requested_plan != verification_plan(self.patch):
            raise ValidationError("Receipt plans do not match registered policy")
        if self.synthetic_cost_units != sum(result.synthetic_cost_units for result in self.executed):
            raise ValidationError("Receipt cost differs from actual execution records")
        if not self.candidate_occurrences or len({c.occurrence_id for c in self.candidate_occurrences}) != len(self.candidate_occurrences):
            raise ValidationError("Receipt requires unique candidate occurrences")
        expected = {(candidate.occurrence_id, test) for candidate in self.candidate_occurrences for test in self.requested_plan}
        actual = [(item.candidate_occurrence_id, item.test_id) for item in self.executed + self.not_run]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValidationError("Receipt must cover each requested candidate/test exactly once")
        by_id = {item.occurrence_id: item.artifact for item in self.candidate_occurrences}
        if any(by_id[item.candidate_occurrence_id] != item.candidate for item in self.executed):
            raise ValidationError("Execution candidate differs from bound occurrence")
        actual_complete = not self.not_run and all(item.status in ("pass", "fail") for item in self.executed)
        if self.complete != actual_complete:
            raise ValidationError("Receipt completion does not match actual results")
        expected_match = ("complete" if self.complete else "incomplete") if self.patch.kind in ("restore", "placebo") else "not_applicable"
        if self.matching_status != expected_match:
            raise ValidationError("Invalid verification matching status")


@dataclass(frozen=True)
class ArchiveScore(StrictRecord):
    occurrence: ArtifactOccurrence
    passed_tests: int
    validation_receipts: tuple[ArtifactRef, ...]
    complete: bool = True
    all_pass: bool = True
    source: str = "prehistory"
    schema_version: int = 1

    def validate(self):
        require_count(self.passed_tests, "passed_tests")
        if (not self.validation_receipts or len(self.validation_receipts) < self.passed_tests
                or len(set(self.validation_receipts)) != len(self.validation_receipts)):
            raise ValidationError("Historical score requires provenance for each passed test")
        if self.source not in ("prehistory", "current"):
            raise ValidationError("Unknown archive score source")


def select_native(candidates: tuple[ArtifactOccurrence, ...], receipt: VerificationReceipt,
                  selector: SProgram, history: tuple[ArchiveScore, ...] = ()) -> ArtifactOccurrence | None:
    """Select from actual completed results, with history included only by its native rule."""
    if candidates != receipt.candidate_occurrences:
        raise ConflictError("Native selection candidate pool differs from verification binding")
    if type(selector) is not SProgram:
        raise ValidationError("Unknown native selector")
    eligible = []
    for candidate in candidates:
        results = [item for item in receipt.executed if item.candidate_occurrence_id == candidate.occurrence_id]
        if len(results) == len(receipt.requested_plan) and all(item.status == "pass" for item in results):
            eligible.append((len(results), candidate))
    if selector == SProgram.ARCHIVE_SCORE:
        seen = {item.occurrence_id: item for item in candidates}
        for entry in history:
            if type(entry) is not ArchiveScore:
                raise ValidationError("Archive entries require typed provenance")
            if entry.occurrence.occurrence_id in seen:
                raise ConflictError("Archive occurrence duplicates a current or historical candidate")
            seen[entry.occurrence.occurrence_id] = entry.occurrence
            if entry.complete and entry.all_pass:
                eligible.append((entry.passed_tests, entry.occurrence))
    if not eligible:
        return None
    if selector == SProgram.LATEST_VALID:
        return min((item[1] for item in eligible), key=lambda item: item.occurrence_id)
    return min(eligible, key=lambda item: (-item[0], item[1].occurrence_id))[1]


class VerificationRuntime:
    def __init__(self, store: ArtifactStore, journal: Journal, workspace: Path, *, fault_hook=None, operation_naming="pool"):
        if operation_naming not in ('pool','single'):
            raise ValidationError('Unknown operation naming')
        self.operation_naming=operation_naming
        self.fault_hook=fault_hook or (lambda point: None)
        self.store = store
        self.journal = journal
        self.workspace = _checked_path(Path(workspace))
        self.workspace.mkdir(parents=True, exist_ok=True)

    def verify(self, candidates: tuple[ArtifactOccurrence, ...], patch: VerificationPatch, *,
               attempt_id: str, branch_id: str, guard_mode: str = "observe", purpose: str = 'discovery',
               cursor_family: CursorFamily = CursorFamily.PUBLIC_DEVELOPMENT) -> VerificationReceipt:
        if purpose not in ('discovery','online_audit') or cursor_family not in (CursorFamily.PUBLIC_DEVELOPMENT,CursorFamily.ADMISSION_PROBE):
            raise ValidationError('Invalid validation evidence purpose')
        if (purpose=='online_audit') != (cursor_family==CursorFamily.ADMISSION_PROBE):
            raise ValidationError('Validation cursor/purpose mismatch')
        require_id(attempt_id, "attempt_id")
        require_id(branch_id, "branch_id")
        if type(candidates) is not tuple or not candidates or any(type(c) is not ArtifactOccurrence for c in candidates):
            raise ValidationError("Verification requires a nonempty tuple of candidate occurrences")
        if len({c.occurrence_id for c in candidates}) != len(candidates):
            raise ValidationError("Candidate occurrences must be unique")
        if guard_mode not in ("observe", "enforce"):
            raise ValidationError("Unknown guard mode")
        if self.operation_naming=='single' and len(candidates)!=1:
            raise ValidationError('Single-candidate operation naming requires exactly one candidate')
        plan = verification_plan(patch)
        # Validate every candidate before any dispatch or charge.
        programs = {c.occurrence_id: Version.from_dict(decode_json(self.store.read_bytes(c.artifact))).agent for c in candidates}
        for candidate in candidates:
            for test_id in plan:
                require_id(f"{attempt_id}/verify/{candidate.occurrence_id}/{test_id}", "verification_operation_id")
        executed, skipped = [], []
        stop_reason = None
        for candidate in candidates:
            candidate_spent = 0
            for test_index, test_id in enumerate(plan):
                if stop_reason or candidate_spent >= patch.max_cost_units_per_candidate:
                    skipped.append(SkippedTest(candidate.occurrence_id, test_id, stop_reason or "candidate_budget_exhausted"))
                    continue
                operation = (f"{attempt_id}/verify/{test_id}" if self.operation_naming=='single' else
                             f"{attempt_id}/verify/{candidate.occurrence_id}/{test_id}")
                request = {"schema_version": 1, "candidate_occurrence": candidate.to_dict(),
                           "test_id": test_id, "test_index": test_index, "patch": patch.to_dict(),
                           "guard_mode": guard_mode, "cursor_family":cursor_family.value, "purpose":purpose, "implementation_version": TEST_IMPLEMENTATION}
                try:
                    reserved = self.journal.reserve(operation, "branch", branch_id, 1, purpose, request, attempt_id=attempt_id)
                except BudgetExceeded:
                    stop_reason = "branch_budget_exhausted"
                    skipped.append(SkippedTest(candidate.occurrence_id, test_id, stop_reason))
                    continue
                if reserved["status"] == "settled":
                    result = TestExecution.from_dict(reserved["receipt"]["execution"])
                    if (result.operation_id != operation or result.candidate != candidate.artifact
                            or result.candidate_occurrence_id != candidate.occurrence_id or result.test_id != test_id
                            or result.synthetic_cost_units != reserved["actual_units"]):
                        raise ConflictError(f"Verification receipt binding mismatch: {operation}")
                    for ref in (result.trace, result.evaluation):
                        if ref is not None:
                            self.store.read_bytes(ref)
                elif reserved["status"] == "outcome_unknown":
                    result = TestExecution.from_dict(decode_json(reserved["unknown_reason"]))
                    stop_reason = "execution_outcome_unknown"
                elif reserved["status"] == "released":
                    raise ConflictError(f"Cannot reuse released verification operation: {operation}")
                else:
                    result = self._execute(candidate, programs[candidate.occurrence_id], test_id, operation,
                                           branch_id, guard_mode, cursor_family)
                    if result.status not in ("pass", "fail"):
                        stop_reason = "execution_outcome_unknown"
                executed.append(result)
                candidate_spent += result.synthetic_cost_units
        complete = not skipped and all(item.status in ("pass", "fail") for item in executed)
        matching = ("complete" if complete else "incomplete") if patch.kind in ("restore", "placebo") else "not_applicable"
        return VerificationReceipt(attempt_id, branch_id, candidates, patch, PUBLIC_PLAN, plan,
                                   tuple(executed), tuple(skipped), complete,
                                   sum(item.synthetic_cost_units for item in executed), matching)

    def _execute(self, candidate, program, test_id, operation, branch_id, guard_mode, cursor_family):
        key = digest({"operation": operation})
        world = Workflow(self.workspace / f"{key}.sqlite")
        task = world.initialize_test(key, test_id)
        self.journal.record_exposure(f"{operation}/exposure", operation, cursor_family.value, test_id,
                                     branch_id=branch_id)
        started = perf_counter()
        try:
            world.execute(program, task, operation, guard_mode=guard_mode, purpose="validation")
        except Exception as error:
            result = TestExecution(candidate.occurrence_id, candidate.artifact, test_id, operation,
                                   "timeout" if isinstance(error, TimeoutError) else "error", None, None, 0,
                                   float(perf_counter() - started), f"{type(error).__name__}: {error}")
            self.journal.mark_unknown(operation, canonical_bytes(result).decode("utf-8"))
            return result
        self.fault_hook("after_effect")
        records = world.records(key)
        evaluation = evaluate(records, (task,))
        trace = self.store.put_json({"schema_version": 1, "records": [item.to_dict() for item in records]})
        evaluation_ref = self.store.put_json(evaluation.to_dict())
        passed = (evaluation.complete and evaluation.actual_violations == 0
                  and (not task.legal or evaluation.legal_completed == 1))
        status = ("pass" if passed else "fail") if evaluation.complete else "error"
        result = TestExecution(candidate.occurrence_id, candidate.artifact, test_id, operation,
                               status, trace, evaluation_ref, 1, float(perf_counter() - started),
                               None if evaluation.complete else "independent_evaluation_incomplete")
        self.journal.settle(operation, 1, {"execution": result.to_dict()})
        return result
