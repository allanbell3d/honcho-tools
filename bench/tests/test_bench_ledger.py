"""The spend ledger: reserve before sending, stop before the cap, charge failures conservatively."""
import json
from decimal import Decimal

import pytest

from ledger import BudgetExceeded, Ledger


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_reservation_then_settlement_replaces_the_reservation(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", cap_usd=1)
    r = ledger.reserve("voyage", "m", "0.50", tokens=100)
    assert ledger.committed == Decimal("0.50")
    r.complete(cost_estimate="0.10", tokens=40)
    assert ledger.committed == Decimal("0.10")
    assert [row["outcome"] for row in rows(tmp_path / "l.jsonl")] == ["reserved", "success"]


def test_cap_stops_a_request_before_it_is_sent(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", cap_usd=1)
    ledger.reserve("p", "m", "0.60")
    with pytest.raises(BudgetExceeded):
        ledger.reserve("p", "m", "0.60")
    assert len(rows(tmp_path / "l.jsonl")) == 1  # the refused request wrote nothing


def test_in_flight_reservations_count_against_the_cap(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", cap_usd=1)
    ledger.reserve("p", "m", "0.40")
    ledger.reserve("p", "m", "0.40")
    with pytest.raises(BudgetExceeded):
        ledger.reserve("p", "m", "0.40")


def test_failed_request_keeps_its_full_reservation(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", cap_usd=1)
    ledger.reserve("p", "m", "0.30").complete(outcome="request_failed")
    assert ledger.committed == Decimal("0.30")


def test_verified_4xx_rejection_is_free_but_a_5xx_is_not(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", cap_usd=1)
    ledger.reserve("p", "m", "0.30").complete(outcome="rejected", http_status=400)
    assert ledger.committed == 0
    ledger.reserve("p", "m", "0.30").complete(outcome="rejected", http_status=503)
    assert ledger.committed == Decimal("0.30")


def test_actual_cost_cannot_exceed_the_reservation_and_settles_once(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", cap_usd=1)
    r = ledger.reserve("p", "m", "0.10")
    with pytest.raises(ValueError):
        r.complete(cost_estimate="0.20")
    r.complete(cost_estimate="0.05")
    with pytest.raises(RuntimeError):
        r.complete(cost_estimate="0.05")


def test_run_limit_is_separate_from_the_global_cap(tmp_path):
    path = tmp_path / "l.jsonl"
    Ledger(path, cap_usd=5).reserve("p", "m", "1.00")  # an earlier run
    ledger = Ledger(path, cap_usd=5, max_run_usd="0.50")
    ledger.reserve("p", "m", "0.40")
    with pytest.raises(BudgetExceeded):
        ledger.reserve("p", "m", "0.20")
    assert ledger.spent_in_run == Decimal("0.40")


def test_invalid_amounts_are_refused(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", cap_usd=1)
    with pytest.raises(ValueError):
        ledger.reserve("p", "m", "-1")
    with pytest.raises(ValueError):
        Ledger(tmp_path / "x.jsonl", cap_usd=-1)


def test_rows_hold_no_payload_fields(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", cap_usd=1)
    ledger.reserve("voyage", "m", "0.01", tokens=3).complete()
    allowed = {"time", "request_id", "provider", "model", "tokens", "cost_estimate", "latency", "outcome", "http_status"}
    for row in rows(tmp_path / "l.jsonl"):
        assert set(row) <= allowed
