# E2E Acceptance Report — Project Materialization (Phase 2B-3, t_025cda02)

- Date: 2026-09-22 (UTC+09:00)
- Branch/worktree: `wt/t_025cda02` @ `I:/project/AI Company OS/.worktrees/t_025cda02`
- Driving spec: `docs/MATERIALIZATION_SECURE_SPEC_V1.md` committed on
  `wt/t_c672b2c2` @ `10d16f4f` (parent card t_c672b2c2) — the single
  adjudicated text the builder and reviewer consume.
- Merged parents under test: `5830624c` (main, incl. Phase 2B-2 Intake
  lifecycle + f067).

## 1. Scope

Closed-loop acceptance of "Materializing a project": copying an
INITIALIZED Project's verified source material into a target agent's
storage subtree (`{agent_id}/projects/{project_id}/{material_name}/`),
executed through the **real FastAPI app, real service, real local storage
backend, real Postgres** — no HTTP-level mocking of the app under test
(spec §11.13). The only stub is the Redis layer, which this dev host does
not run; per the repo's established practice
(`test_password_reset_and_notifications.MockRedis`) the lock layer's
`get_redis` is pointed at an in-memory fake that implements exactly the
two commands `workspace_locking` uses (`set nx` + owner-checked release
`eval`).

Test DB: `clawith_t025cda02_e2e` on `127.0.0.1:5432` (scratch DB, owned by
`postgres`, `clawith` granted DML; schema via `Base.metadata.create_all`
over the **full** model registry because the alembic chain is broken on a
fresh DB by an unrelated pre-existing `agent_schedules.delivery_target_id`
migration — inherited from the intake E2E report §6).

## 2. Evidence commands + results

All from `backend/`, run with the project venv:

| # | Command | Result |
|---|---------|--------|
| 1 | `pytest tests/test_project_materialization_service.py` | 46 passed |
| 2 | `pytest tests/test_materialization_e2e_acceptance.py` (DATABASE_URL -> scratch Postgres) | 11 passed |
| 3 | `pytest tests/test_project_intake_service.py tests/test_workspace_reconciliation.py tests/test_files_api_storage.py` | 76 passed, 1 pre-existing failure (see §5) |
| 4 | `pytest tests/test_intake_e2e_acceptance.py` (DATABASE_URL -> scratch Postgres) | 22 passed |
| 5 | `pyright app/services/project_materialization_service.py app/api/projects.py app/schemas/project_intake.py tests/test_project_materialization_service.py` | 0 errors |
| 6 | `ruff check` on the 6 touched files | remaining findings are repo baseline families only (see §4) |

The 46-test service suite (`tests/test_project_materialization_service.py`)
covers spec §11 items 1–12 (DB-free): status gate (only INITIALIZED),
Zip Slip via **real** malicious archives, host path traversal / sensitive
roots, reserved-name boundary, the §2.3/§2.5 key formula asserted on the
key string, tenant isolation (M9 gate + entry re-check), happy path with
revision/audit provenance, idempotency (CONVERGED, no drift), content
conflict 0-writes + overwrite-before-content, busy publish lock PARTIAL +
staging cleanup, human edit lock, unready sources, budgets, and the §3
read-only-source invariant.

The 11-test acceptance suite (`tests/test_materialization_e2e_acceptance.py`)
covers spec §11.13:

- **Happy path (zip source)**: 201 SUCCESS; files land under the
  authorized agent's subtree with the exact §2.3 layout; 2 revision rows
  (group_key `materialize:{project_id}:{repo_id}:{agent_id}`,
  actor_type=system, actor_id=caller) + 1 AuditLog row committed in the
  request transaction; no staging residue; the §9.3 zip symlink
  limitation is reported in `limitations`.
- **Idempotency**: a repeat call is 201 CONVERGED (1 converged, 0
  written), the target hash is unchanged, and no duplicate revision row.
- **Content conflict**: differing pre-existing target + overwrite=false is
  a 409 FAILED with `CONTENT_CONFLICT`, 0 new writes (existing content
  untouched); overwrite=true replaces the target and the revision row
  records the BEFORE content (`before_content == "OLD"`).
- **Human edit lock**: an active `WorkspaceEditLock` on the target file is
  a 409 `HUMAN_LOCK_CONFLICT` (retryable), 0 writes, the human's draft
  untouched, no revision rows.
- **Busy directory lock** (spec §6.1): a concurrently-held
  `tenant:{tenant}:workspace-lock:{agent}:projects/{project_id}/<material>`
  lock makes that repo `LOCK_CONFLICT` while the other repo still lands
  (PARTIAL, retryable); the failing repo's staging subtree is fully
  cleaned.
