"""
Put this file at app/ai_guards/guards.py

Guardrails AI (https://guardrailsai.com) wrapper for the OPD wallet.

Layers
  1. rules.py        deterministic rules (injection patterns, PII, schema checks). Always on.
  2. Guardrails AI   the rules above are registered as Guardrails AI validators and run inside Guard objects
                     (Guard().use(validator)). Optional model based Hub validators (toxic language,
                     jailbreak) are added when GUARDRAILS_USE_HUB=1 and they are installed.
  3. Fallback        if guardrails-ai is not installed, or a Guard crashes, the same rules are used directly,
                     so the app never runs unprotected and never fails open.

Install
  pip install guardrails-ai
  # optional model based validators:
  guardrails configure
  guardrails hub install hub://guardrails/toxic_language
  guardrails hub install hub://guardrails/detect_jailbreak
  # then set GUARDRAILS_USE_HUB=1
"""
import json
import logging
import os
import warnings
from dataclasses import dataclass

from app.ai_guards import rules
from app.monitoring import metrics                 # NEW (MONITORING)

log = logging.getLogger("ai_guards")

# Guardrails AI sends usage traces to its own cloud endpoint. Without internet/DNS every call waits on retries
# (the "opentelemetry ... NameResolutionError" lines) and slows the app. Switch that off before it is imported.
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("OTEL_TRACES_EXPORTER", "none")

# Guardrails AI warns (harmlessly) when a sync Guard runs inside FastAPI's event loop; our validators are instant
warnings.filterwarnings("ignore", message="Could not obtain an event loop.*")

try:
    from guardrails import Guard, OnFailAction
    from guardrails.validators import (
        FailResult,
        PassResult,
        ValidationResult,
        Validator,
        register_validator,
    )
    GUARDRAILS_AVAILABLE = True
except Exception as _exc:                       # not installed: the built-in rules still run
    GUARDRAILS_AVAILABLE = False
    log.warning("guardrails-ai is not available (%s). Using the built-in rules only.", _exc)

try:
    from guardrails.errors import ValidationError as _GRValidationError
except Exception:
    _GRValidationError = None

# ---------------------------------------------------------------- what the user sees
REPLY_EMPTY = "Please type your question."
REPLY_TOO_LONG = f"That message is too long. Please keep it under {rules.MAX_MESSAGE_CHARS} characters."
REPLY_BLOCKED = (
    "I can only help with your OPD balance, claims and coverage questions, "
    "so I can't act on that request. You can ask me about your balance, claim status or what is covered."
)
REPLY_TOO_FAST = "You are sending messages too fast. Please wait a minute and try again."

UPLOAD_MAX_BYTES = rules.UPLOAD_MAX_BYTES

# 20 chat messages per user per minute
chat_limiter = rules.SlidingWindowLimiter(max_events=20, window_seconds=60)


class GuardError(ValueError):
    """An LLM answer or an input was rejected by the guardrails."""

    def __init__(self, message, code="rejected"):
        super().__init__(message)
        self.code = code
        metrics.output_rejected(code)                  # NEW (MONITORING)


@dataclass
class GuardResult:
    ok: bool
    text: str
    reply: str = ""
    code: str = "ok"


def _event(kind, user_id="", detail=""):
    log.warning("AI_GUARD event=%s user=%s detail=%s", kind, user_id or "-", str(detail)[:200])
    metrics.guard_event(kind, detail)                  # NEW (MONITORING)


