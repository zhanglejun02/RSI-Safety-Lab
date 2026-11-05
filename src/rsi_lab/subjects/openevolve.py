"""Pinned OpenEvolve worker and population, stepped through controlled activation.

The upstream worker builds prompts, generates and parses full rewrites, and calls
our public evaluator. Its ProgramDatabase owns parent selection and MAP-Elites.
Only the LLM transport, bounded evaluator and pre-inheritance commit boundary
are replaced. This serial, single-island adapter is not the upstream async CLI.
"""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
from importlib import resources
from pathlib import Path
import random
from threading import RLock

from rsi_lab.api import (ActivationCommand, Capabilities, CandidateDraft, Component, ExecutionReceipt,
                         PlannedPublicEvaluator, SystemDescriptor, SystemSnapshot,
                         SystemVersion, replace_component)
from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.errors import (IntegrityError, ModelResponseValidationError, OutcomeUnknown,
                            UnsupportedCapability, ValidationError)
from rsi_lab.infra.chat_models import ChatMessage, DurableChatProvider
from rsi_lab.serialization import canonical_bytes, decode_json, digest, json_text
from rsi_lab.tasks.cost_program import validate_program
from .public_validation import task_candidate_valid as public_valid, task_fitness, evaluation_binding
from .local import LocalSystemAdapter

REVISION = "73448d715968b3578d8458b16295e827edd2b6bf"
SYSTEM_ID = "openevolve-bounded"
ADMISSION_PROTOCOL = "openevolve-admission-v2"
ADMISSION_CONDITIONS = ("public_complete_correct", "upstream", "bound_public_evaluation")
TASK_ADMISSION_PROTOCOL = "openevolve-admission-v3"
_LOCK = RLock()
_FIELDS = ("island_feature_maps", "islands", "archive", "best_program_id",
           "island_best_programs", "last_iteration", "current_island",
           "island_generations", "last_migration_generation", "feature_stats",
           "diversity_reference_set")


def verify_upstream():
    import openevolve
    root = Path(openevolve.__file__).parent.parent
    lock = decode_json(resources.files("rsi_lab.data").joinpath("openevolve_source_lock.json").read_text())
    for name, expected in lock["files"].items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise IntegrityError("Installed OpenEvolve differs from pinned source: " + name)
    return lock


def initial_snapshot(seed, source, tool_protocol, contract, *, admission_condition=None, evaluation=None):
    validate_program(source)
    task_contract = {"tool_protocol": tool_protocol, "text": contract}
    if admission_condition is not None:
        if admission_condition not in ADMISSION_CONDITIONS:
            raise ValidationError("Unknown OpenEvolve admission condition")
        task_contract["admission"] = {"protocol": TASK_ADMISSION_PROTOCOL if evaluation else ADMISSION_PROTOCOL, "condition": admission_condition}
    if evaluation is not None:
        task_contract["evaluation"] = evaluation
    return SystemSnapshot(SystemVersion(SYSTEM_ID, (
        Component("policy", json_text({"source": source}), "agent"),
        Component("task_contract", json_text(task_contract)),
    ), REVISION), json_text({"seed": seed, "population": None, "rng": random.Random(seed).getstate(),
                            "public_artifacts": {}, "generation": 0}))


def admission_binding(snapshot):
    """Absent binding identifies historical artifacts, whose native_valid included the gate."""
    binding = snapshot.version.component("task_contract").get("admission")
    if binding is not None and (type(binding) is not dict or set(binding) != {"protocol", "condition"}
            or binding["protocol"] not in (ADMISSION_PROTOCOL, TASK_ADMISSION_PROTOCOL) or binding["condition"] not in ADMISSION_CONDITIONS):
        raise ValidationError("Invalid OpenEvolve admission binding")
    current = evaluation_binding(snapshot)
    if binding and ((binding["protocol"] == TASK_ADMISSION_PROTOCOL) != (current is not None)
                    or (binding["condition"] == "bound_public_evaluation") != (current is not None)):
        raise ValidationError("Versioned admission requires its declared task evaluation")
    return binding


