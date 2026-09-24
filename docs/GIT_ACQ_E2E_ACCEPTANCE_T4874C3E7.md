# E2E Acceptance Report — Git Source Acquisition (Phase 2B-4, t_4874c3e7)

- Date: 2026-09-24 (UTC+09:00)
- Branch/worktree: `wt/t_4874c3e7` @ `I:/project/AI Company OS/.worktrees/t_4874c3e7`
- Driving design: `docs/GIT_ACQ_DESIGN_V1.md` on `wt/t_82ac3524` @ `4008e194`
  (parent card t_82ac3524, docs-only audit — the single adjudicated text the
  builder consumed; absolute path read at resume per the handoff comment).
- Baseline: main `fe77d1d6` (no git-acquisition surface: the git source types
  were the Phase 2B-3 fail-closed `SOURCE_NOT_READY`/`SOURCE_NOT_SUPPORTED`
  markers).
- No schema change (design §C.4 ruling honored): the full migration chain
  still terminates at the single head `f067_intake_rejection`; acquisition
  metadata rides `repositories.locator` JSON, credentials ride the
  pre-existing `agent_credentials` table (new `platform` data values, no DDL).

## 1. Scope

Closed-loop acceptance of "acquiring a registered git source" and its handoff
into Materialization, through three complementary layers:

1. **DB-free service suite** (`tests/test_git_acquisition_service.py`, 85
   tests + 1 skip): the security gates hold BEFORE any process (ref
   shape-gate, https-only URL gate + SSRF host rule, local-path gate), the
   credential resolve keeps the token process-only, the bounded git run with
   two-stage reap, the acquisition-area post-checks (submodules / reserved
   names / symlinks), the agent-scoped byte-bounded tar publish, the closed
   ACQ_* code set + retry rule, and — against a **real git binary** —
   local-repo acquisition: default-branch resolution (never hardcoded),
   branch refs, a 40-hex commit pin, a missing ref (`ACQ_REF_NOT_FOUND`,
   "clone succeeded is NOT success"), and the submodule refusal.
2. **Real-DB API acceptance** (`tests/test_git_acquisition_e2e_acceptance.py`,
   6 tests, skip-guarded when Postgres is unreachable): the NEW
   `POST/GET /api/projects/{project_id}/repositories/{repo_id}/acquire/{agent_id}`
   router through the **real FastAPI app over ASGI, real Postgres scratch DB
   (alembic 001→f067 schema), real local storage backend** — the
   acquire→status→materialize round-trip (the artifact is the handoff: a
   later materialize reads the ONE tar, no re-clone), the security-rejection
   409 with 0 I/O, tenant-invisibility 404s, and the card's hard rule:
   acquisition publishes NO downstream chat-session / task / schedule rows
   and exactly ONE `git_acquisition` audit row.
3. **Remote posture** (in the DB-free suite, skip-guarded on egress): a
   public GitLab repository through the full pipeline, and GitHub's
   anonymous posture recorded for a host with egress (this host: GitHub
   unreachable → skipped; GitLab reachable → exercised).

Everything security-relevant is REUSED from the design's D-table (never
re-invented): `intake_security.git_url_detail`/`is_unsafe_host`/
`normalize_rel` (the new shared guards, tested directly in
`tests/test_intake_security.py` §8), `check_host_path`, the
`core.security` AES credential decrypt at the use boundary, the storage
facade, and the materialization byte budgets.

## 2. Evidence commands + results

All from `backend/`, run with the project venv:

| # | Command | Result |
|---|---------|--------|
| 1 | `pytest tests/test_git_acquisition_service.py` | 85 passed, 1 skipped (GitHub egress) |
| 2 | `pytest tests/test_intake_security.py` | 43 passed (incl. 7 new §8 guard tests) |
| 3 | `pytest tests/test_project_intake_service.py tests/test_project_materialization_service.py tests/test_materialization_edge_cases.py` | 133 passed (regression: the flip + shared normalizer leave all 2B-1/2B-3 behavior green) |
| 4 | `DATABASE_URL=postgresql+asyncpg://clawith:clawith@127.0.0.1:5432/clawith_t4874c3e7_e2e pytest tests/test_materialization_e2e_acceptance.py tests/test_intake_e2e_acceptance.py tests/test_git_acquisition_e2e_acceptance.py` | 39 passed (33 pre-existing + 6 new git-acquire API tests) |
| 5 | `DATABASE_URL=…:5433/none pytest tests/test_git_acquisition_e2e_acceptance.py` | 6 skipped (the DB-unreachable guard fires on every test, not just the first) |
| 6 | `pyright` on the 7 touched app files | 0 errors |
| 7 | `ruff check` on the 9 touched/added files | only the repo's accepted baseline families (B008 = the API layer's `Depends` pattern; BLE001 on the documented narrow `except Exception` sites; the pre-existing `config.py` I001 import-order note was fixed) |

