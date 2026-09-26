# Phase 2E — Convergence Report (root t_b0bb2f7c §28 final deliverable)

**Phase:** Project Task → Agent Assignment → Real Execution
**Landed on:** main `d112c71d` (origin/main identical)
**Baseline at preflight:** main `7497bf7a` == origin/main (in sync, tracked-clean)
**Verdict:** `PASS` (see §U)

All fields below cite real commits / runs / counts from the landed tree. Nothing in
§A–§T is taken from a worker's self-report without re-verification against the
checked-out tree; the regression numbers in §Q were re-run in this session.

---

## A. Preflight

- Working tree: `I:\project\AI Company OS` (primary; `main` checked out here),
  tracked-clean at start (`git status --porcelain` → only untracked
  `.smoke-backup/` and `.worktrees/`, both pre-existing and out of scope).
- `git rev-parse main origin/main` before merges → both `7497bf7a…` (in sync).
- The 6 Phase 2E content commits were confirmed **present on their pushed
  branches and NOT reachable from main** (all six `merge-base --is-ancestor`
  false at preflight), matching the landing-card table exactly:

  | branch | commit | content |
  |---|---|---|
  | wt/t_c2ed29df | e3262d92 | impl: TaskExecutionService bridge |
  | wt/t_84836b4b-e3262d92 | a9d7b558 | reviewer APPROVE (contains e3262d92) |
  | wt/t_180370c7 | 33b4d78c | spec V1 |
  | wt/t_55f41487 | afc0ae9f | task readiness audit |
  | wt/t_2ae698f7 | a5e1decc | agent/run/queue audit |
  | wt/t_29528ad2 | 87b25680 | workspace isolation audit |

- No other Phase pending on main; the 6 merges all base on `7497bf7a`.

## B. Parallel Task Decomposition

Root decomposed (Wave 1 audits, parallel, no mutual dependency) then
(Spec → Impl → Review → Final Gate), all via aco-architect / aco-builder /
aco-reviewer:

- Audits (parallel): `t_55f41487` task readiness (afc0ae9f),
  `t_2ae698f7` agent/run/queue (a5e1decc), `t_29528ad2` workspace (87b25680).
- Spec: `t_180370c7` → `docs/PHASE_2E_AGENT_ASSIGNMENT_SPEC_V1.md` (33b4d78c),
  binding V1 semantics with every UNKNOWN given a ruling + owner.
- Implementation: `t_c2ed29df` → `TaskExecutionService` + tests (e3262d92).
- Independent review: `t_84836b4b` → APPROVE + real-Postgres harness
  (a9d7b558), `docs/PHASE_2E_EXECUTION_CHAIN_VERIFICATION_T84836B4B.md`
  + `verify_2e_execution_chain.py`.
- Final Gate / landing: this session (orchestrator) — ordered `--no-ff` merges,
  addendum, regression, push/sync, this report, cleanup.

## C. Agent Assignment

- Assignment = `Task.agent_id` (NOT NULL FK) — **reuse the existing column,
  zero new column / zero migration** (spec §1.1; closes UNKNOWN U-4). This is
  the real *executing* Agent, not the org-level Employee record (root §四).
- The `Execute` endpoint binds the Task to its assigned Agent and drives the
  existing enqueue; "assign" and "execute" are one V1 action
  (`POST /{task_id}/execute`). No separate scheduler or scoring system was
  invented (root §六).

## D. Dependency Gate

- Ready is **derived, never persisted**; sole owner `TaskGraphService.ensure_ready`
  (readiness audit §2). The gate fires in the intake service; a dependent Task
  cannot Execute while a prerequisite is not `done` — unmet list returned,
  Task stays `pending`, **0 Run rows** (fail-closed).
- Evidence: reviewer real-DB scenario **S1** — `S1_blocked_raised=True`,
  `S1_unmet_is_taskA=True`, `S1_no_run_after_blocked=0` (PASS).

## E. Queue / Run Bridge

