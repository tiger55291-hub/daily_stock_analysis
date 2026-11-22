"""Private durable signal ledger for FAHAD20 AI Lens (Supabase PostgREST).

No credentials are embedded. Uses server-only SUPABASE_SECRET_KEY.
The private schema must be exposed to PostgREST explicitly to use this adapter;
otherwise use a direct PostgreSQL connection adapter instead.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

STATUSES = frozenset({
    "received", "processing", "ai_rejected", "sent", "failed",
    "expired_stale", "ambiguous_sent",
})


class LedgerError(RuntimeError):
    pass


class SignalLedger:
    def __init__(self, url=None, secret=None, schema="fahad_ai_lens", timeout=5):
        self.url = (url or os.getenv("SUPABASE_URL", "")).rstrip("/")
        self.secret = secret or os.getenv("SUPABASE_SECRET_KEY", "")
        self.schema = schema
        self.timeout = timeout
        if not self.url or not self.secret:
            raise LedgerError("Supabase server credentials are missing")

    def _request(self, method, query="", payload=None, prefer=None):
        url = self.url + "/rest/v1/signals_log" + query
        headers = {
            "apikey": self.secret,
            "Authorization": "Bearer " + self.secret,
            "Accept-Profile": self.schema,
            "Content-Profile": self.schema,
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            # Avoid logging response headers or credentials.
            raise LedgerError("Supabase request failed: HTTP " + str(exc.code)) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise LedgerError("Supabase request unavailable") from exc

    def get(self, signal_id):
        query = "?signal_id=eq." + urllib.parse.quote(signal_id, safe="") + "&select=*"
        rows = self._request("GET", query)
        return rows[0] if rows else None

    def insert(self, row):
        if row.get("status", "received") not in STATUSES:
            raise ValueError("invalid signal status")
        # Database primary key enforces idempotency. HTTP 409 is a duplicate.
        try:
            self._request("POST", payload=row, prefer="return=minimal")
            return True
        except LedgerError as exc:
            if "HTTP 409" in str(exc):
                return False
            raise

    def update(self, signal_id, **changes):
        if "status" in changes and changes["status"] not in STATUSES:
            raise ValueError("invalid signal status")
        changes["updated_at"] = datetime.now(timezone.utc).isoformat()
        query = "?signal_id=eq." + urllib.parse.quote(signal_id, safe="")
        self._request("PATCH", query, changes, prefer="return=minimal")
