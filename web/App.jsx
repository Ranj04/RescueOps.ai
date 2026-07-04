import React, { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import {
  ApprovalPanel,
  ChaosConsole,
  EvalBoard,
  StatusStrip,
  Timeline,
} from "./components.jsx";
import OpsFloor from "./OpsFloor.jsx";
// A pack ships its floor layout as data (§6: floor.json); both known packs are
// bundled at build time — the supply-chain redeploy beat rebuilds anyway.
import itOpsFloor from "../packs/it-ops/floor.json";
import secOpsFloor from "../packs/sec-ops/floor.json";

const FLOORS = { "it-ops": itOpsFloor, "sec-ops": secOpsFloor };

const POLL_MS = 1000; // ARCHITECTURE §4: 1s poll is the committed transport

// Derive the incident's display state purely from real events (§4 rule:
// nothing animates from timers or assumptions).
function deriveStatus(events) {
  if (!events.length) return "idle";
  let pendingApproval = false;
  let resolved = false;
  for (const e of events) {
    if (e.type === "approval_requested") pendingApproval = true;
    if (e.type === "approval_granted" || e.type === "approval_denied")
      pendingApproval = false;
    if (e.type === "incident_resolved") resolved = true;
  }
  if (resolved) return "resolved";
  if (pendingApproval) return "waiting";
  return "active";
}

export default function App() {
  const [packs, setPacks] = useState(["it-ops"]);
  const [pack, setPack] = useState("it-ops");
  const [incidents, setIncidents] = useState([]);
  const [incidentId, setIncidentId] = useState(null);
  const [events, setEvents] = useState([]);
  const [chaosFlags, setChaosFlags] = useState(null);
  const [evalSummary, setEvalSummary] = useState(null);
  const [evalNote, setEvalNote] = useState("");
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);
  const [view, setView] = useState("dashboard"); // dashboard = always the fallback renderer
  const lastSeq = useRef(0);
  const resumedSeq = useRef(0); // last approval-decision seq this client resumed
  const sawBacklog = useRef(false); // first poll batch is history, never resumed

  // -- bootstrap: packs, incidents, chaos, eval cache ----------------------
  useEffect(() => {
    api.packs().then(setPacks).catch(() => {});
    api.chaos().then(setChaosFlags).catch(() => {});
  }, []);

  useEffect(() => {
    api
      .incidents(pack)
      .then((list) => {
        setIncidents(list);
        if (list.length && !incidentId) setIncidentId(list[0].id);
      })
      .catch((e) => setError(String(e.message || e)));
    api.evalCache(pack).then(setEvalSummary).catch(() => {});
  }, [pack]);

  // -- switching incidents resets the local stream -------------------------
  useEffect(() => {
    setEvents([]);
    lastSeq.current = 0;
    resumedSeq.current = 0;
    sawBacklog.current = false;
  }, [incidentId]);

  // -- the poller (§4): read real events appended by the incident agent ----
  useEffect(() => {
    if (!incidentId) return;
    let live = true;
    const poll = async () => {
      try {
        const fresh = await api.events(incidentId, lastSeq.current);
        if (!live) return;
        const isBacklog = !sawBacklog.current;
        sawBacklog.current = true;
        if (!fresh.length) return;
        lastSeq.current = fresh[fresh.length - 1].seq;
        setEvents((prev) => [...prev, ...fresh]);

        // SINGLE resume path for BOTH channels (§6A): an approval decision
        // event landing in the stream — written by /api/approval, whether the
        // web panel or the SMS webhook called it — triggers the agent resume.
        // Backlog batches are history (a re-opened page must never re-run).
        if (isBacklog) return;
        for (const e of fresh) {
          if (
            (e.type === "approval_granted" || e.type === "approval_denied") &&
            e.seq > resumedSeq.current
          ) {
            resumedSeq.current = e.seq;
            setRunning(true);
            api
              .resumeIncident(incidentId, {
                approved: e.type === "approval_granted",
                approver: e.payload?.approver || "human-ui",
                note: e.payload?.note || "",
              })
              .catch(() => {}) // "no paused incident" = another client resumed
              .finally(() => live && setRunning(false));
          }
        }
      } catch (e) {
        if (live) setError(String(e.message || e));
      }
    };
    poll();
    const t = setInterval(poll, POLL_MS);
    return () => {
      live = false;
      clearInterval(t);
    };
  }, [incidentId]);

  const status = deriveStatus(events);
  const pendingApproval =
    status === "waiting"
      ? [...events].reverse().find((e) => e.type === "approval_requested")
      : null;

  // Kick off the REAL runner (agents/incident). The invocation returns when the
  // run pauses for approval, escalates, or resolves; the poller renders the
  // events it published along the way.
  const startIncident = useCallback(async () => {
    setRunning(true);
    try {
      const flags = await api.chaos().catch(() => null);
      const chaos_config = flags
        ? {
            disable_sources: flags.disable_sources || [],
            break_primary_model: !!flags.break_primary_model,
            // Chaos console name -> pipeline name for the live CVE feed kill.
            kill_cve_feed: !!flags.kill_real_feed,
          }
        : undefined;
      await api.runIncident(incidentId, chaos_config);
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setRunning(false);
    }
  }, [incidentId]);

  // The web panel only WRITES the approval event (/api/approval is the single
  // writer both channels share); the poller sees the event land and resumes —
  // the identical path an SMS-approved run takes.
  const decide = useCallback(
    (approved) =>
      api
        .approve(incidentId, approved)
        .catch((e) => setError(String(e.message || e))),
    [incidentId]
  );

  const applyChaos = useCallback(
    (flags) =>
      api
        .setChaos(flags, incidentId)
        .then(setChaosFlags)
        .catch((e) => setError(String(e.message || e))),
    [incidentId]
  );

  // Eval: score one incident per agent invocation (runtime limits), then
  // aggregate and cache under eval:{pack} so the board survives a cold refresh.
  const runEval = useCallback(async () => {
    try {
      const rows = [];
      for (let i = 0; i < incidents.length; i++) {
        setEvalNote(`scoring ${i + 1}/${incidents.length}…`);
        const scored = await api.scoreIncident(incidents[i].id);
        rows.push(scored.row);
      }
      setEvalNote("aggregating…");
      const finalized = await api.finalizeEval(pack, rows);
      const summary = await api.cacheEval(pack, finalized.summary);
      setEvalSummary(summary);
      setEvalNote("");
    } catch (e) {
      setEvalNote(String(e.message || e));
    }
  }, [pack, incidents]);

  return (
    <div className="shell">
      <header className="masthead">
        <div>
          <div className="brand">RESCUEOPS HQ</div>
          <div className="tagline">agent harness · live event stream</div>
        </div>
        <div className="controls">
          <label className="ctl">
            <span>DOMAIN</span>
            <select value={pack} onChange={(e) => setPack(e.target.value)}>
              {packs.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </label>
          <label className="ctl">
            <span>INCIDENT</span>
            <select
              value={incidentId || ""}
              onChange={(e) => setIncidentId(e.target.value)}
            >
              {incidents.map((i) => (
                <option key={i.id} value={i.id}>
                  {i.id}
                </option>
              ))}
            </select>
          </label>
          <button
            className="btn primary"
            onClick={startIncident}
            disabled={!incidentId || running}
          >
            {running ? "⏳ RUNNING…" : "▶ RUN INCIDENT"}
          </button>
        </div>
      </header>

      <StatusStrip status={status} incident={incidents.find((i) => i.id === incidentId)} />

      {error && (
        <div className="error-bar" onClick={() => setError("")}>
          {error} — click to dismiss
        </div>
      )}

      <main className="grid">
        <section className="col-main">
          <div className="view-tabs">
            <button
              className={`btn tab ${view === "dashboard" ? "tab-on" : ""}`}
              onClick={() => setView("dashboard")}
            >
              EVENT STREAM
            </button>
            <button
              className={`btn tab ${view === "floor" ? "tab-on" : ""}`}
              onClick={() => setView("floor")}
            >
              OPS FLOOR
            </button>
          </div>
          {view === "floor" && FLOORS[pack] ? (
            <OpsFloor events={events} floorConfig={FLOORS[pack]} />
          ) : (
            <Timeline events={events} />
          )}
        </section>
        <aside className="col-side">
          <ApprovalPanel pending={pendingApproval} onDecide={decide} />
          <ChaosConsole flags={chaosFlags} onApply={applyChaos} />
          <EvalBoard summary={evalSummary} note={evalNote} onRun={runEval} />
        </aside>
      </main>
    </div>
  );
}
