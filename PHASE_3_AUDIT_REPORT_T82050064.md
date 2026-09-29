# Phase 3 — Read-Only Source Audit (t_82050064)

Baseline: `main` = `44651184` (tag `PHASE_2F_CLOSED`).
Workspace: `wt/t_82050064`. All facts verified in live source at this commit.
Method: read-only source audit; every conclusion is tagged [FACT] (directly observed
in source), [OBS] (observed pattern), [INFER] (inference from observed state), or
[UNKNOWN] (not verifiable without a live DB / runtime).

---

## Q1 — Does Project Planning capability already partially exist?

**Verdict: PARTIAL — Group-Chat Planning v2 exists; Project-level Planning does not.**

[FACT] `backend/app/services/agent_runtime/planning.py:38` defines
`_PLANNING_ROLE = "group_planning"` and `_PLAN_VERSION = 2`.
`_PLAN_FIELDS = frozenset({"version", "mode", "goal", "plan_prompt", "entry_steps"})`
(`planning.py:42`). The `PlanningModelService.complete_once()` (line 422) calls a
pinned LLM model with a closed JSON schema; `validate_planning_output()` (line 273)
enforces exact fields, candidate-Agent uniqueness, and mode ∈ {advisory, enforced}.

[FACT] `PlanningRuntimeNodeExecutor` (line 530) executes only the `group_planning`
system Run (identity check at line 547: `context.system_role != "group_planning"`
→ `PlanningContractError("planning_identity_mismatch", ...)`).

[FACT] `planning_scheduler.py:363` `PlanningCheckpointScheduler.handle()` fires
on the terminal `completed` checkpoint of the planning Run and creates entry Runs
via `RuntimeCommandIntake.start_run()` (line 533-543). Each entry Run is a
single-Agent `run_kind="foreground"` Run.

[FACT] The plan object is stored in the `AgentRun` lifecycle checkpoint under
key `"planning"` (`planning.py:594`), NOT in a dedicated `ProjectPlan` table.

[FACT] `models/task.py:37`: `TASK_CREATED_REASONS = ("MANUAL", "ANALYSIS_FINDING",
"ANALYSIS_PLANNING")`. `ANALYSIS_PLANNING` is defined as "derived from an analysis
context, no single finding — finding_id may be NULL." Task creation/mutation paths:
- `TaskDecompositionService.convert()` → `created_reason="ANALYSIS_FINDING"`
- `api/tasks.py` manual POST → `created_reason` defaults to `"MANUAL"`
- `api/tasks.py:241` PATCH → `_PROVENANCE_FIELDS = ("project_id", "analysis_run_id",
  "finding_id", "revision_sha", "created_reason")` are ALL updatable; a detached
  probe Task runs `task_provenance_dao.provenance_consistency()` before the write
  (400 `PROVENANCE_INCONSISTENT` on violation). So a human CAN mint an
  `ANALYSIS_PLANNING` Task via the transport (no dedicated service lane owns it).
- `agent_tools.py:9845` agent tool create → sets supervision fields only → `"MANUAL"`

[FACT] `models/project.py` has `Project.goal: Mapped[str | None] = mapped_column(Text)`
(a free-text column) but no planning entity, work package, milestone, or phase
column. The `project_status_enum` includes `PENDING_CONFIRMATION` (line 59) which
is documented as INERT in `analysis_service.py:29-31`: "no code path in this lane
sets it; the confirmation UI does not exist yet."

**OBS**: The Group-Chat Planning v2 subsystem is a runtime-scoped capability
(triggered by @mention in a Group chat session, stored in AgentRun checkpoints)
and is structurally distinct from any Project-level planning concept. It does not
read from `Project`, `AnalysisRun`, or `Task` tables.

---

## Q2 — Can the Task Graph express a complete Project Plan?

**Verdict: PARTIAL — flat dependency DAG only; no grouping, milestones, or edge attributes.**

[FACT] `models/task.py:138-171` `TaskDependency`:
- Columns: `task_id`, `depends_on_task_id` (both non-nullable UUID FK to `tasks.id`)
- Constraints: `UNIQUE(task_id, depends_on_task_id)`, `CHECK task_id <> depends_on_task_id`
- **No edge type, label, deadline, priority, group tag, milestone reference, or phase attribute.**

