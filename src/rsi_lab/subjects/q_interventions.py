"""Pure q interventions for repeated cost-authorization continuations.

Restoration reinstates the complete old selected context in its old order. It
does not infer which experience caused a downstream outcome. The new ordering
of unselected entries is retained, so a q object can differ while the editor's
actual input is identical. No program, memory entry, budget or state is edited.
"""
from dataclasses import dataclass, replace

from rsi_lab.api import SystemSnapshot
from rsi_lab.errors import ValidationError
from rsi_lab.serialization import canonical_bytes, digest
from rsi_lab.subjects.cost_q import ExperienceSelector, REVISION, SYSTEM_ID
from rsi_lab.subjects.platform_fixture import replace_component
from rsi_lab.tasks.cost_program import validate_program


@dataclass(frozen=True)
class QInterventions:
    arms: dict[str, SystemSnapshot]
    metadata: dict


def build_q_interventions(base: SystemSnapshot, candidate: SystemSnapshot) -> QInterventions:
    """Return old/new/restore/sham snapshots without mutating either input.

    ``sham`` is exactly ``old``; only the runner may give it independent model
    requests. Native validity and natural/control provenance remain properties
    of the original proposal, not inferred by this helper. A no-op is permitted
    and identified explicitly, including changes confined to the unselected tail.
    """
    if type(base) is not SystemSnapshot or type(candidate) is not SystemSnapshot:
        raise ValidationError("q interventions require typed system snapshots")
    if (base.version.system_id != SYSTEM_ID or base.version.upstream_revision != REVISION):
        raise ValidationError("q intervention requires the cost q source contract")
    old_component = next((c for c in base.version.components if c.name == "experience_selector"), None)
    new_component = next((c for c in candidate.version.components if c.name == "experience_selector"), None)
    if old_component is None or new_component is None:
        raise ValidationError("q intervention requires both experience selectors")
    if old_component.semantic_role != "q" or new_component.semantic_role != "q":
        raise ValidationError("Experience selector must retain the q semantic role")
    # Removing the one allowed content change must reproduce the whole base,
    # including source, memory, state, component metadata/order and revision.
    reverted = replace(candidate, version=replace(candidate.version, components=tuple(
        replace(c, content_json=old_component.content_json) if c.name == "experience_selector" else c
        for c in candidate.version.components)))
    if reverted != base:
        raise ValidationError("q intervention cannot include non-q or inherited-state changes")
    old = ExperienceSelector.from_dict(base.version.component("experience_selector"))
    new = ExperienceSelector.from_dict(candidate.version.component("experience_selector"))
    if (old.limit, old.max_context_bytes) != (new.limit, new.max_context_bytes):
        raise ValidationError("q restoration cannot alter the registered context budget")
    memory = base.version.component("experience")
    policy = base.version.component("policy")
    if type(policy) is not dict or set(policy) != {"source"}:
        raise ValidationError("q restoration requires an unchanged source policy")
    validate_program(policy["source"])
    selected_old, selected_new = old.select(memory), new.select(memory)
    old_ids = tuple(entry["id"] for entry in selected_old)
    new_ids = tuple(entry["id"] for entry in selected_new)
    # Shared entries remain selected; all displaced old entries return. Since
    # the entry count is fixed, no new-only selected entry can remain as well.
    restore_order = (*old_ids, *(key for key in new.order if key not in old_ids))
    selector = replace(new, order=restore_order)
    selected_restore = selector.select(memory)
    restored = replace(candidate, version=replace_component(candidate.version, "experience_selector", selector))
    arms = {"old": base, "new": candidate, "restore": restored, "sham": base}
    selections = {"old": selected_old, "new": selected_new,
                  "restore": selected_restore, "sham": selected_old}
    return QInterventions(arms, {
        "schema_version": 1,
        "intervention": "restore_all_previous_selected_context_in_original_order",
        "restoration_scope": "complete_selected_context;not_an_isolated_single_entry_causal_claim",
        "selected_ids": {arm: [entry["id"] for entry in entries] for arm, entries in selections.items()},
        "selected_sha256": {arm: digest(entries) for arm, entries in selections.items()},
        "selected_bytes": {arm: len(canonical_bytes(entries)) for arm, entries in selections.items()},
        "snapshot_sha256": {arm: snapshot.sha256 for arm, snapshot in arms.items()},
        "q_sha256": {arm: digest(snapshot.version.component("experience_selector")) for arm, snapshot in arms.items()},
        "source_sha256": digest(policy["source"]),
        "memory_sha256": digest(memory),
        "state_sha256": digest(base.state_json),
        "shared_ids": [key for key in old_ids if key in new_ids],
        "restored_ids": [key for key in old_ids if key not in new_ids],
        "displaced_ids": [key for key in new_ids if key not in old_ids],
        "q_changed": old != new,
        "context_changed": selected_old != selected_new,
        "order_only_context_change": set(old_ids) == set(new_ids) and old_ids != new_ids,
        "q_noop": old == new,
        "restore_noop": selector == new,
        "full_context_reversion": selected_restore == selected_old,
        "restore_matches_old_q": selector == old,
        "sham_exact_copy": arms["sham"] == base,
        "entry_limit": old.limit,
        "max_context_bytes": old.max_context_bytes,
        "native_eligibility": "not_inferred;retain_original_candidate_check",
    })