- **Status gate**: RECEIVED / SOURCES_OK / COMPLETED projects are a 409
  `SOURCE_NOT_READY` (retryable=False), nothing written; a seeded
  pending-verifier row on an INITIALIZED project also fails closed with
  `SOURCE_NOT_READY` (defense-in-depth).
- **Agent gate**: an unknown agent id is a 404; a foreign-tenant agent is
  a 404 (tenant-scoped DAO — no disclosure, mirroring the intake E2E's
  isolation rule); a same-tenant *private* agent owned by someone else is
  a 403 (`check_agent_access` runs before the service).

## 3. Product bugs found by the real-DB run and fixed in this card

1. **Windows portability defect in the local storage backend** —
   `storage_runtime/local.py::_atomic_write_bytes` called `os.fchmod` to
   preserve an existing target file's mode on overwrite; `os.fchmod`
   does not exist on Windows (this repo's dev platform — the module's
   own `fcntl` note documents the "Windows dev host" situation). The
   overwrite E2E was the first test to exercise the existing-file path,
   so the defect surfaced only here.
   Fix (owning layer = storage backend): the mode preservation is skipped
   when the platform has no POSIX mode bits (`hasattr(os, "fchmod")`),
   mirroring the fcntl import guard at module top.
   No contract change: on Unix deployments behavior is byte-identical.

None of the other fixes touched state machines, security contracts, or
schema — the service re-uses the intake-stage guards
(`intake_security.check_host_path` / `check_zip_slip` /
`verify_tenant_scope`, `workspace_locking.workspace_locks`,
`workspace_collaboration.get_active_lock`, `storage_runtime` conditional
writes) exactly per spec §4's routing rule.

## 4. Linter posture of the 6 touched files

- `app/services/project_materialization_service.py`, the API/schema files,
  and the unit test file: clean except the repo's accepted baseline
  families — BLE001 on the documented narrow `except Exception` sites
  (staging failure, storage outage, revision/audit failure; same pattern
  as the merged intake service, which carries 3 un-flagged) and B008
  (`Depends` in defaults — the whole API layer's pattern).
- `tests/test_materialization_e2e_acceptance.py`: 2 pyright annotation
  findings on the ASGI-client fixture, **identical to the two carried by
  the already-merged `tests/test_intake_e2e_acceptance.py`** (shared
  accepted baseline; not introduced here).
- `storage_runtime/local.py`: my diff is a 5-line guard; the 2 remaining
  findings in the file (I001 import block, TRY201 `raise cancelled`)
  pre-date this change and are outside the diff.

## 5. Pre-existing failures (verified on the clean tree, unrelated to
materialization, recorded for the convergence owner)

Confirmed failing **without** any of this card's changes (git stash):

- `test_storage_conditional_atomicity.py` — 3 barrier tests
  (`require_absent` / `same_version` writer + deleter): the local
  backend's cross-process barrier is `fcntl`-based, a documented no-op on
  Windows, so the "only one writer wins" invariant does not hold on this
  host.
- `test_agent_files_api.py::test_list_files_existing_skills_directory_returns_entries`
  and `test_workspace_reconciliation.py::test_directory_move_candidate_covers_every_source_file`:
  POSIX-slash path-separator assertions against Windows
  `Path`-produced backslash paths.

## 6. Verdict

The "Materializing a project" stage is **functional and secure end to
end** against a real Postgres: files land only under the authorized
agent's subtree; security vectors (Zip Slip, traversal, reserved names,
cross-tenant) are rejected with 0 writes through the authoritative
intake-stage guards; the three-phase read→staging→publish protocol keeps
staging clean on every exit path; idempotency converges without drift;
conflicts are reason-coded and transport-mapped per spec §10.2 (PARTIAL /
FAILED = 409 with the full per-repo body, never 2xx). Final verdict:
PASS.

## 7. Open items (out of this card's scope, recorded)

- **No Redis on this dev host**: the acceptance suite's documented
  in-memory fake stands in for the lock layer only; the production
  Redis-backed lock recipe (spec §6.1, tenant-scoped keys) is exercised
  for real by the lock's own logic, not by a live server. A host with
  Redis running can drop the fixture and re-run the same suite against
  the real layer.
- **S3 backend**: cross-process mutation locking for the S3 backend is a
  documented spec M3 limitation (V1 = local + fallback; conditional
  writes remain available).
- The 3 pre-existing Windows-platform test failures (§5) need a
  dedicated fix card (platform-conditional assertions / barrier
  semantics), not materialization.
