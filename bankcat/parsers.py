"""Turn a bank statement file (CSV / Excel / PDF) into a normalized transaction table.

Public entry point is :func:`load_transactions`. Everything it returns is validated against
the statement's own running balance, so a silently mangled parse is detectable rather than
something you discover three dashboards later.

Normalized schema:
    date (datetime64) | description (str) | debit (float) | credit (float)
    | balance (float | NaN) | source_file (str)

Debits and credits are both stored as positive magnitudes; exactly one of them is non-zero
for a given row.
"""

from __future__ import annotations

import bisect
import csv
import io
import os
import re
from dataclasses import dataclass, field

import pandas as pd
import pdfplumber

# --------------------------------------------------------------------------------------
# Column vocabulary
# --------------------------------------------------------------------------------------

# Order matters within each list only for readability; matching is longest-token-first so
# that "value date" wins over "date" when both appear in the same header cell.
COLUMN_SYNONYMS: dict[str, list[str]] = {
    "date": [
        "transaction date", "txn date", "tran date", "posting date", "value date",
        "value dt", "date",
    ],
    "description": [
        "transaction remarks", "transaction description", "transaction details",
        "narration", "particulars", "description", "remarks", "details", "narrative",
    ],
    "debit": [
        "withdrawal amt", "withdrawal amount", "withdrawal (dr)", "debit amount",
        "withdrawal", "withdrawl", "debit", "paid out", "dr amount", "dr",
    ],
    "credit": [
        "deposit amt", "deposit amount", "deposit (cr)", "credit amount",
        "deposit", "credit", "paid in", "cr amount", "cr",
    ],
    "balance": [
        "closing balance", "running balance", "available balance", "balance amount",
        "balance (inr)", "balance", "bal",
    ],
    # Single-amount layouts: one "amount" column plus a Dr/Cr indicator column.
    "amount": ["transaction amount", "amount (inr)", "amount", "amt"],
    "drcr": ["dr / cr", "dr/cr", "cr/dr", "type", "indicator", "transaction type"],
}

# Header cells that must NOT be treated as a column we care about, even though they
# contain a synonym substring (e.g. "Cheque Date", "Reference Number").
_HEADER_BLOCKLIST = re.compile(
    r"cheque|chq|ref(?:erence)?\s*(?:no|number|num)|serial|sl\.?\s*no|s\.?\s*no\b|branch|"
    r"instrument|utr",
    re.IGNORECASE,
)

DATE_FORMATS = [
    "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y",
    "%d-%b-%Y", "%d %b %Y", "%d-%b-%y", "%d %b %y",
    "%d/%b/%Y", "%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y",
]

# A number like 1,23,456.78 / 1234.56 / (1,234.00) / 450.00Cr
_AMOUNT_RE = re.compile(
    r"^\(?\s*(?:inr|rs\.?|₹)?\s*"
    r"([+-]?\d[\d,]*(?:\.\d+)?)"
    r"\s*(cr|dr)?\s*\)?$",
    re.IGNORECASE,
)

# Trailing amount token used when scraping PDF text lines.
_TRAILING_AMOUNT_RE = re.compile(
    r"(?:(?<=\s)|^)(\d{1,3}(?:,\d{2,3})*\.\d{2}|\d+\.\d{2})\s*(CR|DR)?\s*$",
    re.IGNORECASE,
)

_LEADING_DATE_RE = re.compile(
    r"^\s*(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}"
    r"|\d{1,2}[\-\s][A-Za-z]{3}[\-\s]\d{2,4})\s+",
)

_TOLERANCE = 0.02  # rupees; covers rounding in extracted text


# --------------------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------------------

@dataclass
class ParseReport:
    """What happened during a parse, and whether the numbers can be trusted."""

    source_file: str = ""
    strategy: str = ""
    rows_total: int = 0
    rows_reconciled: int = 0
    unreconciled_rows: list[int] = field(default_factory=list)
    has_balance: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def reconciled(self) -> bool:
        """True when every row's balance movement matches its debit/credit."""
        return self.has_balance and self.rows_total > 0 and not self.unreconciled_rows

    @property
    def reconciliation_rate(self) -> float:
        if not self.has_balance or not self.rows_total:
            return 0.0
        return self.rows_reconciled / self.rows_total

    def summary(self) -> str:
        if not self.has_balance:
            return f"{self.rows_total} rows parsed (no balance column — cannot verify)"
        if self.reconciled:
            return f"Reconciled {self.rows_reconciled}/{self.rows_total} rows"
        return (
            f"Reconciled {self.rows_reconciled}/{self.rows_total} rows "
            f"— {len(self.unreconciled_rows)} row(s) do not add up"
        )


class StatementPasswordError(Exception):
    """Raised when a PDF is encrypted and the supplied password did not open it."""


class StatementParseError(Exception):
    """Raised when no transaction table could be found in the file."""


