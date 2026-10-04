# Execution-to-Review State-Machine & Fail-Open Audit (Phase 0→4)

Task: `t_70258786` (aco-architect). Read-only audit; no business code modified.
Baseline: `main` == `origin/main` == `676b1caf` (worktree `wt/t_70258786`).

Scope per the card: the three seams — (a) Assignment → Runtime, (b) Execution →
Artifact/Evidence, (c) Evidence → Independent Review — plus the "Agent reports
success but system marks complete" surfaces, fail-open bypasses of completion
gates, missing evidence requirements for delivery, and whether the Runtime in
use is the Phase 2F verified instance (not a duplicate).

Findings are tagged FACT (verified against live source at a file:line) /
OBSERVATION / INFERENCE / UNKNOWN, and classified BLOCKING / NON-BLOCKING /
DEFERRED / UNKNOWN.

---

## A. Runtime identity (question 5: reuse of the Phase 2F verified Runtime?)

**FACT — single Runtime, no duplicate.** There is exactly one durable Runtime
worker builder and one command worker:
- `worker_service.py:203 build_runtime_worker_components` is the only place a
  `RuntimeCommandWorker` is composed; `main.py:335`
  `running_runtime_worker_context(settings=...)` is the only production
  start site (via `main.py` lifespan).
- Every task/trigger/heartbeat/a2a/chat/group/planning entry point funnels into
  `RuntimeCommandIntake.start_run` (task_executor.py:114, trigger_runtime/intake.py:304,
  heartbeat_runtime.py:109/170/239, a2a_runtime.py, group_handoff.py:852,
  planning_scheduler.py:531). One shared command inbox + one
  `agent_runtime/command_worker.py` claim loop.
