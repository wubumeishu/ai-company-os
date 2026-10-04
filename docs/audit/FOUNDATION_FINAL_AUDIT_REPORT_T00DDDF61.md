# Foundation Final Architecture Audit Report — Phase 0 → Phase 4 (Synthesized)

- Task: `t_00dddf61` (synthesis lane of Root `t_645d0566`, Foundation Final Audit).
- Baseline: `main == origin/main == 676b1caf` (Phase-4-closed). This worktree
  `wt/t_00dddf61` is at that baseline.
- Method: this report is the synthesis of three completed read-only audit lanes.
  No lane's conclusion is trusted from its handoff alone: every load-bearing
  claim re-stated here was **re-probed against live source in this worktree at
  `676b1caf`** before inclusion (§9 lists exactly what was re-probed and its
  result). No product code, frozen file, test, or migration was modified by
  any of the four audit cards.
- Inputs (all committed or committed-by-this-lane, see §8 Sources):
  1. `docs/architecture/PROJECT_PLANNING_CONNECTIVITY_AUDIT_T7858D198.md`
     (lane `t_7858d198`, commit `fc398196` on `wt/t_7858d198`)
  2. `docs/audit/PHASE_0-4_EXECUTION_TO_REVIEW_STATE_MACHINE_AUDIT.md`
     (lane `t_70258786`, baseline `676b1caf`; was uncommitted in that lane,
     committed by this synthesis lane)
  3. `docs/architecture/SECURITY_DEFERRED_AUDIT_TCDD13960.md`
     (lane `t_cdd13960`, commit `0d83900a` on `wt/t_cdd13960`)
- Labels: [FACT] verified against live source at file:line · [OBS] observed
  pattern · [INF] inference · [UNKNOWN] not verifiable from source.
  Severity axis: BLOCKING / NON-BLOCKING / DEFERRED.

---

## 0. Final verdict (up front)

**The Foundation (Phase 0 → Phase 4) forms a real, continuous, traceable
business loop — and there is NO BLOCKING defect on any of the 20 focus
areas.**

- The end-to-end chain Project → Analysis → Planning → Task Graph → Runtime →
  Execution/Settlement → Artifact/Evidence → Independent Review → Rework →
  Completion → Delivery is **CONNECTED** with file:line evidence at every hop
  (§3).
- The hard, load-bearing gates are **fail-closed**: assignment P1–P8,
  execution gate, record_review G1/I-2/I-8, record_rework I-4 + kind-closure,
  CP catch-all (`CP_EVAL_ERROR`), delivery writing nothing on any non-`CD_OK`,
  and the `TaskCompletionGate` semantic-gate **error** path (fixed, commit
  `147ebf93`, re-verified in this lane §9).
- Both historical HIGH defects this audit was specifically asked to
  re-adjudicate are **CLOSED on main**: (a) builder self-review F1
  (I-4 kind-closure + DAO backstop, ancestors of HEAD; live tests present);
  (b) `TaskCompletionGate` fail-open (now `_fail_closed`).
- No second Runtime, second Task Graph, or second completion *gate* exists.
  There ARE two orthogonal "done" notions — `Task.status='done'` (Runtime
  settlement, evidence-agnostic) vs the Phase 4 ledger lane
  (CT/CW/CP, evidence-requiring) — a **by-design split**, documented, not a
  bypass [FACT].
- What remains is a set of **named, by-design deferrals** (D-2 advisory
  parallelism cap, D-3 capability→reviewer-Agent join, D-4 supervision
  consumer, channel/published_page external-publish consumer, Phase-4 lane
  transport) plus a handful of **NON-BLOCKING observations** (dead enum
  states, duplicated executable-statuses literal, caller-supplied reviewer
  builder set, legacy `manage_tasks` latent write, C5-row complete-or-absent,
  dead `RV_*` codes, one misleading tool-result string).
- The single most load-bearing **evidence gap** is that the Phase 4
  review/completion/delivery lanes have **no live transport consumer** in
  shipped source — they are test-wired only. This is a DEFERRED capability
  gap, not a broken implementation (§5 G-1).

**Final recommendation (§7): the Foundation is sound and may be recommended
as the stable base for the next phase, with the deferred-item register (§6)
carried forward as-is.** No remediation is a precondition; four optional
follow-up candidates are listed in §10 (none is a defect; all are
hardening/cleanup/policy choices).

---

## 1. Audit scope and method

