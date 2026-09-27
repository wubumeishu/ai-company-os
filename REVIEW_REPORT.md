# REVIEW_REPORT — Phase 2F Closure Evidence (independent review)

- **Task:** `t_b68a99fb` — "Execute independent review of Phase 2F closure evidence"
- **Reviewer:** aco-reviewer (independent review/gate profile)
- **Review base:** clean worktree `wt/t_b68a99fb` @ `7258e96d` (== `origin/main`), fresh clone of main, working tree clean (only git-ignored `backend/.venv` junction from my own re-run setup)
- **Date:** 2026-09-27 (Tokyo, UTC+9)

---

## VERDICT: **REQUEST_CHANGES**

One LOW-severity, precisely-scoped documentation-accuracy correction is required in
`docs/architecture/phase2c_2f_evolution.md` (line 191) before this record is frozen and
merged to main. Everything else — code structure, migrations, tests, regression, and the
Phase 2F execution evidence — is **functionally APPROVE-quality** and was independently
reproduced by the reviewer on fresh scratch Postgres DBs (no trust in builder self-reports).

---

## 1. Review method (independence)

Per the task, I did **not** trust the upstream builder reports. I:
1. Checked out a clean worktree from `origin/main` (`7258e96d`); confirmed `main == origin/main`.
2. Located and manually verified each of the three parent deliverables **at their own commits / worktrees** (not assumed): archive `c9301fe4`, arch doc `4a21059a`, inventory `bbdb8e0f`.
3. Re-ran the G7 alembic single-head check.
4. **Re-executed** the 0-LLM deterministic drivers and the static regression pass on fresh scratch DBs in my own tree.
5. Audited git-history integrity (reflog, merges, card-commit reachability).
6. Cross-checked every load-bearing code-structure claim in the arch doc against the actual code at its cited `file:line`.
7. Classified every conclusion FACT / OBSERVATION / INFERENCE / UNKNOWN (§4).

---

## 2. Evidence inventory re-verification (task steps 2–3)

| # | Claim (from parent handoffs) | Independently verified | Result |
|---|---|---|---|
| F1 | Archive `docs/evidence/phase2f/` = 28 files | `find … -type f \| wc -l` → 28 | **FACT — TRUE** |
| F2 | `MANIFEST.sha256` 26/26 OK | re-ran `sha256sum -c MANIFEST.sha256` | **FACT — 26/26 OK** |
| F3 | 11 drivers byte-identical to card tips | re-hashed each on-disk driver vs `git cat-file -p <tip>:<path>` | **FACT — 11/11 identical** |
| F4 | 13 evidence JSONs valid + verdict-bearing | `json.load` all 13; parsed verdict fields | **FACT — all valid, verdicts present** |
| F5 | Arch doc = 272 lines @ `4a21059a` | `git show --stat` → 272 insertions | **FACT — TRUE** |
| F6 | Inventory = 175 lines @ `bbdb8e0f` | `git show --stat` → 175 insertions | **FACT — TRUE** |
| F7 | G7: single alembic head `f070_analysis_task_dedup` | re-ran `uv run --no-sync alembic heads` | **FACT — TRUE** |
| F8 | 76 migration files on main; f066–f070 present | `git ls-files 'backend/alembic/versions/*' \| wc -l` | **FACT — 76** |
| F9 | 0 `verify_2f_*` / `PHASE_2F_*_EVIDENCE.json` on main (G1/G2) | `git ls-files \| grep -c` | **FACT — 0 / 0 (gaps open on main)** |

### Independent re-execution (gold-standard; fresh scratch DBs, my own tree)

