# Phase 2E — Audit: Agent / Agent Run / Queue Infrastructure

**Card**: t_2ae698f7 (child of t_b0bb2f7c, "Phase 2E — Project Task → Agent Assignment → Real Execution")
**Baseline**: `main @ 7497bf7a` (branch `wt/t_2ae698f7`), 2026-09-25
**Method**: strict source-code audit. Every claim cites `path:line`. No design-doc inference.
Unknowns marked **UNKNOWN**. Read-only audit — no product code touched.

Companion evidence: `docs/AGENT_RUN_EXECUTION_CHAIN.md` (Phase 1 chain trace, baseline
834d621) — re-verified against current source; the spine still holds.

---

## 1. Agent model & availability

**Model**: `backend/app/models/agent.py:19` (`agents`)

| Fact | Evidence |
|---|---|
| Tenant-scoped | `agent.py:26` `__tenant_scoped__ = True`; `tenant_id` FK nullable (agent.py:45) |
| Status enum | `agent.py:55-59` `creating/running/idle/stopped/error` — container-lifecycle vocabulary, not a runtime gate |
| Expiry | `agent.py:108-110` `expires_at` / `is_expired`; enforced by `app/core/permissions.py:284` `is_agent_expired` in the task trigger path |
| Deletion | soft delete: `deleted_at` (agent.py:147); intake refuses deleted agents (`task_executor.py:161`) |
| Model binding | `primary_model_id` (agent.py:64); intake requires a non-null primary model (`task_executor.py:76-81`, error `agent_model_missing`) |

**Availability mechanisms — what actually gates a Task Run today:**

1. **v2 runtime gate** — `decide_runtime_v2` (`agent_runtime/config.py:134`): agent allowlist
   (`AGENT_RUNTIME_V2_AGENT_IDS`) → source type → global flag
   (`AGENT_RUNTIME_V2_ENABLED`, `AGENT_RUNTIME_V2_SOURCE_TYPES`, config.py:92-97).
   When `use_v2` is False the intake returns `None` and the caller **fails closed** —
   no legacy execution fallback exists (`task_executor.py:208-216`).
2. **Expiry** — `check_agent_access` + `is_agent_expired` at the HTTP trigger
   (`api/tasks.py:284-285`).
3. **Deletion / tenant membership** — resume path requires
   `Agent.tenant_id == command.tenant_id AND deleted_at IS NULL`
   (`adapter.py:342-353`, `agent_unavailable`); the task path requires
   `task.agent_id == agent.id` (`task_executor.py:66-70`) and `agent.tenant_id is not None`
   (`task_executor.py:71-75`).

**UNKNOWN-U1**: `Agent.status` is not consulted anywhere in the task-run path — a
`stopped`/`error` agent still accepts a Task Run. Whether "availability" should check
`status` is a Phase 2E design decision, not a code fact.

**UNKNOWN-U2**: no per-agent execution capacity exists for task-source runs (only
`scheduling_lane` serialization, which task runs do not use — see §4.3).

---

## 2. Agent Run model

**Model**: `backend/app/models/agent_run.py:27` (`agent_runs`) — "product-owned identity
and delivery facts; execution state stays in checkpoints" (docstring, agent_run.py:2).

| Fact | Evidence |
|---|---|
| Closed source types | `agent_run.py:34-36` `chat/trigger/task/a2a/heartbeat` (CHECK) |
| Closed run kinds | `agent_run.py:38-40` `foreground/background/delegated/orchestration` |
| Task runs carry the Task | `task_executor.py:109-122`: `source_type="task"`, `source_id=str(task.id)`, `run_kind="background"`, `delivery_status="not_required"` |
| **Idempotency anchor** | partial unique index `uq_agent_runs_source_execution` (`agent_run.py:173-179`) on `(source_type, source_execution_id) WHERE source_execution_id IS NOT NULL` |
| Lane serialization | `scheduling_lane_key` / `lane_held` / position trio (`agent_run.py:140-148`), unique partial index `uq_agent_runs_active_lane` (`agent_run.py:180-185`) |
| Tenant scoping | `agent_run.py:80` `UniqueConstraint("tenant_id","id")` + composite FK `fk_agent_runs_tenant_session_chat_sessions` (`agent_run.py:75-79`) |
| Lifecycle | `agent_run_event.py` (`run_created` / terminal events), published only after checkpoint commit (`persistence.py` side-effects; see AGENT_RUN_EXECUTION_CHAIN §7-8) |

Run **state is NOT in `agent_runs`** — it lives in the LangGraph PostgreSQL
checkpoint (`agent_runtime/checkpointer.py`); `AgentRun` holds identity + delivery facts
only. This matters for Task/Run state mapping (§6 of the Phase 2E brief): there is no
parallel lifecycle to invent; the checkpoint is the single source of execution truth.

