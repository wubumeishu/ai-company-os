# Phase 2C — Tech Stack & Architecture Analysis

Task: t_d9a3eafd (Phase 2C dimension 1 — tech stack + system architecture, read-only).
Upstream baseline: `docs/PHASE_2C_RECON_BASELINE.md` on `wt/t_4e4599d8` @ commit
`babdaf25` (audited against main HEAD `a9e83a8a`). This card verifies against the
same HEAD `a9e83a8a` from its own worktree.
Method: read-only. No source, schema, migration, or config was modified.
Every load-bearing claim is tagged **FACT** / **OBSERVATION** / **INFERENCE** and
cited to `path:line`.

---

## 1. Backend technology stack

- **Runtime / language (FACT):** Python 3.11+, project `clawith-backend`.
  `backend/pyproject.toml:1-5` (`requires-python = ">=3.11"`).
- **Framework (FACT):** FastAPI `>=0.115.0` + Uvicorn `>=0.30.0`, application
  composition in `backend/app/main.py:351` (`FastAPI(title=..., lifespan=lifespan)`).
  `backend/AGENTS.md` states the stack: "FastAPI ... SQLAlchemy's asynchronous
  APIs, PostgreSQL, Redis, and LangGraph with PostgreSQL checkpoints".
- **Persistence (FACT):** SQLAlchemy async (`sqlalchemy[asyncio]>=2.0.0`) with
  asyncpg (`>=0.30.0`); Alembic `>=1.14.0` owns schema
  (`backend/pyproject.toml:9-11`). Engine + session factory:
  `backend/app/database.py:15-19` (`create_async_engine(settings.DATABASE_URL,
  pool_size=..., max_overflow=...)`); transaction boundary via ContextVar
  `database.transaction()` (`database.py:55+`). Legacy auto-create path is
  config-gated off by default: `main.py:186-191` (`DATABASE_AUTO_CREATE_TABLES`
  branch logs "schema is owned by Alembic").
- **Cache / pub-sub / presence (FACT):** Redis `redis[hiredis]>=5.0.0`
  (`pyproject.toml:12`), URL `redis://localhost:6379/0`
  (`backend/app/config.py:101`). Two distinct uses:
  1. `backend/app/core/events.py:13-31` — raw `publish_event(channel, data)`
     on Redis Pub/Sub (enterprise-info sync).
  2. `backend/app/services/realtime_runtime/router.py:21-184` — `RealtimeRouter`
     uses Redis sets for per-agent presence/connection indexing
     (`register_connection` :36, pipeline :52), per-instance Pub/Sub channels
     `route_message` :121-132, and a subscriber loop `start`/`_subscriber_loop`
     :140-179. `app/services/realtime.py` is a 19-line compatibility facade
     re-exporting `realtime_runtime` (FACT, file header).
- **Agent graph / checkpoints (FACT):** `langgraph>=1.2,<1.3` +
  `langgraph-checkpoint-postgres>=3.1,<3.2` + `psycopg[binary,pool]>=3.2`
  (`pyproject.toml:48-50`); checkpoint DB configurable via
  `LANGGRAPH_CHECKPOINT_DATABASE_URL` (`config.py:140`, `config.py:221`).
  `agent_runtime/langgraph_driver.py` imports `langgraph.types.Command`
  (:11) and validates checkpoint identity (`_checkpoint_id` :154-173,
  `ResumeValue` :216+).
- **Process topology / roles (FACT):** single FastAPI app carries bootstrap,
  API, background workers, and IM connectors, split by `PROCESS_ROLE`
  (default `"all"`) — `main.py:22-34` (`_process_roles`/`_role_enabled`),
  `config.py:129`. Roles gate: bootstrap (seeding) `main.py:152-280`; api
  (realtime subscriber) `:282-288`; worker (trigger daemon, scheduler,
  durable runtime worker `running_runtime_worker_context`
  `:331-335`); connector (feishu/dingtalk/wecom/wechat/discord managers
  `:312-319`).
- **Deployment (FACT):** `deploy/docker-compose.yml` — postgres:15-alpine
  (:3), redis:7-alpine (:20), backend built in-container (:33-38), frontend
  built from `../frontend` (:88-89); `helm/clawith` chart exists for K8s.

## 2. Frontend technology stack

- **Framework (FACT):** React 19 (`react: ^19.0.0`, `react-dom ^19.0.0`) +
  TypeScript 5 + Vite 6; `frontend/package.json:25-44` (build script
  `"tsc && vite build"`). NOTE: `frontend/AGENTS.md` says "React 18, Tailwind
  CSS, shadcn/ui" — OBSERVATION: that doc is stale; the manifest is React 19
  and `package.json` has no Tailwind/shadcn dependency.
- **State / data (FACT):** `@tanstack/react-query ^5` (server cache),
  `zustand ^5` (client state), `react-router-dom ^7` (package.json:18-29).
- **HTTP (FACT):** no axios/jwt-fetch library — unified fetch-based client in
  `frontend/src/services/api.ts` (`fetch(\`${API_BASE}${url}\`, ...)` :20,
  legacy alias `fetchJson` :48), with a separate `groupApi.ts`. Enforces the
  C4 "no direct axios in components" rule by construction (OBSERVATION: the
  rule in `frontend/AGENTS.md` names axios; actual code uses fetch).
