"""FX reference-rate source adapter (Frankfurter v2).

Shared by the `fx-mcp` server and the app's direct-HTTP fallback, so both routes produce
identical numbers. Rates are returned as `Decimal`; the returned `date` is the provider's
actual rate date (previous business day for weekends/holidays when a single provider is pinned).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.frankfurter.dev/v2"
DEFAULT_PROVIDER = "ECB"


class FXSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Rate:
    date: dt.date
    base: str
    quote: str
    rate: Decimal  # units of `quote` per 1 `base`

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "base": self.base,
            "quote": self.quote,
            "rate": str(self.rate),
        }


def _base_url(url: str) -> str:
    url = url.rstrip("/")
    # Accept a configured host root or a legacy /v1 URL; always talk v2.
    if url.endswith("/v1"):
        url = url[:-3]
    return url if url.endswith("/v2") else url + "/v2"


class FrankfurterProvider:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        provider: str | None = DEFAULT_PROVIDER,
        timeout_s: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = _base_url(base_url)
        self.provider = provider
        self._client = client or httpx.Client(
            timeout=timeout_s, headers={"User-Agent": "finpilot-fx/0.1"}
        )

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        if self.provider:
            params = {**params, "providers": self.provider}
        try:
            r = self._client.get(f"{self.base_url}{path}", params=params)
        except httpx.HTTPError as e:
            raise FXSourceError(f"FX source unreachable: {type(e).__name__}") from e
        if r.status_code != 200:
            raise FXSourceError(f"FX source HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    @staticmethod
    def _parse(rows: Any) -> list[Rate]:
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            raise FXSourceError("unexpected FX payload shape")
        out = []
        for row in rows:
            try:
                out.append(
                    Rate(
                        date=dt.date.fromisoformat(row["date"]),
                        base=str(row["base"]).upper(),
                        quote=str(row["quote"]).upper(),
                        rate=Decimal(str(row["rate"])),
                    )
                )
            except (KeyError, ValueError, TypeError) as e:
                raise FXSourceError(f"bad FX row: {row!r}") from e
        return out

    def get_rate(self, base: str, quote: str, date: dt.date | None = None) -> Rate:
        base, quote = base.upper(), quote.upper()
        if base == quote:
            return Rate(date or dt.date.today(), base, quote, Decimal(1))
        params: dict[str, Any] = {"base": base, "quotes": quote}
        if date:
            params["date"] = date.isoformat()
        rates = [r for r in self._parse(self._get("/rates", params)) if r.quote == quote]
        if not rates:
            raise FXSourceError(f"no rate for {base}/{quote} on {date}")
        return max(rates, key=lambda r: r.date)

    def get_rates_range(
        self, base: str, quotes: list[str], start: dt.date, end: dt.date
    ) -> list[Rate]:
        if start > end:
            raise ValueError("start must be <= end")
        quotes = [q.upper() for q in quotes if q.upper() != base.upper()]
        if not quotes:
            return []
        params = {
            "base": base.upper(),
            "quotes": ",".join(quotes),
            "from": start.isoformat(),
            "to": end.isoformat(),
        }
        return self._parse(self._get("/rates", params))

    def list_supported_currencies(self) -> list[str]:
        data = self._get("/currencies", {})
        if isinstance(data, list):
            return sorted({str(c.get("iso_code", "")).upper() for c in data if c.get("iso_code")})
        if isinstance(data, dict):
            return sorted(k.upper() for k in data)
        raise FXSourceError("unexpected currencies payload")

    def close(self) -> None:
        self._client.close()
