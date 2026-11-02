"""Evidence-bound public optimization episodes and bounded model projections.

Full source, task plans and public execution evidence belong in the immutable
episode record. The memory text is a deterministic, explicitly truncated view;
no model interpretation or independent evaluation label becomes a fact here.
"""
from copy import deepcopy
from dataclasses import dataclass
from difflib import unified_diff
import math

from rsi_lab.contracts import CursorFamily, Partition, StrictRecord, require_id
from rsi_lab.errors import ValidationError
from rsi_lab.serialization import canonical_bytes, decode_json, digest
from rsi_lab.tasks.cost_program import validate_program
from rsi_lab.tasks.platform_cost_authorization import CostPlan

MAX_EXPERIENCE_BYTES = 8192
DEFAULT_PROJECTION_BYTES = 6144


def _sha(value):
    if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValidationError("Expected lowercase SHA256 provenance")


@dataclass(frozen=True)
class PublicOptimizationExecution(StrictRecord):
    source: str
    plan_json: str
    evaluation: dict
    source_provenance: dict
    schema_version: int = 1

    def validate(self):
        validate_program(self.source)
        plan = CostPlan.from_dict(decode_json(self.plan_json))
        if plan.family != CursorFamily.PUBLIC_DEVELOPMENT or plan.partition != Partition.DEV:
            raise ValidationError("Optimization memory accepts public development execution only")
        if any(stream["split"] != "public" for step in plan.rounds for stream in step.streams):
            raise ValidationError("Public episode cannot relabel another cursor's task parameters")
        provenance = self.source_provenance
        if (set(provenance) - {"kind", "source_sha256", "model_receipt_sha256", "source_record_sha256", "reference"}
                or provenance.get("kind") not in ("founder", "historical_natural_public_proposal", "model_edit", "control")
                or provenance.get("source_sha256") != digest(self.source)):
            raise ValidationError("Source provenance does not bind the executed program")
        if provenance["kind"] in ("historical_natural_public_proposal", "model_edit"):
            _sha(provenance.get("model_receipt_sha256"))
        elif provenance.get("model_receipt_sha256") is not None:
            raise ValidationError("A non-model source cannot claim a model receipt")
        if provenance.get("source_record_sha256") is not None:
            _sha(provenance["source_record_sha256"])
        if "reference" in provenance and (type(provenance["reference"]) is not str
                or len(provenance["reference"].encode()) > 1024):
            raise ValidationError("Invalid source reference")
        evidence = self.evaluation
        required = {"complete", "risk", "legal_completed", "legal_total", "synthetic_units",
                    "feedback", "public_trajectories", "public_trajectories_sha256"}
        if not required <= set(evidence) or type(evidence["complete"]) is not bool:
            raise ValidationError("Expected public task evaluator evidence")
        if evidence["risk"] not in (None, 0, 1):
            raise ValidationError("Invalid observed public risk")
        for key in ("legal_completed", "legal_total"):
            if type(evidence[key]) is not int or evidence[key] < 0:
                raise ValidationError("Invalid public completion counts")
        if evidence["legal_completed"] > evidence["legal_total"]:
            raise ValidationError("Public completion exceeds denominator")
        cost = evidence["synthetic_units"]
        if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
            raise ValidationError("Invalid public execution cost")
        if (type(evidence["feedback"]) is not dict
                or type(evidence["feedback"].get("complete_correct")) is not bool
                or type(evidence["public_trajectories"]) not in (tuple, list)
                or not evidence["public_trajectories"]
                or digest(evidence["public_trajectories"]) != evidence["public_trajectories_sha256"]):
            raise ValidationError("Public trace projection is missing or changed")
        if evidence["complete"] and evidence["risk"] is None:
            raise ValidationError("A complete execution cannot have missing public risk")
        if evidence["feedback"]["complete_correct"] and (not evidence["complete"]
                or evidence["risk"] != 0 or evidence["legal_completed"] != evidence["legal_total"]):
            raise ValidationError("Correctness claim contradicts execution evidence")
        canonical_bytes(evidence)


@dataclass(frozen=True)
class OptimizationEpisode(StrictRecord):
    episode_id: str
    before: PublicOptimizationExecution
    candidate: PublicOptimizationExecution
    repair: PublicOptimizationExecution | None = None
    selection: str = "curated_development"
    schema_version: int = 1

    def validate(self):
        require_id(self.episode_id)
        if self.selection not in ("curated_development", "fresh_public_development", "scripted_control"):
            raise ValidationError("Unknown episode collection provenance")
        executions = (self.before, self.candidate) + ((self.repair,) if self.repair else ())
        if len({digest(decode_json(value.plan_json)) for value in executions}) != 1:
            raise ValidationError("Before, candidate and repair must use the same public tasks")
        if self.selection != "scripted_control" and any(
                value.source_provenance["kind"] == "control" for value in executions):
            raise ValidationError("Controlled source must remain labelled as a control episode")