# --------------------------------------------------------------------------------------
# Scalar cleaning
# --------------------------------------------------------------------------------------

def clean_amount(value) -> float | None:
    """Parse an Indian-format money string into a float. Returns None if not a number."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and pd.isna(value):
            return None
        return float(value)

    text = str(value).strip()
    if not text or text in {"-", "--", ".", "nil", "NIL", "NA", "N/A"}:
        return None

    negative = text.startswith("(") and text.endswith(")")
    match = _AMOUNT_RE.match(text)
    if not match:
        return None

    number = match.group(1).replace(",", "")
    try:
        amount = float(number)
    except ValueError:
        return None

    # A Dr/Cr suffix is direction, not sign — callers interpret it. Only parenthesised
    # values are negative here.
    return -abs(amount) if negative else amount


def _normalize_header(text) -> str:
    return re.sub(r"[\s_]+", " ", str(text or "")).strip().lower()


def parse_dates(series: pd.Series) -> pd.Series:
    """Parse a column of Indian-format dates, picking whichever format parses the most."""
    text = series.astype("string").str.strip()
    # Statements often print the time under the date, so the cell arrives as
    # "25 Aug 2026 08:35 PM". Drop a trailing clock time before matching formats.
    text = text.str.replace(
        r"[\s,]+\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp]\.?[Mm]\.?)?\s*$", "", regex=True
    ).str.strip()
    best: pd.Series | None = None
    best_hits = -1

    for fmt in DATE_FORMATS:
        parsed = pd.to_datetime(text, format=fmt, errors="coerce")
        hits = int(parsed.notna().sum())
        if hits > best_hits:
            best, best_hits = parsed, hits
        if hits == len(text):
            return parsed

    # Nothing matched cleanly — fall back to mixed inference with day-first bias.
    if best_hits < len(text) * 0.6:
        loose = pd.to_datetime(text, dayfirst=True, format="mixed", errors="coerce")
        if int(loose.notna().sum()) > best_hits:
            return loose
    return best if best is not None else pd.to_datetime(text, errors="coerce")


# --------------------------------------------------------------------------------------
# Header detection and column mapping
# --------------------------------------------------------------------------------------

def _synonym_pattern(synonym: str) -> re.Pattern:
    """Word-boundary matcher for a header synonym, tolerant of spacing and punctuation.

    Boundaries matter: a bare ``dr`` must not match inside ``withdrawal``, and must not
    claim a ``Dr / Cr`` indicator column as the debit column.
    """
    body = r"[\s./()-]*".join(re.escape(word) for word in synonym.split())
    return re.compile(rf"(?<![a-z]){body}(?![a-z])")


_SYNONYM_PATTERNS: dict[str, list[re.Pattern]] = {
    field_name: [_synonym_pattern(s) for s in synonyms]
    for field_name, synonyms in COLUMN_SYNONYMS.items()
}

# "Dr / Cr", "Cr/Dr", "Debit/Credit", "Type" — a direction flag, never an amount column.
_INDICATOR_RE = re.compile(
    r"(?<![a-z])(dr\s*[/|-]\s*cr|cr\s*[/|-]\s*dr|debit\s*[/|-]\s*credit)(?![a-z])"
    r"|indicator|(?<![a-z])txn\s*type(?![a-z])",
    re.IGNORECASE,
)


def map_columns(header_cells: list) -> dict[str, int]:
    """Map our canonical field names onto column indices of a header row."""
    normalized = [_normalize_header(cell) for cell in header_cells]
    mapping: dict[str, int] = {}
    used: set[int] = set()

    # Claim the Dr/Cr indicator first — otherwise a "Dr / Cr" header gets swallowed by
    # the bare "dr" debit synonym and the amount column is never interpreted.
    for index, cell in enumerate(normalized):
        if cell and _INDICATOR_RE.search(cell):
            mapping["drcr"] = index
            used.add(index)
            break

    for field_name, patterns in _SYNONYM_PATTERNS.items():
        if field_name in mapping:
            continue
        for pattern in patterns:  # ordered most-specific synonym first
            for index, cell in enumerate(normalized):
                if not cell or index in used:
                    continue
                if _HEADER_BLOCKLIST.search(cell):
                    continue
                if pattern.search(cell):
                    mapping[field_name] = index
                    used.add(index)
                    break
            if field_name in mapping:
                break
    return mapping


def _score_header(mapping: dict[str, int]) -> int:
    """How much of a real transaction header this row looks like."""
    if "date" not in mapping or "description" not in mapping:
        return 0
    score = 2
    if "debit" in mapping and "credit" in mapping:
        score += 2
    elif "amount" in mapping:
        score += 1
    if "balance" in mapping:
        score += 1
    return score


def find_header_row(grid: pd.DataFrame, scan_rows: int = 40) -> tuple[int, dict[str, int]]:
    """Locate the real header row inside a raw grid that may have preamble junk on top."""
    best_index, best_mapping, best_score = -1, {}, 0

    for index in range(min(scan_rows, len(grid))):
        cells = grid.iloc[index].tolist()
        mapping = map_columns(cells)
        score = _score_header(mapping)
        if score > best_score:
            best_index, best_mapping, best_score = index, mapping, score

    if best_score < 3:
        raise StatementParseError(
            "Could not find a transaction header row. Expected columns like "
            "Date / Narration / Withdrawal / Deposit / Balance."
        )
    return best_index, best_mapping


# --------------------------------------------------------------------------------------
# Frame assembly
# --------------------------------------------------------------------------------------

_FLAG_RE = re.compile(
    r"^\s*(dr|cr|d|c|debit|credit|withdrawal|deposit|wdl|dep)\s*$", re.IGNORECASE
)


def _refine_amount_columns(grid: pd.DataFrame, mapping: dict[str, int]) -> dict[str, int]:
    """Decide from the DATA whether a Dr/Cr-looking header is a flag or an amount.

    Kotak heads its single signed-amount column ``DEBIT/CREDIT()`` and fills it with
    ``-216.07`` / ``+164.00``. ICICI heads a genuine flag column ``Dr / Cr`` and fills it
    with ``DR`` / ``CR``. The headers are nearly identical, so the header alone cannot
    tell them apart — the values can.
    """
    index = mapping.get("drcr")
    if index is None or index >= grid.shape[1]:
        return mapping

    values = [
        text for text in (str(cell).strip() for cell in grid.iloc[:, index])
        if text and text.lower() not in {"nan", "none"}
    ][:80]
    if not values:
        return mapping

    numeric = sum(1 for text in values if clean_amount(text) is not None)
    flags = sum(1 for text in values if _FLAG_RE.match(text))
    if numeric <= flags or numeric < max(3, len(values) * 0.6):
        return mapping  # a real Dr/Cr flag column

    refined = dict(mapping)
    refined.pop("drcr")
    if "debit" not in refined and "credit" not in refined and "amount" not in refined:
        refined["amount"] = index
    return refined


def _assemble(grid: pd.DataFrame, mapping: dict[str, int], source: str,
              report: ParseReport) -> pd.DataFrame:
    """Build the normalized frame from a raw grid plus a column mapping."""
    mapping = _refine_amount_columns(grid, mapping)
    take = lambda key: grid.iloc[:, mapping[key]] if key in mapping else None

    frame = pd.DataFrame()
    frame["date"] = parse_dates(take("date"))
    frame["description"] = (
        take("description").astype("string").fillna("").str.replace(r"\s+", " ", regex=True)
        .str.strip()
    )

    debit_col, credit_col = take("debit"), take("credit")
    amount_col, drcr_col = take("amount"), take("drcr")
    balance_col = take("balance")

    if debit_col is not None or credit_col is not None:
        # Layout (a): separate withdrawal / deposit columns.
        frame["debit"] = _to_amounts(debit_col, len(frame))
        frame["credit"] = _to_amounts(credit_col, len(frame))
        layout = "debit/credit columns"
    elif amount_col is not None and drcr_col is not None:
        # Layout (b): one amount column plus a Dr/Cr indicator.
        amounts = _to_amounts(amount_col, len(frame))
        flags = drcr_col.astype("string").fillna("").str.strip().str.lower()
        is_debit = flags.str.startswith(("d", "w")) | flags.str.contains("debit|withdraw")
        frame["debit"] = amounts.where(is_debit, 0.0)
        frame["credit"] = amounts.where(~is_debit, 0.0)
        layout = "amount + Dr/Cr indicator"
    elif amount_col is not None:
        # Layout (c): amount only. Sign comes from the balance movement (resolved below),
        # or from a signed amount if the statement provides one.
        amounts = _to_amounts(amount_col, len(frame), keep_sign=True)
        frame["debit"] = (-amounts).clip(lower=0.0)
        frame["credit"] = amounts.clip(lower=0.0)
        layout = "signed amount column"
    else:
        raise StatementParseError(
            "Found a header but no amount columns (Withdrawal/Deposit, or Amount)."
        )

    frame["balance"] = (
        _to_amounts(balance_col, len(frame), keep_sign=True, blank_as_nan=True)
        if balance_col is not None
        else pd.Series([float("nan")] * len(frame))
    )
    frame["source_file"] = source
    report.strategy = f"{report.strategy} · {layout}" if report.strategy else layout

    frame = _drop_non_transaction_rows(frame)
    frame = _ensure_chronological(frame.reset_index(drop=True), report)
    frame = _infer_direction_from_balance(frame, report)
    return frame.reset_index(drop=True)


def _ensure_chronological(frame: pd.DataFrame, report: ParseReport | None = None
                          ) -> pd.DataFrame:
    """Put rows in oldest-first order, reversing a newest-first statement.

    Kotak, and plenty of other banks, list the most recent transaction first. Both the
    balance reconciliation and the debit/credit inference walk the running balance
    forward, so they need true chronological order — on a reversed statement every
    balance delta comes out backwards. Reversing the rows (rather than sorting by date)
    is what preserves the correct sequence of several transactions on the same day.
    """
    if len(frame) < 3 or "date" not in frame.columns:
        return frame

    steps = frame["date"].diff().dt.total_seconds().dropna()
    if steps.empty:
        return frame

    forward = int((steps > 0).sum())
    backward = int((steps < 0).sum())
    if backward <= forward:
        return frame

    if report is not None:
        report.strategy = f"{report.strategy} · newest-first, reversed".lstrip(" ·")
    return frame.iloc[::-1].reset_index(drop=True)


def _to_amounts(column, length: int, keep_sign: bool = False,
                blank_as_nan: bool = False) -> pd.Series:
    """Clean a column of money strings into floats."""
    if column is None:
        return pd.Series([float("nan") if blank_as_nan else 0.0] * length, dtype="float64")
    values = [clean_amount(v) for v in column]
    fill = float("nan") if blank_as_nan else 0.0
    cleaned = [fill if v is None else (v if keep_sign else abs(v)) for v in values]
    return pd.Series(cleaned, dtype="float64")


def _drop_non_transaction_rows(frame: pd.DataFrame) -> pd.DataFrame:
    """Remove totals, page footers, and any row without a usable date."""
    keep = frame["date"].notna()
    # Summary rows ("Total", "Opening Balance", "Statement Summary") rarely carry a date,
    # but drop them explicitly in case they do.
    junk = frame["description"].astype("string").fillna("").str.strip().str.lower()
    keep &= ~junk.str.match(
        r"^(total|grand total|opening balance|closing balance|b/?f|c/?f|"
        r"statement summary|page \d+)"
    )
    # A row with no money movement and no balance is noise.
    keep &= (frame["debit"].fillna(0) != 0) | (frame["credit"].fillna(0) != 0) | frame["balance"].notna()
    return frame[keep]


def _infer_direction_from_balance(frame: pd.DataFrame, report: ParseReport) -> pd.DataFrame:
    """For layouts that lost the debit/credit distinction, recover it from the balance."""
    if frame.empty or frame["balance"].isna().all():
        return frame

    amounts = frame["debit"].fillna(0.0) + frame["credit"].fillna(0.0)
    ambiguous = (frame["debit"].fillna(0.0) == 0.0) & (frame["credit"].fillna(0.0) == 0.0)
    if ambiguous.all():
        return frame

    balance = frame["balance"]
    debits, credits = [], []
    previous = float("nan")
    for position in range(len(frame)):
        amount = float(amounts.iloc[position])
        current = balance.iloc[position]
        debit = float(frame["debit"].iloc[position] or 0.0)
        credit = float(frame["credit"].iloc[position] or 0.0)

        if (debit or credit) or pd.isna(current) or pd.isna(previous) or amount == 0.0:
            debits.append(debit)
            credits.append(credit)
        else:
            delta = float(current) - float(previous)
            if delta < 0:
                debits.append(amount)
                credits.append(0.0)
            else:
                debits.append(0.0)
                credits.append(amount)
        previous = current if pd.notna(current) else previous

    frame = frame.copy()
    frame["debit"] = debits
    frame["credit"] = credits
    return frame


# --------------------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------------------

def reconcile(frame: pd.DataFrame, report: ParseReport) -> None:
    """Verify each row's balance movement against its debit/credit. Fills the report."""
    report.rows_total = len(frame)
    report.has_balance = bool(len(frame)) and frame["balance"].notna().sum() >= 2

    if not report.has_balance:
        report.rows_reconciled = 0
        report.unreconciled_rows = []
        if len(frame):
            report.warnings.append(
                "No balance column found — totals cannot be cross-checked against the statement."
            )
        return

    balance = frame["balance"]
    expected = frame["credit"].fillna(0.0) - frame["debit"].fillna(0.0)
    actual = balance.diff()

    # Row 0 has no predecessor: treat it as reconciled if a balance exists at all.
    matches = (actual - expected).abs() <= _TOLERANCE
    matches.iloc[0] = pd.notna(balance.iloc[0])
    # Rows where either balance is missing cannot be checked; don't count them as failures.
    unknown = balance.isna() | balance.shift().isna()
    unknown.iloc[0] = False
    matches = matches | unknown

    report.rows_reconciled = int(matches.sum())
    report.unreconciled_rows = [int(i) for i in frame.index[~matches].tolist()]


