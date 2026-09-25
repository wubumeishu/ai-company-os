# Phase 2E — Task / Task Graph / Provenance Readiness Audit

Status: DRAFT (audit, evidence-cited; no product code touched)
Source revision audited: `7497bf7a` (branch `wt/t_55f41487`)
Owner lane: aco-architect · Kanban card t_55f41487
Feeds: Phase 2E assignment/execution spec (child card t_180370c7)

---

## 0. What this document is

A read-only audit of the **Task** persistence model, the Phase 2D **Task
Graph + Provenance** DAO/service, and the **Task API**, focused on three
things the task asked for:

1. The fields that carry **dependencies**, **provenance**, and the **Ready
   Gate** — and which layer owns each.
2. The **state transitions that signal a Task is "Ready" for assignment**
   (i.e. eligible to have a Runtime Run created for it).
3. A **structured mapping of Task states → assignment eligibility**, with
   every row tied to a code location, plus the items that **cannot be
   resolved from code alone** (marked UNKNOWN for the 2E spec to decide).

All claims are grounded in source at this revision; nothing here is inferred
from design intent. Where a design-doc expectation has no matching code, it
is called out as a gap (§6), not silently assumed.

---

## 1. Task model fields (three concern groups)

Authoritative model: `backend/app/models/task.py` (`Task`, `TaskDependency`,
`TaskLog`). Migration that persists the Phase 2D columns: `f069_task_graph_
provenance` (`backend/alembic/versions/v1_11_5_f069_task_graph_provenance.py`),
chained `f068_analysis_persistence → f069 → f070_analysis_task_dedup`
(single head confirmed by `down_revision` links: f070 `down_revision` = f069,
f069 `down_revision` = f068).

### 1.1 Task lifecycle fields (pre-Phase-2D, unchanged by 2D)

| Field | Type | Notes (code location) |
|---|---|---|
| `id` | UUID PK | `task.py:46` |
| `tenant_id` | UUID? FK tenants | `task.py:47` — nullable; a Task may carry **no** tenant context |
| `agent_id` | UUID FK agents (NOT NULL) | `task.py:50` — the executing owner |
| `title`, `description` | str / Text? | `task.py:51-52` |
| `type` | enum `todo`\|`supervision` | `task.py:53` — only two values |
| `status` | enum `pending`\|`doing`\|`done` | `task.py:58` — 3-value, **not** extended by 2D |
| `priority` | enum low\|medium\|high\|urgent | `task.py:63` |
| `assignee` | str, default `"self"` | `task.py:68` — a **string hint** ("self" or a user_id); NOT the agent link. See §5.2 |
| `created_by` | UUID FK users | `task.py:69` |
| `due_date` | timestamptz? | `task.py:70` |
| supervision fields | `supervision_target_user_id` / `supervision_target_name` / `supervision_channel` / `remind_schedule` | `task.py:73-76` |
| `created_at` / `updated_at` / `completed_at` | timestamptz | `task.py:115-119` |

**Key fact:** `Task.status` is a 3-value lifecycle (`pending`/`doing`/`done`),
and `f069` explicitly does **not** change `task_status_enum` (f069 docstring
"no change to task_status_enum (3 values, design ADR-2)"). Readiness is NOT a
status value — it is a **derived** property (see §3).

### 1.2 Task provenance fields (Phase 2D, all nullable → legacy path untouched)

The five-column provenance set, `task.py:84-113`. FK lifecycle per f069:

| Column | FK target | ON DELETE | Nullability | Code |
|---|---|---|---|---|
| `project_id` | projects.id | **CASCADE** | nullable | `task.py:84` |
| `analysis_run_id` | analysis_runs.id | **SET NULL** | nullable | `task.py:90` |
| `finding_id` | analysis_findings.id | **SET NULL** | nullable | `task.py:95` |
| `revision_sha` | (no FK — denormalized snapshot) | — | nullable | `task.py:102` |
| `created_reason` | enum `MANUAL`\|`ANALYSIS_FINDING`\|`ANALYSIS_PLANNING` | — | NOT NULL, server_default `MANUAL` | `task.py:108` |

- The **closed 3-value** `created_reason` is the single most important
  provenance fact: it answers "why does this Task exist?" (`task.py:30-37`).
