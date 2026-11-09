import pdfplumber
import re
import json
import os
from app.core.config import settings

def extract_text_from_file(file_path):
    """
    Reads text from PDF or TXT files.
    """
    text = ""
    try:
        if file_path.lower().endswith('.pdf'):
            with pdfplumber.open(file_path) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text += page_text + "\n"
        elif file_path.lower().endswith('.txt'):
            with open(file_path, 'r', encoding='utf-8') as f:
                text = f.read()
        else:
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    text = f.read()
            except:
                pass
    except Exception as e:
        print(f"❌ File Read Error: {e}")
    return text

def extract_bill_data(file_path, llm_client=None):
    """
    Extracts amount, date, and provider from a bill using LLM for better accuracy.
    """
    # Step 1: Parse raw text from file
    raw_text = extract_text_from_file(file_path)

    if not raw_text.strip():
        return {"amount": 0.0, "date": "Unknown", "provider": "Unknown"}

    # Step 2: Use LLM to extract structured data
    if llm_client:
        try:
            prompt = f"""
            Extract the following details from the bill text provided below.
            Return ONLY a JSON object with keys: "amount" (float), "date" (string), "provider" (string).
            If any field is not found, use "Unknown" for strings or 0.0 for amount.

            BILL TEXT:
            {raw_text}
            """
            completion = llm_client.chat.completions.create(
                model=settings.GROQ_MODEL,
                messages=[
                    {"role": "system", "content": "You are a precise medical bill extraction agent."},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"}
            )
            return json.loads(completion.choices[0].message.content)
        except Exception as e:
            print(f"❌ LLM Extraction Error: {e}")

    # Fallback to regex if no LLM client provided
    return {"amount": 0.0, "date": "Unknown", "provider": "Unknown"}
