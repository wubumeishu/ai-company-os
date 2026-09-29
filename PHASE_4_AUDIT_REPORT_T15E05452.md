# Phase 4 — Read-Only Source Audit: Artifact / Evidence / Review / Completion / Delivery

- Task: t_15e05452 (child of Root t_4047050f, "Phase 4 — Artifact, Independent Review & Completion")
- Baseline: main == origin/main == `8f030792`, tag `PHASE_3_CLOSED`. Clean tree verified (`git status --porcelain` empty, `git tag --points-at HEAD` = PHASE_3_CLOSED).
- Method: Inspect → Trace → Verify → Report. Every claim cites live `file:line` in the worktree. Nothing inferred from README.
- Labels: [FACT] (read in source), [OBS] (observed pattern), [INF] (inference), [UNKNOWN] (not verifiable from source).
- Read-only. No implementation proposed, no frozen code modified.

---

## Scope map (where each audited concept actually lives)

| Concept | Location | First-class? |
|---|---|---|
| Task Result | `Task.status` + `TaskLog` (task.py, task_completion.py) | yes |
| Run Result | `AgentRun` + terminal `AgentRunEvent` (agent_run.py, checkpoint_side_effects.py) | yes |
| Agent Tool Execution | `AgentToolExecution` (agent_tool_execution.py) | yes |
| Workspace | `WorkspaceFileRevision` + on-disk files (workspace.py) | yes (revision ledger) |
| Git commit / file changes | `git_acquisition_service.py` (acquisition only) | partial — no agent-side commit |
| Test results | none (only tool stdout in result_summary) | no |
| Artifact | refs in `result_metadata` + `ToolResultStore` + `PublishedPage` + `AgentRunEvent.artifact_refs` | no entity |
| Evidence | `evidence_refs` in `result_metadata` + `_completion_evidence` (conversation) | no entity |
| TaskCompletionGate | `verification.py:622` | yes (fail-open) |
| Verification | `verification.py` | yes |
| Review / approval | `assignment_service.ReviewBinding` + `ApprovalRequest` (audit.py) | no review-verdict entity |
| Project/Planning status | project.py enum + planning.py closed sets | yes |
| Delivery | `delivery.py` + `ChannelDelivery` + `AgentRun.delivery_status` | yes (answer transport) |
| Tenant isolation | `dao/base.py` + ContextVar | yes |

---

## Q1. What Artifact capabilities already exist?

[FACT] Artifact *references* exist; no `Artifact` entity.

- `AgentToolExecution.result_metadata` (JSONB) is the carrier: it holds `artifact_refs` / `evidence_refs` lists, read by the verifier at `verification.py:898-899` (`_refs(metadata, "artifact_refs")`, `_refs(metadata, "evidence_refs")`). Model: `agent_tool_execution.py:106` (`result_metadata` column, CHECK-free JSONB).
- `ToolResultStore` (`tool_result_store.py:194`) persists private text/binary tool results at a deterministic, tenant-scoped key `runtime/tool-results/{tenant_id}/{run_id}/{execution_id}.json|.bin` (`tool_result_store.py:208-216`), reachable via opaque ref `tool-result://{execution_id}` (`:207-208`) / `tool-result-binary://{execution_id}` (`:218-222`).
- `PublishedPage` (`published_page.py:13`) — a published HTML page with `short_id`, `source_path`, `view_count` (`:20-27`), produced by the `publish_page` builtin tool.
- `WorkspaceFileRevision` (`workspace.py:28`) — a per-file revision ledger: `path`, `operation`, `actor_type` (user|agent|system), `before_content`/`after_content`, `content_hash`, `group_key`, `session_id` (`:56-65`). Actual files stay on disk; the DB stores diff/rollback history.
- Git source artifact: `git_acquisition_service` publishes a single bounded tar `source.tar` at `{agent_id}/.git-acq/{repo_id}/source.tar` and records `acq_artifact` + `resolved_rev` + `requested_ref` + `provider` + `acquired_at` in `repositories.locator` JSON (`git_acquisition_service.py:1039-1044`).
- `AgentRunEvent.artifact_refs` column (`agent_run_event.py:91`), used by the runtime event stream.

