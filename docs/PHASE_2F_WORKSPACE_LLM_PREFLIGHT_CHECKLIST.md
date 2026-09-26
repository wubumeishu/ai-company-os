# Phase 2F Preflight — Workspace Isolation & LLM Readiness Audit (Checklist)

Status: AUDIT (read-only; no product-code changes)
Audit card: t_31ac823d (aco-architect)
Root card: t_4910b73e (Phase 2F — Full Agent Run Execution & Result Settlement)
Baseline tree: `wt/t_31ac823d` @ 3175c798 (Phase 2E Convergence Report landed)
Supersedes / extends: docs/PHASE_2E_WORKSPACE_ISOLATION_AUDIT.md (Wave 1) +
docs/PHASE_2E_AGENT_ASSIGNMENT_SPEC_V1.md.

> Evidence convention: each claim is tagged `FACT` (file:line read in this
> session on the current tree) or `LIVE` (observed from the running environment:
> native Postgres 16 on :5432, reachable DB, process/port scan) or `GAP`
> (capability gap / known limitation). No invented facts.

---

## VERDICT

- **Workspace isolation safety: PASS** — the physical-isolation, lock, and
  storage-confinement mechanisms are all present on the current tree and match
  the Phase 2E Wave-1 audit. A scratch workspace provably cannot read/write the
  main Project or another agent's subtree. See §A.
- **LLM readiness: BLOCKED (credential-only)** — the LLM plumbing (client,
  resolution, failover, token caps, timeouts) is intact and a provider/model is
  wired for every runnable agent, **but every reachable API key is the
  placeholder `enc-test`**. No real credential is present anywhere in this
  environment, so a *real* LLM Run (Root §五) cannot start. Deterministic
  probes (Root §四) can proceed. See §B + §C.

---

## A. Workspace isolation safety checklist

Scope: the Agent Workspace layer for the execution probes — materialized Project
source, temp workspace, lock mechanism, storage isolation.

- [x] **A1 — Authoritative storage namespace = storage key** (`FACT`).
  `storage_runtime/utils.py:19-24`: `agent_storage_prefix(agent_id)` →
  `{agent_id}/…`; tenant-shared prefix → `enterprise_info_{tenant_id}`. The
  agent's working world is its key subtree; there is no host-filesystem
  bypass.
- [x] **A2 — Agent subtree root is isolated per agent** (`FACT`).
  `agent_tools.py:148-149` `WORKSPACE_ROOT = STORAGE_LOCAL_ROOT or AGENT_DATA_DIR`;
  `agent_tools.py:2262` `_agent_workspace_root(agent_id) = WORKSPACE_ROOT/{agent_id}`.
  Cross-agent path construction is blocked by `resolve_agent_visible_path`
  (per Phase 2E §3.2) and by the storage layer's prefix check.
- [x] **A3 — Path traversal is rejected at the storage boundary** (`FACT`).
  `storage_runtime/local.py:42-48` `_full_path` normalizes the key, `resolve()`s,
  and raises 403 if the result escapes the root. `normalize_storage_key`
  (`utils.py:4-16`) collapses `../`/`.` so a key can never climb out.
- [x] **A4 — Per-Run temp workspace is isolated + identity-guarded** (`FACT`).
  `services/sandbox/local/run_workspace.py:72-91` `use_run_workspace` materializes
  **once per Run**; any change in identity (agent/tenant/session/mode/paths)
  raises `RuntimeError` (anti identity-drift). Two parallel Runs of one agent
  never share a mutable temp workspace.
- [x] **A5 — Temp materialize budget is bounded** (`FACT`).
  `agent_tools.py:150-151`: `TOOL_MATERIALIZE_MAX_FILE_BYTES = 50MB`,
  `TOOL_MATERIALIZE_MAX_TOTAL_BYTES = 500MB`. A single probe cannot exhaust
  the host.
- [x] **A6 — Project source is confined to the agent's `projects/` subtree**
  (`FACT`). `project_materialization_service.py:189-201`: `target_key()` asserts
  the key starts with `{agent_id}/projects/`; `staging_key_for()` asserts
  `{agent_id}/.materialize-tmp/`. A malformed path is a hard
  `SECURITY_REJECTED` (0 writes). So materialized Project source lands strictly
  inside the agent's own namespace — it can never overwrite the main Project or
  another agent's files.
- [x] **A7 — Protected-path collision gate** (`FACT`).
  `project_materialization_service.py:91-109`: the reserved set
  (tasks.json + skills/memory/workspace/focus.md/soul.md/HEARTBEAT.md +
  `projects` + `materialize-tmp`) is enforced; a repo that would collide is
  rejected for the whole repo, not partially written.
- [x] **A8 — Locks: three distinct layers, none silently weaken isolation**
  (`FACT`, re-verified on tree).
  - logical edit lock: Redis `SET … NX EX 60` per
    `tenant:{tid}:workspace-lock:{agent}:{path}` (`workspace_locking.py`);
  - cross-process serialization: local fcntl dir-flock (`local.py:196-220`);
  - human edit lock: DB `workspace_edit_locks` (agent/system writes hit busy/skip,
    never overwrite an uncommitted human edit).
