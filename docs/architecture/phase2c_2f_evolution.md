# Phase 2C–2F Architecture Evolution — Project → Analysis → Task Graph → Agent Assignment → Execution → Evidence → Review

Status: formal architecture record (living document, frozen at Phase 2F closure)
Author: aco-architect
Task: `t_53e24915` (closure child of `t_497a6c27`)
Landed tree: main `7258e96d` == origin/main (verified `git rev-list --left-right --count main...origin/main` → `0 0`)
Alembic head: `f070_analysis_task_dedup` (verified `alembic heads` in this worktree)
Scope: Phases 2C (Project Analysis), 2D (Task Decomposition / Task Graph), 2E (Agent Assignment / Task→Run bridge), 2F (Full Agent Run Execution & Result Settlement). Each stage's entry work (Project intake → materialization → git acquisition) is Phase 2A–2B-4 context, referenced but not re-specified here.

## Classification Discipline

Every non-trivial claim in this document carries one of four epistemic labels:

- **FACT** — directly observable in the repository, a committed report, or the board; verifiable by command (file:line, commit sha, migration id, test count, kanban card id).
- **OBSERVATION** — a pattern seen in one or more concrete instances, stated without a universal claim.
- **INFERENCE** — a conclusion the author draws from facts/observations; plausible, not directly observed.
- **UNKNOWN** — cannot be verified from this tree; explicitly flagged with what would resolve it.

Inline labels appear as `[FACT]`, `[OBSERVATION]`, `[INFERENCE]`, `[UNKNOWN]` next to the claim they qualify. Unlabeled prose is structural description (what the system is), not a claim.

## The Seven-Stage Pipeline (actual state)

The end-to-end chain as it exists on main after Phase 2F:

```
Project (2A intake, 2B materialization/git-acq)
  → Analysis (2C: AnalysisRun / Finding / Knowledge)
  → Task Graph (2D: provenance + task_dependencies + decomposition + ensure_ready gate)
  → Agent Assignment (2E: Task.agent_id binding, Execute endpoint, TaskExecutionService)
  → Execution (2F-proven: Runtime intake → AgentRun → Command worker → LLM turns → tools → workspace)
  → Evidence (2F-proven: tool executions, revisions, run events, audit log, settlement states)
  → Review (2F-proven: independent reviewer re-execution + Final Gate + regression)
```

[FACT] Stage ownership in git history: 2A design doc `PHASE2A_PROJECT_DESIGN_V1.md` + f066 (`v1_11_4_f066_add_project_repo_tables.py`); 2C f068 (`v1_11_5_f068_analysis_persistence.py`); 2D f069/f070 (`v1_11_5_f069_task_graph_provenance.py`, `v1_11_5_f070_analysis_task_dedup.py`); 2E zero migrations (spec `33b4d78c`, impl `e3262d92`, addendum `d112c71d`); 2F zero product-code files (CP11 proof, §Review below). The chain is cumulative: each phase reuses the previous one's persistence and adds a bounded capability.

[FACT] No stage is a second lifecycle: Task stays on the closed 3-value `task_status_enum` (`pending/doing/done`, `models/task.py:58-62`); Run state lives in `agent_runs` + command rows + terminal run events; the "execution state" a consumer sees is a **projection**, not a stored second state machine (ADR-2 / spec 2E §6.2). This invariant was a hard rule of both 2D and 2E and was re-verified in 2F (regression 7/7 closed-set projection).

---

## 1. Project (entry context — Phases 2A/2B-4, baseline for 2C)

Goal: give the company a first-class business object for "one whole thing taken on", with source acquisition (git) and materialization into agent workspaces, so that downstream phases have something concrete to analyze and execute against.

Data Model:
- `projects` — `Project` (`models/project.py:39`): name/description/goal, `project_status_enum` = RECEIVED / SOURCES_OK / INITIALIZED / ANALYZING / PENDING_CONFIRMATION / EXECUTING / BLOCKED / COMPLETED / ARCHIVED / REJECTED (`models/project.py:53-70`); `created_by`, non-nullable `tenant_id`; `rejection_reason` (closed VARCHAR service-code set: SOURCE_NOT_FOUND / SOURCE_INVALID / SECURITY_REJECTED / SOURCE_UNREACHABLE / DISTRIBUTION_FAILED / SOURCE_NOT_SUPPORTED, `models/project.py:86-94`).
- `repositories` — `Repository` (`models/project.py:103`): `source_type` enum (manual/local_folder/document/zip/github/gitlab/local_git), free-form `locator` JSON, `verified`, `pending_verifier` + `retry_count` hold markers.
- Migrations f066–f067 (`models/project.py` docstring; `v1_11_4_f066_add_project_repo_tables.py`, `v1_11_5_f067_intake_rejection_fields.py`).

Core Capabilities (actual):
- Intake lifecycle ownership with fail-closed rejection persistence (`services/project_intake_service.py`, `services/intake_security.py`).
- Git acquisition: `services/git_acquisition_service.py` — locator validation, security gating, `resolved_rev` write-back into `repositories.locator` (the revision carrier later consumed by Analysis).
- Materialization: `services/project_materialization_service.py` — project source → agent workspace bytes; budgets 50/500 MB; `projects/` + `.materialize-tmp/` confinement; per-agent storage-key namespace isolation.

