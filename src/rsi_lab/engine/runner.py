"""Preparation orchestration; task semantics remain in the subject adapter."""
from rsi_lab.contracts import Checkpoint, PrepareCursor, RunStatus, CursorFamily
from rsi_lab.errors import BudgetExceeded, ConflictError, OutcomeUnknown, ValidationError, UnsupportedCapability
from rsi_lab.preparation import ResumeRequest
from rsi_lab.ports import SubjectAdapter
from rsi_lab.serialization import decode_json


class PreparationRunner:
    def __init__(self, state, artifacts, subject: SubjectAdapter):
        self.state, self.artifacts, self.subject = state, artifacts, subject
        capabilities = subject.capabilities()
        if not capabilities.snapshot_supported or set(capabilities.checkpoints) != set(Checkpoint):
            raise UnsupportedCapability('Subject lacks the four resumable preparation checkpoints')
        declared = {item.checkpoint: item for item in capabilities.checkpoint_contracts}
        if set(declared) != set(Checkpoint) or not all(item.restorable for item in declared.values()):
            raise UnsupportedCapability('Subject must declare checkpoint schemas and restoration support')

    def prepare(self, spec, context_patch, verification_patch, *, stop_after=4,
                fixed_preparation=None, guard_mode='observe', assigned_q=None, purpose='discovery',
                cursor_family=CursorFamily.PUBLIC_DEVELOPMENT, generation_operation_id=None, candidate_occurrence_id=None):
        if type(stop_after) is not int or not 1 <= stop_after <= 4:
            raise ValidationError('stop_after must be between 1 and 4')
        if self.state.get_pair(spec.pair_id).pairing_mode not in self.subject.capabilities().pairing_modes:
            raise UnsupportedCapability('Subject cannot provide requested pairing mode')
        self.state.create_attempt(spec)
        self.state.bind_preparation_options(spec.attempt_id, {'context_patch': context_patch,
            'verification_patch': verification_patch, 'fixed_preparation': fixed_preparation,
            'guard_mode': guard_mode, 'assigned_q': assigned_q, 'purpose':purpose,
            'cursor_family':cursor_family,'generation_operation_id':generation_operation_id,
            'candidate_occurrence_id':candidate_occurrence_id})
        current = self.state.get_attempt(spec.attempt_id)
        if current['status'] != 'active' or current['phase'] in ('prepared','audited','resolved','materialized','committed'):
            return current
        if current['phase'] == 'created':
            self.state.begin_preparation(spec.attempt_id)
        cursor = PrepareCursor.from_dict(current['cursor']) if current['cursor'] else None
        while cursor is None or list(Checkpoint).index(cursor.checkpoint) + 1 < stop_after:
            index = list(Checkpoint).index(cursor.checkpoint) + 1 if cursor else (2 if fixed_preparation else 0)
            if index >= stop_after:
                break
            operation_id = f'{spec.attempt_id}/checkpoint/{index}'
            request = ResumeRequest(spec, cursor, operation_id, context_patch, verification_patch,
                                    guard_mode, fixed_preparation, 'branch', spec.branch_id, assigned_q,
                                    purpose,cursor_family,generation_operation_id,candidate_occurrence_id)
            try:
                result = self.subject.advance(request)
            except OutcomeUnknown as exc:
                self.state.mark_status(spec.attempt_id, RunStatus.OUTCOME_UNKNOWN, str(exc))
                break
            except BudgetExceeded as exc:
                self.state.mark_status(spec.attempt_id, RunStatus.BUDGET_EXHAUSTED, str(exc))
                break
            if list(Checkpoint).index(result.cursor.checkpoint) != index:
                raise ConflictError('Adapter returned an unexpected checkpoint')
            self.state.record_checkpoint(spec.attempt_id, result.cursor, operation_id=operation_id,
                                         input_ref=spec.source_snapshot,
                                         patch_ref=self.artifacts.put_json({'context': context_patch,
                                                                           'verification': verification_patch}),
                                         receipt_ref=result.receipt)
            cursor = result.cursor
            if not result.valid:
                status = (RunStatus.OUTCOME_UNKNOWN if result.missing_reason == 'verification_outcome_unknown' else
                          RunStatus.BUDGET_EXHAUSTED if result.missing_reason == 'verification_incomplete' else RunStatus.INVALID)
                self.state.mark_status(spec.attempt_id, status, result.missing_reason or 'invalid preparation')
                break
        return self.state.get_attempt(spec.attempt_id)
