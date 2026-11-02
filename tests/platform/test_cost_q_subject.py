from dataclasses import replace

import pytest

from rsi_lab.api import PublicTask
from rsi_lab.contracts import CandidateOrigin
from rsi_lab.errors import ValidationError
from rsi_lab.infra.chat_models import ScriptedChatProvider
from rsi_lab.serialization import canonical_bytes, decode_json, digest
from rsi_lab.subjects.cost_q import (CONTROL_CACHE_SOURCE, CostQSystemAdapter,
    ExperienceSelector, TOOL_PROTOCOL, build_public_memory, control_memory,
    control_selector, make_initial_snapshot, native_valid, scripted_cost_response)
from rsi_lab.tasks.cost_authorization import (FOUNDER_SOURCE, SAFE_REFERENCE_SOURCE,
    evaluate_source, make_suite)


class CostEvaluator:
    def evaluate(self, snapshot, operation_id):
        measured = evaluate_source(snapshot.version.component('policy')['source'], make_suite(701, 'public'))
        return {'complete': measured['complete'], 'risk': int(measured['violations'] > 0),
            'legal_total': measured['legal_total'], 'legal_completed': measured['legal_completed'],
            'synthetic_units': measured['cost'], 'feedback': {'complete_correct': measured['correctness'] == 1},
            'public_trajectories': []}


def subject(tmp_path, responder=scripted_cost_response, selector=None):
    provider = ScriptedChatProvider(tmp_path / 'provider.sqlite', responder)
    initial = make_initial_snapshot(701, control_memory(), selector or control_selector('old'))
    return CostQSystemAdapter(tmp_path / 'system', provider, initial)


def test_q_only_changes_selected_context_then_real_source_descendants(tmp_path):
    root = subject(tmp_path)
    before = root.snapshot()
    draft = root.propose_mechanism('q', CostEvaluator())
    assert draft.native_valid and draft.origin == CandidateOrigin.CONTROL
    assert root.snapshot() == before
    assert draft.candidate.state_json == before.state_json
    assert draft.candidate.version.component('policy') == before.version.component('policy')
    assert draft.candidate.version.component('experience') == before.version.component('experience')
    child = root.fork(draft.candidate, tmp_path / 'child')
    old = root.propose('old-edit', CostEvaluator())
    new = child.propose('new-edit', CostEvaluator())
    assert old.native_valid and new.native_valid
    assert old.candidate.version.component('policy')['source'] == SAFE_REFERENCE_SOURCE
    assert new.candidate.version.component('policy')['source'] == CONTROL_CACHE_SOURCE
    assert new.candidate.version.component('experience_selector') == draft.candidate.version.component('experience_selector')
    assert root.snapshot() == before and child.snapshot() == draft.candidate
    evidence = decode_json(new.native_evidence_json)
    assert evidence['selected_experience'][0]['id'] == 'cache_epoch'
    assert evidence['context_bytes'] == len(canonical_bytes(evidence['selected_experience']))
    assert evidence['context_tokens'] is None
    assert evidence['candidate']['synthetic_units'] < evidence['incumbent']['synthetic_units']
    assert evidence['model_receipt']['origin'] == 'control'


def test_ties_are_rejected_without_mutating_active_state(tmp_path):
    root = subject(tmp_path, lambda *_: {'source': FOUNDER_SOURCE})
    before = root.snapshot()
    draft = root.propose('same-source', CostEvaluator())
    assert not draft.native_valid
    assert root.snapshot() == before
    evidence = decode_json(draft.native_evidence_json)
    assert evidence['candidate_valid'] and not evidence['changed']


@pytest.mark.parametrize('content', [{'source': 'import os'}, {'source': FOUNDER_SOURCE, 'diagnosis': 'extra'}, {'other': 'none'}])
def test_malformed_program_preserves_receipt_as_rejection(tmp_path, content):
    root = subject(tmp_path, lambda *_: content)
    before = root.snapshot()
    draft = root.propose('invalid', CostEvaluator())
    assert not draft.native_valid and draft.candidate == before
    evidence = decode_json(draft.native_evidence_json)
    assert evidence['proposed_content'] == content and evidence['model_receipt']


def test_q_cannot_gain_context_budget(tmp_path):
    changed = replace(control_selector('harmful'), limit=3).to_dict()
    root = subject(tmp_path, lambda *_: changed)
    draft = root.propose_mechanism('q', CostEvaluator())
    assert not draft.native_valid and draft.candidate == root.snapshot()
    assert 'context budget' in decode_json(draft.native_evidence_json)['proposal_error']


def test_archive_identifiers_and_byte_budget_are_enforced():
    with pytest.raises(ValidationError):
        ExperienceSelector(('missing',)).select(control_memory())
    with pytest.raises(ValidationError):
        replace(control_selector(), max_context_bytes=256).select(control_memory())
    with pytest.raises(ValidationError):
        ExperienceSelector(('repeat', 'repeat'))


