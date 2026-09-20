# Clawith Capability Concept Map — Code Evidence

Task: t_d018ea9c (baseline 834d621, Clawith @45fc701c). All paths relative to repo root.
Method: no concept marked present without direct code references shown. Execution
status = whether the concept is on a live execution path (runtime, tool, or
scheduler), not just persisted metadata.

## 1. Agent — PRESENT, participates in real execution
- Model: `backend/app/models/agent.py:19` `class Agent(Base)` → table `agents`
  (also `AgentPermission`, `AgentTemplate`, `AgentUserOnboarding`).
- Execution owner: `backend/app/services/agent_manager.py` (start/stop lifecycle),
  `backend/app/services/agent_runtime/langgraph_driver.py` + `worker_service.py`
  (LangGraph durable execution), `backend/app/services/agent_tools.py`
  (per-agent tool assembly).
- API: `backend/app/api/agents.py` (router `/agents`): GET/POST `/agents`,
  `/{agent_id}/start`, `/{agent_id}/stop`, `/templates`, permissions CRUD.
  Registered in `backend/app/main.py:430`.
- DB: `agents`, `agent_permissions`, `agent_templates`, `agent_credentials`,
  `agent_schedules`, `agent_triggers`, `agent_focus_items`,
  `agent_activity_logs`, `daily_token_usage`.
- Execution: yes — an Agent is the subject of every AgentRun
  (`agent_runs.agent_id` FK); start/stop endpoints drive the runtime.

## 2. Task — PRESENT, participates in real execution
- Model: `backend/app/models/task.py:13` `class Task(Base)` → table `tasks`
  (types `todo`/`supervision`, status `pending`/`doing`/`done`);
  `TaskLog` → `task_logs`.
- Execution: `backend/app/services/task_executor.py`:
  `enqueue_task_runtime()` registers a run with `source_type="task"` when the
  agent is on runtime v2 (`decide_runtime_v2`); `execute_task()` (legacy)
  runs the task inline.
- API: `backend/app/api/tasks.py` (router `/agents/{agent_id}/tasks`):
  list/create/update `GET/POST/PATCH`, logs, and `POST /{task_id}/trigger`
  (manual execution, `tasks.py:164` calls `execute_task` via
  `asyncio.create_task`). `main.py:431`.
- Also invoked by the agent tool layer: `agent_tools.py:9857` triggers
  `execute_task` from tool calls.
- Execution: yes — tasks are an AgentRun source type
  (`agent_runs.source_type IN ('chat','trigger','task','a2a','heartbeat')`).

## 3. AgentRun — PRESENT, the durable execution ledger
- Model: `backend/app/models/agent_run.py:27` `class AgentRun(Base)` →
  `agent_runs` (PK `id, tenant_id`; closed enums `run_kind`
  foreground/background/delegated/orchestration, `delivery_status`,
  `runtime_type` legacy/langgraph; self-FK `parent_run_id`).
  Companions: `agent_run_commands`, `agent_run_events`,
  `agent_tool_executions`.
- Execution owner: `backend/app/services/agent_runtime/` —
  `worker_service.py` (daemons: `RuntimeCommandDaemon`, `ChannelDeliveryDaemon`,
  `AsyncToolPollDaemon`, `ProductReconcileDaemon`, `ToolResultReconcileDaemon`),
  `graph.py` (`AgentRuntimeGraph`, LangGraph), `node_executor.py`,
  `command_worker.py`.
- API: no direct CRUD router; runs are created by intake paths
  (`chat_sessions.py`, `task_executor.py`, `a2a_runtime.py`, trigger runtime)
  and observed via `agent_run_event.py` streams and
  `backend/app/api/agents.py:1220` `GET /{agent_id}/gateway-messages`.
