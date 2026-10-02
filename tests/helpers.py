from collections.abc import Sequence
from datetime import date

from installments.checks import check_books, check_processor
from installments.plan import new_plan
from installments.processor import FakeProcessor, Outcome
from installments.service import autopay_steps, run
from installments.store import Refund, Store

PLAN_ID = "plan-1"
TODAY = date(2026, 10, 5)  # a Monday


def first_payment_paid(
    outcomes: Sequence[Outcome] = (), market: str = "US", store_class: type[Store] = Store
) -> tuple[Store, FakeProcessor]:
    """The worked example's starting point: 4 payments of 100.00, and payment 1 is paid."""
    store = store_class()
    processor = FakeProcessor()
    store.add_plan(new_plan(PLAN_ID, market, total=40000, count=4))
    run(autopay_steps(store, processor, PLAN_ID, 1))
    processor.outcomes = list(outcomes)
    return store, processor


def refund(refund_id: str, amount: int, received_on: date = TODAY) -> Refund:
    return Refund(PLAN_ID, refund_id, amount, received_on)


def amounts(store: Store) -> list[int]:
    return [payment.amount for payment in store.load(PLAN_ID).payments]


def sent_back(store: Store) -> int:
    return sum(payout.amount for payout in store.payouts(PLAN_ID))


def problems(store: Store, processor: FakeProcessor) -> list[str]:
    return check_books(store, PLAN_ID) + check_processor(store, processor, PLAN_ID)
