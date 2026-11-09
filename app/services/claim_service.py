"""
Claim lifecycle AFTER extraction:

    DRAFT  --edit-->    DRAFT (values changed) or LOW_BALANCE
    DRAFT  --submit-->  APPROVED (auto) | PENDING (human review) | LOW_BALANCE
    DRAFT  --cancel-->  CANCELLED

Put this file at app/services/claim_service.py
"""
import os
import re
import json
import traceback
from datetime import date

from sqlalchemy import text

from app.core.config import settings
from app.worker.claims_worker import SessionLocal
from app.core import storage                      # NEW (S3)
from app.ai_guards import guards                   # NEW (GUARDRAILS)
from app.monitoring import metrics                 # NEW (MONITORING)

AUTO_APPROVE_MAX_AMOUNT = 1000     # auto-approve only below this amount (rupees)
AUTO_APPROVE_MIN_CONFIDENCE = 80   # and only at or above this confidence (0-100)

EDIT_HELP = (
    "I could not find anything to change. Try one of these:\n"
    "• date is 11 Oct\n"
    "• amount is 750\n"
    "• provider is City Clinic\n"
    "You can also send more than one change in a single message."
)

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

_NUM = r"(\d[\d,]*(?:\.\d+)?)"


# ---------------------------------------------------------------- helpers
def _rupees(value):
    value = float(value)
    return "₹" + (f"{value:,.0f}" if value == int(value) else f"{value:,.2f}")


def _pick(d, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", "Unknown") and not isinstance(v, (dict, list)):
            return v
    return None


def _load(extracted_text):
    try:
        d = json.loads(extracted_text) if extracted_text else {}
    except (TypeError, ValueError):
        d = {}
    return d if isinstance(d, dict) else {}


def _fetch_claim(db, claim_uuid, employee_id):
    """Only returns the claim if it belongs to this user."""
    return db.execute(
        text(
            "SELECT c.id, c.user_id, c.bill_amount, c.status, c.confidence_value, "
            "c.extracted_data, c.file_path, u.current_balance "
            "FROM claims c JOIN users u ON c.user_id = u.id "
            "WHERE c.claim_uuid = :cid AND u.employee_id = :eid"
        ),
        {"cid": claim_uuid, "eid": employee_id},
    ).fetchone()


# ---------------------------------------------------------------- parsing the chat message
def parse_amount(message):
    t = message.lower()
    patterns = [
        r"(?:amount|total|bill|price|cost)\s*(?:is|to|=|:|as|should be|was)?\s*(?:rs\.?|inr|₹)?\s*" + _NUM,
        r"(?:rs\.?|inr|₹)\s*" + _NUM,
        _NUM + r"\s*(?:rs\b|rupees|inr\b)",
    ]
    for p in patterns:
        m = re.search(p, t)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                continue
    return None


def _make_date(y, m, d):
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_date(message, default_year):
    t = message.lower()

    # 2024-11-21
    m = re.search(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b", t)
    if m:
        d = _make_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            return d

    # 21/11/2024 (day first)
    m = re.search(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})\b", t)
    if m:
        d = _make_date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        if d:
            return d

    # 11 oct, 11th october 2024
    for m in re.finditer(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]{3,9})\.?(?:,?\s*(\d{4}))?\b", t):
        mon = MONTHS.get(m.group(2))
        if mon:
            d = _make_date(int(m.group(3) or default_year), mon, int(m.group(1)))
            if d:
                return d

    # oct 11, october 11th 2024
    for m in re.finditer(r"\b([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?\b", t):
        mon = MONTHS.get(m.group(1))
        if mon:
            d = _make_date(int(m.group(3) or default_year), mon, int(m.group(2)))
            if d:
                return d

    return None


def parse_provider(message):
    m = re.search(
        r"\b(?:provider|clinic|hospital|doctor)(?:\s+name)?\s*(?:is|to|=|:|as)\s*(.+)",
        message,
        re.I,
    )
    if not m:
        return None
    # stop at ", date ..." / "and amount ..." when several changes are in one message
    value = re.split(r"\s*(?:,|;|\band\b)\s*(?=(?:date|amount|total|bill)\b)", m.group(1), maxsplit=1, flags=re.I)[0]
    value = value.strip().strip("\"'").strip(" .")
    return value[:80] if len(value) >= 2 else None


