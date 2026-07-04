"""Track A incident runner on the Makers agent runtime — Phase A5.

Exposes the pipeline (pipeline.py) in the platform's `async def handler(context)`
idiom (RECON-B0 Q1), landing beside the Track B `agents/ping` probe as its comment
foretold. One incident == one `Makers-Conversation-Id` (session-sticky routing).

Pause model (RECON-B0 Q1): a human approval wait is unbounded, so a run that stops at
`awaiting_approval` ENDS this invocation after persisting its RunResult to
`context.store`. The follow-up approval arrives as a SEPARATE invocation on the same
conversation id, which restores the snapshot and calls `resume_after_approval`.

Two-track seam (ARCHITECTURE §8/§9.4), RECONCILED at the B3 joint gate:
- Events publish to conversation `evt-<sha1(incident_id)[:16]>` — the exact convention
  Track B's `_storage._cid` reads, so `/api/events` sees them with position-stamped seq.
- The paused-run snapshot stays on the REQUEST conversation id (the frontend reuses one
  id per incident run), so snapshots never pollute the event stream.
- The pipeline's own approval_granted/denied event is NOT published: `/api/approval` is
  the single writer of approval events (it arbitrates web-vs-SMS races), and publishing
  both would double them in the stream.
- `clear_events` runs per invocation: warm containers keep `events.py`'s in-process log
  alive across requests, and without the clear a resume would re-publish the pre-pause
  events.
Everything above the `_Persistence` adapter — driving the pipeline, the pause/resume
handoff, stamping `trace_id = run_id` (RECON-B0 Q9) — is Track A and is covered by
tests/test_incident_agent_handler.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

# The deployed bundle routes agents/<name>/index.py; the Track A modules live at the
# project root, so make sure it is importable (mirrors cloud-functions/api/index.py).
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from events import clear_events, list_events  # noqa: E402
from pipeline import (  # noqa: E402
    STATUS_AWAITING,
    resume_after_approval,
    run_until_approval,
)
from schemas import ApprovalDecision, RunResult  # noqa: E402

_SNAPSHOT_KIND = "rescueops.snapshot"

# /api/approval is the single writer of approval events (see module docstring).
_UNPUBLISHED_TYPES = {"approval_granted", "approval_denied"}


def _event_cid(incident_id: str) -> str:
    """Track B's `_storage._cid` convention — MUST stay in lockstep with it.
    (Conversation ids are 6-36 chars and silently reject ':'; the hash sidesteps
    both, and /api/events reads exactly this conversation.)"""
    return "evt-" + hashlib.sha1(incident_id.encode()).hexdigest()[:16]


class _Persistence:
    """Thin async adapter over `context.store` (RECON-B0 Q1/Q8: append_message /
    get_messages are the confirmed agent-side primitives). The paused RunResult is
    stored as one snapshot message per incident on the request conversation; the
    latest one wins. Events publish to the shared `evt-...` conversation that
    Track B's `/api/events` reads."""

    def __init__(self, store, conversation_id: str) -> None:
        self._store = store
        self._cid = conversation_id

    async def load_snapshot(self) -> RunResult | None:
        if self._store is None:
            return None
        messages = await self._store.get_messages(self._cid, order="desc")
        for message in messages or []:
            content = _message_content(message)
            try:
                envelope = json.loads(content)
            except (TypeError, ValueError):
                continue
            if envelope.get("kind") == _SNAPSHOT_KIND:
                # A null result is the tombstone a completed resume leaves so a
                # stray second resume fails cleanly instead of re-running.
                if envelope["result"] is None:
                    return None
                return RunResult.model_validate(envelope["result"])
        return None

    async def save_snapshot(self, result: RunResult | None) -> None:
        if self._store is None:
            return
        await self._store.append_message(
            self._cid,
            "assistant",
            json.dumps({
                "kind": _SNAPSHOT_KIND,
                "result": result.model_dump() if result is not None else None,
            }),
        )

    async def publish_events(self, incident_id: str, events: list[dict]) -> None:
        if self._store is None:
            return
        cid = _event_cid(incident_id)
        for event in events:
            if event.get("type") in _UNPUBLISHED_TYPES:
                continue
            await self._store.append_message(cid, "system", json.dumps(event))


def _message_content(message) -> str:
    """Tolerate the two message shapes a store may hand back (dict or object)."""
    if isinstance(message, dict):
        return message.get("content", "")
    return getattr(message, "content", "")


def _stamp_trace(events: list[dict], run_id: str | None) -> list[dict]:
    """RECON-B0 Q9: the agent runtime exposes `context.run_id`; stamp it as the
    correlator on events the in-process log left null."""
    if run_id:
        for event in events:
            if event.get("trace_id") is None:
                event["trace_id"] = run_id
    return events


async def handler(context):
    run_id = getattr(context, "run_id", None)
    conversation_id = getattr(context, "conversation_id", None) or "incident-unknown"
    store = getattr(context, "store", None)

    try:
        raw_body = getattr(getattr(context, "request", None), "body", None) or "{}"
        body = json.loads(raw_body)
    except (TypeError, ValueError):
        yield json.dumps({"status": "error", "error": "request body is not valid JSON"})
        return

    incident_id = body.get("incident_id") or conversation_id
    persistence = _Persistence(store, conversation_id)
    # Warm containers share events.py's in-process log across invocations; without
    # this a resume would re-publish every pre-pause event.
    clear_events(incident_id)

    approval = body.get("approval")
    if approval is not None:
        paused = await persistence.load_snapshot()
        if paused is None:
            yield json.dumps(
                {"status": "error", "error": "no paused incident to approve for this conversation"}
            )
            return
        result = resume_after_approval(paused, ApprovalDecision(**approval))
    else:
        result = run_until_approval(incident_id, chaos_config=body.get("chaos_config"))

    if result.status == STATUS_AWAITING:
        await persistence.save_snapshot(result)
    elif approval is not None:
        # Resume finished — tombstone the snapshot so a stray second resume
        # (double-click, second dashboard tab) cannot re-run the incident.
        await persistence.save_snapshot(None)

    events = _stamp_trace(list_events(incident_id), run_id)
    await persistence.publish_events(incident_id, events)

    # §6A on-call SMS: fire-and-forget consumer of the just-published events
    # (Track B's notify.py); a Twilio failure can never stall the pipeline.
    try:
        import notify

        risky = [a.action for a in result.remediation.risky] if result.remediation else []
        notified = notify.dispatch(events, incident_id, risky)
        if notified:
            await persistence.publish_events(incident_id, _stamp_trace(notified, run_id))
    except Exception:
        pass

    yield json.dumps(
        {
            "status": result.status,
            "run_id": result.run_id,
            "trace_id": run_id,
            "result": result.model_dump(),
            "events": events,
        }
    )
