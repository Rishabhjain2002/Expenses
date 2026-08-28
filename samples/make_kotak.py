"""Generate a Kotak-shaped statement — the layout that broke the first parser.

A real Kotak Mahindra statement differs from the HDFC/ICICI/SBI shapes in four ways that
each need their own handling, and all four are reproduced here:

  1. A single **signed** amount column headed ``DEBIT/CREDIT(INR)`` — values like
     ``-216.07`` and ``+164.00``. The header reads almost exactly like a Dr/Cr *flag*
     column, so only the data can tell them apart.
  2. Rows are listed **newest first**, so a forward walk of the running balance sees
     every delta backwards.
  3. The transaction time is printed on a **second line under the date**, so the cell
     arrives as "25 Aug 2026 08:35 PM".
  4. Narrations **wrap onto a second line**, and are positional:
     ``UPI/<payee>/<bank>/<ref>/<note>``.

Merchants and amounts here are invented; only the structure is taken from a real
statement.

    python samples/make_kotak.py
"""

from __future__ import annotations

import csv
import json
import os
from datetime import date

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
OPENING_BALANCE = 17_964.78

# (date, time, narration, signed amount) — oldest first; rendered newest first.
LEDGER: list[tuple[date, str, str, float]] = [
    (date(2026, 7, 27), "06:40 PM", "UPI/XERO DEGREES K/YESB/620871232863/UPI", -262.00),
    (date(2026, 7, 28), "10:12 AM", "UPI/GOOGLE INDIA D/utib/276270482096/UPI", 2.00),
    (date(2026, 7, 28), "02:43 PM", "UPI/Netflix/utib/349259002096/MandateExecu", -199.00),
    (date(2026, 7, 28), "04:14 PM", "UPI/Gopi Service S/YESB/620963487953/UPI", -420.00),
    (date(2026, 7, 29), "12:42 PM", "UPI/ATUL AGARWAL/UTIB/657659069503/ITR", -1_500.00),
    (date(2026, 7, 29), "06:15 PM", "UPI/Hare krishna I/YESB/657688683994/UPI", -25.00),
    (date(2026, 7, 31), "07:27 PM",
     "MB:RECEIVED FROM MAPPING DIGIWORLD PRIVATE LIMITED", 3_000.00),
    (date(2026, 8, 1), "05:03 AM",
     "Recd:IMPS/621305736877/LANEONE VE/KKBK/X0021/BULD7", 41_667.00),
    (date(2026, 8, 2), "09:22 PM", "MB:811SENT NEFT SELF 398000JB00001337 PUN", -5_000.00),
    (date(2026, 8, 2), "09:33 PM", "UPI/CRED Club/UTIB/658029603366/payment on C", -4_220.00),
    (date(2026, 8, 3), "12:09 AM",
     "UPI/ANGEL ONE MUTU/INDB/100141356049/Subscription", -100.00),
    (date(2026, 8, 4), "09:57 PM", "UPI/Blinkit/AIRP/621676284406/Pay via Razo", -235.00),
    (date(2026, 8, 6), "05:12 PM",
     "UPI/ADARSH PRAKASH/ICIC/658488352312/Paid via Sup", 50.00),
    (date(2026, 8, 6), "09:52 PM", "UPI/Amazon Pay/UTIB/621870437039/You are payi", -73.46),
    (date(2026, 8, 8), "03:30 PM", "UPI/VED ELECTROMEC/UTIB/658641467592/UPI", -950.00),
    (date(2026, 8, 8), "08:37 PM", "UPI/Amazon Pay/UTIB/622020600533/You are payi", -107.09),
    (date(2026, 8, 10), "07:02 PM", "UPI/Hare krishna I/YESB/658820552843/UPI", -25.00),
    (date(2026, 8, 11), "10:29 PM",
     "UPI/SPOTIFY INDIA /HDFC/103839732470/Execution te", -139.00),
    (date(2026, 8, 12), "08:14 PM", "UPI/Aman Kumar/YESB/659065523708/UPI", -50.00),
    (date(2026, 8, 13), "04:41 PM", "UPI/Mr SURESH KUM/CBIN/659124786292/UPI", -3_000.00),
    (date(2026, 8, 14), "07:30 AM", "UPI/DELHI METRO RA/AIRP/622695729598/Payment", -600.00),
    (date(2026, 8, 14), "10:54 AM", "UPI/SWIGGY INSTAMA/UTIB/622658630241/UPI", -495.00),
    (date(2026, 8, 14), "11:09 AM", "UPI/MUSKAN/BARB/622630462323/Sent using P", 975.00),
    (date(2026, 8, 15), "12:57 AM", "UPI/1 GGN SGT GH/YESB/659312607863/UPI", -20.00),
    (date(2026, 8, 15), "01:02 AM", "REV-UPI/1 GGN SGT GH/659312607863/COMPLAIN", 20.00),
    (date(2026, 8, 15), "01:01 PM", "UPI/SHAGUN SWEETS/YESB/659369139920/UPI", -580.00),
    (date(2026, 8, 15), "02:35 PM", "UPI/MAX SUPER SPEC/HDFC/659332345289/UPI", -20.00),
    (date(2026, 8, 15), "09:30 PM", "UPI/SWIGGY INSTAMA/UTIB/659330280641/UPI", -223.00),
    (date(2026, 8, 17), "12:05 PM", "UPI/MUNCHMART TECH/UTIB/622974494146/UPI", -105.00),
    (date(2026, 8, 18), "08:09 PM", "UPI/TOSHIT JAIN/KKBK/623033503097/kitty", -7_000.00),
    (date(2026, 8, 19), "06:39 PM", "UPI/Hunger Point/YESB/623102578855/UPI", -25.00),
    (date(2026, 8, 20), "10:17 PM", "UPI/GOOGLE INDIA D/utib/397217822326/UPI", 2.00),
    (date(2026, 8, 21), "12:21 AM", "UPI/ZEPTO/HDFC/623365200740/UPI", -88.00),
    (date(2026, 8, 21), "08:18 PM", "UPI/Zepto/YESB/623388559691/UPI", -74.00),
    (date(2026, 8, 21), "08:19 PM", "UPI/Zepto/AIRP/623318247016/UPI", -72.00),
    (date(2026, 8, 22), "09:28 AM", "UPI/CRED Club/UTIB/660029656566/payment on C", -3_915.06),
    (date(2026, 8, 23), "07:35 PM", "UPI/BHAGATJI BALUS/YESB/660115288923/UPI", -34.00),
    (date(2026, 8, 24), "07:45 AM", "UPI/DELHI METRO RA/AIRP/623694727010/Payment", -500.00),
    (date(2026, 8, 24), "01:04 PM", "NACH-ECS-CR-EICHER FNLDIV 202526-80723", 164.00),
    (date(2026, 8, 25), "12:43 AM", "UPI/ZEPTO MARKETPL/UTIB/623784894716/UPI", -203.00),
    (date(2026, 8, 25), "08:35 PM", "UPI/Amazon Pay/YESB/660316651231/You are payi", -216.07),
]