# ---------------------------------------------------------------- reasons for the admin
def _edit_reason(extracted, amount):
    orig = extracted.get("original_values") or {}
    parts = []
    if "amount" in orig and orig["amount"] is not None:
        parts.append(f"amount changed from {_rupees(orig['amount'])} to {_rupees(amount)}")
    if "date" in orig:
        parts.append(f"date changed from {orig['date'] or 'not found'} to {_pick(extracted, 'date', 'bill_date') or 'not found'}")
    if "provider" in orig:
        parts.append(f"provider changed from {orig['provider'] or 'not found'} to {_pick(extracted, 'provider', 'provider_name') or 'not found'}")
    detail = "; ".join(parts) if parts else "details were changed"

    # NEW: if the original bill also had red flags, the admin should see them too
    flags = extracted.get("red_flags") or []
    flag_text = (" The original bill also had red flags: " + "; ".join(flags[:5]) + ".") if flags else ""

    return (
        f"The user edited the extracted bill details after upload ({detail}). "
        "Verify against the uploaded bill before approval." + flag_text
    )


def _human_review_reason(extracted, conf, amount):
    parts = []
    if amount >= AUTO_APPROVE_MAX_AMOUNT:
        parts.append(f"{_rupees(amount)} is above the {_rupees(AUTO_APPROVE_MAX_AMOUNT)} auto-approval limit")
    if conf < AUTO_APPROVE_MIN_CONFIDENCE:
        parts.append(f"extraction confidence {conf:.0f}% is below the {AUTO_APPROVE_MIN_CONFIDENCE}% needed")
    fallback = (
        "Manual verification required: " + "; ".join(parts) + "."
        if parts else (extracted.get("confidence_reason") or "Manual verification required.")
    )

    try:
        from app.agents.orchestrator import orchestrator

        prompt = f"""
        The following medical bill was extracted, but it requires human review.
        Extracted Data: {json.dumps(extracted)}
        Confidence Score: {conf}
        Amount: {amount}

        Based on these details, provide a concise, professional, and concrete reason why a human must approve this claim.
        IMPORTANT: The currency for all amounts is Indian Rupees (₹). Use the '₹' symbol, NOT '$'.
        Focus on the discrepancy or the risk (e.g., 'High amount of ₹{amount} exceeds auto-approval limit', 'Confidence score is too low due to blurry text', 'Amount does not match expected patterns').

        Return ONLY the reason text.
        """
        completion = orchestrator.client.chat.completions.create(
            model=settings.GROQ_MODEL,
            messages=[
                {"role": "system", "content": "You are a medical insurance auditor."},
                {"role": "user", "content": prompt},
            ],
        )
        # NEW (GUARDRAILS): output guard (card/Aadhaar/keys masked, amounts in rupees)
        reason = guards.guard_llm_text(completion.choices[0].message.content or "")
        return reason or fallback
    except Exception as e:
        print(f"❌ Error generating detailed reason: {e}")
        return fallback


