# Phase 3 Convergence Report — Planning & Squad Orchestration

**Task:** `t_557d9d46` (Convergence Report, Root criterion 11)
**Author:** aco-architect
**Root:** `t_1933cee8` (Phase 3 — Project Planning & Squad Orchestration)
**Landed-on basis:** `main == origin/main == 6a7d869a` (tag `PHASE_3_FEATURE_LANDED`), base `44651184` (tag `PHASE_2F_CLOSED`).
**This document is a durable convergence artifact only** — no new code, no new migration. It ties the phase to its final-main evidence: design → implementation → tests → E2E → independent review → rework → landed main. It does **not** carry the final `PHASE_3_CLOSED` tag; that belongs to the Final Gate (`t_b8d1163b`).

---

## 0. Epistemic discipline & self-re-verification

Classification follows the Phase 2C-2F convention and the Root brief:

- **FACT** — directly observed at the cited commit / from a named constant or migration id.
- **OBSERVATION** — a pattern seen in one or more concrete instances, no universal claim.
- **INFERENCE** — a conclusion drawn from the facts above.
- **UNKNOWN** — not verifiable from this tree; the resolving action is stated.

**Two classes of evidence, kept separate (do not conflate):**
1. **[VERIFIED-ON-MAIN]** — re-run by *this* card against `main` = `6a7d869a` in worktree `wt/t_557d9d46` on 2026-09-30, on a fresh clawith-owned scratch Postgres **`aco_arc_p3_main`** (native PG16 @ `127.0.0.1:5432`, DSN `clawith:clawith`, distinct from the builder's `clawith_t_95658464_*` and the reviewer's `aco_rr_*` sets). Where the report cites a live count as FACT it is a re-execution, not a worker self-report.
2. **[CARD-HANDOFF]** — taken from a completed card's summary/artifact (builder `t_95658464`, original review `t_474b92b1`, re-review `t_1cb8757d`, landing `t_4a80b477`). These are *independently produced* records; I did not re-burn every underlying number, only the load-bearing ones under class 1.

The two review reports live on their own card worktrees (card-evidence policy: worker evidence stays on card branches, main stays clean of review scratch), not on `main`:
- Original review (REQUEST_CHANGES): `I:\project\AI Company OS\.worktrees\t_474b92b1\REVIEW_REPORT_PHASE3_PLANNING.md`
- Re-review (APPROVE): `I:\project\AI Company OS\.worktrees\t_1cb8757d\REVIEW_REPORT_PHASE3_RERECOMMIT.md`

---

## 1. Scope & lineage

**One-line chain (the deliverable this phase closes):**
`Project → Analysis → Plan → Task Graph → Squad/Assignment → Runtime → Run → Tool → Result → Settlement`

- `Project → Analysis → Task Graph` is the frozen Phase 2C/2D spine (base `44651184`).
- This phase adds the **`Plan`** node (durable Planning domain, 5 tables) and makes the **`Squad/Assignment`** node a computed lane over it; the tail `Runtime → Run → Tool → Result → Settlement` is the **reused, frozen Phase 2F spine** — not rebuilt.

**Exact commit lineage that landed it (all reachable from `main`):**

```
44651184  PHASE_2F_CLOSED (base; untouched)
   │
   ├─ 1198ee20  f071 planning persistence (5 tables + DAO + guarded migration)          [t_b342df97]
   ├─ 1bdd3714  PlanningService create_plan + provenance_for_task                       [t_12874a76]
   ├─ 892451e6  Agent Assignment lane + concurrency validation                          [t_9820b3d3]
   ├─ ffb4a6fd  fix(test): dispose app engine after _ensure_schema bootstrap loop        [t_e89399cc]
   ├─ 089fe2c0  Plan/Assignment execution adapter into Phase 2F Runtime intake           [t_e89399cc]
   └─ d22c7ec8  rework Planning adapter per t_474b92b1 review (D1+D2+D3)                [t_95658464]
   │
   ╰──ff8a9608  MERGE feature line (--no-ff, +7585 insertions) into main
   │
   1e29f1e1  pre-audit report        → MERGE 2064d8b7   [t_82050064]
   e700411b  execution-chain trace   → MERGE 9cb3c5f5   [t_5487441c]
   39b412af  Planning boundary design→ MERGE ee450434   [t_0739e600]
   38e4a414  Squad/Team V1 design    → MERGE 6a7d869a   [t_d6b24a82]

   6a7d869a == origin/main == tag PHASE_3_FEATURE_LANDED   [t_4a80b477]
```