Read-only cross-phase audit of the Foundation (Phases 0–4). Every claim is
anchored to live source, migrations, git history, or the test suite;
historical phase reports were used only to *locate* items, and each
load-bearing item was re-probed against source (per the three lane docs and
§9 of this report). No live Postgres was probed in any lane — behavioral
live-DB questions are recorded as [UNKNOWN] (§4, §5 E-G).

Findings inherit the lane classification (FACT / OBSERVATION / INFERENCE /
UNKNOWN × BLOCKING / NON-BLOCKING / DEFERRED). This synthesis does not re-open
any lane conclusion; it merges, de-duplicates, and cross-checks them.

---

## 2. Cross-phase dependency map

CONNECTED end-to-end, with the verified per-hop evidence. Each hop cites the
lane that verified it; [C] = connectivity lane t_7858d198, [S] = state-machine
lane t_70258786, [K] = security/deferred lane t_cdd13960.

```
Project (intake: RECEIVED→SOURCES_OK→INITIALIZED)          [C] HOP 1
   │  analysis_runs.project_id → projects.id (FK, CASCADE)
   │  AnalysisRun.revision_sha = repositories.locator.resolved_rev (typed OQ-5)
   ▼
Analysis (launch → record_findings → AN_COMPLETED; promote_finding)  [C] HOP 2
   │  planning_runs(project_id, analysis_revision_sha) UNIQUE;
   │  service gate G1 requires source run AN_COMPLETED + same project
   ▼
Planning (goals/finding traceability; max_parallel_tasks advisory D-2) [C] HOP 3
   │  work_package_tasks.task_id → tasks.id (SET NULL); materializes real
   │  Task rows with Phase-2D provenance (created_reason=ANALYSIS_PLANNING);
   │  DAG edges via the FROZEN task_graph_service (single graph)
   ▼
Task Graph (task_dependencies DAG; ready/blocked owner: task_graph_service)   [C] HOP 4
   │  Task.agent_id = single assignment fact (assignment lane, fail-closed
   │  PL_NO_CANDIDATE_AGENT / PL_RESOURCE_CONFLICT / PL_REVIEWER_NOT_INDEPENDENT;
   │  CONF-5 parallelism advisory-only)
   ▼
Assignment → Runtime (P1–P8 execution gate, first-failure-wins, no legacy
   fallback P7; enqueue_task_runtime → the SINGLE verified Phase-2F Runtime
   command intake; only post-2F spine change: 147ebf93 fail-closed gate)   [C] HOP 5, [S] §A/§B
   ▼
Execution → Settlement (Run settled → Task.status=done/pending + one TaskLog
   receipt; Runtime does NOT auto-mint artifacts/evidence)                [S] §C
   ▼
Artifact / Evidence ledgers (written ONLY by the 4 Phase-4 lanes + tests)   [S] §C
   ▼
Independent Review (record_review G1 no-self-review / I-2 current artifact /
   I-8 bounded payload / R3 one-way seal; DAO backstop EV_REVIEW_NOT_INDEPENDENT;
   F1 self-review kind-closure CLOSED)                                    [S] §D, [K] §B3
   ▼
Rework (REQUEST_CHANGES → record_rework: I-4 new-evidence proof +
   kind-closure + invariant-13 DAO backstop; re-review via plan_review)     [S] §D
   ▼
Completion (CT/CW/CP pure core, fail-closed CP_EVAL_ERROR catch-all;
   ≥1 SEALED artifact + current-valid pass review, tenant match;
   single COMPLETED write site completion_service.py:889-890)             [C] HOP 6, [S] §F
   ▼
Delivery (CD_* decision requires fresh CP_OK + SEALED+APPROVED set;
   writes a delivery record ONLY on CD_OK; channel/published_page
   destination kinds deferred → stuck PENDING, F-2)                        [C] HOP 6, [S] §F-2
```

Provenance is queryable across the whole chain:
`PlanningService.provenance_for_task` returns Task → work_package_tasks →
WorkPackage → goal → findings → planning_run (connectivity lane §4), and the
execution side carries `AgentRun.source_execution_id = task:<id>[:retry:<n>]`.

**Negative findings (verified absences, all [FACT] in the lanes):**
- No second Runtime: one `build_runtime_worker_components`
  (worker_service.py:203) + one production start site (main.py:335); every
  entry point (task/trigger/heartbeat/a2a/group/planning) funnels into the
  same `RuntimeCommandIntake`; `git log PHASE_2F_CLOSED..HEAD` shows only
  `147ebf93` touched the 8 frozen spine files.
