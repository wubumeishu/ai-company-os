# Phase 2F §26-P — Regression Report (t_933d0fa1)

**Base tree:** main @ `3175c798` (Phase-2E Convergence tip; every Phase-2F wave branch descends from it)
**Generated:** 2026-09-26T19:55:44.402991+00:00
**Orchestrator:** `backend/scripts/regression_2f_all.py` → evidence `backend/scripts/PHASE_2F_REGRESSION_EVIDENCE.json`

---

# OVERALL REGRESSION: **PASS** — 13/13 sections matched.

- 9 driver re-runs: **all exit 0** (each card's recorded verdict) on fresh scratch Postgres DBs.
- Phase-2E pytest: **passed=37 skipped=0 failed=0 :: 37 passed, 1 warning in 1.60s**
- No-core-rewrite audit: **core-runtime files touched across waves = NONE; product-code files forced (non-script/doc/test) = NONE**
- Closed-set projection: **observed={'RUNNING': 'RUNNING', 'QUEUED': 'QUEUED', 'SUCCEEDED': 'SUCCEEDED', 'BLOCKED': 'BLOCKED', 'FAILED': 'FAILED', 'CANCELLED': 'CANCELLED', 'READY': 'READY'} match=True**

The Final Gate (child reviewer t_d066fdd5) may approve — this regression reports **PASS**.

---

## 1. Per-driver / per-section result table (expected vs observed)

Each driver is run at its card's **landed commit** (byte-identical via `git show`), on its own fresh scratch DB. "match" = driver exit code 0 (each card's recorded verdict was "PASS … exit 0").

| section | card | commit | LLM class | expected | observed | match |
|---|---|---|---|---|---|---|
| probes | t_e3a745b1 | 1fa1c07b | 0 | 9/9 PASS | exit=0 verdict='PASS — all 9 deterministic sub' subchecks=9/9 all_true=True | YES |
| settlement | t_77399eca | c61774a8 | 0 | 6/6 PASS | exit=0 all_pass=True scenarios=6/6 | YES |
| timeout | t_255c923f | 2a65509b | 0 | PASS (A/B/C 3 classes, 0 paid LLM) | exit=0 classes=['A', 'B', 'C'] no_orphan=True | YES |
| cancellation | t_6efa793e | e34e0f99 | 0 | PASS 6/6 (RUNNING->CANCELLED, 0 real LLM) | exit=0 run_cancelled=True task_derived=CANCELLED run_count=1 | YES |
| real_llm | t_45477a14 | 0b9b2fdb | real | PASS (1 real LLM run) | exit=0 run_terminal=run_completed task_status=done tools=3 | YES |
| concurrency | t_edbd5f78 | 33de066f | real | PASS (6-Task chain+parallel) | exit=0 branches=6/6 total_revisions=6 workers=4 | YES |
| dedup_retry | t_f28b2fa3 | 41b60e92 | real | 3/3 PASS | exit=0 all_pass=True scenarios=3/3 | YES |
| tenant | t_6e34f801 | 014f677b | real | PASS 8/8 denials + 2/2 positive | exit=0 denials=8/8 positive_controls=2/2 | YES |
| e2e | t_6f849cce | 720e714b | real | PASS 12-link (4 real LLM HTTP calls) | exit=0 terminal=run_completed task_final=done llm_http_calls=4 | YES |
| ladder | t_99c4a1bb | 072592a0 | recorded (no re-run) | stage5 clean + stage10 ceiling (>=1 real 5xx) + stage20 NOT executed (recorded) | stage5_clean=True stage10_5xx=1 verbatim_5xx=True stage10_rate_limited=True stopped_early=True stopped_at=10 safe_concurrency=5 | YES |
| pytest-2e | t_b0bb2f7c (§25 item 17) | HEAD | 0 | all pass (Phase-2E 2E suite; 37 at t_b0bb2f7c, cite current) | passed=37 skipped=0 failed=0 :: 37 passed, 1 warning in 1.60s | YES |
| no-core-rewrite-audit | all-wave | 3175c798.. | n/a | 0 changes to LangGraph driver / command worker / model step / tool executor / result store | core-runtime files touched across waves = NONE; product-code files forced (non-script/doc/test) = NONE | YES |
| projection | §26 item 4 | HEAD | 0 | READY/QUEUED/RUNNING/SUCCEEDED/FAILED/BLOCKED/CANCELLED all project correctly | observed={'RUNNING': 'RUNNING', 'QUEUED': 'QUEUED', 'SUCCEEDED': 'SUCCEEDED', 'BLOCKED': 'BLOCKED', 'FAILED': 'FAILED', 'CANCELLED': 'CANCELLED', 'READY': 'READY'} match=True | YES |

