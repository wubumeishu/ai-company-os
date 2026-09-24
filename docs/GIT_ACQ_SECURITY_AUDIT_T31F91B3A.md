# GIT_ACQ_SECURITY_AUDIT_T31F91B3A.md — Independent Security Audit, Phase 2B-4 Git Source Acquisition

Reviewer: aco-reviewer (task t_31f91b3a) · Date: 2026-09-24
Subject: `wt/t_4874c3e7` @ `6912f53a` "feat(project): add git source acquisition" (diff `fe77d1d6..6912f53a`, 11 files, +3546/-25)
Upstream handoff: t_4874c3e7 (builder) · Design: `docs/GIT_ACQ_DESIGN_V1.md` (t_82ac3524)

## Verdict: REQUEST_CHANGES (2 findings: 1 High, 1 Medium; all security invariants otherwise verified PASS)

The security boundary the task asks about — SSRF/scheme rejection, shell
injection, path traversal, credential containment, fail-closed gates — is
implemented correctly and was re-verified by execution (below). Two defects
remain: a client-reachable 500 in the GET status path, and a dead
default-branch-resolution path with two independent bugs. Neither defeats a
security invariant; both violate the task's own requirements ("all
subprocess executions" are correct, "fail-closed behavior for unsupported
sources remains intact", no 500s on client-reachable inputs) and the card's
acceptance criteria §6/§14 ("use the remote default branch", "do not hardcode
main").

---

## 1. Security-invariant verification (all re-run by the reviewer, not copied from the handoff)

Evidence: executed on `wt/t_4874c3e7` with the project venv; counts match the
acceptance doc `docs/GIT_ACQ_E2E_ACCEPTANCE_T4874C3E7.md` §2.

| Check | Evidence | Result |
|---|---|---|
| SSRF vectors rejected before any process | Reviewer probe over 21 vectors: file/ftp/ssh/git/http schemes, userinfo, 127.0.0.1, localhost, [::1], 0.0.0.0, RFC1918 (10./172.16./192.168.), cloud-metadata literal, 127.0.0.1:port, v4-mapped-v6 [::ffff:127.0.0.1], v6-link-local [fe80::1] → all REJECT with bounded detail; public literals/names accept; unresolvable host → fail-closed reject. `git_url_detail`/`is_unsafe_host` (intake_security.py:341-382) | PASS |
| Unsafe schemes rejected | https-only frozenset `_GIT_SAFE_SCHEMES={https}` enforced first; `git://`, `http://` included in probe → REJECT | PASS |
| Subprocess: argument arrays, no shell | Only `asyncio.create_subprocess_exec(*["git", *argv])` in the service (git_acquisition_service.py:809); no `shell=True`, no `os.system`, no string concat anywhere in the 7 touched files; `grep`-verified | PASS |
| Ref shape gate (injection vectors) | `_REF_RE` (A-Za-z0-9._/−, ≤128) + no-leading-dash + no-bare-integer; SHA gate 7-40 hex; everything else → ACQ_SECURITY_REJECTED pre-spawn. Test `test_e2e_ref_dashes_rejected_before_spawn` proves `_no_spawn` (git child never sees it) | PASS |
| Local path boundary | `check_host_path(source_type="local_folder")` → absolute-host-path shape + traversal/NUL/sensitive-root rejection (SECURITY_REJECTED); plain dir without `.git` → SOURCE_INVALID (local_git vs local_folder boundary, card §9); `os.path.abspath` resolution | PASS |
| Member-path traversal (post-checks + git reader) | One shared rule `intake_security.normalize_rel` (no `..` pop, NUL reject, backslash normalize); used by `_post_check_tree` AND materialization `_plan_git` re-normalization (second-depth guard) + RESERVED_STORAGE_NAMES first-segment check | PASS |
| Symlinks | `os.walk` prune + `is_symlink()` skip in post-checks and publish; materialization reader skips `issym()`/`islnk()` members; tar carries only the working tree (.git pruned → no hooks/objects/executable metadata) | PASS |
| Submodules | `.gitmodules` → SUBMODULES_UNSUPPORTED (fail-closed, card §16); `--no-recurse-submodules` on every clone | PASS |
| Credential containment | Token decrypts only in-process (`_resolve_credential`), injected via `GIT_CONFIG_*` env into the git child only (git_acquisition_service.py:748-780); `scan_locator_for_credentials` re-run at record-success; sanitizer redacts `user:pass@` from captured stderr (bounded 4000-byte tail); audit rows are detail-class only. Verified no model field / log line carries the value; `agent_credentials.cookies_json` is the pre-existing encrypted store (no schema change) | PASS |
| Fail-closed unsupported sources | Intake `_validate_git` (project_intake_service.py:725): git row OK only when `acq_artifact` present AND `verified` AND not `pending_verifier`, else SOURCE_NOT_SUPPORTED permanent. Materialization gate (`_git_artifact_verified`): missing/unverified artifact → SOURCE_NOT_READY (Phase 2B-3 behavior preserved by construction) | PASS |
| Tenant isolation | `_entry_gates` before any I/O: `verify_tenant_scope` re-asserted + M9 fourth gate (agent tenant == project tenant, None → refuse); transport maps `AcquisitionSecurity`/`TenantScopeViolation` → 403; repo lookup via the tenant-scoped project load (cross-tenant → 404, verified by E2E test) | PASS |
| Retry semantics | Closed ACQ set; only ACQ_SOURCE_UNREACHABLE / ACQ_TIMEOUT retryable, bounded by `repositories.retry_count` < MAX_RETRIES; AUTH_FAILED/SECURITY_REJECTED never retried (tests §5 of the suite) | PASS |
| Timeout bound | `GIT_ACQUISITION_MAX_SECONDS=300` config wall shared by ALL git calls in one acquire; two-stage group reap (SIGTERM→grace→SIGKILL, faithful to the sandbox recipe, Windows-degraded via getattr); zero-budget pre-deadline → no spawn | PASS |
| Size bound | Per-file ≤50 MiB, total ≤500 MiB (shared materialization constants) → ACQ_SIZE_LIMIT permanent | PASS |
| No downstream Agent execution | Handler returns the outcome and stops; session writes are exactly `repo` + one `AuditLog` row (asserted by test: `len(db.added) == 2`); no Run/prompt path anywhere in the diff | PASS |
| No schema change | Diff touches no alembic files; locator JSON carries all acq metadata; handoff claims single head f067 (not re-run — alembic chain untouched by this diff, no new migration files) | PASS (by inspection) |
| Regression batteries | Re-run by reviewer: `test_git_acquisition_service.py` + `test_intake_security.py` → **128 passed**; intake+materialization+edge-case suites → **133 passed**; real-DB E2E (scratch Postgres) → **39 passed**; pyright on the 7 touched app files → **0 errors**; ruff on the new files → clean (14 B008 in `api/projects.py` = pre-existing repo-wide `Depends` pattern, 10 of them on pre-existing routes — not new findings) | PASS |

---

## 2. Findings

### [F1] High — GET /acquire status can 500 on a user-controlled, out-of-set `acq_result`

**Where:** `git_acquisition_service.py:350` (`status()`) via
`acq_code_is_retryable` (schemas/project_intake.py:265-270) via the GET route
`api/projects.py:385-400` (no guard on this path — only the POST route has
the `AcquisitionSecurity` catch).

**What:** The persisted locator JSON is INTAKE USER INPUT for git sources
(`SourceSpec.locator` is a free-form dict for github/gitlab/local_git — see
schemas/project_intake.py:31 "git types: free-form"). A caller can register
a git repo with `locator = {"url": ..., "acq_result": "TOTALLY_BOGUS"}`
(the credential scan at create time rejects only credential-shaped values;
arbitrary `acq_*` keys pass). The GET status route then calls
`status()` → `acq_code_is_retryable("TOTALLY_BOGUS")` → `ValueError` →
`unhandled_exception_handler` → **500**, on a client-reachable route that the
task explicitly requires to never surface a 500
(`acquire()` docstring, git_acquisition_service.py:253-259: "Both gate steps
run on CLIENT-REACHABLE inputs ... so they must never surface as a 500").

**Reproduction (executed by the reviewer):**
```
POST create project, source_type=github,
     locator={"url": "https://github.com/o/r", "acq_result": "TOTALLY_BOGUS"}
GET  /projects/{id}/repositories/{rid}/acquire/{agent}
→ 500 (ValueError: 'TOTALLY_BOGUS' is not in the closed acquisition code set)
```
Reviewer's direct call of `GitAcquisitionService.status()` with
`repo.locator={"acq_result": "TOTALLY_BOGUS"}` raised the ValueError —
confirmed, not inferred.

**Why it matters:** (a) client-reachable DoS/500 on an authenticated route
the design promises is 409/200-only; (b) it is a closed-set contract
violation: the closed-set guard exists to make unknown codes "a programming
error, not a guess" — but this value is not a programming error, it is
trusted user data crossing the status boundary; (c) `status()` also mishaps
`acq_result: ""` (empty string): `code or None` makes it None but
`artifact_key` present + not verified → `failed` with `retryable=False`
while the stored code is an empty string that the closed-set semantics do not
define — worth folding into the same fix.

**Required fix (minimum):** in `status()`, validate the stored code against
`ACQ_RESULT_CODES` and map an out-of-set/empty value to a safe read
(treat as `pending`, retryable, `code=None`, or return the stored value
verbatim without the retryable computation — the reviewer's call: pending is
least surprising, since the row is not verified). Add one regression test:
GET status with a user-registered out-of-set `acq_result` returns 200/409,
never 500.

### [F2] Medium — `_default_branch` is dead: two independent bugs; the no-ref remote path silently falls back

**Where:** `git_acquisition_service.py:735-746` (`_default_branch`) and its
call site at :681-692.

**What — bug 1 (argument order):** the call is
`["ls-remote", "--symref", "HEAD", url]` (line 742). Git's positional
grammar is `git ls-remote [--symref] <repository> [<refs>...]` — so git
parses repository=`HEAD`, ref=`<url>`, and the command ALWAYS fails:
`fatal: 'HEAD' does not appear to be a git repository` (exit 128).
**Verified by execution against a real git binary:**
```
$ git ls-remote --symref HEAD <path>     → exit 128 (repo=HEAD is not a repo)
$ git ls-remote --symref <path> HEAD     → exit 0, emits the symref line
```
Because the call uses `allow_failure=True`, the 128 is swallowed and
`_default_branch` always returns `None` → the no-ref remote path clones the
remote's HEAD (the clone does resolve the remote default via its own HEAD
symref) and, `default` being None, skips the explicit checkout. Net effect
today: behavior degrades to "clone then take whatever HEAD points at", which
still works — so the defect is latent, not a live outage.

**What — bug 2 (line parse, independent of bug 1):** even with the argument
order fixed, the parse `line.split("refs/heads/", 1)[-1].strip()` (line 745)
is wrong for the real output. The real ref line is
`ref: refs/heads/acqmain\tHEAD`; the expression returns
`'acqmain\tHEAD'` (tab + literal "HEAD" embedded, `strip()` cannot remove a
mid-string tab) → not a valid ref name → `git checkout 'acqmain\tHEAD'` →
`fatal: invalid refname` → `_GitFailure` → misclassified failure for a
perfectly valid public repo. **Verified by execution** (real git output,
service's exact expression): parsed result `'acqmain\tHEAD'`,
`fullmatch(_REF_RE)` = False.

**Why it matters:** (a) card §6/§14: "default ref — use the remote's OWN
default branch, do not assume main" is the documented behavior of the no-ref
path; today it is NOT exercised (the ls-remote read is dead code that always
"fails"), so the implementation's own claim in the E2E doc
("resolved via ls-remote, never hardcoded") is not what actually runs;
(b) the test suite masks both bugs: the local_git no-ref E2E exercises the
local branch (which never calls `_default_branch`), and the only remote
no-ref E2E (`test_e2e_github_public_repo_default_branch`, :1049) is
skip-guarded and — this host has no anonymous git egress, so it was
**skipped, not passed**; the skip guard itself (line 1038) uses the CORRECT
argument order `["ls-remote", "--symref", url, "HEAD"]`, which is direct
in-repo evidence that the service's order is the inversion; (c) fixing only
bug 1 is NOT sufficient (bug 2 then fires); fixing only bug 2 changes
nothing (bug 1 makes the command fail first).

**Required fix (minimum):** reorder to `["ls-remote", "--symref", url, "HEAD"]`;
parse the ref name from the ref line properly (e.g.
`line[len("ref: "):].split("\t", 1)[0].rsplit("/", 1)[-1]` — the
tab-delimited shape is the documented `--symref` format), and add a
DB-free test that drives `_default_branch` against a real local repo path
(file URL is fine in the test context since the gate is not exercised by a
direct method call — mirror the acceptance test's technique) asserting the
returned string equals the repo's real default branch name.

### [F3] Low — CGNAT (100.64.0.0/10) IP literals pass the host gate

`is_unsafe_host` classifies `100.64.0.1` as safe (Python's
`ipaddress` does not flag the shared-CGNAT range as private/link-local).
CGNAT is operator-space, not a classic SSRF vector (the metadata endpoint
169.254.169.254 IS rejected — verified), and the design's stated threat
model (cloud-internal + RFC1918 + loopback) does not include it. Recorded
for the threat-model file; no change required for this card.

---

## 3. Bounded observations (no action required on this card)

- **Transient re-acquire of an already-acquired repo:** a second acquire on
  a `verified=True` repo that fails transiently runs `_cleanup`'s
  `delete_tree({agent}/.git-acq/{repo})` — deleting the previously-published
  good artifact — while `_record_failure` stamps `verified=False`. The row
  then correctly fails closed downstream (materialization SOURCE_NOT_READY)
  and the client sees the 409 pending; the previous artifact is gone but the
  contract still holds (acquire-then-materialize is the documented flow, and
  the retryer simply acquires again). Intentional, documented at
  git_acquisition_service.py:1037-1044 ("delete on EVERY exit"); keep as-is,
  worth a one-liner in the E2E doc's §R limitations if the team wants it.
- **`GIT_SSL_NO_VERIFY` is stripped** from the child env (line 770) — the
  correct hard direction; nothing in the env inherits a permissive TLS mode.
- **Intake `_validate_git` does not re-verify the artifact's on-disk
  existence** (it trusts `acq_artifact` + `verified` marks) — acceptable:
  the marks are only written by the acquisition service in the same
  transaction as the publish, and the materialization reader re-checks
  storage `exists()` on its own path (second-depth guard). Boundary is
  consistent.

## 4. Out-of-scope deltas in the diff (verified, no finding)

- `intake_security.py:_is_absolute_host_path` regex reformatting
  (`r"^[A-Za-z]:[\\\/]"` → `r"^[A-Za-z]:[\\\\/]"`): reviewer probe over
  `C:\x`, `C:/x`, `c:\users`, `relative\x`, `/x` → identical results;
  style-only, behavior-neutral.
- `config.py` import reordering (I001 family); `GIT_ACQUISITION_MAX_SECONDS`
  addition; both clean.
- Materialization `_normalize_rel` de-duplicated onto the shared rule — the
  delegation matches the shared implementation line-for-line.

## 5. Re-verification commands (run by the reviewer, 2026-09-24)

```
cd backend && uv run --extra dev pytest tests/test_git_acquisition_service.py tests/test_intake_security.py -q
  → 128 passed
uv run --extra dev pytest tests/test_project_intake_service.py tests/test_project_materialization_service.py tests/test_materialization_edge_cases.py -q
  → 133 passed
DATABASE_URL=…clawith_t4874c3e7_e2e uv run --extra dev pytest tests/test_materialization_e2e_acceptance.py tests/test_intake_e2e_acceptance.py tests/test_git_acquisition_e2e_acceptance.py -q
  → 39 passed
uv run --extra dev pyright <7 touched app files> → 0 errors
uv run --extra dev ruff check <4 new/modified app files> → only repo-baseline B008 (Depends) families
real git binary: ls-remote --symref ordering matrix + the _default_branch parse expression → defects F2.1/F2.2 reproduced
status() probe with out-of-set acq_result → ValueError reproduced (F1)
```

## 6. Verdict

REQUEST_CHANGES — implement F1 (required; client-reachable 500 violates the
task's explicit requirement) and F2 (required; the card's default-branch
behavior is dead code). F3 + §3: accepted as-is. All security invariants in
§1 verified PASS by the reviewer's own execution; no new security
vulnerability found in the boundary itself.
