"""M2 prepare-only studies. No admission or deployment is performed."""
import sqlite3
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from rsi_lab import __version__
from rsi_lab.config import LabConfig
from rsi_lab.contracts import (ArtifactOccurrence, ArtifactRef, AttemptSpec, CandidateOrigin,
    Checkpoint, ComponentRefs, CursorFamily, ForkPoint, PairAnchor, PairingMode, PrepareCursor,
    QProgram, SProgram, SnapshotManifest, StrictRecord, TaskCursor, Version)
from rsi_lab.engine.demo import _publish_json
from rsi_lab.engine.runner import PreparationRunner
from rsi_lab.engine.state import StateStore
from rsi_lab.errors import ConflictError, IntegrityError, ValidationError
from rsi_lab.frozen import fixture_bytes, implementation_fingerprint, provenance
from rsi_lab.infra.artifacts import ArtifactStore, _checked_path
from rsi_lab.infra.journal import Journal
from rsi_lab.infra.snapshots import backup_sqlite
from rsi_lab.interventions.context import ContextKind, ContextPatch, frozen_memory
from rsi_lab.interventions.validation import VerificationPatch
from rsi_lab.preparation import PreparationState, ResumeRequest
from rsi_lab.serialization import canonical_bytes, decode_json, digest
from rsi_lab.subjects.fake import FakeSubject
from rsi_lab.subjects.verification import VerificationRuntime
from rsi_lab.tasks.workflow import Workflow


@dataclass(frozen=True)
class RunManifest(StrictRecord):
    run_id: str
    config: ArtifactRef
    protocol: ArtifactRef
    implementation_sha256: str
    sqlite_version: str
    frozen_sha256: str
    stage: str = 'M2'
    execution_kind: str = 'prepare_only'
    arbitrary_code_execution: bool = False
    hard_isolation_verified: bool = False
    schema_version: int = 1

    def validate(self):
        if self.stage != 'M2' or self.execution_kind != 'prepare_only' or self.arbitrary_code_execution or self.hard_isolation_verified:
            raise ValidationError('Unsupported M2 manifest capabilities')


def protocol(config):
    return {'schema_version': 1, 'config': config, 'frozen': provenance(),
            'implementation': implementation_fingerprint(), 'sqlite': sqlite3.sqlite_version,
            'execution_kind': 'prepare_only', 'candidate_origin': 'control'}


def create_run(config, output, *, stop_after=4):
    from rsi_lab.wiring import preflight
    if type(stop_after) is not int or not 1 <= stop_after <= 4:
        raise ValidationError('stop_after must be between 1 and 4')
    preflight(config)
    output = _checked_path(Path(output))
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ConflictError(f'Output exists; use resume --run {output}') from exc
    store = ArtifactStore(output / 'objects')
    manifest = RunManifest('run_' + uuid.uuid4().hex, store.put_json(config), store.put_json(protocol(config)),
                           implementation_fingerprint(), sqlite3.sqlite_version, digest(provenance()))
    _publish_json(output / 'resolved_config.json', config)
    _publish_json(output / 'run_manifest.json', manifest)
    return resume_run(output, stop_after=stop_after)


def load_run(output):
    from rsi_lab.wiring import preflight
    output = _checked_path(Path(output))
    for name in ('run_manifest.json', 'resolved_config.json', 'objects'):
        if not _checked_path(output / name).exists():
            raise IntegrityError(f'Missing run evidence: {name}')
    manifest = RunManifest.from_dict(decode_json((output / 'run_manifest.json').read_bytes()))
    if (manifest.implementation_sha256 != implementation_fingerprint() or
            manifest.sqlite_version != sqlite3.sqlite_version or manifest.frozen_sha256 != digest(provenance())):
        raise IntegrityError('Implementation, SQLite or frozen protocol changed')
    store = ArtifactStore(output / 'objects')
    config = LabConfig.from_dict(decode_json((output / 'resolved_config.json').read_bytes()))
    preflight(config)
    if store.read_bytes(manifest.config) != canonical_bytes(config) or store.read_bytes(manifest.protocol) != canonical_bytes(protocol(config)):
        raise IntegrityError('Resolved configuration or protocol changed')
    return output, manifest, config, store


