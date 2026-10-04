# Phase 4 Convergence Report — Artifact, Independent Review, Completion & Delivery

**Task:** `t_c75fc916` (Phase 4 Convergence Report, Root `t_4047050f` criterion 14)
**Author:** aco-builder
**Root:** `t_4047050f` (Phase 4 — Artifact, Independent Review & Completion)
**Landed-on basis:** `main == origin/main == b2cc6e0d` (tag `PHASE_4_CLOSED`), base `8f030792` (tag `PHASE_3_CLOSED`).
**Precedent:** Phase 3 Convergence Report (`docs/PHASE_3_CONVERGENCE_REPORT.md`, commit `38048791`).
**This document is a durable convergence artifact only** — no new product code, no new migration. It ties the whole phase to its final-main evidence: design → implementation → tests → E2E → independent review → rework → landed main. It does **not** carry the final PASS verdict; that belongs to the independent Final Gate adjudication `t_ebfd863e` (Root criterion 15).

---

## 0. Epistemic discipline & self-re-verification

Classification follows the Phase 2C–3 / Phase 4 audit convention and the Root brief:

- **FACT** — directly observed at the cited commit / from a named constant or migration id.
- **OBSERVATION** — a pattern seen in one or more concrete instances, no universal claim.
- **INFERENCE** — a conclusion drawn from the facts above.
- **UNKNOWN** — not verifiable from this tree; the resolving action is stated.

**Two classes of evidence, kept separate (do not conflate):**

1. **[VERIFIED-ON-MAIN]** — re-run by *this* card against `main` = `b2cc6e0d` in worktree `wt/t_c75fc916` on 2026-10-04, on a **fresh clawith-owned scratch Postgres `clawith_t_c75fc916_f073`** (native PG16 @ `127.0.0.1:5432`, provisioned by the card-local tool `backend/_provision_f073_c75fc916.py`, distinct from every worker's and reviewer's `clawith_t_*` / `aco_*` scratch DB). Where the report cites a live count as FACT it is a re-execution, not a worker self-report.
2. **[CARD-HANDOFF]** — taken from a completed card's summary/artifact. These are *independently produced* records; the load-bearing ones were re-burned under class 1 where this card could re-execute them.

**Re-execution record for this card (all on `clawith_t_c75fc916_f073`, real alembic chain, single head `f073_delivery_records`):**

| Check | Result | Source |
|---|---|---|
| `alembic upgrade head` full chain 001→…→f071→f072→f073 | exit 0, single head `f073_delivery_records` | [VERIFIED-ON-MAIN this run] |
| §7 E2E live tier `test_phase4_e2e_chain_acceptance.py` (all 12 live tests + DB-free tier) | **12 passed** in 81.87 s | [VERIFIED-ON-MAIN this run] |
| F1 execution probe `test_live_record_rework_writes_no_builder_review_verdict` | **PASSED** | [VERIFIED-ON-MAIN this run] |
| Review lane `test_artifact_evidence_review.py` (DB-free + live) | **32 passed** in 1.25 s | [VERIFIED-ON-MAIN this run] |
| Root-§5 gate suite `test_agent_runtime_tool_outcome_contract.py` + `test_agent_runtime_task_completion.py` | **36 passed** in 8.09 s | [VERIFIED-ON-MAIN this run] |
| Completion + delivery live tiers `test_completion_service.py` + `test_delivery_service.py` | **78 passed** in 2.74 s | [VERIFIED-ON-MAIN this run] |
| Frozen Phase 2C-3 spine regression (tool-outcome-contract / task-completion / node-executor / planning / persistence / planning-scheduler / planning-dao / schedule-scheduler / schedule-scheduler-startup) | **135 passed, 11 skipped** in 22.24 s | [VERIFIED-ON-MAIN this run] |

The frozen-spine check for the LAND card (`t_e35ab093`) was the *DB-free* 129/129 battery; this card re-ran an extended 9-suite spine set (135/11-skip) — both are DB-free and non-regressing. The difference is suite selection, not a behavior difference. [OBSERVATION]

---

## 1. Scope & lineage

**One-line chain (the deliverable this phase closes):**

`Task → Assignment → Runtime → Run → Artifact/Evidence → Independent Review → Rework → Re-review → Completion → Delivery`

