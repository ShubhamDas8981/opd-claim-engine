"""
Put this file at app/ai_guards/rules.py

The RULES behind the guardrails: plain Python, no imports from the app and no import of
guardrails-ai, so every rule can be unit tested on its own.
app/ai_guards/guards.py wraps these rules as Guardrails AI validators.
"""
import math
import os
import re
import threading
import time
import unicodedata
from collections import deque

# ---------------------------------------------------------------- limits
MAX_MESSAGE_CHARS = 1000          # one chat message
MAX_BILL_TEXT_CHARS = 20_000      # text of one bill that is sent to the LLM
MAX_BILL_AMOUNT = 1_000_000.0     # above this the extraction is treated as wrong (rupees)

ALLOWED_FIELDS = {"date": 40, "provider": 120, "patient": 120, "treatment": 200}

# Which arguments each tool may receive from the LLM. Anything else is dropped, so a prompt
# injection can never smuggle in file_data, claim_id, user_id and similar.
ALLOWED_TOOLS = {
    "balance_tool": {"date_from", "date_to"},
    "claim_status_tool": set(),
    "claim_flow_tool": set(),
    "policy_tool": {"query"},
    "escalation_tool": {"query"},
}

# ---------------------------------------------------------------- text cleaning
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def normalize(text):
    """Unicode NFKC, no zero-width characters, no control characters (newlines and tabs stay)."""
    t = unicodedata.normalize("NFKC", "" if text is None else str(text))
    t = t.translate(_ZERO_WIDTH)
    return _CONTROL.sub(" ", t)


def clean_field(value, max_len=200):
    """One-line, normalised, length-limited string. Dicts, lists and None become ''."""
    if value is None or isinstance(value, (dict, list)):
        return ""
    t = re.sub(r"\s+", " ", normalize(value)).strip()
    return t[:max_len]


# ---------------------------------------------------------------- prompt injection
_CHAT_PATTERNS = {
    "override-instructions": re.compile(
        r"\b(ignore|disregard|forget|override|bypass)\b[^.\n]{0,40}"
        r"\b(instructions?|prompts?|guardrails?|guidelines?|everything\s+(?:above|before)"
        r"|(?:previous|prior|above|earlier|system|your|all)\s+rules?)\b",
        re.I,
    ),
    "reveal-prompt": re.compile(
        r"\b(reveal|show|print|repeat|leak|display|output|tell me)\b[^.\n]{0,30}"
        r"\b(system|hidden|initial|developer|secret)\b[^.\n]{0,15}"
        r"\b(prompt|instructions?|message|rules?)\b",
        re.I,
    ),
    "role-hijack": re.compile(
        r"\b(you are now|pretend (?:to be|you are)|from now on,? you|developer mode|jailbreak|do anything now)\b",
        re.I,
    ),
    "fake-role-tag": re.compile(
        r"<\|?\s*(system|assistant|im_start|im_end)\s*\|?>|\[/?(INST|SYSTEM)\]|^\s*(system|assistant)\s*:",
        re.I | re.M,
    ),
    "tool-injection": re.compile(
        r"[\"']?\b(tool|function|args)\b[\"']?\s*[:=]\s*[\"'{]?\s*[\"']?"
        r"(claim_flow_tool|escalation_tool|balance_tool|claim_status_tool|policy_tool|file_data|file_path)",
        re.I,
    ),
    "path-traversal": re.compile(r"\.\./|/etc/(?:passwd|shadow)|[a-z]:\\windows|file://", re.I),
}

# Extra patterns that only make sense for BILL text: a real bill never talks to the AI reviewer.
_BILL_EXTRA_PATTERNS = {
    "decision-manipulation": re.compile(
        r"\b(auto[-_ ]?approve"
        r"|approve\s+(?:this|the|my)\s+(?:claim|bill)"
        r"|mark\s+(?:this|it)\s+as\s+(?:approved|genuine|valid|verified)"
        r"|(?:set|give|assign|return)\s+(?:the\s+)?(?:score|confidence)"
        r"|(?:score|confidence)\s*(?:of|to|=|:)\s*(?:9\d|100)"
        r"|no\s+red\s+flags?"
        r"|do\s+not\s+(?:flag|reject|escalate)"
        r"|(?:this|the)\s+(?:bill|claim|invoice)\s+is\s+(?:genuine|real|valid|legit(?:imate)?|authentic))\b",
        re.I,
    ),
    "addresses-ai": re.compile(
        r"\b(?:dear|attention|note\s+to|message\s+for|instruction\s+for)\s+(?:the\s+)?(?:ai|llm|model|assistant|auditor|reviewer|bot)\b"
        r"|\b(?:ai|llm)\s+(?:auditor|reviewer|assistant)\b",
        re.I,
    ),
}

