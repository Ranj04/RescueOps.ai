"""Phase A4 live-wiring tests: pipeline.py driven by IncidentStateMachine + the
Commander agent, with real CrewAI/network calls replaced by scripted responses
at the `_run_single_agent` boundary (the same seam agents.py factories build
against). These exercise control flow — fast-path skip, escalation, illegal
Commander moves, and the retry loop's second approval pause — that
tests/test_policy_state_machine.py already covers offline for the state
machine itself, but pipeline.py never previously invoked.
"""
from __future__ import annotations

import pipeline
from events import clear_events, list_events
from schemas import (
    ApprovalDecision,
    CommanderDecision,
    DiagnosisReport,
    PostmortemReport,
    RemediationAction,
    RemediationPlan,
    TriageReport,
    VerificationReport,
)


def _configure(monkeypatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "https://gateway.example/v1")
    monkeypatch.setenv("MAKERS_MODELS_KEY", "test-key")
    monkeypatch.setenv("LLM_PRIMARY_MODEL", "@makers/primary")
    monkeypatch.setenv("LLM_FALLBACK_MODEL", "@makers/fallback")


def _script(monkeypatch, responses: dict[type, object]) -> None:
    """Replace pipeline._run_single_agent with a fake dispatching on
    output_pydantic. A list value is popped in call order (for decision
    points hit more than once); anything else is returned every time."""

    def fake(agent, description, expected_output, output_pydantic):
        value = responses[output_pydantic]
        if isinstance(value, list):
            return value.pop(0)
        return value

    monkeypatch.setattr(pipeline, "_run_single_agent", fake)


_TRIAGE_SEV3 = TriageReport(
    severity="SEV-3", customer_facing=False,
    summary="Internal batch job alert only.", route_to="Diagnosis", reason="low impact",
)
_TRIAGE_SEV1 = TriageReport(
    severity="SEV-1", customer_facing=True,
    summary="Checkout is down for all customers.", route_to="Diagnosis", reason="full outage",
)
_DIAGNOSIS_LOW_CONF = DiagnosisReport(
    root_cause="Unclear — telemetry too sparse to confirm.",
    cited_evidence=[], confidence=0.1, reasoning="insufficient signal",
)
_DIAGNOSIS_OK = DiagnosisReport(
    root_cause="Connection pool exhausted after a bad deploy.",
    cited_evidence=["pool.active=100/100"], confidence=0.9, reasoning="pool saturated post-deploy",
)
_SAFE_ACTION = RemediationAction(action="Scale up pool size", rationale="relieves pressure", destructive=False)
_RISKY_ACTION = RemediationAction(action="Rollback the deploy", rationale="removes bad code", destructive=True)
_PLAN_NO_RISK = RemediationPlan(safe=[_SAFE_ACTION], risky=[])
_PLAN_RISKY = RemediationPlan(safe=[_SAFE_ACTION], risky=[_RISKY_ACTION])
_VERIFY_PASS = VerificationReport(
    recovered=True, metric_name="error_rate", observed_value=0.001, threshold=0.01, note="recovered",
)
_VERIFY_FAIL = VerificationReport(
    recovered=False, metric_name="error_rate", observed_value=0.5, threshold=0.01, note="still failing",
)
_POSTMORTEM = PostmortemReport(
    summary="Resolved after remediation.", timeline=["t0: alert fired"],
    root_cause="Connection pool exhausted.", actions_taken=["Scaled up pool"], follow_ups=["Add pool alert"],
)


def test_sev3_fast_paths_and_skips_diagnosis(monkeypatch) -> None:
    _configure(monkeypatch)
    clear_events("INC-001-checkout-db-pool")
    _script(monkeypatch, {
        TriageReport: _TRIAGE_SEV3,
        CommanderDecision: CommanderDecision(move="fast_path", rationale="SEV-3, no customer impact"),
        RemediationPlan: _PLAN_NO_RISK,
        VerificationReport: _VERIFY_PASS,
        PostmortemReport: _POSTMORTEM,
    })

    result = pipeline.run_until_approval("INC-001-checkout-db-pool")

    assert result.status == pipeline.STATUS_RESOLVED
    assert result.diagnosis is None  # deep diagnosis was skipped
    events = list_events("INC-001-checkout-db-pool")
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert "diagnosis" not in [e["actor"] for e in events if e["type"] == "finding"]


def test_low_confidence_diagnosis_escalates_before_remediation(monkeypatch) -> None:
    _configure(monkeypatch)
    clear_events("INC-002-payment-latency-baddeploy")
    _script(monkeypatch, {
        TriageReport: _TRIAGE_SEV1,
        CommanderDecision: [
            CommanderDecision(move="deep_diagnosis", rationale="SEV-1 needs a real root cause"),
            CommanderDecision(move="escalate_to_human", rationale="confidence too low to remediate"),
        ],
        DiagnosisReport: _DIAGNOSIS_LOW_CONF,
    })

    result = pipeline.run_until_approval("INC-002-payment-latency-baddeploy")

    assert result.status == pipeline.STATUS_ESCALATED
    assert result.diagnosis is not None
    assert result.remediation is None


