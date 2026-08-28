"""Turn a bank narration string into a merchant, a channel, and a stable lookup key.

Indian narrations are structured channel strings, not free text:

    UPI/DR/412345678901/SWIGGY/YESB/swiggy@ybl/Payment
    POS 4512XXXXXXXX1234 AMAZON PAY INDIA PRIVATE
    NEFT-CITIN0012345678-ACME PRIVATE LIMITED-SALARY

The job here is to throw away the plumbing (reference numbers, IFSC codes, masked card
numbers, bank codes, month stamps) and keep the part a human would call "who I paid".
Getting this right is what makes the rule dictionary in ``rules.yaml`` small and the
categoriser accurate — every merchant must collapse to one key across every month.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --------------------------------------------------------------------------------------
# Channels
# --------------------------------------------------------------------------------------

CHANNELS = [
    "UPI", "Card", "NEFT", "IMPS", "RTGS", "Auto-debit", "ATM", "Cheque",
    "Interest", "Charges", "Transfer", "Other",
]

# Ordered: the first pattern that matches wins.
_CHANNEL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("Interest", re.compile(r"\bINT\.?\s*PD\b|\bCREDIT\s+INTEREST\b|\bINTEREST\s+(?:PAID|CREDIT)"
                            r"|\bSAVINGS?\s+INTEREST\b", re.I)),
    ("Charges",  re.compile(r"\bAMB\s*CHRG\b|\bCHRG(?:S|ES)?\b|\bCHARGES?\b|\bGST\b|\bSMS\s*CHG"
                            r"|\bANNUAL\s+FEE\b|\bPENAL\b|\bLATE\s+FEE\b|\bBOUNCE\b"
                            r"|\bNON\s*MAINTAIN", re.I)),
    ("ATM",      re.compile(r"^\s*(?:ATW|ATM|NWD|EAW)\b|\bCASH\s+WITHDRAWAL\b|\bATM\s+CASH\b"
                            r"|\bCASH\s+WDL\b", re.I)),
    ("UPI",      re.compile(r"^\s*UPI\b|\bUPI[/-]|\b(?:MOB|MMT)/(?:IMPS|UPI)\b|@(?:ybl|okaxis|"
                            r"oksbi|okhdfcbank|okicici|paytm|apl|ibl|axl)\b", re.I)),
    ("Cheque",   re.compile(r"\bCHQ\b|\bCHEQUE\b|\bCLG\b|\bINWARD\s+CLEARING\b", re.I)),
    ("Auto-debit", re.compile(r"^\s*(?:ACH|NACH|ECS)\b|\bACH\s*[DC]\b|\bNACH\b|\bE-?MANDATE\b"
                              r"|^\s*SI[-/ ]", re.I)),
    ("Card",     re.compile(r"^\s*(?:POS|ECOM|IPS|VPS)\b|\bPOS\s+\d|\bCARD\s+(?:PURCHASE|PAYMENT)"
                            r"|\bMERCHANT\s+PURCHASE\b", re.I)),
    ("NEFT",     re.compile(r"\bNEFT\b", re.I)),
    ("RTGS",     re.compile(r"\bRTGS\b", re.I)),
    ("IMPS",     re.compile(r"\bIMPS\b", re.I)),
    ("Transfer", re.compile(r"\bTRANSFER\s+(?:TO|FROM)\b|\bFUND\s+TRANSFER\b|\bSELF\b"
                            r"|\bTPT\b|\bBY\s+TRANSFER\b", re.I)),
]

# --------------------------------------------------------------------------------------
# Token filters
# --------------------------------------------------------------------------------------

# Channel/plumbing words that are never a merchant name on their own.
_NOISE_WORDS = {
    "UPI", "DR", "CR", "POS", "NEFT", "IMPS", "RTGS", "ACH", "NACH", "ECS", "SI", "EMI",
    "CHQ", "CLG", "MMT", "MOB", "TPT", "ATW", "ATM", "NWD", "EAW", "ECOM", "IPS", "VPS",
    "PAYMENT", "PAYMENTS", "PAY", "PMT", "TXN", "TRANSACTION", "TRF", "TRANSFER", "REF",
    "REFNO", "RRN", "UTR", "COLLECT", "REQUEST", "SENT", "RECEIVED", "TO", "FROM", "BY",
    "THE", "AND", "FOR", "VIA", "INR", "RS", "AC", "ACC", "ACCT", "ACCOUNT", "NO", "NA",
    "MERCHANT", "PURCHASE", "ONLINE", "MOBILE", "BANKING", "NETBANKING", "IB", "IMB",
    "SERVICE", "SERVICES", "LTD", "LIMITED", "PVT", "PRIVATE", "INDIA", "IN", "OTHERS",
    "SUCCESS", "SUCCESSFUL", "DEBIT", "CREDIT", "WITHDRAWAL", "DEPOSIT", "AMT", "AMOUNT",
    "FOLIO", "MANDATE", "AUTOPAY", "AUTO", "BILL", "BILLDESK", "BBPS", "RECHARGE",
    # Trailing free-text notes that UPI apps append ("You are paying", "Sent using
    # PhonePe", "Paid via Superpay", "MandateExecution"). Without these the note
    # wins the longest-phrase tiebreak and becomes the merchant name.
    "YOU", "ARE", "PAYI", "PAYIN", "PAYING", "SENT", "USING", "RECD", "RECEIVED",
    "REV", "REVERSAL", "EXECUTION", "MANDATEEXECU", "MANDATEEXEC", "SUBSCRIPTION",
    "SUBSCRIPTI", "COMPLAIN", "COMPLAINT", "PAID", "SUP", "RAZO", "RAZORPAY",
    "MB", "UPI",
}

# Bank / PSP short codes that ride along in UPI and IMPS strings.
_BANK_CODES = {
    "HDFC", "HDFCBANK", "ICIC", "ICICI", "SBI", "SBIN", "AXIS", "AXISBANK", "UTIB",
    "KOTAK", "KKBK", "YESB", "YES", "IDFC", "IDFB", "INDB", "INDUSIND", "PNB", "PUNB",
    "BARB", "BOB", "CNRB", "CANARA", "UBIN", "UNION", "IOBA", "IDIB", "CITI", "CITIN",
    "SCBL", "HSBC", "DBSS", "RATN", "RBL", "AUBL", "FDRL", "FEDERAL", "KVBL", "TMBL",
    "PYTM", "PAYTMBANK", "APL", "IBL", "AXL", "OKAXIS", "OKSBI", "OKHDFCBANK", "OKICICI",
    "YBL", "NPCI", "BANK",
}

_IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
_MASKED_CARD_RE = re.compile(r"^\d{0,6}X{2,}\d{0,6}$", re.I)
_MONTH_STAMP_RE = re.compile(
    r"^(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[-/]?\d{2,4}$", re.I
)
_DATE_LIKE_RE = re.compile(r"^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}$")
_VPA_RE = re.compile(r"\b([A-Za-z0-9_.\-]{2,})@([A-Za-z]{2,})\b")

# "UPI/Amazon Pay/YESB/6603.../You are payi" and "REV-UPI/1 GGN SGT GH/.../COMPLAIN"
_UPI_LAYOUT_RE = re.compile(r"^\s*(?:REV[\s/-]*)?UPI[/-]", re.IGNORECASE)

# Acronyms that should stay upper-case in the display name.
_ACRONYMS = {
    "ATM", "UPI", "NEFT", "IMPS", "RTGS", "EMI", "SIP", "IRCTC", "PVR", "BSNL", "MTNL",
    "BESCOM", "MSEB", "BSES", "TATA", "HDFC", "ICICI", "SBI", "LIC", "HP", "BPCL",
    "HPCL", "IOCL", "KFC", "PVR", "INOX", "OYO", "IKEA", "GST", "TDS", "NSE", "BSE",
    "DTH", "OTT", "AC", "PG", "IT",
}

# Narrations that must collapse to one merchant so months group together.
_CANONICAL: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bINT\.?\s*PD\b|\bCREDIT\s+INTEREST\b|\bINTEREST\s+(?:PAID|CREDIT)", re.I),
     "Interest Credited"),
    (re.compile(r"\bAMB\s*CHRG\b|\bNON\s*MAINTAIN|\bMIN(?:IMUM)?\s+BAL(?:ANCE)?\s+CHARGE", re.I),
     "Minimum Balance Charge"),
    (re.compile(r"^\s*(?:ATW|ATM|NWD|EAW)\b|\bCASH\s+WITHDRAWAL\b|\bCASH\s+WDL\b", re.I),
     "ATM Withdrawal"),
    (re.compile(r"\bSMS\s*(?:CHG|CHARGE)|\bALERT\s+CHARGES?\b", re.I), "SMS Alert Charges"),
    # A credit-card debit on a bank statement is a bill payment, not spending. Canonical
    # so it groups across banks and survives the noise-word filter, which would otherwise
    # strip both "CREDIT" and "PAYMENT" and leave the bare word "CARD".
    (re.compile(r"\bCREDIT\s*CARD\b|\bCC\s+(?:PAYMENT|BILL)\b|\bCARD\s+BILL\s+PAY", re.I),
     "Credit Card Payment"),
]


@dataclass(frozen=True)
class MerchantInfo:
    """What a narration resolves to."""

    merchant: str      # display name, e.g. "Swiggy"
    key: str           # stable lookup key, e.g. "swiggy"
    channel: str       # one of CHANNELS
    vpa: str | None    # UPI handle when present, e.g. "swiggy@ybl"

    @property
    def is_resolved(self) -> bool:
        return bool(self.key) and self.key != "unknown"


# --------------------------------------------------------------------------------------

def detect_channel(narration: str) -> str:
    """Classify how the money moved."""
    for channel, pattern in _CHANNEL_PATTERNS:
        if pattern.search(narration):
            return channel
    return "Other"


def _is_noise_token(token: str) -> bool:
    """True when a token is plumbing rather than a name."""
    bare = token.strip()
    if len(bare) < 2:
        return True
    if bare in _NOISE_WORDS or bare in _BANK_CODES:
        return True
    if _IFSC_RE.match(bare) or _MASKED_CARD_RE.match(bare):
        return True
    if _MONTH_STAMP_RE.match(bare) or _DATE_LIKE_RE.match(bare):
        return True
    if not re.search(r"[A-Z]", bare):          # digits and punctuation only
        return True
    # Reference numbers: long, mixed alphanumerics with a heavy digit share.
    digits = sum(character.isdigit() for character in bare)
    if len(bare) >= 8 and digits >= max(4, len(bare) // 2):
        return True
    return False


def _clean_phrase(phrase: str) -> str:
    """Drop noise words from inside a multi-word token, keeping order."""
    words = [word for word in re.split(r"\s+", phrase) if word]
    kept = [word for word in words if not _is_noise_token(word)]
    # A phrase made entirely of noise ("PAYMENT TO") disappears; one that is mostly a
    # name keeps its qualifiers ("INDIAN OIL PETROL PUMP").
    return " ".join(kept)


def _display_name(text: str) -> str:
    """Human-readable merchant name."""
    words = []
    for word in text.split():
        stripped = re.sub(r"[^A-Za-z0-9&'.]", "", word)
        if not stripped:
            continue
        if stripped.upper() in _ACRONYMS:
            words.append(stripped.upper())
        else:
            words.append(stripped.capitalize())
    return " ".join(words[:6])  # long tails add nothing on a dashboard


def make_key(name: str) -> str:
    """Normalize a merchant name into the key used by rules, cache, and overrides."""
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def extract_merchant(narration: str) -> MerchantInfo:
    """Resolve a raw narration into a merchant, channel, and lookup key."""
    raw = re.sub(r"\s+", " ", str(narration or "")).strip()
    if not raw:
        return MerchantInfo("Unknown", "unknown", "Other", None)

    channel = detect_channel(raw)

    vpa_match = _VPA_RE.search(raw)
    vpa = vpa_match.group(0).lower() if vpa_match else None

    # Narrations that must collapse to a single merchant across months.
    for pattern, canonical in _CANONICAL:
        if pattern.search(raw):
            return MerchantInfo(canonical, make_key(canonical), channel, vpa)

    upper = raw.upper()

    # Strip a leading channel prefix and any masked card number that follows it.
    upper = re.sub(
        r"^\s*(?:REV[\s/-]*)?"
        r"(?:UPI|POS|ECOM|NEFT|IMPS|RTGS|ACH\s*[DC]?|NACH\s*[DC]?|ECS|SI|MMT|TPT|IB)"
        r"[\s/:-]+", " ", upper
    )
    upper = re.sub(r"\b\d{0,6}X{2,}\d{0,6}\b", " ", upper)
    upper = re.sub(r"\b\d{12,}\b", " ", upper)

    # Remove the VPA itself before tokenizing. It is already captured in `vpa`, and left
    # in place it competes with the real name: in ".../MAKEMYTRIP/HDFC/mmt@hdfcbank/..."
    # the handle token would otherwise win and yield "Mmthdfcbank".
    if vpa_match:
        upper = upper.replace(vpa_match.group(0).upper(), " ")

    # Candidate phrases: the narration's own delimiters are meaningful separators.
    candidates: list[str] = []
    for chunk in re.split(r"[/|:;,\\]|\s-\s|(?<=[A-Z0-9])-(?=[A-Z])|-{1,}", upper):
        phrase = _clean_phrase(chunk.strip())
        if phrase:
            candidates.append(phrase)

    if not candidates and vpa:
        candidates = [vpa_match.group(1).upper()]

    if not candidates:
        return MerchantInfo("Unknown", "unknown", channel, vpa)

    # Prefer the phrase that matches the UPI handle — that is the payee by definition.
    chosen = None
    if vpa_match:
        handle = re.sub(r"[^A-Z0-9]", "", vpa_match.group(1).upper())
        for phrase in candidates:
            squashed = re.sub(r"[^A-Z0-9]", "", phrase)
            if squashed and (squashed == handle or squashed in handle or handle in squashed):
                chosen = phrase
                break

    if chosen is None and _UPI_LAYOUT_RE.match(raw):
        # UPI narrations are positional: UPI/<payee>/<bank>/<ref>/<note>. The payee
        # is the first surviving field. Length is the wrong tiebreak here — the
        # trailing note ("You are payi") outruns the merchant ("Amazon Pay").
        chosen = candidates[0]

    if chosen is None:
        # Otherwise the longest surviving phrase: reference numbers and bank codes are
        # already gone, so length is a good proxy for "the actual name".
        chosen = max(candidates, key=lambda phrase: (len(phrase), -candidates.index(phrase)))

    # Trailing digits are folio/bill numbers, not part of the name.
    chosen = re.sub(r"\s*\d{4,}\s*$", "", chosen).strip()
    display = _display_name(chosen) or "Unknown"
    return MerchantInfo(display, make_key(display) or "unknown", channel, vpa)


def annotate(frame, description_column: str = "description"):
    """Add ``merchant``, ``merchant_key``, ``channel``, and ``vpa`` columns to a frame."""
    infos = [extract_merchant(text) for text in frame[description_column]]
    result = frame.copy()
    result["merchant"] = [info.merchant for info in infos]
    result["merchant_key"] = [info.key for info in infos]
    result["channel"] = [info.channel for info in infos]
    result["vpa"] = [info.vpa for info in infos]
    return result
