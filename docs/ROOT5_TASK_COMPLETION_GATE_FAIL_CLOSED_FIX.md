# Root §5 — TaskCompletionGate fail-closed fix (G8 / D-3)

Status: **implemented on this branch** (`wt/t_56fbca2e`, base `8f030792`).

## What this closes

Root §5's "inherited fail-open" clause — surfaced by the Phase 4 audit
(`PHASE_4_AUDIT_REPORT_T15e05452.md`) and by both Phase 4 handoffs as
`flagged_defect_for_separate_fix_task` — named the runtime
`TaskCompletionGate` fail-open asymmetry as a known inherited weakness
(Phase 3 convergence report, residual-risk **G8 / deferred item D-3**:
"the fail-open runtime gate remains a known inherited weakness …
not papered over here"). This card is the dedicated fix that was
deferred, not absorbed into the Phase 4 build.

## The defect (before)

`backend/app/services/agent_runtime/verification.py` —
`TaskCompletionGate._fail_open()` returned
`VerificationResult(outcome="pass", ...)` on every gate-error path:

| gate-error code | before (fail-open) | after (fail-closed) |
|---|---|---|
| `invalid_completion_gate_identity` | `outcome="pass"` | `outcome="fail"` |
| `completion_gate_model_unavailable` | `outcome="pass"` | `outcome="fail"` |
| `completion_gate_call_failed` | `outcome="pass"` | `outcome="fail"` |
| `invalid_completion_gate_output` | `outcome="pass"` | `outcome="fail"` |

Consequence: when the semantic completion gate itself errored (model
unavailable, gate-call exception, or unparseable model output), the Run
still settled as `completed` and the Task was marked done — even though
completion had never been verified. The deterministic verifier was
already fail-closed on unverifiable references (`outcome="fail"` +
`details.code` + actionable `reason`, e.g.
`tool_result_store_unavailable`, `tool_reference_unreadable`); only the
semantic gate was fail-open.

## The fix (after)

`_fail_open` is renamed `_fail_closed` and now returns
`outcome="fail"` with the same closed-code detail shape as the
deterministic verifier — `details={"code": "completion_gate_error",
"gate_error_code": <which error>}` — plus an actionable `reason`
naming which gate-error code caused the fail-closed result. Downstream,
`VerificationOutcome="fail"` already routes to
`Run status=failed / next_route=terminal`
(`node_executor.py` verify-node `fail` branch), so the Task is NOT
marked done and the Run terminates. No new outcome value was invented:
`"fail"` is already in the closed `Literal["pass","repair","fail"]` set
and already consumed by that branch.

## Scope check (deliberately bounded)

- Deterministic-verifier fail-closed paths: **unchanged**.
- Artifact/evidence lanes, completion-service lane, delivery lane:
  **untouched**.
- Only the semantic `TaskCompletionGate` error paths + their
  test expectation changed (the one test that had asserted the old
  fail-open `pass` now asserts `fail`; coverage kept, not deleted).

## Verification

- Focused run: `tests/test_agent_runtime_tool_outcome_contract.py`,
  `tests/test_agent_runtime_task_completion.py` (Phase 2C-3 regression),
  `tests/test_agent_runtime_node_executor.py` (exhaustion path) — all
  74 pass.
- `ruff check` on the two touched files: same 8 pre-existing findings as
  base (2× I001 import-sorting + 6× BLE001 blind-`except Exception`),
  zero new findings.
- `pyright` on the two touched files: same 60 pre-existing errors as
  base (rule-code multiset identical: 44 reportArgumentType / 9
  reportIndexIssue / 3 reportOptionalSubscript / 2 reportCallIssue /
  1 reportReturnType / 1 reportTypedDictNotRequiredAccess), zero new
  errors — my diff introduces no new type errors.
