"""Explicit assembly and preflight. No plugin scanning or global LabContext."""

from .config import LabConfig
from .contracts import CandidateOrigin, PairingMode, Partition
from .errors import UnsupportedCapability


def preflight(config: LabConfig):
    if config.stage == "M4":
        from .contracts import ForkPoint

        if (
            config.runtime != "trusted_fixture"
            or config.subject != "fake"
            or config.arbitrary_code_execution
            or config.partition != Partition.DEV
            or config.candidate_origin != CandidateOrigin.CONTROL
            or config.frozen_definition_version != "frozen-p0-v1"
            or config.fixture_version != "transfer-v1"
            or config.task != "workflow"
            or config.suite != "transfer-v1"
        ):
            raise UnsupportedCapability(
                "M4 S0 requires the registered trusted control development fixture"
            )
        if (
            config.continuation != "frozen_mechanism"
            or config.allowed_updates != ("agent",)
            or config.frozen_components != ("q", "v", "s")
            or config.probe_replicates != 2
            or config.probe_horizon_attempts != 2
        ):
            raise UnsupportedCapability(
                "Frozen P0 requires agent-only continuation and K=2/H_probe=2"
            )
        if config.study not in (
            "s0_smoke",
            "s0_q_pairs",
            "s0_v_fixed_pool",
            "s0_probe_admission",
        ):
            raise UnsupportedCapability("Unknown M4 S0 study")
        fixed = config.study == "s0_v_fixed_pool"
        if fixed and (
            config.pairing.mode != PairingMode.FIXED_CANDIDATE
            or config.pairing.fork_at != ForkPoint.PRE_VERIFICATION
            or not config.pairing.verification_intervention
        ):
            raise UnsupportedCapability(
                "S0 v interventions require fixed_candidate at pre_verification"
            )
        if not fixed and (
            config.pairing.mode != PairingMode.PROCESS
            or config.pairing.verification_intervention
        ):
            raise UnsupportedCapability(
                "S0 process studies cannot declare fixed verification intervention"
            )
        if config.study == "s0_probe_admission" and config.admission_policy != "probe":
            raise UnsupportedCapability("s0_probe_admission requires the probe policy")
        if (
            config.study in ("s0_q_pairs", "s0_v_fixed_pool")
            and config.admission_policy != "native"
        ):
            raise UnsupportedCapability(
                "Frozen S0 mechanism comparisons require native admission"
            )
        if config.admission_policy == "probe" and (
            config.guard_mode != "observe" or config.study == "s0_q_pairs"
        ):
            raise UnsupportedCapability(
                "Frozen probes require observe and q.old; q intervention probes need a new protocol"
            )
        return
    if config.stage == "M3":
        from .contracts import ForkPoint, RandomPlan

        if (
            config.runtime != "trusted_fixture"
            or config.subject != "fake"
            or config.arbitrary_code_execution
            or config.partition != Partition.DEV
            or config.candidate_origin != CandidateOrigin.CONTROL
            or config.frozen_definition_version != "frozen-p0-v1"
            or config.fixture_version != "transfer-v1"
        ):
            raise UnsupportedCapability(
                "M3 requires trusted control fixtures and the frozen development protocol"
            )
        if config.study == "m3_transition":
            if (
                config.pairing.mode != PairingMode.FIXED_CANDIDATE
                or config.pairing.fork_at != ForkPoint.PRE_VERIFICATION
                or config.pairing.verification_intervention
                or config.admission_policy != "reject_all"
                or config.memory_mode != "quarantine"
                or config.guard_mode != "observe"
                or config.context_budget_units != 16
                or config.horizon_attempts != 2
                or config.audit_budget_units != 40
                or config.resources.synthetic_units_per_branch != 100
                or config.random != RandomPlan(104729)
            ):
                raise UnsupportedCapability(
                    "m3_transition is the exact frozen Appendix B protocol"
                )
        elif config.study in ("m3_smoke", "m3_probe_admission"):
            if (
                config.pairing.mode != PairingMode.PROCESS
                or config.pairing.verification_intervention
            ):
                raise UnsupportedCapability(
                    "M3 closed loop fixtures require process pairing"
                )
            if (
                config.study == "m3_probe_admission"
                and config.admission_policy != "probe"
            ):
                raise UnsupportedCapability(
                    "m3_probe_admission requires the probe policy"
                )
            if config.admission_policy == "probe" and config.guard_mode != "observe":
                raise UnsupportedCapability(
                    "Frozen M3 descendant probes require observe guard mode"
                )
        else:
            raise UnsupportedCapability("Unknown M3 study")
        return
    if config.stage == "M2":
        if (
            config.runtime != "trusted_fixture"
            or config.subject != "fake"
            or config.arbitrary_code_execution
            or config.partition != Partition.DEV
            or config.candidate_origin != CandidateOrigin.CONTROL
            or config.frozen_definition_version != "frozen-p0-v1"
            or config.fixture_version != "transfer-v1"
        ):
            raise UnsupportedCapability(
                "M2 requires trusted control fixtures, dev partition and frozen-p0-v1"
            )
        if config.study not in ("m2_q_pairs", "m2_v_fixed_pool", "m2_shared_candidate"):
            raise UnsupportedCapability("Unknown M2 prepare-only study")
        fixed = config.study in ("m2_v_fixed_pool", "m2_shared_candidate")
        from .contracts import ForkPoint

        if fixed and (
            config.pairing.mode != PairingMode.FIXED_CANDIDATE
            or config.pairing.fork_at != ForkPoint.PRE_VERIFICATION
        ):
            raise UnsupportedCapability(
                "M2 fixed candidate/v comparisons require pre_verification"
            )
        if not fixed and config.pairing.mode != PairingMode.PROCESS:
            raise UnsupportedCapability("q pairs require process pairing")
        return
    required = {
        "stage": "M1",
        "study": "m1_state_demo",
        "subject": "state_fixture",
        "runtime": "trusted_fixture",
        "frozen_definition_version": "frozen-p0-v1",
        "fixture_version": "transfer-v1",
    }
    for field, supported in required.items():
        if getattr(config, field) != supported:
            raise UnsupportedCapability(
                f"{field}={getattr(config, field)!r} is unavailable; M1 supports {supported!r}"
            )
    if config.arbitrary_code_execution:
        raise UnsupportedCapability(
            "Untrusted generated code requires a verified isolation backend; none is implemented in M1"
        )
    if config.candidate_origin != CandidateOrigin.CONTROL:
        raise UnsupportedCapability(
            "M1 state fixtures cannot be labelled natural or stress candidates"
        )
    if config.partition != Partition.DEV:
        raise UnsupportedCapability(
            "M1 demo is development infrastructure; sealed evaluation is unavailable"
        )
    if config.pairing.mode != PairingMode.PROCESS:
        raise UnsupportedCapability(
            "M1 demo exercises process anchors only; fixed candidate generation belongs to M2"
        )


