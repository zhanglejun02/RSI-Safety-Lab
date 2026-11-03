"""Public q selection and one-entry interventions, without model or oracle I/O.

These contracts bind already executed public trials. Hashes do not establish
truth by themselves: the runner must retain the corresponding source/evaluator
receipts. Independent evaluation cannot be used to rank q or select a lesson.
"""
from dataclasses import dataclass, replace
from statistics import mean
import math

from rsi_lab.api import SystemSnapshot
from rsi_lab.contracts import CursorFamily, Partition, StrictRecord, require_count, require_id
from rsi_lab.errors import ValidationError
from rsi_lab.learning.contracts import require_sha
from rsi_lab.serialization import canonical_bytes, digest
from .cost_q import ExperienceSelector, native_valid
from rsi_lab.api import replace_component
from .q_interventions import QInterventions, build_q_interventions


def _development(partition, family):
    if partition != Partition.DEV or family != CursorFamily.PUBLIC_DEVELOPMENT:
        raise ValidationError("q optimization and intervention selection require public development evidence")


@dataclass(frozen=True)
class PublicQCandidate(StrictRecord):
    candidate_id: str
    q_sha256: str
    schema_version: int = 1

    def validate(self):
        require_id(self.candidate_id)
        require_sha(self.q_sha256)


@dataclass(frozen=True)
class PublicQPlan(StrictRecord):
    root_id: str
    checkpoint_sha256: str
    candidates: tuple[PublicQCandidate, ...]
    baseline_id: str
    replicates: int
    attempt_budget: int
    editor_call_budget: int
    public_plan_sha256: str
    partition: Partition = Partition.DEV
    family: CursorFamily = CursorFamily.PUBLIC_DEVELOPMENT
    schema_version: int = 1

    def validate(self):
        _development(self.partition, self.family)
        require_id(self.root_id)
        require_id(self.baseline_id)
        require_sha(self.checkpoint_sha256)
        require_sha(self.public_plan_sha256)
        names = [candidate.candidate_id for candidate in self.candidates]
        if not 2 <= len(names) <= 8 or len(set(names)) != len(names) or self.baseline_id not in names:
            raise ValidationError("Register2..8 q candidates with distinct IDs and an explicit baseline")
        for name in ("replicates", "attempt_budget", "editor_call_budget"):
            require_count(getattr(self, name), name, 1)
        if self.replicates > 20 or self.attempt_budget > 100 or self.editor_call_budget > 100:
            raise ValidationError("Public q search exceeds its bounded trial contract")


@dataclass(frozen=True)
class PublicQTrial(StrictRecord):
    plan_sha256: str
    candidate_id: str
    q_sha256: str
    replicate: int
    root_id: str
    checkpoint_sha256: str
    public_plan_sha256: str
    complete: bool
    public_correct: bool | None
    before_cost: float | None
    after_cost: float | None
    attempted_updates: int
    successful_updates: int
    editor_calls: int
    evaluator_evidence_sha256: str
    trajectory_sha256: str
    partition: Partition = Partition.DEV
    family: CursorFamily = CursorFamily.PUBLIC_DEVELOPMENT
    schema_version: int = 1

    def validate(self):
        _development(self.partition, self.family)
        for name in ("plan_sha256", "q_sha256", "checkpoint_sha256", "public_plan_sha256",
                     "evaluator_evidence_sha256", "trajectory_sha256"):
            require_sha(getattr(self, name))
        require_id(self.root_id)
        require_id(self.candidate_id)
        for name in ("replicate", "attempted_updates", "successful_updates", "editor_calls"):
            require_count(getattr(self, name), name)
        if self.successful_updates > self.attempted_updates:
            raise ValidationError("Successful updates cannot exceed actual attempts")
        for value in (self.before_cost, self.after_cost):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValidationError("Measured public cost must be finite and nonnegative")
        if self.complete and (self.public_correct is None or self.before_cost is None or self.after_cost is None):
            raise ValidationError("A completed public trial requires measured correctness and costs")
        if self.before_cost == 0:
            raise ValidationError("The public optimization baseline must have positive measured cost")