[FACT] `services/task_graph_service.py:79-80`:
```python
MAX_PROJECT_EDGES = 1000   # bounded project-level graph assumption
MAX_BATCH_EDGES   = 100    # per-batch write cap
```

[FACT] `services/task_graph_service.py:134-171` `upstream_reachable()`:
Bounded DFS over the project edge set; an oversized graph (visited > `MAX_PROJECT_EDGES`)
is reported "reachable" → candidate edge is REFUSED (fail closed). No topological
sort, no critical-path computation, no level/phase structure.

[FACT] `services/task_graph_service.py:403-416` `ensure_ready()`:
Returns `list[uuid.UUID]` of unmet direct-dependency ids. The readiness projection
is binary: "ready" (all direct deps `done`) or "blocked" (any direct dep not `done`).
No transitive readiness, no group-level readiness, no milestone gating.

[FACT] `services/task_graph_service.py:242-317` `_add_edges()`:
Validation order: not-found → tenant match → self → supervision excluded →
same-project → cycle (bounded DFS) → duplicate. No edge-type or group validation.

[FACT] `TaskDependency` has no `created_at` ordering beyond the PK; there is no
"phase N" or "group G" concept on the edge. Tasks in the same Project can be
arbitrarily inter-mixed with no structural grouping.

**INFER**: To express a "Project Plan" with work packages, milestones, and phases,
the Task Graph would need: (a) a `TaskGroup`/`Milestone`/`Phase` table,
(b) edge attributes (e.g., "this edge belongs to milestone M"), and
(c) a topological/level computation for phase gating. None of these exist.

---

## Q3 — What is the current degree of Analysis Finding → Task mapping?

**Verdict: DETERMINISTIC AND EXPLICIT — 5 of 200 classification combos produce a Task; `planning_only` findings are reported but never converted.**

[FACT] `services/task_decomposition_service.py:83-88`:
```python
_EXECUTABLE_RULES = (
    ("TECH_DEBT", "FACT", {"WARN", "HIGH", "CRITICAL"}),  # E1
    ("SECURITY",  "FACT", {"HIGH", "CRITICAL"}),          # E2
)
_PLANNING_CATEGORIES = frozenset({"OPEN_QUESTION", "RISK"})  # P1, P2
_LOW_CONFIDENCE_TAGS = frozenset({"INFERENCE", "UNKNOWN"})   # P3
```
`classify()` (line 134) evaluates exclusions first (P1-P4 → `planning`), then
inclusions (E1/E2 → `executable`), default → `planning` (P5 fail-closed).

[FACT] `services/task_decomposition_service.py:158-197` `build_task_fields()` (returns a dict — pure):
- `title`: `"[{category}] {summary}"` truncated to 500 chars
- `description`: finding summary + evidence anchors + provenance footer
- `status`: `"pending"` (G4: NEVER enqueued)
- `priority`: `SEVERITY_TO_PRIORITY` closed map
- `created_reason`: `"ANALYSIS_FINDING"`
- `project_id`, `analysis_run_id`, `finding_id`, `revision_sha`: all mandatory

[FACT] `services/task_decomposition_service.py:209-297` `convert()`:
- G1: run must be `AN_COMPLETED` + tenant scope + agent tenant == project tenant
- G3: executable findings → pending Task; planning findings → `planning_only`
- Dedup: `converted_finding_ids` pre-check + `uq_tasks_analysis_finding` UNIQUE
- G4: **NO `enqueue_task_runtime` call** — "conversion never executes"
- G5: bounded to `MAX_TASKS_PER_INVOCATION = 100`

[FACT] `models/task.py:133-135`:
```python
__table_args__ = (
    UniqueConstraint("analysis_run_id", "finding_id", name="uq_tasks_analysis_finding"),
)
```
One Task per (run, finding) pair. PostgreSQL treats NULL as distinct, so MANUAL
rows coexist freely.

[FACT] `api/projects.py:645-704` `POST /{project_id}/analysis/{run_id}/tasks`
is the sole transport entry point; it calls `task_decomposition_service.convert()`.