# ---------------------------------------------------------------- Guardrails AI validators
if GUARDRAILS_AVAILABLE:

    @register_validator(name="opd-prompt-injection", data_type="string")
    class PromptInjection(Validator):
        """Fails when the text tries to give instructions to the AI. scope: 'chat' or 'bill'."""

        def __init__(self, scope="chat", on_fail=None):
            super().__init__(on_fail=on_fail, scope=scope)
            self._scope = scope

        def validate(self, value, metadata=None) -> ValidationResult:
            hits = rules.scan_injection(value, self._scope)
            if hits:
                return FailResult(error_message="possible prompt injection: " + ", ".join(hits))
            return PassResult()

    @register_validator(name="opd-text-length", data_type="string")
    class TextLength(Validator):
        def __init__(self, max_chars=1000, on_fail=None):
            super().__init__(on_fail=on_fail, max_chars=max_chars)
            self._max = max_chars

        def validate(self, value, metadata=None) -> ValidationResult:
            if len(value) > self._max:
                return FailResult(
                    error_message=f"text is longer than {self._max} characters",
                    fix_value=value[: self._max],
                )
            return PassResult()

    @register_validator(name="opd-sensitive-data", data_type="string")
    class SensitiveData(Validator):
        """Aadhaar, PAN, card numbers, CVV/OTP/password words and API keys are masked (fix)."""

        def __init__(self, on_fail=None):
            super().__init__(on_fail=on_fail)

        def validate(self, value, metadata=None) -> ValidationResult:
            clean, kinds = rules.redact_sensitive(value)
            if kinds:
                return FailResult(error_message="sensitive data found: " + ", ".join(kinds), fix_value=clean)
            return PassResult()

    @register_validator(name="opd-rupee-only", data_type="string")
    class RupeeOnly(Validator):
        """The wallet is in rupees: '$' and 'USD' in an LLM answer are turned into rupees (fix)."""

        def __init__(self, on_fail=None):
            super().__init__(on_fail=on_fail)

        def validate(self, value, metadata=None) -> ValidationResult:
            fixed = rules.fix_currency(value)
            if fixed != value:
                return FailResult(error_message="text mentions a currency other than rupees", fix_value=fixed)
            return PassResult()

    @register_validator(name="opd-json-shape", data_type="string")
    class JsonShape(Validator):
        """The raw LLM JSON must have the right fields, types and ranges. kind: extraction | audit | intent."""

        def __init__(self, kind="extraction", on_fail=None):
            super().__init__(on_fail=on_fail, kind=kind)
            self._kind = kind

        def validate(self, value, metadata=None) -> ValidationResult:
            try:
                obj = json.loads(value)
                if self._kind == "extraction":
                    rules.validate_extraction(obj)
                elif self._kind == "audit":
                    rules.validate_audit(obj)
                else:
                    rules.validate_intent(obj)
            except (ValueError, TypeError) as exc:
                return FailResult(error_message=f"invalid {self._kind} output: {exc}")
            return PassResult()


def _hub_validators():
    """Model based validators from the Guardrails Hub. Only when GUARDRAILS_USE_HUB=1 and installed."""
    if not GUARDRAILS_AVAILABLE or os.getenv("GUARDRAILS_USE_HUB", "0") != "1":
        return []
    found = []
    try:
        from guardrails.hub import DetectJailbreak
        found.append(DetectJailbreak(on_fail=OnFailAction.EXCEPTION))
    except Exception as exc:
        log.warning("Hub validator DetectJailbreak not available: %s", exc)
    try:
        from guardrails.hub import ToxicLanguage
        found.append(ToxicLanguage(threshold=0.8, validation_method="sentence", on_fail=OnFailAction.EXCEPTION))
    except Exception as exc:
        log.warning("Hub validator ToxicLanguage not available: %s", exc)
    return found


_GUARDS = {}


def _guard(name):
    """Builds each Guard once. Returns None when Guardrails AI is not usable (the rules take over)."""
    if not GUARDRAILS_AVAILABLE:
        return None
    if name in _GUARDS:
        return _GUARDS[name]

    try:
        block = OnFailAction.EXCEPTION
        fix = OnFailAction.FIX
        if name == "chat-input":
            validators = [
                TextLength(max_chars=rules.MAX_MESSAGE_CHARS, on_fail=block),
                PromptInjection(scope="chat", on_fail=block),
                *_hub_validators(),
            ]
        elif name == "bill-input":
            validators = [PromptInjection(scope="bill", on_fail=block)]
        elif name == "llm-output":
            validators = [SensitiveData(on_fail=fix), RupeeOnly(on_fail=fix)]
        elif name.startswith("json-"):
            validators = [JsonShape(kind=name[5:], on_fail=block)]
        else:
            return None

        guard = Guard(name=f"opd-{name}")
        for v in validators:
            guard = guard.use(v)
        _GUARDS[name] = guard
    except Exception:
        log.exception("Could not build the Guardrails AI guard '%s'. Using the built-in rules.", name)
        _GUARDS[name] = None
    return _GUARDS[name]


def _blocked_by_guard(name, text):
    """True when a Guardrails AI validator rejected the text. A crash of the guard itself is NOT
    a rejection: the rules in rules.py are always checked as well."""
    guard = _guard(name)
    if guard is None:
        return False, None
    try:
        guard.validate(text)
        return False, None
    except Exception as exc:
        if _GRValidationError is not None and isinstance(exc, _GRValidationError):
            return True, str(exc)[:200]
        log.warning("Guardrails AI error in '%s' (rules still apply): %s", name, exc)
        return False, None


# ---------------------------------------------------------------- 1. user input
def check_user_message(text, user_id=""):
    """First stop for every chat message, before any LLM sees it."""
    t = rules.normalize(text).strip()
    if not t:
        metrics.chat_message("empty")
        return GuardResult(False, "", REPLY_EMPTY, "empty")
    if len(t) > rules.MAX_MESSAGE_CHARS:
        metrics.chat_message("too-long")
        return GuardResult(False, t[: rules.MAX_MESSAGE_CHARS], REPLY_TOO_LONG, "too-long")

    blocked, why = _blocked_by_guard("chat-input", t)
    hits = rules.scan_injection(t, "chat")
    if blocked or hits:
        _event("chat-blocked", user_id, why or ", ".join(hits))
        metrics.chat_message("injection")
        return GuardResult(False, t, REPLY_BLOCKED, "injection")
    metrics.chat_message("allowed")
    return GuardResult(True, t)


