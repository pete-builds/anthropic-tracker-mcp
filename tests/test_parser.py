"""Tests for the salary HTML parser (clients/parser.py).

Covers: structured pay-transparency extraction, regex fallback, graceful
handling of missing/empty input, entity-decoding, and currency detection. No
eval, no network — pure string parsing against fixed HTML snippets.
"""

from clients.parser import (
    detect_comp_type,
    normalize_currency,
    parse_compensation,
)

STRUCTURED_HTML = """
<div class="content-pay-transparency">
  <div class="pay-input">
    <div class="pay-range">
      <span>$290,000</span>
      <span class="divider">—</span>
      <span>$435,000</span>
    </div>
  </div>
  <p>Annual Salary: USD</p>
</div>
"""

REGEX_HTML = """
<div class="job-body">
  <p>The expected base salary range for this role is
     $315,000 — $560,000 USD.</p>
</div>
"""

OTE_HTML = """
<div class="content-pay-transparency">
  <div class="pay-range">
    <span>$200,000</span>
    <span class="divider">—</span>
    <span>$300,000</span>
  </div>
  <p>This figure represents on-target earnings (OTE).</p>
</div>
"""

GBP_HTML = """
<div class="job-body">
  <p>Salary range: £195,000 — £260,000 GBP per year.</p>
</div>
"""


def test_structured_pay_extracts_min_max_cents():
    out = parse_compensation(STRUCTURED_HTML)
    assert out is not None
    assert out["salary_min"] == 29000000   # $290,000 in cents
    assert out["salary_max"] == 43500000   # $435,000 in cents
    assert out["currency"] == "USD"
    assert out["comp_type"] == "annual"
    assert "raw_text" in out


def test_regex_fallback_extracts_range():
    out = parse_compensation(REGEX_HTML)
    assert out is not None
    assert out["salary_min"] == 31500000
    assert out["salary_max"] == 56000000
    assert out["currency"] == "USD"


def test_ote_detected():
    out = parse_compensation(OTE_HTML)
    assert out is not None
    assert out["comp_type"] == "ote"


def test_gbp_currency():
    out = parse_compensation(GBP_HTML)
    assert out is not None
    assert out["currency"] == "GBP"
    assert out["salary_min"] == 19500000
    assert out["salary_max"] == 26000000


def test_missing_compensation_returns_none():
    assert parse_compensation("<div>No salary listed here.</div>") is None
    assert parse_compensation("") is None
    assert parse_compensation(None) is None  # type: ignore[arg-type]


def test_entity_encoded_input_is_decoded():
    # Greenhouse per-job endpoint returns entity-encoded HTML; the parser must
    # unescape before handing to BeautifulSoup.
    encoded = (
        "&lt;div class=&quot;content-pay-transparency&quot;&gt;"
        "&lt;div class=&quot;pay-range&quot;&gt;"
        "&lt;span&gt;$100,000&lt;/span&gt;"
        "&lt;span class=&quot;divider&quot;&gt;—&lt;/span&gt;"
        "&lt;span&gt;$150,000&lt;/span&gt;"
        "&lt;/div&gt;&lt;/div&gt;"
    )
    out = parse_compensation(encoded)
    assert out is not None
    assert out["salary_min"] == 10000000
    assert out["salary_max"] == 15000000


def test_normalize_currency_european_format():
    # 120.000 with a 3-digit group is a thousands separator, not a decimal.
    amount, currency = normalize_currency("€120.000")
    assert amount == 12000000  # 120,000.00 in cents
    assert currency == "EUR"


def test_normalize_currency_garbage_returns_zero():
    amount, _currency = normalize_currency("not a salary")
    assert amount == 0


def test_detect_comp_type():
    assert detect_comp_type("total target compensation") == "ote"
    assert detect_comp_type("base annual salary") == "annual"