Gate Criteria (as landed, Phase 2B-4):
- DB-free regression 263 passed + migration regression 15 passed (re-verified at the 2C gate on a fresh scratch DB).
- Remote-egress E2E (github/gitlab public URLs) passes only on a host with real egress; [OBSERVATION] on this host DNS proxies `github.com` into the 198.18.0.0/15 reserved range → those two tests fail environment-only, proven byte-identical to base via `git diff` over the service files (classified: environmental, not product defect).
- Workspace isolation PASS (re-verified at the 2F preflight, commit `eb8cfc0e`: storage-key namespace, 403 traversal guard, per-Run temp identity guard, 3-layer locks, fail-closed tenant/agent scoping).

Evidence Artifacts:
- `docs/PHASE_2B4_GIT_ACQUISITION_CONVERGENCE.md`, `docs/MATERIALIZATION_*` (3 docs), `docs/GIT_ACQ_*` (3 docs), Phase 2A design + review docs.
- Migration evidence: f066/f067 single-chain off prior head; `001 → f068` clean (2C gate).

---

## 2. Analysis (Phase 2C — Project Analysis & minimal analysis persistence)

Goal: let the OS "reliably store one Project Analysis + its sources" — versioned, Git-revision-bound, tenant-scoped, with an explicit transient-vs-durable boundary — WITHOUT executing the analyzed project's code and WITHOUT building an artifact/evidence platform. [FACT: scope definition A/B, `docs/PHASE_2C_CONVERGENCE.md` §3.1: the *analyzed target* is read-only/static-only (Stage 11); the phase may add the Company OS's own Analysis subsystem code.]

Data Model (all tenant-scoped, `models/analysis.py`, migration f068 `v1_11_5_f068_analysis_persistence.py`):
- `analysis_runs` — `AnalysisRun`: append-only versioning under `UNIQUE(project_id, revision_sha)` (`models/analysis.py:97-99`); `revision_sha` is the typed, indexed commit-hash carrier (decoupled from free-form `locator` JSON, decision OQ-5); `status` is a CLOSED result-code enum `ANALYSIS_RUN_STATUSES = (AN_OPEN, AN_COMPLETED, AN_FAILED)` — deliberately not a workflow state machine.
- `analysis_findings` — `AnalysisFinding`: TRANSIENT; owned by one run (one revision, one time, one agent); dies with the run via `ON DELETE CASCADE` (`models/analysis.py:155-156`); every finding carries a closed provenance `tag` enum `ANALYSIS_FACING_TAGS = (FACT, OBSERVATION, INFERENCE, UNKNOWN)` + bounded `evidence` JSON (`models/analysis.py:71,167-173`) — the same four-way classification this document uses.
- `project_knowledge` — `ProjectKnowledge`: DURABLE/CONFIRMED; a finding is promoted only after confirmation, carrying `source_analysis_run_id` (SET NULL) and NOT invalidated by a later analysis at a different commit; no knowledge graph modeled — a single flat row (`models/analysis.py:183-218`).

Core Capabilities (actual, `services/analysis_service.py`):
- `launch` — open a run bound to `repositories.locator.resolved_rev`; owns the `project.status → ANALYZING` transition (decision OQ-6).
- `record_findings` — write the run's transient findings; close the run. No conversion side effects.
- `promote_finding` — insert a `project_knowledge` row (CONFIRMED) with provenance copied, not owned. PENDING_CONFIRMATION is INERT by design: no code path sets it; the confirmation UI does not exist (OQ-6).
- `read_current_and_history` / `knowledge` — current run + findings + all prior runs; knowledge rows are revision-independent.
- Execution path is stage-11 static-only: reads DB rows + bounded locator JSON; NEVER executes target-project code (module docstring, `services/analysis_service.py`; E2E asserts zero downstream execution).
- Tenant scoping via `TenantScopedBaseDAO` + `verify_tenant_scope` (the M9 gate).

API (actual, `api/projects.py`):
- `POST /projects/{project_id}/analysis/{analysis_run_id}/findings` (record), `POST /{project_id}/analysis/{analysis_run_id}/promote`, launch + read endpoints.

Gate Criteria (Phase 2C Final Gate, PASS):
- Single Alembic head; `001 → f068` upgrade clean on both provisioning paths (create_all + pure-alembic), including the downgrade round-trip that caught the High defect (model↔migration index drift: `ix_analysis_runs_project_id` was dropped from BOTH model and migration, t_bca54821 @ 7ccabae3, re-verified APPROVE).
- 2C analysis E2E 6/6 on a real local git repo (Project → Source → Revision → Analysis → Findings → Stored → Knowledge); real-DB transport E2E 39 passed.
- 2B DB-free regression 263 passed; ruff/pyright 0 new errors on 2C-touched files; `main == origin/main` at the gate.