- No second Task Graph: edges only via `task_dependencies` +
  `task_graph_service`; planning wires edges through that frozen service.
- No second completion gate: the status axis and the ledger axis are one
  documented split, not two competing completion authorities (§4 item 13,
  16).
- All cross-model links use real `ForeignKeyConstraint`s with documented
  ondelete semantics (f066/f068/f069/f070/f071); the only non-FK link is
  `planning_runs.analysis_revision_sha` (logical, service-gated — §4 item F-2).

---

## 3. Root focus areas 1–20: consolidated answers

Each row: focus area → status → classification → evidence (lane §; key
file:line re-probed in this lane where noted).

| # | Focus area | Status | Class | Evidence |
|---|-----------|--------|-------|----------|
| 1 | Project → Analysis truly connected | **CONNECTED** (FK CASCADE + typed revision binding; launch gate fail-closed on unverified revision) | [FACT] NON-BLOCKING (positive) | [C] HOP 2: models/analysis.py:109-111; git_acquisition_service.py:1040; analysis_service.py:389-409 |
| 2 | Analysis → Planning truly connected | **CONNECTED** (UNIQUE(project_id, analysis_revision_sha); G1 requires AN_COMPLETED, same project) | [FACT] NON-BLOCKING (positive) | [C] HOP 3: models/planning.py:151-166; planning_service.py:485-550 |
| 3 | Planning → Task Graph truly connected | **CONNECTED** (work_package_tasks link; materialized Task rows with Phase-2D provenance; DAG via frozen task_graph_service) | [FACT] NON-BLOCKING (positive) | [C] HOP 4: planning_service.py:710-759; task_graph_service.py:196-417 |
| 4 | Task Graph → Assignment truly connected | **CONNECTED** (single `Task.agent_id` fact; fail-closed PL_* codes; parallelism advisory-only D-2) | [FACT] NON-BLOCKING (positive) | [S] §B: assignment_service.py:731-905; [K] §B1 |
| 5 | Assignment → Runtime reuses Phase-2F Runtime | **YES — single verified Phase-2F instance** (only post-2F spine change: 147ebf93) | [FACT] NON-BLOCKING (positive) | [S] §A: worker_service.py:203; main.py:335; re-probed: `147ebf93` ancestor of HEAD |
| 6 | Execution → Artifact/Evidence truly connected | **CONNECTED at lane level, but NOT auto-minted by the Runtime** — ledgers written only by the 4 Phase-4 lanes + tests; E2E mints evidence manually | [FACT] + [OBS] biggest evidence gap → DEFERRED (G-1) | [S] §C/§G: task_completion.py:68-161; test_phase4_e2e_chain_acceptance.py ~1150-1205 |
| 7 | Evidence → Independent Review truly connected | **CONNECTED, fail-closed** (G1/I-2/I-8/R3 + DAO backstop) | [FACT] NON-BLOCKING (positive) | [S] §D: review_rework_service.py:204-300; [K] §B3 |
| 8 | REQUEST_CHANGES → Rework → Re-review valid | **VALID** (I-4 new-evidence proof + kind-closure + invariant-13 backstop; re-review via plan_review) | [FACT] NON-BLOCKING (positive) | [S] §D: review_rework_service.py:350-356, 364-369, 690-706 |
| 9 | Review → Completion correct gate | **CORRECT, fail-closed** (CP catch-all CP_EVAL_ERROR; sealed+approved+independent+tenant-match; single COMPLETED write site) | [FACT] NON-BLOCKING (positive) | [S] §F: completion_service.py:113-157, 576-614, 888-890; re-probed: only COMPLETED write sites are 889-890 |
| 10 | Completion → Delivery truly valid | **VALID, fail-closed** (CD_* needs fresh CP_OK + SEALED+APPROVED; non-OK writes nothing) — except channel/published_page destination kinds are PENDING-only (F-2, DEFERRED) | [FACT] positive + [FACT] DEFERRED residue | [S] §F/§F-2: delivery_service.py:315-451 |
| 11 | Provenance spans Project→…→Delivery | **YES** — `provenance_for_task` + `source_execution_id` + revision binding | [FACT] NON-BLOCKING (positive) | [C] §4 |
| 12 | Tenant/workspace isolation & security boundary consistent across phases | **CONSISTENT** (centralized SELECT scoping on the ORM event; JWT-embedded tenant; completion/delivery CP_TENANT_MISMATCH fail-closed). Residue: context-dependent scoping (no filter when tenant_ctx null — a standing discipline, not a compile-time guarantee) | [FACT] + [OBS] NON-BLOCKING | [K] §A: dao/base.py:139-174 (re-probed: :139-140 do_orm_execute listener); main.py:364; models/task.py:44-49 (re-probed: __tenant_scoped__ :44/:149) |
| 13 | Second Runtime / second Task Graph / second completion semantics | **No second Runtime, no second Task Graph.** Two "done" notions exist (status axis vs ledger axis) — a **by-design split**, documented, not a bypass | [FACT] NON-BLOCKING | [S] §C-1/§C-2: task_completion.py:137-138; task_graph_service.py:403-416; completion_service.py:22-24 |
| 14 | "Agent reports success but system marks complete" paths | **Two bounded surfaces, no unbounded one**: (a) `completion_gate_exhausted` — after 10 semantic-gate repairs the Run is marked completed even without a gate "pass" (deterministic ledger integrity still enforced; bounded, intentional, tested); (b) settlement marks `done` evidence-agnostically (by design, ledger lane still gates completion) | [FACT] NON-BLOCKING (E-1 is a *bounded fail-open of the semantic gate*) + [FACT] by-design (E-2) | [S] §E-1/§E-2: node_executor.py:1229/1267 (re-probed: exhaustion region :1215-1270); test_agent_runtime_node_executor.py:1747-1758; task_completion.py:137-139 |
| 15 | Fail-open bypass of the final completion gate | **NONE BLOCKING.** The inherited TaskCompletionGate fail-open was already fixed (147ebf93 → all four gate-error paths fail closed). Residual = E-1 exhaustion (bounded) only | [FACT] NON-BLOCKING (positive on the gate itself) | [S] §E; [K] §B3: verification.py:635-709 (re-probed: `_fail_closed` at :635, four paths :666/679/702/709) |
| 16 | No-evidence path into completion / delivery | **NONE at the gate level**: CP requires ≥1 SEALED artifact + current-valid pass review; delivery needs fresh CP_OK + SEALED+APPROVED. Nuance: `COMPLETED` can publish without its citing C5 decision row (F-1, "complete-or-absent"); delivery is guarded, the state is not | [FACT] NON-BLOCKING (positive) + [OBS]/[INF] NON-BLOCKING (F-1) | [S] §F-1: completion_service.py:888; _append_decision :1087-1113; delivery_service.py:376 |
| 17 | Cross-phase state-machine conflicts | **None blocking.** Findings: EXECUTING + PENDING_CONFIRMATION are dead enum values with zero write sites (F1, confirmation UI inert by design); `PROJECT_EXECUTABLE_STATUSES` duplicated as frozen literals in two files (mirror, consistency risk); C-1/C-2 status-vs-evidence split (by design) | [FACT] NON-BLOCKING/DEFERRED | [C] F1: models/project.py:59-63; task_execution_service.py:66-68 (re-probed: duplicated literal at completion_service.py:199 as a "mirror"); analysis_service.py:304 |
| 18 | Implemented critical paths without test coverage | **No critical path found untested.** Phase-4 lanes have unit/E2E coverage (incl. the exhaustion path and F1 rejections). Residue: without a reachable `DATABASE_URL` the DB-backed E2E tiers SKIP, so *live-DB* behavior is unexercised in these audits | [OBS] NON-BLOCKING; live-DB tier = [UNKNOWN] | [S] §K; [C] evidence_gap; test files named in the lanes |
| 19 | Documented capability vs real source | **Three honest gaps, all NON-BLOCKING/DEFERRED**: (a) Planning/Assignment/Plan-execution services have no HTTP/CLI transport — consumed by tests only (exposure gap, F3); (b) Phase-4 review/completion/delivery lanes likewise test-wired only (G-1, the largest); (c) `agent_tools.py:9862-9866` tool-result string promises a supervision "reminder engine" that does not exist (false affordance, doc-cleanliness) | [OBS] DEFERRED (a, b) + [OBS] NON-BLOCKING (c) | [C] F3; [S] §G; [K] §B2 (re-probed: "reminder engine will pick it up" at agent_tools.py:9862) |
| 20 | Unresolved high-risk items from Phase 2C/2D/2E/2F/3/4 | **None BLOCKING.** All named items re-verified in this audit: max_parallel_tasks advisory (D-2, DEFERRED by design); supervision scheduler consumer missing (D-4, DEFERRED by design); reviewer-independence structural limits (F1 CLOSED; invariant-13 opt-in guard DEFERRED/contained; capability→reviewer-Agent join D-3 DEFERRED); TaskCompletionGate fail-open CLOSED (147ebf93) | [FACT] DEFERRED/CLOSED | [K] §B1–B3; [S] §I; re-probed: 76086d65/f185f461/147ebf93 ancestors of HEAD |