- Cross-table consistency of these columns is enforced **fail-closed** by
  `TaskProvenanceDAO.provenance_consistency` (`task_dao.py:335`), called from
  the API create/patch handlers before any row is written
  (`api/tasks.py:132, 228`). Rules:
  - `MANUAL` ⇒ all four analysis-side columns are NULL.
  - `ANALYSIS_PLANNING` ⇒ project_id + analysis_run_id + revision_sha present
    (finding_id may be NULL); the run must exist, belong to the project, and
    carry that revision.
  - `ANALYSIS_FINDING` ⇒ all four present; the run must be terminal
    `AN_COMPLETED`, and the finding must belong to that run.
- **Dedup invariant (f070):** `uq_tasks_analysis_finding` =
  `UNIQUE(analysis_run_id, finding_id)` (`task.py:133`, enforced pre-check in
  `TaskProvenanceDAO.converted_finding_ids`, `task_dao.py:271`).

### 1.3 Task Graph (dependency) fields — separate edge table

`task_dependencies` (V1 Task Graph), `task.py:138-171`:

| Column | Meaning | Constraint |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | NOT NULL FK tenants | tenant-scoped (M9) |
| `task_id` | the **downstream** dependee | FK tasks.id, **CASCADE** |
| `depends_on_task_id` | the **upstream** dependency | FK tasks.id, **CASCADE** |
| `uq_task_depends_pair` | one edge per (task_id, depends_on_task_id) | `task.py:169` |
| `ck_task_dep_no_self` | `task_id <> depends_on_task_id` | `task.py:170` |

**Edge direction (critical to get right):** an edge row
`task_id → depends_on_task_id` means *task_id depends on depends_on_task_id*;
the arrow points at the **upstream** dependency. Both ends CASCADE, so deleting
a Task removes every edge referencing it (`task.py:154-161`).

Supervision tasks **never** carry dependency edges (design §3.1); the graph
lane rejects them (`task_graph_service._add_edges` `GRAPH_SUPERVISION_NOT_
ALLOWED`, `task_graph_service.py:269`).

---

## 2. Who owns what (layering) — the load-bearing boundaries

| Concern | Owner (single authoritative layer) | File |
|---|---|---|
| Persist the 5 provenance cols + edge rows | DAO — bounded, tenant-scoped SQL only, no business logic | `task_dao.py` |
| §4.2 provenance consistency rules (cross-table) | DAO `provenance_consistency` (validated by owning service) | `task_dao.py:335` |
| §5.1 edge validation (self/tenant/project/supervision/cycle/exists), §5.2 cycle check, §5.3 ready/blocked, §5.4 execution gate | **Service** `TaskGraphService` — the ONLY owner of graph semantics | `task_graph_service.py` |
| Map closed `GRAPH_*` outcome → HTTP 404/409/200/201 | API transport adapter | `api/tasks.py` |
| Actually create / refuse the Run at gate time | `enqueue_task_runtime` (Runtime intake) | `task_executor.py:44` |
| Flip `task.status` on terminal Run checkpoint | `TaskRuntimeCompletionHandler` | `task_completion.py` |

Consequence worth restating: **readiness is never persisted.** There is no
column, no event, and no background job that writes "this task is now ready."
Readiness is re-derived on demand every time a gate or graph read runs (§3).

---

## 3. The Ready Gate — mechanism (design §5.3 / §5.4)

The Ready Gate is the function `TaskGraphService.ensure_ready`
(`task_graph_service.py:403`). Contract:

- **Input:** a `Task` + `tenant_id`.
- **Output:** the list of **unmet** direct-dependency ids. Empty ⇒ ready.
- **Rule:** for `todo`, `ready` ⇔ every direct dependency's `Task.status` is
  `done` (a task with **no** edges is trivially ready). For `supervision`,
  returns `[]` (no edges exist by construction → always "ready").

The two consumers of this gate:

1. **Auto-enqueue on create** — `api/tasks.py:150` calls
   `enqueue_task_runtime`; on `TaskBlockedError` the task stays `pending`, a
   `TaskLog` explains it, and **no Run is created** (`api/tasks.py:155-172`).
2. **Manual trigger** — `api/tasks.py:274` (`POST /{task_id}/trigger`) →
   `execute_task` → `_try_enqueue_runtime_task` → `enqueue_task_runtime`
   (`task_executor.py:138-176`); the gate is at `task_executor.py:96-100`.

