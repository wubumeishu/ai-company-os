# GIT_ACQ_DESIGN_V1 — Git Source Acquisition: Reuse Strategy & Integration Design

- Card: t_82ac3524 (architect audit) → feeds t_4874c3e7 (implement), root t_18e2c495 (Phase 2B-4)
- Evidence base: main HEAD `fe77d1d6` (worktree `wt/t_82ac3524`), audit run 2026-09-23
- Author: aco-architect

Every claim below is `path:line` against the main tree. "REUSE" = existing authoritative
mechanism the acquisition service MUST call; "GAP" = net-new surface this card introduces;
"HOOK" = exact line where existing code must change.

---

## A. Preflight audit findings

### A.1 Git CLI in the codebase (near-zero — no wrapper to reuse)

| Finding | Location |
|---|---|
| Only synchronous git call in app: `subprocess.check_output(["git","rev-parse","--short","HEAD"], timeout=3)` for the version endpoint | `backend/app/main.py:506-513` |
| No git Python library dependency (`gitpython` etc. — grep of `pyproject.toml`/`uv.lock` = none) | `backend/pyproject.toml` |
| `GIT_SOURCE_TYPES = frozenset({"github","gitlab","local_git"})` exists in THREE places (intake service, materialization service, schema) — all currently "fail closed / not supported" | `project_intake_service.py:106-112`, `project_materialization_service.py:86`, `schemas/project_intake.py:22` |

Conclusion: there is NO reusable Git wrapper. The implementer builds a minimal
parameterized runner (see B.1) — NOT a git-platform framework.

### A.2 Subprocess execution patterns (REUSE the discipline, not the code)

| Pattern | Location | Reuse decision |
|---|---|---|
| `asyncio.create_subprocess_exec(*argv)` (arg-list, never shell) | `agent_tools.py:12287`; `sandbox/local/subprocess_backend.py:252,481,522,857,1283` | REUSE the pattern: argument-list calls only |
| Two-stage process-group termination (SIGTERM → grace wait → SIGKILL) via `_terminate_and_reap_process` | `sandbox/local/subprocess_backend.py:221-245` | REUSE pattern (copy the 25-line recipe; do NOT import the bwrap sandbox backend — it is Agent-Run-bound) |
| Bounded output capture (`MAX_EXEC_STDERR_CAPTURE_BYTES`-style stream read loop) | `agent_tools.py:12295-12309` | REUSE pattern for stderr capture of git output |
| Resource-limit preexec (RLIMIT_CPU/AS/FSIZE/NOFILE/NPROC, `start_new_session=True`, umask 0o077) | `sandbox/local/subprocess_backend.py:330-359` | GAP: not cross-platform as-is (bwrap/Linux-only). V1: `start_new_session=True` + timeout-based kill is the portable floor; rlimits are an optional hardening item, not a V1 gate |
| Sandbox timeout config keys `SANDBOX_DEFAULT_TIMEOUT`/`SANDBOX_MAX_TIMEOUT` (180/300) | `app/config.py:206-207` | REUSE as the config *pattern*: add `GIT_ACQUISITION_MAX_SECONDS` (default 300, max 900) in `app/config.py` — do not reuse the sandbox values themselves (different semantics) |

### A.3 Credential / secret mechanisms (REUSE)

| Mechanism | Location | Decision |
|---|---|---|
| AES-256-CBC `encrypt_data(plaintext, key)` / `decrypt_data(ciphertext, key)` (Base64, IV-prefixed) | `app/core/security.py:55-124` | REUSE for token-at-rest (new `agent_credentials` rows, `credential_type="api_key"`) |
| `AgentCredential` model: `agent_id` + `platform` + `credential_type` ("website"/"email"/"social"/"api_key") + encrypted payload + status | `app/models/agent_credential.py:30-64` | REUSE model; new `platform` values `"github"` / `"gitlab"` + `credential_type="api_key"` — NO schema change needed (platform is String(100)) |
| DAO (scoped by agent) | `app/dao/agent_credential_dao.py:17-29` | REUSE `list_by_agent` / `get_by_agent` |
| Locator credential guard: rejects any locator key naming a credential AND any URL userinfo `user:pass@host` | `intake_security.py:347-398` (`scan_locator_for_credentials`) | REUSE at intake — the "credentials never enter Project/Repository fields" invariant is ALREADY enforced; acquisition inherits it, not re-invented |
| Runtime decryption at the use boundary (precedent: agents decrypt `cookies_json` at AgentBay injection) | `agentbay_client.py:1048-1075` | REUSE pattern: decrypt immediately before spawning git, inject into the child's env only, never log/return it |

