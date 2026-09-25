# Phase 2D — Analysis → Task Mapping Boundary & Safety Gate (V1)

**Baseline**: `main @ e6237916` (product baseline `fc233fc1`)
**Ground truth**: `docs/PHASE_2D_CODEBASE_AUDIT.md` (audit `4ddba22b`, code at `e6237916`),
Phase 2C semantics from `docs/PHASE_2C_PROJECT_ANALYSIS.md` §9.2 + merged f068
(`PHASE_2C_ANALYSIS_BUILD_T37E2EB05.md`).
**Companion card**: Task Graph + Provenance **schema** (migration DDL, graph
validation service, blocked/ready computation) is owned by `t_46f8c7cf`
(`PHASE_2D_TASK_GRAPH_PROVENANCE.md`, pending). This document owns the
**mapping boundary and safety gate**; it references the schema draft, it does
not redefine it.

---

## 1. The boundary: Analysis (understanding) ≠ Task (execution intent)

Phase 2C is deliberately analysis-only: the analysis path is STATIC-ONLY
(audit §2.4 — no subprocess, no build, no execution of the target project).
Its output is *understanding*: `analysis_runs` + `analysis_findings`
(transient, die with the run) + `project_knowledge` (durable, CONFIRMED,
revision-independent).

A Task is an **execution intent**: a bounded unit of work owned by exactly
one Agent (`Task.agent_id` is non-nullable, audit §3), which when triggered
becomes a Durable Runtime `AgentRun` (`source_type="task"`) and can mutate
the world (workspace, git, tools).

The two layers must not be merged. The single owned boundary between them is
a new, explicitly-invoked conversion step:

```
AnalysisRun (AN_COMPLETED, findings locked)
        │  ← boundary owner: TaskDecompositionService (V1, new, read-side
        │     of analysis + write-side of tasks, invoked explicitly)
        ▼
classified findings ──(executable class)──▶ Task rows, status=pending,
                                            provenance filled, NOT enqueued
classified findings ──(planning class)──▶ stay findings (→ knowledge / future
                                            Planner), NO Task row
```

Rules of the boundary:

1. **Conversion is explicit.** Nothing auto-converts on `record_findings`.
   Phase 2C's "analysis-only" decision (OQ-6 lane) is preserved: analysis
   stays a read/understanding lane; a human invokes conversion as a separate
   act (same shape as the existing `promote_finding` confirmation lane).
2. **Conversion never executes.** A converted Task lands in `status=pending`
   and is **not** passed to `enqueue_task_runtime`. Execution remains the
   existing, separately-authorized manual trigger path
   (`app/services/task_executor.py` trigger → `enqueue_task_runtime`,
   audit §A.3). "Create" and "run" are decoupled — that decoupling IS the
   V1 safety gate.
3. **One owner.** Only the conversion service may create
   analysis-provenance Task rows. The existing manual task API
   (`POST /agents/{agent_id}/tasks`) is untouched: it keeps creating
   `created_reason=MANUAL` tasks with null provenance. No bypass paths.
4. **Fail closed.** Any gate failure returns a closed `TD_*` result code
   (409-class, mirroring the `AN_*` pattern in `analysis_service.py`);
   nothing is written. Unknown finding shapes are NOT converted (default →
   planning class, never → Task).

---

## 2. V1 safety gate: minimum safe landing (no PENDING_CONFIRMATION UI)

**Facts (verified, audit §2.4 / §9.6):** `PENDING_CONFIRMATION` is declared in
`project_status_enum` but INERT — no code path sets it; the confirmation UI
does not exist. Tasks created today are auto-enqueued for `todo` type
(`create_task` API → `enqueue_task_runtime`), i.e. a task created by any
client executes with no human step.

**Decision (V1):** the safety gate is the **create/executed decoupling**
plus a **closed classification rule set** — no new state machine, no new UI,
no new confirmation table:

