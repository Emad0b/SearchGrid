from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
import re

import pdfplumber

MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}

DATE_RE = re.compile(r"^(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{2})\b")
AMOUNT_RE = re.compile(r"\b\d{1,3}(?:,\d{3})*\.\d{2}\b")
PERIOD_RE = re.compile(
    r"(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+to\s+(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})",
    re.I,
)
SUMMARY_MONEY_RE = re.compile(
    r"(OpeningBalance|Payments In|Payments Out|ClosingBalance)\s*[^\d]*([\d,]+\.\d{2})",
    re.I,
)
PAYMENT_TYPE_RE = re.compile(r"^(DD|VIS|ATM|CR|BP|SO|TFR|DR|CHQ)\b", re.I)
CARD_MARK_RE = re.compile(r"^\)+", re.I)

SKIP_SUBSTRINGS = (
    "contact tel",
    "text phone",
    "www.hsbc",
    "your statement",
    "account name",
    "sortcode",
    "sheet number",
    "your bank account details",
    "date payment type",
    "centenary square",
    "international bank account",
    "bank identifier code",
    "openingbalance",
    "payments in",
    "payments out",
    "closingbalance",
    "balancebroughtforward",
    "balancecarriedforward",
    "account summary",
    "financial services compensation",
    "information about the financial",
    "eligible for protection",
    "telephone banking",
)

CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("Cash & ATM", ("atm cash", "cash note", "notemac", "cash withdrawal")),
    (
        "Education & Fees",
        ("00000000000uea", "university", "tuition", "student finance"),
    ),
    (
        "Utilities",
        (
            "ee limited",
            "british gas",
            "edf energy",
            "octopus energy",
            "eon ",
            "thames water",
            "anglian water",
            "bt group",
            "virgin media",
            "sky digital",
            "council tax",
            "tv licence",
        ),
    ),
    (
        "Subscriptions",
        (
            "deliveroo plus",
            "spotify",
            "netflix",
            "disney+",
            "disney plus",
            "apple.com/bill",
            "google *youtube",
            "microsoft*xbox",
            "adobe",
            "amazon prime",
            "icloud",
        ),
    ),
    (
        "Transport",
        (
            "tfl travel",
            "transport for london",
            "first eastern",
            "trainline",
            "national rail",
            "uber trip",
            "bolt.eu",
            "parking",
            "tesco pfs",
            "pfs ",
            "shell ",
            "esso ",
            "petrol",
            "fuel",
        ),
    ),
    (
        "Groceries",
        (
            "aldi",
            "tesco",
            "sainsbury",
            "asda",
            "lidl",
            "iceland",
            "morrisons",
            "waitrose",
            "co-op",
            "coop ",
            "poundland",
            "farmfoods",
        ),
    ),
    (
        "Food & Drink",
        (
            "deliveroo",
            "uber eats",
            "just eat",
            "mcdonald",
            "kfc",
            "burger king",
            "subway",
            "starbucks",
            "costa",
            "cafe",
            "coffee",
            "restaurant",
            "sushi",
            "pizza",
            "uea catering",
            "sportscafe",
            "campus bar",
            "campus student",
            "ziggys",
            "box uea",
            "blend uea",
            "nvcs vending",
            "the shop kiosk",
            "brownbread",
            "orient express",
            "yo sushi",
            "mary jane",
            "union of uea",
        ),
    ),
    (
        "Shopping",
        (
            "amazon",
            "ebay",
            "argos",
            "ikea",
            "primark",
            "schuh",
            "hmv",
            "phone shop",
            "apple store",
            "currys",
            "boots",
            "superdrug",
            "tk maxx",
            "watch surgery",
            "imobile",
            "nirvana",
        ),
    ),
    (
        "Entertainment",
        (
            "steam",
            "playstation",
            "xbox",
            "nintendo",
            "circuit go",
            "cinema",
            "odeon",
            "ticketmaster",
            "uea.su",
        ),
    ),
    (
        "Transfers",
        ("global money", "faster payment", "mobile banking", "transfer to", "transfer from"),
    ),
    ("Fees", ("non-sterling", "transaction fee", "visa rate")),
]


@dataclass(slots=True)
class BankTransaction:
    txn_date: date
    payment_type: str
    description: str
    amount_out: float | None
    amount_in: float | None
    balance: float | None
    category: str
    source_file: str


@dataclass(slots=True)
class StatementSummary:
    source_file: str
    period_start: date | None
    period_end: date | None
    opening_balance: float | None
    payments_in: float | None
    payments_out: float | None
    closing_balance: float | None


def default_banking_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "Banking"


def list_statement_pdfs(banking_dir: Path | None = None) -> list[Path]:
    root = banking_dir or default_banking_dir()
    if not root.exists():
        return []
    return sorted(root.glob("*.pdf"))


def load_transactions(banking_dir: Path | None = None) -> tuple[list[BankTransaction], list[StatementSummary]]:
    pdfs = list_statement_pdfs(banking_dir)
    transactions: list[BankTransaction] = []
    summaries: list[StatementSummary] = []
    for pdf in pdfs:
        txns, summary = parse_statement_pdf(pdf)
        transactions.extend(txns)
        summaries.append(summary)
    transactions.sort(key=lambda item: (item.txn_date, item.description))
    return transactions, summaries


def parse_statement_pdf(pdf_path: Path) -> tuple[list[BankTransaction], StatementSummary]:
    text = _extract_text(pdf_path)
    summary = _parse_summary(pdf_path.name, text)
    transactions = _parse_transactions(pdf_path.name, text)
    return transactions, summary