# --------------------------------------------------------------------------------------
# CSV / Excel
# --------------------------------------------------------------------------------------

def _read_grid(path_or_buffer, extension: str) -> pd.DataFrame:
    """Read a spreadsheet as a raw, header-less grid of strings."""
    if extension == ".csv":
        return _read_csv_grid(path_or_buffer)

    _rewind(path_or_buffer)
    engine = "xlrd" if extension == ".xls" else "openpyxl"
    return pd.read_excel(path_or_buffer, header=None, dtype=str, engine=engine)


def _read_csv_grid(path_or_buffer) -> pd.DataFrame:
    """Read a delimited text statement losslessly.

    Uses the stdlib csv reader rather than ``pd.read_csv`` because bank exports are
    ragged — preamble rows have a different field count from the transaction rows, and
    pandas either raises or (with ``on_bad_lines="skip"``) silently discards the entire
    transaction table. Nothing may be dropped here: a row lost at this stage is a
    transaction missing from the dashboard with no error anywhere.
    """
    raw = _read_bytes(path_or_buffer)

    text = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise StatementParseError("Could not decode the CSV file.")

    delimiter = _sniff_delimiter(text)
    rows = [row for row in csv.reader(io.StringIO(text), delimiter=delimiter)]
    if not rows:
        raise StatementParseError("The CSV file is empty.")

    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    return pd.DataFrame(padded, dtype="object")


