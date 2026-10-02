"""Payment plan rules: frozen dataclasses and pure functions, no I/O.

Money is in integer minor units (cents or pence). Plans are interest-free.
"""

from dataclasses import dataclass, replace


class PlanError(Exception):
    """The plan's rules do not allow this request."""


@dataclass(frozen=True)
class Payment:
    number: int  # 1 is the first payment
    amount: int  # what this payment is worth now, after refunds
    collected: int = 0  # money captured for this payment
    attempts: int = 0  # charge attempts started so far

    @property
    def owed(self) -> int:
        return max(0, self.amount - self.collected)

    @property
    def overpaid(self) -> int:
        """Money collected beyond what the payment is now worth. It belongs to the customer."""
        return max(0, self.collected - self.amount)


@dataclass(frozen=True)
class Attempt:
    """A charge that has been started and not settled yet."""

    payment_number: int
    number: int
    amount: int
    key: str  # idempotency key sent to the processor


@dataclass(frozen=True)
class Plan:
    id: str
    market: str
    total: int
    payments: tuple[Payment, ...]
    in_flight: Attempt | None = None
    version: int = 0  # set by the store, never by these rules


def split_total(total: int, count: int) -> list[int]:
    """Split a total into payments that add up to it exactly.

    Leftover minor units go on the first payment, so every later payment is the same.
    """
    if count < 1 or total < count:
        raise PlanError("a plan needs at least one minor unit per payment")
    share, leftover = divmod(total, count)
    return [share + leftover] + [share] * (count - 1)


def new_plan(plan_id: str, market: str, total: int, count: int) -> Plan:
    amounts = split_total(total, count)
    payments = tuple(Payment(number=i + 1, amount=amount) for i, amount in enumerate(amounts))
    return Plan(id=plan_id, market=market, total=total, payments=payments)


def charge_key(plan_id: str, payment_number: int, attempt_number: int) -> str:
    return f"{plan_id}/payment-{payment_number}/attempt-{attempt_number}"


def refundable(plan: Plan) -> int:
    """How much can still be refunded: the plan total minus refunds so far."""
    return sum(payment.amount for payment in plan.payments)


def is_closed(plan: Plan) -> bool:
    return plan.in_flight is None and all(payment.owed == 0 for payment in plan.payments)


def apply_refund(plan: Plan, amount: int) -> tuple[Plan, int]:
    """Take a refund off the payments, last payment first.

    Returns the new plan and the amount to send back to the customer now. Payments are
    charged in order, so going backwards reaches every unpaid payment before any paid one,
    and money goes back now only once the refund reaches a payment already collected.
    A payment being charged is lowered like an unpaid one. If that charge succeeds,
    settle_charge sends the difference back.
    """
    if amount <= 0:
        raise PlanError("a refund must be positive")
    if amount > refundable(plan):
        raise PlanError("refunds cannot add up to more than the plan total")
    payments = list(plan.payments)
    left = amount
    send_back = 0
    for index in reversed(range(len(payments))):
        old = payments[index]
        cut = min(left, old.amount)
        new = replace(old, amount=old.amount - cut)
        send_back += new.overpaid - old.overpaid
        payments[index] = new
        left -= cut
    return replace(plan, payments=tuple(payments)), send_back


def start_charge(plan: Plan, payment_number: int) -> tuple[Plan, Attempt]:
    """Start charging a payment for what it owes now.

    Payments are charged in order and a plan has at most one charge in flight, so no
    new attempt can start while an earlier one is still unknown.
    """
    if not 1 <= payment_number <= len(plan.payments):
        raise PlanError("no such payment")
    if plan.in_flight is not None:
        raise PlanError("a charge is already in flight")
    payment = plan.payments[payment_number - 1]
    if payment.owed == 0:
        raise PlanError("nothing is owed on this payment")
    if any(earlier.owed > 0 for earlier in plan.payments[: payment_number - 1]):
        raise PlanError("an earlier payment is still owed")
    number = payment.attempts + 1
    key = charge_key(plan.id, payment_number, number)
    attempt = Attempt(payment_number=payment_number, number=number, amount=payment.owed, key=key)
    payments = _swap(plan.payments, replace(payment, attempts=number))
    return replace(plan, payments=payments, in_flight=attempt), attempt


def settle_charge(plan: Plan, key: str, captured: bool) -> tuple[Plan, int]:
    """Record how the charge in flight ended.

    Returns the new plan and the amount to send back now, which is more than zero only
    when a refund lowered the payment while it was being charged.
    """
    attempt = plan.in_flight
    if attempt is None or attempt.key != key:
        raise PlanError("this charge is not in flight")
    old = plan.payments[attempt.payment_number - 1]
    new = replace(old, collected=old.collected + attempt.amount) if captured else old
    payments = _swap(plan.payments, new)
    return replace(plan, payments=payments, in_flight=None), new.overpaid - old.overpaid


def _swap(payments: tuple[Payment, ...], new: Payment) -> tuple[Payment, ...]:
    return tuple(new if payment.number == new.number else payment for payment in payments)