- The head `Task → Assignment → Runtime → Run` is the **frozen Phase 2C/2F spine** (base `8f030792`).
- This phase adds four bounded new capabilities over it: the two **append-only ledgers** (`artifact_records` / `evidence_records`), the **Independent Review & Rework** lane (verdict rows, `superseded_by` rework, disjoint re-review), the **Completion** lane (derived CT/CW/CP predicates + the single `COMPLETED` write site), and the **Delivery** record lane (CP_OK-gated, closed destination vocabulary).
- The **full chain was proven on real execution** in §7 (Section 8 below): a real scratch Postgres over the frozen spine, [DB-only + mock agent] execution-marking labelled, no second runtime.

**Exact commit lineage that landed on main (all reachable from `b2cc6e0d`):**

```
8f030792  PHASE_3_CLOSED (base; untouched)
   │
   │  — Wave 1: docs + core storage + F1 gate —
   ├─ 7a8a389a  audit: Phase 2C-3 source audit for Artifact/Review (Q1–Q13)      [t_15e05452]
   ├─ b2d4ec51  design: Artifact/Evidence V1 domain                              [t_f19aae89]
   ├─ 8448b7ba  design: Independent Review & Rework V1 state machine             [t_dbb0c0dd]
   ├─ ac10c1a4  design: Completion & Delivery criteria V1                        [t_4185daed]
   ├─ 38ce01fc  feat: Artifact/Evidence ledgers + Review/Rework core (Tasks 1-3) [t_436ddafb]
   ├─ efaf3b17  docs: Independent Review & Acceptance — REJECT F1 (HIGH)         [t_fa30ea5d]
   ├─ 76086d65  fix: F1 rework — builder no longer authors the verdict row       [t_b9e57a65]
   ├─ 147ebf93  fix: Root-§5 TaskCompletionGate fail-open → fail-closed          [t_56fbca2e]
   └─ 0ebf7358  docs: align owning note + ROOT5_TASK_COMPLETION_GATE_FAIL_CLOSED_FIX.md
   │
   │  — Wave 2: completion + delivery + §7 E2E (folds the lane tips into one line) —
   ├─ 581419dc  feat: completion lane (CT/CW/CP + CP_*/CD_* + single COMPLETED site) [t_e399386f]
   ├─ 9ffb1095  feat: delivery record lane (f073 + DAO + C-D1..C-D4 service)     [t_af586c02]
   ├─ 315d405a  fix: G3 rework provenance live wrapper + §7 scratch E2E          [t_ab1fb86a]
   └─ fdeae189  feat: §7 real E2E — full Task→…→Delivery chain + negatives       [t_ab1fb86a]
   │
   ╰──8280d2d3  MERGE feature line (--no-ff, +10122 insertions / 9 deletions) into main
   │
   21d9df6f  merge docs: PHASE_4_ARTIFACT_EVIDENCE_V1_DESIGN            (tip b2d4ec51)
   fbee43a0  merge docs: PHASE_4_INDEPENDENT_REVIEW_REWORK_V1_DESIGN    (tip 8448b7ba)
   dd458a48  merge docs: PHASE_4_COMPLETION_DELIVERY_CRITERIA_V1_DESIGN (tip ac10c1a4)
   f1b8e529  merge docs: PHASE_4_AUDIT_REPORT_T15E05452                 (tip 7a8a389a)
   b2cc6e0d  merge docs: PHASE_4_INDEPENDENT_REVIEW_DECISION_T_FA30EA5D (tip efaf3b17)

   b2cc6e0d == origin/main == tag PHASE_4_CLOSED   [t_e35ab093]
```

- **Verified on this card:** `git rev-parse main origin/main` → both `b2cc6e0d155f5d56c9243a5d884fbed5b7a5aa8f`; `git cat-file -t PHASE_4_CLOSED` → `tag` (annotated, @ `b2cc6e0d`); all six lane tips (`38ce01fc`, `76086d65`, `581419dc`, `9ffb1095`, `0ebf7358`, `fdeae189`) pass `git merge-base --is-ancestor … main` [VERIFIED-ON-MAIN this run].
- **Full Phase 4 diff `8f030792..b2cc6e0d`: 27 files, +12096 / −9.** [VERIFIED-ON-MAIN] The 9 deletion lines are confined to exactly the three files Phase 4 legitimately touched: `verification.py` (+17/−6, the fail-closed rename + G3 wrapper), `test_agent_runtime_tool_outcome_contract.py` (+5/−2, one expectation flip), `docs/AGENT_RUN_EXECUTION_CHAIN.md` (+8/−1). **Zero add / zero delete on the eight frozen Phase 2C-3 spine files** (`node_executor.py`, `task_completion.py`, `task_execution_service.py`, `command_worker.py`, `planning_service.py`, `models/task.py`, `models/analysis.py`, `dao/task_dao.py`) — re-checked per-file on this card [VERIFIED-ON-MAIN this run].

