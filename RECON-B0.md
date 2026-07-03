# Phase B0 — Platform recon (EdgeOne Makers)

Status: **OBSERVED 2026-07-03 — live evidence from deployed hello-world.** Hello-world
agent (python-starter-agent template) deployed to project `hello-recon`
(makers-guizfvthcvxj), live at **https://hello-recon.edgeone.cool** (root 200; POST
/chat streams SSE tokens). A throwaway probe cloud function (`POST /recon`) was used
for storage/env/egress/gateway observations. Two items remain console-only and move
to the human gate: Anthropic key binding (Q3, now optional) and the trace-view URL
shape (Q9). Items marked ⚠ are decision-affecting findings.

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

**Observed:** CONFIRMED. Template agent is `async def handler(context)` (an async
generator yielding SSE strings); `context` exposes `request.body`, `request.signal`,
`conversation_id`, `run_id`, `tracer`, `store`, `tools`. Two-request resume works:
request 1 ("hello") and request 2 ("what did I first say?") were separate invocations
sharing `Makers-Conversation-Id: recon-test-001`; request 2 recalled "hello" from
`context.store`. Response headers carry `Makers-Run-Id` per invocation.

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

**Observed:** PARTIAL (doc values stand; not load-tested to the limit). Multi-round
/chat runs of ~60 s completed fine in the agent runtime. Cloud functions confirmed
running on Tencent SCF (SCF_* env vars, `Eo-Pages-Inner-Scf-Status` response header);
probe requests returned in <1 s. The 120 s cloud-function cap was not provoked —
accepted as documented since our `api/` functions are all sub-second reads/writes.

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

**Observed:** CONFIRMED, with a decision-affecting improvement. Live gateway probes
(from a cloud function using the platform-injected key):
- Free catalog on the international site: `hy3-preview`, `deepseek-v4-flash`,
  `deepseek-v4-flash-202605`, `minimax-m2.7` — the gateway's own 400 error enumerates
  them. `@makers/minimax-m2.7` and `@makers/hy3-preview` both returned 200 completions.
- Vendor-bound models are addressed `<provider>/<model>`: calling
  `anthropic/claude-haiku-4-5` unbound returns 400 "No API key configured for provider
  \"anthropic\". Please bind your anthropic API key in the console." — exact binding
  mechanism and resulting model-string shape confirmed.
- No fallback routing option encountered anywhere; in-code failover stands.
- `GET /models` on the gateway → 404 (no model-list API).
⚠ NEW: `LLM_FALLBACK_MODEL=@makers/minimax-m2.7` works TODAY with zero console setup —
the model-failover demo beat no longer depends on binding an Anthropic key. Binding one
(→ `anthropic/<model-id>`) is now an optional upgrade, decided at the human gate.

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

**Observed:** RESOLVED — the contradiction breaks AGAINST KV, but a better native path
exists. Probe results from a Python cloud function:
- No KV binding exists on the cloud-function `context` (surface: `agent`, `client_ip`,
  `env`, `eo`, `geo`, `params`; `context.eo` holds only `client_ip`/`geo`). KV is
  edge-function (JS) only, as the KV doc says.
- ⚠ BUT `context.agent.store` (the Blob-backed conversation store) is available in BOTH
  the agent runtime and cloud functions, and exposes a generic key-value surface —
  `get` / `put` / `set` / `delete_key` — alongside `append_message` / `get_messages` /
  conversation CRUD (internally `_blob_store`).
- Cross-invocation read-after-write CONFIRMED: `store.put("recon:evt:000001", ...)` in
  one request, `store.get` in a separate request returned the exact value in 49 ms.
  No staleness observed (single canonical read path via our API functions regardless).
Event-log design lands on `context.agent.store`, not KV: either one key per event
(`evt:{incident}:{seq:06d}` via put/get) or one conversation per incident with one
message per event (`append_message`/`get_messages(order="asc")` is a native append-log
primitive). `storage.py` decides at implementation; both observed working.

## Q5 — Secrets handling

**Docs:** project environment variables managed in console or via CLI
(`edgeone pages env ls|pull|add|rm`); .env paste-import supported; changes apply to
NEW deployments only; valid across all environments (no per-env split documented).
- https://edgeone.ai/document/180255338996572160 (env in CI)
- CLI: https://www.npmjs.com/package/edgeone

**Observed:** CONFIRMED at runtime, plus a platform bonus: the runtime AUTO-INJECTS
`AI_GATEWAY_API_KEY` and `AI_GATEWAY_BASE_URL` into both agents and cloud functions —
the deployed hello-world streamed real LLM tokens with zero env vars configured
(project env pull showed none; probe confirmed both names present and non-empty at
runtime; values never printed). `AI_GATEWAY_MODEL` is NOT injected — model choice is
ours. Runtime = Tencent SCF containers (SCF_*/TENCENTCLOUD_* env vars observed).

