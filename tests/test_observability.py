from __future__ import annotations

from finpilot.observability import langfuse as lf
from finpilot.observability.masking import hash_user_id, make_mask, mask_text


def test_langfuse_noop_when_unconfigured():
    lf._client = None
    assert lf.langfuse_enabled() is False
    lf.init_langfuse()
    with lf.trace_context(user_id="u", session_id="s", tags=["agent:x"], metadata={"k": 1}):
        lf.score_current_trace("cache_hit", 1)
    lf.flush()  # must not raise


def test_mask_text_redacts_pii():
    s = mask_text("mail a.b@example.com card 4111111111111111 call +91 98765 43210")
    assert "example.com" not in s and "4111111111111111" not in s and "98765" not in s
    assert "<email>" in s and "<card number>" in s and "<phone>" in s


def test_mask_hashes_bodies_when_capture_off():
    m = make_mask(False)
    out = m({"messages": [{"content": "secret salary 123"}]})
    assert "secret" not in str(out) and "sha256" in str(out)
    assert make_mask(True)("plain") == "plain"


def test_user_ids_are_hashed_stably():
    assert hash_user_id("abc") == hash_user_id("abc") != hash_user_id("abd")
    assert "abc" not in hash_user_id("abc")


def test_mask_keeps_ordinary_numbers():
    s = mask_text("spent 1,25,000 on 2024-01-05 and $12.50 (3 items)")
    assert s == "spent 1,25,000 on 2024-01-05 and $12.50 (3 items)"
    assert "<phone>" in mask_text("call (415) 555-0132 now")
