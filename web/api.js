// Thin fetch wrapper over the /api cloud function (same origin on prod).
async function req(path, options) {
  const res = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    throw new Error(data?.error || `${res.status} ${res.statusText}`);
  }
  return data;
}

// Conversation ids must be 6-36 chars with no ':' (RECON-B0 B1 addendum 2).
// Deterministic per incident so a resume lands on the same conversation
// (that's where the paused snapshot lives).
const cid = (prefix, id) =>
  `${prefix}-${id}`.replace(/[^A-Za-z0-9_-]/g, "-").slice(0, 36);

// The agent runtime may frame yielded chunks as SSE (`data: {...}`); accept both.
function parseAgentPayload(text) {
  try {
    return JSON.parse(text);
  } catch {}
  const dataLines = text.split("\n").filter((l) => l.startsWith("data:"));
  for (const line of dataLines.reverse()) {
    try {
      return JSON.parse(line.slice(5).trim());
    } catch {}
  }
  return null;
}

// POST to an agent route (agents/<name> -> /<name>) — same origin on prod.
async function agentReq(path, conversationId, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Makers-Conversation-Id": conversationId,
    },
    body: JSON.stringify(body),
  });
  const text = await res.text();
  const data = parseAgentPayload(text);
  if (!res.ok || data?.status === "error") {
    throw new Error(data?.error || `${res.status} ${res.statusText}`);
  }
  return data;
}

export const api = {
  packs: () => req("/packs"),
  incidents: (pack) => req(`/incidents?pack=${encodeURIComponent(pack)}`),
  events: (incident, since) =>
    req(`/events?incident=${encodeURIComponent(incident)}&since=${since}`),
  approve: (incident_id, approved, note = "") =>
    req("/approval", {
      method: "POST",
      body: JSON.stringify({ incident_id, approved, approver: "human-ui", note }),
    }),
  chaos: () => req("/chaos"),
  setChaos: (flags, incident_id) =>
    req("/chaos", { method: "POST", body: JSON.stringify({ ...flags, incident_id }) }),
  evalCache: (pack) => req(`/eval?pack=${encodeURIComponent(pack)}`),
  cacheEval: (pack, summary) =>
    req("/eval", { method: "POST", body: JSON.stringify({ pack, summary }) }),

  // Real runner (agents/incident) — one conversation per incident run.
  runIncident: (incident_id, chaos_config) =>
    agentReq("/incident", cid("run", incident_id), { incident_id, chaos_config }),
  resumeIncident: (incident_id, approval) =>
    agentReq("/incident", cid("run", incident_id), { incident_id, approval }),

  // Eval runner (agents/eval) — one invocation per incident, then finalize.
  scoreIncident: (incident_id) =>
    agentReq("/eval", cid("eval", incident_id), { incident_id }),
  finalizeEval: (pack, rows) => agentReq("/eval", cid("eval", pack), { pack, rows }),
};
