"""Render the Streamlit app headlessly and assert the dashboard actually draws.

Uses Streamlit's own AppTest harness, so this catches widget-API misuse and render-time
exceptions that unit tests on the library never see.

    python tests/test_app.py
"""

from __future__ import annotations

import os
import sys

# The console on Windows defaults to cp1252, which cannot print the rupee sign.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streamlit.testing.v1 import AppTest  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app.py")

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


def report_exceptions(app: AppTest, stage: str) -> bool:
    if app.exception:
        for item in app.exception:
            print(f"    {stage}: {item.value}")
            if getattr(item, "stack_trace", None):
                print("    " + "\n    ".join(item.stack_trace[-12:]))
        return False
    return True


def main() -> int:
    if not os.path.exists(os.path.join(ROOT, "samples", "hdfc_sample.csv")):
        print("Run `python samples/make_sample.py` first.")
        return 1

    print("Empty state")
    app = AppTest.from_file(APP, default_timeout=90).run()
    check("renders with no upload", report_exceptions(app, "empty"))
    check("shows the drop zone", len(app.get("file_uploader")) == 1)
    check("offers the sample", any("sample" in b.label.lower() for b in app.button))

    print("\nDashboard (sample statement)")
    app.session_state["demo"] = True
    app.run()
    check("renders the dashboard", report_exceptions(app, "dashboard"))

    markdown = " ".join(element.value for element in app.markdown)
    captions = " ".join(element.value for element in app.caption)
    body = markdown + " " + captions

    check("reconciliation badge shown", "Reconciled 52/52 rows" in body, )
    check("coverage badge shown", "of value categorised" in body)
    check("KPI tiles present", "Money in" in body and "Money out" in body
          and "Average monthly spend" in body)
    check("transfers excluded note", "excluded as transfers" in body)

    headings = [element.value for element in app.markdown if element.value.startswith("###")]
    for heading in ("Where the money went", "Month by month", "Top merchants",
                    "Daily spending", "Recurring payments and subscriptions"):
        check(f"section: {heading}", any(heading in h for h in headings))

    check("four charts drawn", len(app.get("plotly_chart")) == 4,
          f"{len(app.get('plotly_chart'))} found")
    check("tabs present", len(app.tabs) >= 3, f"{len(app.tabs)} found")

    # AppTest surfaces st.data_editor as a "dataframe" element and does not flatten
    # tab contents into the root, so look for tables tab by tab.
    tables = sum(len(tab.get("dataframe")) for tab in app.tabs)
    check("transaction tables present", tables >= 2, f"{tables} found across tabs")
    save_buttons = sum(
        1 for tab in app.tabs for button in tab.get("button")
        if "save" in button.label.lower()
    )
    check("save-categories button present", save_buttons >= 1,
          f"{save_buttons} found")
    check("review tab is clean on the sample",
          len(app.tabs[0].get("success")) == 1,
          "sample statement categorises fully, so nothing should need review")
    check("downloads offered", len(app.get("download_button")) == 2,
          f"{len(app.get('download_button'))} found")
    check("recurring detected", "recurring payment" in body)

    print("\nToggle: count transfers as spending")
    app.toggle[0].set_value(True).run()
    check("renders with transfers included", report_exceptions(app, "toggle"))
    toggled = " ".join(e.value for e in app.markdown) + " ".join(e.value for e in app.caption)
    check("money out grew", "including transfers" in toggled)

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
