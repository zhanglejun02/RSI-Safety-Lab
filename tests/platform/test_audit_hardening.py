"""Concrete audit regressions; do not silently redefine frozen outcomes."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import hashlib
import json
from zipfile import ZipFile

import pytest

from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.errors import IntegrityError, ValidationError
from rsi_lab.serialization import digest
from rsi_lab.studies import stress_bench as corpus, stress_pilot as pilot
from rsi_lab.studies.stress_scorecard import _effects, _evidence_scope, _success
from rsi_lab.tasks import stress_bench as tasks


def test_selected_mechanism_corpus_build_check_score_and_versions(tmp_path):
    manifest = corpus.build(tmp_path / "corpus", seeds=1, domains=("feedback", "quota"))
    assert manifest["protocol"] == tasks.EXTENDED_CORPUS_VERSION
    assert manifest["domains"] == ["feedback", "quota"]
    assert manifest["task_versions"]["feedback"] == tasks.backend("feedback").VERSION
    assert "tasks/mechanism_execution.py" in manifest["implementation"]
    result = corpus.check(tmp_path / "corpus", tmp_path / "controls")
    assert result["validation_passed"] and result["http_calls"] == 0
    programs = {}
    for domain in manifest["domains"]:
        path = tmp_path / (domain + ".py")
        path.write_text(tasks.backend(domain).SAFE_SOURCE)
        programs[domain] = path.name
    (tmp_path / "programs.json").write_text(json.dumps(programs))
    scored = corpus.score(tmp_path / "corpus", tmp_path / "programs.json", "safe", tmp_path / "scores")
    assert set(scored["programs_sha256"]) == {"feedback", "quota"}
    assert all(r["metrics"]["risk"] == 0 for r in scored["rows"])
    bad = dict(manifest); bad["task_versions"] = {**manifest["task_versions"], "feedback": "wrong"}
    p = tmp_path / "corpus/manifest.json"
    p.unlink(); save_record(p, bad)
    with pytest.raises(IntegrityError):
        corpus.load_corpus(tmp_path / "corpus")


def test_original_default_domain_scope_and_case_identity_remain():
    assert tasks.DOMAINS == ("authorization", "finance", "retail", "privacy")
    assert tasks.corpus_version(tasks.DOMAINS) == "rsi-stress-bench-dev-v0.2"
    assert tasks.execution_version("privacy") == "privacy-pressure-v1"
    assert tasks.execution_version("finance") == "invoice-payment-v1"


@pytest.mark.parametrize("domains", [("finance",), ("authorization", "privacy"), tuple(reversed(tasks.DOMAINS))])
def test_nondefault_domain_selections_have_new_corpus_version(tmp_path, domains):
    manifest = corpus.build(tmp_path / "corpus", seeds=1, domains=domains)
    assert manifest["protocol"] == tasks.EXTENDED_CORPUS_VERSION
    loaded, _ = corpus.load_corpus(tmp_path / "corpus")
    assert loaded == manifest


def test_new_manifests_bind_runtime_version_and_validator_bytes():
    from rsi_lab.studies import mechanism_pilot, adaptation_rl_pilot
    registered, _ = mechanism_pilot.registration()
    assert registered["manifest"]["task_revisions"]["privacy"] == "privacy-pressure-v1"
    assert "scripts/verify_mechanism_pilot.py" in registered["source_hashes"]
    registered, _ = adaptation_rl_pilot.registration()
    assert "scripts/verify_adaptation_rl_pilot.py" in registered["source_hashes"]


@pytest.mark.parametrize("error_type", [KeyError, ValidationError])
def test_programming_error_stops_instead_of_becoming_a_missing_model_reply(tmp_path, monkeypatch, error_type):
    template = Path("configs/stress_pilot_control.toml").read_text()
    config = tmp_path / "config.toml"; config.write_text(template)
    adapter_factory = pilot._adapter
    def adapter(*args, **kwargs):
        value = adapter_factory(*args, **kwargs)
        def bug(*args, **kwargs):
            raise error_type("injected internal error")
        value.propose = bug
        return value
    monkeypatch.setattr(pilot, "_adapter", adapter)
    with pytest.raises(error_type):
        pilot.run(config, tmp_path / "run", analyze=False)
    errors = list((tmp_path / "run").rglob("fatal.json"))
    assert len(errors) == 1 and read_record(errors[0])["failure_kind"] == "unexpected_internal_error"
    assert not list((tmp_path / "run").rglob("missing.json"))
    assert not (tmp_path / "run/generation_complete.json").exists()
    with pytest.raises(IntegrityError, match="recorded internal failure"):
        pilot.run(config, tmp_path / "run", resume=True, analyze=False)


def slot_fixture(tmp_path, system, callback):
    from rsi_lab.infra.chat_models import ScriptedChatProvider
    config, kind, settings = pilot.read_config("configs/stress_pilot_control.toml")
    manifest = pilot._manifest(config, kind, settings)
    root = next(r for r in manifest["root_ids"] if r["domain"] == "finance" and r["system"] == system)
    module = tasks.backend("finance")
    package = pilot.OrdinaryPublicTasks("finance")
    base = pilot._initial(system, root["seed"], module, package)
    provider = ScriptedChatProvider(tmp_path / root["path"] / "model.sqlite", callback)
    adapter = pilot._adapter(system, tmp_path / root["path"] / "system", provider, base)
    return root, adapter, package, provider, manifest


@pytest.mark.parametrize("system", pilot.SYSTEMS)
def test_native_worker_cannot_hide_host_candidate_evaluation_failure(tmp_path, monkeypatch, system):
    source = tasks.backend("finance").SAFE_SOURCE
    args = slot_fixture(tmp_path, system, lambda *_: {
        "response" if system == "openevolve" else "text": "```python\n" + source + "\n```"})
    root, adapter, package, provider, manifest = args
    base = adapter.snapshot()
    evaluate = pilot.PublicTaskEvaluator.evaluate
    def broken(self, snapshot, operation):
        if snapshot.version.component("policy")["source"].strip() == source.strip():
            raise ValidationError("host evaluator invariant")
        return evaluate(self, snapshot, operation)
    monkeypatch.setattr(pilot.PublicTaskEvaluator, "evaluate", broken)
    with pytest.raises(ValidationError, match="host evaluator invariant"):
        pilot._slot(tmp_path, root, 1, adapter, package, provider, manifest)
    directory = tmp_path / root["path"] / "step-0001"
    assert read_record(directory / "fatal.json")["error"] == "ValidationError"
    assert not (directory / "missing.json").exists() and not (directory / "slot.json").exists()
    assert adapter.snapshot() == base and provider.summary()["dispatched_calls"] == 1
    with pytest.raises(IntegrityError, match="recorded internal failure"):
        pilot._slot(tmp_path, root, 1, adapter, package, provider, manifest)
    assert provider.summary()["dispatched_calls"] == 1


@pytest.mark.parametrize("system", pilot.SYSTEMS)
def test_invalid_response_shape_consumes_slot_without_inheritance_or_retry(tmp_path, system):
    root, adapter, package, provider, manifest = slot_fixture(tmp_path, system, lambda *_: {"wrong_field": "output"})
    base = adapter.snapshot()
    result = pilot._slot(tmp_path, root, 1, adapter, package, provider, manifest)
    assert result["status"] == "missing" and result["failure_kind"] == "model_response_validation_failure"
    assert not result["committed"] and adapter.snapshot() == base
    assert pilot._slot(tmp_path, root, 1, adapter, package, provider, manifest) == result
    assert provider.summary()["dispatched_calls"] == 1


def test_historical_verifier_explains_exact_source_requirement(monkeypatch):
    spec = spec_from_file_location("verify_mechanism", Path("scripts/verify_mechanism_pilot.py"))
    module = module_from_spec(spec); spec.loader.exec_module(module)
    monkeypatch.setattr(module, "read_record", lambda _: {"different": "archived-version"})
    monkeypatch.setattr(module.study, "registration", lambda: ({"current": "version"}, b""))
    with pytest.raises(IntegrityError, match="scripts/review_frozen_run.py"):
        module.verify()


def test_missing_proposal_and_known_active_effects_have_separate_sensitivity():
    e = {"complete": True, "risk": 0, "actual_violations": 0, "cost": 80,
         "legal_completed": 1, "legal_total": 1, "correct_requests": 1, "total_requests": 1, "correctness": 1}
    root = {"registered_steps": 1, "missing_steps": [1], "safe_cost_comparison_available": False,
            "founder": {**e, "cost": 100}, "final": e, "active_steps": [{"step": 1, "evaluation": e}]}
    effects = _effects(root)
    success = {"one_percent": _success(effects, 100)[0]}
    assert success == {"one_percent": 1}
    scope = _evidence_scope(root, effects, success)
    assert scope["active_effect_evidence_complete"] and not scope["generation_response_complete"]
    assert scope["complete_generation_sensitivity"]["one_percent"] is None
    assert _evidence_scope(root, effects, {"one_percent": 0})["complete_generation_sensitivity"]["one_percent"] == 0


def reviewer():
    spec = spec_from_file_location("review_frozen_run", Path("scripts/review_frozen_run.py"))
    module = module_from_spec(spec); spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", ["../escaped.py", "/absolute.py", "src\\escaped.py"])
def test_frozen_review_rejects_archive_path_escape(tmp_path, name):
    script = reviewer(); raw = b"pass\n"
    archive = tmp_path / "source-snapshot.zip"
    with ZipFile(archive, "w") as z:
        z.writestr(name, raw)
    save_record(tmp_path / "registration.json", {"source_hashes": {name: hashlib.sha256(raw).hexdigest()},
        "snapshot_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()})
    with pytest.raises(ValueError, match="escaped"):
        script.validate_archive(tmp_path)


def test_frozen_review_checks_each_source_hash_and_original_output_boundary(tmp_path):
    script = reviewer(); archive = tmp_path / "source-snapshot.zip"
    with ZipFile(archive, "w") as z:
        z.writestr("src/rsi_lab/example.py", b"changed\n")
    save_record(tmp_path / "registration.json", {"source_hashes": {"src/rsi_lab/example.py": digest("wrong")},
        "snapshot_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()})
    with pytest.raises(ValueError, match="differs"):
        script.validate_archive(tmp_path)
    with pytest.raises(ValueError, match="outside"):
        script.review(tmp_path / "run", tmp_path, tmp_path / "run/overwrite")


def test_hypothetical_power_matches_one_direction_discordance_limit():
    from math import comb
    spec = spec_from_file_location("evidence_audit", Path("scripts/audit_existing_evidence.py"))
    module = module_from_spec(spec); spec.loader.exec_module(module)
    # If all discordant pairs favor A, exact two-sided rejection requires at
    # least six discordant pairs (2 / 2**6 <= .05). This is a binomial tail.
    expected = 1 - sum(comb(64, k) * .125**k * .875**(64-k) for k in range(6))
    assert module.paired_power(64, .125, .125) == pytest.approx(expected)
    assert 0 <= module.paired_power(64, 0, .5) <= .05
