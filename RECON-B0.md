# Phase B0 — Platform recon (EdgeOne Makers)

Status: **DRAFT — doc evidence only.** Every answer below needs its "Observed" column
confirmed against the deployed hello-world before the B0 human gate can close
(TRACK-B.md B0). Items marked ⚠ are decision-affecting findings.

Doc root: https://pages.edgeone.ai/document/product-introduction
Console: https://console.tencentcloud.com/edgeone/makers

## Q1 — Agent-runtime handler shape (Python/CrewAI)

**Docs:** Python agents export `async def handler(context)`; the `context` object is the
platform's dependency-injection surface (request, tools, store) — no SDK import needed.
Requests are routed per agent path; every request carries a `Makers-Conversation-Id`
header (6–36 chars) that drives session-sticky routing, conversation-storage ownership,
and sandbox isolation. CrewAI is listed as a supported framework; `ctx.tools` exposes
framework adapters (e.g. `to_langchain_tools`).
- https://pages.edgeone.ai/document/agents-quick-start
- Template with the idiom live: https://pages.edgeone.ai/templates/python-starter-agent
  (POST /chat SSE + tool loop, `context.store` memory, `context.request.signal` abort)

**Mid-run human pause fit:** the runtime allows runs up to 3600 s, but our pause is
unbounded — so `run_until_approval` must END the handler invocation, persist the
snapshot, and a later request (approval) starts a new invocation that restores it.
Session stickiness via `Makers-Conversation-Id` = one incident maps to one conversation id.

**Observed:** PENDING (deploy hello-world, confirm handler signature + a two-request
resume works).

## Q2 — Execution time limits

| Surface | Limit (docs) |
|---|---|
| Agent runtime | 30–3600 s per run, configurable in `edgeone.json` (`agents.timeout`) |
| Sandbox instance | 300–3600 s persistence |
| Cloud functions | 120 s max (30 s default); 6 MB request body; Node 20 / Python 3.10 / Go |
| Edge functions | 200 ms CPU (excl. I/O); JS (ES2023) only; 1 MB request body |

- https://pages.edgeone.ai/document/agents-quick-start
- https://pages.edgeone.ai/document/cloud-functions
- https://pages.edgeone.ai/document/edge-functions

⚠ Consequence: a full incident run (multiple LLM calls) cannot live in a cloud
function — it must run in the agent runtime. `api/` cloud functions stay thin
(reads, approval, toggles).

**Observed:** PENDING.

## Q3 — Model gateway: native fallback routing?

**Docs:** OpenAI-compatible gateway at `https://ai-gateway.edgeone.link/v1`, Bearer auth
with a Makers Models API key, models addressed as `@makers/<model>`. Vendor keys
(incl. Anthropic) are bound in console → encrypted/hosted; you switch providers by
changing the `model` string. **No documented native fallback routing across bound
keys/models** — nothing in the models doc describes automatic failover.
- https://pages.edgeone.ai/document/models

⚠ Consequence: Track A's in-code failover in `llm_client.py` (primary → one retry on
`LLM_FALLBACK_MODEL` + `model_fallback` event) remains the REAL path, not just a guard.
Anthropic key binding gives a `@makers/...` model string for `LLM_FALLBACK_MODEL`.

**Observed:** PENDING (bind key, confirm exact model string, confirm no console
fallback option).

## Q4 — KV/Blob API surface

**Docs (KV):** `put` / `get` (text|json|arrayBuffer|stream) / `delete` / `list`
(prefix + cursor, ≤256 per page). Key ≤512 B, value ≤25 MB, 1 GB/account,
≤10 namespaces. No append primitive — append = read-modify-write or one-key-per-event
with `list(prefix)`.
⚠ **Consistency:** writes are immediately readable on the SAME node; other nodes may
read stale values for up to **60 s**.
⚠ **Access:** the KV doc says KV is "only supported for use within Edge Functions",
while the cloud-functions overview says cloud functions integrate with KV and Blob.
Contradiction — must be resolved by observation; it decides where `storage.py` lives.
- https://pages.edgeone.ai/document/kv-storage
- https://pages.edgeone.ai/document/cloud-functions

Event-log design consequence: one key per event (`evt:{incident}:{seq:06d}`) +
`list(prefix)` for read-since-seq beats a single growing JSON value (25 MB cap,
read-modify-write races). The 60 s cross-node staleness must be measured: if real,
the poller and the writer should hit the same function/region, or reads go through
one canonical path.

**Observed:** PENDING (read-after-write timing across requests; whether agent runtime
and cloud functions can both reach the same namespace).

## Q5 — Secrets handling

