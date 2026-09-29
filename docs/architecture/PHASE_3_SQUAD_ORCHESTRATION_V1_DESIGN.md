# Phase 3 — Squad / Team Orchestration V1 (constraint design, no org entity)

Task: `t_d6b24a82` — "Design Squad/Team orchestration V1 with resource constraints"
Author: aco-architect
Baseline: main `44651184` (tag `PHASE_2F_CLOSED`) — designed in worktree `wt/t_d6b24a82`

This card **consumes** the sibling design
`docs/architecture/PHASE_3_PLANNING_DOMAIN_BOUNDARY_DESIGN.md`
(`t_0739e600`, commit `39b412af`) and the two read-only inputs it was built on:
- `PHASE_3_EXECUTION_CHAIN_TRACE.md` (`t_5487441c`, commit `e700411b`) — spine +
  §7.2 "must-NOT-duplicate" rules + G4/G8 gaps.
- `PHASE_3_AUDIT_REPORT_T82050064.md` (`t_82050064`, commit `1e29f1e1`) — Q5/Q6/Q9/Q10.

Parent doc §8 explicitly **deferred the squad/team decision to this card** and
bounded its inputs to exactly four things:

> (a) `required_capabilities` on goals, (b) `shared_resources` + `execution_mode`
> on WPs, (c) the existing `Task.agent_id` as the only assignment fact,
> (d) the existing A2A + group-planning entry-Run pattern as the only
> multi-agent coordination. **No org hierarchy is added in this card.**

This document is the **consumer design** of those four inputs: it maps each
WorkPackage to required roles, defines the shared-resource + review-independence
constraint checks, and specifies exactly how those checks feed the Agent
Assignment step (`t_9820b3d3`). It adds **zero new tables, zero new columns,
zero new state machine, zero org hierarchy.**

## Classification Discipline

Carried over from the Phase 2C-2F docs and the Phase 3 root brief:

- **FACT** — directly observable in this tree (file:line, named constant,
  migration id).
- **OBSERVATION** — a pattern seen in one or more concrete instances.
- **INFERENCE** — a conclusion drawn from the facts above.
- **UNKNOWN** — not verifiable from this tree; the resolving action is stated.

The design is normative (what the system *should* be); every claim about the
*current* system carries a class. All live-source re-verification for this card
was done in this tree at `main = 44651184` (listed in §2).

---

## 1. Verdict (up front)

**A first-class `Squad` / `Team` / `Role` entity is NOT introduced in V1.**
The "squad" is a **derived, computed concept** over data that already exists on
the parent design + the frozen Task layer:

```
   "Squad S on work package W"
   ==  WorkPackage W
        + its materialized Task rows (work_package_tasks link, parent P5)
        + their single authoritative Task.agent_id bindings
        + the constraint results computed over W.shared_resources / execution_mode
          / requires_independent_review
```

