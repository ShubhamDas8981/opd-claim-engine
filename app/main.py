from fastapi import FastAPI, Request, UploadFile, File, Form, Body, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse   # NEW (S3)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from app.agents.orchestrator import orchestrator
from app.core.cache import cache
from app.core import storage                      # NEW (S3)
from app.core.config import settings
from app.models.models import Base
from app.worker.claims_worker import ensure_user_exists
from app.ai_guards import guards                  # NEW (GUARDRAILS)
from app.monitoring import metrics                # NEW (MONITORING)
from fastapi import Response                      # NEW (MONITORING)
from app.services import claim_service
from app.services import handoff_service          # NEW (HANDOFF)
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
import asyncio
import re                                          # NEW (EMAIL)
import time                                        # NEW (HANDOFF)
import uuid
import os
import jwt
from pydantic import BaseModel
from datetime import datetime, timedelta

import logging
from pathlib import Path

# Gets the root directory of your project (C:\Users\SHUBHAM\Documents\claim-ops)
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Forces the log file to go into the root logs folder (where your empty file is right now)
log_filepath = BASE_DIR / "logs" / "main.log"
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
logging.info("Successfully logged to root logs/main.log!")


# Database setup
engine = create_engine(settings.MYSQL_URL)
SessionLocal = sessionmaker(bind=engine)

# JWT Configuration
JWT_SECRET = os.getenv("JWT_SECRET", "super-secret-jwt-key")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

ADMIN_ACTIONS = {"APPROVED", "REJECTED", "FLAGGED"}

# NEW (EMAIL)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PLACEHOLDER_EMAILS = {"", "test@example.com"}   # seed value from ensure_user_exists

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET, algorithm=ALGORITHM)

def verify_jwt_token(token: str):
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

async def get_current_admin(request: Request):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing or invalid token")
    token = auth_header.split(" ")[1]
    return verify_jwt_token(token)

def normalize_user_id(user_id: str | None) -> str:
    """USER_123, user_123 and ' User_123 ' are all the same user."""
    return (user_id or "").strip().upper()

app = FastAPI(title=settings.APP_NAME)

# NEW (S3): bills are served from the local folder, or (STORAGE_BACKEND=s3) through a short-lived S3 link
storage.check()
if storage.USE_S3:
    @app.get("/uploads/{filename}", include_in_schema=False)
    def open_uploaded_bill(filename: str):
        if "/" in filename or "\\" in filename or ".." in filename:
            raise HTTPException(status_code=404, detail="Not found")
        return RedirectResponse(storage.presigned_url(filename))
else:
    if not os.path.exists("uploads"):
        os.makedirs("uploads")
    app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

# CREATE TABLES ON STARTUP
Base.metadata.create_all(bind=engine)

# ---------------------------------------------------------------- NEW (MONITORING): Prometheus
metrics.init("api", guardrails_ai=guards.GUARDRAILS_AVAILABLE)
metrics.register_db_collector()

@app.middleware("http")
async def prometheus_middleware(request: Request, call_next):
    if request.url.path == "/metrics":
        return await call_next(request)
    start = time.perf_counter()
    metrics.in_progress(1)
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        metrics.in_progress(-1)
        route = request.scope.get("route")
        path = getattr(route, "path", None) or "unmatched"      # route template, never the raw URL (no claim ids)
        metrics.http_request(request.method, path, status_code, time.perf_counter() - start)

@app.get("/metrics", include_in_schema=False)
def prometheus_metrics():
    return Response(content=metrics.render(), media_type=metrics.CONTENT_TYPE_LATEST)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------- NEW (EMAIL): user login
class LoginBody(BaseModel):
    user_id: str
    email: str

