"""Append-only spend ledger with reservations: no paid request is sent unless its worst case fits under the cap.

Usage pattern for one paid request::

    reservation = ledger.reserve("voyage", "rerank-3-lite", worst_case_usd, tokens=estimate)
    try:
        ... send the request ...
        reservation.complete(cost_estimate=actual_usd, tokens=actual_tokens)
    except Exception:
        reservation.complete(outcome="request_failed")   # keeps the full reservation: the provider may have billed it
        raise

Rules the study relied on:
* the cap applies to the sum of every request's latest row (a settlement replaces its reservation);
* a request that fails is charged at its full reservation unless the provider verifiably rejected it with a 4xx;
* nothing is retried automatically, so one failed paid request never becomes two;
* rows hold provider, model, tokens, cost estimate, latency and outcome. Never payloads, keys or headers.

One process per ledger file. The lock protects threads inside a process, not several processes.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

_LOCKS: dict[str, threading.RLock] = {}
_GUARD = threading.Lock()


class BudgetExceeded(RuntimeError):
    """A reservation would take the committed total over the cap."""


class Ledger:
    def __init__(self, path: str | Path, cap_usd: float | str = 5, max_run_usd: float | str | None = None) -> None:
        self.path = Path(path)
        self.cap = Decimal(str(cap_usd))
        if self.cap < 0:
            raise ValueError("negative cap")
        self.run_limit = None if max_run_usd is None else Decimal(str(max_run_usd))
        self._run_ids: set[str] = set()
        with _GUARD:
            self._lock = _LOCKS.setdefault(str(self.path.resolve()).casefold(), threading.RLock())

    # -- reading ----------------------------------------------------------------------------------------------
    def _latest(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        latest: dict[str, dict] = {}
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    row = json.loads(line)
                    latest[row["request_id"]] = row
        return latest

    @property
    def committed(self) -> Decimal:
        with self._lock:
            return sum((Decimal(str(r["cost_estimate"])) for r in self._latest().values()), Decimal(0))

    @property
    def spent_in_run(self) -> Decimal:
        with self._lock:
            return sum(
                (Decimal(str(r["cost_estimate"])) for k, r in self._latest().items() if k in self._run_ids),
                Decimal(0),
            )

    # -- writing ----------------------------------------------------------------------------------------------
    def _append(self, row: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def reserve(self, provider: str, model: str, worst_case_usd: float | str, tokens: int = 0) -> "Reservation":
        cost = Decimal(str(worst_case_usd))
        if not cost.is_finite() or cost < 0:
            raise ValueError("invalid reservation")
        with self._lock:
            if self.committed + cost > self.cap:
                raise BudgetExceeded("paid request would exceed the spend cap")
            if self.run_limit is not None and self.spent_in_run + cost > self.run_limit:
                raise BudgetExceeded("paid request would exceed this run's spend limit")
            reservation = Reservation(self, str(uuid.uuid4()), provider, model, cost, tokens)
            self._append(reservation.row("reserved", cost, tokens, 0.0))
            self._run_ids.add(reservation.request_id)
            return reservation


class Reservation:
    def __init__(self, ledger: Ledger, request_id: str, provider: str, model: str, reserved: Decimal, tokens: int):
        self.ledger, self.request_id = ledger, request_id
        self.provider, self.model, self.reserved, self.tokens = provider, model, reserved, tokens
        self.started = time.perf_counter()
        self.done = False

    def row(self, outcome: str, cost: Decimal, tokens: int, latency: float) -> dict:
        return dict(
            time=datetime.now(timezone.utc).isoformat(),
            request_id=self.request_id,
            provider=self.provider,
            model=self.model,
            tokens=tokens,
            cost_estimate=float(cost),
            latency=latency,
            outcome=outcome,
        )

    def complete(
        self,
        *,
        outcome: str = "success",
        cost_estimate: float | str | None = None,
        tokens: int | None = None,
        http_status: int | None = None,
    ) -> None:
        """Settle the request. A success may cost less than reserved, never more."""
        cost = self.reserved if cost_estimate is None else Decimal(str(cost_estimate))
        if outcome == "rejected" and isinstance(http_status, int) and 400 <= http_status < 500:
            cost = Decimal(0)  # a verified client-error rejection was not billed
        elif outcome != "success":
            cost = self.reserved  # anything else may have been billed
        if not cost.is_finite() or not 0 <= cost <= self.reserved:
            raise ValueError("actual cost must fit the reserved worst case")
        with self.ledger._lock:
            if self.done:
                raise RuntimeError("reservation already settled")
            row = self.row(outcome, cost, self.tokens if tokens is None else tokens, time.perf_counter() - self.started)
            if http_status is not None:
                row["http_status"] = http_status
            self.ledger._append(row)
            self.done = True
