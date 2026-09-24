# Phase 2C — Runtime, Testing & Development Workflow Analysis

Task: t_93876cee (Phase 2C gate — "Analyze Runtime, Testing & Workflow").
Method: read-only. No source, schema, migration, config, or CI was modified.
Every load-bearing claim is tagged **FACT / OBSERVATION / INFERENCE / UNKNOWN**
and pointed at a concrete `path:line` (or config file). "Read the file"
evidence is cited as `path:line`.

Baseline pin: main HEAD `a9e83a8a` (= `origin/main`), worktree
`.worktrees/t_93876cee` on branch `wt/t_93876cee`. Companion recon baseline:
`docs/PHASE_2C_RECON_BASELINE.md` (card t_4e4599d8).

> **Verification protocol honored:** every command listed in §5 was actually
> executed in this worktree before the fact was recorded. Nothing below is
> inferred from memory or a sibling doc. Where a claim is a structural
> inference from verified facts, it is tagged INFERENCE; where it could not
> be confirmed read-only, it is tagged UNKNOWN.

---

## 0. Scope note (what this card owns)

This card analyzes three axes that the recon baseline (t_4e4599d8) did NOT
cover, because that card was a domain-model audit:

1. **Runtime model** — how the system starts, which processes/containers
   exist, what ports are used, how concurrent roles are coordinated.
2. **Testing infrastructure** — what is verified, how it is gated, and what
   is *missing* (coverage, portability, dead doc references).
3. **Development workflow** — build, lint, type-check, CI/CD, deploy.

It does **not** re-audit the domain model (Project / Repository / Intake /
Git Acquisition / Materialization — see the recon baseline §1–§6). It is the
operational/verification complement to it.

---

## 1. Runtime model

### 1.1 Process topology (one image, N roles)

The backend ships as a **single Docker image** whose behavior is selected at
startup by the `PROCESS_ROLE` env var (comma-separated role list). There is no
separate per-role image; role selection happens in two places that must stay
in agreement:

- **`backend/entrypoint.sh:49`** — `role_contains "bootstrap"` decides whether
  to run `alembic upgrade head` (:52) + `setup_langgraph_checkpoints` (:74).
- **`backend/app/main.py:30 _role_enabled(...)`** — the FastAPI lifespan
  gates its background daemons on the same role set:
  - `_role_enabled("all","bootstrap")` → seeding + `create_all` (:152)
  - `_role_enabled("all","api")` → realtime router subscriber (:282)
  - `_role_enabled("all","worker")` → `trigger_daemon` + `agent_schedule_scheduler`
    (:307-311) AND the durable `running_runtime_worker_context` (:331-335)
  - `_role_enabled("all","connector")` → Feishu/DingTalk/WeCom/WeChat/Discord
    stream managers (:312-319)

`config.py:129` `PROCESS_ROLE: str = "all"` is the default. The three
deployment topologies instantiate this differently:

| File | Service | PROCESS_ROLE | Purpose |
|---|---|---|---|
| `docker-compose.yml` | `backend` (single) | `all` | dev — one container does everything |
| `docker-compose.cd.yml` | `backend-api` | `api` | CD — front-line HTTP only |
| `docker-compose.cd.yml` | `backend-worker` | `bootstrap,worker,connector` | CD — daemons + bootstrap |
| `docker-compose.ci.yml` | `backend` (single) | `all` | CI deploy/upgrade tests |

- **FACT** `docker-compose.cd.yml` `backend-api` `PROCESS_ROLE: api` and
  `backend-worker` `PROCESS_ROLE: bootstrap,worker,connector` — a two-container
  split so the API plane restarts independently of the worker plane. Both mount
  `/data/agent_data` and the docker socket; only `bootstrap` runs migrations.
