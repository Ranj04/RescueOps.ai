"""RescueOps /api cloud function (Track B) — every route under one function.

Makers maps cloud-functions/api/index.py to /api with a catch-all
(^/api(?:/.*)?$ per observed route generation), so this one handler serves
the whole ARCHITECTURE §4 surface:

  GET  /api/health
  GET  /api/packs                      -> pack names for the domain switcher
  GET  /api/incidents?pack=it-ops      -> incident summaries for the picker
  POST /api/incidents                  -> upsert {pack, incidents:[...]} (seed)
  GET  /api/events?incident=X&since=N  -> §4 events, seq > N
  POST /api/approval                   -> {incident_id, approved, approver?, note?}
  GET  /api/chaos                      -> current chaos flags
  POST /api/chaos                      -> set flags (+ chaos event if incident_id)
  GET  /api/eval?pack=it-ops           -> cached eval summary or null
  POST /api/eval                       -> cache {pack, summary} (scored by the
                                          /eval agent; this endpoint only persists)
  POST /api/sms/inbound                -> Twilio webhook (§6A): signature +
                                          allowlist, YES/NO grammar, SAME
                                          approval writer as the web panel
"""

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# EdgeOne loads index.py as a top-level module without package context; only
# the bundle root is importable by default, so both dirs go on sys.path.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_THIS_DIR)
for _d in (_THIS_DIR, _PARENT_DIR):
    if _d not in sys.path:
        sys.path.insert(0, _d)

from _storage import Storage  # noqa: E402
from _sms import parse_reply, twiml, validate_twilio_signature  # noqa: E402

CHAOS_DEFAULTS = {"disable_sources": [], "break_primary_model": False, "kill_real_feed": False}


def find_pending_approval(storage, incident_id):
    """The latest approval_requested not yet answered, or None."""
    pending = None
    for event in storage.read_events(incident_id):
        if event["type"] == "approval_requested":
            pending = event
        elif event["type"] in ("approval_granted", "approval_denied"):
            pending = None
    return pending


def decide_approval(storage, incident_id, approved, channel, approver, note=""):
    """THE approval writer — both channels (web panel, SMS) come through here,
    so first response wins and the §4 stream gets exactly one approval event.
    Returns the appended event, or None when nothing is pending (race lost)."""
    pending = find_pending_approval(storage, incident_id)
    if pending is None:
        return None
    verb = "approved" if approved else "denied"
    return storage.append_event(
        incident_id, "human",
        "approval_granted" if approved else "approval_denied",
        {"summary": f"Human {verb} the risky action from the {channel} {'panel' if channel == 'web' else 'channel'}.",
         "channel": channel,
         "approver": approver,
         "note": note,
         "action": pending["payload"].get("action")},
    )


def pending_approvals(storage):
    """Incident ids with an unanswered approval, across every pack."""
    ids = []
    for pack in storage.get_json("packs", ["it-ops"]):
        for inc in storage.get_json(f"incidents:{pack}", []):
            if find_pending_approval(storage, inc["id"]) is not None:
                ids.append(inc["id"])
    return ids


