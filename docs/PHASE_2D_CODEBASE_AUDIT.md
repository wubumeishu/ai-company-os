# Phase 2D — Source Code Audit: Project / Analysis / Task / Agent Domains

**Baseline**: `main @ e6237916` (product baseline `fc233fc1`)
**Method**: Strict source-code audit. No design documents, no README inference.
Unknown items marked **UNKNOWN**.

---

## 1. Project Domain

**Model**: `backend/app/models/project.py`

| Field | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `name` | String(200) | |
| `description` | Text nullable | |
| `goal` | Text nullable | |
| `status` | Enum (10 values) | RECEIVED, SOURCES_OK, INITIALIZED, ANALYZING, PENDING_CONFIRMATION, EXECUTING, BLOCKED, COMPLETED, ARCHIVED, REJECTED |
| `created_by` | UUID FK→users | |
| `tenant_id` | UUID FK→tenants | non-nullable |
| `rejection_reason` | String(50) nullable | Phase 2B-2 |
| `rejection_detail` | Text nullable | Phase 2B-2 |
| `status_changed_at` | DateTime nullable | |

**Key observation**: The enum declares 10 values, but **only 4 are actually reachable in code**:
- `RECEIVED` → created by `project_intake_service.create_intake`
- `SOURCES_OK` → `project_intake_service.validate_sources` (intermediate)
- `INITIALIZED` → `project_intake_service.validate_sources` (all-pass)
- `REJECTED` → `project_intake_service.validate_sources` (permanent rejection)
- `ANALYZING` → `analysis_service.launch` (via `project_dao.transition`)

The remaining 5 values (`PENDING_CONFIRMATION`, `EXECUTING`, `BLOCKED`, `COMPLETED`, `ARCHIVED`) are **declared in the enum but never written by any service or DAO**. They are future/placeholder states with no current code path.

**Intake transitions** (`intake_security.py:575-602`):
```python
INTAKE_TRANSITIONS = {
    "RECEIVED": frozenset({"SOURCES_OK", "REJECTED"}),
    "SOURCES_OK": frozenset({"INITIALIZED", "REJECTED"}),
}
TERMINAL_INTAKE_STATUSES = frozenset({"INITIALIZED", "REJECTED"})
```

**Repository domain** (same file): `source_type` enum (7 values), `locator` JSON, `verified` bool, `pending_verifier` + `retry_count` for transient validation holds.

**Migrations**: f066 (create projects + repositories), f067 (add rejection fields).

---

## 2. Analysis Domain

**Model**: `backend/app/models/analysis.py` (Phase 2C, migration f068)

### 2.1 AnalysisRun

| Field | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `project_id` | UUID FK→projects CASCADE | |
| `agent_id` | UUID FK→agents SET NULL nullable | |
| `revision_sha` | String(64) | indexed; typed revision carrier |
| `requested_ref` | String(200) nullable | |
| `resolved_at` | DateTime nullable | |
| `status` | Enum (AN_OPEN, AN_COMPLETED, AN_FAILED) | closed result-code, NOT workflow SM |
| `started_at` / `finished_at` | DateTime | |
| `tenant_id` | UUID non-nullable | |

Invariant: `UNIQUE(project_id, revision_sha)` — append-only versioning.

### 2.2 AnalysisFinding

| Field | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `analysis_run_id` | UUID FK→analysis_runs CASCADE | findings die with their run |
| `severity` | Enum (INFO, WARN, HIGH, CRITICAL) | |
| `category` | Enum (SECURITY, RISK, TECH_DEBT, OPEN_QUESTION, FACT) | |
| `tag` | Enum (FACT, OBSERVATION, INFERENCE, UNKNOWN) | provenance tag |
| `summary` | Text | |
| `evidence` | JSON nullable | path:line anchors + source-card provenance |
| `tenant_id` | UUID non-nullable | |

### 2.3 ProjectKnowledge

| Field | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `project_id` | UUID FK→projects CASCADE | |
| `subject` | String(200) indexed | e.g. "backend framework" |
| `statement` | Text | e.g. "backend uses FastAPI" |
| `source_analysis_run_id` | UUID FK→analysis_runs SET NULL nullable | provenance copied, not owned |
| `status` | Enum (PROPOSED, CONFIRMED, SUPERSEDED) | |
| `tenant_id` | UUID non-nullable | |

### 2.4 Service & DAO

