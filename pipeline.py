"""Incident response pipeline — sequential CrewAI crews with progressive autonomy.

Core behavior after remediation produces safe[] and risky[]:
  (a) every SAFE action is auto-executed with no human — that's the autonomy;
  (b) if risky[] is EMPTY -> continue straight to verification + postmortem and
      return a RESOLVED result with no pause — fully autonomous;
  (c) if risky[] is non-empty -> stop and surface them for human approve/deny —
      that's the governance.

Public API:
    run_until_approval(incident_id, chaos_config=None, on_stage=None) -> RunResult
        Triage -> diagnosis -> remediation, then auto-executes safe actions and
        either RESOLVES (no risky actions) or returns status="awaiting_approval"
        with the pending risky actions. The HTTP backend holds the returned
        RunResult in memory so the request never blocks on a human.

    resume_after_approval(result, decision, on_stage=None) -> RunResult
        Executes approved risky actions, then verification -> postmortem; returns
        a resolved RunResult. Only called when status was "awaiting_approval".

    run_incident(incident_id, chaos_config=None, approval_callback=None) -> RunResult
        CLI/eval convenience wrapper: runs to approval; if already resolved
        (autonomous path) returns it, otherwise applies a synchronous approval
        callback (auto-approves if none supplied) and resumes.

`on_stage(stage, artifact)` is an optional progress hook (used by the SSE stream in
Phase 9). It is called with each pydantic artifact as it is produced; default no-op.

Every incident-model binding routes through `llm_client`.
Confidence is computed deterministically from telemetry coverage, never by an LLM.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any, Dict, Optional, Tuple

from crewai import Crew, Process, Task

from agents import (
    build_commander_agent,
    build_diagnosis_agent,
    build_postmortem_agent,
    build_remediation_agent,
    build_triage_agent,
    build_verification_agent,
)
from events import append_event
from incidents import get_incident, load_rubric, observable
from llm_client import begin_model_run
from state_machine import IncidentStateMachine
from schemas import (
    ApprovalDecision,
    CommanderDecision,
    DiagnosisReport,
    PostmortemReport,
    RemediationAction,
    RemediationPlan,
    RunResult,
    TriageReport,
    VerificationReport,
)

# Run-status values carried on RunResult.status.
STATUS_AWAITING = "awaiting_approval"
STATUS_RESOLVED = "resolved"
STATUS_ESCALATED = "escalated"

# A synthetic stand-in for prompts that need "the diagnosis" when triage fast-pathed
# a low-severity incident straight to remediation and deep diagnosis never ran.
_NO_DIAGNOSIS = {
    "root_cause": "Not diagnosed — fast-pathed by policy for a low-severity incident.",
    "cited_evidence": [],
    "confidence": None,
    "reasoning": "N/A — deep diagnosis was skipped.",
}

# ---------------------------------------------------------------------------
# Optional Track-B dependency — no-op if not yet available
# ---------------------------------------------------------------------------
try:
    from chaos import apply_chaos as _apply_chaos
    _CHAOS_AVAILABLE = True
except ImportError:
    _CHAOS_AVAILABLE = False


_NOOP_STAGE = lambda stage, artifact: None  # noqa: E731 — trivial default hook


# ---------------------------------------------------------------------------
# Confidence computed deterministically from telemetry coverage (never by LLM)
# ---------------------------------------------------------------------------
def _compute_confidence(telemetry: dict) -> Tuple[float, str]:
    confidence = 1.0
    missing = []
    if not telemetry.get("logs"):
        confidence -= 0.30
        missing.append("logs (-0.30)")
    if not telemetry.get("metrics"):
        confidence -= 0.40
        missing.append("metrics (-0.40)")
    if not telemetry.get("deploys"):
        confidence -= 0.20
        missing.append("deploys (-0.20)")
    confidence = round(max(0.0, confidence), 2)
    note = ("missing: " + ", ".join(missing)) if missing else "all telemetry sources present"
    return confidence, note


def _prepare_observable(incident_id: str, chaos_config: Optional[Dict[str, Any]]) -> dict:
    """Load the incident and apply chaos. Pure given (incident_id, chaos_config),
    so both phases reconstruct the same observable without passing it around."""
    obs = observable(get_incident(incident_id))
    if _CHAOS_AVAILABLE and chaos_config:
        try:
            obs = _apply_chaos(obs, chaos_config)
        except Exception:
            pass
    return obs


# ---------------------------------------------------------------------------
# Task prompt builders
# ---------------------------------------------------------------------------
def _triage_prompt(obs: dict, rubric: str) -> str:
    return (
        "A production alert has just fired. Classify it by severity and route it "
        "to the right specialist.\n\n"
        f"OBSERVABLE INCIDENT DATA:\n{json.dumps(obs, indent=2)}\n\n"
        f"SEVERITY RUBRIC (single source of truth — classify strictly by this):\n{rubric}\n\n"
        "Pick the single best-fitting level and set `reason` to name the matched rule explicitly.\n"
        "Set route_to to \"Diagnosis\" unless this is a confirmed false alarm."
    )


def _diagnosis_prompt(obs: dict, confidence: float, coverage_note: str) -> str:
    return (
        "The Triage Engineer has classified this incident — see context above. "
        "Your job is to diagnose the root cause.\n\n"
        f"OBSERVABLE INCIDENT DATA:\n{json.dumps(obs, indent=2)}\n\n"
        f"CONFIDENCE (pipeline-computed, read-only): {confidence:.2f}\n"
        f"  Basis: {coverage_note}\n"
        f"  You MUST set confidence to exactly {confidence:.2f} — do not change it.\n\n"
        "Output requirements:\n"
        "  root_cause   — one precise sentence naming the specific failure cause\n"
        "  cited_evidence — list the exact telemetry keys and values that support your diagnosis\n"
        f"  confidence   — {confidence:.2f} (this exact value)\n"
        "  reasoning    — narrative connecting the evidence to the root cause"
    )


def _commander_prompt(context: dict) -> str:
    return (
        "You are the Incident Commander. Choose exactly one move for this incident's "
        "state machine — spelled exactly as it appears in legal_moves — and give a "
        "one-sentence rationale (it becomes your speech bubble on the Ops Floor).\n\n"
        f"Current state: {context['current_state']}\n"
        f"Legal moves: {context['legal_moves']}\n\n"
        f"Latest specialist output:\n{json.dumps(context['latest_specialist_output'], indent=2)}"
    )


def _commander_decide(machine: IncidentStateMachine, latest_output: dict) -> CommanderDecision:
    """Ask the Commander agent for a move; an unparseable reply is treated as an
    illegal move so the state machine's own fallback + commander_overruled event
    handles it, per ARCHITECTURE §3.2 ("Illegal LLM output -> policy default")."""
    context = machine.commander_context(latest_output)
    decision = _run_single_agent(
        build_commander_agent(),
        _commander_prompt(context),
        "A CommanderDecision naming exactly one legal move and a one-sentence rationale.",
        CommanderDecision,
    )
    return decision or CommanderDecision(
        move="__unparseable__",
        rationale="Commander output could not be parsed.",
    )


def _remediation_prompt(obs: dict, diagnosis: dict) -> str:
    return (
        "An incident has been diagnosed. Produce a remediation plan that directly addresses "
        "the confirmed root cause.\n\n"
        f"CONFIRMED DIAGNOSIS:\n{json.dumps(diagnosis, indent=2)}\n\n"
        f"OBSERVABLE INCIDENT DATA:\n{json.dumps(obs, indent=2)}\n\n"
        "Split your actions into two lists:\n"
        "  safe[]  — non-destructive, easily reversible (config tweaks, scaling, adding alerts, "
        "re-enabling a flag). These execute immediately without approval.\n"
        "  risky[] — destructive or hard to reverse (rolling back a deploy, restarting/deleting "
        "resources, failing over, rotating credentials, changing data). These require human approval.\n\n"
        "For EACH action provide: action (imperative), rationale (tie it to the root cause), "
        "and destructive (true for risky, false for safe).\n"
        "Include ONLY the actions you are actually executing now to resolve THIS incident. Do NOT add "
        "speculative, contingency, or 'if the safe fix doesn't work then…' fallback actions — those do "
        "not belong in the plan. An action is risky only if the real remediation genuinely requires a "
        "destructive or irreversible step. If safe, reversible actions fully resolve the incident, then "
        "risky[] MUST be empty — never manufacture risky actions to look thorough.\n"
        "Prefer the least-destructive action that fixes the root cause. Every action must be specific "
        "to THIS incident — no generic boilerplate."
    )


def _verification_prompt(obs: dict, diagnosis: dict, remediation: dict, approval: dict) -> str:
    return (
        "Remediation has been proposed and an approval decision made. Decide whether the incident "
        "recovers.\n\n"
        f"DIAGNOSIS:\n{json.dumps(diagnosis, indent=2)}\n\n"
        f"REMEDIATION PLAN:\n{json.dumps(remediation, indent=2)}\n\n"
        f"APPROVAL DECISION (risky actions approved = {approval.get('approved')}):\n"
        f"{json.dumps(approval, indent=2)}\n\n"
        f"OBSERVABLE INCIDENT DATA:\n{json.dumps(obs, indent=2)}\n\n"
        "Report a verification result:\n"
        "  metric_name   — the single key metric that proves recovery for THIS incident "
        "(choose from the telemetry/alert)\n"
        "  threshold     — the value the metric must beat to be healthy (from the alert/telemetry)\n"
        "  observed_value— the PROJECTED value of that metric after the APPROVED actions are applied\n"
        "  recovered     — true only if the approved actions are sufficient to cross the threshold. "
        "If the real fix is a risky action that was NOT approved, recovered must be false.\n"
        "  note          — one line; state explicitly this is a projected post-remediation check over "
        "simulated telemetry, not a live re-measurement.\n"
        "metric_name must be a string; threshold and observed_value must be numbers."
    )


def _postmortem_prompt(
    obs: dict, triage: dict, diagnosis: dict, remediation: dict, approval: dict, verification: dict
) -> str:
    return (
        "The incident response is complete. Write a blameless postmortem from the artifacts below.\n\n"
        f"TRIAGE:\n{json.dumps(triage, indent=2)}\n\n"
        f"DIAGNOSIS:\n{json.dumps(diagnosis, indent=2)}\n\n"
        f"REMEDIATION PLAN:\n{json.dumps(remediation, indent=2)}\n\n"
        f"APPROVAL DECISION:\n{json.dumps(approval, indent=2)}\n\n"
        f"VERIFICATION:\n{json.dumps(verification, indent=2)}\n\n"
        f"OBSERVABLE INCIDENT DATA:\n{json.dumps(obs, indent=2)}\n\n"
        "Produce:\n"
        "  summary       — one-paragraph executive summary of what happened and the outcome\n"
        "  timeline      — ordered events with timestamps drawn from the logs and deploys\n"
        "  root_cause    — the confirmed root cause\n"
        "  actions_taken — actions actually applied: ALL safe actions, plus risky actions ONLY if "
        "the approval decision approved them\n"
        "  follow_ups    — specific preventive measures to stop recurrence"
    )


# ---------------------------------------------------------------------------
# Crew runners — each stage is a sequential single-/dual-agent crew
# ---------------------------------------------------------------------------
def _run_single_agent(agent, description: str, expected_output: str, output_pydantic):
    """Run one agent as a single-task sequential crew; return its parsed pydantic output (or None)."""
    task = Task(
        description=description,
        expected_output=expected_output,
        agent=agent,
        output_pydantic=output_pydantic,
    )
    result = Crew(
        agents=[agent], tasks=[task], process=Process.sequential, verbose=True
    ).kickoff()
    return getattr(result.tasks_output[0], "pydantic", None)


def _stage_triage_and_diagnosis(
    machine: IncidentStateMachine,
    obs: dict,
    rubric: str,
    confidence: float,
    coverage_note: str,
    on_stage: Callable[[str, Any], None],
) -> Tuple[TriageReport, Optional[DiagnosisReport]]:
    """Triage, then let the Commander choose fast_path vs deep_diagnosis (both are
    legal `triage.commander_decides` moves in policy.json — there's no code default
    here beyond what the state machine already applies on an illegal/unparsed move).
    On fast_path, diagnosis is skipped entirely and `diagnosis` is returned as None.
    """
    triage = (
        _run_single_agent(
            build_triage_agent(rubric=rubric),
            _triage_prompt(obs, rubric),
            "Structured triage report classifying this incident.",
            TriageReport,
        )
        or TriageReport(
            severity="SEV-2",
            customer_facing=True,
            summary="(parse error — see crew logs)",
            route_to="Diagnosis",
            reason="parse error",
        )
    )
    on_stage("triage", triage)
    append_event(
        incident_id=machine.incident_id,
        actor="triage",
        event_type="finding",
        payload={"summary": triage.summary},
    )

    machine.after_triage(triage.severity, _commander_decide(machine, triage.model_dump()))

    if machine.current_state == "remediation":
        return triage, None  # fast-pathed — deep diagnosis skipped

    raw_diag = (
        _run_single_agent(
            build_diagnosis_agent(),
            _diagnosis_prompt(obs, confidence, coverage_note),
            "Structured diagnosis report identifying the root cause.",
            DiagnosisReport,
        )
        or DiagnosisReport(
            root_cause="(parse error — see crew logs)",
            cited_evidence=[],
            confidence=confidence,
            reasoning="parse error",
        )
    )
    # Override confidence with the deterministic pipeline value — never the LLM's number.
    diagnosis = raw_diag.model_copy(update={"confidence": confidence})
    on_stage("diagnosis", diagnosis)
    append_event(
        incident_id=machine.incident_id,
        actor="diagnosis",
        event_type="finding",
        payload={"summary": diagnosis.root_cause},
    )

    # diagnosis.commander_decides == true: dispatch_remediation vs escalate_to_human.
    machine.apply_move(_commander_decide(machine, diagnosis.model_dump()))
    return triage, diagnosis


# ---------------------------------------------------------------------------
# Parse-error fallbacks — generic and labelled, never incident-specific canned
# answers, so a parse failure can't masquerade as a real result.
# ---------------------------------------------------------------------------
def _fallback_plan() -> RemediationPlan:
    return RemediationPlan(
        safe=[
            RemediationAction(
                action="Escalate to on-call owner for manual remediation",
                rationale="Remediation agent output could not be parsed — see crew logs",
                destructive=False,
            )
        ],
        risky=[],
    )


def _fallback_verification() -> VerificationReport:
    return VerificationReport(
        recovered=False,
        metric_name="unknown",
        observed_value=0.0,
        threshold=0.0,
        note="(parse error — verification agent output could not be parsed; see crew logs)",
    )


def _fallback_postmortem(root_cause: str) -> PostmortemReport:
    return PostmortemReport(
        summary="(parse error — postmortem agent output could not be parsed; see crew logs)",
        timeline=["See audit log for the ordered stage events"],
        root_cause=root_cause or "unknown",
        actions_taken=["See remediation and approval artifacts"],
        follow_ups=["Re-run the postmortem stage"],
    )


# ---------------------------------------------------------------------------
# Simulated action execution. Per the hard constraints there are no real cloud
# integrations — "executing" an ops/runbook action means recording an
# action_executed event and surfacing it via on_stage. Safe actions run
# automatically; risky ones only after approval.
# ---------------------------------------------------------------------------
def _execute_actions(
    machine: IncidentStateMachine,
    actions: list[RemediationAction],
    kind: str,
    on_stage: Callable[[str, Any], None],
) -> list[RemediationAction]:
    for action in actions:
        append_event(
            incident_id=machine.incident_id,
            actor="remediation",
            event_type="action_executed",
            payload={"summary": action.action, "destructive": action.destructive},
        )
    on_stage(f"executed_{kind}", actions)
    return list(actions)


def _run_remediation_stage(
    machine: IncidentStateMachine,
    obs: dict,
    diagnosis: Optional[DiagnosisReport],
    on_stage: Callable[[str, Any], None],
) -> RemediationPlan:
    diagnosis_d = diagnosis.model_dump() if diagnosis else _NO_DIAGNOSIS
    plan = (
        _run_single_agent(
            build_remediation_agent(),
            _remediation_prompt(obs, diagnosis_d),
            "A remediation plan with safe[] and risky[] actions addressing the root cause.",
            RemediationPlan,
        )
        or _fallback_plan()
    )
    on_stage("remediation", plan)
    append_event(
        incident_id=machine.incident_id,
        actor="remediation",
        event_type="action_proposed",
        payload={
            "summary": f"Proposed {len(plan.safe)} safe and {len(plan.risky)} risky action(s).",
        },
    )
    return plan


def _run_verification(
    obs: dict,
    diagnosis: Optional[DiagnosisReport],
    plan: RemediationPlan,
    decision: ApprovalDecision,
) -> VerificationReport:
    diagnosis_d = diagnosis.model_dump() if diagnosis else _NO_DIAGNOSIS
    return (
        _run_single_agent(
            build_verification_agent(),
            _verification_prompt(obs, diagnosis_d, plan.model_dump(), decision.model_dump()),
            "A verification report stating the recovery metric, threshold, projected value, and recovered flag.",
            VerificationReport,
        )
        or _fallback_verification()
    )


def _run_postmortem(
    obs: dict,
    triage: TriageReport,
    diagnosis: Optional[DiagnosisReport],
    plan: RemediationPlan,
    decision: ApprovalDecision,
    verification: VerificationReport,
) -> PostmortemReport:
    diagnosis_d = diagnosis.model_dump() if diagnosis else _NO_DIAGNOSIS
    return (
        _run_single_agent(
            build_postmortem_agent(),
            _postmortem_prompt(
                obs, triage.model_dump(), diagnosis_d, plan.model_dump(),
                decision.model_dump(), verification.model_dump(),
            ),
            "A blameless postmortem with summary, timeline, root_cause, actions_taken, and follow_ups.",
            PostmortemReport,
        )
        or _fallback_postmortem(diagnosis_d.get("root_cause", ""))
    )


# ---------------------------------------------------------------------------
# Remediation -> auto-execute safe -> either pause for approval or fall
# straight through to verification. Used on the first pass and on every
# retry_remediation loop (see _verify_and_finish).
# ---------------------------------------------------------------------------
def _remediation_cycle(
    run_id: str,
    incident_id: str,
    obs: dict,
    triage: TriageReport,
    diagnosis: Optional[DiagnosisReport],
    machine: IncidentStateMachine,
    chaos_config: Optional[Dict[str, Any]],
    on_stage: Callable[[str, Any], None],
) -> RunResult:
    plan = _run_remediation_stage(machine, obs, diagnosis, on_stage)

    # (a) Auto-execute every safe action — no human in the loop. That's the autonomy.
    executed_safe = _execute_actions(machine, plan.safe, "safe", on_stage)

    # remediation.commander_decides == false: approval is code-forced on risky
    # classes, never a Commander choice (it "only phrases the request").
    action_classes = ["risky"] if plan.risky else []
    machine.after_remediation(action_classes)

    result = RunResult(
        run_id=run_id,
        incident_id=incident_id,
        status=STATUS_AWAITING,
        triage=triage,
        diagnosis=diagnosis,
        remediation=plan,
        executed_safe=executed_safe,
        chaos_config=chaos_config,
        state_snapshot=machine.to_json(),
    )

    # (b) Risky actions present -> stop and surface them for a human decision.
    if machine.current_state == "awaiting_approval":
        return result

    # (c) No risky actions -> auto-approve and continue straight to verification.
    decision = ApprovalDecision(
        approved=True,
        approver="auto",
        note="No risky actions proposed; resolved autonomously",
    )
    return _verify_and_finish(
        run_id, incident_id, obs, triage, diagnosis, plan, executed_safe,
        decision, machine, chaos_config, on_stage,
    )


# ---------------------------------------------------------------------------
# Shared tail: verification -> (postmortem | retry | escalate). Used by both
# the autonomous-resolve path and resume_after_approval. A retry loops back
# into _remediation_cycle, which can pause for a second approval.
# ---------------------------------------------------------------------------
def _verify_and_finish(
    run_id: str,
    incident_id: str,
    obs: dict,
    triage: TriageReport,
    diagnosis: Optional[DiagnosisReport],
    plan: RemediationPlan,
    executed_safe: list[RemediationAction],
    decision: ApprovalDecision,
    machine: IncidentStateMachine,
    chaos_config: Optional[Dict[str, Any]],
    on_stage: Callable[[str, Any], None],
) -> RunResult:
    verification = _run_verification(obs, diagnosis, plan, decision)
    on_stage("verification", verification)
    machine.after_verification(verification.recovered)

    base_fields = dict(
        run_id=run_id,
        incident_id=incident_id,
        triage=triage,
        diagnosis=diagnosis,
        remediation=plan,
        executed_safe=executed_safe,
        approval=decision,
        verification=verification,
        chaos_config=chaos_config,
    )

    if machine.current_state == "postmortem":
        postmortem = _run_postmortem(obs, triage, diagnosis, plan, decision, verification)
        on_stage("postmortem", postmortem)
        machine.after_postmortem()
        return RunResult(
            **base_fields,
            status=STATUS_RESOLVED,
            postmortem=postmortem,
            state_snapshot=machine.to_json(),
        )

    # Verification failed: now legally in "verification_decision", where
    # retry/escalate are real legal moves — consult the Commander here, not
    # before (verification_decision.commander_decides == true in policy.json).
    move = machine.after_verification_decision(
        _commander_decide(machine, verification.model_dump())
    )

    if move == "escalate":
        return RunResult(
            **base_fields,
            status=STATUS_ESCALATED,
            state_snapshot=machine.to_json(),
        )

    return _remediation_cycle(
        run_id, incident_id, obs, triage, diagnosis, machine, chaos_config, on_stage
    )


# ---------------------------------------------------------------------------
# Phase 1 — triage -> (diagnosis) -> remediation -> auto-execute safe; then
# either resolve/escalate autonomously or stop awaiting human approval.
# ---------------------------------------------------------------------------
def run_until_approval(
    incident_id: str,
    chaos_config: Optional[Dict[str, Any]] = None,
    on_stage: Optional[Callable[[str, Any], None]] = None,
) -> RunResult:
    on_stage = on_stage or _NOOP_STAGE
    run_id = str(uuid.uuid4())

    obs = _prepare_observable(incident_id, chaos_config)
    rubric = load_rubric()
    confidence, coverage_note = _compute_confidence(obs["telemetry"])
    begin_model_run(
        incident_id,
        force_primary_failure=bool(
            chaos_config and chaos_config.get("break_primary_model")
        ),
    )

    machine = IncidentStateMachine(incident_id)
    machine.start()

    triage, diagnosis = _stage_triage_and_diagnosis(
        machine, obs, rubric, confidence, coverage_note, on_stage
    )

    if machine.current_state == "escalated":
        return RunResult(
            run_id=run_id,
            incident_id=incident_id,
            status=STATUS_ESCALATED,
            triage=triage,
            diagnosis=diagnosis,
            chaos_config=chaos_config,
            state_snapshot=machine.to_json(),
        )

    return _remediation_cycle(
        run_id, incident_id, obs, triage, diagnosis, machine, chaos_config, on_stage
    )


# ---------------------------------------------------------------------------
# Phase 2 — apply the human decision, execute approved risky actions, then
# verification -> postmortem/retry/escalate. Only called when status was
# "awaiting_approval"; may itself return "awaiting_approval" again if a retry
# proposes new risky actions.
# ---------------------------------------------------------------------------
def resume_after_approval(
    result: RunResult,
    decision: ApprovalDecision,
    on_stage: Optional[Callable[[str, Any], None]] = None,
) -> RunResult:
    on_stage = on_stage or _NOOP_STAGE
    machine = IncidentStateMachine.from_json(result.state_snapshot)
    machine.after_approval(decision.approved)
    on_stage("approval", decision)

    # Reconstruct the same observable the agents saw when this cycle's remediation ran.
    obs = _prepare_observable(result.incident_id, result.chaos_config)
    begin_model_run(
        result.incident_id,
        force_primary_failure=bool(
            result.chaos_config and result.chaos_config.get("break_primary_model")
        ),
    )

    # Execute approved risky actions (simulated); skip them entirely on denial.
    if decision.approved:
        _execute_actions(machine, result.remediation.risky, "risky", on_stage)

    return _verify_and_finish(
        result.run_id, result.incident_id, obs, result.triage, result.diagnosis,
        result.remediation, result.executed_safe, decision, machine,
        result.chaos_config, on_stage,
    )


# ---------------------------------------------------------------------------
# CLI / eval convenience wrapper — autonomous when possible, else auto-approve.
# Loops because a single incident can pause for approval more than once.
# ---------------------------------------------------------------------------
def run_incident(
    incident_id: str,
    chaos_config: Optional[Dict[str, Any]] = None,
    approval_callback: Optional[Callable[[RemediationPlan], ApprovalDecision]] = None,
) -> RunResult:
    """Run the full pipeline end-to-end. If remediation produces no risky actions
    the run resolves autonomously. Otherwise `approval_callback` is called with the
    RemediationPlan; if None, risky actions are auto-approved."""
    result = run_until_approval(incident_id, chaos_config)

    while result.status == STATUS_AWAITING:
        if approval_callback is not None:
            decision = approval_callback(result.remediation)
        else:
            decision = ApprovalDecision(
                approved=True,
                approver="auto-cli",
                note="No approval_callback supplied; auto-approved per contract",
            )
        result = resume_after_approval(result, decision)

    return result
