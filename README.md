# DisputeAgent

Autonomous AI support desk for delivery riders. Investigates payout disputes, verifies trip records and policies, and moves money via PaySwift or queues for human Ops review.

---

## How to Run

### 1. Prerequisites
- Docker & Docker Compose
- Groq API Key

### 2. Setup & Start
```bash
# 1. Clone repository
git clone <repo-url>
cd <repo-folder>

# 2. Configure environment
cp .env.example .env
# Edit .env and enter your GROQ_API_KEY

# 3. Launch full system (Service + Postgres + PaySwift)
docker compose up --build
```
- **Service API**: `http://localhost:8000` (`GET /health` returns `{"status":"ok"}`)
- **Ops Dashboard**: `http://localhost:8000/ui`
- **PaySwift Sandbox**: `http://localhost:8081`

### 3. Run Evaluations
```bash
python evals/run.py http://localhost:8000
```

---

## Agent Design & Division of Responsibility

Built with a **LangGraph ReAct Agent** powered by Groq (`openai/gpt-oss-20b` with automated fallback to `openai/gpt-oss-120b`).

### Tools
1. `get_rider_details`: Verifies rider identity and city from PostgreSQL.
2. `get_trip_details`: Dynamically checks trip distance, full fare (base + extra km * surge), actual paid amount, and discrepancy.
3. `get_trips_by_date`: Audits completed trips for daily incentives (>= 12 trips), uncredited trips, and duplicate cancellation penalties.
4. `check_payout_history`: Inspects historical payout lines to prevent duplicate claims.
5. `initiate_payout`: Dispatches payouts via PaySwift or queues for Ops review.
6. `escalate_to_ops`: Creates pending escalation tickets for ops executives.

### What the Model Decides
- **Intent Recognition & Tone**: Understands natural, multi-turn Hinglish WhatsApp messages (e.g., *"bhai kal ka payout kam aaya"*, *"10 wala bhi de do"*).
- **Autonomous Tool Routing**: Determines which investigative tools to invoke based on rider context.
- **Synthesized Explanations**: Drafts respectful, concise breakdowns with exact Order IDs and rupee amounts.

### What Deterministic Code Enforces
- **Finance Limits**: Auto-pay capped at `<= ₹200` once per rider per day. Any amount `> ₹200` or repeat payout on the same day is strictly routed to `OpsDispute` for manual approval.
- **7-Day Dispute Policy**: Hard policy gate (< 2026-09-16 strictly ineligible).
- **Vendor Idempotency**: Deduplicates incoming WhatsApp messages using `message_id`; returns cached replies on retries.
- **PaySwift Idempotency**: Issues unique idempotency keys per payout transaction to eliminate double-charges.

---

## Assumptions Made
- All dispute dates belong to September 2026.
- Relative date terms (e.g., *"kal"* / *"yesterday"*) resolve relative to the incoming message timestamp (e.g., 22 Sep -> 21 Sep).
- Daily incentive (₹150) strictly requires >= 12 completed trips; rider cancellations incur standard ₹10 penalty.

---

## Evaluation Results
- **Pass Rate**: **24/24 Passed (100%)**
- **Evaluation Command**: `python evals/run.py http://localhost:8000`

---

## What Was Skipped
- **Voice/Audio Notes**: Only WhatsApp text messages are ingested.
- **OTP Verification**: Sender authentication relies on WhatsApp phone number metadata (`rider_id`).
- **Live SMS Gateway**: Relies on PaySwift sandbox and simulated WhatsApp webhooks per assignment spec.