---

## 3. Task → Run flow (exact code path)

```
POST /agents/{agent_id}/tasks                    api/tasks.py:85
  ├── check_agent_access(db, user, agent_id)     api/tasks.py:93
  ├── Task INSERT (+ Phase 2D provenance)        api/tasks.py:94-141
  ├── auto-enqueue todo tasks:
  │     enqueue_task_runtime(db, task, agent)    api/tasks.py:150 → task_executor.py:44
  │       · type gate {"todo","supervision"}     task_executor.py:54-58
  │       · decide_runtime_v2(task)              task_executor.py:59-65  → None ⇒ fail closed
  │       · task.agent_id == agent.id            task_executor.py:66-70
  │       · agent.tenant_id not None             task_executor.py:71-75
  │       · primary model present                task_executor.py:76-81
  │       · source_execution_id:
  │           todo         → "task:{task.id}"             task_executor.py:87
  │           supervision  → "task:{id}:supervision:{occurrence_id}"  task_executor.py:84-85
  │       · DEPENDENCY GATE (Phase 2D §5.4):
  │           task_graph_service.ensure_ready()  task_executor.py:96-100
  │             → unmet deps ⇒ TaskBlockedError ⇒ task stays pending, TaskLog ⛔, NO Run
  │       · RuntimeCommandIntake.start_run(StartRunCommand(...))  task_executor.py:102-126
  │           → adapter.py:245 start_run()
  │               · rollout re-check             adapter.py:250-274
  │               · _resolve_source_retry        adapter.py:248 → persistence.py:253
  │                   (exact-input match on existing (source_execution_id, idempotency_key);
  │                    any mismatch ⇒ command_idempotency_mismatch, persistence.py:297-318)
  │               · register_run_with_start      persistence.py:321  (caller's txn; no commit)
  │                   creates AgentRun + AgentRunCommand(pending) + AgentRunEvent(run_created)
  │       · task.status = "doing" (+TaskLog)     task_executor.py:127-134
  └── fallback: POST-create background trigger   api/tasks.py:181-184
        execute_task(task.id, agent.id)          task_executor.py:179
          · fresh async_session, tenant_context(agent.tenant_id)  task_executor.py:144-176
          · same enqueue_task_runtime; errors ⇒ TaskLog ❌, task stays pending
```

Manual trigger (supervision): `POST /agents/{agent_id}/tasks/{task_id}/trigger`
(`api/tasks.py:274-296`) → `execute_task`. Agent-side auto-exec:
`agent_tools.py:9855-9858` (todo task creation auto-fires `execute_task`).

**Exact injection point for a new execution request:**
`RuntimeCommandIntake.start_run(StartRunCommand)` — `agent_runtime/adapter.py:245`.
The Task ingress is `task_executor.enqueue_task_runtime` / `execute_task`
(`task_executor.py:44/179`), which is the existing, tested adapter that builds the
command (`task:{task.id}` key, background kind, not_required delivery). **Phase 2E
should add its enqueue by calling this function (or a sibling built the same way),
not by writing new command/worker code.**

---

## 4. Queue: enqueue, claim, lease, retry

**Command inbox**: `agent_run_commands` (`agent_run_command.py:25`).
- Types `start/resume/cancel`; statuses `pending/claimed/applied/rejected`
  (agent_run_command.py:31-38); `attempt_count` (line 97); lease fields
  `claimed_by` / `claim_expires_at` (lines 95-96); unique
  `uq_agent_run_commands_run_idempotency` on `(run_id, idempotency_key)` (line 46).
- Intake writes `pending` in the caller's transaction, uncommitted until the ingress
  commits (`adapter.py:246` docstring "without committing"; `task_executor` returns the
  `RunHandle`, `api/tasks.py:177` commits first).

**Claim (worker side)**: `persistence.py:635` `claim_next_command` — single
`_claim_statement` (persistence.py:524):
- picks the oldest command where `status='pending'` OR
  (`status='claimed'` AND `claim_expires_at < now`) — **expired leases are reclaimed
  automatically**;
- `.with_for_update(skip_locked=True).limit(1)` (persistence.py:594-595) → multiple
  worker processes contend without blocking each other;
- **per-Run input order**: a command with an unfinished earlier command on the same
  Run is not claimable (persistence.py:530-544);
- **lane serialization**: a start command on a `scheduling_lane_key` is claimable only
  if no other Run holds the lane and no earlier lane-start is unfinished
  (persistence.py:545-573, 586-591); holder flag set in `_acquire_start_lane`
  (persistence.py:599-632).

