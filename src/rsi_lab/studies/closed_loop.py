"""Registered M3 closed-loop fixture, using one lifecycle for main and probe arms."""

import sqlite3
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from rsi_lab.auditing.baselines import (
    EvidenceSession,
    NativeAdmission,
    RejectAllAdmission,
    ProbeRuleAdmission,
)
from rsi_lab.auditing.probes import ProbeRunner
from rsi_lab.config import LabConfig
from rsi_lab.contracts import (
    ArtifactRef,
    AttemptSpec,
    CandidateOrigin,
    ComponentRefs,
    ForkPoint,
    PairAnchor,
    PairingMode,
    QProgram,
    SProgram,
    StrictRecord,
    Version,
)
from rsi_lab.engine.commit import LifecycleController
from rsi_lab.engine.demo import _publish_json
from rsi_lab.engine.execution import DeploymentRuntime
from rsi_lab.engine.runner import PreparationRunner
from rsi_lab.engine.state import StateStore
from rsi_lab.errors import ConflictError, IntegrityError, ValidationError
from rsi_lab.frozen import implementation_fingerprint, provenance
from rsi_lab.infra.artifacts import ArtifactStore, _checked_path
from rsi_lab.infra.journal import Journal
from rsi_lab.infra.models import DurableFakeModelClient
from rsi_lab.interventions.context import ContextPatch
from rsi_lab.interventions.validation import VerificationPatch
from rsi_lab.serialization import canonical_bytes, decode_json
from rsi_lab.studies.preparation import _founder
from rsi_lab.subjects.fake import FakeSubject
from rsi_lab.subjects.verification import VerificationRuntime


@dataclass(frozen=True)
class LoopManifest(StrictRecord):
    run_id: str
    study: str
    protocol: ArtifactRef
    implementation_sha256: str
    sqlite_version: str
    stage: str = "M3"
    execution_kind: str = "closed_loop_fixture"
    arbitrary_code_execution: bool = False
    hard_isolation_verified: bool = False
    schema_version: int = 1

    def validate(self):
        if (
            self.stage != "M3"
            or self.study not in ("m3_smoke", "m3_probe_admission")
            or self.execution_kind != "closed_loop_fixture"
            or self.arbitrary_code_execution
            or self.hard_isolation_verified
        ):
            raise ValidationError("Unsupported closed loop fixture")


def _protocol(config: LabConfig) -> dict:
    return {
        "schema_version": 1,
        "config": config,
        "frozen": provenance(),
        "implementation": implementation_fingerprint(),
        "sqlite": sqlite3.sqlite_version,
    }


def create_run(config: LabConfig, output: Path, *, fault_hook=None) -> dict:
    from rsi_lab.wiring import preflight

    preflight(config)
    output = _checked_path(Path(output))
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ConflictError("Output exists; use resume") from exc
    store = ArtifactStore(output / "objects")
    manifest = LoopManifest(
        "run_" + uuid.uuid4().hex,
        config.study,
        store.put_json(_protocol(config)),
        implementation_fingerprint(),
        sqlite3.sqlite_version,
    )
    _publish_json(output / "resolved_config.json", config)
    _publish_json(output / "run_manifest.json", manifest)
    return resume_run(output, fault_hook=fault_hook)


def _load(output: Path):
    from rsi_lab.wiring import preflight

    output = _checked_path(Path(output))
    for name in ("objects", "run_manifest.json", "resolved_config.json"):
        if not _checked_path(output / name).exists():
            raise IntegrityError(f"Missing run evidence: {name}")
    manifest = LoopManifest.from_dict(
        decode_json((output / "run_manifest.json").read_bytes())
    )
    config = LabConfig.from_dict(
        decode_json((output / "resolved_config.json").read_bytes())
    )
    preflight(config)
    store = ArtifactStore(output / "objects")
    if (
        manifest.study != config.study
        or manifest.implementation_sha256 != implementation_fingerprint()
        or manifest.sqlite_version != sqlite3.sqlite_version
        or store.read_bytes(manifest.protocol) != canonical_bytes(_protocol(config))
    ):
        raise IntegrityError("Closed loop config, implementation or protocol changed")
    return output, manifest, config, store


