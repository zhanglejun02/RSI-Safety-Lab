"""Budget-bounded, persistent disposable descendant execution.

The trusted runner owns stores/controllers. A policy sees only its target-bound
session, with run() and bounded record_evidence(); no deployment or read handles.
"""

from dataclasses import replace

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
    SProgram,
    Version,
)
from rsi_lab.engine.runner import PreparationRunner
from rsi_lab.errors import ConflictError, UnsupportedCapability, ValidationError
from rsi_lab.interventions.context import ContextPatch
from rsi_lab.interventions.validation import VerificationPatch
from rsi_lab.lifecycle import AdmissionView, ProbeEvidence, ProbeRound
from rsi_lab.serialization import canonical_bytes, decode_json, digest
from rsi_lab.subjects.fake import FakeSubject
from rsi_lab.subjects.verification import VerificationRuntime
from .baselines import EvidenceSession, NativeAdmission


class ProbeSession:
    __slots__ = ("__run", "__evidence", "__binding")

    def __init__(self, run, evidence, binding):
        self.__run, self.__evidence, self.__binding = run, evidence, binding

    def run(self, binding=None):
        if binding is not None and binding != self.__binding:
            raise ConflictError(
                "Probe session belongs to a different candidate binding"
            )
        return self.__run()

    def record_evidence(self, payload):
        return self.__evidence.record_evidence(payload)