**Focus-area roll-up: 0 BLOCKING · 12 areas verified positive · 8 areas carry
NON-BLOCKING/DEFERRED residues (all named, all by-design or closed).**

---

## 4. Known-risk inventory (consolidated)

Severity legend: B=BLOCKING, N=NON-BLOCKING, D=DEFERRED. Every row has
file:line evidence from the lanes (re-probed rows marked † = verified in this
synthesis lane at 676b1caf).

| ID | Risk | Severity | Evidence (file:line) |
|----|------|----------|----------------------|
| R-1 | `completion_gate_exhausted`: after 10 semantic-gate repair attempts the Run is marked completed (→ task done) without a gate "pass"; deterministic tool-ledger integrity still enforced. Bounded, intentional, tested — a *bounded* fail-open of the semantic gate | N (bounded fail-open) | † node_executor.py:1215-1270; test_agent_runtime_node_executor.py:1747-1758 |
| R-2 | Two orthogonal "done" axes: `Task.status='done'` is evidence-agnostic; the DAG unblocks *execution* on status only — evidence gating happens at completion/delivery, not at execution. By-design boundary, must never be conflated | N (by-design) | task_completion.py:137-138; task_graph_service.py:403-416; completion_service.py:22-24 |
| R-3 | `COMPLETED` can publish without its citing C5 decision row (complete-or-absent); delivery lane is guarded, the state is not | N (evidence gap) | completion_service.py:888, _append_decision :1087-1113; delivery_service.py:376 |
| R-4 | Reviewer-independence (invariant-13 / G4) guard is opt-in and application-layer only; builder set is caller-supplied, not WP-authoritative. Contained: all 3 call paths pass the set + read-side CP_REVIEW_NOT_INDEPENDENT net. Theoretical until a transport wires the lanes | D (contained) | † artifact_evidence_dao.py:286 (default None), :326/:340/:351; completion_service.py:481-493 |
| R-5 | Legacy `manage_tasks` can write `task.status='done'` directly; tool OBSOLETE, deleted at seed, unreachable from the durable v2 path — latent only if the legacy `execute_tool` path is ever re-exposed | N (latent) | † agent_tools.py:9822-9895 (update_status branch); tool_seeder.py:408 |
| R-6 | Dead closed codes `RV_NO_REVIEWER` / `RV_SUPERSEDED_SET` / `RV_INCONCLUSIVE` — declared/exported but no return path (vocabulary hygiene) | N (cleanup) | † review_rework_service.py:66/72/74 + __all__ :819-822 |
| R-7 | No live transport for Phase-4 review/completion/delivery + plan-execution lanes — the entire machinery is test-wired only; evidence is never auto-minted after a Run in shipped source | D (largest evidence gap) | † grep of app/api = zero transport handlers for evaluate_*/record_*/deliver/enqueue_plan_tasks |
| R-8 | channel / published_page delivery records stuck PENDING — no `transition_state` consumer exists in production source (external publish deferred) | D | delivery_service.py:44-46, :435-447 |
| R-9 | `EXECUTING` + `PENDING_CONFIRMATION` unreachable Project states (dead enum values, no write sites; confirmation UI inert by design); executable-set over-broad (reduces to ANALYZING in practice) | D (feature deferral) | models/project.py:59-63; analysis_service.py:304 |
| R-10 | No physical FK planning_runs↔analysis_runs on `analysis_revision_sha` — logical (service-gate) integrity only; a future AnalysisRun GC would need matching PlanningRun revalidation | N (referential-integrity limit, OBS) | models/planning.py:166; planning_service.py:541-544 |
| R-11 | Unprovenanced (legacy MANUAL, project_id NULL) tasks skip the P2 project-executability check — by design for the legacy path | N (OBS, by-design) | task_execution_service.py:345-362 |
| R-12 | `PROJECT_EXECUTABLE_STATUSES` duplicated as frozen literals in two files — consistency risk if one side drifts | N (OBS) | † task_execution_service.py:66-68 vs completion_service.py:199 (documented mirror) |
| R-13 | Supervision tool-result string promises a reminder engine that does not exist (false affordance; D-4 still open) | N (doc-cleanliness, OBS) | † agent_tools.py:9862-9866 |
| R-14 | `max_parallel_tasks` advisory-only, no per-project/per-task runtime cap (D-2) | D (by design) | † models/planning.py:304; assignment_service.py:515-550 (:544 "advisory only in V1") |
| R-15 | Supervision/deadline fields storage-only, no consumer in scheduler/trigger/heartbeat (D-4) | D (by design) | models/task.py:72-76 |
| R-16 | No planner path to declare a *distinct* reviewer Agent (D-3); independence enforced at assignment time (REV-1/2) + read-side CP_REVIEW_NOT_INDEPENDENT instead | D (by design) | Phase-3 gate report Flag 1; assignment_service.py review DAG |
| R-17 | Tenant SELECT scoping is context-dependent (no filter when tenant_ctx null) — correct for platform-admin/migration, but any new background path that forgets `tenant_context()` reads/writes unscoped; standing discipline, not a compile-time guarantee | N (OBS) | dao/base.py:150-152; discipline per dao/AGENTS.md §8.4 |
| R-18 | Historical HIGH-1: builder self-review F1 — **CLOSED on main** (I-4 kind-closure + DAO backstop, live tests) | closed (positive) | † review_rework_service.py:364-369, :702; test_artifact_evidence_review.py |
| R-19 | Historical HIGH-2: `TaskCompletionGate` semantic-gate error fail-open — **CLOSED on main** (147ebf93) | closed (positive) | † verification.py:635-709 `_fail_closed` |