def resume_run(output: Path, *, fault_hook=None) -> dict:
    output, manifest, config, store = _load(output)
    state = StateStore(output, store)
    journal = Journal(output / "controller.sqlite")
    snapshot, _ = _founder(output, store, state, config)
    deployed = Version.from_dict(
        decode_json(store.read_bytes(snapshot.deployed_version))
    )
    edit = Version.from_dict(decode_json(store.read_bytes(snapshot.edit_base_version)))
    deployed = replace(deployed, q=QProgram.OLD, s=SProgram.LATEST_VALID)
    edit = replace(edit, q=QProgram.OLD, s=SProgram.LATEST_VALID)
    snapshot = replace(
        snapshot,
        deployed_version=store.put_json(deployed),
        edit_base_version=store.put_json(edit),
        components=ComponentRefs(
            store.put_json(deployed.agent),
            store.put_json({"registered_id": edit.q.value}),
            store.put_json({"registered_id": edit.v.value}),
            store.put_json({"registered_id": edit.s.value}),
        ),
    )
    initial = state.save_snapshot(snapshot)
    anchor = PairAnchor(
        "loop_pair",
        "loop_root",
        "loop_history",
        initial,
        store.put_json(
            {
                "schema_version": 1,
                "horizon_attempts": config.horizon_attempts,
                "deployment_suite": ["ok", "scope", "missing", "spent"],
            }
        ),
        snapshot.random_plan,
        snapshot.cursors,
        PairingMode.PROCESS,
        ForkPoint.ATTEMPT_START,
        0,
    )
    state.register_pair(anchor)
    state.create_branch("main", anchor.pair_id)
    journal.create_budget("branch", "main", config.resources.synthetic_units_per_branch)
    controller = LifecycleController(
        state, store, journal, ("main",), fault_hook=fault_hook
    )
    deployment = DeploymentRuntime(
        state, store, journal, controller, fault_hook=fault_hook
    )
    model = DurableFakeModelClient(output / "provider.sqlite", fault_hook=fault_hook)
    subject = FakeSubject(
        store,
        journal,
        VerificationRuntime(
            store, journal, output / "validation", fault_hook=fault_hook
        ),
        model,
    )
    runner = PreparationRunner(state, store, subject)
    probes = ProbeRunner(
        state,
        store,
        journal,
        audit_budget_units=config.audit_budget_units,
        model_factory=lambda replicate: DurableFakeModelClient(
            output / f"probe_provider_{replicate}.sqlite", fault_hook=fault_hook
        ),
        fault_hook=fault_hook,
    )
    policy = {
        "native": NativeAdmission,
        "reject_all": RejectAllAdmission,
        "probe": ProbeRuleAdmission,
    }[config.admission_policy]()
    with state._db(True) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS loop_rounds(attempt_index INTEGER PRIMARY KEY,spec_json BLOB,result_json BLOB)"
        )
    for index in range(1, config.horizon_attempts + 1):
        with state._db() as db:
            old = db.execute(
                "SELECT * FROM loop_rounds WHERE attempt_index=?", (index,)
            ).fetchone()
        if old and old["result_json"]:
            continue
        if old:
            spec = AttemptSpec.from_dict(decode_json(old["spec_json"]))
        else:
            if deployment.is_paused("main"):
                missing = {
                    "attempt_index": index,
                    "status": "not_run",
                    "reason": "unsafe_current_pause",
                    "public_evaluation": None,
                }
                with state._db(True) as db:
                    db.execute(
                        "INSERT INTO loop_rounds VALUES (?,NULL,?)",
                        (index, canonical_bytes(missing)),
                    )
                continue
            spec = AttemptSpec(
                f"main_{index:03}",
                manifest.run_id,
                anchor.pair_id,
                "main",
                anchor.root_id,
                anchor.history_group_id,
                "main",
                0,
                index,
                manifest.protocol,
                ArtifactRef.from_dict(state.get_branch("main")["head"]),
                CandidateOrigin.CONTROL,
            )
            with state._db(True) as db:
                db.execute(
                    "INSERT INTO loop_rounds VALUES (?,?,NULL)",
                    (index, canonical_bytes(spec)),
                )
        attempt = runner.prepare(
            spec,
            ContextPatch(budget_units=config.context_budget_units),
            VerificationPatch(),
            guard_mode=config.guard_mode,
        )
        if attempt["phase"] not in (
            "prepared",
            "audited",
            "resolved",
            "materialized",
            "committed",
        ) or attempt["status"] not in ("active", "complete"):
            controller.record_failed_preparation(spec.attempt_id)
            result = {
                "attempt_index": index,
                "attempt_id": spec.attempt_id,
                "status": attempt["status"],
                "reason": attempt["status_reason"],
                "public_evaluation": None,
            }
        else:
            view = controller.view(spec.attempt_id)
            decision = controller.get_decision(spec.attempt_id)
            if view is None:
                receipt = controller.complete_no_candidate(
                    spec.attempt_id, memory_mode=config.memory_mode
                )
            else:
                if decision is None:
                    session = (
                        probes.session(view)
                        if config.admission_policy == "probe"
                        else EvidenceSession(store)
                    )
                    decision = policy.audit(view, session)
                receipt = controller.complete(
                    spec.attempt_id,
                    decision,
                    memory_mode=(
                        "retain" if decision.verdict == "accept" else config.memory_mode
                    ),
                )
            outcome = deployment.run_suite(
                "main", spec.attempt_id, guard_mode=config.guard_mode
            )
            result = {
                "attempt_index": index,
                "attempt_id": spec.attempt_id,
                "status": "committed",
                "decision": decision,
                "commit": receipt,
                "public_evaluation": outcome,
            }
        with state._db(True) as db:
            db.execute(
                "UPDATE loop_rounds SET result_json=? WHERE attempt_index=?",
                (canonical_bytes(result), index),
            )
    return inspect_run(output)


