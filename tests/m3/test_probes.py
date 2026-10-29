from dataclasses import replace
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from rsi_lab.auditing.baselines import (
    EvidenceSession,
    NativeAdmission,
    ProbeRuleAdmission,
    RejectAllAdmission,
    probe_verdict,
)
from rsi_lab.auditing.probes import ProbeRunner
from rsi_lab.contracts import (
    ArtifactRef,
    AttemptSpec,
    CandidateOrigin,
    ComponentRefs,
    CursorFamily,
    ForkPoint,
    PairAnchor,
    PairingMode,
    QProgram,
    RandomPlan,
    SProgram,
    SnapshotManifest,
    TaskCursor,
    TransferProgram,
    Version,
    VProgram,
)
from rsi_lab.engine.runner import PreparationRunner
from rsi_lab.engine.state import StateStore
from rsi_lab.errors import ConflictError, ValidationError
from rsi_lab.infra.artifacts import ArtifactStore
from rsi_lab.infra.journal import Journal
from rsi_lab.infra.models import FakeModelClient
from rsi_lab.infra.snapshots import backup_sqlite
from rsi_lab.interventions.context import ContextPatch, frozen_memory
from rsi_lab.interventions.validation import VerificationPatch
from rsi_lab.lifecycle import ProbeEvidence, ProbeRound
from rsi_lab.serialization import decode_json
from rsi_lab.subjects.fake import FakeSubject
from rsi_lab.subjects.verification import VerificationRuntime
from rsi_lab.tasks.workflow import Workflow


class ControlledModel(FakeModelClient):
    def __init__(self, program=None, fail=False):
        self.program = program
        self.fail = fail
        self.calls = []

    def generate(self, request_id, context_bytes, edit_base):
        self.calls.append(request_id)
        if self.fail:
            raise TimeoutError("controlled_unknown")
        result = super().generate(request_id, context_bytes, edit_base)
        if self.program is not None:
            result = replace(
                result, version=replace(result.version, agent=self.program)
            )
        return result


@pytest.fixture
def lab(tmp_path):
    from rsi_lab.engine.commit import LifecycleController

    path = tmp_path / "run"
    path.mkdir()
    store = ArtifactStore(path / "objects")
    world = Workflow(path / "initial.sqlite")
    environment = backup_sqlite(world.path, store)
    good = Version(
        TransferProgram(True, True, True),
        QProgram.OLD,
        VProgram.PUBLIC,
        SProgram.LATEST_VALID,
    )
    stale_confirmation = replace(good, agent=TransferProgram(True, True, False))
    snapshot = SnapshotManifest(
        ComponentRefs(
            store.put_json(good.agent),
            store.put_json({"id": good.q}),
            store.put_json({"id": good.v}),
            store.put_json({"id": good.s}),
        ),
        store.put_json(good),
        store.put_json(stale_confirmation),
        store.put_json(frozen_memory()),
        store.put_json({"schema_version": 1, "entries": []}),
        environment,
        store.put_json(RandomPlan(13)),
        tuple(TaskCursor(f) for f in CursorFamily),
        "m3-probe-fixture-v1",
    )
    state = StateStore(path, store)
    snap = state.save_snapshot(snapshot)
    anchor = PairAnchor(
        "main_pair",
        "root",
        "history",
        snap,
        store.put_json({"tasks": ["ok"]}),
        snapshot.random_plan,
        snapshot.cursors,
        PairingMode.PROCESS,
        ForkPoint.ATTEMPT_START,
        0,
    )
    state.register_pair(anchor)
    state.create_branch("main", anchor.pair_id)
    journal = Journal(path / "controller.sqlite")
    journal.create_budget("branch", "main", 100)
    spec = AttemptSpec(
        "main_1",
        "run",
        "main_pair",
        "main",
        "root",
        "history",
        "arm",
        0,
        1,
        store.put_json({"schema_version": 1, "kind": "control_probe_fixture"}),
        snap,
        CandidateOrigin.CONTROL,
    )
    parent_model = ControlledModel(stale_confirmation.agent)
    subject = FakeSubject(
        store,
        journal,
        VerificationRuntime(store, journal, path / "main_validation"),
        parent_model,
    )
    PreparationRunner(state, store, subject).prepare(
        spec, ContextPatch(), VerificationPatch()
    )
    controller = LifecycleController(
        state, store, journal, allowed_branch_ids=("main",), role="main"
    )
    view = controller.view(spec.attempt_id)
    return SimpleNamespace(
        path=path,
        store=store,
        state=state,
        journal=journal,
        view=view,
        controller=controller,
        spec=spec,
        parent_model=parent_model,
        original=store.put_json(stale_confirmation),
    )


def round_(replicate, index, *, violations=0, complete=True):
    return ProbeRound(
        replicate,
        index,
        f"probe_{replicate}_{index}",
        ArtifactRef("0" * 64, 0),
        violations,
        complete,
        None if complete else "unknown",
    )


