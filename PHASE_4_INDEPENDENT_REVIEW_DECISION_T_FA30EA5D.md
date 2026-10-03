# Phase 4 — Independent Review & Acceptance Decision (t_fa30ea5d)

Reviewer: aco-reviewer · Independent gate for Root `t_4047050f`
Reviewed commit: `38ce01fc` on `wt/t_436ddafb` (base `8f030792` PHASE_3_CLOSED)
Baseline: main == origin/main == `8f030792`, tag `PHASE_3_CLOSED`
Decision: **REJECT (request changes)** — 1 HIGH finding (F1). All other
dimensions APPROVE. Rework is bounded and specified below; the storage
foundation is sound and ready.

---

## 0. Formal verdict

| Dimension | Result |
|---|---|
| Artifact / Evidence generation (guarantee #1) | **PASS** |
| Rework state machine (guarantee #3) — supersession/non-destructive reads | **PASS** |
| Independent Reviewer prevents self-validation (guarantee #2 / G4) | **FAIL — F1 (HIGH)** |
| Tenant isolation on all reads/writes (guarantee #4 / D6 / I-7) | **PASS** |
| Code strictly matches the three design docs | **FAIL on §3.3 RE_REVIEW actor + G4 / invariant-13 (F1); otherwise conforms** |
| Phase 2C-3 frozen regression | **PASS (no regression)** |
| Additive-only boundary (no frozen file modified) | **PASS** |
| Lane-boundary hygiene (completion/delivery/fail-open out of scope) | **PASS** |

The single HIGH finding is on the *very* guarantee Phase 4 exists to
establish — "the reviewer must not self-review." It is execution-confirmed
and design-material, so the gate output is **REJECT**, not a pass-with-notes.
The fix is small and does not require re-doing the (excellent) storage layer.

---

## 1. Provenance (what was independently verified, on the reviewer's OWN scratch DB)

Every claim below was re-run by the reviewer on a **fresh scratch Postgres
16 DB `aco_p4_gate1`** (distinct from the builder's `clawith_t_436ddafb_f072`),
per the Phase 3 gate convention — the builder's self-reported results were NOT
trusted as the basis for acceptance.

- **Real migration chain:** `alembic upgrade head` against `aco_p4_gate1` →
  full `f060 … f072` transition, **exit 0**, single head
  `f072_artifact_evidence` stamped. (Repro: `backend/_provision_f072.py` or
  main-tree venv + `PYTHONPATH=worktree backend`.)
- **DDL invariants landed live** on `aco_p4_gate1`:
  - `ck_artifact_records_source` (inv 1, D5 XOR),
    `ck_artifact_records_seal` (inv 2, one-way seal),
    `ck_evidence_records_source` (inv 3),
    `ck_evidence_records_outcome` — all present.
  - `uq_artifact_records_tenant_ref` UNIQUE (inv 4, dedup) — present.
  - `uq_evidence_records_reverify` partial UNIQUE (inv 5) — present,
    `WHERE (NOT COALESCE(payload ? 'reverify_of', false))` exactly as designed
    (AgentRunEvent partial-unique precedent).
  - 5 plain btree indexes per table (`ix_*_tenant_id/project_id/task_id/
    execution_id/revision_ref`).
- **Test suites, run by the reviewer on `aco_p4_gate1`:**
  - `tests/test_artifact_evidence_review.py` + `tests/test_artifact_evidence_migration.py`
    → **40 passed, 0 skipped** (the live-schema tier executed, not skipped).
  - Focused frozen Phase 2C-3 set
    (`test_planning_dao`, `test_planning_service`,
    `test_planning_persistence_migration`, `test_assignment_service`,
    `test_agent_runtime_task_completion`, `test_agent_runtime_planning`)
    → **86 passed, 0 skipped.** (The builder's handoff said "50 pass/4 skip";
    the reviewer's selection is a broader superset of the frozen set and all
    pass — **no regression**.)
- **Lint / type:** `ruff check` + `pyright` on the 6 new source files →
  **0 errors, 0 warnings, 0 informations** (both clean).
- **Regression boundary (git):** `38ce01fc` vs `8f030792` touches
  **exactly 8 files** — `M backend/alembic/env.py` (a single additive import of
  `ArtifactRecord, EvidenceRecord`, needed for the f068/f069 create_all
  lockstep) + 7 new `A` files. **No frozen model/service is modified**; the
  lane boundaries hold: completion lane (`CP_*`/`CD_*`/`CT`/`CW`/`CP`/the
  `COMPLETED` write site) is **absent** (correctly routed to `t_e399386f`),
  the delivery record is **absent** (belongs to this lane), and the
  `TaskCompletionGate._fail_open` defect is **not folded in** (only a
  docstring reference at `review_rework_service.py:16`).

---

## 2. Findings

### F1 — HIGH — Rework lane lets the BUILDER author the `kind='review'`
re-review verdict row, bypassing invariant-13 (G4 role-boundary violation)

**Where:**
- `backend/app/services/review_rework_service.py:359-369` — `plan_rework`
  constructs `rework_review = EvidenceRecord(kind="review",
  outcome=re_review_outcome, …, created_by_agent=builder_agent_id)` (line 369).
- `backend/app/services/review_rework_service.py:676-689` — `record_rework`
  persists that row via `add_evidence(plan.rework_review, tenant_id=…, db=…)`
  **without** passing `reviewer_builder_agents`, so the invariant-13 guard
  (`EV_REVIEW_NOT_INDEPENDENT`) is not consulted on this path.
- `backend/tests/test_artifact_evidence_review.py:370-404`
  (`test_rework_links_new_rows_and_carries_rework_of`, `re_review_outcome="pass"`)
  asserts the builder-authored `re_review` as expected behavior, i.e. the
  defect is tested into the acceptance.

**Why it is a problem (design contract):**
- Parent design `PHASE_4_INDEPENDENT_REVIEW_REWORK_V1_DESIGN.md`:
  - §3.3 transition table, row `RE_REVIEW→APPROVED`: **Actor = "Reviewer R"**
    (the disjoint reviewer); the fail-closed code for that transition is
    literally `EV_REVIEW_NOT_INDEPENDENT`.
  - §4 role-boundary table: "Writes a `kind='review'` verdict row — **Builder:
    ✗ forbidden (invariant 13)** | Reviewer: ✓".
  - G4 (Builder/Reviewer boundary): "neither holds the other's write
    authority … **the Builder produces provenance; the Reviewer consumes it and
    writes exactly one verdict row**; the Reviewer's only trusted inputs are
    the ledgers … never the Builder's self-report."
  - Parent invariant 13 (`PHASE_4_ARTIFACT_EVIDENCE_V1_DESIGN.md` §7.13):
    "a `kind='review'` row whose `created_by_agent` is a builder on the same
    WorkPackage is **rejected** with `EV_REVIEW_NOT_INDEPENDENT`."
- The rework step (REQUEST_CHANGES → REWORKING) is where the **builder** writes
  *new `artifact_records` + new `test_result`/`file_revision` evidence +
  `superseded_by` links* (design §3.3 `REQUEST_CHANGES→REWORKING` row,
  Actor = Builder). The **re-review verdict row** (RE_REVIEW) is a *separate
  disjoint-Reviewer act*. The implementation conflates the two and stamps the
  verdict with the builder's own agent id.

**Execution evidence (reviewer's probe, on `aco_p4_gate1`):**
```
record_rework code            : RV_OK
re_review.kind                : review
re_review.outcome             : pass
re_review.created_by_agent    : f46fc94c-… == builder? True
re_review.rework_of           : 203cbe89-… == fail.id? True
builder-authored kind='review' rows: 1   <-- G4 / invariant-13 violation
guard-with-builder-set        : rejected -> EV_REVIEW_NOT_INDEPENDENT
                               (the guard works when the builder set is passed)
```
The identical builder-authored review row **is** rejected when
`reviewer_builder_agents={builder}` is passed (DAO guard, `artifact_evidence_dao.py:324-332`
works); `record_rework` simply never passes it for the re-review row.

**Blast-radius / severity rationale (kept honest):**
- Contained at the *completion* layer **by design**: the completion predicate's
  independence term (`CP_REVIEW_NOT_INDEPENDENT`, read-side assertion) would
  reject a builder-authored current-valid review. So completion stays
  fail-closed even with this row present.
- NOT acceptable as a V1 gate result because: (a) it violates a *named,
  stated* guarantee (G4 / invariant 13) that is the Phase 4 core; (b) the
  rework lane reports `RV_OK` and a `re_review` stamped `created_by_agent=
  builder`, affirming a self-review as "the" re-review — precisely "自己审自己"
    the Root brief forbids; (c) `rework_provenance().re_review` surfaces that
    builder-authored row as the authoritative re-review to any consumer;
    (d) the public API accepts `re_review_outcome="pass"`, inviting a builder
    to record its own approving verdict. This is a layering + role-boundary
    defect on the primary capability, hence **HIGH** (not a cosmetic note).

**Required fix (minimal, design-conformant — route to the builder card):**
1. `plan_rework` / `record_rework` must **NOT** author a `kind='review'`
   verdict row. Rework ends at "new artifacts + new evidence + `superseded_by`
   links" (the REWORKING → RE_REVIEW handoff; §3.3 says the builder writes
   nothing new at RE_REVIEW — "the new set is current"). Drop the
   `rework_review` builder-authored `EvidenceRecord` (line 369) and the
   re-review `add_evidence` call (line 679).
2. The re-review verdict is recorded by the **disjoint reviewer** via the
   existing `record_review(…, rework_of=fail_row_id, …)` path, which already
   passes `reviewer_builder_agents=set(builder_agent_ids)` (line 584) and
   enforces invariant-13. Ensure `plan_review`/`record_review` accepts
   `rework_of` on the re-review verdict (it does, lines 261-262) so the
   §3.3 `RE_REVIEW→APPROVED` row (carry `payload.rework_of`) is satisfied by
   the reviewer, not the builder.
3. Keep `payload.rework_of` on the *reviewer's* re-review row (I-5/G3) — the
   provenance link is preserved; only the **actor** changes from builder to
   the disjoint reviewer.
4. Update `test_rework_links_new_rows_and_carries_rework_of` to assert that
   rework does NOT write a review row, and add a test that a reviewer's
   `record_review(…, rework_of=fail.id, …)` on the new set is accepted while
   a builder-authored `kind='review'` row on the same WP is rejected
   (`EV_REVIEW_NOT_INDEPENDENT`).
5. Re-run the 40-test Phase 4 suite + the focused frozen set; re-review by
   aco-reviewer on the rework commit.

Alternative (if the design owner intends a builder-written "rework marker"):
that marker must NOT be `kind='review'` (the verdict kind); it should be
`kind='structured'` with `payload.rework_of` + `payload.decision='rework_opened'`,
so it never pollutes the `current_valid_review` query or the invariant-13
independence assertion. Either way, a *builder* must not be able to produce
an approving `kind='review'` verdict.

---

## 3. What is APPROVED (ready, no rework needed)

- `backend/app/models/artifact_evidence.py` — two ledger tables, closed sets,
  the three CHECKs + the partial-unique reverify index, `__tenant_scoped__`,
  additive-only. Matches `PHASE_4_ARTIFACT_EVIDENCE_V1_DESIGN.md` §3/§5/§7
  strictly (invariants 1-7 + 11-13 as DB/DAO backstops).
- `backend/app/dao/artifact_evidence_dao.py` — append-only, tenant-scoped
  (`TenantScopedBaseDAO`), closed-set fail-closed validation, the DRAFT-only
  update + one-way seal boundary, `current_valid_review` (§3.4 query, G2),
  the invariant-13 verdict-time guard, bounded payload (I-8), test_result
  execution invariant (I-12). No delete path (invariant 7).
- `backend/alembic/versions/v1_11_5_f072_artifact_evidence.py` — DDL-only,
  single head off `f071`, guarded up/down, index lockstep with the model, no
  frozen-table column/constraint added. Verified live on `aco_p4_gate1`.
- `backend/app/services/artifact_evidence_resolver.py` — additive
  `artifact://` / `evidence://` deterministic readers; delegates every
  non-ledger scheme to the frozen reader; `artifact://` requires the row to be
  *current* (not superseded); wrapped in `tenant_context`. Matches design §4.1/§5.2.
- `backend/app/services/review_rework_service.py` — the pure derivation core
  (`current_valid_review`, `build_execution_evidence`, `rework_provenance`,
  `plan_review` incl. the disjointness gate + payload bound + seal
  semantics) is correct. **Only the `plan_rework`/`record_rework` verdict-row
  authorship (F1) is defective; the rest of the service is sound.**
- Delivery-criteria contract (`PHASE_4_COMPLETION_DELIVERY_CRITERIA_V1_DESIGN.md`
  C-D1..C-D4 / `CD_*`) is consumed by *this* lane and is not implemented yet;
  that remains this card's downstream scope (no `CD_*`/delivery-record code is
  present in `38ce01fc`, which is correct — the completion lane was routed to
  `t_e399386f`).

---

## 4. Gate disposition

- **Root Final Gate (t_4047050f): do NOT pass** while F1 is open (it is one of
  the 15 Root criteria — "Independent Review 已真实建立" / criterion 3, and the
  no-self-review hard boundary).
- Route F1 back to **aco-builder** as a bounded rework child; the rework
  commit requires a **second independent review** by aco-reviewer before the
  Root gate may re-adjudicate.
- No other card should absorb the `TaskCompletionGate._fail_open` fix (Root
  card §5) — correctly out of scope here; that remains its own fix task.

*Decision produced by aco-reviewer on t_fa30ea5d, at main = 8f030792
(PHASE_3_CLOSED), independently verified on scratch Postgres `aco_p4_gate1`.*