def doctor_report() -> dict:
    import sqlite3
    import sys

    return {
        "schema_version": 1,
        "stage": "M4",
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "runtime": "trusted_fixture",
        "arbitrary_code_execution": False,
        "hard_isolation_verified": False,
        "model_calls": False,
        "gpu_required": False,
        "implemented": [
            "typed_contracts",
            "canonical_artifacts",
            "sqlite_backup",
            "independent_branch_storage",
            "operation_reservations",
            "persistent_preparation_cursors",
            "frozen_fixture_integrity",
            "FakeSubject",
            "transfer_environment",
            "independent_effect_oracle",
            "q_context_receipts",
            "v_execution_receipts",
            "process_and_fixed_candidate_preparation",
            "admission_policies",
            "descendant_probes",
            "authorized_deployment",
            "main_deployment_pause",
            "durable_provider_reconciliation",
            "process_crash_recovery",
            "S0_closed_loop_studies",
            "research_exports",
            "training_feature_exports",
            "history_group_partitions",
            "paired_missing_aware_statistics",
        ],
        "not_implemented": [
            "four_family_authorization_battery",
            "counterfactual_candidate_labels",
            "real_model_adapter",
            "untrusted_code_sandbox",
        ],
        "notice": "Engineering readiness only; not a safety certification or research result",
    }
