"""Random plans and random sequences of refunds and charge outcomes."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from installments.markets import MARKETS
from installments.plan import PlanError, new_plan, refundable
from installments.processor import FakeProcessor, Outcome
from installments.service import autopay_steps, refund_steps, run
from installments.store import Refund, Store
from tests.helpers import PLAN_ID, TODAY, amounts, problems, sent_back

ACTIONS = ["charge", "charge any payment", "refund", "refund during a charge", "same refund again"]


def next_owed(store: Store) -> int | None:
    for payment in store.load(PLAN_ID).payments:
        if payment.owed > 0:
            return payment.number
    return None


def assert_last_first(before: list[int], after: list[int]) -> None:
    """Every payment after the first one a refund touched must now be zero."""
    cuts = [old - new for old, new in zip(before, after, strict=True)]
    assert all(cut >= 0 for cut in cuts)
    touched = [index for index, cut in enumerate(cuts) if cut > 0]
    if touched:
        assert all(amount == 0 for amount in after[touched[0] + 1 :])


def assert_nothing_goes_back_while_owed(store: Store, sent_before: int) -> None:
    """A refund sends money back only once it has cleared every payment still owed."""
    if sent_back(store) > sent_before:
        assert all(payment.owed == 0 for payment in store.load(PLAN_ID).payments)


def assert_guarantees(store: Store, processor: FakeProcessor) -> None:
    plan = store.load(PLAN_ID)
    assert problems(store, processor) == []
    assert plan.in_flight is None
    assert sum(amounts(store)) == plan.total - store.refund_total(PLAN_ID)
    for payment in plan.payments:
        assert payment.amount >= 0 and payment.collected >= 0
        # one processor key per attempt started, so a timeout never led to a blind retry
        prefix = f"{PLAN_ID}/payment-{payment.number}/attempt-"
        assert len([key for key in processor.charges if key.startswith(prefix)]) == payment.attempts


@settings(max_examples=200, deadline=None)
@given(st.data())
def test_guarantees_hold_after_every_action(data: st.DataObject) -> None:
    count = data.draw(st.integers(min_value=2, max_value=12), label="payments")
    total = data.draw(st.integers(min_value=count, max_value=1_000_000), label="total")
    market = data.draw(st.sampled_from(sorted(MARKETS)), label="market")
    store = Store()
    processor = FakeProcessor()
    store.add_plan(new_plan(PLAN_ID, market, total, count))
    applied: list[Refund] = []

    for number in range(data.draw(st.integers(min_value=1, max_value=20), label="actions")):
        action = data.draw(st.sampled_from(ACTIONS), label="action")
        before = store.load(PLAN_ID)
        owed = next_owed(store)
        outcome = data.draw(st.sampled_from(Outcome), label="outcome")
        processor.outcomes = [outcome]

        sent_before = sent_back(store)

        if action == "charge" and owed is not None:
            run(autopay_steps(store, processor, PLAN_ID, owed))

        elif action == "charge any payment":
            payment = data.draw(st.integers(min_value=1, max_value=count), label="payment")
            run(autopay_steps(store, processor, PLAN_ID, payment))  # only the next owed one runs

        elif action == "refund":
            amount = data.draw(st.integers(min_value=1, max_value=refundable(before) + 100))
            refund = Refund(PLAN_ID, f"r-{number}", amount, TODAY)
            if amount > refundable(before):
                with pytest.raises(PlanError):
                    run(refund_steps(store, refund, TODAY))
                assert store.load(PLAN_ID) == before
            else:
                run(refund_steps(store, refund, TODAY))
                assert_nothing_goes_back_while_owed(store, sent_before)
                applied.append(refund)

        elif action == "refund during a charge" and owed is not None and refundable(before) > 0:
            amount = data.draw(st.integers(min_value=1, max_value=refundable(before)))
            refund = Refund(PLAN_ID, f"r-{number}", amount, TODAY)
            autopay = autopay_steps(store, processor, PLAN_ID, owed)
            next(autopay)  # loaded the plan
            next(autopay)  # saved the attempt as in flight
            run(refund_steps(store, refund, TODAY))
            assert_nothing_goes_back_while_owed(store, sent_before)
            run(autopay)
            applied.append(refund)

        elif action == "same refund again" and applied:
            run(refund_steps(store, data.draw(st.sampled_from(applied)), TODAY))
            assert store.load(PLAN_ID) == before

        assert_last_first([p.amount for p in before.payments], amounts(store))
        assert_guarantees(store, processor)
