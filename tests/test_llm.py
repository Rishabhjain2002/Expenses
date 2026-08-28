"""Exercise the Claude fallback without an API key, using a stub client.

Checks the request shape (model, structured output, cached system prefix), that answers
are applied and persisted, and — most importantly — that every failure mode degrades to
rules-only instead of breaking the upload.

    python tests/test_llm.py
"""

from __future__ import annotations

import os
import sys

# The console on Windows defaults to cp1252, which cannot print the rupee sign.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

from bankcat import llm  # noqa: E402
from bankcat.categorize import UNCATEGORISED, Categorizer, Store  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


class StubMessages:
    """Stands in for client.messages, recording how it was called."""

    def __init__(self, answers: dict[str, str], fail_with: Exception | None = None):
        self.answers = answers
        self.fail_with = fail_with
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_with:
            raise self.fail_with

        prompt = kwargs["messages"][0]["content"]
        labels = [
            llm.MerchantLabel(key=key, display=key.title(), category=category,
                              confidence=0.93)
            for key, category in self.answers.items()
            if f"key: {key}" in prompt
        ]

        class Response:
            parsed_output = llm.LabelBatch(labels=labels)

        return Response()


class StubClient:
    def __init__(self, answers, fail_with=None):
        self.messages = StubMessages(answers, fail_with)


def sample_frame() -> pd.DataFrame:
    """Two merchants no rule in rules.yaml can match."""
    return pd.DataFrame([
        {"date": pd.Timestamp("2025-04-03"), "source_file": "t.csv", "balance": 9000.0,
         "description": "UPI/DR/412345678901/QUIKRWALLS/YESB/quikrwalls@ybl/Payment",
         "debit": 2400.0, "credit": 0.0},
        {"date": pd.Timestamp("2025-04-09"), "source_file": "t.csv", "balance": 8000.0,
         "description": "POS 4512XXXXXXXX1234 SNITCH APPAREL BANGALORE",
         "debit": 1000.0, "credit": 0.0},
    ])


def main() -> int:
    frame = sample_frame()

    print("Unresolved before Claude")
    with tempfile.TemporaryDirectory() as scratch:
        baseline = Categorizer(store=Store(data_dir=scratch)).categorize(frame, use_llm=False)
    unresolved = set(baseline.loc[baseline["category"] == UNCATEGORISED, "merchant_key"])
    check("both merchants unresolved by rules", len(unresolved) == 2,
          f"{sorted(unresolved)}")

    print("\nRequest shape")
    answers = {"quikrwalls": "Shopping", "snitch apparel bangalore": "Shopping"}
    client = StubClient(answers)
    with tempfile.TemporaryDirectory() as scratch:
        store = Store(data_dir=scratch)
        result = Categorizer(store=store).categorize(frame, use_llm=True, llm_client=client)

        call = client.messages.calls[0] if client.messages.calls else {}
        check("one batched request for two merchants", len(client.messages.calls) == 1,
              f"{len(client.messages.calls)} call(s)")
        check("uses the configured model", call.get("model") == llm.MODEL,
              str(call.get("model")))
        check("uses structured output", call.get("output_format") is llm.LabelBatch)
        system = call.get("system") or [{}]
        check("system prefix is cached",
              system[0].get("cache_control", {}).get("type") == "ephemeral")
        check("system prefix is the frozen taxonomy",
              system[0].get("text") == llm.TAXONOMY_PROMPT)
        check("no amounts in the system prefix", "2400" not in str(system))

        print("\nAnswers applied and remembered")
        check("both rows categorised",
              int((result["category"] == UNCATEGORISED).sum()) == 0,
              f"{int((result['category'] == UNCATEGORISED).sum())} left")
        check("tagged as coming from Claude",
              bool((result["category_source"] == "llm").all()),
              str(result["category_source"].tolist()))
        check("written to the cache", set(answers) <= set(store.cache),
              str(sorted(store.cache)))
        check("cache persisted to disk",
              os.path.exists(os.path.join(scratch, "merchant_cache.json")))

        print("\nSecond run uses the cache, not the API")
        client2 = StubClient(answers)
        again = Categorizer(store=Store(data_dir=scratch)).categorize(
            frame, use_llm=True, llm_client=client2)
        check("no API call on the second run", len(client2.messages.calls) == 0,
              f"{len(client2.messages.calls)} call(s)")
        check("still categorised from cache",
              bool((again["category_source"] == "cache").all()),
              str(again["category_source"].tolist()))

    print("\nFailures degrade instead of breaking")
    for label, error in [
        ("network error", ConnectionError("no route to host")),
        ("unexpected error", RuntimeError("boom")),
    ]:
        with tempfile.TemporaryDirectory() as scratch:
            broken = StubClient({}, fail_with=error)
            try:
                degraded = Categorizer(store=Store(data_dir=scratch)).categorize(
                    frame, use_llm=True, llm_client=broken)
                ok = int((degraded["category"] == UNCATEGORISED).sum()) == 2
                check(f"{label} degrades to rules-only", ok)
            except Exception as raised:  # noqa: BLE001
                check(f"{label} degrades to rules-only", False,
                      f"raised {type(raised).__name__}: {raised}")

    print("\nNo API key configured")
    saved = {k: os.environ.pop(k, None) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    try:
        check("is_configured() is False", llm.is_configured() is False)
        check("classify_merchants returns nothing",
              llm.classify_merchants([{"key": "quikrwalls", "merchant": "Quikrwalls",
                                       "narration": "x", "channel": "UPI",
                                       "direction": "debit"}]) == {})
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value

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
