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
  POST /api/eval                       -> real scoring lands in Phase B2
  POST /api/stub/start                 -> STUB: begin canned replay (B3: delete)
  POST /api/stub/tick                  -> STUB: append events now due (B3: delete)
"""

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

_PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT_DIR not in sys.path:
    sys.path.insert(0, _PARENT_DIR)

from _storage import Storage  # noqa: E402
from _stub import PRE_APPROVAL, POST_APPROVAL, POST_DENIAL  # noqa: E402

CHAOS_DEFAULTS = {"disable_sources": [], "break_primary_model": False, "kill_real_feed": False}


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
                # Real scoring is Phase B2 (evaluation.py); no fake numbers before then.
                return self._json(501, {"error": "eval runner lands in Phase B2"})

            if path == "/stub/start":
                return self._stub_start(storage, body)
            if path == "/stub/tick":
                return self._stub_tick(storage, body)

            return self._json(404, {"error": f"no such route: POST {path}"})
        except Exception as e:
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})

    # -- approval (one state machine, channel=web here) ---------------------
    def _approval(self, storage, body):
        incident = body.get("incident_id")
        if not incident:
            return self._json(400, {"error": "incident_id is required"})
        events = storage.read_events(incident)
        pending = None
        for event in events:
            if event["type"] == "approval_requested":
                pending = event
            elif event["type"] in ("approval_granted", "approval_denied"):
                pending = None
        if pending is None:
            return self._json(409, {"error": "no approval pending", "resolved": True})
        approved = bool(body.get("approved"))
        actor_note = body.get("note", "")
        event = storage.append_event(
            incident, "human",
            "approval_granted" if approved else "approval_denied",
            {"summary": ("Human approved the risky action from the web panel."
                         if approved else "Human denied the risky action from the web panel."),
             "channel": "web",
             "approver": body.get("approver", "human-ui"),
             "note": actor_note,
             "action": pending["payload"].get("action")},
        )
        return self._json(200, event)

    # -- STUB (sanctioned scaffold — DELETE AT INTEGRATION, Phase B3) -------
    def _stub_start(self, storage, body):
        incident = body.get("incident_id", "INC-001-checkout-db-pool")
        storage.put_json(f"stub:{incident}", {"t0": time.time(), "appended": 0})
        return self._json(200, {"incident_id": incident, "status": "replaying"})

    def _stub_tick(self, storage, body):
        incident = body.get("incident_id", "INC-001-checkout-db-pool")
        state = storage.get_json(f"stub:{incident}")
        if not state:
            return self._json(200, {"status": "idle"})
        now = time.time()
        appended = state["appended"]
        new = 0

        # phase 1: pre-approval events due by wall clock
        while appended < len(PRE_APPROVAL):
            offset, actor, event_type, payload = PRE_APPROVAL[appended]
            if now - state["t0"] < offset:
                break
            storage.append_event(incident, actor, event_type, dict(payload))
            appended += 1
            new += 1

        # phase 2: barrier — wait for the REAL approval endpoint's event
        if appended >= len(PRE_APPROVAL):
            events = storage.read_events(incident)
            decision = next((e for e in events
                             if e["type"] in ("approval_granted", "approval_denied")), None)
            if decision is not None:
                tail = POST_APPROVAL if decision["type"] == "approval_granted" else POST_DENIAL
                if "t1" not in state:
                    state["t1"] = now
                done_tail = appended - len(PRE_APPROVAL)
                while done_tail < len(tail):
                    offset, actor, event_type, payload = tail[done_tail]
                    if now - state["t1"] < offset:
                        break
                    storage.append_event(incident, actor, event_type, dict(payload))
                    done_tail += 1
                    appended += 1
                    new += 1
                if done_tail >= len(tail):
                    storage.delete(f"stub:{incident}")
                    return self._json(200, {"status": "complete", "appended_now": new})

        state["appended"] = appended
        storage.put_json(f"stub:{incident}", state)
        return self._json(200, {"status": "replaying", "appended_now": new})
