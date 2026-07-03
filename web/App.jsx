import React, { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import {
  ApprovalPanel,
  ChaosConsole,
  EvalBoard,
  StatusStrip,
  Timeline,
} from "./components.jsx";

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
  const lastSeq = useRef(0);

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
  }, [incidentId]);

  // -- the poller (§4): tick the stub producer, then read real events ------
  useEffect(() => {
    if (!incidentId) return;
    let live = true;
    const poll = async () => {
      try {
        // STUB producer tick — sanctioned scaffold, DELETE AT INTEGRATION (B3).
        await api.stubTick(incidentId).catch(() => {});
        const fresh = await api.events(incidentId, lastSeq.current);
        if (!live || !fresh.length) return;
        lastSeq.current = fresh[fresh.length - 1].seq;
        setEvents((prev) => [...prev, ...fresh]);
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

  const startReplay = useCallback(() => {
    setEvents([]);
    lastSeq.current = 0;
    api.stubStart(incidentId).catch((e) => setError(String(e.message || e)));
  }, [incidentId]);

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

  const runEval = useCallback(() => {
    setEvalNote("running…");
    api
      .runEval(pack)
      .then((s) => {
        setEvalSummary(s);
        setEvalNote("");
      })
      .catch((e) => setEvalNote(String(e.message || e)));
  }, [pack]);

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
          <button className="btn primary" onClick={startReplay} disabled={!incidentId}>
            ▶ REPLAY (STUB)
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
          <Timeline events={events} />
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
