"""Grounding guard: extraction (₹/Rs/INR/$/USD, lakh/crore, Indian grouping, %, dates), currency-aware
verification (converted figures only if they are facts), and the retry → fallback loop."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest
from pydantic import BaseModel

from finpilot.config import Settings
from finpilot.llm.client import FakeBackend, LLMClient
from finpilot.trail.grounding import FactSet, check, extract, guarded_json

D = Decimal


@pytest.mark.parametrize(
    "text, kind, value, currency",
    [
        ("₹1,25,000", "money", D("125000"), "INR"),
        ("Rs. 4,160.50", "money", D("4160.50"), "INR"),
        ("INR 80000", "money", D("80000"), "INR"),
        ("80,000 rupees", "money", D("80000"), "INR"),
        ("₹1.5 lakh", "money", D("150000"), "INR"),
        ("Rs 2 crore", "money", D("20000000"), "INR"),
        ("₹80k", "money", D("80000"), "INR"),
        ("$1,200.00", "money", D("1200.00"), "USD"),
        ("USD 20", "money", D("20"), "USD"),
        ("-$96.50", "money", D("-96.50"), "USD"),
        ("−₹12,300", "money", D("-12300"), "INR"),
        ("23.1%", "percent", D("23.1"), None),
        ("15 percent", "percent", D("15"), None),
        ("5.3×", "number", D("5.3"), None),
        ("3 installments", "number", D("3"), None),
        ("1.5 lakh", "number", D("150000"), None),
    ],
)
def test_extract_amounts(text, kind, value, currency):
    m = extract(text)
    assert len(m) == 1, m
    assert (m[0].kind, m[0].value, m[0].currency) == (kind, value, currency)


@pytest.mark.parametrize(
    "text, date",
    [
        ("on 2026-03-12", (2026, 3, 12)),
        ("on 12 Mar", (None, 3, 12)),
        ("by 12th March 2026", (2026, 3, 12)),
        ("on Mar 12, 2026", (2026, 3, 12)),
        ("in March 2026", (2026, 3, None)),
    ],
)
def test_extract_dates(text, date):
    m = extract(text)
    assert len(m) == 1 and m[0].date == date, m


def facts() -> FactSet:
    f = FactSet()
    f.add_money("1660.00", "INR").add_money("1881.00", "INR").add_money("20", "USD").add_money(
        "22", "USD"
    )
    f.add_money("125000", "INR").add_number(0.023).add_number(5.3).add_number(3).add_number(90)
    f.add_date(dt.date(2026, 3, 12)).add_date("2026-11-01")
    return f


@pytest.mark.parametrize(
    "text, ok",
    [
        ("Your OpenAI plan costs ₹1,881 now vs ₹1,660 at the first charge ($20 → $22).", True),
        ("That is ₹1,25,000 by 12 Mar.", True),
        ("Rounded: about ₹1.25 lakh.", True),  # 125000
        ("A 2.3% chance of going below zero, 5.3× the usual, 3 payments over 90 days.", True),
        ("Starts 1 Nov 2026.", True),
        ("It costs ₹1,700 at today's rate.", False),  # invented conversion
        ("$1,881 a month", False),  # right number, wrong currency: the guard is currency-aware
        ("Save ₹2,000 a month.", False),
        ("a 40% cut", False),
        ("on 13 Mar", False),
        ("in 120 days", False),
    ],
)
def test_verification_is_currency_and_tolerance_aware(text, ok):
    rep = check([text], facts())
    assert rep.passed is ok, rep.as_dict()


def test_tolerance_is_one_percent_or_one_unit():
    f = FactSet().add_money("78947.53", "INR").add_money("12.40", "USD")
    assert check(["₹78,948", "about ₹79,000", "$12"], f).passed  # within 1% / rounding
    assert not check(["₹80,000"], f).passed  # 1.3% off
    assert check(
        ["$13"], f
    ).passed  # 4.8% off but within the ±1 currency-unit tolerance (currencies.yml)
    assert not check(["$14"], f).passed


class Rec(BaseModel):
    title: str
    why: str


def test_guarded_json_retries_with_problems_then_passes():
    seen = []

    def reply(messages):
        seen.append(messages)
        if len(seen) == 1:
            return json.dumps({"title": "Cancel Netflix", "why": "Saves ₹999 a month."})  # invented
        return json.dumps({"title": "Cancel Netflix", "why": "Saves ₹649 a month."})

    llm = LLMClient(
        settings=Settings(mesh_api_key="x"), backend=FakeBackend({"system:hello": reply})
    )
    f = FactSet().add_money("649", "INR")
    out, res = guarded_json(
        llm,
        "system/hello",
        {"app_name": "x", "user_text": "y"},
        Rec,
        facts=f,
        texts=lambda r: [r.title, r.why],
        fallback=lambda: Rec(title="t", why="w"),
        agent="system",
    )
    assert (
        out.why == "Saves ₹649 a month."
        and res.attempts == 2
        and not res.first_attempt_pass
        and not res.fallback_used
    )
    assert "₹999" in seen[1][-1]["content"]  # the retry names the unverified number
    assert res.report.passed and res.as_dict()["retries"] == 1


def test_guarded_json_falls_back_after_two_retries_and_on_validation():
    llm = LLMClient(
        settings=Settings(mesh_api_key="x"),
        backend=FakeBackend(
            {"system:hello": lambda m: json.dumps({"title": "Do things", "why": "Save ₹5,000."})}
        ),
    )
    f = FactSet().add_money("649", "INR")
    out, res = guarded_json(
        llm,
        "system/hello",
        {"app_name": "x", "user_text": "y"},
        Rec,
        facts=f,
        texts=lambda r: [r.why],
        fallback=lambda: Rec(title="Cancel Netflix", why="Saves ₹649."),
        validate=lambda r: [] if "Netflix" in r.title else ["name the merchant"],
        agent="system",
    )
    assert res.fallback_used and res.attempts == 3 and out.title == "Cancel Netflix"
    assert res.problems[0] == ["unverified number: ₹5,000", "name the merchant"]


def test_guarded_json_degraded_without_mesh():
    llm = LLMClient(settings=Settings(mesh_api_key=None))
    out, res = guarded_json(
        llm,
        "system/hello",
        {"app_name": "x", "user_text": "y"},
        Rec,
        facts=FactSet(),
        texts=lambda r: [r.why],
        fallback=lambda: Rec(title="t", why="no numbers"),
        agent="system",
    )
    assert res.fallback_used and res.report.passed and out.why == "no numbers"


def test_add_obj_walks_typed_data():
    f = FactSet().add_obj(
        {
            "currency": "INR",
            "monthly": D("649.00"),
            "n": 3,
            "date": "2026-11-05",
            "fx": {"currency": "USD", "orig_amount": D("20")},
            "hike": {
                "orig_currency": "USD",
                "currency": "INR",
                "baseline": D("15.49"),
                "observed": D("19.98"),
                "annual_extra": D("4500"),
            },
        },
        "INR",
    )
    assert check(["₹649, 3 times, from 5 Nov 2026; $20; $15.49 → $19.98 (₹4,500/yr)"], f).passed
    assert not check(["₹15.49"], f).passed


def test_digits_inside_known_names_are_not_claims():
    f = FactSet().add_obj(
        {"merchant": "LYFT RIDE SAT 11PM", "charge": D("9.71"), "currency": "USD"}, "USD"
    )
    assert check(["Ask LYFT RIDE SAT 11PM about $9.71"], f).passed
    assert not check(["Ask LYFT RIDE about the 11 charges"], f).passed
