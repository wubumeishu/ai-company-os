# Phase 3 — Execution Chain & Supervision/Scheduling Integration Audit (Trace Document)

Task: `t_5487441c` — "Audit execution chain and supervision/scheduling integration points"
Author: aco-architect
Baseline: main `44651184` (tag `PHASE_2F_CLOSED`) — audited in worktree `wt/t_5487441c`
All paths are repository-relative. Every claim is tied to a file:line or a named
closed code set. No speculative additions. UNKNOWN items are flagged explicitly.

## Classification Discipline

Carried over from `docs/architecture/phase2c_2f_evolution.md` and the Phase 3 root
brief: every non-trivial claim carries one of:

- **FACT** — directly observable in this tree (file:line, named constant, migration id).
- **OBSERVATION** — a pattern seen in one or more concrete instances, no universal claim.
- **INFERENCE** — a conclusion drawn from the facts above.
- **UNKNOWN** — not verifiable from this tree; the resolving action is stated.

Unlabeled prose is structural description (what the system *is*), not a claim.

---

## 1. The Execution Spine (call graph)

The durable Agent Runtime is a **single deterministic spine**. Every ingress
adapts to one durable command inbox, which is drained by one advisory-locked
worker that advances one LangGraph. This spine was proven intact in Phase 2F
(`docs/PHASE_2E_EXECUTION_CHAIN_VERIFICATION_T84836B4B.md`,
`docs/PHASE_2F_CONVERGENCE_REPORT.md`). The trace below re-derives it from the
current source, layer by layer, and marks where Planning / Task Graph /
Assignment plug in.

```
[INGRESS — one of the transport/daemon adapters; each produces a StartRunCommand]
  task.Execute            app/api/tasks.py:503 -> TaskExecutionService (task_execution_service.py:145)
  task auto-enqueue/trigger app/api/tasks.py:154,194 -> task_executor.enqueue_task_runtime
  trigger/schedule/heartbeat  app/services/trigger_runtime/, scheduler.py, heartbeat_runtime.py
  chat/websocket/channel  app/api/websocket.py, chat_intake.py
      │
      ▼
[INTAKE — caller-transaction, no commit, no graph]
  RuntimeCommandIntake.start_run   agent_runtime/adapter.py:245
      │  rollout gate (config.py:134 decide_runtime_v2, fail-closed)
      │  model pin + model_turn_limit (adapter.py:96,150)
      ▼
  register_run_with_start         agent_runtime/persistence.py:321
      │  writes in caller tx (NOT committed here):
      │    AgentRun (models/agent_run.py)
      │    AgentRunCommand(command_type=start, status=pending)  (models/agent_run_command.py)
      │    AgentRunEvent(run_created)                          (models/agent_run_event.py)
      │  idempotent source-retry via uq_agent_runs_source_execution (persistence.py:253)
      ▼
  returns RunHandle (created=…) to the ingress; the INGRESS owns the commit
      │
      ▼
[COMMAND INBOX — AgentRunCommand rows, durable, pending/claimed/applied/rejected]
      │
      ▼
[WORKER — one claim at a time, advisory-locked per runtime thread]
  RuntimeCommandDaemon.run   agent_runtime/worker_service.py:390
      → RuntimeCommandWorker.run_once   agent_runtime/command_worker.py:928
          1. _claim()   -> claim_next_command  persistence.py:635 (skip-locked, lane-aware)
          2. _load_run()                                   command_worker.py:379
          3. run_with_thread_lock(thread_id)  thread_lock.py:60 (pg advisory lock)
          4. _process_locked()                            command_worker.py:721
               read checkpoint -> classify -> execute
      │
      ▼
[GRAPH — deterministic LangGraph]
  build_agent_runtime_graph  agent_runtime/graph.py:241
      nodes: control_guard -> {compact, model, tool, verify, wait, terminal}
      routing: route_after_control (graph.py:229) from authoritative lifecycle only
      router: RuntimeNodeExecutorRouter (planning.py:650) picks agent vs planning executor
      │
      ▼
[MODEL STEP]  RuntimeModelStepService.complete_once  agent_runtime/model_step_service.py
      -> complete_llm_once (services/llm/single_step.py) -> provider factory (llm/client.py)
      │  provider-independent: LLMModel row drives AnthropicClient / OpenAI* / Gemini
      ▼
[TOOL STEP]   RuntimeToolStepService.execute_pending  agent_runtime/tool_step_service.py
      -> reserve AgentToolExecution (lease owner, side-effect class) tool_execution.py
      -> execute handler w/ lease renewal + cancel token + deadline
      -> _settle_outcome -> normalize -> tool_result_store archive + tool message
      │  async tools park in waiting_external; AsyncToolPollScheduler polls
      ▼
[CHECKPOINT SETTLEMENT — terminal]
      graph terminal node guards lifecycle (graph.py:173-175)
      mark_command_applied (persistence.py:763) stores receipt vs checkpoint
      RuntimeCheckpointSideEffects terminal-handler chain (worker_service.py:304-319):
          TaskRuntimeCompletionHandler (task_completion.py:56)   -> Task.status + TaskLog
          SessionContextCompletionHandler / Trigger / Heartbeat / Onboarding /
          A2A / SchedulingLaneCompletionHandler (scheduling_lane.py:26)
      events published ONLY after checkpoint commit (checkpoint_side_effects.py)
      channel delivery: ChannelDeliveryWorker (worker_service.py:444)
```

