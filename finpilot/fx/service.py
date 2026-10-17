"""FX resolution: rate lookup, conversion, backfill, status. Deterministic, Decimal-only.

Resolution for (currency, date):
  1. stored `fx_rates` row for the nearest previous date (weekends/holidays) if it is
     close enough to the requested date → reuse it (historical rates never change);
  2. otherwise fetch ONE range [date-7d, date] via the chain
       FX MCP server → direct HTTP to the same source
     and store every returned row;
  3. if both routes fail: latest stored row at or before the date → `stale_db`
     (flagged stale if older than FX_STALE_AFTER_DAYS);
  4. else pinned `config/fx_fallback.yml` → `pinned`, flagged approximate.
Every route attempt is written to `fx_fetch_log`. Future dates use the latest rate.
The LLM never converts or invents rates; it only receives the results of this module.
"""

from __future__ import annotations

import datetime as dt
import itertools
import logging
import time
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from functools import lru_cache
from typing import Any, Literal

import yaml
from sqlalchemy import text
from sqlalchemy.engine import Engine

from finpilot.config import Settings, get_settings
from finpilot.fx.provider import FrankfurterProvider, FXSourceError, Rate
from finpilot.money import get_currency_registry, quantize

log = logging.getLogger(__name__)

FxSource = Literal["base", "mcp", "fallback_api", "stale_db", "pinned", "user_override"]
RATE_Q = Decimal("0.00000001")
HISTORICAL_GAP_DAYS = 4  # a stored rate within this many days before the date is "the" rate
FETCH_WINDOW_DAYS = 7


class FXUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolvedRate:
    currency: str
    rate_inr_per_unit: Decimal
    rate_date: dt.date
    source: FxSource
    stale: bool = False
    approximate: bool = False

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["rate_inr_per_unit"] = str(self.rate_inr_per_unit)
        d["rate_date"] = self.rate_date.isoformat()
        return d


@dataclass(frozen=True)
class Conversion:
    amount: Decimal
    from_currency: str
    to_currency: str
    converted: Decimal
    rate: Decimal  # units of `to` per 1 `from`
    rate_date: dt.date
    source: FxSource
    stale: bool
    approximate: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "amount": str(self.amount),
            "from": self.from_currency,
            "to": self.to_currency,
            "converted": str(self.converted),
            "rate": str(self.rate),
            "rate_date": self.rate_date.isoformat(),
            "source": self.source,
            "stale": self.stale,
            "approximate": self.approximate,
        }


@dataclass(frozen=True)
class FXStatus:
    as_of: dt.date | None
    source: str | None
    stale: bool
    approximate: bool
    live: bool

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "as_of": self.as_of.isoformat() if self.as_of else None}


@lru_cache
def weekend_only(a: dt.date, b: dt.date) -> bool:
    """True if every day after `a` up to and including `b` is a Saturday or Sunday."""
    d = a + dt.timedelta(days=1)
    while d <= b:
        if d.weekday() < 5:
            return False
        d += dt.timedelta(days=1)
    return True


def pinned_rates() -> tuple[dt.date, dict[str, Decimal]]:
    raw = yaml.safe_load((get_settings().config_dir / "fx_fallback.yml").read_text())
    return (
        dt.date.fromisoformat(str(raw["as_of"])),
        {k.upper(): Decimal(str(v)) for k, v in raw["rates_inr_per_unit"].items()},
    )


