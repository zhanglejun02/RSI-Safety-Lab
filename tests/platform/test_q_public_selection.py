from dataclasses import replace

import pytest

from rsi_lab.contracts import CursorFamily, Partition
from rsi_lab.errors import ValidationError
from rsi_lab.serialization import digest
from rsi_lab.subjects.cost_q import ExperienceSelector, make_initial_snapshot
from rsi_lab.subjects.platform_fixture import json_text, replace_component
from rsi_lab.subjects.q_public_selection import (
    DevelopmentInterventionBasis, PublicQCandidate, PublicQPlan, PublicQSelection,
    PublicQTrial, build_targeted_interventions, public_trial_from_evaluations,
    select_public_q,
)
from rsi_lab.tasks.platform_cost_authorization import CostAuthorizationTaskPackage


def plan():
    return PublicQPlan("root-public", digest("checkpoint"),
        (PublicQCandidate("new", digest("new-q")), PublicQCandidate("old", digest("old-q"))),
        "old", 2, 2, 2, digest("public-task-plan"))


def trial(search, candidate, rep, after=80.0, *, success=1, correct=True):
    q = next(c.q_sha256 for c in search.candidates if c.candidate_id == candidate)
    return PublicQTrial(digest(search), candidate, q, rep, search.root_id,
        search.checkpoint_sha256, search.public_plan_sha256, True, correct, 100.0, after,
        2, success, 2, digest((candidate, rep, "evaluation")), digest((candidate, rep, "trajectory")))


def grid(search, *, new_cost=80.0, old_cost=80.0):
    return tuple(trial(search, candidate, rep, new_cost if candidate == "new" else old_cost)
                 for candidate in ("old", "new") for rep in range(search.replicates))


def test_selection_requires_actual_public_gain_and_preserves_binding():
    search = plan()
    rows = grid(search, new_cost=60.0)
    result = select_public_q(search, rows)
    assert result.disposition == "selected" and result.changed
    assert result.selected_candidate_id == "new"
    assert result.candidate_scores["new"]["mean_fractional_cost_gain"] == 0.4
    assert result.candidate_scores["new"]["successful_update_rate"] == 0.5
    assert result.plan_sha256 == digest(search)
    assert PublicQSelection.from_dict(result.to_dict()) == result


def test_equal_scores_keep_old_even_when_new_registered_first():
    search = plan()
    result = select_public_q(search, grid(search))
    assert result.disposition == "retain_baseline" and not result.changed
    assert result.selected_candidate_id == "old"


def test_cost_has_priority_over_success_rate_after_correctness():
    search = plan()
    rows = tuple(replace(row, successful_updates=0 if row.candidate_id == "new" else 2)
                 for row in grid(search, new_cost=60.0))
    assert select_public_q(search, rows).selected_candidate_id == "new"


def test_success_breaks_cost_tie_but_wrong_cheaper_program_never_wins():
    search = plan()
    rows = tuple(replace(row, successful_updates=2) if row.candidate_id == "new" else row for row in grid(search))
    assert select_public_q(search, rows).selected_candidate_id == "new"
    rows = tuple(replace(row, after_cost=1.0, public_correct=False) if row.candidate_id == "new" else row for row in rows)
    result = select_public_q(search, rows)
    assert result.selected_candidate_id == "old"
    assert result.candidate_scores["new"]["mean_fractional_cost_gain"] is None
    assert not result.candidate_scores["new"]["eligible"]


def test_unknown_trial_blocks_selection_instead_of_becoming_zero_gain_or_safe():
    search = plan()
    rows = list(grid(search, new_cost=60.0))
    rows[0] = replace(rows[0], complete=False, public_correct=None, after_cost=None,
                      attempted_updates=1, successful_updates=0, editor_calls=1)
    result = select_public_q(search, rows)
    assert result.disposition == "inconclusive" and result.selected_candidate_id == "old"
    assert result.candidate_scores["old"]["mean_fractional_cost_gain"] is None
    assert result.candidate_scores["old"]["mean_after_cost"] is None
    assert result.candidate_scores["new"]["eligible"]


@pytest.mark.parametrize("fault", ["absent", "duplicate", "checkpoint", "root", "q", "task", "budget", "short", "baseline_cost"])
def test_mismatched_search_or_missing_slot_cannot_select_q(fault):
    search = plan()
    rows = list(grid(search))
    if fault == "absent":
        rows.pop()
    elif fault == "duplicate":
        rows.append(rows[0])
    else:
        values = {"checkpoint": {"checkpoint_sha256": digest("other")},
                  "root": {"root_id": "other-root"}, "q": {"q_sha256": digest("other")},
                  "task": {"public_plan_sha256": digest("other")}, "budget": {"editor_calls": 3},
                  "short": {"attempted_updates": 1}, "baseline_cost": {"before_cost": 101.0}}
        rows[-1] = replace(rows[-1], **values[fault])
    with pytest.raises(ValidationError):
        select_public_q(search, rows)