**OBS**: `planning_only` findings (OPEN_QUESTION, RISK, INFERENCE, UNKNOWN, INFO
severity) are reported in the `DecompositionOutcome.per_finding` dict but
**never produce a Task row** via the owned conversion lane. The
`ANALYSIS_PLANNING` created_reason value is reachable ONLY through the generic
PATCH transport (`api/tasks.py:241` `_PROVENANCE_FIELDS` + `provenance_consistency`
validator, `task_dao.py:363-407`) — there is no dedicated service lane that
converts planning-class findings into Tasks.

---

## Q4 — Do Task Group / parent-child / milestone / phase concepts exist?

**Verdict: NO — no TaskGroup, Milestone, Phase, or WorkPackage model exists.**

[FACT] `models/task.py` — `Task` has:
- `parent_task_id`: **does not exist**
- `group_id`: **does not exist**
- `milestone_id`: **does not exist**
- `phase_id`: **does not exist**
- `work_package_id`: **does not exist**

[FACT] `models/task.py:138-171` — `TaskDependency` is the only relational
structure on Tasks: a flat directed edge with no attributes.

[FACT] `models/schedule.py:13` `AgentSchedule` — a cron instruction table
(`cron_expr`, `instruction`, `delivery_target_id`); not a task-group concept.

[FACT] No `TaskGroup`, `Milestone`, `Phase`, or `WorkPackage` model file exists
in `backend/app/models/` (verified by directory listing: 44 model files, none match).

[FACT] `models/project.py:53-70` — `Project.status` enum includes
`PENDING_CONFIRMATION` (a Project-level status, not a task-grouping mechanism).

**INFER**: The closest existing "grouping" is the `Project` itself (a Task's
`project_id` FK is non-nullable in the provenance set, but the edge table has
no group/milestone tag). To add work-package/milestone semantics, a new
table layer is required.

---

## Q5 — Is Agent assignment currently single-Agent or does it support higher-level organization?

**Verdict: SINGLE-AGENT — `Task.agent_id` is a non-nullable UUID FK to exactly one Agent.**

[FACT] `models/task.py:50`:
```python
agent_id: Mapped[uuid.UUID] = mapped_column(
    UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False
)
```

[FACT] `services/task_executor.py:68-72`:
```python
if task.agent_id != agent.id:
    raise TaskRuntimeIntakeError("task_agent_mismatch",
        "Task does not belong to the requested Agent")
```

[FACT] `services/task_execution_service.py:365-368` (P5 gate):
```python
active_agent = await agent_dao.get_active(agent.id)
if active_agent is None:
    return ("ASSIGNMENT_FAILED", "Task's Agent does not exist (or was deleted)")
```

[FACT] `models/agent_run.py:34-39`:
- `run_kind IN ('foreground', 'background', 'delegated', 'orchestration')`
- CHECK: `run_kind = 'orchestration' AND agent_id IS NULL AND system_role = 'group_planning'`
- CHECK: `run_kind <> 'orchestration' AND agent_id IS NOT NULL AND system_role IS NULL`
  → Every non-orchestration Run carries exactly one Agent.

[FACT] `services/agent_runtime/planning.py:200-205`:
Planning v2 requires "at least two distinct candidate Agents" (`_candidate_agent_ids`)
but each entry Run created by `PlanningCheckpointScheduler` is a **single-Agent**
foreground Run (`planning_scheduler.py:318-360`).

[FACT] `models/agent.py:39`: `Agent.role_description: Mapped[str] = mapped_column(
String(500), default="")` — free-text, not a structured role enum.

**INFER**: A "team owns this work package" or "squad assigns roles to subtasks"
concept does not exist. Multi-Agent coordination is achieved through:
- Group-Chat Planning v2 (entry Runs are single-Agent; handoffs are public @mentions)
- A2A `send_message_to_agent` tool (one Agent messages another)
- No structural "team assignment" model.

---

## Q6 — Do squad / team / role models already exist?

**Verdict: NO execution-relevant team/squad/role model. Org concepts are metadata-only or chat-scoped.**

[FACT] `models/org.py:13-33` `OrgDepartment`:
- "Departments and members synced from Feishu"
- Columns: `name`, `parent_id` (self-FK), `path`, `member_count`, `status`
- `__tenant_scoped__ = True`
- **No execution role: no scheduler, no agent dispatch, no permission decision consumes this.**

