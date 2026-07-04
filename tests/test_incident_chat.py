"""A6b: the incident-chat agent answers questions about a COMPLETED incident, cites
event-log entries by seq (validated against the real log), and carries context across a
two-turn conversation via Makers session memory (the shared context.store).

Handler loaded by path (agents/ shadowed by agents.py); the single LLM call is scripted at
incident_chat._answer so citations and memory are asserted deterministically.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import incident_chat
from incident_chat import ChatAnswer

_HANDLER_PATH = Path(__file__).parents[1] / "agents" / "chat" / "index.py"
_spec = importlib.util.spec_from_file_location("chat_handler", _HANDLER_PATH)
chat_handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chat_handler)

# A completed incident's event log, as the incident runner published it to the store.
_EVENTS = [
    {"seq": 1, "actor": "system", "type": "incident_opened", "payload": {"summary": "Incident INC-9 opened."}},
    {"seq": 2, "actor": "triage", "type": "finding", "payload": {"summary": "Checkout is down for all customers."}},
    {"seq": 3, "actor": "diagnosis", "type": "finding", "payload": {"summary": "Connection pool exhausted after a bad deploy."}},
    {"seq": 4, "actor": "system", "type": "incident_resolved", "payload": {"summary": "Incident INC-9 resolved."}},
]


class _FakeStore:
    def __init__(self):
        self.messages = {}

    async def append_message(self, cid, role, content, **_):
        self.messages.setdefault(cid, []).append({"role": role, "content": content})

    async def get_messages(self, cid, limit=None, order="asc", **_):
        items = list(self.messages.get(cid, []))
        return list(reversed(items))[:limit] if order == "desc" else (items[:limit] if limit else items)


class _Ctx:
    def __init__(self, cid, body, store):
        self.conversation_id = cid
        self.request = type("R", (), {"body": json.dumps(body), "signal": None})()
        self.store = store
        self.run_id = "run-chat"


def _drive(ctx):
    async def collect():
        return [json.loads(c) async for c in chat_handler.handler(ctx)]
    return asyncio.run(collect())


def _seed_store(cid):
    store = _FakeStore()
    store.messages[cid] = [{"role": "system", "content": json.dumps(e)} for e in _EVENTS]
    return store


def test_two_turn_chat_cites_by_seq_and_remembers_context(monkeypatch):
    cid = "INC-9"
    store = _seed_store(cid)
    prompts = []
    answers = [
        ChatAnswer(answer="SEV was implied by triage: checkout down for all customers.",
                   cited_seqs=[2, 99]),          # 2 is real, 99 is hallucinated
        ChatAnswer(answer="Because a connection pool was exhausted after a bad deploy.",
                   cited_seqs=[3]),
    ]

    def fake_answer(prompt):
        prompts.append(prompt)
        return answers.pop(0)

    monkeypatch.setattr(incident_chat, "_answer", fake_answer)

    # --- Turn 1 ---
    first = _drive(_Ctx(cid, {"incident_id": cid, "question": "What did triage find?"}, store))[0]
    assert first["status"] == "ok"
    assert first["valid_citations"] == [2]          # grounded citation kept
    assert first["invalid_citations"] == [99]       # hallucinated seq surfaced, not trusted
    # The event log (with seqs) was actually handed to the model.
    assert "[seq 2]" in prompts[0] and "[seq 3]" in prompts[0]

    # --- Turn 2 (same conversation/store) ---
    second = _drive(_Ctx(cid, {"incident_id": cid, "question": "Why?"}, store))[0]
    assert second["valid_citations"] == [3]
    # Session memory: turn 2's prompt includes turn 1's question and answer.
    assert "What did triage find?" in prompts[1]
    assert "checkout down for all customers" in prompts[1].lower()


def test_chat_refuses_when_no_event_log_exists(monkeypatch):
    monkeypatch.setattr(incident_chat, "_answer", lambda prompt: ChatAnswer(answer="x"))
    out = _drive(_Ctx("INC-empty", {"question": "anything?"}, _FakeStore()))[0]
    assert out["status"] == "error"
    assert "no event log" in out["error"]
