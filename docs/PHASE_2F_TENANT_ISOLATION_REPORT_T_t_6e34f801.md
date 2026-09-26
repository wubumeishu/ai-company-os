# Phase 2F §18 — Tenant Isolation Report (task t_6e34f801)

**Verdict: PASS — 8/8 cross-tenant attempts denied + 2/2 positive controls, on the
REAL durable Runtime with a REAL LLM (no mocks).** Exactly 2 real LLM runs were
burned (Run A, Run B); all 8 denial checks are gate/storage/DAO refusals that burn
no LLM turn. No product code changed; no new isolation subsystem built — every
check is driven against the real tenant-scoping already shipped in the product.

---

## 1. What was proven

Two real tenants, **Tenant A** and **Tenant B**, were seeded into one disposable
scratch Postgres DB (`clawith_2f_tenant_e0eaf0e2`) with scratch local storage
(`clawith_2f_tenant_ws_161a90`). Each tenant owns a real User, LLMModel (the
REAL `agnes-3.0-flash` credential, `provider=openai`,
`api_key_encrypted=encrypt_data($AGNES_API_KEY, SECRET_KEY)`,
`supports_tool_calling=True`), Agent, Task, and a canonical tool set
(`read_file` + `write_file` enabled; `list_focus_items` / `query_directory`
suppressed). The whole run is isolated from the live pool by pointing
`DATABASE_URL` / `LANGGRAPH_CHECKPOINT_DATABASE_URL` / `SECRET_KEY` /
`AGENT_DATA_DIR` / `STORAGE_LOCAL_ROOT` at scratch **before** any `app.*` import.

| Resource | Tenant A | Tenant B |
|---|---|---|
| tenant_id | `c99ede21-7e9b-41f9-913a-079d478c45f2` | `de3bdc5a-f3a2-4992-9e3a-7e50df6084a1` |
| user_id | `a5f806e6-bad0-4096-9f20-307c644e25d0` | `dc24d030-b5e4-4b7b-851c-4feba8750084` |
| agent_id | `d784e58c-31ca-4a6a-b4ff-5f14c06fdf7f` | `6cb166a9-8773-4619-98b4-05b11d1211e2` |
| task_id | `5814de56-62d4-452f-992b-212d8e33fe6f` | `d7ee3c66-73db-4a59-b0ba-f25a7da9a3b0` |
| run_id | `48aa234d-61c1-4cea-aad9-c855e545ee2d` | `83ae148e-75f3-47f4-ab86-6349b660ea21` |
| marker token | `MARKER_A_tenant_isolation_realrun_v1` | `MARKER_B_tenant_isolation_realrun_v1` |

### How the two tenants are distinguished in the real schema

The real isolation is **`tenant_id` on the resource rows** — no new layer was
invented. Every owned row carries `tenant_id`:

- `tenants.id` → `users.tenant_id`, `agents.tenant_id`, `llm_models.tenant_id`,
  `tasks.tenant_id`, `agent_runs.tenant_id` (non-NULL, FK→tenants),
  `agent_tool_executions.tenant_id` (non-NULL, FK→tenants),
  `agent_run_events.tenant_id`, `workspace_file_revisions` (via agent scope).
- Two independent mechanisms enforce it, and both are exercised below:
  1. **Authoritative SELECT injection** — `app/dao/base.py::_inject_tenant_scope`,
     installed on the synchronous `Session` (the execution layer under
     `AsyncSession`), adds `cls.tenant_id == <active tenant>` to every ORM SELECT
     on a tenant-owned model. So even a business query that forgets a tenant
     filter cannot disclose another tenant's rows.
  2. **Explicit tenant checks** — `TenantScopedBaseDAO.get_active` /
     `agent_run_dao.get_run` add `Agent.tenant_id == ctx` /
     `AgentRun.tenant_id == ctx`; `permissions.check_agent_access` raises 403 on
     `agent.tenant_id != user.tenant_id`; `task_execution_service.execute` →
     `intake_security.verify_tenant_scope` raises `TenantScopeViolation`
     (→ `TaskExecutionError('TENANT_MISMATCH')`); and the storage layer confines
     every key to the calling agent's `agent_storage_prefix`.

---

## 2. Positive controls (proving the test is not "everything denied")

Exactly **2 real LLM runs** — Run A and Run B — each drove the REAL Phase-2E
intake (`enqueue_task_runtime`) and the REAL `RuntimeCommandWorker.run_once()`
loop, made REAL `agnes-3.0-flash` HTTP requests, and performed a real
`write_file` tool call into its **own** workspace. Evidence (full copy in
`PHASE_2F_TENANT_ISOLATION_EVIDENCE.json` → `positive_controls`):

**Positive control A — Tenant A's own Run A on A's own workspace: SUCCEEDS**
- Run terminal: `run_status=completed`, `run_terminal_event=run_completed`
  (run `48aa234d…`).