---

## 2. Design → implementation (Root criteria 1–2)

Sources (all on main): `docs/architecture/PHASE_4_ARTIFACT_EVIDENCE_V1_DESIGN.md` (`b2d4ec51`, 493 lines), `docs/architecture/PHASE_4_INDEPENDENT_REVIEW_REWORK_V1_DESIGN.md` (`8448b7ba`, 505 lines), `docs/architecture/PHASE_4_COMPLETION_DELIVERY_CRITERIA_V1_DESIGN.md` (`ac10c1a4`, 568 lines), plus the source audit `PHASE_4_AUDIT_REPORT_T15E05452.md` (`7a8a389a`).

**The two ledger tables (new, tenant-scoped, DDL on main via f072):**
`artifact_records` + `evidence_records` (`models/artifact_evidence.py`, `dao/artifact_evidence_dao.py`), with the DDL backstops named in design §7: `ck_artifact_records_source` (invariant 1, D5 source XOR), `ck_artifact_records_seal` (invariant 2, one-way DRAFT→SEALED), `ck_evidence_records_source` / `ck_evidence_records_outcome` (closed outcome set), `uq_artifact_records_tenant_ref` UNIQUE (invariant 4, dedup), and the partial UNIQUE `uq_evidence_records_reverify` `WHERE NOT (payload ? 'reverify_of')` (invariant 5, one C5 decision row per subject unless chained) [FACT — `alembic/versions/v1_11_5_f072_artifact_evidence.py` on main; DDL confirmed present live on `clawith_t_c75fc916_f073` this run].

**The landed lane, per design:**
- **Artifact/Evidence + Review/Rework** (`38ce01fc`, `t_436ddafb`): `review_rework_service.py` (800 lines at merge; `record_review` / `record_rework` / `seal` / `current_valid_review`), `artifact_evidence_resolver.py`, the DAO, plus the two-tier test `test_artifact_evidence_review.py` + `test_artifact_evidence_migration.py`. [CARD-HANDOFF: 40/40 DB-free/live/f072 + 10/10 migration DDL at commit time]
- **Completion** (`581419dc`, `t_e399386f`): `completion_service.py` (1161 lines) — the CT/CW/CP predicate core, the closed `CP_*` code set, the CD_* delivery contract constants (`CD_OK/CD_NOT_COMPLETED/CD_NO_SEALED_APPROVED/CD_DESTINATION_INVALID/CD_EVAL_ERROR` + `DELIVERY_DESTINATION_KINDS = ("channel","published_page","project_record")`), ONE chained C5 decision row per evaluation (`payload.reverify_of` — the live-f072 adjudication of the invariant-5 partial-unique index), and **the single owning `Project.status='COMPLETED'` write site** (`completion_service.py:890`, fresh CP_OK + executable status + tenant, idempotent by construction). [CARD-HANDOFF: 41/41 tests (37 DB-free + 4 live f072)]
- **Delivery** (`9ffb1095`, `t_af586c02`): `delivery_records` table (f073, lockstep up+down, single head confirmed) + `delivery_record_dao.py` (append-only, one-way PENDING→terminal `transition_state`) + `delivery_service.py` (C-D1..C-D4: CP_OK gate, cited set re-checked against current-valid ledger state, closed destination vocabulary, `Task.status` / `final_answer` / gate verdict **never read**). [CARD-HANDOFF: 37/37 tests (31 DB-free + 6 live)]

**Reusable-not-duplicated boundary held:** the completion lane's CP evaluator is *consumed* by the delivery lane (not redefined); the two trust boundaries (assignment-time `PL_REVIEWER_NOT_INDEPENDENT` in frozen `assignment_service.py` + verdict-time `EV_REVIEW_NOT_INDEPENDENT` in the new lane) are consumed, never re-implemented (review design §2 R2/G1). The only schema additions in the whole phase: f072 (two tables) + f073 (one table) [FACT].

---

## 3. The F1 review cycle (Root criteria 3, 12)

