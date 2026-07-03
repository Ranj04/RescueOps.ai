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
  runEval: (pack) => req("/eval", { method: "POST", body: JSON.stringify({ pack }) }),

  // STUB controls (sanctioned scaffold — DELETE AT INTEGRATION, Phase B3)
  stubStart: (incident_id) =>
    req("/stub/start", { method: "POST", body: JSON.stringify({ incident_id }) }),
  stubTick: (incident_id) =>
    req("/stub/tick", { method: "POST", body: JSON.stringify({ incident_id }) }),
};