There is **no new table, no new SM, no org tree.** The four questions the root
brief asks ("which roles / which role takes which task / which tasks parallel /
which must serialize / which share resources / which Agents can take on which /
which need independent review") are each answered by **named, testable
predicates over existing + parent-defined data**, not by a stored hierarchy.

The one genuinely-new artifact this design contributes is a **closed capability
vocabulary + a per-task-slot candidate-Agent list emitted by the planner**
(§3, §4). It is *plan data* (bounded, rides inside `task_scope`), not an
org entity, and it has a real execution consumer (the assignment step, §8) and
a real test consumer (DB-only unit tests, §10).

| # | Decision | Authority |
|---|---|---|
| S1 | **No `Squad`/`Team`/`Role` table.** The squad is derived over WorkPackage + Task + `Task.agent_id` + computed constraint results. | root hard boundary "no display-only hierarchy" + audit Q10 #4/#11 + AGENTS.md §2 (no SM w/o independent owner). |
| S2 | **Roles = a closed capability vocabulary** (`WORK_CAPABILITIES`, §3). Goal-level on `PlanningGoal.required_capabilities` (parent P5); per-task refinement + candidate list ride in `task_scope` (parent P7). | parent P5/P7; audit Q6 (role_description is free-text → untestable). |
| S3 | **Which-Agent resolution is planner-emitted** (`task_scope` slot `candidate_agent_ids`), NOT a structured capability-join on Agent. Deterministic capability matching is a **named deferral** (§11), not a V1 addition. | keeps parent's "6 tables, no more" footprint; D3 makes the planner the resolution point. |
| S4 | **Conflict + review rules are pure predicates** over `WorkPackage` data + the frozen DAG; they fail closed at plan-validation with closed codes and *do not* add a lock layer (runtime Redis locks + DAG remain the enforcers). | parent §6 inv 12; trace §5.2/§6; G4/G8. |
| S5 | **Assignment stays single-fact.** Every materialized task gets exactly one `Task.agent_id`; the "AssignmentPlan" is a **computed artifact** (reported, not a second table). | trace §7.2 rule 4; parent D2. |

---

## 2. Audit evidence (live source, re-verified in this tree at main=44651184)

Every load-bearing claim below was re-grepped in this worktree before the design
was written. This is what makes the "no org entity" verdict evidence-grounded
rather than keyword-matched.

| # | Fact | Evidence (this tree) | Class |
|---|---|---|---|
| A1 | No `Squad`/`Team`/`Role`/`TaskGroup`/`WorkPackage`/`Milestone`/`PlanningRun` model exists | `backend/app/models/` search → **0 matches** for `Squad|WorkTeam|class Team|class Role|TaskGroup|class WorkPackage|class Milestone|class PlanningRun|class PlanningGoal` | FACT |
| A2 | `Task.agent_id` is the single non-nullable assignment fact | `models/task.py:50` `agent_id ... ForeignKey("agents.id"), nullable=False` | FACT |
| A3 | `TaskDependency` is a flat, attribute-free edge (no group/role/milestone tag) | `models/task.py:138` `class TaskDependency` (task_id, depends_on_task_id only) | FACT |
| A4 | `ANALYSIS_PLANNING` provenance slot is reserved | `models/task.py:37` `TASK_CREATED_REASONS = (...,"ANALYSIS_PLANNING")` | FACT |
| A5 | Agent has **no structured capability/role column** — `role_description` is free-text; `AgentTemplate.category` is a display string | `models/agent.py:39` `role_description: Mapped[str] = ... String(500)` | FACT |
| A6 | Task Runs carry **no** `scheduling_lane_key` → two ready tasks in one Project can run in parallel with no cap (G4) | `services/task_executor.py` has **no** `scheduling_lane_key` reference (only `enqueue_task_runtime` @44/188) | FACT |
| A7 | `scheduling_lane_key` does exist on AgentRun (lane primitive available to generalize) | `models/agent_run.py:140` `scheduling_lane_key: Mapped[str \| None] = String(255)` | FACT |
| A8 | Workspace isolation is enforced by **tenant-prefixed Redis SET-NX** locks at the storage boundary | `services/workspace_locking.py:38` `f"tenant:{tenant_id}:workspace-lock:{agent_id}:{path}"`; `:51` `redis.set(..., nx=True)` | FACT |
| A9 | Tenant isolation is enforced at the ORM level (auto-filter on every SELECT of a tenant-scoped model) | `dao/base.py` `do_orm_execute` tenant-injection (trace §5.1) | FACT |
| A10 | Existing multi-agent coordination is A2A `send_message_to_agent` + group-planning entry-Runs (single-agent) | `agent_runtime/a2a_runtime.py`; `planning_scheduler.py` entry-Runs | FACT (trace §3) |

**OBSERVATION:** the only "team"-ish data in the tree is chat-scoped
(`GroupMember.role IN ('manager','member')`, `models/group.py:59-94`) and
Feishu-synced metadata (`OrgDepartment`/`OrgMember`, `models/org.py`) — both with
**zero execution consumers** (audit Q6/Q10 #2/#10). Nothing existing can be
reused at the Project level; a Project-scoped squad would be a genuinely new
construct. The design therefore does not build a new *entity* — it builds a
*derivation* over the parent's Planning tables + the frozen Task layer, and
declares the capability vocabulary that makes the derivation deterministic.

**INFERENCE:** because (A2) forces one agent per task and (A6) means two ready
tasks in a project may run concurrently with no cap, the only safe place to
catch a file/DB/API/workspace collision is **at plan-validation / assignment
time, over the declared `shared_resources` + the DAG**. Left to runtime, the
collision becomes a silent data race (the Redis lock serializes two *writes*
but does not prevent two agents from *editing the same schema*). Hence the
conflict rules are a plan-time gate, not a runtime one (§6).

---

## 3. Role definitions (S2 — closed capability vocabulary, not a Role table)

A "role" in V1 is a **named bundle of capability codes**, not a stored
entity. Two closed, machine-checked artifacts carry the meaning:

- `WORK_CAPABILITIES` — a module-level closed tuple (new constant, no table):

```python
WORK_CAPABILITIES = (
    "code", "frontend", "backend", "db-migration",
    "testing", "security", "review", "docs", "ops",
)
```

- `PlanningGoal.required_capabilities` (parent P5) is a **bounded JSON list
  drawn from `WORK_CAPABILITIES`**; the goal declares *which capabilities* its
  work packages need, e.g. `["backend","db-migration"]`. This is the
  goal-level role input; it is an **input to assignment, never the assignment
  fact** (parent matrix row, trace §7.2 rule 4).

- Each `task_scope` slot (parent P7) may carry a **per-task refinement**
  `required_capabilities` (subset of the goal's) and a **planner-emitted
  `candidate_agent_ids`** (§4). Per-task capability is how "role → task" is
  expressed: a slot tagged `["review"]` is a reviewer slot; `["backend","code"]`
  is a builder slot; `["db-migration"]` is a migration slot.

**Rejected alternative (named):** reusing `Agent.role_description`
(`agent.py:39`, free-text) or `AgentTemplate.category` (display string) as the
role signal. Both are free-text / display-only → a "does Agent X have
capability Y" check against them is **not unit-testable** and is a second,
soft authority for role. The closed `WORK_CAPABILITIES` is the only
testable signal. A *deterministic capability→Agent join* (a structured
`Agent.capabilities` column) is deliberately **deferred** (§11), because V1 can
resolve candidates from the planner (the planner already sees the Agent roster
+ role_descriptions), and adding a column is speculative until a consumer
proves it.

---

## 4. Role-to-task assignment rules (S3 — planner-emitted candidates, single agent_id)

### 4.1 The `task_scope` slot contract (parent P7, extended by this card)

Parent P7 defines `task_scope` as "the materialization intent: task
titles/descriptions + dependency pairs." This card fixes its **entry schema**
so both the translator (parent §4) and the assignment step (`t_9820b3d3`)
consume the same shape. Each slot is a JSON object:

```jsonc
{
  "slot":  "s1",                                  // intra-WP stable id
  "title":  "...",
  "description": "...",
  "kind":    "build",                             // closed: build | review | gate | other
  "required_capabilities": ["backend","code"],     // ⊆ WORK_CAPABILITIES (may be omitted -> inherit goal)
  "candidate_agent_ids": ["<agent-uuid>", ...],    // planner-emitted; non-empty for build/review
  "depends_on_slots": ["s0"],                     // intra-WP dependency pairs (intra)
  "shared_resources": { "files":[], "db":[], "api":[], "workspace":[] }  // per-slot override (optional)
}
```

- `kind='review'` → the slot is a **reviewer task** (§7); its
  `candidate_agent_ids` must be capability-matched to `review` **and** distinct
  from the builder's agent.
- `kind='gate'` → maps to a `gate` `Milestone` (parent P9) that blocks later WPs.
- `depends_on_slots` become `task_dependencies` edges **only through the
  frozen `task_graph_service.add_edge/bulk_add_edges`** (trace §7.2 rule 2) —
  never a second edge table.

### 4.2 Candidate resolution + single-fact assignment

For each materialized task T from slot S:

1. **Candidate set = S.candidate_agent_ids** (planner-emitted, bounded). If
   empty, fall back to the goal's `required_capabilities` match *only if* the
   deferred capability column (§11) exists; in V1 an empty list is a
   fail-closed `PL_NO_CANDIDATE_AGENT`.
2. **Re-validate at assignment time** against the live roster: every chosen
   candidate must exist and be active (`agent_dao.get_active`, same gate as
   `task_execution_service.py:365`) and be in the same tenant (A9/A8 tenant
   scope). Inactive / foreign candidates are dropped; if none remain →
   `PL_NO_CANDIDATE_AGENT`. This reuses the existing active-agent gate — no new
   authority.
3. **Pick exactly one** `agent_id` from the survivors and write it to
   `Task.agent_id` (A2). One task = one agent. The "who executes" fact stays
   single; multi-agent coordination (if any) is by A2A message / a second
   task row, **never** by multi-agent ownership of one task.
4. The chosen set of (task → agent_id) across the plan is the
   **AssignmentPlan artifact** (§8): a *computed* report, not a table.

**INFERENCE:** "Squad S" = "the set of distinct `Task.agent_id` values bound
to the tasks of WPs sharing a planning revision." It is queryable
(`work_package_tasks` join `tasks` join distinct `agent_id`, per parent §10
`get_assignment_candidates_for_work_package`) — no stored team row needed.

---

## 5. Parallel vs serial task grouping (S4 — DAG is authority, WP mode is a hint)

- **Authoritative order = the frozen `task_dependencies` DAG**
  (trace §7.2 rule 2). A task may run only when all direct deps are `done`
  (`task_graph_service.ensure_ready`). No second readiness concept.
- `WorkPackage.execution_mode` (parent P12, closed
  `serial | parallel | parallel_then_serial`) is a **planner hint**:
  - `serial` → the translator emits a *chain* of `depends_on_slots` (s0 → s1 → s2).
  - `parallel` → slots are emitted as **independent siblings** (no edges among
    them). They *may* run concurrently — and that is exactly when a
    shared-resource conflict becomes dangerous (§6).
  - `parallel_then_serial` → an independent front batch, then a serial tail.
- **On conflict, the DAG wins** (parent matrix row "on conflict, the DAG
  wins"): if `execution_mode='parallel'` produces two concurrent slots that
  share a mutator resource, §6 flags it; the fix is to *add a serializing
  edge*, which is a DAG edit through the frozen graph service — not a change
  to `execution_mode`.
- **Must-serialize set** = transitive DAG ancestors **∪** every pair in a
  §6 resource conflict. **May-parallel set** = concurrent-possible slots with
  **no** shared mutator resource and `execution_mode` permitting.
- **No project/task concurrency cap in V1.** `AGENT_RUNTIME_COMMAND_CONCURRENCY`
  (global, `config.py:145`) and the missing per-task lane key (A6/G4) are out
  of scope; they are named as a separate runtime-side deferral (§11), not
  solved by this card.

---

## 6. Shared-resource conflict detection (S4 — the testable predicates)

Inputs: the set of WPs in one planning revision + their `task_scope` slots
(§4.1) + the DAG. Each slot's effective resource set is `slot.shared_resources`
if present, else the WP's `WorkPackage.shared_resources` (parent P10), with the
closed shape `{files:[...], db:[...], api:[...], workspace:[...]}`.

**Mutator vs observer.** A slot mutates a resource iff `kind ∈ {build, gate}`;
`kind='review'` and `kind='other'` are **observers** (read-only) and never
conflict with a mutator over the same resource.

**Concurrent-possible.** Two slots A,B are *concurrent-possible* iff neither is
transitively reachable from the other in the DAG.

Five named predicates, each a pure function of (slots, DAG, resources) — each
unit-testable in DB-only / mock mode:

- **CONF-1 — parallel mutator overlap (FAIL-CLOSED).**
  `∃ resource identifier r (in any of files|db|api|workspace), ∃ two
  concurrent-possible mutator slots A,B, A.r ∋ r ∧ B.r ∋ r`
  ⇒ report `ConflictReport(r, category, [A,B])` with code
  `PL_RESOURCE_CONFLICT`. Resolution before materialization: insert a
  serializing edge (A→B or B→A), or split the resource between the two slots.
  This is the *only* way to make a `parallel` WP safe.
- **CONF-2 — workspace isolation (already enforced; planning flags it).**
  Two tasks of **different** Agents touching the same materialized workspace
  path are *already* serialized at runtime by the tenant-prefixed Redis lock
  (A8: one holder at a time per `(tenant, agent, path)`). Planning does **not**
  add a second lock; it merely reports the pair in the same `ConflictReport`
  stream (parent §6 inv 12: "planning detects/conflicts, runtime enforces").
- **CONF-3 — tenant isolation (invariant, not a check).**
  Every WP in one revision shares `PlanningRun.tenant_id` (parent D6, A9).
  Cross-tenant WPs cannot coexist in one plan **by construction**; this is an
  invariant (asserted, zero branch), not a per-pair check.
- **CONF-4 — single-agent-per-resource within a project (G4 consequence).**
  Because task Runs carry no lane key (A6/G4), two ready tasks of the *same*
  Agent mutating the same resource can only be serialized if the DAG says so.
  Rule: a resource r may have **at most one mutator slot per Agent** among
  the concurrent-possible set; a second mutator slot of the same Agent over r
  ⇒ `PL_RESOURCE_CONFLICT` (forces a distinct Agent or a serializing edge).
- **CONF-5 — global-parallelism bound (advisory, not enforced in V1).**
  If the sum of concurrent-possible mutator slots across a revision exceeds
  `WorkPackage.max_parallel_tasks` (parent P16, nullable hint), report a
  *warning* only — the global cap is a deferred runtime change (§11), so this
  is advisory and never fail-closes V1.

**Result contract (closed codes, fail-closed at plan-validation):**
`PL_RESOURCE_CONFLICT`, `PL_REVIEWER_NOT_INDEPENDENT` (§7), `PL_NO_CANDIDATE_AGENT`
(§4.2). On any of these the materialization/assignment for that WP is refused
with the named code and the planner is re-invoked or a human resolves; nothing
silently proceeds.

---

## 7. Review-independence rules (S4 — reviewer task + DAG edge + agent-distinctness)

Which tasks need an independent (non-builder) reviewer, driven by
`WorkPackage.requires_independent_review` (parent P11):

- **REV-1 — reviewer ≠ executor, structurally.** When a WP has
  `requires_independent_review=true`, its materialized set **must** include a
  `kind='review'` slot whose `candidate_agent_ids` are (a) capability-matched to
  `review` and (b) **disjoint from every builder slot's chosen `agent_id`**
  in that WP. If no disjoint active reviewer exists ⇒ `PL_REVIEWER_NOT_INDEPENDENT`
  (fail-closed). The independence is enforced by the **separation of two Task
  rows + their distinct `Task.agent_id` values** (A2) — *not* by a new
  "independent review" column, not by a new gate table.
- **REV-2 — the reviewer blocks the reviewed work via the DAG.** The reviewer
  task is a *later* task with a `task_dependencies` edge `builder_task →
  reviewer_task` emitted through the frozen graph service (§4.1, trace §7.2
  rule 2). The reviewed work's terminal is therefore gated on the reviewer
  task being `done` — reusing `ensure_ready` readiness, no second readiness
  concept.
- **REV-3 — named gap: the runtime gate is fail-open today.** Phase 2F
  `TaskCompletionGate` is fail-open (audit Q9 #10, trace G8:
  `verification.py` per the trace §8 row). V1 therefore enforces independence
  **at assignment time** (REV-1/REV-2), i.e. the two task rows + distinct
  agent + the blocking edge exist *before* execution; the runtime fail-open
  gate is a known inherited weakness, re-verified in a dedicated card, not
  papered over here.
- **Propagation to scheduling:** the `requires_independent_review` flag + the
  reviewer `task_scope` slot travel together; the assignment step marks the
  reviewer task and emits the blocking edge, so "independent-review flag
  propagates to execution scheduling" is satisfied by the DAG (no new
  scheduler, trace §7.1 rule 5 / §7.2 rule 5).

---

## 8. How the constraints feed the Agent Assignment step (the `t_9820b3d3` contract)

This is the input/output boundary for the builder card that implements the
assignment + concurrency validation:

**Inputs to assignment (per WorkPackage W, all from the parent design + §4.1):**
1. `PlanningGoal.required_capabilities` (role/capability need, parent P5).
2. `W.task_scope` slots: `kind`, `required_capabilities`,
   `candidate_agent_ids`, `depends_on_slots`, per-slot `shared_resources`.
3. `W.execution_mode` + `W.shared_resources` (WP-level resource + parallel
   hint, parent P10/P12).
4. `W.requires_independent_review` (parent P11).

**What assignment computes (pure, deterministic, no new persistence):**
- For each slot, the chosen single `Task.agent_id` from
  re-validated `candidate_agent_ids` (§4.2) → written to the frozen `Task`
  (the *only* assignment fact).
- The **CONF-1..CONF-5** and **REV-1..REV-3** results (§6/§7) as a per-WP
  `ConstraintReport` (list of conflict reports + review reports, each with a
  closed code + the offending slot ids). Any fail-closed code ⇒ the WP's
  materialization is refused and the planner is re-invoked.
- The **AssignmentPlan artifact** = `{work_package_id, slots:[{slot,
  task_id, chosen_agent_id, kind}], constraint_report, review_bindings:[{reviewed_task_id,
  reviewer_task_id, reviewer_agent_id, blocking_edge}]}`. It is returned /
  logged; the *authoritative* assignments live only in `Task.agent_id`, and the
  authoritative order in `task_dependencies`. **No `TaskAssignment` /
  `SquadAssignment` table** (trace §7.2 rule 4).

**Concurrency validation (the builder's hard requirement):**
- "a task may not be assigned to two agents simultaneously" = trivially true
  by A2 (one non-null `agent_id`); the check that matters is **two tasks may
  not both be the active mutator of the same resource for the same Agent
  concurrently** = CONF-4 + CONF-1. The resource-lock "check" is the existing
  tenant-prefixed Redis lock (A8) at execution; planning only *detects*
  (§6), it never adds a second lock layer.
- All CONF/REV codes are closed and unit-testable in DB-only / mock mode
  (no LLM required): the predicate inputs are plain JSON + DAG, so a test can
  build a two-slot plan and assert `PL_RESOURCE_CONFLICT` fires.

---

## 9. Overlap flags — new vs reusable (acceptance: "no duplicate without justification")

| This design introduces | Overlaps with (existing) | Verdict + justification |
|---|---|---|
| `WORK_CAPABILITIES` closed tuple | `Agent.role_description` (free-text), `AgentTemplate.category` (display) | **NEW, justified.** A *closed, testable* capability code. The two existing fields are free-text / display → untestable and would be a second soft authority for role (A5). |
| `task_scope` slot `candidate_agent_ids` + `kind` + per-slot `required_capabilities` | parent P7 `task_scope` | **Extension of the parent's own blob**, not a new table. Bounded plan data; the translator (parent §4) and assignment (§8) both consume it. |
| `ConstraintReport` / `AssignmentPlan` artifact | any assignment table | **Computed, NOT persisted as a table.** The fact stays `Task.agent_id` (A2) + `task_dependencies`; the artifact is a report. No second assignment authority (§8). |
| Squad "S on W" | `OrgDepartment`/`GroupMember`/`AgentAgentRelationship` | **Derived, NOT stored.** Queried as `distinct Task.agent_id` over W's tasks (parent §10). No new org row; those three have zero execution consumers (A1, audit Q6). |
| Capability→Agent *join* (`Agent.capabilities` column) | — | **DEFERRED** (§11). V1 resolves candidates from the planner; a structured column is added only when a deterministic-matching consumer proves it. |

---

## 10. Invariants (DB-enforced / service-enforced, closed codes)

DB-enforced — *none new in this card* (all reuse the parent §6 invariants on
`planning_*` / `work_packages` / `work_package_tasks` + the frozen
`task_dependencies` UNIQUE + `Task.agent_id` non-null). This card adds **no
table and no new DB constraint**, only closed-code predicates run at
plan-validation / assignment time:

Service-enforced (fail-closed, closed codes, per §6/§7/§8):

1. `PL_RESOURCE_CONFLICT` — a concurrent-possible mutator pair shares a
   resource id (CONF-1 / CONF-4).
2. `PL_REVIEWER_NOT_INDEPENDENT` — a `requires_independent_review` WP has no
   active reviewer candidate disjoint from its builders (REV-1).
3. `PL_NO_CANDIDATE_AGENT` — a build/review slot has no live, active,
   same-tenant candidate agent (§4.2).
4. **Invariant (asserted):** all slots in a revision share one tenant id
   (CONF-3); a violation is a data bug, fail-closed.
5. **Advisory only:** CONF-5 global-parallelism bound exceeds
   `WorkPackage.max_parallel_tasks` → warning, never fail-closes V1.

Every predicate is a pure function of `(slots, DAG, resources, candidate lists)`
⇒ **explicit and unit-testable** (acceptance criterion), with no LLM and no
new lock on the test path.

---

## 11. Explicitly deferred (named, not dropped)

- **D-1 — Deterministic capability→Agent matching.** A structured
  `Agent.capabilities` column (closed `WORK_CAPABILITIES` codes) so a
  capability match is a *DB join* instead of planner-emitted
  `candidate_agent_ids`. Add only when a consumer proves the planner-emitted
  list is insufficient (e.g. auto-recompute on Agent changes). A follow-up card.
- **D-2 — Project/task concurrency cap + lane key.** Generalize
  `AgentRun.scheduling_lane_key` (A7) off the group-mention pattern to
  project/task scope, or add a per-project cap (trace G4/G6, parent §8
  deferral). Runtime-side; out of this design's scope.
- **D-3 — Fix the fail-open `TaskCompletionGate` (G8).** Independent
  verification card; V1 enforces review independence at *assignment* time
  (§7) and does not rely on the runtime gate.
- **D-4 — Supervision / deadline scheduling consumers** (G1, audit Q7/Q9 #6/#9):
  reuse `scheduler.py`; unrelated to squad V1.

---

## 12. Risks / Verification notes

- **Read-only design; no code, migration, or config changed on this card.**
  Matches the Phase 3 "audit + design precede build" contract.
- **No second authority.** This card adds *zero* tables and *zero* columns;
  the only new schema-shaped artifact is the `WORK_CAPABILITIES` constant + the
  `task_scope` entry shape (both data, not org entities). The authoritative
  facts stay exactly where the parent/trace pinned them: order in
  `task_dependencies`, assignment in `Task.agent_id`, enforcement in the Redis
  workspace lock + ORM tenant filter.
- **Testability (acceptance):** every rule in §6/§7/§10 is a pure predicate
  over JSON + DAG → DB-only / mock unit tests can assert each closed code
  without a real LLM and without a second lock. The card's "resource conflict
  rules are explicit and testable" criterion is met by CONF-1..CONF-5 being
  named, input-typed, and code-mapped.
- **`task_scope` entry shape is a new contract** both the builder
  (`t_b342df97`, translator) and the assignment card (`t_9820b3d3`) must honor
  — it is pinned in §4.1 and restated in the handoffs (§13) so the two
  builders do not diverge.
- **UNKNOWNs inherited, not resolved here:** G5 (trigger claim-loop wiring)
  and the 4 live-DB UNKNOWNs from the audit are out of scope for a design card.

---

## 13. Handoff to downstream cards

- **`T_9820B3D3` (builder — assignment + concurrency validation):** implement
  §8 exactly. Per WP, resolve §4.2 candidates → write the single
  `Task.agent_id`; compute the §6 CONF-1..5 + §7 REV-1..3 `ConstraintReport`;
  on any fail-closed code refuse the WP and re-invoke the planner. Emit the
  `AssignmentPlan` artifact (report only — **do not create any
  `*Assignment`/`Squad` table**). Concurrency check = CONF-1 + CONF-4
  (no two active mutators of one resource for one Agent). Tests: build
  synthetic two-slot plans in DB-only mode and assert each closed code fires;
  assert a `requires_independent_review` WP with a disjoint reviewer yields a
  `builder_task → reviewer_task` edge. **No new org layer** (acceptance).
- **`T_B342DF97` (builder — data model + migration + DAO):** parent §3/§6/§10
  as before; additionally the **translator must emit the §4.1 `task_scope`
  slot shape** (`slot`, `kind`, `required_capabilities`, `candidate_agent_ids`,
  `depends_on_slots`, per-slot `shared_resources`) into `work_packages.task_scope`
  so both §6 conflict detection and §8 assignment have a stable input. No
  `Agent.capabilities` column in this card (deferred D-1); no `Squad` table.
- **Design-review card (aco-reviewer, recommended):** an independent design
  review of *this* document + the parent §8 boundary before either builder
  starts (root stage 4 "design review precedes build"). Scope: confirm S1–S5
  hold, that §4.1/§8 are self-consistent across the two builders, and that no
  card accidentally introduces an org entity or a second assignment fact.

---

*Design produced by aco-architect, task `t_d6b24a82`, at `main = 44651184`.*
