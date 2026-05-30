from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, datetime, time
from pathlib import Path
from typing import DefaultDict

import matplotlib.pyplot as plt
import requests

from .csv_parser import read_emakler_csv
from .db import get_stats, init_schema, insert_transactions, load_transactions, open_db
from .ledger import build_fifo_lots
from .models import InstrumentKey, Lot, Transaction
from .nbp import mid_pln_per_unit
from .pricing import build_positions, load_instruments_yaml, lot_fee_pln
from .sqlite_cache import MarketDataCache, open_market_data_cache


def _parse_date(s: str) -> date:
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


def _cutoff_datetime(as_of: date) -> datetime:
    return datetime.combine(as_of, time(23, 59, 59))


def _fmt_pct(pnl: float, denom: float, width: int = 12) -> str:
    if abs(denom) <= 1e-12:
        return f"{'—':>{width}}"
    return f"{(pnl / denom * 100):{width}.2f}"


def _build_fifo_lots_and_realized_pnl_pln(
    transactions: list[Transaction],
    as_of: date,
    *,
    session: requests.Session,
    cache: MarketDataCache | None,
) -> tuple[
    dict[InstrumentKey, list[Lot]],
    dict[InstrumentKey, float],
    dict[InstrumentKey, float],
    dict[InstrumentKey, float],
    list[str],
]:
    """FIFO dla całej historii (do ``as_of``) + zrealizowany P/L (PLN).

    Dodatkowo zwraca:
    - ``invested_pln_by_key``: łączny kapitał zainwestowany (suma kosztów wszystkich kupien) w PLN
      (uwzględnia prowizję kupna)
    - ``commissions_pln_by_key``: suma prowizji (kupno + sprzedaż) w PLN

    Zrealizowany P/L liczony jest jako suma (przychód - koszt FIFO) oraz pomniejszony o prowizję sprzedaży.
    """

    cutoff = _cutoff_datetime(as_of)
    relevant = [t for t in transactions if t.ts <= cutoff and t.qty_executed > 0]
    relevant.sort(key=lambda t: t.ts)

    lots: DefaultDict[InstrumentKey, list[Lot]] = defaultdict(list)
    realized: DefaultDict[InstrumentKey, float] = defaultdict(float)
    invested: DefaultDict[InstrumentKey, float] = defaultdict(float)
    commissions: DefaultDict[InstrumentKey, float] = defaultdict(float)
    warns: list[str] = []

    def fx_pln_per_unit(currency: str, on: date) -> float:
        if cache is not None:
            return cache.mid_pln(currency, on, session)
        return mid_pln_per_unit(currency, on, session=session)

    for t in relevant:
        key = InstrumentKey(papier=t.papier, gielda=t.gielda)
        q = t.qty_executed

        if t.side == "K":
            if t.price is None:
                warns.append(
                    f"Pominięto kupno bez ceny (PKC): {key.as_str()} {q} szt. @ {t.ts}"
                )
                continue

            fx_buy = fx_pln_per_unit(t.currency, t.ts.date())
            buy_fee_pln = 0.0
            if t.fee:
                buy_fee_pln = t.fee * fx_pln_per_unit(t.fee_currency or "PLN", t.ts.date())
                commissions[key] += buy_fee_pln

            invested[key] += q * t.price * fx_buy + buy_fee_pln

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
            continue

        # Sprzedaż
        if t.price is None:
            warns.append(
                f"Sprzedaż PKC (redukcja FIFO bez wyliczenia P/L): {key.as_str()} "
                f"{q} szt. @ {t.ts}"
            )

        sell_fee_pln_total = 0.0
        if t.fee:
            sell_fee_pln_total = t.fee * fx_pln_per_unit(t.fee_currency or "PLN", t.ts.date())
            commissions[key] += sell_fee_pln_total
            if t.price is not None:
                # prowizja sprzedaży zmniejsza przychód, niezależnie od dopasowania FIFO
                realized[key] -= sell_fee_pln_total

        remaining = q
        queue = lots[key]
        while remaining > 1e-9 and queue:
            head = queue[0]
            take = min(head.qty, remaining)

            # proporcjonalnie zdejmij prowizję z partii kupna (żeby otwarte lots miały poprawny koszt)
            head_qty_before = head.qty
            take_buy_fee = 0.0
            if head_qty_before > 1e-12 and head.fee:
                take_buy_fee = head.fee * (take / head_qty_before)
                head.fee -= take_buy_fee

            if t.price is not None:
                if head.currency.upper().strip() != t.currency.upper().strip():
                    warns.append(
                        f"Różne waluty kupna/sprzedaży dla {key.as_str()}: "
                        f"{head.currency} vs {t.currency} @ {t.ts}"
                    )

                fx_buy = fx_pln_per_unit(head.currency, head.trade_date)
                fx_sell = fx_pln_per_unit(t.currency, t.ts.date())

                buy_fee_pln = 0.0
                if take_buy_fee:
                    buy_fee_pln = take_buy_fee * fx_pln_per_unit(
                        head.fee_currency or "PLN", head.trade_date
                    )

                cost_pln = take * head.unit_price * fx_buy + buy_fee_pln
                proceeds_pln = take * t.price * fx_sell
                realized[key] += proceeds_pln - cost_pln

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
    open_lots = {k: [lot for lot in v if lot.qty > 1e-9] for k, v in lots.items() if v}
    return open_lots, dict(realized), dict(invested), dict(commissions), warns


