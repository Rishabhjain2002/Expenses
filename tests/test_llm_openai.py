"""Exercise the OpenAI fallback without an API key, using a stub client.

Mirrors test_llm.py's Anthropic coverage: request shape, answers applied and persisted,
every failure mode degrading to rules-only, and provider selection precedence.

    python tests/test_llm_openai.py
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


class StubResponses:
    """Stands in for client.responses, recording how it was called."""

    def __init__(self, answers: dict[str, str], fail_with: Exception | None = None):
        self.answers = answers
        self.fail_with = fail_with
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_with:
            raise self.fail_with

        prompt = kwargs["input"][-1]["content"]
        labels = [
            llm.MerchantLabel(key=key, display=key.title(), category=category,
                              confidence=0.93)
            for key, category in self.answers.items()
            if f"key: {key}" in prompt
        ]

        class Response:
            output_parsed = llm.LabelBatch(labels=labels)

        return Response()


class StubOpenAIClient:
    def __init__(self, answers, fail_with=None):
        self.responses = StubResponses(answers, fail_with)


def sample_frame() -> pd.DataFrame:
    """Two merchants no rule in rules.yaml can match — same fixture as test_llm.py."""
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

    print("Request shape")
    answers = {"quikrwalls": "Shopping", "snitch apparel bangalore": "Shopping"}
    client = StubOpenAIClient(answers)
    with tempfile.TemporaryDirectory() as scratch:
        store = Store(data_dir=scratch)
        result = Categorizer(store=store).categorize(
            frame, use_llm=True, llm_client=client, llm_provider="openai")

        call = client.responses.calls[0] if client.responses.calls else {}
        check("one batched request for two merchants", len(client.responses.calls) == 1,
              f"{len(client.responses.calls)} call(s)")
        check("uses the configured model", call.get("model") == llm.OPENAI_MODEL,
              str(call.get("model")))
        check("uses structured output", call.get("text_format") is llm.LabelBatch)
        input_messages = call.get("input") or [{}]
        check("system prompt is the frozen taxonomy",
              input_messages[0].get("content") == llm.TAXONOMY_PROMPT)
        check("no amounts in the request", "2400" not in str(input_messages))

        print("\nAnswers applied and remembered")
        check("both rows categorised",
              int((result["category"] == UNCATEGORISED).sum()) == 0,
              f"{int((result['category'] == UNCATEGORISED).sum())} left")
        check("tagged as coming from the LLM layer",
              bool((result["category_source"] == "llm").all()),
              str(result["category_source"].tolist()))
        check("written to the cache", set(answers) <= set(store.cache),
              str(sorted(store.cache)))
        check("cache records the openai model",
              all(store.cache[k]["model"] == llm.OPENAI_MODEL for k in answers))

        print("\nSecond run uses the cache, not the API")
        client2 = StubOpenAIClient(answers)
        again = Categorizer(store=Store(data_dir=scratch)).categorize(
            frame, use_llm=True, llm_client=client2, llm_provider="openai")
        check("no API call on the second run", len(client2.responses.calls) == 0,
              f"{len(client2.responses.calls)} call(s)")
        check("still categorised from cache",
              bool((again["category_source"] == "cache").all()),
              str(again["category_source"].tolist()))

    print("\nFailures degrade instead of breaking")
    for label, error in [
        ("network error", ConnectionError("no route to host")),
        ("unexpected error", RuntimeError("boom")),
    ]:
        with tempfile.TemporaryDirectory() as scratch:
            broken = StubOpenAIClient({}, fail_with=error)
            try:
                degraded = Categorizer(store=Store(data_dir=scratch)).categorize(
                    frame, use_llm=True, llm_client=broken, llm_provider="openai")
                ok = int((degraded["category"] == UNCATEGORISED).sum()) == 2
                check(f"{label} degrades to rules-only", ok)
            except Exception as raised:  # noqa: BLE001
                check(f"{label} degrades to rules-only", False,
                      f"raised {type(raised).__name__}: {raised}")

    print("\nProvider selection")
    saved = {k: os.environ.pop(k, None) for k in
             ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY",
              "BANKCAT_LLM_PROVIDER")}
    try:
        check("nothing configured -> is_configured() False", llm.is_configured() is False)
        os.environ["OPENAI_API_KEY"] = "sk-test-fake"
        check("only an OpenAI key -> active_provider() is openai",
              llm.active_provider() == "openai")
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-fake"
        check("both keys configured -> anthropic wins by default",
              llm.active_provider() == "anthropic")
        os.environ["BANKCAT_LLM_PROVIDER"] = "openai"
        check("BANKCAT_LLM_PROVIDER override forces openai",
              llm.active_provider() == "openai")
    finally:
        for key in list(saved):
            os.environ.pop(key, None)
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
