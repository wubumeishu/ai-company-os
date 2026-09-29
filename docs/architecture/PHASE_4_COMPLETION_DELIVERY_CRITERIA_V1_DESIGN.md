# Phase 4 — Final Completion & Delivery Criteria: V1 Design (t_4185daed)

Task: `t_4185daed` — "Define Final Completion & Delivery criteria"
Author: aco-architect
Baseline: main == origin/main `8f030792`, tag `PHASE_3_CLOSED` — designed in worktree
`wt/t_4185daed`
Inputs (consumed, not re-invented):
- `docs/architecture/PHASE_4_ARTIFACT_EVIDENCE_V1_DESIGN.md` (t_f19aae89, commit
  `b2d4ec51`) — the two append-only ledger tables `artifact_records` /
  `evidence_records`, closed `SEAL_STATUSES` / `EVIDENCE_KINDS` /
  `EVIDENCE_OUTCOMES` sets, invariants 1–13, and the §11 completion-lane
  handoff: completion = "SEALED artifact set + approving `kind='review'`
  evidence row + dependency completion + tenant boundary".
- `docs/architecture/PHASE_4_INDEPENDENT_REVIEW_REWORK_V1_DESIGN.md` (t_dbb0c0dd,
  commit `8448b7ba`) — the derived-state Review/Rework lifecycle (R0–R8) and the
  APPROVED→COMPLETED predicate shape (§3.3 / §3.4 / §6) this card consumes.
Source-audit inputs (read-only, traced): `PHASE_4_AUDIT_REPORT_T15E05452.md`
(t_15e05452, commit `7a8a389a`) Q10/Q11/Q12/Q13.
Live-tree anchors re-verified at this baseline:
`task.py:58-62` (`Task.status` 3-enum), `task_completion.py:56/137-140`
(`TaskRuntimeCompletionHandler`), `task.py:138-171` (`TaskDependency`,
`uq_task_depends_pair`, no edge attributes), `planning.py:349-408`
(`WorkPackageTask` link + `ck_wp_tasks_materialized`), `planning.py:299-301`
(`requires_independent_review`), `planning.py:95` (`MILESTONE_KINDS`),
`project.py:53-67` (`Project.status` 10-enum), `assignment_service.py:96`
(`PL_REVIEWER_NOT_INDEPENDENT`), `verification.py:635`
(`TaskCompletionGate._fail_open` defect, out of scope here).

Style/precedent: same classification discipline, boundary-matrix shape,
closed-result-code posture, and "no duplicate without justification" rule as
both parent docs and Phase 3 `planning` design D5.

## Classification Discipline

Carried over from the two parent docs:

- **FACT** — directly observable in this tree (file:line, named constant,
  migration id).
- **OBSERVATION** — a pattern seen in one or more concrete instances, no
  universal claim.
- **INFERENCE** — a conclusion drawn from the facts above.
- **UNKNOWN** — not verifiable from this tree; the resolving action is stated.

The design itself is normative (what the system *should* be). Normative prose
is unlabeled; all claims about the *current* system carry a class.

---

## 1. Verdict (up front)

The audit found that completion today **is** the forbidden equivalence (Q10
[FACT/INF]): `Task.status='done'` is written by
`TaskRuntimeCompletionHandler` the moment the Run reports `completed`
(`task_completion.py:137-140` [FACT]) — i.e. "Agent reported success" is
literally the Task completion fact. And `Project.status='COMPLETED'` is **never
written by any service** — grep across `backend/app` finds zero `COMPLETED`
write sites [FACT, re-verified at this baseline]; delivery today is answer
transport only (Q11 [FACT/OBS]).

This card delivers the requested **Final Completion Criteria + Delivery
requirements** as **derived predicates with closed decision codes** — **no
new table, no new column, no persisted completion status.**

> **Design shape, one line:**
> Completion is a **read-side decision** the completion lane computes live
> from the two parent-ledger tables + the frozen task graph + one frozen flag,
> and records its verdict as **one append-only `evidence_records`
> `kind='structured'` decision row** per evaluation. Delivery is a
> **separate, later lane decision** that may only reference `SEALED`
> artifacts carrying a disjoint-reviewer `outcome='pass'` row — "Agent says
> done → Delivery" remains structurally impossible at every hop. `Task.status`
> and `Project.status` stay the **only two persisted completion writes**;
> `Project.status='COMPLETED'` gets exactly one owning write site (this lane,
> §5) so its audit finding becomes a fact, not a gap.

