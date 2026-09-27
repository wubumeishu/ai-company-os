# Foundation Evidence Inventory (Phase 2F Closure)

Task: `t_9fbe227d` — performed on branch `wt/t_9fbe227d` at `main` = `7258e96d`.
Method: all facts below verified with `git ls-tree -r <ref>`, `git ls-files`,
`git for-each-ref --contains`, and `git log` against the local repository
`I:/project/AI Company OS` (remotes: `clawith-upstream` [no-push], `origin`).
Every conclusion is tagged **[FACT]** (directly observed in git/file state),
**[OBS]** (observed pattern/inference limited to what was checked),
**[INFER]** (inference from the observed state), or **[UNKNOWN]** (not verifiable here).

## 1. Baseline

| Item | Value | Status |
|---|---|---|
| `main` | `7258e96d` "docs(2f): land Phase 2F Convergence Report" | **[FACT]** satisfies the task's "commit 7258e96d or later" condition (== 7258e96d) |
| `origin/main` | `7258e96d` | **[FACT]** main == origin/main |
| Inventory branch | `wt/t_9fbe227d` at `7258e96d`, clean worktree | **[FACT]** |

## 2. Migrations (backend/alembic/versions/)

- **[FACT]** 76 tracked migration files on `main` (`git ls-files 'backend/alembic/versions/*' | wc -l` = 76).
- **[FACT]** Phase 2C–2F DDL set, all present on `main`:

| File | Phase | On main |
|---|---|---|
| `v1_11_4_f066_add_project_repo_tables.py` | 2C (project/repo intake) | Present |
| `v1_11_5_f067_intake_rejection_fields.py` | 2C | Present |
| `v1_11_5_f068_analysis_persistence.py` | 2C | Present |
| `v1_11_5_f069_task_graph_provenance.py` | 2D | Present |
| `v1_11_5_f070_analysis_task_dedup.py` | 2D/2E | Present |

- **[OBS]** 2F settlement/execution shipped no new DDL (settlement reuses Phase-2E
  Run/Task/checkpoint tables); consistent with reviewer CP11 (0 product-code
  files in `backend/app` across the 12 Phase-2F commits) as recorded in
  `docs/PHASE_2F_CONVERGENCE_REPORT.md` §R.
- **[UNKNOWN]** `alembic heads` single-head check was **not** executed in this
  inventory (requires a live Postgres). The regression re-run (`eebfd379`,
  §26-P) is recorded as 13/13 PASS including migration readiness on scratch DBs.

## 3. Documentation (docs/)

- **[FACT]** 36 tracked `docs/*.md` on `main`. Phase-closure-critical subset:

| Document | Purpose | Status |
|---|---|---|
| `docs/PHASE_2F_CONVERGENCE_REPORT.md` | 2F §A–§T convergence report; lists all 15 card commits (15-item list in §R); landed by `7258e96d` | Present on main |
| `docs/PHASE_2E_CONVERGENCE_REPORT.md` | 2E §A–§U convergence report; landed by `3175c798` | Present on main |
| `docs/PHASE_2E_AGENT_ASSIGNMENT_SPEC_V1.md` | 2E execution-semantics spec (incl. RETRY_CAP_EXCEEDED naming, `d112c71d`) | Present on main |
| `docs/PHASE_2E_EXECUTION_CHAIN_VERIFICATION_T84836B4B.md` | 2E reviewer verification report (APPROVE) + documents `verify_2e_execution_chain.py` | Present on main |
| `docs/PHASE_2D_CONVERGENCE_REPORT.md`, `docs/PHASE_2C_CONVERGENCE.md`, `docs/PHASE_2B4_GIT_ACQUISITION_CONVERGENCE.md`, `docs/PHASE2B3_MATERIALIZATION_CONVERGENCE_T06748FD76.md` | Earlier-phase convergence | Present on main |
| `docs/PHASE_2F_EXECUTION_PROBES_EVIDENCE.json` (on main?) | — | **NOT on main** — see §6 |