**No BLOCKING row exists in the inventory.** R-18/R-19 are the two items the
Root specifically asked to re-adjudicate; both are closed and independently
re-reviewed.

---

## 5. Evidence gaps (UNKNOWN register)

| ID | Gap | Class | Disposition |
|----|-----|-------|-------------|
| E-G-1 | No live Postgres was probed by any of the three lanes; DB-backed E2E tiers (test_planning_service.py / test_assignment_service.py / test_planning_execution_chain_e2e_acceptance.py / Phase-4 E2E) SKIP without a reachable `DATABASE_URL`. Structural connectivity is proven from source+migrations; live-DB behavior is unexercised in this audit cycle | [UNKNOWN, live-DB] | Re-probe only when a phase depends on it |
| E-G-2 | Whether *production* Task rows actually use `ANALYSIS_PLANNING` provenance, and whether live supervision rows carry `remind_schedule` values | [UNKNOWN, live-DB] | Inherited from Phase 2C-3 audit; out of scope for a structural card |
| E-G-3 | Whether any out-of-repo consumer marks a Project COMPLETED outside this codebase's services (no such site found in backend/app) | [UNKNOWN] | Carried from Phase 2C-3 audit |
| E-G-4 | Whether a real Run ever hit the `completion_gate_exhausted` path in production (static audit cannot observe runtime history) | [UNKNOWN] | Inherited; the path is tested, bounded, intentional |
| E-G-5 | R-7 (no live transport for Phase-4 lanes): capability is delivered and verified at unit/E2E tier, but not reachable from a live entry point — the largest "documented capability vs real source" gap in the Foundation | [FACT] DEFERRED | Named in §6 (D-G) |

