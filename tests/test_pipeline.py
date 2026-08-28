"""End-to-end: parse a sample statement, categorise it, and check the money adds up.

Runs with layer 5 (Claude) disabled so it is deterministic, offline, and free. That is
also the honest test of the rule dictionary — how much it covers on its own.

    python tests/test_pipeline.py
"""

from __future__ import annotations

import os
import sys

# The console on Windows defaults to cp1252, which cannot print the rupee sign.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bankcat.categorize import (  # noqa: E402
    NOT_SPENDING,
    UNCATEGORISED,
    Categorizer,
    Store,
    coverage,
    needs_review,
)
from bankcat.parsers import load_transactions  # noqa: E402

SAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")

# Merchants whose category is not a judgement call — getting these wrong is a real bug.
EXPECTED: dict[str, str] = {
    "swiggy": "Food & Dining",
    "zomato": "Food & Dining",
    "blinkit": "Groceries",
    "zepto": "Groceries",
    "bigbasket": "Groceries",
    "dmart": "Groceries",
    "uber": "Transport & Fuel",
    "indian oil petrol pump blr": "Transport & Fuel",
    "amazon": "Shopping",
    "croma retail bangalore": "Shopping",
    "bescom": "Bills & Utilities",
    "airtel": "Bills & Utilities",
    "jio": "Bills & Utilities",
    "netflix entertainment": "Entertainment & Subscriptions",
    "pvr cinemas bangalore": "Entertainment & Subscriptions",
    "makemytrip": "Travel",
    "irctc": "Travel",
    "apollo pharmacy bangalore": "Health & Medical",
    "cult fit bangalore": "Health & Medical",
    "hdfclife": "Insurance",
    "sip mutual fund": "Investments & SIP",
    "atm withdrawal": "Cash Withdrawal",
    "minimum balance charge": "Fees & Charges",
    "credit card payment": "Transfers",
    "self savings": "Transfers",
}

# Money coming in.
EXPECTED_CREDITS: dict[str, str] = {
    "interest credited": "Income",
    "lecxe technologies": "Income",
}

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


def main() -> int:
    path = os.path.join(SAMPLES, "hdfc_sample.csv")
    if not os.path.exists(path):
        print("Run `python samples/make_sample.py` first.")
        return 1

    frame, report = load_transactions(path)
    print(f"\nParsed {len(frame)} rows — {report.summary()}")

    # Isolated store so a developer's real cache/overrides cannot affect the result.
    with tempfile.TemporaryDirectory() as scratch:
        categorizer = Categorizer(store=Store(data_dir=scratch))
        result = categorizer.categorize(frame, use_llm=False)

    print("\nCategory totals (debits)")
    spend = result[result["debit"] > 0]
    for category, amount in spend.groupby("category")["debit"].sum().sort_values(
            ascending=False).items():
        marker = "  (not spending)" if category in NOT_SPENDING else ""
        print(f"  {category:<32} {amount:>12,.2f}{marker}")

    print("\nMerchant categorisation")
    # Keyed off debit rows: the same merchant can legitimately appear on both sides —
    # a MakeMyTrip booking is Travel while a MakeMyTrip refund is Income — so a plain
    # merchant->category map would just record whichever row came last.
    debits = result[result["debit"] > 0]
    by_key = dict(zip(debits["merchant_key"], debits["category"]))
    for key, expected in EXPECTED.items():
        actual = by_key.get(key)
        check(f"{key} -> {expected}", actual == expected, f"got {actual!r}")

    credits = result[result["credit"] > 0]
    by_credit_key = dict(zip(credits["merchant_key"], credits["category"]))
    for key, expected in EXPECTED_CREDITS.items():
        actual = by_credit_key.get(key)
        check(f"{key} (credit) -> {expected}", actual == expected, f"got {actual!r}")

    print("\nRefunds are income, not negative spending")
    refunds = result[
        (result["credit"] > 0) & result["description"].str.contains("REFUND", case=False)
    ]
    check("refund row present", len(refunds) == 1, f"{len(refunds)} found")
    if len(refunds):
        check("merchant refund categorised as Income",
              bool((refunds["category"] == "Income").all()),
              f"got {refunds['category'].tolist()}")

    print("\nCoverage and totals")
    stats = coverage(result)
    print(f"  value coverage: {stats['value_rate']:.1%}  "
          f"row coverage: {stats['row_rate']:.1%}")
    check("value coverage >= 95%", stats["value_rate"] >= 0.95,
          f"{stats['value_rate']:.1%}")
    check("nothing uncategorised", int((result['category'] == UNCATEGORISED).sum()) == 0,
          f"{int((result['category'] == UNCATEGORISED).sum())} rows")

    # The headline number: real spending excludes transfers and investments.
    real_spend = result[
        (result["debit"] > 0) & (~result["category"].isin(NOT_SPENDING))
    ]["debit"].sum()
    all_debits = float(result["debit"].sum())
    excluded = all_debits - float(real_spend)
    print(f"  all debits      : {all_debits:>12,.2f}")
    print(f"  real spending   : {float(real_spend):>12,.2f}")
    print(f"  excluded        : {excluded:>12,.2f}  (transfers, SIPs, card bills)")
    check("transfers excluded from spend", excluded > 0,
          "self-transfers and card bills must not count as spending")
    check("credit card bills not spending",
          by_key.get("credit card payment") in NOT_SPENDING)

    review = needs_review(result)
    print(f"\nNeeds review: {len(review)} row(s)")
    for row in review.head(5).itertuples(index=False):
        print(f"  {row.merchant:<28} {row.category:<24} conf={row.confidence:.2f}")

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} failure(s):")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