def test_illegal_commander_move_at_triage_falls_back_and_overrules(monkeypatch) -> None:
    _configure(monkeypatch)
    clear_events("INC-003-redis-cache-outage")
    _script(monkeypatch, {
        TriageReport: TriageReport(
            severity="SEV-2", customer_facing=True, summary="Elevated latency.",
            route_to="Diagnosis", reason="customer facing",
        ),
        CommanderDecision: [
            CommanderDecision(move="launch_missiles", rationale="not a legal move"),
            CommanderDecision(move="dispatch_remediation", rationale="confidence is fine"),
        ],
        DiagnosisReport: _DIAGNOSIS_OK,
        RemediationPlan: _PLAN_NO_RISK,
        VerificationReport: _VERIFY_PASS,
        PostmortemReport: _POSTMORTEM,
    })

    result = pipeline.run_until_approval("INC-003-redis-cache-outage")

    assert result.status == pipeline.STATUS_RESOLVED
    events = list_events("INC-003-redis-cache-outage")
    overrule = next(e for e in events if e["type"] == "commander_overruled")
    assert overrule["payload"]["requested_move"] == "launch_missiles"
    assert overrule["payload"]["applied_move"] == "deep_diagnosis"  # triage's policy default


def test_retry_after_failed_verification_can_pause_for_a_second_approval(monkeypatch) -> None:
    _configure(monkeypatch)
    clear_events("INC-004-autoscaler-underprovisioned")
    plans = [_PLAN_RISKY, _PLAN_RISKY]
    verifications = [_VERIFY_FAIL, _VERIFY_PASS]
    _script(monkeypatch, {
        TriageReport: _TRIAGE_SEV3,
        CommanderDecision: [
            CommanderDecision(move="fast_path", rationale="SEV-3, skip diagnosis"),
            CommanderDecision(move="retry_remediation", rationale="one bounded retry may recover it"),
        ],
        RemediationPlan: plans,
        VerificationReport: verifications,
        PostmortemReport: _POSTMORTEM,
    })

    first = pipeline.run_until_approval("INC-004-autoscaler-underprovisioned")
    assert first.status == pipeline.STATUS_AWAITING

    approve = ApprovalDecision(approved=True, approver="human-ui", note="looks safe")
    second = pipeline.resume_after_approval(first, approve)

    assert second.status == pipeline.STATUS_AWAITING  # retry proposed new risky actions again
    assert second.run_id == first.run_id

    third = pipeline.resume_after_approval(second, approve)
    assert third.status == pipeline.STATUS_RESOLVED
    assert third.postmortem is not None

    events = list_events("INC-004-autoscaler-underprovisioned")
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert events[-1]["type"] == "incident_resolved"


def test_paused_run_resumes_from_persisted_state_after_process_restart(monkeypatch) -> None:
    """A4 verify: the awaiting_approval RunResult round-trips through JSON — the
    exact shape a process restart forces (persist on pause, revalidate on resume) —
    and the restored state machine continues from awaiting_approval, not the start."""
    from schemas import RunResult

    _configure(monkeypatch)
    clear_events("INC-001-checkout-db-pool")
    _script(monkeypatch, {
        TriageReport: _TRIAGE_SEV3,
        CommanderDecision: CommanderDecision(move="fast_path", rationale="SEV-3, skip diagnosis"),
        RemediationPlan: _PLAN_RISKY,
        VerificationReport: _VERIFY_PASS,
        PostmortemReport: _POSTMORTEM,
    })

    paused = pipeline.run_until_approval("INC-001-checkout-db-pool")
    assert paused.status == pipeline.STATUS_AWAITING

    # Simulate the process dying and a new one restoring the run from storage.
    restored = RunResult.model_validate_json(paused.model_dump_json())

    resolved = pipeline.resume_after_approval(
        restored, ApprovalDecision(approved=True, approver="human-ui", note="approved after restart"),
    )

    assert resolved.status == pipeline.STATUS_RESOLVED
    assert resolved.run_id == paused.run_id
    events = list_events("INC-001-checkout-db-pool")
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    types = [e["type"] for e in events]
    assert types.count("incident_opened") == 1  # resumed, not restarted
    assert "approval_granted" in types and types[-1] == "incident_resolved"


def test_retry_cap_reached_escalates_instead_of_looping_forever(monkeypatch) -> None:
    _configure(monkeypatch)
    clear_events("INC-005-expired-api-key")
    _script(monkeypatch, {
        TriageReport: _TRIAGE_SEV3,
        CommanderDecision: [
            CommanderDecision(move="fast_path", rationale="SEV-3, skip diagnosis"),
            CommanderDecision(move="retry_remediation", rationale="try once more"),
            CommanderDecision(move="retry_remediation", rationale="the commander keeps asking, but the cap is 1"),
        ],
        RemediationPlan: _PLAN_NO_RISK,
        VerificationReport: _VERIFY_FAIL,
    })

    result = pipeline.run_until_approval("INC-005-expired-api-key")

    assert result.status == pipeline.STATUS_ESCALATED
    events = list_events("INC-005-expired-api-key")
    assert events[-1]["type"] == "commander_overruled"
    assert events[-1]["payload"]["applied_move"] == "escalate"
