# Phase 2C — Risk, Secrets & Open-Questions Assessment (Read-Only Defensive Analysis)

Task: t_88a15667 (Phase 2C gate — "Assess Risks, Secrets & Open Questions").
Method: READ-ONLY. No source, schema, migration, or config was modified.
Every load-bearing claim is tagged FACT / OBSERVATION / INFERENCE / UNKNOWN and
cited to `path:line`. Baseline pinned to main HEAD `a9e83a8a` (recon card
t_4e4599d8, `docs/PHASE_2C_RECON_BASELINE.md`).

Scope: defensive analysis only — security risks, secrets handling, dependency
vulnerabilities, architectural debt, and open questions that Phase 2C design
must settle before building the Analysis layer.

---

## 1. Secrets & Cryptographic Handling

### 1.1 Default / weak secret material in committed config

- **FACT** — `backend/app/config.py:91` `SECRET_KEY` defaults to
  `"change-me-in-production"`; `:105` `JWT_SECRET_KEY` defaults to
  `"change-me-jwt-secret"`.
- **FACT** — `docker-compose.yml:48-49` falls back to those same change-me
  values for local development; `docker-compose.cd.yml:18-19` correctly
  enforces `${SECRET_KEY:?...}` for the production-oriented compose file;
  `docker-compose.ci.yml:39-40` uses deliberate `ci-test-*` values.
- **FACT** — `backend/app/main.py:130-134` logs a startup WARNING when a
  change-me value is detected, but does NOT fail startup.
- **RISK (OBSERVATION)** — the local-dev compose ships the app with
  world-known JWT + app secrets; anything deployed from `docker-compose.yml`
  without `.env` overrides runs with forgeable JWTs and is vulnerable to full
  auth bypass. Mitigation is a log line only.
- **RISK (OBSERVATION)** — `SECRET_KEY` is the AES key material for at-rest
  credential encryption (`core/security.py:62-77` — `encrypt_data` derives
  the AES-256 key via `SHA256(key)`; used by `api/agent_credentials.py:111`
  and `services/git_acquisition_service.py:595` `decrypt_data(cipher,
  get_settings().SECRET_KEY)`). A leaked/rotated `SECRET_KEY` silently breaks
  every stored `agent_credentials.cookies_json` (git-acquisition then maps to
  permanent `ACQ_AUTH_FAILED`, `git_acquisition_service.py:598`).

### 1.2 Open Question OQ-1 — secret key rotation for encrypted credentials

