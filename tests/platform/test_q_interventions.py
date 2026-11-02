from dataclasses import replace

import pytest

from rsi_lab.errors import ValidationError
from rsi_lab.serialization import digest
from rsi_lab.subjects.cost_q import (ExperienceSelector, control_memory, control_selector,
                                    make_initial_snapshot)
from rsi_lab.subjects.platform_fixture import json_text, replace_component
from rsi_lab.subjects.q_interventions import build_q_interventions
from rsi_lab.tasks.cost_authorization import SAFE_REFERENCE_SOURCE


def pair(selector=None):
    base = make_initial_snapshot(701, control_memory(), control_selector("old"))
    candidate = replace(base, version=replace_component(base.version, "experience_selector",
                        selector or control_selector("harmful")))
    return base, candidate


def test_restore_keeps_shared_context_and_reinstates_old_entries_without_source_repair():
    base, candidate = pair()
    before = base.sha256, candidate.sha256
    result = build_q_interventions(base, candidate)
    assert result.arms["old"] is base and result.arms["new"] is candidate
    assert result.arms["sham"] is base
    restored = result.arms["restore"]
    assert restored.version.component("experience_selector") == base.version.component("experience_selector")
    for name in ("policy", "experience"):
        assert restored.version.component(name) == base.version.component(name)
    assert restored.state_json == base.state_json
    metadata = result.metadata
    assert metadata["restored_ids"] == ["current_grant"]
    assert metadata["displaced_ids"] == ["cache_epoch"]
    assert metadata["shared_ids"] == ["public_cost"]
    assert metadata["full_context_reversion"] and metadata["sham_exact_copy"]
    assert metadata["q_changed"] and metadata["context_changed"]
    assert not metadata["restore_noop"]
    assert metadata["selected_sha256"]["old"] == metadata["selected_sha256"]["restore"]
    assert metadata["selected_sha256"]["old"] != metadata["selected_sha256"]["new"]
    assert (base.sha256, candidate.sha256) == before


def test_restore_reverts_actual_context_but_preserves_new_unselected_order():
    selector = ExperienceSelector(("public_contract", "cache_epoch", "public_cost", "current_grant"))
    base, candidate = pair(selector)
    result = build_q_interventions(base, candidate)
    restored = result.arms["restore"].version.component("experience_selector")
    assert restored["order"] == ["current_grant", "public_cost", "public_contract", "cache_epoch"]
    assert result.metadata["full_context_reversion"]
    assert not result.metadata["restore_matches_old_q"]
    assert result.metadata["restoration_scope"].startswith("complete_selected_context;")


def test_order_only_change_is_restored_and_disclosed():
    base, candidate = pair(ExperienceSelector(("public_cost", "current_grant", "cache_epoch", "public_contract")))
    result = build_q_interventions(base, candidate)
    assert result.metadata["order_only_context_change"]
    assert result.metadata["context_changed"]
    assert result.metadata["restored_ids"] == result.metadata["displaced_ids"] == []
    assert result.arms["restore"] == base


def test_tail_only_change_remains_a_context_noop():
    base, candidate = pair(ExperienceSelector(("current_grant", "public_cost", "public_contract", "cache_epoch")))
    result = build_q_interventions(base, candidate)
    assert result.metadata["q_changed"] and not result.metadata["context_changed"]
    assert result.metadata["restore_noop"]
    assert result.arms["restore"] == candidate
    assert not result.metadata["restore_matches_old_q"]


def test_noop_and_invalid_proposal_fallback_are_not_relabelled_as_changes():
    base, _ = pair()
    result = build_q_interventions(base, base)
    assert all(snapshot == base for snapshot in result.arms.values())
    assert result.metadata["q_noop"] and result.metadata["restore_noop"]
    assert not result.metadata["q_changed"] and not result.metadata["context_changed"]
    assert result.metadata["native_eligibility"].startswith("not_inferred;")


@pytest.mark.parametrize("change", ["source", "memory", "state", "revision", "role", "extra_component"])
def test_rejects_non_q_changes_even_if_the_selector_itself_is_valid(change):
    from rsi_lab.api import Component
    base, candidate = pair()
    if change == "source":
        candidate = replace(candidate, version=replace_component(candidate.version, "policy", {"source": SAFE_REFERENCE_SOURCE}))
    elif change == "memory":
        memory = candidate.version.component("experience")
        memory["public_cost"]["text"] += " Added advice."
        candidate = replace(candidate, version=replace_component(candidate.version, "experience", memory))
    elif change == "state":
        candidate = replace(candidate, state_json=json_text({"root_seed": 701, "generation": 1, "public_feedback": []}))
    elif change == "revision":
        candidate = replace(candidate, version=replace(candidate.version, upstream_revision="different"))
    elif change == "role":
        candidate = replace(candidate, version=replace(candidate.version, components=tuple(
            replace(c, semantic_role="memory") if c.name == "experience_selector" else c for c in candidate.version.components)))
    else:
        candidate = replace(candidate, version=replace(candidate.version, components=(*candidate.version.components, Component("extra", "{}"))))
    with pytest.raises(ValidationError):
        build_q_interventions(base, candidate)


@pytest.mark.parametrize("selector", [replace(control_selector("harmful"), limit=3),
                                    replace(control_selector("harmful"), max_context_bytes=8192)])
def test_restoration_cannot_change_context_budget(selector):
    with pytest.raises(ValidationError, match="budget"):
        build_q_interventions(*pair(selector))


def test_missing_archive_identifier_and_missing_component_are_rejected():
    base, candidate = pair(ExperienceSelector(("unknown", "public_cost", "cache_epoch", "public_contract")))
    with pytest.raises(ValidationError, match="complete public archive"):
        build_q_interventions(base, candidate)
    incomplete = replace(base, version=replace(base.version, components=tuple(
        c for c in base.version.components if c.name != "experience_selector")))
    with pytest.raises(ValidationError, match="both experience selectors"):
        build_q_interventions(incomplete, incomplete)


def test_natural_archive_is_not_rewritten_or_relabelled():
    memory = {key: {**entry, "origin": "public_execution"} for key, entry in control_memory().items()}
    base = make_initial_snapshot(702, memory, control_selector("old"))
    candidate = replace(base, version=replace_component(base.version, "experience_selector", control_selector("harmful")))
    result = build_q_interventions(base, candidate)
    assert result.metadata["memory_sha256"] == digest(memory)
    assert all(snapshot.version.component("experience") == memory for snapshot in result.arms.values())
    assert all(snapshot.state_json == base.state_json for snapshot in result.arms.values())
    assert all(snapshot.version.component("experience_selector")["limit"] == 2 for snapshot in result.arms.values())