- `git log PHASE_2F_CLOSED..HEAD` shows the only runtime-spine change after
  2F is `147ebf93` (the fail-closed `TaskCompletionGate` rename, §E-1 below);
  the 8 frozen 2F spine files were otherwise untouched (confirmed by the Phase 4
  convergence report's per-file diff re-check on main).

**VERDICT:** the Assignment→Runtime seam reuses the verified Phase 2F instance.
No second task-graph, no second command worker, no second completion write
besides the one named in §B.

**OBSERVATION (latent, NON-BLOCKING).** `task_executor.py:196 execute_task` and the
`api/tasks.py:194` / `api/tasks.py:331` auto-enqueue/trigger paths still call the
legacy `enqueue_task_runtime` with `actor_user_id=None, attempt_id=None` (stable
key + `task.created_by` actor). These are dormant-only when the durable v2 gate is
the real path; they remain reachable transport routes but route into the SAME
verified `RuntimeCommandIntake`, not a parallel runtime. No duplication.

---

## B. Assignment → Runtime seam

**FACT (fail-closed, correct).** `task_execution_service._gate` (P1–P8, first
failure wins, task_execution_service.py:316-385) refuses before enqueue:
- P3 todo-only + terminal guards; P1/P8 tenant + dangling-project; P2 project
  status set; P5 agent availability; P7 `decide_runtime_v2` (no legacy fallback —
  a disabled v2 returns `RUNTIME_V2_DISABLED`, nothing enqueued, line 377);
  P4 dependency readiness re-run.
- The assignment lane (`assignment_service.py`) is fail-closed on
  `PL_NO_CANDIDATE_AGENT` / `PL_RESOURCE_CONFLICT` / `PL_REVIEWER_NOT_INDEPENDENT`
  and writes only the single `Task.agent_id` fact (line 892-899). CONF-5 /
  `max_parallel_tasks` is **advisory only** (never fails a V1 plan) — the deferred
  D-2 concurrency cap (assignment_service.py:515-550).

**VERDICT:** Assignment → Runtime is a clean fail-closed gate; a task cannot reach
the Runtime without passing P1–P8. No "report success, mark complete" shortcut at
this seam.

---

## C. Execution → Artifact/Evidence seam

**FACT — the Runtime does NOT auto-mint ledger evidence.** The terminal settlement
handler `TaskRuntimeCompletionHandler.handle` (agent_runtime/task_completion.py:68-161)
only:
- flips `task.status` to `done` (todo) / `pending` (supervision / cancelled /
  failed), stamps `completed_at`, and appends exactly one `TaskLog` receipt.
It writes **no** `ArtifactRecord` and **no** `EvidenceRecord`.

The `artifact_records` / `evidence_records` ledgers are populated ONLY by the
explicit Phase 4 lanes:
- `artifact_record_dao.add_artifact` — in production source the sole callers are
  `review_rework_service.record_rework` and **tests**.
- `evidence_record_dao.add_evidence` — `completion_service` (C5 decision row),
  `review_rework_service.record_review / record_rework / record_execution_evidence`,
  and tests.

Consequence verified in the Phase 4 E2E (`tests/test_phase4_e2e_chain_acceptance.py`
`test_runtime_run_produces_artifact_and_evidence`, lines ~1150-1205): after the
frozen spine drives the Run to `run_completed` and settles `task.status='done'`,
the test **manually** calls `artifact_record_dao.add_artifact` +
`review_rework_service.record_execution_evidence` to mint the execution-linked
artifact and the `tool_result` evidence. The Runtime path itself does not.

### C-1 — Two independent notions of "done" (question 13, state-machine split)
**OBSERVATION / FACT.** The system carries two orthogonal completion axes:
1. `Task.status == 'done'` — set by Runtime settlement, **evidence-agnostic**.
2. `CT(T)`/`CW(W)`/`CP(P)` — the Phase 4 completion lane, which **requires**
   ≥1 SEALED artifact + a current-valid `outcome='pass'` disjoint review
   (completion_service.py:445-494) and **never reads `Task.status` / `final_answer`
   / the gate verdict** (R-A, stated at completion_service.py:22-24).

A task can be `done` (status) yet `CP_NO_WORK`/`CP_NOT_SEALED`/`CP_NO_APPROVING_REVIEW`
in the evidence lane, and a WP/Project can only reach `COMPLETED` via the evidence
lane. This split is **deliberate** (design pins CT off the status machine), so it
is not a defect — but it is the "second completion semantics" the Root asks about.

### C-2 — The DAG unblocks on status, not on evidence (security-boundary note)
**FACT.** `task_graph_service.ensure_ready` (task_graph_service.py:403-416) unblocks
a dependent task purely when its upstream `Task.status == 'done'`. So a downstream
task may be **executed** (enqueued into the Runtime) as soon as its dependency is
status-`done`, with **no review/evidence requirement**. Evidence gating happens
later, at the completion/delivery lane — not at the execution gate. This is by design
(execution gate = DAG readiness; completion gate = ledger evidence), but it means
"a task with no valid evidence can still RUN". Not a completion bypass; flagged as
the boundary between the two axes.

---

## D. Evidence → Independent Review seam

**FACT (fail-closed).** `review_rework_service.record_review` (plan_review, lines
204-300):
- G1 no self-review: `if reviewer_agent_id in builder_agent_ids` → `RV_REVIEW_NOT_INDEPENDENT`
  (line 237).
- I-2: a review must cite ≥1 **current** (non-superseded) artifact (line 248).
- I-8: bounded payload; R3: APPROVE seals the current set one-way, double-seal →
  `RV_ALREADY_SEALED`.
- DAO backstop `EV_REVIEW_NOT_INDEPENDENT` re-asserts the same set at insert
  (`add_evidence(reviewer_builder_agents=...)`).
`record_rework` is fail-closed on I-4 (new `test_result`/`file_revision` proof
required, lines 350-356) and I-4 kind-closure (a builder-supplied `kind='review'`
row is rejected at the plan tier AND at the DAO boundary, lines 364-369, 690-706).

### D-1 — Reviewer independence is only as strong as the caller's builder set
**OBSERVATION / INFERENCE (NON-BLOCKING security boundary).** Both the G1 mirror
(`reviewer_agent_id in builder_agent_ids`, line 237) and the DAO backstop rely on a
**caller-supplied** `builder_agent_ids` / `reviewer_builder_agents` set. If a caller
passes an empty set, `in` is always False and any agent — including a genuine
builder — passes as "independent." There is no authoritative auto-derivation of the
WP's builder set from the plan; the set is a parameter to `record_review` /
`evaluate_*` / `deliver`. The invariant-13 guarantee therefore holds only under the
assumption that the transport computing the set is honest/correct. Because these
lanes have **no live HTTP consumer** (§G), the exposure is currently theoretical;
it becomes real the moment a transport wires them and must be made to derive the
builder set from the owning WorkPackage (single authority), not accept it blind.

### D-2 — Declared-but-unreachable closed codes
**OBSERVATION (minor).** `RV_NO_REVIEWER`, `RV_SUPERSEDED_SET`, `RV_INCONCLUSIVE`
are members of `REVIEW_RESULT_CODES` (review_rework_service.py:64-74) and exported
in `__all__`, but no `return` path in `plan_review`/`record_review`/`record_rework`
produces any of the three. `RV_SUPERSEDED_SET`'s documented case (a verdict citing a
fully-superseded set) is actually caught earlier by I-2 as `RV_NO_CURRENT_ARTIFACTS`.
Dead vocabulary, not a live bypass.