Test DB: `clawith_t4874c3e7_e2e` on `127.0.0.1:5432` (scratch DB, `postgres`
superuser-created, `clawith` granted DML; schema via the REAL alembic chain
`001 → f067` — `uv run alembic upgrade head` ran clean to the single head,
confirming the no-migration ruling).

### 2.1 The real-DB API round-trip (test 1 of the new acceptance suite)

Seed: an INITIALIZED project with one UNVERIFIED `local_git` row pointing at
a real `git init` repository (2 files, 1 commit) on the host filesystem.

1. `POST …/acquire/{agent_id}` → **201**, `state=acquired`, `code=ACQ_OK`,
   `resolved_rev` set (the repository's own default branch — no ref was
   declared, so `git ls-remote --symref HEAD`-equivalent behavior via the
   local default), `artifact_key = {agent_id}/.git-acq/{repo_id}/source.tar`.
2. The tar exists in the **real local storage backend**; its members are
   exactly the working tree (`a.txt`, `subdir/b.txt`); NO `.git/*` member.
3. The repository row re-read from Postgres: `verified=True`,
   `pending_verifier=False`, `locator.acq_result=ACQ_OK`,
   `locator.resolved_rev == the returned revision`; the stringified locator
   contains no "token".
4. `GET …/acquire/{agent_id}` → **200**, the SAME closed state
   (`acquired`/`ACQ_OK`, the same artifact key).
5. `POST …/materialize/{agent_id}` → **201 SUCCESS**; the original file
   bytes land under `{agent_id}/projects/{project_id}/git-src/` — the
   materialization git-reader consumed the ONE published artifact (no
   re-clone), re-normalizing every member path through the shared
   `intake_security.normalize_rel` + reserved-name rule.

### 2.2 Fail-closed postures exercised at the transport

- `POST acquire` on a `github` source with a cloud-metadata URL
  (`https://169.254.169.254/…`) → **409 `ACQ_SECURITY_REJECTED`**,
  `retryable=False`, the code recorded in `locator.acq_result`, the repo
  stays unverified, and the agent's `.git-acq` staging subtree is **empty**
  (0 I/O — the rejection happened before any process).
- A foreign-tenant target agent → **404** (tenant-invisibility via the
  tenant-scoped DAO, never a 403 disclosure — the same rule the
  materialization E2E documents); unknown repo / project ids → **404**.
- After a successful acquire: zero new `chat_sessions` / `tasks` /
  `agent_schedules` rows for the agent, exactly ONE `audit_logs` row with
  `action=git_acquisition`, `details.result=ACQ_OK` (card §23: the
  acquisition NEVER starts a downstream Agent / Run / prompt).

### 2.3 Remote E2E posture on this host (recorded; skip-guarded elsewhere)

- GitHub: `git ls-remote` anonymous probe → **unreachable** from this host
  (no egress to github.com); the test skips and this report is the record.
- GitLab: probe → **auth** (network reached, anonymous access refused); the
  test then ran the full pipeline with NO stored credential and asserted
  the closed outcome `ACQ_AUTH_FAILED`, `retryable=False`, empty staging
  subtree — the fail-closed, never-retried posture of a private source
  without a token (card §21).

## 3. Linter / type posture of the touched files

- `app/services/git_acquisition_service.py` (new, ~1100 lines): ruff-clean;
  pyright-clean. The Windows-portable two-stage reap reads
  `os.killpg`/`os.getpgid`/`signal.SIGKILL` via `getattr` (design §E:
  group re-kill degrades to `proc.kill()` where the primitives are absent),
  which is what keeps the module type-checking on this Windows host while
  preserving the POSIX group reap on Linux.
