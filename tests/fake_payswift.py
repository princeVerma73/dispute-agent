"""
fake_payswift.py — Lightweight hermetic PaySwift mock server for testing.

Runs an in-process HTTP server on localhost with:
- Idempotency-Key support and deduplication
- Injectable transient/outage 503 failures
- In-memory payout record inspection
- Zero external dependencies
"""

import os
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import List, Dict, Any


class PaySwiftRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Silence default logging during test runs
        pass

    def do_POST(self):
        if self.path == "/v1/payouts":
            # Check injected failures
            if self.server.failure_count > 0:
                self.server.failure_count -= 1
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Service Unavailable (Injected 503)"}).encode("utf-8"))
                return

            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length)
            body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
            idempotency_key = self.headers.get("Idempotency-Key", "")

            # Record request
            self.server.requests.append({
                "body": body,
                "headers": dict(self.headers),
                "idempotency_key": idempotency_key,
            })

            # Check Idempotency-Key cache
            if idempotency_key and idempotency_key in self.server.idempotency_records:
                cached = self.server.idempotency_records[idempotency_key]
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(cached).encode("utf-8"))
                return

            payout_id = f"PS-{len(self.server.payouts) + 1:04d}"
            record = {
                "payout_id": payout_id,
                "rider_id": body.get("rider_id"),
                "amount": body.get("amount"),
                "reference": body.get("reference"),
                "idempotency_key": idempotency_key,
            }
            self.server.payouts.append(record)
            resp = {"status": "success", "payout_id": payout_id}
            if idempotency_key:
                self.server.idempotency_records[idempotency_key] = resp

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        if self.path.startswith("/v1/payouts"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"data": self.server.payouts}).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()


class FakePaySwift:
    def __init__(self):
        self.server = HTTPServer(("127.0.0.1", 0), PaySwiftRequestHandler)
        self.server.failure_count = 0
        self.server.payouts: List[Dict[str, Any]] = []
        self.server.requests: List[Dict[str, Any]] = []
        self.server.idempotency_records: Dict[str, Any] = {}
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self._thread.start()
        os.environ["PAYSWIFT_BASE_URL"] = f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def inject_failures(self, count: int = 1):
        self.server.failure_count = count

    def reset(self):
        self.server.failure_count = 0
        self.server.payouts.clear()
        self.server.requests.clear()
        self.server.idempotency_records.clear()

    @property
    def payouts(self) -> List[Dict[str, Any]]:
        return self.server.payouts

    @property
    def requests(self) -> List[Dict[str, Any]]:
        return self.server.requests
