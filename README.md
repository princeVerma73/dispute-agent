# DisputeAgent

Autonomous payout dispute resolution system for delivery riders. Investigates payout disputes, audits ledger records against platform policy, and settles owed amounts via automated payment or human Operations review.

---

## Architecture & Model Fallback Chain

The agent orchestrates conversation turns using a request-scoped LangGraph ReAct agent loop.

The model is instantiated lazily from the `LLM_ORDER` environment variable (a comma-separated list of `provider:model_name`, e.g. `groq:openai/gpt-oss-120b,groq:openai/gpt-oss-20b,gemini:gemini-2.5-flash`):
- Providers with missing API keys in the environment are automatically skipped at startup.
- The remaining configured providers are chained sequentially using LangChain `.with_fallbacks(...)`.
- If an active provider fails during a request, execution fails over to the next configured model in the chain.
- If the agent execution fails after retries, the system gracefully escalates the issue to the Operations queue and sends a polite fallback reply so no rider is left unanswered.

---

## Division of Responsibilities: Who Decides What

The platform strictly separates natural language understanding from critical financial logic and money movement:

| Responsibility | Handled By | Implementation / Guarantees |
| :--- | :--- | :--- |
| **Language Understanding & Dialogue** | LLM | Interprets conversational intent in Hinglish, Hindi, or English; asks for dates when missing; synthesizes polite responses (max 4 sentences). |
| **Tool Routing & Query Formulation** | LLM | Selects appropriate investigative tools (`audit_day`, `inspect_trip`, `check_payout_history`, `settle_owed`, `escalate_to_ops`). |
| **Fare & Incentive Calculation** | Code (`calculator.py`) | Deterministic base fare, per-km rates, surge multiplication with round-half-up, incentive thresholds (>=12 trips), and duplicate penalty detection. |
| **Money Movement & Payout Amounts** | Code (`settlement.py`) | Recomputes exact discrepancies server-side from verified audit findings. Tools take NO caller-supplied amount parameter. |
| **Financial Safeguards** | Code (`settlement.py`) | Auto-pay limit strictly <= ₹200; maximum 1 auto-pay per rider per IST calendar day; higher amounts or repeat payouts are automatically queued for Ops review. |
| **Account Ownership & Isolation** | Code (`tools.py`) | Every tool is bound to a verified `RequestContext`. Riders can only access and settle their own trips. |
| **Dispute Window Eligibility** | Code (`calculator.py`) | Enforces the 7-calendar-day window dynamically based on `received_at`. Expired trips cannot be paid. |
| **Idempotency & Deduplication** | Code (`main.py`, `settlement.py`) | Incoming messages claimed with unique database locks; deterministic SHA1 idempotency keys for PaySwift transactions. |

---

## Data Quality Rules & Sanitization

1. **Distance Metric Normalization (Metres vs Kilometres)**:
   - Raw distance values greater than 100 in the trip database represent metres (e.g. `5000` is 5,000 metres).
   - Normalization converts these values to kilometres (`5.0 km`) dynamically at calculation time, preserving raw records in the database.

2. **Rider ID Normalization**:
   - Ingestion sanitizes varied user inputs (`R7`, `r19`, ` R019 `) into canonical padded strings (`R007`, `R019`).
   - The rider ID is validated against the database before any request context or LLM execution begins. Invalid or unknown IDs trigger an Ops ticket and a polite fixed reply without invoking the LLM.

3. **IST Calendar Date Alignment**:
   - Timestamps are normalized to Indian Standard Time (`IST`, `UTC+05:30`).
   - Trips, daily incentives, and payouts are evaluated by IST calendar dates, preventing timezone boundary shifts between UTC midnight and IST morning (05:30 AM).

4. **Half-Up Rounding**:
   - All financial amounts use symmetric arithmetic half-up rounding (`floor(x + 0.5)`), avoiding standard Python banker's rounding biases.

---

## Core Operational Assumptions

- **7-Day Dispute Window**:
  - The dispute window spans 7 calendar days inclusive of the message day (`[today - 6 days, today]`). For example, a dispute received on 2026-09-22 is eligible back to 2026-09-16; 2026-09-15 is expired.
- **Single Legitimate Penalty**:
  - A rider cancellation carries a standard ₹10 penalty. Refunds only apply when multiple penalty ledger lines exist for the same trip ID.
- **Platform Policy Precedence**:
  - The platform does not claw back overpayments.
  - Vague complaints require clarifying dates or order numbers prior to running audits.

---

## Evaluation Status

- **Unit & System Integration Tests**:
  - All deterministic business logic, settlement workflows, concurrent race guards, and agent loop behaviors are validated with comprehensive pytest suites using an in-memory hermetic SQLite database and simulated payment gateway.
- **Held-Out Conversations**:
  - LLM-level evaluation on held-out multi-turn conversation datasets is currently pending.
