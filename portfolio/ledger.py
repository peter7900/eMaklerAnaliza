from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time
from typing import DefaultDict

from .models import InstrumentKey, Lot, Transaction


def _cutoff_datetime(as_of: date) -> datetime:
    return datetime.combine(as_of, time(23, 59, 59))


def build_fifo_lots(
    transactions: list[Transaction], as_of: date
) -> tuple[dict[InstrumentKey, list[Lot]], list[str]]:
    """Zwraca pozostałe partie FIFO oraz komunikaty ostrzeżeń."""
    cutoff = _cutoff_datetime(as_of)
    relevant = [t for t in transactions if t.ts <= cutoff and t.qty_executed > 0]
    relevant.sort(key=lambda t: t.ts)

    lots: DefaultDict[InstrumentKey, list[Lot]] = defaultdict(list)
    warns: list[str] = []

    for t in relevant:
        key = InstrumentKey(papier=t.papier, gielda=t.gielda)
        q = t.qty_executed
        if t.side == "K":
            if t.price is None:
                warns.append(
                    f"Pominięto kupno bez ceny (PKC): {key.as_str()} {q} szt. @ {t.ts}"
                )
                continue
            lots[key].append(
                Lot(
                    qty=q,
                    unit_price=t.price,
                    currency=t.currency,
                    trade_date=t.ts.date(),
                    fee=t.fee,
                    fee_currency=t.fee_currency,
                )
            )
        else:
            if t.price is None:
                warns.append(
                    f"Sprzedaż PKC (tylko redukcja stanu FIFO): {key.as_str()} "
                    f"{q} szt. @ {t.ts}"
                )
            remaining = q
            queue = lots[key]
            while remaining > 1e-9 and queue:
                head = queue[0]
                take = min(head.qty, remaining)

                # proporcjonalnie zdejmij prowizję z partii kupna
                head_qty_before = head.qty
                if head_qty_before > 1e-12 and head.fee:
                    take_fee = head.fee * (take / head_qty_before)
                    head.fee -= take_fee

                head.qty -= take
                remaining -= take
                if head.qty <= 1e-9:
                    queue.pop(0)
            if remaining > 1e-6:
                warns.append(
                    f"Sprzedaż przekracza stan (krótka sprzedaż?): {key.as_str()} "
                    f"o {remaining:.4f} szt. @ {t.ts}"
                )

    # usuń puste kolejki
    return {k: [lot for lot in v if lot.qty > 1e-9] for k, v in lots.items() if v}, warns


def total_qty(lots: list[Lot]) -> float:
    return sum(lot.qty for lot in lots)
