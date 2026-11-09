"""
Custom Guardrails AI validators for the OPD Wallet.
Each one is a normal Guardrails `Validator`, so it works in Guard().use(...) and in Pydantic Field(validators=[...]).
Error messages start with a short code (injection:, toxic:, ...) so the app can react to the reason.
"""
import re
from typing import Any, Dict, Optional

from guardrails.validators import FailResult, PassResult, Validator, register_validator

from app.safety import config
from app.safety.pii import find_pii, mask_pii, SENSITIVE
from app.safety.text_utils import injection_hits, abusive, clean_text

UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
USERID_RE = re.compile(r"\bUSER[_-]?[A-Z0-9]{2,}\b", re.I)


@register_validator(name="opd/max-chars", data_type="string")
class MaxChars(Validator):
    def __init__(self, limit: int = config.MAX_MESSAGE_CHARS, on_fail=None):
        super().__init__(on_fail=on_fail, limit=limit)
        self.limit = limit

    def _validate(self, value: Any, metadata: Dict) -> Any:
        if len(value) > self.limit:
            return FailResult(error_message=f"too_long: {len(value)} characters, limit {self.limit}",
                              fix_value=value[: self.limit])
        return PassResult()


@register_validator(name="opd/prompt-injection", data_type="string")
class PromptInjection(Validator):
    """Heuristic jailbreak / injection detector. Works offline. Add the Hub DetectJailbreak model on top for more coverage."""
    def __init__(self, threshold: int = config.INJECTION_BLOCK_SCORE, on_fail=None):
        super().__init__(on_fail=on_fail, threshold=threshold)
        self.threshold = threshold

    def _validate(self, value: Any, metadata: Dict) -> Any:
        score, hits = injection_hits(value)
        if score >= self.threshold:
            return FailResult(error_message=f"injection: score {score}, matched {hits}")
        return PassResult()


@register_validator(name="opd/no-abuse", data_type="string")
class NoAbuse(Validator):
    def _validate(self, value: Any, metadata: Dict) -> Any:
        if abusive(value):
            return FailResult(error_message="toxic: abusive language")
        return PassResult()


@register_validator(name="opd/pii", data_type="string")
class PIIGuard(Validator):
    """Masks Aadhaar / PAN / card / IFSC (checksum-verified). `kinds` can include email and mobile too."""
    def __init__(self, kinds=None, on_fail=None):
        kinds = sorted(kinds or SENSITIVE)
        super().__init__(on_fail=on_fail, kinds=kinds)
        self.kinds = set(kinds)

    def _validate(self, value: Any, metadata: Dict) -> Any:
        found = find_pii(value, self.kinds)
        if found:
            names = sorted({k for k, _, _ in found})
            return FailResult(error_message=f"pii: found {names}", fix_value=mask_pii(value, self.kinds))
        return PassResult()


@register_validator(name="opd/rupees-only", data_type="string")
class RupeesOnly(Validator):
    """The wallet is in rupees. Any $ amount in a reply is a hallucination or a stale template."""
    def _validate(self, value: Any, metadata: Dict) -> Any:
        if re.search(r"\$\s?\d|\b(usd|dollars?)\b", value, re.I):
            fixed = re.sub(r"\$\s?(\d)", r"₹\1", value)
            fixed = re.sub(r"\b(usd|dollars?)\b", "rupees", fixed, flags=re.I)
            return FailResult(error_message="currency: non-rupee amount in reply", fix_value=fixed)
        return PassResult()


@register_validator(name="opd/no-foreign-ids", data_type="string")
class NoForeignIds(Validator):
    """A reply may only mention the logged-in user's own user id and claim ids (metadata: user_id, allowed_ids)."""
    def _validate(self, value: Any, metadata: Dict) -> Any:
        me = str((metadata or {}).get("user_id", "")).lower()
        ok = {str(x).lower() for x in (metadata or {}).get("allowed_ids", [])}
        bad = [u for u in UUID_RE.findall(value) if u.lower() not in ok]
        bad += [u for u in USERID_RE.findall(value) if u.lower() != me]
        if bad:
            fixed = value
            for b in bad:
                fixed = fixed.replace(b, "[hidden]")
            return FailResult(error_message=f"leak: other identifiers in reply {bad[:3]}", fix_value=fixed)
        return PassResult()


_LEAK_MARKERS = ("you are an expert opd", "strict intent classifier", "return only a json object",
                 "classify the user's intent", "medical insurance auditor", "fraud detection auditor")


@register_validator(name="opd/no-prompt-leak", data_type="string")
class NoPromptLeak(Validator):
    def _validate(self, value: Any, metadata: Dict) -> Any:
        low = value.lower()
        if any(m in low for m in _LEAK_MARKERS):
            return FailResult(error_message="leak: system prompt text in reply",
                              fix_value="Sorry, I can't share that. I can help with your balance, claims and OPD policy.")
        return PassResult()


@register_validator(name="opd/safe-user-id", data_type="string")
class SafeUserId(Validator):
    def _validate(self, value: Any, metadata: Dict) -> Any:
        if not re.fullmatch(r"[\w.@-]{2,40}", value or ""):
            return FailResult(error_message="user_id: invalid format")
        return PassResult()