@pytest.mark.parametrize("field,value", [("partition", Partition.SEALED), ("partition", Partition.TRAIN),
    ("family", CursorFamily.INDEPENDENT_EVALUATION), ("family", CursorFamily.ADMISSION_PROBE)])
def test_search_and_receipts_reject_non_development_data(field, value):
    search = plan()
    with pytest.raises(ValidationError, match="public development"):
        replace(search, **{field: value})
    with pytest.raises(ValidationError, match="public development"):
        replace(grid(search)[0], **{field: value})


def test_duplicate_q_candidate_is_not_reported_as_an_update():
    search = plan()
    search = replace(search, candidates=(replace(search.candidates[0], q_sha256=search.candidates[1].q_sha256),
                                         search.candidates[1]))
    result = select_public_q(search, grid(search, new_cost=50.0))
    assert result.selected_candidate_id == "old" and not result.changed


def evaluation(cost=100.0, complete=True):
    trajectories = [{"public_request": "example", "actual_calls": ["ask", "execute"]}]
    return {"complete": complete, "risk": 0 if complete else None, "legal_completed": 1,
            "legal_total": 1, "feedback": {"complete_correct": complete},
            "synthetic_units": cost, "public_trajectories": trajectories,
            "public_trajectories_sha256": digest(trajectories)}


def test_factory_binds_actual_public_plan_and_full_evidence_hashes():
    task_plan = CostAuthorizationTaskPackage().public_suite(19001, Partition.DEV)
    search = replace(plan(), public_plan_sha256=digest(task_plan))
    before, after = evaluation(), evaluation(50.0)
    result = public_trial_from_evaluations(search, "new", 0, evaluation_plan=task_plan,
        before=before, after=after, attempted_updates=2, successful_updates=1, editor_calls=2)
    assert result.after_cost == 50.0 and result.public_correct
    assert result.evaluator_evidence_sha256 == digest({"before": before, "after": after})
    after["public_trajectories"].append({"forged": True})
    with pytest.raises(ValidationError, match="trajectory digest"):
        public_trial_from_evaluations(search, "new", 0, evaluation_plan=task_plan,
            before=before, after=after, attempted_updates=2, successful_updates=1, editor_calls=2)


def test_factory_preserves_missing_cost_and_rejects_wrong_task_family():
    tasks = CostAuthorizationTaskPackage()
    task_plan = tasks.public_suite(19002, Partition.DEV)
    search = replace(plan(), public_plan_sha256=digest(task_plan))
    row = public_trial_from_evaluations(search, "new", 0, evaluation_plan=task_plan,
        before=evaluation(), after=evaluation(None, False), attempted_updates=1, successful_updates=0, editor_calls=1)
    assert not row.complete and row.after_cost is None and row.public_correct is None
    with pytest.raises(ValidationError, match="public development"):
        public_trial_from_evaluations(search, "new", 0,
            evaluation_plan=tasks.plan(19002, CursorFamily.INDEPENDENT_EVALUATION),
            before=evaluation(), after=evaluation(), attempted_updates=2, successful_updates=0, editor_calls=2)


def intervention_pair(relevant="Version changed: 001.", placebo="Routine reduced: 001."):
    texts = {"relevant": relevant, "placeboo": placebo, "shared00": "Shared public optimization record.",
             "current0": "Another currently selected record.", "new_only": "An additional new experience."}
    memory = {key: {"text": text, "origin": "public_execution", "evidence_sha256": digest({"public": key, "text": text})}
              for key, text in texts.items()}
    base = make_initial_snapshot(19003, memory,
        ExperienceSelector(("relevant", "shared00", "placeboo", "current0", "new_only")))
    new = replace(base, version=replace_component(base.version, "experience_selector",
        ExperienceSelector(("current0", "new_only", "placeboo", "shared00", "relevant"))))
    basis = DevelopmentInterventionBasis("root-public", base.sha256, "relevant", "placeboo",
        memory["relevant"]["evidence_sha256"], memory["placeboo"]["evidence_sha256"], digest("development-selection"))
    return base, new, basis