class handler(BaseHTTPRequestHandler):
    # -- plumbing ----------------------------------------------------------
    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=UTF-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")) or {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def _route(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api"):
            path = path[len("/api"):] or "/"
        return path.rstrip("/") or "/", {k: v[0] for k, v in parse_qs(parsed.query).items()}

    def do_GET(self):
        path, q = self._route()
        storage = Storage(self.context.agent.store)
        try:
            if path == "/health":
                return self._json(200, {"status": "ok", "ts": time.time()})
            if path == "/packs":
                return self._json(200, storage.get_json("packs", ["it-ops"]))
            if path == "/incidents":
                pack = q.get("pack", "it-ops")
                return self._json(200, storage.get_json(f"incidents:{pack}", []))
            if path == "/events":
                incident = q.get("incident")
                if not incident:
                    return self._json(400, {"error": "incident is required"})
                since = int(q.get("since", 0))
                return self._json(200, storage.read_events(incident, since))
            if path == "/chaos":
                return self._json(200, storage.get_json("chaos", CHAOS_DEFAULTS))
            if path == "/eval":
                pack = q.get("pack", "it-ops")
                return self._json(200, storage.get_json(f"eval:{pack}"))
            return self._json(404, {"error": f"no such route: GET {path}"})
        except Exception as e:
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        path, q = self._route()
        storage = Storage(self.context.agent.store)
        if path == "/sms/inbound":
            # Twilio posts form-encoded, not JSON — handled before _body().
            return self._sms_inbound(storage)
        body = self._body()
        try:
            if path == "/incidents":
                pack = body.get("pack", "it-ops")
                incidents = body.get("incidents", [])
                storage.put_json(f"incidents:{pack}", incidents)
                packs = storage.get_json("packs", [])
                if pack not in packs:
                    storage.put_json("packs", packs + [pack])
                return self._json(200, {"pack": pack, "count": len(incidents)})

            if path == "/approval":
                return self._approval(storage, body)

            if path == "/chaos":
                flags = storage.get_json("chaos", dict(CHAOS_DEFAULTS))
                for key in CHAOS_DEFAULTS:
                    if key in body:
                        flags[key] = body[key]
                storage.put_json("chaos", flags)
                incident = body.get("incident_id")
                if incident:
                    active = bool(flags["disable_sources"]) or flags["break_primary_model"] or flags["kill_real_feed"]
                    event_type = "chaos_injected" if active else "chaos_cleared"
                    named = ", ".join(flags["disable_sources"]) or "primary model" if active else ""
                    summary = (f"Chaos injected: {named} degraded by the operator."
                               if active else "All chaos flags cleared — systems restored.")
                    storage.append_event(incident, "chaos", event_type,
                                         {"summary": summary, "flags": flags})
                return self._json(200, flags)

            if path == "/eval":
                # Scoring runs on the /eval agent (needs the full pipeline);
                # this endpoint only persists the summary it produced.
                pack = body.get("pack")
                summary = body.get("summary")
                if not pack or not isinstance(summary, dict) or "aggregate" not in summary:
                    return self._json(400, {"error": "pack and summary{aggregate,...} are required"})
                storage.put_json(f"eval:{pack}", summary)
                return self._json(200, summary)

            return self._json(404, {"error": f"no such route: POST {path}"})
        except Exception as e:
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})

    # -- approval (one state machine, channel=web here) ---------------------
    def _approval(self, storage, body):
        incident = body.get("incident_id")
        if not incident:
            return self._json(400, {"error": "incident_id is required"})
        event = decide_approval(
            storage, incident, bool(body.get("approved")),
            channel="web", approver=body.get("approver", "human-ui"),
            note=body.get("note", ""),
        )
        if event is None:
            return self._json(409, {"error": "no approval pending", "resolved": True})
        return self._json(200, event)

    # -- §6A inbound SMS (Twilio webhook) ------------------------------------
    def _xml(self, status, body):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/xml; charset=UTF-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sms_inbound(self, storage):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length > 0 else ""
        params = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

        auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
        webhook_url = os.getenv("SMS_WEBHOOK_URL", "")
        if not auth_token:
            return self._xml(503, twiml("SMS channel is not configured."))
        if not validate_twilio_signature(
            webhook_url, params, self.headers.get("X-Twilio-Signature", ""), auth_token
        ):
            print(f"sms/inbound REJECTED: bad signature from {params.get('From')!r}")
            return self._xml(403, twiml("Rejected."))
        sender = params.get("From", "")
        if sender != os.getenv("ONCALL_PHONE_NUMBER", ""):
            # Security floor: only the registered on-call number is an approver.
            print(f"sms/inbound REJECTED: unregistered sender {sender!r}")
            return self._xml(403, twiml("This number is not the registered on-call engineer."))

        approved, incident_id, reply = parse_reply(params.get("Body", ""), pending_approvals(storage))
        if incident_id is None:
            return self._xml(200, twiml(reply))

        storage.append_event(
            incident_id, "human", "oncall_reply",
            {"summary": f"On-call engineer replied {'YES' if approved else 'NO'} by SMS.",
             "channel": "sms", "body": params.get("Body", "")},
        )
        # Same writer as the web panel — first response wins.
        event = decide_approval(
            storage, incident_id, approved, channel="sms", approver=sender,
        )
        if event is None:
            return self._xml(200, twiml(f"{incident_id} was already resolved by another channel."))
        verb = "approved" if approved else "denied"
        return self._xml(200, twiml(f"Got it — {incident_id} {verb}. The run will resume."))

