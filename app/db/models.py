from sqlalchemy import Column, String, Float, Integer, Text
from app.db.session import Base, engine

class Rider(Base):
    __tablename__ = "riders"
    rider_id = Column(String, primary_key=True)
    name = Column(String)
    phone = Column(String)
    city = Column(String)

class Trip(Base):
    __tablename__ = "trips"
    trip_id = Column(String, primary_key=True)
    rider_id = Column(String)
    date = Column(String)
    distance_km_raw = Column(Float)
    status = Column(String)
    surge_multiplier = Column(Float, default=1.0)

class PayoutLine(Base):
    __tablename__ = "payout_lines"
    line_id = Column(String, primary_key=True)
    rider_id = Column(String)
    trip_id = Column(String, nullable=True)
    amount = Column(Float)
    category = Column(String)
    date = Column(String)

class ConversationMessage(Base):
    __tablename__ = "conversation_messages"
    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(String, unique=True, index=True)
    rider_id = Column(String, index=True)
    sender = Column(String)  # 'rider' or 'agent'
    text = Column(Text)
    created_at = Column(String)
    reply_text = Column(Text, nullable=True)  # cached reply for idempotency / dedup

class OpsDispute(Base):
    __tablename__ = "ops_disputes"
    id = Column(Integer, primary_key=True, autoincrement=True)
    dispute_id = Column(String, unique=True, index=True)  # e.g., 'op-001'
    rider_id = Column(String, index=True)
    trip_id = Column(String, nullable=True)
    type = Column(String, default="approval")  # 'approval' or 'escalation'
    amount = Column(Float, nullable=True)  # in rupees, null for escalations
    reason = Column(Text)
    status = Column(String, default="PENDING")  # 'PENDING', 'APPROVED', 'REJECTED'
    created_at = Column(String)

class TraceStep(Base):
    __tablename__ = "trace_steps"
    id = Column(Integer, primary_key=True, autoincrement=True)
    rider_id = Column(String, index=True)
    at = Column(String)  # ISO timestamp
    type = Column(String)  # 'message_in', 'tool_call', 'decision', 'reply', 'error'
    name = Column(String)
    input = Column(Text, nullable=True)  # JSON-encoded input or null
    output = Column(Text, nullable=True)  # JSON-encoded output or string

class Settlement(Base):
    __tablename__ = "settlements"
    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String, unique=True, index=True, nullable=False)
    rider_id = Column(String, index=True, nullable=False)
    kind = Column(String, nullable=False)
    amount = Column(Integer, nullable=False)
    channel = Column(String, nullable=True)  # 'autopay' | 'ops'
    status = Column(String, nullable=False)  # 'reserved' | 'paid' | 'queued' | 'rejected' | 'failed'
    ref = Column(String, nullable=True)
    paid_on = Column(String, nullable=True)  # IST day string (YYYY-MM-DD)
    created_at = Column(String, nullable=False)

def init_db():
    Base.metadata.create_all(bind=engine)