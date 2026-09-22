# Phase 2B-3 — Project Materialization: Root Convergence Report (t_6748fd76)

Convergence / final report for the **root** card `t_6748fd76`. This is the
orchestrator-level reconciliation of the whole Phase 2B-3 lane: it records what
was merged into `main`, how the verified state was re-proven on `main` itself,
and the final verdict. Detailed per-design facts (A–N in `§22` of the card)
already live in the card-specific docs; this report is the top-level authority
that those pieces converged correctly.

- Root card: `t_6748fd76` (aco-orchestrator)
- Converged lanes (all completed, all feeding this root):
  - `t_df317a55` — preflight audit → `docs/MATERIALIZATION_WORKSPACE_ISOLATION_AUDIT_V1.md` @ `cea71516`
  - `t_c672b2c2` — secure spec (M1–M9) → `docs/MATERIALIZATION_SECURE_SPEC_V1.md` @ `10d16f4f`
  - `t_025cda02` — implementation (service + API + schemas + 46 service tests + 11 E2E + Windows storage fix) @ `7225ca83`
  - `t_be706e41` — independent review → **APPROVE** @ `7225ca83` (review-only, no code)
  - `t_6990cc6d` — edge-case verification (29 tests) @ `a72b6bcb` (superset of `t_025cda02`: base `7225ca83` + `test_materialization_edge_cases.py`)
- `main` HEAD after this convergence: `fd8c7dd9`
- Verification host: Windows; Python 3.11 venv (`backend/.venv`); Postgres 16.15 on `127.0.0.1:5432`
- Scratch E2E database: `clawith_t6748fd76_e2e` (full `Base.metadata` schema + additive patches)

## 1. Convergence action (what this root did)

The five lane tips sat on four branches, none merged into `main`. All three
converging branches share `main` (`5830624c`) as their merge-base and add
**disjoint file sets** (docs-only vs. impl+tests), so every merge was clean:

```text
5830624c (pre-merge main)
   ├── wt/t_df317a55  cea71516   +docs/MATERIALIZATION_WORKSPACE_ISOLATION_AUDIT_V1.md
   ├── wt/t_c672b2c2  10d16f4f   +docs/MATERIALIZATION_SECURE_SPEC_V1.md
   └── wt/t_6990cc6d  a72b6bcb   +impl (service/api/schema/local.py fix)
                                 +46 service tests +11 E2E +edge-case suite +acceptance report
      (t_025cda02 @ 7225ca83 is a direct ancestor of t_6990cc6d, so
       merging t_6990cc6d once covers both; t_be706e41 is review-only)
```

Merge order into `main`:

1. `82926558` — `Merge wt/t_df317a55` (preflight audit doc)
2. `25fcfd47` — `Merge wt/t_c672b2c2` (secure spec doc)
3. `fd8c7dd9` — `Merge wt/t_6990cc6d` (impl + edge-case suite, supersedes `t_025cda02`)

No conflicts. No files were hand-edited during the merge — the merged tree is
exactly the union of the verified lane outputs.

## 2. Re-verification on merged `main` (re-proven here, not assumed from handoffs)

Task `§0` forbids inferring current behavior from reports. Every claim below
was re-executed on the post-merge `main` tree at `fd8c7dd9`.

| # | Check (real command) | Result |
|---|---|---|
| 1 | `pytest tests/test_project_materialization_service.py tests/test_materialization_edge_cases.py` (DB-free: 46 service + 29 edge) | **75 passed** |
| 2 | `pytest tests/test_materialization_e2e_acceptance.py` (real Postgres scratch `clawith_t6748fd76_e2e`, real FastAPI over ASGI) | **11 passed** |
| 3 | `pytest tests/test_intake_e2e_acceptance.py` (Phase 2B-2 regression, real Postgres scratch) | **22 passed** |
| 4 | `pytest tests/test_project_intake_service.py test_workspace_reconciliation.py test_files_api_storage.py test_project_repository_migration.py` (DB-free regression surface) | **83 passed, 1 failed (pre-existing)** |
| 5 | `pyright` on `project_materialization_service.py`, `api/projects.py`, `schemas/project_intake.py`, both materialization test files | **0 errors** |
| 6 | `ruff check` on the same materialization code | findings limited to repo-baseline families `B008` (Depends defaults) + `BLE001` (documented narrow `except Exception`); **no new rule families** |

### 2.1 The single regression failure is pre-existing, not introduced by this merge

`tests/test_workspace_reconciliation.py::test_directory_move_candidate_covers_every_source_file`
fails on merged `main` (POSIX-separator assertion, Windows host). Two independent
proofs that this merge did not cause it:

- The materialization merge touched **zero** files under `backend/app/services/workspace/`
  or the reconciliation test — `git diff --name-only 5830624c..HEAD` contains none
  of them.
- The same test was run on a scratch detached worktree at the **pre-merge** base
  `5830624c` and fails identically there.

So this is the documented Windows/POSIX-separator pre-existing failure; it was
carried in from the Phase 2B-2 baseline, not by 2B-3. It is recorded, not
"fixed" by editing an old test (per `§17`).

### 2.2 E2E schema bootstrap note (this host)