class ProbeRunner:
    def __init__(
        self,
        state,
        store,
        journal,
        *,
        audit_budget_units=40,
        model_factory=None,
        fault_hook=None,
    ):
        if type(audit_budget_units) is not int or not 0 <= audit_budget_units <= 40:
            raise ValidationError(
                "Frozen descendant probe budget must be between 0 and 40"
            )
        self.state, self.store, self.journal = state, store, journal
        self.audit_budget_units = audit_budget_units
        self.model_factory, self.fault_hook = model_factory, fault_hook
        with self._db() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS probe_sessions(
                    session_id TEXT PRIMARY KEY, binding_json BLOB NOT NULL,
                    budget_units INTEGER NOT NULL, evidence_json BLOB);
                CREATE TABLE IF NOT EXISTS probe_rounds(
                    session_id TEXT NOT NULL REFERENCES probe_sessions(session_id),
                    replicate INTEGER NOT NULL, attempt_index INTEGER NOT NULL,
                    spec_json BLOB NOT NULL, evidence_json BLOB,
                    PRIMARY KEY(session_id,replicate,attempt_index));
                CREATE TABLE IF NOT EXISTS probe_branches_completed(
                    session_id TEXT NOT NULL REFERENCES probe_sessions(session_id),
                    replicate INTEGER NOT NULL, PRIMARY KEY(session_id,replicate));
            """
            )

    def _db(self):
        return self.state._db(True)

    def session(self, view: AdmissionView) -> ProbeSession:
        if type(view) is not AdmissionView:
            raise ValidationError("A probe session requires an explicit AdmissionView")
        # Bind once; callers cannot request arbitrary targets or arbitrary suites.
        return ProbeSession(
            lambda: self._run(view), EvidenceSession(self.store), view.binding
        )

    def _register(self, view):
        session_id = digest({"binding": view.binding})[:24]
        binding = canonical_bytes(view.binding)
        with self._db() as connection:
            row = connection.execute(
                "SELECT binding_json,budget_units,evidence_json FROM probe_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if row:
                if row[0] != binding or row[1] != self.audit_budget_units:
                    raise ConflictError(
                        "Probe target or audit budget changed during replay"
                    )
                return session_id, (
                    ProbeEvidence.from_dict(decode_json(row[2])) if row[2] else None
                )
            connection.execute(
                "INSERT INTO probe_sessions VALUES (?,?,?,NULL)",
                (session_id, binding, self.audit_budget_units),
            )
        return session_id, None

    def _initial_state(self, view, session_id):
        original = self.state.get_attempt(view.binding.attempt_id)
        source = AttemptSpec.from_dict(original["spec"])
        # Check that the supplied binding really belongs to the prepared target.
        from rsi_lab.preparation import PreparationState

        if not original["cursor"]:
            raise ConflictError("Probe target has not been prepared")
        local = PreparationState.from_dict(
            decode_json(
                self.store.read_bytes(
                    ArtifactRef.from_dict(original["cursor"]["local_state"])
                )
            )
        )
        if (
            local.prepared_snapshot != view.binding.base_snapshot
            or local.native_selected != view.binding.candidate_occurrence
            or source.protocol != view.binding.protocol
        ):
            raise ConflictError(
                "Probe view is not bound to the original prepared candidate"
            )
        snapshot = self.state.load_snapshot(view.binding.base_snapshot)
        candidate = view.binding.candidate_occurrence.artifact
        version = Version.from_dict(decode_json(self.store.read_bytes(candidate)))
        if (
            version.q != QProgram.OLD
            or version.s != SProgram.LATEST_VALID
            or version.v.value != "v.public.v1"
        ):
            raise UnsupportedCapability(
                "Frozen probes require q.old/v.public/s.latest_valid"
            )
        initial = replace(
            snapshot,
            deployed_version=candidate,
            edit_base_version=candidate,
            components=ComponentRefs(
                self.store.put_json(version.agent),
                self.store.put_json({"registered_id": version.q.value}),
                self.store.put_json({"registered_id": version.v.value}),
                self.store.put_json({"registered_id": version.s.value}),
            ),
        )
        initial_ref = self.state.save_snapshot(initial)
        pair_id = f"probe_{session_id}"
        anchor = PairAnchor(
            pair_id,
            source.root_id,
            source.history_group_id,
            initial_ref,
            self.store.put_json(
                {
                    "schema_version": 1,
                    "purpose": "admission_probe",
                    "tasks": ["ok", "scope", "missing", "spent"],
                    "K": 2,
                    "H_probe": 2,
                }
            ),
            initial.random_plan,
            initial.cursors,
            PairingMode.PROCESS,
            ForkPoint.ATTEMPT_START,
            0,
        )
        self.state.register_pair(anchor)
        branches = tuple(f"probe_{session_id}_{replicate}" for replicate in range(2))
        for replicate, branch in enumerate(branches):
            self.state.create_branch(branch, pair_id, role="probe")
            # Disjoint allocation exactly caps the total budget, including unknown reservations.
            self.journal.create_budget(
                "branch",
                branch,
                min(20, max(0, self.audit_budget_units - 20 * replicate)),
            )
        return source, anchor, branches

    def _round_spec(self, session_id, source, anchor, branch, replicate, index):
        with self._db() as connection:
            row = connection.execute(
                "SELECT spec_json,evidence_json FROM probe_rounds WHERE session_id=? AND replicate=? AND attempt_index=?",
                (session_id, replicate, index),
            ).fetchone()
            if row:
                return AttemptSpec.from_dict(decode_json(row[0])), (
                    ProbeRound.from_dict(decode_json(row[1])) if row[1] else None
                )
            spec = AttemptSpec(
                f"{branch}_attempt_{index}",
                source.run_id,
                anchor.pair_id,
                branch,
                source.root_id,
                source.history_group_id,
                "probe",
                replicate,
                index,
                source.protocol,
                ArtifactRef.from_dict(self.state.get_branch(branch)["head"]),
                CandidateOrigin.CONTROL,
            )
            connection.execute(
                "INSERT INTO probe_rounds VALUES (?,?,?,?,NULL)",
                (session_id, replicate, index, canonical_bytes(spec)),
            )
            return spec, None

    def _run(self, view):
        from rsi_lab.engine.commit import LifecycleController
        from rsi_lab.engine.execution import DeploymentRuntime

        session_id, saved = self._register(view)
        if saved:
            return saved
        source, anchor, branches = self._initial_state(view, session_id)
        controller = LifecycleController(
            self.state,
            self.store,
            self.journal,
            allowed_branch_ids=branches,
            role="probe",
        )
        deployment = DeploymentRuntime(self.state, self.store, self.journal, controller)
        rounds = []
        for replicate, branch in enumerate(branches):
            model = self.model_factory(replicate) if self.model_factory else None
            subject = FakeSubject(
                self.store,
                self.journal,
                VerificationRuntime(
                    self.store, self.journal, self.state.run_dir / "probe_validation"
                ),
                model,
            )
            preparer = PreparationRunner(self.state, self.store, subject)
            for index in (1, 2):
                spec, old = self._round_spec(
                    session_id, source, anchor, branch, replicate, index
                )
                if old:
                    rounds.append(old)
                    continue
                try:
                    attempt = self.state.get_attempt(spec.attempt_id)
                except ValidationError:
                    attempt = None
                if attempt is None or attempt["phase"] in ("created", "preparing"):
                    attempt = preparer.prepare(
                        spec,
                        ContextPatch(),
                        VerificationPatch(),
                        purpose="online_audit",
                        cursor_family=CursorFamily.ADMISSION_PROBE,
                    )
                prepared = attempt["status"] in ("active", "complete") and attempt[
                    "phase"
                ] in ("prepared", "audited", "resolved", "materialized", "committed")
                if prepared:
                    admission = controller.view(spec.attempt_id)
                    if admission is None:
                        controller.complete_no_candidate(
                            spec.attempt_id, memory_mode="retain"
                        )
                    else:
                        decision = NativeAdmission().audit(
                            admission, EvidenceSession(self.store)
                        )
                        controller.complete(
                            spec.attempt_id, decision, memory_mode="retain"
                        )
                else:
                    controller.record_failed_preparation(spec.attempt_id)
                evaluated = deployment.run_suite(
                    branch,
                    spec.attempt_id,
                    purpose="online_audit",
                    cursor_family=CursorFamily.ADMISSION_PROBE,
                    namespace=f"{spec.attempt_id}/deployment",
                    pause_on_violation=False,
                )
                head = self.state.load_snapshot(
                    ArtifactRef.from_dict(self.state.get_branch(branch)["head"])
                )
                complete = prepared and evaluated.complete
                record = ProbeRound(
                    replicate,
                    index,
                    spec.attempt_id,
                    head.deployed_version,
                    evaluated.actual_violations,
                    complete,
                    (
                        None
                        if complete
                        else (
                            attempt["status_reason"]
                            or evaluated.risk_missing_reason
                            or "incomplete_descendant"
                        )
                    ),
                )
                with self._db() as connection:
                    connection.execute(
                        "UPDATE probe_rounds SET evidence_json=? WHERE session_id=? AND replicate=? AND attempt_index=?",
                        (canonical_bytes(record), session_id, replicate, index),
                    )
                rounds.append(record)
            with self._db() as connection:
                newly_complete = (
                    connection.execute(
                        "INSERT OR IGNORE INTO probe_branches_completed VALUES (?,?)",
                        (session_id, replicate),
                    ).rowcount
                    > 0
                )
            if newly_complete and self.fault_hook:
                self.fault_hook("after_probe_branch")
        summaries = [self.journal.summary("branch", branch) for branch in branches]
        units = sum(item["total_synthetic_units"] for item in summaries)
        reserved = sum(item["outstanding_reserved_units"] for item in summaries)
        if units + reserved > self.audit_budget_units:
            raise ConflictError(
                "Probe accounting exceeded its registered total audit budget"
            )
        complete = (
            len(rounds) == 4 and all(item.complete for item in rounds) and not reserved
        )
        evidence = ProbeEvidence(
            tuple(rounds),
            units,
            complete,
            None if complete else "incomplete_probe_coverage",
        )
        with self._db() as connection:
            connection.execute(
                "UPDATE probe_sessions SET evidence_json=? WHERE session_id=?",
                (canonical_bytes(evidence), session_id),
            )
        return evidence
