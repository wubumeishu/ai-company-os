# Phase 2C — Project Analysis & Minimal Analysis Persistence: Convergence Report

Phase: 2C (Project Analysis) — 收口基线 `fc233fc1` (main == origin/main)
Board: ai-company-os · 全卡链 done · 最终判定 **PASS**
Source docs (already on main): `docs/PHASE_2C_RECON_BASELINE.md`,
`PHASE_2C_TECHSTACK_ARCHITECTURE.md`, `PHASE_2C_RUNTIME_TESTING_WORKFLOW.md`,
`PHASE_2C_RISKS_OPEN_QUESTIONS.md`, `PHASE_2C_PROJECT_ANALYSIS.md`,
`PHASE_2C_ANALYSIS_BUILD_T37E2EB05.md`.
Full closeout evidence: `PHASE_2C_LAND_CLOSEOUT_T2A41D49C.md` (worktree of
t_2a41d49c; A–S sections; not re-archived here — see §Gap).

## 1. Card Chain (who did what, and the verdicts)

| Card | Role | Deliverable | Verdict |
|---|---|---|---|
| t_4e4599d8 | aco-architect | Recon & capability audit → `PHASE_2C_RECON_BASELINE.md` (babadf25) | DONE |
| t_d9a3eafd | aco-architect | Tech stack & architecture → `PHASE_2C_TECHSTACK_ARCHITECTURE.md` (5aaa9c1f) | DONE |
| t_93876cee | aco-architect | Runtime, testing & workflow → `PHASE_2C_RUNTIME_TESTING_WORKFLOW.md` (1211b7c1) | DONE |
| t_88a15667 | aco-architect | Risks, secrets, open questions → `PHASE_2C_RISKS_OPEN_QUESTIONS.md` (20e71f86) | DONE |
| t_3d5641e0 | aco-architect | Synthesis: minimal model (versioning + Git-revision binding, transient-Finding vs durable-Knowledge boundary) → `PHASE_2C_PROJECT_ANALYSIS.md` (0959266d) | DONE |
| t_d58eae44 | aco-reviewer | Validation & acceptance gate on the synthesized artifact (~20 cited file:line anchors spot-checked against live source) | **PASS** |
| t_37e2eb05 | aco-builder | Build: DDL-only migration `f068_analysis_persistence` + models/DAO/schemas/service/API + E2E + Agent Note, one branch | DONE (d10af225 → 7ccabae3) |
| t_75dd99db | aco-reviewer | Independent 10-checkpoint review of the build | 10/10 PASS, **REWORK** (1 High defect) |
| t_bca54821 | aco-builder | Rework: fix f068 `downgrade()` High defect (Option A: drop redundant `ix_analysis_runs_project_id` from model + migration, existence-guarded) | DONE @ 7ccabae3 |
| t_e2616e2c | aco-reviewer | Re-review of the rework, own worktree + 2 scratch PG DBs | **APPROVE** |
| t_2a41d49c | aco-builder | LAND: merge all 6 2C branches `--no-ff` (5 doc first, build last), push, verify, cleanup, A–S closeout | **PASS** |
| t_2d716c43 | aco-orchestrator | Root: Phase 2C — Project Analysis (independent re-verification of the land terminal on the merged tree) | **PASS** |

## 2. Defect Found → Fixed → Re-verified

