# Phase 2E — Agent Assignment & Execution Semantics — Technical Specification (V1)

Card: t_180370c7 (aco-architect) · Root: t_b0bb2f7c · Downstream: t_c2ed29df (builder), t_84836b4b
Baseline: `main @ 7497bf7a` (worktree `wt/t_180370c7`)
Evidence convention: every current-state claim is `FACT` (path:line, read in this worktree)
or `UNKNOWN` (cannot be decided from code — owner + ruling recorded in §11).
Sources: `docs/PHASE_2E_TASK_READINESS_AUDIT.md` (t_55f41487 @ afc0ae9f),
`docs/PHASE_2E_AGENT_RUN_QUEUE_AUDIT.md` (t_2ae698f7 @ a5e1decc),
`docs/PHASE_2E_WORKSPACE_ISOLATION_AUDIT.md` (t_29528ad2 @ 87b25680).

---

## 0. Goal, scope, non-goals

Goal: a ready Project Task is **bound to one real Agent** and **enters the existing
Clawith execution chain** (`enqueue_task_runtime` → `RuntimeCommandIntake.start_run` →
command queue → LangGraph → settlement), with dependency/tenant/workspace/duplicate
protection and a real audit trail. Task = "the company's arrangement of work";
Agent Run = "one execution of that work".

In scope (V1, minimum):

1. Explicit Task→Agent binding semantics (§4 — no new column).
2. Precondition gate, fail-closed, closed code set (§5).
3. Idempotency: duplicate Execute never yields two Runs; explicit retry after a
   failed terminal Run (§6).
4. State + failure mapping: no second parallel lifecycle (§7).
5. Tenant hardening of the intake/trigger path (§9).
6. Audit at the intake boundary (§8).
7. Two API surfaces: Execute + query execution (§10).

Non-goals (root card + audits):

- NO new Agent Runtime, worker, LangGraph node, verification, or settlement code —
  Phase 2E only assembles the **input** and the **gate** (§3 of root).
- NO Project-level single writer (workspace audit G4; root §十一): distinct agents
  are physically isolated (storage key namespace `{agent_id}/…` +
  `resolve_agent_visible_path`, FACT workspace audit §3.2); same-agent concurrency
  is handled by per-Run temp workspaces + conditional writes + Redis fast-fail —
  V1 adds **no** queuing/waiting facility.
- NO `Agent.status`-based availability gate, NO per-agent lane serialization by
  default, NO downstream ready-push, NO Project aggregate state machine,
  NO automatic agent (re)selection beyond explicit binding — all UNKNOWN rulings
  in §11, each with its deferral owner.

---

## 1. Data model changes

### 1.1 Agent binding — RULING: reuse `Task.agent_id`, add NO new column

- `FACT` `Task.agent_id` is already a **NOT NULL** FK to `agents.id`
  (`backend/app/models/task.py:50`); the legacy `Task.assignee` string
  (`task.py:68`, "self" or user_id) is unused by graph/provenance/gate lanes
  (task readiness audit §5.2).
- `FACT` the whole chain already consumes `Task.agent_id`:
  `enqueue_task_runtime` asserts `task.agent_id == agent.id`
  (`task_executor.py:66-70`); Run registry stores `agent_id`
  (`agent_run.py:89`); settlement re-checks `task.agent_id == agent_id`
  (`task_completion.py:129-133`).
- **Ruling (resolves U-4)**: V1 defines **assignment ≡ the `Task.agent_id` FK**.
  Do NOT add `explicit_agent_id`: a nullable parallel field would create a second
  "who does this" notion next to `agent_id` and `assignee` (task readiness audit
  §6.2 explicitly warns against inventing a third). The Agent is bound at Task
  authoring time; "reassign" is **post-V1** (§11 U-K).
- No DDL: `f069`/`f070` already ship the graph + provenance columns. **V1 adds zero
  migrations** (root §二十五: first judge whether the Phase 2D model is sufficient —
  it is, for V1). The builder lane (t_c2ed29df) must not open a migration; if
  review discovers a genuine schema need, it comes back to this lane with an ADR.

### 1.2 `Task.status` — keep the closed 3-value enum, add guards

- `FACT` `task_status_enum` = `pending | doing | done` (`task.py:58-61`); f069
  explicitly does not extend it (task readiness audit §1.1).