def inspect_run(output: Path) -> dict:
    output, manifest, config, store = _load(output)
    path = _checked_path(output / "controller.sqlite")
    for suffix in ("-wal", "-shm", "-journal"):
        _checked_path(Path(str(path) + suffix))
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        db.execute("BEGIN")
        rounds = [
            decode_json(r[0]) if r[0] else None
            for r in db.execute(
                "SELECT result_json FROM loop_rounds ORDER BY attempt_index"
            )
        ]
        costs = [
            {
                "operation_id": r[0],
                "owner_kind": r[1],
                "owner_id": r[2],
                "purpose": r[3],
                "synthetic_cost_units": r[4],
            }
            for r in db.execute(
                "SELECT operation_id,owner_kind,owner_id,purpose,synthetic_cost_units FROM cost_events ORDER BY operation_id"
            )
        ]
        branches = [
            {
                "branch_id": r[0],
                "role": r[1],
                "head_revision": r[2],
                "head": decode_json(r[3]),
            }
            for r in db.execute(
                "SELECT branch_id,role,head_revision,head_json FROM branches ORDER BY branch_id"
            )
        ]
        paused = db.execute("SELECT COUNT(*) FROM branch_pauses").fetchone()[0] > 0
        outstanding = sum(
            r[0]
            for r in db.execute(
                "SELECT reserved_units FROM operations WHERE status IN ('reserved','outcome_unknown')"
            )
        )
    return {
        "stage": "M3",
        "study": config.study,
        "run_id": manifest.run_id,
        "candidate_origin": "control",
        "horizon_attempts": config.horizon_attempts,
        "rounds": rounds,
        "branches": branches,
        "paused": paused,
        "accounting": {
            "cost_events": costs,
            "total_synthetic_units": sum(c["synthetic_cost_units"] for c in costs),
            "outstanding_reserved_units": outstanding,
        },
        "independent_evaluation": None,
        "evaluation_missing_reason": "independent_evaluation_not_requested",
        "hard_isolation_verified": False,
        "real_llm_used": False,
    }