| Driver (0-LLM) | Exit | Verdict reproduced | Matches archived JSON? |
|---|---|---|---|
| `verify_2f_execution_probes.py` (staged to repo-root `scripts/`, its native path) | 0 | PASS — 9/9 sub-checks across A–F | **MATCH** (`verdict` field equal) |
| `verify_2f_cancellation.py` | 0 | PASS — RUNNING→CANCELLED, run_count=1, 0 post-cancel tool execs, worker table clean | **MATCH** (all verdict fields equal) |
| `verify_2f_result_settlement.py` | 0 | PASS — 6/6 scenarios (success/tool-fail/verif-fail/agent-fail/cancel/blocked) | **MATCH** (`all_pass=True`) |
| `verify_2f_timeout.py` | 0 | PASS — A/B/C classes, no orphan worker, exit-124 captured | **MATCH** (`global_orphan_markers=0`) |
| `regression_2f_all.py --static` | 0 | ladder re-read (stage5 clean / stage10 5xx / safe=5) + **pytest-2e 37/37** + **no-core-rewrite audit: NONE** + **closed-set projection 7/7** | **MATCH** archived `PHASE_2F_REGRESSION_EVIDENCE.json` (4/4 static sections) |

- I re-staged the 4 drivers **byte-identical** to the archive (`sha256sum` confirmed equal) before running, so the re-execution exercised the exact archived bytes, not a re-derivation.
- The `scratch_db` / run-UUID / port fields differ per-run (expected); every **verdict-bearing** field equals the archived value.

> **Note on coverage (UNKNOWN):** I did **not** personally re-burn the **real-LLM** drivers
> (`real_llm_run`, `concurrency_dependency`, `dedup_retry`, `tenant_isolation`, `full_project_e2e`,
> ladder re-run) — those require the ambient `AGNES_*` credential path + real provider HTTP and are
> cost-gated per the §24 stop rule. Their verdicts stand on (a) the archived evidence JSONs
> (re-read, valid, verdicts present) and (b) the separate independent Final-Gate reviewer
> `t_d066fdd5` who did re-execute ~15 real-LLM checkpoints on their own worktree. This is a
> deliberate cost-controlled boundary, not a gap I left open.

---

## 3. Git-history integrity (task step 4)

| Check | Result | Class |
|---|---|---|
| `main == origin/main` | both `7258e96d…` | **FACT** |
| `git reflog main` | linear doc/merge history, no force-rewrite signatures | **FACT** |
| merge commits on main | 28, all "Merge branch / Fast-forward" (legit) | **OBSERVATION** |
| CP11: product-code files in the 2F diff (`3175c798..720e714b` under `backend/app|tests|frontend|helm|deploy`) | **0 files** | **FACT** |
| 15 Phase-2F card commits — ancestors of `main`? | **0 / 15** — all live on their card branches / `origin` refs only | **FACT** |