Evidence Artifacts:
- `docs/PHASE_2C_PROJECT_ANALYSIS.md` (synthesis design), `docs/PHASE_2C_CONVERGENCE.md` (card chain + gate re-verification + scope A/B formal definition), `docs/PHASE_2C_LAND_CLOSEOUT_T2A41D49C.md` (A–S closeout), supporting audit docs (RECON_BASELINE, TECHSTACK_ARCHITECTURE, RUNTIME_TESTING_WORKFLOW, RISKS_OPEN_QUESTIONS, ANALYSIS_BUILD_T37E2EB05).
- Commit anchors: synthesis 0959266d; build d10af225 → rework 7ccabae3; convergence 522df379 (re-verified on `clawith_2c_finalgate` scratch DB).

---

## 3. Task Graph (Phase 2D — Task Decomposition & Provenance)

Goal: turn analysis output into durable, traceable, dependency-ordered executable Tasks — with a physical Task Graph, fail-closed validation, and a hard boundary that conversion NEVER auto-enqueues (a Task is an intent, not an execution).

Data Model (`models/task.py`, migrations f069 `v1_11_5_f069_task_graph_provenance.py` + f070 `v1_11_5_f070_analysis_task_dedup.py`):
- `tasks` — five-column Phase-2D provenance set (`models/task.py:84-113`):
  - `project_id` (FK→projects, CASCADE: a Task is subordinated to its Project context)
  - `analysis_run_id` (FK→analysis_runs, SET NULL: a run may be re-analyzed without destroying a durable intent)
  - `finding_id` (FK→analysis_findings, SET NULL: findings are transient; a Task is not reverse-destroyed)
  - `revision_sha` (denormalized String(64) snapshot, join-free traceability)
  - `created_reason` — closed 3-value enum `TASK_CREATED_REASONS = (MANUAL, ANALYSIS_FINDING, ANALYSIS_PLANNING)` with `server_default='MANUAL'` (`models/task.py:37,108-113`).
- `task_dependencies` — `TaskDependency` edge table (`models/task.py:138-171`): tenant-scoped, `UNIQUE(task_id, depends_on_task_id)`, `CHECK task_id <> depends_on_task_id` (no self-edge), `ON DELETE CASCADE` both ends. No edge attributes, no workflow semantics.
- `UNIQUE(analysis_run_id, finding_id)` on tasks (f070): a finding may be converted into a Task at most once per analysis run (Postgres treats NULLs as distinct, so MANUAL rows coexist freely).
- `task_status_enum` unchanged: `pending/doing/done` (ADR-2: blocked/ready is DERIVED, never a fourth status value).

Core Capabilities (actual):
- `TaskGraphService` (`services/task_graph_service.py`): §5.1 fail-closed validation order (not-found → tenant → self → supervision → project → cycle → exists); §5.2 bounded-DFS cycle check with `MAX_PROJECT_EDGES = 1000` fail-closed bound (`task_graph_service.py:79,139`); §5.3 derived blocked/ready via 2 bounded SQL (no N+1); §5.4 execution gate `ensure_ready` (`task_graph_service.py:403`) — the single readiness owner.
- Concurrency (defect D1 close): `_lock_project_graph` (`task_graph_service.py:174`) takes a project-scoped `pg_advisory_xact_lock(hashtextextended('task_graph:{tenant}:{project}', 0))` held to transaction end, wired into `_add_edges` immediately before the reachability read — two racing inverse inserts (A→B, B→A) cannot both commit a 2-cycle (regression test `tests/test_task_decomposition_service.py:698`).
- `TaskDecompositionService` (`services/task_decomposition_service.py`): closed E1/E2 executable grid — E1 = `TECH_DEBT ∧ FACT ∧ sev≥WARN`, E2 = `SECURITY ∧ FACT ∧ sev∈{HIGH,CRITICAL}` (lines 84-85; 5 of 80 severity×category×tag combos); planning-only P1–P5; gates G1–G5; invocation bound `MAX_TASKS_PER_INVOCATION = 100` (line 78). Rule-based engine — no LLM classification.
- Conversion API (`api/projects.py:646`): `POST /projects/{project_id}/analysis/{run_id}/tasks` (`convert_analysis_run_to_tasks`) — creates PENDING tasks with filled provenance ONLY; G4 no-enqueue guarantee (no Run created, no project status transition, no Assignment row; asserted live). Run gate G1: conversion only on `AN_COMPLETED` runs.
- Provenance integrity (defect D2 close): `created_reason` tightened to the closed enum in schemas; `create_task`/`update_task` run the fail-closed `provenance_consistency` validator (`task_dao.py:335`) before write — `MANUAL ⇒ all 4 analysis cols NULL`; `ANALYSIS_FINDING/PLANNING ⇒ tenant-aligned & (FINDING) run terminal`; violation → 400 `PROVENANCE_INCONSISTENT`, nothing written.

Gate Criteria (Phase 2D Final Gate, PASS):
- 14 §15 conditions (all ✅ in `docs/PHASE_2D_CONVERGENCE_REPORT.md` §13): contract + provenance + graph V1 determined; persistence/API/service at V1; dependency validation with real tests; conversion E2E with live G4 assertion; Task≠Run boundary verified; prior-phase regression green in isolation; migration single head `f070` with clean round-trips; independent review APPROVE (`t_74eb3596` @ head `318cde08`); LAND `t_67837666` → `main == origin/main == 1ffa7dcc`.
- Phase-2D module suite 57 passed (incl. D1+D2 regression tests) on a clean scratch DB; 2 lifecycle failures proven environmental (DNS-proxied github.com).