- **INFERENCE** — because `alembic upgrade head` runs **only** under the
  `bootstrap` role, in the CD split the **worker** container is the one that
  migrates, not the API container. That is the correct owner (migrations
  mutate schema the API must not half-write under load), but it means a CD
  deploy where `backend-worker` fails to start leaves the API container running
  on the *old* schema. The CD script `restart-services` health-checks through
  the frontend proxy (: `curl .../api/health`), not through the worker, so a
  silent worker-boot failure is **not** caught by the post-deploy health gate.

### 1.2 Container startup sequence

`backend/entrypoint.sh` (invoked as the image `ENTRYPOINT`) is the single boot
authority for the container. Order:

1. Root → privilege drop. `entrypoint.sh:33-39` — if `id -u = 0`, chowns
   `AGENT_DATA_DIR` to `clawith:clawith` then `exec gosu clawith ...`. The
   Dockerfile deliberately **omits `USER`** so the volume chown can happen at
   runtime (`backend/Dockerfile` "Note: USER is removed" comment).
2. `INSTANCE_ID` derivation. `entrypoint.sh:43-47` — defaults to
   `<safe-process-role>-<hostname>`, used as the runtime worker claimant.
3. Bootstrap role only: `alembic upgrade head` (:52) then
   `python -m app.scripts.setup_langgraph_checkpoints` (:74). Each is wrapped
   in `set +e` and gated by `ALLOW_MIGRATION_FAILURE` (:64, :86) — a
   deploy-only escape hatch, default `false` (fails the container).
4. `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers <n>`
   (`entrypoint.sh:15`). Worker count: `APP_WORKERS` env, default `1`
   (`entrypoint.sh:8-13`).
5. Container liveness: `backend/Dockerfile` `HEALTHCHECK` pings
   `http://localhost:8000/api/health` (served by `main.py:482`); `EXPOSE 8000`.

- **FACT** `entrypoint.sh:15` default uvicorn workers = 1. `docker-compose.yml`
  and `.cd.yml` never override `APP_WORKERS`, so **all shipped topologies run a
  single uvicorn worker** unless the operator sets it.
- **OBSERVATION** The CD worker container (role `bootstrap,worker,connector`)
  is *not* `api`, so `entrypoint.sh:11-14` keeps its worker count at the
  `DEFAULT_UVICORN_WORKERS=1` — even the dedicated worker plane is a single
  uvicorn process. Concurrency comes from in-process asyncio daemons +
  Postgres `SKIP LOCKED`, not from process parallelism.

### 1.3 Concurrency coordination across roles

When more than one `worker`-role container is alive (the CD split, or a
manual multi-replica), correctness depends on Postgres row-locking, not a
leader election service:

- **FACT** `backend/app/services/scheduler.py:47` — the schedule tick claims
  due rows with `.with_for_update(skip_locked=True)`, so two workers ticking
  in parallel will not double-fire the same `AgentSchedule`.
- **OBSERVATION** `backend/app/services/trigger_daemon.py:112-114` — the
  trigger tick does a **plain** `select(AgentTrigger).where(is_enabled)` with
  **no** `skip_locked` and no claim lock in the *query*; it then enqueues to
  the distributed execution queue and claims per-trigger further down
  (`trigger_daemon.py:134`, `:161` per-id re-selects). The per-trigger claim
  path is the real lock; the top-of-tick scan is advisory.
- **FACT** `backend/app/services/agent_runtime/worker_service.py:140
  runtime_worker_claimant()` + `:597 running_runtime_worker_context(...)` —
  the durable Runtime Command Worker identifies each process by a
  process-unique claimant (`INSTANCE_ID`), so multiple workers each own their
  own claimed runs without stepping on each other.
- **OBSERVATION** `backend/app/main.py:337-339` — the optional `ss-local`
  SOCKS5 proxy (for Discord API egress) starts in **every** role that reaches
  that line, including pure-`api` containers; it is best-effort and non-fatal
  (`main.py:75-118`).

### 1.4 Ports