_DAN = re.compile(r"\bDAN\b")   # case sensitive on purpose: "Dan" is a normal name


def scan_injection(text, scope="chat"):
    """Returns the sorted names of the injection patterns found. scope is 'chat' or 'bill'."""
    t = normalize(text)
    patterns = dict(_CHAT_PATTERNS)
    if scope == "bill":
        patterns.update(_BILL_EXTRA_PATTERNS)
    hits = {name for name, p in patterns.items() if p.search(t)}
    if _DAN.search(t):
        hits.add("role-hijack")
    return sorted(hits)


# ---------------------------------------------------------------- sensitive data
# (?!91[6-9]\d{9}) keeps an Indian mobile number written with the +91 prefix from being taken for an Aadhaar number
_AADHAAR = re.compile(r"(?<!\d)(?!91[6-9]\d{9}(?!\d))[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?!\d)")
_PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_CREDENTIAL_TALK = re.compile(r"\b(cvv|cvc|otp|password|passcode)\b\s*(?:is|=|:)?\s*\S+", re.I)
_SECRETS = re.compile(
    r"\b(?:gsk_[A-Za-z0-9]{16,}"
    r"|sk-[A-Za-z0-9_-]{16,}"
    r"|pat-[a-z0-9]{2,4}-[A-Za-z0-9-]{16,}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"
)


def _luhn_ok(number):
    digits = [int(c) for c in re.sub(r"\D", "", number)]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def find_secrets(text):
    """API keys / tokens that must never appear in a reply."""
    return [m.group(0)[:12] + "..." for m in _SECRETS.finditer(normalize(text))]


def redact_sensitive(text):
    """Masks Aadhaar, PAN, card numbers, CVV/OTP/password words and API keys.
    Returns (clean_text, kinds_found). Phone numbers and emails are NOT touched on purpose:
    the support team needs them."""
    kinds = []
    t = normalize(text)

    def card(m):
        if _luhn_ok(m.group(0)):
            kinds.append("card")
            return "[CARD REDACTED]"
        return m.group(0)

    t = _CARD.sub(card, t)

    def mark(kind, label):
        def repl(_m):
            kinds.append(kind)
            return label
        return repl

    t = _AADHAAR.sub(mark("aadhaar", "[AADHAAR REDACTED]"), t)
    t = _PAN.sub(mark("pan", "[PAN REDACTED]"), t)
    t = _CREDENTIAL_TALK.sub(mark("credential", "[REDACTED]"), t)
    t = _SECRETS.sub(mark("secret", "[SECRET REDACTED]"), t)
    return t, sorted(set(kinds))


def fix_currency(text):
    """The wallet is in Indian Rupees. Dollar signs and 'USD' in LLM text are turned into rupees."""
    t = re.sub(r"\$\s?(?=\d)", "₹", text)
    t = re.sub(r"\bUSD\b", "INR", t)
    return re.sub(r"\b(?:US\s+)?dollars?\b", "rupees", t, flags=re.I)


# ---------------------------------------------------------------- structured LLM output
def _to_amount(v):
    if isinstance(v, dict):
        v = v.get("amount")
    if isinstance(v, bool):
        raise ValueError("amount is missing")
    if isinstance(v, (int, float)):
        amount = float(v)
    elif isinstance(v, str):
        m = re.search(r"\d[\d,]*(?:\.\d+)?", v)
        if not m:
            raise ValueError("amount is missing")
        amount = float(m.group(0).replace(",", ""))
    else:
        raise ValueError("amount is missing")
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError("amount must be more than zero")
    if amount > MAX_BILL_AMOUNT:
        raise ValueError("amount is implausibly large")
    return round(amount, 2)