**Design ruling (card §5):** token-at-rest lives in `agent_credentials` (agent-scoped,
encrypted, existing schema); the `Repository.locator` gets at most a
`credential_ref` (an `agent_credentials.id` UUID string — NOT the token, NOT agent-bound
at storage time). Runtime resolution: `agent_id` of the *target materialization agent* →
`agent_credential_dao.list_by_agent` → pick active row with `platform in {"github","gitlab"}`
matching the source host → `decrypt_data(..., settings.SECRET_KEY)` → inject as HTTP basic
auth (`https://x-access-token:<token>@host`) in the git process env only. Public repos:
no token required. Token value never written to `repositories`, `projects`, logs, or
audit details.

### A.4 Workspace isolation / staging (REUSE heavily)

| Mechanism | Location | Decision |
|---|---|---|
| Path-escape guard `resolve_path_within_root(root, rel)` (resolve + `relative_to`) | `app/services/workspace_paths.py:21-48` | REUSE for acquisition staging layout |
| Host-path shape gate `check_host_path(raw, source_type)` (traversal/NUL/sensitive-root → verdict) | `intake_security.py:232-268` | REUSE for `local_git` paths |
| Symlink-skip discipline in tree enumeration | `project_materialization_service.py:399-404` | REUSE rule: after any git material read, symlinks are skipped, never followed |
| Staging key formula `{agent_id}/.materialize-tmp/{repo_id}/...` + "delete tree on EVERY exit path" invariant | `project_materialization_service.py:197-200, 626-665` | REUSE the formula convention: acquisition staging root = `{agent_id}/.git-acq/{repo_id}/` (sibling of `.materialize-tmp`, never inside it) |
| Storage facade `write_bytes / read_bytes / get_version / delete_tree / exists / stat` via `get_storage_backend()` | `project_intake_service.py:74-85`, `storage_runtime/facade.py` | REUSE for persisting the acquired artifact (tar) so Materialization re-reads ONE bounded object, not a re-clone |
| TempWorkspace default materialize set + reserved name table | `project_materialization_service.py:95-110` | REUSE: the acquired tar's member paths pass through the SAME reserved-name / `..`-rejection normalizer (`_normalize_rel`) at materialization |

### A.5 SSRF / network (REUSE one helper, GAP one gate)

| Finding | Location |
|---|---|
| `is_private_url(url)`: rejects localhost/127.0.0.1/::1/0.0.0.0, resolves via `socket.getaddrinfo`, rejects private/loopback/link-local/reserved IPs; any exception → `True` (fail-closed) | `app/services/trigger_runtime/evaluator.py:153-172` |
| REUSE: acquire the *logic* but move it to `intake_security.py` as `is_unsafe_host(url)` (fail-closed on exception, `reason_code=SECURITY_REJECTED`). `trigger_runtime` is a consumer, not an owner of a cross-product rule. |
| GAP: a URL-scheme gate (https-only; reject `file://`/`ssh://`/`ftp://`/`javascript:`/`data:`) does not exist anywhere — new `validate_git_url(url)` in `intake_security.py` (keep the module stdlib-only: `urlparse` + `ipaddress` + `socket`). |
| Retry precedent: bounded `MAX_RETRIES = 3` hardcoded (config deferred by design) | `project_intake_service.py:114-116` |

---

## B. What is net-new (GAPs) — minimal service boundary

One new service module: `backend/app/services/git_acquisition_service.py`
(shared instance `git_acquisition_service`, mirroring `project_materialization_service`
module shape). Nothing else new: no new model, no new API router, no new DAO.

### B.1 Core surface

```
class GitAcquisitionService:
    async def acquire(self, *, repo: Repository, project: Project,
                      db: AsyncSession, agent_id: uuid.UUID,
                      actor: User) -> AcquisitionOutcome
```

Pipeline (all bounded, all fail-closed):

