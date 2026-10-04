# Project → Planning → Task Graph Connectivity & Provenance Audit

Card t_7858d198 · read-only · baseline main == 676b1caf
Method: every hop traced from **source models → migrations → owning service
→ DAO → API/executor**. Historical reports were NOT trusted as evidence; each
claim below carries live `file:line` proof. Findings classified FACT /
OBSERVATION / INFERENCE / UNKNOWN, and BLOCKING / NON-BLOCKING / DEFERRED.

---

## 1. Verified dependency map (Project → Task Graph, with evidence)

### HOP 1 — Project (birth / Intake)
- Entity: `projects` table, status enum 10 values.
  - `backend/app/models/project.py:39-101` (Project), `:103-167` (Repository).
- Migration: `f066_add_project_repo_tables` — `projects` + `repositories` +
  `project_status_enum`.
  - `backend/alembic/versions/v1_11_4_f066_add_project_repo_tables.py:114,143`.
- Owning lane: `ProjectIntakeService` — RECEIVED → SOURCES_OK → INITIALIZED.
  - `backend/app/services/project_intake_service.py:331`
    (`transition(project,_STATUS_INITIALIZED)`).
- Status transition legality + terminal set:
  - `backend/app/services/intake_security.py:569-575` (`INTAKE_TRANSITIONS`,
    `TERMINAL_INTAKE_STATUSES = {INITIALIZED, REJECTED}`).
- Persistence seam (the ONLY status write path the DAO exposes):
  - `backend/app/dao/project_intake_dao.py:131-147` (`transition`),
    `:149-171` (`reject`), `:173-182` (`mark_sources_ok`).
- API transport: `backend/app/api/projects.py` `POST /` `:134`, `POST
  /{id}/validate` `:202`.

### HOP 2 — Project → Analysis  (CONNECTED, two independent seams)
Seam A — DB FK: `analysis_runs.project_id → projects.id ON DELETE CASCADE`.
- Model: `backend/app/models/analysis.py:109-111` (AnalysisRun.project_id).
- Migration: `f068_analysis_persistence` creates `analysis_runs` with
  `UNIQUE(project_id, revision_sha)`.
  - `backend/alembic/versions/v1_11_5_f068_analysis_persistence.py:154`.

Seam B — Revision binding (typed, not a JSON guess):
`AnalysisRun.revision_sha` ← `repositories.locator.resolved_rev`
- OQ-5 typed carrier: `backend/app/models/analysis.py:119`.
- Revision source written by GitAcquisition:
  `backend/app/services/git_acquisition_service.py:1040`
  (`loc["resolved_rev"] = resolved_rev`).
- Launch gate refuses a source with no verified revision (fail closed):
  `backend/app/services/analysis_service.py:389-409`
  (`_revision_from_locator`, AN_SOURCE_INVALID).

Owning service: `AnalysisService.launch` → `record_findings` (closes run as
AN_COMPLETED) → `promote_finding`.
- `backend/app/services/analysis_service.py:182-262` (launch; sets
  `project.status -> ANALYZING` at `:253`), `:264-290` (record_findings),
  `:292-326` (promote_finding).
- Findings carry a provenance tag + bounded evidence (README-as-truth
  forbidden): `backend/app/models/analysis.py:142-180` (AnalysisFinding),
  `backend/app/services/analysis_service.py:411-448` (`_validate_finding`).

API transport:
- `POST /{project_id}/analysis/{run_id}/...` — launch
  `backend/app/api/projects.py:460`, findings `:513`, promote `:556`,
  read/knowledge `:591`, `:613`.

### HOP 3 — Analysis → Planning  (CONNECTED, logical revision key)
- `PlanningRun` binds one Project + one analysis revision:
  `planning_runs.project_id → projects.id CASCADE`
  (`backend/app/models/planning.py:160-162`) and
  `analysis_revision_sha` String(64) (`:166`), with
  `UNIQUE(project_id, analysis_revision_sha)` (`:151-154`).
- Migration: `f071_planning_persistence` — five tables
  (planning_runs / planning_goals / work_packages / milestones /
  work_package_tasks).
  - `backend/alembic/versions/v1_11_5_f071_planning_persistence.py:170-345`.
