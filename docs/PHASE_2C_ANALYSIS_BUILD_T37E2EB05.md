# Phase 2C — Minimal Analysis Persistence (build lane) — t_37e2eb05

Owning Agent Note for the Phase 2C **build** deliverable: the minimal
Analysis persistence layer so the OS can "reliably store one Project
Analysis + its sources". Design of record: `docs/PHASE_2C_PROJECT_ANALYSIS.md`
§9.2 (the review-APPROVED minimal model). This note records what was BUILT,
the settled decisions it honors, the transport surface, and the verification
evidence. Code / this note / the commit stay aligned (root AGENTS.md §3).

## 1. The three tenant-scoped tables (one DDL-only migration, `f068_analysis_persistence`)

Created off the single head `f067_intake_rejection_fields` (alembic/AGENTS.md §0:
`uv run alembic heads` prints EXACTLY ONE line afterward — `f068_analysis_persistence`).

| Table | Role | Key invariants |
|---|---|---|
| `analysis_runs` | Versioning + Git-revision binding | `UNIQUE(project_id, revision_sha)` — **append-only**: a re-analysis at a NEW commit sha is a NEW row, history is never clobbered. `revision_sha` is an indexed commit-hash column (the typed OQ-5 carrier). |
| `analysis_findings` | TRANSIENT findings owned by a run | `analysis_run_id` FK `ON DELETE CASCADE` (findings die with their run). Every row carries a closed `tag` + `evidence` JSON (path:line anchors + source-card provenance); README-as-truth is forbidden. |
| `project_knowledge` | DURABLE / CONFIRMED / revision-independent | `source_analysis_run_id` FK `SET NULL` (provenance is COPIED, not owned; a later analysis at a different commit never invalidates it). No knowledge graph modeled — a single flat row. |

All three carry a **non-nullable, indexed `tenant_id`**, so they are
tenant-owned by schema and are picked up automatically by the `do_orm_execute`
tenant filter in `app/dao/base.py` (`_is_tenant_scoped_model`). They are reached
**only** via `TenantScopedBaseDAO` + `verify_tenant_scope` (the M9 fourth gate;
`app/dao/analysis_dao.py`). Existing `projects` / `repositories` rows are
untouched and the `project_status_enum` is unchanged beyond the §2 decision.

## 2. Settled decisions honored (from the orchestrator's card)

- **OQ-5 — revision carrier = minimal typed model.** The revision that binds an
  analysis is `analysis_runs.revision_sha`, decoupled from the free-form
  `repositories.locator` JSON. The sha is read at analysis time from
  `repositories.locator.resolved_rev` (written back by
  `GitAcquisitionService`, `app/services/git_acquisition_service.py`), validated
  against the closed sha shape (7..64 hex chars) in
  `app/services/analysis_service.py::_revision_from_locator` — a source without
  a well-formed `resolved_rev` fails closed with `AN_SOURCE_INVALID`, it is
  never guessed.
- **OQ-6 — Phase 2C OWNS the `ANALYZING` transition.** The launch path
  (`analysis_service.launch`) sets `project.status -> ANALYZING` via
  `project_dao.transition`. `PENDING_CONFIRMATION` **stays inert**: no code
  path in this lane sets it (the confirmation UI does not exist yet);
  promotion flows through `promote_finding` while the project is ANALYZING.
- **Closed result-code enum, NOT a new state machine.** `analysis_runs.status`
  is the closed set `AN_OPEN / AN_COMPLETED / AN_FAILED` (mirrors the ACQ_*
  closed-code pattern; root AGENTS.md §2 — a new step-by-step SM would need an
  independent owner + need; it does not). The service validates against the
  closed `ANALYSIS_RESULT_CODES` set before any write; the DAO persists the
  decided value.
- **Knowledge boundary (hard).** `analysis_findings` = transient (true of one
  revision, one time, one agent; CASCADE with its run). `project_knowledge` =
  durable. A finding is PROMOTED to a knowledge row ONLY on confirmation; the
  promotion copies provenance and is revision-independent.
- **Stage 11 (hard) — static-only.** The analysis execution path reads DB rows
  and the bounded locator JSON written by acquisition. It NEVER executes the
  target project's code: no subprocess, no interpreter, no build, no
  `pip`/`npm` install, no service start. No dynamic analysis (deferred to a
  future Execution/Analysis sandbox).

## 3. Service + transport surface (minimal, one current consumer each)

Owning service: `app/services/analysis_service.py` (holds ALL policy: the
closed AN_* codes, the revision binding, the append-only invariant, the
stage-11 boundary). Handlers in `app/api/projects.py` are pure transport
adapters that map the outcome to a status (the acquire-route pattern).