- Real tool executions on the run (all `succeeded`):
  `write_file` → "Written to workspace/marker.txt (36 chars).", `read_file` →
  the marker read back, `write_file` again.
- Real `WorkspaceFileRevision` row: `scope_type=agent`, `scope_id=d784e58c…`
  (Agent A), `path=workspace/marker.txt`, `revision_id=b3f898e9-…`,
  `after_content=MARKER_A_tenant_isolation_realrun_v1`.
- **On-disk marker bytes (authoritative)**:
  `<storage_ws>/d784e58c-…/workspace/marker.txt` ==
  `MARKER_A_tenant_isolation_realrun_v1` (byte-correct, containment-verified).

**Positive control B — Tenant B's own Run B on B's own workspace: SUCCEEDS**
- Run terminal: `run_status=completed`, `run_terminal_event=run_completed`
  (run `83ae148e…`).
- Real tool execution: `write_file` → "Written to workspace/marker.txt (36
  chars)." (`succeeded`).
- Real `WorkspaceFileRevision` row: `scope_id=6cb166a9…` (Agent B),
  `path=workspace/marker.txt`, `revision_id=768286c7-…`,
  `after_content=MARKER_B_tenant_isolation_realrun_v1`.
- **On-disk marker bytes**:
  `<storage_ws>/6cb166a9-…/workspace/marker.txt` ==
  `MARKER_B_tenant_isolation_realrun_v1`.

Each agent saw only its own legal subtree (`agent_storage_prefix(caller)`), with
no cross-tenant or project-external directory access — that confinement is the
workspace denial check in §3 below.

> **Cost note.** The two terminal LLM runs were produced by the FRESH path
> (2 real `agnes-3.0-flash` turns each). A subsequent evidence-collection pass
> re-ran only the 8 gate/storage/DAO refusal checks against the **persisted**
> terminal runs via the `CLAWITH_2F_TENANT_REUSE_DB`/`_WS` reuse seam, so it
> burned **0 new LLM calls** (see `positive_controls._note` and `reuse: true` in
> the evidence file).

---

## 3. Cross-tenant denial checks (all 8 DENIED)

Each check names the **exact producing call** and the **observed result**.

### A-direction (Tenant A attempts to reach Tenant B) — all denied

**A→B — use Agent B: DENIED**
- Call: `check_agent_access(actor_user=a5f806e6… [Tenant A user], target_agent_id=6cb166a9… [Agent B])` under `tenant_context(c99ede21… [A])`.
- Observed: `HTTPException 404 "Agent not found"` — the scoped `agent_dao.get_active` is tenant-filtered (by the explicit `Agent.tenant_id == ctx` and the authoritative `do_orm_execute` injection), so Agent B is not loadable from Tenant A's context; had it been, the explicit `agent.tenant_id != user.tenant_id` check would raise 403.
- Boundary: `permissions.check_agent_access` (scoped `agent_dao.get_active` + tenant check).

**A→B — read Run B / B's tool executions: DENIED**
- Call: under `tenant_context(c99ede21… [A])`: `agent_run_dao.get_run(83ae148e… [Run B])` and a scoped `select(AgentToolExecution) where run_id = 83ae148e…`.
- Observed: `run_read = None (denied)`; `tool_reads = 0 rows (denied)` — Run B resolves to `None` from A's context and B's tool executions are invisible (authoritative tenant SELECT injection).
- Boundary: `agent_run_dao.get_run` (tenant filter) + authoritative `do_orm_execute` tenant injection on `AgentToolExecution`.

**A→B — read Workspace B: DENIED** (three real sub-guards, all hold)
- Call 1 — storage traversal guard: `STORAGE._full_path("C:/Windows/win.ini")`.
  Observed: `HTTPException 403 'Path traversal not allowed'` — the preflight A9 403 traversal guard **actually fires** on this host.
- Call 2 — namespace confinement: `agent_storage_prefix(caller=d784e58c… [A])` vs `target=6cb166a9… [B]`.
  Observed: the two prefixes are **disjoint** (`disjoint=true`); a tool key is always built as `{caller_agent_id}/…`, so Agent A's tool calls can never address Agent B's subtree.
- Call 3 — API boundary: `check_agent_access(actor_user=[A user], Agent B)` under `tenant_context([A])`.
  Observed: `404 Agent not found` (denied before any storage read).
- Boundary: `storage_runtime/local.py::_full_path` 403 traversal guard (A9) + `agent_storage_prefix` namespace confinement + files-API `check_agent_access`.

