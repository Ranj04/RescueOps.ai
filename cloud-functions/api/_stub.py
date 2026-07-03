"""STUB (sanctioned, ARCHITECTURE §8 day-one seam) — canned event producer.

DELETE THIS FILE AT INTEGRATION (Phase B3). Only the producer is fake: events
flow through the REAL storage append and the REAL /api/events read path.

Replays a canned INC-001 sequence on a wall-clock timer. The sequence pauses
at approval_requested and resumes only after a real approval_granted/denied
event (written by the real /api/approval endpoint) lands in storage.
"""

# (offset_seconds_from_start, actor, type, payload)
PRE_APPROVAL = [
    (0, "system", "incident_opened",
     {"summary": "Checkout API 5xx error rate at 37% for 4 minutes — incident INC-001 opened."}),
    (2, "commander", "commander_decision",
     {"summary": "Severity signals are strong; dispatching triage before anything else.",
      "decision": "dispatch_triage"}),
    (3, "commander", "agent_dispatched",
     {"summary": "Triage, take this one — checkout is bleeding 5xx errors.", "agent": "triage"}),
    (4, "triage", "agent_started",
     {"summary": "Triage picking up INC-001 and pulling the severity rubric."}),
    (6, "triage", "tool_call",
     {"summary": "Reading error-rate metrics for the checkout service.", "tool": "metrics"}),
    (8, "triage", "tool_result",
     {"summary": "Metrics show 5xx at 37% against a 5% threshold, sustained 4 minutes.", "tool": "metrics"}),
    (10, "triage", "finding",
     {"summary": "This is a SEV-1: sustained order-blocking failure on a revenue path."}),
    (12, "commander", "commander_decision",
     {"summary": "SEV-1 confirmed — going deep diagnosis, no fast path.", "decision": "deep_diagnosis"}),
    (13, "commander", "agent_dispatched",
     {"summary": "Diagnosis, find the root cause — logs and recent deploys first.", "agent": "diagnosis"}),
    (14, "diagnosis", "agent_started",
     {"summary": "Diagnosis correlating logs, metrics, and deploy history."}),
    (16, "diagnosis", "tool_call",
     {"summary": "Scanning checkout service logs for the error signature.", "tool": "logs"}),
    (18, "diagnosis", "tool_result",
     {"summary": "Logs show connection-pool exhaustion: 'pool timeout after 5000ms'.", "tool": "logs"}),
    (21, "diagnosis", "finding",
     {"summary": "Root cause: DB connection pool exhausted after the 14:02 deploy doubled worker count.",
      "confidence": 0.87}),
    (23, "commander", "commander_decision",
     {"summary": "Confidence 0.87 clears the bar — dispatching remediation.",
      "decision": "dispatch_remediation"}),
    (24, "remediation", "agent_started",
     {"summary": "Remediation drafting the recovery plan from the playbook."}),
    (26, "remediation", "action_proposed",
     {"summary": "Proposing to raise the DB pool ceiling and restart the checkout workers.",
      "risk": "risky", "action": "restart_checkout_workers"}),
    (28, "commander", "approval_requested",
     {"summary": "Restarting checkout workers is a risky action — requesting human approval.",
      "action": "restart_checkout_workers"}),
]

# offsets relative to the approval event's arrival
POST_APPROVAL = [
    (2, "remediation", "action_executed",
     {"summary": "Pool ceiling raised and checkout workers restarted cleanly.",
      "action": "restart_checkout_workers"}),
    (4, "verification", "agent_started",
     {"summary": "Verification watching the error rate for recovery."}),
    (7, "verification", "verification_passed",
     {"summary": "5xx rate back under 1% for two consecutive windows — recovery confirmed."}),
    (9, "postmortem", "postmortem_ready",
     {"summary": "Postmortem drafted: pool exhaustion after worker-count deploy, fixed by pool raise + restart."}),
    (10, "system", "incident_resolved",
     {"summary": "INC-001 resolved in 6m 41s with one human-approved action."}),
]

# denial path: offsets relative to the approval_denied event
POST_DENIAL = [
    (2, "remediation", "action_executed",
     {"summary": "Human denied the restart — applying the safe fallback: pool ceiling raise only.",
      "action": "raise_pool_ceiling"}),
    (5, "verification", "verification_passed",
     {"summary": "Error rate recovered under the safe fallback — no restart needed."}),
    (7, "system", "incident_resolved",
     {"summary": "INC-001 resolved via safe fallback after human denied the risky restart."}),
]