1. **URL/ref validation (pure, no I/O)** — `validate_git_url`, ref regex
   `^[A-Za-z0-9._/-]{1,128}$` for branch/tag, `^[0-9a-f]{7,40}$` for commit.
   Local_git: `check_host_path` + `resolve_path_within_root` against the tenant
   staging root (card §8/§18).
2. **Credential resolve** (A.3): none / token / AUTH_FAILED — token exists in the
   process only, injected as env for the git child.
3. **Bounded git run** (A.2 pattern): `create_subprocess_exec("git", "clone",
   "--depth", "1", "--no-recurse-submodules", url, dest)` — arg-list only;
   `start_new_session=True`; `asyncio.wait_for(communicate(), GIT_ACQUISITION_MAX_SECONDS)`;
   on timeout → two-stage reap (SIGTERM group → grace 10 s → SIGKILL group).
   Commit-pinning: `--depth 1` then `git fetch --depth 1 origin <sha>` when the server
   advertises `allowReachableSHA1InWant`, else `git fetch --depth 1 origin <sha>` fallback
   = full `git fetch origin <sha>` (documented: big repos pay once); **verify**
   `git rev-parse HEAD` == requested sha (card §13 — "clone succeeded" is NOT
   success). Branch/tag: after clone `git rev-parse <ref>^{commit}` and confirm
   `HEAD` checkout matches. Default branch (card §6): `git ls-remote --symref <url> HEAD`
   to read the remote's own default — never assume `main`.
4. **Acquisition-area post-checks** (in the staging dir, before any publish):
   reserved first-segment names and `..` on every member path (reuse
   `_normalize_rel`-equivalent logic — factor a shared helper into
   `intake_security.py` so materialization and acquisition share ONE rule, card §18);
   symlinks: recorded + skipped (never followed); submodules: `.gitmodules` present →
   `SUBMODULES_UNSUPPORTED` (card §16 V1 ruling: fail-closed, no `--recurse`).
5. **Artifact publish**: `tar` the acquisition dir (no hooks, no install — card §17),
   size-bounded (reuse `MAX_MATERIALIZE_FILE_BYTES`/`_TOTAL_BYTES` constants,
   `project_materialization_service.py:117-118`), write to
   `{agent_id}/.git-acq/{repo_id}/source.tar` via `write_bytes`; then ALWAYS
   `delete_tree` the working dir (A.4 invariant, every exit path).
6. **Record**: `repo.locator` gains `{"acq_artifact": "<storage key>"}` +
   `repo.verified = True, pending_verifier = False, verified_at = now`;
   best-effort `AuditLog(action="git_acquisition", ...)` — NO token, no raw URL
   userinfo, bounded detail (A.3 invariant).
7. Return `AcquisitionOutcome` (closed result set §C below).

### B.2 Closed result codes (V1, service-owned — does NOT collide with the intake 6-set)

| code | meaning | retryable |
|---|---|---|
| `ACQ_OK` | artifact + metadata written | — |
| `ACQ_SOURCE_NOT_FOUND` | repo/referenced ref does not exist | no |
| `ACQ_SOURCE_UNREACHABLE` | transient network/remote | yes (bounded, MAX_RETRIES=3 reused) |
| `ACQ_AUTH_FAILED` | token missing/invalid | no |
| `ACQ_REF_NOT_FOUND` | branch/tag/commit absent | no |
| `ACQ_SOURCE_INVALID` | not a usable git source | no |
| `ACQ_SECURITY_REJECTED` | SSRF / path / reserved-name / URL-scheme violation | no, never retried |
| `ACQ_TIMEOUT` | acquisition over budget | yes (bounded) |
| `ACQ_SIZE_LIMIT` | artifact exceeds byte budget | no |
| `SUBMODULES_UNSUPPORTED` | `.gitmodules` present; V1 refuses | no |

`MaterializationNotReady` (existing) still carries `SOURCE_NOT_READY` for the
pre-acquire gate; the ACQ_* codes are only stored/returned by the acquisition
endpoint — one closed set, no cross-namespace reuse of "SECURITY_REJECTED"
strings (they are related but distinct contracts: intake-set vs acquisition-set;
both documented in `intake_security`/`schemas`).

---

## C. Integration points (exact hooks)

### C.1 — SOURCE_NOT_READY fail-closed hook points (verbatim, for t_4874c3e7)

These are the EXACT current fail-closed lines. The acquisition work must keep
them fail-closed UNTIL a repo has a completed acquisition artifact:

| # | File:Line | Code | Behavior |
|---|---|---|---|
| 1 | `backend/app/services/project_materialization_service.py:302-307` | `for repo ...: if not repo.verified or repo.pending_verifier: raise MaterializationNotReady(code="SOURCE_NOT_READY", ...)` | Unverified repo → 409, 0 I/O |
| 2 | `backend/app/services/project_materialization_service.py:308-312` | `if repo.source_type in GIT_SOURCE_TYPES: raise MaterializationNotReady(code="SOURCE_NOT_READY", ...)` | Any git repo still hard-fails today |
| 3 | `backend/app/api/projects.py:248-255` | `except MaterializationNotReady → 409 {code, message, retryable}` | transport mapping (do not change) |
| 4 | `backend/app/services/project_intake_service.py:471-475` | `if source_type in GIT_SOURCE_TYPES: return await self._validate_unsupported(repo)` | intake dispatch for git |
| 5 | `backend/app/services/project_intake_service.py:725-731` | `_validate_unsupported` → `SOURCE_NOT_SUPPORTED` (permanent; project → REJECTED) | the "enum first, capability later" marker |
| 6 | `backend/app/services/project_intake_service.py:106-112` | `GIT_SOURCE_TYPES` frozenset (single source of truth in intake; materialization re-imports the constant, `project_materialization_service.py:86`) | change capability HERE, not per-call-site |
| 7 | `backend/app/schemas/project_intake.py:22` | `GIT_SOURCE_TYPES = ("github","gitlab","local_git")` (schema-layer known-types) | unchanged |
| 8 | `backend/app/schemas/project_intake.py:217-223` | `MaterializationRepoResult.reason_code` doc: closed set incl. `SOURCE_NOT_READY` | extend the doc-set, not the code-set, when ACQ codes are surfaced |

### C.2 — New API (one router extension, card §22)

In `backend/app/api/projects.py` (same `router = APIRouter(prefix="/projects")`,
line 59; both endpoints under the existing `_load_authorized_project`
gate at line 69):

```
POST /projects/{project_id}/repositories/{repo_id}/acquire
     body: {}   (ref context = repo.locator as-registered)
     201 AcquisitionOut  |  409 {code: ACQ_*, retryable}  |  403 tenant/agent
GET  /projects/{project_id}/repositories/{repo_id}/acquire
     200 AcquisitionStatusOut {state: pending|acquired|failed, code, artifact_key,
                               resolved_rev, acquired_at}
```

`AcquisitionOut`/`AcquisitionStatusOut` → new small models in
`schemas/project_intake.py` beside `MaterializationOut` (lines 191-246).
Transport stays a pure adapter (backend AGENTS.md): all policy in the service.

### C.3 — Intake flip (the moment fail-closed becomes fail-open, and ONLY then)

- `project_intake_service.py:471-475` branch changes: a git repo whose
  `locator.acq_artifact` exists + verified → `ValidationOutcome(ok=True)`;
  otherwise keep `_validate_unsupported` (i.e., NOT-acquired git stays
  `SOURCE_NOT_SUPPORTED`). The flip is a ONE-LINE gate inside the existing
  dispatch — the 6-code intake set, retry budget (line 116), and REJECTED
  terminal semantics are untouched (card §19: no Project-lifecycle extension).
