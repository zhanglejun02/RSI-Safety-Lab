"""Candidate-bound authorization and uncertain activation recovery."""
from dataclasses import replace

import pytest

from rsi_lab.api import (Capabilities, CandidateDraft, Component, PreparedUpdate,
                        SystemDescriptor, SystemSnapshot, SystemVersion, UpdateIdentity)
from rsi_lab.auditing.platform import AdmissionDecision
from rsi_lab.contracts import CandidateOrigin, Partition
from rsi_lab.engine.activation import ActivationController
from rsi_lab.errors import ConflictError, IntegrityError, OutcomeUnknown, UnsupportedCapability, ValidationError
from rsi_lab.serialization import canonical_bytes, digest
from rsi_lab.subjects.local import LocalSystemAdapter


class System(LocalSystemAdapter):
    calls = 0
    lost = False
    unknown = False
    forged = False
    wrong_state = False
    capabilities = Capabilities(snapshot_restore=True, propose=True, pre_activation=True,
                                reconcile_activation=True, mutable_components=("prompt",))

    def describe(self):
        return SystemDescriptor("test-system", "Test system", self.capabilities,
                                "https://example.invalid/system", "fixed-revision")

    def activate(self, command):
        self.calls += 1
        if self.unknown:
            raise OutcomeUnknown("Remote result is unknown")
        receipt = super().activate(command)
        if self.lost:
            raise OutcomeUnknown("Response was lost after activation")
        if self.forged:
            return replace(receipt, after_sha256=digest("wrong"))
        if self.wrong_state:
            with self._db() as db:
                db.execute("UPDATE system_state SET body=? WHERE id=1", (canonical_bytes(initial()),))
        return receipt


def initial():
    return SystemSnapshot(SystemVersion("test-system", (
        Component("prompt", '{"text":"safe"}', "agent"),
        Component("protected", '{"key":"unchanged"}'),
    ), "fixed-revision"))


def prepare(base, key="u1", candidate=None, *, native_valid=True):
    if candidate is None:
        candidate = replace(base, version=replace(base.version, components=(
            Component("prompt", canonical_bytes({"text": key}).decode(), "agent"),
            base.version.components[1],
        )))
    identity = UpdateIdentity(key, "root", "history", digest("protocol"), base.sha256,
                              candidate.sha256, digest("environment"), Partition.DEV,
                              CandidateOrigin.CONTROL, 6, digest("task-plan"))
    return PreparedUpdate(identity, base, CandidateDraft(base.sha256, candidate,
                           native_valid, "{}", CandidateOrigin.CONTROL))


def decision(prepared, disposition="accept"):
    return AdmissionDecision(prepared.identity, digest("report"), "policy-v1", disposition, "test")


def apply(controller, subject, prepared, disposition="accept", **kwargs):
    return controller.apply(subject, prepared, decision(prepared, disposition),
        environment_sha256=kwargs.pop("environment_sha256", prepared.identity.environment_sha256), **kwargs)


@pytest.fixture
def setup(tmp_path):
    return (ActivationController(tmp_path / "controller.sqlite", digest("protocol")),
            System(tmp_path / "subject", initial()))


def test_activation_is_idempotent_and_history_cannot_rewind_head(setup):
    controller, subject = setup
    first = prepare(initial())
    receipt = apply(controller, subject, first)
    assert apply(controller, subject, first) == receipt
    assert subject.calls == 1
    assert subject.snapshot() == first.draft.candidate
    second = prepare(subject.snapshot(), "u2")
    apply(controller, subject, second)
    assert subject.calls == 2
    with pytest.raises(ConflictError, match="historical"):
        apply(controller, subject, first)
    assert subject.snapshot() == second.draft.candidate
    assert subject.calls == 2