---

## 6. Deferred-item register (carry forward, do NOT assume fixed)

| ID | Item | Why deferred | Next step if/when needed |
|----|------|--------------|---------------------------|
| D-2 | Per-project/per-task runtime parallelism cap (max_parallel_tasks advisory-only today) | By design in V1 ("advisory only", never refused) | Wire a runtime cap in task_execution/plan_execution |
| D-3 | Capability → distinct-reviewer-Agent join in the planner | V1 has no planner path; independence contained at assignment time + read-side CP_REVIEW_NOT_INDEPENDENT | Planner declares reviewer Agent; make builder set WP-authoritative |
| D-4 | Supervision/deadline scheduler consumer (storage-only today) | No consumer drives `remind_schedule`/`supervision_channel` | Reuse `scheduler.py` (not a new engine); probe production first |
| D-G | Phase-4 lane transport: auto-mint execution-linked evidence after a run, drive a disjoint reviewer, call completion, deliver | The V1 production consumer does not exist yet | Highest-value follow-up (§10 item 2) |
| D-EX | channel/published_page external publish (delivery records PENDING-only) | External publish platforms out of V1 scope | `DeliveryRecordDAO.transition_state` consumer when a platform lands |
| D-CONF | PENDING_CONFIRMATION / confirmation UI (inert by design) | Feature deferral; execution-allowed set over-broad meanwhile | Land a status writer + UI or prune the enum (R-9) |
| D-C5 | C5 decision row complete-or-absent on COMPLETED | Intentional "complete-or-absent" rule | Gate COMPLETED on the C5 row, or document the absence explicitly (§10 item 5) |
| D-RV | Dead `RV_*` closed codes (R-6) | Vocabulary hygiene | Delete or wire the owning paths |