| Method + path | Outcome |
|---|---|
| `POST /api/projects/{project_id}/repositories/{repo_id}/analyze/{agent_id}` | **Launch.** 201 `launched` (a new `AN_OPEN` run + the ANALYZING transition); 200 `existing` (the revision already has a run — the UNIQUE invariant's re-read, nothing clobbered); 409 `failed` (a closed AN_* code). |
| `POST /api/projects/{project_id}/analysis/{analysis_run_id}/findings` | **Record findings.** 201 (findings written, the run closed `AN_COMPLETED`); 409 on a closed AN_* code (a terminal run refuses findings = `AN_RUN_NOT_OPEN`; an evidence-less / out-of-set / unbounded finding = `AN_INVALID_FINDING`). |
| `POST /api/projects/{project_id}/analysis/{analysis_run_id}/promote` | **Promote to knowledge.** 201 (a `CONFIRMED` knowledge row with provenance copied); 409 on a closed AN_* code. |
| `GET /api/projects/{project_id}/analysis` | **Read.** 200 always. `current` = the newest run (+ its findings); `history` = all prior runs. A never-analyzed project reads back `current=null` + empty history (data, not an error). |
| `GET /api/projects/{project_id}/knowledge` | 200. All durable knowledge rows for a project, newest first. |

Tenant isolation: a foreign-tenant target agent is a **404** (tenant
invisibility, never a 403 disclosure); a cross-tenant tenant-scope violation
at the entry gate is a **403** (`AnalysisSecurity`, mirroring
`AcquisitionSecurity`).

## 4. Files changed (this build)

- `backend/alembic/versions/v1_11_5_f068_analysis_persistence.py` (NEW,
  DDL-only, no inline SELECT→UPDATE loops). `downgrade()` is functional on
  BOTH fresh-DB paths (create_all-provisioned and pure-alembic): the redundant
  `ix_analysis_runs_project_id` was dropped from the migration so its index
  set stays in lockstep with the model, and every `op.drop_index` in
  `downgrade()` is existence-guarded (`_existing_indexes`) so it is a clean
  no-op where 001's `create_all` pre-created the tables from the model
  metadata. Re-verified in t_bca54821 — see §5 "downgrade/upgrade re-verified".
- `backend/alembic/env.py` (import the new models into `Base.metadata`).
- `backend/app/models/analysis.py` (NEW: `AnalysisRun`, `AnalysisFinding`,
  `ProjectKnowledge` + the closed enum value sets).
- `backend/app/dao/analysis_dao.py` (NEW: three `TenantScopedBaseDAO`
  subclasses).
- `backend/app/services/analysis_service.py` (NEW: the owning service + closed
  AN_* codes + bounded-input limits).
- `backend/app/schemas/analysis.py` (NEW: transport request/response schemas).
- `backend/app/api/projects.py` (the five handlers above).
- `backend/tests/test_project_analysis_e2e_acceptance.py` (NEW, real-project
  E2E; criterion #14).
- `docs/PHASE_2C_ANALYSIS_BUILD_T37E2EB05.md` (this note).

## 5. Verification evidence

Run from `backend/`. Scratch Postgres: `clawith_t37e2eb05_e2e` (fresh, schema
from the full 001→f068 chain).

- **Migration chain intact / single head.**
  `uv run alembic upgrade head` on a fresh DB ran the full chain and landed on
  `f068_analysis_persistence`; `uv run alembic heads` prints exactly ONE line.
  The three tables + the `uq_analysis_runs_project_revision` unique index + the
  tenant indexes exist in the fresh DB; `alembic current` = `f068_analysis_persistence (head)`.
- **Real-project E2E** (criterion #14): `tests/test_project_analysis_e2e_acceptance.py`
  drives Project → Source (real local `git init` repo) → Revision (acquire →
  `resolved_rev`) → Analysis (launch bound to that sha) → Findings (tag +
  path:line) → Stored Analysis (read back current + history) → Knowledge
  (promote + read back), and asserts the two hard rules: **append-only
  versioning** (a NEW commit sha appends a new run row; the prior run moves to
  history, never clobbered) and **stage-11 static-only** (zero new
  chat-session / task / schedule rows for the agent after the full chain).
  Skips cleanly without a reachable DB (`_db_available`, copied from the
  git-acq E2E).
- **DB-free service/DAO logic** is covered by the existing 2B/2A suites; the
  analysis service itself is exercised end-to-end by the E2E above.
- **Regression (criterion #15)** — 2B full chain: `uv run --extra dev ruff
  check .`, `uv run --extra dev pyright app`, the 2B Project/Intake/
  Materialization/GitAcquisition suites, and `uv run alembic upgrade head` on
  a fresh DB.
- **Downgrade/upgrade re-verified (rework t_bca54821).** After the review
  t_75dd99db flagged `f068 downgrade()` as hard-failing on fresh DBs
  (`UndefinedObjectError: index "ix_analysis_runs_project_id" does not
  exist`), the fix — dropping the redundant `ix_analysis_runs_project_id`
  from both the model and the migration and existence-guarding every index
  drop in `downgrade()` — was re-verified on two independently freshly
  created scratch DBs (`clawith_tbca54821_createall`,
  `clawith_tbca54821_pure`), each running
  `uv run alembic upgrade head` → `uv run alembic downgrade
  f067_intake_rejection_fields` → `uv run alembic upgrade head`:

  ```text
  $ uv run alembic heads
  f068_analysis_persistence (head)          # exactly ONE revision

  Path A (create_all-provisioned fresh DB):
    [upgrade head]                        EXIT=0
    [downgrade f067_intake_rejection_fields] EXIT=0
    [upgrade head (2nd)]                  EXIT=0

  Path B (pure-alembic fresh DB):
    [upgrade head]                        EXIT=0
    [downgrade f067_intake_rejection_fields] EXIT=0
    [upgrade head (2nd)]                  EXIT=0

  Post-upgrade pg_indexes on both paths (identical):
    analysis_runs:     analysis_runs_pkey, ix_analysis_runs_revision_sha,
                       ix_analysis_runs_tenant_id, uq_analysis_runs_project_revision
    analysis_findings: analysis_findings_pkey,
                       ix_analysis_findings_analysis_run_id,
                       ix_analysis_findings_tenant_id
    project_knowledge: project_knowledge_pkey,
                       ix_project_knowledge_project_id,
                       ix_project_knowledge_subject,
                       ix_project_knowledge_tenant_id
    uq_analysis_runs_project_revision: UNIQUE (project_id, revision_sha)
    => index sets AGREE between paths: True
  ```

  Full machine-captured output of this cycle is in the block above (each step's
  exit status + the post-upgrade `pg_indexes` set). Repro: drop + recreate the
  two scratch DBs and run the three-step alembic cycle with `DATABASE_URL`
  pointed at each — Path A starts from the empty DB (001's `create_all`
  provisions it), Path B provisions to `f067` first, then `DROP TABLE`/
  `DROP TYPE` the three analysis objects so f068's own DDL must recreate them.
  The criterion #14 analysis E2E was re-run on this rework tree against the
  migrated fresh scratch DB: `tests/test_project_analysis_e2e_acceptance.py`
  → **6 passed** (PYTEST_EXIT=0, clean solo run), confirming the fix
  regressed nothing on the execution path.

## 6. Baseline tooling debt (recorded honestly, not fake-passed)

The tree at the analysis start is NOT clean under the two whole-tree static
gates (pre-existing legacy debt, unrelated to this change):

- `uv run --extra dev ruff check .` → **~3107 baseline errors** across legacy
  `app/`, `tests/`, and older `alembic/versions/` files (e.g. `update_schema.py`
  F401s). The **four NEW files** in this change (migration, models, dao,
  schemas) pass `ruff check` with **0 errors**; the `projects.py` additions add
  `B008` (the repo-wide FastAPI `Depends()`-in-default pattern the whole file
  already carries — 14 baseline → 24 with my 4 new handlers, same pattern).
- `uv run --extra dev pyright app` → **714 baseline errors** tree-wide
  (legacy `wecom_*`, `workspace_reconciliation`, …). Every file in this change
  (new files + `env.py` + `projects.py` + `analysis_service.py`) is at
  **0 pyright errors** after this build (the one error I introduced in
  `analysis_service.py`, the `loc.get("resolved_rev")` narrowing, was fixed
  with an `isinstance` guard).

The regression gate is therefore characterized as "adds zero NEW pyright
errors and zero NEW non-B008 ruff errors to the changed files; leaves the
pre-existing tree-wide ruff debt untouched" rather than a green whole-tree
pass.

## 7. Known limitation / open item

- The **confirmation UI** (`PENDING_CONFIRMATION`) does not exist yet, so the
  `promote_finding` path is exercised here as a direct API call (the durable
  knowledge write is real; the human-confirmation step that would normally gate
  it is not). This is by design (OQ-6 keeps PENDING_CONFIRMATION inert).
- Knowledge rows are read back in newest-first order with a `limit=100`
  default in the DAO (`list_for_project`); a project with >100 knowledge rows
  would need the limit raised or paginated — no current consumer needs more.