---

## E. "Agent reports success but the system marks complete" surfaces

### E-1 — `completion_gate_exhausted`: semantic gate waived after the repair budget
**FACT (by-design, TESTED, NON-BLOCKING residual).** In
`node_executor._verify` (node_executor.py:1220-1258), when the semantic
`TaskCompletionGate` keeps returning `task_completion_repair_required` and the
repair attempts exceed `max_verification_repairs` (=10 in
`worker_service.py:266`), the code synthesizes
`exhausted = VerificationResult(outcome="pass")` and drives the Run to
`status="completed"`, `reason="completion_gate_exhausted"`.
- The **deterministic** verifier still must have passed (line 759, tool-ledger
  integrity is enforced); only the **semantic** "did we actually do the task" gate
  is bypassed once the budget is spent.
- `tests/test_agent_runtime_node_executor.py:1747-1758` asserts exactly this:
  `status == "completed"`, `reason == "completion_gate_exhausted"`.
- Settlement then marks `task.status = "done"` (task_completion.py:138).

So there **is** a path where a task is marked complete/done although the semantic
completion gate never returned "pass" — after 10 repair attempts it is treated as
complete anyway. This is the concrete "agent success accepted without a gate pass"
surface. It is bounded (a fixed attempt budget) and intentional (a fail-open
exhaustion chosen over an infinite repair loop), and it is inherited from the Phase
2F spine, but it is a genuine fail-open of the semantic completion gate and belongs
on the fail-open inventory as a NON-BLOCKING residual. The deterministic half of the
gate remains fail-closed, so tool/ledger integrity is NOT waived.

### E-2 — `TaskRuntimeCompletionHandler` marks `done` without any evidence
**FACT.** See §C: the handler flips `todo` → `done` on `run_completed`
(task_completion.py:137-139) with **no** artifact/evidence check. "Done" here means
"the Run settled cleanly," not "the deliverable is verified." This is consistent
with the §C-1 split (status ≠ completion) and is not itself a bypass, but it is the
mechanism by which an agent's clean settlement becomes a `done` task that the
Phase 4 lane may still refuse (no artifacts → `CP_NO_WORK`).

### E-3 — Legacy `manage_tasks` can write `task.status='done'` directly
**OBSERVATION (latent, NON-BLOCKING).** `_manage_tasks` (agent_tools.py:9822-9895,
`update_status` branch) does `task.status = args["status"]` and, when "done",
stamps `completed_at` — a direct task-status write that bypasses both the Runtime
and the evidence lane. The tool is marked OBSOLETE and its `Tool` row is deleted at
seed (`tool_seeder.py:408 OBSOLETE_TOOLS = [..., "manage_tasks"]`), and the durable
Runtime tool-step only dispatches registered/enabled tools in the Run's schema
(`tool_step_service.py:334 tool_not_enabled`), so in the verified v2 path this branch
is not reachable. It remains a latent bypass **only if** the legacy `execute_tool`
path (agent_tools.py:4650) is ever re-exposed to a tool call. Flag for removal or
a hard guard; classify UNKNOWN-until-a-transport-uses-it → NON-BLOCKING latent.

---

## F. Review → Completion → Delivery gates

**FACT (fail-closed).** `completion_service` CT/CW/CP pure core is fail-closed to the
`CP_EVAL_ERROR` catch-all for every unknown/unreadable input (it never returns
`CP_OK` on a bad shape); the closed code set is 11 `CP_*` values
(completion_service.py:113-157). The single `COMPLETED` write site is
`project_dao.transition` (completion_service.py:889), the only `COMPLETED` writer in
production source (verified: the only `status="COMPLETED"` writes are
completion_service.py:890 + analysis INITIALIZED/ANALYZING + intake INITIALIZED —
none else). `delivery_service.deliver` gates on a **fresh** `CP_OK`, re-checks the
cited set against current-valid SEALED+approved ledger state, and writes a delivery
record **only** on `CD_OK` (delivery_service.py:359-451); every non-OK code writes
nothing.

### F-1 — `COMPLETED` can publish without its citing C5 decision row
**OBSERVATION / INFERENCE (NON-BLOCKING).** `evaluate_project` publishes
`COMPLETED` on `code == CP_OK` (completion_service.py:888) **regardless of whether
`_append_decision` wrote the C5 row**. `_append_decision` returns `None` on a payload
overrun (line 1087-1088) or on `ArtifactEvidenceClosedError`/`IntegrityError`
(line 1108-1113) while still returning the computed `CP_OK`. So the terminal
`COMPLETED` projection can occur **without** its durable decision row. The delivery
lane is protected (delivery_service.py:376 fails `CD_EVAL_ERROR` when
`gate.decision is None`), but the **Project COMPLETED state itself** is not gated on
the C5 row existing. This is the design's "complete-or-absent" rule applied to the
C5 row — intentional, but it is a named evidence gap: a `COMPLETED` project may have
no citing decision row. NON-BLOCKING.

