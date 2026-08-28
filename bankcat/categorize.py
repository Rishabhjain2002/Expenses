"""Assign a category to every transaction.

Five layers, checked in order; the first one that resolves a transaction wins:

    1. overrides.json      what you tagged by hand — always wins, never overwritten
    2. merchant_cache.json what Claude answered before — one API call per merchant, ever
    3. rules.yaml          the India merchant dictionary — free, instant, deterministic
    4. heuristics          channel-driven fallbacks (ATM, charges, interest, credits)
    5. Claude              unresolved merchants only, batched, written back to the cache

Layers 1-4 need no network and no API key. Layer 5 is optional; without it, unresolved
transactions land in the review queue instead of being guessed at.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

import pandas as pd
import yaml

from .normalize import annotate, make_key

# --------------------------------------------------------------------------------------
# Taxonomy
# --------------------------------------------------------------------------------------

CATEGORIES = [
    "Food & Dining",
    "Groceries",
    "Transport & Fuel",
    "Shopping",
    "Bills & Utilities",
    "Rent & Housing",
    "Health & Medical",
    "Entertainment & Subscriptions",
    "Travel",
    "Education",
    "Insurance",
    "Investments & SIP",
    "Loan & EMI",
    "Fees & Charges",
    "Cash Withdrawal",
    "Transfers",
    "Income",
    "Uncategorised",
]

UNCATEGORISED = "Uncategorised"

# Money that leaves the account but is not consumption: moving it between your own
# accounts, paying off a card whose purchases are already itemised elsewhere, or buying
# an asset. Counting these as spending is the classic way to overstate a burn rate.
#
# Uncategorised is deliberately NOT in this set. Something we failed to recognise is
# still money that left the account, and dropping it from the total would quietly
# understate spending — the opposite error, and a harder one to notice. It shows up as
# its own bar on the category chart instead, which is the honest way to say "this much
# is unclassified".
NOT_SPENDING = {"Transfers", "Investments & SIP", "Income"}

RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.yaml")
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

SOURCE_LABELS = {
    "override": "You tagged it",
    "cache": "Learned",
    "rule": "Rule",
    "heuristic": "Heuristic",
    "llm": "Claude",
    "none": "Unresolved",
}

# Keywords that turn an incoming credit into income rather than a merchant refund of a
# category. Checked against the raw narration.
_REFUND_RE = re.compile(
    r"\brefund|\breversal|\breversed\b|\bcashback\b|\bchargeback\b|\bcredit\s+adj", re.I
)


@dataclass
class CategoryResult:
    category: str
    source: str
    confidence: float


# --------------------------------------------------------------------------------------
# Rule loading
# --------------------------------------------------------------------------------------

_PLAIN_WORD_RE = re.compile(r"^[a-z0-9 ]+$", re.IGNORECASE)


def _bounded(pattern: str) -> str:
    """Wrap very short plain-word patterns in word boundaries.

    Short brand names and acronyms are otherwise matched as bare substrings, which
    misfires constantly: ``mpl`` matches "co**mpl**ain", ``cred`` matches "credited",
    ``etf`` matches "n**etf**lix", ``rent`` matches "cu**rrent**", ``emi`` matches
    "ch**emi**st". Patterns of five characters or more are distinctive enough to stay
    substrings, so "swiggyinstamart" still matches "swiggy".
    """
    if len(pattern) <= 4 and _PLAIN_WORD_RE.match(pattern):
        return r"\b" + re.escape(pattern) + r"\b"
    return pattern


def load_rules(path: str = RULES_PATH) -> list[tuple[re.Pattern, str, int]]:
    """Compile rules.yaml into (pattern, category, specificity) triples.

    Specificity is the pattern's length: when several patterns match the same narration
    the longest one wins, so "amazon prime" beats "amazon" without needing manual
    ordering in the YAML.
    """
    with open(path, encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    compiled: list[tuple[re.Pattern, str, int]] = []
    for category, patterns in raw.items():
        if category not in CATEGORIES:
            raise ValueError(
                f"rules.yaml lists category {category!r}, which is not in CATEGORIES. "
                f"Add it to categorize.CATEGORIES or fix the spelling."
            )
        for pattern in patterns or []:
            text = str(pattern).strip()
            if not text:
                continue
            try:
                compiled.append((re.compile(_bounded(text), re.IGNORECASE),
                                 category, len(text)))
            except re.error as error:
                raise ValueError(f"Bad pattern {text!r} in rules.yaml: {error}") from error

    # Longest first so the first match found is already the most specific one.
    compiled.sort(key=lambda item: item[2], reverse=True)
    return compiled


# --------------------------------------------------------------------------------------
# Persisted learning
# --------------------------------------------------------------------------------------

def _read_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False, sort_keys=True)
    os.replace(temporary, path)


class Store:
    """The two JSON files that make month two smarter than month one."""

    def __init__(self, data_dir: str = DATA_DIR):
        self.data_dir = data_dir
        self.cache_path = os.path.join(data_dir, "merchant_cache.json")
        self.overrides_path = os.path.join(data_dir, "overrides.json")
        self.cache: dict[str, dict] = _read_json(self.cache_path)
        self.overrides: dict[str, str] = {
            key: value for key, value in _read_json(self.overrides_path).items()
            if isinstance(value, str)
        }

    def set_override(self, merchant_key: str, category: str) -> None:
        key = make_key(merchant_key)
        if not key:
            return
        if category == UNCATEGORISED:
            self.overrides.pop(key, None)
        else:
            self.overrides[key] = category
        _write_json(self.overrides_path, self.overrides)

    def clear_overrides(self) -> None:
        self.overrides = {}
        _write_json(self.overrides_path, self.overrides)

    def remember(self, merchant_key: str, category: str, display: str = "",
                 confidence: float = 0.8, model: str = "") -> None:
        key = make_key(merchant_key)
        if not key or category not in CATEGORIES or category == UNCATEGORISED:
            return
        self.cache[key] = {
            "category": category,
            "display": display or merchant_key,
            "confidence": round(float(confidence), 3),
            "model": model,
        }

    def flush(self) -> None:
        _write_json(self.cache_path, self.cache)

    def clear_cache(self) -> None:
        self.cache = {}
        _write_json(self.cache_path, self.cache)


# --------------------------------------------------------------------------------------
# The categoriser
# --------------------------------------------------------------------------------------

class Categorizer:
    def __init__(self, store: Store | None = None, rules_path: str = RULES_PATH):
        self.store = store or Store()
        self.rules = load_rules(rules_path)
        self._rule_memo: dict[str, tuple[str, int] | None] = {}

    # -- layer 3 -----------------------------------------------------------------------

    def match_rule(self, merchant_key: str, narration: str) -> tuple[str, int] | None:
        """Longest matching pattern across the merchant name and the raw narration."""
        haystack = f"{merchant_key} || {narration}".lower()
        if haystack in self._rule_memo:
            return self._rule_memo[haystack]

        result = None
        for pattern, category, specificity in self.rules:  # already longest-first
            if pattern.search(haystack):
                result = (category, specificity)
                break
        self._rule_memo[haystack] = result
        return result

    # -- layer 4 -----------------------------------------------------------------------

    @staticmethod
    def heuristic(channel: str, is_credit: bool, narration: str) -> CategoryResult | None:
        if channel == "ATM":
            return CategoryResult("Cash Withdrawal", "heuristic", 0.95)
        if channel == "Charges":
            return CategoryResult("Fees & Charges", "heuristic", 0.95)
        if channel == "Interest":
            return CategoryResult("Income", "heuristic", 0.95)
        if is_credit:
            # Money arriving is income unless a rule said otherwise. Lower confidence so
            # a large unexplained credit still surfaces in the review queue.
            confidence = 0.85 if _REFUND_RE.search(narration) else 0.6
            return CategoryResult("Income", "heuristic", confidence)
        return None

    # -- orchestration -----------------------------------------------------------------

    def categorize(self, frame: pd.DataFrame, use_llm: bool = True,
                   llm_client=None) -> pd.DataFrame:
        """Return ``frame`` with category, category_source, and confidence columns."""
        if frame.empty:
            result = frame.copy()
            for column, default in (("merchant", ""), ("merchant_key", ""), ("channel", ""),
                                    ("vpa", None), ("category", UNCATEGORISED),
                                    ("category_source", "none"), ("confidence", 0.0)):
                result[column] = default
            return result

        result = annotate(frame)
        categories, sources, confidences = [], [], []

        for row in result.itertuples(index=False):
            outcome = self._resolve_row(row)
            categories.append(outcome.category)
            sources.append(outcome.source)
            confidences.append(outcome.confidence)

        result["category"] = categories
        result["category_source"] = sources
        result["confidence"] = confidences

        if use_llm:
            result = self._fill_with_llm(result, llm_client)

        return result

    def _resolve_row(self, row) -> CategoryResult:
        merchant_key = getattr(row, "merchant_key", "") or ""
        narration = getattr(row, "description", "") or ""
        channel = getattr(row, "channel", "Other") or "Other"
        is_credit = float(getattr(row, "credit", 0.0) or 0.0) > 0.0

        # 1 — your own tags
        override = self.store.overrides.get(merchant_key)
        if override:
            return CategoryResult(override, "override", 1.0)

        # 2 — what Claude told us before
        cached = self.store.cache.get(merchant_key)
        if cached and cached.get("category") in CATEGORIES:
            return CategoryResult(cached["category"], "cache",
                                  float(cached.get("confidence", 0.8)))

        # 3 — the dictionary
        matched = self.match_rule(merchant_key, narration)
        if matched:
            category, specificity = matched
            # A credit that matched a spending rule is a refund from that merchant, not
            # spending. Route explicit refunds to Income; leave the rest on the merchant's
            # category (spend totals only ever sum debits).
            if is_credit and category not in NOT_SPENDING and _REFUND_RE.search(narration):
                return CategoryResult("Income", "rule", 0.9)
            return CategoryResult(category, "rule", 0.9 if specificity >= 6 else 0.75)

        # 4 — channel heuristics
        guess = self.heuristic(channel, is_credit, narration)
        if guess:
            return guess

        return CategoryResult(UNCATEGORISED, "none", 0.0)

    # -- layer 5 -----------------------------------------------------------------------

    def _fill_with_llm(self, frame: pd.DataFrame, llm_client=None) -> pd.DataFrame:
        """Send unresolved merchants to Claude, then apply and persist the answers."""
        from . import llm as llm_module

        pending = frame[frame["category"] == UNCATEGORISED]
        if pending.empty:
            return frame

        # One entry per unique merchant, with its largest narration as context.
        samples: dict[str, dict] = {}
        for row in pending.itertuples(index=False):
            key = row.merchant_key or make_key(row.description)
            if not key:
                continue
            amount = float(row.debit or 0.0) + float(row.credit or 0.0)
            existing = samples.get(key)
            if existing is None or amount > existing["amount"]:
                samples[key] = {
                    "key": key,
                    "merchant": row.merchant,
                    "narration": row.description,
                    "channel": row.channel,
                    "direction": "credit" if float(row.credit or 0.0) > 0 else "debit",
                    "amount": amount,
                }

        if not samples:
            return frame

        labels = llm_module.classify_merchants(list(samples.values()), client=llm_client)
        if not labels:
            return frame

        for key, label in labels.items():
            self.store.remember(
                key, label["category"], label.get("display", ""),
                label.get("confidence", 0.8), label.get("model", ""),
            )
        self.store.flush()

        unresolved = frame["category"] == UNCATEGORISED
        applied = frame["merchant_key"].map(
            lambda key: labels.get(key, {}).get("category")
        )
        confidences = frame["merchant_key"].map(
            lambda key: labels.get(key, {}).get("confidence", 0.8)
        )
        target = unresolved & applied.notna()

        frame.loc[target, "category"] = applied[target]
        frame.loc[target, "category_source"] = "llm"
        frame.loc[target, "confidence"] = confidences[target]
        return frame


# --------------------------------------------------------------------------------------
# Coverage
# --------------------------------------------------------------------------------------

def coverage(frame: pd.DataFrame) -> dict:
    """How much of the statement was categorised, by count and by value.

    Value coverage is the number that matters — 95% of rows categorised means little if
    the missing 5% holds the largest transactions.
    """
    if frame.empty:
        return {"rows": 0, "rows_categorised": 0, "row_rate": 0.0,
                "value": 0.0, "value_categorised": 0.0, "value_rate": 0.0}

    amounts = frame["debit"].fillna(0.0) + frame["credit"].fillna(0.0)
    known = frame["category"] != UNCATEGORISED
    total_value = float(amounts.sum())
    known_value = float(amounts[known].sum())

    return {
        "rows": int(len(frame)),
        "rows_categorised": int(known.sum()),
        "row_rate": float(known.sum()) / len(frame),
        "value": total_value,
        "value_categorised": known_value,
        "value_rate": (known_value / total_value) if total_value else 0.0,
    }


def needs_review(frame: pd.DataFrame, confidence_floor: float = 0.7) -> pd.DataFrame:
    """Transactions worth a human glance, largest first.

    Sorted by amount because fixing the ten biggest fixes the totals; a mislabelled
    ₹40 coffee changes nothing.
    """
    if frame.empty:
        return frame

    amounts = frame["debit"].fillna(0.0) + frame["credit"].fillna(0.0)
    suspect = (
        (frame["category"] == UNCATEGORISED)
        | ((frame["confidence"] < confidence_floor) & (frame["category_source"] != "override"))
    )
    review = frame[suspect].copy()
    review["_amount"] = amounts[suspect]
    return review.sort_values("_amount", ascending=False).drop(columns=["_amount"])