**Original acceptance review `t_fa30ea5d` → VERDICT REJECT (1 HIGH)** (`efaf3b17`, decision doc on main):

- The reviewer independently re-ran the lane on its **own** scratch DB `aco_p4_gate1` (real alembic chain exit 0, single head f072; 40/40 lane + 86/86 frozen regression; DDL invariants present). All dimensions PASSED *except* the invariant Phase 4 exists to establish: **F1 (HIGH) — the builder-authored `kind='review'` verdict row on the rework path bypassed invariant-13 / G4.** The shipped `plan_rework` wrote a `rework_review` EvidenceRecord with `created_by_agent=builder_agent_id` — the builder writing its own APPROVE/verdict, exactly the "builder self-review" the Root hard boundary forbids. [CARD-HANDOFF + FACT (doc on main)]
- **Rework `t_b9e57a65` → `76086d65`:** `review_rework_service.py` — `plan_rework` no longer authors the builder verdict row (diff: the `rework_review: EvidenceRecord | None` field and the `EvidenceRecord(created_by_agent=builder_agent_id, …)` construction are deleted; rework now ends at "new artifacts + new proof evidence + `superseded_by` links"; `builder_agent_id` is retained only as rework provenance, "never a verdict author"). The re-review verdict is a **separate disjoint-Reviewer act** via `record_review(…, rework_of=fail_review.id)` which already passes `reviewer_builder_agents=set(builder_agent_ids)` and enforces invariant-13 (`EV_REVIEW_NOT_INDEPENDENT`) at the DAO. Before/after at the test level: `test_rework_links_new_rows_and_carries_rework_of` → `test_rework_writes_no_review_verdict_row`; NEW `test_rereview_verdict_carries_rework_of_and_builder_self_review_rejected` (builder verdict rejected at both the lane gate `RV_REVIEW_NOT_INDEPENDENT` and the DAO `EV_REVIEW_NOT_INDEPENDENT`); NEW live `test_live_record_rework_writes_no_builder_review_verdict` (execution: 1 review row == the reviewer's fail verdict, 0 builder-authored, builder verdict rejected live). [FACT — commit diff + test names on main]
- **2nd independent review `t_cb21f262` → VERDICT APPROVE:** F1 CLOSED, execution-verified on the reviewer's own fresh scratch Postgres (independent worktree, not this card's DB) [CARD-HANDOFF].
- **This card's re-execution:** `test_live_record_rework_writes_no_builder_review_verdict` **PASSED** on `clawith_t_c75fc916_f073` — F1 remains closed on final main [VERIFIED-ON-MAIN this run].

---

## 4. Rework / Re-review chain (Root criterion 4)

The G2/G3 chain is real, not a state diagram:

- **REQUEST_CHANGES:** a review row with `outcome='fail'` carrying `payload.critiques` / `payload.required_changes` (review design §3.3 guarded transition table). [FACT — `review_rework_service.py` + design `8448b7ba` §3.3 on main]
- **REWORK (non-destructive, G2):** `record_rework` writes **new** artifact/evidence rows and stamps the prior rows via `superseded_by` — history is never overwritten; old APPROVE rows become automatically historical (the "current-valid review" query in design §3.4 walks `superseded_by`). No row is deleted anywhere in the lane (append-only DAO). [FACT]
- **DISJOINT RE-REVIEW (G3 provenance):** the re-review row carries `payload.rework_of = fail_review.id` — written by the disjoint reviewer (invariant-13 enforced), so the re-review is walkable to the original fail verdict. The `G3 live wrapper` fix `315d405a` added `rework_provenance_for_task` (the live ledger walk) after the DB-free reference test exposed it as structurally empty on live calls. [FACT — commit message on main]
- **Proof:** `test_phase4_e2e_chain_acceptance.py` — `test_review_rework_rereview_chain`, `test_rework_run_and_disjoint_rereview`, and the DB-free `test_db_free_stale_approve_drops_out_and_rework_provenance_walks` (a stale APPROVE drops out of current-valid after rework; provenance walks). All 12 live tests **passed** on this card's scratch DB [VERIFIED-ON-MAIN this run].

---

## 5. Completion ≠ self-report; audit Q10 closed (Root criterion 5)

