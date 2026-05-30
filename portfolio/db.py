from __future__ import annotations

import hashlib
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from .models import Transaction


SCHEMA_VERSION = 1


def _q(v: Decimal, places: str) -> Decimal:
    return v.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def transaction_hash(t: Transaction) -> str:
    """Stabilny hash transakcji do deduplikacji.

    Hash liczony jest z kanonicznej reprezentacji pól (po normalizacji stringów i
    kwantyzacji liczb), żeby ten sam rekord z różnych eksportów CSV dawał taki sam klucz.
    """

    ts = t.ts.replace(microsecond=0).isoformat()
    papier = t.papier.strip().upper()
    gielda = t.gielda.strip().upper()
    side = t.side

    qty = _q(Decimal(str(t.qty_executed)), "0.000001")

    price_s = ""
    if t.price is not None:
        price_s = str(_q(Decimal(str(t.price)), "0.000001"))

    currency = (t.currency or "PLN").strip().upper()

    fee = _q(Decimal(str(t.fee or 0.0)), "0.0001")
    fee_currency = (t.fee_currency or "PLN").strip().upper()

    payload = "|".join(
        [
            ts,
            papier,
            gielda,
            side,
            str(qty),
            price_s,
            currency,
            str(fee),
            fee_currency,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS imports (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            tx_seen INTEGER NOT NULL,
            tx_inserted INTEGER NOT NULL,
            tx_ignored INTEGER NOT NULL,
            file_dup_count INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY,
            tx_hash TEXT NOT NULL UNIQUE,
            ts TEXT NOT NULL,
            papier TEXT NOT NULL,
            gielda TEXT NOT NULL,
            side TEXT NOT NULL,
            qty REAL NOT NULL,
            price REAL,
            currency TEXT NOT NULL,
            fee REAL NOT NULL DEFAULT 0,
            fee_currency TEXT NOT NULL DEFAULT 'PLN'
        );

        -- Mapowanie transakcja <-> import (ta sama transakcja może pojawić się w wielu eksportach)
        CREATE TABLE IF NOT EXISTS transaction_imports (
            tx_hash TEXT NOT NULL,
            import_id INTEGER NOT NULL,
            PRIMARY KEY (tx_hash, import_id),
            FOREIGN KEY (tx_hash) REFERENCES transactions(tx_hash) ON DELETE CASCADE,
            FOREIGN KEY (import_id) REFERENCES imports(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_transactions_key_ts ON transactions(papier, gielda, ts);
        CREATE INDEX IF NOT EXISTS idx_transactions_ts ON transactions(ts);
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


@dataclass(frozen=True)
class ImportResult:
    import_id: int
    tx_seen: int
    tx_inserted: int
    tx_ignored: int
    file_dup_count: int
    # lista (hash, liczba_wystąpień, skrót rekordu) dla duplikatów w obrębie importowanego pliku
    file_dups: list[tuple[str, int, str]]


def insert_transactions(
    conn: sqlite3.Connection, txs: list[Transaction], *, source: str
) -> ImportResult:
    # duplikaty w obrębie pliku
    hashes = [transaction_hash(t) for t in txs]
    counts = Counter(hashes)
    first: dict[str, Transaction] = {}
    for t, h in zip(txs, hashes, strict=True):
        first.setdefault(h, t)

    dup_hashes = [(h, c) for h, c in counts.items() if c > 1]
    file_dup_count = sum(c - 1 for _h, c in dup_hashes)

    def _short(t: Transaction) -> str:
        ts = t.ts.replace(microsecond=0).isoformat(sep=" ")
        price = "PKC" if t.price is None else f"{t.price:.6f}"
        return (
            f"{ts}; {t.papier}|{t.gielda}; {t.side}; qty={t.qty_executed:.6f}; "
            f"price={price} {t.currency}; fee={t.fee:.4f} {t.fee_currency}"
        )

    # insert import record (tymczasowo z zerami; uzupełnimy po wstawieniu)
    now = datetime.now().replace(microsecond=0).isoformat()
    cur = conn.execute(
        """
        INSERT INTO imports(source, imported_at, tx_seen, tx_inserted, tx_ignored, file_dup_count)
        VALUES (?,?,?,?,?,?)
        """,
        (source, now, len(txs), 0, 0, file_dup_count),
    )
    import_id = int(cur.lastrowid)

    inserted = 0
    ignored = 0

    for t, h in zip(txs, hashes, strict=True):
        res = conn.execute(
            """
            INSERT OR IGNORE INTO transactions(
                tx_hash, ts, papier, gielda, side, qty, price, currency, fee, fee_currency
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                h,
                t.ts.replace(microsecond=0).isoformat(),
                t.papier,
                t.gielda,
                t.side,
                float(t.qty_executed),
                float(t.price) if t.price is not None else None,
                t.currency,
                float(t.fee or 0.0),
                t.fee_currency or "PLN",
            ),
        )
        if res.rowcount == 1:
            inserted += 1
        else:
            ignored += 1

        # zawsze odnotuj powiązanie z importem (jeśli transakcja już istnieje, to i tak będzie FK OK)
        conn.execute(
            "INSERT OR IGNORE INTO transaction_imports(tx_hash, import_id) VALUES (?,?)",
            (h, import_id),
        )

    conn.execute(
        """
        UPDATE imports
        SET tx_inserted = ?, tx_ignored = ?
        WHERE id = ?
        """,
        (inserted, ignored, import_id),
    )

    conn.commit()
    return ImportResult(
        import_id=import_id,
        tx_seen=len(txs),
        tx_inserted=inserted,
        tx_ignored=ignored,
        file_dup_count=file_dup_count,
        file_dups=[
            (h, c, _short(first[h])) for h, c in sorted(dup_hashes, key=lambda x: (-x[1], x[0]))
        ],
    )


def load_transactions(conn: sqlite3.Connection) -> list[Transaction]:
    rows = conn.execute(
        """
        SELECT ts, papier, gielda, side, qty, price, currency, fee, fee_currency
        FROM transactions
        ORDER BY ts
        """
    ).fetchall()
    out: list[Transaction] = []
    for ts, papier, gielda, side, qty, price, currency, fee, fee_currency in rows:
        out.append(
            Transaction(
                stan="DB",
                papier=str(papier),
                gielda=str(gielda),
                side=str(side),  # type: ignore[arg-type]
                qty_ordered=float(qty),
                qty_executed=float(qty),
                price=float(price) if price is not None else None,
                currency=str(currency),
                ts=datetime.fromisoformat(ts),
                fee=float(fee or 0.0),
                fee_currency=str(fee_currency or "PLN"),
            )
        )
    return out


@dataclass(frozen=True)
class DbStats:
    tx_count: int
    first_ts: str | None
    last_ts: str | None
    instrument_count: int
    import_count: int


def get_stats(conn: sqlite3.Connection) -> DbStats:
    tx_count = int(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
    first_ts = conn.execute("SELECT MIN(ts) FROM transactions").fetchone()[0]
    last_ts = conn.execute("SELECT MAX(ts) FROM transactions").fetchone()[0]
    instrument_count = int(
        conn.execute(
            "SELECT COUNT(DISTINCT papier || '|' || gielda) FROM transactions"
        ).fetchone()[0]
    )
    import_count = int(conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0])
    return DbStats(
        tx_count=tx_count,
        first_ts=first_ts,
        last_ts=last_ts,
        instrument_count=instrument_count,
        import_count=import_count,
    )