- **Ruling (resolves U-6)**: `done` is **terminal** for `todo` tasks.
  - The only programmatic writers today: `enqueue_task_runtime` sets `doing`
    (`task_executor.py:127`); `TaskRuntimeCompletionHandler` sets `done` on
    completed todo / `pending` on failed-or-cancelled (`task_completion.py:137-154`).
  - The manual PATCH path writes `status` **unrestricted** today
    (`api/tasks.py:237-238`, `TaskUpdate.status: str | None`,
    `schemas.py:405`). V1 guard: **PATCH may write `status` only to `pending`**
    (a human reset of a failed/blocked task back to a queueable state).
    Writing `doing`/`done` via PATCH → 409 `TASK_STATUS_WRITE_FORBIDDEN`
    (those values are owned by the Runtime completion handler; root §十二: no
    second lifecycle, no human override of terminal state).
  - **Re-trigger of a `done` todo is rejected** at Execute with 409
    `TASK_TERMINAL` (fixes audit §5.3 I-2: today a re-trigger would re-open
    `done → doing` via `task_executor.py:127` because the trigger path has no
    status guard, `api/tasks.py:274-296`). A genuinely new piece of work = a new
    Task row; re-running a finished Task is not an implicit feature.
- `doing` semantics stay "a Run for this Task is registered/in flight"; it never
  encodes QUEUED vs RUNNING — those are derived (§7.1).

---

## 2. The execution chain — reuse, do not rebuild

`FACT` the existing, tested chain (agent/queue audit §3, re-verified against this
worktree):

```text
POST /agents/{agent_id}/tasks/{task_id}/execute        ← V1 new (transport only)
  └─ TaskExecutionService.execute (the V1 gate, §5)     ← V1 new (owning service)
       └─ enqueue_task_runtime(db, task, agent,          task_executor.py:44 (EXISTING)
              execution_id=<attempt key>, actor=caller)
            · type gate {todo}                          task_executor.py:54-58
            · decide_runtime_v2 fail-closed             task_executor.py:59-65
            · agent identity/tenant/model checks        task_executor.py:66-81
            · Phase 2D ensure_ready dependency gate     task_executor.py:96-100
            · RuntimeCommandIntake.start_run            adapter.py:245 (EXISTING)
                 · source-retry lookup + exact-input check  adapter.py:248 / persistence.py:297-318
                 · register_run_with_start               persistence.py:321 (EXISTING)
                     AgentRun + AgentRunCommand(pending) + run_created event
            · task.status = "doing"                    task_executor.py:127
       → command queue (claim/lease/retry: 60s TTL, 20s renew, 5 attempts →
         quarantine — persistence.py:635; config.py:145-148)          (EXISTING)
       → LangGraph driver + verification + checkpoint                     (EXISTING)
       → TaskRuntimeCompletionHandler settlement (task flip + one receipt
         TaskLog per checkpoint, uuid5 receipt key)       task_completion.py:56+ (EXISTING)
```

