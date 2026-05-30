from __future__ import annotations

import contextlib
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import yaml
import yfinance as yf

from .ledger import total_qty
from .models import InstrumentKey, Lot, PositionLine
from .nbp import mid_pln_per_unit
from .sqlite_cache import MarketDataCache


def load_instruments_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("instruments.yaml musi być mapą klucz -> {yahoo: ...}")
    return data


def _yahoo_last_close(symbol: str, on: date) -> tuple[float, date]:
    """Ostatnie zamknięcie z sesji na lub przed `on`."""
    start = on - timedelta(days=40)
    end = on + timedelta(days=2)
    t = yf.Ticker(symbol)
    with open(os.devnull, "w", encoding="utf-8") as devnull:
        with contextlib.redirect_stderr(devnull):
            hist = t.history(start=start.isoformat(), end=end.isoformat(), auto_adjust=False)
            if hist.empty:
                hist = t.history(period="5y", auto_adjust=False)
    if hist.empty:
        raise RuntimeError(f"Brak historii Yahoo dla {symbol!r}")
    hist = hist.copy()
    hist["_d"] = [pd.Timestamp(x).date() for x in hist.index]
    hist = hist[hist["_d"] <= on]
    if hist.empty:
        raise RuntimeError(f"Brak notowania Yahoo dla {symbol!r} na lub przed {on}")
    last = hist.iloc[-1]
    close = float(last["Close"])
    d = last["_d"]
    return close, d


def last_close_for_instrument(yahoo: str, on: date) -> tuple[float, date]:
    return _yahoo_last_close(yahoo, on)


def collect_market_cache_keys(
    lots_by_key: dict[InstrumentKey, list[Lot]],
    as_of: date,
    instruments: dict[str, Any],
) -> tuple[set[tuple[str, date]], set[tuple[str, date]]]:
    """
    Zbiór (waluta, data) dla NBP oraz (yahoo_symbol, as_of) dla wyceny,
    zgodnie z logiką ``build_positions`` (pomija brak mapowania, skip, brak Yahoo).
    """
    nbp: set[tuple[str, date]] = set()
    yahoo: set[tuple[str, date]] = set()
    for key, lots in lots_by_key.items():
        if total_qty(lots) <= 1e-9:
            continue
        mkey = key.as_str()
        spec = instruments.get(mkey)
        if not spec or spec.get("skip"):
            continue
        yh = (spec.get("yahoo") or "").strip()
        if not yh:
            continue
        cur = lots[0].currency
        if not all(l.currency == cur for l in lots):
            continue
        yahoo.add((yh, as_of))
        for lot in lots:
            nbp.add((lot.currency.upper().strip(), lot.trade_date))
            if lot.fee and lot.fee_currency:
                nbp.add((lot.fee_currency.upper().strip(), lot.trade_date))
        nbp.add((cur.upper().strip(), as_of))
    return nbp, yahoo


def lot_fee_pln(
    lot: Lot, session: requests.Session, cache: MarketDataCache | None = None
) -> float:
    """Prowizja partii w PLN (wg kursu NBP z dnia transakcji).

    Prowizja w ``Lot`` jest przechowywana w walucie ``lot.fee_currency``.
    """

    if not lot.fee:
        return 0.0

    fee_cur = (lot.fee_currency or "PLN").upper().strip()
    if cache is not None:
        fx_fee = cache.mid_pln(fee_cur, lot.trade_date, session)
    else:
        fx_fee = mid_pln_per_unit(fee_cur, lot.trade_date, session=session)
    return lot.fee * fx_fee


def lot_cost_pln(
    lot: Lot, session: requests.Session, cache: MarketDataCache | None = None
) -> float:
    """Koszt partii w PLN po kursie NBP z dnia transakcji.

    Uwzględnia prowizję (commission), która zwiększa koszt nabycia.
    """

    if cache is not None:
        r = cache.mid_pln(lot.currency, lot.trade_date, session)
    else:
        r = mid_pln_per_unit(lot.currency, lot.trade_date, session=session)

    fee_pln = lot_fee_pln(lot, session, cache)
    return lot.qty * lot.unit_price * r + fee_pln


def build_positions(
    lots_by_key: dict[InstrumentKey, list[Lot]],
    as_of: date,
    instruments: dict[str, Any],
    session: requests.Session | None = None,
    cache: MarketDataCache | None = None,
) -> tuple[list[PositionLine], list[str], list[str]]:
    sess = session or requests.Session()
    lines: list[PositionLine] = []
    errors: list[str] = []
    skipped: list[str] = []

    if cache is not None:
        nbp_k, yahoo_k = collect_market_cache_keys(lots_by_key, as_of, instruments)
        cache.prepare_required_keys(nbp_k, yahoo_k)

    for key, lots in lots_by_key.items():
        q = total_qty(lots)
        if q <= 1e-9:
            continue
        mkey = key.as_str()
        spec = instruments.get(mkey)
        if not spec:
            errors.append(f"Brak mapowania w instruments.yaml: {mkey}")
            continue
        if spec.get("skip"):
            skipped.append(
                f"Pominięto wycenę (skip: true): {mkey}, stan {q:.4f} szt."
            )
            continue
        yahoo = spec.get("yahoo")
        if not yahoo:
            errors.append(f"Brak pola yahoo dla {mkey}")
            continue
        try:
            close, close_d = (
                cache.yahoo_last_close(yahoo, as_of)
                if cache is not None
                else last_close_for_instrument(yahoo, as_of)
            )
        except Exception as e:
            errors.append(f"Cena {mkey}: {e}")
            continue

        cur = lots[0].currency
        if not all(l.currency == cur for l in lots):
            errors.append(f"Różne waluty partii dla {mkey}")
            continue

        cost_pln = sum(lot_cost_pln(lot, sess, cache) for lot in lots)
        fx_val = (
            cache.mid_pln(cur, as_of, sess)
            if cache is not None
            else mid_pln_per_unit(cur, as_of, session=sess)
        )
        value_pln = q * close * fx_val
        pnl = value_pln - cost_pln

        lines.append(
            PositionLine(
                key=key,
                qty=q,
                currency=cur,
                lots=lots,
                value_pln=value_pln,
                cost_pln=cost_pln,
                pnl_pln=pnl,
                yahoo_symbol=yahoo,
                last_close=close,
                last_close_date=close_d,
            )
        )

    lines.sort(key=lambda x: x.key.papier)
    return lines, errors, skipped