def _resolve_details_key(pattern: str, transactions: list[Transaction]) -> InstrumentKey:
    pat = pattern.strip().upper()
    if not pat:
        raise ValueError("Pusty wzorzec w --details")

    keys = sorted({f"{t.papier}|{t.gielda}" for t in transactions})
    matches = [k for k in keys if pat in k.upper()]
    if not matches:
        raise ValueError(f"Brak dopasowań dla --details {pattern!r}")
    if len(matches) > 1:
        m = "\n".join(f"- {x}" for x in matches)
        raise ValueError(f"Wiele dopasowań dla --details {pattern!r}:\n{m}")

    papier, gielda = matches[0].split("|", 1)
    return InstrumentKey(papier=papier, gielda=gielda)


def _run_details_report(
    *,
    key: InstrumentKey,
    transactions: list[Transaction],
    tx_cutoff: date,
    valuation_date: date,
    instruments: dict,
    session: requests.Session,
    cache: MarketDataCache | None,
    show_pct: bool,
    show_comm: bool,
) -> None:
    cutoff_dt = _cutoff_datetime(tx_cutoff)
    txs = [
        t
        for t in transactions
        if t.papier == key.papier and t.gielda == key.gielda and t.ts <= cutoff_dt and t.qty_executed > 0
    ]
    txs.sort(key=lambda t: t.ts)
    if not txs:
        raise ValueError(f"Brak transakcji dla {key.as_str()} do {tx_cutoff}")

    spec = instruments.get(key.as_str())
    if spec is None:
        raise ValueError(f"Brak mapowania w instruments.yaml dla {key.as_str()}")
    if spec.get("skip"):
        raise ValueError(f"Wycena jest wyłączona (skip: true) dla {key.as_str()}")

    def fx_pln_per_unit(currency: str, on: date) -> float:
        if cache is not None:
            return cache.mid_pln(currency, on, session)
        return mid_pln_per_unit(currency, on, session=session)

    lots: list[Lot] = []
    qty_after = 0.0
    cum_realized = 0.0
    cum_invested = 0.0

    # Nagłówek
    print(f"Raport szczegółowy: {key.as_str()}")
    print(f"Transakcje do: {tx_cutoff} (włącznie)")
    print(f"Wycena na: {valuation_date}\n")

    # Tabela
    hdr_parts = [
        f"{'Data':<19}",
        f"{'K/S':>3}",
        f"{'Ilość':>10}",
        f"{'Kurs':>12}",
        f"{'Wal':>4}",
    ]
    if show_comm:
        hdr_parts.append(f"{'Prowizja PLN':>14}")
    hdr_parts += [
        f"{'Ilość po':>10}",
        f"{'P/L trans.':>14}",
        f"{'P/L skum.':>14}",
        f"{'Kapitał skum.':>14}",
    ]
    if show_pct:
        hdr_parts.append(f"{'P/L % skum.':>12}")

    hdr = " ".join(hdr_parts)
    print(hdr)
    print("-" * len(hdr))

    eps = 1e-9

    for t in txs:
        q = t.qty_executed
        if t.price is None:
            raise ValueError(f"Brak ceny transakcji (PKC) dla {key.as_str()} @ {t.ts}")

        fx_price = fx_pln_per_unit(t.currency, t.ts.date())
        gross_pln = q * t.price * fx_price

        fee_pln = 0.0
        if t.fee:
            fee_pln = t.fee * fx_pln_per_unit(t.fee_currency or 'PLN', t.ts.date())

        realized_tx = 0.0

        if t.side == "K":
            cum_invested += gross_pln + fee_pln
            qty_after += q
            lots.append(
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
            # Sprzedaż: realized P/L (FIFO) + walidacja stanu
            proceeds_net_pln = gross_pln - fee_pln
            cost_total_pln = 0.0
            remaining = q

            while remaining > eps:
                if not lots:
                    raise RuntimeError(
                        f"Sprzedaż przekracza stan dla {key.as_str()} o {remaining:.4f} szt. @ {t.ts}"
                    )
                head = lots[0]
                take = min(head.qty, remaining)

                head_qty_before = head.qty
                take_buy_fee = 0.0
                if head_qty_before > eps and head.fee:
                    take_buy_fee = head.fee * (take / head_qty_before)
                    head.fee -= take_buy_fee

                fx_buy = fx_pln_per_unit(head.currency, head.trade_date)
                buy_fee_pln = 0.0
                if take_buy_fee:
                    buy_fee_pln = take_buy_fee * fx_pln_per_unit(
                        head.fee_currency or 'PLN', head.trade_date
                    )

                cost_total_pln += take * head.unit_price * fx_buy + buy_fee_pln

                head.qty -= take
                remaining -= take
                if head.qty <= eps:
                    lots.pop(0)

            realized_tx = proceeds_net_pln - cost_total_pln
            cum_realized += realized_tx
            qty_after -= q

        pct_txt = _fmt_pct(cum_realized, cum_invested, width=12) if show_pct else ""

        row_parts = [
            f"{t.ts:%Y-%m-%d %H:%M:%S}",
            f"{t.side:>3}",
            f"{q:10.4f}",
            f"{t.price:12.4f}",
            f"{t.currency:>4}",
        ]
        if show_comm:
            row_parts.append(f"{fee_pln:14.2f}")
        row_parts += [
            f"{qty_after:10.4f}",
            f"{realized_tx:14.2f}",
            f"{cum_realized:14.2f}",
            f"{cum_invested:14.2f}",
        ]
        if show_pct:
            row_parts.append(pct_txt)

        print(" ".join(row_parts))

    print("-" * len(hdr))

    # Wycena końcowa
    open_qty = sum(l.qty for l in lots)
    realized_total = cum_realized
    invested_total = cum_invested

    unrealized = 0.0
    value_pln = 0.0
    cost_pln = 0.0

    if open_qty > eps:
        lines, errs, _skipped = build_positions(
            {key: lots}, valuation_date, instruments, session=session, cache=cache
        )
        if errs:
            raise RuntimeError("; ".join(errs))
        if not lines:
            raise RuntimeError(f"Brak wyceny dla {key.as_str()}")
        line = lines[0]
        value_pln = line.value_pln
        cost_pln = line.cost_pln
        unrealized = line.pnl_pln

    total_pnl = realized_total + unrealized

    print(f"\nPodsumowanie dla {key.as_str()}:")
    print(f"- Kapitał zainwestowany (suma kupien, PLN): {invested_total:.2f}")
    print(f"- P/L zrealizowany (PLN): {realized_total:.2f}")
    print(f"- Ilość otwarta: {open_qty:.4f}")
    if open_qty > eps:
        print(f"- Koszt otwartej pozycji (PLN): {cost_pln:.2f}")
        print(f"- Wartość rynkowa (PLN): {value_pln:.2f}")
        print(f"- P/L niezrealizowany (PLN): {unrealized:.2f}")
    print(f"- P/L razem (PLN): {total_pnl:.2f}")
    if show_pct:
        pct_txt_total = "—" if abs(invested_total) <= eps else f"{(total_pnl / invested_total * 100):.2f}"
        print(f"- P/L razem (%): {pct_txt_total}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Stan portfela na datę z eksportu mBank + wycena i P/L (FIFO, PLN, NBP)."
    )
    p.add_argument(
        "--csv",
        type=Path,
        required=False,
        default=None,
        help=(
            "Ścieżka do pliku eMAKLER (CSV, średnik, cp1250). "
            "Jeśli nie podasz --csv, program użyje transakcji zapisanych w bazie (--db)."
        ),
    )
    p.add_argument(
        "--db",
        type=Path,
        default=Path("portfolio.sqlite"),
        help="Ścieżka do bazy SQLite z transakcjami (domyślnie: ./portfolio.sqlite).",
    )
    p.add_argument(
        "--import-only",
        action="store_true",
        help="Tylko importuje transakcje z --csv do bazy i kończy działanie.",
    )
    p.add_argument(
        "--stats",
        action="store_true",
        help="Pokazuje statystyki bazy (--db) i kończy działanie.",
    )
    p.add_argument(
        "--date",
        type=_parse_date,
        default=None,
        help=(
            "Data stanu w formacie YYYY-MM-DD. "
            "Jeśli nie podasz --date, program wygeneruje raport zbiorczy P/L za całą historię z bazy (wszystkie zaimportowane transakcje)."
        ),
    )
    p.add_argument(
        "--instruments",
        type=Path,
        default=None,
        help="instruments.yaml (domyślnie obok pakietu: ../instruments.yaml).",
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--tickers",
        nargs="+",
        type=str,
        default=None,
        help=(
            "Ograniczenie do konkretnych tickerów (domyślnie wszystkie z CSV; "
            "format: TICKER1 TICKER2 ...)."
        ),
    )
    g.add_argument(
        "--details",
        type=str,
        default=None,
        help=(
            "Raport szczegółowy dla jednego papieru (dopasowanie jak w --tickers; substring, bez rozróżniania wielkości liter). "
            "Przykład: --details ORACLE"
        ),
    )
    p.add_argument(
        "--no-cache",
        action="store_true",
        help="Wymuś pobranie z sieci (pomiń odczyt z SQLite; przed pobraniem czyść wpisy dla używanych kluczy).",
    )
    p.add_argument(
        "--plot",
        action="store_true",
        help="Pokazuje wykres słupkowy (zysk/strata dla każdego tickera).",
    )
    p.add_argument(
        "--pct",
        action="store_true",
        help="Dodaje kolumnę zysku/straty procentowo (P/L %%).",
    )
    p.add_argument(
        "--comm",
        action="store_true",
        help="Dodaje kolumnę prowizji (PLN).",
    )
    args = p.parse_args(argv)

    base = Path(__file__).resolve().parent.parent
    inst_path = args.instruments or (base / "instruments.yaml")
    if not inst_path.is_file():
        print(f"Brak pliku mapowań: {inst_path}", file=sys.stderr)
        return 2

    instruments = load_instruments_yaml(inst_path)

    # DB init + (opcjonalnie) import z CSV
    conn = open_db(args.db)
    try:
        init_schema(conn)

        if args.import_only and args.csv is None:
            print("[✗] --import-only wymaga --csv", file=sys.stderr)
            return 2

        if args.csv is not None:
            txs_file = read_emakler_csv(args.csv)
            if not txs_file:
                print("[!] Brak transakcji w CSV (nic do importu).", file=sys.stderr)
            else:
                res = insert_transactions(conn, txs_file, source=str(args.csv))
                print(
                    f"[i] Import #{res.import_id}: seen={res.tx_seen}, inserted={res.tx_inserted}, "
                    f"ignored={res.tx_ignored}, file_dups={res.file_dup_count}",
                    file=sys.stderr,
                )
                for h, c, short in res.file_dups:
                    print(
                        f"[!] Duplikat w pliku CSV (hash={h}, count={c}) — do bazy trafi tylko raz. Rekord: {short}",
                        file=sys.stderr,
                    )

        if args.import_only:
            return 0

        if args.stats:
            st = get_stats(conn)
            print(f"DB: {args.db}")
            print(f"Transakcje: {st.tx_count}")
            print(f"Zakres: {st.first_ts} .. {st.last_ts}")
            print(f"Instrumenty: {st.instrument_count}")
            print(f"Importy: {st.import_count}")
            return 0

        txs = load_transactions(conn)
    finally:
        conn.close()

    if not txs:
        print(
            "Brak transakcji w bazie. Podaj --csv aby zaimportować dane.",
            file=sys.stderr,
        )
        return 1

    # Tryb szczegółowy dla jednego papieru
    if args.details:
        tx_cutoff = args.date or max(t.ts.date() for t in txs)
        valuation_d = args.date or date.today()
        key = _resolve_details_key(args.details, txs)
        sess = requests.Session()

        with open_market_data_cache(args.db, no_cache=args.no_cache) as cache:
            try:
                _run_details_report(
                    key=key,
                    transactions=txs,
                    tx_cutoff=tx_cutoff,
                    valuation_date=valuation_d,
                    instruments=instruments,
                    session=sess,
                    cache=cache,
                    show_pct=args.pct,
                    show_comm=args.comm,
                )
            except ValueError as e:
                print(f"[✗] {e}", file=sys.stderr)
                return 2
            except RuntimeError as e:
                print(f"[✗] {e}", file=sys.stderr)
                return 1
        return 0

    want = [t.upper() for t in args.tickers] if args.tickers else None

    def is_wanted(label: str) -> bool:
        if not want:
            return True
        u = label.upper()
        return any(t in u for t in want)

    if want:
        instruments = {k: v for k, v in instruments.items() if is_wanted(k)}

    summary_mode = args.date is None

    # Dla raportu zbiorczego (bez --date):
    # - transakcje bierzemy z całej historii z CSV (do ostatniej transakcji)
    # - wycenę pozycji robimy na dzień dzisiejszy (ostatnie zamknięcie na lub przed tę datę)
    tx_as_of = max(t.ts.date() for t in txs)
    valuation_date = date.today() if summary_mode else args.date  # type: ignore[assignment]

    # Dla trybu na datę: zarówno cutoff transakcji jak i wycena są na tę samą datę.
    as_of = valuation_date if not summary_mode else tx_as_of

    sess = requests.Session()

    with open_market_data_cache(args.db, no_cache=args.no_cache) as cache:

        if summary_mode:
            (
                lots_by_key,
                realized_by_key,
                invested_by_key,
                commissions_by_key,
                ledger_warns,
            ) = _build_fifo_lots_and_realized_pnl_pln(
                txs, tx_as_of, session=sess, cache=cache
            )

            lines, errs, skipped = build_positions(
                lots_by_key, valuation_date, instruments, session=sess, cache=cache
            )

            for w in ledger_warns:
                print(f"[!] {w}", file=sys.stderr)
            for s in skipped:
                print(f"[i] {s}", file=sys.stderr)
            for e in errs:
                print(f"[✗] {e}", file=sys.stderr)

            realized_pnls: DefaultDict[str, float] = defaultdict(float)
            invested_p: DefaultDict[str, float] = defaultdict(float)
            comm_p: DefaultDict[str, float] = defaultdict(float)

            for key, pnl in realized_by_key.items():
                realized_pnls[key.papier] += pnl
            for key, inv in invested_by_key.items():
                invested_p[key.papier] += inv
            for key, c in commissions_by_key.items():
                comm_p[key.papier] += c

            unrealized_pnls: DefaultDict[str, float] = defaultdict(float)
            for line in lines:
                unrealized_pnls[line.key.papier] += line.pnl_pln

            tickers = sorted({*realized_pnls.keys(), *unrealized_pnls.keys(), *invested_p.keys()})
            tickers = [t for t in tickers if is_wanted(t)]

            if not tickers:
                return 1

            w = max(12, max((len(t) for t in tickers), default=10) + 2)

            print(
                "Raport zbiorczy P/L za cały okres z bazy (brak --date)\n"
                f"Zakres transakcji: cała historia do {tx_as_of} (włącznie)\n"
                f"Wycena na dzień: {valuation_date} (kurs zamknięcia na lub przed tą datę; NBP w okolicy tej daty)\n"
                "P/L razem = P/L zrealizowany (sprzedaże FIFO) + P/L niezrealizowany (wycena otwartych pozycji)\n"
            )

            hdr_parts = [
                f"{'Papier':<{w}}",
                f"{'Zrealizowany P/L':>18}",
                f"{'Niezrealizowany P/L':>20}",
                f"{'P/L razem':>14}",
            ]
            if args.comm:
                hdr_parts.append(f"{'Prowizje PLN':>14}")
            if args.pct:
                hdr_parts.append(f"{'P/L %':>12}")
            hdr = " ".join(hdr_parts)

            print(hdr)
            print("-" * len(hdr))

            tot_r = tot_u = tot_t = 0.0
            tot_inv = 0.0
            tot_comm = 0.0
            ticker_pnls: dict[str, float] = {}

            for t in tickers:
                r = float(realized_pnls.get(t, 0.0))
                u = float(unrealized_pnls.get(t, 0.0))
                total = r + u
                inv = float(invested_p.get(t, 0.0))
                comm = float(comm_p.get(t, 0.0))

                tot_r += r
                tot_u += u
                tot_t += total
                tot_inv += inv
                tot_comm += comm
                ticker_pnls[t] = total

                row_parts = [
                    f"{t:<{w}}",
                    f"{r:18.2f}",
                    f"{u:20.2f}",
                    f"{total:14.2f}",
                ]
                if args.comm:
                    row_parts.append(f"{comm:14.2f}")
                if args.pct:
                    row_parts.append(_fmt_pct(total, inv, width=12))
                print(" ".join(row_parts))

            print("-" * len(hdr))

            tot_parts = [
                f"{'PORTFEL (suma)':<{w}}",
                f"{tot_r:18.2f}",
                f"{tot_u:20.2f}",
                f"{tot_t:14.2f}",
            ]
            if args.comm:
                tot_parts.append(f"{tot_comm:14.2f}")
            if args.pct:
                tot_parts.append(_fmt_pct(tot_t, tot_inv, width=12))
            print(" ".join(tot_parts))

            if args.plot:
                tickers_p = list(ticker_pnls.keys())
                pnls = list(ticker_pnls.values())
                plt.figure(figsize=(10, 6))
                plt.bar(tickers_p, pnls, color=["green" if v >= 0 else "red" for v in pnls])
                plt.xlabel("Papier (ticker)")
                plt.ylabel("Zysk/Strata (PLN)")
                plt.title(
                    f"Zysk/Strata dla każdego tickera (transakcje do {tx_as_of}; wycena {valuation_date})"
                )
                plt.axhline(0, color="black", linewidth=0.8)
                plt.xticks(rotation=45, ha="right")
                plt.tight_layout()
                plt.show()

            return 0

        # Tryb dotychczasowy: stan na wybrany dzień
        lots_by_key, ledger_warns = build_fifo_lots(txs, as_of)

        lines, errs, skipped = build_positions(
            lots_by_key, as_of, instruments, session=sess, cache=cache
        )

        for w in ledger_warns:
            print(f"[!] {w}", file=sys.stderr)
        for s in skipped:
            print(f"[i] {s}", file=sys.stderr)
        for e in errs:
            print(f"[✗] {e}", file=sys.stderr)

        if not lines:
            return 1

        w = max(12, max((len(x.key.papier) for x in lines), default=10) + 2)

        print(
            f"Stan na dzień {as_of} (wartość wg kursu zamknięcia; koszt FIFO w PLN po NBP z dnia nabycia)\n"
        )

        hdr_parts = [
            f"{'Papier':<{w}}",
            f"{'Ilość':>10}",
            f"{'Wal':>4}",
            f"{'Kurs':>12}",
            f"{'Kurs data':>12}",
            f"{'Wartość PLN':>14}",
            f"{'Koszt PLN':>14}",
        ]
        if args.comm:
            hdr_parts.append(f"{'Prowizja PLN':>14}")
        hdr_parts.append(f"{'Zysk/Strata':>14}")
        if args.pct:
            hdr_parts.append(f"{'P/L %':>12}")
        hdr = " ".join(hdr_parts)

        print(hdr)
        print("-" * len(hdr))

        tot_v = tot_c = tot_p = 0.0
        tot_comm = 0.0
        ticker_pnls: dict[str, float] = {}

        for line in lines:
            comm_pln = 0.0
            if args.comm:
                comm_pln = sum(lot_fee_pln(lot, sess, cache) for lot in line.lots)

            tot_v += line.value_pln
            tot_c += line.cost_pln
            tot_p += line.pnl_pln
            tot_comm += comm_pln
            ticker_pnls[line.key.papier] = line.pnl_pln

            row_parts = [
                f"{line.key.papier:<{w}}",
                f"{line.qty:10.4f}",
                f"{line.currency:>4}",
                f"{line.last_close:12.4f}",
                f"{str(line.last_close_date):>12}",
                f"{line.value_pln:14.2f}",
                f"{line.cost_pln:14.2f}",
            ]
            if args.comm:
                row_parts.append(f"{comm_pln:14.2f}")
            row_parts.append(f"{line.pnl_pln:14.2f}")
            if args.pct:
                row_parts.append(_fmt_pct(line.pnl_pln, line.cost_pln, width=12))
            print(" ".join(row_parts))

        print("-" * len(hdr))

        tot_parts = [
            f"{'PORTFEL (suma)':<{w}}",
            f"{'':>10}",
            f"{'':>4}",
            f"{'':>12}",
            f"{'':>12}",
            f"{tot_v:14.2f}",
            f"{tot_c:14.2f}",
        ]
        if args.comm:
            tot_parts.append(f"{tot_comm:14.2f}")
        tot_parts.append(f"{tot_p:14.2f}")
        if args.pct:
            tot_parts.append(_fmt_pct(tot_p, tot_c, width=12))
        print(" ".join(tot_parts))

        if args.plot:
            tickers_p = list(ticker_pnls.keys())
            pnls = list(ticker_pnls.values())
            plt.figure(figsize=(10, 6))
            plt.bar(tickers_p, pnls, color=["green" if v >= 0 else "red" for v in pnls])
            plt.xlabel("Papier (ticker)")
            plt.ylabel("Zysk/Strata (PLN)")
            plt.title("Zysk/Strata dla każdego tickera")
            plt.axhline(0, color="black", linewidth=0.8)
            plt.xticks(rotation=45, ha="right")
            plt.tight_layout()
            plt.show()

        return 0


if __name__ == "__main__":
    raise SystemExit(main())