@pytest.mark.parametrize("disposition", ["reject", "inconclusive"])
def test_rejected_candidate_is_not_inherited_or_reauthorized(setup, disposition):
    controller, subject = setup
    prepared = prepare(initial(), candidate=replace(initial(), state_json='{"memory":"poisoned"}'))
    assert apply(controller, subject, prepared, disposition) is None
    assert apply(controller, subject, prepared, disposition) is None
    assert subject.snapshot() == initial()
    assert subject.calls == 0
    with pytest.raises(ConflictError, match="different admission decision"):
        apply(controller, subject, prepared)
    assert subject.calls == 0


def test_source_and_environment_staleness_rejected_before_dispatch(setup):
    controller, subject = setup
    first, stale = prepare(initial(), "u1"), prepare(initial(), "stale")
    with pytest.raises(ConflictError, match="environment"):
        apply(controller, subject, first, environment_sha256=digest("other environment"))
    assert subject.calls == 0
    apply(controller, subject, first)
    with pytest.raises(ConflictError, match="state changed"):
        apply(controller, subject, stale)
    assert subject.calls == 1


def test_lost_response_recovers_only_by_query_without_duplicate_activation(setup):
    controller, subject = setup
    prepared = prepare(initial())
    subject.lost = True
    with pytest.raises(OutcomeUnknown):
        apply(controller, subject, prepared)
    assert subject.calls == 1
    assert subject.snapshot() == prepared.draft.candidate
    restarted = ActivationController(controller.path, digest("protocol"))
    result = apply(restarted, subject, prepared)
    assert result.after_sha256 == prepared.draft.candidate.sha256
    assert subject.calls == 1


def test_unqueryable_outcome_is_not_resent(setup):
    controller, subject = setup
    prepared = prepare(initial())
    subject.unknown = True
    with pytest.raises(OutcomeUnknown):
        apply(controller, subject, prepared)
    subject.unknown = False
    with pytest.raises(OutcomeUnknown, match="do not resend"):
        apply(controller, subject, prepared)
    assert subject.calls == 1
    assert subject.snapshot() == initial()


@pytest.mark.parametrize("fault", ["forged", "wrong_state"])
def test_receipt_must_match_authorization_and_actual_state(setup, fault):
    controller, subject = setup
    setattr(subject, fault, True)
    with pytest.raises((ConflictError, IntegrityError)):
        apply(controller, subject, prepare(initial()))
    assert subject.calls == 1


def test_identity_native_validation_and_capabilities_are_enforced(setup):
    controller, subject = setup
    prepared = prepare(initial())
    other = prepare(initial(), "other")
    with pytest.raises(ValidationError, match="bound"):
        controller.apply(subject, prepared, decision(other),
                         environment_sha256=prepared.identity.environment_sha256)
    with pytest.raises(ValidationError, match="native validation"):
        apply(controller, subject, prepare(initial(), native_valid=False))
    subject.capabilities = replace(subject.capabilities, pre_activation=False)
    with pytest.raises(UnsupportedCapability, match="pre-activation"):
        apply(controller, subject, prepared)
    assert subject.calls == 0


def test_undeclared_component_and_upstream_replacement_are_rejected(setup):
    controller, subject = setup
    protected = replace(initial(), version=replace(initial().version,
        components=(initial().version.components[0], Component("protected", '{"key":"changed"}'))))
    with pytest.raises(UnsupportedCapability, match="undeclared components"):
        apply(controller, subject, prepare(initial(), candidate=protected))
    upstream = replace(initial(), version=replace(initial().version, upstream_revision="unreviewed"))
    with pytest.raises(IntegrityError, match="registered adapter"):
        apply(controller, subject, prepare(initial(), candidate=upstream))
    assert subject.calls == 0


def test_candidate_bytes_cannot_keep_previous_identity():
    prepared = prepare(initial())
    changed = replace(prepared.draft.candidate, state_json='{"different":true}')
    with pytest.raises(ValidationError, match="identity/content mismatch"):
        replace(prepared, draft=replace(prepared.draft, candidate=changed))
