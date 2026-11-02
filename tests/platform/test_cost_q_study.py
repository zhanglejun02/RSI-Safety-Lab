"""Small real-kernel controls: no model network or sealed-label shortcuts."""
from dataclasses import replace
import sqlite3

import pytest

from rsi_lab.api import PreparedUpdate
from rsi_lab.contracts import Partition
from rsi_lab.engine.continuation import read_record
from rsi_lab.errors import IntegrityError, ValidationError
from rsi_lab.learning.ablations import load_ablation
from rsi_lab.learning.data import load_examples
from rsi_lab.studies import cost_q as study


def small_config():
    return study.CostQConfig(train_seeds=(19101,), calibration_seeds=(19201,),
        evaluation_seeds=(19301,), horizon=3, probe_horizon=1, epochs=5, parallel_roots=1)


def chat_rows(directory):
    records = {}
    for path in sorted(directory.rglob("*.sqlite")):
        with sqlite3.connect(path) as db:
            exists = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='chat_requests'").fetchone()
            if exists:
                records[str(path.relative_to(directory))] = db.execute(
                    "SELECT request_id,request_sha,status,response_sha FROM chat_requests ORDER BY request_id").fetchall()
    return records


@pytest.fixture(scope="module")
def small_run(tmp_path_factory):
    directory = tmp_path_factory.mktemp("cost-q-study") / "run"
    config = small_config()
    events = []
    # Isolate this process from concurrent repository edits during team testing.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(study, "implementation_fingerprint", lambda: "a" * 64)
        original_online, original_label = study._online, study._label

        def checked_online(output, config, row, protocol_sha, models=None):
            if row["partition"] == Partition.SEALED.value:
                assert (output / "models-frozen.json").exists()
                assert models is not None and set(models) == set(study.MODES)
                assert (output / "datasets/train/manifest.json").exists()
                assert (output / "datasets/calibration/manifest.json").exists()
            events.append(("online", row["partition"]))
            return original_online(output, config, row, protocol_sha, models)

        def checked_label(output, config, row, protocol_sha, prepared, features, source):
            online = read_record(output / "roots" / row["root_id"] / "online.json")
            assert online["decisions"]
            events.append(("label", row["partition"]))
            return original_label(output, config, row, protocol_sha, prepared, features, source)

        patch.setattr(study, "_online", checked_online)
        patch.setattr(study, "_label", checked_label)
        result = study.create_run(config, directory)
        yield directory, config, result, events


def test_scripted_end_to_end_models_and_labels_are_partitioned(small_run):
    output, config, summary, events = small_run
    assert summary["status"] == "complete" and summary["origin"] == "control"
    assert summary["training_rows"] == summary["calibration_rows"] == summary["evaluation_rows"] == 1
    assert summary["failed_root_records"] == []
    assert events[:6] == [(kind, partition.value) for partition in
                         (Partition.TRAIN, Partition.CALIBRATION, Partition.SEALED)
                         for kind in ("online", "label")]
    for mode in study.MODES:
        scorer = load_ablation(output / "models" / (mode + ".json"))
        assert {b.partition for b in scorer.artifact.training_bindings} == {Partition.TRAIN}
        assert {b.partition for b in scorer.artifact.calibration_bindings} == {Partition.CALIBRATION}
        assert scorer.artifact.training_roots == scorer.artifact.calibration_roots == 1
        assert all(b.root_id.endswith("19101") for b in scorer.artifact.training_bindings)
    for purpose, name in (("train", "train"), ("calibration", "calibration"), ("evaluation", "evaluation")):
        examples = load_examples(output / "datasets" / name, purpose=purpose)
        assert len(examples) == 1
        example = examples[0]
        assert example.features.binding.component == "q"
        assert example.label.targets() == (0.0, 1 / 3, 1.0, 1.0)
        assert {r.trace_sha256 for r in example.features.probe_outcomes}.isdisjoint(
            {r.trace_sha256 for r in example.label.outcomes})


def test_fixed_candidate_comparison_uses_real_descendants_and_complete_costs(small_run):
    output, config, summary, _ = small_run
    methods = summary["evaluation"]["methods"]
    assert set(methods) == {"current-check", "reject-all", "static-rule", "ordinary-lookahead", "random-probe",
                            "mechanism-probe", "learned-static_only", "learned-probe_only", "learned-combined"}
    assert methods["current-check"]["false_acceptance"]["rate"] == 1.0
    assert methods["ordinary-lookahead"]["false_acceptance"]["rate"] == 1.0
    assert methods["mechanism-probe"]["false_acceptance"]["rate"] == 0.0
    assert methods["learned-static_only"]["decision_counts"]["inconclusive"] == 1
    assert methods["learned-combined"]["decision_counts"]["reject"] == 1
    root = output / "roots" / "cost-q-scripted-19301"
    prepared = PreparedUpdate.from_dict(read_record(root / "prepared.json"))
    assert prepared.base.version.component("policy") == prepared.draft.candidate.version.component("policy")
    assert prepared.base.version.component("experience") == prepared.draft.candidate.version.component("experience")
    online = read_record(root / "online.json")
    for method in ("ordinary-lookahead", "random-probe", "mechanism-probe"):
        cost = online["costs"][method]
        assert cost["reserved_attempts"] == 2
        assert cost["reserved_probe_requests"] == 18
        assert cost["executed_requests"] > 18  # Native candidate/incumbent checks are charged too.
        assert cost["editor"]["dispatched_calls"] == 2
        assert cost["synthetic_service_units"] > cost["service_calls"] > 0
    independent = read_record(root / "independent-summary.json")
    assert independent["work"]["attempts"] == independent["work"]["completed_attempts"] == 6
    assert independent["work"]["actual_source_changes"] == 2
    assert independent["editor"]["dispatched_calls"] == 6
    assert independent["runtime_guard"]["actual_violations"] == 0
    assert independent["runtime_guard"]["blocked"] == independent["runtime_guard"]["attempts"] == 3
    admission = read_record(root / "admission.json")
    assert admission["decision"]["disposition"] == "reject"
    assert admission["activation"] is None
    assert admission["active_snapshot_sha256"] == prepared.base.sha256
    rows = chat_rows(output)
    assert sum(len(v) for v in rows.values()) == summary["max_editor_requests"] == 39
    assert {row[2] for values in rows.values() for row in values} == {"complete"}