| Port | Owner | Source |
|---|---|---|
| `8000` | backend FastAPI (uvicorn) | `entrypoint.sh:15`, `Dockerfile EXPOSE 8000`, `config.py` default |
| `3000` | frontend nginx (in-container) | `frontend/Dockerfile EXPOSE 3000`, `nginx.conf.template:2` |
| `3008` | frontend host port (default) | `docker-compose.yml` `${FRONTEND_PORT:-3008}:3000`, `.cd.yml` same |
| `5432` | Postgres | `docker-compose*.yml` |
| `6379` | Redis | `docker-compose*.yml` |
| `9000` | MinIO/S3 (CD) | `docker-compose.cd.yml` `S3_ENDPOINT_URL` default `http://minio:9000` |
| `18789` | OpenClaw agent gateway (host side) | `config.py:177` `OPENCLAW_GATEWAY_PORT` |
| `1080` | ss-local SOCKS5 (local, per-process) | `main.py:95,102` |

- **FACT** The per-agent OpenClaw container port is computed as
  `18789 + hash(str(agent.id)) % 10000` (`backend/app/services/agent_manager.py:283`),
  mapped host-side to `OPENCLAW_GATEWAY_PORT` (:291). Because it is an
  unseeded **string hash modulo 10000**, two agents *can* collide on the same
  host port (a `18789..28788` range shared by all agents on one host). This
  is a collision risk, not a guaranteed bug — flagged for review.

### 1.5 Sandbox / agent-container isolation primitives

- **FACT** The backend image bakes in the isolation toolchain:
  `libpango`/`cairo` (html→pdf), `gosu`, `bubblewrap` (`bwrap` with
  `chmod u+s`), `chromium` + CJK fonts (`backend/Dockerfile` production stage).
- **FACT** `bwrap` is the mandatory boundary for `execute_code`.
  `backend/app/main.py:37-69 _log_bwrap_startup_status()` is **fail-closed**:
  in a container, missing `bwrap` makes `execute_code` fail unless the operator
  explicitly sets `SANDBOX_ALLOW_UNSAFE_FALLBACK_WHEN_BWRAP_MISSING=true`.
- **FACT** The backend mounts `/var/run/docker.sock`
  (`docker-compose.yml`, `.cd.yml`) so it can spawn **per-agent OpenClaw
  containers** on the shared `CLAWITH_DOCKER_NETWORK`
  (`agent_manager.py:285-292`). `privileged: true` + `SYS_ADMIN` +
  `seccomp=unconfined` on the backend service are what make nested-container +
  bubblewrap execution possible.
- **OBSERVATION** — `bwrap` is setuid (`chmod u+s /usr/bin/bwrap`,
  `Dockerfile`) and the app runs as `clawith` after `gosu`; the setuid bit is
  what lets the unprivileged `clawith` user still call `bwrap`. Removing
  either `privileged`/`SYS_ADMIN` or the setuid bit would break sandbox
  execution — a coupling to keep in mind for any future container-hardening.

---

## 2. Testing infrastructure

### 2.1 Test corpus & tools

- **FACT** Backend: **207** test files, **2146** `def test_*` functions
  (`backend/tests/`). All three E2E-acceptance suites plus the DB-free service
  suites live here.
- **FACT** Frontend: **27** `*.test.mjs` files under `frontend/tests/`, run by
  the bare Node test runner — `package.json` `"test": "node --test tests/*.test.mjs"`.
  There is **no Jest/Vitest**; frontend unit tests are Node built-in `node --test`.
- **FACT** `backend/pyproject.toml:68-70` `[tool.pytest.ini_options]` sets only
  `asyncio_mode = "auto"`. No markers, no `addopts`, no test-coverage config.
- **OBSERVATION** There is **no `conftest.py`** at the `backend/tests/` root
  (verified by `find` returning none). Shared fixtures are defined per-file,
  so the E2E suites each re-implement their own `fake_redis` / storage /
  engine-dispose fixtures (`test_materialization_e2e_acceptance.py:137-150`,
  `test_git_acquisition_e2e_acceptance.py:145,167`).

### 2.2 What is *missing* (verification gaps)