def public_trial_from_evaluations(plan, candidate_id, replicate, *, evaluation_plan,
                                  before, after, attempted_updates, successful_updates, editor_calls):
    """Project actual PublicTaskEvaluator outputs into a public-only receipt.

    The caller retains both complete evaluator objects and the task plan. The
    trial is incomplete if either execution is incomplete; unknown costs are not
    replaced with zero. Correctness includes valid denials via ``native_valid``.
    """
    if type(plan) is not PublicQPlan:
        raise ValidationError("Expected a typed public q trial plan")
    _development(evaluation_plan.partition, evaluation_plan.family)
    if digest(evaluation_plan) != plan.public_plan_sha256:
        raise ValidationError("Public evaluator task plan differs from the registered q search")
    candidates = {candidate.candidate_id: candidate for candidate in plan.candidates}
    if candidate_id not in candidates:
        raise ValidationError("Unregistered q candidate")
    if type(before) is not dict or type(after) is not dict:
        raise ValidationError("Expected actual public evaluator objects")
    for evidence in (before, after):
        if type(evidence.get("complete")) is not bool:
            raise ValidationError("Public evaluator completeness is required")
        require_sha(evidence.get("public_trajectories_sha256", ""))
        if digest(evidence.get("public_trajectories", [])) != evidence["public_trajectories_sha256"]:
            raise ValidationError("Public trajectory digest differs from its evidence")
    complete = before["complete"] and after["complete"]
    if before["complete"] and not native_valid(before):
        raise ValidationError("q selection requires a correct public checkpoint before optimization")
    def cost(evidence):
        value = evidence.get("synthetic_units")
        if value is None:
            return None
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValidationError("Invalid measured public evaluator cost")
        return float(value)
    return PublicQTrial(digest(plan), candidate_id, candidates[candidate_id].q_sha256,
        replicate, plan.root_id, plan.checkpoint_sha256, plan.public_plan_sha256,
        complete, native_valid(after) if complete else None, cost(before), cost(after),
        attempted_updates, successful_updates, editor_calls,
        digest({"before": before, "after": after}),
        digest({"before": before["public_trajectories_sha256"], "after": after["public_trajectories_sha256"]}))


@dataclass(frozen=True)
class PublicQSelection(StrictRecord):
    plan_sha256: str
    evidence_sha256: str
    disposition: str
    selected_candidate_id: str
    selected_q_sha256: str
    changed: bool
    reason: str
    candidate_scores: dict
    schema_version: int = 1

    def validate(self):
        require_sha(self.plan_sha256)
        require_sha(self.evidence_sha256)
        require_sha(self.selected_q_sha256)
        require_id(self.selected_candidate_id)
        if self.disposition not in ("selected", "retain_baseline", "inconclusive"):
            raise ValidationError("Unknown public q selection disposition")
        if self.disposition != "selected" and self.changed:
            raise ValidationError("An incomplete/retained q cannot be declared changed")
        canonical_bytes(self.candidate_scores)


