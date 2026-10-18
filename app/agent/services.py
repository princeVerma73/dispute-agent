import os
import json
import uuid
import logging
import urllib.request
import urllib.error
from datetime import datetime
import pytz
from app.db.session import SessionLocal
from app.db.models import TraceStep

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

def get_payswift_url() -> str:
    """Returns the base URL for the PaySwift service."""
    # Inside docker: http://payswift:8081. Local host: http://localhost:8081
    url = os.environ.get("PAYSWIFT_BASE_URL", "http://localhost:8081")
    # If host running locally and cannot resolve 'payswift', fallback to localhost
    if "payswift" in url:
        import socket
        try:
            socket.gethostbyname("payswift")
        except socket.gaierror:
            url = url.replace("payswift", "localhost")
    return url.rstrip("/")

def utc_to_ist_date(utc_str: str) -> str:
    """Converts an ISO timestamp or date string to YYYY-MM-DD in IST."""
    if not utc_str:
        return ""
    try:
        # Normalize trailing Z to +00:00
        clean_str = utc_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_str)
        if dt.tzinfo is None:
            dt = pytz.utc.localize(dt)
        ist_dt = dt.astimezone(IST)
        return ist_dt.strftime("%Y-%m-%d")
    except Exception:
        # Fallback to first 10 characters if format is YYYY-MM-DD
        return str(utc_str)[:10]

def record_trace(rider_id: str, step_type: str, name: str, input_data=None, output_data=None):
    """
    Records an execution step in the trace_steps table.
    step_type must be one of: 'message_in', 'tool_call', 'decision', 'reply', 'error'.
    """
    db = SessionLocal()
    try:
        now_iso = datetime.now(IST).isoformat()
        
        # Serialize input/output to JSON if needed
        input_str = json.dumps(input_data) if input_data is not None and not isinstance(input_data, str) else (input_data if isinstance(input_data, str) else None)
        output_str = json.dumps(output_data) if output_data is not None and not isinstance(output_data, str) else (output_data if isinstance(output_data, str) else None)
        
        step = TraceStep(
            rider_id=rider_id,
            at=now_iso,
            type=step_type,
            name=name,
            input=input_str,
            output=output_str
        )
        db.add(step)
        db.commit()
    except Exception as e:
        logger.error(f"Failed to record trace step: {e}", exc_info=True)
    finally:
        db.close()

session_auto_paid_riders = set()
session_auto_paid_amounts = set()

def record_session_payout(rider_id: str, amount: int):
    """Tracks that this rider received an auto-payout of amount in the current session/day."""
    session_auto_paid_riders.add(rider_id)
    session_auto_paid_amounts.add((rider_id, int(round(amount))))

def reset_session_payouts():
    """Clears session auto-payout tracking (called during /reset)."""
    session_auto_paid_riders.clear()
    session_auto_paid_amounts.clear()

def call_payswift_payout(rider_id: str, amount: int, reference: str, max_retries: int = 5) -> dict:
    """
    Executes a real payout transaction via the PaySwift sandbox POST /v1/payouts endpoint.
    Includes automatic retry for transient 5xx/429 errors and idempotency key support.
    """
    import time
    base_url = get_payswift_url()
    url = f"{base_url}/v1/payouts"
    payload = {
        "rider_id": rider_id,
        "amount": int(round(amount)),
        "reference": reference
    }
    data = json.dumps(payload).encode("utf-8")
    payout_nonce = uuid.uuid4().hex[:10]
    idempotency_key = f"ps-{rider_id}-{amount}-{payout_nonce}"
    headers = {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotency_key
    }
    
    last_err = None
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                logger.info(f"PaySwift payout successful for {rider_id}: {resp_data}")
                record_session_payout(rider_id, amount)
                return resp_data
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (500, 502, 503, 504, 429) and attempt < max_retries - 1:
                logger.warning(f"PaySwift {e.code} error, retrying in {(attempt + 1) * 0.5}s...")
                time.sleep((attempt + 1) * 0.5)
                continue
            error_body = e.read().decode("utf-8")
            logger.error(f"PaySwift HTTP error {e.code}: {error_body}")
            raise RuntimeError(f"PaySwift failed with code {e.code}: {error_body}")
        except Exception as e:
            last_err = e
            if attempt < max_retries - 1:
                time.sleep((attempt + 1) * 0.5)
                continue
            logger.error(f"PaySwift connection error: {e}")
            raise RuntimeError(f"Could not connect to PaySwift: {str(e)}")

def get_payswift_payouts(rider_id: str = None) -> list:
    """Fetches list of payouts from PaySwift sandbox."""
    base_url = get_payswift_url()
    url = f"{base_url}/v1/payouts"
    if rider_id:
        url += f"?rider_id={rider_id}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp_data = json.loads(resp.read().decode("utf-8"))
            return resp_data.get("data", [])
    except Exception as e:
        logger.warning(f"Could not fetch payouts from PaySwift: {e}")
        return []

def has_payout_today(rider_id: str) -> bool:
    """
    Finance Rule Guard:
    'Auto-pay is fine for small amounts: up to ₹200 per dispute, once per rider per day.'
    Enforces once per rider per day by tracking payouts issued in the active session/day.
    """
    return rider_id in session_auto_paid_riders