### F-2 — channel / published_page deliveries are stuck PENDING (deferred consumer)
**FACT / DEFERRED.** A `project_record` delivery is terminal at decision
(delivery_service.py:435-447, `_PROJECT_RECORD_TERMINAL_STATE`). `channel` and
`published_page` records stay `PENDING` "until the owning transport drives
`DeliveryRecordDAO.transition_state`" (delivery_service.py:44-46) — **no such
transport/consumer exists in production source** (grep of `transition_state`
finds only the DAO + model, no caller). So a `channel`/`published_page` delivery
record can never reach `DELIVERED` in the shipped V1. This is an explicitly
deferred item (external publish platforms, C-D2 / Root §6), not a defect, but it
means those two destination kinds are non-functional end-to-end today.

---

## G. Cross-cutting: no live transport for the Phase 4 lanes

**FACT (major evidence gap).** The completion, delivery, and review/rework lanes
(`completion_service`, `delivery_service`, `review_rework_service`) and the
plan→execution bridge (`plan_execution_service.enqueue_plan_tasks`) have **no HTTP
/WebSocket transport consumer**. `grep` across `backend/app/api/*.py` and the
`main.py` `include_router` set returns no route that calls any of
`evaluate_task/work_package/project`, `deliver`, `record_review/rework`, or
`enqueue_plan_tasks`; the only callers are `backend/tests/*`. So:
- The entire Phase 4 delivery/completion machinery is **wired to tests only** in the
  current source. The capability is real and verified at the unit/E2E tier, but it
  is **not reachable from a live entry point** until a transport is added.
