"""
Guardrails AI wiring for the OPD Wallet.

  check_user_message()   chat input   -> block injection / abuse / too long, mask Aadhaar-PAN-card
  guarded_json()         LLM output   -> strict schema + retry with the error message (re-ask)
  guard_text() / guard_response()  reply to the user -> no $ amounts, no other users' ids, no prompt leaks, no PII
  sanitize_reason()      text shown to the admin
"""
import json
import re
from dataclasses import dataclass, field
from typing import Callable

from guardrails import Guard, OnFailAction
from guardrails.errors import ValidationError
from guardrails.settings import settings as _gr_settings

from app.safety import audit, config
from app.safety.schemas import BillAudit, BillExtraction, IntentDecision
from app.safety.text_utils import clean_text
from app.safety.validators import (MaxChars, NoAbuse, NoForeignIds, NoPromptLeak, PIIGuard,
                                   PromptInjection, RupeesOnly, SafeUserId)

# Medical bills and PII must not leave the server: switch Guardrails' own telemetry off.
_gr_settings.rc.enable_metrics = False

# ------------------------------------------------------------------ optional Hub validators
def _hub_extras():
    """Optional ML validators. They are normal PyPI packages now (no API key, no `guardrails hub install`):
         uv add guardrails-ai-detect-jailbreak guardrails-ai-toxic-language
         uv run python -m guardrails_ai.detect_jailbreak.post_install     # downloads the models once
       then set ENABLE_HUB_VALIDATORS = True in config.py.
       They run LOCALLY (use_local=True): the chat text is not sent to anyone.
       If a package is missing the app logs it and keeps the built-in checks."""
    if not config.ENABLE_HUB_VALIDATORS:
        return []
    extras = []
    try:
        try:
            from guardrails_ai.detect_jailbreak import DetectJailbreak
        except ImportError:                                   # older `guardrails hub install` layout
            from guardrails.hub import DetectJailbreak
        extras.append(DetectJailbreak(threshold=0.81, device="cpu", use_local=True, on_fail=OnFailAction.EXCEPTION))
    except Exception as e:
        audit.event("hub_validator_missing", validator="DetectJailbreak", error=e)
    try:
        try:
            from guardrails_ai.toxic_language import ToxicLanguage
        except ImportError:
            from guardrails.hub import ToxicLanguage
        extras.append(ToxicLanguage(threshold=0.8, validation_method="sentence", use_local=True, on_fail=OnFailAction.EXCEPTION))
    except Exception as e:
        audit.event("hub_validator_missing", validator="ToxicLanguage", error=e)
    return extras


# ------------------------------------------------------------------ the guards (built once)
_block_guard = Guard(name="chat_block").use(
    MaxChars(limit=config.MAX_MESSAGE_CHARS, on_fail=OnFailAction.EXCEPTION),
    PromptInjection(on_fail=OnFailAction.EXCEPTION),
    NoAbuse(on_fail=OnFailAction.EXCEPTION),
    *_hub_extras(),
)
_sanitize_guard = Guard(name="chat_sanitize").use(PIIGuard(on_fail=OnFailAction.FIX))
_output_guard = Guard(name="reply_output").use(
    NoPromptLeak(on_fail=OnFailAction.FIX),
    RupeesOnly(on_fail=OnFailAction.FIX),
    NoForeignIds(on_fail=OnFailAction.FIX),
    PIIGuard(on_fail=OnFailAction.FIX),
    MaxChars(limit=2000, on_fail=OnFailAction.FIX),
)
_reason_guard = Guard(name="admin_reason").use(
    NoPromptLeak(on_fail=OnFailAction.FIX), RupeesOnly(on_fail=OnFailAction.FIX), MaxChars(limit=600, on_fail=OnFailAction.FIX))
_userid_guard = Guard(name="user_id").use(SafeUserId(on_fail=OnFailAction.EXCEPTION))

intent_guard = Guard.for_pydantic(IntentDecision, name="intent")
extraction_guard = Guard.for_pydantic(BillExtraction, name="bill_extraction")
audit_guard = Guard.for_pydantic(BillAudit, name="bill_audit")


# ------------------------------------------------------------------ user id
def valid_user_id(user_id: str) -> bool:
    try:
        return _userid_guard.validate(user_id or "").validation_passed
    except ValidationError:
        return False


# ------------------------------------------------------------------ chat input
@dataclass
class Verdict:
    allowed: bool
    text: str = ""
    code: str = ""
    user_message: str = ""
    notes: list = field(default_factory=list)


_USER_MESSAGES = {
    "injection": "I can only help with your OPD balance, claims and policy questions. Please rephrase your request.",
    "toxic": "Please keep the chat respectful. If you need help from a person, choose Talk to a human.",
    "too_long": f"That message is too long. Please keep it under {config.MAX_MESSAGE_CHARS} characters.",
    "empty": "Please type a message.",
}