def _login_sync(uid: str, email: str) -> dict:
    """Saves / checks the Email ID typed at login against the users table."""
    ensure_user_exists(uid, email)   # new users are created with this email
    db = SessionLocal()
    try:
        row = db.execute(
            text("SELECT email FROM users WHERE employee_id = :eid"), {"eid": uid}
        ).fetchone()
        if not row:
            return {"status": "error", "message": "Could not log in. Please try again."}

        saved = (row[0] or "").strip().lower()
        if saved in PLACEHOLDER_EMAILS:
            # old seed rows still have test@example.com: save the real email now
            db.execute(
                text("UPDATE users SET email = :e WHERE employee_id = :eid"),
                {"e": email, "eid": uid},
            )
            db.commit()
        elif saved != email:
            return {"status": "mismatch",
                    "message": "This email does not match the one saved for this User ID."}
        return {"status": "ok", "user_id": uid, "email": email}
    except Exception:
        db.rollback()
        return {"status": "error", "message": "Could not log in. Please try again."}
    finally:
        db.close()

@app.post("/login")
async def user_login(body: LoginBody):
    uid = normalize_user_id(body.user_id)
    email = body.email.strip().lower()
    if not uid or not EMAIL_RE.match(email):
        return JSONResponse(status_code=400, content={
            "status": "invalid", "message": "Enter a valid User ID and Email ID."})

    result = await asyncio.to_thread(_login_sync, uid, email)
    if result["status"] == "ok":
        return result
    return JSONResponse(status_code=401 if result["status"] == "mismatch" else 500, content=result)

@app.post("/chat")
async def chat_endpoint(
    user_id: str = Form(None),
    message: str = Form(...),
    file: UploadFile = File(None)
):
    user_id = normalize_user_id(user_id) or f"USER_{uuid.uuid4().hex[:8].upper()}"

    # NEW (GUARDRAILS): at most 20 messages / uploads per user per minute
    if not guards.chat_limiter.allow(user_id):
        metrics.rate_limited()                             # NEW (MONITORING)
        return JSONResponse(status_code=429, content={
            "source": "guard", "response": guards.REPLY_TOO_FAST})

    # Make sure the user exists (new users get the opening balance)
    ensure_user_exists(user_id)

    # 1. Check Public Input Cache
    # NOTE: this cache is shared by everyone. Never store balance or claim answers in it.
    cached_res = cache.get_cache("input", "public", message)
    if cached_res:
        return JSONResponse(content={"source": "public_cache", "response": cached_res})

    # 2. Check Private Input Cache
    cached_res = cache.get_cache("input", "private", f"{user_id}:{message}")
    if cached_res:
        return JSONResponse(content={"source": "private_cache", "response": cached_res})

    # 3. Process through Agent Orchestrator
    file_data = None
    claim_id = None
    if file:
        # NEW (GUARDRAILS): size limit, allowed types, real file signature, safe file name
        content = await file.read(guards.UPLOAD_MAX_BYTES + 1)
        safe_name, upload_error = guards.check_upload(file.filename, content, user_id)
        if upload_error:
            return JSONResponse(status_code=400, content={"source": "guard", "response": upload_error})

        # This UUID is the MAIN claim id everywhere (chat, DB, admin panel)
        claim_id = str(uuid.uuid4())
        file_data = storage.save_upload(claim_id, safe_name, content)   # NEW (S3): local folder or S3

    response = await orchestrator.process_message(
        user_id, message, file_data=file_data, claim_id=claim_id
    )

    # If it was a claim submission, we return the claim_id so the UI can poll
    if isinstance(response, dict) and response.get("status") == "submitted":
        return JSONResponse(content={
            "source": "agent",
            "response": response,
            "claim_id": claim_id
        })

    return JSONResponse(content={"source": "agent", "response": response})

@app.get("/claims/{claim_id}/status")
async def get_claim_status(claim_id: str, user_id: str = Query(...)):
    """Only the owner of the claim gets an answer (and sees their own balance)."""
    eid = normalize_user_id(user_id)
    db = SessionLocal()
    try:
        row = db.execute(
            text(
                "SELECT c.bill_amount, c.confidentiality_score, c.extracted_data, c.status, u.current_balance "
                "FROM claims c JOIN users u ON c.user_id = u.id "
                "WHERE c.claim_uuid = :cid AND u.employee_id = :eid"
            ),
            {"cid": claim_id, "eid": eid},
        ).fetchone()
    finally:
        db.close()

    # Not found (or belongs to someone else): look the same as "still processing"
    if not row:
        return {"status": "processing", "message": "AI is still reading your bill..."}

    amount, score, extracted, claim_status, balance = row[0], row[1], row[2], row[3], row[4]
    balance = round(float(balance or 0), 2)

    if claim_status == "FAILED":
        return {
            "status": "failed",
            "error": "We could not read this bill right now. Please try again in a few minutes.",
        }

    if claim_status == "LOW_BALANCE":
        return {"status": "low_balance", "data": {"amount": amount, "balance": balance}}

    return {
        "status": "completed",
        "data": {
            "amount": amount,        # bill_amount
            "score": score,          # confidentiality_score
            "extracted": extracted,  # extracted_data (JSON)
            "balance": balance,
        }
    }

