# Phase 2D — Convergence Report (§十六 final deliverable)

> **Root task:** `t_55d5bee6` (Phase 2D — Task Decomposition)
> **Report card:** `t_52378aa0` (this card — the LAST §十六 deliverable before the root can close)
> **Author:** aco-architect
> **Generated:** 2026-09-25 (JST)
> **Reported main (post-merge):** `1ffa7dcc` == `origin/main` (verified live: `git rev-list --left-right --count main...origin/main` = `0 0`)
> **Baseline:** `e6237916` (pre-Phase-2D; `main == origin/main == e6237916`, Phase 2C Final Gate = PASS)

This report states **real evidence only** — every claim cites a commit, file:line, test name, kanban card, or git ref. Nothing here says "全部完成" as a bare assertion; each §十五 final-gate condition is listed with its supporting evidence. Unknowns are marked **UNKNOWN**.

---

## 1. Phase 2D at a glance

| Field | Value | Evidence |
|---|---|---|
| Root Task id | `t_55d5bee6` | kanban board `ai-company-os` |
| Task Count (Phase-2D work cards under root, all waves) | **11** | ancestor-closure of root on `I:\hermes\kanban\boards\ai-company-os\kanban.db` (waves 1–10 below) |
| Wave Count | **11** execution waves (wave 1 audit … wave 10 convergence-report); the 12th DAG node is the root `t_55d5bee6` itself (wave 11) | same board, longest-prereq-path rank |
| Parallel / Sequential distribution | **1 parallel wave + 10 sequential waves** | wave 2 has 2 cards; all other waves have 1 |
| Git baseline (pre) | `e6237916` | root §一; `main==origin/main==e6237916` pre-LAND |
| Git baseline (post) | `1ffa7dcc` (=`1ffa7dccdfa3950decc23d3f6931c89b4f130bc3`) | live `git rev-parse main`/`origin/main`; LAND handoff `t_67837666` |
| Final Gate | **PASS** | re-review `t_74eb3596` = APPROVE at head `318cde08`; LAND `t_67837666` = PASS |
| Product code un-merged into main? | **None** | all Phase-2D product + 3 doc heads merged & pushed (`t_67837666`) |

### Task Count + Wave distribution (board-fact, reconstructed from `task_links`)

The root `t_55d5bee6` is a **convergence sink**: every Phase-2D card is a transitive *prerequisite* (parent) of it. The full Phase-2D DAG, rank-ordered:

| Wave | Card | Assignee | Status | Role |
|---|---|---|---|---|
| 1 (seq) | `t_4b24dd7e` | aco-architect | done | Audit Project/Analysis/Task/Agent domains on `e6237916` → `docs/PHASE_2D_CODEBASE_AUDIT.md` @ `4ddba22b` |
| 2 (**parallel**, 2 cards) | `t_46f8c7cf` | aco-architect | done | Task Graph & Provenance model → `docs/PHASE_2D_TASK_GRAPH_PROVENANCE_DESIGN.md` @ `b80e70e7` |
| 2 (**parallel**, 2 cards) | `t_3867a0f9` | aco-architect | done | Analysis→Task mapping boundary + V1 safety gate → `docs/PHASE_2D_ANALYSIS_TASK_MAPPING.md` @ `d86296a2` |
| 3 (seq) | `t_650ddd87` | aco-builder | done | Persistence: `f069_task_graph_provenance` + DAO → commit `a1bb030e` |
| 4 (seq) | `t_b8545ece` | aco-builder | done | `TaskDecompositionService` + `TaskGraphService` + execution gate + `f070` → commit `5e8fc789` |
| 5 (seq) | `t_b4a29991` | aco-builder | done | Task Graph API + conversion endpoint + DAO rowcount fix → commit `6f32236a` |
| 6 (seq) | `t_ab45f959` | aco-reviewer | done | Final-Gate review → **REQUEST_CHANGES** (2 Medium: D1, D2) |
| 7 (seq) | `t_08d8fb43` | aco-builder | done | Rework: close D1+D2 → commit `318cde08` |
| 8 (seq) | `t_74eb3596` | aco-reviewer | done | Re-review → **APPROVE** at head `318cde08` |
| 9 (seq) | `t_67837666` | aco-builder | done | LAND: ff-merge product chain + 3 `--no-ff` doc heads, push, verify → `1ffa7dcc` |
| 10 (seq) | `t_52378aa0` | aco-architect | running | **This Convergence Report (§十六)** |
| 11 (gate) | `t_55d5bee6` | aco-orchestrator | todo | Root — closes when this report lands + gate = PASS |