| Gate | V1 behavior | Evidence/owner |
|---|---|---|
| G1 Invocation | Explicit `POST /projects/{project_id}/analysis/{run_id}/tasks` against an **AN_COMPLETED** run only; tenant scope via `verify_tenant_scope`; target agent's tenant must equal the project tenant (mirrors `AnalysisService._entry_gates`) | new endpoint, transport-only; service owns logic |
| G2 Classification | Deterministic, closed rule table (§3) maps each finding to `executable` or `planning`. No LLM in the V1 mapping (Planner-integrated classification is a future lane) | `TaskDecompositionService.classify` (pure function, unit-testable) |
| G3 Creation | Executable-class findings → Task rows `status=pending`, `type=todo`, provenance fields filled (§5, §6), **dedup-guarded** (§4) | transactional, all-or-nothing |
| G4 No execution | V1 conversion NEVER calls `enqueue_task_runtime`; a converted task runs only through the existing manual trigger (human decision), which is the same gate already in place for all tasks | existing `task_executor.py` trigger path, unchanged |
| G5 Bounded output | ≤ `MAX_TASKS_PER_INVOCATION` (V1 = 100, aligned to `MAX_FINDINGS_PER_RECORD=100`); title truncated to the 500-char column width | bounded before DB write |

**Why this is the minimum safe V1 (ADR):**

- **Problem.** Analysis findings are execution candidates; without a gate,
  auto-creating them on `record_findings` would immediately enqueue agent
  runs (existing auto-enqueue behavior) — unbounded agent work on the
  project, no human in the loop, and a Phase-2C contract violation
  (analysis lane must not trigger execution lane).
- **Options.**
  - O1 Auto-create + auto-enqueue on `record_findings`. **Rejected**:
    executes without any human decision; re-couples the two lanes Phase 2C
    deliberately separated; blast radius unbounded (100 findings → 100
    background AgentRuns).
  - O2 Task Proposal entity + `PENDING_CONFIRMATION` UI. **Rejected for V1**:
    the UI does not exist (audit: INERT state); a separate Proposal table
    would duplicate the Task lifecycle ("not-yet-confirmed execution") as a
    second authority — violates root AGENTS.md §2 (a new state machine needs
    an independent owner and a current consumer; neither exists yet).
  - O3 (chosen) Explicit invocation + Task rows in `pending` (never
    auto-enqueued) + execution via the existing trigger. **Consequences:**
    "pending" now carries a visible meaning for analysis-driven tasks
    ("proposed, awaiting human trigger"); the confirmation UI of the future
    can be built ON TOP of this state without a schema change — the Task row
    in `pending` + filled provenance *is* the persisted proposal.
- **Rejected-alternatives note for the record:** O1 and O2 documented above.
- **Future (out of V1 scope, do not implement now):** auto-enqueue of a
  closed allow-list subset (e.g. `TECH_DEBT`+`FACT`+severity≥`WARN`) behind
  an explicit configuration flag defaulting to OFF; Planner-LLM
  classification; rework/reopen paths for failed converted tasks.

---

## 3. Classification spec: which Findings may become Tasks

Inputs are the closed enums already persisted by f068
(`analysis.py`): `category ∈ {SECURITY, RISK, TECH_DEBT, OPEN_QUESTION,
FACT}`, `severity ∈ {INFO, WARN, HIGH, CRITICAL}`,
`tag ∈ {FACT, OBSERVATION, INFERENCE, UNKNOWN}`.

Evaluate **exclusions first, inclusions second, default = planning**. A
finding not matched by an inclusion rule is planning-only (fail-closed:
unmapped knowledge never becomes work on its own).

### 3.1 Planning-only class (NEVER auto-converted; human / future Planner)

| # | Rule (any match) | Reason |
|---|---|---|
| P1 | `category = OPEN_QUESTION` (any severity/tag) | The Task cannot be defined before a human answers the question (audit §G) |
| P2 | `category = RISK` (any severity/tag) | Requires an accept / mitigate / transfer decision before any work item exists (audit §G) |
| P3 | `tag ∈ {INFERENCE, UNKNOWN}` (any category/severity) | Low-confidence claim; must be verified before it drives execution (README-as-truth is forbidden; an INFERENCE is not yet a finding-of-fact) |
| P4 | `severity = INFO` | Informational; no action implied |
| P5 | default (everything else unmatched below) | fail-closed |

