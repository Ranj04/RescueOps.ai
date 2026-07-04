"""On-call SMS channel, outbound half (ARCHITECTURE §6A, Track B).

SMS is just another consumer of the event stream: when `approval_requested` or
`postmortem_ready`/`incident_resolved` land in the store's publish path, this
module texts the one on-call engineer (ONCALL_PHONE_NUMBER). Sends are
fire-and-forget — a Twilio outage must never stall the pipeline — and every
successful send is itself an `oncall_notified` event handed back to the caller
to publish.

Stdlib-only on purpose: this rides in both the agent bundle and the lean
cloud-function bundle without touching requirements.txt.

Env (all unset → channel silently off; the web panel is the guaranteed path):
  SMS_ENABLED=true, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
  TWILIO_FROM_NUMBER, ONCALL_PHONE_NUMBER, SITE_URL (optional link)
"""
from __future__ import annotations

import base64
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

_SMS_MAX = 300  # one-segment target per §6A


def enabled() -> bool:
    return (
        os.getenv("SMS_ENABLED", "").lower() == "true"
        and bool(os.getenv("TWILIO_ACCOUNT_SID"))
        and bool(os.getenv("TWILIO_AUTH_TOKEN"))
        and bool(os.getenv("TWILIO_FROM_NUMBER"))
        and bool(os.getenv("ONCALL_PHONE_NUMBER"))
    )


def _truncate(text: str, limit: int = _SMS_MAX) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def compose_approval_sms(incident_id: str, risky_actions: list[str]) -> str:
    actions = "; ".join(risky_actions) or "risky remediation"
    link = os.getenv("SITE_URL", "")
    tail = f' Reply "YES {incident_id}" or "NO {incident_id}".' + (f" {link}" if link else "")
    return _truncate(f"RescueOps {incident_id}: approval needed — {_truncate(actions, 140)}.{tail}")


def compose_resolution_sms(incident_id: str, summary: str) -> str:
    return _truncate(f"RescueOps {incident_id} RESOLVED — {summary}")


def send_sms(body: str) -> bool:
    """POST to Twilio's Messages API. Never raises; False on any failure."""
    if not enabled():
        return False
    try:
        sid = os.environ["TWILIO_ACCOUNT_SID"]
        data = urllib.parse.urlencode({
            "To": os.environ["ONCALL_PHONE_NUMBER"],
            "From": os.environ["TWILIO_FROM_NUMBER"],
            "Body": body,
        }).encode()
        req = urllib.request.Request(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            data=data,
            method="POST",
        )
        auth = base64.b64encode(f"{sid}:{os.environ['TWILIO_AUTH_TOKEN']}".encode()).decode()
        req.add_header("Authorization", f"Basic {auth}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def _notified_event(incident_id: str, trigger: str, body: str) -> dict:
    return {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "incident_id": incident_id,
        "actor": "system",
        "type": "oncall_notified",
        "payload": {
            "summary": f"Texted the on-call engineer about {trigger.replace('_', ' ')}.",
            "trigger": trigger,
            "channel": "sms",
            "body": body,
        },
        "trace_id": None,
    }


def dispatch(events: list[dict], incident_id: str, risky_actions: list[str] | None = None) -> list[dict]:
    """Send SMS for any trigger events in this batch. Returns the
    `oncall_notified` events (one per successful send) for the caller to
    publish. Never raises."""
    if not enabled():
        return []
    out: list[dict] = []
    try:
        types = {e.get("type") for e in events}
        if "approval_requested" in types:
            body = compose_approval_sms(incident_id, risky_actions or [])
            if send_sms(body):
                out.append(_notified_event(incident_id, "approval_requested", body))
        # Prefer the postmortem as the resolution text; fall back to the bare
        # resolution if the postmortem was cut.
        resolution = next(
            (e for e in events if e.get("type") == "postmortem_ready"), None
        ) or next((e for e in events if e.get("type") == "incident_resolved"), None)
        if resolution is not None:
            body = compose_resolution_sms(
                incident_id, resolution.get("payload", {}).get("summary", "resolved.")
            )
            if send_sms(body):
                out.append(_notified_event(incident_id, resolution["type"], body))
    except Exception:
        pass
    return out
