# Security Boundaries, Tenant Isolation & Deferred High-Risk Items — Read-Only Audit

- Task: t_cdd13960 (child of synthesis `t_00dddf61`; Root security/deferred lane of the final-architecture audit)
- Baseline: `wt/t_cdd13960` @ `676b1caf` (main == Phase-4-closed). Clean tree (`git status --porcelain` empty).
- Method: Inspect → Trace → Verify. Every load-bearing claim cites live `file:line` in this worktree.
  Historical phase reports are used only to *locate* an item; its current status was re-probed
  against source (not trusted from the report).
- Labels: [FACT] read in source; [OBS] observed pattern; [INF] inference; [UNKNOWN] not verifiable from source.
- Classification axis (acceptance): **BLOCKING** (must fix before Foundation can be recommended),
  **NON-BLOCKING** (safe as-is, cosmetic / doc / future-funnel), **DEFERRED** (named, by-design,
  evidence-labeled carry-over), **UNKNOWN** (cannot be settled from source).
- Read-only. No product code or frozen file modified by this audit.

---

## 0. Verdict up front

All three named deferred items re-verify as **DEFERRED (non-blocking)**, and **none is a BLOCKING
open risk**. The one HIGH defect this lane was ever meant to catch — builder-authored
`kind='review'` self-review (F1) — is **CLOSED on main** (verified below, §B3). The
`TaskCompletionGate` fail-open defect is also **CLOSED**. The residual open items are
named/by-design capability gaps (advisory parallelism cap, no supervision consumer, no
capability→reviewer-Agent join) that the Phase 3/4 gate reports already carry as **D-2 / D-4 / D-3**
deferrals. No new BLOCKING risk surfaced in the tenant-isolation or security-boundary layers.

---

## A. Tenant isolation & security boundaries — consistent across phases [FACT]

Isolation is real and centralized; it is not scattered per-endpoint.

- **Centralized SELECT scoping (load-bearing).** `dao/base.py:139-174`
  `_inject_tenant_scope` is a `Session.do_orm_execute` event that rewrites *every* tenant-owned
  ORM SELECT to add `tenant_id = <active>` via `with_loader_criteria(... include_aliases=True)`.
  It fires on the synchronous `Session` layer beneath `AsyncSession`, so a missed business-level
  `tenant_id` filter in an API *or* DAO path cannot disclose another tenant's rows. Scope is
  applied only when `_tenant_ctx` is non-null (`dao/base.py:150-152`), i.e. tenant context is set.
- **Tenant context source.** `dao/base.py:122` `ContextVar("tenant_ctx")`. On the HTTP path it is
  set by `TenantContextMiddleware` (`main.py:364`, registered alongside `TraceIdMiddleware` :360 and
  CORS :373-374). On the background/daemon path callers must use the explicit `tenant_context()`
  context manager (`dao/base.py:177-193`); `task_executor.py` / runtime intake use it.
- **Scoped-model detection.** `dao/base.py:125-136` `_is_tenant_scoped_model`: a model is
  tenant-scoped iff `__tenant_scoped__` is set OR it has a *non-null* `tenant_id` column. Legacy
  nullable-tenant tables opt in via `__tenant_scoped__ = True`.
- **Task is tenant-scoped but transitional.** `models/task.py:44` `Task.__tenant_scoped__ = True`
  with `models/task.py:47-49` a **nullable** `tenant_id` FK. So Task isolation is enforced by the
  opt-in flag, not the column-nullability rule — a deliberate transitional choice. `TaskDependency`
  `models/task.py:149` is likewise `__tenant_scoped__ = True`.
- **Completion / delivery tenant containment.** `completion_service.py:455-471`
  `CP_TENANT_MISMATCH` rejects a current-valid review or sealed artifact whose `tenant_id`
  disagrees with the task's tenant before any `CP_OK`. The closed `CP_*` set is fail-closed
  (`completion_service.py:149` includes `CP_REVIEW_NOT_INDEPENDENT`, `CP_TENANT_MISMATCH`; unknown
  input lands in `CP_EVAL_ERROR`, never `CP_OK` — see §B3).
- **Auth / RBAC boundary.** `core/security.py` `create_access_token` (:129) embeds the caller's
  `tenant_id` in the JWT; `get_current_user` / `get_authenticated_user` (:169/:192) and
  `require_role` / `get_current_admin` (:215/:227) gate admin routes. Per `backend/AGENTS.md`
  "Enforcement", enforcement is expected at the DAO/mutation boundary (the centralized SELECT
  scoping above), with upstream preflights as guidance only — consistent with what is present.
- **Artifact/evidence lanes inherit scoping.** `artifact_evidence_dao.py` /
  `review_rework_service.py` run under `TenantScopedBaseDAO` + `tenant_context`, and the Phase 4
  lane's independent review confirmed tenant isolation "on all reads/writes" (guarantee #4) PASS.