# ---------------------------------------------------------------- EDIT
def edit_claim(claim_uuid, employee_id, message):
    message = (message or "").strip()[:300]
    db = SessionLocal()
    try:
        row = _fetch_claim(db, claim_uuid, employee_id)
        if not row:
            return {"status": "error", "message": "Claim not found."}

        claim_id, _uid, bill_amount, claim_status, _conf, extracted_text, _fp, balance = row
        if claim_status != "DRAFT":
            return {"status": "error", "message": "This claim has already been submitted, so it can't be edited."}

        extracted = _load(extracted_text)
        balance = float(balance or 0)
        cur_amount = float(bill_amount or 0)
        cur_date = _pick(extracted, "date", "bill_date")
        cur_provider = _pick(extracted, "provider", "provider_name")

        # "11 Oct" has no year: use the year of the date we already read
        default_year = date.today().year
        if cur_date and re.match(r"\d{4}", str(cur_date)):
            default_year = int(str(cur_date)[:4])

        new_amount = parse_amount(message)
        new_date = parse_date(message, default_year)
        new_provider = parse_provider(message)
        # NEW (GUARDRAILS): the provider name is later read by LLMs, so it must not carry instructions
        if new_provider and guards.contains_injection(new_provider, "bill"):
            return {"status": "no_change",
                    "message": "That provider name can't be used. Please type the name exactly as it is on your bill."}

        if new_amount is None and new_date is None and not new_provider:
            return {"status": "no_change", "message": EDIT_HELP}
        if new_amount is not None and new_amount <= 0:
            return {"status": "no_change", "message": "The amount must be more than zero."}
        if new_date is not None and new_date > date.today():
            return {"status": "no_change", "message": "A bill date can't be in the future."}

        originals = extracted.setdefault("original_values", {})
        conf = extracted.get("confidence")
        if not isinstance(conf, dict):
            conf = {}
            extracted["confidence"] = conf

        changes, changed_fields = [], []

        if new_amount is not None and new_amount != cur_amount:
            originals.setdefault("amount", cur_amount)
            extracted["amount"] = new_amount
            conf["amount"] = 1.0
            changes.append(f"amount to {_rupees(new_amount)}")
            changed_fields.append("amount")

        if new_date is not None and new_date.isoformat() != str(cur_date):
            originals.setdefault("date", cur_date)
            extracted["date"] = new_date.isoformat()
            conf["date"] = 1.0
            changes.append(f"date to {new_date.isoformat()}")
            changed_fields.append("date")

        if new_provider and new_provider != cur_provider:
            originals.setdefault("provider", cur_provider)
            extracted["provider"] = new_provider
            conf["provider"] = 1.0
            changes.append(f"provider to {new_provider}")
            changed_fields.append("provider")

        if not changes:
            return {"status": "no_change", "message": "Those values already match what we read from your bill."}

        edited = set(extracted.get("user_edited") or [])
        edited.update(changed_fields)
        extracted["user_edited"] = sorted(edited)

        amount_now = new_amount if new_amount is not None else cur_amount

        # Edited amount is now more than the balance: stop here
        if amount_now > balance:
            res = db.execute(
                text("UPDATE claims SET status = 'LOW_BALANCE', bill_amount = :amt, extracted_data = :ed, "
                     "review_reason = :rr WHERE id = :id AND status = 'DRAFT'"),
                {
                    "amt": amount_now,
                    "ed": json.dumps(extracted),
                    "rr": f"Insufficient balance: bill amount {_rupees(amount_now)} is more than the available balance {_rupees(balance)}.",
                    "id": claim_id,
                },
            )
            db.commit()
            if res.rowcount != 1:
                return {"status": "error", "message": "This claim has already been submitted, so it can't be edited."}
            return {"status": "low_balance", "data": {"amount": amount_now, "balance": round(balance, 2)}}

        res = db.execute(
            text("UPDATE claims SET bill_amount = :amt, extracted_data = :ed WHERE id = :id AND status = 'DRAFT'"),
            {"amt": amount_now, "ed": json.dumps(extracted), "id": claim_id},
        )
        db.commit()
        if res.rowcount != 1:
            return {"status": "error", "message": "This claim has already been submitted, so it can't be edited."}

        return {
            "status": "updated",
            "message": "Updated " + ", ".join(changes) + ". Please check it again.",
            "data": {
                "amount": amount_now,
                "score": None,
                "extracted": json.dumps(extracted),
                "balance": round(balance, 2),
            },
        }
    except Exception as e:
        db.rollback()
        print(f"❌ edit_claim error: {e}")
        traceback.print_exc()
        return {"status": "error", "message": "Something went wrong while updating the claim."}
    finally:
        db.close()