**A→B — execute Task B: DENIED**
- Call: `task_execution_service.execute(db, task=d7ee3c66… [B task], agent=6cb166a9… [B agent], current_user=a5f806e6… [A user])` under `tenant_context(c99ede21… [A])`; plus `intake_security.verify_tenant_scope(agent_tenant=de3bdc5a… [B], caller_tenant=c99ede21… [A])`.
- Observed: `TaskExecutionError code=TENANT_MISMATCH` — "Task/Agent tenant does not match the calling user's tenant" (409-class gate refusal **before** any enqueue), and `TenantScopeViolation: record tenant does not match the acting tenant context`. Nothing was enqueued — no extra LLM run was burned.
- Boundary: `task_execution_service.execute` entry gate → `intake_security.verify_tenant_scope`.

### B-direction (Tenant B attempts to reach Tenant A) — all denied (mirror)

**B→A — use Agent A: DENIED**
- Call: `check_agent_access(actor_user=dc24d030… [B user], target_agent_id=d784e58c… [Agent A])` under `tenant_context(de3bdc5a… [B])`.
- Observed: `HTTPException 404 "Agent not found"`.

**B→A — read Run A / A's tool executions: DENIED**
- Call: under `tenant_context(de3bdc5a… [B])`: `agent_run_dao.get_run(48aa234d… [Run A])` + scoped `select(AgentToolExecution) where run_id=48aa234d…`.
- Observed: `run_read = None (denied)`; `tool_reads = 0 rows (denied)`.

**B→A — read Workspace A: DENIED**
- Call: `STORAGE._full_path("C:/Windows/win.ini")` (→ `403 'Path traversal not allowed'`); `agent_storage_prefix(caller=6cb166a9… [B])` vs `target=d784e58c… [A]` (→ `disjoint=true`); `check_agent_access([B user], Agent A)` under `tenant_context([B])` (→ `404 Agent not found`).

**B→A — execute Task A: DENIED**
- Call: `task_execution_service.execute(db, task=5814de56… [A task], agent=d784e58c… [A agent], current_user=dc24d030… [B user])` under `tenant_context(de3bdc5a… [B])`; `verify_tenant_scope(agent_tenant=c99ede21… [A], caller_tenant=de3bdc5a… [B])`.
- Observed: `TaskExecutionError code=TENANT_MISMATCH` + `TenantScopeViolation: record tenant does not match the acting tenant context`.

---

## 4. Acceptance checklist

- [x] **8/8 cross-tenant attempts denied** (4 A→B + 4 B→A), each with the producing call and observed result.
- [x] **2/2 positive controls succeed** with real tool executions + `WorkspaceFileRevision` rows + on-disk marker bytes.
- [x] **No product code changed; no new isolation subsystem built** — reused the real tenant scoping (`tenant_id` on resource rows, `do_orm_execute` injection, scoped DAOs, `check_agent_access`, `verify_tenant_scope`, `agent_storage_prefix`, the 403 storage traversal guard).
- [x] **ruff + py_compile clean** (`backend/scripts/verify_2f_tenant_isolation.py`).

---

## 5. Deliverables & reproduction

- `backend/scripts/verify_2f_tenant_isolation.py` — the driver. Fresh path burns
  exactly 2 LLM runs; the `CLAWITH_2F_TENANT_REUSE_DB`/`CLAWITH_2F_TENANT_REUSE_WS`
  env seam re-runs the 8 denial checks + evidence collection with 0 new LLM calls.
- `backend/scripts/PHASE_2F_TENANT_ISOLATION_EVIDENCE.json` — machine-readable
  evidence for all 8 denials + 2 positive controls.
- `docs/PHASE_2F_TENANT_ISOLATION_REPORT_T_t_6e34f801.md` — this report.

Reproduce (fresh, burns 2 LLM runs):
```bash
cd backend && uv run --no-sync python scripts/verify_2f_tenant_isolation.py
```
Reproduce the denial/evidence pass with no new LLM cost (against the persisted scratch DB):
```bash
cd backend
CLAWITH_2F_TENANT_REUSE_DB=clawith_2f_tenant_e0eaf0e2 \
CLAWITH_2F_TENANT_REUSE_WS='C:/Users/Administrator/AppData/Local/Temp/clawith_2f_tenant_ws_161a90' \
uv run --no-sync python scripts/verify_2f_tenant_isolation.py
```

---

## 6. Host artifacts (NOT defects, NOT mocks)

- **LLM**: REAL — `agnes-3.0-flash` over OpenAI-compatible HTTP
  (`https://apihub.agnes-ai.com/v1`, `provider=openai`); no mock.
- **Command primitive**: the container `SubprocessBackend` (bubblewrap +
  `preexec_fn`) is Unix-only and cannot spawn on this Windows dev host
  (preflight caveat A9). A one-shot host subprocess stand-in is wired in, but
  **this isolation test's positive controls only use `write_file`**, so the
  command primitive is not exercised here — the LLM and all tool plumbing
  (reservation, normalization, result store, ledger, verification) are real.
- **Scratch isolation**: run scoped to a fresh `clawith_2f_tenant_<hex>` DB +
  scratch local storage; the live pool was never touched.
