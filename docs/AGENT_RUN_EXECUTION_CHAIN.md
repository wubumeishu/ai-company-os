Phase 1 补充资料：Agent Run 端到端执行链 trace（源自 worktree t_70a95b59，未提交过的独立取证文档；基线 834d621）

# Agent Run — End-to-End Execution Chain Trace

Baseline: 834d621 (Clawith @45fc701c, dataelement/Clawith main).
Scope: `backend/app/services/agent_runtime/` ("durable Agent Runtime") and its ingress boundaries.
All paths are repository-relative.

## Executive verdict

The chain **User request -> Run/Command creation -> Agent invocation -> Context
assembly -> Model call -> Tool execution -> Result saving -> Run completion is
COMPLETE** in the current baseline. Every ingress (web chat, tasks, triggers,
channel bots, A2A, heartbeat) funnels into one durable command inbox
(`AgentRunCommand`), which is drained by one worker (`RuntimeCommandWorker`)
that advances a single deterministic LangGraph under PostgreSQL checkpoints.
No step is missing; there are no parallel/legacy execution paths for v2
runs (fail-closed when v2 is not selected).

Caveats that are NOT breaks in the chain but must be known:

- Untyped built-in tools are rejected as `untyped_tool_outcome` — only
  migrated/typed tool handlers actually run under the durable Runtime
  (agent_tools.py:4123 docstring).
- `TaskCompletionGate` fails open on internal errors (verification.py
  637-645) — a completion-gate risk to flag, not a chain break.
- Supervision tasks are storage-only; no reminder scheduler engine exists.
- No independent Review-agent loop exists yet (out of scope here, see
  Phase 1 audit card).

## The chain, step by step (file -> function -> code path)

### 1. User request (real entry points)

There is no single "agent invocation API". The runtime is command-driven.
Real entry points are ingress adapters that produce durable commands:

| Ingress | Path | Function |
|---|---|---|
| Web chat (primary) | `backend/app/api/websocket.py:231` | `websocket_chat` -> `message_loop` (:512) -> `_accept_client_message` (:685) -> `_enqueue_runtime_chat` (:851) -> `_attach_runtime_run` (:594) |
| Task execution | `backend/app/services/task_executor.py:43` | `enqueue_task_runtime()` builds `StartRunCommand(source_type="task", source_execution_id=f"task:{task.id}", run_kind="background")` — **superseded by Phase 2E**: the Execute path now routes through `TaskExecutionService` (`app/services/task_execution_service.py`, gate P1–P8 + R1–R5 attempt keying; see `docs/PHASE_2E_AGENT_ASSIGNMENT_SPEC_V1.md` §2/§4/§5); the legacy auto-enqueue/trigger paths still call `enqueue_task_runtime` directly with `attempt_id=None` (byte-identical stable key) |
| Channel bots (feishu/slack/dingtalk/wecom/teams/whatsapp/webhooks) | `backend/app/api/*.py` | each calls `enqueue_chat_runtime(...)` with its `source_channel` |
| Triggers / heartbeat / A2A | `backend/app/services/trigger_runtime/`, `a2a_runtime.py` | enqueue through `RuntimeCommandIntake` |

The chat ingress handler `backend/app/api/websocket.py:_enqueue_runtime_chat`
(:851) revalidates scope, opens one transaction, and calls:

```
enqueue_chat_runtime(db, agent, user, session, model, content, ...)
    backend/app/services/agent_runtime/chat_intake.py:479
```

### 2. Task/Run + Command creation (durable intake)

`chat_intake.py:479 enqueue_chat_runtime`:
1. Gate: `decide_runtime_v2(agent_id, source_type, settings)` (:509).
   If `use_v2` is False it returns `None` and callers MUST fail closed
   — there is no legacy execution fallback (:504-515).
2. Persists the `ChatMessage` atomically (`_persist_user_message`, :419).
3. For a new run: `adapter.start_run(StartRunCommand(...))` (:694).
   For a waiting run: `adapter.resume_run(ResumeRunCommand(...))` (:640).

`RuntimeCommandIntake.start_run`
(`backend/app/services/agent_runtime/adapter.py:245`):
- rollout decision + graph identity + model resolution + model_turn_limit
  validation (:256-300);
- `register_run_with_start(...)` (`persistence.py:321`) — in the caller's
  transaction, creates:
  - `AgentRun` row (runtime_thread_id, lane, delivery status, goal,
    graph name/version, model, source identity),
  - `AgentRunCommand(command_type="start", status="pending")`,
  - `AgentRunEvent(event_type="run_created", status="queued")`,
