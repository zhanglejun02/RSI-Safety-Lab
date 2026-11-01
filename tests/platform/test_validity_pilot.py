"""The validity pilot measures four actual continuations of each registered root."""
from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from rsi_lab.api import CandidateDraft
from rsi_lab.contracts import CandidateOrigin, Partition
from rsi_lab.engine.continuation import read_record
from rsi_lab.errors import BudgetExceeded, IntegrityError, ValidationError
from rsi_lab.platform_config import PlatformConfig, ProtocolConfig, load_platform_config
from rsi_lab.platform_wiring import builtin_registry, fixture_descriptor
from rsi_lab.serialization import canonical_bytes, decode_json, digest
from rsi_lab.studies import platform, validity_pilot
from rsi_lab.subjects.platform_fixture import FixtureSystem


@pytest.fixture(autouse=True)
def stable_source_fingerprint(monkeypatch):
    # Protocol construction is shared with platform, while the pilot also binds
    # its implementation independently. Keep both stable during concurrent edits.
    monkeypatch.setattr(platform, "implementation_fingerprint", lambda: "9" * 64)
    monkeypatch.setattr(validity_pilot, "implementation_fingerprint", lambda: "9" * 64)


def fixture_config(**kwargs):
    return PlatformConfig(protocol=ProtocolConfig(mode="paired", horizon=3, **kwargs))


def branches(root, repetitions=1):
    return [root / "branches" / condition / arm / str(replicate)
            for condition in ("frozen", "native") for arm in ("old", "new")
            for replicate in range(repetitions)]


def model_journals(output):
    """Read the durable dispatch identities instead of trusting usage summaries."""
    result = {}
    for directory in output.rglob("*models"):
        if not directory.is_dir():
            continue
        for path in directory.glob("*.sqlite"):
            with sqlite3.connect(path) as db:
                meta = decode_json(db.execute("SELECT value FROM provider_metadata WHERE id=1").fetchone()[0])
                rows = db.execute("SELECT request_id, request_sha, status FROM chat_requests ORDER BY request_id").fetchall()
            result[str(path.relative_to(output))] = {"configuration": meta, "requests": rows}
    return result


def test_two_fixture_roots_use_four_matched_arms_and_resume(tmp_path):
    config, output = fixture_config(), tmp_path / "pilot"
    summary = validity_pilot.create_run(config, output, roots=2)
    assert summary["registered_roots"] == 2 and summary["statistical_unit"] == "root"
    assert [report["seed"] for report in summary["reports"]] == [100, 100100]
    assert all(report["status"] == "prepared" for report in summary["reports"])
    root_plan_hashes = []
    for report in summary["reports"]:
        root = output / "roots" / report["root_id"]
        source, prepared = read_record(root / "source.json"), read_record(root / "prepared.json")
        plan_hashes = []
        for branch in branches(root):
            condition, arm = branch.parts[-3:-1]
            provenance = read_record(branch / "source.json")
            assert provenance["environment"] == source["environment"]
            assert provenance["system"] == (prepared["base"] if arm == "old" else prepared["draft"]["candidate"])
            identity = read_record(branch / "continuation.json")["identity"]
            assert identity["arm"] == arm and identity["update_policy"] == condition
            plan_hashes.append(digest(identity["plan"]))
            attempts = [read_record(branch / f"attempt-{i:04}.json") for i in range(1, 4)]
            assert all(attempt["outcome"]["complete"] for attempt in attempts)
            if condition == "frozen":
                assert not list(branch.glob("prepared-*.json"))
                assert all(attempt["prepared"] is None and attempt["decision"] is None
                           and attempt["activation"] is None for attempt in attempts)
                assert {attempt["active_snapshot_sha256"] for attempt in attempts} == {digest(provenance["system"])}
            else:
                assert len(list(branch.glob("prepared-*.json"))) == 3
                assert all(attempt["prepared"] is not None for attempt in attempts)
            for i, step in enumerate(identity["plan"]["rounds"], 1):
                assert step == read_record(root / "task-anchor.json")["independent"][0][i - 1]
        assert len(set(plan_hashes)) == 1
        root_plan_hashes.append(plan_hashes[0])
        assert report["execution_order"] == ["old_frozen/0", "new_frozen/0", "old_native/0", "new_native/0"]
    assert len(set(root_plan_hashes)) == 2
    before = model_journals(output)
    assert len(before) == 2 * 5 * 2  # proposal + four branches, two roles, two roots
    for name, journal in before.items():
        role = Path(name).stem
        assert journal["configuration"]["config"]["max_calls"] == getattr(config, role).max_calls
    assert validity_pilot.resume_run(output) == summary
    assert model_journals(output) == before


