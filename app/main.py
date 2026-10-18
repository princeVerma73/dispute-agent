"""main.py — FastAPI service for DisputeAgent.

Provides HTTP messaging interface, ops dashboard endpoints, and trace inspections.
All endpoints use plain def (run in threadpool) so slow LLM calls never block the event loop.
"""

from dotenv import load_dotenv
load_dotenv(override=True)

import os
import json
import time
import logging
from typing import Optional, List
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from app.agent.core import process_dispute_message
from app.agent.ops_actions import approve as ops_approve, reject as ops_reject
from app.db.session import SessionLocal
from app.db.models import (
    ConversationMessage,
    OpsDispute,
    TraceStep,
    Settlement,
)
from app.db.seed import seed_database
from app.agent.settlement import set_idempotency_salt

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Ensures database tables are created and seeded on startup."""
    try:
        seed_database()
    except Exception as e:
        logger.warning(f"Database auto-seeding notice: {e}")
    yield


app = FastAPI(title="DisputeAgent API", lifespan=lifespan)

# Mount Ops Dashboard UI if directory exists
ui_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "ops-ui")
if os.path.exists(ui_dir):
    app.mount("/ui", StaticFiles(directory=ui_dir, html=True), name="ui")


class MessageRequest(BaseModel):
    message_id: Optional[str] = None
    rider_id: str
    text: str
    received_at: str


@app.get("/health")
def health_check():
    """Liveness probe: returns 200 OK when ready."""
    return {"status": "ok"}


@app.post("/messages")
def receive_message(msg: MessageRequest):
    """Primary messaging endpoint.

    Claims the message_id first (unique insert with reply_text NULL) BEFORE processing.
    On IntegrityError: polls up to 40s for the first request's reply_text.
    If processing raises: deletes the claim so vendor retry can succeed.
    Never returns str(exception) to the client.
    """
    if msg.message_id:
        db = SessionLocal()
        try:
            claim = ConversationMessage(
                message_id=msg.message_id,
                rider_id=msg.rider_id,
                sender="rider",
                text=msg.text,
                created_at=msg.received_at,
                reply_text=None,
            )
            db.add(claim)
            db.commit()
        except IntegrityError:
            db.rollback()
            db.close()
            # Another request/thread claimed this message_id; poll up to 40s for reply
            start_poll = time.time()
            while time.time() - start_poll < 40.0:
                time.sleep(0.05)
                poll_db = SessionLocal()
                try:
                    existing = poll_db.query(ConversationMessage).filter(
                        ConversationMessage.message_id == msg.message_id
                    ).first()
                    if existing and existing.reply_text:
                        return {"reply": existing.reply_text}
                finally:
                    poll_db.close()
            return {"reply": "Aapka message process ho raha hai. Kripya thoda wait karein."}
        finally:
            db.close()

    try:
        ai_reply = process_dispute_message(
            rider_id=msg.rider_id,
            message=msg.text,
            message_id=msg.message_id or "",
            received_at=msg.received_at or "",
        )

        db = SessionLocal()
        try:
            if msg.message_id:
                row = db.query(ConversationMessage).filter(
                    ConversationMessage.message_id == msg.message_id
                ).first()
                if row:
                    row.reply_text = ai_reply
            else:
                rider_msg = ConversationMessage(
                    message_id=None,
                    rider_id=msg.rider_id,
                    sender="rider",
                    text=msg.text,
                    created_at=msg.received_at,
                    reply_text=ai_reply,
                )
                db.add(rider_msg)

            agent_msg = ConversationMessage(
                message_id=None,
                rider_id=msg.rider_id,
                sender="agent",
                text=ai_reply,
                created_at=datetime.now(IST).isoformat(),
                reply_text=None,
            )
            db.add(agent_msg)
            db.commit()
        finally:
            db.close()

        return {"reply": ai_reply}
    except Exception as e:
        logger.error(f"Error handling message {msg.message_id}: {e}", exc_info=True)
        if msg.message_id:
            # Delete claim so vendor retry can run cleanly
            db = SessionLocal()
            try:
                db.query(ConversationMessage).filter(
                    ConversationMessage.message_id == msg.message_id
                ).delete()
                db.commit()
            except Exception:
                db.rollback()
            finally:
                db.close()
        # Never return raw str(exception) to the client
        return {"reply": "Aapka message process karne mein takleef hui. Kripya thodi der baad dobara koshish karein."}


@app.get("/trace/{rider_id}")
def get_trace(rider_id: str):
    """Returns the ordered list of trace steps for a specific rider."""
    db = SessionLocal()
    try:
        steps = db.query(TraceStep).filter(
            TraceStep.rider_id == rider_id
        ).order_by(TraceStep.id.asc()).all()

        formatted_steps = []
        for s in steps:
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
                "output": parsed_output,
            })
        return formatted_steps
    finally:
        db.close()


@app.get("/ops/pending")
def get_ops_pending():
    """Returns the list of items waiting for ops review."""
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
                "created_at": p.created_at,
            })
        return res
    finally:
        db.close()


@app.post("/ops/pending/{dispute_id}/approve")
def approve_ops_dispute(dispute_id: str):
    """Approves a pending item via ops_actions.approve (idempotent, PaySwift key 'ops-approve-...')."""
    res = ops_approve(dispute_id)
    if res.get("status") == "error" and "not found" in res.get("message", "").lower():
        raise HTTPException(status_code=404, detail=res["message"])
    return res


@app.post("/ops/pending/{dispute_id}/reject")
def reject_ops_dispute(dispute_id: str):
    """Rejects a pending item via ops_actions.reject and marks settlements rejected."""
    res = ops_reject(dispute_id)
    if res.get("status") == "error" and "not found" in res.get("message", "").lower():
        raise HTTPException(status_code=404, detail=res["message"])
    return res


@app.post("/reset")
def reset_runtime_state():
    """Resets runtime tables and settlements only when ALLOW_RESET=1 (otherwise 403)."""
    if os.environ.get("ALLOW_RESET") != "1":
        raise HTTPException(status_code=403, detail="Reset is disabled in this environment.")

    new_salt = set_idempotency_salt()

    db = SessionLocal()
    try:
        db.query(ConversationMessage).delete()
        db.query(TraceStep).delete()
        db.query(OpsDispute).delete()
        db.query(Settlement).delete()
        db.commit()
        return {
            "status": "reset",
            "salt": new_salt,
            "message": "Runtime state and settlements cleared successfully."
        }
    finally:
        db.close()