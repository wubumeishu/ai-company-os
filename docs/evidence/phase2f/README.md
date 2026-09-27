# Phase 2F Verification Archive

Durable archive of the Phase 2F (Agent Run Execution & Result Settlement)
verification drivers, their machine-readable evidence, and the two closure
reports. Phase 2F closed on `main` (root task `t_4910b73e`); this directory
is the fixed, re-runnable record that anyone (including the independent
closure review) can audit without reconstructing 15 card worktrees.

Status of the archive: **verbatim snapshot** — every driver byte is
sha256-verified identical to its card's landed (tip) commit; no driver,
report, or JSON has been edited for archival. This is a docs-only
relocation: it changes no business code, and nothing here is imported by
`app/`, `tests/`, or CI.

## Layout

```text
docs/evidence/phase2f/
  drivers/    10 re-runnable verification drivers + 1 regression orchestrator
  evidence/   13 machine-readable evidence JSONs (per-driver + ladder + regression)
  reports/    2 human-readable closure reports (regression + execution probes)
  README.md   this file
```

## Provenance model (why these bytes are trustworthy)

Each driver was authored on its Phase-2F card worktree and committed at a
known card-tip commit. The archive copies are verified byte-identical to
those commits (`git show <tip>:<path>`, sha256-compare):

| Driver | Card | Landed tip commit |
|---|---|---|
| verify_2f_execution_probes.py | t_e3a745b1 | `1fa1c07b` |
| verify_2f_real_llm_run.py | t_45477a14 | `0b9b2fdb` |
| verify_2f_result_settlement.py | t_77399eca | `c61774a8` |
| verify_2f_concurrency_dependency.py | t_edbd5f78 | `33de066f` |
| verify_2f_dedup_retry.py | t_f28b2fa3 | `41b60e92` |
| verify_2f_timeout.py | t_255c923f | `4dc53e3a` (final; supersedes `2a65509b`) |
| verify_2f_cancellation.py | t_6efa793e | `e34e0f99` |
| verify_2f_tenant_isolation.py | t_6e34f801 | `014f677b` |
| verify_2f_full_project_e2e.py | t_6f849cce | `720e714b` |
| verify_2f_concurrency_ladder.py | t_99c4a1bb | `072592a0` |
| regression_2f_all.py (+ PHASE_2F_REGRESSION_EVIDENCE.json) | t_933d0fa1 | `eebfd379` |

The per-driver evidence JSONs in `evidence/` are the regression re-run
outputs of `t_933d0fa1` (the §26-P card that re-ran all 9 drivers on fresh
scratch DBs and recorded PASS for every section); the ladder stage JSONs
(`PHASE_2F_CONCURRENCY_LADDER_*.json`) are the committed stage evidence of
`072592a0` (re-read by the orchestrator, not re-burned). The probes
evidence JSON and its report are the committed artifacts of `1fa1c07b`.

Full sha256 manifest: see `MANIFEST.sha256` alongside this README
(generate/verify with `sha256sum -c MANIFEST.sha256` from this directory).

## Execution model (read before re-running anything)

The drivers are **location-sensitive**: they are written to run as
`backend/scripts/<driver>.py` inside a checkout that has the backend venv
(`uv sync`), and they resolve `app.*` through that venv. Each driver also
isolates itself from the live pool by pointing every scratch coordinate at
a throwaway Postgres DB (`clawith_2f_<name>_<hex>`) + scratch storage dir,
and writes its evidence JSON **next to the driver file** (a
`backend/scripts/`-relative `__file__` join).

So to re-run an archived driver, stage it back to its native path in a
fresh checkout:

```bash
# 1. checkout with backend deps installed (from the repo root)
cd backend && uv sync --extra dev && cd ..

# 2. stage the driver (and the orchestrator, if you run it) to its native path
cp docs/evidence/phase2f/drivers/verify_2f_cancellation.py backend/scripts/

# 3. run from backend/ (the command every driver documents in its header)
cd backend && uv run --no-sync python scripts/verify_2f_cancellation.py
```