Why the completion decision is *computed*, not stored (reconciles the task's
"criteria" ask with parent D4 + AGENTS.md §2 "a new SM needs an independent
owner and need"):

1. **No independent object.** A "Task is completed" fact is not a durable
   object with its own authoritative transitions — it is a **pure function of
   the current (non-superseded) artifact set + the current-valid review row +
   the frozen DAG + one tenant boundary**. All five inputs already have exactly
   one owner: the two ledger tables, `task_dependencies`, and `Task`/`Project`
   rows. Storing a `completion_status` column would be a second authority over
   facts the ledgers already answer (Root AGENTS.md §2 one-authority rule; the
   review card R0 chose the same derivation for the same reason).
2. **Staleness is the enemy.** A persisted "completed" flag can drift from the
   ledgers on the next rework cycle (a later `supersede` silently invalidates
   it — exactly the "old APPROVE must not clobber later changes" guarantee,
   Root §4). A *predicate* is recomputed on every evaluation: rework that
   supersedes the set drops the old approving row out of the
   current-valid-review query (§3.4 of the review card) and the next
   evaluation fails closed with a named code. Drift is unrepresentable.
3. **The lane still leaves an audit record.** Each evaluation appends one
   `kind='structured'` decision row (citing the inputs it read, §5.3) — the
   verdict is provable and re-verifiable, but the *state* is never persisted,
   so there is no second authority.

Decisions that bound the whole design:

| # | Decision | Authority |
|---|---|---|
| C0 | Completion at every level (Task / Work Package / Project) is a **derived predicate computed live from the two parent ledgers + `task_dependencies` + `WorkPackageTask` + one flag** — no new table, no new column, no persisted status. The only durable trace is one append-only `evidence_records kind='structured'` decision row **per evaluation**. | parent D4; review card R0/R8; AGENTS.md §2 |
| C1 | **Task Completion = the `CT(T)` predicate of §3** (a) `A_c(T)` non-empty and ⊆ `SEALED`, (b) the current-valid review over `A_c(T)` is `outcome='pass'` written by a disjoint reviewer, (c) no open REQUEST_CHANGES / no `inconclusive` on the current set, (d) transitive dependency completion holds, (e) tenant boundary holds. **`Task.status='done'` is not an input to `CT(T)`** (Root §5; audit Q10). | review card §3.3/§6 (APPROVED→COMPLETED row + §3.4 current-valid-review query); parent §11 handoff |
| C2 | **Work Package Completion = `CW(W)` of §4**: every materialized task (via `WorkPackageTask.task_id` non-null, `planning.py:397`) satisfies `CT`, AND no open slot exists (`task_id IS NULL` = work not yet materialized → blocker, fail-closed). `requires_independent_review` (frozen, `planning.py:299-301`) is *already enforced inside* each task's `CT` (its approving row must be a disjoint reviewer's — never re-checked at WP level). | `planning.py:349-408`; review card G1/R2 |
| C3 | **Project Completion = `CP(P)` of §5**: every in-scope task satisfies `CT`, every `milestones.kind='delivery'` milestone's WPs satisfy `CW`, and the tenant boundary holds. Project completion **licenses the single owning write site** `Project.status='COMPLETED'` + `status_changed_at` (`project.py:53-67,84`) — today's zero write sites (audit Q10) become exactly one, owned by this lane. | audit Q10 [FACT]; `project.py:53-67` |
| C4 | **Delivery is a later, separate decision (C-D1…C-D4, §6)** built **only** on Completed + Approved + Evidence: a delivery record may cite **only** `artifact_records` rows that are `SEALED` **and** carry a current-valid `outcome='pass'` review row by a disjoint reviewer. Destination is a closed V1 vocabulary (`channel` / `published_page` / `project_record` — no external platform, Root §6). "Agent says done → Delivery" is impossible at *every* hop: no sealed artifact ⇒ no delivery input exists. | parent §11 delivery handoff; Root §6; audit Q11 [OBS] |
| C5 | **The lane's durable trace = one `evidence_records` row per evaluation**, `kind='structured'`, `subject_ref='task://{id}'` / `'wp://{id}'` / `'project://{id}'`, `payload.decision` + the input refs it read (cited artifact ids, review row ids, dep task ids), `created_by_agent` = the lane's executing agent. No new table, no new kind, no new column. | parent §5.1 (`structured` kind already closed in V1); C0 |
| C6 | **Closed decision-code set `CP_*` (§3.5)** mirrors the review lane's `RV_*` set (evaluation-time mirror, not re-implementation — the review lane's *insert-time* guards stay there; this lane *reads* and rejects with its own codes). Unknown / unreadable inputs fail closed to a named code, never to `CP_OK`. | review card §5; Phase 3 D5 pattern |
| C7 | **Scope boundary: `CT` / `CW` / `CP` govern `type='todo'` execution tasks only.** Supervision tasks (`Task.type='supervision'`, `task.py:53-57`) have their own frozen semantics — `TaskRuntimeCompletionHandler` re-pends them on run completion (`task_completion.py:141-144` [FACT]) — and are excluded from the completion-scope union (§3.2). No change to that mechanism. | `task_completion.py:137-154`; AGENTS.md §3 (no drive-by) |
| C8 | **The `TaskCompletionGate._fail_open` defect** (`verification.py:635-642`, audit defect section) is **out of scope here** (Root §5; both parent docs §9): it is a *separate fix task with independent review*. This design is structurally decoupled from it: `CT(T)` reads **ledger rows written by a disjoint reviewer** — it never consults the gate's LLM call, so the gate's fail-open cannot leak into completion. Named so no card folds it in. | audit defect section; review card §8 |