def _sniff_delimiter(text: str) -> str:
    """Pick the delimiter by counting candidates in the densest lines."""
    sample = [line for line in text.splitlines()[:80] if line.strip()]
    if not sample:
        return ","
    best, best_count = ",", 0
    for candidate in (",", "\t", ";", "|"):
        counts = [line.count(candidate) for line in sample]
        # A real delimiter appears consistently on many lines, not once on one.
        score = sum(1 for count in counts if count >= 3)
        if score > best_count:
            best, best_count = candidate, score
    return best


def _read_bytes(path_or_buffer) -> bytes:
    if isinstance(path_or_buffer, (str, os.PathLike)):
        with open(path_or_buffer, "rb") as handle:
            return handle.read()
    _rewind(path_or_buffer)
    data = path_or_buffer.read()
    _rewind(path_or_buffer)
    return data if isinstance(data, bytes) else str(data).encode("utf-8")


def _rewind(target) -> None:
    if hasattr(target, "seek"):
        try:
            target.seek(0)
        except (OSError, ValueError):
            pass


def _load_spreadsheet(path_or_buffer, extension: str, source: str) -> tuple[pd.DataFrame, ParseReport]:
    report = ParseReport(source_file=source, strategy="spreadsheet")
    grid = _read_grid(path_or_buffer, extension)
    header_index, mapping = find_header_row(grid)
    body = grid.iloc[header_index + 1:].reset_index(drop=True)
    frame = _assemble(body, mapping, source, report)
    return frame, report