`enqueue_task_runtime` is where the gate actually fires
(`task_executor.py:96-100`): it calls `ensure_ready`, and if the list is
non-empty it **raises `TaskBlockedError`** before any Run is registered.

Readiness is also exposed as a **derived read** (never a write):

- `TaskGraphService.graph` → `GET /tasks/{task_id}/graph` returns
  `ready | blocked | not_applicable` + `blocking` list (`task_graph_service.py:375`,
  `api/tasks.py:408`).
- `TaskGraphService.is_ready` / `ready_states` (single / batch; used by the
  decomposition lane and tests; no N+1 — two bounded reads: `list_edges_for`
  + `task_status_map`).

---

## 4. State transitions that make a Task "Ready for assignment"

"Ready for assignment" = the Task is **eligible to have a Runtime Run created
for it** (i.e. it can be assigned to / executed by an agent). There is no
single state bit; it is the **conjunction** of conditions below. A Task becomes
assignable when *all* of these hold simultaneously:

| # | Condition | Source of truth | Evidence |
|---|---|---|---|
| C1 | `type == "todo"` (deps semantics) **or** `type == "supervision"` (occurrence-based, no deps) | `task.type` | `task_graph_service.py:269, 410` |
| C2 | `status == "pending"` | `task.status` | written `pending` at create; `doing` on enqueue; `done` terminal |
| C3 | **Dependency readiness:** every direct dep is `done` (or no edges) — i.e. `ensure_ready` returns `[]` | derived from `task_dependencies` + dep `Task.status` | `task_graph_service.py:403-416` |
| C4 | Agent exists, not deleted, `agent.tenant_id is not None`, `agent.primary_model_id is not None` | `enqueue_task_runtime` preconditions | `task_executor.py:66-81` |
| C5 | Agent not expired (manual trigger path only) | `is_agent_expired` | `api/tasks.py:283-285` |
| C6 | Tenant context is consistent (graph lane requires a non-None tenant; provenance lane requires agent tenant == project tenant) | gate + intake tenant checks | `api/tasks.py:339-348`, `task_decomposition_service.py:238-245` |
| C7 | Runtime v2 is selected for this source (`decide_runtime_v2` → `use_v2=True`), else enqueue returns `None` and the task stays pending with an error log | `decide_runtime_v2` | `task_executor.py:59-64, 213` |

**The moment a Task becomes "Ready for assignment" is therefore the
transition: `status pending` + (all deps `done` / no deps) + agent/prereqs C4–C7
satisfied ⇒ `enqueue_task_runtime` creates the Run and sets `task.status =
"doing"`** (`task_executor.py:127`). If C3 fails at that moment, the task is
**blocked** and the transition does not happen — it re-stays `pending` with a
TaskLog (`task_executor.py:189-195`, `_log_blocked` `task_executor.py:227`).

### 4.1 The complete status transition table (todo + supervision)

Every `task.status` write in the codebase, with the trigger:

| From | To | Triggering event / code location |
|---|---|---|
| — (new row) | `pending` | Task INSERT (legacy + provenance + conversion all default `pending`; `Task.status` default `pending`, `task.py:60`; conversion explicitly `status="pending"`, `task_decomposition_service.py:188`) |
| `pending` | `doing` | Run successfully enqueued — `enqueue_task_runtime` sets it **after** the gate passes and the Run is registered (`task_executor.py:127`) |
| `doing` | `done` | Runtime Run reaches terminal `completed` (todo success) — `TaskRuntimeCompletionHandler` (`task_completion.py:137-140`); also settable manually via PATCH |
| `doing` | `pending` | Run terminal `failed` (todo) — `task_completion.py:150-154` (revert to pending, `completed_at=None`) |
| `doing` | `pending` | Run terminal `cancelled` — `task_completion.py:145-149` |
| `done` (supervision) | `pending` | Supervision Run `completed` resets to pending for the **next occurrence** — `task_completion.py:141-144` (supervision is recurring; todo `done` is terminal) |
| `*` | `*` | Manual PATCH `TaskUpdate.status` is an unrestricted write (`api/tasks.py:237`) — no guard today |

**Readiness overlay (derived, not a status):**

