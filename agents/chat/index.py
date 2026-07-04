"""Incident-chat on the Makers agent runtime — Phase A6b (stretch).

POST /chat with {"incident_id": ..., "question": ...} on a conversation whose id is the
incident id. Session memory IS Makers `context.store` (RECON-B0 Q8): prior turns and the
incident's published events both live there, so a follow-up question keeps context across
invocations. Read-only — no new autonomy (ARCHITECTURE A6b).

The event log is read from the same `context.store` messages the incident runner
(agents/incident) published (system-role JSON events); chat turns are user/assistant
messages. That shared convention is the Track B `_storage.py` reconciliation point.
"""
from __future__ import annotations

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from incident_chat import answer_about_incident  # noqa: E402
from llm_client import begin_model_run  # noqa: E402


def _message_content(message) -> str:
    if isinstance(message, dict):
        return message.get("content", ""), message.get("role", "")
    return getattr(message, "content", ""), getattr(message, "role", "")


async def _read_store(store, conversation_id: str):
    """Split the conversation's stored messages into (events, chat_history)."""
    events, history = [], []
    if store is None:
        return events, history
    for message in await store.get_messages(conversation_id, order="asc") or []:
        content, role = _message_content(message)
        if role == "system":
            try:
                events.append(json.loads(content))
            except (TypeError, ValueError):
                continue
        elif role in ("user", "assistant"):
            history.append({"role": role, "content": content})
    return events, history


async def handler(context):
    conversation_id = getattr(context, "conversation_id", None) or "incident-unknown"
    store = getattr(context, "store", None)

    try:
        body = json.loads(getattr(getattr(context, "request", None), "body", None) or "{}")
    except (TypeError, ValueError):
        yield json.dumps({"status": "error", "error": "request body is not valid JSON"})
        return

    incident_id = body.get("incident_id") or conversation_id
    question = (body.get("question") or "").strip()
    if not question:
        yield json.dumps({"status": "error", "error": "question is required"})
        return

    events, history = await _read_store(store, conversation_id)
    if not events:
        yield json.dumps(
            {"status": "error", "error": f"no event log found for incident {incident_id}"}
        )
        return

    begin_model_run(incident_id)
    result = answer_about_incident(incident_id, question, events, history)

    if store is not None:
        await store.append_message(conversation_id, "user", question)
        await store.append_message(conversation_id, "assistant", result["answer"])

    yield json.dumps({"status": "ok", "incident_id": incident_id, **result})