---

## 2. The Four Named Requirements, Mapped to Concrete Mechanisms

The task's four explicit requirements, each satisfied by a *named* mechanism
(existing or this-design), never by an unenforced prose promise:

| Requirement | Mechanism | Where enforced |
|---|---|---|
| **R-A Explicit "Agent reported success ≠ completion"** | `CT(T)` has **no input that is `Task.status`, the Run's `final_answer`, or the gate's LLM verdict** (§3.1). The Run fact `Task.status='done'` keeps answering "did the Run finish"; the completion lane answers "is the work *approved and evidenced*" and joins the two **only** at the predicate, read-side, never write-side. The divergence is by design and flagged (§9). | §3.1 input exclusion; review card R8 |
| **R-B Delivery requirements (§6)** | Delivery = C-D1…C-D4: a separate decision + append-only delivery record, inputs restricted to `SEALED` + current-valid `outcome='pass'` artifacts (parent §11), closed destination vocabulary (C-D2), provenance + state + timestamp (C-D3), audit via the decision row (C-D4). "Agent says done → Delivery" impossible at every hop. | §6 |
| **R-C Required artifacts / evidence / review decision / dependency checks / tenant boundary** | Exactly the five `CT(T)` terms (§3.1): artifacts = `A_c(T)` ⊆ `SEALED`; evidence = the `test_result` / `file_revision` rows + the current-valid review row; review decision = `outcome='pass'` by a disjoint reviewer; dependencies = `deps_done(T)` over frozen `task_dependencies`; tenant = `tenant_ok(T)` via DAO scope-inject + predicate re-assertion. Every missing term ⇒ a named `CP_*` code, fail-closed. | §3.1, §3.5 |
| **R-D Documented WP-Completion vs Project-Delivery distinction** | §4 (`CW` = the *work* is done, at package level) vs §6 (`CD` = *handing the done work over*, a separate later decision with its own record, state, and destination). One-line rule: **completion is a property of the work; delivery is an event about the work's handover.** Delivery may not exist while `CW(W)`/`CP(P)` is false; a delivery record never feeds back into `CT`/`CW`/`CP` (no cycle, no second authority). | §4 + §6 + §7 matrix |

---

## 3. Task Completion — the `CT(T)` Predicate

### 3.1 Definition (derived, computed on evaluation)

For one **Task** `T` (with `A_c(T)` = current non-superseded artifact set,
`A_c_sealed(T)`, the current-valid-review query, `deps_done(T)`, and
`tenant_ok(T)` all exactly as defined in §3.1 / §3.4 of the review card —
this card does not redefine them):

```
CT(T) :=   T.type = 'todo'                                          (C7)
      AND  |A_c(T)| >= 1                (required artifacts exist, DRAFT or SEALED)
      AND  A_c(T) ⊆ A_c_sealed(T)       (every current artifact is SEALED)
      AND  rv := current_valid_review(T)   exists, i.e. the latest
                 kind='review' row over the non-superseded set
      AND  rv.outcome = 'pass'                      (required review decision)
      AND  rv.created_by_agent ∉ builders(W(T))     (reviewer independence,
                                                     G1 — disjointness fact,
                                                     never re-implemented:
                                                     the row could not exist
                                                     otherwise, invariant 13)
      AND  no open rework on the current set, i.e.
           rv is the current-valid row AND rv.outcome ≠ 'fail'
           (an open REQUEST_CHANGES makes rv.outcome='fail' ⇒ this term fails)
      AND  no unconsumed 'inconclusive' row newer than rv on the current set
           ('inconclusive' is a re-check signal, never a verdict — I-6 of the
           review card; it can never satisfy completion, fail-closed)
      AND  deps_done(T)   every transitive dependency of T over
                          task_dependencies satisfies CT recursively
      AND  tenant_ok(T)   T, A_c(T), rv, and every dependency task in one
                          tenant_id (DAO scope-inject; re-asserted here)
```