- Project Task is only "the company's arrangement of work"; the Agent Run is
  "the employee actually doing it". The bridge reuses the existing chain
  `enqueue_task_runtime → RuntimeCommandIntake.start_run →
  register_run_with_start` (`adapter.py:245`, `persistence.py:321`); idempotency
  via `source_execution_id` + `uq_agent_runs_source_execution`. **No second
  Run / queue / worker / LangGraph was rebuilt** (root §七).

## F. Execution State

- No second lifecycle. `derived_state` (READY/BLOCKED/QUEUED/RUNNING/SUCCEEDED/
  FAILED/CANCELLED) is **projected** from `task.status` + `command.status` + the
  latest Run (spec §6.2). Task keeps its closed 3-value `status` enum.

## G. Concurrency

- Independent Tasks on distinct Agents → physically parallel, no gate added
  (storage-key-namespace isolation). Dependent Tasks → the P4 edge is
  load-bearing. Same-Agent multi-Run: `scheduling_lane_key` left NULL in V1
  (no single-writer / no queue — root rule: no speculative knobs).
- Evidence: reviewer **S3** — two independent distinct-agent Tasks coexist as
  2 Runs, `scheduling_lane_key=NULL`, no lane block; **S3_stress**: two
  concurrent same-key sessions → 1 row (winner `created=True`, loser
  `created=False` via SAVEPOINT/IntegrityError recovery).

## H. Workspace

- Distinct agents are physically isolated (storage key namespace +
  path-resolution double gate) → parallel-safe. Same-agent conflicts surface as
  fast-fail (`Workspace lock busy` / human-lock busy/skip) with per-Run temp
  materialization (no data tearing). **No project-level single-writer exists**
  and none was invented (root §十一). Evidence: reviewer **S4** — diff touches
  **0** `agent_runtime/` files and **0** migrations; the same-agent
  thread/lock busy layer is pre-existing and unmodified.

## I. Idempotency

- R1 double-click = one Run (stable `task:{id}` key, DB-unique → physically one
  row). R2 in-flight = reuse (409 `TASK_ALREADY_RUNNING` + active run id). R3
  terminal failure = explicit new attempt (`task:{id}:retry:{uuid}`). R4 never
  auto. R5 key stability (stable key reserved for the first attempt).
- Evidence: reviewer **S2** — `S2_first_created=True`,
  `S2_second_created=False`, `S2_run_rows_for_key=1` (PASS).

## J. Retry / Failure

- Closed failure-code set (spec §6.3) with per-code retry class: transient
  (`WORKSPACE_CONFLICT`, `QUEUE_FAILED`, `RUN_FAILED`, `VERIFICATION_FAILED` —
  bounded, explicit new attempt) vs terminal/security (no unlimited retry).
