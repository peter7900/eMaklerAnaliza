from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterator

from .nbp import mid_pln_per_unit


@dataclass
class MarketDataCache:
    """Cache NBP i wyceny Yahoo w SQLite.

    W Etapie 2 cache jest współdzielony w jednej bazie (np. ``portfolio.sqlite``),
    więc domyślnie **nie czyścimy** wpisów na koniec uruchomienia.

    Tryb ``no_cache`` wymusza odświeżenie wpisów dla użytych kluczy (DELETE + fetch + upsert).
    """

    db_path: Path
    no_cache: bool
    purge_unused: bool
    _conn: sqlite3.Connection
    _required_nbp: set[tuple[str, str]]
    _required_yahoo: set[tuple[str, str]]

    @classmethod
    def open(
        cls, db_path: Path, *, no_cache: bool, purge_unused: bool = False
    ) -> MarketDataCache:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        obj = cls(
            db_path=db_path,
            no_cache=no_cache,
            purge_unused=purge_unused,
            _conn=conn,
            _required_nbp=set(),
            _required_yahoo=set(),
        )
        obj._init_schema()
        return obj

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS nbp_mid (
                currency TEXT NOT NULL,
                query_date TEXT NOT NULL,
                mid REAL NOT NULL,
                PRIMARY KEY (currency, query_date)
            );
            CREATE TABLE IF NOT EXISTS yahoo_close (
                yahoo_symbol TEXT NOT NULL,
                as_of_date TEXT NOT NULL,
                close REAL NOT NULL,
                close_date TEXT NOT NULL,
                PRIMARY KEY (yahoo_symbol, as_of_date)
            );
            """
        )
        # Uwaga: baza może zawierać inne tabele (np. transakcje) i własne meta.
        # Używamy oddzielnego klucza w meta, żeby uniknąć konfliktów.
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('market_schema_version', '1')"
        )
        self._conn.commit()

    def prepare_required_keys(
        self,
        nbp_keys: set[tuple[str, date]],
        yahoo_keys: set[tuple[str, date]],
    ) -> None:
        """Ustala dozwolony zbiór kluczy dla ``finalize()`` (purge)."""
        self._required_nbp = {(c.upper().strip(), d.isoformat()) for c, d in nbp_keys}
        self._required_yahoo = {(s.strip(), d.isoformat()) for s, d in yahoo_keys}

    def mid_pln(self, currency: str, query_date: date, session) -> float:
        c = currency.upper().strip()
        if c == "PLN":
            return 1.0
        qd = query_date.isoformat()
        self._required_nbp.add((c, qd))
        if self.no_cache:
            self._conn.execute(
                "DELETE FROM nbp_mid WHERE currency = ? AND query_date = ?",
                (c, qd),
            )
        else:
            row = self._conn.execute(
                "SELECT mid FROM nbp_mid WHERE currency = ? AND query_date = ?",
                (c, qd),
            ).fetchone()
            if row is not None:
                return float(row[0])
        mid = mid_pln_per_unit(c, query_date, session=session)
        self._conn.execute(
            "INSERT OR REPLACE INTO nbp_mid(currency, query_date, mid) VALUES (?,?,?)",
            (c, qd, mid),
        )
        self._conn.commit()
        return mid

    def yahoo_last_close(self, yahoo_symbol: str, as_of: date) -> tuple[float, date]:
        """Jedna wartość zamknięcia na ``as_of`` (sesja na lub przed ``as_of``)."""
        sym = yahoo_symbol.strip()
        ad = as_of.isoformat()
        key = (sym, ad)
        self._required_yahoo.add(key)
        if self.no_cache:
            self._conn.execute(
                "DELETE FROM yahoo_close WHERE yahoo_symbol = ? AND as_of_date = ?",
                (sym, ad),
            )
        else:
            row = self._conn.execute(
                "SELECT close, close_date FROM yahoo_close WHERE yahoo_symbol = ? AND as_of_date = ?",
                (sym, ad),
            ).fetchone()
            if row is not None:
                close = float(row[0])
                close_d = date.fromisoformat(row[1])
                return close, close_d

        from .pricing import _yahoo_last_close

        close, close_d = _yahoo_last_close(sym, as_of)
        self._conn.execute(
            """
            INSERT OR REPLACE INTO yahoo_close(yahoo_symbol, as_of_date, close, close_date)
            VALUES (?,?,?,?)
            """,
            (sym, ad, close, close_d.isoformat()),
        )
        self._conn.commit()
        return close, close_d

    def finalize(self) -> None:
        """Opcjonalne czyszczenie cache.

        Dla współdzielonej bazy (Etap 2) domyślnie nie czyścimy nic.
        """

        if not self.purge_unused:
            return

        cur = self._conn.cursor()
        cur.execute("SELECT currency, query_date FROM nbp_mid")
        for currency, query_date in cur.fetchall():
            if (currency, query_date) not in self._required_nbp:
                self._conn.execute(
                    "DELETE FROM nbp_mid WHERE currency = ? AND query_date = ?",
                    (currency, query_date),
                )
        cur.execute("SELECT yahoo_symbol, as_of_date FROM yahoo_close")
        for yahoo_symbol, as_of_date in cur.fetchall():
            if (yahoo_symbol, as_of_date) not in self._required_yahoo:
                self._conn.execute(
                    "DELETE FROM yahoo_close WHERE yahoo_symbol = ? AND as_of_date = ?",
                    (yahoo_symbol, as_of_date),
                )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


@contextmanager
def open_market_data_cache(db_path: Path, *, no_cache: bool) -> Iterator[MarketDataCache]:
    """Otwiera cache rynkowy w podanej bazie SQLite (np. ``portfolio.sqlite``)."""

    cache = MarketDataCache.open(db_path, no_cache=no_cache, purge_unused=False)
    try:
        yield cache
    finally:
        cache.finalize()
        cache.close()
