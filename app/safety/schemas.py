"""Pydantic contracts for everything an LLM returns. Guard.for_pydantic enforces them."""
import re
from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

TOOLS = ("balance_tool", "claim_status_tool", "claim_flow_tool", "policy_tool", "escalation_tool", "none")
ALLOWED_TOOL_ARGS = {"query", "date_from", "date_to"}       # user_id / file / claim_id are set by the server, never by the LLM
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}(:\d{2})?)?$")


class IntentDecision(BaseModel):
    tool: Literal["balance_tool", "claim_status_tool", "claim_flow_tool", "policy_tool", "escalation_tool", "none"]
    args: dict = Field(default_factory=dict)


def clean_args(args: dict, message: str) -> dict:
    """Keep only allowed keys, only ISO dates, only short strings."""
    out = {}
    for k, v in (args or {}).items():
        if k not in ALLOWED_TOOL_ARGS or not isinstance(v, str):
            continue
        if k in ("date_from", "date_to"):
            if _ISO.match(v.strip()):
                out[k] = v.strip()
        else:
            out[k] = v[:300]
    out.setdefault("query", message[:300])
    return out


class BillExtraction(BaseModel):
    amount: float = Field(ge=0, le=10_000_000)
    date: str = Field(max_length=40)
    provider: str = Field(max_length=120)
    patient: str = Field(default="Unknown", max_length=120)
    treatment: str = Field(default="Unknown", max_length=300)


class BillAudit(BaseModel):
    score: int = Field(ge=0, le=100)
    reason: str = Field(max_length=600)
    red_flags: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("red_flags")
    @classmethod
    def _short_flags(cls, v):
        return [str(x)[:200] for x in v]


def coerce_extraction(d: dict) -> dict:
    """LLMs often return "Rs 600" or null. Fix the easy cases before the strict check."""
    d = dict(d or {})
    a = d.get("amount")
    if isinstance(a, str):
        m = re.search(r"\d[\d,]*(?:\.\d+)?", a)
        d["amount"] = float(m.group().replace(",", "")) if m else 0.0
    elif a is None:
        d["amount"] = 0.0
    for k in ("date", "provider", "patient", "treatment"):
        if d.get(k) is None or isinstance(d.get(k), (dict, list)):
            d[k] = "Unknown"
        else:
            d[k] = str(d[k])
    return {k: d[k] for k in ("amount", "date", "provider", "patient", "treatment") if k in d}