The `0/15 on main` result is the *expected, documented* landing policy ("docs-only main; worker
evidence stays on card branches"), and the foundation inventory correctly records it as gaps
**G1–G4**. **But** it is precisely the fact that one line of the arch doc states incorrectly (below).

---

## 4. Required correction (the single change this review requests)

### LOW-1 — False FACT claim in the arch doc (correct before freeze/merge to main)

- **File / location:** `docs/architecture/phase2c_2f_evolution.md`, **line 191** (§5 Execution → Evidence Artifacts)
- **Current text:**
  > `docs/PHASE_2F_CONVERGENCE_REPORT.md` (landed on main as 7258e96d) — §A–§T with per-card citations: **15 card commits reachable on main** (`b7059cdc`, `eb8cfc0e`, … `848dfd2e`).
- **Why it is a problem (FACT-class error, and it breaks the doc's own classification discipline):**
  I measured `git merge-base --is-ancestor <tip> main` for all 15 card commits in §R — **0/15 are
  ancestors of `main`**. They are reachable **from their card branches / `origin` refs**, not from
  main. The arch doc itself says "Landed tree: main `7258e96d`" (line 6), so "reachable on main" is
  internally inconsistent with the same document. Sibling records are already correct on this point:
  - Convergence report §R (line 151): "All 15 Phase-2F card commits **reachable** and verified" — *reachable*, not "on main" (correct).
  - Convergence report §18 (line 189): "this commit on main; all 15 card commits **reachable**" (correct).
  - Foundation inventory §5.2 / G1–G4: explicitly "NOT on main" (correct).
  The arch doc line 191 is the **only** place that overstates reachability as "on main."
- **Risk:** A future reader of this *frozen* cross-phase record who trusts "15 card commits reachable on main" will run `git log`/`git show <tip>` on `main` and fail, then lose confidence in the record's other (accurate) claims. It also violates the closure-task hard rule that every conclusion carry a correct epistemic class — this is an unlabeled, false FACT.
- **Required minimum fix (atomic, one line):** change line 191's phrase
  `15 card commits reachable on main` → `15 card commits reachable via their card branches / origin refs (not on main — see foundation inventory G1–G4)`.
  No other line changes needed; the code-structure and stage claims are all verified accurate (§5 below).

Severity **LOW** (documentation wording; zero impact on code, tests, migrations, or the Phase 2F
functional gate). This is the *only* item that blocks APPROVE.

---

## 5. Architecture-doc ↔ code-structure validation (task step 5)

Every load-bearing `file:line` claim in `docs/architecture/phase2c_2f_evolution.md` was checked
against the actual code in my clean worktree — **all accurate**:

| Arch-doc claim | Code check | Result |
|---|---|---|
| `task.py:58-62` `task_status_enum` = pending/doing/done | `grep` → `:59 Enum("pending","doing","done",…)` | **FACT — TRUE** |
| `task.py:37` `TASK_CREATED_REASONS` = MANUAL/ANALYSIS_FINDING/ANALYSIS_PLANNING | `:37` exact tuple | **FACT — TRUE** |
| `task.py:84-113` five-col provenance (project_id CASCADE / analysis_run_id SET NULL / finding_id SET NULL / revision_sha / created_reason) | `:84,90,95,102,108` all present | **FACT — TRUE** |
| `analysis.py` `ANALYSIS_RUN_STATUSES`(AN_OPEN/COMPLETED/FAILED) `:60`, `ANALYSIS_FACING_TAGS`(FACT/OBS/INFER/UNKNOWN) `:71` | exact | **FACT — TRUE** |
| `task_execution_service.py:548` `_derive_state`; `:72` `RETRY_SOFT_CAP_PER_TASK_PER_DAY=3` | `:548` def, `:72` const | **FACT — TRUE** |
| `api/tasks.py:502` `POST /{task_id}/execute`, `:541` `GET /{task_id}/execution` | `:502`/`:541` exact | **FACT — TRUE** |
| `api/projects.py:646` `convert_analysis_run_to_tasks` | `:646` route, `:650` def | **FACT — TRUE** |
| `task_graph_service.py:79` `MAX_PROJECT_EDGES=1000`, `:174` `_lock_project_graph` | exact | **FACT — TRUE** |
| `RETRY_CAP_EXCEEDED` named in 2E spec §6.3 + §10.1 409 (addendum `d112c71d`) | present in spec §6.3 table + §10.1 mapping | **FACT — TRUE** |

**Conclusion (task step 5):** the architecture doc **matches the code structure** on every claim I
spot-verified. The *only* defect in that doc is the git-ref-topology wording in LOW-1 above.

---

## 6. Observations register (FACT / OBSERVATION / INFERENCE / UNKNOWN)

### FACT (directly observed in this review)
- F-A: `main == origin/main == 7258e96d`; clean worktree used for review.
- F-B: archive = 28 files; `MANIFEST.sha256` 26/26 OK; 11/11 drivers byte-identical to card tips; 13 evidence JSONs valid.
- F-C: 4/4 0-LLM drivers re-passed on fresh scratch DBs; verdict fields equal to archived JSONs.
- F-D: `regression_2f_all.py --static` reproduced 4/4: pytest-2e 37/37, no-core-rewrite audit NONE, closed-set projection 7/7, ladder re-read (stage5 clean / stage10 5xx / safe=5).
- F-E: G7 `alembic heads` = single `f070_analysis_task_dedup`.
- F-F: 76 migrations on main; f066–f070 present; 0 `verify_2f_*`/`PHASE_2F_*_EVIDENCE.json` on main (G1/G2 open).
- F-G: 0 product-code files in the Phase-2F diff (CP11) — `3175c798..720e714b` touches no `backend/app|tests|frontend|helm|deploy`.
- F-H: 0/15 Phase-2F card commits are ancestors of `main` (all on card branches/origin refs).
- F-I: arch doc's code `file:line` claims all accurate (§5).
- F-J: `clawith_2f_*` scratch DBs present in the live local PG (corroborates arch doc §S.6 / inventory U3 "left for traceability").

### OBSERVATION
- O-A: The three closure deliverables (arch doc `4a21059a`, inventory `bbdb8e0f`, archive `c9301fe4`) are all **branch-only** — none is merged to `main` yet. Merging them to main is the *downstream* `t_9dd2695f` (finalization) task, not this review's.
- O-B: The convergence report §R phrasing ("reachable") is consistent and correct; only arch-doc line 191 ("on main") diverges.

### INFERENCE
- I-A: The "reachable on main" wording in arch-doc line 191 is most likely a copy/shorten of the convergence report's "reachable" that picked up the adjacent "on main" from the same report's §18 prose — i.e., a wording error, not a functional misunderstanding. (Basis: the rest of the doc is git-accurate and the sibling records are correct.)
- I-B: If LOW-1 is corrected, the arch doc becomes fully consistent with the inventory + convergence report and can be frozen/merged with no residual factual discrepancy.

### UNKNOWN (cannot be resolved in this review; carried forward)
- U-A: Real-LLM driver verdicts were **not** re-burned by this reviewer (cost-gated; `AGNES_*` path). They stand on archived JSONs + the separate Final-Gate reviewer `t_d066fdd5`'s own real-LLM re-execution.
- U-B: Ladder stage 20/50+ behavior — documented by extrapolation only (arch doc U1 / 2F report §S.5); not executed.
- U-C: `fcntl` cross-process lock no-op on this Windows host (arch doc U6); concurrency safety rests on authoritative timestamp overlap + DB/Redis lock layers.
- U-D: Scratch `clawith_2f_*` DBs have no owner for DROP in headless single-query mode (arch doc U3); interactive hygiene pass needed.
- U-E: Live-pool `llm_models` keys = placeholder `enc-test` (arch doc U4); real-LLM chain used the dedicated `AGNES_*` path, live pool untouched.

---

## 7. What a re-review (round 2) should confirm

1. Line 191 of `docs/architecture/phase2c_2f_evolution.md` now says the card commits are
   "reachable via their card branches / origin refs (not on main — see foundation inventory G1–G4)"
   (or equivalent), and no other "reachable on main" phrasing remains in the doc.
2. No other line was altered that would touch a verified-accurate claim.
3. Everything in §5 / §6 (code-structure + re-execution) is unchanged and still holds.

That is a ~30-second re-review; no re-execution is required unless the doc is touched beyond LOW-1.

---

## 8. Final statement

- **Code, migrations, tests, regression, and Phase-2F execution evidence: APPROVE-quality** —
  independently reproduced by the reviewer on fresh scratch DBs; no defects found.
- **One LOW documentation-accuracy defect (LOW-1)** in the arch doc: a false "reachable on main"
  FACT claim that contradicts the record's own sibling documents and its git-ref reality.
- **Overall verdict: REQUEST_CHANGES** — with exactly the single atomic correction in §4. The
  correction lands naturally in the `t_9dd2695f` finalization merge, so it does not reopen any
  functional work.