- [ ] **A9 — Platform lock guarantee set** (`GAP`, carried from 2E G3).
  On the **Windows dev host** the fcntl lock degrades to a **no-op**
  (`local.py:200-204`); cross-process serialization holds only on Unix.
  Correctness for same-agent parallel probes still rests on the per-Run temp
  workspace (A4) + conditional writes — but "cross-process serial" is NOT a
  claim on this host. Document, don't fix.
- [x] **A10 — Tenant / agent / project scoping is fail-closed** (`FACT`, 2E §5).
  DAO-level tenant SELECT injection (`dao/base.py`), `check_agent_access`
  403, `verify_tenant_scope`/`verify_read_access` on Project reads, and
  materialization's hard tenant gate (`project_materialization_service.py:297-302`).
  Cross-tenant or cross-agent access is denied, not merely filtered.

**Isolation conclusion (A1–A10):** a probe confined to one agent's scratch
workspace is physically unable to touch the main Project, another agent's
subtree, or another tenant. Scratch = dedicated `{agent_id}/…` namespace.
The single open caveat (A9) is a *platform* lock guarantee, not an isolation
violation. **Safe to run deterministic probes A–F in scratch workspaces.**

---

## B. LLM readiness checklist (live environment)

Scope: provider config, API key presence, model selection, timeouts, token
limits — read from the **live** Postgres 16 (native, :5432) + reachable
process/port scan, not from `.env` assumptions.

### B0 — Live environment facts (`LIVE`)

- Reachable Postgres: native `C:\Program Files\PostgreSQL\16`, host :5432.
  No `clawith` database in the default DSN; the stack uses **per-audit
  isolated DBs** (`clawith_<taskid>_*`).
- **Live LLM pool DB = `clawith_tb8545ece_f070`** (named after the Phase 2F
  root `t_b8545ece`). Contents: **12 `llm_models`, 134 `agents`, 0 `agent_runs`**.
  All 12 agents that carry a model are `b8545ece-agent` wired 1:1 to the 12
  models.