def validate_extraction(data):
    """Whitelist + type + range check of what the extraction LLM returned.
    Returns (clean_dict, flags). Raises ValueError when the result is unusable."""
    if not isinstance(data, dict):
        raise ValueError("extraction is not a JSON object")

    clean = {"amount": _to_amount(data.get("amount"))}
    flags = []
    for field, limit in ALLOWED_FIELDS.items():
        value = clean_field(data.get(field), limit)
        if not value:
            value = "Unknown"
        elif scan_injection(value, "bill"):
            flags.append(f"{field} field contains instructions aimed at an AI reviewer")
        clean[field] = value
    return clean, flags


def validate_audit(res):
    """Clamp and clean what the auditor LLM returned. Returns (score 0-100, reason, flags)."""
    if not isinstance(res, dict):
        raise ValueError("audit is not a JSON object")

    try:
        score = int(round(float(res.get("score"))))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("audit score is not a number")
    score = max(0, min(100, score))

    reason = clean_field(res.get("reason"), 400) or "No reason provided"

    raw_flags = res.get("red_flags") or []
    if not isinstance(raw_flags, list):
        raw_flags = [raw_flags]
    flags, seen = [], set()
    for f in raw_flags:
        f = clean_field(f, 120)
        if f and f.lower() not in seen:
            seen.add(f.lower())
            flags.append(f)
    return score, reason, flags[:10]


_DATE_ARG = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?$")


def validate_intent(obj, has_file=False):
    """Checks the intent-classifier output. Returns (tool_name, safe_args).
    'none' = chatter, 'claim_needs_file' = the user must upload a bill first."""
    if not isinstance(obj, dict):
        raise ValueError("intent is not a JSON object")

    tool = obj.get("tool")
    if tool in (None, "none"):
        return "none", {}
    if not isinstance(tool, str) or tool not in ALLOWED_TOOLS:
        raise ValueError("unknown tool")

    raw = obj.get("args")
    raw = raw if isinstance(raw, dict) else {}
    args = {}
    for key in ALLOWED_TOOLS[tool]:
        value = raw.get(key)
        if value is None:
            continue
        if key in ("date_from", "date_to"):
            value = clean_field(value, 20)
            if _DATE_ARG.match(value):
                args[key] = value
        else:
            value = clean_field(value, 300)
            if value:
                args[key] = value

    if tool == "claim_flow_tool" and not has_file:
        return "claim_needs_file", {}
    return tool, args


# ---------------------------------------------------------------- rate limit
class SlidingWindowLimiter:
    """max_events per window_seconds for each key. In memory: use Redis if you run many workers."""

    def __init__(self, max_events=20, window_seconds=60):
        self.max_events = max_events
        self.window = window_seconds
        self._events = {}
        self._lock = threading.Lock()

    def allow(self, key, now=None):
        now = time.time() if now is None else now
        with self._lock:
            dq = self._events.setdefault(key, deque())
            while dq and now - dq[0] > self.window:
                dq.popleft()
            if len(dq) >= self.max_events:
                return False
            dq.append(now)
            return True


# ---------------------------------------------------------------- uploaded bills
UPLOAD_MAX_BYTES = 5 * 1024 * 1024
_UPLOAD_TYPES = {                      # extension -> the first bytes a real file of that type starts with
    ".pdf": b"%PDF",
    ".png": b"\x89PNG\r\n\x1a\n",
    ".jpg": b"\xff\xd8\xff",
    ".jpeg": b"\xff\xd8\xff",
    ".txt": None,
}


def safe_filename(name):
    """No folders, no '..', only letters, digits and . _ - (stops path traversal through the file name)."""
    base = re.split(r"[\\/]", str(name or ""))[-1]
    base = unicodedata.normalize("NFKC", base)
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "bill"
    return base[-80:]


def validate_upload(filename, content):
    """Returns (safe_filename, None) or (None, message for the user)."""
    safe = safe_filename(filename)
    ext = os.path.splitext(safe)[1].lower()
    if ext not in _UPLOAD_TYPES:
        return None, "Please upload your bill as a PDF, JPG, PNG or TXT file."
    if not content:
        return None, "That file is empty. Please upload your bill again."
    if len(content) > UPLOAD_MAX_BYTES:
        return None, "That file is too large. The maximum size is 5 MB."
    magic = _UPLOAD_TYPES[ext]
    if magic and not content.startswith(magic):
        return None, f"That file does not look like a real {ext[1:].upper()} file."
    if ext == ".txt":
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            return None, "That text file could not be read. Please upload a PDF instead."
        if b"\x00" in content:
            return None, "That text file could not be read. Please upload a PDF instead."
    return safe, None