## Q6 — Sandboxed tool mechanism

**Docs:** sandbox capabilities are atomized tools (`commands`, `files_*`, `browser_*`,
`code_interpreter`, `web_search`) exposed on `ctx.tools`; a tool call opts into the
sandbox simply by being one of these tools; per-tool timeouts; sandbox instance is
isolated per conversation id. Debug logging via `MAKERS_AGENT_TOOLKIT_DEBUG=1`.
- https://pages.edgeone.ai/document/sandbox-using-the-agent-framework

**Observed:** CONFIRMED. Asked the deployed agent to run `date -u && uname -a`; the
`commands` platform tool executed it in the sandbox and returned structured
`{stdout, stderr, exitCode}` in 871 ms. Sandbox is an isolated Linux microVM
(`Linux cubebox- 6.6.69-cube.pvm.guest...`). Mechanism: platform tools arrive on
`context.tools`, are exposed to the LLM as OpenAI function-calling tools, and any
call to one of them runs sandboxed — no explicit opt-in syntax beyond using the tool.
§7 evidence (tool_debug SSE event with result payload) captured.

## Q7 — Streaming/SSE

**Docs:** yes — gateway supports native SSE; the starter agent template streams
token-by-token SSE. **Polling remains our committed default regardless** (per TRACK-B).
- https://pages.edgeone.ai/document/models

**Observed:** CONFIRMED working (curl -N against prod /chat streamed `text_delta`
SSE events token-by-token). Polling remains the committed default; no scope change.

## Q8 — Session store (context.store)

**Docs:** conversation storage on `context.store`: `appendMessage` / `getMessages` /
`updateMessage` / conversation CRUD; message content ≤50 MB serialized; ≤10k messages
per conversation; conversation `metadata` field holds **arbitrary business JSON** —
documented for state like summaries/tags. Backed by Blob, cloud-persisted.
- https://pages.edgeone.ai/document/agents-conversation-storage

Fit for the paused `run_until_approval` snapshot: YES on paper — store the snapshot
JSON in conversation `metadata` (or a message) keyed by incident id. Native-first rule
satisfied without hand-rolling, pending observation.

**Observed:** CONFIRMED. Arbitrary state written via `store.put` in one invocation was
read back by a different invocation 49 ms later (see Q4). Conversation-message history
also survives across invocations (Q1 memory test). The paused `run_until_approval`
snapshot can persist either as a `store.put` value keyed by incident id or in
conversation metadata — both native, no hand-rolling.

## Q9 — Tracing / trace_id in-process

**Docs:** observability = console Metric Analysis + Log Analysis (per-function request
logs). **No documented in-process API to retrieve a trace/run id per agent/model/tool
call, and no documented per-trace URL shape.**
- https://pages.edgeone.ai/document/observability

Consequence (per TRACK-A A4): events stamp `trace_id: null` unless observation finds
an id (e.g. a request/conversation id surfaced on `context`). `Makers-Conversation-Id`
is a candidate correlator.

**Observed:** PARTIAL — the in-process half is CONFIRMED: `context.run_id` and
`context.conversation_id` exist in the agent runtime (template uses both), responses
carry a `Makers-Run-Id` header, and `context.tracer` supports manual spans
(`start_span`/`set_attributes`, OpenInference-style attrs). So events CAN stamp
`trace_id = run_id`. What remains console-only: whether a per-trace URL exists that
`run_id` deep-links to (decides if the dashboard click-through renders a link or just
shows the id). → moved to the human gate; browser access was declined this session.

## Q10 — Gateway usage/latency metrics via API

**Docs:** metrics are console-surfaced; no public query API documented. Per TRACK-B:
not trivially available → **skip** (no eval-board footnote).

**Observed:** SKIPPED per plan — `GET /models` and metrics paths 404 on the gateway;
nothing trivially available. No eval-board footnote.

## Q11 — Outbound HTTPS from cloud functions (gates §6A SMS)

**Docs:** edge functions have Fetch API for remote requests; cloud functions run full
Node/Python runtimes (which implies arbitrary HTTPS), but **egress policy to arbitrary
hosts (api.twilio.com) is not explicitly documented**.

**Observed:** CONFIRMED — a Python cloud function fetched
`https://api.twilio.com/2010-04-01.json` directly: HTTP 200 in 433–499 ms. No egress
restriction encountered. §6A SMS channel is GATED OPEN.

---

## CLI surface (OBSERVED, 2026-07-03, edgeone CLI v1.6.11 installed locally)

