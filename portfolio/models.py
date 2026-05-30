from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

Side = Literal["K", "S"]


@dataclass(frozen=True, slots=True)
class Transaction:
    stan: str
    papier: str
    gielda: str
    side: Side
    qty_ordered: float
    qty_executed: float
    price: float | None
    currency: str
    ts: datetime
    fee: float = 0.0
    fee_currency: str = "PLN"


@dataclass(frozen=True, slots=True)
class InstrumentKey:
    papier: str
    gielda: str

    def as_str(self) -> str:
        return f"{self.papier}|{self.gielda}"


@dataclass(slots=True)
class Lot:
    qty: float
    unit_price: float
    currency: str
    trade_date: date
    fee: float = 0.0
    fee_currency: str = "PLN"


@dataclass(slots=True)
class PositionLine:
    key: InstrumentKey
    qty: float
    currency: str
    lots: list[Lot]
    value_pln: float
    cost_pln: float
    pnl_pln: float
    yahoo_symbol: str
    last_close: float
    last_close_date: date