def rupees(value: float) -> str:
    """Indian lakh grouping, no currency symbol: 1,23,456.78."""
    whole, _, fraction = f"{abs(value):.2f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return f"{whole}.{fraction}"


def build_ledger() -> pd.DataFrame:
    """Ground truth, in true chronological order."""
    rows, balance = [], OPENING_BALANCE
    for when, clock, narration, signed in LEDGER:
        balance += signed
        rows.append({
            "date": when,
            "clock": clock,
            "description": narration,
            "signed": float(signed),
            "debit": float(-signed) if signed < 0 else 0.0,
            "credit": float(signed) if signed > 0 else 0.0,
            "balance": round(balance, 2),
        })
    return pd.DataFrame(rows)


def write_csv(ledger: pd.DataFrame, path: str) -> None:
    newest_first = ledger.iloc[::-1].reset_index(drop=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Account Statement"])
        writer.writerow(["Account #", "1234567890", "SAVINGS"])
        writer.writerow(["27 Jul 2026 - 26 Aug 2026"])
        writer.writerow([])
        writer.writerow(["#", "Transaction Date", "Value Date", "Transaction Details",
                         "Chq / Ref No.", "DEBIT/CREDIT(INR)", "BALANCE(INR)"])
        for position, row in newest_first.iterrows():
            signed = row["signed"]
            writer.writerow([
                position + 1,
                row["date"].strftime("%d %b %Y") + " " + row["clock"],
                row["date"].strftime("%d %b %Y"),
                row["description"],
                "UPI-6237379201",
                ("+" if signed > 0 else "-") + rupees(signed),
                rupees(row["balance"]),
            ])


def write_pdf(ledger: pd.DataFrame, path: str) -> None:
    """A ruled table, like the real thing.

    Kotak renders its statement as a bordered table, which is why pdfplumber's table
    extractor finds it. The date cell carries the time on a second line and the
    narration wraps inside its cell — both stay inside one table cell, exactly as they
    do in the bank's own PDF.
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)

    cell = ParagraphStyle("cell", fontName="Helvetica", fontSize=7, leading=9)
    head = ParagraphStyle("head", fontName="Helvetica-Bold", fontSize=7, leading=9)
    title = ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=15, leading=18)
    meta = ParagraphStyle("meta", fontName="Helvetica", fontSize=8, leading=11)

    newest_first = ledger.iloc[::-1].reset_index(drop=True)
    rows = [[Paragraph(text, head) for text in
             ("#", "TRANSACTION DATE", "VALUE DATE", "TRANSACTION DETAILS",
              "CHQ / REF NO.", "DEBIT/CREDIT(INR)", "BALANCE(INR)")]]

    for position, row in newest_first.iterrows():
        stamp = row["date"].strftime("%d %b %Y")
        signed = row["signed"]
        rows.append([
            Paragraph(str(position + 1), cell),
            Paragraph(stamp + "<br/>" + row["clock"], cell),
            Paragraph(stamp, cell),
            Paragraph(row["description"], cell),
            Paragraph("UPI-6237379201", cell),
            Paragraph(("+" if signed > 0 else "-") + rupees(signed), cell),
            Paragraph("<b>" + rupees(row["balance"]) + "</b>", cell),
        ])

    table = Table(
        rows, repeatRows=1,
        colWidths=[8 * mm, 24 * mm, 20 * mm, 58 * mm, 26 * mm, 24 * mm, 22 * mm],
    )
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f2f2")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (5, 0), (6, -1), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))

    document = SimpleDocTemplate(
        path, pagesize=A4, leftMargin=12 * mm, rightMargin=12 * mm,
        topMargin=12 * mm, bottomMargin=12 * mm,
    )
    document.build([
        Paragraph("Account Statement", title),
        Paragraph("Account # 1234567890 SAVINGS &nbsp; Branch SAMPLE BRANCH", meta),
        Paragraph("27 Jul 2026 - 26 Aug 2026", meta),
        Paragraph("Sample Account Holder", meta),
        Paragraph("IFSC KKBK0000000 &nbsp; MICR 110000000", meta),
        Spacer(1, 6 * mm),
        table,
    ])


def main() -> None:
    ledger = build_ledger()
    write_csv(ledger, os.path.join(HERE, "kotak_sample.csv"))
    try:
        write_pdf(ledger, os.path.join(HERE, "kotak_sample.pdf"))
    except ImportError:
        print("reportlab not installed — skipping the Kotak PDF")

    truth = {
        "rows": int(len(ledger)),
        "total_debit": round(float(ledger["debit"].sum()), 2),
        "total_credit": round(float(ledger["credit"].sum()), 2),
        "opening_balance": OPENING_BALANCE,
        "closing_balance": round(float(ledger["balance"].iloc[-1]), 2),
        "first_date": ledger["date"].iloc[0].isoformat(),
        "last_date": ledger["date"].iloc[-1].isoformat(),
    }
    with open(os.path.join(HERE, "kotak_expected.json"), "w", encoding="utf-8") as handle:
        json.dump(truth, handle, indent=2)

    print(f"Wrote {truth['rows']} Kotak-style transactions to samples/")
    print(f"  total debit  : {rupees(truth['total_debit'])}")
    print(f"  total credit : {rupees(truth['total_credit'])}")
    print(f"  closing bal  : {rupees(truth['closing_balance'])}")


if __name__ == "__main__":
    main()
