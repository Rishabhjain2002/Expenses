"""Ask Claude to categorise merchants the rule dictionary did not recognise.

This is layer 5 of the categoriser and it is entirely optional. It runs only on merchants
that layers 1-4 could not resolve, batches them into a handful of requests, and writes
every answer back to ``data/merchant_cache.json`` — so any given merchant costs one API
call once, and never again.

Only merchant name strings and their narrations are sent. No account number, no balance,
no name, no statement file. With no API key configured, everything degrades quietly:
unresolved transactions stay in the review queue instead of being guessed at.
"""

from __future__ import annotations

import os
from typing import Iterable

from pydantic import BaseModel, Field

from .categorize import CATEGORIES, UNCATEGORISED

# Change this one line to trade accuracy for cost. claude-haiku-4-5 is cheaper and
# usually fine for merchant naming; claude-opus-5 is the most accurate on the ambiguous
# Indian narrations that reach this layer at all.
MODEL = "claude-opus-5"

BATCH_SIZE = 40
MAX_BATCHES = 10  # safety valve: never fire more than this many requests in one load

_VALID = [c for c in CATEGORIES if c != UNCATEGORISED]

# Frozen so the cached prefix stays byte-stable across every request. Nothing volatile
# (no timestamps, no counts, no merchant names) may appear in here.
TAXONOMY_PROMPT = f"""You categorise transactions from Indian bank statements.

You will be given a list of merchants extracted from statement narrations. For each one,
return a clean display name and exactly one category from this list:

{chr(10).join('- ' + c for c in _VALID)}

Category guidance, specific to Indian statements:

- Food & Dining — restaurants, cafes, food delivery (Swiggy, Zomato), bars.
- Groceries — supermarkets and quick-commerce grocery (BigBasket, Blinkit, Zepto, DMart),
  milk and vegetable vendors.
- Transport & Fuel — cabs (Ola, Uber, Rapido), petrol pumps, FASTag, tolls, metro, buses.
- Shopping — e-commerce and retail: Amazon, Flipkart, Myntra, electronics, clothing.
- Bills & Utilities — electricity boards (BESCOM, MSEB, TNEB), water, piped gas and LPG,
  mobile and broadband (Airtel, Jio, ACT), DTH, municipal taxes.
- Rent & Housing — house rent, society maintenance, brokerage, packers and movers.
- Health & Medical — pharmacies, hospitals, clinics, diagnostic labs, gyms.
- Entertainment & Subscriptions — OTT (Netflix, Hotstar, Prime), music, gaming, cinemas.
- Travel — flights, trains (IRCTC), hotels, travel aggregators, visas and forex.
- Education — schools, colleges, coaching, ed-tech courses.
- Insurance — life, health, motor insurance premiums.
- Investments & SIP — mutual funds, SIPs, broking (Zerodha, Groww, Upstox), NPS, PPF,
  fixed and recurring deposits, gold bonds.
- Loan & EMI — loan repayments, EMIs, BNPL (Simpl, LazyPay, slice).
- Fees & Charges — bank charges, minimum-balance penalties, GST on charges, late fees.
- Cash Withdrawal — ATM withdrawals and cash handling.
- Transfers — movements that are NOT spending: transfers between the person's own
  accounts, credit-card bill payments (including CRED), and sweep-in/sweep-out.
- Income — salary, interest credited, dividends, refunds, reversals, cashback,
  reimbursements, rent received, maturity proceeds.

Rules that matter most:

1. A credit-card bill payment is Transfers, never Shopping. The individual purchases sit
   on the card statement; counting the bill too would double-count the spending.
2. A transfer between the person's own accounts is Transfers, not an expense.
3. A SIP or mutual fund debit is Investments & SIP, not Shopping — the money became an
   asset, it was not consumed.
4. A person's name (e.g. "Rahul Sharma") sent by IMPS or UPI is usually Transfers unless
   the narration says what it was for — "RENT" makes it Rent & Housing.
5. If you genuinely cannot tell what a merchant is, use your best category and set a low
   confidence. Do not invent a merchant you do not recognise.

For the display name, give the brand as a person would write it — "Swiggy", "HDFC Life",
"Indian Oil" — not the raw narration fragment. Confidence is 0.0 to 1.0: use above 0.9
only for merchants you actually recognise."""