Prerequisites common to all drivers:

- Local PostgreSQL accepting `postgres:postgres` @ `localhost:5432`
  (override the admin DSN with `CLAWITH_2F_PG_ADMIN`). Each driver creates
  its own scratch DB and drops it on exit (probes: `CLAWITH_2F_KEEP_DB=1`
  to keep).
- Real-LLM drivers additionally need `AGNES_API_KEY` +
  `AGNES_BASE_URL` (default `https://apihub.agnes-ai.com/v1`, model
  `agnes-3.0-flash`). 0-paid drivers never contact an external LLM.
- Windows hosts: the command primitive is a host-substituted one-shot
  subprocess (documented host artifact — bubblewrap is Unix-only); the
  drivers are the documented host-portable variant.

## The 10 drivers + orchestrator

All paths below are the archived names; stage them into `backend/scripts/`
before running (see Execution model).

### verify_2f_execution_probes.py — deterministic probes A–F
- Card `t_e3a745b1` (tip `1fa1c07b`). Phase-2F first wave: six deterministic
  execution probes (A–F) plus the result-store persistence proof.
- I/O: scratch Postgres + storage; **0 real LLM calls**. Writes
  `PHASE_2F_EXECUTION_PROBES_EVIDENCE.json` next to itself.
- Env: `CLAWITH_2F_PG_ADMIN`, `CLAWITH_2F_KEEP_DB`, `CLAWITH_2F_KEEP_WS`.
- Run: `cd backend && uv run --no-sync python scripts/verify_2f_execution_probes.py`

### verify_2f_real_llm_run.py — the baseline real-LLM loop
- Card `t_45477a14` (tip `0b9b2fdb`). Proves Task → Agent → real LLM HTTP
  request → tool call → tool result → verification → `run_completed` →
  task `done` end-to-end, no mocks.
- I/O: 1 real LLM call. Writes `PHASE_2F_REAL_LLM_RUN_EVIDENCE.json`.
- Env: `AGNES_API_KEY`, `AGNES_BASE_URL`, `CLAWITH_2F_PG_ADMIN`.
- Run: `cd backend && uv run --no-sync python scripts/verify_2f_real_llm_run.py`

### verify_2f_result_settlement.py — settlement & error handling
- Card `t_77399eca` (tip `c61774a8`). The failure/verification settlement
  sub-cases of the baseline loop: real `RuntimeCheckpointSideEffects`
  terminal projection (run_completed / run_failed / run_cancelled), real
  `TaskRuntimeCompletionHandler`, derived-state projection.
- I/O: 0 real LLM calls (canned/local endpoints). Writes
  `PHASE_2F_RESULT_SETTLEMENT_EVIDENCE.json`.
- Run: `cd backend && uv run --no-sync python scripts/verify_2f_result_settlement.py`

### verify_2f_concurrency_dependency.py — concurrency + dependency chains
- Card `t_edbd5f78` (tip `33de066f`). 6 tasks (chain A→B→C + independent
  D/E/F), 4 concurrent workers; proves true simultaneous RUNNING overlap
  via persisted timestamps, dependency gating (`ensure_ready` refusal until
  upstream `done`), and per-task workspace isolation.
- I/O: 6 real LLM calls. Writes `PHASE_2F_CONCURRENCY_DEP_EVIDENCE.json`.
- Run: `cd backend && uv run --no-sync python scripts/verify_2f_concurrency_dependency.py`

### verify_2f_dedup_retry.py — §13 duplicate-run protection + §14 explicit retry
- Card `t_f28b2fa3` (tip `41b60e92`). In-flight TASK_ALREADY_RUNNING (409)
  + intake exact-input dedup (stable `task:{id}` key, 0 new Run rows) +
  explicit `task:{id}:retry:{uuid}` re-execution.
- I/O: 3 real LLM calls. Writes `PHASE_2F_DEDUP_RETRY_EVIDENCE.json`.
- Run: `cd backend && uv run --no-sync python scripts/verify_2f_dedup_retry.py`

