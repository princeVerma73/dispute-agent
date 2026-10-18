import time
import sys
import json
import os
import urllib.request
import urllib.error

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

def fetch_json(url: str, method: str = "GET", payload: dict = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        return {"error": e.code, "body": body}
    except Exception as e:
        return {"error": str(e)}

def run_evals(base_url: str):
    base_url = base_url.rstrip("/")
    # Determine payswift URL
    payswift_url = os.environ.get("PAYSWIFT_BASE_URL", "http://localhost:8081").rstrip("/")
    if "payswift" in payswift_url:
        import socket
        try:
            socket.gethostbyname("payswift")
        except socket.gaierror:
            payswift_url = payswift_url.replace("payswift", "localhost")

    # Load conversations from data/conversations.json
    script_dir = os.path.dirname(os.path.abspath(__file__))
    conv_file = os.path.join(os.path.dirname(script_dir), "data", "conversations.json")
    if not os.path.exists(conv_file):
        conv_file = "data/conversations.json"

    with open(conv_file, "r", encoding="utf-8") as f:
        conversations = json.load(f)

    print(f"\n=======================================================")
    print(f"  DisputeAgent Evaluation Suite")
    print(f"  Target: {base_url} | PaySwift: {payswift_url}")
    print(f"  Total Scenarios: {len(conversations)}")
    print(f"=======================================================\n")

    passed_count = 0

    for i, conv in enumerate(conversations):
        rider_id = conv["rider_id"]
        scenario = conv.get("scenario", f"Scenario {i+1}")
        
        # 1. Reset system state for clean isolation
        fetch_json(f"{base_url}/reset", method="POST")

        # Snapshot baseline PaySwift payouts before this conversation
        initial_payouts_data = fetch_json(f"{payswift_url}/v1/payouts?rider_id={rider_id}")
        initial_payouts = initial_payouts_data.get("data", []) if isinstance(initial_payouts_data, dict) else []
        initial_sum = sum(p.get("amount", 0) for p in initial_payouts)

        # 2. Run all turns
        replies = []
        for turn in conv["turns"]:
            if turn.get("from") == "rider":
                res = fetch_json(
                    f"{base_url}/messages",
                    method="POST",
                    payload={
                        "message_id": turn.get("message_id"),
                        "rider_id": rider_id,
                        "text": turn.get("text", ""),
                        "received_at": turn.get("received_at", "")
                    }
                )
                replies.append(res.get("reply", ""))

        last_reply = replies[-1] if replies else ""
        all_reply_text = " ".join(replies)

        # 3. Check outcomes against expectation
        # Fetch current PaySwift payouts
        final_payouts_data = fetch_json(f"{payswift_url}/v1/payouts?rider_id={rider_id}")
        final_payouts = final_payouts_data.get("data", []) if isinstance(final_payouts_data, dict) else []
        final_sum = sum(p.get("amount", 0) for p in final_payouts)
        actual_payout = final_sum - initial_sum

        # Fetch current pending ops items for this rider
        ops_data = fetch_json(f"{base_url}/ops/pending")
        rider_ops = [op for op in ops_data if isinstance(op, dict) and op.get("rider_id") == rider_id]
        
        approval_ops = [op for op in rider_ops if op.get("type") == "approval"]
        actual_approval = sum(op.get("amount", 0) for op in approval_ops) if approval_ops else None

        has_escalation = any(op.get("type") == "escalation" for op in rider_ops)

        expected = conv.get("expected", {})
        reasons = []
        is_pass = False

        if "one_of" in expected:
            # Handle one_of acceptable branches
            matched_branch = False
            for branch in expected["one_of"]:
                exp_p = branch.get("payout", 0)
                exp_a = branch.get("approval")
                if actual_payout == exp_p and (actual_approval == exp_a or (exp_a is None and actual_approval is None)):
                    matched_branch = True
                    break
            if not matched_branch:
                reasons.append(f"Payout {actual_payout} or Approval {actual_approval} did not match any one_of branches")
            is_pass = matched_branch
        else:
            # Check payout
            exp_payout = expected.get("payout", 0)
            if actual_payout != exp_payout:
                reasons.append(f"Expected payout ₹{exp_payout}, got ₹{actual_payout}")

            # Check approval
            exp_approval = expected.get("approval")
            if exp_approval is not None and actual_approval != exp_approval:
                reasons.append(f"Expected approval ₹{exp_approval}, got ₹{actual_approval}")
            elif exp_approval is None and actual_approval is not None and actual_approval > 0:
                reasons.append(f"Expected null approval, got ₹{actual_approval}")

            is_pass = len(reasons) == 0

        # Check escalation requirement
        exp_esc = expected.get("escalation", "no")
        if exp_esc == "required" and not has_escalation:
            is_pass = False
            reasons.append("Expected mandatory escalation, none created")
        elif exp_esc == "no" and has_escalation:
            is_pass = False
            reasons.append("Escalation was created when none expected")

        # Check reply mentions across conversation
        for phrase in expected.get("reply_mentions", []):
            if phrase.lower() not in all_reply_text.lower():
                is_pass = False
                reasons.append(f"Reply missing mention '{phrase}'")

        if is_pass:
            passed_count += 1
            print(f"  [PASS] [{i+1:02d}/{len(conversations)}] Rider {rider_id} | {scenario}", flush=True)
        else:
            print(f"  [FAIL] [{i+1:02d}/{len(conversations)}] Rider {rider_id} | {scenario}", flush=True)
            print(f"         Issues: {', '.join(reasons)}", flush=True)
            print(f"         Last Reply Snippet: \"{last_reply[:90]}...\"", flush=True)

        # RATE LIMIT PREVENTER - WAIT 4 SECONDS BEFORE NEXT TEST
        time.sleep(4)

    pct = int(round((passed_count / len(conversations)) * 100))
    print(f"\n=======================================================", flush=True)
    print(f"  FINAL EVAL RESULT: {passed_count}/{len(conversations)} Passed ({pct}%)", flush=True)
    print(f"=======================================================\n", flush=True)
    return passed_count == len(conversations)

if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
    success = run_evals(url)
    sys.exit(0 if success else 1)