- **Feature line** is a single linear spine `44651184..d22c7ec8` (8 commits: `1198ee20, 1bdd3714, 892451e6, ffb4a6fd, 089fe2c0, d22c7ec8` plus the two merge-parent records `d8413adb/6da82a9a` inside `t_9820b3d3`). Merged `--no-ff` into main as `ff8a9608`, then the **4 docs/audit branches merged one-by-one** after it (docs-only, zero conflict): `2064d8b7, 9cb3c5f5, ee450434, 6a7d869a`.
- **[VERIFIED-ON-MAIN]** `git rev-parse main origin/main` → both `6a7d869a6c6773ca1a8918dc308e97c216d440fb`; `git rev-list -1 PHASE_3_FEATURE_LANDED` → `6a7d869a…`. main == origin/main, tag present.
- **[VERIFIED-ON-MAIN]** Frozen-file proof: `git diff 44651184..HEAD` over the 5 frozen execution/model files (`task_graph_service.py`, `task_execution_service.py`, `agent_runtime/`, `models/task.py`, `models/analysis.py`) = **0 add / 0 delete**. Total Phase 3 diff = 21 files, +9488, 0 deletion lines. The code change is purely additive to frozen files; the deletions inside the feature line (the D1/D2/D3 rework's `+191 -45` on two *new* Phase 3 files) do not touch frozen code.

---

## 2. Design boundary summary (what the design pinned, now on main)

Sources: `docs/architecture/PHASE_3_PLANNING_DOMAIN_BOUNDARY_DESIGN.md` (`t_0739e600`, `39b412af`), `docs/architecture/PHASE_3_SQUAD_ORCHESTRATION_V1_DESIGN.md` (`t_d6b24a82`, `38e4a414`), both now on main.

**The 5 Planning tables (new, tenant-scoped, `models/planning.py`):**
`planning_runs` (the durable revision; `PL_OPEN/PL_COMPLETED/PL_FAILED` closed result-codes, **not** a workflow SM) · `planning_goals` · `work_packages` · `milestones` · `work_package_tasks` (the only Planning→Task link, `uq_wp_tasks`).

**Reused-not-duplicated facts (Root criterion 2):**
- **Task Graph** — planning materializes into existing `Task` + `task_dependencies`; edges only through frozen `task_graph_service.bulk_add_edges` (`planning_service.py:738,752` [VERIFIED]). No second edge table, no second readiness model.
- **`Task.agent_id`** — the single authoritative assignment fact (`models/task.py:50`). The only planning-side write is the additive `TaskProvenanceDAO.update_agent_binding` (`dao/task_dao.py:299` [VERIFIED]). No `TaskAssignment`/`SquadAssignment` table.
- **Runtime spine** — plan/assignment tasks enter the **existing** Phase 2F `TaskExecutionService.execute → enqueue_task_runtime → RuntimeCommandIntake` path. No second runtime/checkpointer/worker/inbox/claim-loop/settlement.
- **`ANALYSIS_PLANNING` producer slot** — the reserved `TASK_CREATED_REASONS` value (`models/task.py:37`) now has a real owned producer (`PlanningService.create_plan` materializes `created_reason="ANALYSIS_PLANNING"`, `finding_id=NULL`).

**Explicit V1 "no" decisions (Root criterion 4):**
- **No org entity / no Squad/Team/Role table.** Squad is a *derived* concept (WorkPackage + its `Task.agent_id` bindings + computed CONF/REV constraints). Zero new tables/columns/SM from the squad design.
- **No 2nd runtime, no 2nd graph/edge, no 2nd assignment fact, no 2nd lock layer** (trace §7.2 seven "must-NOT-duplicate" rules, all honored).
- **No new state machine** — every lifecycle column is a closed result-code enum; run lifecycle stays in `AgentRun` checkpoints.
- **No new lock** — resource conflicts are *detected* at plan-validation/assignment (CONF-1/CONF-4 fail-closed); *enforcement* stays in the existing tenant-prefixed Redis workspace lock + the DAG.

---

## 3. Evidence table (Root gate criteria 1–12)

| # | Criterion | Concrete evidence | Class |
|---|---|---|---|
| 1 | Planning domain has a reasonable, evidenced boundary | `docs/architecture/PHASE_3_PLANNING_DOMAIN_BOUNDARY_DESIGN.md` (6-area matrix, §5 overlap flags) + the 5 `planning_*`/`work_packages`/`work_package_tasks` tables on main. Every new entity has a §5 "new vs reusable" justification. | FACT |
| 2 | No duplication of existing Project/Analysis/Task/Runtime | §2 facts: Task Graph via `bulk_add_edges`, single `Task.agent_id` via `update_agent_binding` (`task_dao.py:299`), reused Phase 2F spine, `ANALYSIS_PLANNING` producer. Frozen-file diff 0 add/0 delete [VERIFIED]. | FACT |
| 3 | Task Graph accepts Planning output | `planning_service.py:738,752` emit `task_dependencies` through frozen `bulk_add_edges`; E2E `test_planning_chain_creates_graph_and_tasks` asserts real `tasks` + `task_dependencies` rows [VERIFIED live]. | FACT |
| 4 | Squad/Team abstraction (if needed) truly integrated | V1 = **no org entity** (S1). `Task.agent_id`-only + derived-squad model landed; CONF/REV predicates exercised live (`test_assignment_lane_applies_single_agent_fact`, `test_assignment_review_independence_refuses_single_candidate`) [VERIFIED live]. | FACT |
| 5 | Agent assignment accepts Planning results | `assignment_service.py` `apply_assignment` consumes the `task_scope` §4.1 slot contract (`candidate_agent_ids`, `kind`, `required_capabilities`, per-slot `shared_resources`) → resolves one `Task.agent_id` per task. | FACT |
| 6 | Real execution path holds | Full chain `Project→Analysis→Plan→TaskGraph→Assignment→Runtime→Run→Tool→Result→Settlement` re-run on main: live E2E `test_planning_execution_chain_e2e_acceptance.py` **10/10 PASSED on `aco_arc_p3_main` (67.23 s)** incl. the D1-flagged flagship `test_enqueue_plan_tasks_adapter_end_to_end` [VERIFIED]. | FACT |
| 7 | Evidence complete (FACT/OBS/INFER/UNKNOWN + commit/test/migration/E2E) | This report + the 4 on-main docs + 2 review reports (card worktrees) + per-card test/migration counts below. | FACT |
| 8 | Independent Reviewer APPROVE | Re-review `t_1cb8757d` verdict **APPROVE** on its own scratch PG (`aco_rr_p3e2e/…/…old`) [CARD-HANDOFF]. | FACT (handoff) |
| 9 | Necessary rework done | `d22c7ec8` closes D1+D2+D3; re-verified APPROVE [CARD-HANDOFF] + `test_enqueue_plan_tasks_adapter_end_to_end` green on main [VERIFIED]. | FACT |
| 10 | Git main == origin/main | `6a7d869a == origin/main`, tag `PHASE_3_FEATURE_LANDED` present [VERIFIED]. | FACT |
| 11 | Convergence Report complete | This document (`t_557d9d46`). | FACT |
| 12 | Final Gate explicitly PASS | Deferred to `t_b8d1163b` (independent reviewer). §5 recommends **PASS**; the gate card adjudicates and owns the `PHASE_3_CLOSED` tag. | DEFERRED |

**Test / migration evidence detail (load-bearing numbers):**

| Suite | Count | Source |
|---|---|---|
| E2E live `test_planning_execution_chain_e2e_acceptance.py` | **10/10 PASSED** (67.23 s) on `aco_arc_p3_main` | [VERIFIED-ON-MAIN this run] |
| E2E no-DB baseline | **1 passed, 9 skipped** (73.53 s) — the documented evidence boundary: only the DB-free `topological_order` test runs without a DSN | [VERIFIED-ON-MAIN this run] |
| f071 migration | single head `f071_planning_persistence` (`alembic heads`), parent `f070` [VERIFIED]; guarded up/down verified on a real scratch PG via the full 001→f071 chain + idempotent re-upgrade | [VERIFIED-ON-MAIN head] [CARD t_b342df97] |
| Supporting suites (planning_service / planning_dao / assignment_service / planning_persistence_migration) | 59/59 | [CARD-HANDOFF re-review + landing] |
| Landing pre-push battery | ruff 0 across 11 new Phase 3 files · pyright 0 on both rework-touched files · task suites 119+33 · 2C project-analysis E2E 6/6 | [CARD-HANDOFF `t_4a80b477`] |

**D1 flagship re-verification detail [VERIFIED]:** `PlanExecutionReport` is still `@dataclass(frozen=True, slots=True)` (`plan_execution_service.py:65`) but is now constructed at **exactly two** sites (`:247` no-plan-tasks early return, `:294` final return); per-outcome data accumulate in locals. The in-loop `report.<tuple> = …` rebind that raised `FrozenInstanceError` is gone, and `test_enqueue_plan_tasks_adapter_end_to_end` genuinely drives `enqueue_plan_tasks` (call#1 populates `enqueued`+`blocked`, settles the head Run through the real spine, call#2 populates `skipped_settled` + enqueues the dependent). The method is no longer dead code.

---

## 4. Defect ledger (D1/D2/D3) + design flags

**Original review `t_474b92b1` → VERDICT REQUEST_CHANGES** (independent, on the reviewer's own scratch PG `aco_reviewer_p3_scratch`; six chain steps + dependency/parallel/failure/tenant/idempotency all PASS, E2E 9/9 genuine). Defects:

| Defect | Sev | Location | What closed it (`d22c7ec8`, rework) | Re-review verdict |
|---|---|---|---|---|
| **D1** | HIGH | `plan_execution_service.py` `enqueue_plan_tasks` reassigned tuple fields on a frozen dataclass → `FrozenInstanceError` on any real enqueue; shipped-broken + dead code | Accumulate in locals; `PlanExecutionReport` built once at the 2 sites; **new live E2E test** drives it end-to-end | CLOSED (re-run: live 10/10) |
| **D2** | MEDIUM | `test_planning_execution_chain_e2e_acceptance.py` had 14 ruff errors, contradicting the "ruff clean" claim | 14→0 on the E2E file; 0 across all 11 new feature files | CLOSED (ruff 0) |
| **D3** | MEDIUM | `_ensure_schema` bootstrap discovered ORM models via `os.getcwd()+app/models` (CWD-fragile → empty metadata → live tests ERROR not SKIP from repo root) | Now imports `app.models` by name via `pkgutil.iter_modules` (CWD-independent). Proven: old code ERRORS 9 on a fresh DB from repo root; new code 10/10 under identical conditions | CLOSED (old-ERROR / new-PASS) |

**Re-review `t_1cb8757d` → VERDICT APPROVE** on its own scratch PG (`aco_rr_p3e2e / aco_rr_p3cwd / aco_rr_p3old`); all 3 defects independently reproduced closed; hard boundaries intact; "necessary rework done" (criterion 9) satisfied. **[CARD-HANDOFF]**

**Two design-flags — disposition:**

- **Flag 1 — single-candidate planner ⇒ SECURITY review-independence structurally unachievable through the shipped planner.** `planning_service.py:679` sets `candidate_agent_ids = [str(agent.id)]` (one agent) [VERIFIED]. For a SECURITY finding the only candidate = builder = reviewer, so `PL_REVIEWER_NOT_INDEPENDENT` fires fail-closed **before any write** (asserted by `test_assignment_review_independence_refuses_single_candidate`, green). Fail-closed-correct per design D-3; the capability→Agent join is deferred item **D-1**. **Disposition: DEFER (non-defect).** It does *not* block the gate: it is fail-closed, so a SECURITY review cannot silently pass through the planner — it is refused, not allowed. The structural gap (V1 has no way to declare a *distinct* reviewer agent through the shipped planner) is named, not hidden.
- **Flag 2 — no runtime execution-parallelism cap.** `max_parallel_tasks` is stored (`models/planning.py`) and consumed only as the **advisory** `PL_PARALLELISM_ADVISORY` in `assignment_service.py:537/544` ("advisory only in V1"); there is **no runtime enforcement** anywhere in `task_execution_service` / `plan_execution_service` [VERIFIED]. Consistent with CONF-5 advisory-only + deferred global cap **D-2**. **Disposition: DEFER (non-defect, by design).** Not a regression; the per-project cap + lane-key generalization is a scoped runtime-side follow-up.

Both were re-confirmed as non-defects in the re-review and **do not block** the gate.

---

## 5. Residual risks / UNKNOWNs (named, not hidden)

- **G8 — `TaskCompletionGate` fail-open (inherited from Phase 2F, not new).** Phase 3 enforces review-independence **at assignment time** (REV-1/REV-2: two `Task` rows + distinct `Task.agent_id` + the blocking `builder→reviewer` DAG edge exist *before* execution), so the shipped chain does not rely on the runtime gate. The fail-open runtime gate remains a known inherited weakness (deferred **D-3**). Named, not papered over.
- **Supervision / deadline scheduler consumer still missing** (G1, audit Q7/Q9 #6/#9). `Task.remind_schedule` / `supervision_channel` are storage-only; no `scheduler.py`/`trigger_daemon.py`/`heartbeat_runtime.py` consumer drives them. Deferred **D-4**. If a future phase assumes supervision auto-runs, it must first wire a real consumer (reuse `scheduler.py`, not a new engine).
- **`AGENT_RUNTIME_COMMAND_CONCURRENCY` is a global worker bound, not a per-project/task cap.** Task Runs carry no `scheduling_lane_key`, so two ready tasks in one Project run in parallel unbounded (G4/G6). Planning *detects* conflicts (CONF-1/CONF-4); the per-task cap is deferred **D-2**.
- **G5 — trigger claim-loop wiring** (`claim_pending_trigger_executions`) has no production caller found in-tree; UNKNOWN whether a distributed trigger-claim loop is wired in a separate worker image. Out of Phase 3 scope; re-probe in `deploy/`/`helm/`/`scripts/` before assuming it runs.
- **Live-DB UNKNOWNs inherited from the audit** (whether live `Task` rows use `ANALYSIS_PLANNING`, live edge counts, live `AgentSchedule` consumption, live supervision values) — live-DB questions, out of a design/convergence card's scope; re-probe against production if a future phase needs them.

---

## 6. Recommendation to the Final Gate

**Recommend: PASS.**

Reasoning (against the 12 Root criteria in §3):
- Criteria **1–7** are met by evidence on main (design doc + 5 planning tables; reused-not-duplicated facts; `bulk_add_edges` materialization proven live; squad as derived model with CONF/REV exercised; `task_scope` consumption by the assignment lane; full chain re-run **10/10 live** on main; this report ties it together).
- Criterion **8** (independent APPROVE): re-review `t_1cb8757d` returned **APPROVE** on the reviewer's own scratch PG.
- Criterion **9** (necessary rework done): D1+D2+D3 closed on `d22c7ec8`, independently re-verified; the flagship `enqueue_plan_tasks` now genuinely executes.
- Criterion **10** (main sync): `main == origin/main == 6a7d869a`, tag `PHASE_3_FEATURE_LANDED` present.
- Criterion **11** (convergence report): this document.
- Criterion **12** (final gate PASS): the reviewer's call.

Both design flags are **correctly-deferred, evidence-labeled non-defects** (fail-closed where it matters; advisory-only where it is by design) and **do not block** the gate. The residual risks in §5 are **named deferrals**, not open defects in the shipped chain.

The Final Gate (`t_b8d1163b`) should independently re-run the E2E chain on its own scratch PG (it must re-verify `enqueue_plan_tasks` executes, not trust this or the re-review), adjudicate all 12 criteria against real evidence, and if it returns PASS, tag `PHASE_3_CLOSED` at the final main commit. **This card does not tag; the gate owns the closed tag.**

---

*Convergence report authored by aco-architect, task `t_557d9d46`, at `main = 6a7d869a` (tag `PHASE_3_FEATURE_LANDED`). Self-re-verification this run: live E2E 10/10 + no-DB 1/9 on scratch `aco_arc_p3_main`, `alembic heads` = f071, frozen-file diff 0 add/0 delete, code spot-checks on `6a7d869a`. Source-of-truth commits: rework `d22c7ec8`, landing merges `ff8a9608`/`2064d8b7`/`9cb3c5f5`/`ee450434`/`6a7d869a`; original review `wt/t_474b92b1`; re-review `wt/t_1cb8757d`; docs `1e29f1e1`/`e700411b`/`39b412af`/`38e4a414`.*
