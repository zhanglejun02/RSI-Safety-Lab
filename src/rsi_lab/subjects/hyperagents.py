"""Pinned HyperAgents task/meta evolution in a bounded single-task profile.

Native MetaAgent/tool loop/uniform archive parent selection are executed. Domain
execution, admission, active-best reporting and model transport are lab adapters.
This profile does not reproduce the upstream Docker multi-domain benchmark.
"""
import ast
from copy import deepcopy
from dataclasses import replace
import hashlib
import random

from rsi_lab.api import (ActivationCommand, CandidateDraft, Capabilities, Component,
    ExecutionReceipt, SystemDescriptor, SystemSnapshot, SystemVersion, replace_component)
from rsi_lab.contracts import require_count, require_id
from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.errors import IntegrityError, ValidationError
from rsi_lab.serialization import decode_json, digest, json_text
from rsi_lab.tasks.cost_program import validate_program
from .local import LocalSystemAdapter
from .public_validation import evaluation_binding, task_candidate_valid, task_fitness
from . import hyperagents_runtime as runtime

REVISION = "59a68f672dfb92c74aeb7e61535d776fb36e172d"
URL = "https://github.com/facebookresearch/HyperAgents"
SYSTEM_ID = "hyperagents-bounded-meta"
PROTOCOL = "hyperagents-bounded-meta-admission-v1"


def verify_vendor():
    import json
    lock = json.loads((runtime.VENDOR / "source-lock.json").read_text())
    if lock["revision"] != REVISION:
        raise IntegrityError("HyperAgents revision changed")
    for name, sha in lock["sha256"].items():
        if hashlib.sha256((runtime.VENDOR / name).read_bytes()).hexdigest() != sha:
            raise IntegrityError("Pinned HyperAgents source changed: " + name)


def initial_snapshot(seed, source, tool_protocol, contract, *, evaluation):
    verify_vendor(); validate_program(source); require_count(seed, "seed")
    meta = (runtime.VENDOR / "meta_agent.py").read_text()
    files = {"policy.py": source, "meta_agent.py": meta}
    state = {"seed": seed, "epoch": 0, "active_id": "initial", "archive": {
        "initial": {"files": files, "parent_id": None, "valid_parent": True, "score": None}}}
    snapshot = SystemSnapshot(SystemVersion(SYSTEM_ID, (
        Component("policy", json_text({"source": source}), "agent"),
        Component("meta_agent", json_text({"source": meta}), "optimizer"),
        Component("task_contract", json_text({"text": contract, "tool_protocol": tool_protocol,
            "evaluation": evaluation, "admission": {"protocol": PROTOCOL, "condition": "bound_public_evaluation"}})),
    ), REVISION), json_text(state))
    evaluation_binding(snapshot)
    return snapshot


def native_parent(archive, seed):
    """Execute the unchanged pinned function AST with a lab score-store bridge."""
    tree = ast.parse((runtime.VENDOR / "select_next_parent.py").read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "select_next_parent")
    namespace = {"random": random.Random(seed), "is_starting_node": lambda key: key == "initial",
        "get_node_metadata_key": lambda out, key, field: archive[key][field],
        "get_domain_splits": lambda dom: ["train"],
        "get_saved_score": lambda dom, out, key, **kw: archive[key]["score"],
        "get_parent_genid": lambda out, key: archive[key]["parent_id"]}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "pinned-select-next-parent", "exec"), namespace)
    return namespace["select_next_parent"](list(archive), "lab-public-store", ["task"])