**Distribution:** 11 work cards + 1 root node = 12 DAG nodes. Exactly **one** wave (wave 2) is parallel (the two independent design cards, each gated only by the audit `t_4b24dd7e`); the other **10** waves are strictly single-card sequential. Total parallel cards = 2 (both in wave 2); total sequential cards = 9.

---

## 2. Final product capabilities (what Phase 2D delivered)

Concrete, landed, reviewed, and merged into `1ffa7dcc`:

1. **Task Graph persistence** — `task_dependencies` edge table (tenant-scoped, `UNIQUE(task_id, depends_on_task_id)`, `CHECK task_id != depends_on_task_id` no-self, `ON DELETE CASCADE` both ends) [migration `f069_task_graph_provenance`, `backend/alembic/versions/v1_11_5_f069_task_graph_provenance.py`; model `backend/app/models/task.py`].
2. **Task Provenance** — 5 nullable columns on `tasks`: `project_id` (FK→projects, CASCADE), `analysis_run_id`/`finding_id` (FK, SET NULL), `revision_sha` (denormalized snapshot), `created_reason` (closed 3-value enum `MANUAL/ANALYSIS_FINDING/ANALYSIS_PLANNING`, `server_default='MANUAL'`) [same `f069` migration; `models/task.py:37,59,109`].
3. **Analysis→Task dedup** — `UNIQUE(analysis_run_id, finding_id)` guard + model lockstep [migration `f070_analysis_task_dedup`, `backend/alembic/versions/v1_11_5_f070_analysis_task_dedup.py`; `models/task.py`].
4. **`TaskGraphService` §5.1–§5.4** — fail-closed validation order (not-found/tenant/self/supervision/project/cycle/exists), bounded-DFS cycle check (`MAX_PROJECT_EDGES = 1000`), derived blocked/ready (no new status value — `task_status_enum` stays `pending/doing/done` at `models/task.py:59`), execution gate `ensure_ready` (`task_graph_service.py:403`) wired into `enqueue_task_runtime`'s single funnel (`task_executor.py:44,98,171`; `TaskBlockedError` → `_log_blocked`).
5. **`TaskDecompositionService.classify`** — closed E1/E2 executable grid over the severity×category×tag space (E1 = `TECH_DEBT ∧ FACT ∧ sev≥WARN`; E2 = `SECURITY ∧ FACT ∧ sev≥{HIGH,CRITICAL}`), planning-only `P1–P5`, gates G1–G5, §4 dedup, G4 no-enqueue guarantee.
6. **Task Graph + conversion API** — `GET /agents/{id}/tasks/{task_id}/graph`, `POST /{task_id}/dependencies`, `DELETE /{task_id}/dependencies/{dep}` (`api/tasks.py:352–409`) and `POST /projects/{project_id}/analysis/{run_id}/tasks` conversion (`api/projects.py:646`). Conversion creates **PENDING** tasks with filled provenance only — no auto-enqueue (G4), no project status transition, no Assignment row (Task ≠ Run boundary).
7. **D1 concurrency close** — project-scoped `pg_advisory_xact_lock` in `task_graph_service._add_edges` (§8 below).
8. **D2 provenance close** — fail-closed `provenance_consistency` on the manual `create_task`/`update_task` path + closed `created_reason` enum (§5.2/§6 below).

The end-to-end pipeline now exists end-to-end: **Project → Analysis → Task Decomposition → Task Graph → Executable Tasks → Agent Assignment (boundary) → Execution (gated)**.

---

## 3. Git Baseline (pre → post)

| | Ref | Meaning |
|---|---|---|
| Pre | `e6237916` | `main == origin/main`, Phase 2C Final Gate = PASS. Product baseline `fc233fc1` (Phase 2A/B). |
| Post | `1ffa7dcc` | Post-LAND `main == origin/main` (verified `rev-list --left-right --count = 0 0`). |

**Landed chain** (verified in `I:\project\AI Company OS`, `git log e6237916..main`):