| Graph state | Computed when | Meaning for assignment |
|---|---|---|
| `ready` | task has no edges, **or** every direct dep `Task.status == "done"` | gate passes; eligible to enqueue (`task_graph_service.py:388,393`) |
| `blocked` | some direct dep not `done` | gate raises `TaskBlockedError`; stays `pending`, no Run (`task_graph_service.py:395`) |
| `not_applicable` | `type == "supervision"` | no dep semantics; supervison path is occurrence-based (`task_graph_service.py:385-386`) |

Because a dep's `done` is **terminal** (todo `done` has no reopen path,
§5.3 design; confirmed — the only `done` writer is `task_completion.py:138`
plus manual PATCH), a task's readiness, once `ready`, does not regress unless
an edge is added/removed. Re-block can only happen by **removing a done dep's
done-ness is impossible** — it happens by adding a new non-done edge
(`remove_task_dependency` unblocks lazily, `api/tasks.py:383`), so readiness is
re-derived at the next gate/read, never pushed.

---

## 5. Provenance, Assignment & Idempotency facts (evidence for the 2E spec)

### 5.1 Where provenance lands a Task

- **Manual create:** `POST /agents/{agent_id}/tasks` — provenance fields ride
  the INSERT (`api/tasks.py:108-113`); fail-closed `provenance_consistency`
  gate before write (`api/tasks.py:132`).
- **Analysis→Task conversion:** `TaskDecompositionService.convert` writes each
  converted finding's Task via `TaskProvenanceDAO.create_with_provenance`
  (`task_decomposition_service.py:288`); always `status="pending"` (G4 —
  conversion NEVER enqueues; `task_decomposition_service.py:16,230`); tenant
  gate `agent_tenant == project.tenant_id` (`task_decomposition_service.py:244`).
- **Graph authoring:** `POST /{task_id}/dependencies` / `DELETE
  /{task_id}/dependencies/{dep_task_id}` / `GET /{task_id}/graph`
  (`api/tasks.py:352,383,408`) — **edge writes never enqueue or execute
  anything**; execution stays on the separate trigger path (Task ≠ Run).

### 5.2 The "Agent link" today is the `agent_id` FK, not `assignee`

`Task.agent_id` (NOT NULL, FK agents) is the real executing-owner link.
`Task.assignee` is a legacy **string** column ("self" or a user_id, default
`"self"`, `task.py:68`) that the Task Graph / provenance / gate lanes **do not
use**. This is directly relevant to the 2E "explicit_agent_id for V1" change:
the natural owner already exists as `Task.agent_id`; 2E must decide whether
`agent_id` is *the* explicit link or whether a separate nullable
`explicit_agent_id` coexists with the mandatory `agent_id`.

### 5.3 Idempotency rules (duplicate execution attempts) — the decisive fact

Run registration dedups by **(tenant_id, source_type, source_execution_id)**
via `_find_start_retry` (`adapter.py:67-77`). The `source_execution_id` is set
by `enqueue_task_runtime`:

- **todo:** `f"task:{task.id}"` — **stable** (no occurrence suffix).
- **supervision:** `f"task:{task.id}:supervision:{occurrence_id}` —
  **per-execution** (occurrence UUID generated when none supplied,
  `task_executor.py:84-85,87`).

Consequences (flagged for 2E — two of these are UNKNOWN from code alone):

- I-1 (todo, stable): re-triggering a todo task reuses the **same** Run
  identity; `RunHandle.created` is `False` on the retry and the intake logs a
  TaskLog only when `created` (`task_executor.py:128-134`).
- I-2 (UNKNOWN): a **done** todo task has **no status guard** on the trigger
  path (`api/tasks.py:274-296` checks only agent-expired + 404). Re-triggering
  a `done` todo will re-run `_find_start_retry` (find the completed Run) and
  then `task_executor.py:127` will flip `task.status = "doing"` — i.e. a
  terminal `done` task can be re-opened to `doing`. **Whether this is intended
  is not decidable from code; 2E must rule on it.**
- I-3 (supervision): by design, each trigger is a fresh occurrence
  (occurrence-id suffix) → no start-retry dedup collision across occurrences;
  the `TaskLog` receipt is idempotent per `(run_id, checkpoint_id)` via
  deterministic UUIDv5 (`task_completion.py:32-33,111-115`).

### 5.4 Preconditions the 2E spec must enumerate — code coverage vs. gap

The 2E body asks for these four precondition classes. Coverage today:

