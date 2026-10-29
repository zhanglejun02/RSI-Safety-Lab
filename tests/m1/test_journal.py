"""M1 resource evidence tests; no agent generation or safety labels are implied."""

from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from rsi_lab.errors import BudgetExceeded, ConflictError, IntegrityError, ValidationError
from rsi_lab.infra.journal import Journal


@pytest.fixture
def journal(tmp_path):
    ledger = Journal(tmp_path / "controller.sqlite")
    ledger.create_budget("branch", "L", 10)
    return ledger


def reserve(journal, operation_id="L/generate/1", amount=5, **kwargs):
    return journal.reserve(operation_id, "branch", "L", amount, "discovery",
                           {"operation": "generate", "input": "frozen-fixture"}, **kwargs)


def test_reservation_and_receipt_replay_survive_reopen(journal):
    first = reserve(journal, attempt_id="L_001")
    assert first["status"] == "reserved"
    assert reserve(journal, attempt_id="L_001") == first
    receipt = {"response": "fixture-D", "usage_kind": "synthetic"}
    settled = journal.settle("L/generate/1", 5, receipt)
    reopened = Journal(journal.path)
    assert reopened.settle("L/generate/1", 5, receipt) == settled
    assert reserve(reopened, attempt_id="L_001")["status"] == "settled"
    summary = reopened.summary()
    assert summary["settled_charge_rows"] == 1
    assert summary["total_synthetic_units"] == 5
    assert summary["outstanding_reserved_units"] == 0
    assert [e["event_type"] for e in reopened.events()] == ["budget_created", "reserve", "settle"]


def test_replayed_identity_cannot_change_request_owner_attempt_or_cost(journal):
    reserve(journal, attempt_id="L_001")
    for amount, request, attempt in (
        (6, {"operation": "generate", "input": "frozen-fixture"}, "L_001"),
        (5, {"operation": "generate", "input": "different"}, "L_001"),
        (5, {"operation": "generate", "input": "frozen-fixture"}, "L_002"),
    ):
        with pytest.raises(ConflictError, match="conflicting reservation"):
            journal.reserve("L/generate/1", "branch", "L", amount, "discovery", request,
                            attempt_id=attempt)
    journal.create_budget("branch", "R", 10)
    with pytest.raises(ConflictError):
        journal.reserve("L/generate/1", "branch", "R", 5, "discovery", {}, attempt_id="L_001")
    journal.settle("L/generate/1", 5, {"result": "D"})
    with pytest.raises(ConflictError, match="conflicting final receipt"):
        journal.settle("L/generate/1", 4, {"result": "D"})
    with pytest.raises(ConflictError, match="conflicting final receipt"):
        journal.settle("L/generate/1", 5, {"result": "P"})


def test_unknown_retains_budget_until_reconciliation_and_retry_is_new(journal):
    reserve(journal, amount=6)
    unknown = journal.mark_unknown("L/generate/1", "provider_response_lost")
    assert unknown["outstanding_reserved_units"] == 6
    reopened = Journal(journal.path)
    assert reopened.summary()["unknown_operations"] == 1
    with pytest.raises(BudgetExceeded):
        reserve(reopened, "L/generate/retry", amount=5)
    assert reopened.summary()["operation_count"] == 1
    with pytest.raises(ValidationError, match="confirmed non-execution"):
        reopened.release("L/generate/1", "timeout_is_not_proof")
    reopened.settle("L/generate/1", 5, {"provider_query": "completed", "result": "D"})
    reserve(reopened, "L/generate/retry", amount=5)
    reopened.settle("L/generate/retry", 5, {"result": "D"})
    assert reopened.summary()["total_synthetic_units"] == 10
    assert reopened.summary()["settled_charge_rows"] == 2


def test_unknown_and_completion_transitions_cannot_erase_facts(journal):
    reserve(journal)
    unknown = journal.mark_unknown("L/generate/1", "response_lost")
    assert journal.mark_unknown("L/generate/1", "response_lost") == unknown
    with pytest.raises(ConflictError):
        journal.mark_unknown("L/generate/1", "different_fact")
    journal.settle("L/generate/1", 5, {"provider_query": "completed"})
    with pytest.raises(ConflictError):
        journal.mark_unknown("L/generate/1", "response_lost")
    with pytest.raises(ConflictError, match="cannot be released"):
        journal.release("L/generate/1", "rollback", known_not_executed=True)
    assert journal.summary()["total_synthetic_units"] == 5