**Key spine invariants (all FACT):**
- The spine is **one**. There is no second graph, no second command inbox, no
  second claim loop. Planning is a *second topology* on the **same** Checkpointer,
  selected per-run by `system_role == "group_planning"`
  (`RuntimeGraphRegistry.resolve`, `langgraph_driver.py:72`;
  `build_runtime_worker_components`, `worker_service.py:268-285`).
- Intake never commits and never touches a graph
  (`adapter.py:44` docstring; `persistence.py:330` "in the caller's transaction").
- Execution state is in **checkpoints**, not in a second lifecycle table
  (`models/agent_run.py:28` "execution state stays in checkpoints").

---

## 2. Ingress / scheduling / supervision integration points (Q1, Q4, Q5)

### 2.1 Scheduled cron -> Runtime (a REAL scheduling engine exists)

`app/services/scheduler.py` is a live asyncio cron engine for `AgentSchedule`
(`models/schedule.py`):

- `start_scheduler` (scheduler.py:120) ticks every 30s from FastAPI startup
  (wired in `main.py:310` under role `worker`).
- `_tick` (scheduler.py:27) selects due rows
  `AgentSchedule.is_enabled AND next_run_at <= now`
  with `with_for_update(skip_locked=True)` (scheduler.py:47) — a distributed
  claim across worker processes.
- Each due occurrence calls `enqueue_schedule_runtime`
  (`heartbeat_runtime.py:203`), which mints a stable
  `source_execution_id = f"schedule:{schedule_id}:{occurrence_id}"`
  (heartbeat_runtime.py:229) and registers through the **same**
  `RuntimeCommandIntake.start_run` spine.
- Re-entrancy guard: the occurrence id is `uuid.uuid5(schedule_id,
  "schedule-occurrence:{timestamp}")` (heartbeat_runtime.py:60), so a re-fire of
  the same slot is idempotent against the existing Run/Command.

**FACT:** cron scheduling is real, consumer-backed, and plugs into the single
spine — it does not build a second runtime.

### 2.2 Trigger daemon -> Runtime (a second, independent scheduler)

`app/services/trigger_daemon.py` (`main.py:309`) ticks every 15s:
- evaluates enabled `AgentTrigger` rows (`models/trigger.py`: cron / once /
  interval / poll / on_message), auto-disables expired / rate-limited triggers
  (trigger_daemon.py:149-170), and calls `enqueue_due_trigger`
  (`trigger_runtime/dispatch.py:36`) -> `enqueue_trigger_execution`
  (`trigger_runtime/queue.py:74`) -> `enqueue_trigger_runtime`
  (`trigger_runtime/intake.py:241`) -> the spine.
- `TriggerExecution` (`models/trigger_execution.py`) is a durable claim queue
  with `lease_owner` / `lease_expires_at` and a `UNIQUE(trigger_id,
  idempotency_key)` guard.
- **OBSERVATION:** `claim_pending_trigger_executions`
  (`trigger_runtime/executions.py:19`) and `mark_base_triggers_fired`
  (`executions.py:101`) are defined and re-exported (`__init__.py:7-11`) but
  have **no production caller** found by grep in this tree.
  -> **UNKNOWN:** whether a distributed trigger-claim loop is wired elsewhere
  (e.g. a separate worker image role) or is a reserved owner. Resolving action:
  `grep -rn claim_pending_trigger_executions` across `deploy/`, `helm/`,
  `backend/app/scripts/` and the live process manifest.

