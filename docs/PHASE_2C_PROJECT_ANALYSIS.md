# Phase 2C — Project Analysis (Synthesis Artifact)

Task: `t_3d5641e0` (Phase 2C gate 3 — "Synthesis & Analysis Artifact Definition").
Method: **synthesis** of four read-only source cards. No project source, schema,
migration, or config was modified in producing this document.

Every load-bearing claim carries a **FACT / OBSERVATION / INFERENCE / UNKNOWN**
tag and a `path:line` evidence anchor. Claims synthesized from the four source
cards inherit their source card + its commit; claims re-verified on this
worktree are marked "(re-verified @ a9e83a8a)".

## 0. Provenance & Git Revision Binding (versioning header)

This artifact is **revision-bound**: it is a snapshot of the project state as
analyzed at a specific source revision, NOT "always the current code".

- **Analyzed source revision (HEAD):** `a9e83a8aec27ce36cb7e8c36c48306a65e571342`
  (== `main` == `origin/main`, in sync at every audit time).
- **Artifact location:** `docs/PHASE_2C_PROJECT_ANALYSIS.md` on branch
  `wt/t_3d5641e0`. The exact commit hash that records this artifact is kept in
  the card's completion handoff (metadata), not the doc body — a hash in its
  own body would be self-referential and change on every edit. The binding
  anchor above (`a9e83a8a`) is the commit this analysis was produced **against**.
- **Analysis-run identity:** card `t_3d5641e0`, run #3 of the Phase 2C synthesis
  wave, produced 2026-09-24 (JST) by `aco-architect` (this card).
- **Source inputs (each read via `git show <branch>:docs/<file>`, shared object
  store; worktrees cannot see sibling files directly):**

| Role | Card | Branch @ commit | Source doc |
|------|------|-----------------|------------|
| Recon baseline (gate 1) | `t_4e4599d8` | `wt/t_4e4599d8` @ `babdaf25` | `docs/PHASE_2C_RECON_BASELINE.md` |
| Tech stack & architecture | `t_d9a3eafd` | `wt/t_d9a3eafd` @ `5aaa9c1f` | `docs/PHASE_2C_TECHSTACK_ARCHITECTURE.md` |
| Runtime, testing & workflow | `t_93876cee` | `wt/t_93876cee` @ `1211b7c1` | `docs/PHASE_2C_RUNTIME_TESTING_WORKFLOW.md` |
| Risk, secrets & open questions | `t_88a15667` | `wt/t_88a15667` @ `20e71f86` | `docs/PHASE_2C_RISKS_OPEN_QUESTIONS.md` |

**Migration single head at revision `a9e83a8a`:** `f067_intake_rejection_fields`
(re-verified; 72 Alembic version files). Any Phase 2C schema work chains off it.

---

## 1. Executive Summary

Clawith (the "AI Company OS") is a **single FastAPI monolith** that multiplexes
bootstrap / api / worker / connector roles from one image via the `PROCESS_ROLE`
env var — not a microservice set. Persistence is **PostgreSQL via
SQLAlchemy-async + Alembic** (single head `f067`), coordination/presence is
**Redis**, and durable agent execution rides **LangGraph + Postgres
checkpoints**. The product pipeline
`Project → Intake → Git Acquisition → Materialization` is the **only**
stage-gated state-machine subsystem today; the `ANALYZING` /
`PENDING_CONFIRMATION` enum values are declared but **own no state machine**
yet.

**Phase 2C's goal** (make the system *understand* an already-ingested project)
therefore has a clean insertion point: it should **reuse** the established
Git-Acquisition pattern — *closed result-code set + single service owner +
agent-scoped storage-facade artifact + audit row + `repositories.locator.
resolved_rev` revision binding* — rather than invent a new state machine for
workflow steps. What does **not** exist and must be designed (minimal): an
Analysis / Finding persistence object, its analysis-run + revision binding, and
an explicit transient-Finding vs long-lived-Project-Knowledge boundary
(§8, §9 below define the minimal model).

**Headline postures for the design card** (carried from all four source cards):
- **Reuse-first.** No `Artifact` table exists (artifacts are first-class string
  fields on tool executions); `AuditLog` is deliberately *best-effort and
  non-authoritative*. Phase 2C persistence must pick an **authoritative owner**
  for findings and add **one minimal typed model**, not a full artifact platform.