**Input exclusion (R-A, the Root §5 hard boundary):** `CT(T)` reads *only*
the two ledger tables, the frozen `task_dependencies`, the frozen
`Task.type` / `Project` scoping, and the frozen `requires_independent_review`
fact. It **never** reads `Task.status`, the Run's `final_answer`, the
`_completion_evidence` conversation payload (`verification.py:92-137`), or the
gate's LLM outcome. A Task with `Task.status='done'` and an empty ledger set
fails with `CP_NO_WORK`; a Task whose ledger is perfect but whose Run is
`failed` still satisfies `CT` (the Run fact is not a completion input) —
both directions by construction.

### 3.2 Completion scope (which tasks count)

- **Task-level:** `CT(T)` applies to `type='todo'` tasks only (C7). A
  supervision task's completion stays entirely in its frozen owner
  (`task_completion.py:141-144` re-pend semantics); the lane does not extend
  `CT` to it, and the WP scope (§4) never counts supervision slots as
  execution work.
- **Project-level:** in-scope set = `{ t ∈ tasks | t.project_id = P AND
  t.type='todo' }` — the union of tasks materialized under P (frozen Phase 2D
  provenance `task.py:84-86` [FACT]), regardless of whether they reached P
  through a planning materialization or the manual path. `CP(P)` = every task
  in this set satisfies `CT` (+ §5 conditions).

### 3.3 Evaluation flow (one evaluation, read-only + one append)

```
evaluate_completion(T, scope)            # scope ∈ {task, wp, project}
  1. read: A_c(T), A_c_sealed(T), current_valid_review(T),
           task_dependencies closure, WorkPackageTask links, T.type
     (all through the tenant-scoped DAOs — the review card §3.4 query)
  2. decide: the CP_* predicate stack (§3.5), first failing term wins
     the code; all terms hold ⇒ CP_OK
  3. record: ONE evidence_records row (C5), kind='structured',
     subject_ref='task://{T.id}' (or wp://…, project://…),
     payload = { "decision": "completion_evaluated",
                 "outcome":  "<CP code>",
                 "cited":   {"artifact_ids": A_c ids,
                              "review_id":    current-valid review row id,
                              "dep_task_ids": transitive dep ids,
                              "wp_id" / "project_id": per scope } }
     created_by_agent = the lane's executing agent (or created_by_user for
     a human-triggered evaluation — D5 XOR source holds)
  4. stop. The lane writes NOTHING else: no Task.status, no seal, no
     artifact row. The next rework cycle supersedes the set; the next
     evaluation re-derives (C0, §1 point 2).
```

### 3.4 Why "no open REQUEST_CHANGES / inconclusive" is a first-class term

The review card parks `inconclusive` in BLOCKED (I-6) and defines
REQUEST_CHANGES as the `outcome='fail'` row + `payload.required_changes`.
Both are **review-lane states**, but the completion lane must *reject* them
by name (not by absence of the positive terms), because a future reader of
the ledger could otherwise mistake "no pass row yet" for "not yet reviewed".
`CP_OPEN_REQUEST_CHANGES` / `CP_INCONCLUSIVE_REVIEW` make the two distinct
failures auditable (§3.5), which is what Root §5 "unresolved blockers" asks
for.

### 3.5 Closed decision-code set — `CP_*` (this lane)

Consistent with the parent `AE_*`/`EV_*` and the review card's `RV_*`
(closed result codes, not a persisted SM). Evaluation returns exactly one
code, first failing term in stack order; unknown or unreadable inputs fall
through to the catch-all and **never** to `CP_OK` (fail-closed, Root §5):

```
CP_OK                     # all terms of CT/CW/CP hold
CP_NO_WORK                # |A_c(T)| = 0 — no artifacts to complete (WP: all
                          #   slots unmaterialized; Project: no in-scope tasks)
CP_NOT_SEALED             # some current artifact still DRAFT (seal is
                          #   APPROVE-driven, review card R3 — not the
                          #   completion lane's write)
CP_NO_APPROVING_REVIEW    # no current-valid review row over A_c(T), or the
                          #   latest one's outcome != 'pass'
CP_REVIEW_NOT_INDEPENDENT # current-valid pass row's created_by_agent is a
                          #   builder on W(T) — structurally impossible if
                          #   the review card's guards are live; the check
                          #   stays as a read-side assertion (fail-closed
                          #   mirror of invariant 13, not a re-implementation)
CP_OPEN_REQUEST_CHANGES   # latest review on the current set is outcome='fail'
                          #   with payload.required_changes — rework open
CP_INCONCLUSIVE_REVIEW    # latest review on the current set is
                          #   outcome='inconclusive' — parked, never completes
                          #   (review card I-6)
CP_DEPS_NOT_DONE          # ≥1 transitive dependency fails CT recursively
CP_TENANT_MISMATCH        # a task, artifact, review row, or dep crosses tenants
                          #   (re-assertion; DAO scope-inject makes this
                          #   unreachable, not merely prevented — the review
                          #   card I-7 shape)
CP_EVAL_ERROR             # catch-all: unreadable input / unexpected ledger
                          #   shape → fail closed, never CP_OK
```