## 4. Tests (backend/tests/)

- **[FACT]** 213 tracked `backend/tests/*.py` on `main`.
- **[FACT]** Phase-chain tests present on `main`: `test_task_execution_service.py`,
  `test_intake_e2e_acceptance.py`, `test_materialization_e2e_acceptance.py`,
  `test_project_analysis_e2e_acceptance.py`, `test_git_acquisition_e2e_acceptance.py`,
  `test_agent_runtime_*` family (worker, tool execution, settlement contracts).
- **[OBS]** Phase 2F added no new files under `backend/tests` (CP11: 0 files in
  `backend/tests` in the 2F commits) — 2F behavior is pinned by the 2E suite
  re-run (37/37 in §26-P regression) plus the 2F drivers, not new 2F unit tests.

## 5. Verification scripts

### 5.1 On main — Present and documented

| Script | Location | Documented in | Executable-as-documented |
|---|---|---|---|
| `verify_2e_execution_chain.py` | repo root (tracked, mode 100644) | `docs/PHASE_2E_EXECUTION_CHAIN_VERIFICATION_T84836B4B.md` §1–§2 (`python verify_2e_execution_chain.py`) | **[OBS]** no exec bit (Windows repo, all .py = 100644); invoked via interpreter per its doc report — matches documented usage |
| `backend/scripts/backfill_chat_message_tenant_id.py` | `backend/scripts/` | `backend/scripts/AGENTS.md` (script conventions) | **[FACT]** tracked; dry-run/`--apply` convention documented |

### 5.2 Phase 2F drivers — **NOT on main** (gaps G1–G3)

**[FACT]** `git ls-files | grep -c "verify_2f_"` on main = 0; `_EVIDENCE.json`
count on main = 0. All 10 drivers live only on their card commits/branches:

| # | Driver | Card | Commit | Branch (durable ref) |
|---|---|---|---|---|
| 1 | `scripts/verify_2f_execution_probes.py` | t_e3a745b1 | `1fa1c07b` | local `wt/t_e3a745b1` only |
| 2 | `backend/scripts/verify_2f_real_llm_run.py` | t_45477a14 | `0b9b2fdb` | local `wt/t_45477a14` only |
| 3 | `backend/scripts/verify_2f_result_settlement.py` (+ `_preflight_2f_settle.py`) | t_77399eca | `c61774a8` | local `wt/t_77399eca` + remote `origin/wt/t_77399eca` |
| 4 | `backend/scripts/verify_2f_concurrency_dependency.py` | t_edbd5f78 | `33de066f` | local `wt/t_edbd5f78` only |
| 5 | `backend/scripts/verify_2f_dedup_retry.py` (+ `_preflight_2f_dedup.py`) | t_f28b2fa3 | `41b60e92` | local + `origin/ai-company-os/…f28b2fa3…` |
| 6 | `backend/scripts/verify_2f_timeout.py` | t_255c923f | `2a65509b`+`4dc53e3a` | local + `origin/ai-company-os/…255c923f…` |
| 7 | `backend/scripts/verify_2f_cancellation.py` | t_6efa793e | `e34e0f99` | local + `origin/ai-company-os/…6efa793e…` |
| 8 | `backend/scripts/verify_2f_tenant_isolation.py` | t_6e34f801 | `014f677b` | local + `origin/ai-company-os/…6e34f801…` |
| 9 | `backend/scripts/verify_2f_full_project_e2e.py` | t_6f849cce | `720e714b` | local + `origin/ai-company-os/…6f849cce…` |
| 10 | `backend/scripts/verify_2f_concurrency_ladder.py` | t_99c4a1bb | `072592a0` | local + `origin/ai-company-os/…99c4a1bb…` |
| 11 | `backend/scripts/regression_2f_all.py` | t_933d0fa1 | `eebfd379` | local + `origin/ai-company-os/…933d0fa1…` |