class FXService:
    def __init__(
        self,
        engine: Engine,
        *,
        mcp: Any | None = None,  # finpilot.mcp.client.MCPClient-like: .call(tool, args)
        http: FrankfurterProvider | None = None,
        settings: Settings | None = None,
        today: Any = None,  # callable returning date (tests)
    ) -> None:
        self.engine = engine
        self.mcp = mcp
        self.http = http
        self.settings = settings or get_settings()
        self._today = today or dt.date.today
        self.base = get_currency_registry().base

    # ------------------------------------------------------------------ storage
    def _log(self, route: str, ok: bool, t0: float, error: str | None = None) -> None:
        try:
            with self.engine.begin() as c:
                c.execute(
                    text(
                        "INSERT INTO fx_fetch_log (route, ok, latency_ms, error) "
                        "VALUES (:r, :ok, :ms, :e)"
                    ),
                    {
                        "r": route,
                        "ok": ok,
                        "ms": int((time.perf_counter() - t0) * 1000),
                        "e": error,
                    },
                )
        except Exception:
            log.warning("fx_fetch_log insert failed", exc_info=True)

    def _store(self, rates: list[Rate], source: str) -> int:
        rows = [
            {
                "d": r.date,
                "q": r.base,
                "rate": r.rate.quantize(RATE_Q, ROUND_HALF_EVEN),
                "s": source,
            }
            for r in rates
            if r.quote == self.base and r.base != self.base and r.rate > 0
        ]
        if not rows:
            return 0
        with self.engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO fx_rates (rate_date, quote_currency, rate_inr_per_unit, source) "
                    "VALUES (:d, :q, :rate, :s) ON CONFLICT (scope, rate_date, quote_currency) DO UPDATE "
                    "SET rate_inr_per_unit = EXCLUDED.rate_inr_per_unit, source = EXCLUDED.source, "
                    "fetched_at = now()"
                ),
                rows,
            )
        return len(rows)

    def _stored(
        self, currency: str, on: dt.date
    ) -> tuple[dt.date, Decimal, str, dt.datetime] | None:
        with self.engine.connect() as c:
            row = c.execute(
                text(
                    "SELECT rate_date, rate_inr_per_unit, source, fetched_at FROM fx_rates "
                    "WHERE scope = 'real' AND quote_currency = :q AND rate_date <= :d "
                    "ORDER BY rate_date DESC LIMIT 1"
                ),
                {"q": currency, "d": on},
            ).first()
        return (row[0], Decimal(row[1]), row[2], row[3]) if row else None

    def _known_gap(self, currency: str, rate_date: dt.date, target: dt.date) -> bool:
        """Is `rate_date` really the rate for `target`? Yes if same day, if only weekend days lie
        between them, or if a later stored rate brackets the target (the days in between were
        fetched and had no rate: holidays; a fetch covers FETCH_WINDOW_DAYS, so the bracket must be
        that close). A plain earlier row is not enough: the business days after it may simply not
        have been fetched yet."""
        if rate_date == target or weekend_only(rate_date, target):
            return True
        with self.engine.connect() as c:
            return (
                c.execute(
                    text(
                        "SELECT 1 FROM fx_rates WHERE scope = 'real' AND quote_currency = :q "
                        "AND rate_date > :d AND rate_date <= :e LIMIT 1"
                    ),
                    {
                        "q": currency,
                        "d": target,
                        "e": target + dt.timedelta(days=FETCH_WINDOW_DAYS),
                    },
                ).first()
                is not None
            )

    # ------------------------------------------------------------------ fetch chain
    def _fetch_range(self, currency: str, start: dt.date, end: dt.date) -> int | None:
        """One range call through MCP → HTTP. Returns rows stored, or None if both failed."""
        if self.mcp is not None:
            t0 = time.perf_counter()
            try:
                out = self.mcp.call(
                    "get_rates_range",
                    {
                        "base": currency,
                        "quotes": [self.base],
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                    },
                )
                rates = FrankfurterProvider._parse(
                    out.get("rates", [])
                )  # validates untrusted output
                self._log("mcp", True, t0)
                return self._store(rates, "mcp")
            except Exception as e:
                self._log("mcp", False, t0, f"{type(e).__name__}: {e}"[:300])
        if self.http is not None:
            t0 = time.perf_counter()
            try:
                rates = self.http.get_rates_range(currency, [self.base], start, end)
                self._log("http", True, t0)
                return self._store(rates, "fallback_api")
            except (FXSourceError, Exception) as e:
                self._log("http", False, t0, f"{type(e).__name__}: {e}"[:300])
        return None

    @staticmethod
    def _src(stored_source: str) -> FxSource:
        return "fallback_api" if stored_source == "fallback_api" else "mcp"

    # ------------------------------------------------------------------ public API
    def rate_to_inr(self, currency: str, on: dt.date | None = None) -> ResolvedRate:
        currency = get_currency_registry().get(currency).code
        today = self._today()
        if currency == self.base:
            return ResolvedRate(currency, Decimal(1), on or today, "base")
        target = min(on or today, today)  # future-dated items use the latest rate
        latest_mode = target >= today

        hit = self._stored(currency, target)
        if hit:
            rate_date, rate, src, fetched_at = hit
            gap = (target - rate_date).days
            fresh_latest = rate_date >= today - dt.timedelta(
                days=1
            ) or fetched_at >= dt.datetime.now(dt.UTC) - dt.timedelta(hours=12)
            if (
                not latest_mode
                and gap <= HISTORICAL_GAP_DAYS
                and self._known_gap(currency, rate_date, target)
            ) or (latest_mode and fresh_latest and gap <= HISTORICAL_GAP_DAYS):
                return ResolvedRate(currency, rate, rate_date, self._src(src))

        stored = self._fetch_range(currency, target - dt.timedelta(days=FETCH_WINDOW_DAYS), target)
        if stored is not None:
            hit = self._stored(currency, target)
            if hit and (target - hit[0]).days <= FETCH_WINDOW_DAYS:
                return ResolvedRate(currency, hit[1], hit[0], self._src(hit[2]))

        t0 = time.perf_counter()
        hit = self._stored(currency, target)
        if hit:
            self._log("db", True, t0)
            stale = (target - hit[0]).days > self.settings.fx_stale_after_days
            return ResolvedRate(currency, hit[1], hit[0], "stale_db", stale=stale)
        self._log("db", False, t0, "no stored rate")

        t0 = time.perf_counter()
        as_of, pinned = pinned_rates()
        if currency in pinned:
            self._log("pinned", True, t0)
            return ResolvedRate(currency, pinned[currency], as_of, "pinned", approximate=True)
        self._log("pinned", False, t0, f"no pinned rate for {currency}")
        raise FXUnavailable(f"no FX rate available for {currency} on {target}")

    def convert(
        self, amount: Decimal, from_currency: str, to_currency: str, on: dt.date | None = None
    ) -> Conversion:
        reg = get_currency_registry()
        f, t = reg.get(from_currency).code, reg.get(to_currency).code
        rf = self.rate_to_inr(f, on)
        rt = self.rate_to_inr(t, on)
        rate = (rf.rate_inr_per_unit / rt.rate_inr_per_unit).quantize(RATE_Q, ROUND_HALF_EVEN)
        # Convert through INR with full precision, round once at the end.
        converted = quantize(amount * rf.rate_inr_per_unit / rt.rate_inr_per_unit, t)
        worst = rf if rf.source != "base" else rt
        return Conversion(
            amount=amount,
            from_currency=f,
            to_currency=t,
            converted=converted,
            rate=rate,
            rate_date=min(rf.rate_date, rt.rate_date),
            source=worst.source,
            stale=rf.stale or rt.stale,
            approximate=rf.approximate or rt.approximate,
        )

    def backfill(self, currency: str, start: dt.date, end: dt.date) -> int:
        """Ensure rates exist for every date in [start, end] with ONE range call if needed."""
        currency = get_currency_registry().get(currency).code
        if currency == self.base:
            return 0
        end = min(end, self._today())
        if start > end:
            return 0
        with self.engine.connect() as c:
            dates = [
                r[0]
                for r in c.execute(
                    text(
                        "SELECT rate_date FROM fx_rates WHERE scope = 'real' AND quote_currency = :q "
                        "AND rate_date BETWEEN :s AND :e ORDER BY rate_date"
                    ),
                    {"q": currency, "s": start - dt.timedelta(days=HISTORICAL_GAP_DAYS), "e": end},
                )
            ]
        covered = bool(dates) and dates[0] <= start
        if covered:
            gaps = [(b - a).days for a, b in itertools.pairwise(dates)]
            # interior gaps were fetched (holidays); the right edge must be today-ish or weekend
            covered = all(g <= HISTORICAL_GAP_DAYS for g in gaps) and (
                weekend_only(dates[-1], end)
                if end < self._today() - dt.timedelta(days=1)
                else (end - dates[-1]).days <= 1
            )
        if covered:
            return 0
        return self._fetch_range(currency, start - dt.timedelta(days=FETCH_WINDOW_DAYS), end) or 0

    def probe_live(self) -> bool:
        """Force one live fetch (MCP → HTTP) of the last week for every foreign currency, so the
        status recovers as soon as a live source is back (stored rates would answer otherwise)."""
        today = self._today()
        ok = True
        for code in get_currency_registry().codes:
            if code != self.base:
                ok = (
                    self._fetch_range(code, today - dt.timedelta(days=FETCH_WINDOW_DAYS), today)
                    is not None
                    and ok
                )
        return ok

    def status(self) -> FXStatus:
        """Freshness of the rates the app is currently using (footer / banner / digests)."""
        foreign = [c for c in get_currency_registry().codes if c != self.base]
        with self.engine.connect() as c:
            row = c.execute(
                text(
                    "SELECT rate_date, source FROM fx_rates WHERE scope = 'real' AND quote_currency = ANY(:q) "
                    "ORDER BY rate_date DESC, fetched_at DESC LIMIT 1"
                ),
                {"q": foreign},
            ).first()
            last = c.execute(
                text(
                    "SELECT route, ok FROM fx_fetch_log WHERE route IN ('mcp','http','pinned') "
                    "ORDER BY id DESC LIMIT 1"
                )
            ).first()
        approximate = bool(last and last[0] == "pinned" and last[1])
        if row is None:
            as_of, _ = pinned_rates()
            return FXStatus(as_of, "pinned", stale=True, approximate=True, live=False)
        stale = (self._today() - row[0]).days > self.settings.fx_stale_after_days
        if last is None:  # nothing fetched yet (e.g. right after a restore): judge by age only
            return FXStatus(row[0], row[1], stale=stale, approximate=False, live=not stale)
        # the source is the route that worked LAST (MCP down → "fallback_api"), not the source of
        # the newest stored row; when both live routes failed the app runs on stored rates
        source = (
            {"mcp": "mcp", "http": "fallback_api", "pinned": "pinned"}[last[0]]
            if last[1]
            else "stale_db"
        )
        live = bool(last[1] and last[0] in ("mcp", "http")) and not stale
        return FXStatus(row[0], source, stale=stale, approximate=approximate, live=live)


def build_fx_service(engine: Engine | None = None) -> FXService:
    from finpilot.db.session import get_engine
    from finpilot.mcp.client import get_mcp_client

    s = get_settings()
    try:
        from finpilot.redis_client import get_redis

        mcp = get_mcp_client("fx", cache=get_redis())
    except Exception:
        log.warning("FX MCP client unavailable; using HTTP fallback only", exc_info=True)
        mcp = None
    http = FrankfurterProvider(
        base_url=s.fx_source_base_url, provider=s.fx_provider or None, timeout_s=s.fx_http_timeout_s
    )
    return FXService(engine or get_engine(), mcp=mcp, http=http, settings=s)
