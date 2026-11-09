import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pdfplumber
from app.core.config import settings


def extract_text_from_file(file_path):
    """Read text from PDF or TXT bill files."""
    try:
        path = Path(file_path)
        if path.suffix.lower() == ".pdf":
            pages = []
            with pdfplumber.open(str(path)) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text() or ""
                    if page_text.strip():
                        pages.append(page_text)
            return "\n".join(pages)

        if path.suffix.lower() == ".txt":
            return path.read_text(encoding="utf-8", errors="replace")

        return ""
    except Exception as exc:
        print(f"File read error: {exc}")
        return ""


def _json_object(value):
    """Parse JSON objects, fenced JSON and accidentally double-encoded JSON."""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}

    candidate = value.strip()
    candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\s*```$", "", candidate)

    for _ in range(2):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, str):
                candidate = parsed
                continue
            return {}
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", candidate, flags=re.DOTALL)
            if not match:
                return {}
            try:
                parsed = json.loads(match.group(0))
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}
    return {}


def _amount(value):
    """Normalize amount to a numeric INR value, not a dict/JSON string."""
    if isinstance(value, dict):
        value = value.get("amount", value.get("total", value.get("grand_total", 0)))

    if isinstance(value, str):
        parsed = _json_object(value) if value.strip().startswith("{") else {}
        if parsed:
            value = parsed.get("amount", parsed.get("total", parsed.get("grand_total", 0)))
        else:
            value = re.sub(r"[₹,\s]", "", value)
            match = re.search(r"-?\d+(?:\.\d{1,2})?", value)
            value = match.group(0) if match else 0

    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            return 0.0
        return float(result.quantize(Decimal("0.01")))
    except (InvalidOperation, TypeError, ValueError):
        return 0.0


def normalize_extracted_data(data):
    data = _json_object(data) if isinstance(data, str) else (data or {})
    amount_value = data.get("amount", data.get("total", data.get("grand_total", 0)))
    provider = data.get("provider") or data.get("provider_name") or data.get("clinic_name")
    bill_date = data.get("date") or data.get("bill_date") or data.get("invoice_date")

    try:
        confidence = int(float(data.get("confidence_score", 0)))
    except (TypeError, ValueError):
        confidence = 0

    return {
        "amount": _amount(amount_value),
        "date": str(bill_date).strip() if bill_date else "Unknown",
        "provider": str(provider).strip() if provider else "Unknown",
        "confidence_score": max(0, min(100, confidence)),
        "confidence_reason": str(data.get("confidence_reason") or ""),
    }


def calculate_confidentiality(llm_client, raw_text, extracted_data):
    """
    Score extraction evidence/consistency. This is not a fraud determination.
    """
    try:
        prompt = f"""
Assess whether the extracted fields are supported by the bill text.
Do not claim the document is authentic or fraud-free; text extraction cannot prove that.
Return a score from 0 to 100 and a short evidence-based reason.

BILL TEXT:
{raw_text[:18000]}

EXTRACTED DATA:
{json.dumps(extracted_data, ensure_ascii=False)}

Return only JSON: {{"score": 0, "reason": "brief explanation"}}
"""
        completion = llm_client.chat.completions.create(
            model=settings.GROQ_MODEL,
            messages=[
                {"role": "system", "content": "You review medical bill extraction quality carefully."},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        result = _json_object(completion.choices[0].message.content)
        score = max(0, min(100, int(float(result.get("score", 0)))))
        return score, str(result.get("reason") or "No reason provided.")
    except Exception as exc:
        print(f"Confidence calculation error: {exc}")
        return 0, "Could not calculate extraction confidence."


def extract_bill_data(file_path, llm_client=None):
    """Extract provider, final bill total and date in a stable JSON schema."""
    raw_text = extract_text_from_file(file_path)
    if not raw_text.strip():
        return {
            "amount": 0.0,
            "date": "Unknown",
            "provider": "Unknown",
            "confidence_score": 0,
            "confidence_reason": "No readable text found in the uploaded file.",
        }

    if llm_client is None:
        return {
            "amount": 0.0,
            "date": "Unknown",
            "provider": "Unknown",
            "confidence_score": 0,
            "confidence_reason": "LLM client is unavailable.",
        }

    prompt = f"""
Extract the medical provider/clinic name, bill date and FINAL PAYABLE TOTAL.
Use the grand total/net payable/amount paid, not a subtotal or line item.
Provider means the clinic, hospital, dentist or medical service provider printed on the bill.
Do not put a JSON object inside any field.
If not found, use "Unknown" for provider/date and 0.0 for amount.
Return only this JSON shape:
{{"amount": 2600.0, "date": "02 Nov 2026", "provider": "City Dental Clinic"}}

BILL TEXT:
{raw_text[:18000]}
"""
    try:
        completion = llm_client.chat.completions.create(
            model=settings.GROQ_MODEL,
            messages=[
                {"role": "system", "content": "You extract structured medical bill data. Output valid JSON only."},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        extracted = normalize_extracted_data(completion.choices[0].message.content)
        score, reason = calculate_confidentiality(llm_client, raw_text, extracted)
        extracted["confidence_score"] = score
        extracted["confidence_reason"] = reason
        return extracted
    except Exception as exc:
        print(f"LLM extraction error: {exc}")
        return {
            "amount": 0.0,
            "date": "Unknown",
            "provider": "Unknown",
            "confidence_score": 0,
            "confidence_reason": "LLM extraction failed.",
        }
