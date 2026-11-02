"""Small public-only q search with real source editing and native activation.

The caller proposes/freezes selectors. This runner measures their optimizer
effect on matched public tasks; trial descendants never become the returned
root candidate. Only the selected q is transferred to the original checkpoint.
"""
from dataclasses import dataclass, replace
from pathlib import Path

from rsi_lab.api import PreparedUpdate, SystemSnapshot
from rsi_lab.auditing.platform import AdmissionDecision
from rsi_lab.contracts import Partition
from rsi_lab.engine.activation import ActivationController
from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.engine.platform import PublicTaskEvaluator, prepare_update
from rsi_lab.errors import ConflictError, IntegrityError, LabError, ValidationError
from rsi_lab.frozen import implementation_fingerprint
from rsi_lab.infra.chat_models import DurableChatProvider
from rsi_lab.serialization import digest
from rsi_lab.subjects.cost_q import CostQSystemAdapter, ExperienceSelector
from rsi_lab.subjects.platform_fixture import replace_component
from rsi_lab.subjects.q_interventions import build_q_interventions
from rsi_lab.subjects.q_public_selection import (
    PublicQCandidate, PublicQPlan, PublicQTrial, PublicQSelection,
    public_trial_from_evaluations, select_public_q,
)


@dataclass(frozen=True)
class PublicQSearchResult:
    plan: PublicQPlan
    selection: PublicQSelection
    candidate: SystemSnapshot
    trials: tuple[PublicQTrial, ...]