### verify_2f_timeout.py — §16 timeout (LLM / Tool / Command)
- Card `t_255c923f` (final tip `4dc53e3a`). Three timeout classes through
  the real runtime code paths: A. LLM `request_timeout` → retryable →
  durable `waiting_user` WAIT checkpoint (local slow endpoint, no paid
  turn); B. tool-step `asyncio.wait(timeout=deadline)`; C. command
  subprocess kill (exit 124, `command_timeout`). All no-orphan /
  no-false-success invariants asserted.
- I/O: 0 paid LLM calls. `--class A|B|C|all` (default `all`). Writes
  `PHASE_2F_TIMEOUT_EVIDENCE.json`.
- Run: `cd backend && uv run --no-sync python scripts/verify_2f_timeout.py --class all`

### verify_2f_cancellation.py — §15 durable cancellation
- Card `t_6efa793e` (tip `e34e0f99`). Capability audit (Branch A: cancel
  fully supported) then RUNNING → CANCELLED end-to-end: a real long
  host-subprocess holds the in-flight window, the documented
  `cancel_run()` control-plane command settles it; invariants (a)–(e)
  from the card are asserted.
- I/O: 0 real LLM turns (local canned OpenAI-compatible endpoint). Writes
  `PHASE_2F_CANCELLATION_EVIDENCE.json`.
- Run: `cd backend && uv run --no-sync python scripts/verify_2f_cancellation.py`

### verify_2f_tenant_isolation.py — §18 two-tenant real execution
- Card `t_6e34f801` (tip `014f677b`). Two real tenants on the real durable
  Runtime: authoritative tenant SELECT injection, TenantScopedBaseDAO
  scoped reads, 403/404 agent access, execution-gate tenant refusal,
  storage-key namespace + traversal guard. No new isolation layer
  invented.
- I/O: 2 real LLM calls. Writes `PHASE_2F_TENANT_ISOLATION_EVIDENCE.json`.
- Run: `cd backend && uv run --no-sync python scripts/verify_2f_tenant_isolation.py`

### verify_2f_full_project_e2e.py — §19 the full Project→Task→Run chain
- Card `t_6f849cce` (tip `720e714b`). Twelve links: Project intake →
  local-git GitSource acquisition → materialization → analysis → task
  decomposition → READY gate → agent → real LLM run → tool → workspace
  change → verification → task result. All 12 links PASS on the real
  runtime.
- I/O: 4 real LLM calls. `CLAWITH_2F_E2E_STABLE=1` preflight stops at
  READY with 0 LLM; `CLAWITH_2F_E2E_COLLECT=1` (with the `CLAWITH_2F_E2E_*`
  coordinates) collects-only. Writes `PHASE_2F_FULL_PROJECT_E2E_EVIDENCE.json`.
- Run: `cd backend && uv run --no-sync python scripts/verify_2f_full_project_e2e.py`

### verify_2f_concurrency_ladder.py — §20 performance ladder (5→10→20)
- Card `t_99c4a1bb` (tip `072592a0`). Staged real-load ladder, one scratch
  DB per stage, stop-on-instability; persisted-timestamp overlap proof per
  stage; stage 20 is the tested ceiling. The card's recorded verdict:
  stage 5 clean, stage 10 (one verbatim 5xx retried clean), stage 20 not
  re-run on closure (cost control) → `safe_concurrency = 5`.
- I/O: 74 real LLM calls across the recorded stages (19 stage-5, 55
  stage-10). Writes `PHASE_2F_CONCURRENCY_LADDER_{EVIDENCE,STAGE5,STAGE10}.json`.
  Sub-stage runs are self-spawned via `LADDER_MODE` / `LADDER_BASE` /
  `LADDER_N` / `LADDER_DEADLINE_S` / `LADDER_REBUILD` env.
- Run: `cd backend && uv run --no-sync python scripts/verify_2f_concurrency_ladder.py`
  (expect ~74 paid calls — budget it explicitly before running.)