class MerchantLabel(BaseModel):
    key: str = Field(description="The exact lookup key given in the input, echoed back.")
    display: str = Field(description="Clean brand name, e.g. 'Swiggy'.")
    category: str = Field(description="Exactly one category from the list.")
    confidence: float = Field(ge=0.0, le=1.0)


class LabelBatch(BaseModel):
    labels: list[MerchantLabel]


class LLMUnavailable(Exception):
    """Raised when Claude cannot be reached. Callers degrade instead of failing."""


def is_configured() -> bool:
    """True when an API key is present in the environment."""
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def get_client():
    """Build an Anthropic client, or raise LLMUnavailable."""
    try:
        import anthropic
    except ImportError as error:
        raise LLMUnavailable("The `anthropic` package is not installed.") from error

    try:
        return anthropic.Anthropic()
    except Exception as error:
        raise LLMUnavailable(f"Could not create an Anthropic client: {error}") from error


def _format_batch(items: list[dict]) -> str:
    lines = []
    for item in items:
        lines.append(
            f"key: {item['key']}\n"
            f"  extracted name: {item.get('merchant', '')}\n"
            f"  raw narration : {item.get('narration', '')}\n"
            f"  channel       : {item.get('channel', '')}\n"
            f"  direction     : {item.get('direction', 'debit')}"
        )
    return (
        "Categorise each merchant below. Return one label per key, echoing the key "
        "exactly as given.\n\n" + "\n\n".join(lines)
    )


def classify_merchants(items: Iterable[dict], client=None) -> dict[str, dict]:
    """Categorise unresolved merchants. Returns ``{key: {category, display, ...}}``.

    Never raises: any failure returns whatever was resolved so far, so an API outage
    degrades the app to rules-only rather than breaking the upload.
    """
    pending = [item for item in items if item.get("key")]
    if not pending:
        return {}

    if client is None:
        if not is_configured():
            return {}
        try:
            client = get_client()
        except LLMUnavailable:
            return {}

    resolved: dict[str, dict] = {}
    batches = [pending[i:i + BATCH_SIZE] for i in range(0, len(pending), BATCH_SIZE)]

    for batch in batches[:MAX_BATCHES]:
        try:
            response = client.messages.parse(
                model=MODEL,
                max_tokens=8000,
                output_config={"effort": "low"},
                system=[{
                    "type": "text",
                    "text": TAXONOMY_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": _format_batch(batch)}],
                output_format=LabelBatch,
            )
        except Exception as error:
            # Specific-first so the caller's message is useful, but never fatal.
            _log_api_failure(error)
            break

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            continue

        valid_keys = {item["key"] for item in batch}
        for label in parsed.labels:
            if label.key not in valid_keys or label.category not in _VALID:
                continue
            resolved[label.key] = {
                "category": label.category,
                "display": label.display,
                "confidence": float(label.confidence),
                "model": MODEL,
            }

    return resolved


def _log_api_failure(error: Exception) -> None:
    """Turn an SDK exception into one readable line. Diagnostics only — never raises."""
    try:
        import anthropic
    except ImportError:
        print(f"[bankcat] Claude unavailable: {error}")
        return

    if isinstance(error, anthropic.AuthenticationError):
        message = "invalid or missing ANTHROPIC_API_KEY"
    elif isinstance(error, anthropic.RateLimitError):
        message = "rate limited — try again shortly"
    elif isinstance(error, anthropic.APIConnectionError):
        message = "network unreachable"
    elif isinstance(error, anthropic.APIStatusError):
        message = f"API error {error.status_code}: {error.message}"
    else:
        message = str(error)
    print(f"[bankcat] Claude fallback skipped ({message}). Using rules only.")
