"""The worked example from the README and the other cases, one at a time."""

from datetime import date

import pytest

from installments.checks import late_refunds
from installments.markets import MARKETS, add_business_days
from installments.plan import PlanError, is_closed, new_plan, split_total, start_charge
from installments.processor import FakeProcessor, Outcome
from installments.service import Steps, autopay_steps, refund_steps, run
from installments.store import Store
from tests.helpers import PLAN_ID, TODAY, amounts, first_payment_paid, problems, refund, sent_back


def refund_during_charge(
    outcome: Outcome, amount: int, market: str = "US"
) -> tuple[Store, FakeProcessor, Steps]:
    """Autopay starts charging payment 2 for 100.00, then the refund lands."""
    store, processor = first_payment_paid([outcome], market)
    autopay = autopay_steps(store, processor, PLAN_ID, 2)
    next(autopay)  # loaded the plan
    next(autopay)  # saved attempt 1 as in flight, about to call the processor
    run(refund_steps(store, refund("r-1", amount), TODAY))
    return store, processor, autopay


def test_refund_reaches_the_payment_being_charged() -> None:
    store, _, _ = refund_during_charge(Outcome.SUCCEED, 25000)
    assert amounts(store) == [10000, 5000, 0, 0]
    in_flight = store.load(PLAN_ID).in_flight
    assert in_flight is not None and in_flight.amount == 10000  # the started charge is unchanged
    assert sent_back(store) == 0


def test_charge_succeeds_and_the_difference_goes_back() -> None:
    store, processor, autopay = refund_during_charge(Outcome.SUCCEED, 25000)
    run(autopay)
    payment = store.load(PLAN_ID).payments[1]
    assert (payment.amount, payment.collected) == (5000, 10000)
    payouts = store.payouts(PLAN_ID)
    assert [(p.key, p.amount) for p in payouts] == [("plan-1/payment-2/attempt-1/payback", 5000)]
    assert is_closed(store.load(PLAN_ID))
    assert problems(store, processor) == []


def test_charge_fails_and_the_next_attempt_charges_50() -> None:
    store, processor, autopay = refund_during_charge(Outcome.FAIL, 25000)
    run(autopay)
    assert store.load(PLAN_ID).payments[1].owed == 5000
    run(autopay_steps(store, processor, PLAN_ID, 2))
    retry = processor.charges["plan-1/payment-2/attempt-2"]
    assert (retry.amount, retry.captured) == (5000, True)
    assert sent_back(store) == 0
    assert is_closed(store.load(PLAN_ID))
    assert problems(store, processor) == []


@pytest.mark.parametrize(
    ("outcome", "collected", "paid_back"),
    [(Outcome.TIMEOUT_AFTER_CAPTURE, 10000, 5000), (Outcome.TIMEOUT_BEFORE_CAPTURE, 0, 0)],
)
def test_timeout_is_settled_by_a_lookup_with_the_same_key(
    outcome: Outcome, collected: int, paid_back: int
) -> None:
    store, processor, autopay = refund_during_charge(outcome, 25000)
    before = store.load(PLAN_ID)
    next(autopay)  # the call times out, the worker asks about the same key, then reloads
    assert processor.lookups == ["plan-1/payment-2/attempt-1"]
    assert store.load(PLAN_ID) == before  # nothing changed while the answer was unknown
    run(autopay_steps(store, processor, PLAN_ID, 2))  # a second worker starts nothing
    assert store.load(PLAN_ID) == before
    run(autopay)  # settles with the answer from the lookup
    assert list(processor.charges) == ["plan-1/payment-1/attempt-1", "plan-1/payment-2/attempt-1"]
    assert store.load(PLAN_ID).payments[1].collected == collected
    assert sent_back(store) == paid_back
    assert problems(store, processor) == []


def test_small_refund_leaves_the_charge_alone() -> None:
    store, processor, autopay = refund_during_charge(Outcome.SUCCEED, 3000)
    assert amounts(store) == [10000, 10000, 10000, 7000]
    run(autopay)
    assert store.load(PLAN_ID).payments[1].collected == 10000
    assert sent_back(store) == 0
    assert problems(store, processor) == []


def test_refund_bigger_than_everything_unpaid() -> None:
    store, processor, autopay = refund_during_charge(Outcome.SUCCEED, 35000)
    assert amounts(store) == [5000, 0, 0, 0]
    assert [(p.key, p.amount) for p in store.payouts(PLAN_ID)] == [
        ("plan-1/refund-r-1/payback", 5000)
    ]
    assert not is_closed(store.load(PLAN_ID))  # payment 2 is still in flight
    run(autopay)
    assert sent_back(store) == 15000
    assert is_closed(store.load(PLAN_ID))
    assert problems(store, processor) == []


def test_refund_past_the_plan_total_is_rejected() -> None:
    store, _ = first_payment_paid()
    run(refund_steps(store, refund("r-1", 25000), TODAY))
    before = store.load(PLAN_ID)
    with pytest.raises(PlanError):
        run(refund_steps(store, refund("r-2", 15001), TODAY))
    assert store.load(PLAN_ID) == before
    assert store.refund_total(PLAN_ID) == 25000


def test_same_refund_twice_does_nothing_the_second_time() -> None:
    store, processor = first_payment_paid()
    run(refund_steps(store, refund("r-1", 35000), TODAY))
    before = store.load(PLAN_ID)
    run(refund_steps(store, refund("r-1", 35000), TODAY))
    assert store.load(PLAN_ID) == before  # same version too, so nothing was written
    assert store.refund_total(PLAN_ID) == 35000
    assert sent_back(store) == 5000
    assert problems(store, processor) == []


def test_payments_are_charged_in_order() -> None:
    # This is what lets apply_refund treat the last payments as the unpaid ones.
    plan = new_plan(PLAN_ID, "US", total=40000, count=4)
    with pytest.raises(PlanError, match="earlier payment"):
        start_charge(plan, 2)


def test_leftover_cent_goes_on_the_first_payment() -> None:
    assert split_total(10000, 3) == [3334, 3333, 3333]


def test_business_days_skip_weekends() -> None:
    friday = date(2026, 10, 2)
    assert add_business_days(MARKETS["US"], friday, 3) == date(2026, 10, 7)
    assert add_business_days(MARKETS["UK"], friday, 10) == date(2026, 10, 16)


@pytest.mark.parametrize(
    ("market", "last_day", "too_late"),
    [("US", date(2026, 10, 7), date(2026, 10, 8)), ("UK", date(2026, 10, 16), date(2026, 10, 19))],
)
def test_refund_applied_after_its_window_is_flagged(
    market: str, last_day: date, too_late: date
) -> None:
    store, _ = first_payment_paid(market=market)
    friday = date(2026, 10, 2)
    run(refund_steps(store, refund("on-time", 1000, received_on=friday), last_day))
    run(refund_steps(store, refund("late", 1000, received_on=friday), too_late))
    assert late_refunds(store) == ["plan-1/late"]


def test_uk_money_goes_back_in_pounds() -> None:
    store, processor, autopay = refund_during_charge(Outcome.SUCCEED, 35000, market="UK")
    run(autopay)
    uk = MARKETS["UK"]
    payouts = store.payouts(PLAN_ID)
    assert [(p.amount, p.currency, p.destination) for p in payouts] == [
        (5000, "GBP", uk.payback_destination),
        (10000, "GBP", uk.payback_destination),
    ]
    assert problems(store, processor) == []
