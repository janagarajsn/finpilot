"""Builds the committed sample datasets in data/sample/ (deterministic: same output every run).

Usage: uv run python -m scripts.build_sample_data

Three households, 2026-04-01 → 2026-10-15 (six full months + half of the seventh), plain
descriptions ("Swiggy order", "Electricity bill"), every row categorized, and a few planted events
so Insights has something to show: a duplicate charge, a large first-time purchase, a
subscription price rise, a bill spike and a month of heavy food delivery.

The CSVs use the import template's columns, so they can also be uploaded on the Import page.
The app loads them with "Load sample data" (finpilot/ingest/sample_data.py), which moves the
dates forward by whole months so the last month lines up with the current month.
"""

from __future__ import annotations

import csv
import datetime as dt
import random
from collections.abc import Callable
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "sample"
START, END = dt.date(2026, 4, 1), dt.date(2026, 10, 15)
COLUMNS = [
    "date",
    "description",
    "amount",
    "type",
    "category",
    "account",
    "notes",
    "currency",
    "fx_rate",
]

Row = tuple[
    dt.date, str, float, str, str, str, str
]  # date, desc, amount, type, category, cur, notes


def days() -> list[dt.date]:
    return [START + dt.timedelta(n) for n in range((END - START).days + 1)]


def months() -> list[tuple[int, int]]:
    out, y, m = [], START.year, START.month
    while (y, m) <= (END.year, END.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


class Book:
    def __init__(self, seed: int, base: str) -> None:
        self.r = random.Random(seed)
        self.base = base
        self.rows: list[Row] = []

    def amt(self, lo: float, hi: float, cur: str | None = None) -> float:
        cur = cur or self.base
        x = self.r.uniform(lo, hi)
        return round(x) if cur == "INR" else round(x, 2)

    def add(
        self,
        d: dt.date,
        desc: str,
        amount: float,
        category: str,
        kind: str = "expense",
        cur: str | None = None,
        notes: str = "",
    ) -> None:
        if START <= d <= END:
            self.rows.append((d, desc, amount, kind, category, cur or self.base, notes))

    def monthly(
        self,
        day: int,
        desc: str,
        amount: Callable[[int, int], float] | float,
        category: str,
        kind: str = "expense",
        cur: str | None = None,
    ) -> None:
        for y, m in months():
            a = amount(y, m) if callable(amount) else amount
            self.add(dt.date(y, m, day), desc, a, category, kind, cur)

    def often(
        self,
        per_week: float,
        desc: str,
        lo: float,
        hi: float,
        category: str,
        weekend_boost: float = 1.0,
        cur: str | None = None,
        rate: Callable[[dt.date], float] | None = None,
    ) -> None:
        """Random days at about `per_week` occurrences a week (more at weekends if boosted)."""
        for d in days():
            p = per_week / 7 * (weekend_boost if d.weekday() >= 5 else 1.0)
            if rate:
                p *= rate(d)
            if self.r.random() < p:
                self.add(d, desc, self.amt(lo, hi, cur), category, cur=cur)

    def write(self, name: str) -> int:
        OUT.mkdir(parents=True, exist_ok=True)
        rows = sorted(self.rows, key=lambda r: (r[0], r[3] != "income", r[1]))
        with (OUT / name).open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(COLUMNS)
            for d, desc, a, kind, cat, cur, notes in rows:
                txt = f"{a:.0f}" if cur == "INR" else f"{a:.2f}"
                w.writerow([d.isoformat(), desc, txt, kind, cat, "", notes, cur, ""])
        return len(rows)


def inr_only() -> Book:
    """Asha, Bengaluru: salaried, rent, car loan, SIP. Everything in rupees."""
    b = Book(11, "INR")
    b.monthly(1, "Salary from Acme Technologies", 150000, "Salary", "income")
    for d in (dt.date(2026, 6, 30), dt.date(2026, 9, 30)):
        b.add(
            d,
            "Savings account interest",
            1850 if d.month == 6 else 1920,
            "Interest/Dividends",
            "income",
        )
    b.add(dt.date(2026, 8, 21), "Refund from Amazon", 1299, "Refunds", "income")
    b.monthly(3, "House rent", 28000, "Rent/Mortgage")
    b.monthly(5, "Mutual fund SIP", 10000, "Savings/Investments")
    b.monthly(10, "Car loan EMI", 14500, "Loan/EMI")
    b.monthly(2, "Gym membership", 1999, "Subscriptions")
    b.monthly(12, "Broadband bill", 999, "Utilities")
    b.monthly(15, "Mobile recharge", 599, "Utilities")
    b.monthly(18, "Netflix", lambda y, m: 799 if m >= 9 else 649, "Subscriptions")  # price rise
    b.monthly(20, "Spotify", 119, "Subscriptions")
    b.monthly(25, "Health insurance premium", 2400, "Insurance")
    b.monthly(
        7,
        "Electricity bill",
        lambda y, m: 4850 if m == 9 else b.amt(1450, 2250),  # September spike
        "Utilities",
    )
    b.often(1.0, "BigBasket groceries", 1200, 3200, "Groceries")
    b.often(2.0, "Vegetables and fruits", 250, 650, "Groceries")
    # food delivery: about 3 a week, heavier from mid-September (category spike)
    b.often(
        1.8,
        "Swiggy order",
        250,
        750,
        "Food Delivery",
        1.6,
        rate=lambda d: 2.4 if d >= dt.date(2026, 9, 10) else 1.0,
    )
    b.often(
        1.0,
        "Zomato order",
        280,
        700,
        "Food Delivery",
        1.4,
        rate=lambda d: 2.2 if d >= dt.date(2026, 9, 10) else 1.0,
    )
    b.often(1.5, "Coffee at Third Wave", 240, 420, "Dining Out")
    b.often(0.5, "Lunch with colleagues", 400, 900, "Dining Out")
    b.often(0.35, "Weekend dinner out", 1800, 3400, "Dining Out", 4.0)
    b.often(2.5, "Uber ride", 180, 520, "Transport")
    b.often(0.45, "Metro card recharge", 500, 500, "Transport")
    b.often(0.5, "Petrol", 2000, 2800, "Fuel")
    b.often(0.6, "Amazon order", 400, 1800, "Shopping")
    b.often(0.2, "Myntra clothes", 1200, 2900, "Shopping")
    b.often(0.25, "Pharmacy", 200, 900, "Health")
    b.often(0.4, "Movie tickets", 500, 900, "Entertainment", 3.0)
    b.often(0.25, "ATM cash withdrawal", 2000, 2000, "Cash/ATM")
    b.add(dt.date(2026, 5, 14), "Doctor consultation", 800, "Health")
    b.add(dt.date(2026, 8, 2), "Doctor consultation", 800, "Health")
    b.add(dt.date(2026, 6, 13), "Hotel stay in Coorg", 9800, "Travel")
    b.add(dt.date(2026, 6, 12), "Bus tickets to Coorg", 1650, "Travel")
    # planted events for Insights
    b.add(dt.date(2026, 8, 24), "New laptop from Croma", 72990, "Shopping")  # large first-time
    b.add(dt.date(2026, 9, 22), "Amazon order", 2349, "Shopping")  # duplicate charge …
    b.add(dt.date(2026, 9, 22), "Amazon order", 2349, "Shopping")  # … same day, same amount
    return b


def usd_only() -> Book:
    """Sam, Austin: paid twice a month, rent, car payment, brokerage savings. Everything in dollars."""
    b = Book(23, "USD")
    b.monthly(1, "Paycheck from Northwind", 3250, "Salary", "income")
    b.monthly(15, "Paycheck from Northwind", 3250, "Salary", "income")
    for d in (dt.date(2026, 6, 30), dt.date(2026, 9, 30)):
        b.add(
            d,
            "Savings account interest",
            18.4 if d.month == 6 else 21.15,
            "Interest/Dividends",
            "income",
        )
    b.add(dt.date(2026, 7, 9), "Refund from Target", 34.99, "Refunds", "income")
    b.monthly(1, "Apartment rent", 1850, "Rent/Mortgage")
    b.monthly(12, "Car payment", 385, "Loan/EMI")
    b.monthly(16, "Transfer to brokerage savings", 500, "Savings/Investments")
    b.monthly(3, "Gym membership", 45, "Subscriptions")
    b.monthly(8, "Internet bill", 70, "Utilities")
    b.monthly(22, "Phone bill", 55, "Utilities")
    b.monthly(18, "Netflix", lambda y, m: 17.99 if m >= 9 else 15.49, "Subscriptions")  # price rise
    b.monthly(20, "Spotify", 11.99, "Subscriptions")
    b.monthly(20, "Car insurance", 132, "Insurance")
    b.monthly(
        6,
        "Electric bill",
        lambda y, m: 268.4 if m == 8 else b.amt(85, 135),  # August heat-wave spike
        "Utilities",
    )
    b.often(1.0, "Trader Joe's groceries", 45, 120, "Groceries")
    b.often(0.25, "Costco", 150, 260, "Groceries")
    b.often(3.0, "Starbucks", 5.25, 7.8, "Dining Out")
    b.often(1.0, "Chipotle lunch", 11, 15.5, "Dining Out")
    b.often(0.35, "Dinner out with friends", 45, 95, "Dining Out", 4.0)
    b.often(
        1.0,
        "DoorDash order",
        22,
        48,
        "Food Delivery",
        1.5,
        rate=lambda d: 2.5 if d >= dt.date(2026, 9, 10) else 1.0,
    )
    b.often(1.0, "Shell gas station", 35, 55, "Fuel")
    b.often(0.4, "Uber ride", 12, 30, "Transport")
    b.often(0.7, "Amazon order", 15, 45, "Shopping")
    b.often(0.4, "Target", 30, 48, "Shopping")
    b.often(0.3, "Movie tickets", 24, 32, "Entertainment", 3.0)
    b.often(0.25, "CVS pharmacy", 12, 40, "Health")
    b.add(dt.date(2026, 7, 17), "Flight to Chicago", 320, "Travel")
    b.add(dt.date(2026, 7, 19), "Hotel in Chicago", 410, "Travel")
    b.add(dt.date(2026, 6, 6), "Concert tickets", 160, "Entertainment")
    # planted events for Insights
    b.add(dt.date(2026, 8, 27), "New phone from Apple Store", 1099, "Shopping")  # large first-time
    b.add(dt.date(2026, 9, 26), "Target", 64.99, "Shopping")  # duplicate charge …
    b.add(dt.date(2026, 9, 26), "Target", 64.99, "Shopping")  # … same day, same amount
    return b


def inr_usd_mixed() -> Book:
    """Ravi, Pune: rupee salary plus a US freelance client paid in dollars, and dollar-billed
    software subscriptions. Reporting currency INR."""
    b = Book(37, "INR")
    b.monthly(1, "Salary from Bluebird Software", 95000, "Salary", "income")
    b.monthly(
        20,
        "Payment from US freelance client",
        lambda y, m: b.amt(550, 750, "USD"),
        "Freelance",
        "income",
        cur="USD",
    )
    b.add(dt.date(2026, 9, 30), "Savings account interest", 1240, "Interest/Dividends", "income")
    b.monthly(4, "House rent", 22000, "Rent/Mortgage")
    b.monthly(7, "Mutual fund SIP", 8000, "Savings/Investments")
    b.monthly(11, "Broadband bill", 799, "Utilities")
    b.monthly(16, "Mobile recharge", 449, "Utilities")
    b.monthly(19, "Netflix", 499, "Subscriptions")
    b.monthly(6, "ChatGPT Plus", 20, "Subscriptions", cur="USD")
    b.monthly(9, "GitHub Copilot", 10, "Subscriptions", cur="USD")
    b.monthly(
        14, "Figma subscription", lambda y, m: 20 if m >= 8 else 15, "Subscriptions", cur="USD"
    )
    b.monthly(3, "AWS cloud bill", lambda y, m: b.amt(18, 42, "USD"), "Utilities", cur="USD")
    b.monthly(
        8, "Electricity bill", lambda y, m: 3900 if m == 9 else b.amt(1100, 1800), "Utilities"
    )
    b.often(1.0, "BigBasket groceries", 1000, 2600, "Groceries")
    b.often(1.5, "Vegetables and fruits", 200, 550, "Groceries")
    b.often(1.5, "Swiggy order", 230, 650, "Food Delivery", 1.6)
    b.often(1.0, "Zomato order", 250, 600, "Food Delivery", 1.4)
    b.often(1.5, "Coffee at Starbucks", 280, 450, "Dining Out")
    b.often(0.3, "Weekend dinner out", 1500, 2900, "Dining Out", 4.0)
    b.often(2.0, "Ola ride", 150, 450, "Transport")
    b.often(0.45, "Petrol", 1500, 2200, "Fuel")
    b.often(0.5, "Amazon order", 350, 1600, "Shopping")
    b.often(0.3, "Movie tickets", 450, 850, "Entertainment", 3.0)
    b.often(0.25, "Pharmacy", 180, 800, "Health")
    b.add(dt.date(2026, 5, 20), "Online course on Udemy", 14.99, "Education", cur="USD")
    b.add(dt.date(2026, 7, 2), "Conference ticket", 299, "Education", cur="USD")
    b.add(dt.date(2026, 7, 24), "Hotel stay in Goa", 11200, "Travel")
    b.add(dt.date(2026, 7, 23), "Flight to Goa", 7400, "Travel")
    # planted events for Insights
    b.add(dt.date(2026, 9, 3), "Flight to Singapore", 38500, "Travel")  # large first-time
    b.add(dt.date(2026, 10, 4), "Hotel in Singapore", 540, "Travel", cur="USD")
    b.add(dt.date(2026, 8, 29), "Zomato order", 2486, "Food Delivery")  # duplicate charge …
    b.add(dt.date(2026, 8, 29), "Zomato order", 2486, "Food Delivery")  # … party order, twice
    return b


def main() -> None:
    for name, book in (
        ("inr_only.csv", inr_only()),
        ("usd_only.csv", usd_only()),
        ("inr_usd_mixed.csv", inr_usd_mixed()),
    ):
        print(f"{name}: {book.write(name)} rows")


if __name__ == "__main__":
    main()
