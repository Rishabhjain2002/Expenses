"""Generate synthetic Indian bank statements with known-correct totals.

Lets the parser be tested end to end without putting a real statement on disk. The same
ledger is rendered three ways — an HDFC-style CSV, an ICICI-style Excel sheet with a
Dr/Cr indicator column, and a text-layout PDF — so all three parser paths get exercised
against identical expected numbers.

    python samples/make_sample.py
"""

from __future__ import annotations

import csv
import json
import os
from datetime import date

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
OPENING_BALANCE = 184_320.55

# (day, narration, debit, credit)
LEDGER: list[tuple[date, str, float, float]] = [
    (date(2025, 4, 1),  "UPI/DR/412345678901/SWIGGY/YESB/swiggy@ybl/Payment", 486.00, 0),
    (date(2025, 4, 1),  "NEFT-CITIN0012345678-LECXE TECHNOLOGIES PVT LTD-SALARY APR", 0, 145_000.00),
    (date(2025, 4, 2),  "UPI-BLINKIT-BLINKIT@AXISBANK-UTIB0000123-Groceries", 1_243.50, 0),
    (date(2025, 4, 3),  "ACH D- HDFCLIFE-10023456 PREMIUM", 4_150.00, 0),
    (date(2025, 4, 4),  "POS 4512XXXXXXXX1234 INDIAN OIL PETROL PUMP BLR", 2_500.00, 0),
    (date(2025, 4, 5),  "UPI/DR/509812345601/ZOMATO/HDFC/zomato@hdfcbank/Order", 712.00, 0),
    (date(2025, 4, 5),  "SI-SIP AXIS MUTUAL FUND FOLIO 91234567", 15_000.00, 0),
    (date(2025, 4, 7),  "ATW-412345XXXXXX1234-CASH WITHDRAWAL KORAMANGALA BLR", 10_000.00, 0),
    (date(2025, 4, 8),  "UPI-AIRTEL PAYMENTS-AIRTEL@AXB-POSTPAID BILL", 999.00, 0),
    (date(2025, 4, 10), "IMPS-509812345678-RAHUL SHARMA-HDFC-RENT", 32_000.00, 0),
    (date(2025, 4, 12), "POS 4512XXXXXXXX1234 AMAZON PAY INDIA PRIVATE", 3_499.00, 0),
    (date(2025, 4, 14), "UPI/DR/412399887766/BESCOM/SBIN/bescom@sbi/Electricity", 2_310.00, 0),
    (date(2025, 4, 15), "NACH DR NETFLIX ENTERTAINMENT SERVICES", 649.00, 0),
    (date(2025, 4, 18), "UPI-UBER INDIA-UBERINDIA@ICICI-Trip", 384.00, 0),
    (date(2025, 4, 20), "POS 4512XXXXXXXX1234 APOLLO PHARMACY BANGALORE", 1_180.00, 0),
    (date(2025, 4, 22), "CREDIT CARD PAYMENT HDFC 4512XXXXXXXX9876", 24_500.00, 0),
    (date(2025, 4, 25), "UPI/DR/412300112233/DMART/HDFC/dmart@hdfcbank/Shopping", 4_820.00, 0),
    (date(2025, 4, 28), "INT.PD:01-01-2025 TO 31-03-2025", 0, 1_842.00),
    (date(2025, 4, 30), "AMB CHRG INCL GST FOR APR2025", 177.00, 0),

    (date(2025, 5, 1),  "NEFT-CITIN0012399999-LECXE TECHNOLOGIES PVT LTD-SALARY MAY", 0, 145_000.00),
    (date(2025, 5, 2),  "UPI/DR/509811223344/SWIGGY/YESB/swiggy@ybl/Payment", 634.00, 0),
    (date(2025, 5, 3),  "ACH D- HDFCLIFE-10023456 PREMIUM", 4_150.00, 0),
    (date(2025, 5, 5),  "SI-SIP AXIS MUTUAL FUND FOLIO 91234567", 15_000.00, 0),
    (date(2025, 5, 6),  "UPI-ZEPTO-ZEPTONOW@YBL-Groceries", 918.00, 0),
    (date(2025, 5, 8),  "POS 4512XXXXXXXX1234 INDIAN OIL PETROL PUMP BLR", 2_400.00, 0),
    (date(2025, 5, 10), "IMPS-509812345678-RAHUL SHARMA-HDFC-RENT", 32_000.00, 0),
    (date(2025, 5, 12), "NACH DR NETFLIX ENTERTAINMENT SERVICES", 649.00, 0),
    (date(2025, 5, 14), "UPI-AIRTEL PAYMENTS-AIRTEL@AXB-POSTPAID BILL", 999.00, 0),
    (date(2025, 5, 15), "UPI/DR/412355667788/MAKEMYTRIP/HDFC/mmt@hdfcbank/Flight", 18_640.00, 0),
    (date(2025, 5, 18), "ATW-412345XXXXXX1234-CASH WITHDRAWAL INDIRANAGAR BLR", 5_000.00, 0),
    (date(2025, 5, 20), "POS 4512XXXXXXXX1234 CULT FIT BANGALORE", 2_499.00, 0),
    (date(2025, 5, 22), "CREDIT CARD PAYMENT HDFC 4512XXXXXXXX9876", 31_200.00, 0),
    (date(2025, 5, 24), "UPI-BIGBASKET-BIGBASKET@ICICI-Groceries", 3_240.00, 0),
    (date(2025, 5, 26), "UPI/DR/412344556677/IRCTC/SBIN/irctc@sbi/Ticket", 1_455.00, 0),
    (date(2025, 5, 28), "REFUND UPI/CR/412355667788/MAKEMYTRIP/HDFC", 0, 4_120.00),
    (date(2025, 5, 30), "AMB CHRG INCL GST FOR MAY2025", 177.00, 0),

    (date(2025, 6, 1),  "NEFT-CITIN0012400001-LECXE TECHNOLOGIES PVT LTD-SALARY JUN", 0, 145_000.00),
    (date(2025, 6, 2),  "ACH D- HDFCLIFE-10023456 PREMIUM", 4_150.00, 0),
    (date(2025, 6, 3),  "UPI/DR/509899887711/ZOMATO/HDFC/zomato@hdfcbank/Order", 890.00, 0),
    (date(2025, 6, 5),  "SI-SIP AXIS MUTUAL FUND FOLIO 91234567", 15_000.00, 0),
    (date(2025, 6, 6),  "UPI-BLINKIT-BLINKIT@AXISBANK-UTIB0000123-Groceries", 1_670.00, 0),
    (date(2025, 6, 9),  "POS 4512XXXXXXXX1234 CROMA RETAIL BANGALORE", 42_990.00, 0),
    (date(2025, 6, 10), "IMPS-509812345678-RAHUL SHARMA-HDFC-RENT", 32_000.00, 0),
    (date(2025, 6, 12), "NACH DR NETFLIX ENTERTAINMENT SERVICES", 649.00, 0),
    (date(2025, 6, 13), "UPI-JIO RECHARGE-JIO@HDFCBANK-Prepaid", 349.00, 0),
    (date(2025, 6, 16), "UPI/DR/412366778899/BESCOM/SBIN/bescom@sbi/Electricity", 3_120.00, 0),
    (date(2025, 6, 18), "TRANSFER TO 50100123456789 SELF SAVINGS", 25_000.00, 0),
    (date(2025, 6, 20), "POS 4512XXXXXXXX1234 PVR CINEMAS BANGALORE", 1_180.00, 0),
    (date(2025, 6, 23), "UPI-UBER INDIA-UBERINDIA@ICICI-Trip", 512.00, 0),
    (date(2025, 6, 25), "UPI/DR/412300998877/DMART/HDFC/dmart@hdfcbank/Shopping", 5_310.00, 0),
    (date(2025, 6, 27), "CREDIT CARD PAYMENT HDFC 4512XXXXXXXX9876", 28_400.00, 0),
    (date(2025, 6, 30), "AMB CHRG INCL GST FOR JUN2025", 177.00, 0),
]