[OBS] An "artifact" today = (a) a tool-result blob behind an opaque ref, (b) a published page, (c) a workspace-file revision, or (d) an acquisition tar. There is no single identity/type/provenance record tying them to a Project/Task/Execution/revision.

## Q2. What Evidence capabilities already exist?

[FACT] Evidence = `evidence_refs` in tool-result metadata + the conversation trajectory fed to the completion gate.

- `evidence_refs` alongside `artifact_refs` in `AgentToolExecution.result_metadata` (`verification.py:899`); the reference-existence checkers handle schemes `published://`, `imagekit://`, `http(s)://`, and `tool-result://` (`verification.py:600-617`).
- `_completion_evidence(state)` (`verification.py:92-137`) builds the "available_evidence" payload for the gate: `initial_input` + a 24000-char capped **message trajectory** + `authoritative_task_amendments` + optional `thread_summary`. This is **conversation-based** evidence, not file/commit/test evidence.
- Git revision evidence: `resolved_rev` recorded at acquisition (`git_acquisition_service.py:1040`) and referenced by `analysis.py:86` ("repositories.locator.resolved_rev captured at analysis time").
- Private tool-result envelope (`ToolResultStore`, `tool_result_store.py:86` `ToolResultEnvelope`).

[OBS] There is no `Evidence` record that answers "which test run / which commit / which file revision proves this." Evidence is today either a URL-ish ref, a private tool blob, or the raw chat trajectory.

## Q3. Execution Result ↔ real files / Git commit / test results — what is the relationship?

[FACT]
- Execution result = an `AgentToolExecution` row: `status` ∈ {started,succeeded,failed,unknown} (`agent_tool_execution.py:32-33`), `result_summary` (Text, `:104`), `result_ref` (String(500), `:105`), `result_metadata` (JSONB, `:106`).
- **Files:** an execution that writes files does so through workspace tools; the file change is recorded separately in `WorkspaceFileRevision` (`workspace.py:28`) with `content_hash` + actor + session. The execution row does not itself store the file; the link is only by convention (same agent/session), not a stored FK.
- **Git commit:** `git_acquisition_service` only *acquires* an upstream source tar and records `resolved_rev` (`git_acquisition_service.py:1040`). There is **no agent-side git-commit tool** — `builtin_tool_definitions.py` has no `git_commit`/`git_push`/`git_log` entry (grep of tool `"name":` list returned none). A commit is an upstream source fact, not a produced artifact.
- **Test results:** no persisted test-result model exists. A `test_result|TestResult|pytest_result|run_tests` search over `backend/app` returned 0 dedicated matches (only Python `test_*` identifiers in skill scripts). Test output, if run, survives only as `AgentToolExecution.result_summary` / `result_metadata` of an `execute_code`/`agentbay_command_exec`/`execute_code_e2b` call.

[INF] Execution → file/commit/test is **loosely coupled by string refs and session**, not by explicit provenance edges. The gate can verify that a `tool-result://` / published / http ref *resolves*, but it does not verify a diff/commit/test as a first-class fact.

## Q4. Do reusable Artifact / Evidence models already exist?

[OBS] No dedicated `Artifact` or `Evidence` entity (the model listing has neither table). What is reusable as a **foundation**, not a complete model:
- `AgentToolExecution.result_metadata` — JSONB carrier for `artifact_refs`/`evidence_refs`.
- `ToolResultStore` — private, tenant/run-scoped blob storage + resolver (`tool_result_store.py:194,354`).
- `WorkspaceFileRevision` — a content-hashed revision ledger (immutable per-row, append-only).
- `PublishedPage` — a durable published-document artifact.
- `AgentRunEvent.artifact_refs` + the `evidence_added`/`verification_updated` event slots (`agent_run_event.py:33,91`).