- **Completion is derived, not reported.** `CT(T)` / `CW(W)` / `CP(P)` are read-side predicates over the ledgers + the frozen task graph (completion design §3–§5; `completion_service.py`). `Task.status`, `final_answer`, and the gate verdict are **never inputs** — module doc `completion_service.py:23` states it explicitly, and the lane "writes NOTHING else: no Task.status, no seal, no artifact row" (`:706`). [FACT]
- **The closed `CP_*` code set** (design §3.5, 11 codes: `CP_OK`, `CP_NO_WORK`, `CP_NOT_SEALED`, `CP_NO_APPROVING_REVIEW`, `CP_REVIEW_NOT_INDEPENDENT`, `CP_OPEN_REQUEST_CHANGES`, `CP_INCONCLUSIVE_REVIEW`, `CP_DEPS_NOT_DONE`, `CP_TENANT_MISMATCH`, `CP_OPEN_SLOT`, `CP_EVAL_ERROR` catch-all) is fail-closed: unknown/unreadable input lands in `CP_EVAL_ERROR`, **never** `CP_OK` [FACT — `completion_service.py:113-163`].
- **Single owning write site (audit Q10 closed):** the source audit found **zero** write sites for `Project.status="COMPLETED"` at baseline ("Execution is *gated* … but that governs *when work may run*, not *completion*", `PHASE_4_AUDIT_REPORT_T15E05452.md` Q10). The completion lane created exactly one: `completion_service.py:888-890` (fresh `CP_OK` + executable status + tenant match → `project.status = "COMPLETED"`; idempotent on re-call; C5 decision row chain `payload.reverify_of` records each evaluation, adjudicated live against the invariant-5 partial index — `test_live_c5_chain_appends_twice_and_chains`). **Re-verified on this card:** `grep -rn` for `ProjectStatus.COMPLETED` / `project.status = "COMPLETED"` write sites across `backend/app` returns *only* `completion_service.py` — no second site exists [VERIFIED-ON-MAIN this run].

---

## 6. Fail-open defect closed (Root criterion 6)