`edgeone login | whoami | switch | logout` + `edgeone makers init | dev |
generate-routes | env | link | deploy [directoryOrZip] | create [project-name]`
(`pages` is a deprecated alias for `makers`). `create` scaffolds from a template —
that is the hello-world deploy path. Auth: DONE — `edgeone whoami` returns
ranjiv.jithendran@gmail.com. Observed deploy flow: `edgeone makers create hello-recon
--template python-starter-agent` scaffolds AND creates+links the console project;
`edgeone makers deploy` builds (Python functions bundled ~5 MB) and ships to prod in
~60–90 s end-to-end. Route mapping is directory-convention: `agents/<name>/index.py`
→ agent route `POST /<name>`; `cloud-functions/<name>/index.py` → `POST /<name>`
(`BaseHTTPRequestHandler` subclass named `handler`, context on `self.context`);
`_`-prefixed files are private (not routed).

---

## B1 bring-up addenda (observed 2026-07-03, decision-affecting)

1. **Storage requires an agents dir.** The builder injects `context.agent.store`
   into cloud functions ONLY when `agents/` exists ("Detected agents directory —
   context.agent.store will be injected"). Without it, puts/appends silently no-op.
   → `agents/ping/index.py` exists as deploy plumbing + storage healthcheck.
2. **Conversation ids: 6–36 chars, no ':'** — violations fail SILENTLY (append
   returns nothing, conversation stays empty). Event logs use
   `evt-<sha1(incident_id)[:16]>` (see `_storage._cid`). KV keys DO accept colons.
3. **`get_messages` caps `limit` at 100** (`MemoryValidationError` above 100).
   Fine for demo incidents (~25 events); pagination unproven.
4. **Agent-side store facade differs**: `context.store` in agents is
   ConversationMemory (append_message/get_messages — no put/get); the generic
   put/get facade exists only on `self.context.agent.store` in cloud functions.
5. **requirements.txt is the deploy manifest for BOTH bundles.** Function bundles
   hard-fail over ~200 MB (crewai alone → 461 MB → Deploy Failed); a per-dir
   cloud-functions/requirements.txt did NOT exclude root deps. Agent server
   images preinstall the heavy stack (builder excludes/purges "server-image
   packages"). → root requirements.txt is lean (httpx, python-dotenv); the full
   local-dev set moved to requirements-local.txt.
6. **.env values are baked into bundles at build time** (setdefault lines in the
   platform harness) — env changes need a redeploy, matching the docs.
7. **Project types matter:** the console-created project (rescueops-dpj9utykdvs3,
   git-connected) accepts CLI deployments ("Deploy Success") but never serves
   them — production tracks the GitHub repo. CLI-created upload projects
   (hello-recon) serve CLI deploys immediately. Deploying to the git project =
   push to GitHub main.
8. Stale local build cache can mask requirement changes — `rm -rf
   .edgeone/cloud-functions .edgeone/agent-python` forces a clean bundle.

## Deployment-shape proposal (to lock at the human gate) — updated from observation

- Incident runner = **agent runtime** (`async def handler(context)`), one conversation
  id per incident; pause = end invocation + snapshot via `context.store`
  (`store.put` keyed by incident id, or conversation metadata). OBSERVED working.
- `api/` = **cloud functions** (Python, `BaseHTTPRequestHandler` convention):
  `GET events?since=`, incidents, approval, chaos toggles, eval — all ≤120 s work.
  Storage reachable there via `self.context.agent.store`. OBSERVED working.
- Event log = **`context.agent.store`** (Blob-backed), NOT KV — KV has no cloud-function
  binding (Q4). One key per event via put/get, or one conversation per incident with
  `append_message`/`get_messages` as the append-log. 49 ms cross-invocation
  read-after-write observed. Native-first satisfied; hand-rolling avoided.
- Frontend = static `web/` on Pages, polling per ARCHITECTURE §4.
- Models = platform-injected gateway creds (`AI_GATEWAY_API_KEY`/`AI_GATEWAY_BASE_URL`
  — auto-present at runtime, zero setup); primary `@makers/deepseek-v4-flash`;
  fallback `@makers/minimax-m2.7` (free, OBSERVED 200, no key binding needed);
  optional upgrade: bind Anthropic key in console → `anthropic/<model-id>`.
  Failover stays in-code in `llm_client.py` (Q3: no native fallback exists).
- trace_id on events = `context.run_id` (agent runtime), null elsewhere; whether it
  deep-links to a console trace URL is the remaining human-gate check (Q9).

## `.env.template` updates implied (shared file — needs Track A ack)

- `LLM_BASE_URL=` default to the platform-injected `AI_GATEWAY_BASE_URL` (llm_client
  reads the injected names; local dev fills them in .env manually).
- `MAKERS_MODELS_KEY=` local-dev only; in prod the injected `AI_GATEWAY_API_KEY` is used.
- `LLM_PRIMARY_MODEL=@makers/deepseek-v4-flash` (unchanged).
- `LLM_FALLBACK_MODEL=@makers/minimax-m2.7` (works today; swap to `anthropic/<model>`
  if/when the key is bound at the console).