def build_ledger() -> pd.DataFrame:
    """The ground truth: every row plus a correct running balance."""
    rows, balance = [], OPENING_BALANCE
    for when, narration, debit, credit in LEDGER:
        balance = balance - debit + credit
        rows.append({
            "date": when,
            "description": narration,
            "debit": float(debit),
            "credit": float(credit),
            "balance": round(balance, 2),
        })
    return pd.DataFrame(rows)


def _rupees(value: float) -> str:
    """Format with Indian lakh grouping: 1,23,456.78."""
    whole, _, fraction = f"{value:.2f}".partition(".")
    sign, whole = ("-", whole[1:]) if whole.startswith("-") else ("", whole)
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return f"{sign}{whole}.{fraction}"


def write_hdfc_csv(ledger: pd.DataFrame, path: str) -> None:
    """HDFC-style CSV: ragged preamble rows, then Narration/Withdrawal/Deposit/Balance.

    Amounts carry Indian lakh grouping and are quoted, matching what the bank's own
    export produces. The preamble rows deliberately have fewer fields than the
    transaction rows — that raggedness is what breaks naive readers.
    """
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["HDFC BANK LIMITED"])
        writer.writerow(["Account Holder :", "RISHABH JAIN"])
        writer.writerow(["Account No :", "50100123456789"])
        writer.writerow(["Statement From :", "01/04/2025", "To :", "30/06/2025"])
        writer.writerow([])
        writer.writerow([
            "Date", "Narration", "Chq./Ref.No.", "Value Dt",
            "Withdrawal Amt.", "Deposit Amt.", "Closing Balance",
        ])
        for _, row in ledger.iterrows():
            stamp = row["date"].strftime("%d/%m/%y")
            writer.writerow([
                stamp,
                row["description"],
                "000000000000",
                stamp,
                _rupees(row["debit"]) if row["debit"] else "0.00",
                _rupees(row["credit"]) if row["credit"] else "0.00",
                _rupees(row["balance"]),
            ])
        writer.writerow([])
        writer.writerow(["*Closing Balance"])