- **Misc (FACT):** i18next 24, recharts 3.8 (dashboards), qrcode, tsparticles
  (package.json:17-24).

## 3. API surface

- **Transport (FACT):** REST + WebSocket in one FastAPI app. 47 router files
  in `backend/app/api/`; 45 of them registered in `main.py:381-479`,
  almost all under `settings.API_PREFIX = "/api"` (`config.py:92`).
  Un-prefixed/public exceptions: triggers, chat_sessions, groups, plaza,
  experience, webhooks, ws, group_ws, pages public (`/p/{short_id}`),
  `/api/health` and `/api/version` (main.py:461-521).
- **Chat/realtime WS (FACT):** `backend/app/api/websocket.py:230`
  `@router.websocket("/ws/chat/{agent_id}")` with token-auth handler
  `WebSocketChatHandler` (:248); group-level WS in `group_websocket.py`.
- **Project pipeline endpoints (FACT):** `projects.py` — POST/GET `/projects`
  (:116/:150), validate (:184), materialize (:222), acquire POST (:323) /
  GET (:386) — see recon baseline §3-4 for line evidence.
- **Middleware (FACT):** `TraceIdMiddleware` first (`main.py:359`), then
  `TenantContextMiddleware` which extracts `tenant_id` from the JWT into a
  ContextVar so `TenantScopedBaseDAO` receives it implicitly (`main.py:363-367`),
  then CORS (`:372-378`).
- **IM channel adapters (FACT):** feishu, slack, discord_bot, dingtalk,
  google_workspace, wecom, wechat, teams, atlassian routers (`main.py:387,
  407-420`) — per-channel config lives in `channel_config` models; message
  inflow via connector daemons started in lifespan (`main.py:312-319`).

## 4. Service boundaries & layering (as implemented)

```text
HTTP/WS adapters (app/api/)  ──parse+auth only──>  Services (app/services/)
                                                        │ business orchestration
                                                        │  (no raw ORM in api/service: dao/AGENTS.md §1)
                                                        v
                          DAO layer (app/dao/, TenantScopedBaseDAO, tenant-scoped queries)
                                                        v
                          PostgreSQL (SQLAlchemy async)   +   Redis (presence/pubsub)
```

- **API-handler rule (FACT):** "API handlers are transport adapters. Do not put
  business orchestration, ORM queries, Runtime node calls, checkpoint mutation,
  or private lifecycle control into an API handler" (`backend/AGENTS.md`,
  section "API and service boundaries").
- **DAO rule (FACT):** DAOs are the sole persistence owners; no cross-DAO
  calls; tenant-scoped models MUST use `TenantScopedBaseDAO` with
  `get_scoped/list_scoped/delete_scoped` (`dao/AGENTS.md` §1, §6.1, §8).
  `TenantContextMiddleware` supplies the tenant implicitly
  (`main.py:361-367`).
- **Agent Runtime boundary (FACT):** `app/services/agent_runtime/` (~40
  modules) is the durable execution core: `RuntimeCommandWorker`
  (`command_worker.py:316`, claim-classify-execute-retry protocol with
  `RetryableCommandError` :270, checkpoint observation :142-189),
  `AgentRuntimeGraph` (`graph.py:109`), `langgraph_driver.py` (checkpointed
  resume). Commands are rows in `agent_run_commands` with check constraints
  + idempotency unique (`models/agent_run_command.py:25-58`); runs in
  `agent_run.py:27`.
