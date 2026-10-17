"""FX & MCP suite (AMENDMENT_1 suite 9). No network: the FX MCP server runs in-process with a stub
provider, and the fallback chain uses fakes for the MCP and HTTP routes.

- conversion correctness against a fixed reference table (eval/datasets/fx_reference.yml)
- round trip USD → INR → USD within 0.01 (INR has the finer unit, so this direction is exact
  to a cent; INR → USD → INR can only be exact to one cent × rate)
- weekend / holiday / future date resolution
- fallback chain: all up → mcp; MCP down → fallback_api; both down with an old row → stale_db
  (stale flag); everything down → pinned (approximate flag)
- MCP contract: the server exposes exactly the allow-listed tools, convert/rates/currencies shapes,
  unsupported currency is a tool error that does not trip the breaker
- allowlist enforced before any I/O; circuit breaker opens, half-opens and recovers
- grounding of converted numbers (a converted amount passes only in the right currency)
- fx_exposure correctness on the two cross-currency personas vs ground truth from the generator

The fallback cases write January-2001 rows to fx_rates/fx_fetch_log (no real rate is that old)
and delete them afterwards, so the dev database and its FX banner are left as they were.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import time
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any, cast

import yaml
from sqlalchemy import text

from eval import loader
from eval.suites.base import Case, Metric, SuiteContext, SuiteResult
from eval.synth.generator import generate
from eval.synth.personas import get_persona
from finpilot.db.session import get_engine, user_session
from finpilot.fx.provider import FXSourceError, Rate
from finpilot.fx.service import FXService, pinned_rates
from finpilot.mcp.client import CircuitOpen, MCPCallFailed, MCPClient, MCPError, ToolNotAllowed
from finpilot.mcp.registry import MCPServerConfig
from finpilot.trail.grounding import FactSet, check
from mcp_servers.fx.server import build_server

SUITE = "fx"
DATA = Path(__file__).resolve().parents[1] / "datasets" / "fx_reference.yml"
ALLOWED = ["get_rate", "get_rates_range", "convert", "list_supported_currencies"]
EXPOSURE_PERSONAS = ("inr_with_usd_subscriptions", "usd_with_inr_rent_remittance")
CUTOFF = "2002-01-01"  # suite-owned fx rows are older than this


# ---------------------------------------------------------------------------- fakes
class FakeMCP:
    def __init__(self, rates: dict[dt.date, Decimal], fail: bool = False) -> None:
        self.rates, self.fail, self.calls = rates, fail, 0

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("fx-mcp down")
        s, e = dt.date.fromisoformat(args["start"]), dt.date.fromisoformat(args["end"])
        return {
            "rates": [
                {"date": d.isoformat(), "base": args["base"], "quote": "INR", "rate": str(r)}
                for d, r in sorted(self.rates.items())
                if s <= d <= e
            ]
        }


class FakeHTTP:
    def __init__(self, rates: dict[dt.date, Decimal], fail: bool = False) -> None:
        self.rates, self.fail = rates, fail

    def get_rates_range(self, base: str, quotes: Any, start: dt.date, end: dt.date) -> list[Rate]:
        if self.fail:
            raise FXSourceError("http down")
        return [Rate(d, base, "INR", r) for d, r in sorted(self.rates.items()) if start <= d <= end]


class StubProvider:
    def __init__(self) -> None:
        self.calls = 0

    def get_rate(self, base: str, quote: str, date: dt.date | None = None) -> Rate:
        self.calls += 1
        return Rate(date or dt.date(2001, 1, 5), base.upper(), quote.upper(), Decimal("46.75"))

    def get_rates_range(self, base: str, quotes: Any, start: dt.date, end: dt.date) -> list[Rate]:
        self.calls += 1
        return [Rate(start, base, q, Decimal("46.70")) for q in quotes]


class DictKV:
    def __init__(self) -> None:
        self.d: dict[str, Any] = {}

    def get(self, k: str) -> Any:
        return self.d.get(k)

    def set(self, k: str, v: Any, ex: int | None = None) -> None:
        self.d[k] = v


def _cfg(**kw: Any) -> MCPServerConfig:
    base = dict(
        name="fx",
        url="http://unused/mcp",
        timeout_s=3,
        retries=1,
        breaker_failures=2,
        breaker_reset_s=0.3,
        cache_ttl_s=60,
        allowed_tools=ALLOWED,
    )
    return MCPServerConfig(**{**base, **kw})


# ---------------------------------------------------------------------------- helpers
def _server(provider: Any) -> Any:
    """The FX MCP server around a stub provider (duck-typed like FrankfurterProvider)."""
    return build_server(provider)


def _wipe() -> None:
    with get_engine().begin() as c:
        c.execute(
            text("DELETE FROM fx_rates WHERE scope = 'real' AND rate_date < :d"), {"d": CUTOFF}
        )


class _Log:
    """Remember the fx_fetch_log high-water mark; delete suite rows afterwards."""

    def __enter__(self) -> _Log:
        with get_engine().connect() as c:
            self.start = int(
                c.execute(text("SELECT coalesce(max(id), 0) FROM fx_fetch_log")).scalar_one()
            )
        return self

    def __exit__(self, *exc: Any) -> None:
        with get_engine().begin() as c:
            c.execute(text("DELETE FROM fx_fetch_log WHERE id > :i"), {"i": self.start})
        _wipe()


def _q(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), ROUND_HALF_EVEN)


def run(ctx: SuiteContext) -> SuiteResult:
    res = SuiteResult(SUITE)
    data = yaml.safe_load(DATA.read_text(encoding="utf-8"))
    rates = {dt.date.fromisoformat(k): Decimal(v) for k, v in data["rates"].items()}
    today = dt.date.fromisoformat(data["today"])
    m: dict[str, float] = {}
    details: dict[str, Any] = {}

    def svc(mcp: Any = None, http: Any = None) -> FXService:
        return FXService(get_engine(), mcp=mcp, http=http, today=lambda: today)

    def case(cid: str, inp: Any, exp: Any, act: Any, ok: bool, err: str) -> bool:
        res.cases.append(
            Case(cid, {"x": inp}, {"x": exp}, {"x": act}, bool(ok), error_type=None if ok else err)
        )
        return bool(ok)

    with _Log():
        # ------------------------------------------------ conversions (reference table)
        _wipe()
        s = svc(FakeMCP(rates))
        ok = 0
        for c in data["conversions"]:
            got = s.convert(
                Decimal(c["amount"]), c["from"], c["to"], dt.date.fromisoformat(c["date"])
            )
            ok += case(
                f"convert:{c['amount']} {c['from']}->{c['to']} {c['date']}",
                c,
                c["expected"],
                str(got.converted),
                str(got.converted) == c["expected"],
                "wrong_conversion",
            )
        m["conversion_accuracy"] = ok / len(data["conversions"])

        # ------------------------------------------------ round trip
        ok = 0
        for a in data["round_trip_amounts"]:
            d = dt.date(2001, 1, 5)
            inr = s.convert(Decimal(a), "USD", "INR", d).converted
            back = s.convert(inr, "INR", "USD", d).converted
            ok += case(
                f"roundtrip:{a}",
                a,
                a,
                str(back),
                abs(back - Decimal(a)) <= Decimal("0.01"),
                "round_trip",
            )
        m["round_trip_within_0_01"] = ok / len(data["round_trip_amounts"])

        # ------------------------------------------------ date resolution
        ok = 0
        for c in data["dates"]:
            r = s.rate_to_inr("USD", dt.date.fromisoformat(c["date"]))
            ok += case(
                f"date:{c['kind']}",
                c["date"],
                c["expect_rate_date"],
                r.rate_date.isoformat(),
                r.rate_date.isoformat() == c["expect_rate_date"],
                "date_resolution",
            )
        m["date_resolution"] = ok / len(data["dates"])

        # ------------------------------------------------ fallback chain
        target = dt.date(2001, 1, 9)
        scenarios: list[tuple[str, bool, str]] = []
        _wipe()
        r = svc(FakeMCP(rates), FakeHTTP(rates)).rate_to_inr("USD", target)
        scenarios.append(
            ("all_up", r.source == "mcp" and not r.stale and not r.approximate, r.source)
        )
        _wipe()
        r = svc(FakeMCP(rates, fail=True), FakeHTTP(rates)).rate_to_inr("USD", target)
        scenarios.append(
            (
                "mcp_down",
                r.source == "fallback_api" and r.rate_inr_per_unit == rates[target],
                str(r.source),
            )
        )
        _wipe()
        with get_engine().begin() as c:
            c.execute(
                text("""INSERT INTO fx_rates (rate_date, quote_currency, rate_inr_per_unit, source, fetched_at)
                              VALUES ('2000-12-20', 'USD', 46.40, 'mcp', now())""")
            )
        r = svc(FakeMCP(rates, fail=True), FakeHTTP(rates, fail=True)).rate_to_inr("USD", target)
        scenarios.append(
            ("mcp_and_http_down", r.source == "stale_db" and r.stale, f"{r.source} stale={r.stale}")
        )
        _wipe()
        r = svc(FakeMCP(rates, fail=True), FakeHTTP(rates, fail=True)).rate_to_inr("USD", target)
        pinned = pinned_rates()[1]["USD"]
        scenarios.append(
            (
                "everything_down",
                r.source == "pinned" and r.approximate and r.rate_inr_per_unit == pinned,
                f"{r.source} approximate={r.approximate}",
            )
        )
        for name, good, seen in scenarios:
            case(f"fallback:{name}", name, "expected source + flag", seen, good, "fallback_chain")
        m["fallback_chain"] = sum(g for _, g, _ in scenarios) / len(scenarios)
        details["fallback"] = {n: seen for n, _, seen in scenarios}

    # ---------------------------------------------------- MCP contract (in-process FastMCP)
    import mcp as mcp_sdk

    stub = StubProvider()
    server = _server(stub)

    async def tools() -> list[str]:
        async with mcp_sdk.Client(server._mcp_server) as c:
            return sorted(t.name for t in (await c.list_tools()).tools)

    client = MCPClient(_cfg(), cache=DictKV(), target=server._mcp_server)
    checks: list[tuple[str, bool, Any]] = []
    listed = asyncio.run(tools())
    checks.append(("tools_match_allowlist", listed == sorted(ALLOWED), listed))
    conv = client.call(
        "convert",
        {"amount": "20.00", "from_currency": "USD", "to_currency": "INR", "date": "2001-01-05"},
    )
    checks.append(
        (
            "convert_shape",
            conv
            == {
                "amount": "20.00",
                "converted": "935.00",
                "date": "2001-01-05",
                "base": "USD",
                "quote": "INR",
                "rate": "46.75",
            },
            conv,
        )
    )
    rng = client.call(
        "get_rates_range",
        {"base": "USD", "quotes": ["INR"], "start": "2001-01-02", "end": "2001-01-03"},
    )
    checks.append(("rates_range_shape", rng["rates"][0]["rate"] == "46.70", rng["rates"][:1]))
    checks.append(
        (
            "currencies",
            client.call("list_supported_currencies")["currencies"] == ["INR", "USD"],
            None,
        )
    )
    try:
        client.call("get_rate", {"base": "EUR", "quote": "INR"})
        bad = False
    except MCPCallFailed:
        bad = client.breaker.state == "closed"
    checks.append(("unsupported_currency_is_tool_error", bad, client.breaker.state))
    before = stub.calls
    client.call(
        "convert",
        {"amount": "20.00", "from_currency": "USD", "to_currency": "INR", "date": "2001-01-05"},
    )
    checks.append(("results_cached", stub.calls == before, stub.calls - before))
    for name, good, got in checks:
        case(f"mcp:{name}", name, True, got, good, "mcp_contract")
    m["mcp_contract"] = sum(g for _, g, _ in checks) / len(checks)

    # ---------------------------------------------------- allowlist + circuit breaker
    narrow = MCPClient(_cfg(allowed_tools=["get_rate"]), target=_server(stub)._mcp_server)
    calls = stub.calls
    try:
        narrow.call("convert", {"amount": "1", "from_currency": "USD", "to_currency": "INR"})
        allow_ok = False
    except ToolNotAllowed:
        allow_ok = stub.calls == calls
    case(
        "mcp:allowlist_before_io",
        "convert not allowed",
        "ToolNotAllowed, no I/O",
        allow_ok,
        allow_ok,
        "allowlist",
    )
    m["allowlist_enforced"] = float(allow_ok)

    br = MCPClient(_cfg(retries=0, timeout_s=0.5, cache_ttl_s=0), target="http://127.0.0.1:9/mcp")
    states = []
    for _ in range(2):
        with contextlib.suppress(MCPError):
            br.call("get_rate", {"base": "USD", "quote": "INR"})
    states.append(br.breaker.state)
    try:
        br.call("get_rate", {"base": "USD", "quote": "INR"})
        fast_fail = False
    except CircuitOpen:
        fast_fail = True
    time.sleep(0.35)
    states.append(br.breaker.state)
    br._target = _server(StubProvider())._mcp_server  # the server is back
    recovered = br.call("get_rate", {"base": "USD", "quote": "INR"})["rate"] == "46.75"
    states.append(br.breaker.state)
    cb_ok = states == ["open", "half_open", "closed"] and fast_fail and recovered
    case(
        "mcp:circuit_breaker",
        "2 failures, wait, recover",
        ["open", "half_open", "closed"],
        states,
        cb_ok,
        "breaker",
    )
    m["circuit_breaker"] = float(cb_ok)

    # ---------------------------------------------------- grounding of converted numbers
    from finpilot.agents.chat_tools import ToolContext, convert_currency

    with _Log():  # the facts are exactly what the chat agent's convert_currency tool returns
        _wipe()
        tool_out = convert_currency(
            # convert_currency only uses the FX service: no DB session or user needed
            ToolContext(cast(Any, None), cast(Any, None), None, svc(FakeMCP(rates)), today, "INR"),
            {"amount": 20, "from": "USD", "to": "INR", "date": "2001-01-05"},
        )
    facts = FactSet().add_obj(tool_out, "USD")
    details["grounding_tool_output"] = {k: str(v) for k, v in tool_out.items()}
    good_pass = bad_caught = n_good = n_bad = 0
    for g in data["grounding"]:
        passed = check([g["text"]], facts).passed
        if g["grounded"]:
            n_good += 1
            good_pass += passed
        else:
            n_bad += 1
            bad_caught += not passed
        case(
            f"grounding:{g['text'][:40]}",
            g["text"],
            g["grounded"],
            passed,
            passed == g["grounded"],
            "grounding",
        )
    m["grounding_converted_detection"] = bad_caught / max(n_bad, 1)
    m["grounding_converted_clean_pass"] = good_pass / max(n_good, 1)

    # ---------------------------------------------------- fx_exposure vs generator truth
    from finpilot.agents.analyzer import Analyzer
    from finpilot.categories.taxonomy import load_taxonomy
    from finpilot.insights.fx_exposure import WINDOW_DAYS

    exp_ok = []
    try:
        for name in EXPOSURE_PERSONAS:
            ds = generate(get_persona(name))
            ld = loader.load(ds, ctx.run_tag)
            loader.apply_truth_categories(
                ds, ld
            )  # true categories: measure fx_exposure, not the categorizer
            as_of = max(t.date for t in ds.history())  # the loader stores the history split
            with user_session(ld.user_id) as s_:
                rep = Analyzer().run(s_, ld.user_id, as_of=as_of)
            exposure: dict[str, Any] = rep.fx_exposure or {}
            flows: dict[str, str] = {e.slug: str(e.flow_type) for e in load_taxonomy()}
            flows |= {
                c["name"]: str(c.get("flow_type", "expense")) for c in ds.persona.custom_categories
            }
            truth = _exposure_truth(ds, as_of, WINDOW_DAYS, flows)
            share_ok = abs(float(exposure.get("share_pct", -1)) - truth["share_pct"]) <= 0.5
            spend_ok = abs(
                Decimal(str(exposure.get("foreign_spend", 0))) - truth["foreign_spend"]
            ) <= truth["foreign_spend"] * Decimal("0.01")
            exp_ok.append(share_ok and spend_ok)
            case(
                f"fx_exposure:{name}",
                name,
                {k: str(v) for k, v in truth.items()},
                {
                    "share_pct": exposure.get("share_pct"),
                    "foreign_spend": str(exposure.get("foreign_spend")),
                },
                share_ok and spend_ok,
                "fx_exposure",
            )
    finally:
        loader.cleanup(ctx.run_tag)
    m["fx_exposure_correct"] = sum(exp_ok) / max(len(exp_ok), 1)

    for k, v in m.items():
        res.metrics.append(Metric(k, float(v), "ratio", {"scope": "overall", "primary": True}))
    res.details = details | {"cases": len(res.cases)}
    return res


def _exposure_truth(ds: Any, as_of: dt.date, window: int, flows: dict[str, str]) -> dict[str, Any]:
    """Foreign-currency share of spend over the last `window` days, from the generator's own data
    and FX series (independent of the database and the analyzer)."""
    rep = ds.persona.reporting_currency
    start = as_of - dt.timedelta(days=window - 1)
    fx = dict(sorted(ds.fx.items()))
    dates = list(fx)

    def inr_per_usd(d: dt.date) -> Decimal:
        prev = [x for x in dates if x <= d]
        return fx[prev[-1]] if prev else fx[dates[0]]

    def to_rep(amount: Decimal, cur: str, d: dt.date) -> Decimal:
        if cur == rep:
            return amount
        return amount * inr_per_usd(d) if rep == "INR" else amount / inr_per_usd(d)

    total = foreign = Decimal(0)
    for t in ds.history():
        if not (start <= t.date <= as_of) or t.amount >= 0:
            continue
        if flows.get(t.category, "expense") in ("transfer", "savings"):
            continue
        v = to_rep(-t.amount, t.currency, t.date)
        total += v
        if t.currency != rep:
            foreign += v
    return {
        "share_pct": round(float(foreign / total * 100), 1) if total else 0.0,
        "foreign_spend": _q(foreign),
    }
