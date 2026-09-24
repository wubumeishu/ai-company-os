# Phase 2C — Reconnaissance & Capability Baseline (Read-Only Audit)

Task: t_4e4599d8 (Phase 2C gate 1 — "Reconnaissance & Capability Audit").
Method: read-only. No source, schema, migration, or config was modified.
Every load-bearing claim is tagged FACT / OBSERVATION / INFERENCE / UNKNOWN and
pointed at a code/config/doc location. "Read the file" evidence is cited as
`path:line`.

---

## 0. Audit Baseline (what this snapshot is pinned to)

- **main HEAD audited against:** `a9e83a8a` (== `origin/main`, in sync at
  audit time; verified `git rev-parse HEAD` = `git rev-parse origin/main`).
- **Worktree:** `.worktrees/t_4e4599d8`, branch `wt/t_4e4599d8` (project
  p_6582d63b, "AI Company OS").
- **Migration single head (file-graph, verified by this card):** `f067_intake_rejection_fields`.
  Chain (relevant tail): `f065_feishu_group_target -> f066_add_project_repo_tables
  -> f067_intake_rejection_fields` (each `down_revision` points to the prior;
  f067 is not referenced as any `down_revision`). Consistent with the prior
  migration-graph card (t_a66da085) which re-verified single head = f067.
- Repo root: `I:\project\AI Company OS` (worktree mirror at
  `.worktrees/t_4e4599d8`).

---

## 1. Project — PRESENT, authoritative lifecycle owner

- **Model (FACT):** `backend/app/models/project.py:34` `class Project(Base)`
  -> table `projects`. Fields: `name`, `description`, `goal`, `status`,
  `created_by` (FK users), `tenant_id` (FK tenants), `created_at/updated_at/
  status_changed_at`, `rejection_reason`, `rejection_detail`.
- **Status lifecycle (FACT):** 10-value `Enum` `project_status_enum`
  (`project.py:48-62`): RECEIVED, SOURCES_OK, INITIALIZED, ANALYZING,
  PENDING_CONFIRMATION, EXECUTING, BLOCKED, COMPLETED, ARCHIVED, REJECTED.
  `ProjectIntakeService` owns the RECEIVED/SOURCES_OK/INITIALIZED/REJECTED
  transitions; `ANALYZING`/`PENDING_CONFIRMATION`/`EXECUTING`/`BLOCKED` are
  declared in the enum but **NOT yet consumed by any state machine** (see §7
  Unknowns). They appear only as a defence-in-depth allow-list in
  `intake_security.py:640`.
- **Rejection reason codes (FACT):** closed set owned by the service,
  `project_intake_service.py:92-99`: SOURCE_NOT_FOUND, SOURCE_INVALID,
  SECURITY_REJECTED, SOURCE_UNREACHABLE, DISTRIBUTION_FAILED, SOURCE_NOT_SUPPORTED.
  Persisted on the Project row (`rejection_reason`/`rejection_detail`).
- **Design authority (FACT):** `docs/PHASE2A_PROJECT_DESIGN_V1.md` (§A project,
  §F.1a repository naming boundary, §D.1/D.3 field must-have/excluded lists)
  and `docs/PROJECT_DOMAIN_V1.md`.

## 2. Repository — PRESENT, minimal source/asset registry (NOT implied-git)

- **Model (FACT):** `backend/app/models/project.py:98` `class Repository(Base)`
  -> table `repositories`. Real FK to `projects.id` (`ondelete=CASCADE`).
  `source_type` 7-value Enum `repository_source_type_enum` (`project.py:116-129`):
  manual, local_folder, document, zip, github, gitlab, local_git.
- **Locator JSON (FACT):** free-form `locator: JSON` column (`project.py:135`)
  holds per-type structured locators; no branch/commit/credential/provider
  columns on the model (design §F.1 minimalism rule). The git acquisition
  result rides in this same JSON (§4) — **no schema change was made for git**.
- **Verification + retry marks (FACT):** `verified`, `verified_at`,
  `pending_verifier` (bool), `retry_count` (int, server_default 0) —
  `project.py:137-151`. Git sources are permanently rejected on first validate
  with `SOURCE_NOT_SUPPORTED` and do NOT use the pending-verifier hold
  (`project.py:144-149` docstring; `project_intake_service.py:478 _not_supported_outcome`).