# ---------------------------------------------------------------- SUBMIT
def submit_claim(claim_uuid, employee_id):
    db = SessionLocal()
    try:
        row = _fetch_claim(db, claim_uuid, employee_id)
        if not row:
            return {"status": "error", "message": "Claim not found."}

        claim_id, user_id, bill_amount, claim_status, conf_value, extracted_text, _fp, balance = row
        amount = float(bill_amount or 0)
        balance = float(balance or 0)

        if claim_status == "LOW_BALANCE":
            return {"status": "low_balance", "amount": amount, "balance": round(balance, 2)}
        if claim_status == "CANCELLED":
            return {"status": "error", "message": "This claim was cancelled."}
        if claim_status == "FAILED":
            return {"status": "error", "message": "This bill could not be read. Please upload it again."}
        if claim_status != "DRAFT":
            return {"status": "already_submitted"}
        if amount <= 0:
            return {"status": "error", "message": "The bill amount must be more than zero."}

        # Balance may have changed since the upload
        if amount > balance:
            db.execute(
                text("UPDATE claims SET status = 'LOW_BALANCE', review_reason = :rr WHERE id = :id AND status = 'DRAFT'"),
                {"rr": f"Insufficient balance: bill amount {_rupees(amount)} is more than the available balance {_rupees(balance)}.",
                 "id": claim_id},
            )
            db.commit()
            return {"status": "low_balance", "amount": amount, "balance": round(balance, 2)}

        extracted = _load(extracted_text)
        conf = float(conf_value or 0) * 100
        user_edited = bool(extracted.get("user_edited"))
        red_flags = extracted.get("red_flags") or []     # NEW: written by extraction_service

        if user_edited:
            # Edited values can never be auto-approved: a person must compare them with the bill
            new_status, score = "PENDING", "HUMAN_REVIEW"
            reason = _edit_reason(extracted, amount)
        elif red_flags:
            # NEW: the bill itself looks suspicious, so never auto-approve it, whatever the amount
            new_status, score = "PENDING", "HUMAN_REVIEW"
            reason = "Manual verification required: " + "; ".join(red_flags[:5]) + "."
        elif amount < AUTO_APPROVE_MAX_AMOUNT and conf >= AUTO_APPROVE_MIN_CONFIDENCE:
            new_status, score = "APPROVED", "AUTO_APPROVE"
            reason = extracted.get("confidence_reason") or "Auto-approved based on high confidence score"
        else:
            new_status, score = "PENDING", "HUMAN_REVIEW"
            reason = _human_review_reason(extracted, conf, amount)

        # Win the claim first: only one request can move it out of DRAFT
        res = db.execute(
            text("UPDATE claims SET status = :st, confidentiality_score = :sc, confidentiality_status = :sc, "
                 "review_reason = :rr WHERE id = :id AND status = 'DRAFT'"),
            {"st": new_status, "sc": score, "rr": reason, "id": claim_id},
        )
        if res.rowcount != 1:
            db.rollback()
            return {"status": "already_submitted"}

        if new_status == "APPROVED":
            deducted = db.execute(
                text("UPDATE users SET current_balance = current_balance - :amt "
                     "WHERE id = :uid AND current_balance >= :amt"),
                {"amt": amount, "uid": user_id},
            )
            if deducted.rowcount != 1:
                db.execute(
                    text("UPDATE claims SET status = 'LOW_BALANCE', review_reason = :rr WHERE id = :id"),
                    {"rr": "Insufficient balance at the time of approval.", "id": claim_id},
                )
                db.commit()
                return {"status": "low_balance", "amount": amount, "balance": round(balance, 2)}

            db.execute(
                text("INSERT INTO transactions (user_id, amount, category, transaction_type, description) "
                     "VALUES (:uid, :amt, 'Claim Reimbursement', 'DEBIT', :desc)"),
                {"uid": user_id, "amt": amount, "desc": f"Claim {claim_uuid} auto-approved"},
            )

        db.commit()
        metrics.claim_decision("approved" if new_status == "APPROVED" else "pending", amount)   # NEW (MONITORING)

        if new_status == "APPROVED":
            new_balance = db.execute(
                text("SELECT current_balance FROM users WHERE id = :uid"), {"uid": user_id}
            ).fetchone()
            return {
                "status": "approved",
                "amount": amount,
                "balance": round(float(new_balance[0] or 0), 2) if new_balance else None,
            }
        return {"status": "pending", "amount": amount}

    except Exception as e:
        db.rollback()
        print(f"❌ submit_claim error: {e}")
        metrics.claim_decision("error")                      # NEW (MONITORING)
        traceback.print_exc()
        return {"status": "error", "message": "Something went wrong while submitting the claim."}
    finally:
        db.close()


# ---------------------------------------------------------------- CANCEL
def cancel_claim(claim_uuid, employee_id):
    db = SessionLocal()
    try:
        row = _fetch_claim(db, claim_uuid, employee_id)
        if not row:
            return {"status": "error", "message": "Claim not found."}

        claim_id, _uid, _amt, claim_status, _conf, _ex, file_path, _bal = row
        if claim_status == "CANCELLED":
            return {"status": "cancelled"}
        if claim_status != "DRAFT":
            return {"status": "error", "message": "This claim has already been submitted, so it can't be cancelled here."}

        res = db.execute(
            text("UPDATE claims SET status = 'CANCELLED' WHERE id = :id AND status = 'DRAFT'"),
            {"id": claim_id},
        )
        db.commit()
        if res.rowcount != 1:
            return {"status": "error", "message": "This claim has already been submitted, so it can't be cancelled here."}

        # The user withdrew the bill, so the uploaded file is removed too
        storage.delete(file_path)                          # NEW (S3): removes the local file or the S3 object

        return {"status": "cancelled"}
    except Exception as e:
        db.rollback()
        print(f"❌ cancel_claim error: {e}")
        return {"status": "error", "message": "Something went wrong while cancelling the claim."}
    finally:
        db.close()