- Materialization gate (hook #2 above): the git-reader replaces the hard
  `GIT_SOURCE_TYPES` raise with `elif repo.source_type in GIT_SOURCE_TYPES:`
  → `_plan_git(plan)` that (a) fails closed `SOURCE_NOT_READY` unless
  `locator.acq_artifact` is set AND `repo.verified`, (b) reads the ONE tar
  artifact via `get_storage_backend()`, (c) re-runs the shared member-path
  normalizer + reserved-name check, (d) skips symlinks — i.e., the SAME read
  discipline as `_plan_local_folder` (lines 383-415). No re-clone in the
  materialization stage (the artifact is the handoff object, card §11).

### C.4 — Persistence: `No schema change required` (card §26 ruling)

- `repositories.locator` is a free-form JSON column (`models/project.py:135`)
  with a documented "adding a new source type must not require a schema change"
  invariant (line 131-134). Acquisition metadata rides in `locator`:
  `provider`, `resolved_rev`, `requested_ref`, `acq_artifact`, `acquired_at`,
  `acq_result`. **No Alembic migration, single-head f067 preserved.**
- `agent_credentials` already exists (migration 027 + 047): new platform
  values are data, not schema.
- Retry accounting for acquisition reuses `repositories.retry_count`
  (line 151) with the same `MAX_RETRIES=3` budget (intake line 116).

### C.5 — Tenant / agent scoping

Acquisition runs inside `tenant_context(project.tenant_id)` (DAO layer rule,
`app/dao/AGENTS.md` §8.4) and re-asserts `verify_tenant_scope`
(`intake_security.py:521`) at entry — the materialization precedent
(`project_materialization_service.py:291`). Staging root is agent-scoped
(`{agent_id}/.git-acq/...`) so two tenants can never share a git working dir.

---

## D. Reuse strategy summary (table the builder must honor)

| Capability | Use | Do NOT |
|---|---|---|
| subprocess | `create_subprocess_exec` arg-lists + two-stage reap (`subprocess_backend.py:221-245` recipe) | bwrap sandbox backend, `shell=True`, string concat, shell-quoted env for credentials |
| timeout | `asyncio.wait_for` + `GIT_ACQUISITION_MAX_SECONDS` new config key (pattern `config.py:206-207`) | unbounded clone; reusing 180 s sandbox timeout value |
| secrets | `encrypt_data`/`decrypt_data` (`core/security.py:55`) on `agent_credentials` rows; locator `credential_ref` = UUID only | `repository.token` columns, tokens in URL userinfo, tokens in audit/log, tokens in `locator` |
| path safety | `check_host_path` + `resolve_path_within_root` + shared `_normalize_rel`-class rule | a second, contradicting traversal rule set (card §18) |
| SSRF | promote `is_private_url` logic into `intake_security.is_unsafe_host`; https-only scheme gate | trusting a bare `"http"` substring check |
| staging | `{agent_id}/.git-acq/{repo_id}/` + `delete_tree` on every exit (`.materialize-tmp` invariant) | cloning into project dir, agent workspace root, or user-supplied dirs (card §10) |
| artifact | single bounded tar in storage facade, read back at materialization | re-cloning in the materialization stage; re-running git hooks/installers (card §17) |
| retries | transient only (`ACQ_SOURCE_UNREACHABLE`, `ACQ_TIMEOUT`), bounded 3 (`MAX_RETRIES` line 116) | retrying `ACQ_AUTH_FAILED` / `ACQ_SECURITY_REJECTED` (card §21) |
| default branch | `git ls-remote --symref <url> HEAD` (card §6) | hardcoding `main` |
| submodules | `.gitmodules` → `SUBMODULES_UNSUPPORTED` (card §16) | `--recurse-submodules` |

---

## E. Risks / follow-ups (non-V1, recorded, not built)

- Containerized deployment: git + network egress is an ops contract
  (same UNKNOW 1 posture as the intake host-path ruling,
  `intake_security.py:247-249`).
- rlimit hardening of the git child (Linux-only) — optional future card.
- Token refresh/rotation for private repos — out of V1 (public + static token).
- Windows host: `start_new_session=True` no-ops for process-group kill; the
  two-stage reap already degrades to `proc.kill()` (precedent
  `subprocess_backend.py:229-244`).

## F. Verification plan for t_4874c3e7 (card §24-§27)

- Unit: URL scheme/host/SSRF matrix; ref injection (`; rm -rf`, `$(...)`,
  backtick, NUL); local_git non-dir/plain-dir/non-git/valid-git/path-traversal;
  credential "never in DB field" assertion (scan `repositories.locator` after
  acquire); security-rejection no-retry; timeout simulation.
- Real E2E: one public GitHub repo, one public GitLab repo, one local repo;
  commit-pin + branch + default-ref each verified by `git rev-parse HEAD == requested`.
- Regression: 2B-1/2B-2/2B-3 suites green — the git hard-gate at hook #2 stays
  `SOURCE_NOT_READY` for ANY repo without `acq_artifact` (fail-closed preserved
  by construction: absence of the key = old behavior).

---
End of design doc. Absolute path for the implement card:
`I:\project\AI Company OS\.worktrees\t_82ac3524\docs\GIT_ACQ_DESIGN_V1.md`