- idempotent source-retry via `_resolve_source_retry` (`persistence.py:253`)
  + unique index `uq_agent_runs_source_execution`;
- returns a `RunHandle` WITHOUT committing — the ingress owns the commit
  boundary (`adapter.py:230` docstring: "without committing").

So after step 2, the system holds: AgentRun (queued) + pending start
Command + run_created event. Nothing has executed yet.

### 3. Agent invocation / dispatch (the worker)

Started at app startup (`backend/app/main.py:331-335`, when the worker
role is enabled): `running_runtime_worker_context(...)`
(`worker_service.py:597`) builds all components and spawns daemons.

`RuntimeCommandDaemon.run` (`worker_service.py:390`) polls
`RuntimeCommandWorker.run_once` (`command_worker.py:928`):
1. `_claim()` (:366) -> `claim_next_command` (`persistence.py:635`) picks a
   pending `AgentRunCommand` (scheduling lane / position aware).
2. `_load_run()` (:379) loads the locked `AgentRun`.
3. `run_with_thread_lock(lock_engine, run.thread_id, ...)` (:957) — a
   PostgreSQL advisory lock per runtime thread serializes invocations.
4. `_process_locked()` (:721):
   - reads the checkpoint for this exact command
     (`LangGraphRuntimeDriver.read_for_command`, `langgraph_driver.py:358`);
   - if already applied/waiting/terminal -> `_mark_applied` + product sync;
   - otherwise `command_executor.execute(...)` =
     `LangGraphRuntimeDriver.execute` (`langgraph_driver.py:395`).

`execute()` per command type:
- `start`: `RuntimeInputSnapshotFactory.capture` (`langgraph_driver.py:88`)
  -> `ContextBuilder.capture_run_inputs` (`context_builder.py`) freezes the
  immutable run inputs (agent context, history cutoff, tool set, related
  run summaries) on the advisory-lock connection;
  builds `initial_state` = snapshots + first user message + lifecycle
  `{status: running, next_route: compact|model}` (:437-451);
  `graph.compiled.ainvoke(initial_state, config, context, durability="sync")`
  (:452).
- `resume`: validates the waiting checkpoint + correlation
  (`_resume_value`, :216), then `ainvoke(Command(resume=...))` (:474).
- `cancel`: control-plane only; preserves last checkpoint and is settled
  by the worker (:482-486, `command_worker.py` `_process_locked` cancel
  branch).

The graph itself is `build_agent_runtime_graph`
(`graph.py:241`): deterministic LangGraph StateGraph with nodes
control_guard -> {compact, model, tool, verify, wait, terminal}, a
PostgreSQL checkpointer (`checkpointer.py`), and a second topology
`_group_planning` resolved per-run by `RuntimeGraphRegistry.resolve`
(`langgraph_driver.py:72`).

### 4. Context assembly

- Per-invocation context: `_runtime_context` (`langgraph_driver.py:189`)
  builds `RuntimeContext` (tenant/run/command id, goal, model_id,
  agent_id, session_id, run_kind, parent/root run ids, turn limit,
  actor ids).
- Node routing: `route_after_control` (`graph.py:229`) routes exclusively
  from authoritative `lifecycle.next_route` + `status` in the checkpoint,
  rejecting unknown routes/statuses with `RuntimeGraphContractError`.
- Model-side context: `RuntimeModelStepService.complete_once`
  (`model_step_service.py:1997`) assembles the full prompt:
  static+dynamic prompts from the agent prompt builder, group instructions,
  active-skill prompt, frozen input snapshots + runtime thread messages
  (`_prepare_messages`), and the tool list
  (`_with_runtime_tools`, :418 + application tools filtered by vision
  capability, :440).

### 5. Model call

`model_step_service.py:1997 complete_once`
-> `_call_prepared_with_retry` (:1895)
-> `complete_llm_once` (`backend/app/services/llm/single_step.py:123`)
-> `create_llm_client` (`backend/app/services/llm/client.py:2572`) —
provider-independent factory: `AnthropicClient` (:1967),
`OpenAICompatibleClient` (:570), `OpenAIResponsesClient`, `GeminiClient`.
No provider is hardcoded into the Runtime; the `LLMModel` row's
provider/key/base_url drive the factory. Failover + retry classification
live in `llm/failover.py` and are consumed by `complete_once`
(`failed_over_from` branch). Streaming visible deltas are gated by
`_VisibleDeltaGate` (`single_step.py:34`).

Result shapes: tool-call proposals are normalized into
`ModelStepResult` (`node_executor.py:70`); the model node records
`pending_tool_calls` into the lifecycle checkpoint (node_executor `_model`
:662-918).

