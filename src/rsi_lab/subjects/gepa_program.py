"""Pinned GEPA reflection over bounded source, with explicit activation.

This is a separate GEPA configuration from the prompt/actor adapter. One epoch
uses the official optimizer and at most one reflection. Only the selected,
activated source survives an epoch; the native Pareto pool is not inherited.
"""
from dataclasses import replace

from rsi_lab.api import (ActivationCommand, CandidateDraft, Capabilities, Component,
    ExecutionReceipt, SystemDescriptor, SystemSnapshot, SystemVersion, replace_component)
from rsi_lab.contracts import require_count, require_id
from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.errors import (BudgetExceeded, ConflictError, IntegrityError,
                            ModelResponseValidationError, OutcomeUnknown, ValidationError)
from rsi_lab.infra.chat_models import ChatMessage, DurableChatProvider
from rsi_lab.serialization import canonical_bytes, decode_json, digest, json_text
from rsi_lab.tasks.cost_program import validate_program
from .gepa import GEPA_REVISION, GEPA_URL, _QuietLogger, require_gepa
from .local import LocalSystemAdapter
from .public_validation import task_candidate_valid, task_fitness, evaluation_binding

SYSTEM_ID = "gepa-bounded-program"
ADMISSION_PROTOCOL = "gepa-bounded-program-admission-v1"
TASK_ADMISSION_PROTOCOL = "gepa-bounded-program-admission-v2"
MAX_METRIC_CALLS = 4


def initial_snapshot(seed, source, tool_protocol, contract, *, evaluation=None):
    require_count(seed, "seed")
    validate_program(source)
    if type(tool_protocol) is not str or not tool_protocol or type(contract) is not str or not contract:
        raise ValidationError("A declared bounded tool protocol and contract are required")
    task_contract = {"tool_protocol": tool_protocol, "text": contract,
        "admission": {"protocol": TASK_ADMISSION_PROTOCOL if evaluation else ADMISSION_PROTOCOL,
                      "condition": "bound_public_evaluation" if evaluation else "public_complete_correct"}}
    if evaluation is not None:
        task_contract["evaluation"] = evaluation
    return SystemSnapshot(SystemVersion(SYSTEM_ID, (
        Component("policy", json_text({"source": source}), "agent"),
        Component("task_contract", json_text(task_contract)),
    ), GEPA_REVISION), json_text({"seed": seed, "epoch": 0}))


def _fitness(public, binding=None):
    return task_fitness(public, binding)


def _reflection_example(public, binding=None):
    """Only public evidence, with the same 10,000-byte row allowance as OE."""
    rows, selected = public.get("public_trajectories", ()), []
    for row in rows:
        if len(canonical_bytes([*selected, row])) > 10000:
            break
        selected.append(row)
    return {"Inputs": {"scope": "public_development"},
        "Generated Outputs": {"public_execution": selected, "available_rows": len(rows),
            "full_sha256": public.get("public_trajectories_sha256")},
        "Feedback": {**{key: public[key] for key in ("complete", "risk", "synthetic_units",
            "legal_completed", "legal_total", "feedback") if key in public},
            "combined_score": _fitness(public, binding)}}


