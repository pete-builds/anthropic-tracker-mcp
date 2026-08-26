"""Compensation parser for live Greenhouse job content.

Copied from pete-builds/anthropic-tracker src/anthropic_tracker/parser.py.
Schema-coupled: keep in sync if the upstream parser changes.

Only the compensation pieces are needed here. The DB-side `compensation_for`
tool reads pre-parsed values from the SQLite tables; the live tool runs this
parser against the raw HTML returned by Greenhouse so we don't have to wait
for the next nightly snapshot.

One small addition vs. upstream: `parse_compensation` html-unescapes the
input when it looks entity-encoded. Greenhouse's per-job endpoint
(`/jobs/{id}`) returns `&lt;div...` while the bulk endpoint with
`?content=true` returns raw HTML. The MCP live path uses the per-job URL,
so we decode defensively before passing to BeautifulSoup. Safe no-op when
input is already raw HTML.

Returned amounts are in CENTS (minor units). Callers that surface dollars
(like the `live_compensation` MCP tool) divide by 100.
"""

import html as _html
import re

from bs4 import BeautifulSoup

CURRENCY_SYMBOLS = {"$": "USD", "£": "GBP", "€": "EUR"}

# Matches patterns like "$290,000", "£195,000", "€120.000"
SALARY_PATTERN = re.compile(
    r"([£$€])\s*([\d,.\s]+)"
)

# Matches a full range like "$290,000—$435,000 USD"
SALARY_RANGE_PATTERN = re.compile(
    r"([£$€])\s*([\d,.]+)\s*[—–\-]\s*[£$€]?\s*([\d,.]+)\s*(USD|GBP|EUR|CAD|AUD|CHF|JPY|KRW|INR|SGD)?"
)

# --- Plausibility, for the regex fallback only --------------------------
#
# The fallback searches the WHOLE job description and used to take the first
# dollar range it found. Job posts are full of dollar ranges that are not
# salaries -- a wellness stipend, a referral bonus, an equipment allowance, a
# 401(k) match -- and benefits sections routinely appear ABOVE the pay range.
# So "$500 - $2,000 annual wellness stipend" was returned as the annual salary,
# with no signal that anything was wrong.
#
# Two independent checks, because neither alone is enough.
#
# CONTEXT catches the ones whose numbers look like a salary. A $10,000-$50,000
# signing bonus passes any floor you would dare set, and only the surrounding
# words give it away.
#
# A FLOOR catches the ones whose context is unfamiliar. New benefit vocabulary
# appears constantly and the word list will always be behind it, but a
# four-figure annual salary is implausible in every currency this parser
# accepts -- for JPY, KRW, or INR a real salary is orders of magnitude larger,
# so a single conservative floor does not need a per-currency table.
#
# What this deliberately does NOT do is guess. When no candidate survives, the
# parser returns None, which is what it already returns for a post with no pay
# data at all. A missing salary is visibly missing; a stipend labelled as a
# salary is not.

#: Words near a range that mean it is not the base pay for the job.
_NON_SALARY_CONTEXT = re.compile(
    r"\b(stipend|bonus|allowance|reimburse\w*|credit|discount|match(?:ing|es)?"
    r"|referral|relocation|equity|grant|award|budget|per diem|perk\w*"
    r"|subsid\w+|contribution|deductible|premium|copay|fee|tuition)\b",
    re.IGNORECASE,
)

#: Characters of surrounding text inspected for that vocabulary.
#:
#: This number is a real tradeoff and both directions cost something, so it was
#: set by measuring rather than guessed. Too WIDE and a benefits paragraph
#: poisons the salary that follows it: at 120 the wellness-stipend fixture
#: rejected the genuine $180k-$260k range 61 characters later, turning a
#: precision fix into a recall bug. Too NARROW and the noun a range belongs to
#: falls outside it, so "$500 - $2,000 annual wellness stipend" reads as pay.
#:
#: 48 clears both: the disqualifying noun sits within ~17 characters of its
#: range in the cases observed, while the nearest unrelated sentence is ~58
#: away. Tests pin both sides, so a later widening that reintroduces the recall
#: bug fails rather than passing quietly.
_CONTEXT_WINDOW = 48

#: Below this, in whole currency units, it is not an annual salary. Set low on
#: purpose: the job here is to reject four-figure stipends, not to encode a
#: view about what a role should pay.
_MIN_PLAUSIBLE_ANNUAL = 10_000

#: An hourly rate is a legitimate small number, so it must be recognised rather
#: than rejected by the floor above.
_HOURLY_RE = re.compile(r"\b(per hour|hourly|/\s*hr|/\s*hour|an hour)\b", re.IGNORECASE)

#: Bounds for one, in whole currency units.
_MIN_PLAUSIBLE_HOURLY = 5
_MAX_PLAUSIBLE_HOURLY = 2_000

# Keywords that indicate OTE (on-target earnings) vs base salary
OTE_KEYWORDS = [
    "on-target earnings",
    "on target earnings",
    "OTE",
    "total target compensation",
    "total compensation",
    "expected on-target",
]


