"""Eval runner on the Makers agent runtime — Phase B2 (Track B logic, agent-hosted).

Real scoring needs the full pipeline (crewai + live gateway), which only exists on
the agent server image (RECON-B0 addendum 5) — the lean /api cloud-function bundle
cannot import it and is capped at 120 s. So the runner lives here and the frontend
orchestrates: one invocation per incident (stays inside the per-run limit), then a
finalize invocation aggregates, and POST /api/eval caches the summary in the store.

Requests (route: POST /eval):
  {"incident_id": "..."}          -> score that one labeled incident, return the row
  {"pack": "...", "rows": [...]}  -> aggregate rows into the cacheable summary
"""
from __future__ import annotations

import json
import os
import sys

# The deployed bundle routes agents/<name>/index.py; the shared modules live at the
# project root, so make sure it is importable (mirrors agents/incident/index.py).
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import incidents  # noqa: E402
from evaluation import score_incident, summarize  # noqa: E402


async def handler(context):
    try:
        raw_body = getattr(getattr(context, "request", None), "body", None) or "{}"
        body = json.loads(raw_body)
    except (TypeError, ValueError):
        yield json.dumps({"status": "error", "error": "request body is not valid JSON"})
        return

    try:
        if body.get("rows") is not None:
            pack = body.get("pack") or incidents.DEFAULT_PACK
            yield json.dumps({"status": "ok", "summary": summarize(pack, body["rows"])})
            return

        incident_id = body.get("incident_id")
        if not incident_id:
            yield json.dumps({"status": "error", "error": "incident_id (or rows) is required"})
            return
        row = score_incident(incidents.get_incident(incident_id))
        yield json.dumps({"status": "ok", "row": row})
    except Exception as e:  # scoring failures must surface, never hang the board
        yield json.dumps({"status": "error", "error": f"{type(e).__name__}: {e}"})