```
e6237916  (pre baseline, Phase 2C head)
  ├─ product chain (fast-forward merged in one move by t_67837666):
  │    a1bb030e  feat(2d): f069 Task Graph persistence + Task provenance schema   [t_650ddd87]
  │    a77dc3b6  Merge wt/t_650ddd87 (f069 persistence) into t_b8545ece service lane
  │    5e8fc789  feat(phase-2d): Task Decomposition service + graph validation + execution gate  [t_b8545ece]
  │    6f32236a  feat(phase-2d): Task Graph API + Analysis->Task conversion transport layer  [t_b4a29991]
  │    318cde08  feat(phase-2d): close D1 concurrency + D2 provenance from Final-Gate t_ab45f959  [t_08d8fb43]
  └─ 3 doc heads (--no-ff merges by t_67837666):
       00d89c3a  docs: land Phase 2D codebase audit (4ddba22b)                    [t_4b24dd7e]
       18b5640d  docs: land Phase 2D Task Graph & Provenance design + f069 DDL smoke (b80e70e7)  [t_46f8c7cf]
       1ffa7dcc  docs: land Phase 2D Analysis->Task mapping + V1 safety gate spec (d86296a2)  [t_3867a0f9]  ← main head
```

- **Alembic single head = `f070_analysis_task_dedup`** (verified `python -m alembic heads` in `backend/.venv` by `t_67837666`; both `v1_11_5_f069_task_graph_provenance.py` and `v1_11_5_f070_analysis_task_dedup.py` present under `backend/alembic/versions/`; `f070.down_revision == "f069_task_graph_provenance"`).
- The 3 doc heads were confirmed **disjoint from the product chain** before merge; merges ran on the `main` worktree, `clawith-upstream` remote untouched.

> Note on "main is synced": per the card, I do **not** re-assert sync as my own act — that is `t_67837666`'s job. I **read** the post-merge main sha (`1ffa7dcc`) live via `git rev-parse main` / `origin/main` and confirm it matches the LAND handoff. Any divergence after this read is UNKNOWN (out of this report's clock).

---

## 4. Tests — Phase-2D modules, lifecycle regression, and the pre-existing/env failures (with proof they are NOT Phase-2D defects)

### 4.1 Phase-2D module results (re-review `t_74eb3596`, own clean scratch DB)

Command (run on clean scratch DB `clawith_t74eb3596_2d` @ `f070`):

```
DATABASE_URL=postgresql+asyncpg://clawith:***@127.0.0.1:5432/clawith_t74eb3596_2d \
  uv run --extra dev pytest \
  tests/test_task_graph_api.py \
  tests/test_task_decomposition_service.py \
  tests/test_task_graph_provenance.py \
  tests/test_task_graph_provenance_migration.py \
  tests/test_task_api_runtime_intake.py
```

**Result: 57 passed** (55 baseline + 2 new D1/D2 regression tests), in 100.63 s.
New tests:
- `tests/test_task_decomposition_service.py:698 test_r1_concurrent_inverse_edges_at_most_one_no_cycle` (D1).
- `tests/test_task_graph_api.py:826 test_d2_manual_provenance_fails_closed_and_conversion_lane_still_works` (D2).

### 4.2 Existing-lifecycle regression (10 intake/materialization/analysis/git-acq files)

**Result: 315 passed, 1 skipped, 2 FAILED** on clean DB `clawith_t74eb3596_lc` @ `f070`.
The 2 failures are `test_git_acquisition_service.py::test_remote_url_gate_accepts_public_https_url` and `::test_e2e_github_public_repo_default_branch` — **both environmental (github.com E2E), NOT a Phase-2D regression.**

**Proof they are pre-existing/environmental, not Phase-2D:**
- Baseline control: re-reviewer checked out the **pre-rework head `6f32236a`** and ran the identical 2 tests on a **third** clean DB `clawith_t74eb3596_base` → the **same 2 tests fail identically**. Root cause: this host's DNS proxies `github.com→198.18.0.22` / `gitlab.com→198.18.0.58` (the `198.18.0.0/15` IANA-reserved range, unreachable). So the failure predates the rework — the rework introduced **zero new** lifecycle failures.
- Corroborated by the rework handoff `t_08d8fb43` ("308 pass, 1 skipped, 2 pre-existing github.com E2E") and the prior review baseline finding `t_ab45f959`.
- None of the 5 rework diff files is in the git-acquisition service's import path.

### 4.3 The "68 failed" full-suite — proven NOT a Phase-2D defect

On a **single shared** scratch Postgres, the full backend suite showed `68 failed / 2875 passed`. The review `t_ab45f959` ran a **baseline control** to prove these are not Phase-2D:
- **5 failures** (`test_wechat_channel_context`, `test_workspace_reconciliation`, `test_storage_conditional_atomicity`) **reproduce identically on the `e6237916` baseline** (no Phase-2D code) = pre-existing/unrelated.
- The remainder are **shared-DB cross-contamination**: the suite is not DB-isolated per module, so earlier live-DB tests mutate schema/data (`uuid = character varying` tenant_id type drift) and later tests hit it.
- **Every Phase-2D module passes 100% in isolation on a clean DB** (4.1/4.2 above).
- Recommendation (test-infra, not product): run the full suite per-module / on a per-run fresh DB. This is a test-infra concern for the orchestrator; it was explicitly out of rework scope ("do NOT chase the full-suite 68 failed").

