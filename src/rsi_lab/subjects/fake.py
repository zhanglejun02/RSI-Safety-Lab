"""Deterministic executable subject: actual q bytes -> candidate -> tests -> selection.

No admission, deployment or arbitrary code execution is exposed here.
"""
from dataclasses import replace

from rsi_lab.contracts import (ArtifactOccurrence, ArtifactRef, CandidateBinding, Capabilities,
    Checkpoint, CheckpointCapability, CursorFamily, PairingMode, PrepareCursor, SnapshotManifest, TaskCursor, Version)
from rsi_lab.errors import ConflictError, ValidationError, OutcomeUnknown
from rsi_lab.infra.models import FakeModelClient, ModelResponse
from rsi_lab.interventions.context import ContextReceipt, ExperienceEntry, MemoryState, render_context
from rsi_lab.preparation import CheckpointResult, PreparationState, ResumeRequest
from rsi_lab.serialization import decode_json, digest
from rsi_lab.subjects.verification import ArchiveScore, select_native


class FakeSubject:
    def __init__(self, store, journal, verification, model=None):
        self.store, self.journal, self.verification = store, journal, verification
        self.model = model or FakeModelClient()

    def capabilities(self):
        fields = (('selected_entries','budget_units'), (), ('test_ids','budget_units'), ())
        contracts = tuple(CheckpointCapability(cp, 'preparation-state-v1', allowed)
                          for cp, allowed in zip(Checkpoint, fields))
        return Capabilities('fake.transfer.v1', tuple(Checkpoint), tuple(PairingMode), True,
                            checkpoint_contracts=contracts)

    def _read(self, cls, ref):
        return cls.from_dict(decode_json(self.store.read_bytes(ref)))

    def advance(self, request: ResumeRequest) -> CheckpointResult:
        # Intent precedes each stage; completed calls replay the recorded result.
        operation = self.journal.reserve(request.operation_id, request.owner_kind,
            request.owner_id, 0, request.purpose, request.to_dict(), attempt_id=request.attempt.attempt_id)
        if operation['status'] == 'settled':
            return CheckpointResult.from_dict(operation['receipt']['result'])
        if operation['status'] != 'reserved':
            raise OutcomeUnknown('Checkpoint outcome requires reconciliation')
        snapshot = self._read(SnapshotManifest, request.attempt.source_snapshot)
        edit_base = self._read(Version, snapshot.edit_base_version)
        if request.assigned_q is not None:
            edit_base = replace(edit_base, q=request.assigned_q)
        if request.cursor:
            current = self._read(PreparationState, request.cursor.local_state)
            if current.source_snapshot != request.attempt.source_snapshot:
                raise ConflictError('Preparation cursor belongs to another source snapshot')
            if current.context_patch != request.context_patch or current.verification_patch != request.verification_patch or current.assigned_q != request.assigned_q:
                raise ConflictError('Cannot change patches after preparation started')
            index = list(Checkpoint).index(request.cursor.checkpoint) + 1
        elif request.fixed_preparation:
            inherited = self._read(PreparationState, request.fixed_preparation)
            if not inherited.valid or not inherited.candidates or inherited.native_plan != ('ok',):
                raise ValidationError('Fixed candidate requires a valid pre_verification preparation')
            if inherited.verification_receipt is not None:
                raise ValidationError('Cannot redo v from an already verified preparation')
            for candidate in inherited.candidates:
                self._read(Version, candidate.artifact)
            current = replace(inherited, source_snapshot=request.attempt.source_snapshot,
                              context_patch=request.context_patch, verification_patch=request.verification_patch,
                              assigned_q=request.assigned_q)
            index = 2
        else:
            current = PreparationState(request.attempt.source_snapshot,
                                       request.context_patch, request.verification_patch, assigned_q=request.assigned_q)
            index = 0
        if index >= 4 or not current.valid:
            raise ConflictError('Cannot advance completed or invalid preparation')
        checkpoint = list(Checkpoint)[index]
        if index == 0:
            memory = self._read(MemoryState, snapshot.memory)
            receipt = render_context(memory, edit_base.q, request.context_patch,
                request.generation_operation_id or f'{request.attempt.attempt_id}/generate', self.store)
            current = replace(current, context_receipt=self.store.put_json(receipt),
                              valid=receipt.valid, missing_reason=receipt.invalid_reason)
        elif index == 1:
            receipt = self._read(ContextReceipt, current.context_receipt)
            if not receipt.valid:
                raise ValidationError('Invalid actual context cannot be sent to a model')
            body = self.store.read_bytes(receipt.actual_context)
            generation_id = request.generation_operation_id or f'{request.attempt.attempt_id}/generate'
            operation = self.journal.reserve(generation_id, request.owner_kind, request.owner_id,
                5, request.purpose, {'request_id': generation_id, 'context': receipt.actual_context.to_dict(),
                                 'edit_base': edit_base.to_dict()}, attempt_id=request.attempt.attempt_id)
            if operation['status'] == 'settled':
                response = ModelResponse.from_dict(operation['receipt']['response'])
            else:
                lookup=getattr(self.model,'lookup',None)
                response=lookup(generation_id) if callable(lookup) else None
                if response is None:
                    if operation['status'] != 'reserved':
                        raise OutcomeUnknown(f'Generation {generation_id} cannot be retried without reconciliation')
                    try:
                        response=self.model.generate(generation_id,body,edit_base)
                    except Exception as exc:
                        self.journal.mark_unknown(generation_id,f'{type(exc).__name__}: {exc}')
                        response=lookup(generation_id) if callable(lookup) else None
                        if response is None:
                            raise OutcomeUnknown(f'Generation {generation_id} outcome unknown') from exc
                self.journal.settle(generation_id,5,{'response':response.to_dict()})
            expected_hash = digest({'request_id': generation_id, 'context_sha256': receipt.actual_context.sha256, 'edit_base': edit_base.to_dict()})
            if (response.context_sha256 != receipt.actual_context.sha256 or response.request_id != generation_id
                    or response.request_sha256 != expected_hash or response.synthetic_input_units != receipt.synthetic_units
                    or (response.version.q, response.version.v, response.version.s) != (edit_base.q, edit_base.v, edit_base.s)):
                raise ConflictError('Generated response request binding or frozen mechanisms differ')
            occurrence = ArtifactOccurrence(request.candidate_occurrence_id or f'{request.attempt.attempt_id}_generated_00',
                self.store.put_json(response.version), request.attempt.attempt_id,
                request.attempt.candidate_origin, 'generation')
            current = replace(current, candidates=(occurrence,),
                shared_cost_ref=generation_id if request.owner_kind == 'pair' else None)
        elif index == 2:
            if not current.candidates:
                raise ValidationError('pre_verification requires actual candidate artifacts')
            current = replace(current, native_plan=('ok',))
        else:
            receipt = self.verification.verify(current.candidates, request.verification_patch,
                attempt_id=request.attempt.attempt_id, branch_id=request.attempt.branch_id,
                guard_mode=request.guard_mode, purpose=request.purpose, cursor_family=request.cursor_family)
            receipt_ref = self.store.put_json(receipt)
            archive = decode_json(self.store.read_bytes(snapshot.archive))
            history = tuple(ArchiveScore.from_dict(item) for item in archive.get('entries', []))
            for item in history:
                self.store.read_bytes(item.occurrence.artifact)
                for ref in item.validation_receipts:
                    self.store.read_bytes(ref)
            selected = select_native(current.candidates, receipt, edit_base.s, history=history)
            memory = self._read(MemoryState, snapshot.memory)
            public_entry = ExperienceEntry(f'delta_{request.attempt.attempt_id}',
                'Public verification outcomes: ' + ','.join(
                    f'{test.test_id}:{test.status}' for test in receipt.executed))
            memory = replace(memory, entries=memory.entries + (public_entry,),
                             active_ids=memory.active_ids + (public_entry.entry_id,))
            # Immutable prepared state preserves deployed and edit-base pointers.
            scores = []
            for candidate in current.candidates:
                tests = tuple(t for t in receipt.executed if t.candidate_occurrence_id == candidate.occurrence_id)
                if tests:
                    scores.append(ArchiveScore(candidate, sum(t.status == 'pass' for t in tests),
                        tuple(self.store.put_json(t) for t in tests),
                        len(tests) == len(receipt.requested_plan) and all(t.status in ('pass','fail') for t in tests),
                        len(tests) == len(receipt.requested_plan) and all(t.status == 'pass' for t in tests), 'current'))
            cursor_namespace = f'{request.attempt.attempt_id}/validation' if request.purpose=='online_audit' else 'main'
            cursors=list(snapshot.cursors)
            for i,c in enumerate(cursors):
                if c.family==request.cursor_family and c.namespace==cursor_namespace:
                    cursors[i]=replace(c,position=c.position+len(receipt.executed))
                    break
            else:
                cursors.append(TaskCursor(request.cursor_family,position=len(receipt.executed),namespace=cursor_namespace))
            prepared = replace(snapshot, memory=self.store.put_json(memory),
                components=replace(snapshot.components, q=self.store.put_json({'registered_id': edit_base.q.value})),
                archive=self.store.put_json({'schema_version': 1, 'entries': (*history, *scores),
                    'current_candidates': current.candidates, 'verification_receipt': receipt_ref}),
                cursors=tuple(cursors))
            prepared_ref = self.store.put_json(prepared)
            binding = (self.store.put_json(CandidateBinding(request.attempt.attempt_id, selected,
                       prepared_ref, request.attempt.protocol)) if selected else None)
            current = replace(current, verification_receipt=receipt_ref, native_selected=selected,
                prepared_snapshot=prepared_ref, candidate_binding=binding,
                valid=receipt.complete, missing_reason=(None if receipt.complete else
                    'verification_outcome_unknown' if any(t.status in ('error','timeout') for t in receipt.executed) else 'verification_incomplete'))
        local_ref = self.store.put_json(current)
        cursor = PrepareCursor(checkpoint, local_ref, f'{request.attempt.attempt_id}/checkpoint/{index+1}',
                               next_operation_index=index+1)
        stage_receipt = self.store.put_json({'schema_version': 1, 'operation_id': request.operation_id,
            'checkpoint': checkpoint.value, 'state': local_ref, 'subject_executed': True})
        result = CheckpointResult(cursor, stage_receipt, current.valid, current.missing_reason)
        self.journal.settle(request.operation_id, 0, {'result': result.to_dict()})
        return result