Planning-class findings remain where Phase 2C already puts them: readable
via `GET /projects/{id}/analysis`, promotable to `project_knowledge` via the
existing `promote_finding` lane when confirmed. A future Planner lane (out
of V1 scope) may consume P1–P5.

### 3.2 Executable class (V1-convertible; the closed allow-list)

| # | Rule (ALL must hold) | Rationale |
|---|---|---|
| E1 | `category = TECH_DEBT` ∧ `tag = FACT` ∧ `severity ∈ {WARN, HIGH, CRITICAL}` | Concrete, high-confidence, bounded remediation work (audit §F design candidate) |
| E2 | `category = SECURITY` ∧ `tag = FACT` ∧ `severity ∈ {HIGH, CRITICAL}` | Concrete high-severity security work; the fix boundary is well-defined by the finding + evidence anchors (audit §F design candidate) |

E1/E2 are the **only** V1 conversion paths. They are a closed set, chosen to
match exactly the two "concrete + traceable + action-implying" classes in the
existing closed enums — no free-text judgment in V1.

### 3.3 Finding → Task field mapping (spec)

For each executable-class finding of run `R` (revision `R.revision_sha`):

| Task field | Value | Source |
|---|---|---|
| `title` | `[{category}] {summary}` truncated to 500 chars | finding |
| `description` | `finding.summary` + evidence anchors (path:line list) + a provenance footer: `analysis run {R.id} @ revision {R.revision_sha}` | finding + run |
| `type` | `todo` | fixed |
| `status` | `pending` | fixed (G4) |
| `agent_id` | request `agent_id` (mandatory in V1; M9 tenant check, G1). If the request omits it → closed code `TD_AGENT_REQUIRED`, no write | run's agent is NOT silently inherited: the executing agent is a human decision, not an analysis artifact |
| `priority` | `CRITICAL→urgent`, `HIGH→high`, `WARN→medium`, `INFO→low` (INFO never reaches a Task — P4) | closed severity map |
| `created_by` | invoking `current_user.id` | API caller |
| `created_reason` | `ANALYSIS_FINDING` | closed code (§6) |
| provenance (§6) | `project_id`, `analysis_run_id=R.id`, `finding_id`, `revision_sha=R.revision_sha` | run + finding |
| `due_date`, supervision fields | NULL / absent | not part of a converted task |

Notes:
- **Project Knowledge is NOT injected in V1** (no verified consumer exists;
  AGENTS.md "Public choices" rule). Known follow-up: embedding CONFIRMED
  knowledge rows as durable constraints/acceptance criteria in the task
  description (audit §H design role).
- Task **dependencies** (a Task depending on another converted Task) are
  NOT part of V1 conversion — V1 produces a **flat set of independent
  Tasks**. Dependency declaration belongs to the Task Graph lane
  (`t_46f8c7cf`); the graph service can later be fed explicit dependency
  input by a human/Planner. Keeping dependency authoring out of the V1
  mapping keeps each lane independently verifiable.

---

## 4. Idempotency & duplicate guards

- **Dedup invariant (required input to the `t_46f8c7cf` schema draft):**
  a finding may be converted **once per analysis run** → unique constraint
  on the provenance (`analysis_run_id, finding_id` — where
  `finding_id` is non-null; manual tasks have no finding).
- Second invocation on the same run: already-converted findings are
  **skipped** (reported per-finding in the closed outcome — `converted` /
  `skipped_duplicate` / `not_executable` / `rejected`), not re-created.
- The UNIQUE constraint is the last-resort guard (racing invocations);
  the service pre-checks for fast, closed-code feedback.

## 5. Result contract (closed codes)