- Owning service: `PlanningService.create_plan` — requires the source run to
  be `AN_COMPLETED` and to belong to the project (fail-closed G1),
  then materializes.
  - `backend/app/services/planning_service.py:485-550` (G1/G2/R gates),
    `:612-770` (write phase).
- Finding → goal traceability via `PlanningGoal.analysis_finding_ids`
  (JSON, bounded list): `backend/app/models/planning.py:236`; populated in
  `backend/app/services/planning_service.py:632`.

### HOP 4 — Planning → Task Graph  (CONNECTED, the explicit link the card asks for)
- The ONLY new Planning↔Task link: `work_package_tasks` (a pure link +
  intent record, `task_id` NULL until materialized).
  - `backend/app/models/planning.py:349-407` (WorkPackageTask).
  - FK `work_package_tasks.task_id → tasks.id ON DELETE SET NULL`:
    `backend/alembic/versions/v1_11_5_f071_planning_persistence.py:341`.
- Materialization: `PlanningService.create_plan` writes a real `Task` row
  with the full Phase-2D provenance set (`created_reason=ANALYSIS_PLANNING`),
  links it, and wires DAG edges through the **frozen** `task_graph_service`
  (no second graph, no second readiness model).
  - Task write: `backend/app/services/planning_service.py:710-732`.
  - Edge wiring: `:734-759` (`task_graph_service.bulk_add_edges`).
- The single Task provenance set + graph edge table:
  - `backend/app/models/task.py:84-113` (five provenance columns),
    `:138-171` (TaskDependency).
  - Migration: `f069_task_graph_provenance`
    (`backend/alembic/versions/v1_11_5_f069_task_graph_provenance.py:181-284`);
    dedup `f070` (`..._f070_analysis_task_dedup.py`, UNIQUE
    `(analysis_run_id, finding_id)`).
- Graph semantics owner (ready/blocked/execution gate + cycle + advisory
  lock): `backend/app/services/task_graph_service.py:196-417`.
- End-to-end provenance query:
  `PlanningService.provenance_for_task`
  (`backend/app/services/planning_service.py:788-850`) —
  Task → work_package_tasks link (`get_link_for_task`,
  `backend/app/dao/planning_dao.py:594`) → WP → goal → findings →
  planning_run.

Alternate Analysis → Task lane (finding-level, no planning):
`TaskDecompositionService.convert` writes `created_reason=ANALYSIS_FINDING`
Tasks.
- `backend/app/services/task_decomposition_service.py:183-197`
  (`build_task_fields`), `:209-297` (convert).
- API: `backend/app/api/projects.py:650` (`convert_analysis_run_to_tasks`).

### HOP 5 — Task Graph → Runtime execution  (CONNECTED, reuses Phase-2F spine)
- Execution gate: `TaskExecutionService.execute` (P1–P8 fail-closed order,
  first failure wins).
  - `backend/app/services/task_execution_service.py:145-233` (execute),
    `:316-385` (`_gate`), executable-set constant `:66-68`.
- Enqueue spine → the **existing** Phase-2F Runtime (NOT a second runtime):
  `enqueue_task_runtime`.
  - `backend/app/services/task_executor.py:44-144` (gate re-run at `:108-112`,
    status flip to `doing` at `:144`), command-keying `:88-99`.
- Plan → execution hand-off adapter (thin, owns no lifecycle of its own):
  `PlanExecutionService.enqueue_plan_tasks` runs the plan's materialized tasks
  through the real Phase-2E gate.
  - `backend/app/services/plan_execution_service.py:176-300`
    (assignment-first per `:229-244`, topological order `:105-142`).
- Assignment lane (single `Task.agent_id` fact + fail-closed constraints):
  `AssignmentService.apply_assignment`.
  - `backend/app/services/assignment_service.py:731-905`.
- API transport: `backend/app/api/tasks.py` `POST /{task_id}/execute` `:502`,
  `GET /{task_id}/execution` `:541`, graph `:389/:420/:445`.

