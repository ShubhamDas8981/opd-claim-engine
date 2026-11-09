"""Cleaning and prompt-injection detection shared by every guard."""
import re
import unicodedata

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad\u202a\u202b\u202c\u202d\u202e"), None)


def clean_text(text: str, max_chars: int | None = None) -> str:
    """NFKC (turns fullwidth / look-alike forms into plain ones), removes hidden characters, collapses spaces."""
    t = unicodedata.normalize("NFKC", str(text or "")).translate(_ZERO_WIDTH)
    t = "".join(ch for ch in t if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    t = re.sub(r"[ \t]+", " ", t).strip()
    return t[:max_chars] if max_chars else t


# (regex, weight). Includes Hinglish phrasing because users here write in Indian languages too.
_INJECTION = [
    (r"\b(ignore|disregard|forget|override|bypass)\b.{0,40}\b(previous|prior|above|earlier|all|any|your|the)\b.{0,30}\b(instruction|prompt|rule|guideline|polic)", 3),
    (r"\b(pichle|purane|sab|saare)\b.{0,25}\b(instructions?|nirdesh|rules?)\b.{0,25}\b(ignore|bhool|ignore kar|mat maan)", 3),
    (r"\b(reveal|show|print|repeat|leak|tell me)\b.{0,30}\b(system prompt|your prompt|your instructions|hidden instructions|developer message)", 3),
    (r"\bsystem (note|prompt|message|override)\b", 2),
    (r"\b(note|message|instruction)s? (to|for) (the )?(ai|assistant|llm|model|auditor|bot)\b", 3),
    (r"\b(give|assign|set|rate|mark)\b.{0,30}\b(score|confidence|rating)\b.{0,20}\b(100|maximum|max|full)\b", 3),
    (r"\b(approve|accept|pass|auto[- ]?approve)\b.{0,20}\b(this|the|my)\b.{0,15}\b(bill|claim|document)\b", 2),
    (r"\byou are (now|no longer)\b|\bpretend (to be|you are)\b|\bact as (an?|the)\b|\broleplay as\b", 2),
    (r"\b(jailbreak|do anything now|developer mode|dan mode)\b", 3),
    (r"<\|?(im_start|im_end|system|assistant)\|?>|\[/?(inst|sys)\]|^\s*(system|assistant)\s*:", 3),
    (r"\bignore (the )?(fraud|red flag|check|validation|audit)", 3),
    (r"\bbase64\b.{0,20}\b(decode|instruction)", 2),
]
_COMPILED = [(re.compile(p, re.I | re.S | re.M), w) for p, w in _INJECTION]


def injection_hits(text: str):
    """Returns (total_score, [matched snippets])."""
    t = clean_text(text)
    score, hits = 0, []
    for rx, w in _COMPILED:
        m = rx.search(t)
        if m:
            score += w
            hits.append(m.group()[:60])
    return score, hits


def neutralize(text: str, max_chars: int | None = None):
    """For UNTRUSTED text that must still go to an LLM (bill text, chat transcript):
    cut out injection phrases instead of blocking. Returns (clean_text, hits)."""
    t = clean_text(text, max_chars)
    hits = []
    for rx, _w in _COMPILED:
        for m in rx.finditer(t):
            hits.append(m.group()[:60])
        t = rx.sub("[removed: instruction aimed at the AI]", t)
    return t, hits


_ABUSE = re.compile(
    r"\b(fuck\w*|shit\w*|bitch\w*|bastard|asshole|madarchod|behenchod|bhosdi\w*|chutiya\w*|harami|randi|saala|kamina)\b", re.I)


def abusive(text: str) -> bool:
    return bool(_ABUSE.search(clean_text(text)))