class ClaimUserBody(BaseModel):
    user_id: str

class ClaimEditBody(BaseModel):
    user_id: str
    message: str

def _claim_response(result: dict):
    """Unknown or foreign claims answer 404, everything else 200 with the service result."""
    if result.get("status") == "error" and result.get("message") == "Claim not found.":
        return JSONResponse(status_code=404, content=result)
    return JSONResponse(content=result)

@app.post("/claims/{claim_id}/edit")
async def edit_claim_endpoint(claim_id: str, body: ClaimEditBody):
    """User changes date / amount / provider through the chat while the claim is still a draft."""
    result = await asyncio.to_thread(
        claim_service.edit_claim, claim_id, normalize_user_id(body.user_id), body.message
    )
    return _claim_response(result)

@app.post("/claims/{claim_id}/submit")
async def submit_claim_endpoint(claim_id: str, body: ClaimUserBody):
    """The user pressed 'Submit claim'. Only now does the claim reach the admin / balance."""
    result = await asyncio.to_thread(
        claim_service.submit_claim, claim_id, normalize_user_id(body.user_id)
    )
    return _claim_response(result)

@app.post("/claims/{claim_id}/cancel")
async def cancel_claim_endpoint(claim_id: str, body: ClaimUserBody):
    """The user cancelled before submitting."""
    result = await asyncio.to_thread(
        claim_service.cancel_claim, claim_id, normalize_user_id(body.user_id)
    )
    return _claim_response(result)

# ---------------------------------------------------------------- NEW (HANDOFF): talk to a human
_handoff_last: dict[str, float] = {}   # user_id -> time of the last ticket (stops double clicks and spam)

class HandoffMsg(BaseModel):
    role: str
    text: str
    ts: int | None = None

class HandoffBody(BaseModel):
    user_id: str
    name: str
    mobile: str
    email: str
    reason: str
    history: list[HandoffMsg] = []
    claim_ids: list[str] = []

@app.post("/support/handoff")
async def handoff_endpoint(body: HandoffBody):
    """The user filled in the 'Talk to a human' form. Creates a HubSpot ticket with the chat summary."""
    uid = normalize_user_id(body.user_id)
    if not uid:
        return JSONResponse(status_code=400, content={"status": "invalid", "message": "Please log in again."})

    if time.time() - _handoff_last.get(uid, 0) < 60:
        return JSONResponse(status_code=429, content={
            "status": "error",
            "message": "A request was just sent. Please wait a minute before sending another.",
        })

    result = await asyncio.to_thread(
        handoff_service.create_handoff,
        uid, body.name, body.mobile, body.email, body.reason,
        [m.dict() for m in body.history], body.claim_ids,
    )
    if result["status"] == "ok":
        _handoff_last[uid] = time.time()
        return result
    return JSONResponse(status_code=400 if result["status"] == "invalid" else 502, content=result)

@app.post("/admin/login")
async def admin_login(data: dict = Body(...)):
    username = data.get("username")
    password = data.get("password")

    expected_user = os.getenv("ADMIN_USERNAME", "admin")
    expected_pass = os.getenv("ADMIN_PASSWORD", "admin")

    if username == expected_user and password == expected_pass:
        token = create_access_token({"sub": username, "role": "admin"})
        return {"access_token": token, "token_type": "bearer"}

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid username or password"
    )