`AnalysisService` (`app/services/analysis_service.py`) owns 5 paths:
- `launch` — open AN_OPEN run + set project→ANALYZING
- `record_findings` — write ≥1 findings, close run as AN_COMPLETED
- `promote_finding` — insert CONFIRMED ProjectKnowledge row
- `read_current_and_history` — current run + findings + prior runs
- `knowledge` — all durable knowledge rows

**PENDING_CONFIRMATION is INERT**: No code path in `analysis_service` sets project status to PENDING_CONFIRMATION. The confirmation UI does not exist. Promotion happens through `promote_finding` while the project remains in ANALYZING.

**Static-only boundary**: The analysis path NEVER executes the target project's code. No subprocess, no interpreter, no build, no pip/npm install.

### 2.5 API

`app/api/projects.py` exposes:
- `POST /projects/{id}/repositories/{repo_id}/analyze/{agent_id}` → launch
- `POST /projects/{id}/analysis/{run_id}/findings` → record findings
- `POST /projects/{id}/analysis/{run_id}/promote` → promote to knowledge
- `GET /projects/{id}/analysis` → read current + history
- `GET /projects/{id}/knowledge` → read knowledge

---

## 3. Task Domain (Current State)

**Model**: `backend/app/models/task.py`

| Field | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID FK→tenants nullable | |
| `agent_id` | UUID FK→agents non-nullable | Task is bound to exactly one Agent |
| `title` | String(500) | |
| `description` | Text nullable | |
| `type` | Enum (todo, supervision) | |
| `status` | Enum (pending, doing, done) | |
| `priority` | Enum (low, medium, high, urgent) | |
| `assignee` | String(50) default "self" | "self" or user_id |
| `created_by` | UUID FK→users non-nullable | |
| `due_date` | DateTime nullable | |
| `supervision_target_user_id` | UUID FK→users nullable | |
| `supervision_target_name` | String(100) nullable | |
| `supervision_channel` | String(50) nullable | |
| `remind_schedule` | String(100) nullable | |
| `completed_at` | DateTime nullable | |

**TaskLog** (`task_logs`): id, task_id FK→tasks, content, created_at.

### 3.1 Task → Run Relationship

`app/services/task_executor.py` implements the handoff:
1. `create_task` API → if type=todo → `enqueue_task_runtime(task, agent)`
2. `enqueue_task_runtime` → `decide_runtime_v2(source_type="task")` → if v2 selected:
   - Creates `AgentRun` via `RuntimeCommandIntake.start_run(StartRunCommand(source_type="task", source_id=str(task.id), source_execution_id=f"task:{task.id}", run_kind="background"))`
   - Sets `task.status = "doing"`
   - Appends TaskLog "🤖 已进入持久化执行队列"
3. If v2 not selected → fallback `execute_task` → logs error "统一 Runtime 当前未对 task 入口启用"

### 3.2 Run → Task Completion

`app/services/agent_runtime/task_completion.py` (`TaskRuntimeCompletionHandler`):
- Triggers on terminal checkpoint (`completed`/`failed`/`cancelled`) for `source_type="task"` runs
- **todo + completed** → `task.status = "done"`, `completed_at = now`
- **supervision + completed** → `task.status = "pending"` (re-arm for next cycle)
- **cancelled/failed** → `task.status = "pending"`
- Appends exactly one terminal TaskLog per checkpoint (idempotent via deterministic receipt_id)

### 3.3 What Task CAN Do Today

- CRUD via API (`/agents/{agent_id}/tasks`)
- Auto-execute todo tasks via Durable Runtime (LangGraph)
- Progress logging (TaskLog)
- Manual trigger (supervision testing)
- Agent tool integration (create/update/delete Task via agent tool)
- Priority + due date tracking
- Tenant-scoped (via `__tenant_scoped__ = True`)

### 3.4 What Task CANNOT Do Today (Gaps for Phase 2D)

- **No `project_id` field** — Task is not linked to a Project
- **No `analysis_run_id` / `finding_id` / `revision_sha` fields** — no provenance to Analysis
- **No `depends_on` / dependency fields** — no Task Graph
- **No `created_reason` / `source_type` field** — cannot answer "why was this Task created?"
- **No Task Proposal / PENDING_CONFIRMATION gate** — Tasks are created directly, no safety boundary
- **No Agent Assignment layer** — Task is directly bound to `agent_id`; no intermediate Assignment entity
- **No Artifact tracking at Task level** — Run has `artifact_refs` in `AgentToolExecution.result_metadata`, but no Task-level artifact table
- **No Review / Verification result persistence** — completion gate verdicts live only in checkpoint state + `AgentRunEvent` (event_type=`verification_updated`), not in a dedicated Task Review table
- **No blocked/ready computation** — no code evaluates whether a Task's dependencies are satisfied

