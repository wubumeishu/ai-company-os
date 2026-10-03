# Phase 4 — Independent Review & Rework: V1 State-Machine Design (t_dbb0c0dd)

Task: `t_dbb0c0dd` — "Design Independent Review & Rework state machine"
Author: aco-architect
Baseline: main == origin/main `8f030792`, tag `PHASE_3_CLOSED` — designed in worktree
`wt/t_dbb0c0dd`
Storage contract (consumed, not re-invented):
`docs/architecture/PHASE_4_ARTIFACT_EVIDENCE_V1_DESIGN.md` (t_f19aae89, commit
`b2d4ec51`) — the two append-only ledger tables `artifact_records` /
`evidence_records`, their closed `SEAL_STATUSES` / `kind` / `outcome` sets,
invariants 1–13, and §11 handoff to this card.
Source-audit inputs (read-only, traced): `PHASE_4_AUDIT_REPORT_T15e05452.md`
(t_15e05452, commit `7a8a389a`) Q5/Q7/Q12/Q13.
Live-tree anchors re-verified at this baseline:
`assignment_service.py:202/334-441` (`ReviewBinding`, REV-1/2),
`assignment_service.py:94-96` (`PL_REVIEWER_NOT_INDEPENDENT`),
`planning.py:296-301` (`WorkPackage.requires_independent_review`),
`task.py:58-62` (`Task.status` 3-state frozen enum),
`task_completion.py:56/137-140` (`TaskRuntimeCompletionHandler`),
`verification.py:622-704/920-1032` (`TaskCompletionGate`, fail-open defect at
`634-642`), `verification.py:92-99` (`_completion_evidence`).

Style/precedent: same classification discipline, boundary-matrix shape,
closed-result-code posture, and "no duplicate without justification" rule as the
parent design doc and Phase 3 `planning` design D5.

## Classification Discipline

Carried over from the parent doc and Phase 2C–4 docs:

- **FACT** — directly observable in this tree (file:line, named constant, migration id).
- **OBSERVATION** — a pattern seen in one or more concrete instances, no universal claim.
- **INFERENCE** — a conclusion drawn from the facts above.
- **UNKNOWN** — not verifiable from this tree; the resolving action is stated.

The design itself is normative (what the system *should* be). Normative prose is
unlabeled; all claims about the *current* system carry a class.

---

## 1. Verdict (up front)