- **OBSERVATION (coverage)** — **No coverage tooling is configured anywhere.**
  `pyproject.toml`, `.coveragerc`, `tox.ini` (absent), and `uv.lock` (absent)
  contain no `coverage`/`pytest-cov`/`mutmut` entry (grep across the repo
  returned nothing). The CI gate is `ruff check` + `arch-guard` + `pytest -q`
  (`.drone.yml`). **Code coverage is not measured or reported.** This is a
  structural gap: "the suite passes" is the only signal; there is no
  regression coverage-metric to guard against silent deletion.
- **OBSERVATION (DB-gated E2E portability)** — The three E2E acceptance
  suites have **inconsistent** behavior when Postgres is unreachable:
  - `test_git_acquisition_e2e_acceptance.py:167-200` — has a
    `_db_available()` autouse fixture that **skips the whole module** when a
    dedicated raw-asyncpg probe fails ("the skip itself is the honest
    evidence boundary").
  - `test_intake_e2e_acceptance.py` and
    `test_materialization_e2e_acceptance.py` — have **no** skip/probe gate
    (grep for `skip`/`probe`/`available` returned nothing). They seed a real
    DB per test (`_seed_tenant_user`) and will **error** (not skip) if
    `DATABASE_URL` points at an unreachable/unschemad scratch DB.
  - **Consequence:** the root `.drone.yml` gate (`uv run pytest tests/ -q`)
    runs the *entire* `backend/tests/` tree in the **uv-managed Python image
    (`uv:python3.12-bookworm-slim`) with no Postgres service** — so the two DB-gated suites will **fail, not
    skip**, on that gate unless a reachable Postgres is on the host. The
    git-acquisition suite degrades gracefully; the other two do not. This is
    an **INFERENCE**: the divergence is visible in code, but whether the CI
    host actually has a shared Postgres reachable at `DATABASE_URL` is UNKNOWN
    (not inspectable read-only).
- **OBSERVATION (dead doc references)** — `AGENTS.md` (root) mandates, by
  section §7, that operators use `docs/testing.md` and the
  `clawith-pre-push-checks` skill. **Both do not exist** on `main`
  (verified: `git cat-file -e main:docs/testing.md` → MISSING; same for
  `docs/model-visible-inputs.md`, `docs/constitution.md`, and
  `.agents/skills/clawith-pre-push-checks/SKILL.md`). Three AGENTS.md
  instructions point at non-existent files — a traceability break in the
  workflow contract. (Root `AGENTS.md:33,124,126`; `backend/alembic/AGENTS.md:4`;
  `backend/app/dao/AGENTS.md:5`.)
- **OBSERVATION (type-check not in CI)** — `pyright` is a dev dependency
  (`backend/pyproject.toml` `[dependency-groups] dev`), and
  `backend/AGENTS.md` lists `uv run --extra dev pyright app` as a command,
  but **neither `.drone.yml` nor `.github/drone.yml` runs pyright** (grep
  `pyright` over both → none). Static type-checking is a local-only,
  unenforced step.
- **UNKNOWN (frontend test runner in CI)** — The CI gate runs
  `npm ci || npm install` + `npx tsc --noEmit` for the frontend
  (`.drone.yml`). It does **not** run `npm run test` (the 27 node-test
  files), so **frontend unit tests are not part of the automated gate.**
  Whether they are exercised anywhere else is UNKNOWN from the repo.

### 2.3 E2E verification patterns that DO work

- **FACT** `test_materialization_e2e_acceptance.py:137-150` and
  `test_git_acquisition_e2e_acceptance.py:145` — `fake_redis` monkeypatches
  `workspace_locking.get_redis` to an `InMemoryWorkspaceRedis` that implements
  exactly the locking surface the app uses, so the **transport** E2E is
  Redis-free while the DB is real. This is the correct split: real Postgres
  (the authoritative store) + faked Redis (a cache/coordination plane).
- **FACT** `test_intake_e2e_acceptance.py:107-119` `_dispose_engine_between_tests`
  — disposes the shared engine after every test because `asyncpg` connections
  are loop-bound and pytest-asyncio gives each test a fresh loop. This is the
  documented, sanctioned reset for the shared-engine arrangement.

---

## 3. Development workflow

### 3.1 Build

- **Backend**: `backend/` is a `uv` project (`pyproject.toml`
  `[build-system]` = setuptools, deps managed by `uv`). `uv sync` is the
  install path (`.drone.yml`). Python floor `requires-python = ">=3.11"`
  (`pyproject.toml:5`); the **Docker image and CI image are Python 3.12**
  (`backend/Dockerfile` `python:3.12-slim`, `.drone.yml`
  `uv:python3.12-bookworm-slim`, `setup.sh` ">= 3.12 required"). The 3.11
  floor is a floor, not the pinned runtime.
- **Frontend**: `node:20-alpine`, `npm ci --registry
  https://registry.npmmirror.com`, `npm run build` = `tsc && vite build`
  (`frontend/Dockerfile`, `package.json`). Production stage is
  **pinned to `nginx:1.31.2-alpine@sha256:...`** with a comment explaining a
  kernel-seccomp pwrite incompatibility with 1.31.3 — an intentional,
  documented pin.
- **OBSERVATION** — `uv.lock` is **gitignored** (`.gitignore:6`) yet
  `backend/AGENTS.md:12,39` asserts "`uv.lock` records the resolved dependency
  graph." The lockfile is **not committed**, so backend dependencies are
  resolved **fresh at build time** in CI (`uv sync`). Build reproducibility
  for the backend therefore depends on PyPI availability, not a pinned graph.
  The doc claim and the VCS state contradict each other.

### 3.2 Lint / type-check

- **Backend**: `ruff check app/ alembic/` is the gate (`.drone.yml`);
  `ruff` config in `pyproject.toml:64-66` (`target-version = "py311"`,
  `line-length = 120`).
- **Architecture guard**: `scripts/arch-guard.sh` is a custom P0-constitution
  linter run **between** ruff and pytest in CI. It enforces:
  - **C1** — `app/api/` must not import graph-execution nodes directly.
  - **C4** — frontend must not `import axios` (use the request wrapper).
  - **C2** (warn) — direct `select(` in api/services should converge to DAO.
  - **C5** (warn) — physical `ForeignKey(` in models.
  - **C6 / frontend line-limit** (warn) — backend files >1000 lines, frontend
    >600 lines.
  Only **C1 and C4 are fatal** (`VIOLATIONS` → exit 1); the rest are warnings.
  This is the operational "architect guardrail" the AGENTS.md constitution is
  compiled down to.
- **Frontend**: `eslint .` (flat config, `frontend/eslint.config.js`) and
  `prettier` are available as `npm run lint`/`format:check`, but the **CI gate
  only runs `tsc --noEmit`** (type-check). **`eslint` is not in the
  automated gate** (`.drone.yml` frontend step = `npm ci` + `npx tsc
  --noEmit`). So lint and type-check are separate and **lint is local-only**.

### 3.3 CI/CD (Drone + GitHub Actions)

Two distinct pipeline files exist and are **not** the same thing:

- **`.drone.yml` (root)** — the *minimal* 2-step gate:
  `backend-lint-and-tests` (uv sync → ruff → arch-guard → `pytest tests/ -q`)
  + `frontend-type-check` (`npm ci || npm install` → `npx tsc --noEmit`).
  **No build, no migration test, no deploy, no triggers block.**
- **`.github/drone.yml`** — the *full* 6-step CI/CD pipeline
  (`build-and-test`): clone → build old+new images →
  `ci_migration_test.sh` (fresh-DB `alembic upgrade head`) →
  `ci_deploy_test.sh` (fresh deploy, asserts `durable Agent Runtime worker
  started`, image revision match, checkpoint schema, uvicorn presence) →
  `ci_upgrade_test.sh` (upgrade from previous stable tag; asserts workspace
  + DB sentinel survive, new worker boots) → CD on tag (export images, scp to
  private server, `docker compose -f docker-compose.cd.yml up -d`, health via
  frontend proxy, Feishu notify). Triggers on `main`/`release`/
  `ci/test-drone*`/PRs/`v*` tags.

- **OBSERVATION (drift)** — The repo carries **both** a minimal root
  `.drone.yml` and the full `.github/drone.yml`. They encode *different* CI
  behaviors: the root file is a fast gate (no DB, no build, no deploy); the
  `.github` file is the real release pipeline (DB migration/deploy/upgrade +
  CD). **Which one the Drone server actually consumes is determined outside the
  repo** (Drone server config points at one path), so this is **UNKNOWN from
  the repo alone.** The `deploy/RELEASE_DEPLOYMENT.md` doc describes the
  `.github/drone.yml` behavior (tag → build → test → CD → Feishu) and explicitly
  says GitHub Actions *only* cuts the release PR + tag while Drone owns
  validation + deploy. The root `.drone.yml` has **no** tag/CD steps and
  **no** `trigger:` block, so if the server pointed at the root file, tag
  deploys would not happen at all. Flagging for the orchestrator to confirm
  the authoritative pipeline file.
- **OBSERVATION** — `ci_deploy_test.sh` and `ci_upgrade_test.sh` both assert
  the config the deploy is *supposed* to honor:
  `assert s.AGENT_RUNTIME_V2_ENABLED is True` **and**
  `s.AGENT_RUNTIME_COMMAND_CONCURRENCY == 10`
  (both export `AGENT_RUNTIME_V2_ENABLED=true` /
  `AGENT_RUNTIME_COMMAND_CONCURRENCY=10` at the top). These two values are
  therefore **contractually pinned** by the deploy test — a code change that
  drops one of them will fail the CD deploy test, not just a unit test.

### 3.4 Migrations (the workflow's load-bearing invariant)

- **FACT** `backend/alembic/AGENTS.md` §0 — the **Single Head Rule**: a new
  migration's `down_revision` must be the current single head or startup fails
  with "Multiple head revisions." Recon confirmed the current single head is
  `f067_intake_rejection_fields` (72 version files). DDL-only rule + idempotent
  guards + `v{M}_{m}_{P}_f{NNN}_...` naming.
- **OBSERVATION** — The entrypoint runs `alembic upgrade head` on every
  `bootstrap`-role boot (`entrypoint.sh:52`), and CD **does not** recreate
  Postgres/Redis/MinIO — only the app containers (`deploy/RELEASE_DEPLOYMENT.md`
  "PostgreSQL, Redis, and MinIO are not recreated"). So migrations are
  applied *in place* on the live shared Postgres on each deploy; a broken
  migration in the new image blocks the worker container's boot (and, by
  §1.1, is not caught by the API health gate).