def evaluate_public_q_candidates(base, selectors, *, baseline_id, tasks, provider_factory,
                                 directory, root_id, seed, replicates=2):
    """One actual editor call per candidate/repetition, same source/public plan.

    ``provider_factory(candidate_id, replicate)`` must return an independent
    DurableChatProvider-compatible journal with max_calls=1 and matching model
    configuration. No independent task/label argument exists. Failed or unknown
    slots persist and make selection inconclusive; they are never re-dispatched.
    """
    if type(base) is not SystemSnapshot or type(selectors) is not dict:
        raise ValidationError("Public q search requires a checkpoint and registered selector mapping")
    if baseline_id not in selectors or any(type(q) is not ExperienceSelector for q in selectors.values()):
        raise ValidationError("Public q search requires typed candidates and an explicit baseline")
    baseline = ExperienceSelector.from_dict(base.version.component("experience_selector"))
    if selectors[baseline_id] != baseline:
        raise ValidationError("The registered baseline must equal the checkpoint's actual q")
    snapshots = {}
    for name, selector in selectors.items():
        snapshot = replace(base, version=replace_component(base.version, "experience_selector", selector))
        build_q_interventions(base, snapshot)  # q-only, same memory/state/entry and byte budgets.
        snapshots[name] = snapshot
    task_plan = tasks.public_suite(seed, Partition.DEV)
    plan = PublicQPlan(root_id, base.sha256, tuple(PublicQCandidate(name, digest(q)) for name, q in selectors.items()),
                       baseline_id, replicates, 1, 1, digest(task_plan))
    directory = Path(directory)
    manifest = {"plan": plan, "checkpoint": base, "selectors": selectors,
                "implementation_sha256": implementation_fingerprint(),
                "scope": "public_q_search_one_editor_attempt;no_trial_A_inheritance;no_independent_labels"}
    save_record(directory / "manifest.json", manifest)
    protocol = digest(manifest)
    slots = [(name, rep) for name in selectors for rep in range(replicates)]
    providers, journal_paths, settings = {}, set(), None
    for name, rep in slots:
        provider = provider_factory(name, rep)
        if not isinstance(provider, DurableChatProvider) or provider.config.max_calls != 1:
            raise ValidationError("Each public q trial requires a durable one-call provider")
        path = provider.path.resolve()
        if path in journal_paths:
            raise ValidationError("q trials cannot share a model journal or response")
        journal_paths.add(path)
        configuration = {"config": provider.config, "provider_id": provider.provider_id, "origin": provider.origin}
        if settings is not None and configuration != settings:
            raise ValidationError("q trial models, origin, and resource limits must match")
        settings = configuration
        providers[name, rep] = provider
    save_record(directory / "providers.json", {"settings": settings,
        "journals": [{"candidate_id": name, "replicate": rep, "path": str(providers[name, rep].path.resolve())}
                     for name, rep in slots]})
    trials = []
    for name, rep in slots:
        location = directory / "trials" / name / f"{rep:03d}"
        snapshot, provider = snapshots[name], providers[name, rep]
        subject = CostQSystemAdapter(location / "system", provider, snapshot)
        evaluator = PublicTaskEvaluator(subject, tasks, location / "public", seed=seed, partition=Partition.DEV)
        namespace = root_id + f"/public-q/{name}/{rep}"
        failure = location / "failure.json"
        if failure.exists():
            saved = read_record(failure)
            if subject.snapshot().sha256 != saved["evidence"]["active_snapshot_sha256"]:
                raise IntegrityError("Failed public q trial's actual state changed on recovery")
            evaluator.verify_cached()
            trial = PublicQTrial.from_dict(saved["trial"])
            save_record(location / "trial.json", trial)
            trials.append(trial)
            continue
        before = None
        try:
            # Same operation as adapter.propose's incumbent measurement: no
            # duplicated physical evaluation when the proposal reads it again.
            before = evaluator.evaluate(snapshot, namespace + "/incumbent")
            prepared_path = location / "prepared.json"
            source = tasks.create_environment(location / "source-environment").snapshot()
            if prepared_path.exists():
                prepared = PreparedUpdate.from_dict(read_record(prepared_path))
                evaluator.verify_cached()
            else:
                draft = subject.propose(namespace, evaluator)
                prepared = prepare_update(subject, draft, snapshot, update_id=namespace + "/update",
                    root_id=root_id, protocol_sha256=protocol, environment_sha256=digest(source),
                    partition=Partition.DEV, horizon=1, task_plan_sha256=digest(task_plan))
                save_record(prepared_path, prepared)
            decision = AdmissionDecision(prepared.identity, digest({"native": prepared.draft.native_evidence_json}),
                "public-q-trial-native-v1", "accept" if prepared.draft.native_valid else "reject",
                "public_complete_correct_and_strict_cost_gain")
            activation = ActivationController(location / "activation.sqlite", protocol).apply(
                subject, prepared, decision, environment_sha256=digest(source))
            after = evaluator.evaluate(subject.snapshot(), namespace + "/selected")
            usage = provider.summary()
            trial = public_trial_from_evaluations(plan, name, rep, evaluation_plan=task_plan,
                before=before, after=after, attempted_updates=1,
                successful_updates=int(activation is not None), editor_calls=usage["dispatched_calls"])
            save_record(location / "execution.json", {"before": before, "after": after, "decision": decision,
                "activation": activation, "active_snapshot": subject.snapshot(), "editor": usage,
                "actual_public_service_units": evaluator.synthetic_cost()})
        except (ConflictError, IntegrityError):
            raise
        except LabError as exc:
            usage = provider.summary()
            evidence = {"reason": type(exc).__name__, "before": before, "editor": usage,
                        "active_snapshot_sha256": subject.snapshot().sha256}
            trial = PublicQTrial(digest(plan), name, digest(selectors[name]), rep, root_id, base.sha256,
                digest(task_plan), False, None,
                float(before["synthetic_units"]) if before and before.get("synthetic_units") is not None else None,
                None, 1, 0, usage["dispatched_calls"], digest(evidence),
                digest({"before": before.get("public_trajectories_sha256") if before else None, "missing": True}))
            save_record(failure, {"evidence": evidence, "trial": trial})
        save_record(location / "trial.json", trial)
        trials.append(trial)
    selection = select_public_q(plan, tuple(trials))
    # Only q returns to the original root. Native trial activations remain in
    # isolated directories and cannot leak their policy or feedback state here.
    candidate = snapshots[selection.selected_candidate_id]
    build_q_interventions(base, candidate)
    result = PublicQSearchResult(plan, selection, candidate, tuple(trials))
    save_record(directory / "selection.json", result)
    return result