**Docs:** project environment variables managed in console or via CLI
(`edgeone pages env ls|pull|add|rm`); .env paste-import supported; changes apply to
NEW deployments only; valid across all environments (no per-env split documented).
- https://edgeone.ai/document/180255338996572160 (env in CI)
- CLI: https://www.npmjs.com/package/edgeone

**Observed:** PENDING (confirm agent runtime reads them at runtime, not only build time).

## Q6 — Sandboxed tool mechanism

**Docs:** sandbox capabilities are atomized tools (`commands`, `files_*`, `browser_*`,
`code_interpreter`, `web_search`) exposed on `ctx.tools`; a tool call opts into the
sandbox simply by being one of these tools; per-tool timeouts; sandbox instance is
isolated per conversation id. Debug logging via `MAKERS_AGENT_TOOLKIT_DEBUG=1`.
- https://pages.edgeone.ai/document/sandbox-using-the-agent-framework

**Observed:** PENDING (needed later for §7 evidence; not core-MVP-blocking).

## Q7 — Streaming/SSE

**Docs:** yes — gateway supports native SSE; the starter agent template streams
token-by-token SSE. **Polling remains our committed default regardless** (per TRACK-B).
- https://pages.edgeone.ai/document/models

**Observed:** n/a (informational; no scope change).

## Q8 — Session store (context.store)

**Docs:** conversation storage on `context.store`: `appendMessage` / `getMessages` /
`updateMessage` / conversation CRUD; message content ≤50 MB serialized; ≤10k messages
per conversation; conversation `metadata` field holds **arbitrary business JSON** —
documented for state like summaries/tags. Backed by Blob, cloud-persisted.
- https://pages.edgeone.ai/document/agents-conversation-storage

Fit for the paused `run_until_approval` snapshot: YES on paper — store the snapshot
JSON in conversation `metadata` (or a message) keyed by incident id. Native-first rule
satisfied without hand-rolling, pending observation.

**Observed:** PENDING (write snapshot → new process/invocation reads it back).

## Q9 — Tracing / trace_id in-process

**Docs:** observability = console Metric Analysis + Log Analysis (per-function request
logs). **No documented in-process API to retrieve a trace/run id per agent/model/tool
call, and no documented per-trace URL shape.**
- https://pages.edgeone.ai/document/observability

Consequence (per TRACK-A A4): events stamp `trace_id: null` unless observation finds
an id (e.g. a request/conversation id surfaced on `context`). `Makers-Conversation-Id`
is a candidate correlator.

**Observed:** PENDING.

## Q10 — Gateway usage/latency metrics via API

**Docs:** metrics are console-surfaced; no public query API documented. Per TRACK-B:
not trivially available → **skip** (no eval-board footnote).

**Observed:** n/a unless something shows up in console.

## Q11 — Outbound HTTPS from cloud functions (gates §6A SMS)

**Docs:** edge functions have Fetch API for remote requests; cloud functions run full
Node/Python runtimes (which implies arbitrary HTTPS), but **egress policy to arbitrary
hosts (api.twilio.com) is not explicitly documented**.

**Observed:** PENDING — must be tested live before any SMS-stretch planning.

---

## CLI surface (OBSERVED, 2026-07-03, edgeone CLI v1.6.11 installed locally)

`edgeone login | whoami | switch | logout` + `edgeone makers init | dev |
generate-routes | env | link | deploy [directoryOrZip] | create [project-name]`
(`pages` is a deprecated alias for `makers`). `create` scaffolds from a template —
that is the hello-world deploy path. All makers commands require auth
(`edgeone login` or `EDGEONE_PAGES_API_TOKEN`); this machine has neither yet.

---

## Deployment-shape proposal (to lock at the human gate)

- Incident runner = **agent runtime** (`async def handler(context)`), one conversation
  id per incident; pause = end invocation + snapshot in `context.store` metadata.
- `api/` = **cloud functions** (Python): `GET events?since=`, incidents, approval,
  chaos toggles, eval — all ≤120 s work.
- Event log = **KV**, one key per event + `list(prefix)` reads (pending Q4 access
  resolution; fallback = Blob via context.store if KV is edge-only).
- Frontend = static `web/` on Pages, polling per ARCHITECTURE §4.
- Models = gateway `https://ai-gateway.edgeone.link/v1`; primary
  `@makers/deepseek-v4-flash`; fallback = bound Anthropic key model string; failover
  stays in-code in `llm_client.py` (Q3: no native fallback found).

## `.env.template` updates implied (shared file — needs Track A ack)

- `LLM_BASE_URL=https://ai-gateway.edgeone.link/v1` as documented default.
- `LLM_FALLBACK_MODEL=` ← the `@makers/...` string of the bound Anthropic key (pending).