The materialization E2E suite seeds via the real `app.database` engine and does
**not** build schema inline. A freshly-created scratch DB has no tables, so the
first E2E run failed with `UndefinedTableError: 关系 "tenants" 不存在`. This is
an environment/precondition matter, not a product defect: the repo's own
`app/scripts/bootstrap_db.py` omits 14 model modules (incl. `group`, `identity`,
`workspace`), so its `create_all` hits an unresolved FK
(`chat_sessions.group_id → groups`). The full `Base.metadata` was therefore
bootstrapped by importing **every** `app.models.*` module then `create_all`
+ the same additive `PATCHES`, matching the acceptance approach documented in
the card E2E header ("schema from `Base.metadata`, alembic NOT used on fresh DB
because an unrelated schedule migration crashes fresh DBs"). Redis, the one
dependency the real lock layer needs that this host does not run, is faked via
the established in-memory `MockRedis` convention (spec `M3` documented
limitation).

## 3. Design mapping (Phase 2A → Materialization, as realized)

- `Intake ≠ Materialization ≠ Execution` is held: the materialization service
  reads only a project whose status gate passes (`INITIALIZED` only — all 9
  non-INITIALIZED statuses, incl. `RECEIVED`/`REJECTED`, are pre-I/O
  `SOURCE_NOT_READY`/409) and only Intake-verified sources; it never creates a
  Task, Agent Run, or auto-starts any Squad (card `§13`/`§14` hard rules).
- Workspace model: **Model B** (agent keeps its own workspace; project material
  is injected into the target agent's storage subtree). No per-Project permanent
  physical workspace; no Model C implemented (card `§2`).
- Security layer reuses the `intake_security` single guard (`check_zip_slip`,
  host-path checks) rather than a second authority.
- No unified Artifact/Evidence system was introduced (card `§10`/`§22.P`).

## 4. Source-type behavior (V1) — as realized

- `manual` → explicit `SKIPPED_NO_MATERIAL`, zero content invented (spec `M4`).
- `local_folder` / `document` → safe copy into `{agent_id}/projects/{project_id}/{material_name}/`,
  original filename preserved, content unmodified.
- `zip` → pure in-memory extraction with **double** Zip-Slip guard
  (`check_zip_slip` + per-member re-normalization + reserved-name set);
  `../../escape.txt` → 0-write `SECURITY_REJECTED`.
- `github` / `gitlab` / `local_git` → fail-closed `SOURCE_NOT_READY`
  (no Git acquisition in V1; never faked as success, card `§4`/`§22.P`).

## 5. Final verdict (card `§21` / `§22.R`)

- Project: only legal (INITIALIZED) projects materialize. ✅
- Source: only Intake-verified sources enter materialization; unready git sources fail-closed. ✅
- Workspace: material lands in the target agent's real storage subtree; isolation holds. ✅
- Security: path traversal / zip-slip / credential / boundary guards with real tests. ✅
- Isolation: tenant / agent / workspace isolation + 4-gate tenant chain (404 no-disclosure cross-tenant). ✅
- Repeatability: probe-then-write idempotency — converge / content-conflict / overwrite table. ✅
- Failure: partial failure is explicit `PARTIAL`/`FAILED` (409), never disguised as success; staging cleaned on every exit. ✅
- Provenance: traceable via `WorkspaceFileRevision` rows (`group_key=materialize:{project}:{repo}:{agent}`) + per-call `AuditLog`; declared V1 limitations, no fake Evidence. ✅
- Execution boundary: no auto Task/Run/code/Squad start. ✅
- Regression: Phase 2B-1 + 2B-2 remain green on merged `main` (83/84 DB-free surface with the 1 documented pre-existing Windows failure proven pre-merge; 22/22 intake E2E). ✅
- Review: independent reviewer `t_be706e41` **APPROVE** on the realized code. ✅
- Git: committed and pushed to origin (below). ✅

**Final verdict: PASS.**

The Phase 2B-3 Project Materialization closed loop — a formally accepted
Project's verified sources can be placed into the named agent's real workspace
in a safe, repeatable, traceable way without silently starting execution —
is merged, independently reviewed, and re-verified on `main`.

## 6. Git evidence (this root)

- `main` advanced `5830624c → fd8c7dd9` (3 merge commits: `82926558`, `25fcfd47`, `fd8c7dd9`).
- Working tree clean apart from the untracked `.worktrees/` git-dir metadata
  (identical to the pre-merge baseline state).
- Pushed to `origin/main` (`https://github.com/wubumeishu/ai-company-os.git`)
  — verify `git rev-parse origin/main` matches `main` locally after push.
- No force-push / `reset --hard` / `git clean` / `branch -D` used.

### `§22.P` Scope check (explicitly NOT implemented in Phase 2B-3)

Git acquisition (github/gitlab/local_git) · Project Analysis · Task
generation · Squad · Agent assignment · Agent Run · Execution ·
Artifact/Evidence unified system · Review/Rework engine · Scheduler ·
Project Single Writer. Workspace Lock was used only to prevent runtime write
conflicts; it is **not** claimed as Project Single Writer.

### `§22.Q` UNKNOWN / DESIGN GAP / declared LIMITATIONS

- No `UNKNOWN` carried forward — the 9 audit gaps `M1`–`M9` were all
  adjudicated in the secure spec (`t_c672b2c2`) and closed.
- Declared V1 LIMITATIONS (documented, not silent):
  1. S3 cross-process mutation lock is not available (conditional writes OK) — spec `M3`.
  2. Redis-free host: E2E lock layer uses the documented in-memory `MockRedis` fake;
     real-Redis cross-process behavior is a declared limitation.
  3. `local.py` Windows `fchmod` guard leaves non-Windows new-file mode `0o666` when
     `fchmod` is absent but the platform is not POSIX-strict (pre-existing baseline family).
  4. 60 s directory-lock TTL: a large call's expiry window is bounded by the
     `require_absent` backstop (spec `M8`).
  5. Pre-existing Windows/POSIX test failure
     (`test_workspace_reconciliation.py::test_directory_move_candidate...`)
     — recorded, out of 2B-3 scope, proven pre-merge.