- **Settle OQ-4 (acquire serialization) + OQ-5 (revision-binding carrier)
  first.** Both determine whether `resolved_rev` stays trustworthy under
  concurrency; building revision-binding on top of an unserialized locator is
  unsafe (risk card §3.2, §5).
- **Read-only discipline is absolute.** Analysis never executes project code;
  the new persistence must go through a **tenant-scoped DAO +
  `verify_tenant_scope`** (the M9 fourth gate) and must **not** pollute Project
  rows or change the status enum without an OQ-6 decision.

---

## 2. Project Identity

- **Product:** Clawith — "an enterprise agent harness for durable single-agent
  and multi-agent execution" (root `AGENTS.md` §1). Backend project name
  `clawith-backend`; Python floor `>=3.11`, runtime image `python:3.12-slim`
  (re-verified @ a9e83a8a; `backend/pyproject.toml:1-5`).
- **Domain model:** `Project` (`backend/app/models/project.py:34`, table
  `projects`) with a 10-value `project_status_enum`
  (`project.py:48-62`): RECEIVED, SOURCES_OK, INITIALIZED, ANALYZING,
  PENDING_CONFIRMATION, EXECUTING, BLOCKED, COMPLETED, ARCHIVED, REJECTED.
  `Repository` (`project.py:98`, table `repositories`) is the minimal §F.1
  source/asset registry with a 7-value `repository_source_type_enum`
  (`project.py:116-129`) and a free-form `locator: JSON` column
  (`project.py:135`).
- **Authority boundary:** `Project` is the authoritative lifecycle owner.
  `app/models/project.py:11` explicitly **excludes** "analysis JSON" from the
  Project model — analysis is a *separate* object, not a field on Project.

## 3. Source Revision

- **Revision binding today:** the resolved git revision is persisted on the
  Repository row as `repositories.locator.resolved_rev` — the acquisition service
  writes `acq_artifact` / `resolved_rev` / `requested_ref` / `provider` /
  `acquired_at` / `acq_result` / `acq_detail` back into that JSON
  (`git_acquisition_service.py:1034-1042`; `loc["resolved_rev"] = resolved_rev`
  at `:1040`, re-verified @ a9e83a8a). **This is the existing revision-binding
  hook Phase 2C can reuse** — but it lives in a free-form column with **no DDL
  constraint and no index** (debt D1; risk card §4).
