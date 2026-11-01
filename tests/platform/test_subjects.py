from dataclasses import replace

from rsi_lab.api import ActivationCommand, UpdateIdentity
from rsi_lab.contracts import CandidateOrigin, Partition
from rsi_lab.infra.chat_models import ScriptedChatProvider
from rsi_lab.serialization import digest
from rsi_lab.subjects.bounded_task_a import BoundedTaskASystem
from rsi_lab.subjects.platform_fixture import FixtureSystem
from rsi_lab.studies.task_a import _control_response
from rsi_lab.tasks.platform_authorization import AuthorizationTaskPackage


class Evaluator:
    def __init__(self, subject, directory):
        self.subject, self.directory = subject, directory

    def evaluate(self, candidate, operation_id):
        directory = self.directory / digest(operation_id)
        fork = self.subject.fork(candidate, directory / 'subject')
        pack = AuthorizationTaskPackage()
        env = pack.create_environment(directory / 'env')
        plan = pack.plan(91, horizon=1, schedule='benign')
        round_plan = plan.rounds[0]
        env.prepare(round_plan)
        for task in env.public_tasks(round_plan):
            fork.run_task(task, env.tools(task.task_id, 'validation'), 'run')
        result = env.evaluate(round_plan)
        return {'complete': result.complete, 'risk': result.risk,
                'legal_completed': result.legal_completed, 'legal_total': result.legal_denominator,
                'synthetic_units': env.synthetic_cost(round_plan)}


def activate(subject, draft, operation_id):
    identity = UpdateIdentity(operation_id, 'root', 'history', 'a' * 64,
        draft.base_sha256, draft.candidate.sha256, 'b' * 64, Partition.DEV, draft.origin, 6, 'd' * 64)
    return subject.activate(ActivationCommand(operation_id, identity, 'c' * 64, draft.candidate))


def test_fixture_proposal_does_not_activate_and_execution_uses_actual_version(tmp_path):
    subject = FixtureSystem(tmp_path / 'subject')
    base = subject.snapshot()
    evaluator = Evaluator(subject, tmp_path / 'validation')
    draft = subject.propose('proposal', evaluator)
    assert draft.native_valid
    assert subject.snapshot() == base
    receipt = activate(subject, draft, 'activation')
    assert receipt.after_sha256 == draft.candidate.sha256
    assert subject.snapshot() == draft.candidate
    assert subject.activation_status('activation') == receipt
    assert activate(subject, draft, 'activation') == receipt
    pack = AuthorizationTaskPackage()
    env = pack.create_environment(tmp_path / 'environment')
    plan = pack.plan(93, horizon=3)
    results = []
    for round_plan in plan.rounds:
        env.prepare(round_plan)
        for task in env.public_tasks(round_plan):
            execution = subject.run_task(task, env.tools(task.task_id, 'main'), 'execute/' + task.task_id)
            assert execution.version_sha256 == draft.candidate.version.sha256
        results.append(env.evaluate(round_plan))
    assert [r.risk for r in results] == [0, 0, 1]


def test_bounded_q_controls_editor_context_and_forks_do_not_pollute_parent(tmp_path):
    provider = ScriptedChatProvider(tmp_path / 'provider.sqlite', _control_response)
    subject = BoundedTaskASystem(tmp_path / 'subject', provider)
    base = subject.snapshot()
    evaluator = Evaluator(subject, tmp_path / 'validation')
    draft = subject.propose_mechanism('q_proposal', evaluator)
    assert draft.native_valid and subject.snapshot() == base
    assert draft.candidate.version.component('policy') == base.version.component('policy')
    assert draft.candidate.version.component('experience_selector')['order'][0] == 'e2'
    child = subject.fork(draft.candidate, tmp_path / 'child')
    child_proposal = child.propose('descendant', Evaluator(child, tmp_path / 'child_validation'))
    assert child_proposal.candidate.version.component('policy')['strategy'] == 'reuse_confirmation'
    assert subject.snapshot() == base
    assert child.snapshot() == draft.candidate
    assert child_proposal.origin == CandidateOrigin.CONTROL
    old = subject.propose('old_descendant', evaluator)
    assert old.candidate.version.component('policy')['strategy'] == 'strict'


def test_generic_native_continuation_keeps_old_new_branch_and_resume(tmp_path):
    from rsi_lab.contracts import CursorFamily
    from rsi_lab.engine.platform import continue_system, PublicTaskEvaluator
    pack = AuthorizationTaskPackage()
    subject = FixtureSystem(tmp_path / 'subject')
    source_env = pack.create_environment(tmp_path / 'source').snapshot()
    base = subject.snapshot()
    draft = subject.propose('root', PublicTaskEvaluator(subject, pack, tmp_path / 'native', seed=9))
    results = {}
    for arm, snapshot in [('old', base), ('new', draft.candidate)]:
        directory = tmp_path / arm
        actor = subject.fork(snapshot, directory / 'system')
        kwargs = dict(seed=11, family=CursorFamily.INDEPENDENT_EVALUATION, partition=Partition.DEV,
                      horizon=3, schedule='delayed', root_id='root', protocol_sha256='d' * 64,
                      arm=arm, stream='independent')
        rows, cost = continue_system(actor, pack, source_env, directory, **kwargs)
        results[arm] = [r.risk for r in rows]
        assert all(r.complete for r in rows)
        again, again_cost = continue_system(actor, pack, source_env, directory, **kwargs)
        assert rows == again and cost == again_cost
    assert results == {'old': [0, 0, 0], 'new': [0, 0, 1]}
    assert subject.snapshot() == base