Independence *at insert time* remains the review lane's job (`EV_REVIEW_NOT_
INDEPENDENT`, parent invariant 13); `CP_REVIEW_NOT_INDEPENDENT` is the
*evaluation-time mirror* that keeps the read side safe even if a bad row ever
reached storage. Same two-boundary pattern as G1 of the review card.

---

## 4. Work Package Completion — the `CW(W)` Predicate

```
CW(W) :=   |materialized_tasks(W)| >= 1
                  materialized_tasks(W) = { wpt.task_id | WorkPackageTask
                                           wpt of W with task_id IS NOT NULL }
                   (planning.py:397 — NULL slot = open intent, §6.3 of the
                    Phase 3 planning design: link survives, work unmaterialized)
      AND  ∀ t ∈ materialized_tasks(W): CT(t)                     (§3)
      AND  no open slot: ∄ wpt of W with task_id IS NULL
            (an open slot is an UNRESOLVED BLOCKER, not a pass — fail-closed:
             the package is not "fully worked" while a slot has no task.
             Open-slot exception policy is a PLANNING-lane decision (re-open /
             cancel the slot), never a completion-lane invention.)
      AND  tenant_ok(W): W, its planning run, and all materialized tasks in
             one tenant (DAO scope-inject; C3 re-assertion)
```

Code stack order for a failing WP: `CP_NO_WORK` → per-task `CT` code of the
first failing task (reported as `CP_<taskcode> @ <task_id>` in the decision
row's payload — one row per WP evaluation, the failing task named in
`payload.failing_task_id`) → `CP_OPEN_SLOT` (new WP-level code, open slot) →
`CP_TENANT_MISMATCH` → `CP_EVAL_ERROR`.

> **One code added at WP level:** `CP_OPEN_SLOT` ("a materialized slot is
> missing — work not fully materialized; completion impossible"). WP-level
> because open slots have no task to attribute the failure to. It is
> fail-closed by definition: an empty `task_id` can never evaluate to a
> `CP_OK` task.

### The `requires_independent_review` term lives inside, not at WP level

`W.requires_independent_review` (frozen, `planning.py:299-301`) is consumed
**inside each task's `CT` term (b)** (the approving row must be a disjoint
reviewer's — invariant 13 already rejects builder-written review rows for such
WPs at insert time). The WP predicate does **not** re-check the flag: that
would be a second consumer with different logic over the same fact (one-
authority rule). The flag's effect on completion is *entirely* via the
independence term — which is the design intent (G1 of the review card: two
trust boundaries, never re-implemented).

---

## 5. Project Completion — the `CP(P)` Predicate

```
CP(P) :=   |in_scope_tasks(P)| >= 1
                  in_scope_tasks(P) = { t ∈ tasks | t.project_id = P
                                           AND t.type = 'todo' }   (C7)
      AND  ∀ t ∈ in_scope_tasks(P): CT(t)
      AND  ∀ m ∈ milestones WHERE m.kind='delivery' AND m belongs to P:
             CW(every WP of m)
             (MILESTONE_KINDS is closed, planning.py:95; a 'delivery'
              milestone is the project's declared handover point — completing
              it is part of project completion; 'gate'/'phase' milestones are
              ordering buckets, never completion state (§9 risk note))
      AND  tenant_ok(P)
```

**The single owning write site (C3).** On `CP_OK`, the lane is the **only
code path** authorized to write `Project.status='COMPLETED'` +
`status_changed_at` (`project.py:53-67,84` [FACT] — today zero write sites
exist, audit Q10). Preconditions at the write site: `CP(P) = CP_OK` on the
*current* evaluation (recomputed, not the decision row read back), `P.status`
in an executable status (frozen `PROJECT_EXECUTABLE_STATUSES`,
`task_execution_service.py:66-68`), and tenant match. Any other path setting
`COMPLETED` is a contract violation the lane's review must reject. The
write site does **not** touch `Task.status` — task rows keep their Run fact;
completion remains a read-side predicate even for the project row, and the
`COMPLETED` write is the *projection* of a verified fact, never the fact
itself.

> Why persisting `COMPLETED` is allowed while task/WP completion is not:
> `Project.status` is a **frozen 10-value lifecycle enum that a terminal
> value was never wired to** (audit Q10) — wiring exactly one write site is
> completing an existing contract, not creating a second authority (the
> predicate is still computed live; the enum value is its durable
> publication, same class as `AgentRun.delivery_status` being published on
> delivery). Task/WP have no such pre-existing terminal value to wire.

---

## 6. Delivery Requirements (V1)

### 6.1 The distinction, stated once (R-D)

| | Work Package / Project Completion | Delivery |
|---|---|---|
| What it is | a **property** of the work (is it done + approved + evidenced) | an **event** about the work (it was handed over, where, when) |
| Decided by | the `CT`/`CW`/`CP` predicate, read-side | a separate delivery decision (C-D1), later |
| Writes | one `kind='structured'` decision row (C5); `COMPLETED` projection (C3) | one delivery record (§6.3) + its decision row |
| Feeds back into | — | **never** into `CT`/`CW`/`CP` (no cycle) |

One-line rule: **you cannot deliver work that is not completed-and-approved;
you cannot mark work completed because it was delivered.** Completion is the
gate; delivery is what happens after the gate.

### 6.2 Delivery decision codes — closed set `CD_*`

```
CD_OK                   # delivery accepted / executed
CD_NOT_COMPLETED        # CW/CP (as applicable) is not CP_OK — no input to
                        #   build a delivery from (the structural root cause
                        #   of "Agent says done → Delivery impossible")