class GepaProgramAdapter(LocalSystemAdapter):
    """Official text reflection/selection; generated source is interpreted only."""

    def __init__(self, directory, provider, initial_snapshot):
        require_gepa()
        self._check_snapshot(initial_snapshot)
        self.provider = provider
        super().__init__(directory, initial_snapshot)
        save_record(self.directory / "configuration.json", {"provider_id": provider.provider_id,
            "upstream_revision": GEPA_REVISION, "admission_protocol": initial_snapshot.version.component("task_contract")["admission"]["protocol"],
            "max_metric_calls": MAX_METRIC_CALLS, "max_reflections": 1,
            "pool_inheritance": "activated_source_only", "actor_calls": 0,
            "reflection_public_rows_max_bytes": 10000})

    @staticmethod
    def _check_snapshot(snapshot):
        if (snapshot.version.system_id != SYSTEM_ID or snapshot.version.upstream_revision != GEPA_REVISION
                or [c.name for c in snapshot.version.components] != ["policy", "task_contract"]):
            raise ValidationError("Unsupported GEPA bounded-program snapshot")
        policy, contract = snapshot.version.component("policy"), snapshot.version.component("task_contract")
        bound = evaluation_binding(snapshot)
        expected_keys = {"tool_protocol", "text", "admission"} | ({"evaluation"} if bound else set())
        admission = {"protocol": TASK_ADMISSION_PROTOCOL if bound else ADMISSION_PROTOCOL,
                     "condition": "bound_public_evaluation" if bound else "public_complete_correct"}
        if (set(policy) != {"source"} or set(contract) != expected_keys
                or contract["admission"] != admission
                or type(contract["text"]) is not str or not contract["text"]
                or type(contract["tool_protocol"]) is not str or not contract["tool_protocol"]):
            raise ValidationError("Invalid GEPA bounded-program contract")
        validate_program(policy["source"])
        state = decode_json(snapshot.state_json)
        if set(state) != {"seed", "epoch"}:
            raise ValidationError("Invalid GEPA bounded-program state")
        require_count(state["seed"], "seed")
        require_count(state["epoch"], "epoch")

    def describe(self):
        protocol = self.snapshot().version.component("task_contract")["tool_protocol"]
        return SystemDescriptor(SYSTEM_ID, "GEPA (native bounded-source reflection)",
            Capabilities(True, True, True, True, True, (protocol,), ("policy",)), GEPA_URL, GEPA_REVISION)

    def fork(self, snapshot, directory):
        return type(self)(directory, self.provider, snapshot)

    def activate(self, command):
        if type(command) is not ActivationCommand:
            raise ValidationError("Expected controller activation command")
        self._check_snapshot(command.candidate)
        if command.candidate.version.component("task_contract") != self.snapshot().version.component("task_contract"):
            raise IntegrityError("GEPA activation changed the task contract")
        matches = []
        for path in (self.directory / "proposals").glob("*.json"):
            if path.name.endswith(".plan.json"):
                continue
            draft = CandidateDraft.from_dict(read_record(path))
            if draft.base_sha256 == command.identity.base_sha256 and draft.candidate == command.candidate:
                evidence = decode_json(draft.native_evidence_json)
                matches.append(draft.native_valid and evidence.get("platform_accepted") is True)
        if not any(matches):
            raise IntegrityError("GEPA activation lacks a matching platform-admissible proposal")
        return super().activate(command)

    def run_task(self, task, tools, operation_id):
        if task.tool_protocol != self.describe().capabilities.tool_protocols[0]:
            raise ValidationError("GEPA bounded-program task protocol mismatch")
        snapshot = self.snapshot()
        result = tools.call("execute_program", snapshot.version.component("policy"), operation_id)
        return ExecutionReceipt(task.task_id, snapshot.version.sha256, result["complete"],
            json_text({"runtime": "bounded_interpreter", "model_evidence": [], "execution": result}))

    def propose(self, operation_id, evaluator):
        require_id(operation_id)
        if not hasattr(evaluator, "plan"):
            raise ValidationError("GEPA bounded-source search requires a bound public plan")
        base = self.snapshot()
        path = self.directory / "proposals" / (digest(operation_id) + ".json")
        binding = {"base_sha256": base.sha256, "public_plan_sha256": digest(evaluator.plan),
            "operation_id": operation_id}
        plan_path = path.with_suffix(".plan.json")
        if plan_path.exists():
            if read_record(plan_path) != binding:
                raise ConflictError("GEPA proposal belongs to different inputs")
        else:
            save_record(plan_path, binding)
        if path.exists():
            draft = CandidateDraft.from_dict(read_record(path))
            if draft.base_sha256 != base.sha256:
                raise IntegrityError("GEPA proposal base mismatch")
            return draft
        request_id = "gepa.program.reflect." + digest({"operation": operation_id, "base": base.sha256})
        if isinstance(self.provider, DurableChatProvider):
            with self.provider._db() as db:
                row = db.execute("SELECT status FROM chat_requests WHERE request_id=?", (request_id,)).fetchone()
                if row and row["status"] in ("pending", "unknown"):
                    raise OutcomeUnknown("Known unknown GEPA request; no automatic resend")
        optimize, EvaluationBatch = require_gepa()
        state = decode_json(base.state_json)
        source_before = base.version.component("policy")["source"]
        evidence, reflection_inputs, reflection_receipts, reflection_errors = [], [], [], []
        owner = self

        def snapshot(source):
            return replace(base, version=replace_component(base.version, "policy", {"source": source}),
                state_json=json_text({**state, "epoch": state["epoch"] + 1}))

        class NativeTaskAdapter:
            propose_new_texts = None  # Official GEPA reflection, never a scripted proposer.

            def evaluate(self, batch, candidate, capture_traces=False):
                if set(candidate) != {"policy"} or type(candidate["policy"]) is not str:
                    raise ValidationError("GEPA changed undeclared source components")
                outputs, scores = [], []
                for _ in batch:
                    if len(evidence) >= MAX_METRIC_CALLS:
                        raise BudgetExceeded("GEPA public metric budget exhausted")
                    source = candidate["policy"]
                    op = "gepa.program.public." + digest({"operation": operation_id, "index": len(evidence)})
                    validation_error = None
                    try:
                        validate_program(source)
                    except ValidationError as exc:
                        validation_error = str(exc)
                    if validation_error:
                        feedback = {"complete": False, "risk": None, "synthetic_units": 0,
                            "legal_completed": 0, "legal_total": 0, "public_trajectories": [],
                            "feedback": {"complete_correct": False, "bounded_program_error": validation_error}}
                    else:
                        feedback = evaluator.evaluate(snapshot(source), op)
                    score = _fitness(feedback, evaluation_binding(base))
                    evidence.append({"source": source, "source_sha256": digest(source),
                        "candidate_sha256": snapshot(source).sha256, "operation_id": op,
                        "public": feedback, "combined_score": score,
                        "bounded_validation_error": validation_error})
                    outputs.append(feedback)
                    scores.append(float(score))
                return EvaluationBatch(outputs=outputs, scores=scores,
                    trajectories=outputs if capture_traces else None)

            def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
                return {name: [_reflection_example(output, evaluation_binding(base)) for output in eval_batch.outputs]
                        for name in components_to_update}

        def reflect(prompt):
            if reflection_inputs:
                raise BudgetExceeded("GEPA epoch allows only one reflection request")
            raw = prompt if type(prompt) is str else json_text({"messages": prompt})
            messages = (ChatMessage("system", base.version.component("task_contract")["text"] +
                "\nThe GEPA text component named policy is the entire bounded Python program. "
                "Follow the supplied reflection request and revise that program using only public evidence. " +
                ("Preserve the declared contract and optimize its declared task objective. " if evaluation_binding(base) else
                 "Preserve the declared contract while improving the complete-correct cost objective. ") +
                "Return a JSON object with exactly one key text. Its value must contain the complete "
                "rewritten program inside a fenced code block, as requested by GEPA."), ChatMessage("user", raw))
            reflection_inputs.append({"request_id": request_id, "messages_sha256": digest(messages),
                "upstream_prompt": raw, "parent_source_sha256": digest(source_before),
                "parent_source_present": source_before in raw})
            try:
                result = owner.provider.generate(request_id, messages)
                reflection_receipts.append(result)
                if set(result.content) != {"text"} or type(result.content["text"]) is not str:
                    raise ModelResponseValidationError("GEPA reflection requires one JSON text field")
                return result.content["text"]
            except Exception as exc:
                # Upstream reflection can absorb a failed job. A lost remote
                # outcome must remain missing, never a completed no-op proposal.
                reflection_errors.append(exc)
                raise

        result = optimize(seed_candidate={"policy": source_before},
            trainset=[{"public": True}], valset=[{"public": True}], adapter=NativeTaskAdapter(),
            reflection_lm=reflect, reflection_minibatch_size=1, max_metric_calls=MAX_METRIC_CALLS,
            stop_callbacks=lambda s: s.total_num_evals + 3 > MAX_METRIC_CALLS,
            skip_perfect_score=False, acceptance_criterion="improvement_or_equal",
            candidate_selection_strategy="pareto", seed=state["seed"] + state["epoch"],
            logger=_QuietLogger(), run_dir=None, raise_on_exception=True,
            use_merge=False, track_best_outputs=False)
        if reflection_errors:
            raise reflection_errors[0]
        index = max(range(len(result.candidates)), key=lambda i: (result.val_aggregate_scores[i], i))
        selected_source = result.candidates[index]["policy"]
        native_admitted = len(result.candidates) > 1
        selected_public = [e["public"] for e in evidence if e["source"] == selected_source]
        platform_accepted = bool(native_admitted and selected_public and
            all(task_candidate_valid(p, evaluation_binding(base)) for p in selected_public))
        source_changed = selected_source != source_before
        candidate = snapshot(selected_source) if source_changed else base
        # The first two evaluations are native seed validation and parent minibatch.
        # Keep evaluated rewrites even when identical to their parent or rejected.
        proposed = evidence[2]["source"] if len(evidence) >= 3 else None
        pool = [{"source": c["policy"], "source_sha256": digest(c["policy"]),
            "score": result.val_aggregate_scores[i], "parents": result.parents[i]}
            for i, c in enumerate(result.candidates)]
        draft = CandidateDraft(base.sha256, candidate, native_admitted, json_text({
            "optimizer": "gepa.optimize", "upstream_revision": GEPA_REVISION,
            "configuration": "bounded_source_single_reflection_epoch", "epoch": state["epoch"],
            "acceptance_semantics": base.version.component("task_contract")["admission"]["protocol"], "native_acceptance_criterion": "improvement_or_equal",
            "selection": "highest_native_validation_score_latest_tie", "pool_inheritance": "activated_source_only",
            "public_plan_sha256": digest(evaluator.plan), "parent_source_sha256": digest(source_before),
            "parent_id": "source-" + digest(source_before), "inspiration_ids": [],
            "proposed_source": proposed, "proposed_source_sha256": digest(proposed) if proposed is not None else None,
            "proposed_source_changed": proposed is not None and proposed != source_before,
            "native_admitted": native_admitted, "native_pool": pool, "native_parents": result.parents,
            "selected_index": index, "selected_source_sha256": digest(selected_source),
            "source_changed": source_changed, "state_changed": candidate != base,
            "platform_accepted": platform_accepted, "extra_gate": base.version.component("task_contract")["admission"]["condition"],
            "public_valid": bool(selected_public and all(task_candidate_valid(p, evaluation_binding(base)) for p in selected_public)),
            "public_candidate": evidence[2]["public"] if len(evidence) >= 3 else None,
            "public_evaluations": evidence, "metric_calls": result.total_metric_calls,
            "reflection_inputs": reflection_inputs, "model_receipts": reflection_receipts,
            "actor_model_calls": 0, "reflection_calls": len(reflection_receipts)}), self.provider.origin)
        save_record(path, draft)
        return draft