def test_only_confirmed_non_execution_cancels_reservation(journal):
    reserve(journal)
    released = journal.release("L/generate/1", "provider_confirmed_not_started", known_not_executed=True)
    assert released["status"] == "released"
    assert journal.release("L/generate/1", "provider_confirmed_not_started", known_not_executed=True) == released
    assert journal.summary()["outstanding_reserved_units"] == 0
    assert journal.costs() == []
    with pytest.raises(ConflictError):
        journal.settle("L/generate/1", 5, {"result": "D"})
    with pytest.raises(ConflictError):
        journal.release("L/generate/1", "new_reason", known_not_executed=True)
    reserve(journal, "L/new_request", amount=10)


def test_actual_overrun_remains_chargeable_and_blocks_further_work(journal):
    reserve(journal, amount=5)
    journal.settle("L/generate/1", 12, {"confirmed_usage": 12})
    summary = journal.summary()
    assert summary["total_synthetic_units"] == 12
    assert summary["accounts"][0]["over_budget"] is True
    assert summary["accounts"][0]["available_units"] == 0
    assert "reservation_overrun" in [e["event_type"] for e in journal.events()]
    with pytest.raises(BudgetExceeded):
        reserve(journal, "L/new_request", amount=1)


def test_settlement_releases_only_receipt_confirmed_remainder(journal):
    reserve(journal, amount=8)
    journal.settle("L/generate/1", 5, {"complete": True})
    released = [e for e in journal.events() if e["event_type"] == "release_remainder"]
    assert released[0]["payload"]["synthetic_cost_units"] == 3
    assert journal.summary()["outstanding_reserved_units"] == 0
    reserve(journal, "L/generate/2", amount=5)


def test_zero_cost_infrastructure_has_intent_and_receipt_but_no_charge(journal):
    reserve(journal, "L/snapshot", amount=0)
    journal.settle("L/snapshot", 0, {"snapshot_sha256": "a" * 64})
    assert journal.summary()["operation_count"] == 1
    assert journal.costs() == []
    assert [e["event_type"] for e in journal.events()][-2:] == ["reserve", "settle"]


def test_settlement_state_charge_and_events_share_one_transaction(journal):
    reserve(journal)
    with sqlite3.connect(journal.path) as db:
        db.executescript("""
            CREATE TRIGGER inject_storage_failure BEFORE INSERT ON cost_events
            BEGIN SELECT RAISE(ABORT, 'injected_write_failure'); END;
        """)
    with pytest.raises(sqlite3.IntegrityError, match="injected_write_failure"):
        journal.settle("L/generate/1", 5, {"result": "D"})
    assert journal.get_operation("L/generate/1")["status"] == "reserved"
    assert journal.summary()["outstanding_reserved_units"] == 5
    assert journal.costs() == []
    assert [e["event_type"] for e in journal.events()] == ["budget_created", "reserve"]
    with sqlite3.connect(journal.path) as db:
        db.execute("DROP TRIGGER inject_storage_failure")
    journal.settle("L/generate/1", 5, {"result": "D"})
    assert journal.summary()["total_synthetic_units"] == 5


def test_pair_owned_generation_is_counted_once_not_copied_to_arms(journal):
    journal.create_budget("pair", "pair_01", 5)
    journal.create_budget("branch", "R", 10)
    journal.reserve("pair_01/generate/1", "pair", "pair_01", 5, "discovery", {"candidate": "P"},
                    attempt_id="S_001")
    journal.settle("pair_01/generate/1", 5, {"candidate": "P"})
    for arm in ("L", "R"):
        journal.reserve(f"{arm}/verify/ok", "branch", arm, 1, "discovery", {"test_id": "ok"})
        journal.settle(f"{arm}/verify/ok", 1, {"status": "pass"})
    summary = journal.summary()
    assert summary["settled_charge_rows"] == 3
    assert summary["pair_shared_units"] == 5
    assert summary["branch_units"] == 2
    assert summary["total_synthetic_units"] == 7
    assert journal.summary("branch", "L")["total_synthetic_units"] == 1
    assert all(row["monetary_amount"] is None for row in journal.costs())


def test_concurrent_reservations_cannot_overallocate(journal):
    def contender(index):
        try:
            reserve(journal, f"L/concurrent/{index}", amount=6)
            return "reserved"
        except BudgetExceeded:
            return "denied"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(contender, range(8)))
    assert results.count("reserved") == 1
    assert results.count("denied") == 7
    assert journal.summary()["outstanding_reserved_units"] == 6


