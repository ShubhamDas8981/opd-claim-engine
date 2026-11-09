"""Structured guardrail log (JSON lines). Values are PII-masked and cut short, so the log itself is safe to ship to a log tool."""
import json
import logging
import os
import time
from logging.handlers import RotatingFileHandler

from app.safety import config
from app.safety.pii import mask_pii

_logger = logging.getLogger("opd.guardrails")
_logger.setLevel(logging.INFO)
_logger.propagate = False

if not _logger.handlers:
    try:
        os.makedirs(os.path.dirname(config.AUDIT_LOG_PATH) or ".", exist_ok=True)
        _h = RotatingFileHandler(config.AUDIT_LOG_PATH, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    except OSError:                               # read-only disk: fall back to the console
        _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_h)


def _safe(v):
    return mask_pii(str(v))[:300]


def event(kind: str, user_id: str | None = None, **detail):
    """kind examples: input_blocked, output_fixed, upload_rejected, llm_reask, policy_review, rate_limited"""
    rec = {"ts": round(time.time(), 3), "kind": kind, "user": _safe(user_id) if user_id else None,
           **{k: _safe(v) for k, v in detail.items()}}
    _logger.info(json.dumps(rec, ensure_ascii=False))
    return rec