def normalize_currency(text: str) -> tuple[int, str]:
    """Parse a salary string like '$290,000' into (29000000, 'USD').

    Returns amount in cents (minor units) and currency code.
    """
    text = text.strip()
    currency = "USD"

    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol in text:
            currency = code
            break

    # Also check for trailing currency code
    for code in ("USD", "GBP", "EUR", "CAD", "AUD", "CHF", "JPY", "KRW", "INR", "SGD"):
        if code in text:
            currency = code
            break

    # Extract numeric value: remove currency symbols, spaces, commas
    numeric = re.sub(r"[£$€a-zA-Z\s]", "", text)
    # Handle European format (dots as thousands sep): 120.000 -> 120000
    # If there's exactly one dot and three digits after, it's a thousands separator
    if re.match(r"^\d{1,3}\.\d{3}$", numeric):
        numeric = numeric.replace(".", "")
    else:
        numeric = numeric.replace(",", "")

    try:
        amount = int(float(numeric) * 100)
    except (ValueError, OverflowError):
        return 0, currency

    return amount, currency


def detect_comp_type(text: str) -> str:
    """Determine if compensation is base salary or OTE."""
    text_lower = text.lower()
    for keyword in OTE_KEYWORDS:
        if keyword.lower() in text_lower:
            return "ote"
    return "annual"


def parse_compensation(html_content: str) -> dict | None:
    """Extract salary range from job description HTML.

    Looks for structured pay-transparency markup first,
    falls back to regex pattern matching.

    Returns dict with salary_min, salary_max (in cents), currency, comp_type, raw_text.
    Returns None if no compensation data found.
    """
    if not html_content:
        return None

    # Greenhouse's per-job endpoint returns HTML-entity-encoded content
    # (`&lt;div&gt;...`) where the bulk endpoint returns raw HTML. Decode
    # once if we see the encoded sentinel — safe no-op if already decoded.
    if "&lt;" in html_content and "<" not in html_content[:200]:
        html_content = _html.unescape(html_content)

    soup = BeautifulSoup(html_content, "html.parser")

    # Strategy 1: structured pay-transparency div
    pay_div = soup.select_one("div.content-pay-transparency")
    if pay_div:
        return _parse_structured_pay(pay_div)

    # Strategy 2: regex fallback on full text
    text = soup.get_text(" ", strip=True)
    return _parse_salary_regex(text)


def _parse_structured_pay(pay_div) -> dict | None:
    """Parse salary from structured pay-transparency markup."""
    spans = pay_div.select("div.pay-range span")
    value_spans = [s for s in spans if "divider" not in (s.get("class") or [])]

    raw_text = pay_div.get_text(" ", strip=True)

    if len(value_spans) >= 2:
        min_text = value_spans[0].get_text(strip=True)
        max_text = value_spans[1].get_text(strip=True)

        salary_min, currency = normalize_currency(min_text)
        salary_max, _ = normalize_currency(max_text)

        if salary_min and salary_max:
            return {
                "salary_min": salary_min,
                "salary_max": salary_max,
                "currency": currency,
                "comp_type": detect_comp_type(raw_text),
                "raw_text": raw_text,
            }

    # Fall through to regex on the pay div text
    return _parse_salary_regex(raw_text)


def _is_plausible(salary_min_cents: int, comp_type: str) -> bool:
    """Could this range be the pay for a job, as opposed to a benefit?

    Only the low end is tested. A stipend's ceiling can be arbitrarily high and
    tells you nothing; its floor is what gives it away.
    """
    units = salary_min_cents / 100
    if comp_type == "hourly":
        return _MIN_PLAUSIBLE_HOURLY <= units <= _MAX_PLAUSIBLE_HOURLY
    return units >= _MIN_PLAUSIBLE_ANNUAL


def _parse_salary_regex(text: str) -> dict | None:
    """Extract a salary range, skipping ranges that are plainly not salaries.

    Walks every match rather than taking the first, because in a job post the
    first dollar range is very often a benefit. See the plausibility note above
    _NON_SALARY_CONTEXT for why both a context check and a floor are needed.
    """
    for match in SALARY_RANGE_PATTERN.finditer(text):
        symbol = match.group(1)
        min_str = match.group(2)
        max_str = match.group(3)
        explicit_currency = match.group(4)

        currency = explicit_currency or CURRENCY_SYMBOLS.get(symbol, "USD")
        salary_min, _ = normalize_currency(f"{symbol}{min_str}")
        salary_max, _ = normalize_currency(f"{symbol}{max_str}")

        if not salary_min or not salary_max:
            continue

        window = text[
            max(0, match.start() - _CONTEXT_WINDOW) : match.end() + _CONTEXT_WINDOW
        ]
        if _NON_SALARY_CONTEXT.search(window):
            continue

        # Comp type is read from the window, not the whole document: a post
        # that says "hourly" once about a different role should not relabel
        # this range, and OTE wording is likewise local to the range it
        # describes.
        comp_type = "hourly" if _HOURLY_RE.search(window) else detect_comp_type(window)
        if not _is_plausible(salary_min, comp_type):
            continue

        return {
            "salary_min": salary_min,
            "salary_max": salary_max,
            "currency": currency,
            "comp_type": comp_type,
            "raw_text": text[:500],
        }

    return None