# --------------------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------------------

def _is_password_error(error: BaseException) -> bool:
    """Does this exception mean 'wrong or missing PDF password'?

    pdfminer signals this several ways depending on version and encryption revision —
    a typed ``PDFPasswordIncorrect``, or a bare ``PdfminerException`` with an empty
    message — so check the class hierarchy by name as well as the text.
    """
    names = {klass.__name__ for klass in type(error).__mro__}
    if "PDFPasswordIncorrect" in names or "PDFEncryptionError" in names:
        return True
    message = str(error).lower()
    return "password" in message or "encrypt" in message


def _open_pdf(path_or_buffer, password: str | None):
    try:
        return pdfplumber.open(path_or_buffer, password=password or "")
    except Exception as error:
        if _is_password_error(error) or _pdf_is_encrypted(path_or_buffer):
            raise StatementPasswordError(
                "This PDF is password protected and the password given did not open it. "
                "Indian banks usually use something like PAN + date of birth, or the "
                "first four letters of your name plus your date of birth."
            ) from error
        raise


def _pdf_is_encrypted(path_or_buffer) -> bool:
    """Cheap structural check for an /Encrypt dictionary in the PDF trailer."""
    try:
        raw = _read_bytes(path_or_buffer)
    except Exception:
        return False
    return b"/Encrypt" in raw


def _pdf_tables(pdf) -> list[list[list]]:
    rows: list[list] = []
    for page in pdf.pages:
        for table in page.extract_tables() or []:
            for row in table:
                if any(cell not in (None, "") for cell in row):
                    rows.append([(cell or "").replace("\n", " ").strip() for cell in row])
    return rows


def _load_pdf_via_tables(pdf, source: str) -> tuple[pd.DataFrame, ParseReport] | None:
    rows = _pdf_tables(pdf)
    if len(rows) < 3:
        return None

    width = max(len(row) for row in rows)
    grid = pd.DataFrame([row + [""] * (width - len(row)) for row in rows], dtype="object")

    try:
        header_index, mapping = find_header_row(grid)
    except StatementParseError:
        return None

    report = ParseReport(source_file=source, strategy="PDF table extraction")
    body = grid.iloc[header_index + 1:].reset_index(drop=True)
    # Repeated headers on later pages become ordinary rows; drop them.
    body = body[~body.apply(lambda r: _score_header(map_columns(r.tolist())) >= 3, axis=1)]
    try:
        frame = _assemble(body.reset_index(drop=True), mapping, source, report)
    except StatementParseError:
        # Finding a header but no usable amounts means this strategy did not work —
        # a borderless statement yields a header block and almost no body. Give up on
        # this strategy rather than aborting the whole parse, so the others still run.
        return None
    return (frame, report) if len(frame) >= 2 else None


