"""A5: the Makers agent handler (agents/incident/index.py) drives the real pipeline
through a pause and a resume across TWO separate invocations that share only the
platform store — the exact shape RECON-B0 Q1 says a human approval pause forces.

Loaded by file path because `agents.py` (the factories) shadows the `agents/` package,
so `import agents.incident.index` cannot resolve. Crew calls are replaced at
pipeline._run_single_agent, the same seam the other pipeline tests use.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pipeline
from events import clear_events
from schemas import (
    CommanderDecision,
    PostmortemReport,
    RemediationAction,
    RemediationPlan,
    TriageReport,
    VerificationReport,
)

_HANDLER_PATH = Path(__file__).parents[1] / "agents" / "incident" / "index.py"
_spec = importlib.util.spec_from_file_location("incident_handler", _HANDLER_PATH)
incident_handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(incident_handler)


class _FakeStore:
    """In-memory stand-in for context.store (append_message / get_messages)."""

    def __init__(self) -> None:
        self.messages: dict[str, list[dict]] = {}

    async def append_message(self, cid: str, role: str, content: str, **_: object) -> None:
        self.messages.setdefault(cid, []).append({"role": role, "content": content})

    async def get_messages(self, cid: str, limit=None, order="asc", **_: object):
        items = list(self.messages.get(cid, []))
        if order == "desc":
            items = list(reversed(items))
        return items[:limit] if limit else items


class _FakeRequest:
    def __init__(self, body: dict) -> None:
        self.body = json.dumps(body)
        self.signal = None


class _FakeContext:
    def __init__(self, cid: str, run_id: str, body: dict, store: _FakeStore) -> None:
        self.conversation_id = cid
        self.run_id = run_id
        self.request = _FakeRequest(body)
        self.store = store
        self.tracer = None
        self.tools = None


def _script(monkeypatch, responses: dict) -> None:
    def fake(agent, description, expected_output, output_pydantic):
        value = responses[output_pydantic]
        return value.pop(0) if isinstance(value, list) else value

    monkeypatch.setattr(pipeline, "_run_single_agent", fake)


def _drive(ctx: _FakeContext) -> list[dict]:
    async def collect():
        return [json.loads(chunk) async for chunk in incident_handler.handler(ctx)]

    return asyncio.run(collect())


_SAFE = RemediationAction(action="Raise pool size", rationale="relieves pressure", destructive=False)
_RISKY = RemediationAction(action="Roll back the deploy", rationale="removes bad code", destructive=True)


def test_handler_pauses_then_resumes_across_two_invocations(monkeypatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "https://gateway.example/v1")
    monkeypatch.setenv("MAKERS_MODELS_KEY", "k")
    monkeypatch.setenv("LLM_PRIMARY_MODEL", "@makers/primary")
    monkeypatch.setenv("LLM_FALLBACK_MODEL", "@makers/fallback")

    incident_id = "INC-001-checkout-db-pool"
    clear_events(incident_id)
    store = _FakeStore()

    # --- Invocation 1: fresh run that stops at the risky-action approval gate. ---
    _script(monkeypatch, {
        TriageReport: TriageReport(
            severity="SEV-3", customer_facing=False,
            summary="Internal only.", route_to="Diagnosis", reason="low impact",
        ),
        CommanderDecision: CommanderDecision(move="fast_path", rationale="SEV-3, skip diagnosis"),
        RemediationPlan: RemediationPlan(safe=[_SAFE], risky=[_RISKY]),
    })
    first = _drive(_FakeContext(incident_id, "run-aaa", {"incident_id": incident_id}, store))

    assert len(first) == 1
    assert first[0]["status"] == "awaiting_approval"
    # trace_id was stamped from context.run_id on every published event.
    assert first[0]["events"], "events should have been published"
    assert all(e["trace_id"] == "run-aaa" for e in first[0]["events"])
    # The paused snapshot was persisted on the REQUEST conversation for a later
    # invocation — never on the event conversation /api/events reads.
    assert any(json.loads(m["content"]).get("kind") == "rescueops.snapshot"
               for m in store.messages[incident_id])
    # Events published to Track B's evt-<sha1> conversation (B3 reconciliation).
    evt_cid = incident_handler._event_cid(incident_id)
    published = [json.loads(m["content"]) for m in store.messages[evt_cid]]
    assert published and all(e["trace_id"] == "run-aaa" for e in published)
    assert sum(e["type"] == "approval_requested" for e in published) == 1
    pre_pause_count = len(published)

    # --- Invocation 2: a SEPARATE invocation (new run id) approves and resumes. ---
    _script(monkeypatch, {
        VerificationReport: VerificationReport(
            recovered=True, metric_name="error_rate", observed_value=0.001,
            threshold=0.01, note="recovered",
        ),
        PostmortemReport: PostmortemReport(
            summary="Resolved.", timeline=["t0: alert"], root_cause="pool exhaustion",
            actions_taken=["Raised pool"], follow_ups=["Add alert"],
        ),
    })
    second = _drive(_FakeContext(
        incident_id, "run-bbb",
        {"incident_id": incident_id,
         "approval": {"approved": True, "approver": "human-ui", "note": "ok"}},
        store,
    ))

    assert second[0]["status"] == "resolved"
    assert second[0]["run_id"] == first[0]["run_id"]  # same run resumed, not restarted
    assert second[0]["result"]["postmortem"] is not None

    # Warm-container dedup: the resume published ONLY its own new events — the
    # pre-pause ones were not re-published even though both invocations shared
    # this process's in-memory event log.
    published = [json.loads(m["content"]) for m in store.messages[evt_cid]]
    resumed = published[pre_pause_count:]
    assert resumed, "resume phase should publish new events"
    assert sum(e["type"] == "approval_requested" for e in published) == 1
    assert sum(e["type"] == "incident_resolved" for e in published) == 1
    # /api/approval is the single writer of approval events — the pipeline's own
    # copy is filtered out of the published stream.
    assert not any(e["type"] in ("approval_granted", "approval_denied") for e in published)


def test_handler_rejects_approval_without_a_paused_incident(monkeypatch) -> None:
    _script(monkeypatch, {})  # no crew calls should happen
    out = _drive(_FakeContext(
        "INC-404", "run-x",
        {"incident_id": "INC-404", "approval": {"approved": True, "approver": "x", "note": ""}},
        _FakeStore(),
    ))
    assert out[0]["status"] == "error"
    assert "no paused incident" in out[0]["error"]