def test_workflow_native_gepa_keeps_strict_change_no_op_descendants_and_isolated_budgets(tmp_path):
    pytest.importorskip("gepa")
    config = load_platform_config(Path(__file__).parents[2] / "configs/platform_workflow_control.toml")
    config = replace(config, protocol=replace(config.protocol, mode="paired"),
                     system=replace(config.system, options={**config.system.options, "control_strategy": "unsafe_cache"}))
    output = tmp_path / "workflow"
    summary = validity_pilot.create_run(config, output, roots=1)
    report = summary["reports"][0]
    assert report["status"] == "prepared" and report["origin"] == "control"
    assert report["update_class"] == "strict_improvement" and report["primary_eligible"]
    assert report["public"]["old"]["model_calls"] == 21
    assert report["public"]["new"]["model_calls"] == 11
    assert report["public"]["old"]["synthetic_units"] == 18
    assert report["public"]["new"]["synthetic_units"] == 8
    root = output / "roots" / report["root_id"]
    risks = {}
    for branch in branches(root):
        condition, arm = branch.parts[-3:-1]
        attempts = [read_record(branch / f"attempt-{i:04}.json") for i in range(1, 7)]
        risks[f"{arm}_{condition}"] = [attempt["outcome"]["risk"] for attempt in attempts]
        assert all(attempt["outcome"]["complete"] for attempt in attempts)
        if condition == "native" and arm == "new":
            # Equality is retained in the continuation, not silently filtered out.
            for attempt in attempts:
                draft = attempt["prepared"]["draft"]
                assert decode_json(draft["native_evidence_json"])["update_class"] == "no_op"
                assert draft["native_valid"] and attempt["decision"]["disposition"] == "accept"
                assert attempt["activation"]["before_sha256"] == attempt["activation"]["after_sha256"]
    assert risks["old_frozen"] == [0] * 6
    assert risks["new_frozen"] == risks["old_native"] == risks["new_native"] == [0, 0, 1, 1, 0, 1]
    before = model_journals(output)
    assert len(before) == 10
    per_role_ids = {"editor": [], "actor": []}
    for name, journal in before.items():
        role = Path(name).stem
        assert journal["configuration"]["config"]["max_calls"] == getattr(config, role).max_calls
        ids = {row[0] for row in journal["requests"]}
        assert all(ids.isdisjoint(previous) for previous in per_role_ids[role])
        per_role_ids[role].append(ids)
        if "/frozen/" in name and role == "editor":
            assert not ids
        else:
            assert ids
    assert validity_pilot.resume_run(output) == summary
    assert model_journals(output) == before


def test_no_op_root_is_kept_as_a_registered_nonprimary_result(tmp_path):
    class NoopFixture(FixtureSystem):
        def propose(self, operation_id, evaluator):
            base = self.snapshot()
            return CandidateDraft(base.sha256, base, False,
                canonical_bytes({"update_class": "no_op", "score_gain": 0.0}).decode(), CandidateOrigin.CONTROL)

    registry = builtin_registry()
    registry.systems["fixture"] = (lambda directory, providers, options: NoopFixture(directory, **options), fixture_descriptor)
    output = tmp_path / "noop"
    summary = validity_pilot.create_run(fixture_config(), output, roots=1, registry=registry)
    report = summary["reports"][0]
    assert report["status"] == "prepared" and report["update_class"] == "no_op"
    assert not report["primary_eligible"] and not report["native_valid"]
    assert summary["aggregate"]["update_class_counts"] == {"no_op": 1}
    assert summary["aggregate"]["strict_improvement_roots"] == 0
    assert set(report["arms"]) == {"old_frozen", "new_frozen", "old_native", "new_native"}
    root = output / "roots" / report["root_id"]
    prepared = read_record(root / "prepared.json")
    assert prepared["base"] == prepared["draft"]["candidate"]
    for branch in branches(root):
        assert all(read_record(path)["outcome"]["complete"] for path in branch.glob("attempt-*.json"))
    assert validity_pilot.resume_run(output, registry=registry) == summary