[FACT] `models/org.py:35-64` `OrgMember`:
- Feishu-synced person record; `title` is a free-text String(200), not a role enum.

[FACT] `models/org.py:84-97` `AgentAgentRelationship`:
- `relation: Mapped[str] = mapped_column(String(50), nullable=False, default="collaborator")`
- Free-text relation string; no structured role or squad membership.

[FACT] `models/group.py:59-94` `GroupMember`:
- `role IN ('manager', 'member')` — chat-group membership role, NOT a work-execution role.
- A Group is a "tenant-owned, long-lived native group chat" (docstring line 2), not a team.

[FACT] No `Squad`, `Team`, `WorkTeam`, `Role` model exists in `models/` (44 model files verified).

[FACT] `models/agent.py:39` `Agent.role_description` is free-text String(500).
`AgentTemplate.category` is String(50) defaulting to `"general"` — a display category, not an execution role.

**INFER**: To introduce a meaningful "squad" abstraction for task assignment,
the system would need a new model that links N Agents to a shared work package
with role assignments. No such model exists. `OrgDepartment` is a Feishu-sync
mirror and must NOT be repurposed as an execution unit without a documented
ADR (it currently has zero execution consumers).

---

## Q7 — Does supervision scheduling already have a real consumer?

**Verdict: NO automated consumer — supervision tasks are manual-trigger only; `remind_schedule` is metadata.**

[FACT] `models/task.py:72-76`:
```python
supervision_target_user_id: Mapped[uuid.UUID | None] = mapped_column(
    UUID(as_uuid=True), ForeignKey("users.id"))
supervision_target_name: Mapped[str | None] = mapped_column(String(100))
supervision_channel: Mapped[str | None] = mapped_column(String(50))
remind_schedule: Mapped[str | None] = mapped_column(String(100))
```
Note: `supervision_target_user_id` is a nullable UUID column WITHOUT a `ForeignKey`
constraint (unlike `Task.created_by` which has `ForeignKey("users.id")`).

[FACT] `services/task_executor.py:31-41` `_task_goal()`:
```python
if task.type == "supervision":
    goal = f"[督办任务] {task.title}"
    if task.supervision_target_name:
        goal += f"\n督办对象: {task.supervision_target_name}"
    return goal + "\n\n请执行此督办任务：联系督办对象，了解进展，并汇报结果。"
```
The supervision Run goal is a **template string** — no schedule parsing, no
reminder loop, no escalation logic.

[FACT] `api/tasks.py:301-331` `POST /{task_id}/trigger`:
```python
"""Manually trigger a supervision task execution (for testing)."""
# ...
asyncio.create_task(execute_task(task.id, agent_id))
```
The docstring explicitly says "for testing." This is the **only** path to
execute a supervision Task.

[FACT] `services/agent_tools.py:9862-9865`:
```python
# Supervision task — reminder engine will pick it up
target = args.get('supervision_target_name', 'someone')
schedule = args.get('remind_schedule', 'not set')
```
The agent tool response text mentions "reminder engine will pick it up" but
**no such engine exists in source**. `services/scheduler.py` only consumes
`AgentSchedule` rows (cron-based agent instruction runs), not `Task` rows.

[FACT] `services/trigger_daemon.py:174` — the trigger daemon evaluates
`AgentTrigger` rows (message/schedule triggers), not supervision Task rows.

**OBS**: `remind_schedule` is a free-text String(100) column with no consumer.
No code in `scheduler.py`, `trigger_daemon.py`, or `heartbeat_runtime.py` reads
`Task.supervision_channel` or `Task.remind_schedule` for scheduling purposes.
Supervision tasks are stored as data rows; execution requires a human to call
the trigger endpoint.

---

## Q8 — Which capabilities are currently reusable?

**Reusable for Phase 3 Planning/Squad work:**

