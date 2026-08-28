"""Run every check: parsers, merchant extraction, categorisation, Claude layer, and UI.

    python run_tests.py
"""

from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SUITES = [
    ("Parsers      ", "tests/test_parsers.py"),
    ("Merchants    ", "tests/test_normalize.py"),
    ("Pipeline     ", "tests/test_pipeline.py"),
    ("Kotak layout ", "tests/test_kotak.py"),
    ("Claude layer ", "tests/test_llm.py"),
    ("App / UI     ", "tests/test_app.py"),
]


def main() -> int:
    for marker, script in (("hdfc_sample.csv", "samples/make_sample.py"),
                           ("kotak_sample.csv", "samples/make_kotak.py")):
        if not os.path.exists(os.path.join(ROOT, "samples", marker)):
            print(f"Generating sample statements ({script})…")
            subprocess.run([sys.executable, script], cwd=ROOT, check=True)

    results = []
    for label, script in SUITES:
        print(f"\n{'=' * 70}\n{label.strip()} — {script}\n{'=' * 70}")
        completed = subprocess.run([sys.executable, script], cwd=ROOT)
        results.append((label, completed.returncode == 0))

    print(f"\n{'=' * 70}\nSummary\n{'=' * 70}")
    for label, passed in results:
        print(f"  {label} {'PASS' if passed else 'FAIL'}")

    failed = [label.strip() for label, passed in results if not passed]
    if failed:
        print(f"\n{len(failed)} suite(s) failed: {', '.join(failed)}")
        return 1
    print("\nEverything passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
