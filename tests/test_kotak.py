"""Regression tests for the Kotak statement layout.

A real Kotak Mahindra statement broke the first version of the parser in four separate
ways at once. Each of them is checked here:

  1. ``DEBIT/CREDIT(INR)`` is a single **signed** amount column, but reads like a Dr/Cr
     *flag* column. Getting this wrong produced "Found a header but no amount columns".
  2. Rows are **newest first**, so a forward walk of the running balance sees every
     delta backwards and nothing reconciles.
  3. The time is printed **under the date**, so the cell arrives as "25 Aug 2026 08:35 PM".
  4. Narrations are **positional** — ``UPI/<payee>/<bank>/<ref>/<note>`` — so the longest
     phrase is the wrong pick: the note "You are payi" outruns the merchant "Amazon Pay".

    python tests/test_kotak.py
"""

from __future__ import annotations

import json
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
)
from bankcat.normalize import extract_merchant  # noqa: E402
from bankcat.parsers import load_transactions  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")

# Narrations in Kotak's positional UPI format, and the payee that must come out.
NARRATIONS = [
    ("UPI/Amazon Pay/YESB/660316651231/You are payi", "amazon", "UPI"),
    ("UPI/ZEPTO MARKETPL/UTIB/623784894716/UPI", "zepto", "UPI"),
    ("UPI/CRED Club/UTIB/660029656566/payment on C", "cred club", "UPI"),
    ("UPI/TOSHIT JAIN/KKBK/623033503097/kitty", "toshit jain", "UPI"),
    ("UPI/SPOTIFY INDIA /HDFC/103839732470/Execution te", "spotify", "UPI"),
    ("UPI/Netflix/utib/349259002096/MandateExecu", "netflix", "UPI"),
    ("UPI/DELHI METRO RA/AIRP/623694727010/Payment", "delhi metro", "UPI"),
    ("UPI/ANGEL ONE MUTU/INDB/100141356049/Subscription", "angel one", "UPI"),
    ("UPI/Blinkit/AIRP/621676284406/Pay via Razo", "blinkit", "UPI"),
    ("UPI/MUSKAN/BARB/622630462323/Sent using P", "muskan", "UPI"),
    ("REV-UPI/1 GGN SGT GH/659312607863/COMPLAIN", "ggn sgt gh", "UPI"),
    ("NACH-ECS-CR-EICHER FNLDIV 202526-80723", "eicher", "Auto-debit"),
    ("Recd:IMPS/621305736877/LANEONE VE/KKBK/X0021/BULD7", "laneone", "IMPS"),
]

# Categories that are not a judgement call on this statement.
CATEGORIES = {
    "angel one mutu": "Investments & SIP",       # the note said "Subscription"
    "swiggy instama": "Groceries",               # Instamart, truncated mid-word
    "delhi metro ra": "Transport & Fuel",
    "max super spec": "Health & Medical",
    "xero degrees": "Food & Dining",
    "shagun sweets": "Food & Dining",
    "zepto": "Groceries",
    "blinkit": "Groceries",
    "netflix": "Entertainment & Subscriptions",
    "spotify": "Entertainment & Subscriptions",
    "cred club": "Transfers",                    # a credit-card bill, not spending
    "811sent self pun": "Transfers",             # NEFT to own account
    "amazon": "Shopping",
}

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


def test_narrations() -> None:
    print("Positional UPI narrations")
    for narration, expected_key, expected_channel in NARRATIONS:
        info = extract_merchant(narration)
        ok = expected_key in info.key and info.channel == expected_channel
        check(f"{info.merchant:<20} {info.channel:<11}", ok,
              f"want {expected_key!r}/{expected_channel}, got {info.key!r}/{info.channel}")


def test_parse(path: str, truth: dict):
    name = os.path.basename(path)
    print(f"\n{name}")
    if not os.path.exists(path):
        check(f"{name} exists", False, "run samples/make_kotak.py first")
        return None

    frame, report = load_transactions(path)
    print(f"  strategy: {report.strategy}")

    check("signed amount column read", abs(
        float(frame["debit"].sum()) - truth["total_debit"]) < 0.05,
        f"debit {frame['debit'].sum():,.2f} vs {truth['total_debit']:,.2f}")
    check("credits kept positive", abs(
        float(frame["credit"].sum()) - truth["total_credit"]) < 0.05,
        f"credit {frame['credit'].sum():,.2f} vs {truth['total_credit']:,.2f}")
    check("row count", len(frame) == truth["rows"], f"{len(frame)} vs {truth['rows']}")
    check("newest-first statement reversed",
          str(frame["date"].iloc[0].date()) == truth["first_date"]
          and str(frame["date"].iloc[-1].date()) == truth["last_date"],
          f"{frame['date'].iloc[0].date()} .. {frame['date'].iloc[-1].date()}")
    check("time stripped from date cell", bool(frame["date"].notna().all()))
    check("reconciled", report.reconciled, report.summary())
    check("closing balance",
          abs(float(frame["balance"].dropna().iloc[-1]) - truth["closing_balance"]) < 0.05)
    return frame


def test_categories(frame) -> None:
    print("\nCategorisation")
    with tempfile.TemporaryDirectory() as scratch:
        result = Categorizer(store=Store(data_dir=scratch)).categorize(frame, use_llm=False)

    debits = result[result["debit"] > 0]
    by_key = dict(zip(debits["merchant_key"], debits["category"]))
    for key, expected in CATEGORIES.items():
        check(f"{key} -> {expected}", by_key.get(key) == expected, f"got {by_key.get(key)!r}")

    # "mpl" (Mobile Premier League) used to match inside "COMPLAIN".
    check("no false match inside COMPLAIN",
          by_key.get("ggn sgt gh", UNCATEGORISED) == UNCATEGORISED,
          f"got {by_key.get('ggn sgt gh')!r}")

    stats = coverage(result)
    print(f"  value coverage {stats['value_rate']:.1%}, row coverage {stats['row_rate']:.1%}")
    check("value coverage >= 80%", stats["value_rate"] >= 0.80, f"{stats['value_rate']:.1%}")

    # An unrecognised debit is still money that left the account.
    spend = result[(result["debit"] > 0) & (~result["category"].isin(NOT_SPENDING))]
    unknown_spend = spend[spend["category"] == UNCATEGORISED]["debit"].sum()
    check("uncategorised debits still counted as spending", float(unknown_spend) > 0,
          f"{float(unknown_spend):,.2f} would otherwise vanish from the total")


def main() -> int:
    truth_path = os.path.join(SAMPLES, "kotak_expected.json")
    if not os.path.exists(truth_path):
        print("Run `python samples/make_kotak.py` first.")
        return 1
    with open(truth_path, encoding="utf-8") as handle:
        truth = json.load(handle)

    test_narrations()
    frame = test_parse(os.path.join(SAMPLES, "kotak_sample.csv"), truth)
    test_parse(os.path.join(SAMPLES, "kotak_sample.pdf"), truth)
    if frame is not None:
        test_categories(frame)

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