def select_public_q(plan, trials):
    """Select using public results only; fail closed on an incomplete search.

    Candidate ranking is prespecified: all trials publicly correct, then mean
    fractional cost improvement, then successful-update fraction. All-incorrect
    candidates are ineligible. Equal rankings retain the old q; other ties use
    the registered order. Actual costs and calls remain separate measurements.
    """
    trials = tuple(trials)
    if type(plan) is not PublicQPlan or any(type(row) is not PublicQTrial for row in trials):
        raise ValidationError("Expected typed public q plan and trial receipts")
    candidates = {candidate.candidate_id: candidate for candidate in plan.candidates}
    grid = {}
    for trial in trials:
        if (trial.plan_sha256 != digest(plan) or trial.root_id != plan.root_id or
                trial.checkpoint_sha256 != plan.checkpoint_sha256 or
                trial.public_plan_sha256 != plan.public_plan_sha256 or
                trial.candidate_id not in candidates or
                trial.q_sha256 != candidates[trial.candidate_id].q_sha256 or
                not 0 <= trial.replicate < plan.replicates):
            raise ValidationError("Public q receipt is not bound to this registered search")
        if (trial.attempted_updates > plan.attempt_budget or trial.editor_calls > plan.editor_call_budget
                or trial.complete and trial.attempted_updates != plan.attempt_budget):
            raise ValidationError("q candidates must use the registered attempt/call allowance")
        key = trial.candidate_id, trial.replicate
        if key in grid:
            raise ValidationError("Duplicate q trial receipt")
        grid[key] = trial
    expected = {(candidate_id, replicate) for candidate_id in candidates for replicate in range(plan.replicates)}
    if set(grid) != expected:
        raise ValidationError("Every registered candidate and repetition requires an explicit trial, including failures")
    # Matching task plan and checkpoint imply matching measured baseline work in
    # each repeat; differing before costs expose a hidden task/source confound.
    for rep in range(plan.replicates):
        before = {grid[(candidate_id, rep)].before_cost for candidate_id in candidates
                  if grid[(candidate_id, rep)].before_cost is not None}
        if len(before) > 1:
            raise ValidationError("q trials do not share the measured public checkpoint cost")
    scores = {}
    for candidate_id in candidates:
        rows = [grid[(candidate_id, rep)] for rep in range(plan.replicates)]
        complete = all(row.complete for row in rows)
        correct = complete and all(row.public_correct for row in rows)
        scores[candidate_id] = {
            "complete_replicates": sum(row.complete for row in rows),
            "registered_replicates": plan.replicates, "eligible": correct,
            "mean_fractional_cost_gain": mean((row.before_cost - row.after_cost) / row.before_cost for row in rows)
                if correct else None,
            "successful_update_rate": sum(row.successful_updates for row in rows) / (plan.attempt_budget * plan.replicates)
                if complete else None,
            "mean_after_cost": mean(row.after_cost for row in rows) if complete else None,
            "actual_editor_calls": sum(row.editor_calls for row in rows),
            "actual_attempts": sum(row.attempted_updates for row in rows)}
    baseline = candidates[plan.baseline_id]
    def result(disposition, candidate, reason):
        return PublicQSelection(digest(plan), digest(tuple(grid[key] for key in sorted(grid))),
            disposition, candidate.candidate_id, candidate.q_sha256,
            candidate.q_sha256 != baseline.q_sha256, reason, scores)
    if not all(row.complete for row in grid.values()):
        return result("inconclusive", baseline, "incomplete_public_search_keep_baseline_without_claim_of_winner")
    eligible = [candidate for candidate in plan.candidates if scores[candidate.candidate_id]["eligible"]]
    if not eligible:
        return result("retain_baseline", baseline, "no_publicly_correct_q_trial_candidate")
    def rank(candidate):
        score = scores[candidate.candidate_id]
        return score["mean_fractional_cost_gain"], score["successful_update_rate"]
    best_rank = max(map(rank, eligible))
    best = [candidate for candidate in eligible if rank(candidate) == best_rank]
    winner = next((candidate for candidate in best if candidate.candidate_id == plan.baseline_id), best[0])
    if winner.q_sha256 == baseline.q_sha256:
        return result("retain_baseline", baseline, "baseline_wins_tie_or_selected_q_is_unchanged")
    return result("selected", winner, "public_correctness_then_cost_gain_then_optimization_success")


@dataclass(frozen=True)
class DevelopmentInterventionBasis(StrictRecord):
    root_id: str
    checkpoint_sha256: str
    relevant_id: str
    placebo_id: str
    relevant_evidence_sha256: str
    placebo_evidence_sha256: str
    selection_evidence_sha256: str
    partition: Partition = Partition.DEV
    family: CursorFamily = CursorFamily.PUBLIC_DEVELOPMENT
    schema_version: int = 1

    def validate(self):
        _development(self.partition, self.family)
        for name in ("root_id", "relevant_id", "placebo_id"):
            require_id(getattr(self, name))
        for name in ("checkpoint_sha256", "relevant_evidence_sha256", "placebo_evidence_sha256",
                     "selection_evidence_sha256"):
            require_sha(getattr(self, name))
        if self.relevant_id == self.placebo_id:
            raise ValidationError("Relevant and placebo experiences must be different entries")


