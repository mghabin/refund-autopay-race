"""Checks a scheduled job could run. Each returns a list of problems, empty when all is well."""

from installments.markets import MARKETS, refund_is_late
from installments.plan import refundable
from installments.processor import Processor
from installments.store import Store


def check_books(store: Store, plan_id: str) -> list[str]:
    """Collected, minus sent back, plus still due, must equal the plan total minus refunds.

    Refunds and payouts come from their own tables, not from totals kept on the plan row,
    so a plan row that lost an update does not agree with them.
    """
    plan = store.load(plan_id)
    refunded = store.refund_total(plan_id)
    sent_back = sum(payout.amount for payout in store.payouts(plan_id))
    collected = sum(payment.collected for payment in plan.payments)
    still_due = sum(payment.owed for payment in plan.payments)
    expected = plan.total - refunded
    problems = []
    if refundable(plan) != expected:
        problems.append(f"payments add up to {refundable(plan)}, expected {expected}")
    if collected - sent_back + still_due != expected:
        problems.append(
            f"collected {collected} - sent back {sent_back} + still due {still_due}"
            f" is not total {plan.total} - refunds {refunded}"
        )
    return problems


def check_processor(store: Store, processor: Processor, plan_id: str) -> list[str]:
    """No payment is captured twice, and with nothing in flight the processor agrees with us."""
    plan = store.load(plan_id)
    captures = processor.captures(plan_id)
    problems = []
    for payment in plan.payments:
        count = len([c for c in captures if c.payment_number == payment.number])
        if count > 1:
            problems.append(f"payment {payment.number} was captured {count} times")
    collected = sum(payment.collected for payment in plan.payments)
    captured = sum(c.amount for c in captures)
    if plan.in_flight is None and captured != collected:
        problems.append(f"processor captured {captured} but the plan collected {collected}")
    return problems


def late_refunds(store: Store) -> list[str]:
    """Refunds applied later than their market's published window, as plan_id/refund_id."""
    late = []
    for plan_id, refund_id, market, received_on, applied_on in store.refund_dates():
        if refund_is_late(MARKETS[market], received_on, applied_on):
            late.append(f"{plan_id}/{refund_id}")
    return late
