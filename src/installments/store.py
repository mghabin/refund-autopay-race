"""SQLite storage. A plan is only saved if nobody else saved it since it was loaded."""

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date

from installments.plan import Attempt, Payment, Plan

SCHEMA = """
CREATE TABLE plans (
    id TEXT PRIMARY KEY,
    market TEXT NOT NULL,
    total INTEGER NOT NULL,
    payments TEXT NOT NULL, -- JSON, so one version covers the plan and all its payments
    in_flight TEXT, -- JSON, the charge attempt waiting for an answer
    version INTEGER NOT NULL
);
CREATE TABLE refunds (
    plan_id TEXT NOT NULL REFERENCES plans (id),
    refund_id TEXT NOT NULL,
    amount INTEGER NOT NULL,
    received_on TEXT NOT NULL,
    applied_on TEXT NOT NULL,
    PRIMARY KEY (plan_id, refund_id)
);
CREATE TABLE payouts (
    payout_key TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES plans (id),
    amount INTEGER NOT NULL,
    currency TEXT NOT NULL,
    destination TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Refund:
    """A refund confirmed by the store. refund_id is the store's own ID for it."""

    plan_id: str
    refund_id: str
    amount: int
    received_on: date


@dataclass(frozen=True)
class Payout:
    """Money owed back to the customer. A separate sender pays it out, once per key."""

    key: str
    amount: int
    currency: str
    destination: str


def plan_columns(plan: Plan) -> tuple[str, str | None]:
    """The payments and the attempt in flight as JSON, the way the plans table holds them."""
    payments = json.dumps([asdict(payment) for payment in plan.payments])
    in_flight = json.dumps(asdict(plan.in_flight)) if plan.in_flight else None
    return payments, in_flight


class Store:
    def __init__(self) -> None:
        # autocommit=True: sqlite3 opens no transactions of its own, only the BEGIN in save().
        self.db = sqlite3.connect(":memory:", autocommit=True)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)

    def add_plan(self, plan: Plan) -> None:
        payments, in_flight = plan_columns(plan)
        self.db.execute(
            "INSERT INTO plans (id, market, total, payments, in_flight, version)"
            " VALUES (?, ?, ?, ?, ?, 0)",
            (plan.id, plan.market, plan.total, payments, in_flight),
        )

    def load(self, plan_id: str) -> Plan:
        row = self.db.execute(
            "SELECT market, total, payments, in_flight, version FROM plans WHERE id = ?",
            (plan_id,),
        ).fetchone()
        if row is None:
            raise KeyError(plan_id)
        market, total, payments, in_flight, version = row
        return Plan(
            id=plan_id,
            market=market,
            total=total,
            payments=tuple(Payment(**item) for item in json.loads(payments)),
            in_flight=Attempt(**json.loads(in_flight)) if in_flight else None,
            version=version,
        )

    def save(
        self,
        plan: Plan,
        payouts: Sequence[Payout] = (),
        refund: Refund | None = None,
        applied_on: date | None = None,
    ) -> bool:
        """Save the plan, the refund it applied and any payouts in one transaction.

        Writes only if the stored version is still plan.version, the one it was loaded at.
        Returns False, with nothing written, if another writer got there first.
        """
        self.db.execute("BEGIN")
        try:
            if not self._update_plan(plan):
                self.db.execute("ROLLBACK")
                return False
            if refund is not None:
                assert applied_on is not None, "a refund is saved with the day it was applied"
                self.db.execute(
                    "INSERT INTO refunds (plan_id, refund_id, amount, received_on, applied_on)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        plan.id,
                        refund.refund_id,
                        refund.amount,
                        refund.received_on.isoformat(),
                        applied_on.isoformat(),
                    ),
                )
            for payout in payouts:
                self.db.execute(
                    "INSERT INTO payouts (payout_key, plan_id, amount, currency, destination)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (payout.key, plan.id, payout.amount, payout.currency, payout.destination),
                )
            self.db.execute("COMMIT")
            return True
        except Exception:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def _update_plan(self, plan: Plan) -> bool:
        """The compare-and-swap. Changes nothing unless the version is still the one loaded."""
        payments, in_flight = plan_columns(plan)
        cursor = self.db.execute(
            "UPDATE plans SET payments = ?, in_flight = ?, version = version + 1"
            " WHERE id = ? AND version = ?",
            (payments, in_flight, plan.id, plan.version),
        )
        return cursor.rowcount == 1

    def has_refund(self, plan_id: str, refund_id: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM refunds WHERE plan_id = ? AND refund_id = ?", (plan_id, refund_id)
        ).fetchone()
        return row is not None

    def refund_total(self, plan_id: str) -> int:
        row = self.db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM refunds WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        return row[0]

    def payouts(self, plan_id: str) -> list[Payout]:
        rows = self.db.execute(
            "SELECT payout_key, amount, currency, destination FROM payouts WHERE plan_id = ?"
            " ORDER BY rowid",
            (plan_id,),
        ).fetchall()
        return [Payout(*row) for row in rows]

    def refund_dates(self) -> list[tuple[str, str, str, date, date]]:
        """(plan_id, refund_id, market, received_on, applied_on) for every applied refund."""
        rows = self.db.execute(
            "SELECT r.plan_id, r.refund_id, p.market, r.received_on, r.applied_on"
            " FROM refunds r JOIN plans p ON p.id = r.plan_id ORDER BY r.rowid"
        ).fetchall()
        return [
            (plan_id, refund_id, market, date.fromisoformat(received), date.fromisoformat(applied))
            for plan_id, refund_id, market, received, applied in rows
        ]
