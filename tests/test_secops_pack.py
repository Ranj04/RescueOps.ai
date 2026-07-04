"""A6a: sec-ops pack loads as a second domain, the Diagnosis CVE tool runs end-to-end
emitting tool events, and a chaos-killed feed degrades confidence instead of crashing.

Crew calls are scripted at pipeline._run_single_agent; the CVE feed HTTP call is replaced
with an injected fetch on the loaded tools module, so the suite is deterministic/offline.
The real NVD + CISA-KEV feed is smoke-verified separately (not committed, to avoid network
flakiness).
"""
from __future__ import annotations

import pipeline
from incidents import PACKS, load_incidents, load_pack_tools, load_rubric, pack_of
from events import clear_events, list_events
from schemas import (
    CommanderDecision,
    DiagnosisReport,
    PostmortemReport,
    RemediationAction,
    RemediationPlan,
    TriageReport,
    VerificationReport,
)

_SEC = "SEC-001-log4shell-jndi"

_NVD_LOG4SHELL = {
    "vulnerabilities": [{"cve": {
        "id": "CVE-2021-44228",
        "descriptions": [{"lang": "en", "value": "Apache Log4j2 JNDI RCE (Log4Shell)."}],
        "metrics": {"cvssMetricV31": [{"cvssData": {"baseScore": 10.0, "baseSeverity": "CRITICAL"}}]},
    }}]
}
_KEV_WITH_LOG4SHELL = {"vulnerabilities": [{"cveID": "CVE-2021-44228"}]}


def _fake_fetch(url: str):
    return _KEV_WITH_LOG4SHELL if "cisa.gov" in url else _NVD_LOG4SHELL


def _configure(monkeypatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "https://gateway.example/v1")
    monkeypatch.setenv("MAKERS_MODELS_KEY", "k")
    monkeypatch.setenv("LLM_PRIMARY_MODEL", "@makers/primary")
    monkeypatch.setenv("LLM_FALLBACK_MODEL", "@makers/fallback")


def _script(monkeypatch, responses: dict) -> None:
    def fake(agent, description, expected_output, output_pydantic):
        value = responses[output_pydantic]
        return value.pop(0) if isinstance(value, list) else value
    monkeypatch.setattr(pipeline, "_run_single_agent", fake)


def _inject_feed(monkeypatch):
    tools = load_pack_tools("sec-ops")
    tools.reset_cache()
    monkeypatch.setattr(tools, "_http_get_json", _fake_fetch)


_DIAG = DiagnosisReport(
    root_cause="Log4Shell RCE.", cited_evidence=["jndi:ldap in User-Agent"],
    confidence=0.9, reasoning="JNDI callback + child shell",
)
_PLAN = RemediationPlan(
    safe=[RemediationAction(action="Isolate pod egress", rationale="stop callbacks", destructive=False)],
    risky=[],
)
_VERIFY = VerificationReport(
    recovered=True, metric_name="egress_connections_per_min", observed_value=1.0,
    threshold=5.0, note="recovered",
)
_POST = PostmortemReport(
    summary="Contained.", timeline=["t0: jndi callback"], root_cause="Log4Shell",
    actions_taken=["Isolated pod"], follow_ups=["Patch log4j"],
)


def test_both_packs_exist_and_load() -> None:
    assert PACKS == ("it-ops", "sec-ops")
    assert len(load_incidents("sec-ops")) == 5
    assert "SEV-1" in load_rubric("sec-ops")
    assert pack_of(_SEC) == "sec-ops"


def test_secops_runs_end_to_end_and_emits_cve_tool_events(monkeypatch) -> None:
    _configure(monkeypatch)
    _inject_feed(monkeypatch)
    clear_events(_SEC)
    _script(monkeypatch, {
        TriageReport: TriageReport(
            severity="SEV-1", customer_facing=True,
            summary="Active Log4Shell exploitation.", route_to="Diagnosis", reason="known-exploited RCE",
        ),
        CommanderDecision: [
            CommanderDecision(move="deep_diagnosis", rationale="SEV-1 needs root cause"),
            CommanderDecision(move="dispatch_remediation", rationale="confidence high enough"),
        ],
        DiagnosisReport: _DIAG,
        RemediationPlan: _PLAN,
        VerificationReport: _VERIFY,
        PostmortemReport: _POST,
    })

    result = pipeline.run_until_approval(_SEC)

    assert result.status == pipeline.STATUS_RESOLVED
    types = [e["type"] for e in list_events(_SEC)]
    assert "tool_call" in types and "tool_result" in types  # CVE tool ran, feed healthy
    assert "tool_failed" not in types
    # Feed healthy → no confidence penalty.
    assert result.diagnosis.confidence == 1.0


def test_secops_feed_kill_degrades_confidence_without_crashing(monkeypatch) -> None:
    _configure(monkeypatch)
    _inject_feed(monkeypatch)
    clear_events(_SEC)
    _script(monkeypatch, {
        TriageReport: TriageReport(
            severity="SEV-1", customer_facing=True,
            summary="Active Log4Shell exploitation.", route_to="Diagnosis", reason="known-exploited RCE",
        ),
        CommanderDecision: [
            CommanderDecision(move="deep_diagnosis", rationale="SEV-1 needs root cause"),
            CommanderDecision(move="dispatch_remediation", rationale="proceed on degraded intel"),
        ],
        DiagnosisReport: _DIAG,
        RemediationPlan: _PLAN,
        VerificationReport: _VERIFY,
        PostmortemReport: _POST,
    })

    result = pipeline.run_until_approval(_SEC, chaos_config={"kill_cve_feed": True})

    assert result.status == pipeline.STATUS_RESOLVED  # degraded, not crashed
    events = list_events(_SEC)
    failed = [e for e in events if e["type"] == "tool_failed"]
    assert failed and "disabled by chaos" in failed[0]["payload"]["summary"]
    # One CVE, feed killed → 0.2 penalty off a full-telemetry 1.0 baseline.
    assert result.diagnosis.confidence == 0.8