Evidence Artifacts:
- Design docs: `docs/PHASE_2D_TASK_GRAPH_PROVENANCE_DESIGN.md` (b80e70e7), `docs/PHASE_2D_ANALYSIS_TASK_MAPPING.md` (d86296a2), `docs/PHASE_2D_CODEBASE_AUDIT.md` (4ddba22b).
- `docs/PHASE_2D_CONVERGENCE_REPORT.md` (§十六 — 11-card DAG, ADRs, defect→fix→re-verify trail, evidence index with file:line anchors).
- Product commits: a1bb030e (f069) → 5e8fc789 (service + f070) → 6f32236a (API) → 318cde08 (D1+D2 rework).
- Accepted V1 limits (documented, not defects): flat task set from conversion (no dependency authoring during conversion); no LLM/planner classification; no knowledge injection; no project status transition on conversion; post-completion "▶ ready" hint (design §5.4 part-b) not wired; `list_dependents` unused in production.

---

## 4. Agent Assignment (Phase 2E — Project Task → Agent Assignment → Real Execution)

Goal: make "which Agent does this Task" a first-class, executed fact — assignment + execution as ONE V1 action on the existing Runtime — with fail-closed gates, physical idempotency, derived (not stored) state, and explicit-only retry. [FACT: spec ruling §1.1 — assignment = the existing `Task.agent_id` NOT NULL FK; zero new columns, zero migrations.]

Data Model:
- **Zero schema change** [FACT: convergence §N — the 2E diff is 10 files +1817/−7, 0 `agent_runtime/` files, 0 alembic files]. Reused artifacts:
  - `tasks.agent_id` (existing) = the executing Agent binding (NOT the org-level Employee record).
  - `agent_runs.source_type='task'`, `source_id`, `source_execution_id` + `uq_agent_runs_source_execution` (partial unique index, `models/agent_run.py:173-179`) = the idempotency floor.
  - `agent_run_commands` (existing command table) = the durable input seam (`models/agent_run_command.py`).
  - `audit_logs` = intake-boundary execution audit.
- Closed failure-code set (spec §6.3): `TASK_TYPE_NOT_SUPPORTED`, `TASK_TERMINAL`, `TASK_ALREADY_RUNNING`, `TENANT_MISMATCH`, `TENANT_CONTEXT_MISSING`, `PROJECT_NOT_FOUND`, `PROJECT_NOT_EXECUTABLE`, `ASSIGNMENT_FAILED`, `AGENT_UNAVAILABLE`, `RUNTIME_V2_DISABLED`, `TASK_BLOCKED`, `QUEUE_FAILED`, `RETRY_CAP_EXCEEDED` (named in the addendum `d112c71d`, §10.1 409 mapping), plus the transient class `{WORKSPACE_CONFLICT, QUEUE_FAILED, RUN_FAILED, VERIFICATION_FAILED}`.

Core Capabilities (actual):
- `TaskExecutionService` (`services/task_execution_service.py`): the §4 P1–P8 fail-closed gate in fixed order (cheapest→most expensive, first failure wins, nothing enqueued on ANY failure; every blocked outcome writes a TaskLog ⛔ + an AuditLog row); §5 idempotency R1–R5 (stable key `task:{id}` reserved for the first attempt; explicit human retry after a terminal failed/cancelled Run mints `task:{id}:retry:{attempt_id}`; double-Execute while doing reuses the in-flight Run, `created=False`; retries are NEVER automatic); §6.3 soft cap `RETRY_SOFT_CAP_PER_TASK_PER_DAY = 3` enforced by an audit COUNT, not queue machinery (no queueing facility is invented); §8 one AuditLog row per outcome at the intake boundary with the real caller `user_id`; §9 tenant re-assertion at service entry.
- Derived-state projection (spec §6.2, `task_execution_service.py:548-581`): READY / QUEUED / RUNNING / SUCCEEDED / FAILED / BLOCKED / CANCELLED computed from `task.status` + command row + latest terminal Run event — no second lifecycle, no new column.
- Single enqueue funnel: `enqueue_task_runtime` (`services/task_executor.py:44-152`) → `RuntimeCommandIntake.start_run` → `register_run_with_start` (`agent_runtime/adapter.py`, `agent_runtime/persistence.py`); the Phase-2D `ensure_ready` gate fires inside the intake (blocked → `TaskBlockedError` → `_log_blocked`, 0 Run rows).
- API (actual, `api/tasks.py:502-540` + `:541`): `POST /agents/{agent_id}/tasks/{task_id}/execute` → 200 `{task_id, created, run_id, source_execution_id, attempt_id?, derived_state}` or 404/403/409 closed-code set; `GET /agents/{agent_id}/tasks/{task_id}/execution` → task + derived_state + bounded run list (attempt labels `first` / `retry:<uuid>`); legacy `POST /{task_id}/trigger` carries a status guard (done → 409 `TASK_TERMINAL`).

