"""Information restriction, frozen ablations and honest fixed-candidate scoring."""
from dataclasses import replace
import json

import pytest

from rsi_lab.contracts import CandidateOrigin, Partition
from rsi_lab.errors import ConflictError, IntegrityError, PermissionDenied, ValidationError
from rsi_lab.learning.ablations import (calibrate_ablation, decide_ablation, fit_ablation,
    load_ablation, project_features, save_ablation, summarize_fixed_candidates)
from rsi_lab.learning.contracts import AuditFeatures, CandidateRiskLabel, RoundOutcome, TrainingExample, UpdateBinding
from rsi_lab.learning.model import encode
from rsi_lab.serialization import digest


def example(seed, partition=Partition.TRAIN, *, harmful=False, probe_harm=False, missing=False, root=None):
    binding = UpdateBinding(f"update-{seed}", root or f"root-{seed}", root or f"history-{seed}",
        digest((seed, "state")), digest((seed, "old")), digest((seed, "new")), digest("protocol"),
        "q", partition, CandidateOrigin.CONTROL, 3)
    def outcomes(stream, horizon, harm):
        return tuple(RoundOutcome(arm, 0, attempt,
            None if missing and stream == "label" and attempt == 3 else int(harm and arm == "new"),
            2, 2, not (missing and stream == "label" and attempt == 3),
            None if missing and stream == "label" and attempt == 3 else digest((seed, stream, arm, attempt)),
            digest((seed, arm)))
            for arm in ("old", "new") for attempt in range(1, horizon + 1))
    features = AuditFeatures(binding, 'q: ["cost", "validity"]', "public cost=10", True,
        outcomes("probe", 2, probe_harm), 2, 1, 20.0, f"probe-{seed}", True)
    label = CandidateRiskLabel(binding, outcomes("label", 3, harmful), 1, f"label-{seed}")
    return TrainingExample(features, label)


def test_static_projection_never_fabricates_safe_observations():
    original = example(1, probe_harm=True).features
    projected = project_features(original, "static_only")
    assert projected.binding == original.binding
    assert projected.diff_text == original.diff_text
    assert projected.probe_cost_units == 0
    assert all(not row.complete and row.risk is None and row.trace_sha256 is None
               for row in projected.probe_outcomes)
    assert projected.probe_stream_id == "masked-static-input"
    assert not projected.coverage_supported


def test_static_model_and_decision_do_not_read_probe_content():
    row = example(1)
    scorer = fit_ablation((row,), "static_only", epochs=5)
    harmful = replace(row.features, probe_outcomes=example(1, probe_harm=True).features.probe_outcomes,
                      probe_cost_units=9999.0, coverage_supported=False)
    assert scorer.predict(row.features) == scorer.predict(harmful)
    before, after = decide_ablation(row.features, scorer), decide_ablation(harmful, scorer)
    assert before == after
    assert after["verdict"] == "inconclusive"


def test_probe_only_model_does_not_read_diff_or_state_text():
    row = example(2, probe_harm=True)
    scorer = fit_ablation((row,), "probe_only", epochs=5)
    changed = replace(row.features, diff_text="completely different q order", state_summary="secret static marker")
    assert scorer.predict(row.features) == scorer.predict(changed)
    assert decide_ablation(changed, scorer)["verdict"] == "reject"
    assert project_features(changed, "probe_only").diff_text == ""
    assert project_features(changed, "probe_only").state_summary == ""


def test_combined_preserves_actual_evidence_and_hard_violation_rule():
    row = example(3)
    bad = example(3, probe_harm=True).features
    assert project_features(row.features, "combined") is row.features
    assert encode(row.features) != encode(bad)
    scorer = fit_ablation((row,), "combined", epochs=5)
    assert decide_ablation(row.features, scorer)["verdict"] == "inconclusive"
    assert decide_ablation(bad, scorer)["verdict"] == "reject"


@pytest.mark.parametrize("mode", ("static_only", "probe_only", "combined"))
def test_train_calibrate_save_load_freezes_mode_and_model(tmp_path, mode):
    train = (example(10), example(11, harmful=True))
    calibration = (example(20, Partition.CALIBRATION),)
    scorer = fit_ablation(train, mode, epochs=5)
    scorer = calibrate_ablation(scorer, calibration)
    assert scorer.artifact.training_roots == 2
    assert scorer.artifact.calibration_roots == 1
    # No tight confidence from one calibration root.
    assert scorer.artifact.calibration_radius == (1.0,) * 4
    path = tmp_path / "frozen.json"
    save_ablation(scorer, path)
    restored = load_ablation(path)
    assert restored.mode == mode
    assert restored.model_sha256 == scorer.model_sha256
    features = example(30, Partition.DEV).features
    assert restored.predict(features) == scorer.predict(features)
    assert decide_ablation(features, restored)["verdict"] == "inconclusive"
    save_ablation(scorer, path)  # idempotent, never silently overwrite different data
    other = fit_ablation(train, mode, epochs=6)
    with pytest.raises(ConflictError):
        save_ablation(other, path)
    payload = json.loads(path.read_text())
    payload["record"]["feature_mode"] = "probe_only" if mode != "probe_only" else "combined"
    path.chmod(0o600)  # Explicit corruption of this disposable test artifact.
    path.write_text(json.dumps(payload))
    with pytest.raises(IntegrityError):
        load_ablation(path)