def categorize(description: str, payment_type: str = "") -> str:
    haystack = f"{payment_type} {description}".lower()
    if payment_type.upper() == "ATM" or "atm cash" in haystack:
        return "Cash & ATM"
    for category, keywords in CATEGORY_RULES:
        if any(keyword in haystack for keyword in keywords):
            return category
    if payment_type.upper() in {"DD", "SO"}:
        return "Utilities"
    return "Other"


def _extract_text(pdf_path: Path) -> str:
    pages: list[str] = []
    with pdfplumber.open(pdf_path) as doc:
        for page in doc.pages:
            pages.append(page.extract_text() or "")
    return "\n".join(pages)


def _parse_summary(source_file: str, text: str) -> StatementSummary:
    values: dict[str, float] = {}
    for label, amount in SUMMARY_MONEY_RE.findall(text):
        key = label.lower().replace(" ", "")
        values[key] = _money(amount) or 0.0

    period_start = period_end = None
    match = PERIOD_RE.search(text)
    if match:
        d1, m1, d2, m2, year = match.groups()
        period_start = date(int(year), _full_month(m1), int(d1))
        period_end = date(int(year), _full_month(m2), int(d2))
        if period_end < period_start:
            period_end = date(int(year) + 1, _full_month(m2), int(d2))

    return StatementSummary(
        source_file=source_file,
        period_start=period_start,
        period_end=period_end,
        opening_balance=values.get("openingbalance"),
        payments_in=values.get("paymentsin"),
        payments_out=values.get("paymentsout"),
        closing_balance=values.get("closingbalance"),
    )


def _parse_transactions(source_file: str, text: str) -> list[BankTransaction]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    start = 0
    for idx, line in enumerate(lines):
        if "your bank account details" in line.lower():
            start = idx + 1
            break
    lines = lines[start:]

    completed: list[BankTransaction] = []
    current_date: date | None = None
    buffer_type = ""
    buffer_parts: list[str] = []

    def flush(amounts: list[float]) -> None:
        nonlocal buffer_type, buffer_parts
        if current_date is None or not amounts:
            buffer_type = ""
            buffer_parts = []
            return

        description = _clean_description(" ".join(buffer_parts))
        if not description or "balance" in description.lower().replace(" ", ""):
            buffer_type = ""
            buffer_parts = []
            return

        balance = None
        money_amounts = amounts
        if len(amounts) >= 2:
            # HSBC puts running balance after the transaction amount.
            money_amounts = amounts[:-1]
            balance = amounts[-1]

        payment_type = (buffer_type or "CARD").upper()
        is_credit = payment_type == "CR"

        for amount in money_amounts:
            completed.append(
                BankTransaction(
                    txn_date=current_date,
                    payment_type=payment_type,
                    description=description,
                    amount_out=None if is_credit else amount,
                    amount_in=amount if is_credit else None,
                    balance=balance,
                    category=categorize(description, payment_type),
                    source_file=source_file,
                )
            )

        buffer_type = ""
        buffer_parts = []

    for line in lines:
        lower = line.lower()
        if any(token in lower for token in SKIP_SUBSTRINGS):
            continue
        if re.fullmatch(r"\d+", line):
            continue

        date_match = DATE_RE.match(line)
        if date_match:
            day, mon, yy = date_match.groups()
            current_date = date(2000 + int(yy), MONTHS[mon], int(day))
            rest = line[date_match.end() :].strip()
            if not rest or "balance" in rest.lower().replace(" ", ""):
                continue
            buffer_type, rest = _split_payment_type(rest)
            amounts, desc = _split_trailing_amounts(rest)
            buffer_parts = [desc] if desc else []
            if amounts:
                flush(amounts)
            continue

        if current_date is None:
            continue

        if CARD_MARK_RE.match(line) or PAYMENT_TYPE_RE.match(line):
            buffer_type, rest = _split_payment_type(line)
            amounts, desc = _split_trailing_amounts(rest)
            buffer_parts = [desc] if desc else []
            if amounts:
                flush(amounts)
            continue

        amounts, desc = _split_trailing_amounts(line)
        if desc:
            if _is_fx_artifact(desc):
                if amounts and buffer_parts:
                    # Keep collecting the real GBP settlement line.
                    pass
                elif not buffer_parts:
                    continue
            else:
                buffer_parts.append(desc)
        if amounts:
            flush(amounts)

    return completed


def _split_payment_type(text: str) -> tuple[str, str]:
    cleaned = text.strip()
    card = CARD_MARK_RE.match(cleaned)
    if card:
        return "CARD", cleaned[card.end() :].strip()
    match = PAYMENT_TYPE_RE.match(cleaned)
    if match:
        return match.group(1).upper(), cleaned[match.end() :].strip()
    return "CARD", cleaned


def _split_trailing_amounts(text: str) -> tuple[list[float], str]:
    amounts = [float(value.replace(",", "")) for value in AMOUNT_RE.findall(text)]
    if not amounts:
        return [], text.strip()
    desc = text
    for _ in amounts:
        desc = re.sub(r"\s*\d{1,3}(?:,\d{3})*\.\d{2}\s*$", "", desc)
    return amounts, desc.strip(" .")


def _clean_description(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip(" .")
    cleaned = re.sub(r"^\)+\s*", "", cleaned)
    return cleaned


def _is_fx_artifact(text: str) -> bool:
    lower = text.lower()
    return bool(
        re.search(r"\bvisa rate\b", lower)
        or re.search(r"\b[a-z]{3}\s+\d+(?:\.\d+)?\s*@\s*\d", lower)
        or lower.startswith("usd ")
        or lower.startswith("eur ")
    )


def _money(value: str) -> float | None:
    try:
        return float(value.replace(",", "").replace("£", "").strip())
    except ValueError:
        return None


def _full_month(name: str) -> int:
    return {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }[name.lower()]