- **UNKNOWN** — no key-rotation / re-encryption path exists for
  `agent_credentials.cookies_json`. If `SECRET_KEY` is ever rotated in
  production, all encrypted credentials (browser cookies + git API keys,
  stored in the `cookies_json` column, `models/agent_credential.py:18`)
  become permanently undecryptable. **Design decision required before Phase
  2C touches the credential path**: either (a) document that key rotation is
  unsupported in V1, or (b) add a data-migration story (which is forbidden in
  Alembic DDL-only migrations — `backend/alembic/AGENTS.md` §2 "no inline
  data migration", would need an out-of-band script under `backend/scripts/`).

### 1.3 AgentCredential DAO is not tenant-scoped

- **FACT** — `models/agent_credential.py` defines no `tenant_id` column;
  `dao/agent_credential_dao.py:9` `AgentCredentialDAO(BaseDAO)` extends
  `BaseDAO`, not `TenantScopedBaseDAO`, and `list_by_agent`
  (`:17-25`) filters only on `agent_id`.
- **OBSERVATION** — this matches the DAO-layer transitional rule for models
  without `tenant_id` (`backend/app/dao/AGENTS.md` §8.5: document the
  isolation mechanism used). Isolation is achieved INDIRECTLY: every call
  site authorizes the agent first — `api/agent_credentials.py:59` (manage
  level + role check) and `git_acquisition_service._resolve_credential`
  (`:578`), whose agent argument passed `check_agent_access` (tenant match,
  `core/permissions.py:557`) plus the M9 fourth gate
  (`git_acquisition_service.py:393-403`) before the DAO call.
- **RISK (INFERENCE)** — any FUTURE call site that passes an agent object
  without the tenant gate (a background worker, a new service) can read
  another tenant's decrypted tokens via `list_by_agent(agent_id)`. The guard
  lives in caller discipline, not in the DAO.

### 1.4 Credential host-suffix matching semantics

- **FACT** — `git_acquisition_service.py:590-593`: a stored credential's
  `platform` matches a source host iff `host == platform or
  host.endswith("." + platform)` — i.e. the credential row must store the
  FULL host (e.g. `github.com`), not a bare label (e.g. `github`).
- **OBSERVATION** — the module docstring's own example ("`github.com`
  matches `github`", `:587`) contradicts the implemented semantics: a row
  with `platform="github"` does NOT match host `github.com` (neither `==`
  nor `endswith(".github")`), so a private GitHub clone with a bare-label
  credential degrades to no token → `ACQ_AUTH_FAILED`.
- **UNKNOWN (OQ-2)** — which shape is the contract: full host (implemented)
  or bare label (documented example)? Phase 2C should fix ONE: either
  normalize `platform` at write time (API `agent_credentials.py:102-107`
  stores it raw) or align the docstring. Tests use only full-host fixtures
  (`tests/test_git_acquisition_service.py:484,501-504`), so the bare-label
  case is untested — behavior there is silent credential-miss, not an error
  with a useful detail.

### 1.5 What is handled well (do not regress)

- **FACT** — git credential injection is process-only: decrypted at the use
  boundary, injected via `GIT_CONFIG_*` env to the child only
  (`git_acquisition_service.py:800-821`); never written to locator/model/log/
  audit; `_record_success` re-scans the updated locator
  (`:1058-1061`) before flushing.
- **FACT** — the child env strips `GIT_CONFIG_GLOBAL/SYSTEM`,
  `GIT_SSL_NO_VERIFY`, `GIT_ASKPASS` and sets `GIT_TERMINAL_PROMPT=0`
  (`:811-816`) — no interactive credential hang, no host git-config override.
- **FACT** — intake credential guard rejects locator fields naming
  credentials and URL userinfo shapes (`intake_security.py:518-540`), with
  a bounded, secret-free detail string stored in `rejection_detail`
  (`:172-183`).

---

## 2. Dependency Vulnerabilities

- **FACT** — `pip-audit` (run 2026-09-24 against `backend/uv.lock`, `uvx
  pip-audit`): **"No known vulnerabilities found"**.
- **OBSERVATION** — the lock pins conservative majors: fastapi 0.141.1,
  langgraph `>=1.2,<1.3` + `langgraph-checkpoint-postgres >=3.1,<3.2`,
  sqlalchemy 2.x, asyncpg 0.30+, redis 5.x, httpx 0.27+
  (`backend/pyproject.toml:7-60`).
- **RISK (OBSERVATION)** — `httpx[socks]` and `httpx>=0.27.0` are declared
  independently at `pyproject.toml:18` and `:60` — the floor is far below the
  lock; a fresh `uv lock` on a different machine could re-resolve to older
  httpx depending on the registry snapshot. The audit result is valid only
  for the committed `uv.lock`.
- **Open Question OQ-3 — dependency governance:** there is no lockfile-CI
  attestation or scheduled re-audit documented; "clean today" is a
  point-in-time fact. Recommend Phase 2C treat the uv.lock audit as a
  regression gate, not a standing guarantee.

---

## 3. Security-Surface Risks (Phases 2A/2B-4 code, still live)

### 3.1 SSRF guard has a DNS-rebinding TOCTOU window

- **FACT** — `intake_security.is_unsafe_host` (`:307-357`) resolves the host
  with `socket.getaddrinfo` at VALIDATION time and rejects private/
  loopback/link-local/reserved targets, failing closed on any exception.
- **RISK (OBSERVATION)** — the guard and the subsequent git clone use
  DIFFERENT resolution moments: a DNS response that validates public can
  re-resolve private at clone time (classic rebinding TOCTOU; the check is
  not pinned to the resolved address and git re-resolves independently).
  The git child honors the `extraheader` scheme `https://<host>/` so the
  attacker needs an operator-controlled git host — low likelihood, but the
  SSRF rule is documented as THE cross-product rule (design §A.5), so the
  window should be noted in that contract.

### 3.2 Concurrent acquires of the same repo write the same locator

- **FACT** — no per-repo in-flight lock exists in
  `git_acquisition_service` (grep-verified: only a per-call nonce working
  dir, `:282-284`). Two concurrent `acquire` calls for the same
  (agent, repo) run two git children in two nonces, publish to the SAME
  artifact key, and both flush `repo.locator` (last flush wins).
- **OBSERVATION / RISK** — the artifacts are content-equivalent only if the
  two runs resolve the same rev; a ref move between the two calls leaves
  two tar contents racing into one key. Materialization then reads the loser.
  No data corruption is expected (tar is one object, published last), but
  "which revision does the tar carry?" becomes racy. **OQ-4: decide
  whether acquire needs a per-(agent, repo) serialization gate before
  Phase 2C builds revision-binding on top of `resolved_rev`.**

### 3.3 `local_git` source type reads a host path as a git repo

- **FACT** — `local_git` locators are host paths checked by
  `intake_security.check_host_path` (traversal + sensitive-root denylist,
  `:238-274`) and must contain `.git` (`git_acquisition_service.py:560-562`);
  then `git clone` runs FROM that directory on the app host
  (`:647`). The sensitive-root list (`:148-163`) is a denylist of well-known
  roots; any readable git repo OUTSIDE those roots is a valid source.
- **OBSERVATION** — this is by-design for the deployment model (host access
  is the ops contract, recon §UNKNOW-1), but it means "which host paths may
  be a git source?" is policy defined by the denylist, not an allowlist.
  Worth one explicit sentence in Phase 2C's design.

### 3.4 Git executable resolution is PATH-trusted

- **OBSERVATION** — `git` is spawned by bare name through `PATH`
  (`_git`, `:850`); a host whose `PATH` is shadowable could substitute the
  executable. Low likelihood on a controlled deployment; recorded as a
  residual assumption, not a finding to fix in this phase.

### 3.5 Intake/Materialization/Acquisition authorization (verified sound)

- **FACT** — project routes gate on `verify_read_access` (creator or
  same-tenant admin, `intake_security.py:679-699`, wired in
  `api/projects.py:28-38`); agent-dimension gates via
  `check_agent_access` (tenant isolation at `core/permissions.py:557`);
  cross-tenant combinations closed by the M9 fourth gate
  (`git_acquisition_service.py:393-403`). `POST /projects` maps to the
  intake state machine with the closed 6-code rejection set
  (`intake_security.py:91-100`).
- **OBSERVATION** — the acquire/materialize routes DISCARD the
  `check_agent_access` access level (`_access_level`,
  `api/projects.py:249,359,404`): any caller with 'use'-level access to the
  agent can trigger acquisition / materialization. Whether "use" is the
  intended grant for consuming an agent's scoped artifact is a permission
  model question, not a boundary violation.

---

## 4. Architectural Debt (relevant to Phase 2C)

- **Debt D1 (OBSERVATION)** — `repositories.locator` free-form JSON is the
  carrier of acquisition state (`acq_artifact`, `resolved_rev`,
  `requested_ref`, `provider`, `acquired_at`, `acq_result`, `acq_detail`,
  written at `git_acquisition_service.py:1034-1042`). The git-revision
  binding Phase 2C needs lives inside a free-form column with NO DDL
  constraints and NO index. The recon baseline (§8) already flags the
  decision: add a minimal typed model vs. keep the locator field. This doc
  adds the risk framing: revision-binding queries ("which findings belong to
  rev X?") become JSON scans without a schema object.
- **Debt D2 (OBSERVATION)** — `ANALYZING` / `PENDING_CONFIRMATION` enum
  values exist but own no state machine (recon §9; confirmed here:
  grep across `backend/app` finds only `models/project.py:53-54` and the
  intake_security defense list `:640-641`). Anyone reading the 10-value
  enum today cannot tell which service legally produces `ANALYZING`.
- **Debt D3 (OBSERVATION)** — analysis output has no home: no
  `Artifact` model exists (recon §7 — artifacts are string fields on tool
  executions, `agent_runtime/tool_result_store.py:94`); the `AuditLog`
  pattern is best-effort and deliberately non-authoritative
  (`git_acquisition_service.py:1062-1074`). Phase 2C persistence must pick
  an authoritative owner for Analysis findings; `AuditLog` alone would be
  the second, non-authoritative home.
- **Debt D4 (OBSERVATION)** — the credential model is a
  cookies-shape table repurposed for API keys ("reuse the model, no schema
  change", `git_acquisition_service.py:584-588`). The column name
  `cookies_json` holding a git token is semantically muddled and is a known
  debt the acquisition design accepted explicitly. Flag for a future
  schema decision, not Phase 2C.

---

## 5. Open-Questions Register (for the Phase 2C design card)

| # | Question | Default leaning (not a decision) | Evidence anchor |
|---|----------|----------------------------------|-----------------|
| OQ-1 | Is `SECRET_KEY` rotation supported for at-rest credentials, or documented unsupported in V1? | Document unsupported + warn on rotation | §1.2; `core/security.py:62-77` |
| OQ-2 | Credential `platform` contract: full host or bare label? Fix doc/code/tests to ONE. | Full host (implemented behavior wins; fix the docstring) | §1.4; `git_acquisition_service.py:584-595` |
| OQ-3 | How is the dependency audit kept fresh (CI attestation vs. scheduled re-audit)? | CI gate on lockfile change | §2 |
| OQ-4 | Does acquire need a per-(agent, repo) serialization gate before revision-binding builds on `resolved_rev`? | Yes, minimal in-process lock, keyed on (agent, repo) | §3.2 |
| OQ-5 | Revision binding: extend `repositories.locator` JSON or add a minimal typed model (recon §8 / design-stage 6 "reuse first")? | Decide in the Phase 2C design doc; this doc records that JSON has no DDL/index support for revision-keyed queries | §4 D1 |
| OQ-6 | Who owns the `ANALYZING` / `PENDING_CONFIRMATION` transitions — a new Analysis service, or stay inert and documented? | Phase 2C owns ANALYZING; PENDING_CONFIRMATION stays inert until a confirmation UI exists | §4 D2; `intake_security.py:640-641` |
| OQ-7 | Is 'use'-level agent access sufficient to trigger acquire/materialize, or should it require 'manage'? | Keep 'use' for read/status, 'manage' for the mutating acquire POST — decision belongs to the design card, not this audit | §3.5; `api/projects.py:249,359,404` |
| OQ-8 | Is the SSRF DNS-rebinding TOCTOU acceptable in V1 (documented residual risk) or must the resolver be pinned? | Accept + document; re-verify when a pinned-resolver path becomes practical | §3.1 |

---

## 6. Verdict (for orchestrator)

1. **FACT** — no committed secrets; no unbounded dependencies (`pip-audit`
   clean on `uv.lock`); no code was modified in this card.
2. **RISK HEADLINES** — weak default secrets in the dev compose (§1.1),
   unsupported credential key rotation (§1.2/OQ-1), untenant-scoped
   credential DAO guarded only by caller discipline (§1.3), and a
   doc/implementation mismatch on credential host matching (§1.4/OQ-2).
3. **DESIGN PREREQUISITES** — OQ-4 (acquire serialization) and OQ-5
   (revision-binding carrier) must be settled BEFORE Phase 2C starts
   writing the Analysis layer, because both determine whether
   `resolved_rev` remains trustworthy under concurrency.
4. **GUARDRAILS CARRIED FORWARD** (recon §10, re-verified here): Phase 2C
   persistence goes through a tenant-scoped DAO + `verify_tenant_scope`;
   analysis stays read-only against existing Project rows; no status-enum
   change without an OQ-6 decision.