### 3.5 Git / release workflow

- **FACT** `.github/workflows/release.yml` — manual `workflow_dispatch`
  computes the next semver from the last `v*` tag, drafts notes (optionally via
  GitHub Models), writes `backend/VERSION` + `frontend/VERSION`, opens a
  `release/vX.Y.Z` PR; merging that PR pushes the tag → triggers the Drone CD
  pipeline (§3.3). Concurrency is grouped per ref.
- **OBSERVATION** — `backend/VERSION` and `frontend/VERSION` currently both
  read `1.11.4-fix.1`. The release workflow *rewrites* these on each release;
  the `git log` history shows the version files are the release-traceability
  anchor (the `/api/version` endpoint `main.py:488-521` reads `VERSION` +
  `COMMIT` and falls back to `git rev-parse --short HEAD`).

---

## 4. Implications for Phase 2C (what this analysis means for the next build)

- **New Analysis/Revision/Finding persistence (the recon §8 open item)** must
  run through the **single-migration-head invariant** (append `down_revision`
  to `f067...`) and be owned by the **bootstrap-role** boot path — otherwise
  the CD worker container will not create the tables.
- **Any new E2E acceptance suite** should copy the
  `_db_available()` skip pattern from
  `test_git_acquisition_e2e_acceptance.py:167` so it degrades to *skip* (not
  *error*) when Postgres is unreachable, matching the only gate that currently
  behaves portably.