### 4.4 Static checks on rework-touched files (`t_08d8fb43` / `t_74eb3596`)

- `ruff check --select F` on all 5 rework-touched files → **All checks passed**.
- `pyright` on rework app files: `task_graph_service.py` + `api/tasks.py` = **0 errors**; `schemas.py` = **1 PRE-EXISTING** error at `:522` (`_redact_channel_secrets`) — a function the D2 diff does **not** touch (out of rework scope; present in baseline `6f32236a`).
- Test-file pyright nits are Low-severity / baseline-style (repo gate is `pyright app` per `backend/AGENTS.md`); non-blocking.
- `ruff` full-suite `B008×652` etc. = **repo-wide pre-existing** FastAPI `Depends()` default-arg pattern, not introduced this phase.

### 4.5 Migration round-trip

`f069` + `f070` single chain off `f068`; full `001→f070` upgrade clean on scratch DBs; `f069` downgrade→upgrade round-trip clean on **both** provisioning paths (fresh `create_all` no-op + pre-f069 existing-DB) — from persistence handoff `t_650ddd87` + re-review. `alembic heads` = single head `f070`.

---

## 5. E2E

- **Live E2E on f070 scratch Postgres** (from `t_b4a29991`, commit `6f32236a`, `tests/test_task_graph_api.py` live tier): graph view **with provenance**, edge add/remove, cycle rejection → 409 + derived blocked/ready, cross-tenant 404/403 isolation, and the **conversion E2E with G4 no-enqueue assertion** (AgentRun count unchanged after conversion; no project status transition — project stays `ANALYZING`); dedup idempotency confirmed.
- **Re-verified in isolation** by re-review `t_74eb3596`: the 5 Phase-2D modules (incl. the live E2E tier) = 57/57 green; the D2 test's "legal conversion lane" sub-case returns 201 with `provenance_consistency(created) is True`.
- **Root §十四 end-to-end proof:** a real Project, analyzed, converts via `TaskDecompositionService` → a set of pending, provenance-filled, dependency-ordered Tasks that can be assigned and gated into execution. Each Task has a clear source (finding→run→revision→project), clear boundary (severity×category×tag closed grid), dependencies (`task_dependencies`), Agent-assignment boundary (pending + provenance only, no auto-enqueue/assignment row), execution path (`ensure_ready` gate), and evidence (5 test modules + API contract tests).

---

## 6. Review Chain + Rework

```
t_ab45f959  (review)        REQUEST_CHANGES — 4 gates PASS in isolation; 2 Medium defects: D1 (concurrency), D2 (provenance)
        │
        ▼
t_08d8fb43  (rework)        closes D1 + D2 on top of 6f32236a → commit 318cde08 (5 files: 3 app + 2 test)
        │
        ▼
t_74eb3596  (re-review)     APPROVE — independently re-verifies D1+D2 closed at 318cde08; 5 modules 57/57 green; zero new lifecycle regression
```

### Defect D1 — concurrency (gate #4; design §8 R1 + §11 test-matrix "并发/互逆边")
- **Found (t_ab45f959):** `task_graph_service._add_edges` (~L243-256) did the §5.2 cycle-reachability check as a **READ-then-WRITE with no serialization point**. Under Postgres `READ COMMITTED`, two concurrent inverse inserts (A→B & B→A) each read the pre-commit state, both pass, both commit → a **2-cycle** that `UNIQUE(self-pair)` + self-`CHECK` cannot catch. The design-mandated R1 concurrency test was **absent**.
- **Closed (t_08d8fb43):** new helper `_lock_project_graph` (`task_graph_service.py:174`) takes a project-scoped `pg_advisory_xact_lock(hashtextextended("task_graph:{tenant}:{project}",0))` held to transaction end; wired into `_add_edges` at `:289` **immediately before** the §5.2 reachability read (`:291`); `add_edge` + `bulk_add_edges` both funnel through `_add_edges` → **no bypass**. A concurrent inverse writer blocks until the first commits, then re-reads the committed edge and is refused as `GRAPH_CYCLE`. The §5.1 fail-closed order is unchanged.
- **Regression test:** `tests/test_task_decomposition_service.py:698 test_r1_concurrent_inverse_edges_at_most_one_no_cycle` (two sessions insert inverse edges concurrently → exactly one `added`, one `GRAPH_CYCLE`, no 2-cycle afterward). Re-run 4× in the rework → 4/4 passed (advisory-lock serialization is not flaky).