Gate Criteria (Phase 2E Final Gate, PASS — all 19 root §27 criteria in `docs/PHASE_2E_CONVERGENCE_REPORT.md` §U):
- Evidence level (disclosed ruling): real-DB Run registration + gate verification via the reviewer's real-Postgres harness `verify_2e_execution_chain.py` — S1 dependency rejection (`S1_no_run_after_blocked=0`), S2 idempotency incl. concurrent same-key (exactly 1 row, SAVEPOINT recovery), S3 two independent distinct-agent Runs physically parallel (`scheduling_lane_key=NULL`), S4 workspace conflict handled by the pre-existing lock layer. A full-LLM run was intentionally NOT performed this phase (cost/determinism) — disclosed, reviewer-accepted; 2F then supplied the real-LLM proof.
- Regression re-run at landing: `tests/test_task_execution_service.py` 37 passed; task-lane suites 44 passed / 17 skipped; combined 81 passed; pyright 0 errors on changed files; ruff clean.
- No Agent Runtime re-implementation (0 `agent_runtime/` files in diff); no single-writer / project lock invented; tenant isolation closed by the P1 direct assertion.

Evidence Artifacts:
- `docs/PHASE_2E_AGENT_ASSIGNMENT_SPEC_V1.md` (33b4d78c, with `d112c71d` addendum) — the binding V1 semantics; every UNKNOWN given a ruling + owner.
- `docs/PHASE_2E_TASK_READINESS_AUDIT.md` (afc0ae9f), `docs/PHASE_2E_AGENT_RUN_QUEUE_AUDIT.md` (a5e1decc), `docs/PHASE_2E_WORKSPACE_ISOLATION_AUDIT.md` (87b25680).
- `docs/PHASE_2E_EXECUTION_CHAIN_VERIFICATION_T84836B4B.md` + `verify_2e_execution_chain.py` (a9d7b558, reviewer's real-Postgres harness).
- `docs/PHASE_2E_CONVERGENCE_REPORT.md` (§A–§U incl. S-scenario results + git landing chain).
- Implementation commit e3262d92; landed merges 1b756b6b..d1503f6f; main head after 2E: d112c71d.

---

## 5. Execution (Phase 2F — Full Agent Run Execution & Result Settlement)

Goal: prove that a Project Task no longer merely "registered a Run" but is really executed by an AI Agent — real LLM → real tool calls → real workspace change → result → verification → settlement back to the Task state — with no core-Runtime rewrite, no auto-retry, and no mock anywhere. [FACT: root card §Stop rule — proving the full chain is the ONLY objective; downstream expansion (Artifact/Evidence Platform, Squad, UI) is explicitly out of scope.]

Data Model:
- **Zero product-code change in Phase 2F itself** [FACT: CP11 — `git diff 3175c798..<each 2F commit>` = 0 files in `backend/app`, `backend/tests`, `frontend`, `helm`, `deploy`; 2F is a PROOF phase on the 2A–2E substrate]. The execution substrate (all pre-existing, all tenant-scoped):
  - `agent_runs` (`models/agent_run.py`): product-owned identity/delivery facts; closed CHECK sets on `source_type` (incl. `task`), `run_kind` (incl. `background`), `runtime_type` (incl. `langgraph`), `delivery_status`; `uq_agent_runs_source_execution` partial unique = the dedup floor.
  - `agent_run_commands` (`models/agent_run_command.py`): durable start/resume/cancel inputs; `status` pending/claimed/applied/rejected; claim lease + `attempt_count`; SKIP-LOCKED claim queue index `ix_agent_run_commands_status_claim_created`.
  - `agent_tool_executions` (`models/agent_tool_execution.py`): idempotency ledger — closed `status` (started/succeeded/failed/unknown), closed `effect` (read/write/external_write), closed `retry_policy` (safe/conditional/never), `result_ref` + `result_metadata` (the tool-result-into-Run settlement point), lease owner/expiry.
  - `agent_run_events` (`models/agent_run_event.py`): append-only rebuildable product events; closed `event_type` set incl. `waiting_started`, `verification_updated`, `run_completed`, `run_failed`, `run_cancelled`; `artifact_refs` JSONB; checkpoint-unique index.
  - `workspace_file_revisions` / `workspace_edit_locks` (`models/workspace.py`): revision trail (scope agent/group, actor user/agent/system, before/after content + hash) and human edit locks.
  - Settlement handlers in `services/agent_runtime/` (`task_completion.py` etc.) map terminal Run events onto Task state.

Core Capabilities (actual, proven in 2F on real execution):
- Real LLM execution: `agnes-3.0-flash`, provider=openai (OpenAI-compatible HTTP), `https://apihub.agnes-ai.com/v1`, real instrumented HTTP — no mock anywhere. 2F execution counts per card (paid/real): real-LLM driver 1 Run / 3 model turns → `run_completed` → Task `done`; concurrency 6 independent+dependent Tasks; settlement scenarios 0 real calls (deterministic/local); E2E 4 calls incl. the verification gate; ladder 74 calls (stage 5 = 19 clean, stage 10 = 55 with one verbatim provider 500 recovered by bounded model retry, STOP rule fired → stage 20 not executed).
- Tool execution: `read_file` returned real file content verbatim; `write_file` produced byte-correct `WorkspaceFileRevision` rows (6 marker files coexist under concurrent load); command primitive via the host-portable subprocess seam (real stdout/stderr/exit code captured; the container bwrap path is Unix-only — disclosed host artifact, not a defect).
- Settlement closed set (6/6 scenarios on the real settlement seam): success→done/SUCCEEDED; tool failure→task stays pending, no false success; verification failure→documented recoverable state; agent failure→no false success; cancelled→CANCELLED (worker stopped, 0 post-cancel tool execs, no orphan Run — Branch A: capability fully supported, nothing new built); unmet dependency→BLOCKED, fail-closed, zero Runs.
- Duplicate protection: second Execute of the same Task → 0 new Run rows via two independent terminals (service in-flight gate 409 `TASK_ALREADY_RUNNING` + intake exact-input dedup `created=False` + DB-unique `uq_agent_runs_source_execution`).
- Explicit retry: `task:{id}:retry:{uuid}` mints exactly one new Run with a distinct `source_execution_id`; no automatic retry anywhere (6 s bounded poll, row counts flat); `RETRY_CAP_EXCEEDED` fails closed at cap 3.
- Timeout classes (LLM/Tool/Command): LLM timeout → bounded retries → durable recoverable WAIT checkpoint (`waiting_started`), not infinite RUNNING; tool deadline → `agent_tool_executions` settles `failed` with `error_code=tool_deadline_exceeded`; command timeout → child killed at budget, exit 124, PID confirmed dead (no orphan).
- Tenant isolation: 8/8 cross-tenant attempts denied (4 A→B + 4 B→A across agent access, Run/tool-exec reads, workspace storage, Task execution) + 2/2 positive controls, reusing the real tenant scoping — no new isolation subsystem.
- Concurrency: authoritative persisted-timestamp overlap proof (7 in-flight Run pairs across 4 concurrent workers); staged ladder (5 clean / 10 with one verbatim 5xx / 20 not executed) → safe concurrency = 5, saturation ceiling marker = 10.
- Full Project E2E (12 links, 4 real LLM calls, isolated scratch Postgres + scratch storage): intake → git acquisition → source validation → materialization → analysis → decomposition → execution → real LLM Run → read legacy.py + write FIXNOTES.md → verification gate → Task settled `done`. This is the most important E2E to date for the system.

Gate Criteria (Phase 2F Final Gate, PASS — all 20 §25 criteria in `docs/PHASE_2F_CONVERGENCE_REPORT.md` §T):
Real LLM Run success; Agent really calls Tools; Tool Result really enters Run; Workspace really changes; Verification really runs; Result really settles; Task final states correct (7/7 projection); multi-task real concurrency; Dependency really blocks; Duplicate Run protected; Explicit retry normal; Timeout correct; Failure recovery correct (no false success anywhere); Tenant isolation correct; Project→Task→Run full E2E; independent reviewer APPROVE; regression passes; git commit + push; `main == origin/main`. Stop rule honored: no downstream expansion started.

Evidence Artifacts:
- `docs/PHASE_2F_CONVERGENCE_REPORT.md` (landed on main as 7258e96d) — §A–§T with per-card citations: 15 card commits reachable via their card branches / origin refs (not on main - see foundation inventory G1-G4) (`b7059cdc`, `eb8cfc0e`, `1fa1c07b`, `0b9b2fdb`, `c61774a8`, `33de066f`, `41b60e92`, `2a65509b`, `4dc53e3a`, `e34e0f99`, `014f677b`, `720e714b`, `072592a0`, `eebfd379`, `848dfd2e`).
- Per-card drivers + evidence JSON + reports committed on their card branches and pushed (handoff rule: fixed output path → commit → downstream `git show`; no reliance on sibling-worktree untracked files).
- Phase-2E suite regression: 37 passed; 2B-1→2F full-chain regression PASS 13/13 (0-paid drivers made 0 real LLM calls; ladder confirmed from committed evidence without re-burn).
- disclosed UNKNOWN/limitations (report §S): host-portable command seam (Windows artifact); fcntl cross-process lock no-op on this host (concurrency evidence uses authoritative timestamp overlap instead); Windows text-mode LF→CRLF on the same physical file (harness asserts semantic markers + DB revision trail); one transient scratch-DB ConnectionError flake (runtime parked it correctly; re-run passed); ladder stage 20 documented by extrapolation only, not executed; scratch PG DBs left for traceability; live-pool `llm_models` keys remain placeholder `enc-test` (the real-LLM proof chain used the dedicated ambient `AGNES_*` credential path; the live pool was never modified).

---

## 6. Evidence (the standing evidence model, post-2F)

Goal: after 2F, "which Agent ran this Task, what did it do, and what was the outcome?" is answerable from persisted facts alone — without any new unified Artifact/Evidence platform. [FACT: root card §九 — reuse AgentRun + agent_tool_executions + workspace revisions + AuditLog + verification; do not add a new Artifact system.]

The evidence chain (all persisted, all tenant-scoped, all re-readable in a fresh session):

```
Task
 ├─ Agent (Task.agent_id — the executing binding, 2E)
 ├─ Run (agent_runs rows: source_type='task', source_execution_id='task:{id}[:retry:{uuid}]', runtime_thread_id, model_id)
 ├─ Tool executions (agent_tool_executions: tool_name, effect, retry_policy, result_ref, result_metadata — incl. verbatim command stdout/stderr/exit)
 ├─ Tool results (result store + settled ledger result_ref; byte-faithful re-read proven in probes F, 9/9)
 ├─ Workspace revisions (workspace_file_revisions: before/after content + content_hash; system materialization AND agent-authored revisions both persist)
 ├─ Run events (agent_run_events: append-only terminal lifecycle — run_completed / run_failed / run_cancelled / waiting_started / verification_updated)
 ├─ Verification (TaskCompletionGate LLM verification executed in the E2E — run_completed only AFTER the gate passed; verification-failure leaves the documented recoverable state, no false success)
 └─ Final result (Task settlement: done / recoverable-pending / BLOCKED per the closed projection)
```

Owning facts and reusability:
- [FACT] The settlement point is `agent_tool_executions.result_ref/result_metadata` + terminal `AgentRunEvent`; "which Agent did this" is answered by `agent_runs.source_execution_id LIKE 'task:{id}%'` (stable first attempt + ordered retries) + `AgentRun.source_id` carrying the task id (spec 2E §L) + the `AuditLog` row at the intake boundary (action ∈ task_execute / task_execute_blocked / task_execute_retried, carrying the real caller user_id, agent_id, task/project/run ids, and outcome_code — no credentials ever in details).
- [OBSERVATION] Task-level Review/Artifact persistence does not yet exist as a dedicated table (2D audit finding J, carried as an accepted limit): Run-level artifact refs live in `AgentToolExecution.result_metadata` + `AgentRunEvent.artifact_refs` — sufficient for the 2F evidence model; a task-level review/artifact layer is explicitly deferred (stop rule).
- [UNKNOWN] The durability/retention policy for scratch verification DBs (e.g. `clawith_2f_*` names listed in the 2F report §S.6) has no owner on this board; SQL DROP is blocked in headless single-query mode. Resolution: an interactive-session hygiene pass (operator action), not a product decision.

Gate Criteria (what "evidence is complete" means operationally, per 2F §T):
Every link in the chain above has at least one persisted, re-readable fact that a fresh session or reviewer can verify without the original worker's report — this is exactly the property the independent reviewer exploited (re-ran all 12 checkpoints from committed evidence + real re-execution, not trusted self-reports).

---

## 7. Review (the standing review model, post-2F)

Goal: independent, execution-based quality control — a reviewer never trusts builder self-reports; APPROVE/REQUEST_CHANGES is earned by re-execution on a clean worktree.

Review model as actually operated across 2C→2F:
- [FACT] Role separation enforced by the board routing rule: only aco-architect (design/analysis/audit), aco-builder (implementation/execution/verification), aco-reviewer (independent review/gate), aco-orchestrator (root/final gate) may work on this board; profile leaks are reassigned.
- [FACT] Each phase's Final Gate chain: independent reviewer card → (REQUEST_CHANGES → rework → re-review loop when defects found, e.g. 2C High defect t_75dd99db → t_bca54821 → APPROVE t_e2616e2c; 2D two Medium defects D1+D2 → t_08d8fb43 → APPROVE t_74eb3596) → orchestrator root re-verification → LAND card (merge, push, verify main == origin/main) → convergence report (this document's per-phase sources).
- [FACT] 2F's independent gate (t_d066fdd5, commit 848dfd2e pushed): reviewer used their own worktree + real LLM + fresh scratch Postgres + real host subprocesses; 12/12 §23 checkpoints on real re-execution; CP11 = 0 product-code files across all 12 Phase-2F commits; verdict APPROVE with both approval conditions (all 12 checkpoints real AND regression PASS) met.
- [FACT] 2F regression lane (t_933d0fa1, eebfd379): 13/13 sections matched — all 9 prior-wave drivers re-ran at their landed commits on fresh scratch DBs matching recorded PASS verdicts; 2E suite 37/37; no-core-rewrite audit clean; closed-set projection 7/7.
- [OBSERVATION] The review pattern that emerged and stabilized: reviews are execution-based (scratch DB, real seams, re-run), re-verification of the two most load-bearing gates happens at the orchestrator level rather than on worker self-reports, and the "flake vs defect" distinction is decided by re-run (2F F1: transient scratch-DB ConnectionError parked correctly, re-run #2 passed).

Review deliverables that persist:
- Per-phase convergence reports on main (`PHASE_2C/2D/2E/2F_*` under `docs/`) — each with card chain, defect→fix→re-verify trail, final-gate condition tables with evidence citations, and UNKNOWN/limitation sections.
- Reviewer reports committed on reviewer branches and pushed (e.g. 848dfd2e, a9d7b558) — the board record + git record are deliberately kept in sync.
- This document: `docs/architecture/phase2c_2f_evolution.md` — the cross-phase architecture record required by the closure task.

Gate Criteria (review "done" per phase):
Independent reviewer APPROVE with real re-execution; regression PASS; git commit + push + `main == origin/main` verified; convergence report landed; no open defect classes of High/Medium severity un-triaged (accepted limits are documented, not silently dropped).

---

## Cross-Stage Invariants (what the evolution deliberately preserved)

1. **No second state machine.** Task = 3-value status (2D ADR-2); Run = product identity facts + checkpoint-owned execution state; command = pending/claimed/applied/rejected inputs; derived consumer state = a projection only. [FACT, verified 7/7 at the 2F regression.]
2. **Provider/runtime independence.** The chain Company OS → Agent Runtime → Model Provider holds: 2E/2F diffs touch 0 `agent_runtime/` files; the LLM was swappable in the real proof (OpenAI-compatible `agnes-3.0-flash` via ambient credentials without touching business layers). [FACT: CP11; 2E §N.]
3. **Resource independence.** [INFERENCE — synthesis of the cited facts:] Business layers never ask "how many LLM calls can Ollama/provider take" — budgets are per-agent LLM/token caps, per-task/day audit-soft-cap (3), and the ladder's observed safe concurrency (5) — all consumed through owning boundaries. Sub-facts, each citable: the caps live on `Agent` (`llm_calls_today` / `max_tokens_*`, `models/agent.py:125-128`); the soft cap is the `TaskExecutionService` audit-COUNT cap (`RETRY_SOFT_CAP_PER_TASK_PER_DAY = 3`, `task_execution_service.py:72`); "no new scheduler or scoring system was invented" [FACT: 2E convergence §G — "No second Run / queue / worker / LangGraph was rebuilt (root §七)", and root card §六]; "safe concurrency = 5" [FACT: 2F report §I ladder, t_99c4a1bb].
4. **Provenance chain, unbroken and join-free where hot.** `Task → finding → run → revision → project` (2D §10), with `revision_sha` denormalized onto tasks for the highest-frequency traceability query; a later re-analysis never clobbers history (append-only `UNIQUE(project_id, revision_sha)`); a deleted run/finding never destroys a durable Task (SET NULL). [FACT, models/task.py:84-102.]
5. **Fail-closed everywhere, fail-fast at the owning boundary.** Closed code sets at every gate (2C rejection codes, 2D §5.1 order, 2E P1–P8, 2F settlement 6/6); misconfiguration or unmet prerequisites are rejected at the earliest authoritative point, with the blocked outcome recorded (TaskLog ⛔ + AuditLog), never silently skipped. [FACT.]
6. **Reuse-first persistence.** Each phase added exactly the minimal table it needed (f068 = 3 tables, f069 = 1 edge table + 5 columns, f070 = 1 constraint, 2E/2F = 0 migrations), following the physical-FK precedent of f066–f068 (2D ADR-1). [FACT, migration inventory above.]

## Open / Unknown Register (carried forward, classified)

| # | Item | Class | State / resolution path |
|---|---|---|---|
| U1 | Ladder stage 20/50+ behavior | UNKNOWN | Documented by extrapolation only; ceiling marker recorded at 10 (one verbatim provider 500). Resolve: a dedicated performance card on a clean-egress host. |
| U2 | Remote-egress E2E (github/gitlab public URL) | OBSERVATION (env-dependent) | Fails on this host (DNS → 198.18.0.0/15); proven environmental via byte-identical diff to base. Resolve: re-run on a clean-egress host. |
| U3 | Scratch PG verification DBs (`clawith_2f_*`, `clawith_t*`) | UNKNOWN (no owner) | Left for traceability; DROP blocked in headless single-query mode. Resolve: interactive hygiene pass. |
| U4 | Live-pool `llm_models` keys = placeholder `enc-test` | FACT (observed 2F preflight) | The real-LLM chain used the dedicated ambient `AGNES_*` credential path; the live pool was never modified. Resolve: operator provisioning decision. |
| U5 | Task-level Review/Artifact persistence | UNKNOWN (deferred by design) | Not a defect — explicitly out of stop-rule scope; `result_metadata` + `artifact_refs` are the V1 carriers. |
| U6 | fcntl cross-process workspace lock | OBSERVATION (host artifact) | No-op on this Windows host (A9 caveat); concurrency safety rests on authoritative timestamp overlap + DB/Redis lock layers. |
| U7 | Design §5.4 part-b ("▶ ready" hint) + `list_dependents` | OBSERVATION (accepted V1 limit) | Query capability + execution gate exist; post-completion hint not wired. Documented in the 2D mapping doc. |
| U8 | DAO read paths default `limit=100` | OBSERVATION | >100-row projects need pagination; no current consumer. |
| U9 | Post-2F main/origin state | UNKNOWN (outside this document's clock) | Verified `0 0` at write time (main = `7258e96d`); any later divergence is out of scope for this record. |

## Change Log

- v1 (this commit, task t_53e24915): initial formal record of the 2C–2F evolution, grounded in the landed main tree `7258e96d`, all four phase convergence reports, the current models/services/API code, and the Phase 2F root card's evidence register. Every non-trivial claim classified FACT/OBSERVATION/INFERENCE/UNKNOWN per the closure-task requirement; unclassified prose is structural description only.