def test_targeted_restore_changes_only_one_selected_slot_not_all_old_context():
    base, new, basis = intervention_pair()
    result = build_targeted_interventions(base, new, "relevant", "placeboo", 0, development_evidence=basis)
    assert result.arms["sham"] is new
    assert result.metadata["selected_ids"]["restore"] == ["relevant", "new_only"]
    assert result.metadata["selected_ids"]["placebo"] == ["placeboo", "new_only"]
    assert result.metadata["selected_ids"]["old"] == ["relevant", "shared00"]
    assert not result.metadata["restore_equals_old_context"]
    assert result.metadata["selected_bytes"]["restore"] == result.metadata["selected_bytes"]["placebo"]
    for snapshot in result.arms.values():
        assert snapshot.state_json == base.state_json
        assert snapshot.version.component("policy") == base.version.component("policy")
        assert snapshot.version.component("experience") == base.version.component("experience")
        assert snapshot.version.component("experience_selector")["max_context_bytes"] == 4096
    restored_order = result.arms["restore"].version.component("experience_selector")["order"]
    assert restored_order == ["relevant", "new_only", "placeboo", "shared00", "current0"]


@pytest.mark.parametrize("fault", ["basis", "evidence", "not_selected", "already_selected", "slot", "state", "memory"])
def test_targeted_intervention_rejects_confounds_and_posthoc_evidence(fault):
    base, new, basis = intervention_pair()
    relevant, placebo, slot = "relevant", "placeboo", 0
    if fault == "basis":
        basis = replace(basis, checkpoint_sha256=digest("other"))
    elif fault == "evidence":
        basis = replace(basis, relevant_evidence_sha256=digest("other"))
    elif fault == "not_selected":
        relevant = "shared00"  # selected by old but wrong evidence identity
    elif fault == "already_selected":
        new = replace(new, version=replace_component(new.version, "experience_selector",
            ExperienceSelector(("relevant", "new_only", "placeboo", "shared00", "current0"))))
    elif fault == "slot":
        slot = 2
    elif fault == "state":
        new = replace(new, state_json=json_text({"different": "checkpoint"}))
    else:
        memory = new.version.component("experience")
        memory["shared00"]["text"] += " new"
        new = replace(new, version=replace_component(new.version, "experience", memory))
    with pytest.raises(ValidationError):
        build_targeted_interventions(base, new, relevant, placebo, slot, development_evidence=basis)


def test_matching_checks_serialized_bytes_not_only_text_characters():
    base, new, basis = intervention_pair('a"', "bb")
    with pytest.raises(ValidationError, match="serialized contexts"):
        build_targeted_interventions(base, new, "relevant", "placeboo", 0, development_evidence=basis)
    base, new, basis = intervention_pair("中文", "ab")
    with pytest.raises(ValidationError, match="serialized contexts"):
        build_targeted_interventions(base, new, "relevant", "placeboo", 0, development_evidence=basis)


def test_targeted_restore_accepts_archive_length_matching_with_different_json_escaping():
    from rsi_lab.subjects.optimization_memory import match_entry_lengths
    base, new, _ = intervention_pair()
    memory = base.version.component("experience")
    # These are structured episode projections. Quote/backslash density differs,
    # so equal serialized entry bytes do not imply equal raw text bytes.
    memory["relevant"]["text"] = json_text({"source_excerpt": 'cached["epoch"] = "a\\b"'})
    memory["placeboo"]["text"] = json_text({"source_excerpt": "ordinary cost reduction"})
    matched, matching = match_entry_lengths(memory, ("relevant", "placeboo"))
    assert len(matched["relevant"]["text"].encode()) != len(matched["placeboo"]["text"].encode())
    base = replace(base, version=replace_component(base.version, "experience", matched))
    new = replace(new, version=replace_component(new.version, "experience", matched))
    frozen_sha = digest(matched)
    basis = DevelopmentInterventionBasis("root-public", base.sha256, "relevant", "placeboo",
        matched["relevant"]["evidence_sha256"], matched["placeboo"]["evidence_sha256"], digest(matching))
    result = build_targeted_interventions(base, new, "relevant", "placeboo", 0, development_evidence=basis)
    assert result.metadata["selected_bytes"]["restore"] == result.metadata["selected_bytes"]["placebo"]
    assert result.metadata["raw_text_bytes"]["relevant"] != result.metadata["raw_text_bytes"]["placebo"]
    assert result.metadata["model_tokens_matched"] is None
    assert result.metadata["selected_ids"]["restore"] == ["relevant", "new_only"]
    assert result.metadata["selected_ids"]["placebo"] == ["placeboo", "new_only"]
    for snapshot in result.arms.values():
        assert digest(snapshot.version.component("experience")) == frozen_sha


def test_targeted_basis_cannot_be_independent_or_sealed():
    _, _, basis = intervention_pair()
    with pytest.raises(ValidationError, match="public development"):
        replace(basis, partition=Partition.SEALED)
    with pytest.raises(ValidationError, match="public development"):
        replace(basis, family=CursorFamily.INDEPENDENT_EVALUATION)