| # | Capability | Owner | Reuse rationale |
|---|---|---|---|
| 1 | Project lifecycle (10-state enum + transitions) | `project_intake_service.py`, `intake_security.py` | `EXECUTING`/`BLOCKED` states already gate Task execution via `PROJECT_EXECUTABLE_STATUSES` |
| 2 | Analysis subsystem (Run/Finding/Knowledge) | `analysis_service.py`, `models/analysis.py` | Planning can read `ProjectKnowledge` + `AnalysisFinding` as inputs; no need to re-implement analysis |
| 3 | Task provenance (5-column set + created_reason) | `models/task.py`, `task_dao.py` | `ANALYSIS_PLANNING` reason is a reserved slot — the schema already anticipates planning-derived Tasks |
| 4 | Task Graph (flat DAG + cycle detection + execution gate) | `task_graph_service.py` | Planning output can emit `TaskDependency` edges; the graph service already validates tenant/project/cycle constraints |
| 5 | Task→Run bridge (gate P1-P8 + idempotency R1-R5 + audit) | `task_execution_service.py` | Planning-generated Tasks enter the same execution path — no second Runtime |
| 6 | RuntimeCommandIntake (idempotent Run registration) | `agent_runtime/adapter.py` | Planning entry Runs already use this; Task Runs will too |
| 7 | Task Runtime Settlement (idempotent status update) | `agent_runtime/task_completion.py` | Terminal Run checkpoints update Task status; Planning Tasks follow the same settlement |
| 8 | Task Decomposition (ANALYSIS_FINDING→Task, 5×4×4 grid) | `task_decomposition_service.py` | The `classify()` + `build_task_fields()` pattern is the template for `ANALYSIS_PLANNING` → Task conversion |
| 9 | Group Planning v2 (LLM plan call + validation + checkpoint) | `agent_runtime/planning.py` | The plan-validation contract (`validate_planning_output`, `_PLAN_FIELDS`) is a reusable pattern for Project-level planning |
| 10 | Planning Checkpoint Scheduler (plan→entry Runs) | `agent_runtime/planning_scheduler.py` | The "completed checkpoint triggers entry Run creation" pattern is reusable for Plan→Task-Graph emission |
| 11 | Agent/Workspace models (role_description, model_id, autonomy) | `models/agent.py`, `models/workspace.py` | Squad role assignments can reference `Agent.role_description`; workspace isolation already exists |
| 12 | Agent Schedule (cron-based periodic runs) | `models/schedule.py`, `services/scheduler.py` | If supervision scheduling is needed, this is the existing cron engine to extend (not a new engine) |
| 13 | A2A Runtime (inter-Agent messaging) | `agent_runtime/a2a_runtime.py` | Multi-Agent coordination within a Squad can use the existing A2A path |
| 14 | Group Chat + Planning v2 orchestration | `agent_runtime/group_*.py`, `planning.py` | The multi-Agent coordination pattern (entry Runs + public handoffs) is the existing squad-execution reference |

---

## Q9 — Which capabilities are missing?

| # | Missing capability | Gap description |
|---|---|---|
| 1 | **Project-level Planning domain** | No `ProjectPlan`, `PlanningRun`, or `PlanningRevision` model. Group Planning v2 is chat-scoped and stored in AgentRun checkpoints; it does not produce a durable, project-scoped plan artifact. |
| 2 | **Work Package / Milestone / Phase model** | No `TaskGroup`, `Milestone`, `Phase`, or `WorkPackage` table. `TaskDependency` is a flat edge with no group tag. Tasks in a Project have no structural grouping. |
| 3 | **`ANALYSIS_PLANNING` producer** | `TASK_CREATED_REASONS` includes `ANALYSIS_PLANNING` (finding_id nullable, project/revision mandatory) but **no service creates Tasks with this value**. The `planning_only` findings from `classify()` are reported but never converted. |
| 4 | **Squad / Team / Role execution model** | No model links N Agents to a shared work unit with role assignments. `OrgDepartment` is Feishu metadata; `GroupMember.role` is chat-scoped. A "this work package is owned by Squad S, with role R1 on tasks T1-T5" concept does not exist. |
| 5 | **Multi-Agent Task ownership** | `Task.agent_id` is a single non-nullable FK. No "team owns this Task" or "Squad S is responsible for work package W" column. |
| 6 | **Automated supervision scheduling** | `Task.remind_schedule` (String(100)) and `supervision_channel` have no consumer. No daemon reads these fields. Supervision execution is manual-trigger only. |
| 7 | **Planning → Task Graph integration** | Group Planning v2 produces an in-checkpoint plan object; it does NOT emit `Task` rows or `TaskDependency` edges. A Project-level plan-to-task-graph translator is absent. |
| 8 | **Project-level resource budget / concurrency cap** | `Agent.max_tool_rounds`, `Agent.max_llm_calls_per_day` are per-Agent. No Project-level or Team-level resource ceiling exists. `TaskExecutionService` has no project-budget gate. |
| 9 | **Task-level deadline scheduling** | `Task.due_date` is a nullable datetime column with no consumer. No scheduler checks `due_date` for escalation or auto-firing. |
| 10 | **Review independence enforcement** | Phase 2F settlement reuses the same Agent Run checkpoint; there is no "reviewer Agent must be a different Agent than the executor Agent" constraint in the Task/Run model. |