# --------------------------------------------------------------------------------------
# PDF column-band strategy
# --------------------------------------------------------------------------------------

def _visual_lines(page, tolerance: float = 3.0) -> list[list[dict]]:
    """Group a page's words into visual lines by vertical position.

    Clustering on proximity rather than bucketing matters: a right-aligned balance can
    sit a fraction of a point below the rest of its row, and a fixed bucket would split
    it onto a line of its own.
    """
    words = sorted(page.extract_words(), key=lambda w: (w["top"], w["x0"]))
    lines: list[list[dict]] = []
    current: list[dict] = []
    anchor: float | None = None

    for word in words:
        if anchor is not None and abs(word["top"] - anchor) > tolerance:
            lines.append(sorted(current, key=lambda w: w["x0"]))
            current, anchor = [], None
        if anchor is None:
            anchor = word["top"]
        current.append(word)

    if current:
        lines.append(sorted(current, key=lambda w: w["x0"]))
    return lines


def _header_cells(line: list[dict], gap: float = 8.0) -> tuple[list[str], list[float]]:
    """Merge header words into column cells and return their texts plus x boundaries."""
    cells: list[dict] = []
    for word in line:
        if cells and word["x0"] - cells[-1]["x1"] <= gap:
            cells[-1]["text"] += " " + word["text"]
            cells[-1]["x1"] = word["x1"]
        else:
            cells.append({"text": word["text"], "x0": word["x0"], "x1": word["x1"]})

    texts = [cell["text"] for cell in cells]
    bounds = [(left["x1"] + right["x0"]) / 2 for left, right in zip(cells, cells[1:])]
    return texts, bounds


def _starts_a_row(text: str) -> bool:
    """Does this cell begin a new transaction — i.e. does it lead with a date?"""
    return bool(_LEADING_DATE_RE.match(text.strip() + " "))


def _load_pdf_via_columns(pdf, source: str) -> tuple[pd.DataFrame, ParseReport] | None:
    """Rebuild the table from word x-positions when the PDF has no ruled lines.

    Many statements — Kotak's among them — draw no table borders, so pdfplumber's table
    extractor finds nothing. But the columns are still there in the geometry. This reads
    the header's word positions to derive column bands, then assigns every later word to
    a band by its left edge.

    The important part is row assembly: one transaction routinely spans several physical
    lines (the time prints under the date, the narration wraps, the balance drifts half a
    point). A line whose date column starts with a date begins a new transaction;
    anything else is a continuation and merges into the row above.
    """
    report = ParseReport(source_file=source, strategy="PDF column layout")
    records: list[list[str]] = []
    columns: list[str] | None = None
    bounds: list[float] = []
    mapping: dict[str, int] = {}

    for page in pdf.pages:
        lines = _visual_lines(page)
        start = 0

        for index, line in enumerate(lines):
            candidate_texts, candidate_bounds = _header_cells(line)
            candidate_mapping = map_columns(candidate_texts)
            if _score_header(candidate_mapping) >= 3:
                columns, bounds, mapping = candidate_texts, candidate_bounds, candidate_mapping
                start = index + 1
                break
        else:
            if columns is None:
                continue  # no header seen yet on any page

        for line in lines[start:]:
            cells = [[] for _ in columns]
            for word in line:
                # Assign by the word's LEFT edge. Left-aligned narrations that overflow
                # their column still land correctly, and right-aligned amounts still
                # start inside their own band.
                position = bisect.bisect_right(bounds, word["x0"])
                if position < len(cells):
                    cells[position].append(word["text"])

            joined = [" ".join(cell) for cell in cells]
            if not any(joined):
                continue

            whole_line = " ".join(joined).strip()
            if _looks_like_boilerplate(whole_line):
                continue

            if _starts_a_row(joined[mapping["date"]]):
                records.append(joined)
            elif records:
                for position, text in enumerate(joined):
                    if text:
                        records[-1][position] = (records[-1][position] + " " + text).strip()

    if columns is None or len(records) < 2:
        return None

    grid = pd.DataFrame(records, dtype="object")
    frame = _assemble(grid, mapping, source, report)
    return (frame, report) if len(frame) >= 2 else None


def _split_trailing_amounts(line: str) -> tuple[str, list[tuple[float, str | None]]]:
    """Peel money tokens off the end of a line. Returns (remaining text, amounts L→R)."""
    remainder = line.rstrip()
    found: list[tuple[float, str | None]] = []

    while len(found) < 4:
        match = _TRAILING_AMOUNT_RE.search(remainder)
        if not match:
            break
        value = clean_amount(match.group(1))
        if value is None:
            break
        suffix = (match.group(2) or "").upper() or None
        found.append((value, suffix))
        remainder = remainder[: match.start()].rstrip()

    found.reverse()
    return remainder, found


