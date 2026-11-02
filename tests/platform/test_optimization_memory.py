from copy import deepcopy
from dataclasses import replace

import pytest

from rsi_lab.contracts import CursorFamily, Partition
from rsi_lab.engine.platform import PublicTaskEvaluator
from rsi_lab.errors import ValidationError
from rsi_lab.infra.chat_models import ScriptedChatProvider
from rsi_lab.serialization import canonical_bytes, decode_json, digest
from rsi_lab.subjects.cost_q import CostQSystemAdapter, ExperienceSelector, control_memory, make_initial_snapshot
from rsi_lab.subjects.optimization_memory import (
    PublicOptimizationExecution, OptimizationEpisode, build_optimization_memory,
    episode_status, match_entry_lengths, project_episode,
)
from rsi_lab.tasks.cost_authorization import FOUNDER_SOURCE, SAFE_REFERENCE_SOURCE, UNSAFE_REFERENCE_SOURCE
from rsi_lab.tasks.platform_cost_authorization import CostAuthorizationTaskPackage


class PublicBoundaryTasks(CostAuthorizationTaskPackage):
    def public_suite(self, seed, partition=Partition.DEV):
        return self.plan(seed, CursorFamily.PUBLIC_DEVELOPMENT, partition, 1, "revocation")


@pytest.fixture(scope="module")
def episode(tmp_path_factory):
    directory = tmp_path_factory.mktemp("optimization-memory")
    provider = ScriptedChatProvider(directory / "unused.sqlite", lambda *_: pytest.fail("Unexpected model call"))
    tasks = PublicBoundaryTasks()
    executions = []
    for name, source in (("before", FOUNDER_SOURCE), ("candidate", UNSAFE_REFERENCE_SOURCE), ("repair", SAFE_REFERENCE_SOURCE)):
        snapshot = make_initial_snapshot(18001, control_memory(), source=source)
        subject = CostQSystemAdapter(directory / name / "system", provider, snapshot)
        evaluator = PublicTaskEvaluator(subject, tasks, directory / name / "public", seed=18001)
        evidence = evaluator.evaluate(snapshot, name)
        executions.append(PublicOptimizationExecution(source, canonical_bytes(evaluator.plan).decode(), evidence,
            {"kind": "control", "source_sha256": digest(source)}))
    return OptimizationEpisode("actual-control-episode", *executions, selection="scripted_control")


def test_actual_public_failure_and_repair_are_bound_to_full_evidence(episode):
    status = episode_status(episode)
    assert status["observed_public_failure"] and status["verified_repair"]
    entry = project_episode(episode)
    assert entry["origin"] == "manual_control" and entry["evidence_sha256"] == digest(episode)
    text = decode_json(entry["text"])
    assert text["checks"]["candidate"]["correct"] is False
    assert text["checks"]["repair"]["correct"] is True
    assert "api.version()" in text["repair_change"]["added_lines"]
    # Terminal request evidence must survive; prefixes previously lost the failure.
    failed_trace = text["candidate_public_trace"]
    assert failed_trace["sampled_request_indices"][0] == 2
    assert "execute" in [c["name"] for c in failed_trace["requests"][0]["calls"]]
    assert len(entry["text"].encode()) <= 6144
    assert project_episode(OptimizationEpisode.from_dict(episode.to_dict())) == entry


def test_missing_or_failed_repair_is_not_reported_as_success(episode):
    assert not episode_status(replace(episode, repair=None))["verified_repair"]
    assert not episode_status(replace(episode, repair=episode.candidate))["verified_repair"]
    unchanged = replace(episode, candidate=episode.before, repair=episode.before)
    assert not episode_status(unchanged)["observed_public_failure"]
    assert not episode_status(unchanged)["verified_repair"]


def test_known_public_harm_survives_later_incompletion(episode):
    evidence = deepcopy(episode.candidate.evaluation)
    evidence["complete"] = False
    partial = replace(episode, candidate=replace(episode.candidate, evaluation=evidence))
    assert episode_status(partial)["observed_public_failure"]
    evidence["risk"] = None
    unknown = replace(episode, candidate=replace(episode.candidate, evaluation=evidence))
    assert not episode_status(unknown)["observed_public_failure"]


@pytest.mark.parametrize("family,partition", [
    (CursorFamily.INDEPENDENT_EVALUATION, Partition.DEV),
    (CursorFamily.ADMISSION_PROBE, Partition.DEV),
    (CursorFamily.PUBLIC_DEVELOPMENT, Partition.SEALED),
    (CursorFamily.PUBLIC_DEVELOPMENT, Partition.TRAIN),
])
def test_non_development_evidence_cannot_enter_archive(episode, family, partition):
    plan = decode_json(episode.candidate.plan_json)
    plan.update(family=family.value, partition=partition.value)
    with pytest.raises(ValidationError, match="public development"):
        replace(episode.candidate, plan_json=canonical_bytes(plan).decode())