---

## Q10 — Which capabilities should NOT be newly added?

| # | Do NOT add | Reason (source evidence) |
|---|---|---|
| 1 | **A second Agent Runtime / execution chain** | Phase 2F closure (`docs/PHASE_2F_CONVERGENCE_REPORT.md`, `foundation_inventory.md` §10): the existing Runtime (Task→Run→Result→Settlement) is frozen and verified. The root task body explicitly forbids "不得新造第二套 Runtime." |
| 2 | **A full organizational hierarchy system** | `OrgDepartment`/`OrgMember` are Feishu-synced metadata (no execution consumers per `CAPABILITY_CONCEPT_MAP.md` §11). Adding a new org tree would duplicate synced data and create a second authority for department structure. |
| 3 | **Automatic task execution on conversion** | `TaskDecompositionService` G4: "Conversion never executes. A converted Task lands in status='pending' and is NEVER passed to enqueue_task_runtime. 'Create' and 'run' are decoupled — that decoupling IS the V1 safety gate." (`task_decomposition_service.py:15-17`) |
| 4 | **A "Squad" state machine without an independent owner** | Root `AGENTS.md` §2: "Do not introduce a state machine merely to represent workflow steps, UI progress, or a lifecycle already owned elsewhere." If a Squad concept is added, it must be a data model (Agent + Task + work package links), NOT a new lifecycle SM alongside `Task` and `AgentRun`. |
| 5 | **Vector RAG / knowledge graph** | Phase 2C explicitly chose a flat `ProjectKnowledge` table over a knowledge graph (`models/analysis.py:189`: "A knowledge graph is deliberately NOT modeled — this is a single flat row."). Re-introducing RAG would be scope creep and violate the 2C minimal-model decision. |
| 6 | **Dynamic code execution in Analysis** | `analysis_service.py:44-48`: "Stage 11 (hard): this path is STATIC-ONLY. It reads DB rows and the bounded locator JSON written by acquisition; it NEVER executes the target project's code." Dynamic analysis is explicitly deferred. |
| 7 | **UI-first team display / virtual employee interfaces** | Root task body: "这个阶段不是制作虚拟员工展示界面" (This phase is NOT about building virtual employee display interfaces). No UI work is in scope for Phase 3. |
| 8 | **A second task-graph engine** | `TaskGraphService` with bounded DFS + per-project advisory locks (`pg_advisory_xact_lock`) is the V1 owner (`task_graph_service.py:174-193`). A second graph engine would create dual authority over dependency semantics. |
| 9 | **Automatic retry of failed Runs** | `TaskExecutionService` R4: "retries are NEVER automatic" (`task_execution_service.py:16`); `RETRY_SOFT_CAP_PER_TASK_PER_DAY = 3` is a human-initiated cap, not an auto-retry. The root task body forbids "自动升级无关依赖" and "隐藏自动重试." |
| 10 | **Repurposing `OrgDepartment` as an execution unit** | `OrgDepartment` has zero execution consumers (no scheduler, no agent dispatch, no permission decision reads it — `CAPABILITY_CONCEPT_MAP.md` §11: "No scheduler, agent dispatch, or permission decision consumes department data."). Making it an execution unit would blur the Feishu-metadata boundary and create a second authority for org structure. |
| 11 | **Adding a `Squad` model without real execution semantics** | Root task body: "不能因为'Squad'这个词而新增不必要的组织层级" (must not add unnecessary organizational layers just because of the word "Squad"). Any Squad abstraction must have a concrete execution role: "which roles handle which tasks, which can parallel, which must serialize, which share resources, which Agents can take on which tasks." |

