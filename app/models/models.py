from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import func

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(String(50), unique=True, index=True)
    full_name = Column(String(100))
    email = Column(String(100))
    department = Column(String(100))
    current_balance = Column(Float, default=20000.0)  # opening balance for every new user
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    amount = Column(Float)
    category = Column(String(50)) # e.g., Pharmacy, Consultation, Lab
    transaction_type = Column(String(20)) # CREDIT | DEBIT
    description = Column(Text)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())

class Claim(Base):
    __tablename__ = "claims"
    id = Column(Integer, primary_key=True, index=True)
    claim_uuid = Column(String(36), unique=True, index=True)  # MAIN claim id shown to users and admins
    user_id = Column(Integer, ForeignKey("users.id"))
    bill_amount = Column(Float)
    extracted_data = Column(Text) # JSON string of OCR results
    confidentiality_score = Column(String(20)) # AUTO_APPROVE | HUMAN_REVIEW | HOLD
    confidence_value = Column(Float) # Actual numerical score (0.0 to 1.0)
    review_reason = Column(Text) # Reason why human review is required
    confidentiality_status = Column(String(20)) # Duplicate of score but kept for clarity/history
    status = Column(String(20), default="PENDING") # PENDING, APPROVED, REJECTED
    file_path = Column(String(255))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

class Policy(Base):
    __tablename__ = "policies"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(200))
    content = Column(Text)
    category = Column(String(100))
    last_updated = Column(DateTime(timezone=True), server_default=func.now())