def test_episode_requires_matching_public_task_plan(episode):
    plan = decode_json(episode.candidate.plan_json)
    plan["seed"] += 1
    other = replace(episode.candidate, plan_json=canonical_bytes(plan).decode())
    with pytest.raises(ValidationError, match="same public tasks"):
        replace(episode, candidate=other)


def test_independent_parameters_cannot_be_relabelled_public(episode):
    plan = CostAuthorizationTaskPackage().plan(19001, CursorFamily.INDEPENDENT_EVALUATION,
                                              Partition.DEV, 1, "revocation")
    fake = replace(plan, family=CursorFamily.PUBLIC_DEVELOPMENT)
    with pytest.raises(ValidationError, match="relabel"):
        replace(episode.candidate, plan_json=canonical_bytes(fake).decode())


def test_trace_or_source_substitution_rejected(episode):
    value = deepcopy(episode.candidate.evaluation)
    value["public_trajectories"][0]["request"] = {"injected": True}
    with pytest.raises(ValidationError, match="trace projection"):
        replace(episode.candidate, evaluation=value)
    with pytest.raises(ValidationError, match="executed program"):
        replace(episode.candidate, source=FOUNDER_SOURCE)


def test_false_correctness_claim_rejected(episode):
    value = deepcopy(episode.candidate.evaluation)
    value["feedback"]["complete_correct"] = True
    with pytest.raises(ValidationError, match="contradicts"):
        replace(episode.candidate, evaluation=value)


def test_model_provenance_is_explicit_and_control_cannot_be_laundered(episode):
    with pytest.raises(ValidationError, match="control episode"):
        replace(episode, selection="curated_development")
    with pytest.raises(ValidationError, match="SHA256"):
        replace(episode.candidate, source_provenance={"kind": "model_edit", "source_sha256": digest(episode.candidate.source)})
    model_executions = [replace(execution, source_provenance={"kind": "model_edit",
        "source_sha256": digest(execution.source), "model_receipt_sha256": "f" * 64})
        for execution in (episode.before, episode.candidate, episode.repair)]
    curated = OptimizationEpisode("curated", *model_executions, selection="curated_development")
    assert project_episode(curated)["origin"] == "public_execution"
    assert decode_json(project_episode(curated)["text"])["selection"] == "curated_development"


def test_projection_is_allowlisted_not_an_arbitrary_feedback_copy(episode):
    value = deepcopy(episode.candidate.evaluation)
    value["model_guess"] = "not a demonstrated causal fact"
    modified = replace(episode, candidate=replace(episode.candidate, evaluation=value))
    projected = project_episode(modified)
    assert "not a demonstrated causal fact" not in projected["text"]
    assert projected["evidence_sha256"] != project_episode(episode)["evidence_sha256"]


def test_matching_uses_actual_selected_encoding_and_keeps_sources_unchanged(episode):
    other = replace(episode, episode_id="other", candidate=episode.before, repair=None)
    memory = build_optimization_memory((episode, other))
    original = deepcopy(memory)
    keys = tuple(memory)
    matched, accounting = match_entry_lengths(memory, keys)
    assert memory == original
    sizes = []
    for key in keys:
        selection = ExperienceSelector((key, *(k for k in keys if k != key)), 1, 16384).select(matched)
        sizes.append(len(canonical_bytes(selection)))
        assert matched[key]["evidence_sha256"] == memory[key]["evidence_sha256"]
        assert len(matched[key]["text"].encode()) <= 8192
    assert len(set(sizes)) == 1 and sizes[0] == accounting["selected_bytes"]
    assert accounting["model_tokens"] is None and accounting["equal_tokens_claim"] is False
    with pytest.raises(ValidationError, match="already been padded"):
        match_entry_lengths(matched, keys)


def test_utf8_and_different_ids_match_without_truncating_json():
    memory = {"short": {"text": canonical_bytes({"public_fact": "公开经验\\\""}).decode(),
                        "origin": "public_execution", "evidence_sha256": "a" * 64},
              "a-longer-id": {"text": canonical_bytes({"public_fact": "longer plain ASCII evidence"}).decode(),
                        "origin": "public_execution", "evidence_sha256": "b" * 64}}
    matched, accounting = match_entry_lengths(memory, tuple(memory))
    sizes = [len(canonical_bytes(({"id": key, **entry},))) for key, entry in matched.items()]
    assert sizes == [accounting["selected_bytes"]] * 2
    assert decode_json(matched["short"]["text"])["public_fact"] == "公开经验\\\""


def test_memory_cannot_silently_drop_duplicate_episode_ids(episode):
    with pytest.raises(ValidationError, match="distinct"):
        build_optimization_memory((episode, episode))