### HOP 6 — Execution → Settlement → Completion  (CONNECTED)
- Settlement of a settled Run into Task status (done / pending-on-failure +
  TaskLog receipts): `TaskRuntimeCompletionHandler`.
  - `backend/app/services/agent_runtime/task_completion.py:56-151`
    (flip to done `:137-138`).
- Project completion gate: `CompletionService.evaluate_project` — CP predicate
  (`:576-614`), single COMPLETED write site (`:888-890`).
  - `backend/app/services/completion_service.py:801-897`.
- Delivery gate is fail-closed: `CD_*` decision requires the scope to be
  `CP_OK` + a SEALED+APPROVED artifact set; no evidence →
  `CD_NOT_COMPLETED`, structurally no delivery.
  - `backend/app/services/completion_service.py:633-661`
    (`delivery_decision_code`);
    `backend/app/services/delivery_service.py:315-451`.

---

## 2. Findings

### F1 — PENDING_CONFIRMATION and EXECUTING are unreachable Project states
**FACT.** Both are declared in the 10-value `project_status_enum`
(`backend/app/models/project.py:59-63`) and are members of
`PROJECT_EXECUTABLE_STATUSES` (`task_execution_service.py:66-68`), yet the
**only** `project.status` write sites in the whole app are:
RECEIVED/SOURCES_OK/INITIALIZED/REJECTED (`project_intake_dao.py:139-182`),
ANALYZING (`analysis_service.py:253`), COMPLETED (`completion_service.py:889`).
No code path ever writes `EXECUTING` or `PENDING_CONFIRMATION`.
- Classification: **NON-BLOCKING / DEFERRED** (feature deferral). The
  confirmation UI is intentionally inert — documented at
  `backend/app/services/analysis_service.py:304` ("the PENDING_CONFIRMATION
  lane stays INERT — no code path sets it").
- Consequence: the "execution-allowed" set is over-broad; only `ANALYZING`
  is actually reachable among the three members, so every executable check
  in practice reduces to `status == ANALYZING` until a writer lands.
- Note: `PROJECT_EXECUTABLE_STATUSES` is **duplicated** as a frozen literal in
  two files (`task_execution_service.py:66-68` and
  `completion_service.py:199`) rather than a shared import — an OBSERVATION
  (D5 consistency risk), NON-BLOCKING.

### F2 — No physical FK between planning_runs and analysis_runs; integrity is logical-only
**FACT.** `planning_runs.analysis_revision_sha` is a plain String(64)
(`planning.py:166`; migration `f071` declares FKs only on project_id,
planner_agent_id, source_agent_run_id, tenant_id —
`f071...py:206-209`, no FK to analysis_runs). The Plan↔Analysis link is
therefore enforced **only** at the service layer
(`planning_service.py:541-544`: run must belong to the project; and G1
requires `AN_COMPLETED`). Because `AnalysisRun` rows are never physically
deleted by this lane (append-only), there is no cascade gap today; but there
is no DB-level referential guard on the revision key.
- Classification: **NON-BLOCKING / OBSERVATION.** Consistent with the
  analysis lane's "copied, not owned" precedent (`analysis.py:112-116`
  SET-NULL on agent), and with design decision D2 (no second authority). Flag
  as a known referential-integrity limit; a future GC of AnalysisRun rows
  would need a matching PlanningRun revalidation.

### F3 — Planning / Assignment / Plan-Execution services have NO HTTP API surface
**OBSERVATION.** `planning_service`, `plan_execution_service`,
`assignment_service` are consumed **only** by the test suite
(`tests/test_planning_service.py`, `tests/test_assignment_service.py`,
`tests/test_planning_execution_chain_e2e_acceptance.py`); no router in
`app/api/` exposes `create_plan` / `apply_assignment` / `enqueue_plan_tasks`
(verified: `grep planning/plan_execution/assignment` across `app/api/` returns
no transport handler). The chain is real, DB-backed, and test-verified, but
the Planning→Task-Graph half is not reachable from the product API/CLI as
shipped.
- Classification: **NON-BLOCKING / DEFERRED.** The connectivity itself is
  intact; the gap is exposure. If the Foundation intends to *operate* plans,
  a transport lane is required.

