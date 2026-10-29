"""Registered S0 studies assembled from the shared preparation/commit lifecycle.

The study layer owns preregistered slots, interventions and lineage identities.
It does not score its own candidates or bypass controller authorization.
"""

from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
import sqlite3
import uuid

from rsi_lab.auditing.baselines import (
    EvidenceSession,
    NativeAdmission,
    RejectAllAdmission,
    ProbeRuleAdmission,
)
from rsi_lab.auditing.probes import ProbeRunner
from rsi_lab.config import LabConfig
from rsi_lab.contracts import (
    ArtifactOccurrence,
    ArtifactRef,
    AttemptSpec,
    CandidateOrigin,
    ComponentRefs,
    PairAnchor,
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
from rsi_lab.interventions.context import ContextKind, ContextPatch
from rsi_lab.interventions.validation import VerificationPatch
from rsi_lab.preparation import PreparationState
from rsi_lab.serialization import canonical_bytes, decode_json, digest
from rsi_lab.studies.preparation import _founder
from rsi_lab.subjects.fake import FakeSubject
from rsi_lab.subjects.verification import VerificationRuntime

STUDIES = ("s0_smoke", "s0_q_pairs", "s0_v_fixed_pool", "s0_probe_admission")


@dataclass(frozen=True)
class S0Manifest(StrictRecord):
    run_id: str
    study: str
    config: ArtifactRef
    protocol: ArtifactRef
    implementation_sha256: str
    sqlite_version: str
    frozen_sha256: str
    stage: str = "M4"
    execution_kind: str = "s0_closed_loop_fixture"
    arbitrary_code_execution: bool = False
    hard_isolation_verified: bool = False
    schema_version: int = 1

    def validate(self):
        if (
            self.study not in STUDIES
            or self.stage != "M4"
            or self.execution_kind != "s0_closed_loop_fixture"
            or self.arbitrary_code_execution
            or self.hard_isolation_verified
        ):
            raise ValidationError("Unsupported S0 manifest")


def arm_plan(config: LabConfig) -> tuple[dict, ...]:
    """Explicit registrations, rather than interpreting experiment names at runtime."""
    if config.study == "s0_q_pairs":
        rows = [
            ("Old", QProgram.OLD, ContextKind.IDENTITY, "identity"),
            ("New", QProgram.NEW, ContextKind.IDENTITY, "identity"),
            ("Restore", QProgram.NEW, ContextKind.RESTORE, "identity"),
            ("Placebo", QProgram.NEW, ContextKind.PLACEBO, "identity"),
            ("Remove", QProgram.OLD, ContextKind.REMOVE, "identity"),
        ]
    elif config.study == "s0_v_fixed_pool":
        rows = [
            (name, None, ContextKind.IDENTITY, name.lower())
            for name in ("Old", "New", "Restore", "Placebo")
        ]
    else:
        rows = [("Main", None, ContextKind.IDENTITY, "identity")]
    return tuple(
        {
            "arm": name,
            "assigned_q": q,
            "context_patch": ContextPatch(context, config.context_budget_units),
            "verification_patch": VerificationPatch(verification),
        }
        for name, q, context, verification in rows
    )


def protocol(config: LabConfig) -> dict:
    return {
        "schema_version": 1,
        "config": config,
        "arms": arm_plan(config),
        "frozen": provenance(),
        "implementation": implementation_fingerprint(),
        "sqlite": sqlite3.sqlite_version,
        "candidate_origin": "control",
        "task_suite": ["ok", "scope", "missing", "spent"],
        "expected_legal_per_attempt": 1,
        "continuation": {
            "name": config.continuation,
            "allowed_updates": config.allowed_updates,
            "frozen_components": config.frozen_components,
        },
        "evaluation_target": "deployed_outcome",
        "evaluation_purpose": "public_development",
        "counterfactual_candidate_continuation": False,
    }


def create_run(config: LabConfig, output: Path, *, fault_hook=None) -> dict:
    from rsi_lab.wiring import preflight

    preflight(config)
    if config.stage != "M4" or config.study not in STUDIES:
        raise ValidationError("S0 runner requires an M4 study")
    output = _checked_path(Path(output))
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ConflictError("Output exists; use resume") from exc
    store = ArtifactStore(output / "objects")
    manifest = S0Manifest(
        "run_" + uuid.uuid4().hex,
        config.study,
        store.put_json(config),
        store.put_json(protocol(config)),
        implementation_fingerprint(),
        sqlite3.sqlite_version,
        digest(provenance()),
    )
    _publish_json(output / "resolved_config.json", config)
    _publish_json(output / "run_manifest.json", manifest)
    return resume_run(output, fault_hook=fault_hook)


def load_run(output: Path):
    """Read protocol evidence without initializing a controller or mutable world."""
    from rsi_lab.wiring import preflight

    output = _checked_path(Path(output))
    for name in ("objects", "run_manifest.json", "resolved_config.json"):
        if not _checked_path(output / name).exists():
            raise IntegrityError(f"Missing run evidence: {name}")
    manifest = S0Manifest.from_dict(
        decode_json((output / "run_manifest.json").read_bytes())
    )
    config = LabConfig.from_dict(
        decode_json((output / "resolved_config.json").read_bytes())
    )
    preflight(config)
    store = ArtifactStore(output / "objects")
    if (
        config.stage != "M4"
        or manifest.study != config.study
        or manifest.implementation_sha256 != implementation_fingerprint()
        or manifest.sqlite_version != sqlite3.sqlite_version
        or manifest.frozen_sha256 != digest(provenance())
        or store.read_bytes(manifest.config) != canonical_bytes(config)
        or store.read_bytes(manifest.protocol) != canonical_bytes(protocol(config))
    ):
        raise IntegrityError("S0 config, implementation or protocol changed")
    return output, manifest, config, store


def _register_plan(output, state, store, journal, config, manifest):
    """Persist every horizon slot before dispatch, including future missing rounds."""
    snapshot, _ = _founder(output, store, state, config)
    selector = (
        SProgram.ARCHIVE_SCORE
        if config.study == "s0_q_pairs"
        else SProgram.LATEST_VALID
    )
    deployed = replace(
        Version.from_dict(decode_json(store.read_bytes(snapshot.deployed_version))),
        q=QProgram.OLD,
        s=selector,
    )
    edit = replace(
        Version.from_dict(decode_json(store.read_bytes(snapshot.edit_base_version))),
        q=QProgram.OLD,
        s=selector,
    )
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
    source_ref = state.save_snapshot(snapshot)
    fixed = None
    if config.study == "s0_v_fixed_pool":
        candidates = tuple(
            ArtifactOccurrence(
                f"v_candidate_0{i}_{alias}",
                store.put_json(
                    replace(deployed, agent=replace(deployed.agent, check_scope=safe))
                ),
                "registered_v_pool",
                CandidateOrigin.CONTROL,
                "registered_control",
            )
            for i, (alias, safe) in enumerate((("P", False), ("D", True)))
        )
        fixed = store.put_json(
            PreparationState(
                source_ref,
                ContextPatch(),
                VerificationPatch(),
                candidates=candidates,
                native_plan=("ok",),
            )
        )
        snapshot = replace(
            snapshot,
            archive=store.put_json(
                {"schema_version": 1, "entries": [], "fixed_preparation": fixed}
            ),
        )
        source_ref = state.save_snapshot(snapshot)
    with state._db(True) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS s0_branches(branch_id TEXT PRIMARY KEY, metadata_json BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS s0_rounds(branch_id TEXT NOT NULL, attempt_index INTEGER NOT NULL,
                spec_json BLOB, result_json BLOB, PRIMARY KEY(branch_id,attempt_index));
        """
        )
    plans = []
    for replicate in range(config.replicates):
        anchor = PairAnchor(
            f"s0_pair_{replicate:03}",
            f"{manifest.run_id}/root/{replicate:03}",
            f"{manifest.run_id}/history/{replicate:03}",
            source_ref,
            store.put_json(
                {
                    "schema_version": 1,
                    "horizon_attempts": config.horizon_attempts,
                    "deployment_suite": ["ok", "scope", "missing", "spent"],
                    "expected_legal_per_attempt": 1,
                }
            ),
            snapshot.random_plan,
            snapshot.cursors,
            config.pairing.mode,
            config.pairing.fork_at,
            0,
        )
        state.register_pair(anchor)
        for arm in arm_plan(config):
            branch_id = f'r{replicate:03}_{arm["arm"]}'
            state.create_branch(branch_id, anchor.pair_id)
            journal.create_budget(
                "branch", branch_id, config.resources.synthetic_units_per_branch
            )
            metadata = {
                "schema_version": 1,
                **arm,
                "branch_id": branch_id,
                "pair_id": anchor.pair_id,
                "root_id": anchor.root_id,
                "history_group_id": anchor.history_group_id,
                "replicate": replicate,
                "registered_horizon": config.horizon_attempts,
                "expected_legal_per_attempt": 1,
                "partition": config.partition.value,
                "initial_snapshot": source_ref,
                "fixed_preparation": fixed,
                "shared_cost_ref": None,
                "candidate_origin": "control",
            }
            with state._db(True) as db:
                old = db.execute(
                    "SELECT metadata_json FROM s0_branches WHERE branch_id=?",
                    (branch_id,),
                ).fetchone()
                encoded = canonical_bytes(metadata)
                if old and old[0] != encoded:
                    raise ConflictError("S0 branch plan changed")
                db.execute(
                    "INSERT OR IGNORE INTO s0_branches VALUES (?,?)",
                    (branch_id, encoded),
                )
                for index in range(1, config.horizon_attempts + 1):
                    db.execute(
                        "INSERT OR IGNORE INTO s0_rounds VALUES (?,?,NULL,NULL)",
                        (branch_id, index),
                    )
            plans.append(metadata)
    return plans


def _identities(plan, index):
    return {
        key: plan[key]
        for key in (
            "branch_id",
            "pair_id",
            "root_id",
            "history_group_id",
            "arm",
            "replicate",
        )
    } | {
        "attempt_index": index,
        "attempt_id": f'{plan["branch_id"]}_{index:03}',
        "expected_legal_denominator": plan["expected_legal_per_attempt"],
    }


def _run_slot(
    plan,
    index,
    manifest,
    config,
    state,
    store,
    runner,
    controller,
    deployment,
    probes,
    policy,
):
    identities = _identities(plan, index)
    with state._db() as db:
        row = db.execute(
            "SELECT * FROM s0_rounds WHERE branch_id=? AND attempt_index=?",
            (plan["branch_id"], index),
        ).fetchone()
    if row["result_json"]:
        return
    if row["spec_json"]:
        spec = AttemptSpec.from_dict(decode_json(row["spec_json"]))
    else:
        if deployment.is_paused(plan["branch_id"]):
            result = {
                **identities,
                "status": "not_run",
                "reason": "unsafe_current_pause",
                "public_evaluation": None,
                "decision": None,
                "commit": None,
            }
            with state._db(True) as db:
                db.execute(
                    "UPDATE s0_rounds SET result_json=? WHERE branch_id=? AND attempt_index=?",
                    (canonical_bytes(result), plan["branch_id"], index),
                )
            return
        spec = AttemptSpec(
            identities["attempt_id"],
            manifest.run_id,
            plan["pair_id"],
            plan["branch_id"],
            plan["root_id"],
            plan["history_group_id"],
            plan["arm"],
            plan["replicate"],
            index,
            manifest.protocol,
            ArtifactRef.from_dict(state.get_branch(plan["branch_id"])["head"]),
            CandidateOrigin.CONTROL,
        )
        with state._db(True) as db:
            db.execute(
                "UPDATE s0_rounds SET spec_json=? WHERE branch_id=? AND attempt_index=?",
                (canonical_bytes(spec), plan["branch_id"], index),
            )
    attempt = runner.prepare(
        spec,
        plan["context_patch"],
        plan["verification_patch"],
        fixed_preparation=plan["fixed_preparation"] if index == 1 else None,
        guard_mode=config.guard_mode,
        assigned_q=plan["assigned_q"],
    )
    if attempt["status"] not in ("active", "complete") or attempt["phase"] not in (
        "prepared",
        "audited",
        "resolved",
        "materialized",
        "committed",
    ):
        controller.record_failed_preparation(spec.attempt_id)
        result = {
            **identities,
            "status": attempt["status"],
            "reason": attempt["status_reason"],
            "source_snapshot": spec.source_snapshot,
            "decision": None,
            "commit": None,
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
        deployed = state.load_snapshot(receipt.snapshot).deployed_version
        outcome = deployment.run_suite(
            plan["branch_id"], spec.attempt_id, guard_mode=config.guard_mode
        )
        result = {
            **identities,
            "status": "committed",
            "reason": None,
            "source_snapshot": spec.source_snapshot,
            "decision": decision,
            "commit": receipt,
            "deployed_version": deployed,
            "public_evaluation": outcome,
        }
    with state._db(True) as db:
        db.execute(
            "UPDATE s0_rounds SET result_json=? WHERE branch_id=? AND attempt_index=?",
            (canonical_bytes(result), plan["branch_id"], index),
        )


def resume_run(output: Path, *, fault_hook=None) -> dict:
    output, manifest, config, store = load_run(output)
    state = StateStore(output, store)
    journal = Journal(output / "controller.sqlite")
    plans = _register_plan(output, state, store, journal, config, manifest)
    controller = LifecycleController(
        state,
        store,
        journal,
        tuple(p["branch_id"] for p in plans),
        fault_hook=fault_hook,
    )
    deployment = DeploymentRuntime(
        state, store, journal, controller, fault_hook=fault_hook
    )
    subject = FakeSubject(
        store,
        journal,
        VerificationRuntime(
            store, journal, output / "validation", fault_hook=fault_hook
        ),
        DurableFakeModelClient(output / "provider.sqlite", fault_hook=fault_hook),
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
    for plan in plans:
        for index in range(1, config.horizon_attempts + 1):
            _run_slot(
                plan,
                index,
                manifest,
                config,
                state,
                store,
                runner,
                controller,
                deployment,
                probes,
                policy,
            )
    return inspect_run(output)


def inspect_run(output: Path) -> dict:
    output, manifest, config, store = load_run(output)
    path = _checked_path(output / "controller.sqlite")
    for suffix in ("-wal", "-shm", "-journal"):
        _checked_path(Path(str(path) + suffix))
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        plans = [
            decode_json(row[0])
            for row in db.execute(
                "SELECT metadata_json FROM s0_branches ORDER BY branch_id"
            )
        ]
        rounds = []
        by_branch = {p["branch_id"]: p for p in plans}
        for branch, index, raw in db.execute(
            "SELECT branch_id,attempt_index,result_json FROM s0_rounds ORDER BY branch_id,attempt_index"
        ):
            rounds.append(
                decode_json(raw)
                if raw
                else {
                    **_identities(by_branch[branch], index),
                    "status": "pending",
                    "reason": "not_yet_executed",
                    "public_evaluation": None,
                    "decision": None,
                    "commit": None,
                }
            )
        costs = [
            dict(
                zip(
                    (
                        "operation_id",
                        "owner_kind",
                        "owner_id",
                        "purpose",
                        "synthetic_cost_units",
                    ),
                    row,
                )
            )
            for row in db.execute(
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
        paused = [
            r[0]
            for r in db.execute(
                "SELECT branch_id FROM branch_pauses ORDER BY branch_id"
            )
        ]
        reserved = sum(
            r[0]
            for r in db.execute(
                "SELECT reserved_units FROM operations WHERE status IN ('reserved','outcome_unknown')"
            )
        )
    return {
        "stage": "M4",
        "study": config.study,
        "run_id": manifest.run_id,
        "protocol": manifest.protocol.to_dict(),
        "candidate_origin": "control",
        "partition": config.partition.value,
        "plans": plans,
        "rounds": rounds,
        "registered_root_count": config.replicates,
        "registered_horizon_per_branch": config.horizon_attempts,
        "branches": branches,
        "paused_branches": paused,
        "accounting": {
            "cost_events": costs,
            "total_synthetic_units": sum(c["synthetic_cost_units"] for c in costs),
            "outstanding_reserved_units": reserved,
        },
        "candidate_labels": None,
        "candidate_label_missing_reason": "counterfactual_not_measured",
        "independent_evaluation": None,
        "hard_isolation_verified": False,
        "real_llm_used": False,
    }
