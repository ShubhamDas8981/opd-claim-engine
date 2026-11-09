"""Indian PII finder/masker with real checksums (no false alarms on ordinary numbers)."""
import re

_D = [[0,1,2,3,4,5,6,7,8,9],[1,2,3,4,0,6,7,8,9,5],[2,3,4,0,1,7,8,9,5,6],[3,4,0,1,2,8,9,5,6,7],
      [4,0,1,2,3,9,5,6,7,8],[5,9,8,7,6,0,4,3,2,1],[6,5,9,8,7,1,0,4,3,2],[7,6,5,9,8,2,1,0,4,3],
      [8,7,6,5,9,3,2,1,0,4],[9,8,7,6,5,4,3,2,1,0]]
_P = [[0,1,2,3,4,5,6,7,8,9],[1,5,7,6,2,8,3,0,9,4],[5,8,0,3,7,9,6,1,4,2],[8,9,1,6,0,4,3,5,2,7],
      [9,4,5,3,1,2,6,8,7,0],[4,2,8,6,5,7,3,9,0,1],[2,7,9,3,8,0,6,4,1,5],[7,0,4,6,9,1,3,2,5,8]]
_INV = [0,4,3,2,1,5,6,7,8,9]


def verhoeff_ok(num: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(num)):
        c = _D[c][_P[i % 8][int(ch)]]
    return c == 0


def verhoeff_check_digit(num: str) -> str:          # used by the tests
    c = 0
    for i, ch in enumerate(reversed(num)):
        c = _D[c][_P[(i + 1) % 8][int(ch)]]
    return str(_INV[c])


def luhn_ok(num: str) -> bool:
    total, alt = 0, False
    for ch in reversed(num):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


# kind -> (regex, validator or None). Order = priority when spans overlap.
_PATTERNS = [
    ("card",    re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)"), lambda s: luhn_ok(re.sub(r"\D", "", s))),
    ("aadhaar", re.compile(r"(?<!\d)[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?!\d)"), lambda s: verhoeff_ok(re.sub(r"\D", "", s))),
    ("pan",     re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), None),
    ("ifsc",    re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b"), None),
    ("email",   re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), None),
    ("mobile",  re.compile(r"(?<!\d)(?:\+91[ -]?|0)?[6-9]\d{9}(?!\d)"), None),
]

SENSITIVE = {"card", "aadhaar", "pan", "ifsc"}      # never needed in chat or in replies
ALL_KINDS = {k for k, _, _ in _PATTERNS}


def find_pii(text: str, kinds=None):
    kinds = set(kinds) if kinds else ALL_KINDS
    taken, found = [], []
    for kind, rx, check in _PATTERNS:
        if kind not in kinds:
            continue
        for m in rx.finditer(text or ""):
            s, e = m.span()
            if any(s < te and e > ts for ts, te in taken):
                continue
            if check and not check(m.group()):
                continue
            taken.append((s, e))
            found.append((kind, s, e))
    return sorted(found, key=lambda x: x[1])


def mask_pii(text: str, kinds=None) -> str:
    out, last = [], 0
    for kind, s, e in find_pii(text, kinds):
        out.append(text[last:s])
        out.append(f"[{kind.upper()} removed]")
        last = e
    out.append(text[last:])
    return "".join(out)