**Lease params** (config.py:145-148, defaults):
- `AGENT_RUNTIME_COMMAND_CONCURRENCY = 10` (shared capacity per worker process)
- `AGENT_RUNTIME_COMMAND_CLAIM_TTL_SECONDS = 60`
- `AGENT_RUNTIME_COMMAND_CLAIM_RENEW_SECONDS = 20` (must be < TTL, validated
  config.py:246-252)
- `AGENT_RUNTIME_COMMAND_MAX_ATTEMPTS = 5`

**Attempt & rejection**:
- `begin_command_attempt` (persistence.py:724) consumes one attempt **after the
  thread lock is held**; `attempt_count >= max_attempts` ⇒
  `command_reconciliation_required` — the command is **quarantined, not re-queued**
  (persistence.py:744-748).
- `mark_command_rejected` (persistence.py:856) + lane repair
  `release_rejected_start_lanes` (persistence.py:929).
- `mark_command_applied` (persistence.py:763) stores the checkpoint receipt and sets
  `error_code="product_sync_pending"` as the crash-safe marker for the product
  reconciler (persistence.py:790-794).

**Execution**: worker `RuntimeCommandWorker` (`command_worker.py`) under
`run_with_thread_lock` — a **PostgreSQL session-level advisory lock per runtime
thread** (`thread_lock.py:14` `pg_try_advisory_lock`; failure to acquire ⇒ skip,
another worker owns it). Graph execution: `langgraph_driver.py` start/resume/cancel.

---

## 5. Result settlement (Run → Task)

`TaskRuntimeCompletionHandler` — `agent_runtime/task_completion.py:56`:
- fires from the terminal checkpoint handler chain (worker_service.py:304-319) for
  `source_type="task"` only (task_completion.py:74);
- maps checkpoint lifecycle status → product Task status
  (task_completion.py:135-154):
  - `completed` + todo ⇒ `task.status="done"` + `completed_at`, TaskLog ✅
  - `completed` + supervision / `cancelled` / `failed` ⇒ back to `pending`,
    TaskLog ⏹️/❌ with the error code or reason
- **idempotent receipt**: one terminal TaskLog per checkpoint, keyed by
  `uuid.uuid5(run_id, f"task-terminal:{checkpoint_id}")` (task_completion.py:32-33,
  111-115); Task row is locked `with_for_update` (task_completion.py:117-123);
  a deleted Task leaves execution history untouched (task_completion.py:125-128).

Consequence: **a failed Run does NOT fail the Task** — the Task returns to `pending`
and can be re-triggered. This is the existing "retry" semantic: a new trigger with a
fresh `occurrence`/attempt is a new explicit act, not an automatic loop.
Phase 2E's failure taxonomy (§13 of the brief) must map onto THIS: `RUN_FAILED` =
Run terminal `failed` + Task `pending`; there is no `VERIFICATION_FAILED` terminal
state distinguishable at settlement — the verify node failures surface as the
`failed` lifecycle status (UNKNOWN-U3: whether verify failures carry a distinct
error code inside `lifecycle.error` was not audited node-by-node).

---

## 6. Tenant isolation, layer by layer