def admission_decision(prepared):
    """Research-driver policy; v2 callers must use this instead of native_valid alone."""
    from rsi_lab.auditing.platform import AdmissionDecision
    binding = admission_binding(prepared.base)
    if admission_binding(prepared.draft.candidate) != binding:
        raise IntegrityError("An update changed its admission condition")
    if evaluation_binding(prepared.base) != evaluation_binding(prepared.draft.candidate):
        raise IntegrityError("An update changed its task evaluation binding")
    evidence = decode_json(prepared.draft.native_evidence_json)
    if binding is None:
        accepted = prepared.draft.native_valid
        policy, reason = "openevolve-admission-legacy", "historical_combined_native_and_public_gate"
    else:
        if (evidence.get("admission_binding") != binding
                or evidence.get("native_admitted") is not prepared.draft.native_valid):
            raise IntegrityError("Proposal evidence differs from the bound admission protocol")
        accepted = bool(prepared.draft.native_valid and (binding["condition"] == "upstream"
                        or public_valid(evidence.get("public_candidate") or {}, evaluation_binding(prepared.base))))
        if evidence.get("platform_accepted") is not accepted:
            raise IntegrityError("Proposal platform admission evidence is inconsistent")
        policy, reason = binding["protocol"], binding["condition"]
    return AdmissionDecision(prepared.identity, digest(evidence), policy,
                             "accept" if accepted else "reject", reason)


def database_config():
    """One pinned database configuration shared by live proposals and offline replay."""
    from openevolve.config import DatabaseConfig
    return DatabaseConfig(num_islands=1, population_size=32, archive_size=16,
        feature_dimensions=["complexity"], random_seed=None, embedding_model=None,
        novelty_llm=None, log_prompts=False)


def _tuples(value):
    return tuple(_tuples(x) for x in value) if isinstance(value, list) else value


def _export(db):
    values = {name: getattr(db, name) for name in _FIELDS}
    values["islands"] = [sorted(x) for x in db.islands]
    values["archive"] = sorted(db.archive)
    values["programs"] = {key: item.to_dict() for key, item in sorted(db.programs.items())}
    return decode_json(canonical_bytes(values))


def _restore(db, population):
    from openevolve.database import Program
    if set(population) != set(_FIELDS) | {"programs"}:
        raise IntegrityError("Incomplete OpenEvolve population snapshot")
    population = decode_json(canonical_bytes(population))
    for name in _FIELDS:
        setattr(db, name, population[name])
    db.programs = {key: Program.from_dict(row) for key, row in population["programs"].items()}
    db.archive = set(db.archive)
    db.islands = [set(x) for x in db.islands]
    ids = set(db.programs)
    references = db.archive | set().union(*db.islands) | {db.best_program_id}
    if not ids or references - ids or any(key != p.id for key, p in db.programs.items()):
        raise IntegrityError("Population references a missing program")
    for p in db.programs.values():
        validate_program(p.code)


def _fitness(public, binding=None):
    return {"combined_score": task_fitness(public, binding)}


def _artifact(public):
    rows = public.get("public_trajectories", ())
    selected = []
    for row in rows:
        if len(canonical_bytes([*selected, row])) > 10000:
            break
        selected.append(row)
    return {"public_feedback": json_text({k: public[k] for k in
        ("complete", "risk", "synthetic_units", "legal_completed", "legal_total", "feedback")}),
        "public_execution": json_text({"rows": selected, "available": len(rows),
                                       "full_sha256": public.get("public_trajectories_sha256")})}


class _ModelBridge:
    def __init__(self, provider, operation):
        self.provider, self.operation = provider, operation
        self.receipt, self.error = None, None
        self.last_usage, self.last_model = None, None

    def reseed(self, iteration):
        pass  # A cloud generation is a distinct sample; no fabricated provider seed.

    async def generate_with_context(self, system_message, messages):
        wrapped = system_message + '\nTransport requirement: return a JSON object with exactly one key "response", whose string value contains your full rewritten program in a Python code fence.'
        try:
            self.receipt = self.provider.generate(self.operation + "/model", (
                ChatMessage("system", wrapped), *tuple(ChatMessage(m["role"], m["content"]) for m in messages)))
            if set(self.receipt.content) != {"response"} or type(self.receipt.content["response"]) is not str:
                raise ModelResponseValidationError("OpenEvolve transport requires a response string")
            self.last_model = self.receipt.response_model
            self.last_usage = {"total_tokens": self.receipt.total_tokens}
            return self.receipt.content["response"]
        except Exception as exc:
            self.error = exc
            raise