[OBS] These are storage/ledger primitives. None carries identity, type, provenance (source execution/task/commit), revision, or a re-verification hook. So: **a foundation exists to build on; a directly-reusable Artifact/Evidence record does not.**

## Q5. Where is the current Review capability?

[FACT] Review is **not a domain object**; it is spread across three unrelated mechanisms:
- **Who reviews (disjointness):** `assignment_service.ReviewBinding` (`assignment_service.py:202`) + REV-1/REV-2 checks — a `requires_independent_review` work package's reviewer task must be assigned to a candidate **disjoint from every builder pick** (`assignment_service.py:340-410`), enforced fail-closed at assignment time.
- **When review happens (ordering):** a `review`/`gate` task scope slot (`TASK_SCOPE_SLOT_KINDS = ("build","review","gate","other")`, `planning.py`) blocks later work packages through the DAG (`WorkPackage.requires_independent_review`, `planning.py` WorkPackage fields; `MILESTONE_KINDS = ("phase","gate","delivery")`).
- **Human approval (not code review):** `ApprovalRequest` (`audit.py:31`), status pending/approved/rejected (`:40-41`), resolved by creator/platform-admin in `autonomy_service.py` (L1/L2/L3, `:51-163`).

[OBS] There is no Review record that captures a **verdict against an artifact/execution/commit/test set**. "Review" today = picking a disjoint Agent (who) + a blocking DAG edge (when) + the single completion gate (a coarse LLM check of the final answer). No independent re-check of real artifacts.

## Q6. Are Independent Review and Completion Gate currently separated?

[OBS] **Separated in concern, coupled in execution, and neither is a real independent review.**
- The Completion Gate is `TaskCompletionGate` (`verification.py:622`): a semantic LLM comparison of the candidate final answer against the *conversation* evidence (`_completion_evidence`, `verification.py:674`). It is a **completion criterion**, not a reviewer of artifacts.
- Independent Review today is only the assignment-time disjoint Agent + DAG edge (§Q5).
- So the gate and "review" are different mechanisms, but the gate does **not** re-verify artifacts/commits/tests, and the review does **not** re-run the gate. Neither independently proves the work meets acceptance criteria against real evidence.

## Q7. Does REQUEST_CHANGES have real semantics?

[FACT] No. `REQUEST_CHANGES` appears only in a **docstring** in `project_intake_service.py:105` ("capability later … needs no rework"). There is no `REQUEST_CHANGES` status value, no request-changes workflow, no reviewer→builder channel.

[OBS] The closest real thing is the runtime's `VerificationResult(outcome="repair")` (`verification.py:717-726`, "The task is not complete yet. Continue working before finishing") — an **intra-Run** keep-going signal, and `TaskRuntimeCompletionHandler` re-pending the Task (`task_completion.py:141-154`). That is a repair loop *inside one Run*, not a reviewer-driven REQUEST_CHANGES that re-enters Execute→Re-review.

## Q8. Does Rework have a current mechanism?

[UNKNOWN] No dedicated rework entity/mechanism exists. [OBS] What exists is:
- The in-Run repair loop (verification `outcome="repair"` → retry until `completion_gate_exhausted`, `node_executor.py:1229,1267`).
- Task-level **retries of a failed Run** with a new attempt id (`task_execution_service.py:391` `_new_attempt_id`, retry idempotency keys `:60-66`).

None of these links **re-execution → new evidence → re-review** back to the *original* Review. A rework that must "produce genuinely new verification evidence and be re-reviewed against the original review" is absent.

## Q9. How to re-execute after Review?

[OBS] There is no Review→re-execute path. The generic re-run is `TaskExecutionService.execute` (`task_execution_service.py:145`) enqueueing a new Run for a Task. After a reviewer rejects (a capability that does not exist), nothing is defined. The nearest is a failed-Run **retry** (`_new_attempt_id`, `task_execution_service.py:391`) — a new Run, but not Review-driven and not tied to a re-review of fresh evidence.

## Q10. What currently decides Project / Task completion?

