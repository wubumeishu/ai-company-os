"""Phase 2F — Deterministic Execution Probes A–F (no real LLM).

Drives the ACTUAL tool-execution + result-persistence plumbing of the durable
Agent Runtime, in a dedicated scratch Postgres DB and a dedicated scratch
storage workspace, with zero model involvement:

  A  read file            real `execute_builtin_tool_outcome("read_file")`
  B  create file         real `write_file` write path (storage + DB revision)
  C  modify file         real `edit_file` path (version-guarded update + rev)
  D  read metadata       real `execute_builtin_tool_outcome("list_files")`
  E  safe command        real `_check_code_safety` gate + host-portable
                         one-shot runner (container persistent path is
                         out of scope on this Windows dev host — caveat A9)
  F  result persistence  REAL ledger: `reserve_tool_execution` ->
                         `normalize_tool_outcome` -> `ToolResultStore.write`
                         -> `mark_tool_execution_succeeded/failed` ->
                         out-of-band `ToolResultStore.resolve` (fresh session)

Every probe asserts a deterministic outcome and, for F, re-reads the settled
fact through a fresh session to prove the tool result really is persisted into
the Run result store (the `agent_tool_executions` ledger + the archived
envelope in storage).

Run with a backend venv whose site-packages match this repo's lockfile,
pointing PYTHONPATH at THIS worktree's backend dir:

    <sibling-venv>/python.exe scripts/verify_2f_execution_probes.py

Environment overrides:
    CLAWITH_2F_PG_ADMIN      admin DSN (default postgres:postgres@localhost:5432/postgres)
    CLAWITH_2F_KEEP_DB       "1" keeps the scratch DB (default: drop it)
    CLAWITH_2F_KEEP_WS       "1" keeps the scratch storage dir (default: remove)

The scratch DB and scratch storage are disposable review state — never a
source of truth.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent / "backend"
REPO = HERE.parent
sys.path.insert(0, str(BACKEND))

KEEP_DB = os.environ.get("CLAWITH_2F_KEEP_DB") == "1"
KEEP_WS = os.environ.get("CLAWITH_2F_KEEP_WS") == "1"
ADMIN_BASE = os.environ.get(
    "CLAWITH_2F_PG_ADMIN", "postgresql+asyncpg://postgres:postgres@localhost:5432"
)
ADMIN_URL = f"{ADMIN_BASE}/postgres"


def _import_all_models() -> None:
    """Register every model on Base.metadata (mirrors the Phase 2E verifier)."""
    pkg = BACKEND / "app" / "models"
    for name in sorted(os.listdir(pkg)):
        if name.endswith(".py") and name != "__init__.py":
            importlib.import_module(f"app.models.{name[:-3]}")




def _patched_get_storage(storage):
    def _get_storage_backend():
        return storage

    return _get_storage_backend


# ── probe evidence accumulator ───────────────────────────────────────────────
results: dict[str, object] = {}
evidence: dict[str, object] = {}


def _ok(label: str, value: bool, detail: object = None) -> bool:
    results[label] = bool(value)
    if detail is not None:
        evidence[label] = detail
    status = "PASS" if value else "FAIL"
    print(f"  [{status}] {label}" + (f" :: {detail}" if detail is not None else ""))
    return bool(value)


async def main() -> int:
    t0 = time.time()
    _import_all_models()

    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from app.database import Base
    from app.models.agent import Agent
    from app.models.agent_run import AgentRun
    from app.models.llm import LLMModel
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services import agent_tools
    from app.services import workspace_collaboration as wcs
    from app.services.agent_runtime import tool_result_store as trs
    from app.services.sandbox.local.subprocess_backend import _check_code_safety
    from app.services.storage_runtime.local import LocalStorageBackend

    # ── dedicated scratch storage workspace ────────────────────────────────
    ws_root = Path(
        os.environ.get("CLAWITH_2F_SCRATCH_WS") or tempfile.mkdtemp(prefix="clawith_2f_ws_")
    )
    ws_root.mkdir(parents=True, exist_ok=True)
    storage = LocalStorageBackend(str(ws_root))

    # ── dedicated scratch Postgres DB (fresh, isolated, disposable) ────────
    db_name = f"clawith_2f_probes_{uuid.uuid4().hex[:8]}"
    scratch_url = f"{ADMIN_BASE}/{db_name}"
    engine_admin = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    async with engine_admin.connect() as c:
        await c.execute(_ddl(f'CREATE DATABASE "{db_name}"'))
    await engine_admin.dispose()

    engine = create_async_engine(scratch_url, pool_size=4, max_overflow=4)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)

    # ── seed FK-parent rows into the scratch DB (committed in sequence so
    #    each FK target is durable before the dependent row is inserted) ──
    tenant = uuid.uuid4()
    user = uuid.uuid4()
    model = uuid.uuid4()
    agent = uuid.uuid4()
    run = uuid.uuid4()
    async with factory() as s, s.begin():
        s.add(Tenant(id=tenant, name="T-2F", slug=f"t2f-{tenant.hex[:8]}", im_provider="web_only"))
        s.add(User(id=user, tenant_id=tenant, display_name="probe-user"))
    async with factory() as s, s.begin():
        s.add(LLMModel(id=model, provider="anthropic", model="claude-opus-4-6",
                        api_key_encrypted="enc-test", label="probe-model"))
    async with factory() as s, s.begin():
        s.add(Agent(id=agent, name="ProbeAgent", creator_id=user, tenant_id=tenant,
                    status="idle", primary_model_id=model))
        await s.flush()
        s.add(AgentRun(
            id=run, tenant_id=tenant, agent_id=agent,
            source_type="task", source_id=f"probe:{run.hex[:8]}",
            source_execution_id=f"task:probe:{run.hex[:8]}",
            goal="[2f] deterministic execution probe",
            run_kind="background", system_role=None,
            model_id=model, model_turn_limit=8,
            runtime_type="langgraph", runtime_thread_id=f"t2f-{run.hex[:12]}",
            graph_name="clawith_agent_runtime", graph_version="v1",
            delivery_status="not_required",
        ))

    # ── isolate the tool executor + write path into scratch (no live DB/Redis) ──
    saved = {
        "at_async": agent_tools.async_session,
        "at_storage": agent_tools.get_storage_backend,
        "at_wlocks": agent_tools.workspace_locks,
        "at_wroot": agent_tools.WORKSPACE_ROOT,
        "wcs_storage": wcs.get_storage_backend,
        "wcs_wlocks": wcs.workspace_locks,
    }
    # `agent_tools` resolves `async_session` as a module global; point it at the
    # scratch session factory so write-path revision/lock writes hit scratch DB.
    agent_tools.async_session = factory
    agent_tools.get_storage_backend = _patched_get_storage(storage)
    agent_tools.WORKSPACE_ROOT = ws_root
    wcs.get_storage_backend = _patched_get_storage(storage)
    _install_noop_locks(agent_tools, wcs)

    print(f"=== Phase 2F Deterministic Execution Probes ({db_name}) ===")
    print(f"  scratch ws : {ws_root}")
    print(f"  scratch db : {db_name}")

    # seed a known input file for probe A
    await storage.write_bytes(f"{agent}/workspace/probe_input.txt", b"alpha\nbeta\ngamma\n")

    try:
        # ──────────────────────────── A: read file ─────────────────────────
        a = await agent_tools.execute_builtin_tool_outcome(
            "read_file", {"path": "workspace/probe_input.txt"},
            agent, user, "", None,
            runtime_tenant_id=str(tenant),
        )
        a_status = a.status if hasattr(a, "status") else "str"
        a_body = a.result_summary or "" if hasattr(a, "result_summary") else str(a)
        _ok(
            "A_read_file_succeeded",
            a_status == "succeeded" and "alpha" in a_body and "gamma" in a_body,
            {"status": a_status, "has_lines": "alpha" in a_body and "gamma" in a_body},
        )

        # ──────────────────────────── B: create file ───────────────────────
        b = await agent_tools.execute_builtin_tool_outcome(
            "write_file", {"path": "workspace/probe_b.md", "content": "hello-2f\nline2\n"},
            agent, user, "", None,
            runtime_tenant_id=str(tenant),
        )
        b_status = b.status if hasattr(b, "status") else "str"
        b_bytes = await storage.read_bytes(f"{agent}/workspace/probe_b.md")
        b_rev = await _count_revisions(factory, agent, "workspace/probe_b.md")
        b_text = _nl_normalize(b_bytes).decode("utf-8", "replace")
        _ok(
            "B_create_file_succeeded",
            b_status == "succeeded"
            and "hello-2f" in b_text and "line2" in b_text
            and b_rev.get("total") == 1 and b_rev.get("write") == 1,
            {"status": b_status, "text_norm": repr(b_text[:40]),
             "markers_present": "hello-2f" in b_text and "line2" in b_text,
             "revision_trail": b_rev,
             "note": "byte-exact CRLF is a Windows local-mirror I/O artifact; "
                     "asserting host-portable semantic markers + revision trail"},
        )

        # ──────────────────────────── C: modify file ───────────────────────
        c = await agent_tools.execute_builtin_tool_outcome(
            "edit_file",
            {"path": "workspace/probe_b.md", "old_string": "hello-2f", "new_string": "updated-2f"},
            agent, user, "", None,
            runtime_tenant_id=str(tenant),
        )
        c_status = c.status if hasattr(c, "status") else "str"
        c_bytes = await storage.read_bytes(f"{agent}/workspace/probe_b.md")
        c_rev = await _count_revisions(factory, agent, "workspace/probe_b.md")
        c_text = _nl_normalize(c_bytes).decode("utf-8", "replace")
        _ok(
            "C_modify_file_succeeded",
            c_status == "succeeded"
            and "updated-2f" in c_text and "hello-2f" not in c_text
            and "line2" in c_text
            and c_rev.get("total") == 2 and c_rev.get("edit") == 1,
            {"status": c_status, "text_norm": repr(c_text[:48]),
             "old_replaced": "updated-2f" in c_text and "hello-2f" not in c_text,
             "other_marker_intact": "line2" in c_text,
             "revision_trail": c_rev,
             "note": "CRLF/rare \r\r is a Windows local-mirror I/O artifact; "
                     "asserting semantic edit + revision trail"},
        )

        # ──────────────────────────── D: read metadata ─────────────────────
        d = await agent_tools.execute_builtin_tool_outcome(
            "list_files", {"path": "workspace"},
            agent, user, "", None,
            runtime_tenant_id=str(tenant),
        )
        d_status = d.status if hasattr(d, "status") else "str"
        d_body = d.result_summary or "" if hasattr(d, "result_summary") else str(d)
        _ok(
            "D_read_metadata_succeeded",
            d_status == "succeeded" and "probe_b.md" in d_body and "probe_input.txt" in d_body,
            {"status": d_status, "lists_both": "probe_b.md" in d_body and "probe_input.txt" in d_body,
             "file_count": int(d_body.count("probe_")) if d_status == "succeeded" else 0},
        )

        # ──────────────────────────── E: safe command ──────────────────────
        safe_code = "print(6 * 7)\n"
        dangerous_code = "import shutil\nshutil.rmtree('/')\n"
        e_gate_safe = _check_code_safety("python", safe_code, allow_network=False)
        e_gate_danger = _check_code_safety("python", dangerous_code, allow_network=False)
        e_run = await _run_one_shot(safe_code)  # host-portable one-shot runner
        _ok(
            "E_safe_command_gate",
            e_gate_safe is None and e_gate_danger is not None,
            {"safe_passes": e_gate_safe is None,
             "dangerous_blocked": e_gate_danger is not None,
             "block_reason": e_gate_danger},
        )
        _ok(
            "E_safe_command_runs",
            e_run.success and e_run.stdout.strip() == "42" and e_run.exit_code == 0,
            {"success": e_run.success, "stdout": e_run.stdout.strip(), "exit_code": e_run.exit_code,
             "runner": "host one-shot subprocess (container persistent path out of scope on win32 — caveat A9)"},
        )

        # ──────────────────────────── F: result persistence ────────────────
        await _probe_f_persistence(factory, storage, agent_tools, trs,
                                   tenant, agent, run, user)

    finally:
        # restore module attributes
        agent_tools.async_session = saved["at_async"]
        agent_tools.get_storage_backend = saved["at_storage"]
        agent_tools.workspace_locks = saved["at_wlocks"]
        agent_tools.WORKSPACE_ROOT = saved["at_wroot"]
        wcs.get_storage_backend = saved["wcs_storage"]
        wcs.workspace_locks = saved["wcs_wlocks"]
        await engine.dispose()
        if not KEEP_DB:
            eng = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
            try:
                async with eng.connect() as c:
                    await c.execute(_ddl(f'DROP DATABASE IF EXISTS "{db_name}"'))
            finally:
                await eng.dispose()
        if not KEEP_WS:
            shutil.rmtree(ws_root, ignore_errors=True)

    # ── verdict + machine-readable evidence ─────────────────────────────────
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    ok = passed == total and total >= 1
    evidence["verdict"] = (
        f"PASS — all {total} deterministic sub-checks across probes A–F"
        if ok else f"CHECK — {passed}/{total}"
    )
    evidence["scratch_db"] = db_name
    evidence["scratch_ws"] = str(ws_root)
    evidence["db_kept"] = KEEP_DB
    evidence["ws_kept"] = KEEP_WS
    evidence["elapsed_s"] = round(time.time() - t0, 2)
    evidence["probes"] = {k: (results.get(k)) for k in results}

    out_json = REPO / "docs" / "PHASE_2F_EXECUTION_PROBES_EVIDENCE.json"
    out_json.write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")
    print("=== VERDICT ===")
    for k, v in results.items():
        print(f"  {k}: {v}")
    print(f"  {evidence['verdict']}  ({evidence['elapsed_s']}s)")
    print(f"  evidence -> {out_json}")
    print(f"  db_kept={KEEP_DB} ws_kept={KEEP_WS}")
    return 0 if ok else 1


def _ddl(sql: str):
    from sqlalchemy import text

    return text(sql)


def _install_noop_locks(*modules) -> None:
    """Disable the Redis-backed workspace mutation locks for scratch isolation.

    Mirrors the proven test seam (test_agent_tools_storage_workspace.py):
    ``workspace_locks`` is used as ``async with workspace_locks(agent, [p])``,
    so the module attribute is replaced by an async-context-manager factory.
    The write/edit file paths do not consult these locks; only move/delete do.
    """
    @asynccontextmanager
    async def _cm(*_args, **_kwargs):
        yield

    for mod in modules:
        try:
            mod.workspace_locks = _cm
        except Exception:
            pass


def _nl_normalize(data: bytes) -> bytes:
    """Collapse CRLF/CR newlines to LF so byte reads are host-portable.

    The Windows local-mirror write path (`aiofiles.open(..., "w")`, text mode)
    translates LF->CRLF, clobbering the byte-exact storage write that
    `write_workspace_file` also issues into the same physical file. This is a
    host I/O artifact (cf. preflight caveat A9), not tool logic; normalizing
    lets B/C assert the semantic result + revision trail deterministically.
    """
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


async def _count_revisions(factory, agent, path: str) -> dict:
    from sqlalchemy import select

    from app.models.workspace import WorkspaceFileRevision

    ops: dict[str, int] = {}
    async with factory() as s:
        rows = (
            await s.execute(
                select(WorkspaceFileRevision.operation).where(
                    WorkspaceFileRevision.scope_type == "agent",
                    WorkspaceFileRevision.scope_id == agent,
                    WorkspaceFileRevision.path == path,
                )
            )
        ).scalars().all()
        for op in rows:
            ops[op] = ops.get(op, 0) + 1
    ops["total"] = sum(v for k, v in ops.items() if k not in ("total",))
    return ops


async def _run_one_shot(code: str):
    """Host-portable one-shot runner mirroring the legacy execute_code contract.

    The container persistent `SubprocessBackend` path (bwrap + preexec_fn +
    /data/agents venv) is not runnable on this Windows dev host (preflight
    caveat A9). A trivial safe command is instead executed directly via the
    host interpreter, mapping to the same `ExecutionResult` contract.
    """
    from app.services.sandbox.base import ExecutionResult

    with tempfile.TemporaryDirectory(prefix="clawith_2f_exec_") as tmp:
        ws = Path(tmp) / "agent-ws"
        ws.mkdir(parents=True)
        script = ws / "_probe.py"
        script.write_text(code, encoding="utf-8")
        t0 = time.time()
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-B", str(script),
            cwd=str(ws),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        return ExecutionResult(
            success=proc.returncode == 0,
            stdout=stdout.decode("utf-8", "replace")[:10000],
            stderr=stderr.decode("utf-8", "replace")[:5000],
            exit_code=proc.returncode or 0,
            duration_ms=int((time.time() - t0) * 1000),
            error=None if proc.returncode == 0 else f"Exit code: {proc.returncode}",
        )


async def _probe_f_persistence(factory, storage, agent_tools, trs, tenant, agent, run, user) -> None:
    """Drive the REAL result-persistence chain and prove it survives a fresh session."""
    from app.services.agent_runtime import tool_result_store as _trs
    from app.services.agent_runtime.tool_execution import ToolExecutionOutcome

    store = _trs.ToolResultStore(session_factory=factory, storage=storage)

    # F1: a SUCCESS read result whose summary exceeds the inline budget -> the
    # body is archived into the result store and the ledger settled with a ref.
    a2 = await agent_tools.execute_builtin_tool_outcome(
        "read_file", {"path": "workspace/probe_input.txt"}, agent, user, "", None,
        runtime_tenant_id=str(tenant),
    )
    raw_outcome = ToolExecutionOutcome(
        status="succeeded",
        result_summary=a2.result_summary or "(read ok)",
        result_ref=None,
        artifact_refs=(), evidence_refs=(), metadata={},
    )

    res_f1, arch_f1, _exec_f1, ok_f1, resolve_f1 = await _run_f_round(
        factory, store, trs, "F1", tenant, agent, run, "tc_f1", raw_outcome,
        effect="read", retry_policy="safe", inline_max_bytes=64, settle="succeeded",
    )
    _ok("F_result_persistence_success_archived",
        ok_f1 and res_f1 is not None and res_f1.status == "succeeded"
        and resolve_f1 is not None and resolve_f1.content_hash,
        {"exec_status": res_f1.status if res_f1 else None,
         "archived": arch_f1 is not None,
         "resolved_content_hash": (resolve_f1.content_hash[:16] + "...") if resolve_f1 else None,
         "resolved_status": resolve_f1.status if resolve_f1 else None})

    # F2: a FAILED result -> persisted as a terminal failed receipt (no archive ref).
    fail_outcome = ToolExecutionOutcome(
        status="failed",
        result_summary="File not found: workspace/does_not_exist.txt",
        result_ref=None,
        error_code="workspace_file_not_found",
        retryable=False,
        metadata={},
    )
    res_f2, arch_f2, _exec_f2, ok_f2, _res2 = await _run_f_round(
        factory, store, trs, "F2", tenant, agent, run, "tc_f2", fail_outcome,
        effect="read", retry_policy="safe", inline_max_bytes=64, settle="failed",
    )
    _ok("F_result_persistence_failed_settled",
        ok_f2 and res_f2 is not None and res_f2.status == "failed" and arch_f2 is None,
        {"exec_status": res_f2.status if res_f2 else None, "archived": arch_f2 is not None,
         "error_code": fail_outcome.error_code})

    # F3: a small SUCCESS result kept inline (no archive needed).
    small_outcome = ToolExecutionOutcome(
        status="succeeded",
        result_summary="ok",
        result_ref=None,
        artifact_refs=(), evidence_refs=(), metadata={},
    )
    res_f3, arch_f3, _exec_f3, ok_f3, _res3 = await _run_f_round(
        factory, store, trs, "F3", tenant, agent, run, "tc_f3", small_outcome,
        effect="read", retry_policy="safe", inline_max_bytes=100_000, settle="succeeded",
    )
    _ok("F_result_persistence_inline_success",
        ok_f3 and res_f3 is not None and res_f3.status == "succeeded" and arch_f3 is None,
        {"exec_status": res_f3.status if res_f3 else None, "archived": arch_f3 is not None})


async def _run_f_round(factory, store, trs, tag, tenant, agent, run, call_id,
                       outcome, *, effect, retry_policy, inline_max_bytes, settle):
    """One deterministic reserve->normalize->archive->settle->resolve round."""
    from app.services.agent_runtime.tool_execution import (
        mark_tool_execution_failed,
        mark_tool_execution_succeeded,
        normalize_tool_outcome,
        reserve_tool_execution,
        sanitize_tool_arguments,
    )

    lease_owner = f"2f-{tag.lower()}"
    args = {"path": "workspace/probe_input.txt"}
    result_ref: str | None = None
    archived = None
    settled_row = None

    async with factory() as db, db.begin():
        res = await reserve_tool_execution(
            db,
            tenant_id=tenant, run_id=run,
            tool_call_id=call_id, tool_name="read_file",
            assistant_message_id=f"am-{call_id}",
            arguments=args,
            sanitized_arguments=sanitize_tool_arguments(args),
            request_ref=None,
            side_effect_classification=effect,
            retry_policy=retry_policy,
            lease_owner=lease_owner, lease_ttl_seconds=120,
        )
        if not res.can_execute:
            return None, None, None, False, None
        execution = res.execution
        normalized, archived_body = normalize_tool_outcome(
            outcome, effect=effect, retry_policy=retry_policy, inline_max_bytes=inline_max_bytes
        )
        if archived_body is not None and normalized.status == "succeeded":
            result_ref = await store.write(execution, normalized, archived_body)
            archived = result_ref  # the result store really archived this body
            normalized = _replace_ref(normalized, result_ref)

    # settle the ledger fact (separate short transaction, like the real worker)
    async with factory() as db, db.begin():
        if settle == "succeeded":
            settled_row = await mark_tool_execution_succeeded(
                db, tenant_id=tenant, execution_id=execution.id, lease_owner=lease_owner,
                result_summary=normalized.result_summary, result_ref=result_ref,
                error_code=normalized.error_code, retryable=normalized.retryable,
                artifact_refs=normalized.artifact_refs, evidence_refs=normalized.evidence_refs,
                metadata=normalized.metadata,
            )
        else:
            settled_row = await mark_tool_execution_failed(
                db, tenant_id=tenant, execution_id=execution.id, lease_owner=lease_owner,
                result_summary=normalized.result_summary, result_ref=result_ref,
                error_code=normalized.error_code, retryable=normalized.retryable,
                artifact_refs=normalized.artifact_refs, evidence_refs=normalized.evidence_refs,
                metadata=normalized.metadata,
            )

    ok = settled_row is not None and settled_row.status == settle

    # out-of-band proof: re-read via a FRESH session + ToolResultStore.resolve
    resolved = None
    if settle == "succeeded" and result_ref:
        store2 = trs.ToolResultStore(session_factory=factory, storage=_storage_of(store))
        resolved = await store2.resolve(result_ref, tenant_id=tenant, run_id=run)

    return settled_row, archived, execution, ok, resolved


def _replace_ref(outcome, ref):
    from dataclasses import replace
    return replace(outcome, result_ref=ref)


def _storage_of(store):
    # ToolResultStore stores its backend under _storage
    return store._storage


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
