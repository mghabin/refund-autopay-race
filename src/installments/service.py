"""Handlers: load a plan, apply a rule, save it if the version still matches, retry if not.

Each handler is a generator. It yields after every plan load, and after saving a charge
attempt while the processor works on it, so the tests can run several handlers in every
possible order of those steps. run() drives one handler to the end.
"""

from collections.abc import Generator
from datetime import date

from installments.markets import MARKETS
from installments.plan import Attempt, Plan, PlanError, apply_refund, settle_charge, start_charge
from installments.processor import Processor, ProcessorTimeout
from installments.store import Payout, Refund, Store

MAX_TRIES = 5

Steps = Generator[None, None, None]


class TooManyConflicts(Exception):
    """The plan kept changing under us. The caller should try again later."""


def run(steps: Steps) -> None:
    for _ in steps:
        pass


def refund_steps(store: Store, refund: Refund, today: date) -> Steps:
    """Apply a store refund once, however many times it is delivered."""
    for _ in range(MAX_TRIES):
        plan = store.load(refund.plan_id)
        yield
        # Checked after the load. A delivery that records this refund after our load
        # also changes the plan version, so our save fails and the next pass sees it here.
        if store.has_refund(refund.plan_id, refund.refund_id):
            return
        new, send_back = apply_refund(plan, refund.amount)
        payouts = _payouts(new, f"{refund.plan_id}/refund-{refund.refund_id}/payback", send_back)
        if store.save(new, payouts, refund, today):
            return
    raise TooManyConflicts(refund.plan_id)


def autopay_steps(store: Store, processor: Processor, plan_id: str, payment_number: int) -> Steps:
    """Charge one payment: save the attempt, call the processor, then settle the attempt."""
    for _ in range(MAX_TRIES):
        plan = store.load(plan_id)
        yield
        try:
            new, attempt = start_charge(plan, payment_number)
        except PlanError:
            return  # nothing to charge now, for example another attempt is in flight
        if store.save(new):
            yield  # the attempt is in flight, and other handlers can run before the answer
            captured = call_processor(processor, plan_id, attempt)
            yield from settle_steps(store, plan_id, attempt.key, captured)
            return
    raise TooManyConflicts(plan_id)


def call_processor(processor: Processor, plan_id: str, attempt: Attempt) -> bool:
    """Returns True if the money was captured."""
    try:
        return processor.charge(plan_id, attempt)
    except ProcessorTimeout:
        # Ask about the same key. A new attempt with a new key could take the money twice.
        return processor.was_captured(attempt.key)


def settle_steps(store: Store, plan_id: str, key: str, captured: bool) -> Steps:
    for _ in range(MAX_TRIES):
        plan = store.load(plan_id)
        yield
        new, send_back = settle_charge(plan, key, captured)
        if store.save(new, _payouts(new, f"{key}/payback", send_back)):
            return
    raise TooManyConflicts(plan_id)


def _payouts(plan: Plan, key: str, amount: int) -> list[Payout]:
    if amount == 0:
        return []
    market = MARKETS[plan.market]
    return [Payout(key, amount, market.currency, market.payback_destination)]
