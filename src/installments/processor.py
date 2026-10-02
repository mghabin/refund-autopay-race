"""The payment processor interface, and a fake one that honours idempotency keys."""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from installments.plan import Attempt


class ProcessorTimeout(Exception):
    """No answer in time. The charge may or may not have been captured."""


@dataclass(frozen=True)
class Charge:
    plan_id: str
    payment_number: int
    amount: int
    captured: bool


class Processor(Protocol):
    def charge(self, plan_id: str, attempt: Attempt) -> bool:
        """Returns True if the money was captured. A repeated key gets the first answer again."""
        ...

    def was_captured(self, key: str) -> bool:
        """Status lookup by idempotency key."""
        ...

    def captures(self, plan_id: str) -> list[Charge]:
        """What the processor says it captured for a plan, as a settlement report would."""
        ...


class Outcome(Enum):
    SUCCEED = "succeed"
    FAIL = "fail"
    TIMEOUT_BEFORE_CAPTURE = "timeout before capture"
    TIMEOUT_AFTER_CAPTURE = "timeout after capture"


class FakeProcessor:
    """Each new key gets the next scripted outcome, and succeeds once the script runs out.

    On a timeout the processor has still finished the charge. Only the reply was lost.
    """

    def __init__(self, outcomes: Sequence[Outcome] = ()) -> None:
        self.outcomes = list(outcomes)
        self.charges: dict[str, Charge] = {}
        self.lookups: list[str] = []

    def charge(self, plan_id: str, attempt: Attempt) -> bool:
        known = self.charges.get(attempt.key)
        if known is not None:
            if known.amount != attempt.amount:
                raise ValueError(f"key {attempt.key} was first used for a different amount")
            return known.captured
        outcome = self.outcomes.pop(0) if self.outcomes else Outcome.SUCCEED
        captured = outcome in (Outcome.SUCCEED, Outcome.TIMEOUT_AFTER_CAPTURE)
        self.charges[attempt.key] = Charge(
            plan_id, attempt.payment_number, attempt.amount, captured
        )
        if outcome in (Outcome.TIMEOUT_BEFORE_CAPTURE, Outcome.TIMEOUT_AFTER_CAPTURE):
            raise ProcessorTimeout(attempt.key)
        return captured

    def was_captured(self, key: str) -> bool:
        # A real lookup can also fail or not know yet. This fake always knows.
        self.lookups.append(key)
        known = self.charges.get(key)
        return known is not None and known.captured

    def captures(self, plan_id: str) -> list[Charge]:
        return [c for c in self.charges.values() if c.plan_id == plan_id and c.captured]