def test_audit_baselines_are_frozen_before_labels_and_resume_without_calls(tmp_path, monkeypatch):
    output = tmp_path / "audited"
    original = validity_pilot.continue_system
    checks = []
    def independent(*args, **kwargs):
        root = output / "roots" / "root-100"
        prepared = read_record(root / "prepared.json")
        for name in ("current-check", "reject-all", "random-probe", "diff-probe"):
            online = read_record(root / "audits" / name / "online.json")
            assert online["identity_sha256"] == digest(prepared["identity"])
        checks.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(validity_pilot, "continue_system", independent)
    result = validity_pilot.create_run(fixture_config(), output, roots=1, audit_baselines=True)
    assert len(checks) == 4
    root = result["reports"][0]
    assert root["audit_evaluation"]["frozen"]["classification"] == "harmful"
    assert root["audits"]["current-check"]["decision"] == "accept"
    assert root["audits"]["diff-probe"]["decision"] == "reject"
    for condition in ("frozen", "native"):
        for arm in ("old", "new"):
            evidence = root["usage"]["branches"][f"{arm}_{condition}"]["execution_by_replicate"][0]
            assert evidence["complete"]
            assert evidence["unauthorized_attempts"] >= evidence["effective_violations"]
            if condition == "frozen":
                assert evidence["native_accepts"] == evidence["changed_activations"] == 0
                assert all(evidence["uses_root_snapshot"])
    journals = model_journals(output)
    assert validity_pilot.resume_run(output) == result
    assert model_journals(output) == journals


@pytest.mark.parametrize("horizon,budget", [(2, 4), (3, 2)])
def test_audit_comparison_rejects_insufficient_registration(tmp_path, horizon, budget):
    config = PlatformConfig(protocol=ProtocolConfig(mode="paired", horizon=horizon, probe_round_budget=budget),
                            detector=replace(PlatformConfig().detector, name="none"))
    with pytest.raises(ValidationError, match="H >= 3"):
        validity_pilot.create_run(config, tmp_path / "invalid", roots=1, audit_baselines=True)
    assert not (tmp_path / "invalid").exists()


def test_failed_registered_root_keeps_missing_slots_and_does_not_retry(tmp_path):
    failed_calls = []
    class FailedFixture(FixtureSystem):
        def propose(self, operation_id, evaluator):
            failed_calls.append(operation_id)
            raise BudgetExceeded("controlled root failure")

    registry = builtin_registry()
    def factory(directory, providers, options):
        cls = FailedFixture if "root-100100" in directory.parts else FixtureSystem
        return cls(directory, **options)
    registry.systems["fixture"] = (factory, fixture_descriptor)
    output = tmp_path / "missing"
    summary = validity_pilot.create_run(fixture_config(), output, roots=2, registry=registry)
    good, failed = summary["reports"]
    assert good["status"] == "prepared"
    assert failed["status"] == "proposal_failed" and failed["failure_reason"] == "BudgetExceeded"
    assert failed["origin"] is None and not failed["primary_eligible"]
    assert summary["registered_roots"] == 2
    assert summary["aggregate"]["status_counts"] == {"prepared": 1, "proposal_failed": 1}
    assert summary["aggregate"]["complete_four_arm_roots"] == 1
    assert summary["aggregate"]["arms"]["old_frozen"]["mean_risk"]["missing_roots"] == 1
    for arm in failed["arms"].values():
        assert arm["mean_risk"] is None and arm["utility"] is None
        assert arm["missing_slots"] == 3
        assert arm["risk_by_replicate"] == [[None, None, None]]
    failed_dir = output / "roots" / failed["root_id"]
    assert not (failed_dir / "prepared.json").exists() and not (failed_dir / "branches").exists()
    assert len(failed_calls) == 1
    assert validity_pilot.resume_run(output, registry=registry) == summary
    assert len(failed_calls) == 1


@pytest.mark.parametrize("mode,partition,roots", [("admission", Partition.DEV, 1),
    ("paired", Partition.SEALED, 1), ("paired", Partition.DEV, 0), ("paired", Partition.DEV, 31)])
def test_invalid_pilot_registration_fails_before_output(tmp_path, mode, partition, roots):
    config = PlatformConfig(protocol=ProtocolConfig(mode=mode, partition=partition))
    output = tmp_path / "invalid"
    with pytest.raises(ValidationError):
        validity_pilot.create_run(config, output, roots=roots)
    assert not output.exists()


def test_resume_rejects_source_fingerprint_change_before_provider_creation(tmp_path, monkeypatch):
    output = tmp_path / "run"
    validity_pilot.create_run(fixture_config(), output, roots=1)
    monkeypatch.setattr(validity_pilot, "implementation_fingerprint", lambda: "8" * 64)
    def forbidden(*_):
        pytest.fail("Cannot construct providers after source fingerprint drift")
    with pytest.raises(IntegrityError, match="implementation or protocol changed"):
        validity_pilot.resume_run(output, provider_factory=forbidden)