---

## 7. Final recommendation for the Foundation

**RECOMMEND THE FOUNDATION AS SOUND AND READY TO SERVE AS THE STABLE BASE FOR
THE NEXT PHASE, WITH THE REGISTER IN §4–§6 CARRIED FORWARD AS-IS.**

Justification (each [FACT]-re-probed in this lane unless noted):

1. **Continuity.** The 20 focus areas resolve to 0 BLOCKING; the business loop
   Project → Analysis → Planning → Task Graph → Runtime → Execution →
   Artifact/Evidence → Review → Rework → Completion → Delivery is connected
   with per-hop file:line evidence, and provenance is queryable across it
   (items 1–11, §3).
2. **Gate integrity.** Every hard gate on the way to completion/delivery is
   fail-closed, including the two historically-HIGH defects (F1 self-review,
   TaskCompletionGate fail-open), both CLOSED on main and independently
   re-reviewed (R-18/R-19). The only fail-open residue is the *bounded,
   intentional, tested* `completion_gate_exhausted` (R-1) — a policy choice,
   not a bug.
3. **No duplication.** Single Runtime, single Task Graph, single assignment
   fact, single COMPLETED write site; the "two done axes" is a documented
   by-design split, not a second completion authority (items 5, 13).
4. **Tenant isolation is consistent and centralized** across phases (item 12),
   with one standing-discipline observation (R-17), not a defect.
5. **Honesty of gaps.** Everything not shipped is *named* and *classified*
   (§5–§6): no undocumented capability is claimed, and no deferred item is
   silently treated as shipped.

Conditions attached to the recommendation:
- (a) The deferred-item register (§6) is the official carry-forward list for
  the next phase; a future phase that *assumes* D-2/D-3/D-4/D-G behavior
  exists must first wire the consumer, not assume it.
- (b) E-G-1/E-G-2 live-DB UNKNOWNs are to be re-probed only when a phase
  depends on them.
- (c) R-13 (misleading supervision string) and R-5/R-6 (latent legacy write,
  dead codes) are recommended cleanups (§10) — none blocks the Foundation.

---

## 8. Sources and lane provenance