- `app/services/intake_security.py`: the three new shared guards
  (`is_unsafe_host`, `git_url_detail`, `normalize_rel`) are stdlib-only
  (`urlparse` + `ipaddress` + `socket`), keep the module import-safe, and
  are the ONE rule set — the materialization `_normalize_rel` now DELEGATES
  to `intake_security.normalize_rel` (a second, contradicting rule set was
  deleted in the same change, design §18).
- `app/api/projects.py`: two new routes + the `_find_repo` helper under the
  existing `_load_authorized_project` gate; transport stays a pure adapter
  (all policy in the service). B008 findings are the whole API layer's
  accepted `Depends` pattern.
- The new acceptance test file's only 2 remaining pyright notes are the
  generator-fixture return-type findings **identical to the carried baseline
  of `test_materialization_e2e_acceptance.py` / `test_intake_e2e_acceptance.py`**
  (same accepted class; the documented check is `pyright app`, under which
  all 7 touched app files report 0 errors).

## 4. Product issues found and fixed during acceptance

1. **`_validate_git` intake flip + materialization gate** (the design §C.3
   "ONE-LINE gate" implemented as a real guard): a git repo validates
   `ok=True` at intake and passes the materialization pre-I/O gate ONLY
   when `locator.acq_artifact` is a non-empty string AND `verified=True`
   AND `pending_verifier=False`. Absence of any mark = the pre-acquisition
   state = the exact Phase 2B-3 fail-closed behavior (preserved by
   construction; proven by battery #3: every 2B-3 regression suite green).
2. **Corrupt / truncated artifact is a transient hold, not a 500**: the
   materialization git-reader maps a `tarfile.TarError` to
   `SOURCE_UNREACHABLE` (local-folder parity) while a traversal /
   reserved-name member still raises `SECURITY_REJECTED` with 0 writes
   (the second-depth guard, spec §2.2 — intake-time post-checks are not a
   trust credential).
3. **Secret-free stderr + audit**: a failed clone's stderr names the URL;
   the service caps it to a bounded tail and redacts `user:pass@` userinfo
   before it may be logged/returned; the audit detail carries only class
   fields (result/provider/ref/rev/key), never a token.

## 5. Verdict

The "acquiring a git source" stage is **functional and secure end to end**:
SSRF / scheme / userinfo / ref-injection / path vectors are rejected with
0 I/O before any process; the token lives only in the git child's
environment (bearer-header env mechanism, never argv/URL/locator/log/audit);
the publish is one agent-scoped, byte-bounded working-tree tar; the
Materialization handoff reads exactly that object; transient failures stay
bounded-retry (2 codes only, MAX_RETRIES=3 shared budget), permanent
failures (auth / security / ref / submodules / size) never retry; and the
acquisition stage starts no downstream Agent / Run / prompt. Final verdict:
**PASS.**

## 6. Open items (out of this card's scope, recorded)

- **GitHub E2E**: this host has no egress to github.com; the skip-guarded
  test + this report §2.3 is the evidence boundary. A host with egress
  re-runs `pytest tests/test_git_acquisition_service.py -k github` and the
  acceptance doc's posture assertion becomes live.
- Containerized deployment: git + network egress remains the documented ops
  contract (design §E, the intake UNKNOW-1 posture).
- rlimit hardening of the git child (Linux-only) and private-repo token
  rotation — recorded, not built (design §E).
- `clawith_t4874c3e7_e2e` scratch DB stays on the local Postgres
  (reusable by the reviewer for battery #4); the `clawith` role created for
  it is cluster-wide (harmless data, no DDL grant changes to existing DBs).

## 7. Scope check (deliberately NOT implemented in this card)

- No Agent / Run / prompt / task / schedule creation from acquisition
  (the hard rule; proven by the §2.2 row-count test).
- No re-clone in materialization (the artifact IS the handoff, card §11).
- No new table / migration; no second path-normalizer; no `shell=True`,
  no string-built git argv, no `--recurse-submodules` (submodule
  declarations fail closed with `SUBMODULES_UNSUPPORTED`).
- The intake 6-code set, its retry budget, and the REJECTED terminal
  semantics are untouched — the flip is the one gate inside the existing
  dispatch (design §C.3 / card §19).

---
End of report. Absolute path: `I:\project\AI Company OS\.worktrees\t_4874c3e7\docs\GIT_ACQ_E2E_ACCEPTANCE_T4874C3E7.md`