def build_targeted_interventions(base, new, relevant_id, placebo_id, slot_index, *, development_evidence):
    """Replace one selected slot of new q, preserving every other selected slot.

    Matching is enforced on actual serialized context and identifier bytes.
    Raw text sizes may differ because JSON escaping contributes to the model's
    serialized input. Padding/projection must already be in the frozen archive;
    this helper never edits memory to make a comparison fit. Tokens are unknown.
    """
    if type(development_evidence) is not DevelopmentInterventionBasis:
        raise ValidationError("A frozen public-development basis is required for targeted restoration")
    # Reuse existing strict whole-snapshot q-only validation, not its restoration.
    build_q_interventions(base, new)
    basis = development_evidence
    if (basis.checkpoint_sha256 != base.sha256 or basis.relevant_id != relevant_id
            or basis.placebo_id != placebo_id):
        raise ValidationError("Targeted intervention differs from its declared development basis")
    memory = base.version.component("experience")
    old_q = ExperienceSelector.from_dict(base.version.component("experience_selector"))
    new_q = ExperienceSelector.from_dict(new.version.component("experience_selector"))
    require_count(slot_index, "slot index")
    if slot_index >= new_q.limit:
        raise ValidationError("Targeted intervention slot must be selected")
    if relevant_id not in old_q.order[:old_q.limit]:
        raise ValidationError("Restoration must return an experience actually selected by old q")
    for name, expected in ((relevant_id, basis.relevant_evidence_sha256), (placebo_id, basis.placebo_evidence_sha256)):
        if name not in memory or name in new_q.order[:new_q.limit]:
            raise ValidationError("Replacement experience must exist and be absent from new selected context")
        if memory[name]["origin"] != "public_execution" or memory[name]["evidence_sha256"] != expected:
            raise ValidationError("Intervention experience is not bound to its public execution evidence")
    if len(relevant_id.encode()) != len(placebo_id.encode()):
        raise ValidationError("Relevant and placebo entry identifiers must match UTF-8 byte lengths")
    def substitute(entry_id):
        order = list(new_q.order)
        position = order.index(entry_id)
        order[slot_index], order[position] = order[position], order[slot_index]
        q = replace(new_q, order=tuple(order))
        snapshot = replace(new, version=replace_component(new.version, "experience_selector", q))
        return snapshot, q.select(memory)
    restored, relevant_context = substitute(relevant_id)
    placebo, placebo_context = substitute(placebo_id)
    if len(canonical_bytes(relevant_context)) != len(canonical_bytes(placebo_context)):
        raise ValidationError("JSON escaping/provenance differs: actual serialized contexts must match byte lengths")
    arms = {"old": base, "new": new, "restore": restored, "placebo": placebo, "sham": new}
    selections = {arm: ExperienceSelector.from_dict(snapshot.version.component("experience_selector")).select(memory)
                  for arm, snapshot in arms.items()}
    return QInterventions(arms, {"schema_version": 1,
        "intervention": "single_selected_slot_replacement", "slot_index": slot_index,
        "development_basis": basis.to_dict(), "development_basis_sha256": digest(basis),
        "relevant_id": relevant_id, "placebo_id": placebo_id, "displaced_id": new_q.order[slot_index],
        "relevant_vs_placebo_serialized_bytes_equal": True, "model_tokens_matched": None,
        "length_matching_measurement": "actual_canonical_selected_context_utf8_bytes",
        "raw_text_bytes": {"relevant": len(memory[relevant_id]["text"].encode()),
                           "placebo": len(memory[placebo_id]["text"].encode())},
        "raw_text_bytes_may_differ_due_to_json_escaping": True,
        "all_arms_context_budget_equal": True, "other_new_selected_slots_unchanged": True,
        "sham_exact_copy_of_new": arms["sham"] == new,
        "restore_equals_old_context": relevant_context == selections["old"],
        "selected_ids": {arm: [entry["id"] for entry in entries] for arm, entries in selections.items()},
        "selected_sha256": {arm: digest(entries) for arm, entries in selections.items()},
        "selected_bytes": {arm: len(canonical_bytes(entries)) for arm, entries in selections.items()},
        "q_sha256": {arm: digest(snapshot.version.component("experience_selector")) for arm, snapshot in arms.items()},
        "snapshot_sha256": {arm: snapshot.sha256 for arm, snapshot in arms.items()},
        "memory_sha256": digest(memory), "source_sha256": digest(base.version.component("policy")["source"]),
        "limits": "single_slot_context_intervention;requires_repeated_effect_evaluation;no_causal_result_implied"})
