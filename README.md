# Bank Statement Categoriser

Drop in a statement from any Indian bank — PDF, CSV, or Excel — and get every transaction
categorised, with a dashboard on top. Runs locally in your browser.

```
python -m venv .venv
.venv\Scripts\activate            # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
streamlit run app.py
```

It opens at http://localhost:8501. Drag your statement onto the drop zone. No account to
create, nothing uploaded to a server.

Want to see it work before handing it your own data:

```
pip install -r requirements-dev.txt
python samples/make_sample.py
```

then click **Or try it with a sample statement** — that generates realistic HDFC, ICICI,
and SBI statements (including a password-protected PDF) with known-correct totals.

## Why you can trust the numbers

Two things separate this from a tool that quietly gets it wrong.

**The parse is proved, not assumed.** Nearly every Indian statement carries a running
balance. After parsing, every row is checked: does the balance movement equal the debit
or credit on that row? If all of them do, the header says **Reconciled 247/247 rows** and
you know the numbers are the statement's own. If any don't, it says so and names the rows
— you are never shown a confident dashboard built on a mangled parse.

**Transfers are not spending.** Moving money to your own account, paying a credit-card
bill, and a SIP debit are all money leaving the account, and none of them are expenses.
Counting them — which naive tools do — can nearly double an apparent burn rate. They get
their own categories and sit outside "Money out" by default. A sidebar toggle includes
them if you want the raw figure.

## How categorisation works

Five layers. The first one that recognises a transaction wins.

| | Layer | What it is |
|---|---|---|
| 1 | Your tags | Categories you set in the app. Always win, never overwritten. |
| 2 | Learned | What Claude answered before. Each merchant costs one API call, ever. |
| 3 | Rules | ~500 India-specific merchant patterns in `bankcat/rules.yaml`. Free and instant. |
| 4 | Heuristics | ATM → Cash, bank charges → Fees, interest → Income, unexplained credits → Income. |
| 5 | Claude | Only merchants layers 1–4 could not place. Batched, then cached forever. |

Layers 1–4 need no internet and no API key. On the sample statement they cover **100% of
transaction value** on their own.

### Teaching it a merchant

Two ways, and both stick permanently:

- **In the app** — change the category in the transactions table and press Save. That
  merchant is yours from then on, in every future statement.
- **In `bankcat/rules.yaml`** — add a line under a category. No code change:

  ```yaml
  Food & Dining:
    - swiggy
    - my local cafe
  ```

  Patterns are case-insensitive regular expressions matched against both the cleaned
  merchant name and the raw narration. When several match, the longest wins — so
  `amazon prime` lands in Entertainment even though `amazon` is under Shopping.

### The optional Claude layer

Set `ANTHROPIC_API_KEY` and unknown merchants get sent to Claude, named, categorised, and
remembered. Without a key, everything else still works and unknowns go to the review list
instead.

Only merchant name strings and their narrations are sent — never amounts, balances, or
account numbers. Change the model in one line at the top of `bankcat/llm.py` if you want
`claude-haiku-4-5` instead.

## Working through a statement

The **Needs review** tab lists anything unresolved or low-confidence, **sorted largest
first**. That ordering is the point: fixing the top few is what moves the totals, while a
mislabelled ₹40 coffee changes nothing. Every fix is remembered.

The dashboard also finds **recurring payments** — same merchant, similar amount, steady
cadence — and tells you what they cost per month and per year. It usually needs about
three months of statements to spot a pattern, and it is usually the panel that pays for
the whole tool.

Export from the **Export** tab: an Excel workbook (transactions, by category, by month,
recurring) or a flat CSV.

## Password-protected PDFs

Type the password into the box under the drop zone. Indian banks typically use PAN plus
date of birth, or the first four letters of your name plus date of birth. Scanned or
photographed statements are not supported — there is no OCR; export a CSV or Excel from
your bank's site instead, which is also the most accurate route.

## Layout

```
app.py                    the Streamlit app — upload, dashboard, review, export
bankcat/
  parsers.py              PDF/CSV/XLSX -> normalized table + balance reconciliation
  normalize.py            narration -> merchant, channel, UPI handle
  rules.yaml              the merchant dictionary — edit this to teach it
  categorize.py           the five layers
  llm.py                  the Claude fallback
  insights.py             rollups and recurring-payment detection
  theme.py                chart palette and formatting
data/
  merchant_cache.json     learned from Claude — back this up
  overrides.json          your own tags — back this up
samples/make_sample.py    generates test statements with known totals
tests/                    run them all with `python run_tests.py`
```

`data/` is what makes month two better than month one. It is the only state worth keeping.

## Tests

```
python run_tests.py
```

Covers parsing all four sample formats against known totals, merchant extraction on real
Indian narration formats, end-to-end categorisation, the Claude layer (with a stub client,
so it needs no API key), and a headless render of the dashboard itself.