### regression_2f_all.py — §26-P the all-wave regression orchestrator
- Card `t_933d0fa1` (tip `eebfd379`). Re-runs every prior-wave driver on a
  fresh scratch DB, then ladder confirm (re-reads committed stage JSONs,
  no re-burn), Phase-2E pytest, no-core-rewrite audit, closed-set
  projection spot check. 13/13 sections matched → OVERALL PASS.
- Drives the nine prior-wave drivers at their card-tip bytes via
  `git show <tip>:<path>` into a scratch location, so it is faithful to
  the landed code, not to the working tree it runs in.
- Flags: `--zero-llm` (0-paid drivers), `--real-llm` (paid drivers),
  `--static` (ladder/pytest/audit/projection, no LLM), `--report`,
  `--only a,b,c`, `--all`. Env: `REGRESSION_DRIVER_TIMEOUT_S` (default 900).
- Run: `cd backend && uv run --no-sync python scripts/regression_2f_all.py --static`

## The evidence JSONs

- `PHASE_2F_<DRIVER>_EVIDENCE.json` (×8) — per-driver PASS records from
  the §26-P regression re-run (`t_933d0fa1`), one per driver above.
- `PHASE_2F_EXECUTION_PROBES_EVIDENCE.json` — probes A–F record
  (`1fa1c07b`).
- `PHASE_2F_CONCURRENCY_LADDER_{EVIDENCE,STAGE5,STAGE10}.json` — committed
  ladder stage records (`072592a0`); the orchestrator re-reads these
  instead of re-burning the ladder.
- `PHASE_2F_REGRESSION_EVIDENCE.json` — the 13-section cumulative
  regression record (`eebfd379`).

All JSONs are machine-checked by `regression_2f_all.py`; each carries its
own task id, scratch coordinates, expected-vs-observed verdict, and LLM
call accounting.

## The reports

- `reports/PHASE_2F_REGRESSION_REPORT_T_933D0FA1.md` — per-driver result
  table, 2E pytest count, no-core-rewrite audit, projection,
  LLM-cost reconciliation, overall verdict (PASS 13/13).
- `reports/PHASE_2F_DETERMINISTIC_EXECUTION_PROBES_REPORT.md` — probes
  A–F findings + result-store persistence proof.

Related root-level docs on `main`: `docs/PHASE_2F_CONVERGENCE_REPORT.md`
(the §26 final deliverable of root `t_4910b73e`).

## What was deliberately NOT archived

- Per-worktree `_drivers/`, `_scratch/` copies and scratch JSONs
  (`ladder_ev.json`, `stage10.json` dumps) — temporary debug state,
  superseded by the committed evidence above.
- The 8 per-card report `.md` files that live on their card branches:
  `PHASE_2F_CANCELLATION_REPORT_T_6EFA793E.md` (e34e0f99),
  `PHASE_2F_REAL_LLM_RUN_REPORT_T45477A14.md` (0b9b2fdb),
  `PHASE_2F_RESULT_SETTLEMENT_REPORT_T77399ECA.md` (c61774a8),
  `PHASE_2F_CONCURRENCY_DEPENDENCY_REPORT_TEDBDF78.md` (33de066f),
  `PHASE_2F_DEDUP_RETRY_REPORT_TF28B2FA3.md` (41b60e92),
  `PHASE_2F_TIMEOUT_REPORT_T_255C923F.md` (4dc53e3a),
  `PHASE_2F_TENANT_ISOLATION_REPORT_T_t_6e34f801.md` (014f677b),
  `PHASE_2F_CONCURRENCY_LADDER_REPORT_T_99C4A1BB.md` (072592a0).
  These remain cited by the drivers' own docstrings on their card
  branches. The full-project e2e card (720e714b) committed no report
  .md — its findings are recorded in the card + convergence report +
  `PHASE_2F_FULL_PROJECT_E2E_EVIDENCE.json` (archived here).
  This archive covers the re-runnable drivers + their machine evidence,
  which is what re-verification needs.