---

## Summary Table — Reusable / Missing / Avoid-New

### Reusable (do NOT rebuild)
| Capability | Where |
|---|---|
| Project lifecycle + intake validation | `project_intake_service.py`, `intake_security.py`, `models/project.py` |
| Analysis Run/Finding/Knowledge (append-only, revision-bound) | `models/analysis.py`, `analysis_service.py` |
| Task provenance 5-column set + created_reason enum | `models/task.py:84-113` |
| Task Graph (flat DAG, cycle detection, execution gate) | `task_graph_service.py`, `models/task.py:TaskDependency` |
| Task→Run bridge (fail-closed gate, idempotency, audit) | `task_execution_service.py` |
| RuntimeCommandIntake (idempotent Run registration) | `agent_runtime/adapter.py` |
| Task settlement (idempotent status update from terminal checkpoint) | `agent_runtime/task_completion.py` |
| Task Decomposition (finding→Task, 5×4×4 grid) | `task_decomposition_service.py` |
| Group Planning v2 (LLM plan + validation + checkpoint) | `agent_runtime/planning.py` |
| Planning Checkpoint Scheduler (checkpoint→entry Runs) | `agent_runtime/planning_scheduler.py` |
| Agent/Workspace models | `models/agent.py`, `models/workspace.py` |
| Cron scheduler (AgentSchedule) | `models/schedule.py`, `services/scheduler.py` |
| A2A inter-Agent messaging | `agent_runtime/a2a_runtime.py` |

### Missing (needs new design in Phase 3)
| Gap | Note |
|---|---|
| Project-level Planning domain (Plan/Revision/WorkPackage/Milestone) | New model layer; must NOT duplicate Task/Project/Analysis |
| `ANALYSIS_PLANNING` Task producer | Schema slot exists; no service writes it |
| Task grouping / milestone / phase | `TaskDependency` has no group tag; no `TaskGroup` table |
| Squad / Team / Role execution model | No model links N Agents to a shared work unit with roles |
| Multi-Agent Task ownership | `Task.agent_id` is single-Agent; no "team owns task" column |
| Planning → Task Graph translator | Group Planning v2 emits Runs, not Tasks/Dependencies |
| Automated supervision scheduling | `remind_schedule` has no consumer |
| Project-level resource budget | No team/project-level cap on concurrent Runs |
| Review independence constraint | No "reviewer ≠ executor Agent" enforcement in Task/Run model |

### Avoid-new (do NOT add in Phase 3)
| Anti-pattern | Why |
|---|---|
| Second Runtime / execution chain | Phase 2F Runtime is frozen and verified |
| Full org hierarchy system | `OrgDepartment` is Feishu metadata; no execution consumers |
| Auto-execute on conversion | V1 safety gate: create ≠ run (G4) |
| Squad state machine | Must be a data model, not a lifecycle SM (AGENTS.md §2) |
| Vector RAG / knowledge graph | Phase 2C explicitly chose flat `ProjectKnowledge` |
| Dynamic code execution in Analysis | Stage 11 hard constraint: static-only |
| UI-first team display | Out of scope for Phase 3 |
| Second task-graph engine | `TaskGraphService` is the V1 owner |
| Automatic retry of failed Runs | R4: retries are never automatic |
| Repurposing `OrgDepartment` as execution unit | Would create a second authority for org structure |

---

## UNKNOWN items (cannot be resolved without a live DB / runtime)

| Item | Reason |
|---|---|
| Whether any `Task` row in production has `created_reason="ANALYSIS_PLANNING"` | Requires a live DB query; the schema allows it but no source path writes it |
| Whether `TaskDependency` edges in production exceed `MAX_PROJECT_EDGES=1000` for any Project | Requires a live DB query on `task_dependencies` grouped by `project_id` |
| Whether `AgentSchedule` rows are being actively consumed by the scheduler in production | Requires a live `agent_schedules` table query + scheduler log |
| Whether `supervision_channel` / `remind_schedule` values exist on any live Task row | Requires a live DB query; no code path reads them for scheduling |

---

*Report produced by aco-architect, task t_82050064, at `main` = `44651184`.*
*All file paths relative to `I:\project\AI Company OS\.worktrees\t_82050064`.*