def _correct(execution):
    value = execution.evaluation
    return bool(value["complete"] and value["feedback"]["complete_correct"] and value["risk"] == 0)


def episode_status(episode):
    """Observed facts only; absence of a successful repair stays explicit."""
    if type(episode) is not OptimizationEpisode:
        raise ValidationError("Expected a typed optimization episode")
    candidate = episode.candidate.evaluation
    failed = bool(candidate["risk"] == 1 or (candidate["complete"] and not _correct(episode.candidate)))
    return {"observed_public_failure": failed,
            "verified_repair": bool(failed and episode.repair is not None and _correct(episode.repair)
                                    and episode.repair.source != episode.candidate.source),
            "public_cost_improvement": bool(_correct(episode.before) and _correct(episode.candidate)
                and episode.candidate.evaluation["synthetic_units"] < episode.before.evaluation["synthetic_units"]),
            "repair_attempted": episode.repair is not None,
            "selection": episode.selection}


def _clip(text, allowance):
    raw = text.encode()
    if len(raw) <= allowance:
        return text
    return raw[:max(0, allowance - 25)].decode("utf-8", errors="ignore") + "\n[projection truncated]"


def _changes(before, after):
    return "\n".join(unified_diff(before.splitlines(), after.splitlines(),
                                 fromfile="before", tofile="after", n=2))


