"""Guardrails: PII masking on the way out (Mesh + Langfuse), PII leak check on answers, and the
advice-scope rule (no specific investment / tax / legal directives)."""

from __future__ import annotations

import json

from finpilot.config import Settings
from finpilot.llm.client import FakeBackend, LLMClient
from finpilot.privacy import pii
from finpilot.trail.grounding import FactSet, check
from finpilot.trail.scope import violations


def test_identifiers_are_masked_and_figures_are_not():
    text = (
        "Paid card 4111 1111 1111 1111, a/c no 123456789012 (HDFC0001234), mail a.b@x.com, "
        "call +91 98765 43210, UPI swiggy@axisbank, PAN ABCPE1234F, Aadhaar 4991 1866 5246. "
        "Spent ₹1,25,000.00 on 2026-10-15; balance 10373403.94; ref UPI/P2M/412345678901."
    )
    out, counts = pii.mask(text)
    for secret in (
        "4111",
        "123456789012",
        "HDFC0001234",
        "a.b@x.com",
        "98765",
        "swiggy@axisbank",
        "ABCPE1234F",
        "4991",
    ):
        assert secret not in out, secret
    for kept in ("₹1,25,000.00", "2026-10-15", "10373403.94", "412345678901", "a/c no"):
        assert kept in out, kept
    assert set(counts) >= {
        "CREDIT_CARD",
        "BANK_ACCOUNT",
        "IN_IFSC",
        "EMAIL_ADDRESS",
        "PHONE_NUMBER",
        "UPI_ID",
        "IN_PAN",
        "IN_AADHAAR",
    }


def test_every_prompt_sent_to_mesh_is_masked():
    fake = FakeBackend(
        {
            "intent_router:classify": json.dumps(
                {"intent": "free_form", "period": None, "confidence": 0.9}
            )
        }
    )
    llm = LLMClient(settings=Settings(mesh_api_key="x"), backend=fake)
    llm.chat(
        "intent_router/classify",
        {"intents": "-", "question": "my card 4111111111111111 was charged twice"},
        agent="t",
        use_cache=False,
    )
    sent = json.dumps(fake.calls[-1]["messages"])
    assert "4111111111111111" not in sent and "<card number>" in sent


def test_masking_can_be_switched_off():
    fake = FakeBackend({"intent_router:classify": "{}"})
    llm = LLMClient(settings=Settings(mesh_api_key="x", pii_masking=False), backend=fake)
    llm.chat(
        "intent_router/classify",
        {"intents": "-", "question": "card 4111111111111111"},
        agent="t",
        use_cache=False,
    )
    assert "4111111111111111" in json.dumps(fake.calls[-1]["messages"])


def test_answers_that_leak_identifiers_fail_the_guard():
    facts = FactSet()
    assert check(["Your spending was steady."], facts).passed
    rep = check(["Your card 4111 1111 1111 1111 was charged."], facts)
    assert not rep.passed and rep.pii == ["CREDIT_CARD"]
    assert check(["Call me on +91 98765 43210"], facts).pii == ["PHONE_NUMBER"]


def test_advice_scope_flags_directives_and_passes_finpilot_levers():
    for bad, kind in [
        ("You should buy Reliance shares now.", "investment"),
        ("Put your money in bitcoin.", "investment"),
        ("Claim the 80C deduction on your SIP.", "tax"),
        ("File your ITR before July.", "tax"),
        ("You should sue the merchant.", "legal"),
    ]:
        assert [k for k, _ in violations(bad)] == [kind], bad
    for good in (
        "Cut your Food Delivery spending by 30% to save ₹5,000 a month.",
        "Cancel Netflix to save ₹649 a month.",
        "Raise a dispute with your bank for the duplicate charge.",
        "Your income after tax was ₹1,50,000.",
        "Gold jewellery spending was ₹40,000.",
        "Move ₹10,000 a month to savings to reach your goal by May.",
    ):
        assert violations(good) == [], good
