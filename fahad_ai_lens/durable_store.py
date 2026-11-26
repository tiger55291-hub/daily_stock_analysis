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
    def __init__(self, url=None, secret=None, schema="public", timeout=5):
        self.url = (url or os.getenv("SUPABASE_URL", "")).rstrip("/")
        self.secret = secret or os.getenv("SUPABASE_SECRET_KEY", "")
        self.schema = schema
        self.timeout = timeout
        if not self.url or not self.secret:
            raise LedgerError("Supabase server credentials are missing")

    def _request(self, method, query="", payload=None, prefer=None, resource="signals_log"):
        url = self.url + "/rest/v1/" + resource + query
        headers = {
            "apikey": self.secret,
            "Accept-Profile": self.schema,
            "Content-Profile": self.schema,
            "Content-Type": "application/json",
        }
        if not self.secret.startswith("sb_secret_"):
            headers["Authorization"] = "Bearer " + self.secret
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


    def record_candles(self, symbol, bars):
        self._request("POST", payload={"p_symbol": symbol, "p_bars": bars},
                      resource="rpc/record_lens_candles")

    def reconcile_trades(self, symbol):
        self._request("POST", payload={"p_symbol": symbol},
                      resource="rpc/reconcile_lens_trades")

    def trade_stats(self, period):
        # The receiver accepts only these two constant periods.
        query = "?period=eq." + period + "&order=period_start.desc,symbol,side&limit=300"
        return self._request("GET", query, resource="lens_trade_stats") or []

    def insert_gemini(self, row):
        rows = self._request("POST", "?on_conflict=signal_id", row,
                             prefer="resolution=ignore-duplicates,return=representation",
                             resource="lens_gemini_signals")
        return bool(rows)

    def update_gemini(self, signal_id, **changes):
        query = "?signal_id=eq." + urllib.parse.quote(signal_id, safe="")
        self._request("PATCH", query, changes, prefer="return=minimal", resource="lens_gemini_signals")

    def record_gemini_candles(self, bars):
        self._request("POST", "?on_conflict=closed_at", bars,
                      prefer="resolution=ignore-duplicates,return=minimal",
                      resource="lens_gemini_candles_1m")
        self.reconcile_gemini()

    def reconcile_gemini(self):
        self._request("POST", payload={}, resource="rpc/reconcile_lens_gemini")

    def gemini_stats(self, period):
        query = "?period=eq." + period + "&order=period_start.desc,symbol,pattern,side&limit=300"
        return self._request("GET", query, resource="lens_gemini_stats") or []

    def opening_range_bars(self, opened, closed):
        query = urllib.parse.urlencode([
            ("closed_at", "gt." + opened.isoformat()), ("closed_at", "lte." + closed.isoformat()),
            ("order", "closed_at.asc"), ("limit", 390),
        ])
        return self._request("GET", "?" + query, resource="lens_gemini_candles_1m") or []

    def insert_orb(self, row):
        return bool(self._request("POST", "?on_conflict=session_date,side", row,
                                  prefer="resolution=ignore-duplicates,return=representation",
                                  resource="lens_openai_orb"))

    def update_orb(self, setup, **changes):
        query = urllib.parse.urlencode({k: "eq." + setup[k] for k in ("session_date", "side")})
        self._request("PATCH", "?" + query, changes, prefer="return=minimal", resource="lens_openai_orb")

    def reconcile_orb(self):
        self._request("POST", payload={}, resource="rpc/reconcile_lens_openai_orb")

    def orb_stats(self, period):
        query = "?period=eq." + period + "&order=period_start.desc,side&limit=300"
        return self._request("GET", query, resource="lens_openai_orb_stats") or []
