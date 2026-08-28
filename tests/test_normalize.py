"""Check merchant extraction against the narration formats Indian banks actually emit.

    python tests/test_normalize.py
"""

from __future__ import annotations

import os
import sys

# The console on Windows defaults to cp1252, which cannot print the rupee sign.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bankcat.normalize import extract_merchant  # noqa: E402

# (narration, expected merchant key fragment, expected channel)
CASES: list[tuple[str, str, str]] = [
    ("UPI/DR/412345678901/SWIGGY/YESB/swiggy@ybl/Payment", "swiggy", "UPI"),
    ("UPI-SWIGGY-SWIGGY@AXISBANK-UTIB0000123", "swiggy", "UPI"),
    ("UPI-BLINKIT-BLINKIT@AXISBANK-UTIB0000123-Groceries", "blinkit", "UPI"),
    ("UPI/DR/509812345601/ZOMATO/HDFC/zomato@hdfcbank/Order", "zomato", "UPI"),
    ("UPI-ZEPTO-ZEPTONOW@YBL-Groceries", "zepto", "UPI"),
    ("UPI-BIGBASKET-BIGBASKET@ICICI-Groceries", "bigbasket", "UPI"),
    ("UPI/DR/412300112233/DMART/HDFC/dmart@hdfcbank/Shopping", "dmart", "UPI"),
    ("UPI-UBER INDIA-UBERINDIA@ICICI-Trip", "uber", "UPI"),
    ("UPI/DR/412355667788/MAKEMYTRIP/HDFC/mmt@hdfcbank/Flight", "makemytrip", "UPI"),
    ("UPI/DR/412399887766/BESCOM/SBIN/bescom@sbi/Electricity", "bescom", "UPI"),
    ("UPI-AIRTEL PAYMENTS-AIRTEL@AXB-POSTPAID BILL", "airtel", "UPI"),
    ("UPI-JIO RECHARGE-JIO@HDFCBANK-Prepaid", "jio", "UPI"),
    ("UPI/DR/412344556677/IRCTC/SBIN/irctc@sbi/Ticket", "irctc", "UPI"),

    ("POS 4512XXXXXXXX1234 AMAZON PAY INDIA PRIVATE", "amazon", "Card"),
    ("POS 4512XXXXXXXX1234 INDIAN OIL PETROL PUMP BLR", "indian oil", "Card"),
    ("POS 4512XXXXXXXX1234 APOLLO PHARMACY BANGALORE", "apollo", "Card"),
    ("POS 4512XXXXXXXX1234 CROMA RETAIL BANGALORE", "croma", "Card"),
    ("POS 4512XXXXXXXX1234 PVR CINEMAS BANGALORE", "pvr", "Card"),
    ("POS 4512XXXXXXXX1234 CULT FIT BANGALORE", "cult", "Card"),

    ("NEFT-CITIN0012345678-LECXE TECHNOLOGIES PVT LTD-SALARY APR", "lecxe", "NEFT"),
    ("IMPS-509812345678-RAHUL SHARMA-HDFC-RENT", "rahul sharma", "IMPS"),
    ("ACH D- HDFCLIFE-10023456 PREMIUM", "hdfclife", "Auto-debit"),
    ("NACH DR NETFLIX ENTERTAINMENT SERVICES", "netflix", "Auto-debit"),
    ("SI-SIP AXIS MUTUAL FUND FOLIO 91234567", "mutual fund", "Auto-debit"),

    ("ATW-412345XXXXXX1234-CASH WITHDRAWAL KORAMANGALA BLR", "atm withdrawal", "ATM"),
    ("INT.PD:01-01-2025 TO 31-03-2025", "interest credited", "Interest"),
    ("AMB CHRG INCL GST FOR APR2025", "minimum balance charge", "Charges"),
    ("TRANSFER TO 50100123456789 SELF SAVINGS", "self", "Transfer"),
    ("CREDIT CARD PAYMENT HDFC 4512XXXXXXXX9876", "credit card", "Card"),
]

# Narrations that differ only by month/reference must collapse to the same key.
GROUPING_CASES: list[tuple[str, str]] = [
    ("AMB CHRG INCL GST FOR APR2025", "AMB CHRG INCL GST FOR JUN2025"),
    ("UPI/DR/412345678901/SWIGGY/YESB/swiggy@ybl/Payment",
     "UPI/DR/509811223344/SWIGGY/YESB/swiggy@ybl/Payment"),
    ("NACH DR NETFLIX ENTERTAINMENT SERVICES", "NACH DR NETFLIX ENTERTAINMENT SERVICES"),
    ("ATW-412345XXXXXX1234-CASH WITHDRAWAL KORAMANGALA BLR",
     "ATW-412345XXXXXX1234-CASH WITHDRAWAL INDIRANAGAR BLR"),
]

failures: list[str] = []


def main() -> int:
    print("Merchant extraction")
    for narration, expected_key, expected_channel in CASES:
        info = extract_merchant(narration)
        key_ok = expected_key in info.key
        channel_ok = info.channel == expected_channel
        status = "PASS" if (key_ok and channel_ok) else "FAIL"
        print(f"  [{status}] {info.merchant:<28} {info.channel:<11} <- {narration[:52]}")
        if not key_ok:
            failures.append(f"key {info.key!r} missing {expected_key!r} ({narration[:40]})")
        if not channel_ok:
            failures.append(
                f"channel {info.channel!r} != {expected_channel!r} ({narration[:40]})"
            )

    print("\nGrouping across months / references")
    for left, right in GROUPING_CASES:
        left_key = extract_merchant(left).key
        right_key = extract_merchant(right).key
        same = left_key == right_key
        print(f"  [{'PASS' if same else 'FAIL'}] {left_key!r} == {right_key!r}")
        if not same:
            failures.append(f"grouping: {left_key!r} != {right_key!r}")

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