def _founder(output, store, state, config):
    Workflow(output / 'founder.sqlite')
    env = backup_sqlite(output / 'founder.sqlite', store)
    deployed = Version.from_dict(decode_json(fixture_bytes('version_D.json')))
    edit = Version.from_dict(decode_json(fixture_bytes('version_E.json')))
    if config.study == 'm2_v_fixed_pool':
        deployed = replace(deployed, s=SProgram.LATEST_VALID)
        edit = replace(edit, s=SProgram.LATEST_VALID)
    components = ComponentRefs(store.put_json(deployed.agent), store.put_json({'registered_id': edit.q.value}),
                               store.put_json({'registered_id': edit.v.value}), store.put_json({'registered_id': edit.s.value}))
    snapshot = SnapshotManifest(components, store.put_json(deployed), store.put_json(edit),
        store.put_json(frozen_memory()), store.put_json({'schema_version': 1, 'entries': []}), env,
        store.put_json(config.random), tuple(TaskCursor(f) for f in CursorFamily), __version__)
    return snapshot, state.save_snapshot(snapshot)


def _source(store, journal, subject, manifest, config, base_ref):
    """Shared source is a prefix, never an arm attempt in a horizon denominator."""
    spec = AttemptSpec('S_001', manifest.run_id, 'm2_pair', 'shared_source', 'm2_root', 'm2_history',
        'shared_preparation', 0, 1, manifest.protocol, base_ref, CandidateOrigin.CONTROL)
    journal.create_budget('pair', 'm2_pair', 5)
    cursor = None
    stages = []
    for index in range(3):
        result = subject.advance(ResumeRequest(spec, cursor, f'S_001/checkpoint/{index}',
            ContextPatch(budget_units=config.context_budget_units), VerificationPatch(), config.guard_mode, None, 'pair', 'm2_pair'))
        if not result.valid:
            raise ValidationError('Shared preparation is invalid')
        cursor = result.cursor
        stages.append(result)
    source = {'schema_version': 1, 'spec': spec, 'checkpoints': tuple(stages),
              'stop_reason': 'pair_anchor_completed', 'purpose': 'shared_preparation',
              'counts_toward_arm_horizon': False}
    # Each stage/response is already durable in Journal; this source index is replayable.
    with sqlite3.connect(journal.path) as connection:
        connection.execute('CREATE TABLE IF NOT EXISTS shared_preparations(source_id TEXT PRIMARY KEY, record_json BLOB NOT NULL)')
        data = canonical_bytes(source)
        row = connection.execute('SELECT record_json FROM shared_preparations WHERE source_id=?', ('S_001',)).fetchone()
        if row and row[0] != data:
            raise ConflictError('Shared source replay mismatch')
        connection.execute('INSERT OR IGNORE INTO shared_preparations VALUES (?,?)', ('S_001', data))
    return cursor.local_state


