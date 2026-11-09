"""
Business guardrails that run when the user presses "Submit claim".
They use the database, not an LLM, so they cannot be talked around.

  DUPLICATE      same file (sha256) or same bill (provider + date + amount) already claimed  -> BLOCK
  CROSS-USER     the same bill was also submitted by another account                         -> human review
  RE-SUBMISSION  the same bill was REJECTED before                                           -> human review
  VELOCITY       too many claims in 24 hours                                                 -> human review
  AUTO CAP       auto-approvals in the last 30 days would pass the cap                       -> human review
"""
import hashlib
import json
import re
from dataclasses import dataclass, field

from sqlalchemy import text

from app.safety import audit, config
from app.services.bill_validation import parse_bill_date

_ACTIVE = ("PENDING", "APPROVED", "FLAGGED")


@dataclass
class PolicyDecision:
    action: str = "allow"                    # "allow" | "human_review" | "block"
    reasons: list = field(default_factory=list)
    message: str = ""


def sha256_file(path: str) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def bill_fingerprint(provider, bill_date, amount) -> str | None:
    """Same clinic + same day + same amount = same bill, even if the file is a different photo."""
    prov = re.sub(r"[^a-z0-9]", "", str(provider or "").lower())
    d = parse_bill_date(bill_date)
    try:
        amt = f"{float(amount):.2f}"
    except (TypeError, ValueError):
        return None
    if not prov or prov == "unknown" or not d:
        return None
    return hashlib.sha1(f"{prov}|{d.isoformat()}|{amt}".encode()).hexdigest()


def _ago(db, days: int) -> str:
    """SQL for 'now minus N days' that works on MySQL (production) and SQLite (tests). `days` is our own int constant."""
    days = int(days)
    return f"datetime('now','-{days} day')" if db.bind.dialect.name == "sqlite" else f"DATE_SUB(NOW(), INTERVAL {days} DAY)"


def evaluate_submission(db, *, claim_pk: int, user_pk: int, amount: float, extracted: dict, auto_candidate: bool) -> PolicyDecision:
    d = PolicyDecision()
    sha, fp = extracted.get("file_sha256"), extracted.get("fingerprint")

    # 1-3: duplicates
    clauses, params = [], {"cid": claim_pk}
    if sha:
        clauses.append("c.extracted_data LIKE :sha"); params["sha"] = f'%"file_sha256": "{sha}"%'
    if fp:
        clauses.append("c.extracted_data LIKE :fp"); params["fp"] = f'%"fingerprint": "{fp}"%'
    if clauses:
        rows = db.execute(text(
            "SELECT c.user_id, c.status FROM claims c "
            f"WHERE c.id != :cid AND c.created_at >= {_ago(db, config.DUPLICATE_WINDOW_DAYS)} "
            f"AND ({' OR '.join(clauses)})"), params).fetchall()
        mine = [r for r in rows if r[0] == user_pk]
        theirs = [r for r in rows if r[0] != user_pk and r[1] in _ACTIVE + ("REJECTED",)]
        if any(r[1] in _ACTIVE for r in mine):
            d.action, d.message = "block", "This bill has already been claimed, so it can't be submitted again. Check Claim status for the earlier claim."
            d.reasons.append("duplicate of an earlier claim by the same user")
            audit.event("policy_block_duplicate", str(user_pk), claim=claim_pk)
            return d
        if any(r[1] == "REJECTED" for r in mine):
            d.reasons.append("this bill was rejected earlier and is being submitted again")
        if theirs:
            d.reasons.append("the same bill was also submitted from another account")

    # 4: velocity
    n = db.execute(text(
        "SELECT COUNT(*) FROM claims WHERE user_id = :u AND id != :cid "
        "AND status NOT IN ('DRAFT','CANCELLED','FAILED','LOW_BALANCE') "
        f"AND created_at >= {_ago(db, 1)}"), {"u": user_pk, "cid": claim_pk}).scalar() or 0
    if n >= config.MAX_CLAIMS_PER_DAY:
        d.reasons.append(f"{n} claims were already submitted in the last 24 hours")

    # 5: auto-approval cap
    if auto_candidate:
        used = float(db.execute(text(
            "SELECT COALESCE(SUM(bill_amount),0) FROM claims WHERE user_id = :u AND status = 'APPROVED' "
            f"AND confidentiality_status = 'AUTO_APPROVE' AND created_at >= {_ago(db, 30)}"), {"u": user_pk}).scalar() or 0)
        if used + float(amount) > config.AUTO_APPROVE_MONTHLY_CAP:
            d.reasons.append(f"auto-approvals this month (Rs {used:,.0f}) plus this claim would pass the Rs {config.AUTO_APPROVE_MONTHLY_CAP:,} cap")

    if d.reasons:
        d.action = "human_review"
        audit.event("policy_review", str(user_pk), claim=claim_pk, reasons="; ".join(d.reasons))
    return d
