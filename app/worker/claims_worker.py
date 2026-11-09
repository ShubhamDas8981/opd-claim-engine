import os
import json
import threading
import traceback
import uuid
import time
import requests # Added for real API calls
from app.core.kafka_client import kafka_client
from app.core.config import settings
from app.models.models import Claim, User
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from app.services.extraction_service import extract_bill_data
from app.core import storage                      # NEW (S3)
from app.monitoring import metrics
import logging
from pathlib import Path

# Gets the root directory of your project (C:\Users\SHUBHAM\Documents\claim-ops)
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Forces the log file to go into the root logs folder (where your empty file is right now)
log_filepath = BASE_DIR / "logs" / "worker.log"
log_filepath.parent.mkdir(parents=True, exist_ok=True)

# Configure logging
logging.basicConfig(
    filename=str(log_filepath),
    filemode='a',
    encoding='utf-8',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Test log
logging.info("Successfully logged to root logs/worker.log!")


engine = create_engine(settings.MYSQL_URL)
SessionLocal = sessionmaker(bind=engine)

# ---- Wallet rules ----
OPENING_BALANCE = 20000.0          # every new user starts with this (rupees)
# The approve / human-review decision now happens when the user presses
# "Submit claim" (see app/services/claim_service.py), not here.


def ensure_user_exists(user_id_str, email=None):
    """Returns the internal id of the user. Creates the user with the opening balance if new.

    NEW (EMAIL): `email` is the Email ID typed at login. New users are saved with it.
    Callers that do not know the email (for example /chat) keep the old placeholder.
    """
    db = SessionLocal()
    try:
        user = db.execute(text("SELECT id FROM users WHERE employee_id = :eid"), {"eid": user_id_str}).fetchone()
        if user:
            return user[0]

        print(f"👤 Creating user {user_id_str} with opening balance {OPENING_BALANCE}...")
        new_user = User(
            employee_id=user_id_str,
            full_name="Test User",
            email=(email or "test@example.com"),
            current_balance=OPENING_BALANCE,
        )
        db.add(new_user)
        try:
            db.commit()
        except IntegrityError:
            # another process created the same user at the same moment
            db.rollback()
            user = db.execute(text("SELECT id FROM users WHERE employee_id = :eid"), {"eid": user_id_str}).fetchone()
            return user[0] if user else None
        return new_user.id
    except Exception as e:
        print(f"❌ User Setup Error: {e}")
        return None
    finally:
        db.close()


def process_claims():
    group_id = f"claim_processor_{uuid.uuid4().hex[:6]}"
    consumer = kafka_client.get_consumer(settings.KAFKA_TOPIC_CLAIMS, group_id)
    print(f"✅ Claim Worker started. Group: {group_id}. Listening on: {settings.KAFKA_TOPIC_CLAIMS}")

    while True:
        try:
            messages = consumer.poll(timeout_ms=1000)
            if not messages:
                continue

            for tp, msg_list in messages.items():
                for message in msg_list:
                    data = message.value
                    user_id_str = data.get("user_id") or f"USER_{uuid.uuid4().hex[:8].upper()}"
                    file_path = data.get("file_data")

                    # Claim UUID generated in main.py (the MAIN claim id everywhere)
                    claim_uuid = data.get("claim_id")

                    # Fallback: recover it from the file name "uploads/<uuid>_<name>"
                    if not claim_uuid and file_path and file_path != "no_file":
                        claim_uuid = os.path.basename(file_path)[:36]

                    print(f"\n📩 [CLAIM EVENT] Received for {user_id_str} (claim {claim_uuid})")

                    # 📍 Track event arrival metric
                    metrics.worker_event("received")

                    # A claim without a bill file cannot be processed
                    if not file_path or file_path == "no_file":
                        print(f"⚠️ Skipping claim for {user_id_str}: no file attached")
                        continue

                    internal_user_id = ensure_user_exists(user_id_str)
                    if not internal_user_id:
                        print(f"❌ Skipping claim for {user_id_str} due to user resolution failure.")
                        continue

                    from app.agents.orchestrator import orchestrator

                    print(f"🔍 Extracting data from file: {file_path}")
                    with storage.local_copy(file_path) as local_path:      # NEW (S3): temporary local copy of the bill
                        extracted_data = extract_bill_data(local_path, llm_client=orchestrator.client)

                    conf_score = extracted_data.get("confidence_score", 0)
                    amount = extracted_data.get("amount", 0.0)
                    if isinstance(amount, dict):
                        amount = amount.get("amount", 0.0)
                    try:
                        amount = float(amount)
                    except (TypeError, ValueError):
                        amount = 0.0

                    # Extraction failed (for example the LLM call was rate limited)
                    extraction_failed = (not conf_score) or amount <= 0

                    db = SessionLocal()
                    try:
                        # Current balance of THIS user only
                        row = db.execute(
                            text("SELECT current_balance FROM users WHERE id = :uid"),
                            {"uid": internal_user_id},
                        ).fetchone()
                        balance = float(row[0] or 0) if row else 0.0

                        if extraction_failed:
                            claim_status = "FAILED"
                            reason = "Automatic extraction failed. Please re-upload the bill."
                            print("⚠️ Extraction failed, claim marked FAILED")

                        elif amount > balance:
                            claim_status = "LOW_BALANCE"
                            reason = (
                                f"Insufficient balance: bill amount ₹{amount:.2f} is more than "
                                f"the available balance ₹{balance:.2f}."
                            )
                            print(f"⛔ Not enough balance (bill {amount}, balance {balance}), not processing further")

                        else:
                            # DRAFT = read from the bill, waiting for the user to confirm.
                            # Drafts are NOT shown to the admin and do NOT touch the balance.
                            claim_status = "DRAFT"
                            reason = None

                        print(f"💾 Writing claim to MySQL (User ID: {internal_user_id}, status: {claim_status})...")
                        new_claim = Claim(
                            user_id=internal_user_id,
                            claim_uuid=claim_uuid,
                            bill_amount=float(amount),
                            extracted_data=json.dumps(extracted_data),
                            confidentiality_score=None,
                            confidentiality_status=None,
                            confidence_value=conf_score / 100.0 if conf_score else 0.0,
                            review_reason=reason,
                            status=claim_status,
                            file_path=file_path,
                        )
                        db.add(new_claim)
                        db.commit()
                        print(f"✅ DB COMMIT SUCCESS: Claim {claim_uuid} saved as {claim_status}")
                        # 📍 Track successful save metric
                        metrics.worker_event("saved")
                    except Exception as db_err:
                        print(f"❌ DATABASE ERROR: {db_err}")
                        db.rollback()
                        metrics.db_error("write_claim", db_err)
                        metrics.worker_event("failed")
                    finally:
                        db.close()
        except Exception as e:
            print(f"❌ WORKER ERROR: {e}")
            traceback.print_exc()


def process_tickets():
    group_id = f"ticket_processor_{uuid.uuid4().hex[:6]}"
    consumer = kafka_client.get_consumer(settings.KAFKA_TOPIC_TICKETS, group_id)
    print(f"✅ Ticket Worker started. Group: {group_id}. Listening on: {settings.KAFKA_TOPIC_TICKETS}")

    for message in consumer:
        try:
            data = message.value
            user_id = data.get("user_id")
            query = data.get("query")

            print(f"🚀 [ESCALATION] Received for {user_id}. Attempting to create HubSpot ticket...")

            # REAL HUBSPOT API CALL
            hubspot_url = "https://api.hubapi.com/crm/v3/objects/tickets"
            headers = {
                "Authorization": f"Bearer {settings.HUBSPOT_API_KEY}",
                "Content-Type": "application/json"
            }
            payload = {
                "properties": {
                    "subject": f"OPD Wallet Escalation: {user_id}",
                    "content": query,
                    "hs_pipeline": "default",
                    "hs_pipeline_stage": "open"
                }
            }

            response = requests.post(hubspot_url, headers=headers, json=payload, timeout=10)

            if response.status_code in [200, 201]:
                print(f"✅ HubSpot Ticket Created Successfully! ID: {response.json().get('id')}")
            else:
                print(f"❌ HubSpot API Error ({response.status_code}): {response.text}")

        except Exception as e:
            print(f"❌ Ticket Error: {e}")

if __name__ == "__main__":
    metrics.start_worker_metrics_server(8001)
    t1 = threading.Thread(target=process_claims, daemon=True)
    t2 = threading.Thread(target=process_tickets, daemon=True)
    t1.start()
    t2.start()
    print("🚀 All workers are active. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Shutting down workers...")