The audit found that "review" today is **assignment-time agent
disjointness only** (Q5 [FACT/OBS]): `ReviewBinding` REV-1 (the reviewer agent is
disjoint from every builder agent, `assignment_service.py:400-417`) + REV-2 (the
reviewer *task* blocks the builder tasks via the DAG, `check_review_dag`
`:424+`), gated by the frozen `WorkPackage.requires_independent_review` flag
(`planning.py:296-301`). `REQUEST_CHANGES` has **no real semantics** today — it
is a docstring mention only (Q7 [OBS]); there is **no rework / re-review
mechanism** (Q8 [OBS]). Task "done" is written by
`TaskRuntimeCompletionHandler` the moment the Run reports `completed`
(`task_completion.py:137-140`) — i.e. **Agent-reported success is currently
treated as Task completion** (Root §5's forbidden equivalence, Q10 [FACT/INF]).

This card delivers the requested **state machine + logic specification** for
`Execution → Review → APPROVE/REQUEST_CHANGES → Rework → Re-review → Completion`
as a **derived-state lifecycle** — **not a new persisted state machine, not a
new table, not a new column.**

> **Design shape, one line:**
> The Review/Rework lifecycle is a **set of guarded transitions between
> *derived* states** (EXECUTING / IN_REVIEW / REQUEST_CHANGES / REWORKING /
> RE_REVIEW / APPROVED-SEALED / COMPLETED) that is **computed live from the two
> frozen ledger tables** the parent design owns —
> `artifact_records.superseded_by` + `evidence_records.kind='review'` —
> plus one **frozen** assignment-time independence fact
> (`ReviewBinding` REV-1/2) and one **new** verdict-time independence guard
> (`EV_REVIEW_NOT_INDEPENDENT`). The narrative state machine is the *spec*;
> storage stays derived. No schema change. No new SM entity (D4 kept).

Why derived, not persisted (reconciles the task's "state machine" ask with the
parent D4 + AGENTS.md §2 "a new state machine needs an independent owner and
need"):

1. **No independent object.** A "review state" is not a durable object with its
   own authoritative transitions — it is *the current disposition of a Task's
   artifact+evidence set*, and that set already has exactly one owner: the two
   ledger tables. A persisted `review_status` column would be a second authority
   over a fact the ledgers already answer, violating the one-authority rule.
2. **The "current valid review" question is already a clean query** over
   `evidence_records.kind='review'` + `artifact_records.superseded_by`
   (parent §11): "the latest `kind='review'` row over the *current*
   (non-superseded) artifact set". Computing the derived state from that query is
   O(1) reads, no writes, no write-path to drift.
3. **Phase 3 precedent.** `planning` D5 already chose closed result-code sets
   over workflow SMs for the same reason (Root AGENTS.md §2). D4 of the parent
   doc restates it for artifact/evidence. This card extends the same
   restraint to the review/rework layer.

The seven *narrative* states below are **labels** a consumer (UI, gate,
completion lane) may compute and show; they are **not** a persisted enum. The
*transitions* are the normative part of this design (each is a guarded write to
the frozen ledgers). Decisions that bound the whole design:

| # | Decision | Authority |
|---|---|---|
| R0 | The lifecycle is a **derived state machine over the two parent-ledger tables** — no new table, no new column, no persisted status. The narrative states (EXECUTING … COMPLETED) are computed, never stored. | Root AGENTS.md §2 "new SM needs independent owner + need"; parent D4; audit Q8 "no rework/re-review mechanism" (nothing to migrate) |
| R1 | A **Review** is one `evidence_records` row `kind='review'`, `outcome ∈ {pass, fail, inconclusive}`, `created_by_agent` = the **disjoint** reviewer (invariant 13 / REV-1/2). A verdict is the *only* durable record a review adds — zero new mechanism. | parent §11 handoff; audit Q5 [OBS] "no review-verdict entity" |
| R2 | **Independence is two-layered, and both layers are frozen-or-new-guard, never re-implemented:** (a) *assignment-time* disjointness = existing `ReviewBinding` REV-1/2 (`assignment_service.py:334-441`, fail-closed `PL_REVIEWER_NOT_INDEPENDENT`); (b) *verdict-time* guard = the parent invariant-13 rejection code `EV_REVIEW_NOT_INDEPENDENT` (a `kind='review'` row whose `created_by_agent` is a builder on the same WorkPackage is rejected at insert). "No self-review" is enforced at **both** the plan boundary and the write boundary. | audit Q5 (assignment disjointness exists) + parent invariant 13 (verdict-time) |
| R3 | **APPROVE** = reviewer writes a `kind='review'` row `outcome='pass'` **AND** the lane seals the cited current `artifact_records` rows `DRAFT→SEALED` (invariant 10: sealing is the only mutation, one-way). APPROVE is a **disjoint reviewer's** evidence row + a one-way seal — a deliberately **different code path** from `TaskCompletionGate._fail_open` (the flagged out-of-scope defect, `verification.py:634-642`): the APPROVE path writes a proof row; the gate's LLM path never does. | parent §11 (APPROVE → seal); Root §5 (fail-open defect is a separate fix task, not absorbed here) |
| R4 | **REQUEST_CHANGES** = reviewer writes a `kind='review'` row `outcome='fail'` with `payload.critiques` (bounded) + `payload.required_changes`. It adds **no** new entity; the *consequence* is a new Rework (R5). "REQUEST_CHANGES" therefore acquires real semantics for the first time (closes audit Q7). | audit Q7 [OBS] "no real semantics" |
| R5 | **Rework** = the Builder produces **new** `artifact_records` rows (each `superseded_by`-free, i.e. new/current) whose `superseded_by`-predecessor is the old row — i.e. new rows that **supersede** the old ones, plus new `test_result` / `file_revision` `evidence_records`. The old review row + old artifacts are **never touched** (R6). | parent D2 (rework = new row + `superseded_by` link); Root Phase 4 §4 "do not overwrite history" |
| R6 | **Re-review** = a *new* `kind='review'` row over the *new* (non-superseded) artifact set, whose `payload.rework_of = <the REQUEST_CHANGES review row id>` (and `payload.reverify_of` reused only for pure re-verification of an *unchanged* subject, not rework). Every review/re-review is individually traceable; history is never deleted. | parent §5.2 (`reverify_of`), §11 (`current valid review`); Root §4 "re-review relates to the original review" |
| R7 | **Provenance link Rework → the specific REQUEST_CHANGES** is carried **in-storage, no new column**: `evidence_records.payload.rework_of` (the failed review row) on the re-review row + the `artifact_records.superseded_by` chain. Zero schema change; the parent's "rework row carries a link to the triggering decision" (§5.2) is exactly this. | parent §5.2 / §11; Root §4 "Rework has explicit provenance" |
| R8 | **Completion** is **not** `Task.status='done'` (Run-reported, `task_completion.py:137-140`). Completion = a **separate derived lane decision**: "current SEALED artifact set + current approving `kind='review'` evidence row + dependency completion + tenant boundary" (Root §5, parent §11). `Task.status` stays frozen as the *Run* fact; the review lane never trusts it and the completion lane never writes `done` from `final_answer`. | Root §5 hard boundary; audit Q10 [FACT/INF]; parent §11 |

---

## 2. The Four Guarantees, Mapped to Concrete Mechanisms

The task's four explicit guarantees, each satisfied by a *named* mechanism
(existing or this-design), never by an unenforced prose promise:

| Guarantee | Mechanism | Where enforced |
|---|---|---|
| **G1 Reviewer independence (no self-review)** | Two-layer: (a) `ReviewBinding` REV-1 disjoint agent pick + REV-2 DAG block edge (frozen, `assignment_service.py:334-441`); (b) verdict-time `EV_REVIEW_NOT_INDEPENDENT` rejection (parent invariant 13) on a `kind='review'` row written by a builder agent on the same WP. | plan boundary (a) + insert boundary (b) |
| **G2 Non-destructive history (old Reviews/Evidence never overwritten)** | Both ledgers are **append-only, no delete path** (parent invariants 7, 2: `SEAL_STATUSES` one-way, DAO exposes no delete). A rework writes **new** rows + `superseded_by` links; the old `kind='review'` row and old `artifact`/`evidence` rows remain queryable forever. | DAO (structural) + D2 one-way boundary |
| **G3 Traceable provenance linking Rework to the specific REQUEST_CHANGES** | `evidence_records.payload.rework_of = <failed review id>` on the re-review row, plus the `artifact_records.superseded_by` chain, plus `payload.critiques`/`required_changes` on the REQUEST_CHANGES row itself. | storage (in `payload`, bounded ≤32 KiB) — no new column |
| **G4 Clear Builder / Reviewer role boundary** | Builder = producer of `artifact_records` + `test_result`/`file_revision` `evidence` rows, runs the rework execution, may *seal nothing* (sealing is review-driven, R3). Reviewer = the disjoint agent that reads ledgers + `Task` acceptance criteria + real source/tests and writes the single `kind='review'` verdict row. They share no write authority: builder cannot write a `kind='review'` row; reviewer cannot write `artifact_records` or re-run builder code. The builder's `final_answer` / `Task.status='done'` is **never** an input the reviewer may cite as proof (audit Q13 "Reviewer 不得直接信 Builder 自报"). | role split in §4 + invariant 13 |

---

## 3. Derived State Machine

### 3.1 State definition (computed, not stored)

For one **Task** `T` (with its current Work Package `W`, reviewer `R`, and the
frozen flag `W.requires_independent_review`):

Let
- `A_c(T)` = `artifact_records` of `T` with `superseded_by IS NULL` (the
  **current** artifact set),
- `A_c_sealed(T)` = `A_c(T)` with `seal_status='SEALED'`,
- `V(T)` = `evidence_records.kind='review'` rows whose cited `artifact_id` ∈
  `A_c(T)`,
- `V_cur(T)` = the **latest** (by `created_at`) row in `V(T)` — the
  "current valid review",
- `deps_done(T)` = every transitive dependency of `T` (via `task_dependencies`,
  frozen Phase 2D) is in a completed state,
- `tenant_ok(T)` = `T`, its `A_c(T)`, and `V_cur(T)` are all in one
  `tenant_id` (DAO scope-inject, parent D6).

The derived state is:

| State | Definition (derived predicate) |
|---|---|
| **EXECUTING** | the builder run for `T` (or its rework run) is in progress — `Task.status ∈ {pending, doing}` and `V_cur(T) = ∅` |
| **IN_REVIEW** | builder artifacts exist and are submitted; `V(T) ≠ ∅` and no verdict yet this cycle, i.e. a disjoint reviewer `R` is reading `A_c(T)` / acceptance criteria / source / tests |
| **APPROVED** | `V_cur(T).outcome = 'pass'` **and** `A_c(T) ⊆ A_c_sealed(T)` (the approving review sealed the current set, R3) |
| **REQUEST_CHANGES** | `V_cur(T).outcome = 'fail'` (a REQUEST_CHANGES row, R4); `A_c(T)` **not** sealed; `W` carries `payload.required_changes` |
| **REWORKING** | `T` has ≥1 REQUEST_CHANGES row and the builder has started (or produced) a *new* superseding artifact set — a new row whose predecessor is `superseded_by`-linked; `A_c(T)` is now the new set, unsealed |
| **RE_REVIEW** | the new `A_c(T)` exists; a new disjoint review is in flight (will write a `kind='review'` row with `payload.rework_of = <the REQUEST_CHANGES id>`) |
| **COMPLETED** | `V_cur(T).outcome='pass'` **and** `A_c(T) ⊆ A_c_sealed(T)` **and** `deps_done(T)` **and** `tenant_ok(T)` (R8) — the completion-lane predicate; distinct from `Task.status='done'` |
| **BLOCKED** | `W.requires_independent_review` and REV-1/2 has no disjoint candidate (`PL_REVIEWER_NOT_INDEPENDENT`) — review cannot start; or `V_cur(T).outcome='inconclusive'` awaiting re-verification |

> Note on `inconclusive`: `outcome='inconclusive'` is a *re-check* signal, not a
> verdict. It parks the Task in **BLOCKED** (re-verify the cited refs via the
> deterministic resolver, parent §5.2) and must **not** satisfy a completion or
> delivery gate (fail-closed; Root "fail-closed" requirement §5).

### 3.2 State-machine diagram (the requested deliverable)

```mermaid
stateDiagram-v2
    [*] --> EXECUTING : builder run for Task T

    EXECUTING --> IN_REVIEW : artifacts A_c(T) submitted (DRAFT)
    IN_REVIEW --> APPROVED : reviewer R (disjoint) writes review outcome=pass
                             + seals A_c(T) DRAFT->SEALED [APPROVE, R3]
    APPROVED --> COMPLETED : deps_done & tenant_ok  [completion lane, R8]
    COMPLETED --> [*]

    IN_REVIEW --> REQUEST_CHANGES : reviewer R writes review outcome=fail
                                     + payload.required_changes [R4]
    IN_REVIEW --> BLOCKED : outcome=inconclusive (re-verify cited refs)

    REQUEST_CHANGES --> REWORKING : builder starts rework; new artifact rows
                                     supersede old via superseded_by [R5]
    REWORKING --> RE_REVIEW : new A_c(T) ready; new disjoint review starts
    RE_REVIEW --> APPROVED : reviewer writes review outcome=pass,
                             payload.rework_of=<the REQUEST_CHANGES id>,
                             seals new A_c(T) [R3+R6]
    RE_REVIEW --> REQUEST_CHANGES : reviewer writes outcome=fail again
                                     (new required_changes; new row, G2)
    RE_REVIEW --> BLOCKED : outcome=inconclusive

    BLOCKED --> IN_REVIEW : re-verification yields a determinate verdict
    APPROVED --> REWORKING : (late) a change supersedes a sealed artifact ->
                             new review cycle (old APPROVE never clobbers, G2)
    note right of APPROVED
        APPROVED is re-enterable from RE_REVIEW;
        each entry is a new kind='review' row + a one-way seal.
        "Old APPROVE" rows remain; the current-valid-review query
        reads the latest row over the current (non-superseded) set.
    end note
```

Legend for guards:
- **seal** = `artifact_records.seal_status DRAFT→SEALED` (invariant 10, one-way).
- **review row** = one `evidence_records` insert, `kind='review'`,
  `created_by_agent = R` (invariant 13 disjointness).
- **supersede** = insert a new `artifact_records` row; the replaced row gains
  `superseded_by = <new id>` (D2 link; the *new* row is current, the old row is
  historical — never deleted).

### 3.3 Guarded transition table (the normative logic spec)

Each transition is an atomic, service-enforced write to the frozen ledgers.
"Cites" = the row's `artifact_id` / `execution_id` / `revision_ref` /
`subject_ref` are non-empty (fail-closed, D5).

| Transition | Precondition | Actor | Writes (frozen ledgers only) | Postcondition | Fails-closed code |
|---|---|---|---|---|---|
| EXECUTING→IN_REVIEW | `A_c(T)` non-empty; each current artifact has a provenance edge | Builder (producer) | `artifact_records` DRAFT rows exist; optional `test_result` / `file_revision` `evidence_records` | `A_c(T)` queryable; no review row yet | `EV_NO_SOURCE` / `EV_NO_SUBJECT` / `AE_UNKNOWN_SCHEME` on bad refs |
| IN_REVIEW→APPROVED | `W.requires_independent_review ⇒ R` disjoint from all builders (REV-1/2); `R` is the assigned reviewer agent | Reviewer `R` | 1× `evidence_records kind='review' outcome='pass'` (payload: verdict + cited `artifact_id`s + `revision_ref`), `created_by_agent=R`; then seal `A_c(T)` `DRAFT→SEALED` | `V_cur(T).outcome='pass'` ∧ `A_c(T)⊆sealed` | `EV_REVIEW_NOT_INDEPENDENT` (R is a builder on W); `AE_ALREADY_SEALED` (double-seal); `EV_NO_SOURCE` |
| IN_REVIEW→REQUEST_CHANGES | same independence precondition | Reviewer `R` | 1× `evidence_records kind='review' outcome='fail'` + `payload.critiques`, `payload.required_changes` (≤32 KiB total) | `V_cur(T).outcome='fail'`; `A_c(T)` stays DRAFT | same as above; `EV_PAYLOAD_OVERRUN` if critiques exceed bound |
| IN_REVIEW→BLOCKED | cited refs unresolved | Reviewer `R` | 1× `evidence_records kind='review' outcome='inconclusive'` (payload: which refs + why) | parked for re-verification | `EV_NO_SOURCE` on a bad ref; resolver returns `verify_failed` |
| REQUEST_CHANGES→REWORKING | `A_c(T)` DRAFT; builder has a disjoint execution budget | Builder | new `artifact_records` rows; for each replaced old row, set `superseded_by=<new id>`; new `test_result`/`file_revision` `evidence` | `A_c(T)` is now the new set; old rows retained, linked | `AE_ALREADY_EXISTS` idempotent-cite (parent §10); rework **must** produce new evidence (Root §4) — `EV_NO_SOURCE` if the new set re-cites a stale artifact with no new proof |
| REWORKING→RE_REVIEW | new `A_c(T)` ready | Builder | (no new write; the new set is current) | a new disjoint review may start | — |
| RE_REVIEW→APPROVED | independence; re-review cites the **new** `A_c(T)` | Reviewer `R` | 1× `kind='review' outcome='pass'` + `payload.rework_of=<REQUEST_CHANGES id>`; seal new `A_c(T)` | APPROVED re-entered; the REQUEST_CHANGES row + old set remain queryable (G2, G3) | `EV_REVIEW_NOT_INDEPENDENT`; `AE_ALREADY_SEALED` |
| RE_REVIEW→REQUEST_CHANGES | — | Reviewer `R` | 1× `kind='review' outcome='fail'` + `payload.rework_of` + new `required_changes` | a further cycle; history accumulates, never overwritten | same |
| APPROVED→COMPLETED | `V_cur(T).outcome='pass'` ∧ `A_c(T)⊆sealed` ∧ `deps_done(T)` ∧ `tenant_ok(T)` | Completion lane (separate card/lane, §6) | **no ledger write by the completion lane itself** — it *reads* the two ledgers; it may set `Task.status='done'` only as the Run fact, but **Completion is the read-side predicate, not `Task.status`** | COMPLETED | fail-closed: any missing predicate ⇒ not completed (Root §5 "fail-closed") |

### 3.4 The "current valid review" query (identifiable, no history rewrite)

The task requires "能识别当前有效 Review" (identify the current valid review).
With no new column this is a single read:

```
current_valid_review(T) :=
    the MAX(created_at) row r in evidence_records
    where r.tenant = T.tenant
      and r.kind   = 'review'
      and r.artifact_id in A_c(T)          -- cites the CURRENT set only
      and (r.artifact_id in A_c_sealed(T)
           or r.created_at >= seal_epoch(A_c(T)))   -- a live review on the current set
-- a review that cited a now-superseded artifact is HISTORICAL, not current;
-- the APPROVE that sealed the old set is never "clobbered": it stays the
-- latest-over-that-set row, while a rework supersedes the set so the old
-- review drops out of A_c(T) automatically.
```

Property (G2): an old `APPROVE` on a superseded set **cannot** be mistaken for
approval of later changes, because `A_c(T)` is recomputed from
`superseded_by IS NULL`; the moment a rework supersedes the set, the old
approving row no longer satisfies the predicate — it is not deleted, it simply
becomes historical. This is the storage-level form of "old APPROVE never
clobbers later changes" (Root §4).

### 3.5 Provenance chain for Rework (G3, the concrete shape)

A full Rework cycle leaves this in-storage, in order:

```
[REVIEW-1] kind='review' outcome=fail  created_by=R1  payload{required_changes:[...]}
     │
     └─(builder rework)─►  ART-2.superseded_by=NULL  (new, current)
                            ART-1.superseded_by=ART-2  (old, historical)
                            EV-TEST-2  kind='test_result' execution=<rework exec>
     │
     └─(re-review)──────►  [REVIEW-2] kind='review' outcome=pass  created_by=R2
                            payload{ rework_of = REVIEW-1.id, verdict:'ok' }
                            + seal ART-2 DRAFT->SEALED
```

Every link is a stored edge: `REVIEW-2.rework_of → REVIEW-1`, `ART-2 →
(supersedes) ART-1`, `EV-TEST-2.execution → rework execution`. A reviewer or
auditor can walk `REVIEW-2 → REVIEW-1 → (required_changes) → ART-2 ← ART-1`
entirely from the two ledgers. No new column, no new table (R7).

---

## 4. Builder / Reviewer Role Boundary (G4)

| Capability | **Builder** | **Reviewer** |
|---|---|---|
| Reads the two ledgers | ✓ | ✓ |
| Runs executions / produces `artifact_records` + `test_result` / `file_revision` `evidence` | ✓ (owns production + the rework execution) | ✗ |
| Cites the builder's `final_answer` / `Task.status='done'` as proof | n/a | **✗ forbidden** (audit Q13 "Reviewer 不得直接信 Builder 自报"; the reviewer reads **ledgers + acceptance criteria + real source/tests**, never the self-report) |
| Writes a `kind='review'` verdict row | **✗ forbidden** (invariant 13 — a builder agent writing a `kind='review'` row for a WP it built is rejected with `EV_REVIEW_NOT_INDEPENDENT`) | ✓ (the single new row per verdict / re-review) |
| Seals `artifact_records` (`DRAFT→SEALED`) | ✗ (the producer never seals its own output) | ✓ (APPROVE is the seal, R3) |
| Reads `Task` acceptance criteria + real source / git / test results | ✓ (owns what to build) | ✓ (owns the *check*) |
| Must be agent-disjoint from every builder on the WP | (is a builder) | ✓ (REV-1/2 frozen; `W.requires_independent_review`) |

One-line boundary: **the Builder produces provenance; the Reviewer consumes it
and writes exactly one verdict row; neither holds the other's write authority,
and the Reviewer's only trusted inputs are the ledgers + frozen acceptance
criteria + real source/tests — never the Builder's self-report.**

---

## 5. Closed Result-Code Set (this lane) — `RV_*` / `EV_REVIEW_*`

Consistent with the parent `AE_*` / `EV_*` sets and Phase 3 `PL_*` (closed
result codes, not a persisted SM). Unknown values fail closed at the service
layer (the `PLANNING_RUN_STATUSES` re-validation pattern):

```
RV_OK                       = "RV_OK"   # transition applied
RV_NO_CURRENT_ARTIFACTS     = "RV_NO_CURRENT_ARTIFACTS"   # EXECUTING->IN_REVIEW with empty A_c(T)
RV_NO_REVIEWER              = "RV_NO_REVIEWER"             # no assigned reviewer row to act
RV_REVIEW_NOT_INDEPENDENT   = "RV_REVIEW_NOT_INDEPENDENT"  # lane-side mirror of EV_REVIEW_NOT_INDEPENDENT
RV_ALREADY_SEALED           = "RV_ALREADY_SEALED"          # attempting a 2nd seal on a SEALED set (parent AE_ALREADY_SEALED)
RV_SUPERSEDED_SET           = "RV_SUPERSEDED_SET"          # verdict/re-review cited a superseded (historical) artifact set
RV_INCONCLUSIVE             = "RV_INCONCLUSIVE"            # review parked in BLOCKED; must re-verify, never completes
RV_PAYLOAD_OVERRUN         = "RV_PAYLOAD_OVERRUN"          # critiques/required_changes exceed 32 KiB
```

Independence reuses **two existing** codes rather than inventing a new
mechanism: the lane may surface `PL_REVIEWER_NOT_INDEPENDENT` (assignment-time,
`assignment_service.py:94-96`) or `EV_REVIEW_NOT_INDEPENDENT` (verdict-time,
parent invariant 13). Neither is re-implemented here.

---

## 6. Boundary Matrix — who owns what (required 6-area assignment)

"—" = not owned here; "CONSUMES" = reads; "PRODUCES" = writes; "DECIDES" = the
derived decision is computed by this lane.

| Concern | **Storage (frozen: 2 ledgers, parent card)** | **Assignment lane (frozen: REV-1/2)** | **Completion lane (card `t_4185daed`)** | **Builder (role)** | **Reviewer (role)** | **This design (derived SM + guards)** |
|---|---|---|---|---|---|---|
| who produced it | ✓ `artifact_records.execution_id/agent_id` | — | — | PRODUCES | — | CONSUMES |
| who may seal it | ✓ `seal_status` (invariant 10) | — | — | ✗ | ✓ APPROVE seals | CONSUMES + guard |
| is the reviewer independent? | ✓ invariant 13 (`EV_REVIEW_NOT_INDEPENDENT`) | ✓ REV-1/2 (`PL_REVIEWER_NOT_INDEPENDENT`) | — | (builder) | (disjoint) | CONSUMES both, never re-implements |
| "what proves it now" | ✓ `evidence_records` + `subject_hash` re-verify | — | — | PRODUCES test/file evidence | writes verdict row | CONSUMES + "current valid review" query |
| APPROVE vs REQUEST_CHANGES | ✓ `outcome` value on the row | — | DECIDES completion from it | — | DECIDES the verdict | DECIDES the transition (guarded writes) |
| Rework provenance | ✓ `superseded_by` + `payload.rework_of` | — | — | PRODUCES the new set + link | writes `rework_of` re-review | DECIDES REWORKING→RE_REVIEW |
| Completion (≠ `Task.status`) | ✓ SEALED set + approving review row | — | **DECIDES** the predicate (this card only *names* it; `t_4185daed` owns the final criteria) | — | — | CONSUMES; explicitly not `Task.status` |

The Completion **decision** is a separate lane (`t_4185daed`); this card owns
the Review→Rework→Re-review→APPROVE sub-machine and the APPROVED→COMPLETED
*predicate shape* (so the completion card has a stable input). No cell is owned
twice.

---

## 7. Invariants (this lane, on top of the parent's 1–13)

Service-enforced, fail-closed, closed codes (§5):

1. **I-1 (no self-review, verdict-time).** Any `evidence_records` insert with
   `kind='review'` whose `created_by_agent` is a builder agent on the same
   WorkPackage is rejected → `EV_REVIEW_NOT_INDEPENDENT` (parent invariant 13,
   reused verbatim — never re-implemented).
2. **I-2 (review reads ledgers, not self-report).** A `kind='review'` row must
   cite ≥1 `artifact_id` in `A_c(T)` (its `payload.verdict` may reference
   `Task` acceptance criteria + real source/test refs, but **not** the builder's
   `final_answer` / `Task.status`) → `RV_NO_CURRENT_ARTIFACTS` if it cites
   nothing current.
3. **I-3 (seal only on APPROVE, one-way).** `artifact_records` seal
   (`DRAFT→SEALED`) happens only on the APPROVE transition (R3); a second seal
   on the same row → `AE_ALREADY_SEALED` (parent invariant 10).
4. **I-4 (rework produces new evidence, non-destructive).** REQUEST_CHANGES →
   REWORKING must yield ≥1 **new** `test_result` or `file_revision`
   `evidence_records` bound to the rework `execution_id` **and** new
   `artifact_records` that supersede the old set via `superseded_by`; the old
   rows are never deleted or updated (G2) → `EV_NO_SOURCE` if the new set
   re-cites a stale artifact with no new proof.
5. **I-5 (re-review links to the triggering REQUEST_CHANGES).** The re-review
   row **must** carry `payload.rework_of = <the fail-row id>` (G3, R6/R7); a
   `kind='review'` row written after a prior REQUEST_CHANGES that omits
   `rework_of` → `RV_SUPERSEDED_SET` / rejected.
6. **I-6 (inconclusive never completes).** `outcome='inconclusive'` parks the
   Task in BLOCKED and satisfies no completion/delivery gate; the lane
   re-verifies via the deterministic resolver (parent §5.2) before re-entering
   IN_REVIEW → `RV_INCONCLUSIVE` blocks completion.
7. **I-7 (tenant boundary).** All rows of one Task's review/rework chain share
   one `tenant_id` (DAO scope-inject, parent D6); a cross-tenant verdict row is
   impossible by construction, and the completion predicate re-asserts
   `tenant_ok(T)` (fail-closed, Root §5).
8. **I-8 (critiques bounded).** `payload.critiques` + `payload.required_changes`
   together ≤ 32 KiB on the REQUEST_CHANGES row → `RV_PAYLOAD_OVERRUN`
   (evidence must be complete-or-absent; backend complete-operation-bounds).

---

## 8. Deferred, Not Dropped (named so downstream cards see them)

- **Completion-lane ownership.** The *decision* "Task/WorkPackage/Project is
  completed" is owned by card `t_4185daed` (Final Completion & Delivery
  criteria). This card supplies the **APPROVED predicate shape**
  (§3.3 APPROVED→COMPLETED row) as a stable input; it does not define
  dependency-closure / delivery criteria. No overlap.
- **The `TaskCompletionGate._fail_open` defect** (`verification.py:634-642`)
  is **out of scope here** (Root card §5; parent §9): it is a *separate fix
  task with independent review*. This design deliberately routes APPROVE through
  the **ledger + disjoint-reviewer** path (R3), which writes a proof row and is
  structurally unaffected by that gate's LLM fail-open — so the defect does not
  leak into the Review lane. It is named so no card accidentally folds it in.
- **Test re-execution / re-run spine.** Rework *requires new evidence* (I-4)
  but V1 does **not** add a test-runner tool; producing new
  `test_result` evidence is a runtime-tool question (a new `execute`-class
  builtin), the parent §8 deferral — own card, own review. This design only
  says "the new set must cite new evidence"; it does not build the runner.
- **Retention / archival of superseded rows.** Out of V1 (parent §8): rows are
  append-only and cheap; a rework chain grows without bound. A retention
  policy is an ops decision on a real volume signal, not a design guess.
- **Reviewer re-verification re-run (vs re-cite).** `reverify_of` (parent
  §5.2) is for *unchanged* subjects; `rework_of` (this card) is for
  *changed* subjects. Both are `payload` keys, never columns. If a consumer
  later needs a persisted "re-review attempt counter", that is a new object with
  an independent need — deferred until then (AGENTS.md §2).

---

## 9. Overlap Flags — new vs reusable ("no duplicate without justification")

| New thing | Overlaps with (existing) | Verdict + justification |
|---|---|---|
| The 7 narrative states (§3.1) | `Task.status` 3-enum; `TaskRuntimeCompletionHandler` | **DERIVED, not a 4th status.** The states are computed from the ledgers + `Task.status` + DAG; they are a *label layer* a consumer shows. No persisted column, no second authority over `Task.status` (kept frozen). Justified: the task asks for an SM diagram; this is the SM, expressed derivation-first per D4 + §2. |
| The guarded transition table (§3.3) | parent invariants 7/10/13; `PL_*` codes | **PATTERN REUSE.** Every transition is a write the parent already authorized (append-only insert + one-way seal + invariant-13 reject). This card adds only the *guard predicates + `RV_*` mirror codes*; it does not add storage or a second write path. |
| `EV_REVIEW_NOT_INDEPENDENT` | `PL_REVIEWER_NOT_INDEPENDENT` | **TWO LAYERS OF ONE FACT.** Assignment-time (plan) and verdict-time (write) are different trust boundaries with different owners; the parent owns the verdict-time code, this card reuses it and pairs it with the existing assignment-time code. Not a duplicate — a two-boundary coverage of the same guarantee (G1). |
| `payload.rework_of` | parent `payload.reverify_of` | **NEW payload key, zero schema change.** `reverify_of` = "same subject, re-checked"; `rework_of` = "new subject (superseded set), triggered by this fail-row". Distinct semantics; both fit in the existing bounded `payload`. |
| "current valid review" query (§3.4) | parent §11 "current-valid-review query" | **IMPLEMENT THE CONTRACT.** The parent named the query; this card gives its exact predicate (latest `kind='review'` over the non-superseded set). No new object, just the read. |
| Completion predicate shape (§3.3 APPROVED→COMPLETED) | card `t_4185daed` (Final Completion & Delivery criteria) | **SPLIT, not duplicate.** This card names the *review-side* inputs (SEALED set + approving review row); `t_4185daed` owns the *full* final criteria (dependencies, delivery, tenant). The completion *decision* is `t_4185daed`'s; this card only makes its inputs queryable. |

---

## 10. Risk / Verification Notes

- **Read-only design; no code, migration, or config changed on this card**
  (Phase 3 two-stage contract: design precedes build). All storage is the
  parent's two ledgers; this card adds **zero** schema and **one** closed code
  set (`RV_*`, service-side only).
- **Derivation correctness risk.** Because states are *computed*, a UI or gate
  that caches the derived state can drift from the ledgers. Mitigation (owner
  = whoever consumes): the derived state is always recomputed from
  `artifact_records`/`evidence_records` on read (no cache of the state itself);
  the ledgers are the only authority. This is the AGENTS.md "derived state must
  be rebuilt from the authoritative committed fact" rule applied here.
- **Seal-then-rework ordering.** If a rework supersedes a *SEALED* artifact
  after APPROVE, the old row stays SEALED-and-historical; the *new* row is
  DRAFT until the re-review seals it. The "current valid review" query keys off
  `superseded_by IS NULL`, so the pre-rework APPROVE is automatically
  historical (G2 "old APPROVE never clobbers later changes"). Risk: a consumer
  that reads "the approving review" instead of "the current-valid review" —
  §3.4 defines the only correct read; downstream cards must use it.
- **`inconclusive` looping.** A reviewer that keeps returning
  `inconclusive` parks the Task in BLOCKED forever. Mitigation: BLOCKED re-entry
  is only via deterministic re-verification (parent §5.2), which is fail-closed
  and produces a determinate `pass`/`fail`; `inconclusive` is a *transient*
  signal, not a terminal verdict (I-6). A real "reviewer can't tell" is the
  reviewer's `fail` + `required_changes: "reviewer unable to verify X"` — the
  lane does not need a third terminal.
- **Tenant crossing inside a rework chain.** A rework that cites an artifact
  from another tenant is impossible: DAO scope-inject (parent D6) filters all
  reads/writes to the caller's tenant, and I-7 re-asserts it in the
  completion predicate. No cross-tenant rework is expressible, not merely
  prevented.
- **`Task.status` divergence (by design).** `Task.status='done'` (Run fact,
  `task_completion.py:137-140`) can be set while the Review lane is still in
  REQUEST_CHANGES/REWORKING. This is **intended**: `done` answers "did the Run
  finish"; the review lane answers "is the work *approved*". The completion
  lane (`t_4185daed`) is the **only** place the two are joined, and it joins
  them by the SEALED+approving-review predicate, never by `Task.status` alone
  (Root §5 "Agent reported success ≠ completion"). This divergence is a
  feature (two independent outcomes), flagged so no card "fixes" it by
  re-coupling them.
- **Verification for this card.** §3.3 transition table covers every edge the
  task names (Execution→Review→APPROVE/REQUEST_CHANGES→Rework→Re-review→
  Completion); §2 maps the four guarantees to named mechanisms; §6 assigns each
  concern to exactly one owner; §9 justifies every new thing against "no
  duplicate without justification". Ready for review by the independent
  reviewer (Root §5 / gate) and for the completion card `t_4185daed` to
  consume the APPROVED predicate.

---

## 11. Handoff to Downstream Cards

- **`t_4185daed` (Final Completion & Delivery criteria):** consume the
  **APPROVED→COMPLETED predicate** from §3.3 / §6: a Task/WP/Project is
  completed iff (a) `A_c(T)` ⊆ SEALED, (b) the current-valid-review row
  (`§3.4`) over `A_c(T)` has `outcome='pass'` and was written by a disjoint
  reviewer, (c) dependency completion holds (`task_dependencies`, frozen),
  (d) tenant boundary holds (I-7), and (e) no `inconclusive` / open
  REQUEST_CHANGES remains on the current set. Delivery may only reference
  SEALED + approving-review artifacts (parent §11, delivery card
  `t_fa30ea5d`). "Agent says done → Delivery" remains structurally impossible.
- **Builder card (implementation, next batch):** implement the **service-side
  guard layer** only — the `RV_*` result codes (§5), the transition guard
  functions (§3.3), and the "current valid review" + "rework provenance"
  reads (§3.4/§3.5) over the **parent's** two ledgers. **Do not** add a
  review/rework table or column (D4 / R0); **do not** touch the parent's
  avoid-list (§9 of that doc) or the frozen model list; **do not** fold in the
  `TaskCompletionGate._fail_open` fix (it is a separate card). Sealing,
  independence, and rework-linking all ride on `artifact_records` /
  `evidence_records` + the existing REV-1/2 and `EV_REVIEW_NOT_INDEPENDENT`.
- **The `TaskCompletionGate` fail-open fix card (separate, Root §5):** that
  card fixes `verification.py:634-642` to be fail-**closed** (an errored gate
  must not mark a Task done). This card's design is deliberately decoupled
  from it: APPROVE writes a ledger proof row via the disjoint reviewer and is
  not gated by the LLM completion call. The two converge only at the
  completion-lane predicate (`t_4185daed`), which is fail-closed regardless.

---

*Design produced by aco-architect, task t_dbb0c0dd, at main = 8f030792
(tag PHASE_3_CLOSED), consuming the parent design
PHASE_4_ARTIFACT_EVIDENCE_V1_DESIGN.md (t_f19aae89 @ b2d4ec51) and the source
audit PHASE_4_AUDIT_REPORT_T15e05452.md (Q5/Q7/Q8/Q10/Q12/Q13), re-verified
against the live tree at this baseline (assignment_service.py REV-1/2,
planning.py flag, task.py status, task_completion.py handler, verification.py
gate + fail-open defect + deterministic verifier).*
