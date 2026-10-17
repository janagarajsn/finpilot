"""FinPilot FX MCP server (FastMCP, streamable HTTP at /mcp, health at /health).

Tools: get_rate, get_rates_range, convert, list_supported_currencies.
Source adapter: `finpilot.fx.provider.FrankfurterProvider` (same module the app's
direct-HTTP fallback uses). Amounts and rates travel as strings to preserve Decimal precision.
"""

from __future__ import annotations

import datetime as dt
import os
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from finpilot.fx.provider import DEFAULT_BASE_URL, DEFAULT_PROVIDER, FrankfurterProvider

SUPPORTED = tuple(
    c.strip().upper() for c in os.getenv("FX_SUPPORTED", "INR,USD").split(",") if c.strip()
)


def _date(value: str | None) -> dt.date | None:
    return dt.date.fromisoformat(value) if value else None


def _check(*codes: str) -> None:
    bad = [c for c in codes if c.upper() not in SUPPORTED]
    if bad:
        raise ValueError(f"unsupported currency {bad}; supported: {list(SUPPORTED)}")


def build_server(provider: FrankfurterProvider | None = None) -> FastMCP:
    src = provider or FrankfurterProvider(
        base_url=os.getenv("FX_SOURCE_BASE_URL", DEFAULT_BASE_URL),
        provider=os.getenv("FX_PROVIDER", DEFAULT_PROVIDER) or None,
    )
    mcp = FastMCP("finpilot-fx", instructions="Daily reference FX rates (Frankfurter). Read-only.")

    @mcp.tool
    def get_rate(base: str, quote: str, date: str | None = None) -> dict:
        """Rate as units of `quote` per 1 `base` for `date` (YYYY-MM-DD; default latest).
        The returned `date` is the actual rate date (previous business day on weekends)."""
        _check(base, quote)
        return src.get_rate(base, quote, _date(date)).as_dict()

    @mcp.tool
    def get_rates_range(base: str, quotes: list[str], start: str, end: str) -> dict:
        """All available daily rates from `base` to each of `quotes` between start and end."""
        _check(base, *quotes)
        rows = src.get_rates_range(
            base, quotes, dt.date.fromisoformat(start), dt.date.fromisoformat(end)
        )
        return {"rates": [r.as_dict() for r in rows]}

    @mcp.tool
    def convert(amount: str, from_currency: str, to_currency: str, date: str | None = None) -> dict:
        """Convert `amount` (decimal string) at the reference rate for `date`; 2dp, half-even."""
        _check(from_currency, to_currency)
        try:
            amt = Decimal(amount)
        except InvalidOperation as e:
            raise ValueError("amount must be a decimal string") from e
        r = src.get_rate(from_currency, to_currency, _date(date))
        converted = (amt * r.rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
        return {"amount": str(amt), "converted": str(converted), **r.as_dict()}

    @mcp.tool
    def list_supported_currencies() -> dict:
        """Currencies this server will convert between."""
        return {"currencies": list(SUPPORTED)}

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "supported": list(SUPPORTED)})

    return mcp


def main() -> None:
    build_server().run(
        transport="http",
        host=os.getenv("FX_MCP_HOST", "0.0.0.0"),  # noqa: S104 — container bind
        port=int(os.getenv("FX_MCP_PORT", "8765")),
    )


if __name__ == "__main__":
    main()
