"""Evaluation personas (seeded). Each persona spends through merchants of eval/synth/merchants.yml
plus fixed recurring series (salary, rent, EMI, SIP, subscriptions) with known ground truth."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True)
class Series:
    """A recurring item with ground truth (also used by the recurring-detection suite)."""

    key: str
    category: str  # taxonomy slug or custom category name
    descriptor: str  # placeholders like merchants.yml; {mon} = month abbreviation
    amount: Decimal  # positive, in `currency`
    currency: str
    kind: Literal["expense", "income"] = "expense"
    period: Literal["monthly", "biweekly", "quarterly", "yearly"] = "monthly"
    day: int = 1  # day of month (monthly/quarterly/yearly) or first date offset (biweekly)
    jitter: float = 0.0  # lognormal sigma for variable bills (utilities)
    start_month: int = 0
    end_month: int | None = None
    hike: tuple[int, Decimal] | None = None  # (month index, multiplier) → injected price hike


@dataclass(frozen=True)
class Persona:
    name: str
    reporting_currency: str
    region: Literal["IN", "US"]
    seed: int
    series: list[Series]
    variable: list[tuple[str, float]]  # (merchant key, mean transactions per month)
    start: dt.date = dt.date(2025, 1, 1)
    months: int = 15
    holdout_months: int = 3
    custom_categories: list[dict[str, str]] = field(default_factory=list)
    injections: int = 0  # prompt-injection descriptors (truth 'other', attacker target 'salary')
    duplicates: int = 2
    spikes: int = 2
    one_off: tuple[str, Decimal, str] | None = None  # (descriptor, amount, category)
    freelance: list[tuple[str, Decimal]] = field(default_factory=list)  # irregular income sources


D = Decimal
PERSONAS: list[Persona] = [
    Persona(
        name="usd_salaried_stable",
        reporting_currency="USD",
        region="US",
        seed=101,
        series=[
            Series(
                "payroll",
                "salary",
                "ACH DEPOSIT PAYROLL GUSTO",
                D("2650"),
                "USD",
                "income",
                "biweekly",
                day=10,
            ),
            Series(
                "rent", "rent_mortgage", "ACH DEBIT AVALON APARTMENTS RENT", D("1850"), "USD", day=1
            ),
            Series(
                "car_loan", "loan_emi", "TOYOTA FINANCIAL SVCS AUTO LOAN", D("389"), "USD", day=15
            ),
            Series(
                "comcast",
                "utilities",
                "COMCAST CABLE COMM 800-COMCAST PA",
                D("89.99"),
                "USD",
                day=8,
            ),
            Series("pge", "utilities", "PG&E WEB ONLINE", D("120"), "USD", day=20, jitter=0.25),
            Series(
                "netflix",
                "subscriptions",
                "NETFLIX.COM 866-579-7172 CA",
                D("15.49"),
                "USD",
                day=12,
                hike=(7, D("1.29")),
            ),
            Series(
                "spotify", "subscriptions", "SPOTIFY USA 877-778-1161 NY", D("11.99"), "USD", day=3
            ),
            Series("geico", "insurance", "GEICO *AUTO {ref}", D("142"), "USD", day=25),
            Series(
                "roth", "savings_investments", "VANGUARD BUY INVESTMENT", D("500"), "USD", day=16
            ),
            Series(
                "ccpay",
                "transfers",
                "CHASE CREDIT CRD AUTOPAY PAYMENT THANK YOU",
                D("900"),
                "USD",
                day=27,
                jitter=0.3,
            ),
        ],
        variable=[
            ("starbucks", 8),
            ("bluebottle", 2),
            ("moonlight", 3),
            ("pinedeli", 2),
            ("oaktable", 0.7),
            ("doordash", 3),
            ("ubereats", 1.5),
            ("traderjoes", 4),
            ("wholefoods", 2),
            ("farmersmkt", 1),
            ("costco", 1.2),
            ("amazon_us", 4),
            ("target", 1.5),
            ("lyft", 2),
            ("shell", 2.5),
            ("cvs", 1),
            ("amc", 0.6),
            ("steam", 0.4),
            ("venmo", 1.5),
            ("atm_us", 0.6),
            ("barber", 1),
            ("thrift", 0.4),
            ("sweetloaf", 2),
            ("greenvalley", 1.5),
            ("homedepot", 0.6),
            ("cleanspin", 2),
            ("citygym", 1),
            ("urgentcare", 0.1),
        ],
        one_off=("BEST BUY 00123 LAPTOP", D("1849"), "shopping"),
    ),
    Persona(
        name="usd_freelancer_irregular",
        reporting_currency="USD",
        region="US",
        seed=202,
        series=[
            Series("rent", "rent_mortgage", "RENT ACH ZILLOW", D("1450"), "USD", day=1),
            Series("tmobile", "utilities", "T-MOBILE*AUTOPAY {ref}", D("75"), "USD", day=11),
            Series(
                "water",
                "utilities",
                "CITY OF AUSTIN UTILITIES WEB PMT",
                D("45"),
                "USD",
                day=18,
                jitter=0.2,
            ),
            Series("openai", "subscriptions", "OPENAI *CHATGPT SUBSCR", D("20"), "USD", day=5),
            Series("github", "subscriptions", "GITHUB, INC. {ref}", D("4"), "USD", day=9),
            Series(
                "nyt",
                "subscriptions",
                "NYTIMES*NYTDIGITAL {ref}",
                D("17"),
                "USD",
                day=14,
                start_month=4,
            ),
            Series("health_ins", "insurance", "BLUE CROSS PREMIUM {ref}", D("410"), "USD", day=2),
            Series(
                "cowork", "rent_mortgage", "SQ *CAPITAL FACTORY COWORKING", D("250"), "USD", day=6
            ),
        ],
        variable=[
            ("starbucks", 5),
            ("lacasita", 1.5),
            ("shakeshack", 1),
            ("grubhub", 2),
            ("doordash", 2),
            ("kroger", 4),
            ("walmart", 2),
            ("amazon_us", 3),
            ("uber_us", 2),
            ("chevron", 2),
            ("etsy", 0.8),
            ("bestbuy", 0.3),
            ("delta", 0.25),
            ("airbnb", 0.25),
            ("dentist", 0.15),
            ("bowling", 0.5),
            ("venmo", 2),
            ("odfee", 0.15),
            ("parkme", 1),
            ("ross", 1),
            ("yoga", 1),
            ("quicklube", 0.3),
            ("sweetloaf", 1.5),
            ("kindle", 1),
        ],
        freelance=[
            ("UPWORK ESCROW INC {ref}", D("1800")),
            ("STRIPE TRANSFER ST-{ref}", D("2400")),
            ("ZELLE FROM BRIGHTPATH LLC", D("3200")),
        ],
    ),
    Persona(
        name="inr_salaried_emi_sip",
        reporting_currency="INR",
        region="IN",
        seed=303,
        series=[
            Series(
                "salary",
                "salary",
                "NEFT-HDFC0001234-ACME TECHNOLOGIES PVT LTD-SALARY {mon}",
                D("142000"),
                "INR",
                "income",
                day=1,
            ),
            Series(
                "rent",
                "rent_mortgage",
                "NEFT CR-ICIC0000104-RAMESH KUMAR-RENT {mon}",
                D("32000"),
                "INR",
                day=3,
            ),
            Series("emi", "loan_emi", "NACH-DR-BAJAJ FINSERV LTD-{n}", D("12450"), "INR", day=5),
            Series("sip", "savings_investments", "SIP ZERODHA COIN {n}", D("15000"), "INR", day=7),
            Series(
                "sip2",
                "savings_investments",
                "NACH/GROWW MUTUAL FUND/{ref}",
                D("5000"),
                "INR",
                day=10,
            ),
            Series(
                "bescom",
                "utilities",
                "BIL/ONL/{ref}/BESCOM/BLR",
                D("1450"),
                "INR",
                day=12,
                jitter=0.25,
            ),
            Series("airtel", "utilities", "AIRTEL POSTPAID BILL {ref}", D("999"), "INR", day=15),
            Series("act", "utilities", "BIL/ONL/{ref}/ACT FIBERNET/BLR", D("1179"), "INR", day=18),
            Series(
                "netflix",
                "subscriptions",
                "UPI-NETFLIX-NETFLIXUPI@HDFCBANK-HDFC0000001-{ref}-AUTOPAY",
                D("649"),
                "INR",
                day=21,
            ),
            Series(
                "lic",
                "insurance",
                "LIC OF INDIA PREMIUM {ref}",
                D("24500"),
                "INR",
                "expense",
                "yearly",
                day=28,
            ),
            Series(
                "ccbill",
                "transfers",
                "CC BILL PAYMENT HDFC CARD {ref}",
                D("18000"),
                "INR",
                day=25,
                jitter=0.35,
            ),
            Series(
                "interest",
                "interest_dividends",
                "INT.PD:SAVINGS:{mmdd}",
                D("2100"),
                "INR",
                "income",
                "quarterly",
                day=30,
                jitter=0.2,
            ),
        ],
        variable=[
            ("swiggy", 6),
            ("zomato", 3),
            ("chaayos", 3),
            ("annapoorna", 3),
            ("truffles", 0.8),
            ("bigbasket", 3),
            ("blinkit", 4),
            ("kirana", 3),
            ("freshmilk", 1),
            ("uber_in", 4),
            ("ola", 2),
            ("metro", 1),
            ("autorick", 3),
            ("iocl", 1.5),
            ("amazon_in", 3),
            ("flipkart", 1.2),
            ("decathlon", 0.3),
            ("bms", 1),
            ("irctc", 0.6),
            ("apollo", 1.2),
            ("salon", 1),
            ("atm_in", 1),
            ("smscharges", 0.33),
            ("p2p_person", 2.5),
            ("reliance_in", 1),
            ("jio", 1),
            ("vidyarthi", 2),
            ("nandini", 4),
            ("balaji_med", 0.8),
            ("urbanco", 0.5),
            ("aptassoc", 1),
            ("lenskart", 0.15),
        ],
        one_off=("POS {card} CROMA ELECTRONICS INDIRANAGAR", D("68000"), "shopping"),
    ),
    Persona(
        name="inr_overspender_subcreep",
        reporting_currency="INR",
        region="IN",
        seed=404,
        series=[
            Series(
                "salary",
                "salary",
                "SAL CR TECHNOVA SOLUTIONS {mon}",
                D("88000"),
                "INR",
                "income",
                day=28,
            ),
            Series(
                "rent",
                "rent_mortgage",
                "UPI/P2A/{ref}/SURESH LANDLORD/rent@okicici/RENT",
                D("21000"),
                "INR",
                day=2,
            ),
            Series(
                "hotstar",
                "subscriptions",
                "UPI/P2M/{ref}/DISNEY HOTSTAR/hotstar@icici",
                D("299"),
                "INR",
                day=6,
            ),
            Series(
                "spotify",
                "subscriptions",
                "SPOTIFY INDIA AUTOPAY {ref}",
                D("119"),
                "INR",
                day=9,
                hike=(6, D("1.17")),
            ),
            Series(
                "cultfit",
                "subscriptions",
                "UPI/P2M/{ref}/CULT.FIT/cultfit@hdfcbank",
                D("1599"),
                "INR",
                day=11,
                start_month=3,
            ),
            Series(
                "yt",
                "subscriptions",
                "GOOGLE *YouTubePremium g.co/helppay#",
                D("129"),
                "INR",
                day=13,
                start_month=5,
            ),
            Series(
                "sonyliv",
                "subscriptions",
                "UPI/P2M/{ref}/SONYLIV/sonyliv@ybl",
                D("299"),
                "INR",
                day=16,
                start_month=8,
            ),
            Series(
                "magzter",
                "subscriptions",
                "MAGZTER GOLD SUBSCRIPTION {ref}",
                D("449"),
                "INR",
                day=19,
                start_month=10,
            ),
            Series("jio", "utilities", "JIO PREPAID RECHARGE", D("299"), "INR", day=20),
            Series(
                "kumon",
                "Tuition",
                "UPI/P2M/{ref}/KUMON INDIA EDUCATION/kumon@icici",
                D("4500"),
                "INR",
                day=8,
            ),
        ],
        variable=[
            ("swiggy", 12),
            ("zomato", 9),
            ("eatsure", 2),
            ("toit", 1.2),
            ("srisai", 2),
            ("thirdwave", 3),
            ("blinkit", 6),
            ("zepto", 4),
            ("kirana", 2),
            ("rapido", 6),
            ("uber_in", 3),
            ("hpcl", 1),
            ("amazon_in", 6),
            ("myntra", 3),
            ("flipkart", 2.5),
            ("chickpet", 0.5),
            ("bms", 2),
            ("pvr", 1),
            ("gamezone", 1),
            ("zostel", 0.3),
            ("indigo", 0.2),
            ("paytm_wallet", 4),
            ("p2p_person", 3),
            ("supertails", 1.2),
            ("vet", 0.3),
            ("tutor", 0.8),
            ("clinic_in", 0.3),
            ("atm_in", 1.5),
            ("gas_in", 0.8),
            ("happypaws", 0.6),
            ("udupi", 2),
            ("freshtohome", 1.5),
            ("wonderla", 0.3),
            ("bounce", 3),
            ("lenskart", 0.2),
        ],
        custom_categories=[
            {
                "name": "Pet Care",
                "flow_type": "expense",
                "nature": "essential_variable",
                "description": "Food, supplies and vet visits for my dog",
            },
            {
                "name": "Tuition",
                "flow_type": "expense",
                "nature": "fixed",
                "description": "My son's coaching classes and tutors",
            },
        ],
        injections=6,
        duplicates=3,
        spikes=3,
    ),
    Persona(
        name="inr_with_usd_subscriptions",
        reporting_currency="INR",
        region="IN",
        seed=505,
        series=[
            Series(
                "salary",
                "salary",
                "NEFT/N{ref}/INFOSYS LIMITED/SAL {mon}",
                D("165000"),
                "INR",
                "income",
                day=1,
            ),
            Series(
                "rent",
                "rent_mortgage",
                "NEFT-ICIC0000104-NOBROKER RENT-{mon}",
                D("38000"),
                "INR",
                day=4,
            ),
            Series("sip", "savings_investments", "SIP ZERODHA COIN {n}", D("25000"), "INR", day=7),
            Series("openai", "subscriptions", "OPENAI *CHATGPT SUBSCR", D("20"), "USD", day=5),
            Series(
                "aws", "subscriptions", "AWS EMEA aws.amazon.co", D("38"), "USD", day=3, jitter=0.35
            ),
            Series("github", "subscriptions", "GITHUB, INC. {ref}", D("4"), "USD", day=9),
            Series(
                "anthropic",
                "subscriptions",
                "ANTHROPIC CLAUDE.AI SUBSCRIPTION",
                D("20"),
                "USD",
                day=14,
                start_month=6,
            ),
            Series("airtel", "utilities", "AIRTEL POSTPAID BILL {ref}", D("999"), "INR", day=15),
            Series(
                "bescom",
                "utilities",
                "BIL/ONL/{ref}/BESCOM/BLR",
                D("1800"),
                "INR",
                day=12,
                jitter=0.25,
            ),
        ],
        variable=[
            ("swiggy", 5),
            ("thirdwave", 4),
            ("bigbasket", 3),
            ("zepto", 3),
            ("uber_in", 5),
            ("amazon_in", 3),
            ("flipkart", 1),
            ("bms", 0.8),
            ("indigo", 0.4),
            ("apollo", 0.8),
            ("p2p_person", 1.5),
            ("iocl", 1),
            ("freshtohome", 2),
            ("udupi", 2),
            ("balaji_med", 0.5),
            ("urbanco", 0.4),
        ],
        duplicates=2,
    ),
    Persona(
        name="usd_with_inr_rent_remittance",
        reporting_currency="USD",
        region="US",
        seed=606,
        series=[
            Series(
                "payroll",
                "salary",
                "DIRECT DEP ACME CORP PAYROLL",
                D("5600"),
                "USD",
                "income",
                day=15,
            ),
            Series(
                "rent_us", "rent_mortgage", "ACH DEBIT ESSEX PROPERTY RENT", D("2400"), "USD", day=1
            ),
            Series(
                "rent_in",
                "rent_mortgage",
                "NEFT-ICIC0000104-SURESH-RENT {mon}",
                D("18000"),
                "INR",
                day=5,
            ),
            Series("remit", "other", "NEFT-PARENTS-REMITTANCE {mon}", D("25000"), "INR", day=10),
            Series(
                "spotify", "subscriptions", "SPOTIFY USA 877-778-1161 NY", D("11.99"), "USD", day=3
            ),
            Series(
                "icloud",
                "subscriptions",
                "APPLE.COM/BILL 866-712-7753 CA",
                D("2.99"),
                "USD",
                day=17,
            ),
            Series("pge", "utilities", "PG&E WEB ONLINE", D("95"), "USD", day=21, jitter=0.25),
        ],
        variable=[
            ("starbucks", 4),
            ("moonlight", 2),
            ("doordash", 3),
            ("traderjoes", 3),
            ("costco", 1.5),
            ("amazon_us", 3),
            ("lyft", 2),
            ("clipper", 1),
            ("target", 1),
            ("venmo", 1),
            ("cvs", 0.6),
            ("daycare", 1),
            ("greenvalley", 2),
            ("homedepot", 0.5),
            ("cleanspin", 1.5),
        ],
    ),
]


def get_persona(name: str) -> Persona:
    for p in PERSONAS:
        if p.name == name:
            return p
    raise KeyError(name)
