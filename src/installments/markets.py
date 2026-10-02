"""What differs between markets, behind one small interface.

A new market is one new class and one line in MARKETS.
"""

from datetime import date, timedelta
from typing import Protocol


class Market(Protocol):
    currency: str
    payback_destination: str  # where money owed back to the customer is sent
    refund_window_days: int  # published business days to apply a refund once it arrives

    def is_business_day(self, day: date) -> bool: ...


class UnitedStates:
    currency = "USD"
    payback_destination = "payment method"
    refund_window_days = 3

    def is_business_day(self, day: date) -> bool:
        return day.weekday() < 5  # weekends only, bank holidays are not modelled yet


class UnitedKingdom:
    currency = "GBP"
    payback_destination = "original payment method, else bank transfer to the customer's account"
    refund_window_days = 10

    def is_business_day(self, day: date) -> bool:
        return day.weekday() < 5  # weekends only, bank holidays are not modelled yet


MARKETS: dict[str, Market] = {"US": UnitedStates(), "UK": UnitedKingdom()}


def add_business_days(market: Market, start: date, days: int) -> date:
    day = start
    left = days
    while left > 0:
        day += timedelta(days=1)
        if market.is_business_day(day):
            left -= 1
    return day


def refund_is_late(market: Market, received_on: date, applied_on: date) -> bool:
    return applied_on > add_business_days(market, received_on, market.refund_window_days)