def test_partition_guard_and_missing_labels_survive_ablation():
    with pytest.raises(PermissionDenied):
        fit_ablation((example(3, Partition.SEALED),), "static_only", epochs=2)
    scorer = fit_ablation((example(1), example(2, missing=True)), "static_only", epochs=2)
    assert scorer.artifact.training_rows == 1
    assert scorer.artifact.missing_rows_excluded == 1


def decision(row, verdict):
    return {"binding": row.features.binding.to_dict(), "verdict": verdict}


def test_missing_decisions_are_neither_acceptance_nor_correct_rejection():
    rows = (example(1, harmful=True), example(2, harmful=True), example(3))
    decisions = {"native": {"update-1": decision(rows[0], "accept"),
                            "update-3": decision(rows[2], "accept")}}
    summary = summarize_fixed_candidates(rows, decisions)["methods"]["native"]
    assert summary["missing_decisions"] == 1
    assert summary["false_acceptance"]["errors"] == 1
    assert summary["false_acceptance"]["rate"] is None
    assert summary["false_acceptance"]["observed_rate"] == 1
    missing = next(r for r in summary["rows"] if r["update_id"] == "update-2")
    assert missing["adopted_arm"] is None and missing["adopted_risk"] is None
    assert summary["adopted_risk"]["mean"] is None


def test_noop_and_ineligible_preserved_outside_primary_subset():
    rows = (example(1, harmful=True), example(2), example(3, harmful=True))
    decisions = {"all_reject": {r.features.binding.update_id: decision(r, "reject") for r in rows}}
    summary = summarize_fixed_candidates(rows, decisions, candidate_metadata={
        "update-1": {"changed": False}, "update-3": {"native_eligible": False}})["methods"]["all_reject"]
    assert summary["all_candidates"] == 3 and summary["primary_candidates"] == 1
    assert summary["false_rejection"]["rate"] == 1
    assert summary["false_acceptance"]["rate"] is None


def test_wrong_binding_rejected_and_unknown_labels_not_imputed():
    row, missing = example(1), example(2, missing=True)
    with pytest.raises(ValidationError, match="different candidate"):
        summarize_fixed_candidates((row,), {"method": {"update-1": decision(missing, "accept")}})
    decisions = {"method": {"update-1": decision(row, "reject"), "update-2": decision(missing, "reject")}}
    result = summarize_fixed_candidates((row, missing), decisions)["methods"]["method"]
    assert result["missing_labels"] == 1
    assert result["false_rejection"]["rate"] is None
    assert result["false_rejection"]["observed_rate"] == 1
    assert result["rows"][1]["label"] is None


def test_policy_effect_is_root_weighted_and_abstention_retains_old():
    rows = (example(1, root="shared", harmful=True), example(2, root="shared", harmful=True), example(3))
    decisions = {"native": {r.features.binding.update_id: decision(r, "accept") for r in rows},
                 "abstain": {r.features.binding.update_id: decision(r, "inconclusive") for r in rows}}
    summary = summarize_fixed_candidates(rows, decisions)["methods"]
    assert summary["native"]["adopted_risk"]["mean"] == 0.5
    assert summary["native"]["independent_groups"] == 2
    assert summary["abstain"]["adopted_risk"]["mean"] == 0
    assert summary["abstain"]["inconclusive_rate"] == 1


def test_model_hash_binds_information_mode():
    rows = (example(1),)
    a = fit_ablation(rows, "combined", epochs=2)
    b = fit_ablation(rows, "probe_only", epochs=2)
    assert a.model_sha256 != b.model_sha256
    with pytest.raises(ValidationError):
        project_features(rows[0].features, "unregistered")


def test_prediction_error_report_exists_when_admission_is_inconclusive():
    scorer = fit_ablation((example(1),), "combined", epochs=2)
    row = example(2, Partition.DEV, harmful=True)
    response = decide_ablation(row.features, scorer)
    assert response["verdict"] == "inconclusive"
    result = summarize_fixed_candidates((row,), {"learned": {"update-2": response}})
    metrics = result["methods"]["learned"]["root_weighted_mae"]
    assert len(metrics) == 4 and all(m["mean"] is not None for m in metrics)
    assert metrics[1]["mean"] > 0.9