- **Open (OQ-5):** whether to (a) keep binding on the `locator` JSON field or
  (b) add a minimal typed revision object. Revision-keyed queries ("which
  findings belong to rev X?") become JSON scans with no schema object — this is
  the concrete cost of option (a). §9 below recommends the minimal typed model.

## 4. Repository Structure

- **Layout (root `AGENTS.md`):** `backend/` (FastAPI app + Agent Runtime),
  `frontend/` (React 19 + Vite), `docs/` (durable sources of truth), `specs/`,
  `scripts/`, `deploy/`, `helm/`, `docker-compose*.yml`.
- **Backend layering (re-verified @ a9e83a8a):**
  `app/api/` (transport adapters only) → `app/services/` (business
  orchestration) → `app/dao/` (sole persistence owners, `TenantScopedBaseDAO`)
  → PostgreSQL + Redis. 47 router files, 45 registered in `main.py:381-479`,
  almost all under `settings.API_PREFIX = "/api"` (`config.py:92`).
- **Frontend:** `frontend/src/services/api.ts` unified fetch-based client
  (`fetch(...)` at `:20`); no axios anywhere (arch-guard C4, fatal).
- **Docs-of-record for the pipeline:** `docs/GIT_ACQ_DESIGN_V1.md`,
  `docs/GIT_ACQ_SECURITY_AUDIT_T31F91B3A.md`,
  `docs/GIT_ACQ_E2E_ACCEPTANCE_T4874C3E7.md`,
  `docs/PHASE_2B4_GIT_ACQUISITION_CONVERGENCE.md` (all archived in main).

## 5. Technology Stack

- **Backend (FACT):** Python 3.12 runtime image; FastAPI `>=0.115` + Uvicorn;
  SQLAlchemy-async + asyncpg + Alembic; Redis `redis[hiredis]>=5` (only cache
  / pub-sub / presence store — no app-level object cache); LangGraph
  `>=1.2,<1.3` + `langgraph-checkpoint-postgres >=3.1,<3.2` + psycopg for
  durable agent execution.
- **LLM abstraction (FACT, provider-agnostic):** `LLMClient` ABC
  (`llm/client.py:518`) with four concrete clients
  (OpenAICompatible `:570`, OpenAIResponses `:1049`, Gemini `:1473`,
  Anthropic `:1967`), a canonical provider registry (`:2342`) and
  `create_llm_client` factory (`:2572`). Provider name is a **data column**
  (`models/llm.py:54`). **No request-budget / resource-manager abstraction
  exists** — the abstraction is message/transport-layer only.
- **Frontend (FACT):** React 19 + TypeScript 5 + Vite 6; `@tanstack/
  react-query` (server cache) + `zustand` (client state) +
  `react-router-dom` v7; fetch HTTP layer.
- **OBSERVATION — frontend doc drift:** `frontend/AGENTS.md` claims "React 18,
  Tailwind CSS, shadcn/ui"; the manifest is React 19 with **no** Tailwind or
  shadcn dependency and a fetch (not axios) HTTP layer. Documentation defect,
  not code defect — flag, don't fix inside Phase 2C.
- **Sandbox (FACT):** `bubblewrap` (bwrap) is the mandatory `execute_code`
  boundary; container deployments **fail closed** unless
  `SANDBOX_ALLOW_UNSAFE_FALLBACK_WHEN_BWRAP_MISSING` is set
  (`main.py:37-69`). Two-stage process-group reap recipe at
  `sandbox/local/subprocess_backend.py:221-245` (already copied by git
  acquisition).

## 6. Architecture

- **Process topology (FACT):** one image, N roles via `PROCESS_ROLE`
  (`main.py:22-34`, `config.py:129`). Deployment topologies:
  dev = single `all`; CD = `backend-api` (`api`) + `backend-worker`
  (`bootstrap,worker,connector`); CI = single `all`.
- **Concurrency (INFERENCE, re-verified):** multi-worker correctness is
  coordinated by **Postgres `SKIP LOCKED`** (`scheduler.py:47`) + per-run
  claimant (`worker_service.py:140`, `INSTANCE_ID`-keyed), **not** leader
  election. A new analysis-execution loop should reuse this pattern, not
  invent a scheduler.
- **Service boundaries:** API handlers are transport-only; DAOs are the sole
  persistence owners; Agent Runtime (`agent_runtime/`, ~40 modules) is the
  durable execution core (`RuntimeCommandWorker` `command_worker.py:316`);
  provider layer is abstracted; storage is a facade
  (`storage_runtime/facade.py get_storage_backend()`).
- **Phase 2C insertion point (INFERENCE):** the established
  *closed-code + storage-facade + audit-row + revision-binding* pattern is the
  model to follow; no new state machine for workflow steps (root `AGENTS.md` §2
  "New state machines require an independent owner").

## 7. Runtime

- **Boot authority (FACT):** `backend/entrypoint.sh` is the single boot owner —
  privilege drop (root→`clawith` via gosu), `INSTANCE_ID` derivation,
  bootstrap-role-only `alembic upgrade head` (`:52`) +
  `setup_langgraph_checkpoints` (`:74`), then `uvicorn ... --port 8000`
  (`:15`). Default uvicorn workers = 1 across all shipped topologies.
- **Ports:** 8000 (backend), 3000/3008 (frontend), 5432 (Postgres), 6379
  (Redis), 9000 (MinIO/S3 in CD), 18789 (OpenClaw gateway), 1080 (ss-local
  SOCKS5). Per-agent OpenClaw port = `18789 + hash(agent.id) % 10000`
  (`agent_manager.py:283`) — **unseeded string hash; two agents can collide on
  one host** (risk R4).
- **Verification blind spot (OBSERVATION):** CD post-deploy health pings the
  **API** through the frontend proxy; a `backend-worker` boot/migration failure
  is **not** caught — so a revision-bound analysis table that fails to migrate
  silently leaves the analysis API unavailable while the fleet reports healthy.
  (risk R1 — add a worker-side schema-readiness probe.)

## 8. Existing Features & Reusable Capabilities

Build ON these (recon baseline §7; re-verified):
- **Pipeline services:** `ProjectIntakeService` (state machine + closed 6-code
  rejection set, `project_intake_service.py:92-99`), `GitAcquisitionService`
  (`git_acquisition_service.py:211`, ACQ_* closed set + agent-scoped
  `source.tar` + `resolved_rev` binding), `ProjectMaterializationService`
  (INITIALIZED-only gate, reads the one tar, no re-clone).
- **Storage facade:** `get_storage_backend()`; agent-scoped keys
  `{agent_id}/...` + `normalize_storage_key` are the existing tenant-isolation
  primitive.
- **Knowledge:** `experience_retrieval.search_experience` /
  `ExperienceEntry` / `Skill` — this is the **long-lived Project Knowledge**
  side (no vector RAG; structured entries + skills + optional directory search).
- **Audit:** `AuditLog` — best-effort, bounded, secret-free; **not
  authoritative** (reused by acquisition; Phase 2C can follow the same *row*
  pattern but must NOT rely on it as the findings owner).
- **Security rule-set:** `intake_security.py` is the single SSRF/traversal/
  tenant-scope rule-set shared by Intake, Materialization, and Git Acquisition.

**What does NOT exist yet (grep-verified; Phase 2C must design the minimal):**
no `ProjectAnalysis` / `AnalysisRun` / `ProjectRevision` / `ProjectFinding` /
`AnalysisArtifact` model; no analysis-run or synthesis service; no analysis
version history; `ANALYZING`/`PENDING_CONFIRMATION` own no state machine.

## 9. Dependencies & the Minimal Analysis Storage Model

### 9.1 Dependencies (load-bearing)
- **Conservative lock (FACT):** fastapi 0.141.1, langgraph `>=1.2,<1.3`,
  sqlalchemy 2.x, asyncpg 0.30+, redis 5.x, httpx 0.27+. `pip-audit` on
  `backend/uv.lock` = "No known vulnerabilities" (point-in-time 2026-09-24; OQ-3
  asks how to keep it fresh — treat as a regression gate, not a standing
  guarantee).
- **Reproducibility (OBSERVATION):** `uv.lock` is **gitignored**
  (`.gitignore:6`) yet `backend/AGENTS.md` claims it "records the resolved
  dependency graph" — contradictory; backend deps resolve fresh at CI build
  (risk R6).
- **Contractually pinned values:** `ci_deploy_test.sh` / `ci_upgrade_test.sh`
  assert `AGENT_RUNTIME_V2_ENABLED is True` and
  `AGENT_RUNTIME_COMMAND_CONCURRENCY == 10`.

### 9.2 Minimal model to store an Analysis (the core deliverable)

**Principle:** reuse-first. `AuditLog` + storage + `resolved_rev` can *express*
traceability but none is authoritative or revision-indexable, so we add
**one minimal typed model** (a migration chained off `f067`), deliberately
**not** a full Artifact/Evidence platform.

Entities (all tenant-scoped, `TenantScopedBaseDAO`, via a new single-head
Alembic migration; a DDL-only schema change is a **build-phase** task, not part
of this read-only analysis):

```text
analysis_runs            -- one row per analysis execution (versioning)
  id (pk)
  project_id   (fk projects)          -- WHICH Project
  agent_id     (fk)                   -- WHO / which Agent ran it
  revision_sha (str, indexed)         -- Git Revision Binding (commit hash)
  requested_ref (str)                -- what ref the analysis targeted
  resolved_at  (ts)                   -- WHEN the analysis was produced
  status       (enum)                 -- reuses a CLOSED set, no new workflow SM
  tenant_id    (fk, indexed)          -- isolation
  started_at / finished_at (ts)      -- WHEN (run window)
  UNIQUE (project_id, revision_sha)   -- re-analysis at a NEW sha => NEW row
                                       -- (never overwrites history)

analysis_findings        -- one row per finding, owned by a run
  id (pk)
  analysis_run_id (fk analysis_runs, ondelete CASCADE)
  severity  (enum)         -- e.g. INFO/WARN/HIGH/CRITICAL
  category  (enum)         -- SECURITY / RISK / TECH-DEBT / OPEN-QUESTION / FACT
  tag       (enum)         -- FACT / OBSERVATION / INFERENCE / UNKNOWN
  summary   (text)
  evidence  (json)         -- path:line anchors + source cards (traceable)
  tenant_id (fk, indexed)

project_knowledge        -- long-lived, CONFIRMED knowledge (separate object)
  id (pk)
  project_id (fk projects, indexed)
  subject  (str, indexed)  -- e.g. "backend framework"
  statement (text)         -- e.g. "backend uses FastAPI"
  source_analysis_run_id (fk analysis_runs, nullable)  -- provenance
  status   (enum)         -- PROPOSED -> CONFIRMED -> SUPERSEDED
  tenant_id (fk, indexed)
```

**Design decisions encoded (the minimum, per card stage 6 "reuse first"):**
1. **Versioning** — `analysis_runs` is append-only; `UNIQUE (project_id,
   revision_sha)` means a later analysis at a *new* commit is a *new* run, so
   history is never clobbered. "Current" = latest run per project; "history"
   = all prior runs. Satisfies card stage 8.
2. **Git Revision Binding** — `analysis_runs.revision_sha` is the commit hash;
   it *binds* findings to that revision via `analysis_findings.
   analysis_run_id`. This answers "Analysis #1 is for commit A, not 'always the
   current code'" (card stage 9). Recommend **decoupling** the revision from the
   free-form `locator` JSON (OQ-5) so revision-keyed queries are indexed, not
   JSON scans (debt D1).
3. **Transient Finding vs long-lived Knowledge — hard boundary** (card stage 7):
   - A **`analysis_findings`** row is *transient*: it is true **of one revision,
     at one time, by one agent** ("auth endpoint returned 500 in today's run").
     It is owned by a run and dies/supersedes with that run.
   - A **`project_knowledge`** row is *durable*: it is **confirmed, revision-
     independent** knowledge ("backend uses FastAPI"). A finding is *promoted*
     to knowledge only after human/company confirmation (the card's
     `PENDING_CONFIRMATION` step). Promotion copies the provenance
     (`source_analysis_run_id`) but the knowledge row is **not** invalidated by
     a later analysis of a different commit. **Do not** model a knowledge graph.
4. **No new workflow state machine.** `analysis_runs.status` is a *closed
   result-code* enum (mirroring the ACQ_* pattern), NOT a step-by-step machine
   — consistent with root `AGENTS.md` §2 (a new SM needs an independent owner +
   need). `ANALYZING`/`PENDING_CONFIRMATION` ownership is OQ-6.
5. **Concurrency prerequisite (OQ-4):** the per-(agent, repo) acquire
   serialization gate must be settled **before** revision-binding is built on
   `resolved_rev`, or two concurrent acquires race one artifact key + one
   locator flush (risk card §3.2).
6. **Isolation:** every table above is tenant-scoped and reached only through
   `TenantScopedBaseDAO` + `verify_tenant_scope` (M9 fourth gate). The
   credential DAO's lack of tenant scoping (risk §1.3) is the counter-example to
   avoid.

**Ownership / migration:** these tables are a **build-phase** deliverable
(schema + DAO + service). This document only *defines* the minimal model; it
does not create it. The owning Agent Note will record the decision when the
build card lands.

## 10. Security Findings (from the risk card, re-verified)

- **Default secrets (RISK/OBSERVATION):** dev compose ships change-me
  `SECRET_KEY`/`JWT_SECRET_KEY` (`docker-compose.yml:48-49`; startup only
  warns, `main.py:130-134`) — forgeable-JWT path if `.env` is unset.
- **No key-rotation path (UNKNOWN/OQ-1):** rotating `SECRET_KEY` permanently
  breaks decryption of at-rest `agent_credentials.cookies_json`
  (`core/security.py:62-77`; `git_acquisition_service.py:595`) → permanent
  `ACQ_AUTH_FAILED`. Decide: document unsupported-in-V1 vs add an out-of-band
  re-encryption story (Alembic DDL-only forbids inline data migration).
- **Credential host matching mismatch (OQ-2):** bare-label `github` silently
  misses `github.com`; the docstring example contradicts the implemented
  full-host semantics; the bare-label case is untested.
- **SSRF DNS-rebind TOCTOU (OQ-8, residual):** `is_unsafe_host`
  (`intake_security.py:307-357`) resolves at validation; git re-resolves at
  clone. Low likelihood (needs operator-controlled host); document as a known
  residual risk in the cross-product contract.
- **Credential injection is done well (FACT, do not regress):** process-only,
  env-injected to the git child, child env strips `GIT_CONFIG_GLOBAL/SYSTEM`,
  `GIT_SSL_NO_VERIFY`, `GIT_ASKPASS`, sets `GIT_TERMINAL_PROMPT=0`
  (`git_acquisition_service.py:800-821`).

## 11. Technical Risks

- **D1 (recon §4 / risk §4):** revision binding lives in free-form
  `repositories.locator` JSON — no DDL, no index. (§9.2 recommends decoupling.)
- **D2 (risk §4):** `ANALYZING`/`PENDING_CONFIRMATION` enum values own no state
  machine — anyone reading the enum cannot tell which service legally produces
  `ANALYZING` (OQ-6 decides the owner).
- **D3 (risk §4):** analysis output has no home — `AuditLog` is the only
  audit and is non-authoritative; findings need an authoritative owner (§9.2).
- **D4 (risk §4):** `cookies_json` is a cookies-shape table repurposed for git
  API keys (semantically muddled; accepted debt, out of Phase 2C scope).
- **R1 (runtime card):** worker boot/migration failure invisible to API health
  gate → add a worker-side schema-readiness probe.
- **R2:** minimal `.drone.yml` vs full `.github/drone.yml` drift; which the
  Drone server consumes is **UNKNOWN** from the repo — orchestrator to confirm.
- **R3:** four AGENTS.md-mandated doc refs point at files missing on
  `main` (`docs/testing.md`, `docs/model-visible-inputs.md`,
  `docs/constitution.md`, `.agents/skills/clawith-pre-push-checks/SKILL.md`).
- **R4:** unseeded `hash(agent.id)%10000` OpenClaw port collision risk
  (`agent_manager.py:283`).
- **R5:** no coverage tooling anywhere — a passing suite is backed by no
  regression metric.

## 12. Testing

- **Corpus (FACT):** 207 backend test files / 2146 `def test_*`; 27
  `*.test.mjs` frontend files run by bare `node --test` (no Jest/Vitest).
- **Gates (OBSERVATION):** CI gate = `ruff` + `arch-guard` + `pytest -q`
  (`.drone.yml`); **no coverage**; **pyright is local-only** (not in CI);
  **frontend `npm run test` is not in the automated gate**.
- **E2E DB-gating divergent (OBSERVATION):** `test_git_acquisition_
  e2e_acceptance.py:167` has a `_db_available()` autouse skip; the intake and
  materialization E2E suites have **no** skip gate and will **error, not
  skip**, if Postgres is unreachable. A new Phase 2C E2E must copy the git-acq
  skip pattern.
- **Working patterns (FACT, reuse):** real-Postgres + fake-Redis split
  (`InMemoryWorkspaceRedis` monkeypatch) and per-test engine disposal
  (`_dispose_engine_between_tests`).

## 13. Development Workflow

- **Build (FACT):** backend = `uv` project (setuptools build backend, `uv
  sync`); frontend = `node:20-alpine` + `npm ci` + `tsc && vite build`;
  production nginx pinned to `1.31.2-alpine@sha256:...` (documented seccomp
  pwrite pin).
- **Lint / type-check (OBSERVATION):** `ruff check` + `scripts/arch-guard.sh`
  (C1/C4 fatal; C2/C5/C6 warn) is the backend gate; frontend `eslint` is
  **local-only** (CI runs `tsc --noEmit` only).
- **CI/CD (OBSERVATION):** two divergent Drone files — minimal root `.drone.
  yml` (fast gate, no DB/build/CD/triggers) vs full `.github/drone.yml`
  (build → fresh-DB migration test → deploy test → upgrade test → CD on tag +
  Feishu). Which the server executes is UNKNOWN from the repo (R2).
- **Migrations (FACT, load-bearing invariant):** Single Head Rule — every new
  migration's `down_revision` must be the current single head (`f067`); the
  entrypoint runs `alembic upgrade head` on every bootstrap-role boot; CD
  migrates **in place** on the live shared Postgres (Postgres/Redis/MinIO are
  not recreated).
- **Release (FACT):** `.github/workflows/release.yml` (manual dispatch) bumps
  semver from the last `v*` tag, rewrites `backend/VERSION` + `frontend/VERSION`
  (both currently `1.11.4-fix.1`), opens a `release/vX.Y.Z` PR; merging pushes
  the tag → Drone CD. `/api/version` (`main.py:488-521`) reads VERSION+COMMIT.

## 14. Open Questions (register, for the design card)

| # | Question | Default leaning (not a decision) |
|---|----------|----------------------------------|
| OQ-1 | `SECRET_KEY` rotation supported, or documented unsupported in V1? | Document unsupported + warn on rotation |
| OQ-2 | Credential `platform` contract: full host or bare label? Fix to ONE. | Full host (implemented wins; fix docstring) |
| OQ-3 | How is the dependency audit kept fresh? | CI gate on lockfile change |
| OQ-4 | Acquire needs a per-(agent, repo) serialization gate before revision-binding? | Yes, minimal in-process lock keyed on (agent, repo) — **settle first** |
| OQ-5 | Revision binding: extend `locator` JSON or add a minimal typed model? | **Add minimal typed model** (§9.2) |
| OQ-6 | Who owns `ANALYZING`/`PENDING_CONFIRMATION`? | Phase 2C owns ANALYZING; PENDING_CONFIRMATION stays inert until a confirmation UI exists |
| OQ-7 | Is 'use'-level agent access enough for mutating acquire POST? | Keep 'use' for read/status, 'manage' for the mutating POST — design-card decision |
| OQ-8 | Is the SSRF DNS-rebind TOCTOU acceptable in V1? | Accept + document; re-verify when a pinned-resolver path is practical |

**UNKNOWN (declared, not inspectable read-only):** live tenant data volume;
live `STORAGE_BACKEND` value; whether CI host has a reachable shared Postgres
(→ decides whether the two un-gated E2E suites pass or fail on the gate); which
of `.drone.yml` / `.github/drone.yml` the Drone server executes; whether the
frontend's 27 node tests are exercised outside the repo.

## 15. Evidence References

- **Source cards (all four):** listed in §0 table with branch + commit; each
  source doc carries its own `path:line`-cited, tag-evidenced claims.
- **Re-verified on this worktree @ `a9e83a8a`:** `main.py:22-34`
  (`_process_roles`/`_role_enabled`), `config.py:129` (`PROCESS_ROLE`),
  `project.py:34/48-62/98/116-129/135` (Project, status enum, Repository,
  source_type enum, locator), `git_acquisition_service.py:178/284-299/340/1040`
  (`resolved_rev` writeback), HEAD `a9e83a8aec27ce36cb7e8c36c48306a65e571342`.
- **Not re-verified in this card (inherited from source cards, cited in them):**
  the LLM-client registry line anchors, scheduler `SKIP LOCKED`, entrypoint
  boot sequence, test-corpus counts, arch-guard rules. These are carried
  forward with their source-card + commit provenance and were NOT re-run here;
  the reviewer (`t_d58eae44`) spot-checks them against live source on its own
  worktree.

---

### Verdict for the design / build card

Phase 2C has a **clean, reuse-first insertion point**: add **one minimal
tenant-scoped, revision-bound, versioned persistence model**
(`analysis_runs` + `analysis_findings` + `project_knowledge`, §9.2) chained off
`f067`, following the Git-Acquisition closed-code / storage-facade / audit-row
pattern, with a **hard boundary** between transient findings and confirmed
long-lived knowledge. **Prerequisites before building: settle OQ-4
(concurrency) and OQ-5 (revision carrier).** This document is **read-only** —
it defines the model, it does not create the schema or service (build phase).