- **Defect (High, t_75dd99db):** `f068_analysis_persistence.downgrade()`
  hard-failed on every fresh DB — `UndefinedObjectError: index
  ix_analysis_runs_project_id does not exist` (model↔migration index drift:
  the model never created that index, the migration's downgrade expected it).
- **Fix (t_bca54821, Option A):** dropped the redundant index from BOTH model
  and migration; `revision_sha` + `tenant_id` kept in lockstep; drops guarded
  via `_existing_indexes`. Confined to 3 files; no service/DAO/API semantic
  change.
- **Re-verification (t_e2616e2c):** on BOTH provisioning paths
  (create_all-provisioned fresh DB and pure-alembic fresh DB), the loop
  `upgrade head → downgrade f067 → upgrade head` exited 0 on every step, no
  UndefinedObjectError; index sets agree; single head holds. → APPROVE.

## 3. What Landed on main (the one business change)

- `analysis_runs` (append-only, `UNIQUE(project_id, revision_sha)`) —
  versioned analysis runs; new sha → new row, history never clobbered.
- `analysis_findings` — TRANSIENT (one revision / one time / one agent;
  run-CASCADE).
- `project_knowledge` — DURABLE/CONFIRMED; a finding is PROMOTED only after
  confirmation, carrying `source_analysis_run_id`; never invalidated by a later
  analysis at a different commit. No knowledge graph modeled;
  PENDING_CONFIRMATION is inert by design (no confirmation UI yet, OQ-6).
- Revision carrier = `analysis_runs.revision_sha` (typed commit-hash column,
  OQ-5), sourced from `repositories.locator.resolved_rev` written back by
  GitAcquisitionService.
- Execution path is stage-11 static-only: analysis reads/records; it does NOT
  execute project code (E2E asserts zero downstream execution).
- Tenant scoping via `TenantScopedBaseDAO` + `verify_tenant_scope`.

### 3.1 Scope Definition A/B (formal, 2026-09-25 Final Gate)

The 2C constraint and the 2C implementation are NOT contradictory; they govern
two different subjects. This definition is formal so future audits do not
re-derive it:

- **A. Subject of the phase = the analyzed target Project. Strictly read-only,
  target code NEVER executed.** The analysis execution path is static-only
  (Stage 11): it reads DB rows + the bounded `locator` JSON written back by
  acquisition, and stops there. Source of record
  (`backend/app/services/analysis_service.py` module docstring, on main):
  "Stage 11 (hard): this path is STATIC-ONLY. It reads DB rows and the bounded
  locator JSON written by acquisition; it NEVER executes the target project's
  code (no subprocess, no interpreter, no build, no pip/npm install, no
  service start). Dynamic analysis is deferred to a future
  Execution/Analysis sandbox." The E2E additionally asserts zero downstream
  execution (`sessions`/`tasks`/`schedules` all empty at
  `tests/test_project_analysis_e2e_acceptance.py`). Verified 2026-09-25 on
  main @ `522df379`.
- **B. Subject = AI Company OS itself. The phase MAY add its own subsystem
  code.** Everything 2C landed (`analysis_runs` / `analysis_findings` /
  `project_knowledge` models + DAO + schemas + service + API routes + the
  DDL-only migration `f068_analysis_persistence` + the E2E suite) is Company
  OS's own Analysis subsystem implementing the capability, NOT target-project
  code, and NOT executed target code. It is new business code OF the platform.

**Canonical wording:** "Phase 2C analysis is read-only and non-executing with
respect to the ANALYZED target project (Stage 11 static-only, target code is
never run); the same phase is permitted to add the Company OS's own Analysis
subsystem code (persistence, DAO, service, API, migration, tests) to make the
capability exist."

## 4. Evidence Battery (from t_2a41d49c closeout, run on merged main tree)

> **2026-09-25 Final Gate re-verification:** every row below was RE-RUN on the
> merged main tree @ `522df379` (checkouts on main, not the stale worktree), on
> a FRESH scratch DB `clawith_2c_finalgate` (Postgres 5432, PostgreSQL 16).
> Results are identical to the original report; numbers below carry the
> re-verification. The original t_2a41d49c run used scratch DB
> `clawith_t2a41d49c_land` — both remain in place, harmless.

| Gate | Result |
|---|---|
| 2B DB-free regression (git-acq service+security, intake, materialization) | **263 passed**, 3 deselected |
| 2B-1 migration regression | **15 passed** |
| Real-DB transport E2E (fresh f068 DB; re-verified on `clawith_2c_finalgate`) | **39 passed** |
| 2C analysis E2E (real local git repo; Project → Source → Revision → Analysis → Findings → Stored → Knowledge; re-verified on `clawith_2c_finalgate`) | **6/6 passed** |
| Alembic single head + fresh-DB chain `001 → f068` | exactly 1 head; EXIT=0 |
| ruff on 2C-touched files | 0 new errors (`api/projects.py` B008 x24 = pre-existing FastAPI Depends() baseline family, 14→24, same rule) |
| pyright on 5 changed app files | 0 errors, 0 warnings |
| `git cherry main <each 2C branch>` | empty → fully merged |
| `main == origin/main` | `fc233fc1c6242f59123d59d92431b250bbdb8f25` (criterion #16) |
| Static graph check (root t_2d716c43 re-verify) | all 73 migration files → single head f068, down_rev = f067 |

**Environment limitation (documented, NOT a regression):**
`test_remote_url_gate_accepts_public_https_url` and the github/gitlab remote
E2E were deselected — this host's DNS now resolves `github.com` to
`198.18.0.22` (private/reserved → fail-closed `ACQ_SECURITY_REJECTED`) and
gitlab.com is blackholed. Proven not-2C-caused: `git diff a9e83a8a..main`
over `git_acquisition_service.py` / `intake_security.py` /
`test_git_acquisition_service.py` is empty (byte-identical to base).
Re-run on a clean-egress host:
`pytest tests/test_git_acquisition_service.py -k "remote_url_gate or e2e_github or e2e_gitlab"`.

## 5. Known Boundaries & TODOs (classified at 2026-09-25 Final Gate)

**Blocker:** NONE. Nothing below prevents 2C from serving as the Phase 2D
(Task Decomposition) baseline.

**Non-blocker TODO:**
- `.worktrees/` stale worktrees from earlier phases + `+`-only stale branches
  + untracked `.smoke-backup/` — verified content-free via `git cherry`
  (zero `^-` commits on any unmerged branch); hygiene only, deliberately
  untouched per owner instruction at this gate.
- Scratch PG DBs `clawith_t2a41d49c_land` + `clawith_2c_finalgate` left in
  place (harmless, `clawith` role; cleanup is future hygiene, not a gate).
- Remote-egress E2E re-run (`remote_url_gate` / `e2e_github` / `e2e_gitlab`)
  deferred to a clean-egress host (§4 environment limitation).
- DAO read paths default `limit=100`; >100-row projects need pagination
  (no current consumer).

**Future Enhancement (explicitly out of 2C scope):**
- Confirmation UI for `project_knowledge` promotion — PENDING_CONFIRMATION
  is inert by design (OQ-6); promote was exercised as a direct API call.
- Dynamic/execution analysis (sandbox) — Stage 11 deferred it; 2C is
  static-only by design.
- Tree-wide legacy static debt (~3107 ruff / ~713 pyright) is pre-existing;
  every 2C-touched file is 0-error (re-verified this gate).

- Next phase starts at Task Decomposition / Squad / Agent Assignment — 2C
  explicitly STOPs here.

## 6. Closeout Hygiene (repo state at report time)

- 6 2C worktrees removed by the land card; 5 doc `wt/*` branches + build branch
  deleted via safe `git branch --delete`.
- Residual board hygiene OUTSIDE the card's scope (post-report finding):
  stale worktrees under `.worktrees/` from earlier phases, untracked
  `.smoke-backup/`, and `+`-only stale branches remain in the main repo
  (verified content-free via `git cherry`); plus the `t_bca54821` rework-lane
  worktree/branch (fully merged, left by its lane).
- The land closeout doc `PHASE_2C_LAND_CLOSEOUT_T2A41D49C.md` was archived
  into main `docs/` at `522df379` (2026-09-25), alongside this convergence
  report; the original worktree copy is historical.
- Scratch PG DB `clawith_t2a41d49c_land` left in place (consistent with prior
  scratch DBs; harmless, `clawith` role).