- The audit named the inherited defect: `TaskCompletionGate._fail_open` — on gate error the semantic completion gate returned `outcome="pass"`, letting a broken/unevaluable gate mark a Task done (Root §5 "inherited fail-open" clause, Phase 3 residual G8/D-3).
- **Fix `147ebf93`** (`verification.py` +17/−6): rename `_fail_open → _fail_closed`; all four gate-error paths (`invalid_completion_gate_identity`, `completion_gate_model_unavailable`, `completion_gate_call_failed`, `invalid_completion_gate_output`) now return `outcome="fail"` with the deterministic verifier's closed-code detail shape (`details["code"]="completion_gate_error"` + `gate_error_code` + actionable reason). `outcome="fail"` already routes to Run failed/terminal → the Task is NOT marked done. No new outcome value — `"fail"` was already in the closed `VerificationOutcome` set. The single test asserting the old fail-open "pass" had its expectation flipped (the +5/−2 on `test_agent_runtime_tool_outcome_contract.py`), coverage kept. [FACT — commit diff + current `verification.py:625-650` on main]
- **Isolated with its own independent review** `t_675b2d56` → **APPROVE** (reviewer's own checkout; all three gate-error paths + invalid-identity verified fail-closed; deterministic verifier unchanged) [CARD-HANDOFF].
- **This card's re-execution:** `test_agent_runtime_tool_outcome_contract.py` + `test_agent_runtime_task_completion.py` = **36 passed** on final main; and the §7 E2E negative `test_gate_error_fails_closed_task_not_done` (gate ERROR → Run terminal-failed, Task NOT marked done) passed in the 12/12 live tier [VERIFIED-ON-MAIN this run].

---

## 7. Delivery: "Agent says done → Delivery" impossible at every hop (Root criterion 7)

`delivery_service.py` (`9ffb1095`) implements C-D1..C-D4, each hop gated:

| Hop | Gate | Failure code (closed set) |
|---|---|---|
| C-D1 (completion gate) | runs the completion lane's own `evaluate_work_package` / `evaluate_project`; a non-CP_OK scope writes **no record** | `CD_NOT_COMPLETED` (tenant scope → `CP_TENANT_MISMATCH` first) |
| C-D2 (cited set) | only SEALED artifacts with a current-valid **approving** review (disjoint reviewer, invariant-13 mirror) may be cited; a cited id outside that set → no write | `CD_NO_SEALED_APPROVED` |
| C-D3 (destination) | closed V1 vocabulary `channel` / `published_page` / `project_record` (no external publishing platforms, Root §6) | `CD_DESTINATION_INVALID` |
| C-D4 (provenance/audit) | the record carries `cp_decision_row_id` + cited review row ids + sealed artifact ids + `decided_by/at`; `Task.status`/`final_answer`/gate verdict never read | `CD_EVAL_ERROR` (unknown → fail-closed, never `CD_OK`) |

Proof (this card, scratch DB): `test_live_non_cp_ok_project_writes_no_record`, `test_live_cp_ok_scope_citing_unapproved_id_writes_no_record`, `test_live_cross_tenant_delivery_fails_before_any_write`, `test_live_re_delivery_is_idempotent` — all in the 78/78 completion+delivery run [VERIFIED-ON-MAIN this run]. Plus the two §7 E2E hops `test_completion_cp_ok_completes_project_exactly_once` (COMPLETED exactly once, C5 chain) and `test_delivery_cites_only_sealed_approving` [VERIFIED-ON-MAIN this run].

---

## 8. §7 E2E: the full chain on real execution (Root criteria 8, 10)

`fdeae189` (+ `315d405a` live wrapper; `3f0ab635` merged the fail-closed fix into the E2E base; `40169d4e` merged the F1 fix): `test_phase4_e2e_chain_acceptance.py` drives the **full** `Task → Assignment → Runtime → Run → Artifact/Evidence → Review → Rework → Re-review → Completion → Delivery` chain on a REAL scratch Postgres over the **FROZEN Phase 2F spine** — the E2E Run goes through the real `command_worker` → `node_executor` → `task_completion` → verification path; **no second runtime, no second worker/checkpoint/settlement was built** (frozen-file diff 0 add/0 delete, §1 above).

**Execution-marking discipline (not mixed):** every live test is labelled `[DB-only + mock agent]` — a deterministic LLM port injected into the real frozen spine; **no real LLM** (out of scope, labelled so). The 1-hop DB-free tier (`test_db_free_*`) guards the pure G2/G3 ledger logic without a DB. Negatives included in the chain: cross-tenant delivery fails before any write (no row in either tenant); builder-authored `kind='review'` verdict rejected (`RV_REVIEW_NOT_INDEPENDENT`, no row persisted); gate ERROR → Run terminal-failed, Task not done.

- [CARD-HANDOFF] commit-time verification: 12/12 live on `clawith_t_ab1fb86a_f073`, lane regression 110, frozen 2C-3 86/86, ruff 0.
- **This card's re-execution: 12/12 live passed on `clawith_t_c75fc916_f073`** (81.87 s) [VERIFIED-ON-MAIN this run].
- Independent review `t_a2fb3190` → **APPROVE** on the reviewer's **own** fresh scratch `clawith_t_a2fb3190_f073` (real alembic chain, execution-marking boundary + negative assertions independently re-verified) [CARD-HANDOFF].

---

## 9. Boundary confirmations (Root criteria 9, 11, 13)

- **Tenant / workspace / security:** D6 scope-inject on every ledger read/write (DAO `TenantScopedBaseDAO` contract); the re-assertion codes make cross-tenant reads fail **before** any write (`CP_TENANT_MISMATCH`, `CD_NOT_COMPLETED`). Live proof this run: `test_live_cross_tenant_delivery_fails_before_any_write` + E2E `test_cross_tenant_blocked_before_any_write` + review-lane tenant isolation (in the 32/32 `test_artifact_evidence_review.py` pass) [VERIFIED-ON-MAIN this run]. The completion lane's own cross-tenant live test (`CP_TENANT_MISMATCH` before any read/write, no C5 row for an unseen subject) was in the 78/78 pass [VERIFIED-ON-MAIN this run].
- **All 5 lane independent reviews APPROVE** (each on its own fresh scratch DB, per the Phase 3 gate convention): F1 rework `t_cb21f262` · completion `t_cc519e65` · delivery `t_7b4ad9ab` · fail-open fix `t_675b2d56` · §7 E2E `t_a2fb3190` [CARD-HANDOFF ×5].
- **main == origin/main + tag `PHASE_4_CLOSED`:** `b2cc6e0d` == `origin/main`; annotated tag @ `b2cc6e0d` "PHASE_4_CLOSED — … all 5 lane reviews APPROVED" [VERIFIED-ON-MAIN this run].
- **Execution-marking discipline not mixed:** mock/DB-only tiers labelled, DB-free tiers labelled, no real-LLM claim anywhere in the chain; no second runtime [FACT — §1 frozen diff + §8].

---

## 10. Evidence index — the 15 Root Final-Gate criteria

| # | Root criterion (t_4047050f §Final Gate) | Where evidenced in this report / on main | Class |
|---|---|---|---|
| 1 | Artifact/Evidence semantics clear (two append-only ledgers, closed sets, DRAFT→SEALED) | §2: design `b2d4ec51` §A1–§A2/§7 invariants; f072 DDL on main (`ck_*_seal` one-way, `ck_evidence_records_outcome` closed); live DDL confirmed on `clawith_t_c75fc916_f073` | FACT |
| 2 | Real provenance (content ref + hash; the 6 Root questions; D5 fail-closed at creation) | §2: design §4.2 "six Root questions, answered per record"; `ck_artifact_records_source` (D5 source XOR) live on scratch; provenance walk `rework_provenance_for_task` (`315d405a`) + E2E chain test | FACT |
| 3 | Independent Review real (reviewer-not-builder verdict; two trust boundaries; F1 rework) | §3: `PL_REVIEWER_NOT_INDEPENDENT` (frozen assignment lane) + `EV_REVIEW_NOT_INDEPENDENT` (verdict-time, DAO); F1 cycle `efaf3b17→76086d65→t_cb21f262 APPROVE`; live probe PASSED this run | FACT |
| 4 | REVIEW→REQUEST_CHANGES→Rework→Re-review chain real (superseded_by, disjoint re-review, provenance walkable) | §4: `record_rework` new rows + `superseded_by`; `payload.rework_of` disjoint re-review; G2 non-destructive (append-only DAO); 3 chain tests PASSED this run | FACT |
| 5 | Completion ≠ agent self-report (CT/CW/CP derived; Task.status/final_answer/gate verdict excluded) | §5: `completion_service.py:23,706` (never inputs); closed `CP_*` set fail-closed; E2E `test_completion_cp_ok_completes_project_exactly_once` + orphan-project CP_NO_WORK hop PASSED this run | FACT |
| 6 | Completion Gate keeps correct constraints (fail-closed CP_*; TaskCompletionGate defect closed) | §6: `147ebf93` `_fail_open→_fail_closed` on main (4 gate-error paths → outcome="fail"); gate suite 36/36 + E2E negative PASSED this run; review `t_675b2d56` APPROVE | FACT |
| 7 | Delivery real & traceable (CP_OK-gated, cites only SEALED + current-valid-approving, closed destinations) | §7: C-D1..C-D4 table; `DELIVERY_DESTINATION_KINDS` closed; 4 live negative/positive delivery tests PASSED this run; f073 single new-table migration | FACT |
| 8 | Reused Phase 2F Runtime spine (no second runtime) | §1 frozen-file diff 0 add/0 delete (8 spine files); §8 E2E Run drives the real frozen worker spine | FACT |
| 9 | Tenant / workspace / security boundaries pass | §9: D6 scope-inject; cross-tenant fails before write (live, this run); completion cross-tenant live test in 78/78 | FACT |
| 10 | Required E2E real pass (full chain, execution-marking labelled, not mixed) | §8: 12/12 live on `clawith_t_c75fc916_f073` this run; `[DB-only + mock agent]` labels in every live docstring; DB-free tier separate; review `t_a2fb3190` APPROVE | FACT |
| 11 | Independent Reviewer APPROVE (all 5 lane reviews) | §9: t_cb21f262 / t_cc519e65 / t_7b4ad9ab / t_675b2d56 / t_a2fb3190 — five APPROVE verdicts, each on its own fresh scratch DB | FACT (handoff ×5) |
| 12 | Required Rework completed (F1 rework + 2nd review; no open REQUEST_CHANGES) | §3: `76086d65` landed + 2nd review APPROVE + live probe PASSED this run; no open REQUEST_CHANGES anywhere on main (the only review-lane outcome rows are in tests) | FACT |
| 13 | Git main in sync with origin/main (+ tag) | §9: `b2cc6e0d == origin/main`, annotated tag `PHASE_4_CLOSED` @ `b2cc6e0d`, re-verified this run | FACT |
| 14 | Convergence Report complete | This document (`t_c75fc916`), committed on a docs-only branch off main; every load-bearing execution claim re-burned on this card's own scratch DB (§0 record) | FACT |
| 15 | Final Gate PASS | **DEFERRED to `t_ebfd863e`** (independent adjudicator, aco-reviewer). §11 below recommends PASS; the gate owns the verdict. | DEFERRED |

**Commit SHAs + test counts cited in this report (the gate's verification list):**

- Design docs: `b2d4ec51`, `8448b7ba`, `ac10c1a4`; audit `7a8a389a`.
- F1 cycle: REJECT `efaf3b17` (decision doc on main) → rework `76086d65` → 2nd review `t_cb21f262` APPROVE [CARD-HANDOFF].
- Lanes: `38ce01fc` (40/40 at commit) · `581419dc` (41/41) · `9ffb1095` (37/37) · `147ebf93`+`0ebf7358` (74/74 gate suite at commit) · `315d405a` (6/6 live + G3 wrapper) · `fdeae189` (12/12 live at commit).
- Merges to main: `8280d2d3` (feature line, +10122/−9), `21d9df6f`, `fbee43a0`, `dd458a48`, `f1b8e529`, `b2cc6e0d`.
- **This card's re-execution on `clawith_t_c75fc916_f073` (fresh scratch, §0):** alembic chain exit 0 / head `f073_delivery_records`; E2E 12/12 (81.87 s); F1 live probe PASSED; review lane 32/32; gate suite 36/36; completion+delivery live 78/78; frozen spine regression 135 passed / 11 skipped (22.24 s).

---

## 11. Residual risks / UNKNOWNs (named, not hidden)

- **Lane test counts are [CARD-HANDOFF] where not re-burned.** The per-lane commit-time counts (40/40, 41/41, 37/37, 74/74, 86/86) are from lane/review handoffs; this card re-burned the E2E tier, the F1 live probe, the gate suite, the review lane, and the completion/delivery live tiers (§0). The E2E commit's "lane regression 110" number is NOT re-burned here — unknown if it still reproduces; the gate can re-run it.
- **`clawith_t_e35ab093_f073_1791062555` scratch DB left on the native PG16** (the LAND card's pre-push check DB; DROP requires user approval in single-query mode; it is an isolated clawith-owned scratch DB, not a product DB) [CARD-HANDOFF].
- **Live-DB questions out of scope** (whether any external consumer outside `backend/app` marks a Project `COMPLETED` — the audit found none in-repo; whether live `Task` rows carry `final_answer` values that any lane reads — none do, by construction) — re-probe against production if a future phase needs them.
- **Deferred-by-design, named in the three design docs §8** ("Deferred, Not Dropped"): delivery RECALLED/supersession state; external publishing platforms; real-LLM E2E tier (the [DB-only + mock agent] boundary is explicit, not a gap); runtime-parallelism caps (Phase 3 D-2 carry-over). These are non-defects.

## 12. Recommendation to the Final Gate

**Recommend: PASS.**

Criteria 1–10, 12–14 are met by landed evidence on `b2cc6e0d` with the load-bearing execution claims re-burned on this card's own scratch DB (§0/§10). Criterion 11 rests on the five independent APPROVE handoffs (each reviewer on its own fresh DB — the Phase 3 gate convention). Criterion 15 is the gate's own call.

The Final Gate (`t_ebfd863e`) should independently re-run the E2E chain + the F1 live probe + the gate suite on its **own** fresh scratch Postgres (per its card body: "do not accept the workers' DBs as truth"), adjudicate all 15 criteria against the live diff + `alembic heads`, and if it returns PASS, close Root `t_4047050f`. **This card does not adjudicate; the gate owns the verdict.**

---

*Convergence report authored by aco-builder, task `t_c75fc916`, at `main = b2cc6e0d` (tag `PHASE_4_CLOSED`). Self-re-verification this run: §0 table, all on scratch `clawith_t_c75fc916_f073` (provisioned by `backend/_provision_f073_c75fc916.py`, the real alembic chain, never `create_all`). Source-of-truth commits: design `b2d4ec51`/`8448b7ba`/`ac10c1a4`; audit `7a8a389a`; lanes `38ce01fc`/`76086d65`/`581419dc`/`9ffb1095`/`147ebf93`/`0ebf7358`; E2E `315d405a`/`fdeae189`; F1 decision `efaf3b17`; merges `8280d2d3`/`21d9df6f`/`fbee43a0`/`dd458a48`/`f1b8e529`/`b2cc6e0d`; Phase 3 precedent `38048791`.*