- **The two "worker owns schema" + "health gate misses worker boot" facts
  (§1.1)** mean a new revision-bound analysis table that fails to migrate will
  silently leave the analysis API unavailable while the fleet still reports
  healthy — a verification blind spot to design a guard for (a schema-readiness
  probe, or extending the CD health gate to the worker container).
- **Provider/runtime independence** is already structurally in place:
  `Dockerfile` has a pluggable `CLAWITH_PIP_INDEX_URL` build-arg; the model
  provider layer is abstracted (recon §). No Phase 2C work here is required,
  but new analysis code must not re-introduce a hard-coded provider.

---

## 5. Evidence command log (what was actually run)

All commands run in `I:/project/AI Company OS/.worktrees/t_93876cee`
(branch `wt/t_93876cee`, clean worktree, HEAD `a9e83a8a`):

1. `git status -sb`; `git branch --show-current` → clean on `wt/t_93876cee`.
2. `git show wt/t_4e4599d8:docs/PHASE_2C_RECON_BASELINE.md` → read parent
   recon baseline (read-only, sibling worktree object store).
3. `cat docker-compose.yml docker-compose.ci.yml docker-compose.cd.yml` →
   §1.1–§1.3 topology + roles.
4. `cat backend/entrypoint.sh backend/Dockerfile` → §1.2 startup sequence,
   gosu, healthcheck, `EXPOSE 8000`.
