# Phase 3 — Agent Assignment & Concurrency Validation (implementation note)

Task: `t_9820b3d3` — "Implement Squad/Assignment integration and concurrency validation"
Owner: aco-builder. Consumes `PHASE_3_SQUAD_ORCHESTRATION_V1_DESIGN.md` (§8 input/output
contract, t_d6b24a82) and the planning lane (`planning_service.py`, t_12874a76; f071
persistence, t_b342df97).

## 1. What exists now

- `backend/app/services/assignment_service.py` (new) — `AssignmentService.apply_assignment(db,
  work_package_id, current_user)`: the Agent-Assignment lane over a materialized ProjectPlan.
- `backend/app/dao/task_dao.py` `TaskProvenanceDAO.update_agent_binding` (additive) — the only
  planning-side write to the single assignment fact `Task.agent_id` (frozen A2/S5 fact).
- `backend/tests/test_assignment_service.py` (new) — 20 DB-free predicate tests + 5
  live-schema tests.

No new table, no new column, no new state machine, no org entity, no second graph/assignment
fact, no second lock layer (design S1/S4/S5). The AssignmentPlan artifact is a computed report
(§8), never persisted.

## 2. Contract

Inputs per work package (all pre-existing rows): `PlanningGoal.required_capabilities`,
`work_packages.task_scope` (§4.1 slot shape: slot/kind/required_capabilities/
candidate_agent_ids/depends_on_slots/per-slot shared_resources), `execution_mode`,
`shared_resources`, `requires_independent_review`, `max_parallel_tasks`, plus the frozen
`task_dependencies` subgraph of the WP's materialized tasks and the live roster.

Slot↔task pairing is by **slot title** (the materialized Task carries the slot's `title`;
`work_package_tasks` link rows have random UUID keys, so DB ordering is not a positional
contract). A title that matches no linked task (or a task linked to two slots) fails closed
with `PL_INVALID_INPUT`.

Candidate resolution (§4.2): re-validate planner-emitted `candidate_agent_ids` against the
live roster — active (not soft-deleted), `primary_model_id` bound, not expired (the Phase 2E
P5 availability gate); exactly one survivor is picked per task (first in planner-declared
order → deterministic). A flagged reviewer slot may only pick a candidate **disjoint** from
every builder pick (REV-1).

Constraint layer (pure predicates, DB-free, fail-closed closed codes):

| code | rule | behavior |
|---|---|---|
| `PL_RESOURCE_CONFLICT` | CONF-1 (distinct-agent) / CONF-4 (same-agent) concurrent-possible mutators share a resource | refuses the WP before any write |
| `PL_REVIEWER_NOT_INDEPENDENT` | REV-1 no disjoint active reviewer / no review slot; REV-2 reviewer does not block builders via the DAG | refuses the WP before any write |
| `PL_NO_CANDIDATE_AGENT` | §4.2 empty or fully-inactive candidate list | refuses the WP before any write |
| `PL_PARALLELISM_ADVISORY` | CONF-5 concurrent mutator count > `max_parallel_tasks` | advisory only, never refuses |
| `PL_INVALID_INPUT` / `PL_RUN_NOT_COMPLETED` | shape/length/bounded-graph defects; run not PL_COMPLETED | refuses before any write |

Mutators = slot kind ∈ {build, gate}; `review`/`other` are observers and never conflict.
Resource sets: per-slot `shared_resources` override, else the WP-level declaration
(closed shape `{files, db, api, workspace}`). Concurrency check = "two tasks may not both be
the active mutator of the same resource for the same Agent" = CONF-1 + CONF-4 at
plan-validation; the runtime enforcer stays the existing tenant-prefixed Redis workspace lock
(A8) + the DAG. "A task may not be assigned to two agents simultaneously" holds structurally
(one non-null `Task.agent_id`, one task per (WP, slot) via `uq_wp_tasks`) AND by
determinism: two racing applies converge on the same single writer value; an unchanged
binding is a zero-write no-op.

Review-independence propagates to execution scheduling purely through the blocking
`builder_task → reviewer_task` DAG edge (REV-2) — no new scheduler. CONF-3 (tenant
isolation) is an invariant, not a per-pair check: the acting user's `tenant_context` scopes
every read (the ORM tenant-injection filter makes a cross-tenant WP id resolve to `None`, and
the roster batch read can never return a foreign tenant's agents).

On any fail-closed code the whole package is refused BEFORE any `Task.agent_id` write and
the planner is re-invoked or a human resolves; the `AssignmentPlan` artifact
(`{work_package_id, slots:[{slot, task_id, chosen_agent_id, kind}], constraint_report,
review_bindings}`) is returned/logged either way.

## 3. Acceptance status (verified)

- Conflicting assignments are detected and reported: 3 DB-free + 2 live tests assert each
  fail-closed code fires (resource overlap CONF-1/CONF-4, no-disjoint-reviewer, missing
  blocking edge, no-candidate, open-run) and that the DB fact is UNCHANGED after a refusal.
- Independent-review flag propagates to scheduling: `requires_independent_review` WPs yield a
  `ReviewBinding` (reviewer task + distinct agent + blocking edge) in the artifact; the live
  pass test asserts `Task.agent_id` of reviewer ≠ builder and the DAG edge exists.
- No new organizational layer: diff is one new service + one additive DAO method + tests;
  zero schema change (verified: no alembic migration in this branch; the only planning DDL
  is the pre-existing f071 from t_b342df97).

## 4. Evidence

- DB-free tier: `tests/test_assignment_service.py` 20/20 (always run, no DB).
- Live tier: 5/5 against scratch Postgres `clawith_t_12874a76_plansvc`
  (`DATABASE_URL=postgresql+asyncpg://clawith:clawith@127.0.0.1:5432/clawith_t_12874a76_plansvc`
  + main-tree venv `I:/project/AI Company OS/backend/.venv/Scripts/python.exe`,
  `PYTHONPATH=<worktree>/backend`; model registration via `import app.models.analysis/planning/task`
  in the fixture — do NOT `import app.main` in the test process, it hangs).
- Regression scoped to touched contracts: planning service 16, planning DAO 19,
  task-decomposition 11, analysis-e2e + persistence/migration 17, task-graph
  api/provenance/migration + execution service 70 — all green.
- Static: ruff clean + pyright 0 errors on the three touched files.

## 5. Deferred (named, inherited from the design §11)

- D-1 structured `Agent.capabilities` join — candidates stay planner-emitted.
- D-2 project/task concurrency cap + lane key — CONF-5 advisory only.
- D-3 runtime `TaskCompletionGate` fail-open (G8) — independence enforced at assignment time.
- D-4 supervision/deadline consumers — out of scope.