- **DAO (FACT):** `backend/app/dao/project_intake_dao.py` owns project/repo
  persistence; tenant-scoped per the DAO layer rules
  (`backend/app/dao/AGENTS.md`, `TenantScopedBaseDAO` mandatory `tenant_id`).

## 3. Intake — PRESENT, authoritative state-machine owner

- **Service (FACT):** `backend/app/services/project_intake_service.py:184`
  `class ProjectIntakeService`. Public: `create_intake()` (:192),
  `validate_sources()` (:268). Constants: `GIT_SOURCE_TYPES` frozenset
  {github, gitlab, local_git} (:106), `MAX_RETRIES = 3` (:116).
- **State machine (FACT):** status transitions gated by `_assert_transition`
  (:440); terminal set = `intake_security.TERMINAL_INTAKE_STATUSES`.
  Non-reachability discipline: git sources held in RECEIVED/SOURCES_OK with a
  pending-verifier mark are intentionally unreachable under the INITIALIZED
  gate.
- **Security module (FACT):** `backend/app/services/intake_security.py` —
  `verify_tenant_scope`, `git_url_detail`, `is_unsafe_host`, `check_host_path`,
  `normalize_rel`, `scan_locator_for_credentials`. This is the **single
  shared security/traversal/SSRF rule-set reused by Intake, Materialization
  AND Git Acquisition** (design "reuse strategy", card §18).
- **API (FACT):** `backend/app/api/projects.py` — `POST /projects`
  (:116), `GET /projects` (:150), `GET /projects/{id}` (:173),
  `POST /projects/{id}/validate` (:184). Router registered in
  `backend/app/main.py:428` + included `:479` under `settings.API_PREFIX`.

## 4. Git Acquisition — PRESENT (Phase 2B-4), the net-new reusable stage

- **Service (FACT):** `backend/app/services/git_acquisition_service.py:211`
  `class GitAcquisitionService` (singleton `git_acquisition_service` :1146).
  Independent stage **between Intake and Materialization** (design §B):
  registered git source -> verified bounded `source.tar` artifact + resolved
  revision; never re-clones, never runs project code, never starts an Agent/Run.
- **Public API (FACT):** `acquire()` (:219), `status()` (:316).
  `acquire` pipeline: entry gates -> shape validation -> credential resolve
  (process-only, env-injected) -> bounded git run (two-stage process-group
  reap) -> post-checks -> publish one bounded tar -> record locator metadata +
  audit row.
- **Closed result codes (FACT):** `backend/app/schemas/project_intake.py:259-288`
  — ACQ_OK, ACQ_SOURCE_NOT_FOUND, ACQ_SOURCE_UNREACHABLE, ACQ_AUTH_FAILED,
  ACQ_REF_NOT_FOUND, ACQ_SOURCE_INVALID, ACQ_SECURITY_REJECTED, ACQ_TIMEOUT,
  ACQ_SIZE_LIMIT, SUBMODULES_UNSUPPORTED; `ACQ_RESULT_CODES` frozenset :270;
  retryable = {ACQ_SOURCE_UNREACHABLE, ACQ_TIMEOUT} :288 (bounded by
  `repositories.retry_count` + intake `MAX_RETRIES`). Distinct from the Intake
  6-code set.
- **Storage key (FACT):** `{agent_id}/.git-acq/{repo_id}/source.tar`
  (agent-scoped, normalized via `storage_runtime.utils.normalize_storage_key`),
  `_artifact_key` :1122. Metadata (acq_artifact, resolved_rev, requested_ref,
  provider, acquired_at, acq_result, acq_detail) written back into
  `repositories.locator` JSON — **the resolved revision IS persisted on the
  repo row**, which is the existing revision-binding hook Phase 2C can reuse.
- **Credential handling (FACT):** `agent_credentials` (`credential_type=api_key`,
  `platform` host-suffix match) decrypted at the use boundary via
  `core.security.decrypt_data`, injected only into the git child's env
  (`GIT_CONFIG_COUNT`/`extraheader`), never into locator/model/log/audit
  (`_resolve_credential` :569, `_git_env` :789).
- **Bounds + config (FACT):** total wall `config.GIT_ACQUISITION_MAX_SECONDS`
  (default 300, `app/config.py:218`); per-file/total byte budgets reuse
  Materialization's `MAX_MATERIALIZE_FILE_BYTES`/`MAX_MATERIALIZE_TOTAL_BYTES`
  (50MB/500MB); ref shape gates `_REF_RE`/`_SHA_RE` :125-126.