[FACT]
- **Task:** `Task.status` ∈ {pending,doing,done} (`task.py:58-59`). `TaskRuntimeCompletionHandler.handle` (`task_completion.py:68-162`) sets `task.status="done"` + `completed_at` when the Run's terminal checkpoint is `completed` with a non-empty `final_answer` (`:137-139`), and back to `pending` on `failed`/`cancelled` (`:145-154`). **Task "done" = the Run reported completed.** It is not gated on a review verdict, approved evidence, or acceptance criteria.
- **Project:** `Project.status` is a 10-value lifecycle enum (`project.py:53-65`: RECEIVED…COMPLETED/ARCHIVED/REJECTED). **No service writes `Project.status="COMPLETED"`** — grep across `backend/app/api` and `backend/app/services` returned zero `project.status = "COMPLETED"` sites. Execution is *gated* by `PROJECT_EXECUTABLE_STATUSES = {ANALYZING, PENDING_CONFIRMATION, EXECUTING}` (`task_execution_service.py:66-68`), but that governs *when work may run*, not *completion*.
- **Run:** terminal via checkpoint lifecycle; the completion gate governs whether the Run reaches `completed`.

[INF] **Completion is currently "Agent reported success / Run completed," not "independently verified + approved + evidenced."** This is exactly the failure mode the Root card flags: "Agent 报告完成 绝不等于 完成."

## Q11. Does Delivery currently exist?

[FACT] Yes, but only as **answer/message transport**, not as "delivery of completed+approved+evidenced artifacts."
- `deliver_runtime_message` (`delivery.py:795`) with `DeliveryReceipt` (`delivery.py:98`); emits `delivery_succeeded`/`delivery_failed` `AgentRunEvent`s (`delivery.py:747`) and sets `AgentRun.delivery_status` ∈ {not_required,pending,delivered,failed} (`agent_run.py:46-47,152`) + `delivery_target` JSON (`:153`).
- `ChannelDelivery` model, status pending/claimed/delivered/failed (`channel_delivery.py:42-43`).
- `PublishedPage` is a published-page delivery.

[OBS] There is no Delivery object with provenance, delivered-artifact list, destination, state, and timestamp built on top of **Completed + Approved + Evidence**. Current delivery = "send the Run's answer to a channel/group target."

## Q12. Which capabilities are missing?

1. **No first-class `Artifact` / `Evidence` entity** (identity, type, provenance: source execution/task/project/tenant, revision/commit, file/test/structured-DB-record kind, immutable/mutable boundary, retention, re-verification hook). Existing pieces are refs + private blobs + a file-revision ledger + a published-page table.
2. **No independent Review record** that captures a verdict over real artifacts/commits/tests. Review is only assignment-time disjointness + a DAG edge + one completion gate.
3. **No `REQUEST_CHANGES` semantics** and **no Re-work mechanism** linking Re-execution → new evidence → Re-review to the original Review (history-preserving, re-reviewable, staleness-aware).
4. **Completion is not fail-closed on real change:** `TaskCompletionGate._fail_open` returns `outcome="pass"` on its own errors — model unavailable (`verification.py:668`), gate call failed (`:691`), or unparseable output (`:698`). An incomplete/unverifiable gate currently *passes*.
5. **No Task/Work-Package/Project completion decided by a completion gate + review + evidence + dependency + tenant boundary.** Task "done" = Run completed; Project "COMPLETED" is never written.
6. **No Delivery object built on Completed + Approved + Evidence.**
7. **No persisted/first-class test-result evidence** (test output only as tool stdout).
8. **No agent-side git commit** (commit is acquisition-only, upstream).

## Q13. Which capabilities should NOT be newly added?

