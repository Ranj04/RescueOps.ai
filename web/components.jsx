import React from "react";

const ACTOR_GLYPH = {
  commander: "◆",
  triage: "▲",
  diagnosis: "●",
  remediation: "■",
  verification: "✓",
  postmortem: "☰",
  human: "☺",
  chaos: "🔥",
  gateway: "⇄",
  system: "◈",
};

const TYPE_CLASS = {
  approval_requested: "evt-warn",
  approval_denied: "evt-bad",
  tool_failed: "evt-bad",
  verification_failed: "evt-bad",
  chaos_injected: "evt-bad",
  commander_overruled: "evt-bad",
  approval_granted: "evt-good",
  verification_passed: "evt-good",
  incident_resolved: "evt-good",
  chaos_cleared: "evt-good",
};

export function StatusStrip({ status, incident }) {
  const label = {
    idle: "STANDBY",
    active: "INCIDENT ACTIVE",
    waiting: "WAITING FOR HUMAN",
    resolved: "RESOLVED",
  }[status];
  return (
    <div className={`status-strip st-${status}`}>
      <span className="lamp" />
      <span className="st-label">{label}</span>
      {incident && <span className="st-alert">{incident.alert}</span>}
    </div>
  );
}

export function Timeline({ events }) {
  if (!events.length) {
    return (
      <div className="panel timeline">
        <div className="panel-title">EVENT STREAM</div>
        <div className="empty">No events yet — run an incident and the crew takes it from there.</div>
      </div>
    );
  }
  return (
    <div className="panel timeline">
      <div className="panel-title">
        EVENT STREAM <span className="count">{events.length} events</span>
      </div>
      <ol className="tl">
        {events.map((e) => (
          <li key={e.seq} className={`tl-row ${TYPE_CLASS[e.type] || ""}`}>
            <span className="tl-seq">{String(e.seq).padStart(3, "0")}</span>
            <span className="tl-actor">
              {ACTOR_GLYPH[e.actor] || "·"} {e.actor}
            </span>
            <span className="tl-type">{e.type}</span>
            <span className="tl-summary">{(e.payload?.summary || "").slice(0, 90)}</span>
            <span className="tl-ts">{(e.ts || "").slice(11, 19)}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}

export function ApprovalPanel({ pending, onDecide }) {
  return (
    <div className={`panel approval ${pending ? "pulsing" : ""}`}>
      <div className="panel-title">HUMAN GATE</div>
      {pending ? (
        <>
          <div className="appr-summary">{pending.payload?.summary}</div>
          <div className="appr-action">
            action: <code>{pending.payload?.action || "unknown"}</code>
          </div>
          <div className="appr-buttons">
            <button className="btn good" onClick={() => onDecide(true)}>
              APPROVE
            </button>
            <button className="btn bad" onClick={() => onDecide(false)}>
              DENY
            </button>
          </div>
        </>
      ) : (
        <div className="empty">No approval pending.</div>
      )}
    </div>
  );
}

const SOURCES = ["logs", "metrics", "deploys"];

export function ChaosConsole({ flags, onApply }) {
  if (!flags) return null;
  const toggleSource = (src) => {
    const set = new Set(flags.disable_sources || []);
    set.has(src) ? set.delete(src) : set.add(src);
    onApply({ ...flags, disable_sources: [...set] });
  };
  return (
    <div className="panel chaos">
      <div className="panel-title">CHAOS CONSOLE</div>
      <div className="chaos-row">
        {SOURCES.map((src) => {
          const dead = (flags.disable_sources || []).includes(src);
          return (
            <button
              key={src}
              className={`btn chaos-btn ${dead ? "bad" : ""}`}
              onClick={() => toggleSource(src)}
            >
              {dead ? "🔥" : "●"} {src}
            </button>
          );
        })}
      </div>
      <button
        className={`btn chaos-btn wide ${flags.break_primary_model ? "bad" : ""}`}
        onClick={() => onApply({ ...flags, break_primary_model: !flags.break_primary_model })}
      >
        {flags.break_primary_model ? "🔥 PRIMARY MODEL DOWN" : "● primary model"}
      </button>
    </div>
  );
}

export function EvalBoard({ summary, note, onRun }) {
  return (
    <div className="panel evalboard">
      <div className="panel-title">EVAL BOARD</div>
      {summary ? (
        <div className="eval-grid">
          {Object.entries({
            ...(summary.incidents_run != null ? { incidents_run: summary.incidents_run } : {}),
            ...(summary.aggregate || summary.scores || summary),
          }).map(([k, v]) => (
            <div key={k} className="eval-cell">
              <div className="eval-k">{k}</div>
              <div className="eval-v">{typeof v === "number" ? v.toFixed(2) : String(v)}</div>
            </div>
          ))}
        </div>
      ) : (
        <div className="empty">No cached evals for this pack yet.</div>
      )}
      {note && <div className="eval-note">{note}</div>}
      <button className="btn" onClick={onRun}>
        RUN EVALS
      </button>
    </div>
  );
}
