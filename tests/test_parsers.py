"""Parse every sample format and check the numbers against the generator's ground truth.

    python tests/test_parsers.py

Regenerate the samples first with `python samples/make_sample.py`.
"""

from __future__ import annotations

import io
import json
import os
import sys

# The console on Windows defaults to cp1252, which cannot print the rupee sign.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bankcat.parsers import (  # noqa: E402
    StatementPasswordError,
    clean_amount,
    load_transactions,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


def test_amount_cleaning() -> None:
    print("\nAmount parsing")
    cases = [
        ("1,23,456.78", 123456.78), ("₹4,150.00", 4150.0), ("(1,234.00)", -1234.0),
        ("450.00 Cr", 450.0), ("2500.00Dr", 2500.0), ("INR 999.00", 999.0),
        ("-", None), ("", None), ("NIL", None), ("ABC", None), (1234.5, 1234.5),
    ]
    for raw, expected in cases:
        actual = clean_amount(raw)
        ok = (actual is None and expected is None) or (
            actual is not None and expected is not None and abs(actual - expected) < 0.01
        )
        check(f"clean_amount({raw!r})", ok, f"got {actual!r}, want {expected!r}")


def test_file(path: str, truth: dict, password: str | None = None) -> None:
    name = os.path.basename(path)
    print(f"\n{name}")
    if not os.path.exists(path):
        check(f"{name} exists", False, "run samples/make_sample.py first")
        return

    frame, report = load_transactions(path, password=password)
    debit = round(float(frame["debit"].sum()), 2)
    credit = round(float(frame["credit"].sum()), 2)
    closing = round(float(frame["balance"].dropna().iloc[-1]), 2) if frame["balance"].notna().any() else None

    print(f"  strategy: {report.strategy}")
    check("row count", len(frame) == truth["rows"], f"{len(frame)} vs {truth['rows']}")
    check("total debit", abs(debit - truth["total_debit"]) < 0.05,
          f"{debit} vs {truth['total_debit']}")
    check("total credit", abs(credit - truth["total_credit"]) < 0.05,
          f"{credit} vs {truth['total_credit']}")
    check("closing balance", closing is not None and abs(closing - truth["closing_balance"]) < 0.05,
          f"{closing} vs {truth['closing_balance']}")
    check("reconciled", report.reconciled, report.summary())
    check("no truncated narrations",
          bool((frame["description"].str.len() > 20).all()),
          f"shortest: {frame['description'].str.len().min()}")


def test_password_handling() -> None:
    print("\nPassword-protected PDF")
    locked = os.path.join(SAMPLES, "sbi_sample_locked.pdf")
    if not os.path.exists(locked):
        check("locked sample exists", False)
        return
    try:
        load_transactions(locked, password="wrong-password")
        check("wrong password rejected", False, "no error raised")
    except StatementPasswordError:
        check("wrong password rejected", True, "raised StatementPasswordError")
    except Exception as error:
        check("wrong password rejected", False, f"raised {type(error).__name__}: {error}")


def test_buffers(truth: dict) -> None:
    """The app hands the parser in-memory uploads, never file paths."""
    print()
    print("In-memory uploads (what the app actually passes)")
    for name in ("hdfc_sample.csv", "icici_sample.xlsx", "sbi_sample.pdf"):
        path = os.path.join(SAMPLES, name)
        if not os.path.exists(path):
            check(f"buffer {name}", False, "missing sample")
            continue
        with open(path, "rb") as handle:
            frame, report = load_transactions(io.BytesIO(handle.read()), filename=name)
        ok = (len(frame) == truth["rows"]
              and abs(float(frame["debit"].sum()) - truth["total_debit"]) < 0.05
              and report.reconciled)
        check(f"buffer {name}", ok, f"{len(frame)} rows, {report.summary()}")

    locked = os.path.join(SAMPLES, "sbi_sample_locked.pdf")
    if os.path.exists(locked):
        with open(locked, "rb") as handle:
            data = handle.read()
        try:
            load_transactions(io.BytesIO(data), password="nope", filename="locked.pdf")
            check("buffer: wrong password rejected", False, "no error raised")
        except StatementPasswordError:
            check("buffer: wrong password rejected", True)
        except Exception as error:
            check("buffer: wrong password rejected", False,
                  f"raised {type(error).__name__}")
        frame, report = load_transactions(
            io.BytesIO(data), password=truth["locked_pdf_password"], filename="locked.pdf")
        check("buffer: correct password opens it",
              len(frame) == truth["rows"] and report.reconciled, report.summary())

    # Streamlit re-reads the same upload on every rerun, so parsing must not consume it.
    path = os.path.join(SAMPLES, "hdfc_sample.csv")
    with open(path, "rb") as handle:
        buffer = io.BytesIO(handle.read())
    first, _ = load_transactions(buffer, filename="hdfc_sample.csv")
    second, _ = load_transactions(buffer, filename="hdfc_sample.csv")
    check("same buffer parses twice", len(first) == len(second) == truth["rows"],
          f"{len(first)} then {len(second)}")


def main() -> int:
    truth_path = os.path.join(SAMPLES, "expected.json")
    if not os.path.exists(truth_path):
        print("Run `python samples/make_sample.py` first.")
        return 1
    with open(truth_path, encoding="utf-8") as handle:
        truth = json.load(handle)

    test_amount_cleaning()
    test_file(os.path.join(SAMPLES, "hdfc_sample.csv"), truth)
    test_file(os.path.join(SAMPLES, "icici_sample.xlsx"), truth)
    test_file(os.path.join(SAMPLES, "sbi_sample.pdf"), truth)
    test_file(os.path.join(SAMPLES, "sbi_sample_locked.pdf"), truth,
              password=truth["locked_pdf_password"])
    test_password_handling()
    test_buffers(truth)

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
