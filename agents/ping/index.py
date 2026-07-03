"""Minimal agent route (Track B deploy plumbing) — POST /ping.

Exists because the platform provisions the project's conversation/blob store
only when an agents directory is present (builder: "Detected agents directory
— context.agent.store will be injected"); without one, cloud-function storage
writes vanish silently (observed during B1 bring-up). Doubles as a storage
healthcheck. Track A's real incident runner lands beside this at integration
(Phase B3).

Note: the agent-side context.store is ConversationMemory (append_message /
get_messages) — the generic put/get facade exists only on the cloud-function
side (self.context.agent.store).
"""

import json


async def handler(context):
    probe = "unknown"
    try:
        cid = context.conversation_id or "ping-probe"
        await context.store.append_message(cid, "user", "ping")
        messages = await context.store.get_messages(cid, limit=1, order="desc")
        probe = "ok" if messages else "append invisible"
    except Exception as e:
        probe = f"store error: {type(e).__name__}: {e}"
    yield json.dumps({"status": "ok", "store_probe": probe})