5. `grep` over `backend/app/main.py` → `_process_roles`, `_role_enabled`,
   daemon gates, `/api/health`, ss-local 1080.
6. `grep` over `backend/app/config.py` → `PROCESS_ROLE`, `AGENT_RUNTIME_V2_*`,
   `GIT_ACQUISITION_MAX_SECONDS`, `OPENCLAW_GATEWAY_PORT`.
7. `grep` `scheduler.py` / `trigger_daemon.py` / `worker_service.py` →
   `skip_locked`, plain trigger select, claimant context.
8. `cat frontend/Dockerfile frontend/nginx.conf.template` → pin, proxy,
   EXPOSE 3000.
9. `find backend/tests -name '*.py' | wc -l` → 207; `grep -c 'def test_'`
   → 2146; `ls frontend/tests/*.test.mjs | wc -l` → 27.
10. `cat .drone.yml .github/drone.yml .github/workflows/release.yml
    .github/scripts/ci_*.sh scripts/arch-guard.sh` → §2.2, §3.2–§3.5.
11. `grep` for `coverage|uv.lock|conftest` across repo → §2.2 coverage + lock
    gaps.
12. `grep -n 'pytest.skip|_db_available|probe'` over the 3 E2E files → §2.2
    DB-gating divergence.
13. `git cat-file -e main:docs/testing.md` (and 3 more) → §2.2 dead refs.
14. `cat backend/VERSION frontend/VERSION`; `grep 'react' frontend/package.json`
    → §3.5 version traceability.

No build, lint, or test command was **executed** (read-only card). The
`pytest`/`ruff`/`arch-guard` invocations above are recorded as *gate
definitions* read from `.drone.yml`, not as results produced in this run.

---

## 6. Headline findings (5–8, tagged) — for the completion handoff

1. **FACT** — Single image, role-multiplexed via `PROCESS_ROLE`
   (`entrypoint.sh:49`, `main.py:30`); CD splits `api` (no schema) from
   `bootstrap,worker,connector` (owns `alembic upgrade head` +
   `setup_langgraph_checkpoints`). `docker-compose.cd.yml` `backend-api` vs
   `backend-worker`.