[INF] Constraints/avoid-list (grounded in the Root card's hard boundaries and observed reuse):
- **Do not create a second Runtime.** Reuse the Phase-2F frozen Runtime spine (`checkpoint_side_effects`, `command_worker`, `node_executor`, `delivery`).
- **Do not duplicate the existing foundations** — build on `AgentToolExecution.result_metadata`, `ToolResultStore`, `WorkspaceFileRevision`, `PublishedPage`, `AgentRunEvent.artifact_refs`, `repositories.locator`/`resolved_rev`, rather than new parallel storage.
- **Do not add a Review that trusts the Builder's self-report** — the gate must not take the Run's `final_answer` as proof; and the human `ApprovalRequest`/`AuditLog` already covers the approval channel (don't add a new one).
- **Do not add a state machine merely to represent UI progress / workflow steps** — only add one for an independently-identified object with authoritative transitions and a real consumer (Root hard boundary + AGENTS.md rule 6).
- **Do not auto-equate Agent success with Completion, and do not auto-retry around an explicit execution semantics.**
- **Do not re-do frozen Phase 2A-3** (intake, materialization, task-graph, assignment spine).
- **Do not invent an Artifact/Evidence without provenance** — every record must answer "who/which task/which revision/when/can it be re-verified."

---

## The one defect that must become its own fix task (not hidden in Phase 4)

[FACT] **`TaskCompletionGate` is fail-open on its own failure.** `verification.py:634-642` `_fail_open()` returns `VerificationResult(outcome="pass", details={"code":"completion_gate_error", ...})`. It is reached on:
- `invalid_completion_gate_identity` (`:655`),
- `completion_gate_model_unavailable` (`:668` — model row missing / disabled / wrong tenant),
- `completion_gate_call_failed` (`:691` — the LLM call raised),
- `invalid_completion_gate_output` (`:698` — unparseable verdict).

In every one of these the Run's completion gate reports **pass**, so a Task can be marked `done` (`task_completion.py:137-139`) when the gate itself errored. This is an **inherited fail-open risk** that directly undermines Phase 4's "Completion must be fail-closed" goal and the "Completion Gate keeps correct constraint on actual change" gate criterion. Per the Root card ("如果现有 TaskCompletionGate 存在 inherited fail-open 风险，必须先判断是否影响 Phase 4 完成语义，并将真实问题单独形成修复任务，而不是隐藏"), this must be recorded as a **separate fix task with independent review**, not silently folded into the Phase 4 build.

[OBS] The *deterministic* verifier is the opposite (fail-closed on unverifiable refs, `verification.py:920-932, 1025-1032`); only the *semantic* completion gate is fail-open. That asymmetry is the risk.

---

## Tenant-isolation & workspace/security (supporting audit)

[FACT]
- All tenant-owned ORM SELECTs are scope-injected by `_inject_tenant_scope` (`dao/base.py:140`): any model with a **non-null** `tenant_id` column (or `__tenant_scoped__=True`) gets `WHERE tenant_id = <active>` appended; the active id comes from a `ContextVar` set by `TenantContextMiddleware` / `tenant_context()` (`dao/base.py:120-193`). A null context fails open for legacy nullable-tenant tables only.
- `git_acquisition_service._entry_gates` re-asserts tenant scope + agent-tenant==project-tenant (`git_acquisition_service.py:387-413`), 403-class.
- `WorkspaceFileRevision` scopes revisions by agent/group + path, content-hashed, actor-stamped (`workspace.py:28-68`).
- `ToolResultStore` keys are tenant/run-scoped (`tool_result_store.py:208-224`).

[OBS] Isolation is real and centralized at the DAO layer; the artifact/evidence store must inherit the same tenant scoping (it does, via `result_metadata` + `ToolResultStore` keys).

---

## Evidence classification summary

- FACT: Q1–Q13 location/absence findings (all cited file:line above); the fail-open gate defect.
- OBS: "artifact/evidence are refs+blobs, not entities"; review=disjointness+DAG+gate; completion=Run-reported.
- INF: rework/re-review/re-execute are absent; completion is not fail-closed on change; avoid-list for Q13.
- UNKNOWN: (a) whether any external consumer marks a Project `COMPLETED` outside this repo's services — none found in `backend/app`, so **Project completion is UNKNOWN/unimplemented here**; (b) any test-harness that persists test results — **not found**, treated as absent.