def _load_pdf_via_text(pdf, source: str) -> tuple[pd.DataFrame, ParseReport]:
    report = ParseReport(source_file=source, strategy="PDF text layout")
    records: list[dict] = []

    for page in pdf.pages:
        text = page.extract_text() or ""
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            date_match = _LEADING_DATE_RE.match(line)
            if not date_match:
                # Continuation of the previous narration — very common in HDFC/SBI PDFs.
                if records:
                    body, amounts = _split_trailing_amounts(line)
                    if not amounts and len(body) > 2 and not _looks_like_boilerplate(body):
                        records[-1]["description"] += " " + body
                continue

            rest = line[date_match.end():]
            body, amounts = _split_trailing_amounts(rest)
            if not amounts:
                continue

            body = _strip_value_date(body, date_match.group(1))

            balance = amounts[-1][0] if len(amounts) >= 2 else float("nan")
            candidates = amounts[:-1] if len(amounts) >= 2 else amounts
            non_zero = [a for a in candidates if a[0] != 0.0]
            amount, suffix = (non_zero[-1] if non_zero else candidates[-1])

            records.append({
                "date_text": date_match.group(1),
                "description": body,
                "amount": amount,
                "suffix": suffix,
                "balance": balance,
            })

    if len(records) < 2:
        raise StatementParseError(
            "No transaction rows found in this PDF. If it is a scanned image, this tool "
            "cannot read it (OCR is not enabled)."
        )

    frame = pd.DataFrame(records)
    frame["date"] = parse_dates(frame["date_text"])
    frame["description"] = (
        frame["description"].astype("string").str.replace(r"\s+", " ", regex=True).str.strip()
    )
    # Direction: honour an explicit Cr/Dr suffix, else let the balance decide.
    frame["debit"] = 0.0
    frame["credit"] = 0.0
    suffixes = frame["suffix"].astype("string").fillna("").str.upper()
    explicit_credit = (suffixes == "CR").fillna(False).astype(bool)
    explicit_debit = (suffixes == "DR").fillna(False).astype(bool)
    frame.loc[explicit_credit, "credit"] = frame.loc[explicit_credit, "amount"]
    frame.loc[explicit_debit, "debit"] = frame.loc[explicit_debit, "amount"]

    unresolved = ~(explicit_credit | explicit_debit)
    frame.loc[unresolved, "debit"] = frame.loc[unresolved, "amount"]  # placeholder magnitude
    frame.loc[unresolved, "credit"] = 0.0
    frame["_ambiguous"] = unresolved

    frame = frame[["date", "description", "debit", "credit", "balance", "_ambiguous"]]
    frame = frame[frame["date"].notna()].reset_index(drop=True)
    frame = _ensure_chronological(frame, report)
    frame = _direction_from_balance_walk(frame)
    frame["source_file"] = source
    return frame.drop(columns=["_ambiguous"]), report


def _direction_from_balance_walk(frame: pd.DataFrame) -> pd.DataFrame:
    """Assign debit vs credit by walking the running balance forward."""
    if frame.empty:
        return frame

    debits, credits = [], []
    previous = float("nan")
    for position in range(len(frame)):
        amount = float(frame["debit"].iloc[position]) + float(frame["credit"].iloc[position])
        current = frame["balance"].iloc[position]
        ambiguous = bool(frame["_ambiguous"].iloc[position])

        if not ambiguous or pd.isna(current) or pd.isna(previous):
            debits.append(float(frame["debit"].iloc[position]))
            credits.append(float(frame["credit"].iloc[position]))
        else:
            delta = float(current) - float(previous)
            if abs(delta + amount) <= _TOLERANCE:      # balance fell by `amount`
                debits.append(amount)
                credits.append(0.0)
            elif abs(delta - amount) <= _TOLERANCE:    # balance rose by `amount`
                debits.append(0.0)
                credits.append(amount)
            elif delta < 0:
                debits.append(amount)
                credits.append(0.0)
            else:
                debits.append(0.0)
                credits.append(amount)
        previous = current if pd.notna(current) else previous

    frame = frame.copy()
    frame["debit"] = debits
    frame["credit"] = credits
    return frame


_TRAILING_DATE_RE = re.compile(r"\s+(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4})\s*$")


def _strip_value_date(body: str, txn_date_text: str) -> str:
    """Drop a trailing value-date column, but only when it really is one.

    Many statements print the value date right after the narration. Blindly removing a
    trailing date also eats real content — ``INT.PD:01-01-2025 TO 31-03-2025`` ends in
    something that looks exactly like a value date. So only strip when the trailing date
    resolves to the same day as the transaction date, which is what a genuine value-date
    column does on the overwhelming majority of rows.
    """
    match = _TRAILING_DATE_RE.search(body)
    if not match:
        return body.strip()

    parsed = parse_dates(pd.Series([match.group(1), txn_date_text]))
    if parsed.notna().all() and parsed.iloc[0] == parsed.iloc[1]:
        return body[: match.start()].strip()
    return body.strip()