### 2.3 Scheduling lanes (serialization, not scheduling)

`AgentRun` carries a **scheduling lane** primitive
(`scheduling_lane_key` / `lane_held` / `lane_claimed_at`,
`models/agent_run.py:140-148`, constrained by
`ck_agent_runs_lane_holder_key` and `ck_agent_runs_lane_position`):
- **Hold:** `_acquire_start_lane` (`persistence.py:599`) sets `lane_held=True`
  when a start command is claimed.
- **Release:** `SchedulingLaneCompletionHandler`
  (`scheduling_lane.py:26`) clears it **only** from an authoritative terminal
  checkpoint.
- **Ordering:** the claim statement (`persistence.py:524`) refuses to start a
  lane Run while another Run holds the lane or an earlier-positioned start is
  unfinished — this serializes **group-mention** lanes
  (`f"group_mention:{tenant}:{agent}"`, `planning_scheduler.py:330`).

**FACT:** this is a per-agent/per-group **serialization** mechanism, not a
time scheduler. It is the closest existing analog to "lane concurrency control"
that Phase 3 can reuse for task-level ordering.

### 2.4 Planning -> entry Runs (a REAL planning engine, group-scoped)

The **existing** planning capability is the `group_planning` orchestration run:
- Intake: a `run_kind="orchestration"` Run with `system_role="group_planning"`,
  `agent_id=NULL`, pinned model, no Agent turn limit
  (`models/agent_run.py:64-74` `ck_agent_runs_orchestration_identity`).
- Graph topology: `<name>_group_planning`
  (`graph.py:96` `RuntimeGraphIdentity.planning_from_settings`).
- Node executor: `PlanningRuntimeNodeExecutor` (`planning.py:530`) — produces
  one immutable v2 plan (`goal`/`plan_prompt`/`entry_steps`) and terminates.
- Scheduling: `PlanningCheckpointScheduler`
  (`planning_scheduler.py:363`) runs as a **post-checkpoint handler**
  (worker_service.py:306); on a completed planning checkpoint it creates the
  **entry Agent Runs** through `RuntimeCommandIntake.start_run`
  (`planning_scheduler.py:531-543`), each with a per-agent `scheduling_lane_key`
  (`planning_scheduler.py:330`).

**FACT:** a Planning->Assignment->Execution capability **already exists**, but
it is **group-mention scoped** (candidate Agents come from a Group trigger
message, `planning_scheduler.py:81`), not Project-scoped. This is the single
most important integration finding for Phase 3 (see §5 / §6).

### 2.5 Supervision tasks (Q1: real consumers beyond tests?)

`Task.type` is a closed 2-value enum `{todo, supervision}`
(`models/task.py:53`). Supervision-specific columns exist:
`supervision_target_user_id` / `supervision_target_name` /
`supervision_channel` / `remind_schedule` (`models/task.py:72-76`).

What actually happens with them:
- **Goal text only.** `task_executor._task_goal`
  (`task_executor.py:30`) folds `supervision_target_name` into the Run goal
  string and appends a fixed "contact the target, report back" instruction
  (`task_executor.py:37-40`). No field drives a scheduler.
- **Supervision keying.** A supervision Run's idempotency key is
  `f"task:{task.id}:supervision:{occurrence_id}"`
  (`task_executor.py:85-87`) — an occurrence-id parameter is accepted but is
  **only ever supplied by the manual trigger path** (`execute_task`,
  `task_executor.py:196` mints `execution_id=uuid.uuid4()`).
- **Settlement.** `task_completion.py:136-144`: a *completed* supervision Run
  sets `Task.status="pending"` (NOT `done`) — i.e. supervision is
  "re-runnable", but nothing re-runs it automatically.
- **Graph exclusion.** `task_graph_service.py:269` rejects dependency edges on
  supervision tasks (`GRAPH_SUPERVISION_NOT_ALLOWED`); `ensure_ready` returns
  `[]` for non-todo (`task_graph_service.py:410`).
- **Schemas.** `remind_schedule` / `supervision_channel` round-trip through
  `TaskCreate`/`TaskOut`/`TaskUpdate` (`schemas/schemas.py:354-408`) and are
  written at create (`api/tasks.py:112-114`) — **stored, never consumed**.

