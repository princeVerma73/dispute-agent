from dotenv import load_dotenv
load_dotenv(override=True)

import json
import logging
from typing import Optional, List
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.agent.core import process_dispute_message
from app.agent.services import call_payswift_payout, IST, reset_session_payouts
from app.db.session import SessionLocal
from app.db.models import (
    ConversationMessage,
    OpsDispute,
    TraceStep
)
from app.db.seed import seed_database

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Ensures database tables are created and seeded on startup."""
    try:
        seed_database()
    except Exception as e:
        logger.warning(f"Database auto-seeding notice: {e}")
    yield

import os
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="DisputeAgent API", lifespan=lifespan)

# Mount Ops Dashboard UI
ui_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "ops-ui")
if os.path.exists(ui_dir):
    app.mount("/ui", StaticFiles(directory=ui_dir, html=True), name="ui")

class MessageRequest(BaseModel):
    message_id: Optional[str] = None
    rider_id: str
    text: str
    received_at: str

@app.get("/health")
async def health_check():
    """Liveness probe: returns 200 OK when ready."""
    return {"status": "ok"}

@app.post("/messages")
async def receive_message(msg: MessageRequest):
    """
    Primary messaging endpoint.
    - Performs idempotency dedup: returns cached reply if message_id was previously processed.
    - Dispatches to the LangGraph ReAct agent.
    - Persists conversation turns in database.
    """
    db = SessionLocal()
    try:
        # 1. Message Deduplication Check (Messaging Vendor Retry Guard)
        if msg.message_id:
            existing = db.query(ConversationMessage).filter(
                ConversationMessage.message_id == msg.message_id,
                ConversationMessage.sender == "rider"
            ).first()
            if existing and existing.reply_text:
                logger.info(f"Duplicate message {msg.message_id} detected. Returning cached reply.")
                return {"reply": existing.reply_text}

        # 2. Process message through AI Agent
        ai_reply = process_dispute_message(
            rider_id=msg.rider_id,
            message=msg.text,
            message_id=msg.message_id,
            received_at=msg.received_at
        )

        # 3. Store conversation turn in database
        rider_msg = ConversationMessage(
            message_id=msg.message_id,
            rider_id=msg.rider_id,
            sender="rider",
            text=msg.text,
            created_at=msg.received_at,
            reply_text=ai_reply
        )
        db.add(rider_msg)

        agent_msg = ConversationMessage(
            message_id=None,
            rider_id=msg.rider_id,
            sender="agent",
            text=ai_reply,
            created_at=datetime.now(IST).isoformat(),
            reply_text=None
        )
        db.add(agent_msg)
        db.commit()

        return {"reply": ai_reply}
    except Exception as e:
        logger.error(f"Error handling message: {e}", exc_info=True)
        return {"reply": f"Internal System Error: {str(e)}"}
    finally:
        db.close()

@app.get("/trace/{rider_id}")
async def get_trace(rider_id: str):
    """
    Returns the ordered list of trace steps for a specific rider.
    Shape matches SUBMISSION.md contract:
    [{"at": "<ISO>", "type": "...", "name": "...", "input": ..., "output": ...}]
    """
    db = SessionLocal()
    try:
        steps = db.query(TraceStep).filter(
            TraceStep.rider_id == rider_id
        ).order_by(TraceStep.id.asc()).all()

        formatted_steps = []
        for s in steps:
            # Parse JSON input/output if serialized
            parsed_input = None
            if s.input:
                try:
                    parsed_input = json.loads(s.input)
                except Exception:
                    parsed_input = s.input

            parsed_output = None
            if s.output:
                try:
                    parsed_output = json.loads(s.output)
                except Exception:
                    parsed_output = s.output

            formatted_steps.append({
                "at": s.at,
                "type": s.type,
                "name": s.name,
                "input": parsed_input,
                "output": parsed_output
            })
        return formatted_steps
    finally:
        db.close()

@app.get("/ops/pending")
async def get_ops_pending():
    """
    Returns the list of items waiting for ops review.
    Shape matches SUBMISSION.md contract:
    [{"id": "...", "rider_id": "...", "type": "approval"|"escalation", "amount": ..., "reason": "...", "created_at": "..."}]
    """
    db = SessionLocal()
    try:
        pending = db.query(OpsDispute).filter(
            OpsDispute.status == "PENDING"
        ).order_by(OpsDispute.id.asc()).all()

        res = []
        for p in pending:
            res.append({
                "id": p.dispute_id,
                "rider_id": p.rider_id,
                "type": p.type,
                "amount": int(round(p.amount)) if p.amount is not None else None,
                "reason": p.reason,
                "created_at": p.created_at
            })
        return res
    finally:
        db.close()

@app.post("/ops/pending/{dispute_id}/approve")
async def approve_ops_dispute(dispute_id: str):
    """Approves a pending item and initiates PaySwift payout if an amount is attached."""
    db = SessionLocal()
    try:
        dispute = db.query(OpsDispute).filter(
            OpsDispute.dispute_id == dispute_id,
            OpsDispute.status == "PENDING"
        ).first()
        if not dispute:
            raise HTTPException(status_code=404, detail="Pending dispute not found")

        payout_id = None
        if dispute.amount and dispute.amount > 0:
            resp = call_payswift_payout(
                rider_id=dispute.rider_id,
                amount=int(round(dispute.amount)),
                reference=f"Ops Approved Dispute {dispute.dispute_id}"
            )
            payout_id = resp.get("payout_id")

        dispute.status = "APPROVED"
        db.commit()
        return {"status": "approved", "id": dispute_id, "payout_id": payout_id}
    finally:
        db.close()

@app.post("/ops/pending/{dispute_id}/reject")
async def reject_ops_dispute(dispute_id: str):
    """Rejects a pending item."""
    db = SessionLocal()
    try:
        dispute = db.query(OpsDispute).filter(
            OpsDispute.dispute_id == dispute_id,
            OpsDispute.status == "PENDING"
        ).first()
        if not dispute:
            raise HTTPException(status_code=404, detail="Pending dispute not found")

        dispute.status = "REJECTED"
        db.commit()
        return {"status": "rejected", "id": dispute_id}
    finally:
        db.close()

@app.post("/reset")
async def reset_runtime_state():
    """
    Resets runtime tables (conversation messages, trace steps, ops disputes)
    and session auto-pay state for clean automated evaluation runs.
    """
    db = SessionLocal()
    try:
        db.query(ConversationMessage).delete()
        db.query(TraceStep).delete()
        db.query(OpsDispute).delete()
        db.commit()
        reset_session_payouts()
        return {"status": "reset", "message": "Runtime state cleared successfully"}
    finally:
        db.close()