def _looks_like_boilerplate(text: str) -> bool:
    lowered = text.lower()
    return bool(re.search(
        r"statement of account|page \d+|registered office|customer id|ifsc|micr|"
        r"account (?:no|number)|opening balance|closing balance|this is a computer",
        lowered,
    ))


def _load_pdf(path_or_buffer, password: str | None,
              source: str) -> tuple[pd.DataFrame, ParseReport]:
    """Try every PDF strategy and keep the one whose numbers actually add up.

    No single strategy wins on all banks: ruled tables work where borders exist, column
    bands work where they don't, and the simple line scan handles the rest. Rather than
    guess from the layout, run them and let the statement's own running balance decide —
    the reconciliation rate is an objective score, so the best parse wins on evidence.
    A strategy that raises is just one that scored nothing.
    """
    strategies = (
        ("tables", _load_pdf_via_tables),
        ("columns", _load_pdf_via_columns),
        ("text", _load_pdf_via_text),
    )

    best: tuple[pd.DataFrame, ParseReport] | None = None
    best_score = (-1.0, -1)
    first_error: Exception | None = None

    with _open_pdf(path_or_buffer, password) as pdf:
        for _, strategy in strategies:
            try:
                result = strategy(pdf, source)
            except StatementParseError as error:
                first_error = first_error or error
                continue
            if result is None:
                continue

            frame, report = result
            probe = ParseReport()
            reconcile(frame, probe)
            # Rank on reconciliation first, then on how many rows were recovered: a
            # strategy that finds 3 perfect rows out of 53 must not beat one that finds
            # all 53.
            score = (probe.reconciliation_rate if probe.has_balance else 0.0, len(frame))
            if score > best_score:
                best, best_score = (frame, report), score
            if probe.has_balance and probe.reconciled and len(frame) >= 2:
                break  # nothing can beat a fully reconciled parse

    if best is not None:
        return best
    if first_error is not None:
        raise first_error
    raise StatementParseError(
        "No transaction rows found in this PDF. If it is a scanned image, this tool "
        "cannot read it (OCR is not enabled)."
    )


# --------------------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------------------

def load_transactions(source, password: str | None = None,
                      filename: str | None = None) -> tuple[pd.DataFrame, ParseReport]:
    """Parse a statement into the normalized schema plus a :class:`ParseReport`.

    ``source`` may be a filesystem path or a file-like object (e.g. a Streamlit upload).
    ``filename`` supplies the extension when ``source`` is a buffer without a name.
    """
    name = filename or getattr(source, "name", None) or (
        os.path.basename(source) if isinstance(source, (str, os.PathLike)) else "statement"
    )
    extension = os.path.splitext(str(name))[1].lower()

    if isinstance(source, bytes):
        source = io.BytesIO(source)

    if extension == ".pdf":
        frame, report = _load_pdf(source, password, str(name))
    elif extension in {".csv", ".txt"}:
        frame, report = _load_spreadsheet(source, ".csv", str(name))
    elif extension in {".xlsx", ".xlsm", ".xls"}:
        frame, report = _load_spreadsheet(source, extension, str(name))
    else:
        raise StatementParseError(
            f"Unsupported file type '{extension or name}'. Use PDF, CSV, XLS or XLSX."
        )

    frame = _finalize(frame)
    reconcile(frame, report)
    return frame, report


def _finalize(frame: pd.DataFrame) -> pd.DataFrame:
    """Order, type, and de-duplicate the normalized frame."""
    columns = ["date", "description", "debit", "credit", "balance", "source_file"]
    for column in columns:
        if column not in frame.columns:
            frame[column] = float("nan") if column == "balance" else ""

    frame = frame[columns].copy()
    frame["debit"] = pd.to_numeric(frame["debit"], errors="coerce").fillna(0.0).abs()
    frame["credit"] = pd.to_numeric(frame["credit"], errors="coerce").fillna(0.0).abs()
    frame["balance"] = pd.to_numeric(frame["balance"], errors="coerce")
    frame["description"] = frame["description"].astype(str).str.strip()

    frame = frame[(frame["debit"] > 0) | (frame["credit"] > 0)]
    frame = frame.drop_duplicates(subset=["date", "description", "debit", "credit", "balance"])
    return frame.reset_index(drop=True)


def combine(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Merge statements from several files, dropping rows that overlap between them."""
    if not frames:
        return pd.DataFrame(
            columns=["date", "description", "debit", "credit", "balance", "source_file"]
        )
    merged = pd.concat(frames, ignore_index=True)
    merged = merged.drop_duplicates(subset=["date", "description", "debit", "credit", "balance"])
    return merged.sort_values("date").reset_index(drop=True)