def check_user_message(text: str, user_id: str | None = None) -> Verdict:
    cleaned = clean_text(text)
    if not cleaned:
        return Verdict(False, code="empty", user_message=_USER_MESSAGES["empty"])
    try:
        _block_guard.validate(cleaned)
    except ValidationError as e:
        code = (re.match(r"\s*(\w+):", str(e)) or re.search(r"(injection|toxic|too_long)", str(e)) or [None, "injection"])[1]
        code = code if code in _USER_MESSAGES else "injection"
        audit.event("input_blocked", user_id, code=code, detail=str(e)[:200])
        return Verdict(False, code=code, user_message=_USER_MESSAGES[code])

    out = _sanitize_guard.validate(cleaned)
    safe = out.validated_output if out.validated_output is not None else cleaned
    notes = []
    if safe != cleaned:
        notes.append("pii_masked")
        audit.event("input_pii_masked", user_id)
    return Verdict(True, text=safe, notes=notes)


# ------------------------------------------------------------------ reply to the user
def guard_text(text: str, user_id: str = "", allowed_ids=()) -> str:
    if not isinstance(text, str) or not text:
        return text
    res = _output_guard.validate(text, metadata={"user_id": user_id, "allowed_ids": list(allowed_ids)})
    fixed = res.validated_output if res.validated_output is not None else text
    if fixed != text:
        audit.event("output_fixed", user_id, before=text[:120], after=fixed[:120])
    return fixed


_FREE_TEXT_KEYS = {"answer", "message", "text", "note", "error"}


def guard_response(resp, user_id: str = "", allowed_ids=()):
    """Walks a tool result and guards only the free-text fields. Structured fields (balances, claim lists from the DB) are left alone."""
    if isinstance(resp, str):
        return guard_text(resp, user_id, allowed_ids)
    if isinstance(resp, dict):
        return {k: (guard_text(v, user_id, allowed_ids) if k in _FREE_TEXT_KEYS and isinstance(v, str)
                    else guard_response(v, user_id, allowed_ids) if isinstance(v, (dict, list)) and k not in ("claims_history", "history")
                    else v) for k, v in resp.items()}
    if isinstance(resp, list):
        return [guard_response(x, user_id, allowed_ids) for x in resp]
    return resp


def sanitize_reason(text: str) -> str:
    res = _reason_guard.validate(clean_text(text or ""))
    return res.validated_output if res.validated_output is not None else (text or "")[:600]


# ------------------------------------------------------------------ LLM output: validate, then re-ask
class GuardFailure(Exception):
    """The LLM could not produce a valid answer even after re-asking."""


def _why(outcome) -> str:
    msgs = []
    try:
        for fr in (outcome.reask.fail_results if outcome.reask else []) or []:
            msgs.append(fr.error_message)
    except Exception:
        pass
    if not msgs and outcome.error:
        msgs.append(str(outcome.error))
    for s in getattr(outcome, "validation_summaries", None) or []:
        if getattr(s, "failure_reason", None):
            msgs.append(s.failure_reason)
    return "; ".join(dict.fromkeys(msgs))[:400] or "output did not match the required format"


def guarded_json(call_llm: Callable[[list], str], guard: Guard, messages: list, *,
                 pre: Callable[[dict], dict] | None = None, metadata: dict | None = None,
                 max_reasks: int = config.MAX_REASKS, name: str = "llm", user_id: str | None = None) -> dict:
    """call_llm(messages) -> raw text. Returns the validated dict, or raises GuardFailure.
    On a bad answer the error message is sent back to the model (this is Guardrails' 're-ask')."""
    msgs, why = list(messages), "unknown"
    for attempt in range(max_reasks + 1):
        raw = call_llm(msgs) or ""
        candidate = raw
        if pre:
            try:
                candidate = json.dumps(pre(json.loads(raw)))
            except Exception:
                candidate = raw                      # let the guard report the real problem
        outcome = guard.parse(candidate, metadata=metadata or {})
        if outcome.validation_passed and outcome.validated_output is not None:
            if attempt:
                audit.event("llm_reask_ok", user_id, guard=name, attempts=attempt + 1)
            return outcome.validated_output
        why = _why(outcome)
        audit.event("llm_reask", user_id, guard=name, attempt=attempt + 1, why=why)
        msgs = msgs + [
            {"role": "assistant", "content": raw[:2000]},
            {"role": "user", "content": f"That JSON was rejected: {why}. Return ONLY corrected JSON that follows the format exactly."},
        ]
    audit.event("llm_failed", user_id, guard=name, why=why)
    raise GuardFailure(why)