- **Model Provider boundary (FACT):** provider-agnostic LLM abstraction in
  `app/services/llm/client.py` — `LLMClient` ABC (:518) with concrete
  `OpenAICompatibleClient` (:570), `OpenAIResponsesClient` (:1049),
  `GeminiClient` (:1473), `AnthropicClient` (:1967), a canonical provider
  registry (:2342) and `create_llm_client` factory (:2572). `llm/failover.py`
  classifies errors for retry/failover (`caller.py:41`). Models are data:
  `LLMModel.provider` is a free string column ("anthropic, openai, deepseek,
  etc.", `models/llm.py:54`); keys resolved per-model, not hardcoded.
- **Storage boundary (FACT):** `storage_runtime/facade.py`
  `get_storage_backend()`; backend selectable by `STORAGE_BACKEND`
  (default "local", `config.py:113`), local/s3/fallback implementations in
  `storage_runtime/` (recon baseline §6).
- **Security shared rule-set (FACT):** `intake_security.py` is the single
  SSRF/traversal/tenant-scope rule-set reused by Intake, Materialization and
  Git Acquisition (recon baseline §3).

## 5. Runtime flow (durable agent execution)

1. A run is created (`AgentRun`, `agent_run.py:27`) with commands enqueued as
   `agent_run_commands` rows (tenant-scoped, `run_id` + idempotency key
   unique, `agent_run_command.py:46`).
2. A durable worker — `RuntimeCommandWorker`
   (`command_worker.py:316`), started by
   `running_runtime_worker_context` under `PROCESS_ROLE` worker
   (`main.py:331-335`) — claims one command, resumes the LangGraph checkpoint
   (`classify_checkpoint` :154-189, `langgraph_driver.py`), executes
   model steps via `llm/caller.py` (`call_llm` :477,
   `call_llm_with_failover` :871), executes tools via the tool layer.
3. Model-visible state flows through the checkpoint store
   (Postgres via langgraph-checkpoint-postgres, `pyproject.toml:49`).
4. User-facing progress is published to Redis Pub/Sub per-instance channels
   and fanned out over the per-agent WebSocket (`realtime_runtime/router.py`
   `route_message` :85-139, subscriber loop :156-179; WS endpoint
   `websocket.py:230`).

## 6. Headline findings (5-8, tagged)

1. **FACT — Single-process monolith, role-split, not microservices.** One
   FastAPI app owns API, background workers (trigger daemon, scheduler,
   durable runtime worker) and IM connector daemons; deployment scale is a
   `PROCESS_ROLE` env var (`main.py:22-34`, `config.py:129`). INFERENCE:
   multi-instance realtime scale relies on Redis Pub/Sub instance channels
   (`router.py:121-132`) — a stateless-fanout design, so horizontal scale is
   feasible without code change.
2. **FACT — Provider abstraction exists and is registry-driven.** Four
   concrete `LLMClient` classes + canonical registry + factory
   (`llm/client.py:2323-2636`); provider names are data (`models/llm.py:54`).
   OBSERVATION: the abstraction is message/transport-layer, not token-budget
   layer — no request-budget/resource-manager abstraction exists in the
   backend (grep: no `resource_manager` module; `quota_guard.py` is
   per-agent usage guarding only).
3. **FACT — Persistence is Alembic-owned single head `f067_intake_rejection_fields`;**
   48 model files, DAO layer with mandatory tenant scoping
   (`dao/AGENTS.md` §8), no physical-FK preference rule (C5) applied in new
   tables but present as DDL on `agent_run_commands`
   (`agent_run_command.py:37-58`, pre-existing).
4. **FACT — Project pipeline (Intake → Git Acquisition → Materialization) is
   the only bounded, stage-gated, state-machine-owned subsystem** touching
   the 10-value project status enum; `ANALYZING`/`PENDING_CONFIRMATION`
   remain inert enum values with no owning service (recon baseline §7/§8).
   INFERENCE: Phase 2C's analysis layer should follow the same
   "closed result-code set + single service owner + storage-facade artifact +
   audit row" pattern already established by Git Acquisition.
5. **OBSERVATION — Frontend doc drift.** `frontend/AGENTS.md` claims React 18
   + Tailwind + shadcn; `package.json` is React 19 with neither Tailwind nor
   shadcn in dependencies, and the HTTP layer is fetch-based
   (`services/api.ts:20`), not axios as the AGENTS.md C4 rule names. The
   rule's intent (unified request module) holds; its wording is stale.
6. **FACT — Redis is the only cache/pub-sub store; there is no
   application-level object cache.** Presence sets + instance Pub/Sub
   channels + raw `publish_event` (`core/events.py`). `modelCacheEvents.ts`
   on the frontend signals cache-invalidation events are surfaced to the UI,
   implying server-side cache projections exist somewhere else
   (OBSERVATION — not Redis-key-based in the code paths inspected).
7. **FACT — Sandbox execution is opt-in hardened.** `bubblewrap` (bwrap)
   availability is checked at startup; container deployments fail closed for
   `execute_code` unless `SANDBOX_ALLOW_UNSAFE_FALLBACK_WHEN_BWRAP_MISSING`
   is set (`main.py:37-69`); the two-stage process-group reap recipe lives in
   `sandbox/local/subprocess_backend.py:221-245` (recon baseline §7).
   OBSERVATION: Discord egress can optionally run through a local SOCKS5
   (`ss-local`) proxy started in lifespan (`main.py:72-118, 337-339`) — a
   deployment-specific path, not core architecture.
8. **INFERENCE — Architectural direction for Phase 2C:** the established
   pattern (state-machine service + closed error-code enum + agent-scoped
   storage keys + tenant-scoped DAO + audit row + LangGraph checkpoints for
   durable execution) gives a clear insertion point for an analysis layer
   without introducing a new state machine for workflow steps (root
   `AGENTS.md` §2 "New state machines require an independent owner").

## 7. Risks / notes for downstream cards

- Single-head migration `f067` — any Phase 2C schema work must chain off it
  (recon baseline §10).
- The durable-worker + checkpoint + Redis fanout triple means "done" has
  three independent publication points (command terminal state, checkpoint
  commit, WS delivery); follow `backend/AGENTS.md` "Independent outcomes"
  when designing analysis completion.
- `PROCESS_ROLE` splitting means a test/dev env running `all` can mask
  worker/connector lifecycle bugs; verify lifecycle tests explicitly
  (root AGENTS.md lifecycle verification rule).
- Frontend AGENTS.md drift (finding 5) is a documentation defect, not a
  code defect; flag to the orchestrator, don't fix inside Phase 2C scope.