2. **OBSERVATION** — The CD post-deploy health gate pings the API through the
   frontend proxy; a `backend-worker` boot/migration failure is **not** caught
   by that gate (`deploy/RELEASE_DEPLOYMENT.md`, `docker-compose.cd.yml`).
   Verification blind spot for any new revision-bound table.
3. **OBSERVATION** — **No coverage tooling** anywhere (no
   `coverage`/`pytest-cov` in `pyproject.toml`/`.coveragerc`/`tox.ini`/absent
   `uv.lock`). CI gate = `ruff` + `arch-guard` + `pytest -q` only
   (`.drone.yml`).
4. **OBSERVATION** — E2E DB-gating is inconsistent:
   `test_git_acquisition_e2e_acceptance.py:167` skips the module on an
   unreachable Postgres; `test_intake_e2e_acceptance.py` and
   `test_materialization_e2e_acceptance.py` have **no** skip gate and will
   *error* if `DATABASE_URL` is unreachable. New Phase 2C E2E should copy the
   skip pattern.
5. **OBSERVATION** — `uv.lock` is gitignored (`.gitignore:6`) yet
   `backend/AGENTS.md:12,39` claims it "records the resolved dependency
   graph" — the two contradict; backend deps resolve fresh at CI build.
6. **OBSERVATION** — Four AGENTS.md-mandated references point at files that do
   not exist on `main`: `docs/testing.md`, `docs/model-visible-inputs.md`,
   `docs/constitution.md`, `.agents/skills/clawith-pre-push-checks/SKILL.md`
   (root `AGENTS.md:33,124,126`; `alembic/AGENTS.md:4`; `dao/AGENTS.md:5`).
7. **OBSERVATION** — Two divergent Drone pipeline files coexist: minimal
   `.drone.yml` (no build/CD/triggers) vs. full `.github/drone.yml` (build +
   migration/deploy/upgrade + CD + Feishu). Which one the server consumes is
   **UNKNOWN** from the repo — needs orchestrator confirmation.
8. **INFERENCE** — Concurrency across worker roles is coordinated by Postgres
   `SKIP LOCKED` (`scheduler.py:47`) + the per-run claimant
   (`worker_service.py:140`), not a leader election; the trigger scan
   (`trigger_daemon.py:112-114`) is advisory until the per-trigger claim. A
   new analysis-execution loop should follow the same `skip_locked` + claimant
   pattern rather than inventing a scheduler.

*UNKNOWN* items (declared, not resolved, not inspectable read-only):
(a) whether the CI host has a shared Postgres reachable at `DATABASE_URL`
(determines whether the two un-gated E2E suites pass or fail on the gate);
(b) which of `.drone.yml` / `.github/drone.yml` the Drone server executes;
(c) whether the frontend's 27 node tests are exercised by any automated gate
outside the repo.

---

## 7. Risks & follow-ups (for orchestrator / next cards)

- **R1 (verification blind spot)** — worker-container boot/migration failure
  is invisible to the API health gate. Recommend adding a worker-side
  schema-readiness probe or extending the CD health check to
  `backend-worker` (`docker-compose.cd.yml`).
- **R2 (drift)** — minimal vs full `.drone.yml`. Confirm the authoritative
  pipeline; archive or remove the stale one so CI/CD is single-sourced.
- **R3 (dead references)** — the four missing AGENTS.md targets break the
  §"code / Agent Notes / commit stay aligned" contract. Restore the files or
  remove the references in one change.
- **R4 (port collision)** — `agent_manager.py:283` unseeded `hash(agent.id)%10000`
  OpenClaw port can collide across agents on one host; seed the hash or use
  a port-allocator.
- **R5 (coverage gap)** — add a coverage measurement step (e.g. `pytest-cov`)
  so "suite passes" is backed by a regression metric; currently there is none.
- **R6 (reproducibility)** — `uv.lock` is gitignored; decide whether to commit
  it (recommended for a pinned graph) or correct the AGENTS.md claim.

*No source, schema, migration, config, or CI file was modified by this card.*
