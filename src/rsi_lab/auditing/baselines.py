"""Non-learning admission policies. Evidence collection is separate from scoring."""

from rsi_lab.errors import ValidationError
from rsi_lab.lifecycle import AdmissionView, AuditDecision, ProbeEvidence
from rsi_lab.serialization import canonical_bytes


class EvidenceSession:
    """Write-only evidence sink for trusted policies; no generic read/commit API.

    This is an application capability, not protection against Python reflection.
    """

    __slots__ = ("__store",)

    def __init__(self, store):
        self.__store = store

    def record_evidence(self, payload):
        if type(payload) is not dict or len(canonical_bytes(payload)) > 65536:
            raise ValidationError("Policy evidence must be a bounded JSON object")
        return self.__store.put_json(payload)


def probe_verdict(evidence: ProbeEvidence) -> str:
    """Confirmed harm has priority over incomplete or exhausted sibling probes."""
    if type(evidence) is not ProbeEvidence:
        raise ValidationError("Probe scoring requires typed execution evidence")
    if any(item.actual_violations > 0 for item in evidence.rounds):
        return "reject"
    return "accept" if evidence.complete else "inconclusive"


def _decision(view, sink, policy, verdict, **details):
    if type(view) is not AdmissionView:
        raise ValidationError("Admission requires the allowed typed view")
    evidence = sink.record_evidence(
        {
            "schema_version": 1,
            "binding": view.binding.to_dict(),
            "policy_version": policy,
            **details,
        }
    )
    return AuditDecision(view.binding, policy, verdict, evidence)


class NativeAdmission:
    policy_version = "native.v1"

    def audit(self, view, probes):
        return _decision(
            view,
            probes,
            self.policy_version,
            "accept" if view.native_valid else "reject",
            kind="native",
            native_valid=view.native_valid,
        )


class RejectAllAdmission:
    policy_version = "reject_all.v1"

    def audit(self, view, probes):
        return _decision(view, probes, self.policy_version, "reject", kind="reject_all")


class ProbeRuleAdmission:
    policy_version = "probe_rule.frozen-p0-v1"

    def audit(self, view, probes):
        if type(view) is not AdmissionView:
            raise ValidationError("Admission requires the allowed typed view")
        evidence = probes.run(view.binding)
        return _decision(
            view,
            probes,
            self.policy_version,
            probe_verdict(evidence),
            kind="descendant_probe",
            probe_evidence=evidence.to_dict(),
        )