def resume_run(output, *, stop_after=4):
    if type(stop_after) is not int or not 1 <= stop_after <= 4:
        raise ValidationError('stop_after must be between 1 and 4')
    output, manifest, config, store = load_run(output)
    state = StateStore(output, store)
    journal = Journal(output / 'controller.sqlite')
    subject = FakeSubject(store, journal, VerificationRuntime(store, journal, output / 'validation'))
    runner = PreparationRunner(state, store, subject)
    snapshot, snapshot_ref = _founder(output, store, state, config)
    fixed = None
    shared_cost_ref = None
    if config.study == 'm2_v_fixed_pool':
        base = Version.from_dict(decode_json(store.read_bytes(snapshot.deployed_version)))
        pool = tuple(ArtifactOccurrence(f'v_candidate_0{i}_{alias}',
            store.put_json(replace(base, agent=replace(base.agent, check_scope=safe))),
            'registered_v_pool', CandidateOrigin.CONTROL, 'registered_control')
            for i, (alias, safe) in enumerate((('P', False), ('D', True))))
        fixed = store.put_json(PreparationState(snapshot_ref, ContextPatch(), VerificationPatch(),
                                               candidates=pool, native_plan=('ok',)))
        arms = [(name, None, ContextKind.IDENTITY, name.lower()) for name in ('Old','New','Restore','Placebo')]
    elif config.study == 'm2_shared_candidate':
        fixed = _source(store, journal, subject, manifest, config, snapshot_ref)
        shared_cost_ref = 'S_001/generate'
        arms = [(name, None, ContextKind.IDENTITY, 'identity') for name in ('L','R')]
    else:
        arms = [('Old', QProgram.OLD, ContextKind.IDENTITY, 'identity'),
                ('New', QProgram.NEW, ContextKind.IDENTITY, 'identity'),
                ('Restore', QProgram.NEW, ContextKind.RESTORE, 'identity'),
                ('Placebo', QProgram.NEW, ContextKind.PLACEBO, 'identity'),
                ('Remove', QProgram.OLD, ContextKind.REMOVE, 'identity')]
    if fixed:
        # Fork snapshot includes source prefix and candidate artifacts, before validation.
        snapshot = replace(snapshot, archive=store.put_json({'schema_version': 1, 'entries': [], 'fixed_preparation': fixed}))
        snapshot_ref = state.save_snapshot(snapshot)
    anchor = PairAnchor('m2_pair','m2_root','m2_history',snapshot_ref,
        store.put_json({'schema_version':1,'validation_tests':['ok','scope','missing','spent']}),
        snapshot.random_plan,snapshot.cursors,config.pairing.mode,config.pairing.fork_at,0)
    state.register_pair(anchor)
    for arm, assigned_q, context_kind, verification_kind in arms:
        state.create_branch(arm,anchor.pair_id)
        journal.create_budget('branch',arm,config.resources.synthetic_units_per_branch)
        spec = AttemptSpec(f'{arm}_001',manifest.run_id,anchor.pair_id,arm,anchor.root_id,
            anchor.history_group_id,arm,0,1,manifest.protocol,snapshot_ref,CandidateOrigin.CONTROL)
        runner.prepare(spec,ContextPatch(context_kind,config.context_budget_units),VerificationPatch(verification_kind),
            stop_after=stop_after,fixed_preparation=fixed,guard_mode=config.guard_mode,assigned_q=assigned_q)
    return inspect_run(output)


def inspect_run(output):
    output, manifest, config, store = load_run(output)
    path = _checked_path(output / 'controller.sqlite')
    for suffix in ('-wal','-shm','-journal'):
        _checked_path(Path(str(path)+suffix))
    connection = sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    connection.row_factory=sqlite3.Row
    try:
        connection.execute('BEGIN')
        attempts=[]
        for row in connection.execute('SELECT * FROM attempts ORDER BY attempt_id'):
            spec = AttemptSpec.from_dict(decode_json(row['spec_json']))
            base = SnapshotManifest.from_dict(decode_json(store.read_bytes(spec.source_snapshot)))
            prepared=None
            if row['cursor_json']:
                cursor=PrepareCursor.from_dict(decode_json(row['cursor_json']))
                prepared=PreparationState.from_dict(decode_json(store.read_bytes(cursor.local_state)))
            attempts.append({'attempt_id':spec.attempt_id,'phase':row['phase'],'status':row['status'],
                'cursor':decode_json(row['cursor_json']) if row['cursor_json'] else None,
                'preparation':prepared,'deployed_version':base.deployed_version,
                'next_edit_base':base.edit_base_version,'deployed_risk':None,
                'risk_missing_reason':'deployment_evaluation_not_requested'})
        costs=[dict(r) for r in connection.execute('SELECT * FROM cost_events ORDER BY operation_id')]
        reserved=sum(r[0] for r in connection.execute("SELECT reserved_units FROM operations WHERE status IN ('reserved','outcome_unknown')"))
        branches=[dict(r) for r in connection.execute('SELECT branch_id,head_revision FROM branches ORDER BY branch_id')]
    finally:
        connection.close()
    return {'stage':'M2','execution_kind':'prepare_only','run_id':manifest.run_id,'study':config.study,
        'attempts':attempts,'branches':branches,'shared_cost_ref':'S_001/generate' if config.study=='m2_shared_candidate' else None,
        'accounting':{'cost_events':costs,'total_synthetic_units':sum(r['synthetic_cost_units'] for r in costs),
            'pair_shared_units':sum(r['synthetic_cost_units'] for r in costs if r['owner_kind']=='pair'),
            'outstanding_reserved_units':reserved},'admission_performed':False,'deployment_performed':False}