def contains_injection(text, scope="chat"):
    """Names of the injection patterns found in a piece of text (empty list = clean)."""
    return rules.scan_injection(text, scope)


# ---------------------------------------------------------------- 2. bill text (indirect injection)
def prepare_bill_text(raw_text):
    """Cleans the text of an uploaded bill before it goes to an LLM.
    Returns (text_for_llm, red_flags). A bill that talks to the AI gets a red flag, and any red flag
    sends the claim to human review (see claim_service.submit_claim)."""
    text = rules.normalize(raw_text)[: rules.MAX_BILL_TEXT_CHARS]
    flags = []

    blocked, why = _blocked_by_guard("bill-input", text)
    hits = rules.scan_injection(text, "bill")
    if blocked or hits:
        flags.append(
            "bill text contains instructions aimed at an AI reviewer ("
            + (", ".join(hits) or "flagged by guardrails") + ")"
        )
        _event("bill-injection", "", why or ", ".join(hits))

    clean, kinds = rules.redact_sensitive(text)      # data minimisation: no Aadhaar/PAN/card goes to the LLM
    if kinds:
        log.info("AI_GUARD redacted %s from bill text before sending it to the LLM", ", ".join(kinds))
        metrics.pii_redacted("bill", kinds)            # NEW (MONITORING)
    return clean, flags


# ---------------------------------------------------------------- 3. structured LLM output
def _guarded_json(kind, raw):
    if not isinstance(raw, str) or not raw.strip():
        raise GuardError("empty LLM answer", "empty")
    if len(raw) > 20_000:
        raise GuardError("LLM answer is too long", "too-long")
    blocked, why = _blocked_by_guard(f"json-{kind}", raw)
    if blocked:
        raise GuardError(why or f"invalid {kind} output", f"bad-{kind}")
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise GuardError(f"not valid JSON: {exc}", f"bad-{kind}")


def parse_extraction(raw):
    """Raw LLM text -> (clean dict with amount/date/provider/patient/treatment, flags). Raises GuardError."""
    obj = _guarded_json("extraction", raw)
    try:
        return rules.validate_extraction(obj)
    except ValueError as exc:
        raise GuardError(str(exc), "bad-extraction")


def parse_audit(raw):
    """Raw LLM text -> (score 0-100, reason, red_flags). Raises GuardError."""
    obj = _guarded_json("audit", raw)
    try:
        return rules.validate_audit(obj)
    except ValueError as exc:
        raise GuardError(str(exc), "bad-audit")


def parse_intent(raw, has_file=False):
    """Raw LLM text -> (tool_name, safe_args). tool_name can be 'none' or 'claim_needs_file'."""
    obj = _guarded_json("intent", raw)
    try:
        return rules.validate_intent(obj, has_file=has_file)
    except ValueError as exc:
        raise GuardError(str(exc), "bad-intent")


# ---------------------------------------------------------------- 4. text that goes to a person
def redact(text):
    """Masks Aadhaar / PAN / card / OTP / password / API keys. Phone numbers and emails stay."""
    clean, kinds = rules.redact_sensitive(text)
    metrics.pii_redacted("handoff", kinds)             # NEW (MONITORING)
    return clean


def guard_llm_text(text):
    """Output guard for any text an LLM wrote (admin reasons, support summaries, chat answers)."""
    t = rules.normalize(text).strip()
    guard = _guard("llm-output")
    if guard is not None:
        try:
            out = guard.validate(t).validated_output
            if isinstance(out, str):
                t = out
        except Exception as exc:
            log.warning("Guardrails AI error in 'llm-output' (rules still apply): %s", exc)
    # always applied, so the guarantee does not depend on the Guardrails AI version
    t, kinds = rules.redact_sensitive(t)
    metrics.pii_redacted("llm-output", kinds)          # NEW (MONITORING)
    return rules.fix_currency(t)


_TEXT_KEYS = ("answer", "message", "note")


def guard_response(resp):
    """Output guard for whatever the orchestrator returns to the chat."""
    if isinstance(resp, str):
        return guard_llm_text(resp)
    if isinstance(resp, dict):
        out = dict(resp)
        for key in _TEXT_KEYS:
            if isinstance(out.get(key), str):
                out[key] = guard_llm_text(out[key])
        return out
    return resp


# ---------------------------------------------------------------- 5. uploads
def check_upload(filename, content, user_id=""):
    """Returns (safe_filename, None) or (None, message for the user)."""
    safe, error = rules.validate_upload(filename, content)
    if error:
        _event("upload-blocked", user_id, f"{filename!r}: {error}")
    metrics.upload("refused" if error else "accepted")   # NEW (MONITORING)
    return safe, error