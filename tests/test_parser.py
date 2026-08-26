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


# --- plausibility: a benefit is not a salary ------------------------------
#
# The regex fallback searches the WHOLE description and used to take the first
# dollar range it found. Job posts are full of dollar ranges that are not
# salaries, and benefits sections routinely appear ABOVE the pay range.


def test_a_wellness_stipend_above_the_salary_is_not_returned_as_the_salary():
    """The exact shape that motivated this: benefit first, salary second."""
    html = """
    <div>
      <h3>Benefits</h3>
      <p>We offer a $500 - $2,000 annual wellness stipend.</p>
      <h3>Compensation</h3>
      <p>The base salary range for this role is $180,000 - $260,000 USD.</p>
    </div>
    """
    result = parse_compensation(html)
    assert result is not None
    assert result["salary_min"] == 18_000_000
    assert result["salary_max"] == 26_000_000


def test_a_signing_bonus_is_skipped_even_though_its_numbers_are_plausible():
    """The floor cannot catch this one. Only the surrounding words can."""
    html = """
    <div>
      <p>Signing bonus of $10,000 - $50,000 depending on experience.</p>
      <p>Base pay range: $200,000 - $300,000 USD.</p>
    </div>
    """
    result = parse_compensation(html)
    assert result["salary_min"] == 20_000_000


def test_an_unfamiliar_benefit_is_still_caught_by_the_floor():
    """New benefit vocabulary appears constantly; the word list is always behind.

    The floor is the backstop for exactly that, which is why both checks exist.
    """
    html = """
    <div>
      <p>Annual gizmo entitlement of $300 - $900.</p>
      <p>Salary: $150,000 - $190,000 USD.</p>
    </div>
    """
    result = parse_compensation(html)
    assert result["salary_min"] == 15_000_000


def test_an_hourly_rate_is_recognised_not_rejected_by_the_floor():
    """A legitimate small number. Rejecting it would trade one bug for another."""
    html = "<div><p>This role pays $28 - $42 per hour.</p></div>"
    result = parse_compensation(html)
    assert result is not None
    assert result["comp_type"] == "hourly"
    assert result["salary_min"] == 2_800
    assert result["salary_max"] == 4_200


def test_a_post_with_only_a_stipend_returns_nothing_rather_than_the_stipend():
    """Returning None is what a post with no pay data already returns.

    A missing salary is visibly missing. A stipend labelled as a salary is not,
    which is why guessing would be worse than declining.
    """
    html = "<div><p>Includes a $500 - $1,500 home office stipend.</p></div>"
    assert parse_compensation(html) is None


def test_a_plain_salary_with_no_benefits_section_is_unaffected():
    """The common case must not regress."""
    html = "<div><p>Compensation: $290,000 - $435,000 USD</p></div>"
    result = parse_compensation(html)
    assert result["salary_min"] == 29_000_000
    assert result["salary_max"] == 43_500_000
    assert result["comp_type"] == "annual"


def test_ote_wording_near_the_range_still_wins():
    """Comp type is read from the window around the match, not the document.

    A post mentioning OTE about a different role should not relabel this one.
    """
    html = "<div><p>On-target earnings of $250,000 - $320,000 USD.</p></div>"
    assert parse_compensation(html)["comp_type"] == "ote"
