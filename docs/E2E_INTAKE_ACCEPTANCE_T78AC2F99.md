# E2E Acceptance Report — Project Intake (Phase 2B-2, t_78ac2f99)

- Date: 2026-09-22 (UTC+09:00)
- Branch/worktree: `wt/t_78ac2f99` @ `I:/project/AI Company OS/.worktrees/t_78ac2f99`
- Merged parents under test: `bcb423c` (security slice, t_3fdac523) + `2d54122` (Intake service + f067, t_7d2ae798)

## 1. Scope

Closed-loop acceptance of "Receiving a project": Create Project (RECEIVED) ->
Validate Source (SOURCES_OK / REJECTED / hold) -> Initialize (INITIALIZED),
executed through the **real FastAPI app, real services, real DAOs, real
Postgres** — no HTTP-level mocking of the app under test.

Test DB: `clawith_t78ac2f99_e2e_v2` on `127.0.0.1:5432` (scratch DB; schema
applied via SQLAlchemy metadata because the pre-existing alembic chain is
broken on an unrelated `agent_schedules.delivery_target_id` migration — see §6).

## 2. Evidence commands + results

All from `backend/`, run with `uv run --extra dev`:

| # | Command | Result |
|---|---------|--------|
| 1 | `pytest tests/test_intake_security.py tests/test_project_intake_service.py tests/test_project_intake_migration.py tests/test_project_repository_migration.py` | 109 passed |
| 2 | `pytest tests/test_intake_e2e_acceptance.py` (DATABASE_URL -> scratch Postgres) | 22 passed |
| 3 | `ruff check app/dao/project_intake_dao.py tests/test_intake_e2e_acceptance.py` | All checks passed |
| 4 | `pyright app/dao/project_intake_dao.py app/services/project_intake_service.py` | 0 errors |
| 5 | `alembic heads` (inherited gate, from t_7d2ae798) | single head f067 |

The 22-test acceptance suite (`backend/tests/test_intake_e2e_acceptance.py`)
covers, per brief §13:

- **Lifecycle**: manual happy path RECEIVED -> SOURCES_OK -> INITIALIZED
  (create/validate/initialize endpoints, real rows + real transitions).
- **Source types**: local_folder (real dir on disk, empty/missing ->
  SOURCE_INVALID / SOURCE_NOT_FOUND), document (real file + storage key,
  unsupported extension -> SOURCE_INVALID), zip (real zip on disk; zip-slip
  hostile archive -> SECURITY_REJECTED, hostile member path is NOT echoed in
  rejection_detail), manual (no-op validation succeeds).
- **git source types**: github/gitlab/local_git -> SOURCE_NOT_SUPPORTED,
  permanent, no retry (Phase 2B-2 boundary honored).
- **Reason-code / no-leak invariant**: every REJECTED response carries a
  closed-set reason_code; `rejection_detail` contains only class-level
  strings — raw host paths / zip member names / locators are asserted absent.
- **Illegal jumps**: REJECTED -> INITIALIZED, INITIALIZED -> RECEIVED,
  re-validate of terminal projects -> 409 / `IntakeTransitionError`;
  RECEIVED -> INITIALIZED without SOURCES_OK also refused.
- **Transient + retry bound**: storage-outage backend -> SOURCE_UNREACHABLE
  holds (RECEIVED, retry_count climbing 0->1->2), escalation at MAX_RETRIES
  (counter ends at the bound 3, terminal REJECTED, retryable=False), and a
  4th validate on the terminal project raises `IntakeTransitionError`
  (state machine enforced at the real DB-backed path).
- **Cross-tenant isolation**: tenant A's projects invisible/forbidden to
  tenant B (read 403/404, validate 403, list scoped); unbound-tenant request
  fails closed (no 500, no cross-tenant data).
- **Audit + persistence shape**: `project.intake.received` /
  `project.intake.validated` audit rows written; rejection fields persisted
  on projects (f067 columns populated).

## 3. Product bugs found by the real-DB run and fixed in this card

The unit suite (109 tests, fake-DAO level) did not exercise the async ORM
lifecycle; the real-DB run surfaced three genuine defects:

1. **NOT NULL `project_id` on child rows at create flush** — `Project.id`
   default (`uuid.uuid4`) applies only at INSERT, so child `Repository`
   rows constructed before flush carried `project_id=None`.
   Fix: `project_intake_service.create_intake` pre-assigns
   `project.id = uuid.uuid4()` before building children (single flush,
   concrete FK).
2. **MissingGreenlet on the create API view** — `ProjectOut.from_project`
   read the unloaded `repositories` relationship of the just-inserted
   object; lazy load outside the async session's greenlet.
   Fix (owning layer = DAO): `add_project_with_repositories` now re-queries
   the project with `selectinload(Project.repositories)` inside its
   session context and returns the fully-loaded instance.
3. **MissingGreenlet after every status/repository write** — server-side
   `onupdate`/server-default columns (`created_at`, `updated_at`) are
   expired after `flush()`; `ProjectOut`/`RepositoryOut` serialization in
   the API then triggered out-of-greenlet refreshes.
   Fix (owning layer = DAO): every write method
   (`mark_status`, `reject`, `mark_sources_ok`, `mark_verified`,
   `mark_pending_verifier`, `clear_pending_verifier`,
   `increment_retry_count`) re-queries with `selectinload` / refreshes the
   affected rows inside its `session()` context, mirroring the existing
   read-scoped DAO pattern.

None of the fixes change state-machine, security, or schema contracts —
they make the already-persisted facts correctly observable by API views in
an async session.

## 4. Test-side notes (not product)

- Test DB schema built via `Base.metadata.create_all` (all models) because
  the alembic chain is currently broken (see §6).
- `asyncpg` pools connections bound to the creating event loop; an autouse
  fixture disposes the shared engine pool between tests so each
  pytest-asyncio test's fresh loop never reuses a cross-loop connection.
- JWT auth in tests uses the app's own `python-jose` token factory (no
  `pyjwt` dependency introduced); routes use trailing slashes.
- Two initial acceptance assertions were corrected to the real (correct)
  product contract: zip-slip detail is class-level (asserts *absence* of the
  raw hostile path, not a literal `".."`), and a sensitive-root host path is
  registered at create (credential gate blocks *secrets*, not paths) and
  rejected at validate.

## 5. Verdict

The "Receiving a project" loop is **functional and secure end to end**
against a real Postgres: lifecycle transitions succeed only along the
legal state machine; all four V1 source types validate with real I/O;
rejections are reason-coded and leak-free; cross-tenant access fails
closed; audit + f067 persistence verified row-level. Final verdict: PASS.

## 6. Open items (out of this card's scope, recorded for the convergence owner)

- **Pre-existing broken alembic migration** `agent_schedules.delivery_target_id`
  blocks `alembic upgrade head` on a fresh DB (worktree-local
  `agent_schedules` schema drift); unrelated to Intake. E2E used metadata
  DDL instead. Needs a dedicated fix card.
- Host-side egress block on `github.com:443` prevented pushing
  `wt/t_7d2ae798` (recorded on that card; this branch inherits the same
  limitation — commit is local, push attempted from this host).
