"""Ask Claude or OpenAI to categorise merchants the rule dictionary did not recognise.

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

PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI = "openai"
PROVIDER_DISPLAY = {PROVIDER_ANTHROPIC: "Claude", PROVIDER_OPENAI: "OpenAI"}

# Change this one line to trade accuracy for cost on the Anthropic path. claude-haiku-4-5
# is cheaper and usually fine for merchant naming; claude-opus-5 is the most accurate on
# the ambiguous Indian narrations that reach this layer at all.
MODEL = "claude-opus-5"

# OpenAI model for this layer, used only when OpenAI is the active provider. Override with
# OPENAI_MODEL if this default no longer matches what's available on your account.
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

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
    """Raised when the configured LLM provider cannot be reached. Callers degrade
    instead of failing."""


def active_provider() -> str | None:
    """Which provider a client-less call will use.

    An explicit BANKCAT_LLM_PROVIDER override wins outright. Otherwise Anthropic wins
    when both are configured, since it's bankcat's original/default provider; OpenAI is
    used when only its key is present. None when neither is configured.
    """
    override = os.environ.get("BANKCAT_LLM_PROVIDER", "").strip().lower()
    if override in PROVIDER_DISPLAY:
        return override
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return PROVIDER_ANTHROPIC
    if os.environ.get("OPENAI_API_KEY"):
        return PROVIDER_OPENAI
    return None


def is_configured() -> bool:
    """True when at least one supported provider's API key is present."""
    return active_provider() is not None


def active_model() -> str | None:
    """Model id a client-less call will use, or None if nothing is configured."""
    provider = active_provider()
    if provider == PROVIDER_ANTHROPIC:
        return MODEL
    if provider == PROVIDER_OPENAI:
        return OPENAI_MODEL
    return None


def provider_display_name(provider: str | None) -> str:
    """Human label for a provider key, for UI text."""
    return PROVIDER_DISPLAY.get(provider, "an AI provider")


def label_for_model(model: str) -> str:
    """Best-effort human label for a *stored* model id (e.g. from the merchant cache).

    Prefix-matched, not exact-matched against MODEL/OPENAI_MODEL, because a cache entry
    may have been written under a model value that has since changed.
    """
    if not model:
        return "AI"
    lowered = model.lower()
    if lowered.startswith("claude"):
        return "Claude"
    if lowered.startswith(("gpt", "chatgpt", "o1", "o3", "o4", "o5")):
        return "OpenAI"
    return "AI"


def get_client(provider: str | None = None):
    """Build a client for `provider` (or the active one), or raise LLMUnavailable."""
    provider = provider or active_provider()
    if provider == PROVIDER_OPENAI:
        return _get_openai_client()
    if provider == PROVIDER_ANTHROPIC:
        return _get_anthropic_client()
    raise LLMUnavailable("No LLM provider is configured.")


def _get_anthropic_client():
    try:
        import anthropic
    except ImportError as error:
        raise LLMUnavailable("The `anthropic` package is not installed.") from error
    try:
        return anthropic.Anthropic()
    except Exception as error:
        raise LLMUnavailable(f"Could not create an Anthropic client: {error}") from error


def _get_openai_client():
    try:
        import openai
    except ImportError as error:
        raise LLMUnavailable("The `openai` package is not installed.") from error
    try:
        return openai.OpenAI()
    except Exception as error:
        raise LLMUnavailable(f"Could not create an OpenAI client: {error}") from error


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


def classify_merchants(items: Iterable[dict], client=None,
                        provider: str | None = None) -> dict[str, dict]:
    """Categorise unresolved merchants. Returns ``{key: {category, display, ...}}``.

    Never raises: any failure returns whatever was resolved so far, so an API outage
    degrades the app to rules-only rather than breaking the upload.

    ``provider`` picks the call shape ("anthropic" or "openai"). When ``client`` is given
    directly and ``provider`` is not, it defaults to "anthropic" — the behaviour every
    existing caller relies on. When ``client`` is None, the active provider (see
    `active_provider`) is used and its client is built here.
    """
    pending = [item for item in items if item.get("key")]
    if not pending:
        return {}

    if client is None:
        provider = provider or active_provider()
        if provider is None:
            return {}
        try:
            client = get_client(provider)
        except LLMUnavailable:
            return {}
    elif provider is None:
        provider = PROVIDER_ANTHROPIC
    else:
        provider = provider.lower()

    classify_batch = _BATCH_CLASSIFIERS.get(provider)
    if classify_batch is None:
        return {}

    resolved: dict[str, dict] = {}
    batches = [pending[i:i + BATCH_SIZE] for i in range(0, len(pending), BATCH_SIZE)]

    for batch in batches[:MAX_BATCHES]:
        try:
            resolved.update(classify_batch(client, batch))
        except Exception as error:
            # Specific-first so the caller's message is useful, but never fatal.
            _log_api_failure(error, provider)
            break

    return resolved


def _valid_labels(parsed, batch: list[dict]) -> dict[str, dict]:
    """Shared validation: echo-key + category membership check, provider-agnostic."""
    if parsed is None:
        return {}
    valid_keys = {item["key"] for item in batch}
    out = {}
    for label in parsed.labels:
        if label.key not in valid_keys or label.category not in _VALID:
            continue
        out[label.key] = {
            "category": label.category,
            "display": label.display,
            "confidence": float(label.confidence),
        }
    return out


def _classify_batch_anthropic(client, batch: list[dict]) -> dict[str, dict]:
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
    resolved = _valid_labels(getattr(response, "parsed_output", None), batch)
    for value in resolved.values():
        value["model"] = MODEL
    return resolved


def _classify_batch_openai(client, batch: list[dict]) -> dict[str, dict]:
    response = client.responses.parse(
        model=OPENAI_MODEL,
        max_output_tokens=8000,
        input=[
            {"role": "system", "content": TAXONOMY_PROMPT},
            {"role": "user", "content": _format_batch(batch)},
        ],
        text_format=LabelBatch,
    )
    resolved = _valid_labels(getattr(response, "output_parsed", None), batch)
    for value in resolved.values():
        value["model"] = OPENAI_MODEL
    return resolved


_BATCH_CLASSIFIERS = {
    PROVIDER_ANTHROPIC: _classify_batch_anthropic,
    PROVIDER_OPENAI: _classify_batch_openai,
}


def _log_api_failure(error: Exception, provider: str = PROVIDER_ANTHROPIC) -> None:
    """Turn an SDK exception into one readable line. Diagnostics only — never raises."""
    if provider == PROVIDER_OPENAI:
        _log_openai_failure(error)
    else:
        _log_anthropic_failure(error)


def _log_anthropic_failure(error: Exception) -> None:
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


def _log_openai_failure(error: Exception) -> None:
    try:
        import openai
    except ImportError:
        print(f"[bankcat] OpenAI unavailable: {error}")
        return

    if isinstance(error, openai.AuthenticationError):
        message = "invalid or missing OPENAI_API_KEY"
    elif isinstance(error, openai.RateLimitError):
        message = "rate limited — try again shortly"
    elif isinstance(error, openai.APIConnectionError):
        message = "network unreachable"
    elif isinstance(error, openai.APIStatusError):
        message = f"API error {error.status_code}: {error.message}"
    else:
        message = str(error)
    print(f"[bankcat] OpenAI fallback skipped ({message}). Using rules only.")