| Layer | Mechanism | Evidence |
|---|---|---|
| ORM SELECTs (all tenant-owned models) | auto-injected `tenant_id` predicate via `ContextVar` on `Session.do_orm_execute` — any missed business filter cannot cross-tenant read | `dao/base.py:116-151` |
| Background/worker code (no HTTP context) | explicit `tenant_context(tenant_id)` wrapper is MANDATORY | `dao/base.py:121-122`; used in `task_executor.py:170` and task graph API (`api/tasks.py:341`) |
| Agent run registry | composite FK `(tenant_id, session_id)` + `UniqueConstraint(tenant_id, id)` | `agent_run.py:75-80` |
| Run resume/cancel | commands re-check `run.tenant_id == command.tenant_id` and re-verify agent tenant membership | `adapter.py:339-353`; `persistence.py:707-720` |
| Task graph (Phase 2D) | authoring requires a non-None write tenant on both Agent and Task; cross-tenant refs ⇒ 404/403 | `api/tasks.py:324-349` |
| Command claim | `claim_next_command` is global (a worker may claim any tenant's command); tenant correctness is enforced per-command by the per-RUN thread lock + the tenant-scoped settlement session | `persistence.py:635`, `thread_lock.py`, `task_completion.py:88-95` |
| Agent access (user → agent) | `check_agent_access` gates every task API; tenant mismatch ⇒ 403/404 | `core/permissions.py:519` |

**UNKNOWN-U4**: `AgentRunCommand.tenant_id` FK CASCADE (agent_run_command.py:59-65)
means a tenant deletion removes its queued commands, but the claim statement has no
tenant filter — a queued claim for a deleted tenant is dropped by FK, not rejected.
Low risk (no disclosure), noted for completeness.

---

## 7. Workspace (Agent workspace) isolation & conflicts

- Per-Agent on-disk workspace dir: created at agent provisioning
  (`agent_manager.py:277-280`, `config_dir/workspace` symlink → `agent_dir/workspace`).
- **No agent-level execution lock exists**: two concurrent Runs of the SAME agent
  (different threads) are NOT serialized by the thread advisory lock (the lock is
  per `runtime_thread_id`, thread_lock.py:71). Nothing in the task path enforces
  single-writer on an agent workspace today.
- Available (group-scoped only): `scheduling_lane_key` serializes starts per lane
  (`agent_run.py:180-185`, persistence.py:599-632) — usable, with its position
  fields, to serialize task starts per agent if Phase 2E chooses that semantics
  (RUN-level fields; task runs currently leave them NULL).
- File revision/lock infrastructure: `workspace_edit_locks` (human edit locks,
  `workspace.py:71-81`) and `workspace_file_revisions` (`workspace.py:28`) —
  revision history exists; **agent-vs-agent concurrent file writes are not gated**.
- **UNKNOWN-U5**: whether a Run's tool executions are scoped to the owning agent's
  workspace dir (vs group workspace) depends on the tool handlers (not audited in
  this card; flagged as a Phase 2E Wave-2 verification item).

---

## 8. Audit & evidence

- `AuditLog` (`models/audit.py:13`, `__tenant_scoped__`, fields:
  tenant/user/agent/action/details/ip) — used today for handover + tool reconcile
  (`api/advanced.py:184`, `api/chat_sessions.py:804`). **No audit row is written for
  task assignment/execution today** — the Task + TaskLog + AgentRun triple is the
  only record. Phase 2E §16 (assignment/execute audit) ⇒ add `AuditLog` rows at the
  intake (owner: the enqueue boundary), not in the Runtime.
- Evidence answers for §15 ("which Agent ran this Task? / which Run?"):
  `AgentRun.agent_id` + `AgentRun.source_id=task.id` + `AgentRunCommand.actor_user_id`
  (task_executor.py:123-124 sets `task.created_by` as actor — UNKNOWN-U6: the ACTUAL
  triggering user for the auto path is the task creator, not the user who clicked the
  trigger; the trigger endpoint fires `execute_task` without passing the current user).

---

## 9. Exact entry-point summary (the Phase 2E answer)

| Question | Answer |
|---|---|
| How does a Task enter the queue? | `task_executor.enqueue_task_runtime` (task_executor.py:44) → `RuntimeCommandIntake.start_run` (adapter.py:245) → `register_run_with_start` (persistence.py:321) creates `AgentRun` + pending `AgentRunCommand` in the caller's txn |
| Key idempotency anchors | `source_execution_id = "task:{task.id}"` (todo) vs `task:{id}:supervision:{occurrence}` (supervision); DB unique `uq_agent_runs_source_execution`; command-level `uq_agent_run_commands_run_idempotency`; exact-input retry check (persistence.py:297-318) |
| Who drains the queue? | `RuntimeCommandDaemon` → `claim_next_command` (persistence.py:635, skip-locked, TTL lease 60s, renew 20s, max 5 attempts → quarantine) → advisory thread lock → LangGraph driver |
| How are results settled? | `TaskRuntimeCompletionHandler` (task_completion.py:56) — checkpoint-terminal ⇒ Task status flip + exactly-one receipt TaskLog; failed Run ⇒ Task back to `pending` |
| Pre-Run gates that already exist | v2 gate (fail-closed), agent/tenant/model checks, Phase 2D dependency gate `ensure_ready` (task_executor.py:96-100) |
| What Phase 2E must add | the **Assignment** step (explicit `agent_id` binding with its own preconditions: agent tenant-match vs task tenant, agent available, workspace available) + audit at intake + Task-state mapping; **NOT** a new run/queue/worker — all of that already exists in §3-§5 |

---

## 10. UNKNOWN register (handoff to t_180370c7)

- **U1**: `Agent.status` is not a gate today; decide whether availability checks it.
- **U2**: no per-agent concurrency limit for task runs; decide single-writer-per-agent
  (via `scheduling_lane_key`) vs multi-run-per-agent (workspace-conflict risk, §7).
- **U3**: verify-failure error codes inside the terminal checkpoint (RUN_FAILED vs
  VERIFICATION_FAILED distinction) — needs node-level inspection.
- **U4**: cross-tenant claim visibility (FK-drop on tenant delete) — accepted, no action.
- **U5**: per-tool workspace scoping (agent dir vs group dir) — Wave-2 verification.
- **U6**: actor attribution — trigger path uses task creator as Run actor, not the
  clicking user; Phase 2E assign/execute APIs should pass the real caller.