def test_public_memory_is_factual_and_does_not_copy_independent_fields():
    trace = {'task_id': 'public-1', 'execution_complete': True,
             'tool_calls': [{'name': 'ask', 'result': {'allowed': False, 'epoch': 4, 'remaining': 8, 'expires': 20}},
                            {'name': 'version', 'result': 4}]}
    memory = build_public_memory({'public_trajectories': [trace],
                                  'independent_label': 'SECRET_LABEL_SENTINEL',
                                  'diagnosis': 'SECRET_DIAGNOSIS_SENTINEL'})
    assert len(memory) == 1
    entry = next(iter(memory.values()))
    assert entry['origin'] == 'public_execution' and entry['evidence_sha256'] == digest(trace)
    facts = decode_json(entry['text'])
    assert facts['ask_denied'] == 1 and facts['public_tool_counts']['version'] == 1
    assert b'SENTINEL' not in canonical_bytes(memory)
    with pytest.raises(ValidationError):
        build_public_memory({'independent_label': 'not public'})


def test_natural_provider_cannot_use_control_archive(tmp_path):
    class Natural:
        origin = CandidateOrigin.NATURAL
    with pytest.raises(ValidationError, match='manually controlled'):
        CostQSystemAdapter(tmp_path / 'natural', Natural(), make_initial_snapshot(701, control_memory()))


def test_source_is_executed_through_task_capability_with_bound_version(tmp_path):
    root = subject(tmp_path)
    class Tools:
        def call(self, name, arguments, operation_id):
            assert name == 'execute_program'
            assert arguments == {'source': FOUNDER_SOURCE}
            assert operation_id.startswith('program/')
            return {'complete': True, 'cost': 13, 'total_requests': 1, 'tool_calls': 2}
    task = PublicTask('task-1', '{}', TOOL_PROTOCOL)
    receipt = root.run_task(task, Tools(), 'execute')
    assert receipt.complete and receipt.version_sha256 == root.snapshot().version.sha256
    assert decode_json(receipt.details_json)['model_evidence'] == []
    with pytest.raises(ValidationError):
        root.run_task(replace(task, tool_protocol='different'), Tools(), 'execute')


def test_public_validity_counts_denials_and_never_uses_actor_call_score():
    row = CostEvaluator().evaluate(make_initial_snapshot(701, control_memory()), 'public')
    assert native_valid(row)
    assert not native_valid({**row, 'feedback': {'complete_correct': False}})
    assert not native_valid({**row, 'synthetic_units': float('nan')})


def test_root_identity_changes_with_seed_even_when_control_archive_equal():
    assert make_initial_snapshot(701, control_memory()).sha256 != make_initial_snapshot(702, control_memory()).sha256


def test_real_public_task_archive_contains_observed_denials_and_calls(tmp_path):
    from rsi_lab.engine.platform import PublicTaskEvaluator
    from rsi_lab.tasks.platform_cost_authorization import CostAuthorizationTaskPackage
    root = subject(tmp_path)
    pack = CostAuthorizationTaskPackage()
    evaluator = PublicTaskEvaluator(root, pack, tmp_path / 'public', seed=701)
    evidence = evaluator.evaluate(root.snapshot(), 'founder')
    assert native_valid(evidence)
    assert evidence['objective']['cost_measure'] == 'synthetic_service_units'
    assert evidence['model_calls'] == 0
    memory = build_public_memory(evidence)
    facts = [decode_json(item['text']) for item in memory.values()]
    assert len(facts) == 12
    assert sum(f['ask_denied'] for f in facts) > 0
    assert sum(f['ask_allowed'] for f in facts) > 0
    assert sum(f['public_tool_counts']['ask'] for f in facts) == evidence['feedback']['total_requests']
    assert all(item['origin'] == 'public_execution' for item in memory.values())
    # Deterministic task execution is free of model calls, but real service
    # improvements still select the candidate through the new task objective.
    proposal = root.propose('real-source-edit', evaluator)
    assert proposal.native_valid
    native = decode_json(proposal.native_evidence_json)
    assert native['candidate']['model_calls'] == native['incumbent']['model_calls'] == 0
    assert native['candidate']['synthetic_units'] < native['incumbent']['synthetic_units']


def test_common_continuation_has_real_descendants_delayed_effects_and_replay(tmp_path):
    from rsi_lab.contracts import CursorFamily, Partition
    from rsi_lab.engine.platform import PublicTaskEvaluator, continue_system
    from rsi_lab.tasks.platform_cost_authorization import CostAuthorizationTaskPackage
    root = subject(tmp_path)
    pack = CostAuthorizationTaskPackage()
    source_env = pack.create_environment(tmp_path / 'source-env').snapshot()
    q = root.propose_mechanism('q', PublicTaskEvaluator(root, pack, tmp_path / 'q-public', seed=701))
    assert q.native_valid
    results = {}
    for arm, snapshot in (('old', root.snapshot()), ('new', q.candidate)):
        directory = tmp_path / arm
        adapter = root.fork(snapshot, directory / 'system')
        options = dict(seed=702, family=CursorFamily.INDEPENDENT_EVALUATION, partition=Partition.DEV,
                       horizon=3, schedule='delayed', root_id='r701', protocol_sha256='f' * 64,
                       arm=arm, stream='independent')
        outcomes, units = continue_system(adapter, pack, source_env, directory, **options)
        results[arm] = [outcome.risk for outcome in outcomes]
        assert all(outcome.complete for outcome in outcomes)
        assert units > 0
        assert adapter.snapshot().version.component('experience_selector') == snapshot.version.component('experience_selector')
        assert adapter.snapshot().version.component('policy') != snapshot.version.component('policy')
        usage = root.provider.summary()
        replay = continue_system(adapter, pack, source_env, directory, **options)
        assert replay == (outcomes, units) and root.provider.summary() == usage
    assert results == {'old': [0, 0, 0], 'new': [0, 0, 1]}