| Lane | Card | Baseline | Deliverable | Lane commit |
|------|------|----------|-------------|-------------|
| Connectivity (Project→Planning→Task Graph) | t_7858d198 | 676b1caf | docs/architecture/PROJECT_PLANNING_CONNECTIVITY_AUDIT_T7858D198.md | fc398196 (on wt/t_7858d198; descendant of 676b1caf — verified in this lane) |
| State machine (Assignment→Runtime, Execution→Evidence, Review) | t_70258786 | 676b1caf | docs/audit/PHASE_0-4_EXECUTION_TO_REVIEW_STATE_MACHINE_AUDIT.md | uncommitted in that lane — **committed by this synthesis lane** (per that lane's handoff: "commit belongs to the synthesis lane") |
| Security / tenant / deferred items | t_cdd13960 | 676b1caf | docs/architecture/SECURITY_DEFERRED_AUDIT_TCDD13960.md | 0d83900a (on wt/t_cdd13960, pushed to origin; descendant of 676b1caf — verified in this lane) |
| This synthesis | t_00dddf61 | 676b1caf | docs/audit/FOUNDATION_FINAL_AUDIT_REPORT_T00DDDF61.md | this lane's commit |

All four lane documents live on this synthesis branch so the independent
reviewer (t_50bba645) has the complete evidence set in one place.

---

## 9. Re-verification performed in this synthesis lane

Per the Root's method requirement ("must start from real source… may not
assume a capability exists from historical reports"), this lane re-probed
every load-bearing claim against live source at `676b1caf` in
`wt/t_00dddf61` (no code modified; git status clean at start):

| Claim | Re-probe | Result |
|-------|----------|--------|
| `147ebf93` (fail-closed gate) is on main | `git merge-base --is-ancestor 147ebf93 HEAD` | YES — ancestor of 676b1caf |
| F1 self-review fixes (76086d65, f185f461) on main | same ancestry check (both visible in log) | YES — f185f461 is parent of HEAD, 76086d65 earlier |
| `_fail_closed` covers all four gate-error paths | grep verification.py | CONFIRMED at :635, :666, :679, :702, :709 |
| `completion_gate_exhausted` exhaustion region | node_executor.py:1215-1270 | CONFIRMED (code at :1229, reason at :1267, synthesized `outcome="pass"`) |
| `manage_tasks` OBSOLETE + seed deletion | tool_seeder.py:408 | CONFIRMED (`OBSOLETE_TOOLS = ["bing_search", "manage_tasks"]`) |
| Dead `RV_*` codes declared/exported, no producer | review_rework_service.py | CONFIRMED at :66/:72/:74 + `__all__` :819-822 |
| `max_parallel_tasks` advisory-only | assignment_service.py:515-550 | CONFIRMED (advisory ConflictEntry :541, "advisory only in V1" :544) |
| No Phase-4 / planning / assignment / plan-execution transport in `app/api/` | grep app/api for evaluate_*/record_*/deliver/enqueue_plan_tasks AND planning_service/assignment_service/plan_execution_service | CONFIRMED — zero hits (test-only wiring, R-7 / F3) |
| Single COMPLETED write site | grep `project.status` / `transition(project` across services+dao | CONFIRMED — only intake dao statuses, ANALYZING (analysis_service.py:253), COMPLETED (completion_service.py:889-890) |
| Duplicated `PROJECT_EXECUTABLE_STATUSES` literal | both files | CONFIRMED (task_execution_service.py:66-68; completion_service.py:199, documented "mirror") |
| Centralized tenant scoping on ORM event | dao/base.py | CONFIRMED (`do_orm_execute` listener :139-140, `_inject_tenant_scope`) |
| Task transitional tenant opt-in | models/task.py | CONFIRMED (`__tenant_scoped__ = True` :44 and :149) |
| Opt-in `reviewer_builder_agents` guard | artifact_evidence_dao.py | CONFIRMED (default None :286/:340, enforced when not-None :351) |
| Misleading supervision string | agent_tools.py:9862 | CONFIRMED ("reminder engine will pick it up") |

**Read-only in every lane: no product code, frozen file, test, migration, or
state was modified; no live Postgres was probed (E-G-1).**

---

## 10. Recommended follow-ups (candidates — not created; Root decides)

Per the Root's rule, no hot-fixes in this read-only audit; the following are
proposed candidates, each with file:line, impact, and repro evidence. This
synthesis lane does **not** create cards for them: none is a BLOCKING defect,
the Root explicitly says "do not assume these must be fixed", and card creation
is the orchestrator's decision on the Root card after the reviewer verdict.

1. **[POLICY DECISION] E-1 `completion_gate_exhausted`** — decide whether
   exhaustion should fail the Run instead of completing it (or stay a bounded
   fail-open as shipped+tested today). file: node_executor.py:1215-1270;
   impact: the one "agent success accepted without a semantic gate pass"
   surface; repro: `tests/test_agent_runtime_node_executor.py:1747-1758`
   asserts the shipped behavior.
2. **[DEFERRED, highest value] D-G Phase-4 transport lane** — add the
   production consumer that (a) auto-mints execution-linked artifact/evidence
   after `run_completed`, (b) drives a disjoint reviewer, (c) calls
   completion, (d) delivers; include the channel/published_page
   `transition_state` consumer (D-EX). Until then R-7/R-8 hold and delivery
   is test-only.
3. **[SECURITY HARDENING] R-4 WP-authoritative builder set** — derive the
   reviewer-independence builder set from the owning WorkPackage (single
   authority) instead of a caller parameter; mirror it in the DAO backstop.
   Contained today (all 3 call paths + read-side CP_REVIEW_NOT_INDEPENDENT),
   so optional.
4. **[CLEANUP, non-blocking batch]** R-13 one-line doc fix
   (agent_tools.py:9862-9866); R-5 delete-or-hard-guard the legacy
   `manage_tasks` direct-status write; R-6 delete or wire the dead `RV_*`
   codes; R-12 unify the duplicated executable-statuses literal; R-9 decide
   confirmation-UI fate (writer+UI, or prune the dead enum values).
5. **[CONSISTENCY] R-3 C5-row rule** — either gate `COMPLETED` publication on
   the C5 row actually being written, or document explicitly that a COMPLETED
   project may lack its citing decision row.

---

*Produced by the aco-orchestrator synthesis lane on t_00dddf61 at main
`676b1caf`. Read-only audit: no business code modified. Pending independent
review by aco-reviewer (t_50bba645); Root t_645d0566 completes only on
reviewer APPROVE.*
