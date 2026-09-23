from __future__ import annotations

import enum
from typing import Optional, Protocol, overload


class Color(enum.Enum):
    RED = 1
    GREEN = 2


class Priced(Protocol):
    def price(self) -> int: ...


class Order:
    count = 0

    def __init__(self, total: int):
        self.total = total

    @staticmethod
    def empty() -> "Order":
        return Order(0)

    @classmethod
    def of(cls, total: int) -> "Order":
        return cls(total)

    def price(self) -> int:
        return self.total


class Rush(Order):
    def price(self) -> int:
        return super().price() * 2


def summary(order: Optional[Order]) -> str:
    return str(order.price()) if order else "-"


def total(xs: list[int]) -> int:
    return sum(xs)


def report(orders: list[Order]) -> list[int]:
    doubled = [total for total in (o.price() for o in orders)]
    with open("log.txt") as total:
        total.write("x")
    try:
        pass
    except ValueError as total:
        print(total)
    return doubled


def grand_total(orders: list[Order]) -> int:
    return total([o.price() for o in orders])


scale = lambda v: v * 2  # noqa: E731


async def fetch_order(order_id: int) -> Order:
    return Order.of(order_id)