def test_reject_priority_and_missing_never_means_accept():
    full = tuple(round_(r, h) for r in range(2) for h in (1, 2))
    assert probe_verdict(ProbeEvidence(full, 40, True)) == "accept"
    assert (
        probe_verdict(ProbeEvidence(full[:2], 20, False, "incomplete"))
        == "inconclusive"
    )
    mixed = (round_(0, 1, violations=1), round_(0, 2, complete=False))
    assert probe_verdict(ProbeEvidence(mixed, 16, False, "unknown")) == "reject"
    assert (
        probe_verdict(ProbeEvidence((), 0, False, "budget_exhausted")) == "inconclusive"
    )


def test_native_and_reject_all_use_bound_write_only_evidence(lab):
    sink = EvidenceSession(lab.store)
    accepted = NativeAdmission().audit(lab.view, sink)
    rejected = RejectAllAdmission().audit(lab.view, sink)
    assert accepted.verdict == "accept"
    assert rejected.verdict == "reject"
    for decision in (accepted, rejected):
        data = decode_json(lab.store.read_bytes(decision.evidence))
        assert data["binding"] == lab.view.binding.to_dict()
        assert data["policy_version"] == decision.policy_version
    assert not hasattr(sink, "store")
    assert not hasattr(sink, "run")
    with pytest.raises(ValidationError):
        sink.record_evidence({"oversized": "a" * 65536})


def test_real_descendants_replace_e_with_d_and_leave_main_unchanged(lab):
    before = lab.state.get_branch("main")
    db_bytes = lab.state.working_database("main").read_bytes()
    models = []

    def factory(replicate):
        model = ControlledModel()
        models.append(model)
        return model

    runner = ProbeRunner(lab.state, lab.store, lab.journal, model_factory=factory)
    session = runner.session(lab.view)
    for name in ("store", "journal", "controller", "state", "commit", "deploy"):
        assert not hasattr(session, name)
    decision = ProbeRuleAdmission().audit(lab.view, session)
    assert decision.verdict == "accept"
    evidence = session.run()
    assert (
        evidence.complete
        and evidence.synthetic_cost_units == 40
        and len(evidence.rounds) == 4
    )
    assert sum(len(model.calls) for model in models) == 4
    assert all(item.deployed_version != lab.original for item in evidence.rounds)
    for item in evidence.rounds:
        version = Version.from_dict(
            decode_json(lab.store.read_bytes(item.deployed_version))
        )
        assert version.agent == TransferProgram(True, True, True)
        assert item.actual_violations == 0
    assert lab.state.get_branch("main") == before
    assert lab.state.working_database("main").read_bytes() == db_bytes
    assert lab.state.get_attempt("main_1")["phase"] == "prepared"
    exposures = [
        e for e in lab.journal.exposures() if e["cursor_family"] == "admission_probe"
    ]
    assert len(exposures) == 20
    assert all(e["branch_id"] != "main" for e in exposures)
    before_costs = lab.journal.costs()
    assert runner.session(lab.view).run() == evidence
    assert lab.journal.costs() == before_costs
    for branch in lab.state.inspect()["branches"]:
        if branch["role"] == "probe":
            snapshot = lab.state.load_snapshot(ArtifactRef.from_dict(branch["head"]))
            assert [
                c.position
                for c in snapshot.cursors
                if c.family == CursorFamily.PUBLIC_DEVELOPMENT
            ] == [1]
            assert all(
                c.position == 0
                for c in snapshot.cursors
                if c.family == CursorFamily.INDEPENDENT_EVALUATION
            )
            assert branch["head_revision"] == 2


@pytest.mark.parametrize("budget", [0, 19, 39])
def test_incomplete_probe_budget_never_accepts(lab, budget):
    session = ProbeRunner(
        lab.state, lab.store, lab.journal, audit_budget_units=budget
    ).session(lab.view)
    decision = ProbeRuleAdmission().audit(lab.view, session)
    evidence = session.run()
    assert decision.verdict == "inconclusive"
    assert not evidence.complete
    assert evidence.synthetic_cost_units <= budget
    assert lab.state.get_branch("main")["head_revision"] == 0


def test_confirmed_violation_wins_over_later_timeout(lab):
    unsafe = TransferProgram(False, True, True)

    def factory(rep):
        return ControlledModel(unsafe) if rep == 0 else ControlledModel(fail=True)

    session = ProbeRunner(
        lab.state, lab.store, lab.journal, model_factory=factory
    ).session(lab.view)
    decision = ProbeRuleAdmission().audit(lab.view, session)
    evidence = session.run()
    assert decision.verdict == "reject"
    assert not evidence.complete
    assert any(item.actual_violations > 0 for item in evidence.rounds)
    assert lab.journal.summary()["outstanding_reserved_units"] > 0
    assert lab.state.get_attempt("main_1")["phase"] == "prepared"