### F4 — Unprovenanced tasks pass the Project gate (fail-open by omission)
**INFERENCE.** `TaskExecutionService._gate` P8/P2 (`task_execution_service.py:345-362`)
only applies the Project-state check `if project is not None`. A legacy
`MANUAL` task with `project_id NULL` skips P8/P2 entirely, so an unprovenanced
Task can be enqueued/executed with **no** project-executability check. This is
the deliberate Phase-2E scope (project gate binds only provenanced work), but
it means the "correct gate on the way to completion/delivery" applies only to
Tasks carrying `project_id`.
- Classification: **NON-BLOCKING / OBSERVATION.** Intended for the legacy path;
  flag because a provenance-free task is outside the Project→…→Completion
  chain by construction.

### F5 — No second Runtime / Task Graph / Completion semantics (negative finding)
**FACT (verified absence).** Single sources of truth confirmed:
- Task Graph edges: only `task_dependencies` + `task_graph_service`
  (planning wires edges through the frozen service, `planning_service.py:738,752`).
- Assignment: only `Task.agent_id` (`assignment_service.py:29-31,888-899`).
- Runtime: planning/execution hand-off reuses `enqueue_task_runtime` +
  Phase-2F worker (`plan_execution_service.py:14-16`;
  `task_executor.py:44`). No parallel graph, readiness, assignment, or
  completion model exists.

### F6 — Completion / Delivery gate is fail-closed (no evidence → no completion)
**FACT.** `CP` requires ≥1 in-scope task + every CT + every delivery-milestone
CW + tenant match (`completion_service.py:576-614`); `COMPLETED` is published
only on a FRESH `CP_OK` while the project is in an executable status
(`completion_service.py:888-890`); Delivery refuses unless the scope is
`CP_OK` AND cited artifacts are SEALED+APPROVED (`completion_service.py:633-661`).
No fail-open path to completion/delivery was found. **NON-BLOCKING (positive).**

---

## 3. Orphaned / dangling states & missing connectors (flagged)
1. **Unreachable Project states**: `EXECUTING`, `PENDING_CONFIRMATION`
   (F1) — dead enum values with no writer; deferred confirmation feature.
2. **No transport for the Planning→Task-Graph half** (F3) — services exist and
   are test-exercised but no API/CLI lane; a missing *exposure* connector.
3. **Logical (non-FK) Plan↔Analysis link** (F2) — referential integrity on
   `analysis_revision_sha` rests on the service gate, not the schema.
4. **Unprovenanced-task gate bypass** (F4) — legacy MANUAL tasks are outside
   the project-executability gate by design.
5. No dangling FKs found: all cross-model links use real
   `ForeignKeyConstraint`s with matching ondelete semantics
   (`f066`/`f068`/`f069`/`f071`), and CASCADE/SET-NULL choices are
   documented per the "copied, not owned" precedent.

## 4. Provenance end-to-end queryability (confirms the acceptance)
`Project.id → analysis_runs.project_id(FK,CASCADE) →
analysis_runs.revision_sha(=repositories.locator.resolved_rev) →
planning_runs(analysis_revision_sha + project_id, UNIQUE) →
work_packages → work_package_tasks.task_id(SET NULL) →
tasks(project_id/analysis_run_id/finding_id/revision_sha/created_reason) →
task_dependencies(DAG) → [execution] AgentRun(source_execution_id=
`task:<id>[:retry:<attempt>]`) → [settlement] Task.status=done + TaskLog →
[completion] C5 decision rows + Project.status=COMPLETED →
[delivery] delivery_records (sealed+approved).
Backward query: `PlanningService.provenance_for_task`
(`planning_service.py:788-850`) returns the full Task→WP→goal→findings→run
chain.

## 5. Deferred items surfaced (do NOT assume fixed)
- Confirmation/PENDING_CONFIRMATION UI (deferred; inert by design).
- Project-level max-parallelism cap `CONF-5` (advisory only; runtime change
  deferred D-2) — `assignment_service.py:515-550`.
- Structured capability→Agent matching (deferred D-1) —
  `planning.py:112-118`, `assignment_service.py:58-59`.
- Runtime enforcement of reviewer-independence REV-3 (deferred D-3) —
  `planning.py:294-296`.