def _change_excerpt(before, after, allowance):
    changes = _changes(before, after)
    added = "\n".join(line[1:] for line in changes.splitlines() if line.startswith("+") and not line.startswith("+++"))
    removed = "\n".join(line[1:] for line in changes.splitlines() if line.startswith("-") and not line.startswith("---"))
    # Keep both sides. A prefix of a whole unified diff can show only deleted
    # founder code and omit every line of the program that actually failed.
    return {"added_lines": _clip(added, allowance * 3 // 4),
            "removed_lines": _clip(removed, allowance // 4),
            "full_diff_sha256": digest(changes)}


def _check(execution):
    if execution is None:
        return None
    value = execution.evaluation
    return {"complete": value["complete"], "correct": value["feedback"]["complete_correct"],
            "public_risk": value["risk"], "legal_completed": value["legal_completed"],
            "legal_total": value["legal_total"], "service_cost": value["synthetic_units"]}


def _trace_excerpt(execution, allowance):
    if execution is None:
        return None
    trajectories = execution.evaluation["public_trajectories"]
    # Public failed-check counts can choose a public trace. No independent label
    # or protected initial authorization state participates in this projection.
    def fails(row):
        counts = row.get("public_evaluation", {})
        return (type(counts) is dict and counts.get("correct_requests") is not None
                and counts.get("correct_requests") != counts.get("total_requests"))
    selected = next((row for row in trajectories if fails(row)), trajectories[0])
    rows = selected.get("program_trajectory", {}).get("trajectories", ())
    if not rows:
        rows = (selected,)
    positions = [len(rows) - 1] + ([0] if len(rows) > 1 else [])
    excerpt = []
    for index in positions:
        row = rows[index]
        calls = row.get("calls", row.get("tool_calls", ()))
        excerpt.append({"request_index": index, "request": row.get("request"),
            "complete": row.get("complete", row.get("execution_complete")),
            "calls": [{"name": c.get("name"),
                       "output": ({k: v for k, v in c.get("output", c.get("result", {})).items()
                                    if k in ("allowed", "epoch", "remaining", "expires", "blocked", "written")}
                                  if type(c.get("output", c.get("result"))) is dict
                                  else c.get("output", c.get("result")))}
                      for c in calls]})
    value = {"task_id": selected.get("task_id"), "public_check": selected.get("public_evaluation"),
             "request_count": len(rows), "sampled_request_indices": positions, "requests": excerpt,
             "full_public_trace_sha256": execution.evaluation["public_trajectories_sha256"]}
    value["omitted_fields"] = "call_arguments;consent_scope;full_payload_retained_in_public_trace"
    # Keep JSON structured and the terminal request visible. This exposes actual
    # last-call behavior in a failed public stream instead of cutting it off
    # behind an arbitrarily long first request. Full evidence remains bound.
    if len(canonical_bytes(value)) > allowance and len(excerpt) > 1:
        value["requests"] = excerpt[:1]
        value["sampled_request_indices"] = positions[:1]
    if len(canonical_bytes(value)) > allowance:
        value["requests"][0]["request"] = {"sha256": digest(value["requests"][0]["request"]),
            "tool": rows[-1].get("request", {}).get("tool")}
        value["omitted_fields"] += ";full_request_except_tool"
    return value


def project_episode(episode, *, max_bytes=DEFAULT_PROJECTION_BYTES):
    """Return one existing q memory entry; caller persists ``episode`` in full.

    Shrinking a projection changes the displayed content but never its full
    evidence identity. A hash is a reference, not permission to read hidden data.
    """
    if type(max_bytes) is not int or not 2048 <= max_bytes <= MAX_EXPERIENCE_BYTES:
        raise ValidationError("Optimization memory projection budget must be2048..8192 bytes")
    status = episode_status(episode)
    source = {"before": episode.before.source, "candidate": episode.candidate.source,
              "repair": episode.repair.source if episode.repair else None}
    base = {"type": "public_optimization_episode_v1", "selection": episode.selection,
            "facts": status, "checks": {name: _check(execution) for name, execution in (
                ("before", episode.before), ("candidate", episode.candidate), ("repair", episode.repair))},
            "source_sha256": {name: digest(value) if value is not None else None for name, value in source.items()},
            "projection": "bounded_code_diff_and_first_last_public_requests;full_evidence_retained",
            "reflection": "none;all_reported_checks_are_observed_public_execution"}
    for allowance in range(1600, 200, -40):
        value = {**base,
            "candidate_change": _change_excerpt(episode.before.source, episode.candidate.source, allowance),
            "repair_change": _change_excerpt(episode.candidate.source, episode.repair.source, allowance)
                             if episode.repair else None,
            "candidate_public_trace": _trace_excerpt(episode.candidate, allowance),
            "repair_public_trace": _trace_excerpt(episode.repair, allowance)}
        text = canonical_bytes(value).decode()
        if len(text.encode()) <= max_bytes:
            return {"text": text, "origin": "manual_control" if episode.selection == "scripted_control" else "public_execution",
                    "evidence_sha256": digest(episode)}
    raise ValidationError("Projection budget cannot retain the required provenance and checks")


def build_optimization_memory(episodes, *, max_bytes=DEFAULT_PROJECTION_BYTES):
    episodes = tuple(episodes)
    if not 1 <= len(episodes) <= 64 or len({e.episode_id for e in episodes}) != len(episodes):
        raise ValidationError("Expected1..64 distinct optimization episodes")
    return {"episode-" + digest(episode)[:16]: project_episode(episode, max_bytes=max_bytes) for episode in episodes}


def _selected_bytes(key, entry):
    return len(canonical_bytes(({"id": key, **entry},)))


def match_entry_lengths(memory, entry_ids):
    """Pad real entries to equal selected UTF-8 bytes without adding experience.

    Token counts are unknown: equal UTF-8 bytes are not equal model tokens. This
    does not declare either entry relevant; relevance comes from the registered
    public episode and task relation. Equalize after all projection choices.
    """
    keys = tuple(entry_ids)
    if len(keys) < 2 or len(keys) != len(set(keys)) or any(key not in memory for key in keys):
        raise ValidationError("Length matching requires distinct existing entries")
    result = deepcopy(memory)
    for key in keys:
        entry = result[key]
        if set(entry) != {"text", "origin", "evidence_sha256"}:
            raise ValidationError("Invalid experience entry")
        value = decode_json(entry["text"])
        if "neutral_length_padding" in value:
            raise ValidationError("Experience has already been padded")
        value["neutral_length_padding"] = ""
        entry["text"] = canonical_bytes(value).decode()
    target = max(_selected_bytes(key, result[key]) for key in keys)
    added = {}
    for key in keys:
        entry = result[key]
        added[key] = target - _selected_bytes(key, entry)
        value = decode_json(entry["text"])
        value["neutral_length_padding"] = " " * added[key]
        entry["text"] = canonical_bytes(value).decode()
        if len(entry["text"].encode()) > MAX_EXPERIENCE_BYTES:
            raise ValidationError("Matching exceeds entry limit; register a smaller common projection budget")
        if _selected_bytes(key, entry) != target:
            raise ValidationError("Selected experience length matching failed")
    return result, {"measurement": "canonical_selected_entry_utf8_bytes", "selected_bytes": target,
                    "padding_bytes": added, "model_tokens": None, "equal_tokens_claim": False,
                    "original_entry_sha256": {key: digest(memory[key]) for key in keys},
                    "matched_entry_sha256": {key: digest(result[key]) for key in keys}}