---

## 4. Agent / Delegation / A2A

### 4.1 Agent Model

`Agent` (`agents` table): digital employee with LLM config, autonomy policy (L1/L2/L3), token limits, heartbeat, access mode (company/private/custom). Relationship: `Agent.tasks` (cascade delete).

### 4.2 AgentRun

`AgentRun` (`agent_runs` table): the execution identity.
- `source_type`: chat, trigger, task, a2a, heartbeat
- `run_kind`: foreground, background, delegated, orchestration
- `runtime_type`: legacy, langgraph
- `parent_run_id` / `root_run_id`: delegation tree
- `correlation_id`: A2A correlation
- `origin_agent_id`: who delegated
- `delivery_status`: not_required, pending, delivered, failed

### 4.3 A2A (Agent-to-Agent)

`app/services/agent_runtime/a2a_runtime.py` + `a2a_completion.py`:
- **Modes**: `notify` (one-way), `consult` (request/response), `task_delegate` (delegated execution)
- **Mechanism**: Source Run calls tool `send_message_to_agent` → target AgentRun created (run_kind=`delegated`) → source Run interrupted (waiting) → target Run completes → source Run resumed
- **Correlation**: deterministic UUID5-based correlation IDs
- **Cycle guard**: `AgentCycleGuard.ensure_delegation_allowed` prevents infinite delegation loops
- **Gateway bridge**: OpenClaw ↔ native via `GatewayMessage` table

**Key boundary**: A2A is a **Run-level** delegation mechanism. It is NOT a Task-level delegation. There is no `TaskAssignment` entity, no "assign Task to Agent" intermediate table. The Task is created with `agent_id` already set.

### 4.4 Planning (Group Orchestration)

`app/services/agent_runtime/planning.py`: model contract for group planning (run_kind=`orchestration`, system_role=`group_planning`). This is a **Run-level** planning mechanism, not a Task-level decomposition engine. It produces `entry_steps` (up to 50) with `agent_id` + `instruction` — these are NOT persisted as Task rows.

---

## 5. Workspace

`app/models/workspace.py`:
- `WorkspaceFileRevision`: tracks file revisions in agent workspaces (scope_type: agent/group)
- `WorkspaceEditLock`: short-lived human editing locks

**No binding between Workspace and Task or Project in the model.** Workspace is agent-scoped (or group-scoped), not task-scoped.

---

## 6. Verification / Completion Gate

`app/services/agent_runtime/verification.py`:
- `TaskCompletionGate`: LLM-based semantic completion check (verdict: pass/repair)
- `ToolLedgerRuntimeVerifier`: deterministic checks (pending tools, unsettled executions, artifact/evidence reference validation)
- `CompletionGateRuntimeVerifier`: combines deterministic + semantic

**Where the result lives**: In the checkpoint state (LangGraph) + projected as `AgentRunEvent` (event_type=`verification_updated`). **No dedicated Review/Verification table exists.**

`app/services/agent_runtime/task_completion.py`:
- `TaskRuntimeCompletionHandler`: idempotent Task status update from terminal checkpoint

---

## 7. Existing DB Schema / Migrations

**Total**: 73 migration files in `backend/alembic/versions/`

**Phase 2A–2C relevant** (f-series):
- `f060_tenant_id_backfill` — adds tenant_id to legacy tables
- `f061_default_tenant_timezone` + `f061_enterprise_info_tenant_id`
- `f062_tool_execution_identity` — AgentToolExecution table
- `f063_merge_tool_runtime_heads`
- `f064_backfill_tool_call_tenants`
- `f066_add_project_repo_tables` — creates `projects` + `repositories`
- `f067_intake_rejection_fields` — adds rejection columns to projects
- `f068_analysis_persistence` — creates `analysis_runs`, `analysis_findings`, `project_knowledge`

**Task-related**: The `tasks` + `task_logs` tables originate from the **initial schema** (001). No f-series migration has touched them. The Task model is architecturally isolated from the Project/Analysis domain.

**No migration exists for**: Task dependencies, Task provenance, Task-to-Project linking, Task Review, or Task Graph.

---