Mirrors the `AN_*` closed result-code pattern of `analysis_service.py`:

```
TD_OK                 201 (or 200 with per-finding statuses) — invocation complete
TD_RUN_NOT_COMPLETED  409 — run.status ∉ {AN_COMPLETED}
TD_INVALID_INPUT      409 — bad agent_id / oversized invocation (> G5 bound)
TD_AGENT_REQUIRED     409 — no agent_id and none accepted as implicit (V1: always required)
TD_AGENT_TENANT_MISMATCH 403 — M9 fourth gate
```

Per-finding outcomes are data in the response body (closed string set:
`converted | skipped_duplicate | planning_only`), never free text.

## 6. Provenance value spec (values written by this lane)

The **columns** come from the `t_46f8c7cf` schema draft; this document owns
the **values and semantics** for the analysis-driven lane:

| Field | V1 rule |
|---|---|
| `project_id` | run's project — mandatory for analysis-driven tasks; NULL = manual task (existing rows unchanged, backfilled as NULL) |
| `analysis_run_id` | the AN_COMPLETED run — mandatory for this lane |
| `finding_id` | the exact finding — mandatory for this lane (V1 has no run-level Task creation; a Task always names a Finding) |
| `revision_sha` | copied from the run at conversion time — answers "was this Task based on stale analysis?" (audit §I) |
| `created_reason` | closed code set V1 = `{MANUAL, ANALYSIS_FINDING}`. `MANUAL` is the default for the existing API path; no speculative `PLANNER` value until that lane exists (closed sets, root AGENTS.md §2) |

Chain this makes answerable after V1:
`Task → finding_id → AnalysisFinding → analysis_run_id → revision_sha →
project_id` (the Run → Artifact → Review tail is a later lane, audit §J).

## 7. Verification expectations (for the builder lane, `t_b8545ece`)

Deterministic, unit-testable without a live Runtime:

1. `classify`: full truth table over the 5×4×4 closed grid (200 combos) —
   exactly the E1/E2 rows are `executable`; assert every exclusion P1–P5 and
   the default fall-through.
2. Field mapping: each E1/E2 row produces the §3.3 values, including the
   severity→priority closed map and the 500-char title truncation boundary
   (499/500/501 chars).
3. Gates: G1 (AN_OPEN/AN_FAILED run rejected), G5 (invocation >100 findings
   rejected), dedup (second invocation → `skipped_duplicate`, row count
   unchanged), G1 tenant mismatch → 403-class.
4. **No-enqueue assertion (the safety gate itself):** after a successful
   conversion, assert NO `AgentRun`/`AgentRunCommand` row exists for the
   created tasks and `task.status = pending`; then the existing trigger path
   does enqueue (integration, existing test pattern).
5. Regression: the manual task path (`POST /agents/{id}/tasks`) is
   byte-identical in behavior — `created_reason=MANUAL`, provenance NULL,
   auto-enqueue unchanged (existing tests pass untouched).

## 8. Known limitations / explicit TODOs (V1)

- No dependency authoring during conversion (flat task set) — graph lane.
- No LLM/Planner in classification — planning class (P1–P5) is reachable by
  humans only today.
- No knowledge injection into task descriptions — follow-up with a
  verified consumer.
- No project-status transition on conversion: the project remains
  `ANALYZING` (Phase 2C lifecycle preserved; `EXECUTING` is an inert state
  and re-designing lifecycle is out of scope, root §九).
- `created_by` is the invoking user; agent-driven conversion (future) keeps
  the same user-attribution model — no new actor type invented.

## 9. Lane split (ownership summary)

| Concern | Owner |
|---|---|
| Classification rules, field mapping, gates G1–G5, result codes | this document (`t_3867a0f9`) |
| DDL (provenance columns, unique dedup constraint, created_reason enum), Task Graph, blocked/ready | `t_46f8c7cf` schema draft |
| Service + endpoint + unit tests | `t_b8545ece` (builder), per §7 |
| Independent review | reviewer lane, per root §十二 |
