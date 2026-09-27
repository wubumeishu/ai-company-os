# Phase 2F — Deterministic Execution Probes A–F (t_e3a745b1)

Baseline: Phase 2F root `t_4910b73e`, on top of the Phase 2E-converged main
(§28). Upstream preflight handoffs:
- `t_2a4a4dcc` — Task→Run→Result→Settlement execution path **INTACT** (doc-only audit).
- `t_31ac823d` — workspace isolation **PASS**; real-LLM **BLOCKED on credential**;
  deterministic probes **GO** (scratch workspaces only, no real LLM key needed).

This card delivers the **no-LLM basic plumbing validation**: six deterministic
execution probes that drive the *real* tool-execution + result-persistence
contract of the durable Agent Runtime, confined to a **dedicated scratch
Postgres DB** and a **dedicated scratch storage workspace**, then proves the
tool results are persisted into the Run result store.

## Verdict

**PASS — 9/9 deterministic sub-checks across probes A–F.** No runtime defect.
The `Task → Run → Tool → Result-store → Ledger settlement` chain is functionally
correct end-to-end for tool execution without any model involvement.

Reproduce (from `backend/`, with a backend venv + this worktree's `PYTHONPATH`):

```
PYTHONPATH=<this-worktree>/backend <venv-python> scripts/verify_2f_execution_probes.py
```

The harness is self-isolating: it creates a uniquely-named scratch DB
(`clawith_2f_probes_<hex>`), builds the full schema via `Base.metadata.create_all`,
seeds FK-parent rows (tenant/user/model/agent/run), monkeypoints the tool
executor + write path into scratch (storage backend + session factory + workspace
root, no live DB/Redis), runs the probes, drops the scratch DB and removes the
scratch workspace, and writes machine-readable evidence to
`docs/PHASE_2F_EXECUTION_PROBES_EVIDENCE.json`.

## What each probe drives (real product seams, not mocks)

| Probe | Real code path exercised | Deterministic assertion |
|---|---|---|
| **A** read file | `agent_tools.execute_builtin_tool_outcome("read_file")` over scratch `LocalStorageBackend` | `status=="succeeded"` + content markers returned |
| **B** create file | `write_file` → `workspace_collaboration.write_workspace_file` (storage write + `workspace_file_revisions` row + version guard) | `succeeded` + semantic markers stored + **1 revision** row |
| **C** modify file | `edit_file` → version-guarded `write_workspace_file` (op=`edit`) | `succeeded` + old marker replaced, new marker present + **2 revision** rows |
| **D** read metadata | `execute_builtin_tool_outcome("list_files")` directory metadata read | `succeeded` + listing enumerates the created files |
| **E** safe command | real `_check_code_safety` gate + host-portable one-shot subprocess runner | safe code passes the gate & runs (`stdout=="42"`, exit 0); dangerous code is **blocked** by the real gate |
| **F** result persistence | REAL ledger: `reserve_tool_execution` → `normalize_tool_outcome` → `ToolResultStore.write` (archive) → `mark_tool_execution_succeeded/failed` → out-of-band `ToolResultStore.resolve` from a **fresh session** | success archived + `result_ref` settled + envelope re-resolves with matching `content_hash`; failed terminal receipt settled; small result kept inline (no archive) |

F is the authoritative answer to the card's core question — *"are Tool Results
correctly persisted into the Run result store?"* — and **yes**: the
`agent_tool_executions` ledger row is settled with the archived `result_ref`,
and the archived envelope re-reads byte-faithfully (SHA-256 content hash match)
through a fresh session + `ToolResultStore.resolve`, exactly as `tool_step_service`
does at runtime.

## Host artifacts (documented, NOT defects)

These are **Windows dev-host I/O artifacts** in the same class as preflight
caveat A9 (Unix-only `fcntl`/`preexec_fn`/bwrap). They are *not* product-code
defects and do not affect the tool-execution contract:

- **CRLF local-mirror clobbering (B/C).** `write_workspace_file` writes the
  byte-exact storage key AND mirrors to the local filesystem via
  `aiofiles.open(..., "w")` (text mode). On Windows the mirror translates
  `\n`→`\r\n` into the same physical file, so byte-exact read-back differs from
  the tool's logical content. The harness therefore asserts the host-portable
  facts — semantic markers + the DB revision trail — and documents the CRLF.
- **Container persistent-sandbox path (E).** `SubprocessBackend`'s
  persistent-session path requires `bwrap` + `preexec_fn` + `/data/agents` venv
  (Unix/container). On this host it raises `preexec_fn is not supported on
  Windows platforms`. The deterministic seam for E is therefore the **real
  `_check_code_safety` gate** (host-independent product logic) plus a
  host-portable one-shot runner that reproduces the legacy `execute_code`
  contract. The gate half is the load-bearing product behavior; the runner half
  proves a trivial command executes to a deterministic result.

Both artifacts are out of scope for this card (they belong to the container/
Unix deployment path and the real-LLM wave), and are flagged here so the
reviewer does not mistake them for runtime defects.

## Evidence

Machine-readable output of the last successful run:
`docs/PHASE_2F_EXECUTION_PROBES_EVIDENCE.json`.

- Scratch DB: `clawith_2f_probes_<8hex>` (created fresh, dropped after run unless `CLAWITH_2F_KEEP_DB=1`).
- Scratch workspace: `tempfile.mkdtemp(prefix="clawith_2f_ws_")` (removed after run unless `CLAWITH_2F_KEEP_WS=1`).

## Verification performed

- `scripts/verify_2f_execution_probes.py` → **PASS 9/9**, exit 0 (multiple runs).
- `ruff check` (F-class / real errors) on the two new scripts → **clean**.
  (Broad-exception style rules that the repo's own root-level
  `verify_2e_execution_chain.py` also carries remain; CI ruff gates only `app/` + `alembic/`.)
- No product-code changes: the change is a **root-level review/verification
  script + a doc + a JSON evidence artifact** (mirrors `verify_2e_execution_chain.py`
  precedent). No test expectations were altered; no mainline edited.

## Scope / next

- This validates **tool-execution + result-store plumbing** without a model.
- The **real-LLM wave** remains gated on the `t_31ac823d` blocker: all
  `llm_models.api_key_encrypted` rows are placeholder `enc-test` in the live
  pool — no real key is provisioned. Probes A–F do **not** require that key.
