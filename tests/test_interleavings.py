"""Every order of the steps of two autopay workers and a refund handler on one plan.

There are no threads. Each handler is a generator that yields after every plan load and
while its charge is with the processor. explore() runs every possible order of those
steps, each one on a fresh store, and checks the books after every step. A failure
shows the exact order, so it reproduces every time.
"""

from collections.abc import Callable
from dataclasses import dataclass

import pytest

from installments.plan import Plan
from installments.processor import FakeProcessor, Outcome
from installments.service import Steps, autopay_steps, refund_steps
from installments.store import Store, plan_columns
from tests.helpers import PLAN_ID, TODAY, amounts, first_payment_paid, problems, refund, sent_back


class NaiveStore(Store):
    """Saves without the version check, so a stale write replaces a newer one."""

    def _update_plan(self, plan: Plan) -> bool:
        payments, in_flight = plan_columns(plan)
        self.db.execute(
            "UPDATE plans SET payments = ?, in_flight = ?, version = version + 1 WHERE id = ?",
            (payments, in_flight, plan.id),
        )
        return True


@dataclass
class World:
    store: Store
    processor: FakeProcessor
    handlers: dict[str, Steps]

    def step(self, name: str) -> None:
        try:
            next(self.handlers[name])
        except StopIteration:
            del self.handlers[name]

    def finish(self) -> None:
        while self.handlers:
            self.step(next(iter(self.handlers)))


def worked_example(outcome: Outcome, store_class: type[Store], names: list[str]) -> World:
    """Payment 1 is paid, payment 2 is being charged, and a 250.00 refund arrives."""
    store, processor = first_payment_paid([outcome], store_class=store_class)
    handlers = {
        "autopay A": autopay_steps(store, processor, PLAN_ID, 2),
        "autopay B": autopay_steps(store, processor, PLAN_ID, 2),
        "refund": refund_steps(store, refund("r-1", 25000), TODAY),
        "same refund again": refund_steps(store, refund("r-1", 25000), TODAY),
    }
    return World(store, processor, {name: handlers[name] for name in names})


def checked(world: World, order: list[str]) -> list[str]:
    return [f"{order}: {problem}" for problem in problems(world.store, world.processor)]


def end_problems(world: World) -> list[str]:
    """What must be true once every handler has finished, on top of the books."""
    found = []
    if world.store.load(PLAN_ID).in_flight is not None:
        found.append("a charge is still in flight")
    if world.store.refund_total(PLAN_ID) != 25000:
        found.append(f"refunds recorded: {world.store.refund_total(PLAN_ID)}")
    if amounts(world.store) != [10000, 5000, 0, 0]:
        found.append(f"payments are {amounts(world.store)}")
    return found


def explore(make_world: Callable[[], World]) -> tuple[int, list[str]]:
    """Run every order of the handlers' steps. Returns how many orders ran and what went wrong.

    Depth first: replay a prefix on a fresh world, then always take the first handler,
    and remember each other handler at each step as a prefix still to run. Each step is
    checked the first time it runs. In a replayed prefix, only the last step is new.
    """
    runs = 0
    found: list[str] = []
    todo: list[list[str]] = [[]]
    while todo:
        order = todo.pop()
        world = make_world()
        try:
            for name in order:
                world.step(name)
            found.extend(checked(world, order))
            while world.handlers:
                names = list(world.handlers)
                for other in names[1:]:
                    todo.append([*order, other])
                order = [*order, names[0]]
                world.step(names[0])
                found.extend(checked(world, order))
        except Exception as error:
            found.append(f"{order}: {error!r}")
            continue
        runs += 1
        found.extend(f"{order}: {problem}" for problem in end_problems(world))
    return runs, found


@pytest.mark.parametrize("outcome", list(Outcome))
def test_every_order_of_two_workers_and_a_refund_is_safe(outcome: Outcome) -> None:
    names = ["autopay A", "autopay B", "refund"]
    runs, found = explore(lambda: worked_example(outcome, Store, names))
    assert found == []
    assert runs > 1000


def test_every_order_of_a_charge_and_a_refund_delivered_twice_is_safe() -> None:
    names = ["autopay A", "refund", "same refund again"]
    runs, found = explore(lambda: worked_example(Outcome.SUCCEED, Store, names))
    assert found == []
    assert runs > 1000


def test_without_the_version_check_the_same_checks_catch_lost_money() -> None:
    names = ["autopay A", "refund"]
    _, found = explore(lambda: worked_example(Outcome.SUCCEED, NaiveStore, names))
    assert found


def test_one_order_that_loses_the_refund_without_the_version_check() -> None:
    # A loads the plan to settle its charge, the refund saves, then A saves its stale copy.
    order = ["autopay A"] * 3 + ["refund"] * 2 + ["autopay A"]

    naive = worked_example(Outcome.SUCCEED, NaiveStore, ["autopay A", "refund"])
    for name in order:
        naive.step(name)
    assert naive.handlers == {}
    assert naive.store.refund_total(PLAN_ID) == 25000  # the refund is recorded
    assert amounts(naive.store) == [10000, 10000, 10000, 10000]  # but its change is gone
    assert sent_back(naive.store) == 0  # and the 50.00 never goes back
    assert problems(naive.store, naive.processor) != []

    real = worked_example(Outcome.SUCCEED, Store, ["autopay A", "refund"])
    for name in order:
        real.step(name)
    real.finish()  # A's stale save conflicted, so it reloads and settles again
    assert amounts(real.store) == [10000, 5000, 0, 0]
    assert sent_back(real.store) == 5000
    assert problems(real.store, real.processor) == []