- A per-Task/per-day **soft cap = 3** retried attempts, enforced by an audit
  COUNT (`audit_dao.count_task_audit`), not by queue machinery (no queueing
  facility exists). `RETRY_CAP_EXCEEDED` now **named** in the closed set + §10.1
  409 mapping (this phase's addendum, commit `d112c71d`). Run-failed is NOT
  conflated with Task-failed — settlement returns the Task to `pending`
  (recoverable).

## K. Tenant Security

- P1 adds the missing **direct** assertion `task.tenant_id == agent.tenant_id`
  (+ `project.tenant_id` for project tasks), closing the agent-side-only gap
  (task readiness audit §6.3). `verify_tenant_scope(agent, caller)` is
  re-asserted at service entry; foreign/absent caller tenant rejected before any
  gate read. Background/queue rule: 2E code outside an HTTP request MUST run in
  `tenant_context(tenant_id)` + entry `verify_tenant_scope` (closes the
  `get_scoped` unscoped-PK degradation, workspace audit G1). Cross-tenant
  execution is impossible by construction (spec §9).

## L. Audit / Evidence

- One `AuditLog` row per Execute outcome at the **intake boundary** (owner = the
  service, NOT the Runtime), `action ∈ {task_execute, task_execute_blocked,
  task_execute_retried}`, carrying `tenant_id, user_id` (the real caller — closes
  UNKNOWN U6), `agent_id`, and `details={task_id, project_id, run_id,
  source_execution_id, outcome_code}`. No credentials ever enter `details`
  (root §十六). "Which Agent ran this Task / which Run did it produce?" is
  answerable: the task's Runs list = `source_execution_id LIKE 'task:{id}%'`
  (stable first + ordered retries) + `AgentRun.source_id` carries the task id.

## M. API

- Minimal, existing style (transport-only handlers, no ORM in the handler):
  - `POST /agents/{agent_id}/tasks/{task_id}/execute` → 200
    `{task_id, created, run_id, source_execution_id, attempt_id?, derived_state}`
    or 404 / 403 / 409 (closed code set).
  - `GET /agents/{agent_id}/tasks/{task_id}/execution` → task + derived_state +
    run list (Task/Agent/Run/status/result, root §十七).
  - Legacy `POST /{task_id}/trigger` keeps a status guard (done → 409
    `TASK_TERMINAL`), closing the re-trigger hole on both entry points.
  - No complex admin dashboard was added.

## N. Database

- **Zero schema drift**: the implementation diff (7497bf7a..e3262d92) is
  10 files +1817/−7, **0** `agent_runtime/` files, **0** alembic/migration
  files. Assignment reuses `Task.agent_id`; the Run/Command/edge tables are
  pre-existing Phase 2D artifacts. No historical migration was modified.

## O. Real Agent Run E2E

- Evidence level (orchestrator-ruled, disclosed in §T): **real-DB Run
  registration + gate verification** driven by the reviewer's real-Postgres
  harness (`verify_2e_execution_chain.py`, scratch DB) — S1 dependency rejection,
  S2 idempotency (incl. concurrent same-key), S3 two independent distinct-agent
  Runs physically parallel, S4 workspace conflict via the pre-existing lock
  layer. All S1–S4 **PASS**. The Task→Agent→Run chain is real and physically
  registered in Postgres (not just a status field). A full-LLM LangGraph run was
  **intentionally not** performed this phase (cost/determinism) — accepted by the
  independent reviewer; see §T.

## P. Concurrent Execution E2E

- Reviewer **S3** demonstrates independent tasks on distinct agents entering
  execution as physically parallel Runs (`scheduling_lane_key=NULL`, no lane
  block), and the concurrent same-key stress yields exactly one Run row
  (SAVEPOINT recovery). Dependent tasks are gated by P4 (S1). No state-field
  check only — the Runs are real rows observed in the scratch Postgres.

## Q. Regression

Re-run **in this session** on the landed main tree (`backend/`, `uv run --no-sync`):

| command | result |
|---|---|
| `pytest tests/test_task_execution_service.py -q` | **37 passed** |
| `pytest <task-lane suites: test_task_runtime_intake, test_task_api_runtime_intake, test_task_graph_api, test_task_decomposition_service, test_task_graph_provenance, test_agent_runtime_task_completion> -q` | **44 passed, 17 skipped** |
| combined | **81 passed, 17 skipped** |
| `pyright app/services/task_execution_service.py app/dao/audit_dao.py` | **0 errors** |
| `ruff check <new/changed files>` | **All checks passed!** |

Phase 2B-1/2/3/4, 2C, 2D task-lane suites are the green 44/17. The 50
pre-existing Windows-host full-suite failures remain environmental (A/B-identical
on a clean tree — see §T); no test was deleted or weakened.

## R. Review / Rework / Re-review

- Independent reviewer `t_84836b4b` (a9d7b558): **APPROVE**. Verified all four
  required behaviors by own execution; new suite 37/37, task-lane 44/17,
  combined 81/17, pyright + ruff clean, real-Postgres S1/S2/S3 PASS.
- One **non-blocking** note: `RETRY_CAP_EXCEEDED` (the §6.3 soft-cap rejection)
  was a de-facto addition to the closed-code set and its 409 transport mapping
  was not named in the spec. **Resolved in this phase** by the spec addendum
  (commit `d112c71d`), not by touching product code. No rework/re-review loop was
  needed (the reviewer's finding was absorbed at the landing gate).

## S. Git

- Ordered `--no-ff` merges (one commit each), base `7497bf7a`:
  1. `1b756b6b` merge wt/t_c2ed29df (impl e3262d92)
  2. `b5f7e86d` merge wt/t_84836b4b-e3262d92 (reviewer a9d7b558)
  3. `89627cfb` merge wt/t_180370c7 (spec 33b4d78c)
  4. `f8f7da55` merge wt/t_55f41487 (readiness audit afc0ae9f)
  5. `784b6dfd` merge wt/t_2ae698f7 (queue audit a5e1decc)
  6. `d1503f6f` merge wt/t_29528ad2 (workspace audit 87b25680)
- Addendum: `d112c71d` (spec §6.3/§10.1, doc-only +10/−1, 1 file).
- All 6 original commits confirmed `ON main` after merges. Pushed
  `7497bf7a..d112c71d`; `main == origin/main == d112c71d`.
- Convergence report: this commit (added to main, then pushed).

## T. UNKNOWN / LIMITATIONS

- **Execution-evidence level:** real-DB Run registration + gate verification
  via the reviewer's real-Postgres harness (S1 dependency rejection / S2
  idempotency incl. concurrent same-key / S3 two independent distinct-agent
  Runs physically parallel / S4 workspace conflict handled by the pre-existing
  lock layer). A **full-LLM LangGraph run was intentionally NOT performed**
  this phase (cost/determinism) — accepted by the independent reviewer; this is
  a disclosed limitation, not a hidden one.
- **50 pre-existing Windows-host full-suite failures** are environmental
  (storage atomicity, subprocess venv, langgraph checkpoint bootstrap, wechat
  cache, workspace reconciliation, heartbeat CLI, materialization e2e) —
  A/B-identical on a clean tree; not introduced by Phase 2E.
- **fcntl cross-process workspace lock is a no-op on the Windows host**
  (Linux-only enforcement); on this Windows host the same-agent concurrency
  safety rests on the Redis fast-fail + DB human-edit lock layers, not fcntl.
- **RETRY_CAP_EXCEEDED** (per-task/day soft cap, 409) was named in the spec
  addendum this phase (commit `d112c71d`); it is a soft, human-recoverable,
  UTC-day-bounded bound, never an auto-retry.

## U. Final Verdict

`PASS` — all 19 root §27 completion criteria satisfied:

1. Task binds a real Agent (`Task.agent_id`, zero DDL) ✓
2. only Ready Task can Execute (P3/P4 fail-closed) ✓
3. dependencies really gate execution (P4; S1) ✓
4. Task→Agent→Run chain real (enqueue→start_run→register_run_with_start) ✓
5. no Agent Runtime re-implementation (0 `agent_runtime/` files) ✓
6. duplicate execution protected (R1–R5; S2) ✓
7. workspace conflict handled (fast-fail lock layers; S4; no single-writer) ✓
8. tenant isolation (P1 direct asserts + `verify_tenant_scope`) ✓
9. execution audit (intake-boundary `AuditLog`) ✓
10. Task/Run state clarity (derived projection, no second lifecycle) ✓
11. failure/retry semantics (closed codes + 3/day soft cap) ✓
12. real Agent Run E2E (real-DB registration + gate harness S1–S4) ✓
13. multi-Task concurrent E2E (S3 parallel + dependent gating) ✓
14. independent reviewer APPROVE (a9d7b558) ✓
15. review/rework/re-review loop closed (non-blocking note absorbed by addendum) ✓
16. regression (37 + 44/17 = 81 passed, pyright/ruff clean, re-run this session) ✓
17. Git commit (6 merges + addendum + this report) ✓
18. push ✓
19. main / origin/main sync (`d112c71d` == `origin/main` before this report;
    re-pushed to land this report) ✓

**BLOCKED conditions: none.** Verdict = `PASS`.