- `build_execution_evidence`/`record_execution_evidence` (guarantee #1) is likewise
  only called from `record_rework`/`record_execution_evidence` + tests.

This is the largest "documented capability vs. real source" gap: the lanes are
delivered and tested, but the production consumer that would (a) auto-mint evidence
after a Run, (b) drive a disjoint reviewer, (c) call completion, and (d) deliver,
does not exist yet. Until it does, §C-1/§E-2 stand: a `done` task has no evidence,
and nothing in the shipped product advances it through review→completion→delivery.
Classify DEFERRED (a V1 transport gap, not a wrong implementation), but it is the
single most load-bearing evidence gap for the Foundation.

---

## H. Consolidated answers to the card's acceptance list

State-machine conflicts / security boundaries where completion can occur **without
valid evidence**:

1. **E-1 `completion_gate_exhausted`** — after 10 semantic-gate repair attempts the
   Run is marked `completed` (→ task `done`) even though the semantic gate never
   returned "pass." Deterministic/tool-ledger integrity is still enforced; only the
   semantic check is waived. Bounded, intentional, tested. **NON-BLOCKING fail-open.**
2. **C-1/E-2 status-vs-evidence split** — `Task.status='done'` (Runtime) is
   evidence-agnostic; the DAG unblocks execution on `done` (C-2) with no review.
   Not a completion bypass (the Phase 4 lane still requires evidence for
   `COMPLETED`/delivery), but "done" ≠ "complete" and the two must never be
   conflated. **NON-BLOCKING (by-design split).**
3. **F-1 C5-row gap on COMPLETED** — `Project.status=COMPLETED` can publish without
   its citing C5 decision row (complete-or-absent). Delivery is guarded; the
   COMPLETED state is not. **NON-BLOCKING evidence gap.**
4. **D-1 reviewer-independence is caller-supplied** — G1/invariant-13 hold only under
   an honest builder-set; no authoritative WP-derived builder set. **NON-BLOCKING
   security boundary** (theoretical until a transport wires the lanes).
5. **E-3 legacy `manage_tasks`** — latent direct `task.status='done'` write; tool is
   OBSOLETE/deleted and unreachable from the durable v2 path. **NON-BLOCKING latent.**
6. **G.1 no live transport** — Phase 4 review/completion/delivery are test-wired only;
   evidence is never auto-minted after a Run in the shipped product. **DEFERRED
   evidence gap (the main one).**
7. **F-2 channel/published_page stuck PENDING** — no `transition_state` consumer.
   **DEFERRED.**

**No BLOCKING fail-open found.** The hard, load-bearing gates are correct and
fail-closed:
- Assignment (CONF/REV codes), execution P1–P8 + `RUNTIME_V2_DISABLED` (no legacy
  fallback), `record_review` G1/I-2/I-8/R3, `record_rework` I-4 + kind-closure +
  invariant-13 DAO backstop, CT/CW/CP `CP_EVAL_ERROR` catch-all, and
  `delivery_service.deliver` writing **nothing** on any non-`CD_OK`.
- The `TaskCompletionGate` semantic-ERROR fail-open was already fixed
  (`147ebf93`): a gate **error** now fails the Run closed (`verification.py:634-653`
  `_fail_closed`, `TaskRuntimeCompletionHandler` leaves the task not-done). The
  residual is only the **exhaustion** path (E-1), which is a separate, bounded,
  intentional behavior.

---

## I. Deferred / known-residual inventory (cross-phase)

- **D-2 / CONF-5** — `max_parallel_tasks` advisory only; global concurrency cap +
  lane key deferred to a future runtime change (assignment_service.py:515-550).
- **D-4 / supervision scheduler** — `Task.remind_schedule` / `supervision_channel`
  are storage-only; no consumer in `scheduler.py` / `trigger_daemon.py` /
  `heartbeat_runtime.py` drives them (Phase 3 Final Gate, carried to 4).
- **C-D2 / delivery recall + external publish** — RECALLED/supersession state and
  external publish platforms deferred; `channel`/`published_page` are PENDING-only
  (F-2).
- **G / Phase-4 transport** — no live entry point for review/completion/delivery
  (the largest gap; §G).
- **C5-row complete-or-absent** — `COMPLETED` can lack its citing decision row
  (F-1).
- **RV dead codes** — `RV_NO_REVIEWER`/`RV_SUPERSEDED_SET`/`RV_INCONCLUSIVE`
  declared but unreachable (D-2).
- **manage_tasks legacy write** — obsolete tool, latent direct-status write (E-3).

---

## J. Recommended follow-ups (do NOT hot-fix here; Root is read-only)

1. [NON-BLOCKING, decision needed] Decide whether `completion_gate_exhausted`
   (E-1) should fail the Run instead of completing it, or stay a bounded
   exhaustion. If the Foundation wants a hard "no completion without a gate pass,"
   this is the one change; it is currently intentional+tested, so it is a
   policy decision, not a bug. Owner: aco-architect design card.
2. [DEFERRED, highest value] Add the Phase 4 transport that (a) auto-mints
   execution-linked artifact/evidence after a `run_completed` Run, (b) schedules a
   disjoint reviewer, (c) drives completion, (d) delivers. Until then §C/§E/§G hold
   and delivery is test-only.
3. [NON-BLOCKING, security] Make the reviewer-independence builder set
   WP-authoritative (derived from the owning WorkPackage, single source) instead of
   a caller parameter, and have the DAO backstop derive the same set (D-1).
4. [NON-BLOCKING, cleanup] Delete or hard-guard the obsolete `manage_tasks`
   direct-status write (E-3) so it cannot resurface as a bypass.
5. [NON-BLOCKING, consistency] Either gate the `COMPLETED` publication on the C5
   row actually being written, or document that a `COMPLETED` project may lack a
   citing decision row (F-1).
6. [DEFERRED] Remove the unreachable `RV_*` dead codes or wire the paths that own
   them (D-2).

## K. Verification performed (read-only)
- Source reads: assignment_service.py, task_execution_service.py, task_executor.py,
  agent_runtime/{task_completion,command_worker,node_executor,worker_service,
  verification,config,persistence}.py, completion_service.py, review_rework_service.py,
  delivery_service.py, artifact_evidence_resolver.py, task_graph_service.py,
  planning_service.py, agent_tools.py (manage_tasks), tool_seeder.py, api/tasks.py,
  main.py (router set), dao/{project_intake,artifact_evidence,planning}.py,
  models/{task,project,artifact_evidence,delivery_record}.py.
- Git: `git log PHASE_2F_CLOSED..HEAD` on the 8 frozen spine files → only `147ebf93`
  (the fail-closed gate rename); `git show 147ebf93` confirms scope.
- Grep: production callers of `add_artifact`/`add_evidence` (only the 4 Phase-4
  lanes + tests); transport consumers of the Phase-4 services (none); `COMPLETED`
  write sites (single); `legacy_runtime`/duplicate worker builders (single);
  `completion_gate_exhausted` (node_executor + its test).

No live-system / database execution was performed (static read-only audit against
`main` source). Live-DB behavioral claims (e.g. whether a real Run ever hit the
exhaustion path, or whether any Phase-4 transport was added out-of-band) are
UNKNOWN at this tier.