| 2E precondition class | Enforced today? | Where / gap |
|---|---|---|
| **Dependency readiness** | ✅ yes | `ensure_ready` gate (`task_executor.py:96-100`) |
| **Tenant match** | ✅ yes, task↔agent via `agent.tenant_id` + `task.agent_id`; ✅ analysis lane agent-tenant==project-tenant | `task_executor.py:71-75`, `task_decomposition_service.py:238-245`. **Gap:** the gate/intake path does NOT assert `task.tenant_id == agent.tenant_id` directly — task tenant is inferred through the agent only. |
| **Project state** | ❌ **NO** — the execution gate checks only task deps, never project state | `task_graph_service.py:403` reads deps only. A "project must be active/unchanged" precondition does not exist in code today. |
| **Workspace availability** | ❌ **NO** — no workspace/git-worktree availability check anywhere in the task intake path | `enqueue_task_runtime` checks agent/model/tenant only (`task_executor.py:59-81`). "Workspace availability" has no code equivalent. |

**UNKNOWN items for 2E (cannot be resolved from code alone):**

- U-1: Should a **project-state** precondition be added before enqueue (which
  project state blocks? active-only? a frozen/analysis-in-progress project?).
- U-2: Should **workspace availability** be a precondition (what is the
  workspace bound — a git worktree, a scratch dir, a per-tenant cap)?
- U-3: The done-todo re-trigger behavior (§5.3 I-2) — terminal or re-openable?
- U-4: Does 2E use `Task.agent_id` as the explicit link, or introduce a
  separate `explicit_agent_id` (nullable) alongside the NOT NULL `agent_id`?
- U-5: Whether the "completion → recompute downstream ready" push (design §5.4
  "完成后行为", the "▶ 下游 T1,T2 已 ready" hint) is in scope for 2E —
  **the `list_dependents` DAO primitive exists** (`task_dao.py:70`) but has
  **no production consumer** as of this revision (verified by search); readiness
  is currently **pull-only** (re-derived at gate/read), never pushed.
- U-6: The manual PATCH `TaskUpdate.status` path writes `status` with **no
  guard** (`api/tasks.py:237`) — can a human force a `done`/`doing`/`pending`
  value that contradicts the graph? 2E must decide the guard policy.

---

## 6. Gaps / risks the audit surfaced (architecture protection)

1. **No push on upstream completion.** Design §5.4 specifies a lazy
   "recompute + hint" on `done`; only the `list_dependents` read primitive was
   shipped, with no caller. Readiness is entirely pull-based today. If 2E
   wants downstream tasks to surface as ready **without** a human re-polling,
   a new (small, bounded) completion-side hook is required — but it must stay
   in the gate/notify owner, not in the persistence layer (layering rule §2).
2. **`agent_id` is NOT NULL but "assignment" is the legacy string `assignee`.**
   Two notions of "who does this" coexist. 2E should not invent a third; it
   should either promote `agent_id` to the explicit-assignment field or remove
   the ambiguity in `assignee`.
3. **Intake tenant check is agent-side only.** A `task` row with a `tenant_id`
   that differs from `agent.tenant_id` is not directly rejected in the gate
   path (it is only in the analysis-conversion path). For a *project* task
   whose provenance ties it to a tenant, 2E should close this gap explicitly.
4. **Manual PATCH status has no guard** (§5.4 U-6). Combined with U-2, a
   human can currently move a task's status out from under the graph. 2E must
   decide whether status writes are restricted to the owning Runtime/completion
   handler.

---

## 7. Evidence index (file:line)

- Model / enums / constraints: `backend/app/models/task.py:46-171`
- Provenance + edge DAO: `backend/app/dao/task_dao.py:44-333`
- Graph service (validation, ready, gate): `backend/app/services/task_graph_service.py:55-416`
- Execution gate + intake: `backend/app/services/task_executor.py:44-135,179-235`
- API (CRUD + graph + trigger): `backend/app/api/tasks.py:85-434`
- Terminal status writes: `backend/app/services/agent_runtime/task_completion.py:135-161`
- Run dedup (idempotency): `backend/app/services/agent_runtime/adapter.py:67-77,245-235`
- Migration DDL: `backend/alembic/versions/v1_11_5_f069_task_graph_provenance.py`
- Design references: `docs/PHASE_2D_TASK_GRAPH_PROVENANCE_DESIGN.md` §3–§6, §8 R1