- **API (FACT):** `backend/app/api/projects.py:323`
  `POST /projects/{project_id}/repositories/{repo_id}/acquire/{agent_id}`
  -> `AcquisitionOut`, 201. (Siblings: `POST /{project_id}/materialize/
  {agent_id}` :222 -> `MaterializationOut`, 201; `GET /{project_id}/repositories/
  {repo_id}/acquire/{agent_id}` :386 -> `AcquisitionOut`.)
- **Design + audit docs (FACT, archived in main):** `docs/GIT_ACQ_DESIGN_V1.md`,
  `docs/GIT_ACQ_SECURITY_AUDIT_T31F91B3A.md`,
  `docs/GIT_ACQ_E2E_ACCEPTANCE_T4874C3E7.md`,
  `docs/PHASE_2B4_GIT_ACQUISITION_CONVERGENCE.md`.

## 5. Materialization — PRESENT, the consumer of the acquired artifact

- **Service (FACT):** `backend/app/services/project_materialization_service.py:228`
  `class ProjectMaterializationService`. Public: `materialize()` (:236).
- **Status gate (FACT):** only `INITIALIZED` projects may materialize
  (`_entry_gates` :273, check :303 `project.status != "INITIALIZED"`).
  A materialization that ever sees a non-INITIALIZED project is a programming
  error, not a supported path.
- **Git source consumption (FACT):** `_plan_git` :566 reads the ONE `source.tar`
  that acquisition published (`_git_artifact_verified` :549 verifies the repo's
  `acq_artifact` mark first — **no re-clone**). Byte budgets:
  `MAX_MATERIALIZE_FILE_BYTES = 50MB`, `MAX_MATERIALIZE_TOTAL_BYTES = 500MB`
  (:118-119). Reserved first-segment names: `RESERVED_STORAGE_NAMES` :96.
- **Staging cleanup invariant (FACT):** staging tree is deleted on EVERY exit
  path; the acquisition service reuses this exact invariant (design §A.4).

## 6. Storage — PRESENT facade, local default

- **Facade (FACT):** `backend/app/services/storage_runtime/facade.py:30`
  `get_storage_backend()`. `settings.STORAGE_BACKEND` default `"local"`
  (`app/config.py:113`); local root `STORAGE_LOCAL_ROOT` :116; S3 primary
  + optional local fallback (`STORAGE_LOCAL_FALLBACK_ENABLED` :117).
  Concrete backends: `local.py`, `s3.py`, `fallback.py`, `base.py`,
  `agent_files.py`, `utils.py`. Both Intake/Materialization/Acquisition
  publish + read through this facade (`get_storage_backend` is the shared
  owner of the physical material).

## 7. Reusable-for-Phase-2C capabilities (what to build ON, not replace)

- **Knowledge / retrieval (FACT, reusable):**
  `backend/app/services/experience_retrieval.py` — `search_experience` (:368),
  `read_experience`, `record_experience_citations`, `build_experience_hint`
  (:135), department-scoped visibility (`_agent_department_ids` :91).
  Models: `experience.py` `ExperienceEntry` -> `experience_entries`,
  `experience_reference.py` -> `experience_references`;
  `skill.py` `Skill`/`SkillFile` -> `skills`, `skill_files`.
  **Boundary:** Experience = long-lived (≈Project Knowledge); a one-run
  "Analysis Finding" is a different object. No vector RAG in this baseline
  (structured entries + skills + optional directory search only).
- **Audit (FACT, reusable):** `backend/app/models/audit.py` `AuditLog` —
  the acquisition service already writes best-effort, bounded, secret-free
  audit rows (`git_acquisition_service.py:1057-1076`); Phase 2C analysis
  persistence can follow the same pattern.
- **Artifact-as-typed-field, NOT a table (OBSERVATION):** there is **no
  `Artifact` model/table** — artifacts are first-class string fields on
  tool executions (`agent_runtime/tool_result_store.py:94`
  `artifact_refs`) + `published_pages` for pages. `docs/CAPABILITY_CONCEPT_MAP.md`
  §7 records this explicitly.
- **Storage + agent-scoped keying (FACT, reusable):** the
  `{agent_id}/...` scoping + `normalize_storage_key` rule is the existing
  isolation primitive Phase 2C artifacts should reuse for tenant isolation
  (reviewer point 10, "cross-tenant read risk").
