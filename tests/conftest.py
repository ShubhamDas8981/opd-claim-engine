"""Stubs for the parts of your project that need MySQL/Kafka/Groq, so the guardrails can be tested on their own."""
import os, sys, types
os.environ["OTEL_SDK_DISABLED"] = "true"
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.safety import config
config.AUDIT_LOG_PATH = "/tmp/gr_test/guardrails.jsonl"

# app.core.config
cfg = types.ModuleType("app.core.config")
cfg.settings = types.SimpleNamespace(GROQ_MODEL="test-model", GROQ_API_KEY="x")
sys.modules["app.core.config"] = cfg

# app.worker.claims_worker with an in-memory SQLite database
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SessionLocal = sessionmaker(bind=engine)
cw = types.ModuleType("app.worker.claims_worker")
cw.SessionLocal = SessionLocal
cw.OPENING_BALANCE = 20000.0
sys.modules["app.worker.claims_worker"] = cw

import pytest

@pytest.fixture()
def db():
    with engine.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS claims")); c.execute(text("DROP TABLE IF EXISTS users")); c.execute(text("DROP TABLE IF EXISTS transactions"))
        c.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, employee_id TEXT, current_balance REAL)"))
        c.execute(text("CREATE TABLE claims (id INTEGER PRIMARY KEY AUTOINCREMENT, claim_uuid TEXT, user_id INTEGER, bill_amount REAL, "
                       "status TEXT, confidence_value REAL, extracted_data TEXT, file_path TEXT, review_reason TEXT, "
                       "confidentiality_score TEXT, confidentiality_status TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"))
        c.execute(text("CREATE TABLE transactions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL, category TEXT, "
                       "transaction_type TEXT, description TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"))
        c.execute(text("INSERT INTO users VALUES (1,'USER_A',20000),(2,'USER_B',20000)"))
    s = SessionLocal()
    yield s
    s.close()