def test_exposure_is_immutable_idempotent_and_preserved_after_unknown(journal):
    reserve(journal, "L/test/ok", amount=1)
    exposure = journal.record_exposure("L/test/ok/exposure", "L/test/ok", "public_development", "ok",
                                       branch_id="L")
    assert journal.record_exposure("L/test/ok/exposure", "L/test/ok", "public_development", "ok",
                                   branch_id="L") == exposure
    journal.mark_unknown("L/test/ok", "worker_disconnected_after_dispatch")
    with pytest.raises(ConflictError, match="exposure proves dispatch"):
        journal.release("L/test/ok", "cancelled", known_not_executed=True)
    journal.settle("L/test/ok", 1, {"status": "failure"})
    assert len(journal.exposures()) == 1
    assert journal.summary()["total_synthetic_units"] == 1
    with pytest.raises(ConflictError):
        journal.record_exposure("L/test/ok/exposure", "L/test/ok", "public_development", "different",
                                branch_id="L")
    with sqlite3.connect(journal.path) as db:
        for table in ("events", "cost_events", "exposure_events"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                db.execute(f"DELETE FROM {table}")


def test_exposure_partition_and_owner_purpose_constraints(journal):
    reserve(journal, "L/test/ok", amount=1)
    with pytest.raises(ConflictError, match="branch differs"):
        journal.record_exposure("bad-owner", "L/test/ok", "public_development", "ok", branch_id="R")
    with pytest.raises(ValidationError, match="purpose incompatible"):
        journal.record_exposure("bad-purpose", "L/test/ok", "admission_probe", "ok", branch_id="L")
    with pytest.raises(ValidationError, match="only independent"):
        journal.record_exposure("bad-partition", "L/test/ok", "public_development", "ok", branch_id="L",
                                partition="sealed")
    journal.reserve("L/final/test", "branch", "L", 1, "final_eval", {"test_id": "unseen"})
    with pytest.raises(ValidationError):
        journal.record_exposure("missing-partition", "L/final/test", "independent_evaluation", "unseen",
                                branch_id="L")
    with pytest.raises(ValidationError, match="sealed partition"):
        journal.record_exposure("wrong-partition", "L/final/test", "independent_evaluation", "unseen",
                                branch_id="L", partition="train")
    journal.record_exposure("final-exposure", "L/final/test", "independent_evaluation", "unseen",
                            branch_id="L", partition="sealed")
    assert len(journal.exposures()) == 1


@pytest.mark.parametrize("amount", [-1, True, 1.5, "5", 2**63])
def test_invalid_unit_inputs_are_rejected_before_mutation(journal, amount):
    with pytest.raises(ValidationError):
        reserve(journal, amount=amount)
    assert journal.summary()["operation_count"] == 0


def test_budget_definition_and_schema_validation(journal):
    assert journal.create_budget("branch", "L", 10)["limit_units"] == 10
    with pytest.raises(ConflictError):
        journal.create_budget("branch", "L", 11)
    with pytest.raises(ValidationError):
        journal.create_budget("probe", "L", 10)
    with pytest.raises(ValidationError):
        journal.reserve("op", "branch", "L", 1, "unknown", {})
    with pytest.raises(ValidationError):
        journal.reserve("op", "branch", "missing-owner", 1, "discovery", {})
    with pytest.raises(ValidationError):
        journal.summary("branch")
    with pytest.raises(ValidationError):
        journal.get_operation("missing-op")


def test_ledger_rejects_symlinked_ancestor_without_touching_target(tmp_path):
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real_directory, target_is_directory=True)
    with pytest.raises(IntegrityError, match="Symbolic link"):
        Journal(alias / "controller.sqlite")
    assert list(real_directory.iterdir()) == []


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_ledger_rejects_symlinked_database_and_sidecars_before_open(tmp_path, suffix):
    sentinel = tmp_path / "unrelated"
    sentinel.write_bytes(b"unrelated file must not be changed")
    database = tmp_path / "controller.sqlite"
    link = tmp_path / (database.name + suffix)
    link.symlink_to(sentinel)
    with pytest.raises(IntegrityError, match="Symbolic link"):
        Journal(database)
    assert sentinel.read_bytes() == b"unrelated file must not be changed"
    if suffix:
        assert not database.exists()


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_ledger_rechecks_sidecars_when_existing_object_is_reused(journal, suffix):
    sentinel = journal.path.parent / "unrelated"
    sentinel.write_bytes(b"keep")
    sidecar = journal.path.with_name(journal.path.name + suffix)
    sidecar.symlink_to(sentinel)
    with pytest.raises(IntegrityError, match="Symbolic link"):
        journal.summary()
    assert sentinel.read_bytes() == b"keep"