- **Subprocess + two-stage reap recipe (FACT, reusable):**
  `sandbox/local/subprocess_backend.py:221-245` is the authoritative
  process-group reap the git acquisition already copied.

## 8. What does NOT exist yet (Phase 2C must design the minimum of)

- **No dedicated Analysis / Finding / Revision model (FACT — verified):**
  grep for `class ProjectAnalysis|AnalysisRun|ProjectRevision|AnalysisRevision|
  ProjectFinding|AnalysisArtifact` across `app/models/` + `app/services/`
  returns **nothing**. `app/models/project.py:11` explicitly EXCLUDES
  "analysis JSON" from the Project model. `ANALYZING`/`PENDING_CONFIRMATION`
  are enum-declared but have **no owning state machine** yet.
  **Implication:** Phase 2C needs a *minimal* new model (or reuse of
  `AuditLog` + storage + `repositories.locator.resolved_rev`) to express
  "which Project, which Analysis run, when, by which Agent, based on which
  Sources, produced which Findings" — **and** the git-revision binding
  (Analysis #1 is for commit A, not "always the current code"). Per card
  stage 6: reuse existing models first; add the minimal model only if they
  genuinely cannot express it.
- **No analysis-run / synthesis service, no Finding model, no
  analysis-version history (UNKNOWN — none of these exist; confirm before
  building).**
- **No project-code-execution sandbox for analysis (by design):** the card
  forbids running project code; `execute_code`/sandbox exists but is for
  the Agent Runtime, not for static analysis. Dynamic analysis is deferred
  to a later Execution/Analysis-sandbox stage.

## 9. Unknowns / Limits (explicit `UNKNOWN` markers)

- `UNKNOWN` — whether any tenant currently carries live Project/Repository
  rows (DB not queried in this read-only card; the gate is structural, not
  data-volume).
- `UNKNOWN` — `STORAGE_BACKEND` value in the actual deployment (config default
  is "local"; live prod setting not inspected here).
- `UNKNOWN` — whether a "Source Revision" object distinct from
  `repositories.locator.resolved_rev` is required, or whether the existing
  locator field suffices for git-revision binding (design decision, Phase 2C).
- `OBSERVATION` — the 10-value Project status enum anticipates ANALYZING /
  PENDING_CONFIRMATION, but no service currently drives them; Phase 2C must
  either own the ANALYZING transition or document that it stays inert.
- `INFERENCE` — "Repository is not guaranteed to be a git repo" is a naming
  boundary (design §F.1a); the 3 `*_git`/`*github`/`gitlab` source_types are
  the only ones the acquisition stage supports.

## 10. Regression guardrails for Phase 2C (must not break)

- Project + Intake + Materialization + Git Acquisition all live on
  `a9e83a8a`; the 2B-4 security audit + E2E acceptance docs are archived in
  main. Phase 2C analysis must be **read-only against the project** and must
  not pollute Project rows or change the status enum without a decision.
- Multi-tenant isolation: every new persistence path must go through a
  tenant-scoped DAO + `verify_tenant_scope` (the M9 fourth gate the
  acquisition entry already enforces, `git_acquisition_service.py:387-413`).
- Analysis persistence must stay revision-bound and versioned (card stages 6/8/9).

---

### Three key facts downstream cards (t_88a15667, t_93876cee, t_d9a3eafd) need

1. **What exists & is reusable:** `Project` + `Repository` models
   (`models/project.py`), `ProjectIntakeService` (state machine + 6 rejection
   codes), `GitAcquisitionService` (ACQ_* closed set, agent-scoped
   `source.tar` + `repositories.locator.resolved_rev` revision binding),
   `ProjectMaterializationService` (INITIALIZED-only gate, reads the one tar),
   storage facade `get_storage_backend()`, knowledge via `experience_retrieval`
   / `ExperienceEntry` / `Skill`, and `AuditLog`. No project code is run.
2. **What is UNKNOWN / net-new for Phase 2C:** there is **no**
   Analysis/Revision/Finding model or analysis-run service; `ANALYZING` /
   `PENDING_CONFIRMATION` enum values have no owning state machine; the
   git-revision binding must be designed on top of
   `repositories.locator.resolved_rev` (or a minimal new model). Confirm
   "source revision" ownership before building.
3. **Baseline HEAD audited against:** `a9e83a8a` (== origin/main, in sync),
   worktree branch `wt/t_4e4599d8`, migration single head `f067_intake_rejection_fields`.
