"""Engineering fixtures for the protected post-generation analysis boundary."""
import json
from pathlib import Path

import pytest

from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.errors import IntegrityError
from rsi_lab.frozen import implementation_fingerprint
from rsi_lab.serialization import canonical_bytes, digest
from rsi_lab.studies.stress_pilot_analysis import analyze
from rsi_lab.tasks import stress_bench as bench


def _fixture(path, stages, *, context=True, claim_match=True, provider_kind="scripted", models=("scripted.control",)):
    domain, seed, system = "finance", 77001, "gepa_program"
    root_id = "finance-77001-gepa"
    root_dir = path / "roots" / root_id
    founder = bench.backend(domain).FOUNDER_SOURCE
    root = {"slots": [], "slot_sha256": {}, "founder_source": founder,
        "response_models": list(models),
        "founder_source_sha256": digest(founder), "final_snapshot_sha256": digest("snapshot"),
        "model_usage": {"dispatched_calls": len(stages), "actual_total_tokens": None,
            "complete_responses": sum(x is not None for x in stages),
            "unknown_responses": sum(x is None for x in stages), "invalid_responses": 0,
            "usage_missing_calls": len(stages), "reserved_tokens": 0,
            "cost_missing_calls": len(stages), "reported_usage_cost": None,
            "token_reservation_kind": "control_no_model_tokens"}}
    previous = founder
    retained = [digest(founder)]
    for step, source in enumerate(stages, 1):
        request_id = "request-" + str(step)
        body = {"messages": [{"role": "user", "content": json.dumps({"previous_code": previous if context else "not provided"})}]}
        request = {"request_id": request_id, "request_sha256": digest(body), "body": body, "status": "complete"}
        request_path = "requests/" + digest(request_id) + ".json"
        save_record(root_dir / request_path, request)
        candidate = []
        if source is not None:
            retained.append(digest(source))
            candidate = [{"source": source, "source_sha256": digest(source), "native_id": str(step),
                          "native_admitted": True, "platform_retained": True, "active_after": True}]
        slot = {"step": step, "status": "complete" if source is not None else "missing",
            "base_source": previous, "active_source": source if source is not None else previous,
            "public_active": {"synthetic_units": 1, "complete": True}, "candidate_sources": candidate,
            "retained_source_hashes": list(retained), "model_request_ids": [request_id],
            "context_sources": [{"source_sha256": digest(previous), "native_id": str(step - 1),
                "role": "parent", "matched_in_request": claim_match if context else False,
                "request_id": request_id, "request_sha256": digest(body), "request_path": request_path}]}
        relative = f"slots/{step}/slot.json"
        save_record(root_dir / relative, slot)
        root["slots"].append(relative)
        root["slot_sha256"][relative] = digest(slot)
        previous = slot["active_source"]
    entry = {"domain": domain, "seed": seed, "system": system, "root_id": root_id, "path": "roots/" + root_id}
    manifest = {"protocol": "test-pressure", "domains": [domain], "seeds": [seed],
        "systems": [system], "horizon": len(stages), "root_ids": [entry], "provider_kind": provider_kind,
        "implementation": implementation_fingerprint(), "bench_protocol": bench.VERSION,
        "source_match_rule": "whole_source_outer_whitespace_trimmed;recursive_JSON_depth8"}
    save_record(root_dir / "root.json", root)
    save_record(path / "manifest.json", manifest)
    save_record(path / "generation_complete.json", {"manifest_sha256": digest(manifest),
                "roots_sha256": {root_id: digest(root)}, "response_models": sorted(models),
                "response_model_consistent": len(models) <= 1})
    return root_dir, root


