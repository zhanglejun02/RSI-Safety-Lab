from types import SimpleNamespace

import pytest

from rsi_lab.engine.continuation import read_record
from rsi_lab.errors import IntegrityError
from rsi_lab.studies import stress_finance_warning as warning
from rsi_lab.tasks import stress_finance as finance


def test_probe_distinguishes_revocation_but_misses_other_mechanism():
    seed = 2049051107
    safe = warning.probe(finance.SAFE_SOURCE, seed)
    harmful = warning.probe(finance.UNSAFE_SOURCES["approval_revocation"], seed)
    different = warning.probe(finance.UNSAFE_SOURCES["receipt_exhaustion"], seed)
    assert safe["prediction"] == different["prediction"] == "clear"
    assert harmful["prediction"] == "alert"
    assert [(w["arm"], w["risk"]) for w in harmful["cases"]] == [("control", 0), ("stress", 1)]
    assert warning.probe("???", seed)["prediction"] == "abstain"


def test_pre_activation_warning_binds_source_and_does_not_repeat_probe(tmp_path, monkeypatch):
    from rsi_lab.api import UpdateIdentity
    from rsi_lab.auditing.platform import AdmissionDecision
    from rsi_lab.contracts import CandidateOrigin, Partition
    from rsi_lab.serialization import digest

    root = {"domain": "finance", "root_id": "probe-1", "seed": 2100000}
    sha = digest("test")
    identity = UpdateIdentity("proposal-1", "probe-1", "group", sha, sha, sha, sha,
                              Partition.DEV, CandidateOrigin.CONTROL, 3, sha)
    decision = AdmissionDecision(identity, digest("report"), "public-test", "accept", "public-correct")
    prepared = SimpleNamespace(identity=identity)
    evidence = {"proposed_source": finance.SAFE_SOURCE}
    warning.record_warning(tmp_path, root, prepared, decision, evidence)
    assert read_record(tmp_path / "warning.json")["prediction"] == "clear"
    monkeypatch.setattr(warning, "probe", lambda *args: pytest.fail("retried a frozen probe"))
    warning.record_warning(tmp_path, root, prepared, decision, evidence)
    with pytest.raises(IntegrityError):
        warning.record_warning(tmp_path, root, prepared, decision, {"proposed_source": "different"})
