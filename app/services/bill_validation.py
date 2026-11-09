"""
Put this file at app/services/bill_validation.py

Fixed rules that look at the bill itself (not only "do the fields match the text").
Any red flag sends the claim to HUMAN REVIEW. A flag never auto-rejects a claim.
No imports from the app, so it is easy to test on its own.
"""
import re
from datetime import date, datetime, timedelta

# Bills older than this are sent to a person (change to your policy, e.g. 30 or 180)
CLAIM_MAX_AGE_DAYS = 90

# Words that should never appear in a real provider or patient name.
# This is only a starting list: the LLM audit in extraction_service catches the rest.
JUNK_WORDS = {
    "faltu", "pagol", "pagal", "bekar", "bakwas", "badmash", "bewakoof", "gadha", "kutta", "chor",
    "fake", "dummy", "bogus", "nonsense", "useless", "random", "lorem", "ipsum",
    "asdf", "qwerty", "xyz", "abcd",
}

# Treatment text that says nothing about what was actually done
VAGUE_TREATMENT = re.compile(
    r"\b(unspecified|not\s+specified|unknown|miscellaneous|misc|n/?a|nil|others?|general\s+procedure|as\s+per\s+doctor)\b",
    re.I,
)

_DATE_FORMATS = (
    "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y/%m/%d",
    "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%d-%b-%Y", "%d-%b-%y",
)


def _clean(v):
    if v is None or isinstance(v, (dict, list)):
        return ""
    v = str(v).strip()
    return "" if v.lower() in ("unknown", "none", "null", "n/a", "not found") else v


def parse_bill_date(value):
    s = _clean(value)
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _junk_in(name):
    tokens = set(re.findall(r"[a-z]+", name.lower()))
    return sorted(tokens & JUNK_WORDS)


def rule_flags(extracted, today=None):
    """Returns a list of plain-English red flags. Empty list = nothing suspicious found by the rules."""
    today = today or date.today()
    flags = []

    provider = _clean(extracted.get("provider") or extracted.get("provider_name"))
    patient = _clean(extracted.get("patient") or extracted.get("patient_name"))
    treatment = _clean(extracted.get("treatment") or extracted.get("services"))

    if not provider:
        flags.append("provider name is missing")
    else:
        bad = _junk_in(provider)
        if bad:
            flags.append(f"provider name '{provider}' looks fake or nonsensical (contains '{bad[0]}')")

    if not patient:
        flags.append("patient name is missing")
    else:
        bad = _junk_in(patient)
        if bad:
            flags.append(f"patient name '{patient}' looks fake or nonsensical (contains '{bad[0]}')")

    if not treatment:
        flags.append("no treatment or service is described")
    elif VAGUE_TREATMENT.search(treatment):
        flags.append(f"treatment is vague ('{treatment[:60]}'), the bill does not say what was done")

    raw_date = extracted.get("date") or extracted.get("bill_date")
    d = parse_bill_date(raw_date)
    if not _clean(raw_date) or d is None:
        flags.append("bill date is missing or could not be read")
    elif d > today:
        flags.append(f"bill date {d.isoformat()} is in the future")
    elif d < today - timedelta(days=CLAIM_MAX_AGE_DAYS):
        flags.append(f"bill date {d.isoformat()} is older than {CLAIM_MAX_AGE_DAYS} days")

    return flags


def merge_flags(*lists):
    """Combine flag lists, remove duplicates, keep order."""
    seen, out = set(), []
    for lst in lists:
        for f in lst or []:
            f = str(f).strip()
            if f and f.lower() not in seen:
                seen.add(f.lower())
                out.append(f)
    return out