## 8. Answers to Phase 2D §三 A–J

### A: 当前 Task 已经能做到什么？

Based on `app/models/task.py`, `app/api/tasks.py`, `app/services/task_executor.py`, `app/services/agent_runtime/task_completion.py`:

1. **Create** a Task (todo or supervision) bound to a specific Agent
2. **Auto-enqueue** into the Durable Runtime as a background AgentRun (source_type="task")
3. **Track progress** via TaskLog entries
4. **Receive terminal status** from the Runtime: todo→done, supervision→pending (re-armed)
5. **Manual trigger** for supervision tasks (testing)
6. **Agent tool integration**: Agents can create/update/delete their own Tasks via the task tool
7. **CRUD API** at `/agents/{agent_id}/tasks`

**Limitations**: Task is a flat, single-Agent record. No graph, no provenance, no project linkage, no review persistence.

### B: 当前 Task 还缺什么，才能接收 Analysis 产生的工作？

Based on source code gaps:

| Missing Capability | Evidence |
|---|---|
| `project_id` FK on Task | `task.py` has no project field |
| `analysis_run_id` / `finding_id` / `revision_sha` provenance | No such columns exist |
| `depends_on` (Task Graph) | No dependency field or table |
| `created_reason` / `source_type` (provenance) | Only `created_by` (user) exists |
| Task Proposal / confirmation gate | No PENDING_CONFIRMATION path for Tasks |
| Agent Assignment boundary | Task directly stores `agent_id`; no intermediate Assignment entity |
| Task-level Artifact tracking | Only in Run's `AgentToolExecution.result_metadata`; no Task table |
| Task Review / Verification persistence | Only in checkpoint + AgentRunEvent; no dedicated table |
| blocked/ready computation | No code evaluates dependency satisfaction |

### C: 当前 Task 是不是已经支持依赖图？

**NO.** The `Task` model has zero dependency fields. There is no `task_dependencies` table, no `depends_on` column, no cycle detection, no blocked/ready computation. The only "graph-like" structures in the codebase are:
- `AgentRun.parent_run_id` / `root_run_id` (delegation tree, Run-level)
- `planning.py` entry_steps (ephemeral, not persisted as Tasks)
- Hermes Kanban board (external, not in this codebase)

### D: 如果不支持，最小必要的 Task Graph 模型是什么？

Based on the codebase's existing patterns (closed enums, tenant scoping, DAO layering):

**Minimum V1 Task Graph**:
- New table `task_dependencies` (or `tasks.depends_on` JSONB array):
  - `task_id` (FK→tasks, CASCADE)
  - `depends_on_task_id` (FK→tasks, CASCADE)
  - `tenant_id`
  - UNIQUE(task_id, depends_on_task_id)
- **Self-dependency prevention**: task_id ≠ depends_on_task_id (DB CHECK or service gate)
- **Cycle prevention**: service-layer DFS/Kahn's algorithm on insert; reject on cycle
- **blocked/ready**: a Task is `ready` when all its `depends_on_task_id` tasks have `status = "done"`
- **No complex DAG platform**: no priority scheduling, no resource constraints, no workflow engine

### E: Analysis 的什么输出可以成为 Task Decomposition 的输入？

Based on `analysis.py` + `analysis_service.py`:

| Analysis Output | Usable as Task Input? | Reasoning |
|---|---|---|
| `AnalysisFinding.category` | ✅ Yes | Closed taxonomy (SECURITY, RISK, TECH_DEBT, OPEN_QUESTION, FACT) determines the kind of work |
| `AnalysisFinding.severity` | ✅ Yes | Drives priority mapping |
| `AnalysisFinding.tag` | ✅ Yes | FACT/OBSERVATION = high confidence; INFERENCE/UNKNOWN = needs human |
| `AnalysisFinding.summary` | ✅ Yes | Becomes Task title/description |
| `AnalysisFinding.evidence` | ✅ Yes | Becomes Task context / acceptance criteria |
| `AnalysisRun.revision_sha` | ✅ Yes | Provenance anchor |
| `ProjectKnowledge` (CONFIRMED) | ✅ Yes | Durable context for Task planning |
| `AnalysisRun.status` | Gate | Only AN_COMPLETED runs produce usable findings |

### F: 哪些 Analysis Findings 可以直接变成 Task？

**No current code path auto-converts findings to Tasks.** This is by design (Phase 2C is analysis-only).

