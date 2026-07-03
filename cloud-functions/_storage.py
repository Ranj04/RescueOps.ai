"""storage.py (ARCHITECTURE §8, Track B) — Makers store adapter.

Lives at cloud-functions/_storage.py because Makers bundles each function group
from this directory and `_`-prefixed files are private (not routed). All server
state goes through the platform's Blob-backed store (`self.context.agent.store`)
per RECON-B0 Q4: KV has no cloud-function binding; the store's generic
get/put surface plus append_message/get_messages IS the native path.

Layout in the store:
  conversation "evt:{incident_id}"  -> one message per event (append-only log).
       seq is stamped AT READ TIME from message position (order="asc" is
       stable), so concurrent producers can never mint duplicate seqs.
  key "incidents:{pack}"            -> list of incident summaries for the picker
  key "packs"                       -> list of pack names (domain switcher)
  key "chaos"                       -> chaos flag dict
  key "eval:{pack}"                 -> cached eval summary
  key "stub:{incident_id}"          -> STUB replay state (deleted with the stub)
"""

import json
import time
from datetime import datetime, timezone

EVENT_LIMIT = 1000  # per-incident read ceiling; demo incidents emit ~20


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Storage:
    """Thin adapter over the Makers conversation/blob store."""

    def __init__(self, store):
        self._store = store

    # -- generic keys ------------------------------------------------------
    def get_json(self, key, default=None):
        raw = self._store.get(key)
        if raw is None:
            return default
        return json.loads(raw) if isinstance(raw, str) else raw

    def put_json(self, key, value) -> None:
        self._store.put(key, json.dumps(value))

    def delete(self, key) -> None:
        self._store.delete_key(key)

    # -- event log ---------------------------------------------------------
    def append_event(self, incident_id, actor, event_type, payload, trace_id=None):
        """Append one §4-envelope event. seq is authoritative on read."""
        event = {
            "ts": _now_iso(),
            "incident_id": incident_id,
            "actor": actor,
            "type": event_type,
            "payload": payload,
            "trace_id": trace_id,
        }
        self._store.append_message(f"evt:{incident_id}", "user", json.dumps(event))
        return event

    def read_events(self, incident_id, since=0):
        """Events with seq > since, seq stamped from stable message position."""
        try:
            messages = self._store.get_messages(
                f"evt:{incident_id}", limit=EVENT_LIMIT, order="asc"
            ) or []
        except Exception:
            return []
        events = []
        for i, msg in enumerate(messages):
            seq = i + 1
            if seq <= since:
                continue
            content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
            if not isinstance(content, str):
                continue
            try:
                event = json.loads(content)
            except ValueError:
                continue
            event["seq"] = seq
            events.append(event)
        return events
