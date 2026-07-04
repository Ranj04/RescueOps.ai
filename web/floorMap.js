// The §5 event→animation mapping, implemented as LITERALLY A TABLE (TRACK-B
// B4b): each event type maps to a directive object; `deriveFloor` below is a
// small interpreter that folds real events through the table. Nothing here
// runs on timers — every visual is a pure function of the event stream (§4).

export const SPECIALISTS = [
  "triage",
  "diagnosis",
  "remediation",
  "verification",
  "postmortem",
  "human",
];

const BUBBLE_MAX = 90; // TRACK-B: truncate speech bubbles at 90 chars

// Directive vocabulary (any subset per event type):
//   alarm: bool           spin/stop the alarm light
//   light: "red"|"green"  status light
//   board: "post"|"resolved"
//   bubble: "actor"|"commander"|<name>   speech bubble = payload.summary
//   state: { <who>: "idle"|"working"|"blocked"|"down" }  who may be "actor"/"target"
//   tag:   { <who>: string|null }
//   rack: "auto"|"gateway"  + rackEffect: "blink"|"warn"
//   fires: "flags"        set rack fires from payload.flags (chaos)
//   clearFires: bool      extinguish everything (chaos_cleared)
//   warnAll: bool         ⚠ on every character (dependents of chaos)
//   float: "✔"|"✖"        floats up from the human desk
//   confetti: bool        burst (verification_passed)
//   podiumFlash: bool     commander_overruled flash
//   allIdle: bool         everyone back to desks
export const EVENT_ANIMATIONS = {
  incident_opened: { alarm: true, light: "red", board: "post" },
  agent_dispatched: { bubble: "commander", state: { target: "working" } },
  agent_started: { state: { actor: "working" } },
  tool_call: { state: { actor: "working" }, rack: "auto", rackEffect: "blink" },
  tool_result: { rack: "auto", rackEffect: "blink" },
  tool_failed: { state: { actor: "down" }, rack: "auto", rackEffect: "warn" },
  finding: { bubble: "actor", state: { actor: "idle" } },
  action_proposed: { bubble: "actor", state: { actor: "working" } },
  approval_requested: {
    state: { remediation: "blocked", human: "working" },
    tag: { remediation: "WAITING FOR HUMAN", human: "DECIDING…" },
  },
  approval_granted: {
    float: "✔",
    bubble: "human",
    state: { remediation: "working", human: "idle" },
    tag: { remediation: null, human: null },
  },
  approval_denied: {
    float: "✖",
    bubble: "human",
    state: { remediation: "idle", human: "idle" },
    tag: { remediation: null, human: null },
  },
  action_executed: { bubble: "actor", state: { actor: "working" } },
  verification_passed: { confetti: true, light: "green", state: { verification: "idle" } },
  verification_failed: { light: "red", state: { verification: "blocked" } },
  commander_decision: { bubble: "commander" },
  commander_overruled: { bubble: "commander", podiumFlash: true },
  model_fallback: { rack: "gateway", rackEffect: "warn", bubble: "commander" },
  chaos_injected: { fires: "flags", warnAll: true },
  chaos_cleared: { clearFires: true },
  incident_resolved: { allIdle: true, board: "resolved", light: "green", alarm: false },
  postmortem_ready: { bubble: "postmortem", state: { postmortem: "working" } },
  oncall_notified: { bubble: "human", tag: { human: "📱 TEXTED" } },
  oncall_reply: { bubble: "human", tag: { human: "📱 REPLIED" } },
};

function rackFor(event) {
  if (event.payload?.cve_id) return "cve";
  if (event.payload?.tool) return event.payload.tool;
  return null;
}

function whoIs(key, event) {
  if (key === "actor") return event.actor;
  if (key === "target") return event.payload?.agent;
  return key;
}

export function deriveFloor(events, rackIds) {
  const floor = {
    alarm: false,
    light: "off",
    board: null, // { id, resolved }
    commanderBubble: null,
    podiumFlashSeq: 0,
    chars: Object.fromEntries(
      SPECIALISTS.map((s) => [s, { state: "idle", bubble: null, tag: null, warn: false }])
    ),
    racks: Object.fromEntries(
      rackIds.map((r) => [r, { fire: false, warn: false, blinkSeq: 0 }])
    ),
    confettiSeq: 0,
    float: null, // { seq, symbol }
  };

  for (const event of events) {
    const d = EVENT_ANIMATIONS[event.type];
    if (!d) continue;
    const summary = (event.payload?.summary || "").slice(0, BUBBLE_MAX);

    if (d.alarm !== undefined) floor.alarm = d.alarm;
    if (d.light) floor.light = d.light;
    if (d.board === "post") floor.board = { id: event.incident_id, resolved: false };
    if (d.board === "resolved" && floor.board) floor.board.resolved = true;

    if (d.bubble) {
      const who = whoIs(d.bubble, event);
      if (who === "commander") floor.commanderBubble = summary;
      else if (floor.chars[who]) floor.chars[who].bubble = summary;
    }
    if (d.state) {
      for (const [key, st] of Object.entries(d.state)) {
        const who = whoIs(key, event);
        if (floor.chars[who]) floor.chars[who].state = st;
      }
    }
    if (d.tag) {
      for (const [key, text] of Object.entries(d.tag)) {
        const who = whoIs(key, event);
        if (floor.chars[who]) floor.chars[who].tag = text;
      }
    }

    if (d.rack) {
      const id = d.rack === "auto" ? rackFor(event) : d.rack;
      const rack = floor.racks[id];
      if (rack) {
        if (d.rackEffect === "blink") rack.blinkSeq = event.seq;
        if (d.rackEffect === "warn") rack.warn = true;
      }
    }
    if (d.fires === "flags") {
      const flags = event.payload?.flags || {};
      for (const id of rackIds) {
        floor.racks[id].fire =
          (flags.disable_sources || []).includes(id) ||
          (id === "gateway" && !!flags.break_primary_model) ||
          (id === "cve" && !!flags.kill_real_feed);
      }
    }
    if (d.clearFires) {
      for (const id of rackIds) {
        floor.racks[id].fire = false;
        floor.racks[id].warn = false;
      }
      for (const s of SPECIALISTS) floor.chars[s].warn = false;
    }
    if (d.warnAll) for (const s of SPECIALISTS) floor.chars[s].warn = true;

    if (d.float) floor.float = { seq: event.seq, symbol: d.float };
    if (d.confetti) floor.confettiSeq = event.seq;
    if (d.podiumFlash) floor.podiumFlashSeq = event.seq;
    if (d.allIdle) {
      for (const s of SPECIALISTS) {
        floor.chars[s].state = "idle";
        floor.chars[s].tag = null;
      }
    }
  }
  return floor;
}
