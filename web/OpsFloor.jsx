import React from "react";
import { SPECIALISTS, deriveFloor } from "./floorMap.js";

// The Ops Floor (ARCHITECTURE §5) — a second renderer of the IDENTICAL event
// stream the dashboard shows. DOM/CSS + inline SVG only; every animation is a
// pure function of real events via floorMap.js. The dashboard remains the
// fallback renderer if this view ever breaks.

const DESK_LABEL = {
  triage: "TRIAGE",
  diagnosis: "DIAGNOSIS",
  remediation: "REMEDIATION",
  verification: "VERIFICATION",
  postmortem: "POSTMORTEM",
  human: "ON-CALL",
};

// Flat 2-color humanoid (§10.3): ink head, pack-accent body.
function Responder({ accent, state }) {
  return (
    <svg viewBox="0 0 40 60" className={`resp resp-${state}`} aria-hidden="true">
      <circle cx="20" cy="11" r="8" fill="var(--ink)" />
      <rect x="11" y="22" width="18" height="22" rx="5" fill={accent} />
      <rect x="5" y="24" width="5" height="14" rx="2.5" fill={accent} />
      <rect x="30" y="24" width="5" height="14" rx="2.5" fill={accent} />
      <rect x="13" y="46" width="5" height="12" rx="2.5" fill="var(--ink)" />
      <rect x="22" y="46" width="5" height="12" rx="2.5" fill="var(--ink)" />
    </svg>
  );
}

function Desk({ name, accent, char }) {
  return (
    <div className={`desk desk-${char.state} ${char.warn ? "desk-warn" : ""}`}>
      {char.bubble && <div className="bubble">{char.bubble}</div>}
      {char.tag && <div className={`desk-tag ${char.state === "blocked" ? "tag-red" : ""}`}>{char.tag}</div>}
      <Responder accent={accent} state={char.state} />
      <div className="desk-surface" />
      <div className="desk-name">
        {DESK_LABEL[name]}
        {char.warn && <span className="desk-warn-mark"> ⚠</span>}
      </div>
    </div>
  );
}

function Rack({ rack, cfg }) {
  return (
    <div
      key={`${cfg.id}-${rack.blinkSeq}`}
      className={`rack ${rack.fire ? "rack-fire" : ""} ${rack.warn ? "rack-warn" : ""} ${
        rack.blinkSeq ? "rack-blink" : ""
      }`}
    >
      <div className="rack-icon">{rack.fire ? "🔥" : cfg.icon}</div>
      <div className="rack-label">{cfg.label}</div>
      <div className="rack-lights">
        <span /><span /><span />
      </div>
    </div>
  );
}

export default function OpsFloor({ events, floorConfig }) {
  const rackIds = floorConfig.racks.map((r) => r.id);
  const floor = deriveFloor(events, rackIds);
  const accent = floorConfig.accent;

  return (
    <div className="panel opsfloor" style={{ "--accent": accent }}>
      <div className="panel-title">
        OPS FLOOR{" "}
        <span className="count">{events.length ? "live" : "standby"}</span>
      </div>

      {/* wall: incident board + alarm + status light */}
      <div className="floor-wall">
        <div className={`alarm ${floor.alarm ? "alarm-on" : ""}`} />
        <div className="incident-board">
          {floor.board ? (
            <div className={`board-card ${floor.board.resolved ? "card-resolved" : ""}`}>
              <span className="card-id">{floor.board.id}</span>
              {floor.board.resolved && <span className="card-stamp">RESOLVED</span>}
            </div>
          ) : (
            <div className="board-empty">NO ACTIVE INCIDENT</div>
          )}
        </div>
        <div className={`floor-light light-${floor.light}`} />
      </div>

      {/* commander podium */}
      <div className="podium-row">
        <div
          key={`podium-${floor.podiumFlashSeq}`}
          className={`podium ${floor.podiumFlashSeq ? "podium-flash" : ""}`}
        >
          {floor.commanderBubble && <div className="bubble bubble-cmd">{floor.commanderBubble}</div>}
          <Responder accent="var(--amber)" state={floor.board && !floor.board.resolved ? "working" : "idle"} />
          <div className="podium-base" />
          <div className="desk-name">COMMANDER</div>
        </div>
        {floor.float && (
          <div key={`float-${floor.float.seq}`} className={`float-mark ${floor.float.symbol === "✔" ? "good" : "bad"}`}>
            {floor.float.symbol}
          </div>
        )}
        {floor.confettiSeq > 0 && (
          <div key={`confetti-${floor.confettiSeq}`} className="confetti">
            {Array.from({ length: 14 }).map((_, i) => (
              <i key={i} style={{ "--i": i }} />
            ))}
          </div>
        )}
      </div>

      {/* six desks */}
      <div className="desk-row">
        {SPECIALISTS.map((s) => (
          <Desk key={s} name={s} accent={accent} char={floor.chars[s]} />
        ))}
      </div>

      {/* service racks from the pack's floor.json */}
      <div className="rack-row">
        {floorConfig.racks.map((cfg) => (
          <Rack key={cfg.id} cfg={cfg} rack={floor.racks[cfg.id]} />
        ))}
      </div>
    </div>
  );
}
