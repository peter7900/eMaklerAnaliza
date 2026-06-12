from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from .models import Side, Transaction

ENCODING = "cp1250"


def _find_header_line(lines: list[str]) -> tuple[int, str]:
    """Zwraca (indeks_linii, typ_pliku).

    Obsługiwane eksporty eMAKLER:
    - historia zleceń: nagłówek zaczyna się od ``Stan;...``
    - historia transakcji: nagłówek zaczyna się od ``Czas transakcji;...``
    """

    for i, line in enumerate(lines):
        if line.startswith("Stan;") and "Papier;" in line:
            return i, "orders"
        if line.startswith("Czas transakcji;") and "Papier;" in line:
            return i, "transactions"

    raise ValueError(
        "Nie znaleziono wiersza nagłówka (oczekiwano kolumn Stan;Papier;... lub Czas transakcji;Papier;...)."
    )


def _parse_float(raw: str) -> float | None:
    s = raw.strip().replace(" ", "").replace("\xa0", "")
    if not s or s.upper() == "PKC":
        return None
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _parse_money(raw: str) -> float:
    """Paruje kwotę (z separacją tysięcy spacją, przecinkiem dziesiętnym).

    Zwraca 0.0 dla pustego pola.
    """

    s = raw.strip().replace(" ", "").replace("\xa0", "")
    if not s:
        return 0.0
    s = s.replace(",", ".")
    return float(s)


def _parse_qty(raw: str) -> float:
    s = raw.strip().replace(" ", "").replace("\xa0", "")
    s = s.replace(",", ".")
    return float(s)


def _parse_ts(raw: str) -> datetime:
    raw = raw.strip()
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError(f"Nieznany format daty: {raw!r}")


def read_emakler_csv(path: Path) -> list[Transaction]:
    text = path.read_bytes().decode(ENCODING, errors="replace")
    lines = text.splitlines()
    header_idx, kind = _find_header_line(lines)
    body = "\n".join(lines[header_idx:])
    reader = csv.reader(body.splitlines(), delimiter=";")
    rows = list(reader)
    if not rows:
        return []

    header = rows[0]
    idx = {name.strip(): i for i, name in enumerate(header)}

    def _indices(name: str) -> list[int]:
        return [i for i, h in enumerate(header) if h.strip() == name]

    def _require_columns(required: list[str], *, kind_label: str) -> None:
        missing = [c for c in required if c not in idx]
        if missing:
            raise ValueError(
                f"Brak wymaganych kolumn w eksporcie eMAKLER ({kind_label}): {', '.join(missing)}. "
                f"Nagłówek: {', '.join(x.strip() for x in header)}"
            )

    out: list[Transaction] = []

    if kind == "orders":
        _require_columns(
            [
                "Stan",
                "Papier",
                "Giełda",
                "K/S",
                "Liczba zlecona",
                "Liczba zrealizowana",
                "Limit ceny",
                "Walute",
                "Data zlecenia",
            ],
            kind_label="historia zleceń",
        )

        for row in rows[1:]:
            if len(row) < len(header):
                continue
            stan = row[idx["Stan"]].strip()
            if stan != "Zrealizowane":
                continue
            papier = row[idx["Papier"]].strip()
            gielda = row[idx["Giełda"]].strip()
            side_raw = row[idx["K/S"]].strip().upper()
            if side_raw not in ("K", "S"):
                continue
            side: Side = side_raw  # type: ignore[assignment]
            qty_ordered = _parse_qty(row[idx["Liczba zlecona"]])
            qty_executed = _parse_qty(row[idx["Liczba zrealizowana"]])
            price = _parse_float(row[idx["Limit ceny"]])
            cur = row[idx["Walute"]].strip().upper() or "PLN"
            ts_raw = row[idx["Data zlecenia"]]
            ts = _parse_ts(ts_raw)
            out.append(
                Transaction(
                    stan=stan,
                    papier=papier,
                    gielda=gielda,
                    side=side,
                    qty_ordered=qty_ordered,
                    qty_executed=qty_executed,
                    price=price,
                    currency=cur,
                    ts=ts,
                    fee=0.0,
                    fee_currency="PLN",
                )
            )
        return out

    # kind == "transactions"
    _require_columns(
        [
            "Czas transakcji",
            "Papier",
            "Giełda",
            "K/S",
            "Liczba",
            "Kurs",
            "Prowizja",
        ],
        kind_label="historia transakcji",
    )

    # Uwaga: nagłówek ma kilka kolumn o nazwie "Waluta".
    waluta_idx = _indices("Waluta")


    if len(waluta_idx) < 2:
        raise ValueError(
            "Nieprawidłowy eksport historii transakcji: oczekiwano co najmniej 2 kolumn 'Waluta' "
            "(dla ceny i prowizji)."
        )

    for row in rows[1:]:
        if len(row) < len(header):
            continue
        ts = _parse_ts(row[idx["Czas transakcji"]])
        papier = row[idx["Papier"]].strip()
        gielda = row[idx["Giełda"]].strip()
        side_raw = row[idx["K/S"]].strip().upper()
        if side_raw not in ("K", "S"):
            continue
        side: Side = side_raw  # type: ignore[assignment]

        qty = _parse_qty(row[idx["Liczba"]])
        price = _parse_float(row[idx["Kurs"]])




        cur_price = (row[waluta_idx[0]].strip().upper() or "PLN")





        fee = _parse_money(row[idx["Prowizja"]])
        cur_fee = (row[waluta_idx[1]].strip().upper() or "PLN")

        out.append(
            Transaction(
                stan="Transakcja",
                papier=papier,
                gielda=gielda,
                side=side,
                qty_ordered=qty,
                qty_executed=qty,
                price=price,
                currency=cur_price,
                ts=ts,
                fee=fee,
                fee_currency=cur_fee,
            )
        )

    return out