def write_icici_xlsx(ledger: pd.DataFrame, path: str) -> None:
    """ICICI-style Excel: single Amount column plus a Dr/Cr indicator."""
    preamble = pd.DataFrame([
        ["ICICI BANK LTD", "", "", "", "", ""],
        ["Detailed Statement", "", "", "", "", ""],
        ["Account Number", "003701501234", "", "", "", ""],
        ["", "", "", "", "", ""],
    ])
    header = ["Sr No", "Value Date", "Transaction Remarks", "Amount", "Dr / Cr", "Balance"]

    body = []
    for index, (_, row) in enumerate(ledger.iterrows(), start=1):
        amount = row["debit"] if row["debit"] else row["credit"]
        body.append([
            index,
            row["date"].strftime("%d-%m-%Y"),
            row["description"],
            f"{amount:.2f}",
            "DR" if row["debit"] else "CR",
            f'{row["balance"]:.2f}',
        ])

    grid = pd.concat(
        [preamble, pd.DataFrame([header]), pd.DataFrame(body)],
        ignore_index=True,
    )
    grid.to_excel(path, index=False, header=False, engine="openpyxl")


def write_sbi_pdf(ledger: pd.DataFrame, path: str, password: str | None = None) -> None:
    """SBI-style text-layout PDF, optionally encrypted. Requires reportlab."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    encrypt = None
    if password:
        from reportlab.lib import pdfencrypt
        encrypt = pdfencrypt.StandardEncryption(password, canPrint=1)

    page = canvas.Canvas(path, pagesize=A4, encrypt=encrypt)
    width, height = A4
    margin, line_height = 28, 11
    y = height - margin

    def newpage() -> float:
        page.showPage()
        page.setFont("Helvetica", 7)
        return height - margin

    page.setFont("Helvetica-Bold", 11)
    page.drawString(margin, y, "STATE BANK OF INDIA - Statement of Account")
    y -= 16
    page.setFont("Helvetica", 8)
    for text in (
        "Account Name : RISHABH JAIN",
        "Account Number : 30012345678    IFSC : SBIN0001234",
        "Period : 01 Apr 2025 to 30 Jun 2025",
        f"Opening Balance : {_rupees(OPENING_BALANCE)}",
    ):
        page.drawString(margin, y, text)
        y -= 11
    y -= 6

    page.setFont("Helvetica-Bold", 7)
    page.drawString(margin, y, "Txn Date   Description")
    page.drawRightString(width - margin - 190, y, "Debit")
    page.drawRightString(width - margin - 100, y, "Credit")
    page.drawRightString(width - margin, y, "Balance")
    y -= 12
    page.setFont("Helvetica", 7)

    for _, row in ledger.iterrows():
        if y < margin + 40:
            y = newpage()

        narration = row["description"]
        # Wrap long narrations onto a continuation line with no leading date — exactly the
        # layout that breaks naive line-by-line parsers.
        head, tail = (narration[:58], narration[58:]) if len(narration) > 58 else (narration, "")

        page.drawString(margin, y, row["date"].strftime("%d-%b-%Y"))
        page.drawString(margin + 62, y, head)
        page.drawRightString(width - margin - 190, y, _rupees(row["debit"]) if row["debit"] else "")
        page.drawRightString(width - margin - 100, y, _rupees(row["credit"]) if row["credit"] else "")
        page.drawRightString(width - margin, y, _rupees(row["balance"]))
        y -= line_height

        if tail:
            page.drawString(margin + 62, y, tail)
            y -= line_height

    page.showPage()
    page.save()


def main() -> None:
    ledger = build_ledger()

    csv_path = os.path.join(HERE, "hdfc_sample.csv")
    xlsx_path = os.path.join(HERE, "icici_sample.xlsx")
    pdf_path = os.path.join(HERE, "sbi_sample.pdf")
    locked_pdf_path = os.path.join(HERE, "sbi_sample_locked.pdf")
    truth_path = os.path.join(HERE, "expected.json")

    write_hdfc_csv(ledger, csv_path)
    write_icici_xlsx(ledger, xlsx_path)
    try:
        write_sbi_pdf(ledger, pdf_path)
        write_sbi_pdf(ledger, locked_pdf_path, password="RISH1234")
    except ImportError:
        print("reportlab not installed — skipping PDF samples "
              "(pip install -r requirements-dev.txt)")

    truth = {
        "rows": int(len(ledger)),
        "total_debit": round(float(ledger["debit"].sum()), 2),
        "total_credit": round(float(ledger["credit"].sum()), 2),
        "opening_balance": OPENING_BALANCE,
        "closing_balance": round(float(ledger["balance"].iloc[-1]), 2),
        "locked_pdf_password": "RISH1234",
    }
    with open(truth_path, "w", encoding="utf-8") as handle:
        json.dump(truth, handle, indent=2)

    print(f"Wrote {len(ledger)} transactions to samples/")
    print(f"  total debit  : {_rupees(truth['total_debit'])}")
    print(f"  total credit : {_rupees(truth['total_credit'])}")
    print(f"  closing bal  : {_rupees(truth['closing_balance'])}")


if __name__ == "__main__":
    main()