class _EvaluatorBridge:
    def __init__(self, base, evaluator, operation):
        self.base, self.evaluator, self.operation = base, evaluator, operation
        self.public, self.artifacts = None, {}
        self.error = None

    async def evaluate_program(self, source, child_id):
        validate_program(source)
        try:
            candidate = replace(self.base, version=replace_component(self.base.version, "policy", {"source": source}))
            # Use source identity, not upstream's random uuid, for durable replay.
            self.public = self.evaluator.evaluate(candidate, self.operation + "/child/" + digest(source)[:20])
            self.artifacts = _artifact(self.public)
            return _fitness(self.public, evaluation_binding(self.base))
        except Exception as exc:
            # Native workers may absorb exceptions. Only bounded-source rejection
            # above is a candidate failure; trusted evaluation errors must escape.
            self.error = exc
            raise

    def get_pending_artifacts(self, child_id):
        return self.artifacts


@contextmanager
def _native_context(config, sampler, model, evaluator, rng):
    import openevolve.process_parallel as worker
    names = ("_worker_config", "_worker_prompt_sampler", "_worker_llm_ensemble", "_worker_evaluator", "_worker_event_loop")
    with _LOCK:
        before = {key: getattr(worker, key, None) for key in names}
        rng_before = random.getstate()
        try:
            random.setstate(_tuples(rng))
            for key, value in zip(names, (config, sampler, model, evaluator, None)):
                setattr(worker, key, value)
            yield worker
        finally:
            worker._close_worker_event_loop()
            for key, value in before.items():
                setattr(worker, key, value)
            random.setstate(rng_before)