**OBSERVATION / FACT:** the supervision fields are **storage-only**. There is
no cron / interval consumer of `remind_schedule`. The only runtime consumers of
"supervision" are: (a) the manual `/trigger` endpoint (`api/tasks.py:301`),
(b) the goal-text builder, (c) settlement, and (d) tests
(`backend/tests/test_task_runtime_intake.py`,
`test_agent_runtime_chat_intake.py`). This matches the pre-existing audit
`docs/PROJECT_TAKEOVER_MODE.md:110` ("supervision 调度引擎 | 周期/监督任务只存
不跑 | MISSING（字段在，无消费者）").

**Answer to Q1:** supervision has **no production scheduler consumer** beyond
tests + the manual trigger. `remind_schedule` is dead weight at the moment.
The cron engine that *would* drive it is `scheduler.py` (currently bound to
`AgentSchedule`, not `Task`), and the lane primitive in `models/agent_run.py`
is the only serialization tool available.

---

## 3. Team / organizational abstractions that exist (Q5 background)

- **`Group` / `GroupMember`** (`models/group.py`) — a tenant-owned native group
  chat; members carry `role IN ('manager','member')`
  (`models/group.py:64`). This is the **only** existing "team" primitive, and
  it is scoped to a Group chat, not to a Project.
- **`AgentRelationship` / `AgentAgentRelationship`** (`models/org.py:66,84`) —
  free-text `relation` strings (default `"collaborator"`) linking an Agent to
  an org member / another Agent. Soft, no execution semantics.
- **A2A delegation** (`agent_runtime/a2a_runtime.py`) — Agent-to-Agent
  `notify` / `consult` / `task_delegate` behind Runtime tool receipts; this is
  the real multi-Agent "hand-off" channel and is how the group planning engine
  moves work forward.
- **`group_handoff.py`** — terminal public-mention hand-off for native Group
  Runs (stages participant ids via the `at` tool, freezes a delivery intent).
- **OrgDepartment / OrgMember** (`models/org.py:13,35`) — Feishu-synced org
  structure, soft-coupled (provider_id has no FK). No execution semantics.

**OBSERVATION:** there is **no Project-scoped team/squad model** and **no
role->task-assignment structure**. The only org data is chat-scoped (Group) or
identity-provider-scoped (Feishu). A `Squad`/`Team` would be a genuinely new
construct; nothing existing to reuse at the Project level. (This is a design
input for `t_0739e600`, not something Phase 3 can inherit.)

---

## 4. Ownership boundaries today (Q2)

| Concern | Owner (file) | Boundary note |
|---|---|---|
| **Task row + type/status/priority + supervision cols + provenance** | `models/task.py` | `Task` is `__tenant_scoped__=True`. `status` closed 3-valued; `type` closed 2-valued. Phase 2D added 5 provenance cols + `task_dependencies` edges. |
| **Task Graph semantics (edges, blocked/ready, execution gate)** | `services/task_graph_service.py` | Owns validation order, cycle gate, `ensure_ready`. DAO (`dao/task_dao.py`, `task_dependency_dao`) is bounded reads only. |
| **Analysis->Task mapping** | `services/task_decomposition_service.py` | The single boundary between Analysis and Task; explicit convert, never executes (G4), flat (no edges it creates). |
| **Task->Run bridge (gate P1–P8, attempt keying R1–R5, audit, projection)** | `services/task_execution_service.py` | Called only by the Execute transport (`api/tasks.py:502`). Everything below `enqueue_task_runtime` is existing/untouched. |
| **Runtime intake (start/resume/cancel)** | `agent_runtime/adapter.py` | Caller-tx, no commit, no graph, no ORM pass-through. |
| **Run/Command/Event registry + claim + lane + idempotency** | `agent_runtime/persistence.py` + `models/agent_run*.py` | DB ownership: `agent_runs`, `agent_run_commands`, `agent_run_events`. |
| **Graph topologies (agent + planning)** | `agent_runtime/graph.py` | One Checkpointer, two compiled topologies, selected per-run. |
| **Planning (group scope)** | `agent_runtime/planning.py` + `planning_scheduler.py` | Plan model + post-checkpoint entry-Run scheduling, group-mention only. |
| **Scheduling (cron)** | `services/scheduler.py` | Bound to `AgentSchedule`, not `Task`. |
| **Trigger engine** | `services/trigger_daemon.py` + `trigger_runtime/` | Bound to `AgentTrigger`/`TriggerExecution`. |
| **API / transport** | `api/tasks.py`, `api/schedules.py`, `api/triggers.py` | Pure adapters; closed codes live in the services. |
| **File/workspace confinement** | `services/workspace_paths.py`, `project_materialization_service.py` | Per-agent storage-key namespace; 403 traversal guard; 3-layer locks (per Phase 2F preflight commit `eb8cfc0e`). |
| **DB writes** | `app/dao/*` (TenantScopedBaseDAO) | No ORM writes in services/API; tenant filter via `do_orm_execute` in `dao/base.py:139`. |

**FACT:** the Task->Run->Result->Settlement chain is fully partitioned:
Task/Graph/Assignment live in the *product* services; everything from
`RuntimeCommandIntake` down is the *Runtime* (`agent_runtime/`). The Phase 2E
spec (2E §2 / root §三) explicitly froze this boundary: TaskExecutionService
"only provides the validated input payload."

---

## 5. Tenant & workspace isolation enforcement at the runtime level (Q3)

### 5.1 Tenant isolation — ENFORCED at runtime, mechanically

- **ORM-level filter.** `_inject_tenant_scope`
  (`app/dao/base.py:139`) is a SQLAlchemy `do_orm_execute` listener that, when a
  `tenant_context` is active, attaches a `with_loader_criteria(model,
  tenant_id == active)` to **every** SELECT of a tenant-owned model
  (`_is_tenant_scoped_model`, `dao/base.py:125` — non-null `tenant_id` column or
  `__tenant_scoped__=True`). This is a hard runtime gate, not a convention.
- **Explicit context binding.** `tenant_context(tenant_id)` (`dao/base.py:178`)
  is a `ContextVar`; background/daemon code (scheduler, trigger daemon, worker)
  wraps reads in it.
- **Intake re-assertion.** `RuntimeCommandIntake._get_run`
  (`adapter.py:79`) re-checks `run.tenant_id == requested tenant` and the
  command/run scope match; `command_scope_mismatch` is fail-closed
  (`persistence.py` via the claim path).
- **Service gate.** `TaskExecutionService.execute` re-asserts
  `verify_tenant_scope(agent.tenant_id, caller_tenant)` at entry
  (`task_execution_service.py:170`), so an unscoped/foreign caller fails
  closed *before* any gate or enqueue (Phase 2E §9 / G1).

**FACT:** tenant isolation is enforced at the runtime level (DAO ORM filter +
intake re-assert + explicit `tenant_context`), not merely at the API layer.

### 5.2 Workspace isolation — ENFORCED at the storage boundary

- **Per-agent storage-key namespace.** `project_materialization_service.py`
  materializes into per-agent `projects/` + `.materialize-tmp/` confinement;
  Phase 2F preflight (commit `eb8cfc0e`) re-verified storage-key namespace
  isolation, a 403 path-traversal guard, a per-Run temp-identity guard, and
  3-layer locks.
- **Path confinement.** `workspace_paths.resolve_path_within_root`
  (`workspace_paths.py:21`) rejects absolute paths and any `..` escape via
  `target.relative_to(root)` (raises `WorkspacePathError`).
- **Tenant-scoped mutation locks.** `workspace_locking._lock_key`
  (`workspace_locking.py:35`) prefixes `tenant:{id}:workspace-lock:{agent}:{path}`
  when a tenant is supplied — Redis `SET NX` with owner-token release.
- **Terminal cleanup.** `command_worker.py:870-883` reconciles and cleans
  terminal-Run workspace candidates on completion.

**FACT:** workspace isolation is enforced at the storage/lock boundary
(tenant-prefixed keys + path confinement + Redis locks), verified at Phase 2F
preflight. It is NOT enforced inside the LangGraph model step — the model sees
only tool-level, lease-fenced file operations.

**UNKNOWN:** per-Run temp-identity guard detail is documented in the Phase 2F
preflight commit, not re-read here; treat as Phase-2F-verified FACT only.

---

## 6. Concurrency controls (Q4)

- **Command claim serialization.** `claim_next_command`
  (`persistence.py:635`) picks one pending/claimable `AgentRunCommand`
  `ORDER BY created_at, id` with `with_for_update(skip_locked=True)`
  (`persistence.py:594`) — safe multi-worker claim, no lost updates.
- **Claim lifecycle.** `claimed_by` / `claim_expires_at` /
  `renew_command_claim` / `release_command_claim` /
  `begin_command_attempt` (persistence.py:724,935,954) with
  `AGENT_RUNTIME_COMMAND_CLAIM_TTL_SECONDS` + renewal
  (`worker_service.py:344-352`). A dead worker's claim expires and is reclaimed.
- **Per-thread advisory lock.** `run_with_thread_lock`
  (`thread_lock.py:60`) — `pg_try_advisory_lock` keyed by the Run thread id
  (blake2b, `thread_lock_key`, thread_lock.py:40) serializes all invocations of
  one Run across all workers.
- **Worker concurrency bound.** `AGENT_RUNTIME_COMMAND_CONCURRENCY`
  (config.py:145, default 10) spawns N parallel command daemons
  (`worker_service.py:617-623`); `max_attempts` quarantines poison commands
  (`persistence.py:732`).
- **Scheduling-lane hold.** §2.3 — one active holder per lane, checkpoint-derived
  release.
- **Task graph edge serialization.** `_lock_project_graph`
  (`task_graph_service.py:174`) — `pg_advisory_xact_lock` on
  `task_graph:{tenant}:{project}` serializes the cycle-check read-then-write
  (Phase 2D §8 R1).
- **Analysis dedup guard.** `UNIQUE(project_id, revision_sha)` on
  `analysis_runs` (`models/analysis.py:97`) — racing same-revision analyses
  collapse to one row.
- **Workspace mutation locks.** §5.2 Redis per-(tenant,agent,path) locks.
- **Trigger queue lease.** `TriggerExecution.lease_owner/lease_expires_at`
  + `skip_locked` claim (`executions.py:44-61`) — 5-minute distributed lease.
- **Scheduler claim.** `with_for_update(skip_locked=True)` on due schedules
  (scheduler.py:47).

**FACT:** concurrency is controlled by **Postgres-level** primitives
(advisory locks, `skip_locked` claims, unique constraints) plus one
**Redis-level** primitive (workspace locks). There is **no in-process**
lock/semaphore guarding Run execution — everything is durable-DB-fenced.

**OBSERVATION (gap):** the Task/Assignment layer today has **no**
per-Project or per-Task concurrency ceiling. Two ready Tasks in the same
Project can be enqueued in parallel with no lane key and no cap
(`task_execution_service.py` mints a new Run per Execute; no
`scheduling_lane_key` is set on task Runs — `task_executor.py:114-143`
omits it). Only group-mention planning Runs carry lane keys
(`planning_scheduler.py:330`).

---

## 7. Where a new Planning->Task Graph->Assignment->Execution plugs in
   WITHOUT creating a second Runtime (Q5)

The design constraint (Phase 3 root brief, stage 5): **reuse the Phase-2F-verified
spine; do not build a second Runtime.** The trace shows exactly one place a
new Project-Planning layer can hang on, and exactly where it must NOT fork:

### 7.1 The integration surface (plug-in points)

1. **A new planning *ingress* that emits Run/Command, not a new executor.**
   The existing `group_planning` orchestration already proves the pattern:
   a planning Run (topology `<name>_group_planning`, `planning.py`) produces an
   immutable plan, and `PlanningCheckpointScheduler` turns it into entry Runs
   through the **same** `RuntimeCommandIntake.start_run`. A Project Planner
   would be a **new `system_role` + new graph topology** in
   `RuntimeGraphRegistry` (langgraph_driver.py:46) that ends by writing Task
   rows / assignment facts and enqueuing Runs — it does **not** own a graph
   runtime, a checkpointer, a worker, or a command inbox.
   -> Reuse `build_agent_runtime_graph` + a new identity; do NOT add a second
   `build_runtime_worker_components` / daemon.

2. **Task Graph stays the dependency authority.** Planning's output must land
   as `Task` rows + `task_dependencies` edges through
   `task_graph_service.add_edge/bulk_add_edges` (task_graph_service.py:204,220)
   and the `ensure_ready` gate (task_graph_service.py:403). Do NOT invent a
   second edge table or a second readiness concept; `task_dependencies`
   (`models/task.py:138`) is the one owner, and `GRAPH_*` codes are closed.

3. **Assignment = the existing `Task.agent_id` binding.** Phase 2E settled
   "assignment = `Task.agent_id` FK" (2E §1.1, `api/tasks.py:509` docstring;
   no agent id in the body). A Squad/Team model may *compute* which
   `agent_id` each Task gets, but the **authoritative assignment fact stays
   `Task.agent_id`** — do not add a parallel `TaskAssignment` table that
   diverges from it.

4. **Execution = the existing Task->Run bridge.** Enqueue via
   `TaskExecutionService.execute` (task_execution_service.py:145) or
   `enqueue_task_runtime` (task_executor.py:44). The gate P1–P8 already
   includes P2 "Project execution-allowed set"
   (`PROJECT_EXECUTABLE_STATUSES`, task_execution_service.py:66) — Planning
   runs at `PENDING_CONFIRMATION`/`EXECUTING` without touching the gate.

5. **Scheduling of plan-ordered work = reuse lane + cron primitives.** If a
   plan has ordered / parallel / resource-shared sub-tasks, the *only*
   existing serialization tool is the `scheduling_lane_key`
   (`models/agent_run.py:140`). Phase 3 must either (a) generalize the lane key
   off the group-mention pattern, or (b) add a project/task-scoped lane key on
   `AgentRun`. Do NOT add a second claim loop; `claim_next_command`
   (persistence.py:635) already honors lane position ordering
   (persistence.py:552-573).

6. **Supervision, if revived, = a scheduler binding, not a new runtime.** The
   cron engine `scheduler.py` is the natural owner; a supervision reminder
   would be a new `AgentSchedule`-like row (or a generalization) whose
   `_tick` calls an intake that mints a `task:{id}:supervision:{occurrence}`
   Run — reusing `heartbeat_runtime`'s occurrence-id + `RuntimeCommandIntake`
   pattern. The supervision *columns already exist* (`models/task.py:72-76`);
   only the consumer is missing.

### 7.2 Integration points that must NOT be duplicated (explicit)

- **Do NOT add a second command inbox.** `agent_run_commands`
  (persistence.py) is the one durable inbox; all new enqueuing goes through
  `RuntimeCommandIntake` / `register_run_with_start`.
- **Do NOT add a second LangGraph runtime / checkpointer / worker daemon.**
  One `build_runtime_worker_components` + `running_runtime_worker_context`
  (worker_service.py). New planning = new topology on the shared Checkpointer
  (as `group_planning` already does).
- **Do NOT add a second Task dependency / readiness model.** `task_dependencies`
  + `task_graph_service.ensure_ready` are the authority.
- **Do NOT add a second assignment fact.** `Task.agent_id` is it.
- **Do NOT add a second scheduling/claim loop.** Reuse `claim_next_command`
  lane ordering + the cron `skip_locked` claim; generalize the lane key if
  needed, don't fork the claimer.
- **Do NOT add a second execution-settlement path.** Terminal settlement is the
  `RuntimeCheckpointSideEffects` handler chain
  (worker_service.py:303-323); a plan's tasks settle via
  `TaskRuntimeCompletionHandler` like any task Run.
- **Do NOT add a second Runtime source_type.** `source_type` is a closed 5-set
  (`contracts.py:13` `chat/trigger/task/a2a/heartbeat`;
  `models/agent_run.py:33` CHECK). A project-plan Run is a `task` (or, for the
  planner itself, an `orchestration` `system_role`) — extend within the closed
  set or amend it deliberately with a migration, do not scatter strings.

---

## 8. Gap list (what is missing for Phase 3)

| # | Gap | Evidence | Disposition |
|---|---|---|---|
| G1 | **Supervision scheduler consumer** — `remind_schedule` / supervision cols stored, never driven | task.py:72-76; task_executor.py:37-40; PROJECT_TAKEOVER_MODE.md:110 | NEW consumer: a cron binding reusing `scheduler.py` + occurrence-id intake. Field already exists. |
| G2 | **No Project-scoped planning engine.** Existing planning is group-mention scoped (`planning_scheduler.py:81`), candidate Agents from a Group trigger | planning_scheduler.py:81,330 | NEW `system_role` + topology feeding Task rows; reuse spine, no second runtime. |
| G3 | **No Project/team/squad model.** Only chat-scoped `Group`/`GroupMember` and Feishu `Org*` | models/group.py:64; models/org.py | DESIGN (t_0739e600). Do not reuse Group for Project; new minimal construct. |
| G4 | **No Task/Project concurrency cap or lane key on task Runs.** Task Runs omit `scheduling_lane_key` | task_executor.py:114-143 (no lane field) | Generalize the lane primitive (`agent_run.py:140`) to project/task scope, or add a cap. |
| G5 | **Trigger claim loop possibly unwired.** `claim_pending_trigger_executions` has no production caller in-tree | executions.py:19; grep tree-wide | UNKNOWN — verify in `deploy/`, `helm/`, `scripts/`; do not assume it runs. |
| G6 | **No per-Task dependency cap in the execution gate beyond graph bounds.** `MAX_PROJECT_EDGES=1000` / `MAX_BATCH_EDGES=100` are graph-authoring bounds, not execution-parallelism | task_graph_service.py:79-80 | Design decision for Phase 3 (how much of a plan runs in parallel). |
| G7 | **Planning output -> Task mapping is undefined.** `group_planning` emits entry Runs only; no ProjectPlan/WorkPackage entity yet | planning.py:348; no ProjectPlan model | DESIGN (t_0739e600) defines the minimal Planning domain; implementation maps it onto Task + edges. |
| G8 | **`TaskCompletionGate` fail-open risk** (inherited, not new) | verification.py:637-645 (per AGENT_RUN_EXECUTION_CHAIN.md:25) | Not a Phase 3 new gap; re-verify if review-independence is added. |

---

## 9. Direct answers to the task's five questions

1. **Does supervision have real consumers beyond tests?**
   **No.** Storage-only. `remind_schedule` / supervision columns are written and
   read for goal-text + settlement, but no scheduler consumes them. Consumers:
   manual `/trigger` (api/tasks.py:301), goal builder (task_executor.py:37),
   settlement (task_completion.py:136), and tests. (G1.)

2. **What file/DB/API ownership boundaries exist today?**
   §4 table. Task/Graph/Assignment = product services; Runtime =
   `agent_runtime/` (intake, registry, worker, graph, tool, settlement). DB
   tables: `tasks`/`task_dependencies`/`task_logs` (product),
   `agent_runs`/`agent_run_commands`/`agent_run_events`/
   `agent_tool_executions` (Runtime), `agent_schedules`/`agent_triggers`/
   `trigger_executions` (scheduling). API handlers are pure transport adapters.

3. **Is tenant + workspace isolation enforced at the runtime level?**
   **Yes for tenant** (ORM-level `do_orm_execute` filter + intake re-assert +
   `tenant_context`), **yes for workspace** (per-agent storage namespace +
   path confinement + tenant-prefixed Redis locks), both re-verified at Phase
   2F preflight. See §5.

4. **What concurrency controls exist?**
   Postgres: per-thread advisory lock (Run execution), `skip_locked` command
   claim (one command/worker at a time), project graph advisory xact lock,
   `UNIQUE(project_id, revision_sha)`, lane hold/release. Redis: per-(tenant,
   agent, path) workspace locks. Bound: `AGENT_RUNTIME_COMMAND_CONCURRENCY`.
   See §6. **Gap:** no Task/Project-level concurrency cap (G4/G6).

5. **Where does Planning->Task Graph->Assignment->Execution plug in without a
   second Runtime?**
   §7.1 (six plug-in points) + §7.2 (seven "must NOT duplicate" rules). A new
   Project Planner is a new `system_role`/topology that emits Tasks + edges +
   `Task.agent_id` and enqueues through the existing
   `RuntimeCommandIntake`; it reuses the one Checkpointer, one worker, one
   claimer, one lane primitive, one settlement chain.

---

## 10. Verification notes

- Read-only audit; no code, migration, or config changed on this card.
- Trace re-derived from the current tree (worktree `wt/t_5487441c`, main
  `44651184`). Source-line citations throughout; no claim rests on README or
  task prose.
- Cross-checked against the frozen Phase 2F spine docs
  (`docs/AGENT_RUN_EXECUTION_CHAIN.md`,
  `docs/architecture/phase2c_2f_evolution.md`,
  `docs/PHASE_2E_EXECUTION_CHAIN_VERIFICATION_T84836B4B.md`) — consistent.
- **UNKNOWNs left open** (to be resolved by a separate probe, not assumed):
  G5 (trigger claim-loop wiring in deploy/helm/scripts), and the exact
  per-Run temp-identity guard mechanics (Phase 2F preflight, commit `eb8cfc0e`).