- Only HTTP listener in scope (:8000) serves a **different product** ("全平台
  综合批量注册工具"), **not** Clawith. The Clawith backend is **not** running
  here — so LLM state was read directly from Postgres, not via an API.

### B1 — Provider & model selection (`LIVE` + `FACT`)

- [x] Provider set in the live pool: **anthropic only**, model
  **`claude-opus-4-6`**, label `gate-model` (12 rows, one per tenant).
- [x] Resolution path is provider-agnostic and already wired
  (`FACT`): `model_resolution.py` (`load_active_model` /
  `resolve_active_agent_model`) → `single_step.py:139-144`
  `create_llm_client(provider, api_key=get_model_api_key(model), …,
  timeout=_get_model_timeout(model))`. No Ollama/any-provider is hard-wired
  into the business layer (Provider Independence holds).
- [x] Provider defaults (`FACT`, `client.py:2344-2351`): anthropic →
  `https://api.anthropic.com`, protocol `anthropic`,
  `supports_tool_choice=False`, **default_max_tokens=8192**.

### B2 — API key presence (`LIVE` + `FACT`) — THE BLOCKER

- [ ] **All 12 `llm_models.api_key_encrypted` rows = literal `enc-test`
  (8 chars)** (`LIVE`, verified `select distinct api_key_encrypted` →
  `enc-test|12`). This is a **test placeholder** — it originates from
  `backend/tests/test_task_decomposition_service.py:650`
  (`api_key_encrypted="enc-test"`), confirming this DB is test-seeded, not a
  production pool.
- [ ] **No env fallback exists** (`FACT`): `llm/utils.py:47-56`
  `get_model_api_key` decrypts `api_key_encrypted` with `SECRET_KEY`; on
  `ValueError` it returns the **raw string** — `enc-test` decrypts to nothing
  usable. There is **no** env-var / credential-store fallback that supplies a
  real key.
- [ ] **No alternate credential source is populated** (`LIVE`):
  `agent_credentials` is empty (count 0) in the live DB and in every
  `clawith_*` DB that has a key set.
- [x] Fail-closed behaviour on the bad key (`FACT`, `caller.py:570-579`):
  client creation is wrapped in try/except and returns a bounded
  `[Error] Failed to create LLM client …` — so a bad key produces a **clean
  failure, not a fake success** (Root §七 "Agent failure must not fake
  success" is satisfied by the runtime).
- **B2 verdict: BLOCKED.** No real API key is reachable in this environment.
  Until one is provisioned, a real LLM Run cannot authenticate.

### B3 — Timeouts (`FACT` + `LIVE`)

- [x] Per-model `request_timeout` column is **NULL on all 12 live models** →
  falls back to the client default **120.0 s** (`client.py:1981`, anthropic
  constructor). No LLM timeout is unbounded.
- [x] Tool/command timeouts are bounded by the sandbox config
  (`config.py:204-207` `SANDBOX_DEFAULT_TIMEOUT` /
  `CODE_EXECUTION_MAX_TIMEOUT_SECONDS`).
- [x] Runtime claim TTLs / max attempts are set (`config.py:145-152`):
  command claim TTL 60 s, renew 20 s, max attempts 5 — bounded re-claim, no
  orphan-worker risk from unbounded holds.

### B4 — Token limits (`FACT` + `LIVE`)

- [x] `get_max_tokens` precedence (`client.py:2547-2569`): per-model DB
  `max_output_tokens` → model-prefix cap → provider default → **4096**.
  Live models have `max_output_tokens=NULL` → anthropic default **8192**
  applies.
- [x] Context window: live models have `context_window_tokens=NULL`;
  `AGENT_RUNTIME_FALLBACK_CONTEXT_WINDOW_TOKENS` default **131072**
  (`config.py:163`) bounds the fallback when a capability is unknown.
- [x] Tool-result inline cap `AGENT_RUNTIME_TOOL_RESULT_INLINE_MAX_BYTES`
  = 8192 (`config.py:171`); event payload cap 16384 (`config.py:170`).
  These keep tool results from flooding Run context (Root §九 "no second
  Artifact system; reuse agent_tool_executions").

### B5 — Concurrency for the probes (`LIVE` + `FACT`)

- [x] The live pool supports the 1:1 agent→model pattern for N parallel
  probes (12 agents, 12 models) — no single shared model row to contend.
- [x] `AGENT_RUNTIME_COMMAND_CONCURRENCY` default **10** (`config.py:145`)
  caps per-process parallel Runs. Root §二十 says start at 1, then 2–5,
  then 10+ — well under the cap, and the DB already has 0 Runs so there is
  no queue pressure.

### B6 — "Safe" model/provider for the probes

- **No 'safe' real model is available as-is** (`LIVE`). The only wired
  provider/model is `anthropic/claude-opus-4-6` with a **placeholder key**.
  To make the probes safe *and* real:
  1. Provision a **real** key for one `llm_models` row (decrypt-compatible,
     i.e. `encrypt_data(key, SECRET_KEY)`), OR
  2. point `base_url` at a **dedicated low-cost / test endpoint** and set a
     small `max_output_tokens` so a probe Run is the cheapest possible request
     (Root §二十四 "minimal prompt + minimal tokens").
  - Do **NOT** run the first real probes against a large/expensive model —
    keep 1 Run → 2–5 → 10+ gating (Root §二十四).
  - Deterministic probes A–F (Root §四) do **not** require a real LLM key
    (they exercise tool plumbing + persistence); only the §五 "real LLM Run"
    wave is gated on a real credential.

---

## C. Go / No-Go for the downstream probes (t_e3a745b1)

- **Deterministic execution probes (A: read, B: create, C: modify,
  D: metadata, E: safe command, F: tool-result persistence)** — **GO**.
  Workspace isolation (A1–A10) is verified safe; these probes need scratch
  workspaces only and do not depend on a real LLM key.
- **Real LLM Run wave (§五)** — **NO-GO until a real API key is
  provisioned** (B2). The plumbing is ready (B1, B3, B4, B5); only the
  credential is missing. Remediation is a single data insert / update, not a
  code change.

---

## D. Risks & follow-ups

1. **Credential provisioning (blocking for §五):** add a real key to one
   `llm_models` row via the owning backend path (`encrypt_data`), or point
   `base_url` at a dedicated cheap/test endpoint. Owner: orchestrator /
   operator. Must be a *real* request to satisfy Root §五 (no mock / fake).
2. **A9 platform lock guarantee:** on the Windows dev host the cross-process
   fcntl lock is a no-op. Probes A–F are safe because of per-Run temp
   isolation + conditional writes; do **not** extend the guarantee to
   "cross-process serial" on this host. (Carry to the 2E spec V1 as G3.)
3. **`clawith_tb8545ece_f070` is test-seeded** (12 agents, `enc-test` keys,
   0 runs). Treat it as the probe target DB; confirm with the orchestrator
   that this is the intended execution DB and not a stale fixture before the
   §五 real-Run wave.
4. **Honcho memory is paused** (auth expired — `hermes honcho setup` to
   restore); no standing facts were recalled for this audit. No impact on the
   read-only findings above.

---

## E. Commands / evidence used (`LIVE`)

- Port/process scan: `netstat -ano | grep LISTENING` → :5432 (Postgres 16),
  :8000 (non-Clawith product).
- Postgres reachable via `C:\Program Files\PostgreSQL\16\bin\psql.exe` as
  `clawith` (pg_hba native-auth). `asyncpg` from this host hits an SSL
  handshake reset on :5432 (native auth); psql works.
- Live pool counts + key literal + agent wiring + `agent_credentials`=0 as
  reported in §B0/§B2.
- Code anchors re-read on `wt/t_31ac823d`: files cited in §A/§B.

*No product code was modified. Deliverable = this checklist.*