[OBS] The centralization is a strength (one event owns SELECT scoping), and the residual nuance is
that isolation is *context-dependent*: with `tenant_ctx` null (no active tenant), scoping is not
applied, which is correct for platform-admin / migration paths but means any *new* background path
that forgets `tenant_context()` reads/writes unscoped. That obligation is documented
(`dao/AGENTS.md` §8.4) but is a standing discipline, not a compile-time guarantee. NON-BLOCKING.

---

## B. Re-verification of the three named deferred items

### B1. `max_parallel_tasks` — advisory, no runtime cap → **DEFERRED (D-2, by design)** [FACT]

- Stored as a nullable hint: `models/planning.py:304`
  `max_parallel_tasks: Mapped[int | None] = mapped_column(Integer, nullable=True)`; carried into
  the planner as `WpContext.max_parallel_tasks` (`assignment_service.py:174`), sourced from
  `wp.max_parallel_tasks` at `assignment_service.py:663`.
- Consumed **only** as the advisory `PL_PARALLELISM_ADVISORY` in
  `assignment_service.py:515-550` `check_parallelism_advisory` (CONF-5): when
  `concurrent_count > ctx.max_parallel_tasks` it *appends a ConflictEntry* but the entry is an
  **advisory**, not a `fail_closed` code — `assignment_service.py:544`
  `"(advisory only in V1)"` and the module docstring (CONF-5) says a breach is "reported, never
  refused".
- **No runtime enforcement** anywhere in `task_execution_service` / `plan_execution_service`
  (grep of those files returned no `max_parallel_tasks` / concurrency-cap site). `AGENT_RUNTIME_COMMAND_CONCURRENCY`
  is a *global* worker bound, not a per-project/per-task cap.