- **[FACT]** `git worktree list` shows dedicated worktrees pinned at exactly
  these commits (e.g. `.worktrees/t_45477a14` at `0b9b2fdb`), so every driver is
  re-runnable today on-disk at its recorded commit.
- **[OBS]** Driver #1 intentionally sits at repo-root `scripts/` (pre-Phase-2F
  layout) — the archive task t_3aa8fbb7 pre-seeded fact says the canonical
  9-file set lives in `.worktrees/t_d066fdd5/backend/scripts/` (verified: 9/9
  `verify_2f_*.py` present there).
- **[INFER]** "Present" for each driver = present **at its card commit**, not
  on main; the main-branch gap is the known closure item G1.

## 6. Evidence JSONs (machine-readable verdict artifacts)

| Artifact | Committed at | On main? |
|---|---|---|
| `docs/PHASE_2F_EXECUTION_PROBES_EVIDENCE.json` | `1fa1c07b`, re-landed in `848dfd2e` | **No** |
| `PHASE_2F_REAL_LLM_RUN_EVIDENCE.json` | `0b9b2fdb`, re-landed in `848dfd2e` | **No** |
| `PHASE_2F_RESULT_SETTLEMENT_EVIDENCE.json` | `c61774a8` (+ `848dfd2e`) | **No** |
| `PHASE_2F_CONCURRENCY_DEP_EVIDENCE.json` | `33de066f` (+ `848dfd2e`) | **No** |
| `PHASE_2F_DEDUP_RETRY_EVIDENCE.json` | `41b60e92` (+ `848dfd2e`) | **No** |
| `PHASE_2F_TIMEOUT_EVIDENCE.json` | `4dc53e3a` (+ `848dfd2e`) | **No** |
| `PHASE_2F_CANCELLATION_EVIDENCE.json` | `e34e0f99` (+ `848dfd2e`) | **No** |
| `PHASE_2F_TENANT_ISOLATION_EVIDENCE.json` | `014f677b` (+ `848dfd2e`) | **No** |
| `PHASE_2F_FULL_PROJECT_E2E_EVIDENCE.json` | `720e714b` (+ `848dfd2e`) | **No** |
| `PHASE_2F_CONCURRENCY_LADDER_EVIDENCE.json` (+STAGE5/STAGE10) | `072592a0` | **No** |
| `PHASE_2F_REGRESSION_EVIDENCE.json` | `eebfd379` | **No** |

- **[FACT]** `git show --stat 848dfd2e`: the reviewer commit itself landed 9
  evidence JSONs under `backend/scripts/` + `docs/` on its branch
  (`848dfd2e` is on `ai-company-os/t_d066fdd5-…`, local + origin; **not on main**).
- **[OBS]** This is the documented "docs-only main; worker evidence stays on
  card branches" landing policy (`PHASE_2F_CONVERGENCE_REPORT.md` line 3) —
  a deliberate decision, not an accident.

## 7. Review reports

| Report | Commit | On main? | Task link |
|---|---|---|---|
| 2F Convergence Report (embeds §23 reviewer verdict APPROVE 12/12 + §26-P regression PASS) | `7258e96d` | **Yes** | t_4910b73e |
| 2F Final-Gate review report `docs/PHASE_2F_FINAL_GATE_REVIEW_T_D066FDD5.md` | `848dfd2e` | **No** (branch-only) | t_d066fdd5 |
| 2F per-card reports (`…REAL_LLM_RUN_REPORT_T45477A14.md`, `…SETTLEMENT…T77399ECA.md`, `…DEPENDENCY…TEDBDF78.md`, `…DEDUP_RETRY_REPORT_TF28B2FA3.md`, `…CANCELLATION_REPORT_T_6EFA793E.md`, `…TENANT_ISOLATION_REPORT_T_t_6e34f801.md`, `…REGRESSION_REPORT_T_933D0FA1.md`) | card commits | **No** (branch-only) | respective cards |
| 2E chain-verification report (APPROVE) | `a9d7b558` | Yes | t_84836b4b |
| 2E Convergence Report | `3175c798` | Yes | t_b0bb2f7c |
| 2D Convergence + Final-Gate re-review refs | `7497bf7a` / t_74eb3596 | Yes (convergence); re-review report on branch | t_55d5bee6 |