@app.get("/admin/claims")
async def get_admin_claims(admin=Depends(get_current_admin)):
    db = SessionLocal()
    try:
        # Only claims the user actually submitted are listed. Drafts (not submitted yet),
        # cancelled, failed and low-balance claims never reach the admin.
        result = db.execute(text(
            "SELECT COALESCE(c.claim_uuid, CAST(c.id AS CHAR)), u.employee_id, c.bill_amount, "
            "c.extracted_data, c.confidentiality_score, "
            "c.confidence_value, c.review_reason, c.confidentiality_status, c.status, c.file_path, c.created_at "
            "FROM claims c JOIN users u ON c.user_id = u.id "
            "WHERE COALESCE(c.status, 'PENDING') NOT IN ('DRAFT', 'CANCELLED', 'FAILED', 'LOW_BALANCE') "
            "ORDER BY c.created_at DESC"
        )).fetchall()
        claims = []
        for r in result:
            claims.append({
                "id": r[0],          # claim UUID
                "user_id": r[1],     # employee_id (e.g. 'USER_123')
                "amount": r[2],
                "extracted": r[3],
                "score": r[4],
                "confidence_value": r[5] if len(r) > 5 else None,
                "review_reason": r[6] if len(r) > 6 else None,
                "confidentiality_status": r[7] if len(r) > 7 else None,
                "status": r[8] if len(r) > 8 else "PENDING",
                "file_path": r[9] if len(r) > 9 else None,
                "created_at": str(r[10]) if len(r) > 10 else None,
            })
        return claims
    finally:
        db.close()

@app.post("/admin/claims/{claim_id}/action")
async def update_claim_status(claim_id: str, status: str = Body(..., embed=True), admin=Depends(get_current_admin)):
    """Update claim status (claim_id is the claim UUID). Approving takes the money from the user's balance."""
    status = str(status).upper()
    if status not in ADMIN_ACTIONS:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Invalid action"})

    db = SessionLocal()
    try:
        # 1. Find the claim by UUID
        res = db.execute(
            text("SELECT id, user_id, bill_amount, status FROM claims WHERE claim_uuid = :cid"),
            {"cid": claim_id},
        ).fetchone()
        if not res:
            return JSONResponse(status_code=404, content={"status": "error", "message": "Claim not found"})

        internal_id, user_id, amount, current_status = res[0], res[1], res[2], res[3]

        # Claims that were never processed cannot be actioned
        if current_status in ("DRAFT", "CANCELLED", "FAILED", "LOW_BALANCE", "REJECTED"):
            return JSONResponse(status_code=400, content={"status": "error", "message": f"Claim is already {current_status}"})

        if status == "FLAGGED":
            # an auto-approved (APPROVED) claim can still be flagged by the admin
            if current_status == "FLAGGED":
                return JSONResponse(status_code=400, content={"status": "error", "message": "Claim is already FLAGGED"})
        elif current_status == "APPROVED":
            return JSONResponse(status_code=400, content={"status": "error", "message": "Claim is already APPROVED"})

        # 2. Approving: take the money first. The WHERE guard stops the balance going below zero.
        if status == "APPROVED":
            deducted = db.execute(
                text("UPDATE users SET current_balance = current_balance - :amt "
                     "WHERE id = :uid AND current_balance >= :amt"),
                {"amt": amount, "uid": user_id},
            )
            if deducted.rowcount != 1:
                db.rollback()
                return JSONResponse(status_code=400, content={
                    "status": "error",
                    "message": "User does not have enough balance to approve this claim",
                })

            db.execute(
                text("INSERT INTO transactions (user_id, amount, category, transaction_type, description) "
                     "VALUES (:uid, :amt, 'Claim Reimbursement', 'DEBIT', :desc)"),
                {"uid": user_id, "amt": amount, "desc": f"Claim {claim_id} approved"}
            )

        # 3. Update the claim status
        db.execute(text("UPDATE claims SET status = :status WHERE id = :id"), {"status": status, "id": internal_id})

        db.commit()
        return {"status": "success", "message": f"Claim {claim_id} updated to {status}"}
    except Exception as e:
        db.rollback()
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})
    finally:
        db.close()

@app.get("/health")
async def health_check():
    return {"status": "healthy"}

if __name__ == "__main__":
    import uvicorn
    if not os.path.exists("uploads"):
        os.makedirs("uploads")
    uvicorn.run(app, host="0.0.0.0", port=8000)