- Phase 3 carry-over: `docs/PHASE_3_FINAL_GATE_REPORT.md` (Flag 2 + D-2),
  `docs/architecture/PHASE_3_PLANNING_DOMAIN_BOUNDARY_DESIGN.md:91,196` (P16 "per-project cap is a
  genuine Phase 3 gap; kept minimal, not a new scheduler"), `docs/PHASE_3_CONVERGENCE_REPORT.md:133`.

Classification: **DEFERRED / NON-BLOCKING (by design).** Not a regression; the predicate is kept
visible (advisory) so a later per-project/per-task runtime cap (D-2) can be wired without a planning
rework. [FACT verified against live source, not the report.]

### B2. Supervision / deadline scheduler consumer → **DEFERRED (D-4, by design)** [FACT]

- Supervision is **storage-only**: `models/task.py:72-76` `supervision_target_user_id`,
  `supervision_target_name`, `supervision_channel`, `remind_schedule` are columns with **no driver**.
- **No consumer** reads these fields: grep of `remind_schedule` / `supervision_channel` /
  `supervision_target_*` across `backend/app` returns only *write* sites
  (`schemas/schemas.py:355-409`, `api/tasks.py:110-114`, `agent_tools.py:9846-9848`) and one
  *prompt-text* read (`task_executor.py:38-40`, which formats `supervision_target_name` into the
  agent goal — it does not schedule or remind). Grep of the scheduler/daemon family
  (`scheduler.py`, `trigger_daemon.py`, `heartbeat_runtime.py`, `heartbeat.py`) for
  `supervision` / `remind_schedule` returned **zero** matches.
- Phase 3 carry-over: `docs/PHASE_3_FINAL_GATE_REPORT.md` (G1 + D-4: "no consumer drives them.
  Deferred D-4; a future phase that assumes supervision auto-runs must first wire a real consumer
  (reuse `scheduler.py`, not a new engine)"); `docs/architecture/PHASE_3_EXECUTION_CHAIN_TRACE.md:234,484` (G1).

[OBS] **Misleading dead comment (NON-BLOCKING, doc-cleanliness):** `agent_tools.py:9862-9866` tells
the operator a supervision task "will remind {target} on schedule ({schedule})" with a comment
"reminder engine will pick it up". **No reminder engine exists** (see the zero-match grep above).
The string is a user-facing promise that the system does not keep — supervision tasks are
manual-trigger-only. This is a *false affordance in the tool result text*, not a security defect,
but it is the one place a human would be led to believe D-4 is already wired. Recommend a one-line
comment/doc fix (out of scope for this read-only audit; flagged for a cleanup card).

Classification: **DEFERRED / NON-BLOCKING (by design).** Safe as-is (nothing silently fails to run —
there is simply no auto-run promise kept in the *product*, only in a tool-result string). The
misleading comment is a NON-BLOCKING [OBS] sub-item.

### B3. Security — reviewer independence (invariant-13 / G4) → **F1 CLOSED; residual DEFERRED** [FACT]

The lane's core guarantee ("the reviewer must not self-review") was the single HIGH (F1) that
rejected the first Phase 4 review. Current state, re-probed against source:

- **F1 builder-authored verdict row — CLOSED on main.**
  - `review_rework_service.py:364-369` `plan_rework` now *rejects* any `kind='review'` row in
    `new_evidence` (I-4 kind-closure): `if any(e.kind == "review" for e in new_evidence_rows):
    return ReworkPlan(RV_INVALID_INPUT, ...)`.
  - `review_rework_service.py:702` `record_rework` backstops at the DAO: the new-evidence
    `add_evidence` loop passes `reviewer_builder_agents={builder_agent_id}`, so a slipped
    `kind='review'` row trips `EV_REVIEW_NOT_INDEPENDENT` (`artifact_evidence_dao.py:319-332`).
  - Both fixes are ancestors of HEAD (`git merge-base --is-ancestor 76086d65` and `f185f461`
    against `HEAD` → true). Live tests: `test_rework_new_evidence_kind_closed_rejects_review_row`
    and the live-tier two-gate rejection in `test_artifact_evidence_review.py`.
- **invariant-13 guard is opt-in (DEFERRED nuance, contained).** `artifact_evidence_dao.py:286`
  `reviewer_builder_agents` defaults to `None` and `:326` only enforces when
  `reviewer_builder_agents is not None`. The guard is **application-layer only** — the DDL
  CHECK constraints (`ck_artifact_records_source/seal`, `ck_evidence_records_source/outcome`) do
  **not** encode independence (an agent row has no "is a builder" column). Independence is held by
  every call path: `record_review` → `plan_review` G1 check `review_rework_service.py:237`
  (`reviewer_agent_id in builder_agent_ids → RV_REVIEW_NOT_INDEPENDENT`) *and* the DAO backstop
  at `:602-606`; `record_rework` at `:702`. [OBS] A *future* caller that invokes `add_evidence`
  without the builder set would get no verdict-time guard — mitigated today by all three call
  sites passing it, and further contained at the completion lane (below).
- **Read-side containment (independent of the write side).** `completion_service.py:481-493`
  `CP_REVIEW_NOT_INDEPENDENT` recomputes the current-valid review
  (`review_rework_service.py:177-201`, "latest non-superseded, cited-on-current-set") and rejects
  completion when the review's `created_by_agent` is in `builder_agent_ids`. So even if a
  builder-authored verdict row ever persisted, `CP_OK` would not issue. This is why the F1 defect
  was judged *contained* (HIGH but completion stays fail-closed) — and it remains the safety net.
- **Capability→Agent join gap — DEFERRED (D-3, Phase 3).** `docs/PHASE_3_FINAL_GATE_REPORT.md`
  (Flag 1 / D-3): V1 has no way to declare a *distinct* reviewer **Agent** through the shipped
  planner; reviewer disjointness is enforced at **assignment time** (REV-1/REV-2,
  `assignment_service.py` review DAG + `PL_REVIEWER_NOT_INDEPENDENT`) plus the read-side
  `CP_REVIEW_NOT_INDEPENDENT`. Named deferral, not a shipped defect.
- **`TaskCompletionGate` fail-open — CLOSED.** `verification.py:622` now uses
  `_fail_closed` (`verification.py:635-653`): all four gate-error paths
  (`invalid_completion_gate_identity`, `completion_gate_model_unavailable`,
  `completion_gate_call_failed`, `invalid_completion_gate_output`) return `outcome="fail"` with
  `details["code"]="completion_gate_error"` — completion stays unverified, Task not marked done.
  Commit `147ebf93` (independent review `t_675b2d56` APPROVE); `docs/PHASE_4_CONVERGENCE_REPORT.md`
  §6 confirms it is on main.

Classification: reviewer-independence is **F1 = CLOSED (NON-BLOCKING)**; the opt-in-invariant-13
guard and the capability→Agent join are **DEFERRED / NON-BLOCKING (by design, contained)**;
the fail-open gate is **CLOSED (NON-BLOCKING)**. No BLOCKING open risk.

---

## C. Risk inventory (acceptance table)

| # | Item | Classification | Live code reference(s) | Blocking? |
|---|------|----------------|------------------------|-----------|
| 1 | `max_parallel_tasks` advisory, no runtime cap | **DEFERRED** (D-2) | `models/planning.py:304`; `assignment_service.py:515-550` (`:524`, `:537`, `:544` "advisory only"); no cap site in `task_execution_service`/`plan_execution_service` | No |
| 2 | Supervision/deadline scheduler consumer | **DEFERRED** (D-4) | `models/task.py:72-76` (storage-only); zero-match grep of `scheduler.py`/`trigger_daemon.py`/`heartbeat_runtime.py`/`heartbeat.py` | No |
| 2a | Supervision tool-result string promises a reminder engine that does not exist | **NON-BLOCKING** [OBS] | `agent_tools.py:9862-9866` (misleading "reminder engine will pick it up") | No (doc-cleanliness) |
| 3 | Reviewer-independence (invariant-13 / G4) — F1 self-review | **DEFERRED→CLOSED** | F1 fixed: `review_rework_service.py:364-369` (I-4 kind-closure) + `:702` (DAO backstop); both ancestors of HEAD; live-test coverage in `test_artifact_evidence_review.py` | No (closed) |
| 3a | invariant-13 guard is opt-in, application-layer only (no DDL backstop) | **DEFERRED** / contained | `artifact_evidence_dao.py:286` (default `None`), `:326` (`is not None`); all 3 call paths pass the set (`:602-606`, `:702`); read-side net `completion_service.py:481-493` | No (contained) |
| 3b | No capability→distinct-reviewer-Agent join in V1 planner | **DEFERRED** (D-3) | Phase-3 gate report Flag 1; enforced instead by assignment-time REV-1/2 + read-side `CP_REVIEW_NOT_INDEPENDENT` | No |
| 3c | `TaskCompletionGate` fail-open | **DEFERRED→CLOSED** | `verification.py:622,635-653` `_fail_closed`; commit `147ebf93`; `docs/PHASE_4_CONVERGENCE_REPORT.md` §6 | No (closed) |
| 4 | Tenant isolation consistency across phases | **FACT / sound** | `dao/base.py:139-174` central SELECT scoping; `main.py:364` middleware; `models/task.py:44-49` transitional nullable-tenant opt-in | No |
| 4a | Isolation is context-dependent (no scoping when `tenant_ctx` is null) | **NON-BLOCKING** [OBS] | `dao/base.py:150-152`; discipline per `dao/AGENTS.md` §8.4 (background paths must use `tenant_context()`) | No |
| 5 | New open BLOCKING risk found by this audit | **NONE** | — | — |

**No item is BLOCKING.** The two HIGH-level defects this lane historically owned
(builder self-review F1; `TaskCompletionGate` fail-open) are both **CLOSED on main** and
independently re-reviewed. What remains is a set of **named, by-design deferrals**
(D-2 advisory parallelism cap, D-4 supervision consumer, D-3 capability→reviewer-Agent join)
plus two **NON-BLOCKING** observations (a misleading tool-result string; a context-dependent
tenant-scoping discipline).

---

## D. Evidence gaps / UNKNOWNs (carried forward, not newly introduced)

- **[UNKNOWN, live-DB]** Whether *production* `Task` rows actually use `ANALYSIS_PLANNING`
  provenance, whether live supervision rows carry `remind_schedule` values, and whether live
  `AgentSchedule`/supervision values are set. This is a read-only structural audit; it does not
  probe a live Postgres. A future phase that *relies* on supervision auto-running must first
  (a) probe production and (b) wire the D-4 consumer (reuse `scheduler.py`).
- **[UNKNOWN]** Any out-of-repo consumer that marks a `Project` `COMPLETED` outside this codebase's
  services (carried from the Phase 2C-3 audit; no such site found in `backend/app`).
- These UNKNOWNs are inherited live-DB questions, out of scope for a design/structural card;
  re-probe only when a phase depends on them.

---

## E. Recommendation (for the synthesizing orchestrator card `t_00dddf61`)

- **Foundation: no BLOCKING security / tenant-isolation / deferred-item risk surfaced.** The three
  named deferred items are all **DEFERRED, non-blocking, by design** and are already evidence-labeled
  in the Phase 3/4 gate + convergence reports. Both historical HIGH defects are CLOSED on main.
- The audit adds exactly **one** actionable NON-BLOCKING item worth a cleanup card: the
  `agent_tools.py:9862-9866` supervision string that promises a reminder engine which does not
  exist (false affordance; D-4 still open). Low risk, one-line doc fix.
- The two open DEFERRED capability gaps to surface in the final known-risk inventory (so the
  Foundation's "known-risk" section is honest):
  1. **D-2** — no per-project/per-task runtime parallelism cap (advisory-only today).
  2. **D-4** — no supervision/deadline consumer (supervision is manual-trigger-only).
  3. **D-3** — no planner path to declare a *distinct* reviewer Agent (independence is
     assignment-time + read-side contained).
- Optional hardening (non-blocking): make the invariant-13 independence guard a **DDL backstop**
  rather than application-only, or keep the read-side `CP_REVIEW_NOT_INDEPENDENT` as the stated
  containment (already present). This is a future-robustness choice, not a gate blocker.

*Produced by aco-architect on t_cdd13960, main @ `676b1caf` (Phase-4-closed), verified against live
source in the worktree — no code modified.*