- Execution: yes — by definition; this is where execution state is tracked
  ("Product-owned identity and delivery facts; execution state stays in
  checkpoints").

## 4. Tool — PRESENT, participates in real execution
- Model: `backend/app/models/tool.py` → tables `tools`, `agent_tools`
  (DB rows are enablement/config/UI; explicitly NOT the model contract).
- Canonical definitions: `backend/app/services/builtin_tool_definitions.py`
  (agent-relative path namespace, schemas, execution policy, incl.
  `execute_code` timeout bounds and `send_message_to_agent` at line 583).
- Runtime: `backend/app/services/agent_runtime/tool_registry.py`
  (`RegisteredTool`, `resolve_registered_tool`), `tool_execution.py`
  (`ToolExecutionOutcome`, sanitization/redaction, reconciliation),
  `tool_step_service.py`, `tool_result_store.py`, `tool_contracts.py`.
- API: `backend/app/api/tools.py` (router `/tools`), `main.py:445`.
- Execution: yes — every model turn's tool calls run through
  `tool_step_service` inside the AgentRun graph.

## 5. Workspace — PRESENT, participates in real execution
- Physical: `initialize_agent_workspace()` in `agent_tools.py:1653`;
  `TempWorkspace` materialization (`agent_tools.py:1690-1877`) syncs the
  storage-backed agent workspace into the sandbox run directory and flushes
  results back.
- Models: `backend/app/models/workspace.py` → `workspace_file_revisions`,
  `workspace_edit_locks`.
- Services: `workspace_paths.py` (agent-root-relative resolution,
  `enterprise_info_root`), `workspace_locking.py` (edit locks),
  `workspace_collaboration.py`, `workspace_reconciliation.py`;
  storage backends in `storage_runtime/{local,s3,base,facade}.py`.
- API: no dedicated workspace router; workspace access is exposed through the
  builtin file tools (`list_files`/`read_file`/`write_file`/... in
  `builtin_tool_definitions.py`) and group file sharing (`group_file_service.py`).
- Execution: yes — tools and `verification.py` (source-preservation conflicts,
  `normalize_workspace_path`) operate on the live workspace.

## 6. Sandbox — PRESENT, participates in real execution
- Subsystem: `backend/app/services/sandbox/` —
  `base.py` (`SandboxBackend` protocol, `ExecutionResult`, `SandboxCapabilities`),
  `config.py` (`SandboxType`: subprocess, docker, e2b, judge0, codesandbox,
  self_hosted, aio_sandbox; default subprocess),
  `registry.py` (`get_sandbox_backend` factory), `execution_lease.py`,
  `run_scope.py`, `workspace_policy.py`.
  Backends: `local/subprocess_backend.py` (`SubprocessBackend`,
  `_PersistentBwrapSession`, code safety check), `local/docker_backend.py`,
  `api/{e2b,judge0,codesandbox}_backend.py`, `remote/{aio_sandbox,self_hosted}_backend.py`.
- API: no dedicated sandbox router; sandbox is driven by the `execute_code`
  tool (`agent_tools.py` lines 2676, 4289, 4606, 4954
  `_execute_code_with_workspace_outcome`, 12164 legacy path; config at 1344).
- Execution: yes — `execute_code`/`execute_code_e2b` are live builtin tools;
  `execute_code_e2b` hard-requires `sandbox_type=e2b` (agent_tools.py:12003).

## 7. Artifact — PRESENT (as typed outcome field), no dedicated table
- No `Artifact` model/table exists; artifacts are first-class fields on tool
  results: `backend/app/services/agent_runtime/tool_result_store.py:94`
  `artifact_refs: tuple[str, ...]` (+`artifact_content_hash`), stored inside
  `agent_tool_executions`.
- Verification consumes them: `node_executor.py` `VerificationResult`
  (`_verified_refs(verification, "artifact_refs")`),
  `verification.py` deterministic completion gate.
- Publishable artifacts: `published_pages` table (`published_page.py`) via
  the `publish_page` builtin tool.
- API: no artifact router; artifacts surface through run events
  (`agent_run_events`) and delivery.
- Execution: yes — artifact refs flow through tool results into the
  completion gate; but they are metadata, not an independently tracked
  object.

## 8. Verification — PRESENT, participates in real execution
- `backend/app/services/agent_runtime/verification.py` (1050 lines):
  "Database-backed deterministic completion checks for Durable Runtime" —
  independent LLM completion gate (`_TASK_COMPLETION_SYSTEM_PROMPT`,
  pass/repair verdict JSON schema), workspace source-preservation evidence,
  evidence extraction from run messages.
- `node_executor.py:103` `VerificationResult` (`outcome: pass|repair|fail`),
  `RuntimeVerifier` protocol (`node_executor.py:186`),
  `DeterministicRuntimeVerifier` v1 fallback (line 202), repair episodes with
  a bounded budget (`max_verification_repairs=2`, line 573).
- Execution: yes — the finish node routes to `verify`
  (node_executor.py:768), on failure the graph repairs instead of completing.

## 9. A2A (agent-to-agent) — PRESENT, participates in real execution
- `backend/app/services/agent_runtime/a2a_runtime.py`:
  `GatewayA2ARuntimeIntake` / `GatewayA2ARuntimeCompletion`,
  `ensure_a2a_session` (line 468), `enqueue_gateway_a2a_runtime` (533),
  `RuntimeA2AService.execute` (line 774).
- Trigger: builtin tool `send_message_to_agent`
  (`builtin_tool_definitions.py:583`, executed in
  `tool_step_service.py:2319,2587` via `RuntimeA2AService`);
  `agent_runs.source_type` includes `'a2a'`.
- Related: `a2a_completion.py` (completion semantics),
  `group_handoff.py`, `group_runtime_tools.py`;
  `agent_agent_relationships` table (org.py:87); async flag in
  migration `alembic/versions/032_add_a2a_async_enabled.py`.
- API: no public A2A router; A2A is a tool-mediated intake into
  `gateway_messages`, observed via `GET /agents/{id}/gateway-messages`.
- Tests: `test_agent_runtime_a2a.py` (11 test functions).
- Execution: yes.

## 10. Knowledge — PRESENT as Experience library + Skills + Enterprise KB
- Models: `experience.py` `ExperienceEntry` → `experience_entries`,
  `experience_reference.py` `ExperienceReference` → `experience_references`;
  `skill.py` `Skill`/`SkillFile` → `skills`, `skill_files`.
- Retrieval (model-side): `experience_retrieval.py` —
  `search_experience`, `read_experience`, `record_experience_citations`,
  `build_experience_hint`; department-scoped visibility
  (`_agent_department_ids`, line 91).
- API: `backend/app/api/experience.py` (router `/api/experience`:
  entries CRUD, `/distill`, drafts, publish/retire, review, stats;
  main.py:448 as `enterprise_kb_router`), `backend/app/api/skills.py`
  (skills CRUD + ClawHub search/install; main.py:448).
- Execution: yes — experience tools are agent-invokable (search/read
  experience) and skills are injected into agent context; the library is
  read on the model path, written by distillation.
- Note: no vector RAG in this baseline; "knowledge" is structured
  entries/skills + optional directory search.

## 11. Department — PRESENT, metadata-only (no execution role)
- Model: `backend/app/models/org.py:12` `class OrgDepartment(Base)` →
  `org_departments` (name, parent_id, path, member_count; "departments and
  members synced from Feishu"); `OrgMember` → `org_members` (department FK).
- Sync: `backend/app/services/org_sync_service.py` `OrgSyncService.sync_provider`
  (+ `feishu_contact_search.py`, `org_sync_adapter.py`).
- API: `backend/app/api/organization.py` router `/org` — only
  `GET /org/users`, `PATCH /org/users/{user_id}`; no department CRUD
  endpoint in this router (main.py:435).
- Execution: indirect only — departments gate knowledge visibility in
  `experience_retrieval.py:91` ("Agents have no first-class department; they
  inherit it from the human who created the agent"). No scheduler, agent
  dispatch, or permission decision consumes department data.
- Verdict: present in code and DB, but NOT a participant in real execution
  beyond knowledge scoping.

---

## Summary table

| Concept | Table(s) | Core code | API entry | In real execution |
|---|---|---|---|---|
| Agent | agents, agent_permissions, agent_credentials | models/agent.py; agent_manager.py; agent_runtime/ | /agents (incl. /start, /stop) | Yes |
| Task | tasks, task_logs | models/task.py; task_executor.py | /agents/{id}/tasks (+/trigger) | Yes (run source_type=task) |
| AgentRun | agent_runs, agent_run_commands, agent_run_events, agent_tool_executions | models/agent_run.py; agent_runtime/{worker_service,graph,node_executor} | none direct (intake + gateway-messages) | Yes (ledger of execution) |
| Tool | tools, agent_tools | builtin_tool_definitions.py; tool_registry/tool_execution/tool_step_service | /tools | Yes |
| Workspace | workspace_file_revisions, workspace_edit_locks | agent_tools.py workspace fns; workspace_paths/locking/reconciliation; storage_runtime | via file tools; group_file_service | Yes |
| Sandbox | (config via settings/agent_tools config) | services/sandbox/ (7 backends, registry, lease) | via execute_code / execute_code_e2b tools | Yes |
| Artifact | none (fields on agent_tool_executions; published_pages for pages) | tool_result_store.py; node_executor.py | none | Yes (typed refs) |
| Verification | (state in agent_run checkpoint/events) | verification.py; node_executor.py | none | Yes (completion gate) |
| A2A | gateway_messages, agent_agent_relationships | a2a_runtime.py; a2a_completion.py | via send_message_to_agent tool | Yes |
| Knowledge | experience_entries, experience_references, skills, skill_files | experience_retrieval.py; skill_creator_* | /api/experience, /skills | Yes (retrieval on model path) |
| Department | org_departments, org_members | models/org.py; org_sync_service.py | /org/users (members only) | No (metadata + knowledge scoping only) |

## Gaps / risks
- Artifact has no owner table — artifact refs are unvalidated strings on tool
  executions; a future "Artifact" object (owner, retention, promotion) would
  be a new state machine requiring its own need per AGENTS.md.
- Department has no API surface of its own; only Feishu sync writes it.
- `advanced.py` collaboration endpoints (`/agents/{id}/collaborate/delegate`,
  `/collaborate/message`, `/handover`) are a second, older agent-to-agent
  path that coexists with the runtime A2A path — worth an ADR review on which
  is authoritative.
- Task execution has dual paths (legacy inline `execute_task` vs v2
  `enqueue_task_runtime` chosen by `decide_runtime_v2`); v1 legacy is a
  compatibility path needing a documented removal condition.