def test_full_chain_requires_actual_context_and_keeps_frozen_source_evidence(tmp_path, monkeypatch):
    module = bench.backend("finance")
    bad = module.UNSAFE_SOURCES["approval_revocation"]
    _fixture(tmp_path, [bad, bad + "\n# another generated version\n", module.SAFE_SOURCE])
    report = analyze(tmp_path)
    root = report["roots"][0]
    assert report["origin"] == "control" and report["analysis_model_calls"] == 0
    assert report["systems"][0]["usage"]["actual_total_tokens"] is None
    assert root["generated_unsafe"] == root["retained_unsafe"] == root["used_unsafe_context"] == 1
    assert root["observed_lineage_recurrence"] == root["path_risk"] == 1
    assert root["final_risk"] == 0 and root["safe_cost_comparison_available"]
    assert root["cost_saving_vs_founder"] > 0
    assert root["actual_context_uses"][0]["step"] == 2
    assert root["lineage_recurrences"][0]["families"]
    assert not root["lineage_recurrences"][0]["causal_claim"]
    assert report["ranking"] is None and (tmp_path / "analysis/report.md").is_file()
    # Re-analysis verifies logged effects, without executing sources or models again.
    def no_execution(*args, **kwargs):
        raise AssertionError("exact frozen-source evidence should be replayed")
    monkeypatch.setattr(bench, "evaluate_case", no_execution)
    assert analyze(tmp_path) == report


def test_sampling_id_without_actual_prompt_match_is_not_use_or_propagation(tmp_path):
    bad = bench.backend("finance").UNSAFE_SOURCES["approval_revocation"]
    _fixture(tmp_path, [bad, bad, bad], context=False)
    root = analyze(tmp_path)["roots"][0]
    assert root["retained_unsafe"] == root["final_risk"] == 1
    assert root["used_unsafe_context"] == root["observed_lineage_recurrence"] == 0
    assert root["candidates"][-1]["right_censored"]
    assert not root["candidates"][0]["right_censored"]
    assert not root["safe_cost_comparison_available"]


def test_all_missing_slots_preserve_unknown_root_risk(tmp_path):
    _fixture(tmp_path, [None, None, None])
    result = analyze(tmp_path)
    root = result["roots"][0]
    assert root["final"]["risk"] == 0  # Evidence about the still-frozen founder only.
    assert root["path_risk"] is root["final_risk"] is root["generated_unsafe"] is None
    assert root["complete_steps"] == 0 and root["candidate_count"] == 0
    assert result["systems"][0]["path_risk"]["bounds"] == [0, 1]
    assert result["domains"][0]["final_legal_completion"] is None


def test_known_harm_survives_missing_future_generation(tmp_path):
    bad = bench.backend("finance").UNSAFE_SOURCES["approval_revocation"]
    _fixture(tmp_path, [bad, None, None])
    root = analyze(tmp_path)["roots"][0]
    assert root["path_risk"] == root["final_risk"] == 1
    assert root["missing_steps"] == [2, 3]
    assert root["candidates"][0]["later_missing_steps"] == [2, 3]


def test_false_claim_of_actual_context_fails_independent_verification(tmp_path):
    bad = bench.backend("finance").UNSAFE_SOURCES["approval_revocation"]
    _fixture(tmp_path, [bad], claim_match=False)
    with pytest.raises(IntegrityError, match="actual model request"):
        analyze(tmp_path)


def test_root_mutation_after_freeze_cannot_change_denominator(tmp_path):
    root_dir, root = _fixture(tmp_path, [None])
    root["slots"] = []
    # Recompute only the file's own checksum; the freeze must still catch it.
    (root_dir / "root.json").chmod(0o600)
    (root_dir / "root.json").write_bytes(canonical_bytes({"record": root, "sha256": digest(root)}))
    with pytest.raises(IntegrityError, match="generation freeze"):
        analyze(tmp_path)


def test_missing_freeze_prevents_any_independent_scoring(tmp_path):
    save_record(tmp_path / "manifest.json", {"protocol": "test"})
    with pytest.raises(FileNotFoundError):
        analyze(tmp_path)
    assert not (tmp_path / "analysis").exists()


def test_changed_implementation_cannot_reinterpret_frozen_search(tmp_path, monkeypatch):
    from rsi_lab.studies import stress_pilot_analysis as analysis
    _fixture(tmp_path, [None])
    monkeypatch.setattr(analysis, "implementation_fingerprint", lambda: "changed")
    with pytest.raises(IntegrityError, match="implementation or task protocol"):
        analyze(tmp_path)
    assert not (tmp_path / "analysis").exists()


def test_cross_root_model_identity_metadata_does_not_invent_common_model_comparison(tmp_path):
    _fixture(tmp_path, [None], provider_kind="http", models=("model-a", "model-b"))
    result = analyze(tmp_path)
    assert result["response_models"] == ["model-a", "model-b"]
    assert not result["comparison_model_identity_valid"] and not result["response_model_consistent"]
    assert result["ranking"] is None
