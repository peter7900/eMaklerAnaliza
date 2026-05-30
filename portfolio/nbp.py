from __future__ import annotations

from datetime import date, timedelta
import time

import requests

NBP_A = "https://api.nbp.pl/api/exchangerates/rates/a/{code}/{date}/"


def _previous_days(d: date, n: int) -> list[date]:
    out: list[date] = []
    cur = d
    for _ in range(n):
        out.append(cur)
        cur -= timedelta(days=1)
    return out


def mid_pln_per_unit(currency: str, on: date, session: requests.Session | None = None) -> float:
    """
    Średni kurs NBP (tabela A): PLN za 1 jednostkę waluty obcej.
    """
    c = currency.upper().strip()
    if c == "PLN":
        return 1.0
    sess = session or requests.Session()
    for day in _previous_days(on, 20):
        url = NBP_A.format(code=c.lower(), date=day.isoformat())

        # NBP potrafi zwrócić 429 (rate limit) przy wielu zapytaniach w krótkim czasie.
        # Prosty retry z backoffem.
        backoff_s = 0.5
        for attempt in range(6):
            r = sess.get(url, timeout=30)
            if r.status_code != 429:
                break
            time.sleep(backoff_s)
            backoff_s *= 2
        if r.status_code == 404:
            continue
        r.raise_for_status()
        data = r.json()
        rate = data["rates"][0]["mid"]
        return float(rate)
    raise RuntimeError(
        f"Brak kursu NBP dla {c} w okolicy dnia {on} (dni robocze / kod waluty)."
    )