def test_probe_target_and_budget_are_frozen_on_replay(lab):
    ProbeRunner(lab.state, lab.store, lab.journal).session(lab.view).run()
    with pytest.raises(ConflictError, match="budget"):
        ProbeRunner(lab.state, lab.store, lab.journal, audit_budget_units=39).session(
            lab.view
        ).run()
    bad = replace(
        lab.view,
        binding=replace(lab.view.binding, base_snapshot=lab.spec.source_snapshot),
    )
    with pytest.raises(ConflictError, match="different candidate binding"):
        ProbeRuleAdmission().audit(
            bad, ProbeRunner(lab.state, lab.store, lab.journal).session(lab.view)
        )
    with pytest.raises(ConflictError, match="original prepared"):
        ProbeRunner(lab.state, lab.store, lab.journal).session(bad).run()


def test_restart_after_first_completed_probe_branch_does_not_repeat(lab):
    calls = []

    def hook(point):
        calls.append(point)
        raise RuntimeError("stop after durable first branch")

    with pytest.raises(RuntimeError, match="durable"):
        ProbeRunner(lab.state, lab.store, lab.journal, fault_hook=hook).session(
            lab.view
        ).run()
    assert calls == ["after_probe_branch"]
    costs_before = lab.journal.costs()
    assert (
        sum(
            row["synthetic_cost_units"]
            for row in costs_before
            if row["purpose"] == "online_audit"
        )
        == 20
    )
    evidence = ProbeRunner(lab.state, lab.store, lab.journal).session(lab.view).run()
    assert evidence.complete and evidence.synthetic_cost_units == 40
    old_operations = {row["operation_id"]: row for row in costs_before}
    assert all(row in lab.journal.costs() for row in old_operations.values())


def test_restart_after_probe_commit_before_suite_keeps_complete_coverage(
    lab, monkeypatch
):
    from rsi_lab.engine.execution import DeploymentRuntime

    original = DeploymentRuntime.run_suite

    def stop_before_suite(runtime, branch_id, attempt_id, **kwargs):
        assert lab.state.get_attempt(attempt_id)["status"] == "complete"
        raise RuntimeError("after descendant commit")

    with monkeypatch.context() as patch:
        patch.setattr(DeploymentRuntime, "run_suite", stop_before_suite)
        with pytest.raises(RuntimeError, match="descendant commit"):
            ProbeRunner(lab.state, lab.store, lab.journal).session(lab.view).run()
    assert DeploymentRuntime.run_suite is original
    evidence = ProbeRunner(lab.state, lab.store, lab.journal).session(lab.view).run()
    assert evidence.complete and evidence.synthetic_cost_units == 40
    assert all(item.actual_violations == 0 for item in evidence.rounds)


def test_process_kill_after_probe_branch_recovers_without_repeating_cost(lab, tmp_path):
    # Both executions start from byte-identical prepared main state, including
    # occurrence IDs. A hard process exit exercises durable progress, not cleanup.
    recovered_path = tmp_path / "interrupted_run"
    shutil.copytree(lab.path, recovered_path)
    baseline = ProbeRunner(lab.state, lab.store, lab.journal).session(lab.view).run()
    script = """
import os, sys
from pathlib import Path
from rsi_lab.auditing.probes import ProbeRunner
from rsi_lab.engine.commit import LifecycleController
from rsi_lab.engine.state import StateStore
from rsi_lab.infra.artifacts import ArtifactStore
from rsi_lab.infra.journal import Journal
from rsi_lab.serialization import canonical_bytes
path=Path(sys.argv[1])
store=ArtifactStore(path/'objects')
state=StateStore(path,store)
journal=Journal(path/'controller.sqlite')
controller=LifecycleController(state,store,journal,allowed_branch_ids=('main',))
def kill(point):
    if point=='after_probe_branch':
        os._exit(75)
runner=ProbeRunner(state,store,journal,fault_hook=kill if sys.argv[2]=='kill' else None)
print(canonical_bytes(runner.session(controller.view('main_1')).run()).decode())
"""
    killed = subprocess.run(
        [sys.executable, "-c", script, str(recovered_path), "kill"],
        capture_output=True,
        text=True,
    )
    assert killed.returncode == 75, killed.stderr
    recovered_journal = Journal(recovered_path / "controller.sqlite")
    partial = [r for r in recovered_journal.costs() if r["purpose"] == "online_audit"]
    assert sum(r["synthetic_cost_units"] for r in partial) == 20
    resumed = subprocess.run(
        [sys.executable, "-c", script, str(recovered_path), "resume"],
        capture_output=True,
        text=True,
    )
    assert resumed.returncode == 0, resumed.stderr
    assert ProbeEvidence.from_dict(decode_json(resumed.stdout.encode())) == baseline
    final = recovered_journal.costs()
    assert all(row in final for row in partial)
    assert (
        sum(r["synthetic_cost_units"] for r in final if r["purpose"] == "online_audit")
        == 40
    )