### 6. Tool execution

`DeterministicRuntimeNodeExecutor._tool` (`node_executor.py:920`)
-> `RuntimeToolStepService.execute_pending`
(`tool_step_service.py:1963`):
1. Validates every call against the frozen allowed tool set + repair
   budgets; reserves an `AgentToolExecution` row (`_reserve` :1058) with a
   lease owner and side-effect classification (`tool_execution.py`).
2. Executes the handler in a task with lease renewal, cancellation token,
   and deadline (`_execute_application_with_controls` :1523; dispatch at
   :1603 `self._tool_executor(...)`).
   The default executor is
   `execute_builtin_tool_outcome` (`backend/app/services/agent_tools.py:4123`);
   MCP-bound tools go through `execution_binding` (`tool_contracts.py`).
   Untyped (unmigrated) handlers are rejected — see caveats.
3. Async tools park the run in `waiting_external` and are polled by
   `AsyncToolPollScheduler` / `AsyncToolPollDaemon`
   (`async_tool_poll.py`, `worker_service.py:450`).

### 7. Result saving / persistence

- Tool outcomes: `_settle_outcome` (`tool_step_service.py:1146`)
  normalizes to `ToolExecutionOutcome` (`tool_execution.py:695`),
  archives private binaries through `tool_result_store.py`
  (`RuntimeToolResultStore.write_binary`, receipts with content hash),
  and the settled result becomes a tool message appended to the runtime
  thread state.
- Every graph node commits through the PostgreSQL checkpointer
  (`graph.py` compile with checkpointer; `durability="sync"` in the driver
  invocations) — messages + lifecycle are durable per step.
- Event publication: `RuntimeCheckpointSideEffects.handle`
  (`checkpoint_side_effects.py`, event assembly ~:600-660) publishes
  `AgentRunEvent` rows (`run_completed` / `run_failed` /
  `run_cancelled`, waiting/resumed events) ONLY after the authoritative
  checkpoint commits — state publication at the commit point.
- Answer delivery to the user's channel: `ChannelDeliveryWorker`
  (`worker_service.py:450` + `channel_delivery.py` / `delivery.py` /
  `answer_stream.py`) picks up committed delivery work; visible chat
  deltas already streamed via `chat_stream.py`.

### 8. Run completion

- Terminal state: `terminal` graph node guards lifecycle
  (`graph.py:173-175` — terminal status must be completed/failed/cancelled
  and route to END).
- Worker settlement: `mark_command_applied` (`persistence.py:763`) stores
  the command receipt against the checkpoint; retries reconcile via
  `read_for_command` (idempotent, :358).
- Terminal handlers: `checkpoint_side_effects.py` terminal handler chain
  (`worker_service.py:304-319`): `TaskRuntimeCompletionHandler`
  (`task_completion.py:56` — task done/pending + terminal TaskLog),
  `SessionContextCompletionHandler`, `TriggerRuntimeCompletionHandler`,
  `HeartbeatRuntimeCompletionHandler`, `OnboardingRuntimeCompletionHandler`,
  `A2ARuntimeCompletionHandler`, `SchedulingLaneCompletionHandler`.
- Channel delivery then carries the final answer to the originating
  channel; products reconciled by `RuntimeProductReconciler`
  (`product_reconciler.py`).

## Chain status

Complete. Single deterministic spine:
ingress adapters -> durable command inbox -> advisory-locked worker ->
LangGraph (compact/model/tool/verify/wait/terminal) -> provider-abstract
model calls -> leased, settled tool executions -> checkpoint-committed
events + terminal handlers -> channel delivery.

Phase 2E (Agent Assignment & Execution Semantics V1) adds ONE owning
service on the Task ingress — `TaskExecutionService`
(`backend/app/services/task_execution_service.py`,
`docs/PHASE_2E_AGENT_ASSIGNMENT_SPEC_V1.md`): the fail-closed P1–P8 gate,
R1–R5 idempotency/attempt keying, the §6.2 derived-state projection, and
the intake-boundary AuditLog. Everything from `enqueue_task_runtime`
downward is untouched; legacy auto-enqueue/trigger call sites keep
`attempt_id=None` / `actor_user_id=None` (byte-identical behavior).

Broken/missing (outside the main spine, for the Phase 1 report):
- Untyped legacy tool handlers: rejected, not executed (by design,
  partial migration).
- Supervision-task scheduler: storage + manual trigger only.
- Independent Review -> Rework loop: does not exist yet.
- Project entity / Project Intake: does not exist yet.
- `TaskCompletionGate` fail-open behavior is a risk, not a gap.