**No NEW failure was observed** — every driver matched its card's recorded verdict; no verdict was hidden or papered over.

## 2. Ladder (t_99c4a1bb) — CONFIRMED-RECORDED, not re-executed

Re-running the 5→10→20 ladder would burn ~74 real LLM HTTP calls (violates §24 cost control). Instead the orchestrator re-reads the **committed** stage evidence via `git show 072592a0:backend/scripts/PHASE_2F_CONCURRENCY_LADDER_*.json`:

- Observed: `stage5_clean=True stage10_5xx=1 verbatim_5xx=True stage10_rate_limited=True stopped_early=True stopped_at=10 safe_concurrency=5`
- Match: YES (stage 5 clean, stage 10 ceiling with ≥1 verbatim 5xx, stage 20 NOT executed).

## 3. Phase-2E regression (§25 item 17)

Command (worktree venv, current tree): `uv run --no-sync pytest tests/test_task_execution_service.py -q`

- Observed: `passed=37 skipped=0 failed=0 :: 37 passed, 1 warning in 1.60s` — the Phase-2E suite count (37 at t_b0bb2f7c) holds on the current tree.

## 4. No-core-rewrite audit

Method: for **every** Phase-2F wave branch tip, `git diff --name-only 3175c798..<tip>`, keeping only files NOT under a driver/evidence/report prefix (`backend/scripts/`, `scripts/`, `docs/`, `backend/tests/`); any remaining file is a **product-code** change the phase forced, then assert NONE lands in the core-runtime set (LangGraph driver, command worker, model step, tool executor, result store — `backend/app/services/agent_runtime/` + the 2E execution service/executor/API files).

- Result: `core-runtime files touched across waves = NONE; product-code files forced (non-script/doc/test) = NONE`

No product-code fix was forced this phase, so the 'list with file:line + why' clause is satisfied by the empty list. **AUDIT: CLEAN.**

## 5. Closed-set projection spot check (§26 item 4)

Re-executed the **real** `TaskExecutionService._derive_state` (`backend/app/services/task_execution_service.py:547`) for all 7 states of the §6.2 closed set:

- Observed: `observed={'RUNNING': 'RUNNING', 'QUEUED': 'QUEUED', 'SUCCEEDED': 'SUCCEEDED', 'BLOCKED': 'BLOCKED', 'FAILED': 'FAILED', 'CANCELLED': 'CANCELLED', 'READY': 'READY'} match=True`

All 7 derived states (READY/QUEUED/RUNNING/SUCCEEDED/FAILED/BLOCKED/CANCELLED) project correctly from Task.status + unmet deps + active Run + command row + latest terminal event.

## 6. LLM cost reconciliation (§24 cost control)

| class | drivers | LLM load |
|---|---|---|
| 0-paid | probes, settlement, timeout, cancellation | 0 real LLM calls (canned/local endpoints) |
| real (bounded) | real_llm, concurrency, dedup_retry, tenant, e2e | each at its card's recorded call count |
| recorded (no re-run) | ladder | 0 new (re-read committed evidence) |

No **new** heavy LLM load was added; every driver ran at its card's already-recorded budget.

## 7. Reproduction

```bash
cd backend
uv run --no-sync python scripts/regression_2f_all.py --zero-llm   # 0-paid drivers
uv run --no-sync python scripts/regression_2f_all.py --real-llm   # bounded real-LLM drivers
uv run --no-sync python scripts/regression_2f_all.py --static     # ladder/pytest/audit/projection
uv run --no-sync python scripts/regression_2f_all.py --refresh    # 0-LLM: re-derive observed + re-run static
uv run --no-sync python scripts/regression_2f_all.py --report     # emit this report
```

Each pass is cumulative (merge-by-section-key) and crash-resilient: a crash in one pass preserves the others.

## 8. Acceptance checklist (card §26-P)

- [x] Every prior-wave driver re-run, matching its recorded verdict (or a NEW failure reported verbatim).
- [x] Phase-2E suite green.
- [x] No-core-rewrite audit clean (or violations listed).
- [x] Overall verdict REGRESSION: PASS.
- [x] ruff + py_compile clean on the new orchestrator.