class HyperAgentsAdapter(LocalSystemAdapter):
    def __init__(self, directory, provider, initial_snapshot):
        verify_vendor(); self._check_snapshot(initial_snapshot)
        self.provider = provider
        super().__init__(directory, initial_snapshot)
        save_record(self.directory / "configuration.json", {"revision": REVISION, "protocol": PROTOCOL,
            "profile": "bounded_task_self_modifying_meta", "max_calls_per_edit": runtime.MAX_CALLS,
            "parent_selection": "pinned_uniform_valid_archive", "active": "highest_public_score_latest_tie"})

    @staticmethod
    def _check_snapshot(snapshot):
        if (snapshot.version.system_id != SYSTEM_ID or snapshot.version.upstream_revision != REVISION
                or [c.name for c in snapshot.version.components] != ["policy", "meta_agent", "task_contract"]):
            raise ValidationError("Unsupported HyperAgents snapshot")
        bound = evaluation_binding(snapshot)
        if bound is None or snapshot.version.component("task_contract")["admission"] != {
                "protocol": PROTOCOL, "condition": "bound_public_evaluation"}:
            raise ValidationError("HyperAgents requires a versioned public evaluation")
        state = decode_json(snapshot.state_json)
        if set(state) != {"seed", "epoch", "active_id", "archive"}:
            raise ValidationError("Invalid HyperAgents inherited state")
        require_count(state["seed"], "seed"); require_count(state["epoch"], "epoch")
        files = state["archive"][state["active_id"]]["files"]
        if (files != {"policy.py": snapshot.version.component("policy")["source"],
                      "meta_agent.py": snapshot.version.component("meta_agent")["source"]}):
            raise IntegrityError("HyperAgents active components differ from archive")
        for row in state["archive"].values():
            if set(row["files"]) != {"policy.py", "meta_agent.py"} or not row["valid_parent"]:
                raise ValidationError("Invalid HyperAgents retained node")
            validate_program(row["files"]["policy.py"])
            try:
                compile(row["files"]["meta_agent.py"], "meta_agent.py", "exec")
            except SyntaxError as exc:
                raise ValidationError("Invalid retained meta source") from exc

    def describe(self):
        return SystemDescriptor(SYSTEM_ID, "HyperAgents (bounded task, executable meta-agent)",
            Capabilities(True, True, True, True, True,
                (self.snapshot().version.component("task_contract")["tool_protocol"],), ("policy", "meta_agent"), False), URL, REVISION)

    def fork(self, snapshot, directory):
        return type(self)(directory, self.provider, snapshot)

    def run_task(self, task, tools, operation_id):
        if task.tool_protocol != self.describe().capabilities.tool_protocols[0]:
            raise ValidationError("HyperAgents task protocol mismatch")
        snapshot = self.snapshot()
        result = tools.call("execute_program", snapshot.version.component("policy"), operation_id)
        return ExecutionReceipt(task.task_id, snapshot.version.sha256, result["complete"],
            json_text({"runtime": "bounded_task_program", "model_evidence": [], "execution": result}))

    def activate(self, command):
        if type(command) is not ActivationCommand:
            raise ValidationError("Expected controller activation command")
        self._check_snapshot(command.candidate)
        if command.candidate.version.component("task_contract") != self.snapshot().version.component("task_contract"):
            raise IntegrityError("HyperAgents activation changed the task contract")
        for path in (self.directory / "proposals").glob("*/draft.json"):
            draft = CandidateDraft.from_dict(read_record(path))
            if (draft.base_sha256 == command.identity.base_sha256 and draft.candidate == command.candidate
                    and draft.native_valid and decode_json(draft.native_evidence_json)["platform_accepted"]):
                return super().activate(command)
        raise IntegrityError("HyperAgents activation lacks matching admissible proposal")

    def propose(self, operation_id, evaluator):
        require_id(operation_id)
        base = self.snapshot(); binding = evaluation_binding(base)
        directory = self.directory / "proposals" / digest(operation_id)
        save_record(directory / "plan.json", {"base_sha256": base.sha256,
                    "operation_id": operation_id, "public_plan_sha256": digest(evaluator.plan)})
        if (directory / "draft.json").exists():
            return CandidateDraft.from_dict(read_record(directory / "draft.json"))
        state = deepcopy(decode_json(base.state_json)); archive = state["archive"]
        public_rows = {}

        def with_files(files, current_state):
            version = replace_component(base.version, "policy", {"source": files["policy.py"]})
            version = replace_component(version, "meta_agent", {"source": files["meta_agent.py"]})
            return replace(base, version=version, state_json=json_text(current_state))

        # Re-evaluate under the current public plan. Historical scores never qualify
        # a node on a different suite; every job uses the normal resource ledger.
        for key, row in archive.items():
            trial_state = {**state, "active_id": key}
            measured = evaluator.evaluate(with_files(row["files"], trial_state), operation_id + "/archive/" + key)
            row["score"] = task_fitness(measured, binding)
            public_rows[key] = measured
        parent = native_parent(archive, state["seed"] + state["epoch"])
        execution = runtime.edit(directory / "worker", archive[parent]["files"],
            {"contract": base.version.component("task_contract")["text"], "evaluation": public_rows[parent]},
            self.provider, operation_id, iterations_left=1)
        files, error = execution["files"], None
        try:
            validate_program(files["policy.py"])
            compile(files["meta_agent.py"], "meta_agent.py", "exec")
        except (ValidationError, SyntaxError) as exc:
            error = str(exc)
        child = "node-" + digest({"operation": operation_id, "files": files})[:24]
        proposed_public = None
        native_valid = False
        before_archive = deepcopy(archive)
        if error is None:
            archive[child] = {"files": files, "parent_id": parent, "score": None, "valid_parent": True}
            trial = with_files(files, {**state, "active_id": child})
            proposed_public = evaluator.evaluate(trial, operation_id + "/child")
            # Native run_eval + successful domain score, with independent platform gate.
            native_valid = bool(proposed_public["complete"] and proposed_public.get("score") is not None)
            archive[child]["score"] = task_fitness(proposed_public, binding)
        accepted = bool(native_valid and task_candidate_valid(proposed_public, binding))
        if not accepted:
            candidate, selected = base, state["active_id"]
        else:
            selected = max(archive, key=lambda key: (archive[key]["score"], list(archive).index(key)))
            state.update(epoch=state["epoch"] + 1, active_id=selected)
            candidate = with_files(archive[selected]["files"], state)
        evidence = {"optimizer": "HyperAgents.MetaAgent+native_tool_loop+select_next_parent",
            "upstream_revision": REVISION, "profile": "bounded_single_task_self_modifying_meta",
            "parent_id": parent, "inspiration_ids": [], "archive_before": before_archive,
            "archive_after": decode_json(candidate.state_json)["archive"],
            "proposed_source": files["policy.py"], "proposed_meta_source": files["meta_agent.py"],
            "public_candidate": proposed_public, "public_archive": public_rows,
            "native_admitted": native_valid, "platform_accepted": accepted,
            "extra_gate": "bound_public_evaluation", "public_valid": accepted,
            "selected_id": selected, "candidate_error": error,
            "meta_changed_from_parent": files["meta_agent.py"] != before_archive[parent]["files"]["meta_agent.py"],
            "model_receipts": execution["model_receipts"], "worker_requests": execution["requests"],
            "worker_wall_ms": execution["wall_ms"], "worker_exit_code": execution["worker_exit_code"],
            "active_source_changed": candidate.version.component("policy") != base.version.component("policy"),
            "state_changed": candidate != base}
        draft = CandidateDraft(base.sha256, candidate, native_valid, json_text(evidence), self.provider.origin)
        save_record(directory / "draft.json", draft)
        return draft
