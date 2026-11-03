import pytest

pytest.importorskip("openevolve")
from rsi_lab.studies.openevolve_screen import _paired_test, _diagnostic, static_certificate
from rsi_lab.tasks import cost_authorization as auth, retail_workflow as retail


def test_fixed_source_diagnostics_replay_and_separate_axes(tmp_path):
    for task, source in (("authorization", auth.SAFE_REFERENCE_SOURCE), ("retail", retail.SAFE_SOURCE)):
        result = _diagnostic(source, task, 10, "independent", tmp_path / task)
        assert result["risk"] == 0 and result["legal_completed"] == result["legal_total"]
        assert result == _diagnostic(source, task, 10, "independent", tmp_path / task)


def test_repetition_permutation_counts_trajectories_not_requests():
    roots = [{"continuations": [{"arm": arm, "path_risk": int(arm == "new")} for arm in ("old", "new") for _ in range(2)]} for _ in range(2)]
    result = _paired_test(roots)
    assert result["difference"] == 1 and result["one_sided_p"] == pytest.approx(1/36)
    roots[0]["continuations"][0]["path_risk"] = None
    assert _paired_test(roots)["complete_roots"] == 1


def test_static_certificate_does_not_reject_unknown_programs():
    assert static_certificate(retail.SAFE_SOURCE, "retail") == "accept"
    assert static_certificate(retail.SAFE_SOURCE.replace('    return cache', '    api.now()\n    return cache'), "retail") == "inconclusive"


def test_continuation_public_validation_is_not_independent_cost(tmp_path):
    from rsi_lab.engine.continuation import save_record
    from rsi_lab.errors import IntegrityError
    from rsi_lab.studies.openevolve_screen import execution_accounting
    branch = tmp_path / "roots" / "retail-1" / "continuations" / "old" / "0"
    for split, folder in (("public", branch / "public" / "candidate"), ("independent", branch)):
        stream = retail.make_suite(1, split)[0]
        result = retail.evaluate_source(retail.SAFE_SOURCE, [stream])
        save_record(folder / "environment" / "operations" / "one.json", {"stream": stream, "result": result})
    measured = execution_accounting(tmp_path)
    assert measured["public_optimizer_and_rechecks"]["executions"] == 1
    assert measured["independent_continuation"]["executions"] == 1
    stream = {**stream, "split": "unknown"}
    save_record(branch / "environment" / "operations" / "bad.json", {"stream": stream, "result": result})
    with pytest.raises(IntegrityError, match="Unexpected task split"):
        execution_accounting(tmp_path)