V1 adds exactly two new units: the **gate** (`TaskExecutionService`, §5) and the
**Execute transport** (§10). Everything from `enqueue_task_runtime` downward is
untouched — the builder's contract is to call it, not to modify it
(t_c2ed29df: "Do not modify the LangGraph or worker internals; only provide the
input payload").

---

## 3. Assignment semantics (root §四)

- **Employee ≠ Agent.** The link is the executing `Agent` row, never an
  organizational Employee record; V1 introduces no mapping between them.
- One Task binds exactly one Agent (NOT NULL FK, §1.1). Two Tasks may bind two
  different Agents; that is how parallel independent execution works
  (root §十 情形 1 — physically isolated, workspace audit §4.1).
- Two Tasks binding the SAME Agent may run concurrently (V1 default, §11 U-B);
  their file-level conflict surface is fast-fail + bounded retry, never a
  silent overwrite (workspace audit §4.2-4.3) and never an invented queue.

---

## 4. Precondition gate (root §五, §八) — closed, fail-closed

Owner: one new service `TaskExecutionService` (`app/services/task_execution_service.py`),
called ONLY by the Execute transport. Evaluation order is fixed (cheapest →
most expensive); **first failure = closed code, HTTP mapping in §7.2, nothing
enqueued** (root §五: "any one condition fails → fail-closed, don't sneak an
enqueue").

| # | Precondition | Rule (V1) | Evidence / owner |
|---|---|---|---|
| P1 | **Tenant** | The write tenant is `agent.tenant_id` (must be non-None, else `TENANT_CONTEXT_MISSING`). If `task.project_id` is set: load the Project with tenant scope and require `project.tenant_id == agent.tenant_id` (code `TENANT_MISMATCH`). If `task.tenant_id` is non-None: require `task.tenant_id == agent.tenant_id` (code `TENANT_MISMATCH`). **This directly closes the task readiness gap §6.3 ("intake tenant check is agent-side only").** | `task.py:47` nullable task tenant; `task_decomposition_service.py:238-245` precedent (analysis lane asserts agent tenant == project tenant) |
| P2 | **Project state** | If `task.project_id` set: `project.status ∈ {ANALYZING, PENDING_CONFIRMATION, EXECUTING}`. Anything else — `RECEIVED`, `SOURCES_OK` (material not ready), `BLOCKED`, `COMPLETED`, `ARCHIVED`, `REJECTED` — → 409 `PROJECT_NOT_EXECUTABLE`. If `project_id` is NULL: pass (non-project Task; its source is the Agent itself). | `project.py:53-70` closed enum; materialization only allowed at `INITIALIZED`-plus (`project_materialization_service.py:297-323`, SOURCE_NOT_READY same-code reuse precedent); **RULING on U-1: execution-allowed set = ANALYZING ∪ PENDING_CONFIRMATION ∪ EXECUTING — a Project that is still sourcing (RECEIVED/SOURCES_OK) or frozen/terminal cannot have Tasks run on it.** The set is a named constant in the service so root §十八's "Project lifecycle design must come first" is honored: 2E depends on the set, does not own the Project state machine. |
| P3 | **Task state** | `task.type == "todo"` (supervision keeps its existing trigger path; its keys are occurrence-based and out of V1 Execute scope) AND `task.status == "pending"`. `doing` → 409 `TASK_ALREADY_RUNNING`; `done` → 409 `TASK_TERMINAL` (root §八 "Task 已完成" must not re-enter). | `task.py:58-62`; re-trigger hole audit §5.3 I-2 |
| P4 | **Dependency readiness** | `task_graph_service.ensure_ready(task, agent.tenant_id) == []` (the Phase 2D gate; todo: every direct dep `done`, no edges = trivially ready). Unmet → 409 `TASK_BLOCKED` + unmet list; task stays `pending`, **no Run** (existing `TaskBlockedError` semantics, `task_executor.py:96-100`). The gate re-runs `ensure_ready` itself as defense-in-depth even though P3-P5 already passed — the service never trusts a cached derivation (readiness is never persisted, task readiness audit §2). | `task_graph_service.py:403` |
| P5 | **Agent availability** | Agent row exists, `deleted_at IS NULL`, `agent.tenant_id` non-None (P1 covers match), `primary_model_id` non-None, `decide_runtime_v2` selects v2 for this agent+`"task"` source, and `is_agent_expired` passes (already checked on the trigger path, `api/tasks.py:284-285` — the Execute path must check it too, today only the legacy trigger does). Failures map to `ASSIGNMENT_FAILED` (agent missing/deleted) or `AGENT_UNAVAILABLE` (expired / no model / v2 gate off). | `task_executor.py:66-81`; `agent.py:55-59,109,147`; **UNKNOWN U-A: `Agent.status` (`creating/running/idle/stopped/error`) is NOT consulted anywhere in the task path today (agent/queue audit U1) — V1 ruling: do NOT add it as a gate; it is container-lifecycle vocabulary. Opt-in check is post-V1 config, owner §11.** |
| P6 | **Workspace availability** | **RULING on U-2 / root §五 "Workspace": NO enqueue-time check exists and V1 adds none.** Workspace = storage-key namespace `{agent_id}/…` with on-demand directory creation + per-Run temp materialization (workspace audit §1). Availability is decided by the OWNING layer — the storage runtime / run workspace materialization — at run time (root rule: resolve policy at the owning boundary, not speculative preflight). A busy Redis lock / human edit lock surfaces as a Run-level fast-fail `WORKSPACE_CONFLICT` (§7.3, transient, bounded retry §7.4). | workspace audit §1, §4, G2; `run_workspace.py:72-91`; `workspace_locking.py:35-91` |
| P7 | **v2 runtime gate** | `decide_runtime_v2` False → fail-closed, code `RUNTIME_V2_DISABLED`; no legacy fallback (existing behavior, `task_executor.py:59-65,208-216`). | `agent_runtime/config.py:134` |
| P8 | **Provenance** | For project Tasks: `project_id` must be set and consistent (already fail-closed at authoring by `provenance_consistency`, `task_dao.py:335`); Execute re-loads the Project via P1 so a dangling/CASCADE-deleted project is caught as 404 `PROJECT_NOT_FOUND`, not executed blind. | `task.py:84` CASCADE; audit §5.1 |

Every gate failure writes a `TaskLog` (⛔/❌ with the closed code, existing
precedent `task_executor.py:189-236`) AND an `AuditLog` row (§8) — the blocked
outcome is an event, not a silent no-op.

---

## 5. Idempotency — duplicate execution attempts (root §九)

`FACT` anchors (re-verified): Run dedup key = `source_execution_id`;
`enqueue_task_runtime` uses `task:{task.id}` for todo (stable,
`task_executor.py:87`); the intake finds an existing Run via
`_find_start_retry` scoped by `(tenant_id, source_type, source_execution_id)`
(`adapter.py:67-77`), enforces an **exact-input match** on payload +
`idempotency_key` + actor (`persistence.py:297-318`, mismatch →
`command_idempotency_mismatch`), and DB-enforces one Run per key via
`uq_agent_runs_source_execution` (`agent_run.py:173-179`). Command level:
`uq_agent_run_commands_run_idempotency` (`agent_run_command.py:46`).

V1 rules:

- **R1 — double-click = one Run.** Execute for a `pending` todo builds
  `source_execution_id = "task:{task.id}"` (stable). A second Execute with the
  same task state finds the existing Run and returns it with
  `handle.created == False` — **reuse existing run**, the API reports
  `{"created": false, "run_id": …}`. No duplicate Run is physically possible
  (DB unique), and the UI double-click is irrelevant — the rule lives in the
  intake, not the front end (root §九: "don't rely on UI anti-double-click").
- **R2 — in-flight = reuse.** Task `doing` (Run queued/running): Execute →
  409 `TASK_ALREADY_RUNNING` with the active `run_id` in the body (root §八
  "Task 正在执行且不允许 duplicate run"; one attempt per Task at a time —
  no parallel attempts on the same Task, no lane needed to say so: the
  status is the guard).
- **R3 — terminal failure = explicit new attempt.** A Run that ended
  `failed`/`cancelled` is IMMUTABLE history (a new start command would
  collide on the stable key — `persistence.py:297-318` exact-input check, and
  settlement already returned the Task to `pending`,
  `task_completion.py:145-154`). A human re-clicking Execute after a failed
  attempt creates a **new Run under a new attempt key**:
  `source_execution_id = "task:{task.id}:retry:{attempt_id}"` where
  `attempt_id` is a fresh UUID generated by the Execute service **only** when
  the Task's latest Run (by `created_at`, for that task) is in a terminal
  failed/cancelled state (no in-flight Run). This is the supervision
  occurrence-key precedent (`task_executor.py:84-85`) extended deliberately;
  **root §九 "create explicit new attempt" is chosen for the failed case and
  "reuse existing run" for the pending/in-flight case.** The Run payload gains
  one field, `"task_attempt": "<attempt_id>"`, so "which Run did this
  execution produce?" (root §十五) is answerable: the Run list for the task is
  `source_execution_id LIKE 'task:{id}%'` (stable first + ordered retries),
  plus `AgentRun.source_id` already carries the task id (`task_executor.py:110`).
- **R4 — never automatic.** Retries are human-initiated (Execute click). There
  is no automatic retry loop anywhere in V1; worker-side queue retries
  (5 attempts → quarantine, `persistence.py:744-748`) are the Runtime's
  internal crash-recovery, not product retry (root §十四: queue transient
  failure is the one transient class — and only inside the command worker).
- **R5 — key stability.** The stable key `"task:{task.id}"` is reserved for
  the FIRST attempt only. Retries never reuse the stable key; retry keys are
  per-attempt. `idempotency_key` stays `start:{source_execution_id}`
  (`task_executor.py:116`).

---

## 6. State mapping — no second lifecycle (root §十二)

### 6.1 Task states (3-value, §1.2)

| Task.status | Meaning | Writers |
|---|---|---|
| `pending` | not yet in a Run, OR the last Run ended failed/cancelled (recoverable) | create; settlement on failed/cancelled (`task_completion.py:145-154`); human PATCH guard (§1.2) |
| `doing` | a Run is registered for this Task and has not settled | `enqueue_task_runtime` (`task_executor.py:127`) |
| `done` | terminal (todo) | settlement completed (`task_completion.py:137-140`) — programmatic writer only in V1 |

### 6.2 Derived execution states (root §十二 READY/QUEUED/RUNNING/SUCCEEDED/FAILED)

**Ruling**: the root's 8-state list is a *projection*, not a table. V1 adds no
column and no event; the projection is computed by the query endpoint (§10.2):

| Derived | Computed from |
|---|---|
| READY | `status == pending` ∧ `ensure_ready == []` (pull-only; task readiness §3/§4) |
| BLOCKED | `status == pending` ∧ unmet deps non-empty |
| QUEUED | `status == doing` ∧ latest Run command `pending` (`agent_run_commands.status`, `agent_run_command.py:36`) |
| RUNNING | `status == doing` ∧ command `claimed` |
| SUCCEEDED | `status == done` (Run terminal `completed`) |
| FAILED | `status == pending` ∧ latest Run terminal `failed` (audit §5: "a failed Run does NOT fail the Task" — the two lifecycles are deliberately decoupled, root §十三 "don't write Run Failed as Task Failed") |
| CANCELLED | `status == pending` ∧ latest Run terminal `cancelled` |

QUEUED/RUNNING come from the command row; the authoritative in-execution state
is the LangGraph checkpoint (agent/queue audit §2: "execution state stays in
checkpoints"). The projection therefore never claims more truth than
Task-row + command-row + latest-Run identity give it; it is labeled
`derived_state` in the API response.

### 6.3 Failure taxonomy (root §十三) — closed code set, owner = intake service

| Code | Class | Trigger | Retry |
|---|---|---|---|
| `ASSIGNMENT_FAILED` | terminal, human | agent missing / deleted (P5) | NO (root §十四: nonexistent agent — no unlimited retry) |
| `AGENT_UNAVAILABLE` | terminal, human | expired / no primary model / v2 gate off (P5/P7) | NO until human changes config/agent |
| `TENANT_MISMATCH`, `TENANT_CONTEXT_MISSING` | terminal, security | P1 | **NO, ever** (root §十四: security failure / invalid tenant) |
| `PROJECT_NOT_EXECUTABLE` | terminal, human | P2 | NO (human moves the Project) |
| `TASK_BLOCKED` | waiting | P4 — unmet deps, unmet list in body | implicit: re-ready when deps finish (pull re-derive), human re-triggers; no auto-loop |
| `TASK_ALREADY_RUNNING` | 409 | R2 | NO (wait for settlement) |
| `TASK_TERMINAL` | 409 | P3 done | NO |
| `TASK_STATUS_WRITE_FORBIDDEN` | 409 | PATCH guard §1.2 | NO |
| `WORKSPACE_CONFLICT` | **transient** | Run hit a busy Redis/human lock (fast-fail, workspace audit §4.3) → settlement flips Task back to `pending` | YES, bounded: human re-Execute with new attempt key (§5 R3); V1 cap = 3 retries per Task per Project day recorded in audit — a soft bound, enforced by the API (audit count), NOT by queue machinery (no queueing facility exists, G2) |
| `QUEUE_FAILED` | **transient** | `start_run`/commit failure at intake | YES bounded (same 3/Task/day; new attempt key, since a half-registered command may exist — the exact-input check makes re-submitting the same key safe or explicitly mismatched, never silently divergent) |
| `RUN_FAILED` | terminal per-attempt | Run checkpoint terminal `failed` (settlement → Task `pending`) | YES via R3 (explicit new attempt); root §十三 distinguishes it from Task-failed — Task is recoverable |
| `VERIFICATION_FAILED` | terminal per-attempt | verify node failure surfaces as terminal `failed` lifecycle today (agent/queue audit §5) | YES via R3 |
| `RETRY_CAP_EXCEEDED` | **transient, human** | R3 per-task/day **soft cap**: a re-Execute that would mint a NEW retry attempt is rejected when today's `task_execute_retried` audit count is already ≥ `RETRY_SOFT_CAP_PER_TASK_PER_DAY` (3) — `audit_dao.count_task_audit` (task/agent/audit §8), enforced at `task_execution_service.py:202-209` before any enqueue | NO for the day: the cap is a **soft, human-recoverable** bound that resets at the UTC day start; it is bounded (root §十四: no unlimited retry) and never auto-retried (R4) |

> **Transport mapping (closed-code path, §10.1).** `RETRY_CAP_EXCEEDED` is a
> first-class member of the §6.3 closed code set: the intake service raises it
> via `TaskExecutionError` (a fail-closed gate rejection, like every other §6.3
> code), and the Execute transport maps it to **HTTP 409 `CONFLICT`** with body
> `{ "code": "RETRY_CAP_EXCEEDED", "message": … }` (the same 409 closed-code
> path as `TASK_BLOCKED`/`TASK_ALREADY_RUNNING`; it is not in the 404
> not-found set). Nothing is enqueued on this path (fail-closed, root §五).

**UNKNOWN U-C** (agent/queue audit U3): whether verify failures carry a distinct
error code inside the terminal checkpoint was not audited node-by-node — V1
therefore does not *invent* a separate code path; `VERIFICATION_FAILED` is
reported as its own code ONLY when the checkpoint `lifecycle.error` carries a
recognizable verify marker (Wave-2 verification item; until then it reports
`RUN_FAILED` and carries the checkpoint detail in the TaskLog — bounded, no
second classification system).

---

## 7. Concurrency boundary (root §十, §十一)

- Independent Tasks on distinct Agents: parallel, no gate added — physical
  isolation already proven (workspace audit §4.1, storage key namespace +
  path-resolution double gate).
- Dependent Tasks: P4 makes the dependency edge load-bearing: B cannot Execute
  while A is not `done` (root §十 "B 必须等待 A" — the gate is the mechanism;
  no separate scheduler exists in V1 and none is invented).
- Same Agent, multiple in-flight Tasks: allowed (U-B, §11). Conflict surface =
  file-level fast-fail (`Workspace lock busy` / human-lock busy/skip, workspace
  audit §4.2-4.3) + per-Run temp materialization (no data tearing) →
  `WORKSPACE_CONFLICT` transient (§6.3). **No single-writer, no queue, no
  wait.** Optional opt-in serialization for a later phase: set
  `scheduling_lane_key = "agent:{agent_id}"` on task Runs (the trio +
  `uq_agent_runs_active_lane` already exist, `agent_run.py:140-148,180-185`);
  V1 leaves it NULL (root rule: no speculative knobs).
- Cross-tenant: impossible by construction (§9).

---

## 8. Audit (root §十六)

- Owner: the intake service (NOT the Runtime — agent/queue audit §8: "add
  AuditLog rows at the intake, not in the Runtime").
- Write one `AuditLog` row per Execute outcome:
  `action ∈ {"task_execute", "task_execute_blocked", "task_execute_retried"}`,
  `tenant_id`, `user_id` (the calling user — real caller, not task creator,
  closing UNKNOWN U6 of the agent/queue audit), `agent_id`, `details = {task_id,
  project_id, run_id, source_execution_id, outcome_code}`.
- `AuditLog` schema already fits: `models/audit.py:13-28`
  (tenant/user/agent/action/details/ip, `__tenant_scoped__`). No new table.
- Audit write failure: narrow catch, logged, **primary outcome not swallowed**
  (materialization precedent `project_materialization_service.py:886,981-1005`).
  No credentials ever enter `details` (root §十六).

---

## 9. Tenant hardening (root §二十一 + workspace audit G1)

- HTTP Execute/assign paths: `check_agent_access` already 403s cross-tenant
  agent access (`core/permissions.py:556-558`); task loads are DAO-scoped —
  a foreign tenant's task is 404 by construction (`dao/base.py:139-174`
  auto-injected `tenant_id` predicate).
- P1 adds the missing **direct** assertion `task.tenant_id == agent.tenant_id`
  (and `project.tenant_id == agent.tenant_id` for project tasks) at the gate
  (closes task readiness gap §6.3 — today the task tenant is only *inferred*
  through the agent).
- **Background/queue rule (workspace audit G1, mandatory)**: any 2E code that
  touches Task/Agent/Run rows outside an HTTP request MUST run inside
  `tenant_context(tenant_id)` AND re-verify `verify_tenant_scope` at entry —
  because `get_scoped` silently degrades to an unscoped PK read when no tenant
  context is bound (`dao/base.py:242-246`). The Execute path today is HTTP
  (context present); the rule is written into the service contract so a future
  caller (e.g. a scheduler) cannot inherit the hole.
- Claim side: `claim_next_command` is tenant-unfiltered but per-Run thread
  locks + tenant-scoped settlement keep cross-tenant execution impossible
  (agent/queue audit §6, U4 accepted, no disclosure risk).

---

## 10. API surface (root §十七 — minimal, existing style)

### 10.1 Execute (assign + trigger in one, V1)

```
POST /agents/{agent_id}/tasks/{task_id}/execute
body: {}                                    (no agent_id in body — §1.1: the FK is the binding)
200 → { task_id, created, run_id, source_execution_id, attempt_id?, derived_state }
404 → TASK/PROJECT/AGENT not found for this tenant/agent
403 → tenant mismatch at agent access (existing check_agent_access)
409 → { code ∈ §6.3 closed set (incl. RETRY_CAP_EXCEEDED, §6.3 addendum), unmet_dependencies?: [...], active_run_id?: ... }
```

- Transport only (`api/tasks.py` style: parse → `check_agent_access` → call
  owning service → map outcome; no ORM in the handler, backend/AGENTS.md
  boundary rule). The service performs §4 gates + §5 keying + §8 audit + the
  `enqueue_task_runtime` call, passing `actor_user_id=current_user.id` and
  `origin_user_id=current_user.id` (UNKNOWN U6 closed: the triggering caller,
  not `task.created_by`, is the Run's actor — `task_executor.py:123-124`
  currently hard-wires the task creator; the V1 call site passes the real
  caller, and `enqueue_task_runtime` gains an `actor_user_id` parameter defaulting
  to `task.created_by` so legacy call paths keep byte-identical behavior).
- The legacy `POST /{task_id}/trigger` stays for supervision; V1 adds a status
  guard to it (done → 409 `TASK_TERMINAL`) so the audit §5.3 I-2 hole is
  closed on both entry points.

### 10.2 Query execution

```
GET /agents/{agent_id}/tasks/{task_id}/execution
200 → { task: TaskOut, derived_state, active_run_id?, runs: [
           { run_id, source_execution_id, attempt (first|retry:<id>),
             started_at, settled_state?, result_summary? } ] }
```

- `runs` = the task's Runs ordered by `created_at` (join-free via
  `AgentRun.source_type="task"` + `source_execution_id LIKE 'task:{id}%'`
  within tenant scope — bounded per Task, root §2 data-bound rule: one task,
  small N; no unbounded sweep).
- `result_summary` is the latest settlement TaskLog line (existing evidence,
  root §十五: reuse Run result / TaskLog / revisions — no new Artifact system).
- Reassignment / "which Agent executed this Task?": `AgentRun.agent_id` +
  provenance (`task.project_id`) answer it (root §十五 question 1); the
  workspace revision `group_key` pattern already exists for materialization
  (workspace audit §7.4) — reuse, don't extend, in V1.

### 10.3 Assign Task — V1 answer

Root §十七 asks for a separate "Assign Task → Agent" surface. V1 ruling:
assignment **is** `Task.agent_id`, bound at Task authoring
(`POST /agents/{agent_id}/tasks`, `api/tasks.py:85-186` — the URL itself
carries the Agent). A distinct reassignment endpoint is **post-V1** (U-K, §11):
it would have to move a Task between Agents' workspaces + provenance, which
V1 explicitly does not invent.

---

## 11. UNKNOWN register (cannot be resolved from code alone — rulings)

| ID | Question | V1 ruling | Owner / revisit |
|---|---|---|---|
| U-1 | Project-state precondition (task readiness U-1) | §4 P2: execution-allowed set `{ANALYZING, PENDING_CONFIRMATION, EXECUTING}` as a named constant; BLOCKED/terminal/sourcing states reject | root card t_b0bb2f7c owns the Project lifecycle (root §十八); 2E depends on the constant, not on the machine |
| U-2 | Workspace-availability precondition (task readiness U-2, workspace G2) | NO enqueue-time check — availability is decided by the storage owner at run time; busy lock = `WORKSPACE_CONFLICT` transient, bounded human retry, no queue (workspace audit G2 recommendation) | post-V1 only if real wait/queue demand appears |
| U-3 | done-todo re-trigger = terminal or re-openable? (task readiness U-3) | **Terminal** for todo; explicit new Task for new work; re-Execute after a FAILED attempt = new attempt key (§5 R3) | — |
| U-4 | reuse `Task.agent_id` vs new `explicit_agent_id` (task readiness U-4) | Reuse `Task.agent_id`; no new column (§1.1) | post-V1 capability-matching may introduce selector fields, not a second binding |
| U-5 | downstream ready-push in scope? (task readiness U-5) | NO — pull-only V1; `list_dependents` stays an unused primitive; readiness re-derives at the next gate/read/UI poll (task readiness §3, audit §6.1) | post-V1 small completion-side hint, owned by the gate owner, not persistence |
| U-6 | PATCH status guard (task readiness U-6) | PATCH may write `status` only to `pending`; `doing/done` writes → 409 (§1.2) | — |
| U-A | gate on `Agent.status`? (agent/queue U1) | NO gate; `status` is container-lifecycle vocabulary, not execution availability | post-V1 opt-in config flag, owner: runtime config lane |
| U-B | per-agent concurrency: lane-serialize vs multi-run? (agent/queue U2) | multi-run per agent (V1 default); lane stays NULL; serialization is an opt-in `scheduling_lane_key` for a later phase | reviewer may promote lane-default if E2E shows conflict storms |
| U-C | verify-failed vs run-failed error codes (agent/queue U3) | not distinguishable node-level today → report `RUN_FAILED` with checkpoint detail; `VERIFICATION_FAILED` only when a recognizable marker exists (Wave-2 item) | Wave-2 verification task |
| U-D | production backend set (local vs s3+fallback; workspace W2/G3/G5) | V1 concurrency-correctness claim = local(Unix) + conditional writes; Windows dev host is NOT a concurrency-correctness claim surface (workspace G3) | ops decision, outside 2E |
| U-K | reassignment (change `Task.agent_id`) | out of V1 (§10.3) | post-V1 |
| U-L | Project aggregate EXECUTING (root §十八) | out of 2E; P2 consumes a named state set, the Project machine stays with its owner | root card |

---

## 12. Verification matrix (builder t_c2ed29df implements; reviewer checks)

1. **Gate negative matrix** — one test per §4 code (P1-P8 × closed code), each
   asserting NO Run row was created (query `agent_runs` for the task's
   `source_execution_id` prefix = empty) and a TaskLog + AuditLog row exists.
2. **Double-click** — two concurrent Executes for a fresh ready Task ⇒ exactly
   one Run row (unique index), second response `created=false` with the first
   `run_id`.
3. **Failure-retry** — force a Run to terminal failed (test stub at the
   worker boundary, not LangGraph internals), Task returns `pending`,
   re-Execute ⇒ new Run under `task:{id}:retry:{…}`, stable-key Run untouched.
4. **Dependency** — B depending on A: Execute B while A pending ⇒ 409
   `TASK_BLOCKED` + unmet=[A]; finish A (test settlement stub) ⇒ Execute B
   succeeds. E2E variant per root §十九: independent A/B/C → parallel Runs;
   chain B→A waits. Observe real Run rows, not just status fields.
5. **Tenant** — Tenant A user executing Tenant B's task/agent ⇒ 404/403 by
   construction + P1 direct-assertion test (task row `tenant_id` ≠ agent
   tenant). Plus: background-caller test that proves an un-`tenant_context`'d
   call is rejected at the service entry (G1 rule, §9).
6. **State projection** — §10.2 responses for READY/BLOCKED/QUEUED/RUNNING/
   SUCCEEDED/FAILED shapes over stubbed command/checkpoint states.
7. **Regression** — Phase 2B/2C/2D suites green (root §二十六); specifically
   the trigger path still byte-identical when `actor_user_id` is not passed
   (legacy default).
8. **Migration** — none (V1 = zero DDL, §1.1). If review forces a DDL:
   independent new migration off the single head, fresh+existing+downgrade
   (root §二十五), NO historical edits.

---

## 13. ADR (architecture decision record, condensed — root contract)

- **Problem**: a confirmed-ready Project Task must reach a real Agent Run with
  duplicate, dependency, tenant, and workspace protection, without rebuilding
  the Runtime.
- **Context**: Phase 2D graph/provenance + the existing Task→Run chain already
  carry 80% of the semantics (dedup keys, settlement, tenant scoping, fast-fail
  locks); the gaps are the gate, the guards, the attempt-keying, and the audit.
- **Options**: (A) minimal service gate + key reuse on the existing chain (chosen);
  (B) new execution scheduler/queue for Tasks — rejected: duplicates the
  command-inbox machinery and violates the root §三 boundary; (C) parallel
  `TaskExecutionState` table — rejected: invents a second lifecycle (root §十二).
- **Decision**: §1-§10 above; V1 delta = 1 service + 2 endpoints + guards +
  audit + attempt keying; zero schema change.
- **Consequences**: retry history grows per-task Run rows (bounded: retry is an
  explicit human act, cap in audit §6.3); `AgentRun` query for a task's Runs
  uses a prefix LIKE within tenant scope (fine at V1 volumes); post-V1 items
  (§11) have named owners so the gate never silently grows.
- **Rejected alternatives**: explicit_agent_id column (U-4); workspace
  pre-flight check + wait queue (U-2/G2); ready-push hook (U-5);
  lane-default serialization (U-B); Project EXECUTING aggregation (U-L).

---

## 14. Deliverable handoff

- Builder (t_c2ed29df): implement §4-§10 in `backend/app/services/task_execution_service.py`
  (+ Execute/execution endpoints in `api/tasks.py`, PATCH/trigger guards,
  `enqueue_task_runtime` `actor_user_id` param with legacy default), zero
  migrations; verification per §12.
- Reviewer (t_84836b4b or successor): check the root §二十二 14-point list;
  pay attention to (1) gate fail-closedness — no enqueue on ANY P1-P8 failure,
  (2) P1 closing the agent-side-only tenant gap, (3) the attempt-keying rule
  §5 (stable first / retry-N never auto), (4) no schema drift, (5) regression
  of the legacy trigger path.
- This doc is the source of truth for V1 semantics; any deviation found during
  implementation comes back to this lane with an ADR addendum before it lands.