Based on the data model, the findings most likely to be directly actionable (a design decision for Phase 2D, not a current capability):
- `category=TECH_DEBT` + `tag=FACT` + `severity ≥ WARN` — concrete, high-confidence, actionable
- `category=SECURITY` + `tag=FACT` + `severity ≥ HIGH` — urgent, concrete fix

**These are design proposals, not implemented behavior.**

### G: 哪些 Findings 只能进入 Task Planning，不能自动生成 Task？

- `category=OPEN_QUESTION` — requires human answer before a Task can be defined
- `category=RISK` — requires a human decision (accept / mitigate / transfer)
- `tag=INFERENCE` or `tag=UNKNOWN` — low confidence; must be verified before execution
- `severity=INFO` — informational; no action needed
- `category=FACT` (pure observation) — context, not work

These should go into a **Task Planning** step (Planner / human review) before concrete Tasks are created.

### H: Project Knowledge 在 Task Decomposition 中扮演什么角色？

**Current code**: `ProjectKnowledge` is read-only via `GET /projects/{id}/knowledge`. No code injects knowledge into Tasks or Agent Runs.

**Design role (for Phase 2D)**:
- **Context injection**: CONFIRMED knowledge rows provide "known facts" that reduce the scope of analysis-driven Tasks (e.g., "backend uses FastAPI" → don't create a Task to investigate the framework)
- **Constraint enforcement**: Knowledge can carry acceptance criteria for Tasks (e.g., "API must be tenant-scoped" → Task verification criterion)
- **Durable, revision-independent**: Unlike findings (transient, die with run), knowledge persists and applies across revisions

### I: Task 是否需要保留"为什么创建它"的 provenance？

**YES, for Analysis-driven Tasks.** Current Task has only `created_by` (user UUID). To support the traceability chain:

```
Project → Analysis Run → Finding → Task → Run → Artifact → Review
```

A Task created from Analysis MUST carry:
- `project_id` — which Project
- `analysis_run_id` — which Analysis Run
- `finding_id` — which Finding (nullable if manually created from a run)
- `revision_sha` — which Git revision the analysis targeted
- `created_by` — who/what created it (user or system)
- `created_reason` — closed code (e.g., "analysis_finding", "manual", "planner")

For manually created Tasks (not from Analysis), these fields are nullable (no project linkage).

**Justification for each field**:
- `project_id`: scopes the Task to a business context; enables "all Tasks for Project X" queries
- `analysis_run_id`: binds to the specific analysis execution; enables re-analysis invalidation
- `finding_id`: the exact Finding that motivated this Task; nullable for "run-level" Tasks
- `revision_sha`: the commit the analysis was against; essential for "was this Task based on outdated analysis?"
- `created_reason`: distinguishes auto-generated from manual; drives the confirmation gate

### J: 以后能否回答完整追溯链？

**Currently NO.** The chain is broken at multiple points:

| Question | Current State | Gap |
|---|---|---|
| 这个 Task 是为什 produce? | `created_by` (user) + `description` (free text) | No `created_reason` closed code |
| 来自哪个 Project? | **UNKNOWN** — Task has no `project_id` | Missing field |
| 来自哪个 Analysis Run? | **UNKNOWN** — Task has no `analysis_run_id` | Missing field |
| 来自哪个 Finding? | **UNKNOWN** — Task has no `finding_id` | Missing field |
| 使用哪个 revision? | **UNKNOWN** — Task has no `revision_sha` | Missing field |
| 为什么需要这个 Agent? | `Task.agent_id` (direct) | No Assignment rationale |
| 依赖哪些 Task? | **UNKNOWN** — no dependency fields | Missing Task Graph |
| 最终产生了什么 Artifact? | Run-level only (`AgentToolExecution.result_metadata.artifact_refs`) | No Task-level artifact table |
| Review 结果是什么? | Checkpoint + `AgentRunEvent(verification_updated)` only | No dedicated Review table |

**Conclusion**: To achieve the full traceability chain, the following must be added:
1. Task provenance fields (project_id, analysis_run_id, finding_id, revision_sha, created_reason)
2. Task Graph (task_dependencies table)
3. Task-level Artifact tracking (or reuse Run's artifact_refs via Task→Run link)
4. Task Review persistence (new table or extend AgentRunEvent)

---

## 9. Current Task Capability Limitations Analysis

### 9.1 Architectural Isolation

The Task domain (`tasks`, `task_logs`) is **completely isolated** from the Project/Analysis domain. There is zero FK linkage:
- `tasks` → `agents` (one Agent owns N Tasks)
- `tasks` → `users` (created_by)
- No `tasks` → `projects`, no `tasks` → `analysis_runs`, no `tasks` → `analysis_findings`

This is a deliberate Phase 2A design constraint (project.py docstring: "Task/Agent/Workspace/Execution/Artifact/Review/Scheduler models stay untouched (§G.1: the V1 task graph does not add project fields to Task)").

### 9.2 Single-Agent Binding

`Task.agent_id` is a non-nullable FK. A Task belongs to exactly one Agent. There is no:
- Assignment layer (Task → Agent mapping)
- Reassignment path
- Multi-agent Task

For Phase 2D's Agent Assignment boundary, a new intermediate entity or nullable `agent_id` + assignment table is needed.

### 9.3 No Execution Feedback Loop

When a Task's Run completes:
- `todo` → Task.status = "done" (terminal, no re-execution path)
- `supervision` → Task.status = "pending" (re-armed for next heartbeat cycle)

There is no "rework" path: a failed Task does NOT auto-create a follow-up Task or re-queue itself. The completion handler only sets status; it does not evaluate whether the result was satisfactory.

### 9.4 No Task Decomposition

The current system has **no mechanism** to break a Task into sub-Tasks. The Planning v2 mechanism (`planning.py`) produces ephemeral `entry_steps` that are NOT persisted as Task rows. A2A delegation creates new Runs but not new Tasks.

### 9.5 No Dependency Resolution

There is no scheduler that:
- Evaluates Task dependencies
- Determines which Tasks are "ready" vs "blocked"
- Triggers execution when dependencies are satisfied

The Hermes Kanban board (external to this codebase) has `parents`/`children` dependency tracking, but this is NOT part of the backend Task domain.

### 9.6 No Confirmation Gate

Tasks are created directly (API POST or Agent tool) and immediately enqueued for execution. There is no:
- PENDING_CONFIRMATION state for Tasks
- Task Proposal entity
- Human approval step before execution

This is a safety gap for Analysis-driven Task creation.

---

## 10. Summary of Code-Level Facts

| Domain | Tables | Migrations | Service Owner | API |
|---|---|---|---|---|
| Project | `projects` | f066, f067 | `project_intake_service` | `/projects/` |
| Repository | `repositories` | f066 | (same) | `/projects/{id}/repositories/` |
| AnalysisRun | `analysis_runs` | f068 | `analysis_service` | `/projects/{id}/.../analyze/` |
| AnalysisFinding | `analysis_findings` | f068 | `analysis_service` | `/projects/{id}/analysis/{run}/findings` |
| ProjectKnowledge | `project_knowledge` | f068 | `analysis_service` | `/projects/{id}/knowledge` |
| Task | `tasks`, `task_logs` | 001 (initial) | `task_executor` + Runtime | `/agents/{id}/tasks/` |
| AgentRun | `agent_runs`, `agent_run_commands`, `agent_run_events` | 20260716+ | `agent_runtime/*` | (internal) |
| AgentToolExecution | `agent_tool_executions` | f062 | `agent_runtime/*` | (internal) |
| A2A | (uses `agent_runs` + `gateway_messages` + `chat_sessions`) | 20260716+ | `a2a_runtime`, `a2a_completion` | (internal) |
| Workspace | `workspace_file_revisions`, `workspace_edit_locks` | 041 | `workspace_*` | (internal) |

**No table exists for**: Task dependencies, Task provenance, Task assignments, Task artifacts, Task reviews, Task proposals.

---

## 11. UNKNOWN Items

| Item | Status |
|---|---|
| Is there a Task scheduler daemon? | UNKNOWN — no cron/trigger found for Task dependency evaluation |
| Does the frontend have a Task Graph view? | UNKNOWN — not audited (frontend/ not in scope) |
| Is `planning.py` entry_steps ever persisted as Tasks? | NO — confirmed by code: it produces in-memory steps consumed by the Run |
| Can a Task be re-executed after "done"? | NO — "done" is terminal for todo; no re-open path in code |
| Is there a Task budget / resource limit? | UNKNOWN — no Task-level budget in code (Agent has token limits) |
| Do A2A Runs create Task rows? | NO — A2A creates AgentRuns directly, not Tasks |

---

*Audit complete. All findings based on `backend/app/` source code at commit e6237916. No design documents were referenced for capability claims.*