### Defect D2 — provenance (gate #2; design §4.2 "落库前 fail closed" / mapping §1 rule 3 "no bypass path")
- **Found (t_ab45f959):** `api/tasks.py::create_task` (~L94-113) + `::update_task` (~L177-179) persisted the 5 provenance fields **RAW with no §4.2 cross-table check**. The validator `task_provenance_dao.provenance_consistency` (`task_dao.py:335`) was shipped + unit-tested-correct but **ORPHANED** (0 call sites in `app/`). A manual caller could forge `created_reason=ANALYSIS_FINDING` + a finding that does not belong to the named run/revision and it would persist → untraceable/corrupt provenance on the exact fields this phase guarantees. `created_reason` was free text at the schema layer.
- **Closed (t_08d8fb43, Option A = reviewer's recommended default):** `created_reason` on `TaskCreate`/`TaskUpdate` tightened to the closed 3-value set via `TASK_CREATED_REASONS` (`schemas.py:8,371,419`). `create_task` (`api/tasks.py:117,132`) runs `provenance_consistency` **before** `query_dao.add`; on `False` → 400 `PROVENANCE_INCONSISTENT`, nothing written (fail closed). `update_task` (`api/tasks.py:214,228`) validates merged provenance on a **detached probe** Task (never added to the session) so the validator's internal SELECT cannot autoflush a forged UPDATE into the `f070` UNIQUE constraint — single authoritative write only on a pass. Validator enforces: closed 3-value reason (None/unknown → fail closed), `MANUAL ⇒ all 4 analysis cols NULL`, `ANALYSIS_FINDING/PLANNING ⇒ project+run+revision tenant-aligned & (FINDING) run terminal`.
- **Regression test:** `tests/test_task_graph_api.py:826 test_d2_manual_provenance_fails_closed_and_conversion_lane_still_works` — (a) `MANUAL` + non-null analysis col → 400 + no row; (b) `ANALYSIS_FINDING` + revision-mismatch on POST **and** PATCH → 400 + no row; (c) legal conversion lane on a fresh `AN_COMPLETED` run → 201 + `provenance_consistency(created) is True`. PASSED on a clean DB.

---

## 7. Known TODO (bounded, explicitly not Phase-2D product defects)

1. **G5 unreachable guard** — `convert` has a `len(findings) > MAX_TASKS_PER_INVOCATION` branch (`task_decomposition_service.py` ~L269) that is **unreachable**: the `list_for_run` read is already capped at the same `MAX_TASKS_PER_INVOCATION = 100` and the analysis lane caps writes at 100. Defense-in-depth; **document or drop** (accepted V1 limit).
2. **Design §5.4 part-b not implemented** — post-completion "recompute downstream + '▶ ready' hint" `TaskLog` is **not** wired; `task_dependency_dao.list_dependents` (`task_dao.py:70`) is **unused in production**. Root §十四 V1 minimum only requires the *query capability* + the *execution gate*, both present. Accepted documented V1 limit; wire only if the team wants it.
3. **Test-infra** — full backend suite on a **single shared scratch Postgres** has schema contamination + stalls at ~68% (CPU/PG contention). Recommendation: run per-module / on a per-run fresh DB. Separate test-infra card; not a Phase-2D product defect.
4. **2 pre-existing github.com E2E failures** — `test_git_acquisition_service.py` public-URL E2E tests fail **only** on hosts that DNS-proxy `github.com` into the unreachable `198.18.0.0/15` range. Out of Phase-2D scope; pass on a host with real github.com reachability.

---

## 8. Known Limitations (accepted V1 limits, documented in the mapping + design docs)

- **Flat task set from conversion** — no dependency authoring during conversion (the graph lane owns dependency authoring); V1 conversion produces a flat, pending, provenance-filled task set.
- **No LLM/Planner classification** — classification is the closed-grid rule-based `severity×category×tag` engine only.
- **No knowledge injection** — `ProjectKnowledge` is read-only today (`GET /projects/{id}/knowledge`); injection to shrink Task scope / durable acceptance criteria is deferred.
- **No project status transition on conversion** — project stays `ANALYZING`.
- **No PENDING_CONFIRMATION UI** — `O1` auto-enqueue and `O2` Proposal-entity + PENDING_CONFIRMATION-UI are both **documented and rejected** in the mapping doc §2 ADR; "pending Task + filled provenance" *is* the persisted proposal, so a future confirmation UI builds on top without schema change.
- **created_reason closed set** = `{MANUAL, ANALYSIS_FINDING, ANALYSIS_PLANNING}`; the V1 mapping lane consumes only `{MANUAL, ANALYSIS_FINDING}`.
- **Oversized-graph bound** — `MAX_PROJECT_EDGES = 1000` → fail-closed refusal (documented bound).
- **Task-level Review/Artifact persistence still absent** — from the audit finding J: Run-level artifact refs + completion-gate verdicts live only in `AgentToolExecution.result_metadata` + checkpoint/`AgentRunEvent`; no Task-level review/artifact table yet.
- **Agent Assignment entity** is not yet a first-class object; Phase 2D defined the **Task ≠ Run boundary** and guarantees conversion produces pending+provenance only (no auto-enqueue, no assignment row).

---

## 9. Architecture Decisions (ADRs from the design + mapping docs)

From `docs/PHASE_2D_TASK_GRAPH_PROVENANCE_DESIGN.md` (t_46f8c7cf, `b80e70e7`):
- **ADR-1 — Physical FKs follow the f066–f068 Phase 2C precedent.** The `tasks`/`task_dependencies` provenance + edge FKs are physical DDL, matching the established Phase 2B/2C pattern for the Project/Analysis→Task domain (documented as a constitutional no-op, not a new violation).
- **ADR-2 — blocked/ready is DERIVED, not persisted.** `task_status_enum` stays 3 values (`pending/doing/done`); no `blocked` status value is added. Readiness is computed on demand via bounded direct-dependency SQL.
- **ADR-3 — No Task→Run reverse FK.** Reuse the forward `agent_runs.source_type/source_id` chain instead of adding a reverse FK.
- **ADR-4 — Post-completion only *hints* (TaskLog), never auto-enqueue.** Automatic execution behavior is deliberately deferred to the mapping safety gate.

From `docs/PHASE_2D_ANALYSIS_TASK_MAPPING.md` (t_3867a0f9, `d86296a2`):
- **ADR (§2) — V1 safety gate = explicit invocation + create/execute decoupling.** Converted Tasks land in `status=pending` and are **never** auto-enqueued (gate G4, with a dedicated no-enqueue test); execution stays on the existing human-trigger path. Both `O1` (auto-enqueue) and `O2` (Proposal-entity + PENDING_CONFIRMATION-UI) are documented and rejected.
- **ADR (§1) — The boundary owner is a new explicit conversion step** (`TaskDecompositionService` + `POST /projects/{id}/analysis/{run_id}/tasks`); the analysis lane stays read/understanding and conversion never auto-runs on `record_findings`. No bypass path (mapping §1 rule 3) — enforced at D2 closeout.

From rework `t_08d8fb43`:
- **ADR — D2 Option A (fail-closed validator wired in, not Option B reject-all).** The manual lane stays usable for *consistent* provenance; `created_reason` is a closed enum; `MANUAL ⇒ 4 analysis cols NULL`. (Documented in the commit message + `api/tasks.py` inline notes.)
- **ADR — D1 = project-scoped `pg_advisory_xact_lock`** (precedent `chat_session_service._lock_direct_scope`), not `SELECT ... FOR UPDATE`.

---

## 10. Provenance Model (concrete)

5 columns on `tasks` (`models/task.py`, landed by `f069`):

| Column | Type / DDL | Semantics |
|---|---|---|
| `project_id` | FK→`projects`, `ON DELETE CASCADE` | which Project this Task belongs to |
| `analysis_run_id` | FK→`analysis_runs`, `ON DELETE SET NULL` | which Analysis Run produced it (run may be deleted/re-analyzed) |
| `finding_id` | FK→`analysis_findings`, `ON DELETE SET NULL` | which specific Finding (transient; dies with its run) |
| `revision_sha` | text (denormalized snapshot, **not** an FK) | the source-repo revision the analysis ran against |
| `created_reason` | closed 3-value enum, `server_default='MANUAL'`, no backfill | `MANUAL` / `ANALYSIS_FINDING` / `ANALYSIS_PLANNING` |

- **Traceability chain:** `Task → finding → run → revision → project` (design §4.2 / mapping §3).
- **§4.2 fail-closed validator** `task_provenance_dao.provenance_consistency` (`task_dao.py:335`): closed-reason rule + `MANUAL ⇒ all 4 analysis cols NULL` + `ANALYSIS_FINDING/PLANNING ⇒ tenant-aligned & (FINDING) run terminal`. Wired into `api/tasks.py:117` (create) + `:214` (update, detached probe) → `400 PROVENANCE_INCONSISTENT`, no write on violation.

---

## 11. Task Graph Model (concrete)

- **Edge table `task_dependencies`** (`f069`): tenant-scoped, `UNIQUE(task_id, depends_on_task_id)`, `CHECK task_id != depends_on_task_id` (no self-edge), `ON DELETE CASCADE` on **both** FK ends (Task delete → edge vanishes).
- **Validation (§5.1–§5.4, `task_graph_service.py`):** fail-closed order not-found/tenant/self/supervision/project/cycle/exists; §5.2 bounded-DFS cycle check (inbound-only, same-project bounded, `MAX_PROJECT_EDGES=1000` → fail closed); §5.3 derived blocked/ready via 2 bounded SQL (no N+1, **no new status value**); §5.4 execution gate `ensure_ready` (`:403`) wired into `enqueue_task_runtime`'s single funnel (`task_executor.py:44/98/171`, `TaskBlockedError` → `_log_blocked`).
- **Concurrency (R1 / D1):** `_lock_project_graph` (`task_graph_service.py:174`) = `pg_advisory_xact_lock(hashtextextended('task_graph:{tenant}:{project}',0))` held to transaction end; taken at `:289` **before** the `:291` reachability read; `add_edge`/`bulk_add_edges` both funnel through `_add_edges` → no bypass.
- **DB double-line defense (design R6):** self/duplicate edges are backstopped by DB `CHECK` + `UNIQUE`; cycles are enforced by the **service** authoritative gate (schema cannot express "no cycle").

---

## 12. Analysis → Task Contract (concrete)

- **Owner:** `TaskDecompositionService` + `POST /projects/{project_id}/analysis/{run_id}/tasks` (`api/projects.py:646`). Analysis is the read/understanding lane; conversion never auto-runs.
- **Run gate (G1):** only convert when `AnalysisRun.status = AN_COMPLETED` (M9 tenant scope).
- **Classification (mapping §3.3, closed):** executable `E1 = TECH_DEBT ∧ FACT ∧ sev≥WARN`, `E2 = SECURITY ∧ FACT ∧ sev≥{HIGH,CRITICAL}`; planning-only = `OPEN_QUESTION`, `RISK`, `tag∈{INFERENCE,UNKNOWN}`, `sev=INFO`, default fail-closed. The engine is the closed 5×4×4=80-combo `severity×category×tag` truth table.
- **Field mapping:** `severity→priority` closed map; **500-char title truncation** (boundary tested at 499/500/501); `agent_id` mandatory (`TD_AGENT_REQUIRED`).
- **Dedup (§4):** `converted_finding_ids` pre-check + `f070` `UNIQUE(analysis_run_id, finding_id)` floor.
- **G4 no-enqueue:** converted Tasks are `pending` + provenance only; **no auto-enqueue** (asserted live via AgentRun count), no project status transition, no assignment row.
- **Provenance (this lane):** `project_id/analysis_run_id/finding_id/revision_sha` mandatory; `created_reason` closed V1 set `{MANUAL, ANALYSIS_FINDING}`.

---

## 13. Final Gate status (root §十五, 14 conditions)

| # | Condition | Status | Evidence |
|---|---|---|---|
| 1 | Task Decomposition contract determined | ✅ | mapping doc §3 (`d86296a2`) |
| 2 | Analysis→Task provenance determined | ✅ | design §4.2 + mapping §3 (`b80e70e7`, `d86296a2`) |
| 3 | Task Graph V1 determined + independent review | ✅ | design doc + review `t_ab45f959` + re-review `t_74eb3596` |
| 4 | Task persistence/API/service at V1 | ✅ | `f069`/`f070` + `TaskGraphService` + API lanes (`a1bb030e`,`5e8fc789`,`6f32236a`) |
| 5 | dependency validation has real tests | ✅ | 5 modules, R1 concurrency + cycle tests green |
| 6 | Analysis→Task real E2E passed | ✅ | conversion E2E on f070 scratch; G4 no-enqueue live-asserted |
| 7 | Agent assignment boundary verified | ✅ | Task≠Run; conversion = pending+provenance only (no auto-enqueue/assignment) |
| 8 | Phase 0–2C capabilities not broken | ✅ | 146 acceptance + 315 lifecycle green in isolation; git E2E 6/6 in isolation |
| 9 | migrations/regression/E2E pass | ✅ | `f069`/`f070` single head, round-trip clean; isolated green (full-suite env failures are NOT Phase-2D, §4.3) |
| 10 | Independent Review = APPROVE | ✅ | re-review `t_74eb3596` = APPROVE @ `318cde08` |
| 11 | Rework re-reviewed = APPROVE | ✅ | `t_74eb3596` APPROVE (rework `t_08d8fb43`) |
| 12 | Final Gate = PASS | ✅ | gate released by APPROVE re-review; D1+D2 closed |
| 13 | `main`/`origin/main` confirmed synced | ✅ | `t_67837666` PASS; live read `main == origin/main == 1ffa7dcc`, `rev-list 0 0` |
| 14 | No un-merged real product code | ✅ | full product chain ff-merged + 3 doc heads `--no-ff` merged & pushed (`t_67837666`) |

**Final Gate = PASS.** Root `t_55d5bee6` may be closed once this §十六 report lands. Known limitations/TODOs (§7/§8) are **documented and accepted** — they are not blocking the gate.

> UNKNOWN caveats (honest, not asserted as fact): the 2 github.com E2E failures depend on host network reachability (UNKNOWN whether they pass on a non-DNS-proxying host — expected to pass); the full-suite shared-DB contamination is a test-infra property, UNKNOWN whether it has been remediated; and any `main`/`origin/main` state diverging **after** the `1ffa7dcc` read is outside this report's clock.

---

## 14. Provenance / evidence index (all citations)

**Design / spec docs (all on main @ `1ffa7dcc`):**
- `docs/PHASE_2D_CODEBASE_AUDIT.md` @ `4ddba22b` (audit `t_4b24dd7e`)
- `docs/PHASE_2D_TASK_GRAPH_PROVENANCE_DESIGN.md` @ `b80e70e7` (design `t_46f8c7cf`)
- `docs/PHASE_2D_ANALYSIS_TASK_MAPPING.md` @ `d86296a2` (mapping `t_3867a0f9`)

**Product commits (main chain):** `a1bb030e` (f069) → `a77dc3b6` (merge) → `5e8fc789` (service+f070) → `6f32236a` (API) → `318cde08` (D1+D2 rework).

**Key file:line anchors (verified in worktree @ `1ffa7dcc`):**
- `backend/app/services/task_graph_service.py:174` (`_lock_project_graph`), `:192–193` (advisory lock), `:289` (wired before `:291` reachability read), `:403` (`ensure_ready`), `:79` (`MAX_PROJECT_EDGES=1000`)
- `backend/app/services/task_executor.py:44,98,171` (`enqueue_task_runtime` gate funnel)
- `backend/app/api/tasks.py:117,132` (create_task gate), `:214,228` (update_task detached-probe gate), `:352–409` (3 graph endpoints)
- `backend/app/api/projects.py:646` (conversion endpoint)
- `backend/app/dao/task_dao.py:335` (`provenance_consistency`), `:70` (`list_dependents`, unused-in-prod V1 limit)
- `backend/app/schemas/schemas.py:8,371,419` (`created_reason` closed enum)
- `backend/app/models/task.py:37` (`TASK_CREATED_REASONS`), `:59` (`task_status_enum` 3 values), `:109` (enum DDL)
- `backend/alembic/versions/v1_11_5_f069_task_graph_provenance.py`, `v1_11_5_f070_analysis_task_dedup.py`
- `backend/tests/test_task_decomposition_service.py:698` (D1 regression), `backend/tests/test_task_graph_api.py:826` (D2 regression)

**Kanban evidence (board `ai-company-os`):** `t_ab45f959` (REQUEST_CHANGES), `t_08d8fb43` (rework → `318cde08`), `t_74eb3596` (APPROVE @ `318cde08`), `t_67837666` (LAND → `1ffa7dcc`).

**Scratch DBs kept for the operator / any re-verification (single-query mode blocked `DROP DATABASE`):** `clawith_t74eb3596_2d`, `clawith_t74eb3596_lc`, `clawith_t74eb3596_base` (re-reviewer); `clawith_t08d8fb43_2d_v2`, `clawith_t08d8fb43_lc` (rework, kept); `clawith_tb8545ece_f070`, `clawith_t650ddd87_f069_79bafb` (API/persistence). None are shared/production.

---

*This report is a doc-only deliverable on branch `ai-company-os/t_52378aa0-phase-2d-convergence-report-final-delive`; no product code was modified. Absolute artifact path: `I:\project\AI Company OS\.worktrees\t_52378aa0\docs\PHASE_2D_CONVERGENCE_REPORT.md`.*
