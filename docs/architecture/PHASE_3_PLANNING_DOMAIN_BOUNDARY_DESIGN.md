# Phase 3 — Planning Domain: Boundary & Data-Model Design (t_0739e600)

Task: `t_0739e600` — "Design Planning Domain boundary and data model"
Author: aco-architect
Baseline: main `44651184` (tag `PHASE_2F_CLOSED`) — designed in worktree `wt/t_0739e600`
Inputs (both read-only, both audited at this same baseline):
- `docs/architecture/PHASE_3_EXECUTION_CHAIN_TRACE.md` (t_5487441c, commit `e700411b`) — the execution spine + "must-NOT-duplicate" rules (§7.2).
- `PHASE_3_AUDIT_REPORT_T82050064.md` (t_82050064, commit `1e29f1e1`) — the 10-question pre-audit (reusable / missing / avoid-new tables).

All file paths below are repository-relative. Every load-bearing claim was
re-grepped against this tree before the design was written; each new-entity
decision cites the audit rows and the in-source evidence that justify it.

## Classification Discipline

Carried over from the Phase 2C-2F docs and the Phase 3 root brief:

- **FACT** — directly observable in this tree (file:line, named constant, migration id).
- **OBSERVATION** — a pattern seen in one or more concrete instances, no universal claim.
- **INFERENCE** — a conclusion drawn from the facts above.
- **UNKNOWN** — not verifiable from this tree; the resolving action is stated.

The design itself is normative (what the system *should* be). Normative prose
is unlabeled; all claims about the *current* system carry a class.

---

## 1. Verdict (up front)

The minimal Planning Domain is **new and justified**: the audit found no
Project-scoped plan artifact (`PHASE_3_AUDIT T82050064` Q9 #1/#2, G2/G7 in the
trace), and the existing Task Graph is a flat, attribute-free DAG that cannot
carry work-package / milestone / phase structure (Q2).

Design shape, one line:

```
   PlanningRun (durable revision)  ──owns──>  PlanningGoal
             │                                      │
             │ owns                                 │ requires
             ▼                                      ▼
        WorkPackage   ──< contains ──<   Milestone (optional, same-plan)
             │
             │ materializes (explicit, human-gated)
             ▼
   Task rows + task_dependencies edges   (existing, frozen — Phase 2D)
             │
             ▼
   Task.agent_id binding                 (existing, frozen — Phase 2E)
             │
             ▼
   RuntimeCommandIntake -> spine         (existing, frozen — Phase 2F)
```

Decisions that bound the whole design:

| # | Decision | Authority |
|---|---|---|
| D1 | Planning is a **durable data layer** (5 new tables) — NOT the existing `group_planning` LLM checkpoint flow. Both coexist; neither replaces the other. | audit G2/G7; trace §7.1(1) |
| D2 | Planning materializes into the **existing** `Task` + `TaskDependency` + `Task.agent_id`. No second edge table, no second readiness model, no second assignment fact. | trace §7.2 (3/4 rules); audit Q10 #8 |
| D3 | **Planner Run = one new `system_role` + one new topology** on the shared Checkpointer, enqueueing through the existing `RuntimeCommandIntake`. No second runtime / worker / inbox / claim loop / settlement path. | trace §7.1(1), §7.2 (1/2/5/6/7 rules) |
| D4 | **Squad/Team: explicitly NOT modeled in V1** (audit Q6/Q9 #4/#5; root brief "no speculative org hierarchy"). What Phase 3 "squad orchestration" needs (shared-resource conflicts, review independence, parallelism) is carried as **constraint metadata on `WorkPackage`**, computed at materialization + assignment, not as an org entity. See §8. | audit Q6/Q10 #4/#11 |
| D5 | No **state machine** is introduced: every lifecycle status column is a **closed result-code enum**, and the only authoritative lifecycle of a planning *run* lives in `AgentRun` + checkpoint (execution state stays in checkpoints — `models/agent_run.py:28`). | root AGENTS.md §2; audit Q10 #4 |
| D6 | All new tables are `__tenant_scoped__` (non-nullable `tenant_id`), reached only through `TenantScopedBaseDAO`, following the Phase 2C `analysis.py` precedent. | audit Q8 #2; `models/analysis.py` docstring |

---

## 2. Boundary Decision Matrix (the required 6-area matrix)

Every field a planning feature *might* carry is assigned to exactly one owning
area. This is the acceptance criterion "boundary matrix covers all 6 domain
areas"; each row is a decision, and §3 turns the "Planning" rows into schema.

| Field / concern | **Project** | **Analysis** | **Planning (NEW)** | **Task (existing)** | **Agent Assignment (existing)** | **Execution (existing)** | Rationale |
|---|---|---|---|---|---|---|---|
| `project_id` (which project) | ✓ owned | ✓ owns run | ✓ plan/run/plan-goal all reference | ✓ provenance col | — | — | single owner = Project; all others FK to it |
| `goal` (business intent, free text) | ✓ `Project.goal` (`project.py:52`) | — | PlanningGoal is a *derived, refined* goal (see P3); not a second authority for intent | — | — | — | Project keeps the raw intent; planning refines, never redefines |
| analysis revision bound | — | ✓ `AnalysisRun.revision_sha` + `UNIQUE(project_id,revision_sha)` (`analysis.py:97-99`) | ✓ run + plan-goal snapshot `analysis_revision_sha` | ✓ provenance col `revision_sha` | — | — | a plan is only valid against a known revision; re-analysis => new plan |
| `status` (lifecycle of the object) | ✓ `project_status_enum` (frozen) | ✓ `ANALYSIS_RUN_STATUSES` (frozen) | ✓ **new closed** `PLANNING_RUN_STATUSES` + `PLANNING_GOAL_STATUSES` | ✓ `task_status_enum` (frozen) | — | ✓ Run lifecycle in checkpoint (frozen) | closed code-sets only; no new workflow SM (D5) |
| goal → concrete objective text | — | — | ✓ `PlanningGoal` (P3) | — | — | — | new object; Analysis produces *findings*, planning produces *goals* |
| required capability / role hint | — | — | ✓ `PlanningGoal.required_capabilities` (P5, JSON) | — | — | — | *input* to assignment, NOT the assignment fact |
| work-package grouping | — | — | ✓ `WorkPackage` + `WorkPackage.task_scope` (P7) | — | — | — | **cannot** live on Task: Task has no group col, and a group owns N tasks → must be a table |
| milestone / phase / deadline | — | — | ✓ `Milestone` + `Milestone.kind` (P9) | — | — | — | no edge attribute exists (Q2); phase = closed milestone kind |
| ordering / parallelism / serial | — | — | ✓ `WorkPackage.execution_mode` + `Milestone.ordered` (P12/P13) | ✓ edges ARE the order source of truth (frozen) | — | — | planning *proposes* order; the DAG *enforces* it; on conflict DAG wins |
| shared-resource conflict (file/DB/API/workspace/tenant) | — | — | ✓ `WorkPackage.shared_resources` (P10, JSON) | — | — | ✓ Redis workspace locks + advisory lane already *enforce* at runtime | planning *detects/conflicts*, runtime *enforces* — no new lock layer |
| review independence (reviewer ≠ executor) | — | — | ✓ `WorkPackage.requires_independent_review` (P11) | — | ✓ `Task.agent_id` is the assignment fact; reviewer = a 2nd task's agent | — | constraint computed at assignment; Phase 2F gate G8 is fail-open today (known) |
| who executes a task | — | — | — | ✓ `Task.agent_id` (non-null FK, frozen) | ✓ THIS is the assignment fact | — | D2: no parallel assignment table; `TaskAssignment` is forbidden |
| execution order of Tasks | — | — | — | ✓ `task_dependencies` (frozen DAG) | — | ✓ claim/lane order at runtime | D2 |
| a task's Run lifecycle (running/done/failed) | — | — | — | ✓ settlement sets `Task.status` | — | ✓ `AgentRun` + checkpoint | D2 |
| idempotency / attempt keying | — | — | ✓ run `PLAN_SHA256` (P2) for dedup | ✓ `R1–R5` attempt keys (frozen) | — | ✓ `uq_agent_runs_source_execution` (frozen) | planning dedups on content hash; task dedup already exists |
| budget / concurrency cap | — | — | ✓ **defers**: `Project.max_parallel_tasks` optional int on plan (P16) — see §8 deferral | — | — | ✓ `AGENT_RUNTIME_COMMAND_CONCURRENCY` (frozen global) | per-project cap is a genuine Phase 3 gap (G4/G6); kept minimal, not a new scheduler |
| created_reason for planning-born tasks | — | — | — | ✓ `ANALYSIS_PLANNING` already reserved (`task.py:37`) | — | — | the schema slot exists; this design owns the *producer* (audit Q9 #3) |

Six domain columns, every planning-relevant concern assigned. The two "defers"
(budget, review-independence enforcement) are named so they are not silently
dropped (§8).

---

## 3. Entity Definitions (the 5 new Planning tables + 1 link table)

Naming convention follows `models/analysis.py`: snake_case tables, `Mapped[...]`
columns, closed enum tuples as module-level constants, `__tenant_scoped__ =
True`, `created_at`/`updated_at` server defaults. A new model file
`backend/app/models/planning.py` holds all of it (single domain boundary), plus
one new migration (next id after the Phase 2D/2C/2E chain — builder assigns).

> Overlap flags are answered per entity in §5 ("new vs reusable"), because the
> acceptance criterion is "no new entity duplicates an existing one without
> explicit justification."

### P1 `PlanningRun` — table `planning_runs` (the durable revision)

One planning execution, bound to one Project and one analysis revision.
**Append-only**, mirroring `AnalysisRun`: a re-plan at a new revision is a new
row; `UNIQUE(project_id, analysis_revision_sha)` is the concurrency guard and
the dedup key (same pattern as `uq_analysis_runs_project_revision`).
The **only** lifecycle column; it is a closed result-code enum (D5) — it is NOT
a workflow SM, and it does not describe the *Run* (that lives in `AgentRun`
checkpoints).

| Field | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID FK non-null, indexed | D6 |
| `project_id` | UUID FK → `projects.id` CASCADE, non-null | a plan belongs to one project |
| `analysis_revision_sha` | String(64), non-null, indexed | the revision the plan was produced against |
| `plan_sha256` | String(64), nullable, indexed | content hash of the emitted (goals+packages+milestones) payload; the materialization idempotency key (§4). NULL while open |
| `status` | `planning_run_status_enum` | closed set below |
| `planner_agent_id` | UUID FK → `agents.id` SET NULL nullable | who produced it (audit analog of `AnalysisRun.agent_id`) |
| `source_agent_run_id` | UUID FK → `agent_runs.id` SET NULL nullable | ties this durable row to the planner *Run* (D3) — traceability of the LLM step |
| `plan_payload` | JSON, nullable | the full emitted plan (bounded); the authoritative body the translator reads |
| `started_at` / `finished_at` / `created_at` / `updated_at` | DateTime | |

`PLANNING_RUN_STATUSES = ("PL_OPEN", "PL_COMPLETED", "PL_FAILED")` — a
**closed result-code set, not a workflow SM** (mirrors `ANALYSIS_RUN_STATUSES`,
`analysis.py:60`). `PL_OPEN`: plan may still be recorded. `PL_COMPLETED` /
`PL_FAILED`: terminal, payload locked. Unknown values fail closed at the
service layer (the re-validation pattern from every other enum).

```
planning_runs
  UNIQUE(project_id, analysis_revision_sha)   uq_planning_runs_project_revision
```

### P2 `PlanningGoal` — table `planning_goals`

One concrete objective of a run. This is the "derived, refined goal" of the
matrix: it is **not** a second authority for business intent (that stays on
`Project.goal`); it is an *operational* goal the plan pursues, traceable to the
analysis context that justified it.

| Field | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID FK non-null, indexed | |
| `planning_run_id` | UUID FK → `planning_runs.id` CASCADE non-null | goals die with their run revision |
| `title` | String(500) non-null | |
| `description` | Text nullable | |
| `required_capabilities` | JSON nullable (P5) | **assignment input** (e.g. `["frontend","migrations"]`), not the assignment fact |
| `analysis_finding_ids` | JSON nullable (bounded list) | which findings/Knowledge motivated this goal (traceable, like finding `evidence`) |
| `status` | `planning_goal_status_enum` | closed, see below |
| `created_at` / `updated_at` | DateTime | |

`PLANNING_GOAL_STATUSES = ("PL_PROPOSED", "PL_APPROVED", "PL_MATERIALIZED")`
— a **closed result-code set, not a workflow SM** (root AGENTS.md §2: a new SM
needs an independent owner + need; a goal's status codes are consumed by the
materialization gate in §4, which *is* its behavioral consumer).
- `PL_PROPOSED`: emitted by the planner, not yet approved.
- `PL_APPROVED`: human/company confirmation (the PENDING_CONFIRMATION step,
  reusing the 2C confirmation precedent) — a goal may only be materialized from
  here.
- `PL_MATERIALIZED`: all of its work packages have materialized into Tasks.
  Terminal. A later re-plan creates a **new** run's goals (append-only); it
  never clobbers an already-materialized goal.

### P3 `WorkPackage` — table `work_packages`

The **structural grouping** the flat Task Graph cannot express (Q2). A
WorkPackage = "the set of tasks needed to reach one PlanningGoal, with a
suggested execution shape and resource footprint." It owns the matrix rows for
grouping, ordering, shared resources, and review independence.

| Field | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID FK non-null, indexed | |
| `planning_run_id` | UUID FK → `planning_runs.id` CASCADE non-null | a WP belongs to a revision |
| `planning_goal_id` | UUID FK → `planning_goals.id` CASCADE non-null | every WP pursues exactly one goal |
| `milestone_id` | UUID FK → `milestones.id` SET NULL nullable | optional phase/milestone bucket (§P9); nullable so a plan can have no milestones at all |
| `title` | String(500) non-null | |
| `task_scope` | JSON nullable (P7) | the *materialization intent*: the task titles/descriptions + dependency pairs this WP should become (the translator's input, §4) |
| `execution_mode` | String(20) non-null, closed (P12) | `serial` \| `parallel` \| `parallel_then_serial` — the proposed shape |
| `shared_resources` | JSON nullable (P10) | e.g. `{"files":["a/b.py"],"db":["schema_x"],"api":["/v1/..."],"workspace":["agent-ws"]}` — shared-resource **declaration** for conflict detection |
| `requires_independent_review` | Boolean default false, non-null (P11) | which WP's outputs need a non-builder reviewer |
| `max_parallel_tasks` | Integer nullable (P16) | per-WP parallelism hint; feeds the (deferred) project cap, §8 |
| `created_at` / `updated_at` | DateTime | |

`WORK_PACKAGE_EXECUTION_MODES = ("serial", "parallel", "parallel_then_serial")`
— closed (P12). This is a **proposal** recorded on the WP; the *enforced* order
is always the `task_dependencies` DAG (matrix row: "on conflict, the DAG wins").

### P4 `Milestone` — table `milestones`

An optional ordering/phase bucket for work packages within one run. **Phase is
a closed kind of Milestone, not a separate table** (P9) — this keeps the model
minimal: no `Phase` entity, no nested structure.

| Field | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID FK non-null, indexed | |
| `planning_run_id` | UUID FK → `planning_runs.id` CASCADE non-null | milestones belong to a revision |
| `kind` | String(20) non-null, closed (P9) | `phase` \| `gate` \| `delivery` |
| `seq` | Integer non-null default 0 | ordering key within the run; 0 = unordered |
| `title` | String(500) non-null | |
| `created_at` / `updated_at` | DateTime | |

`MILESTONE_KINDS = ("phase", "gate", "delivery")` — closed. `gate` = a
checkpoint that must pass before later WPs (this is where "review
independence"/quality gates attach); `delivery` = a shippable increment.

```
milestones: UNIQUE(planning_run_id, seq)   -- one bucket per position
```

### P5 — the link table `work_package_tasks`

The **only** new table that connects Planning to Task. It is a *pure link +
intent record*, deliberately NOT a re-statement of assignment or provenance:

| Field | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID FK non-null, indexed | |
| `work_package_id` | UUID FK → `work_packages.id` CASCADE non-null | |
| `task_id` | UUID FK → `tasks.id` **SET NULL** nullable | NULL until materialized (§4); on task deletion the link survives the intent |
| `materialized_at` | DateTime nullable | when the Task row was created for this WP slot |

```
work_package_tasks: UNIQUE(work_package_id, task_id)  uq_wp_tasks
                     CHECK(materialized_at IS NULL OR task_id IS NOT NULL)  ck_wp_tasks_materialized
```

**Why a link table instead of a `Task.work_package_id` column?** Because the
builder's card (`t_b342df97`) explicitly says "Do NOT modify existing Task or
Analysis models unless adding explicit foreign-key links." A link table is the
minimal change that adds an explicit FK link *to* Task without touching the
frozen `Task` model at all, and it lets one WP slot be filled by exactly one
task (UNIQUE) while keeping "which WP proposed this task" queryable in both
directions. This is also the traceability carrier: `Task.created_reason =
ANALYSIS_PLANNING` (existing col) + `work_package_tasks.work_package_id` gives
"this task came from plan revision R, WP W" end to end.

---

## 4. The Materialization Contract (Planning → Task Graph → Assignment)

This is G7 ("planning->Task mapping undefined") resolved. It is the **single
owned lane** that fills the reserved `ANALYSIS_PLANNING` producer slot (audit
Q9 #3), and it replaces the generic-PATCH reachability that the audit found
dangerous (a human can mint `ANALYSIS_PLANNING` tasks today via the transport —
no owned service lane).

**Actor:** `PlanningService.materialize(work_package_id, tenant)` — a new
service in `app/services/` (product layer, not runtime).

**Preconditions (fail-closed, each a named closed code):**
- `PL_PLAN_NOT_APPROVED` — run/goal not `PL_APPROVED` / goal not `PL_APPROVED`.
- `PL_PROJECT_NOT_EXECUTABLE` — `Project.status` not in the existing
  `PROJECT_EXECUTABLE_STATUSES` (`task_execution_service.py:66`, frozen) —
  planning materializes alongside execution, reusing that gate, not a new one.
- `PL_TASK_EXISTS` — a `work_package_tasks` row already has a non-null
  `task_id` for this slot → idempotent no-op (returns existing task), never
  creates a duplicate. This is the materialization idempotency.

**On success (one transaction, owner = service, via `_session_ctx`):**
1. Build the `Task` row **pure** from `work_package.task_scope` (the
   `build_task_fields()` pattern from `task_decomposition_service.py`, which
   returns a dict and is the reusable template — audit Q8 #8):
   - `title` / `description` from `task_scope`
   - `created_reason = "ANALYSIS_PLANNING"` (**finding_id NULL** — the audit
     documents this slot's contract: "derived from an analysis context, no
     single finding")
   - `project_id`, `analysis_run_id` (= the run's bound analysis run),
     `revision_sha` = `planning_run.analysis_revision_sha` — the full
     2D provenance set, so `provenance_consistency` (`task_dao.py:363`) passes
     by construction
   - `status = "pending"` — **G4 honored: create ≠ run**. A materialized task
     is NEVER auto-enqueued (audit Q10 #3).
2. Insert `work_package_tasks` row (`task_id` set, `materialized_at` now).
3. **No edge creation here** unless `task_scope` explicitly carries
   dependency pairs — and then only through the existing
   `task_graph_service.add_edge` / `bulk_add_edges` (frozen, §7.2 rule 2).
   The WP's `execution_mode` is a *hint*; the authoritative order is the DAG
   the translator emits.

**Milestone/`gate` enforcement:** a `gate` milestone blocks *later* WPs'
materialization until all tasks of the gate's WPs are `done`. This reuses
`task_graph_service.ensure_ready` readiness + a service-side check; it does not
invent a second readiness concept (D2/§7.2 rule 2).

---

## 5. Overlap Flags — new vs reusable (acceptance: "no duplicate without justification")

| New entity | Overlaps with (existing) | Verdict + justification |
|---|---|---|
| `PlanningRun` | `AnalysisRun` (also revision-bound append-only) | **NEW, justified.** Different owner/lifecycle: Analysis produces *findings* against a revision; Planning produces a *goal+WP+milestone* plan. Sharing `UNIQUE(project,revision)` shape is a **pattern reuse** (the audit's reusable #8 template), not a duplicate — the two answer different questions ("what's wrong / what should we do"). |
| `PlanningGoal` | `Project.goal` (free text), `AnalysisFinding` | **NEW, justified.** `Project.goal` is the *raw intent* (stays authoritative); a `PlanningGoal` is a *derived operational objective* with `required_capabilities` that feeds assignment. It is a refinement, never a second source of intent (matrix row). Distinct from `AnalysisFinding` (finding = observed defect/claim; goal = intended outcome). |
| `WorkPackage` | `TaskGroup` (does not exist, Q4), `TaskDependency` (flat edge, Q2) | **NEW, justified.** No grouping concept exists anywhere (Q4: no TaskGroup/Milestone/Phase/WorkPackage in the 44 model files). A group owns N tasks with an execution shape + resource footprint → must be a table, cannot be a Task column or an edge attribute (Q2). |
| `Milestone` | `TaskDependency` ordering, `AgentSchedule` | **NEW, justified.** No phase/milestone bucket exists (Q2). It is a *planning-time ordering bucket*, not a schedule (`AgentSchedule` is cron instruction, Q8 #12) and not a task edge. |
| `work_package_tasks` | `Task.provenance` cols (project/analysis_run/finding/revision/reason) | **NEW link, justified + minimal.** Does NOT duplicate the 5 provenance columns (those stay on Task untouched). It adds only the missing *planning-side* link (WP → task) that no existing column carries. |
| `PlanningRun.plan_payload` JSON | LLM plan object stored in `AgentRun` checkpoint (`planning.py:594`) | **NOT a duplicate — different scope.** The checkpoint plan is a *transient LLM artifact of one run's graph state*; `plan_payload` is the *durable, project-scoped, materializable record*. The checkpoint is where the planner LLM step *executes* (D3); the durable row is what the translator *consumes*. Traceability link is `PlanningRun.source_agent_run_id`. |
| Planner `system_role`/topology | `group_planning` runtime (audit Q8 #9/#10) | **NEW topology, reused runtime.** Per trace §7.1(1): a new `system_role` + graph identity on the **same** Checkpointer; the plan *emission* logic is reusable, the group-mention *scoping* is NOT (that is the chat trigger, `planning_scheduler.py:81`). No second runtime (D3). |
| `Task` rows for plans | existing `Task` (frozen) | **REUSED, not new.** `Task.created_reason=ANALYSIS_PLANNING` already reserved (`task.py:37`); this design is its *producer*. No Task schema change. |
| Execution of plan tasks | existing `TaskExecutionService` + Runtime spine (frozen) | **REUSED, not new.** D2/D3: materialized tasks enter the exact frozen path; no second runtime/claim loop. |
| Squad/Team | **not modeled** (see §8) | Avoided by design (audit Q10 #4/#11). |

---

## 6. Invariants (the DB-enforced + service-enforced set)

DB-enforced (in the new migration):
1. `uq_planning_runs_project_revision` — one `PlanningRun` per `(project, analysis_revision_sha)` (append-only revision binding).
2. `uq_wp_tasks` — one task per `(work_package, task)` slot.
3. `ck_wp_tasks_materialized` — `materialized_at` requires `task_id`.
4. `UNIQUE(milestones.planning_run_id, seq)` — one bucket per position.
5. `UNIQUE(planning_goals.planning_run_id, ...)` is NOT required — a run has many goals; goals are only referenced by WPs (FK CASCADE).
6. All 6 planning tables `__tenant_scoped__` (non-null `tenant_id`, auto ORM filter).

Service-enforced (fail-closed, closed codes, per the matrix + §4):
7. `planning_run.status` transitions are the closed `PL_*` set; a `PL_COMPLETED`/`PL_FAILED` run cannot have goals/WPs appended (payload locked).
8. A goal materializes only from `PL_APPROVED`; a WP materializes only when its goal is `PL_MATERIALIZED`-capable and `Project.status ∈ PROJECT_EXECUTABLE_STATUSES`.
9. `created_reason=ANALYSIS_PLANNING` ⇒ `finding_id IS NULL` and `project_id`/`analysis_run_id`/`revision_sha` set — enforced by the existing `provenance_consistency` gate, satisfied by construction in §4.
10. `Milestone.kind='gate'` ⇒ later WPs blocked until gate WPs' tasks all `done` (via `ensure_ready`, no new readiness).
11. `execution_mode` is advisory: the authoritative task order is always the `task_dependencies` DAG; a WP hint that contradicts the DAG is ignored (recorded, not enforced).
12. `shared_resources` overlaps between two `execution_mode='parallel'` WPs **within the same tenant** is reported as a conflict at plan-validation time (the "resource conflict detection" the squad card needs); enforcement stays in the existing Redis workspace locks + advisory lane — planning does not add a lock layer.

---

## 7. Explicit "must NOT duplicate" (carried from the trace §7.2, restated for the builder)

The Planning Domain must respect all seven trace §7.2 rules. In this design:

- No second command inbox — planner Run enqueues via `RuntimeCommandIntake` (D3).
- No second LangGraph runtime/checkpointer/worker — new topology on the shared
  Checkpointer only.
- No second dependency/readiness model — `task_dependencies` + `ensure_ready`
  are the authority (D2/§4/inv 10-11).
- No second assignment fact — `Task.agent_id` stays the single source of truth;
  `PlanningGoal.required_capabilities` and `WorkPackage.shared_resources` are
  *inputs* to the assignment step, not parallel assignment tables (matrix).
- No second scheduling/claim loop — reuse `claim_next_command` lane ordering.
- No second settlement path — plan tasks settle via `TaskRuntimeCompletionHandler` like any task.
- No second `source_type` — a plan task is a `task` source; the planner itself is an
  `orchestration` `system_role` (extend within the closed set or amend deliberately with a migration).

---

## 8. Squad / Team — the deferred-decision the orchestrator card (t_d6b24a82) will own

Per the routing rule, `t_d6b24a82` (Squad/Team V1 design) is a **child** of
`T_0739E600`; it consumes this document. To keep V1 minimal and avoid a
speculative org hierarchy (root brief + audit Q10 #4/#11), this design
**deliberately does NOT add a `Squad`/`Team`/`Role` entity.** Instead it
provides the two *constraints* that "squad orchestration" actually needs, as
data already placed in §3:

- `WorkPackage.shared_resources` (P10) → shared-resource **conflict detection**
  (which WPs can run in parallel vs must serialize; who touches the same
  file/DB/API/workspace). This is the concrete "which tasks can parallel /
  must serialize / share resources" the root brief lists.
- `WorkPackage.requires_independent_review` (P11) → the review-independence
  flag. Enforcement (reviewer Agent ≠ executor Agent) is a **genuine open gap**
  today (audit Q9 #10: Phase 2F `TaskCompletionGate` is fail-open, trace G8)
  and is **deferred** to the squad/review lane, not solved here.

Decision handed to `T_D6B24A82`: whether to introduce a real `Squad` entity
later. Its inputs are now bounded: (a) `required_capabilities` on goals,
(b) `shared_resources` + `execution_mode` on WPs, (c) the existing
`Task.agent_id` as the only assignment fact, (d) the existing A2A + group-
planning entry-Run pattern as the only multi-agent coordination. **No org
hierarchy is added in this card** — if the squad card later adds one, it must
be a data model with a real execution consumer, not a display layer (audit
Q10 #4/#11), and it must not fork `Task.agent_id` or the claim loop.

Two explicitly **deferred** items (named, not dropped):
- **Project-level budget / concurrency cap** (audit Q9 #8, trace G4/G6): the
  minimal `WorkPackage.max_parallel_tasks` (P16) is a *hint* on the plan; a
  true project-level cap + task Run lane key is a runtime-side change and is
  scoped to its own card, not this design.
- **Supervision scheduling consumer** (audit Q7/Q9 #6, trace G1): out of
  planning scope; reuses the `scheduler.py` cron engine, not a new engine.

---

## 9. Risk / Verification notes

- **Read-only design; no code, migration, or config changed on this card.**
  (Matches the Phase 3 first-two-stage contract: audit + design precede build.)
- New-model DDL must keep `create_all` and the migration in lockstep for the
  fresh-DB vs existing-DB case — the f068/f069 "index-lockstep" lesson the
  2D/2C models document. Builder must test up **and** down.
- The `ANALYSIS_PLANNING` dedup: `work_package_tasks.UNIQUE(work_package_id,
  task_id)` prevents double-materializing one slot, but a *new* WP slot in a
  *new* run revision can legitimately map to a *new* task — that is intended
  (append-only revisions), and the `uq_tasks_analysis_finding` dedup on the
  finding path is unaffected (planning path is `finding_id IS NULL`, which PG
  treats as distinct).
- **UNKNOWNs inherited, not resolved here:** G5 (trigger claim-loop wiring in
  deploy/helm/scripts), the 4 DB-state UNKNOWNs from the audit (whether live
  rows use `ANALYSIS_PLANNING`, edge counts, live `AgentSchedule` consumption,
  live supervision values) — these are live-DB questions, correctly out of a
  design card's scope.
- Verification for this card: the matrix (§2) covers all 6 areas; every new
  entity has a §5 justification row; §7 restates the 7 "must NOT duplicate"
  rules. Ready for internal design review before `T_B342DF97` (builder) starts.

---

## 10. Handoff to downstream cards

- **`T_B342DF97` (builder, data model + migration + DAO):** implement §3 tables
  (P1–P5 + `work_package_tasks`), the `PL_*` / `PL_GOAL_*` / milestone-enum /
  execution-mode constants, and the §6 invariants as named constraints. DAO
  query methods named in the card body (`get_plan_for_project`,
  `list_work_packages_by_milestone`,
  `get_assignment_candidates_for_work_package`) map to:
  - `get_plan_for_project` → `planning_runs` by `(project_id, analysis_revision_sha)`;
  - `list_work_packages_by_milestone` → `work_packages` by `milestone_id` (nullable → "no milestone");
  - `get_assignment_candidates_for_work_package` → `work_package_tasks` join `tasks` join `agents` filtered by `PlanningGoal.required_capabilities`.
  Do NOT touch frozen `Task`/`AnalysisRun`/graph models.
- **`T_D6B24A82` (squad/team V1, a child of this card):** read §8 for the exact
  constraint inputs; decide on (or explicitly reject) a `Squad` entity per §8.
- **Planner-run card (future, D3):** new `system_role` + topology on the shared
  Checkpointer, enqueueing via `RuntimeCommandIntake`, emitting the §3 durable
  rows + the `task_scope` payload the §4 translator consumes. Must respect §7.

*Design produced by aco-architect, task t_0739e600, at main = 44651184.*