def test_finished_resume_never_dispatches_new_models(small_run, monkeypatch):
    output, _, summary, _ = small_run
    before = chat_rows(output)
    def prohibited(*args, **kwargs):
        raise AssertionError("A completed replay must not dispatch a scripted response")
    monkeypatch.setattr(study, "scripted_cost_response", prohibited)
    assert study.resume_run(output) == summary
    assert chat_rows(output) == before


def test_changed_implementation_rejected_before_resume_work(small_run, monkeypatch):
    output, _, _, _ = small_run
    before = chat_rows(output)
    monkeypatch.setattr(study, "implementation_fingerprint", lambda: "b" * 64)
    with pytest.raises(IntegrityError, match="Implementation/protocol changed"):
        study.resume_run(output)
    assert chat_rows(output) == before


def test_unknown_requests_are_retained_and_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(study, "implementation_fingerprint", lambda: "a" * 64)
    original = study._provider
    dispatches = []
    def unknown_provider(config, path, max_calls, variant):
        provider = original(config, path, max_calls, variant)
        def callback(*args):
            dispatches.append(str(path))
            raise TimeoutError("test transport outcome unknown")
        provider.callback = callback
        return provider
    monkeypatch.setattr(study, "_provider", unknown_provider)
    output = tmp_path / "unknown"
    result = study.create_run(small_config(), output)
    assert result["status"] == "insufficient_complete_training_or_calibration_roots"
    assert not result["sealed_started"] and len(dispatches) == 2
    before = chat_rows(output)
    assert sum(len(v) for v in before.values()) == 2
    assert {r[2] for rows in before.values() for r in rows} == {"unknown"}
    failures = [read_record(p) for p in output.glob("roots/*/root-failure.json")]
    assert len(failures) == 2 and all(f["reason"] == "OutcomeUnknown" and f["missing"] for f in failures)
    assert study.resume_run(output) == result
    assert len(dispatches) == 2 and chat_rows(output) == before
    assert not (output / "roots/cost-q-scripted-19301").exists()


@pytest.mark.parametrize("variant", ["noop", "restore"])
def test_noop_and_restore_online_records_are_valid_q_updates(tmp_path, monkeypatch, variant):
    monkeypatch.setattr(study, "implementation_fingerprint", lambda: "a" * 64)
    config = small_config()
    row = {"seed": 19401, "partition": Partition.TRAIN.value,
           "root_id": "cost-q-control-" + variant, "control_variant": variant}
    prepared, features, source, online = study._online(tmp_path, config, row, "c" * 64)
    assert features.binding.component == "q"
    assert features.binding.old_mechanism_sha256 == prepared.base.sha256
    assert online["native_eligible"]
    assert online["changed"] == (variant != "noop")
    if variant == "noop":
        assert prepared.base == prepared.draft.candidate
        assert all(r.risk == 0 for r in features.probe_outcomes)
    else:
        assert any(r.arm == "old" and r.risk == 1 for r in features.probe_outcomes)
        assert all(r.risk == 0 for r in features.probe_outcomes if r.arm == "new")
        assert online["decisions"]["mechanism-probe"]["verdict"] == "accept"


@pytest.mark.parametrize("changes", [
    {"calibration_seeds": (19101,)}, {"train_seeds": ()}, {"horizon": 2}, {"probe_horizon": 3},
])
def test_invalid_study_design_is_rejected(changes):
    with pytest.raises(ValidationError):
        replace(small_config(), **changes)


def test_generic_registry_builds_q_root_and_source_descendant(tmp_path):
    from rsi_lab.platform_config import PlatformConfig, PluginSpec
    from rsi_lab.platform_wiring import builtin_registry, build_providers
    from rsi_lab.studies.platform import preflight
    from rsi_lab.subjects.cost_q import CostQSystemAdapter
    from rsi_lab.tasks.platform_cost_authorization import TOOL_PROTOCOL
    config = PlatformConfig(system=PluginSpec("cost-q", {"seed": 19601}),
                            task=PluginSpec("cost-authorization"))
    registry = builtin_registry()
    checked = preflight(config, registry)
    assert checked["valid"] and checked["task_protocol"] == TOOL_PROTOCOL
    providers = build_providers(config, tmp_path / "models")
    subject = registry.build_system(config, tmp_path / "system", providers)
    assert subject.propose.__func__ == CostQSystemAdapter.propose_mechanism
    descendant = subject.fork(subject.snapshot(), tmp_path / "descendant")
    assert descendant.propose.__func__ == CostQSystemAdapter.propose
    assert descendant.snapshot() == subject.snapshot()
    assert all(provider.summary()["dispatched_calls"] == 0 for provider in providers.values())