- **[OBS]** Every review report is linked to a specific kanban task id
  (filename suffix `_T<taskid>` or commit message ` (t_…)`) — requirement 4 met
  for the on-main subset; the branch-only 2F reports carry the same convention.

## 8. Durability of the 15 Phase-2F card commits

- **[FACT]** `git for-each-ref --contains` per commit: 14/15 held by ≥1 local
  branch, 9/15 held by ≥1 `origin/` ref. `eb8cfc0e` (2F preflight checklist) is
  held by a remote ref only (`origin/wt/t_31ac823d`).
- **[FACT]** `clawith-upstream` remote is read-only (`no-push`); `origin` holds
  the pushed refs.
- **[INFER]** No card commit is currently orphaned (all 15 reachable from
  some ref), but 6/15 survive solely on local branches — if the local repo is
  lost, drivers + evidence for `1fa1c07b`, `0b9b2fdb`, `33de066f`,
  `b7059cdc`, `720e714b` would be unrecoverable from remote. Archiving the
  assets onto main (gap G1, sibling task t_3aa8fbb7) removes this fragility.

## 9. Gaps that would prevent future re-verification

| Gap | What | Blocked? | Mitigating action |
|---|---|---|---|
| **G1** | 10 `verify_2f_*` drivers + `regression_2f_all.py` not on main (§5.2) | Re-verification from a fresh clone of `main` alone is impossible | Sibling task **t_3aa8fbb7** (running) archives the 9-driver set to `docs/evidence/phase2f/` + README on main |
| **G2** | 11 machine-readable evidence JSONs not on main (§6) | Verdicts reproducible only via 96+ local worktrees/branches | To be folded into t_3aa8fbb7 scope or a follow-up (the `848dfd2e` tree is the most complete single source: 9 JSONs + final-gate report) |
| **G3** | `docs/PHASE_2F_FINAL_GATE_REVIEW_T_D066FDD5.md` (reviewer's own report) not on main | Main carries the verdict *by reference* inside the convergence report only | Land via t_3aa8fbb7 or a new card |
| **G4** | 6/15 card commits lack any `origin/` ref (§8) | Loss of local repo = loss of those drivers/evidence | Push card branches to `origin` or archive-on-main (G1/G2/G3) |
| **G5** | `verify_2e_execution_chain.py` sits at repo root, outside `backend/scripts/` convention | Minor: undocumented-by-location drift | Note in t_3aa8fbb7 README; no move (no business-logic change allowed in closure) |
| **G6** | `docs/evidence/` did not exist on main before this inventory | n/a | Created by this commit; t_3aa8fbb7 owns `docs/evidence/phase2f/` |
| **G7** | Alembic single-head check not re-run in this inventory (§2 UNKNOWN) | Low: recorded PASS in §26-P regression at `eebfd379` | Re-run `uv run alembic heads` in t_b68a99fb review |

## 10. Verdict

- Migrations: **Present** (76 on main; f066–f070 all on main).
- Docs (phase-critical): **Present** (convergence + spec + 2E/2D reports on main); 2F branch-only reports **Incomplete on main** (G3).
- Tests: **Present** (213 files; 2F pinned by 2E suite + drivers).
- Verification scripts: 2E harness **Present & documented**; 2F drivers **Missing on main / Present at card commits** (G1, G4).
- Review reports: on-main subset **Present & task-linked**; 2F final-gate report **Missing on main** (G3).
- Re-verifiability: **blocked for a main-only fresh clone** until G1+G2+G3 are
  closed by t_3aa8fbb7; re-verifiability today via local worktrees: yes.

*Inventory produced by aco-builder, task t_9fbe227d, at `main` = `7258e96d`.*
