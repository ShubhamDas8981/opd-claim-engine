import json
import threading
import traceback
import uuid
import time
import requests

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.kafka_client import kafka_client
from app.core.config import settings
from app.models.models import Claim, User
from app.services.extraction_service import extract_bill_data, normalize_extracted_data


engine = create_engine(settings.MYSQL_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def ensure_user_exists(user_id_str):
    """Resolve employee_id to the internal users.id foreign key."""
    db = SessionLocal()
    try:
        row = db.execute(
            text("SELECT id FROM users WHERE employee_id = :eid"),
            {"eid": str(user_id_str)},
        ).fetchone()
        if row:
            return row[0]

        # Demo fallback retained from the original project. Disable/remove for production.
        print(f"WARNING: creating demo user for unknown employee_id={user_id_str}")
        user = User(
            employee_id=str(user_id_str),
            full_name="Test User",
            email=f"{user_id_str}@example.invalid",
            current_balance=1000.0,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user.id
    except Exception as exc:
        db.rollback()
        print(f"User resolution error: {exc}")
        return None
    finally:
        db.close()


def process_claims():
    group_id = f"claim_processor_{uuid.uuid4().hex[:6]}"
    consumer = kafka_client.get_consumer(settings.KAFKA_TOPIC_CLAIMS, group_id)
    print(f"Claim worker started. Group={group_id}, topic={settings.KAFKA_TOPIC_CLAIMS}")

    while True:
        try:
            messages = consumer.poll(timeout_ms=1000)
            for _, msg_list in (messages or {}).items():
                for message in msg_list:
                    event = message.value or {}
                    employee_id = event.get("user_id")
                    file_path = event.get("file_data")

                    if not employee_id or not file_path:
                        print("Skipping malformed claim event: user_id/file_data missing.")
                        continue

                    internal_user_id = ensure_user_exists(employee_id)
                    if internal_user_id is None:
                        print(f"Could not resolve user {employee_id}; skipping claim.")
                        continue

                    from app.agents.orchestrator import orchestrator

                    extracted = normalize_extracted_data(
                        extract_bill_data(file_path, llm_client=orchestrator.client)
                    )
                    amount = float(extracted["amount"])
                    confidence = int(extracted["confidence_score"])

                    # Extraction confidence is NOT the claim approval status.
                    # Every claim starts pending; an admin decision changes Claim.status.
                    review_classification = (
                        "HUMAN_REVIEW"
                        if confidence < 70 or amount <= 0 or extracted["provider"] == "Unknown"
                        else "EXTRACTION_CHECKED"
                    )

                    db = SessionLocal()
                    try:
                        claim = Claim(
                            user_id=internal_user_id,
                            bill_amount=amount,
                            extracted_data=json.dumps(extracted, ensure_ascii=False),
                            confidentiality_score=review_classification,
                            file_path=file_path,
                        )

                        # Your AdminPanel expects c.status. This assumes Claim has a
                        # mapped status column; add it in models.py + migrate if absent.
                        if hasattr(type(claim), "status") or hasattr(claim, "status"):
                            claim.status = "PENDING"
                        elif hasattr(type(claim), "claim_status") or hasattr(claim, "claim_status"):
                            claim.claim_status = "PENDING"
                        else:
                            print("ERROR: Claim model has no status/claim_status field; add a DB status column.")

                        db.add(claim)
                        db.commit()
                        db.refresh(claim)
                        print(
                            f"Saved claim id={getattr(claim, 'id', 'unknown')}, "
                            f"amount={amount:.2f}, extraction_review={review_classification}, "
                            f"claim_status={getattr(claim, 'status', getattr(claim, 'claim_status', 'MISSING'))}"
                        )
                    except Exception as db_error:
                        db.rollback()
                        print(f"Claim database error: {db_error}")
                    finally:
                        db.close()

        except Exception as exc:
            print(f"Claim worker error: {exc}")
            traceback.print_exc()
            time.sleep(2)


def process_tickets():
    group_id = f"ticket_processor_{uuid.uuid4().hex[:6]}"
    consumer = kafka_client.get_consumer(settings.KAFKA_TOPIC_TICKETS, group_id)
    print(f"Ticket worker started. Group={group_id}, topic={settings.KAFKA_TOPIC_TICKETS}")

    for message in consumer:
        try:
            event = message.value or {}
            user_id = event.get("user_id")
            query = event.get("query")

            if not settings.HUBSPOT_API_KEY:
                print("HubSpot ticket skipped: HUBSPOT_API_KEY is not configured.")
                continue

            response = requests.post(
                "https://api.hubapi.com/crm/v3/objects/tickets",
                headers={
                    "Authorization": f"Bearer {settings.HUBSPOT_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "properties": {
                        "subject": f"OPD Wallet Escalation: {user_id}",
                        "content": str(query or ""),
                        "hs_pipeline": "default",
                        "hs_pipeline_stage": "open",
                    }
                },
                timeout=15,
            )

            if response.status_code in (200, 201):
                print(f"HubSpot ticket created: {response.json().get('id')}")
            else:
                print(f"HubSpot API error ({response.status_code}): {response.text[:1000]}")
        except Exception as exc:
            print(f"Ticket worker error: {exc}")
            traceback.print_exc()


if __name__ == "__main__":
    claims_thread = threading.Thread(target=process_claims, daemon=True)
    tickets_thread = threading.Thread(target=process_tickets, daemon=True)
    claims_thread.start()
    tickets_thread.start()
    print("Workers are running. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Shutting down workers...")
