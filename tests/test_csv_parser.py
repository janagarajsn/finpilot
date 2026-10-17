"""Pure parser tests (no DB)."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest

from finpilot.ingest import csv_parser as P

CTX = P.ParseContext(
    category_index={"groceries": uuid.uuid4()},
    account_index={"main": (uuid.uuid4(), "INR")},
    supported_currencies={"INR", "USD"},
    reporting_currency="INR",
    today=dt.date(2026, 10, 15),
)


@pytest.mark.parametrize(
    "raw, expected, hint",
    [
        ("1,25,000.50", Decimal("125000.50"), 0),
        ("125,000.50", Decimal("125000.50"), 0),
        ("₹ 1,234", Decimal("1234"), 0),
        ("Rs. 450.00", Decimal("450.00"), 0),
        ("$6.45", Decimal("6.45"), 0),
        ("(1,200.00)", Decimal("-1200.00"), 0),
        ("-99", Decimal("-99"), 0),
        ("250.00 Dr", Decimal("250.00"), -1),
        ("1000 CR", Decimal("1000"), 1),
        ("45-", Decimal("-45"), 0),
        ("abc", None, 0),
        ("", None, 0),
    ],
)
def test_parse_amount(raw, expected, hint):
    assert P.parse_amount(raw) == (expected, hint)


def test_decimal_comma():
    assert P.looks_decimal_comma(["1.234,56", "12,50", "3,00"])
    assert not P.looks_decimal_comma(["1,234.56", "12.50"])
    assert P.parse_amount("1.234,56", decimal_comma=True)[0] == Decimal("1234.56")


def test_date_format_detection_and_ambiguity():
    assert P.detect_date_formats(["2024-01-05", "2024-12-31"]) == ["%Y-%m-%d"]
    amb = P.detect_date_formats(["05/01/2024", "06/02/2024"])
    assert "%d/%m/%Y" in amb and "%m/%d/%Y" in amb and P.is_ambiguous(amb)
    resolved = P.detect_date_formats(["05/01/2024", "25/02/2024"])
    assert resolved == ["%d/%m/%Y"] and not P.is_ambiguous(resolved)


def test_read_csv_semicolon_latin1_with_preamble():
    content = (
        "Account statement for XXXX1234\nGenerated 2024-02-01\n"
        "Txn Date;Narration;Withdrawal Amt;Deposit Amt\n"
        "05/01/2024;Café Coffee Day;250,00;\n"
    ).encode("latin-1")
    t = P.read_table(content, "stmt.csv")
    assert t.delimiter == ";" and t.headers[1] == "Narration" and t.rows[0][1] == "Café Coffee Day"
    m = P.guess_mapping(t.headers)
    assert (m["date"], m["description"], m["debit"], m["credit"]) == (0, 1, 2, 3)


def test_limits():
    with pytest.raises(P.ImportFileError, match="5 MB"):
        P.read_table(b"x" * (P.MAX_BYTES + 1))
    with pytest.raises(P.ImportFileError, match="empty"):
        P.read_table(b"   ")


def _rows(lines, mapping=None, options=None):
    t = P.read_table("\n".join(lines).encode())
    return P.parse_rows(t, mapping or P.guess_mapping(t.headers), options or {}, CTX)


def test_template_rows_parse():
    rows = _rows(
        [
            "date,description,amount,type,category,account,notes,currency,fx_rate",
            "2026-03-01,SALARY,85000,income,,Main,,,",
            "2026-03-05,BIGBASKET,1200.50,expense,Groceries,,,INR,",
            "2026-03-07,OPENAI,20,expense,,,,USD,84.10",
        ]
    )
    assert all(r.data for r in rows), [r.errors for r in rows]
    assert rows[0].data["kind"] == "income" and rows[0].data["currency"] == "INR"
    assert rows[1].data["category_id"] == CTX.category_index["groceries"]
    assert rows[2].data["fx_rate"] == Decimal("84.10")


def test_row_errors_are_specific():
    rows = _rows(
        [
            "date,description,amount,type,category,currency",
            "2026-13-01,X,10,expense,,",
            "2026-03-01,,10,expense,,",
            "2026-03-01,Y,ten,expense,,",
            "2026-03-01,Z,10,spend-ish,,",
            "2026-03-01,W,10,expense,Nonexistent,",
            "2026-03-01,V,10,expense,,EUR",
            "2026-03-01,U,0,expense,,",
        ]
    )
    msgs = [" ".join(r.errors) for r in rows]
    assert "Unrecognized date" in msgs[0] and "Description is missing" in msgs[1]
    assert "not a number" in msgs[2] and "must be expense or income" in msgs[3]
    assert "does not exist" in msgs[4] and "not supported" in msgs[5] and "zero" in msgs[6]
    assert not any(r.data for r in rows)


def test_signed_amounts_and_debit_credit_columns():
    rows = _rows(["Date,Description,Amount", "2026-03-01,Coffee,-4.50", "2026-03-02,Refund,10"])
    assert [r.data["kind"] for r in rows] == ["expense", "income"]
    flipped = _rows(
        ["Date,Description,Amount", "2026-03-01,Coffee,4.50"],
        options={"amount_sign": "positive_is_expense"},
    )
    assert flipped[0].data["kind"] == "expense"
    dc = _rows(
        [
            "Date,Narration,Debit,Credit",
            "2026-03-01,ATM,500,",
            "2026-03-02,Salary,,9000",
            "2026-03-03,Both,1,2",
        ]
    )
    assert (
        dc[0].data["kind"] == "expense"
        and dc[1].data["kind"] == "income"
        and "Both" in dc[2].errors[0]
    )


def test_mapping_validation():
    with pytest.raises(P.ImportFileError, match="date and description"):
        P.validate_mapping({"amount": 0}, 3)
    with pytest.raises(P.ImportFileError, match="only one field"):
        P.validate_mapping({"date": 0, "description": 0, "amount": 1}, 3)
    assert P.validate_mapping({"date": 0, "description": 1, "debit": 2}, 3)["debit"] == 2