class OpenEvolveAdapter(LocalSystemAdapter):
    def __init__(self, directory, provider, initial_snapshot):
        verify_upstream()
        if (initial_snapshot.version.system_id != SYSTEM_ID
                or initial_snapshot.version.upstream_revision != REVISION):
            raise ValidationError("OpenEvolve snapshot revision mismatch")
        validate_program(initial_snapshot.version.component("policy")["source"])
        admission_binding(initial_snapshot)
        self.provider = provider
        super().__init__(directory, initial_snapshot)

    def describe(self):
        protocol = self.snapshot().version.component("task_contract")["tool_protocol"]
        return SystemDescriptor(SYSTEM_ID, "OpenEvolve bounded native worker",
            Capabilities(True, True, True, True, True, (protocol,), ("policy",)),
            "https://github.com/algorithmicsuperintelligence/openevolve", REVISION)

    def fork(self, snapshot, directory):
        return type(self)(directory, self.provider, snapshot)

    def activate(self, command):
        # V2 requires the study's platform decision. Fail closed if a generic
        # native-only driver tries to commit an upstream-admitted gated rejection.
        if type(command) is not ActivationCommand:
            raise ValidationError("Expected controller activation command")
        binding = admission_binding(command.candidate)
        if evaluation_binding(command.candidate) and command.candidate.version.component("task_contract") != self.snapshot().version.component("task_contract"):
            raise IntegrityError("Candidate changed the trusted task evaluation contract")
        if binding:
            if binding != admission_binding(self.snapshot()):
                raise IntegrityError("Activation changed the bound admission condition")
            matches = []
            for path in (self.directory / "proposals").glob("*.json"):
                if path.name.endswith(".plan.json"):
                    continue
                draft = CandidateDraft.from_dict(read_record(path))
                if (draft.base_sha256 == command.identity.base_sha256
                        and draft.candidate.sha256 == command.candidate.sha256):
                    evidence = decode_json(draft.native_evidence_json)
                    matches.append(bool(draft.native_valid and evidence.get("native_admitted") is True
                        and evidence.get("admission_binding") == binding
                        and evidence.get("platform_accepted") is True
                        and (binding["condition"] == "upstream"
                             or public_valid(evidence.get("public_candidate") or {}, evaluation_binding(command.candidate)))))
            if not matches or not all(matches):
                raise ValidationError("V2 activation requires a persisted platform-admitted proposal")
        return super().activate(command)

    def propose(self, operation_id, evaluator: PlannedPublicEvaluator):
        if getattr(evaluator, "plan", None) is None:
            raise UnsupportedCapability("OpenEvolve requires PlannedPublicEvaluator with a public task plan")
        from openevolve.config import Config, PromptConfig
        from openevolve.database import Program, ProgramDatabase
        from openevolve.prompt.sampler import PromptSampler
        base = self.snapshot()
        path = self.directory / "proposals" / (digest(operation_id) + ".json")
        if path.exists():
            draft = CandidateDraft.from_dict(read_record(path))
            if draft.base_sha256 != base.sha256:
                raise IntegrityError("Proposal replay uses a different active snapshot")
            return draft
        # A persisted unknown intent is terminal for this attempt. Do not rebuild
        # a possibly different upstream prompt merely to discover it cannot retry.
        if isinstance(self.provider, DurableChatProvider):
            with self.provider._db() as db:
                row = db.execute("SELECT status FROM chat_requests WHERE request_id=?", (operation_id + "/model",)).fetchone()
                if row and row["status"] in ("pending", "unknown"):
                    raise OutcomeUnknown("Known unknown OpenEvolve request; no automatic resend")
        binding = admission_binding(base)
        condition = binding["condition"] if binding else "public_complete_correct"
        state = decode_json(base.state_json)
        config = Config(diff_based_evolution=False, language="python", max_code_length=16384,
            database=database_config(),
            prompt=PromptConfig(system_message=base.version.component("task_contract")["text"],
                use_template_stochasticity=False, num_top_programs=2, num_diverse_programs=1,
                max_artifact_bytes=12000))
        model = _ModelBridge(self.provider, operation_id)
        bridge = _EvaluatorBridge(base, evaluator, operation_id)
        with _native_context(config, PromptSampler(config.prompt), model, bridge, state["rng"]) as worker:
            db = ProgramDatabase(config.database)
            if state["population"]:
                _restore(db, state["population"])
            else:
                seed_public = evaluator.evaluate(base, operation_id + "/seed")
                seed = Program(id="seed-" + digest(base.version.component("policy"))[0:20],
                               code=base.version.component("policy")["source"],
                               metrics=_fitness(seed_public, evaluation_binding(base)), timestamp=0.0)
                db.add(seed, iteration=0, target_island=0)
            # All archive scores refer to this same public suite; no stale task scores.
            artifacts = {}
            public_by_id = {}
            for key, program in sorted(db.programs.items()):
                snapshot = replace(base, version=replace_component(base.version, "policy", {"source": program.code}))
                public = evaluator.evaluate(snapshot, operation_id + "/archive/" + key)
                program.metrics = _fitness(public, evaluation_binding(base))
                artifacts[key], public_by_id[key] = _artifact(public), public
            db.best_program_id = None
            incumbent_program = db.get_best_program()
            parent, inspirations = db.sample(num_inspirations=2)
            iteration = state["generation"] + 1
            native_input = {"programs": {k: p.to_dict() for k, p in db.programs.items()},
                "artifacts": artifacts, "islands": [sorted(x) for x in db.islands],
                "current_island": 0, "sampling_island": 0, "feature_dimensions": ["complexity"]}
            # Persist and canonicalize BEFORE dispatch. Native sets and equal-score
            # tie ordering otherwise depend on Python's per-process hash seed.
            plan_path = path.with_suffix(".plan.json")
            if not plan_path.exists():
                save_record(plan_path, {"base_sha256": base.sha256,
                    "public_plan_sha256": digest(evaluator.plan), "input": native_input,
                    "parent_id": parent.id, "inspiration_ids": [p.id for p in inspirations],
                    "population": _export(db), "rng": random.getstate()})
            plan = read_record(plan_path)
            if plan["base_sha256"] != base.sha256 or plan["public_plan_sha256"] != digest(evaluator.plan):
                raise IntegrityError("Native preparation plan belongs to different inputs")
            _restore(db, plan["population"])
            random.setstate(_tuples(plan["rng"]))
            native_input = plan["input"]
            parent = db.programs[plan["parent_id"]]
            inspirations = [db.programs[key] for key in plan["inspiration_ids"]]
            result = worker._run_iteration(iteration, native_input, parent.id,
                                           [p.id for p in inspirations], "full_rewrite")
            if model.error:
                raise model.error
            if bridge.error:
                raise bridge.error
            evidence = {"optimizer": "openevolve_native_worker_single_island", "upstream_revision": REVISION,
                "native_entrypoint": "openevolve.process_parallel._run_iteration", "parent_id": parent.id,
                "inspiration_ids": [p.id for p in inspirations], "population_before": _export(db),
                "native_prompt": result.prompt, "model_receipt": model.receipt,
                "public_candidate": bridge.public, "native_error": result.error,
                "incumbent": public_by_id[incumbent_program.id],
                "acceptance_semantics": "native_archive_admission_plus_public_complete_correct_gate",
                "extra_gate": "public_complete_correct;no_independent_labels;not_strict_improvement"}
            active_before = (state["population"] or {}).get("best_program_id")
            if active_before is None:
                active_before = next((key for key, p in db.programs.items()
                    if p.code == base.version.component("policy")["source"]), None)
            candidate, admitted, platform_accepted = base, False, False
            native_attempted, admitted_id = False, None
            evidence.update(admission_binding=binding, public_evaluation=bridge.public,
                public_valid=bool(bridge.public and public_valid(bridge.public, evaluation_binding(base))),
                active_program_id_before=active_before, admission_rng=random.getstate())
            if binding:
                evidence.update(acceptance_semantics=binding["protocol"],
                    extra_gate=condition, admission_condition=condition)
            if result.child_program_dict:
                child = Program.from_dict(result.child_program_dict)
                # IDs and timestamps are metadata, normalized for exact durable replay.
                identity_base = base
                if binding:
                    # Admission is the treatment, not a source of label/tie-order
                    # randomness when both arms admit the same candidate.
                    contract = dict(base.version.component("task_contract"))
                    contract.pop("admission")
                    identity_base = replace(base, version=replace_component(base.version, "task_contract", contract))
                child.id = "child-" + digest({"operation": operation_id, "base": identity_base.sha256,
                                             "source": child.code})[:24]
                child.timestamp = float(iteration)
                evidence.update(proposed_source=child.code, child_id=child.id,
                                child_before_admission=child.to_dict())
                # Run upstream admission independently of the platform gate. This is
                # proposal-local state; rejection never writes it into the adapter.
                native_attempted = True
                admitted_id = db.add(child, iteration=iteration, target_island=0)
                admitted = admitted_id is not None and child.id in db.programs
                platform_accepted = bool(admitted and bridge.public and
                    (condition == "upstream" or public_valid(bridge.public, evaluation_binding(base))))
                if admitted:
                    best = db.get_best_program()
                    artifacts[child.id] = bridge.artifacts
                    native_candidate = replace(base, version=replace_component(base.version, "policy", {"source": best.code}),
                        state_json=json_text({**state, "population": _export(db), "rng": random.getstate(),
                            "public_artifacts": {k: v for k, v in artifacts.items() if k in db.programs},
                            "generation": iteration}))
                    # Legacy draft semantics remain unchanged. V2 represents the
                    # upstream-permitted proposal, then the study applies its gate.
                    if binding or platform_accepted:
                        candidate = native_candidate
                    evidence["native_active_program_id"] = best.id
            evidence.update(native_admission={"attempted": native_attempted, "admitted": admitted,
                    "returned_id": admitted_id}, native_admitted=admitted,
                platform_accepted=platform_accepted,
                platform_admission={"condition": condition, "accepted": platform_accepted},
                population_after_native=_export(db),
                population_after_platform=(decode_json(candidate.state_json)["population"]
                                           if platform_accepted else state["population"]),
                active_program_id=(evidence.get("native_active_program_id", active_before)
                                   if platform_accepted else active_before),
                selected=platform_accepted, changed=candidate != base,
                archive_only_update=bool(platform_accepted and candidate != base
                                         and candidate.version == base.version),
                proposed_population_sha256=digest(decode_json(candidate.state_json)["population"]))
            if evaluation_binding(base):
                evidence["evaluation_binding"] = evaluation_binding(base)
                evidence["rescored_archive"] = public_by_id
            draft = CandidateDraft(base.sha256, candidate, admitted if binding else platform_accepted,
                                   json_text(evidence), self.provider.origin)
        save_record(path, draft)
        return draft

    def run_task(self, task, tools, operation_id):
        if task.tool_protocol != self.describe().capabilities.tool_protocols[0]:
            raise ValidationError("OpenEvolve task protocol mismatch")
        snapshot = self.snapshot()
        result = tools.call("execute_program", snapshot.version.component("policy"), operation_id)
        return ExecutionReceipt(task.task_id, snapshot.version.sha256, result["complete"],
            json_text({"runtime": "bounded_interpreter", "model_evidence": [], "execution": result}))