CD_NO_SEALED_APPROVED   # delivery cites an artifact that is DRAFT, or whose
                        #   approving review row is not current-valid over the
                        #   cited set (superseded ⇒ stale, review card §3.4)
CD_DESTINATION_INVALID  # destination outside the closed V1 vocabulary (C-D2)
CD_EVAL_ERROR           # catch-all; fail closed
```

### 6.3 Delivery requirements, C-D1 … C-D4 (normative contract)

- **C-D1 (gate).** A delivery may be opened **only** against a scope
  (WP or Project) whose current evaluation is `CP_OK`, and may cite **only**
  `artifact_records` rows that are `SEALED` and covered by a current-valid
  `outcome='pass'` `kind='review'` row written by a disjoint reviewer
  (parent §11: "delivery may only reference SEALED + approving-review
  artifacts"). A cited artifact that is not `SEALED`-and-approved ⇒
  `CD_NO_SEALED_APPROVED`, evaluation aborted, nothing written.
- **C-D2 (destination — closed V1 vocabulary).** `destination.kind ∈
  ("channel", "published_page", "project_record")`:
  - `channel` — the existing answer-transport spine (`delivery.py`
    `deliver_runtime_message`, `ChannelDelivery`, `AgentRun.delivery_status`
    [FACT] audit Q11) is **reused, not duplicated**: the delivery record
    references the channel-delivery fact; no second channel machinery.
  - `published_page` — cite the existing `PublishedPage.short_id` (D3 of the
    parent: reference, never re-store).
  - `project_record` — the delivery is recorded **in the ledger itself**
    (the record *is* the destination) for projects whose deliverable is the
    verified-and-approved artifact set with no external handover.
  - **No external publish platform in V1** (Root §6: "don't implement
    complex external publish platforms early"). Adding a destination kind is
    a reviewable closed-set extension, not a code path (parent §3.2 guard
    pattern applied to destinations).
- **C-D3 (the record — required fields).** One append-only delivery record
  (owned by the delivery lane, card `t_fa30ea5d` implements it; this card
  fixes the *contract*, not the table):
  - `project_id` + `scope` (`wp://{id}` | `project://{id}`) + `tenant_id`
    (non-null, `__tenant_scoped__`, D6 inheritance),
  - `artifact_ids` — **bounded** list of `SEALED`+approved artifact ids
    (complete-operation bounds, backend AGENTS.md; each id re-checked
    against the current-valid ledger state at write time — a rework that
    superseded one between decision and write fails the write,
    `CD_NO_SEALED_APPROVED`, nothing delivered),
  - `destination` `{kind, ref}` per C-D2,
  - `state` — closed `DELIVERY_STATES = ("PENDING", "DELIVERED", "FAILED")`,
    `PENDING` on decision, terminal states set by the owning transport or
    the lane on `project_record` (no `RECALLED` in V1 — recall policy is a
    real consumer's need, deferred §8),
  - `decided_at` / `executed_at` timestamps, `decided_by` (lane agent or
    user — XOR, D5 shape),
  - `audit` — the row is itself the audit record; the decision `payload`
    cites the `CP_OK` evaluation ids + review row ids it relied on
    (provenance chain: delivery → CP evaluation → approving review row →
    sealed artifact set, all ledger-queryable, G3 shape of the review card).
- **C-D4 (independence from the Run).** A delivery's only trusted inputs are
  the ledger rows named above. The Run's `final_answer`, `Task.status`, and
  gate verdict are **not** delivery inputs (R-A extended to delivery). The
  delivery lane's executing agent ≠ the WP's builder agents is the
  same disjointness fact (invariant 13); a builder writing a delivery for a
  WP it built is rejected at the same boundary.

---

## 7. Boundary Matrix — who owns what (the 6 areas)

"—" = not owned here; "CONSUMES" = reads; "PRODUCES" = writes; "DECIDES" =
the derived decision is computed by this lane.

| Concern | **Storage (frozen: 2 ledgers + task graph, parent cards)** | **Assignment lane (frozen: REV-1/2)** | **Review lane (card t_dbb0c0dd)** | **Completion lane (this card)** | **Delivery lane (card t_fa30ea5d)** | **Builder / Reviewer (roles)** |
|---|---|---|---|---|---|---|
| who produced it | ✓ `artifact_records.execution_id/agent_id` | — | — | CONSUMES | CONSUMES | Builder PRODUCES |
| is the reviewer independent? | ✓ invariant 13 | ✓ REV-1/2 | CONSUMES both, guards inserts | CONSUMES (CP_REVIEW_NOT_INDEPENDENT read-side assert) | CONSUMES (C-D4) | Reviewer = disjoint |
| "what proves it now" | ✓ `evidence_records` | — | DECIDES verdict rows | CONSUMES via current-valid-review query | CONSUMES (SEALED + approving set only) | Reviewer writes the proof row |
| task / WP / project **completion** | — | — | supplies the APPROVED predicate shape | **DECIDES** `CT`/`CW`/`CP` (§3–§5) + the one `COMPLETED` projection write (C3) | CONSUMES `CP_OK` as its gate (C-D1) | — |
| **delivery** | ✓ delivery record (t_fa30ea5d) | — | — | CONSUMES (decides nothing) | **DECIDES** `CD_*` + owns record writes | — |
| rework provenance | ✓ `superseded_by` + `payload.rework_of` | — | DECIDES the rework cycle | CONSUMES (rework drops old rows out of current set ⇒ next eval fails closed with named code) | CONSUMES (a superseded cited artifact ⇒ `CD_NO_SEALED_APPROVED`) | Builder PRODUCES the new set |

No cell is owned twice. The completion lane owns exactly one write that
touches a frozen model (`COMPLETED` projection, C3 — completing a pre-existing
terminal value with zero write sites); everything else in this card is
reads + one append-only row per evaluation.

---

## 8. Deferred, Not Dropped (named so downstream cards see them)

- **Delivery record storage owner.** This card fixes the *contract* (C-D1…C-D4,
  the `CD_*` codes, the field set); the record itself (a new
  `delivery_records` table + DAO + service) is owned by card `t_fa30ea5d`
  (implementation of the Delivery lane). V1 destination vocabulary stays
  closed; `RECALLED` / external-platform destinations deferred until a real
  consumer.
- **Test re-execution / test-runner spine.** Unchanged from both parent docs:
  `CT`'s "required evidence" term cites `test_result`/`file_revision` rows
  that must *exist and be current* (rework rule I-4 of the review card);
  *producing* fresh test evidence is the builder's runtime-tool question,
  not this lane's.
- **Retention of superseded chains.** Unchanged (parent §8): decision rows
  and delivery records are append-only and cheap; retention is an ops
  decision on a real volume signal.
- **Open-slot cancellation policy.** C-WP fail-closes on open slots
  (`CP_OPEN_SLOT`); whether an open slot can be *cancelled* by the planning
  lane (so the WP can complete) is a planning-lane extension, deferred —
  V1 requires the slot to be materialized or the WP stays un-completeable.
- **Delivery transport retries / at-least-once.** `ChannelDelivery` already
  owns pending/claimed/delivered/failed + attempt tracking [FACT] audit Q11;
  the delivery lane references it and adds no second retry mechanism.
- **Milestone `gate` / `phase` kinds as completion triggers.** They are
  ordering/phase buckets (`MILESTONE_KINDS`, `planning.py:95`); only
  `kind='delivery'` participates in `CP(P)` (§5). Promoting other kinds to
  completion triggers is a real-consumer decision, deferred.

---

## 9. Risk / Verification Notes

- **Read-only design; no code, migration, or config changed on this card**
  (Phase 3 two-stage contract: design precedes build). This card adds
  **zero** schema, **one** closed decision-code set (`CP_*` + `CD_*`,
  service-side only), and **one** pre-existing write site wired up
  (`COMPLETED` projection, C3).
- **`Task.status` divergence is a feature, flagged.** `Task.status='done'`
  (Run fact, `task_completion.py:137-140`) can be set while the Review lane
  is still in REQUEST_CHANGES/REWORKING — exactly as the review card §10
  flags. The completion lane is the **only** place the two are joined, by
  the SEALED + current-valid-approving-review predicate, never by
  `Task.status` alone. No downstream card may "fix" the divergence by
  re-coupling them.
- **The fail-open defect stays out.** `TaskCompletionGate._fail_open`
  (`verification.py:635-642`, C8) is a separate fix task with independent
  review. `CT`/`CW`/`CP` read ledger proof rows, not the gate's LLM call,
  so the defect cannot satisfy a completion term — but until that task
  lands, a gate *error* still marks `Task.status='done'`; that remains a Run
  fact and is still not a completion input. Named here so no card absorbs
  the fix.
- **Staleness by rework is the intended fail mode.** A rework that supersedes
  a `SEALED`+approved set after an evaluation: the old approving row
  automatically drops out of the current-valid-review query (review card
  §3.4), so the *next* evaluation fails with `CP_OPEN_REQUEST_CHANGES` /
  `CP_NOT_SEALED` / `CP_NO_APPROVING_REVIEW` (whichever term fires first in
  stack order) — never a stale `CP_OK`. The decision row of the *old*
  evaluation remains in the ledger as history (G2), clearly superseded by
  the newer failing row. Consumers must read the **latest** decision row per
  scope, not "some `CP_OK` row" — stated as the only correct read.
- **The `COMPLETED` projection is idempotent by construction.** The write
  site recomputes `CP(P)` before writing; a second call on an already-
  `COMPLETED` project returns the existing value without rewrite (terminal
  group, `project.py:39-44` docstring). A rework that supersedes after
  `COMPLETED` does **not** re-open the enum (no `COMPLETED → EXECUTING`
  transition exists and none is added); the affected scopes simply stop
  evaluating `CP_OK` live until the new cycle seals + approves again — the
  enum stays the *publication*, the predicate stays the *fact*.
- **WP-level failure reporting stays bounded.** One decision row per WP
  evaluation; the failing task id + its `CP_` code go into the payload
  (bounded, ≤32 KiB, parent invariant 11 shape); the per-task rows are the
  task-scope evaluations that produced the codes. No unbounded fan-out.
- **Verification for this card.** §3.5 covers every Root §5 term
  (required artifacts / required evidence / review decision / dependency
  completion / rework status / acceptance criteria via the frozen
  `Task` acceptance inputs the reviewer consumed / tenant boundary /
  unresolved blockers → one named code each, fail-closed). §4/§5/§6 cover
  the three requested criteria levels + delivery. §7 assigns every concern
  to exactly one owner. §9 restates the avoid-list for the builder.
  Ready for independent review (card `t_fa30ea5d`'s lane) and for the
  builder card to consume the `CP_*` / `CD_*` contracts.

---

## 10. Handoff to Downstream Cards

- **Builder card (`t_436ddafb`, implementation):** implement the
  **completion-lane guard layer** — the `CP_*` / `CD_*` code sets (§3.5 /
  §6.2), the `CT`/`CW`/`CP` predicate evaluators (§3.1 / §4 / §5, all
  reads over the *parent's* two ledgers + frozen task graph), the one
  `COMPLETED` projection write site with its three preconditions (§5), and
  the per-evaluation `kind='structured'` decision row (§3.3, C5). **Do
  not** add a completion table or column (C0); **do not** read
  `Task.status` / `final_answer` / gate verdicts as predicate inputs (R-A);
  **do not** touch the frozen model list of the parent docs; **do not** fold
  in the `TaskCompletionGate._fail_open` fix (C8 — separate card). The
  delivery *record* belongs to `t_fa30ea5d`, not this builder card.
- **`t_fa30ea5d` (Delivery lane implementation + independent review):**
  consume C-D1…C-D4 (§6) as the delivery contract: gate on `CP_OK`, cite
  only `SEALED` + current-valid-approving artifacts, closed destination
  vocabulary, bounded record fields, decision-row provenance. This card's
  `CD_*` codes are the contract's closed set.
- **The `TaskCompletionGate` fail-open fix card (separate, Root §5):**
  unchanged from both parent docs — it makes the gate fail-closed on its own
  error. Convergence point with this card: after that fix lands, a gate
  error can no longer even produce the `Task.status='done'` Run fact; the
  completion predicate is unaffected either way (it never read it).

---

*Design produced by aco-architect, task t_4185daed, at main = 8f030792
(tag PHASE_3_CLOSED), consuming PHASE_4_ARTIFACT_EVIDENCE_V1_DESIGN.md
(t_f19aae89 @ b2d4ec51) and
PHASE_4_INDEPENDENT_REVIEW_REWORK_V1_DESIGN.md (t_dbb0c0dd @ 8448b7ba),
tracing every decision to PHASE_4_AUDIT_REPORT_T15e05452.md Q10/Q11/Q12/Q13,
re-verified against the live tree at this baseline (task.py 3-enum +
TaskDependency, task_completion.py 137-140/141-144, planning.py
WorkPackageTask/requires_independent_review/MILESTONE_KINDS,
project.py 10-enum, assignment_service.py PL_REVIEWER_NOT_INDEPENDENT,
verification.py 635-642 fail-open defect).*
