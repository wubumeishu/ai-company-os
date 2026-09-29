# Phase 4 — Artifact / Evidence Domain: V1 Data-Model Design (t_f19aae89)

Task: `t_f19aae89` — "Design V1 Artifact/Evidence Domain from audit findings"
Author: aco-architect
Baseline: main == origin/main `8f030792`, tag `PHASE_3_CLOSED` — designed in worktree `wt/t_f19aae89`
Input (read-only): `PHASE_4_AUDIT_REPORT_T15e05452.md` (t_15e05452, commit `7a8a389a`) —
every V1 decision below cites an audit question (Q1–Q13) and live `file:line` in this tree.
Style/precedent: `docs/architecture/PHASE_3_PLANNING_DOMAIN_BOUNDARY_DESIGN.md` (t_0739e600) —
same classification discipline, boundary-matrix shape, and "no duplicate without justification" rule.

## Classification Discipline

Carried over from Phase 2C–3 docs and the Phase 4 root brief:

- **FACT** — directly observable in this tree (file:line, named constant, migration id).
- **OBSERVATION** — a pattern seen in one or more concrete instances, no universal claim.
- **INFERENCE** — a conclusion drawn from the facts above.
- **UNKNOWN** — not verifiable from this tree; the resolving action is stated.

The design itself is normative (what the system *should* be). Normative prose is
unlabeled; all claims about the *current* system carry a class.

---

## 1. Verdict (up front)

The audit (Q1/Q2/Q4/Q12 #1/#7) found that "artifact" and "evidence" today are
**refs in `AgentToolExecution.result_metadata`** + **private blobs in
`ToolResultStore`** + **a file-revision ledger** + **a published-page table** —
never a record with identity, type, provenance, revision, or a re-verification
hook. No code may be asked to prove "which test run / which commit / which file
revision proves this" (Q2 [OBS], Q3 [INF]).

The minimal V1 is **two new append-only ledger tables in a new model module
`backend/app/models/artifact_evidence.py`**, built *on top of* — never in
parallel with — the frozen foundations:

```
   AgentToolExecution (frozen: status/result_ref/result_metadata)
             │ owns (execution_id FK, the "who produced it" fact)
             ▼
   artifact_records (NEW) ──< optional link >── evidence_records (NEW)
   identity · type · ref    provenance · kind · outcome ·
   · content_hash · seal     re-verifiable
             │                        │
             ▼                        ▼
   EXISTING storage (NO new blob layer):
   ToolResultStore blobs · disk files · PublishedPage ·
   WorkspaceFileRevision ledger · repositories.locator (git facts)
```

Design shape, one line:

> Artifact = **what was produced** (an immutable, content-addressed, provenance-
> stamped record pointing at existing storage).
> Evidence = **what proves it** (an immutable, re-verifiable record binding a
> fact — file revision / git revision / test result / tool result / published
> page / review verdict / structured result — to its source execution/task/agent).
> Execution Result stays `AgentToolExecution` (frozen, untouched). Review and
> Completion are separate designs (cards `t_dbb0c0dd` and the completion lane)
> that *consume* these two tables; they add no storage of their own beyond one
> evidence row per verdict.

Decisions that bound the whole design:

| # | Decision | Authority |
|---|---|---|
| D1 | Two new tables (`artifact_records`, `evidence_records`) — **not** more, **not** fewer. Every other concept in the Root brief (file/test/DB-record kinds, review evidence) is a *closed `kind` value* on these two tables, not a new entity. | audit Q4 [OBS] "foundation exists; reusable record does not"; Q12 #1/#7; Root hard boundary "no artifact without real semantics" |
| D2 | Both tables are **append-only ledgers with a sealed boundary**: rows are mutable while `seal_status='DRAFT'` and immutable after `SEALED` (service-enforced, `WorkspaceFileRevision` / `AgentRunEvent` precedent — both already append-only [FACT]). Rework = new rows; old rows are never overwritten. | audit Q12 #1 (retention/re-verify), Root Phase 4 §4 "do not overwrite history" |
| D3 | **No new blob storage, no new workspace ledger, no new git layer.** Content stays exactly where the audit found it: `ToolResultStore` keys, on-disk workspace files, `PublishedPage`, `WorkspaceFileRevision`, `repositories.locator`. The new tables store *refs + content_hash only*. | audit Q13 avoid-list ("do not duplicate the existing foundations"); Q1 [FACT] |
| D4 | **No state machine.** `kind`, `seal_status`, `outcome` are closed result-code sets (Phase 3 D5 precedent — closed enums, not workflow SMs). The only lifecycle a record owns is DRAFT→SEALED; who may seal it is defined by the consumer (gate/review), not by a new SM. | Root AGENTS.md §2 ("new SM needs independent owner + need"); Phase 3 design D5 |
| D5 | **Provenance is mandatory at creation, fail-closed.** An `artifact_records` row must carry `execution_id` (agent-produced) OR `created_by_user` (human-supplied); an `evidence_records` row must carry a source (`artifact_id` and/or `execution_id` and/or `revision_ref`) plus `created_by`. A row with neither source nor actor is rejected with a named closed code — "no Evidence without provenance" is a Root hard boundary. | audit Q12 #1, Q13 ("do not invent an Artifact/Evidence without provenance"); audit tenant section |
| D6 | All tables are `__tenant_scoped__` (non-nullable `tenant_id`) and are reached only through `TenantScopedBaseDAO`, inheriting the centralized DAO scope-inject (`dao/base.py:140`) — the same scoping the audit found on every tenant-owned table; no new tenant mechanism. | audit Q1/Q4 [FACT] (ToolResultStore keys, result_metadata inheritance); Phase 2C `analysis.py` precedent (Phase 3 design D6) |

---

## 2. Semantic Boundaries (the required 5-way distinction)

The Root Phase 4 brief requires an explicit separation of
Artifact / Evidence / Execution Result / Review / Completion. This design owns
the first three; Review (card `t_dbb0c0dd`) and Completion (completion lane)
are *consumers* and add no parallel storage.

| Concept | Owner in V1 | Authority / notes |
|---|---|---|
| **Execution Result** | `AgentToolExecution` (frozen, `agent_tool_execution.py:32-106`): `status ∈ {started,succeeded,failed,unknown}`, `result_summary`, `result_ref`, `result_metadata` | [FACT] audit Q3. This design does NOT touch it. It is the "did the operation happen" fact; it is never the "what was produced" fact. |
| **Artifact** | NEW `artifact_records` row — a durable, content-addressed record of a produced thing, bound to its source execution | answers "it came from where / who / which revision / can it be re-verified" (Root brief §2) |
| **Evidence** | NEW `evidence_records` row — a re-verifiable assertion about an artifact, a task, or a run: file revision, git revision, test result, tool result, published page, review verdict, structured result | answers "what proves this" (audit Q2 [OBS]: today nothing answers this) |
| **Review** | NOT this card. The review *verdict* persists as one `evidence_records` row with `kind='review'` (see §5 R1). The Review object, its independence rule, and REQUEST_CHANGES semantics are owned by card `t_dbb0c0dd` | audit Q5/Q7 [FACT/OBS]: no review-verdict entity exists; building the entity here would steal that card's state machine |
| **Completion** | NOT this card. The completion decision is a gate outcome; when a gate/review seals a Task, it writes (a) the `artifact`/`evidence` rows that prove it, (b) an `evidence` row with `kind='review'` for the verdict. Task/Project completion-status semantics are owned by the completion lane | audit Q10 [FACT/INF]: Task "done" = Run reported completed; `Project.status='COMPLETED'` is never written. This design makes those decisions *provable* without redefining them here |

**The one-sentence rule downstream cards must keep:**
`AgentToolExecution.result_metadata.artifact_refs/evidence_refs` (frozen,
`verification.py:898-899` audit Q1) remain the **producer-side transient
carrier**; `artifact_records` / `evidence_records` are the **durable authority**.
A ref in metadata points at storage; a ledger row points at a ref *and*
stamps provenance. Neither replaces the other; the gate's re-verification
(`t_dbb0c0dd` / completion lane) reads ledger rows, not raw metadata.

---

## 3. Entity Definitions

Naming follows `models/analysis.py` / `models/planning.py`: snake_case tables,
`Mapped[...]` columns, module-level closed constant tuples,
`__tenant_scoped__ = True`, `created_at` server default. One new model file
`backend/app/models/artifact_evidence.py` holds both tables (single domain
boundary, Phase 3 `planning.py` precedent), plus one new migration (builder
assigns the next id after the f071 chain).

### A1 `ArtifactRecord` — table `artifact_records`

One durable record of a produced thing. **It never stores bytes.** The
`storage_scheme` + `storage_ref` pair points at one of the five existing
storage authorities (D3); `content_hash` makes the record
content-addressed so a re-verification can detect drift or loss.

| Field | Type | Notes |
|---|---|---|
| `id` | UUID PK | stable identity; model ref `artifact://{id}` (§4.1) |
| `tenant_id` | UUID FK non-null, indexed | D6 |
| `project_id` | UUID FK → `projects.id` SET NULL nullable, indexed | project scope; NULL allowed for tool-level artifacts (the back-link exists via `execution → AgentRun → task → project`, so the column is a convenience, never a second authority — matrix row P1) |
| `task_id` | UUID FK → `tasks.id` SET NULL nullable, indexed | "produced by which Task"; NULL when no Task context |
| `execution_id` | UUID FK → `agent_tool_executions.id` SET NULL nullable, indexed | **the primary provenance edge** (D5): which tool execution produced it; NULL only for the `created_by_user` path |
| `agent_id` | UUID FK → `agents.id` SET NULL nullable | "who" when the producer is an agent (mirrors `PublishedPage.agent_id`, `analysis.py` `AnalysisRun.agent_id`) |
| `created_by_user` | UUID FK → `users.id` SET NULL nullable | "who" for human-supplied artifacts; `ck_artifact_records_source`: exactly one of (`execution_id`, `created_by_user`) is non-null |
| `type` | String(40), closed `ARTIFACT_TYPES` (§3.1) | what kind of thing |
| `title` | String(500) non-null | human-readable name (mirror of `PublishedPage.title`, `WorkspaceFileRevision.path`-style) |
| `storage_scheme` | String(40) non-null, closed `STORAGE_SCHEMES` (§3.2) | where the content physically lives — one of the existing authorities |
| `storage_ref` | String(500) non-null | the locator: opaque ref (`tool-result://{execution_id}`), blob key, published-page short id, file path, tar key, or external URL |
| `content_hash` | String(64) nullable | SHA-256 when the producer computed it (mirrors `WorkspaceFileRevision.content_hash`, `PlanningRun.plan_sha256`); NULL only for live URLs whose hash is unknowable at creation |
| `revision_ref` | String(120) nullable, indexed | "which revision": `resolved_rev`/commit sha (git), a `WorkspaceFileRevision` content hash (file), or a DB snapshot tag. The single column answers the Root "对应哪个 revision" question |
| `seal_status` | String(16) non-null default `'DRAFT'`, closed `SEAL_STATUSES` (§3.3) | D2 boundary |
| `sealed_at` | DateTime nullable | set on DRAFT→SEALED; `ck_artifact_records_seal`: SEALED ⇒ `sealed_at IS NOT NULL` |
| `superseded_by` | UUID FK → `artifact_records.id` SET NULL nullable | the *only* row-to-row link a rework ever adds (D2): "this artifact was replaced by that one"; history is never deleted, only linked |
| `created_at` / `updated_at` | DateTime | `updated_at` changes only while DRAFT |

#### 3.1 `ARTIFACT_TYPES` (closed, D1)

```
ARTIFACT_TYPES = ("file", "document", "tool_result", "published_page",
                  "git_snapshot", "test_report", "structured", "db_record")
```

| Value | Maps to (existing authority) | Why it exists in V1 |
|---|---|---|
| `file` | on-disk workspace path + `WorkspaceFileRevision` | audit Q1 [OBS] (b)/(c): the "artifact = workspace file revision" case |
| `document` | on-disk path / `ToolResultStore` text blob | prose/deliverable outputs |
| `tool_result` | `tool-result://{execution_id}` / `tool-result-binary://{execution_id}` (`tool_result_store.py:207-222`) | the largest existing artifact population today; V1 back-references, never copies |
| `published_page` | `PublishedPage.short_id` (`published_page.py:20`) | the one artifact type that already has a durable table — we reference it, not re-store it (matrix R3) |
| `git_snapshot` | `acq_artifact` tar key + `resolved_rev` (`git_acquisition_service.py:1039-1044`) | the source-artifact fact; audit Q3 [FACT] commits are acquisition-only, so V1 records, it does not create |
| `test_report` | `ToolResultStore` blob or on-disk report path | audit Q12 #7: "no persisted/first-class test-result evidence" — the *report file* side; the *results* side is `evidence_records.kind='test_result'` (§5) |
| `structured` | `ToolResultStore` blob / bounded JSON in `result_metadata` | "structured result" from the Root brief; the structured payload stays where it is, the ledger row carries identity+hash |
| `db_record` | a locator string (`table:id` or `schema_ref`) | Root brief "database record" — deliberately the thinnest value: V1 records *which* record, without copying it; a re-verification hook is out of V1 scope (deferred, §8) |

Unknown values fail closed at the service layer (the re-validation pattern from
`PLANNING_RUN_STATUSES`, `analysis.py`).

#### 3.2 `STORAGE_SCHEMES` (closed — the "don't duplicate foundations" guard, D3)

```
STORAGE_SCHEMES = ("tool_result", "blob_key", "workspace_path",
                   "published_page", "git_acq_tar", "external_url")
```

Every scheme is an existing authority the audit cited (Q1 [FACT]). A scheme
value outside this set is rejected — this is the mechanism that enforces
"no second blob store / no parallel storage" (audit Q13 avoid-list).

#### 3.3 `SEAL_STATUSES` (closed, D2/D4)

```
SEAL_STATUSES = ("DRAFT", "SEALED")
```

- `DRAFT`: creator (the producing service, or the user API) may update
  `title` / `content_hash` / `revision_ref` until seal. This is the *only*
  mutable window; the row cannot be deleted in either state (append-only,
  `WorkspaceFileRevision` precedent — it has no delete path today, and neither
  do these tables: DAO exposes no `delete`).
- `SEALED`: immutable. Any "change" is a **new row** with
  `superseded_by` linking back (D2) — this is exactly the
  "old APPROVE never clobbers later changes" guarantee the Root Phase 4 §4
  demands, at the storage level, before the review lane is even built.

### A2 `EvidenceRecord` — table `evidence_records`

One durable, re-verifiable assertion. Evidence never *is* content; it points
at a content authority and records an **outcome** + the binding that makes it
checkable later.

| Field | Type | Notes |
|---|---|---|
| `id` | UUID PK | stable identity; model ref `evidence://{id}` |
| `tenant_id` | UUID FK non-null, indexed | D6 |
| `project_id` / `task_id` | UUID FK SET NULL nullable, indexed | same scoping logic as A1 |
| `artifact_id` | UUID FK → `artifact_records.id` SET NULL nullable | which artifact this evidence speaks about; NULL for execution-level evidence (e.g. a test result with no named artifact) |
| `execution_id` | UUID FK → `agent_tool_executions.id` SET NULL nullable, indexed | the source execution ("which run/test/command produced this proof") |
| `kind` | String(40), closed `EVIDENCE_KINDS` (§5.1) | what kind of proof |
| `outcome` | String(20) non-null, closed `EVIDENCE_OUTCOMES = ("pass", "fail", "inconclusive")` | the recorded verdict at capture time; re-verification later writes a **new** evidence row (§5.2), it never rewrites this one |
| `subject_ref` | String(500) non-null | the locator the verifier re-checks: file path (+ `revision_ref` hash), git sha, `tool-result://` ref, published short id, command string |
| `subject_hash` | String(64) nullable | content hash of the subject at capture time (drift detection on re-verify; mirror of `content_hash` on A1) |
| `revision_ref` | String(120) nullable, indexed | "which revision/commit" (Root brief question); e.g. `resolved_rev` for git evidence |
| `payload` | JSONB nullable, bounded (≤ 32 KiB, service-enforced) | kind-specific structured facts (test counts, verdict text, structured result) — bounded per the backend AGENTS.md complete-operation-bounds rule |
| `created_by_agent` | UUID FK → `agents.id` SET NULL nullable | who captured it (agent reviewer, builder, or system gate) |
| `created_by_user` | UUID FK → `users.id` SET NULL nullable | human-captured evidence |
| `created_at` | DateTime | the *only* timestamp — evidence has no `updated_at`; it is born immutable |

**Provenance check (D5, fail-closed):** `ck_evidence_records_source` —
at least one of (`artifact_id`, `execution_id`, `created_by_agent`,
`created_by_user`) is non-null, AND `subject_ref` is non-empty. Named closed
rejection codes: `EV_NO_SOURCE`, `EV_NO_SUBJECT`, `EV_UNKNOWN_KIND`,
`EV_PAYLOAD_OVERRUN`. "No Evidence without provenance" (audit Q13) is
enforced in the DAO, not in the service.

#### 5.1 `EVIDENCE_KINDS` (closed, D1)

```
EVIDENCE_KINDS = ("file_revision", "git_revision", "test_result",
                  "tool_result", "published_page", "review", "structured")
```

| Value | `subject_ref` carries | Re-verification (V1) | Authority per audit |
|---|---|---|---|
| `file_revision` | workspace path (+ `subject_hash` = `WorkspaceFileRevision.content_hash` of the cited revision) | re-hash current on-disk content; compare `content_hash` | Q1 [FACT] workspace.py:28-65 — the revision ledger is the content authority; evidence only cites it |
| `git_revision` | `repo:sha` (e.g. `acme/web:38e4a414…`) | `sha` is a fact from `repositories.locator.resolved_rev` (`git_acquisition_service.py:1040` audit Q1) — re-verify = confirm the acquisition row still records it; V1 does NOT add an agent-side commit tool (Q3 [FACT], Q12 #8) | acquisition-only by design |
| `test_result` | test command / harness name (+ `payload`: bounded structured facts — counts, exit status, failing test ids, source execution) | re-run is out of V1 scope (no frozen test-runner spine exists — audit Q3 "no persisted test-result model"); re-*check* = resolve the cited `execution_id` + `subject_hash` | Q12 #7: this is the one genuinely new persisted concept, hence its own kind, not a table |
| `tool_result` | `tool-result://{execution_id}` / `tool-result-binary://{execution_id}` | `ToolResultStore.resolve` (`tool_result_store.py:354`) — the exact check the deterministic verifier already does (`verification.py:920-932` audit fail-open section) | Q1 [FACT] |
| `published_page` | `published://{short_id}` | `PublishedPage` row existence + `short_id` match — the `published-page` scheme the deterministic verifier already checks (`verification.py:512-574, 595-617`) | Q1 [FACT] |
| `review` | `evidence://…`/verdict text in `payload` (+ `created_by_agent` = the disjoint reviewer) | n/a for the verdict itself; it is the input the completion lane consumes — **owned by card `t_dbb0c0dd`**, which writes this row type | Q5 [OBS] "no review-verdict entity" — R1 below |
| `structured` | locator string for structured/DB-record evidence (`table:id`, blob key) | resolve + `subject_hash` compare (db_record deep-recheck deferred, §8) | Root brief "structured result / database record" |

#### 5.2 Re-verification semantics (answers the Root "能否重新验证？")

Re-verification is a **pure function of an `evidence_records` row + the cited
storage authority** — it produces a *new* `evidence_records` row (kind
unspecified-carrying the same subject, `payload.reverify_of = <old id>`,
`created_at` later) and never mutates the old one (D2). The V1 resolver is
deliberately the **deterministic** one already frozen in
`backend/app/services/agent_runtime/verification.py:920-1032`
(schemes `workspace`, `published-page`, `imagekit`, `http(s)`, `tool-result`)
— extended by two new refs (§4.1). No LLM step is in re-verification; LLM
involvement belongs to the completion/review lane, not to this domain.

---

## 4. Identity, Refs, and Scoping

### 4.1 Refs (model-visible + gate-visible)

| Ref | Resolves to | Registered where |
|---|---|---|
| `artifact://{artifact_id}` | the `artifact_records` row (identity + storage locator + provenance) | new resolver entry in the deterministic verifier + `ToolResultStore`-adjacent resolver (implementation card; the current scheme switch is `verification.py:595-617`) |
| `evidence://{evidence_id}` | the `evidence_records` row | same resolver extension |

Producers keep emitting the *existing* schemes in `result_metadata`
(`published://`, `tool-result://`, `http(s)://` — frozen, audit Q1/Q2 [FACT]);
the ledger rows reference those same locators. The two new refs exist so a
reviewer/gate can cite "the artifact" and "the proof" by stable id instead of
by a blob key — the thing the audit found missing (Q4 [OBS] "none carries
identity").

### 4.2 The six Root questions, answered per record

| Root question | `artifact_records` answers it via | `evidence_records` answers it via |
|---|---|---|
| 它从哪里产生？(where from) | `storage_scheme` + `storage_ref` (an existing authority, §3.2) | `subject_ref` + kind (same authority list) |
| 由哪个 Project / Task / Execution 产生？ | `project_id` / `task_id` / `execution_id` (+ back-link `execution → run → task`) | `project_id` / `task_id` / `execution_id` / `artifact_id` |
| 对应哪个 revision？ | `revision_ref` | `revision_ref` + `subject_hash` |
| 谁产生？ | `agent_id` XOR `created_by_user` (ck D5) | `created_by_agent` / `created_by_user` |
| 什么时候产生？ | `created_at` (draft) / `sealed_at` (final) | `created_at` (born immutable) |
| 能否重新验证？ | §5.2 resolver over `storage_scheme` | §5.2 resolver over kind (§5.1 table) |

### 4.3 Tenant / project scoping

- `tenant_id` non-null + `__tenant_scoped__` on both tables (D6, matrix P2):
  the centralized `_inject_tenant_scope` (`dao/base.py:140`, audit tenant
  section [FACT]) applies with zero new code.
- `project_id` is **convenience, not authority** (matrix P1): the authoritative
  project link is `execution_id → agent_runs → tasks.project_id`
  (frozen provenance chain, Phase 2D). A `project_id` value that contradicts
  that chain is rejected: `AE_PROJECT_MISMATCH` (service check, fail-closed).

---

## 5. Boundary Matrix (the required 6-area assignment)

Every concern the Root Phase 4 brief lists (file / directory / document / test
result / structured result / database record / review evidence / immutable /
mutable / retention / auditability) is assigned to exactly one owner. "— "
means not owned here; "NEW" means the V1 table owns it.

| Concern | **Execution (frozen)** | **Storage (frozen)** | **Workspace ledger (frozen)** | **Git facts (frozen)** | **Artifact/Evidence (NEW, this card)** | **Review/Completion lane (card `t_dbb0c0dd` + completion lane)** |
|---|---|---|---|---|---|---|
| did the tool op happen | ✓ `AgentToolExecution.status` | — | — | — | cites it (`execution_id`) | consumes it |
| where the bytes live | — | ✓ `ToolResultStore` keys `runtime/tool-results/{tenant}/{run}/{exec}.json/.bin` (`tool_result_store.py:208-224`) | ✓ on-disk files (DB stores history only, `workspace.py` docstring) | ✓ `{agent_id}/.git-acq/{repo_id}/source.tar` | ✓ only `storage_scheme`+`storage_ref` (D3) | — |
| file revision fact | — | — | ✓ `WorkspaceFileRevision` (`path`,`content_hash`,actor) | — | cites it (`file_revision` kind, `revision_ref`=hash) | — |
| commit / revision fact | — | — | — | ✓ `repositories.locator` (`acq_artifact`,`resolved_rev`) (`git_acquisition_service.py:1039-1044`) | cites it (`git_revision` kind, `revision_ref`=sha) | — |
| test-result fact | ✗ (stdout only, audit Q3) | ✗ | ✗ | ✗ | ✓ NEW `evidence_records.kind='test_result'` (+ `artifact_records.type='test_report'` for the report file) | re-runs? (out of V1, §8) |
| "what was produced" identity | ✗ (refs only, audit Q4) | ✗ | ✗ (ledger, not identity) | ✗ | ✓ NEW `artifact_records` (id, type, hash, provenance) | judges it |
| "what proves it" | ✗ (`_completion_evidence` is conversation, audit Q2 [OBS]) | ✗ | ✗ | ✗ | ✓ NEW `evidence_records` | produces `kind='review'` rows (R1) |
| immutable/mutable boundary | ✓ (exec rows terminal after `completed_at`) | ✓ (blobs written once per ref) | ✓ (append-only, no delete) | ✓ | ✓ `SEAL_STATUSES` + no delete + `superseded_by` only link (D2) | never rewrites history (Root §4 requirement satisfied at storage level) |
| retention | — | — | — | — | V1: **no retention policy** — deletion is out of scope, named below (§8 R-defer) | — |
| auditability | ✓ `AgentRunEvent` slots `evidence_added`/`verification_updated` (`agent_run_event.py:33`) | ✓ | ✓ | ✓ | ✓ ledger rows *are* the audit record; new events only if the lane wants them (not forced) | ✓ verdict rows |

Six areas; every concern assigned once. The two "deferred" cells (test
re-run, retention) are named in §8, not silently dropped.

---

## 6. Overlap Flags — new vs reusable (acceptance: "no duplicate without justification")

| New thing | Overlaps with (existing) | Verdict + justification |
|---|---|---|
| `artifact_records` table | `AgentToolExecution.result_metadata` refs; `AgentRunEvent.artifact_refs` | **NEW, justified.** Those are transient carriers ([FACT] audit Q1); neither has identity/type/provenance/hash/re-verify (audit Q4 [OBS]). This table *consumes* their refs, never re-emits them. |
| `artifact_records` (type `tool_result`) | `ToolResultStore` blobs | **REFERENCE, not duplicate.** D3: the table stores the opaque ref + hash; the blob stays at its deterministic key. No second blob layer (audit Q13). |
| `artifact_records` (type `published_page`) | `PublishedPage` | **REFERENCE.** We store `short_id` in `storage_ref`; all page fields (view_count, title) stay on the existing table. |
| `artifact_records` (type `file`) | `WorkspaceFileRevision` | **REFERENCE + cite.** The revision ledger remains the content authority (D3); the artifact row cites it via `revision_ref` = the revision's `content_hash`. No copy of `before/after_content`. |
| `artifact_records` (type `git_snapshot`) | `repositories.locator` JSON | **REFERENCE.** The acquisition JSON stays the git fact (frozen); the artifact row carries `acq_artifact` key + `resolved_rev`. |
| `evidence_records` (kind `file_revision`) | `WorkspaceFileRevision` | **CITE, not copy** (as above). |
| `evidence_records` (kind `tool_result` / `published_page`) | the deterministic verifier's existing scheme checks (`verification.py:595-617, 920-1032`) | **REUSE.** V1 re-verification *is* that resolver + two new ref schemes (§4.1). No parallel verifier (audit Q13: don't build a second mechanism). |
| `evidence_records` (kind `test_result`) | — (audit Q3: nothing persists test results) | **NEW, justified — the only genuinely new persisted fact in V1**, matching Root brief "test result". |
| `evidence_records` (kind `review`) | `AssignmentService.ReviewBinding` (who), `ApprovalRequest` (human approval) | **NEW row, zero new mechanism.** REV-1/2 disjointness (`assignment_service.py:340-410` audit Q5) stays the assignment fact; `ApprovalRequest` stays the human-approval channel (audit Q13: don't add one). Only the *verdict record* is new, and card `t_dbb0c0dd` owns writing it. |
| `SEAL_STATUSES` / `kind` / `outcome` | Phase 3 `PL_*` closed enums | **PATTERN REUSE.** Closed result-code sets, not state machines (D4 = Phase 3 D5). |
| Tenant scoping | `dao/base.py:140` scope-inject | **REUSE.** No new tenant mechanism (matrix P2). |

---

## 7. Invariants

DB-enforced (the new migration):
1. `ck_artifact_records_source` — exactly one of (`execution_id`,
   `created_by_user`) is non-null (D5).
2. `ck_artifact_records_seal` — `seal_status='SEALED'` ⇒ `sealed_at IS NOT NULL`.
3. `ck_evidence_records_source` — at least one of (`artifact_id`,
   `execution_id`, `created_by_agent`, `created_by_user`) non-null (D5).
4. `uq_artifact_records_tenant_ref` — `UNIQUE(tenant_id, storage_scheme,
   storage_ref)` — dedup: the same locator under the same tenant is one record
   (mirror of `uq_agent_runs_source_execution`, `uq_planning_runs_project_revision`).
5. `uq_evidence_records_reverify` — `UNIQUE(tenant_id, kind, subject_ref,
   revision_ref)` when `payload` does *not* carry `reverify_of`; a re-verify
   row (with `reverify_of`) is explicitly excluded from that constraint
   (partial index, `AgentRunEvent` partial-unique precedent
   `uq_agent_run_events_checkpoint_type_non_delivery`).
6. Both tables `__tenant_scoped__`, non-null `tenant_id` (D6).
7. No delete path in the DAO for either table (append-only, D2) — the
   `TenantScopedBaseDAO` subclass is read + insert + DRAFT-update only.

Service-enforced (fail-closed, closed codes):
8. Creation with an unknown `type` / `kind` / `storage_scheme` value →
   `AE_UNKNOWN_TYPE` / `EV_UNKNOWN_KIND` / `AE_UNKNOWN_SCHEME` (closed-set
   re-validation, the `PLANNING_RUN_STATUSES` pattern).
9. `project_id` present but contradicting the `execution → run → task →
   project` chain → `AE_PROJECT_MISMATCH` (§4.3).
10. Any write to a `SEALED` artifact row → rejected (`AE_ALREADY_SEALED`);
    "change it" = insert a new row + `superseded_by`.
11. `evidence_records.payload` > 32 KiB → truncated-or-rejected per the
    owning contract: reject with `EV_PAYLOAD_OVERRUN` (evidence must be
    complete or absent; a half-proof is worse than none — backend AGENTS.md
    complete-operation-bounds rule).
12. `kind='test_result'` ⇒ `execution_id` non-null (a test result must say
    which command execution produced it — `EV_NO_SOURCE` specialization).
13. `kind='review'` ⇒ `created_by_agent` non-null AND the reviewer-agent
    disjointness is validated against the existing `ReviewBinding` REV-1/2
    fact (never re-implemented here — call `assignment_service`, audit Q5) —
    `EV_REVIEW_NOT_INDEPENDENT` when the reviewer is a builder on the same
    WorkPackage. This is the storage-level half of the Root "no self-review"
    guarantee; the state machine half is card `t_dbb0c0dd`'s.

---

## 8. Deferred, Not Dropped (named so downstream cards see them)

- **Test re-execution / test-runner spine.** V1 records test *results*; it
  does not add a tool to run tests or re-run them (§5.2, §5.1
  `test_result` row). If the completion lane later wants "Rework must produce
  genuinely new verification evidence", re-running is a runtime-tool question
  (a new `execute`-class builtin), not an artifact-domain question — own card,
  own review.
- **Retention.** No TTL / archival policy in V1 (matrix, retention row).
  Rows are append-only and cheap; retention is an ops decision waiting on a
  real volume signal, not a design guess (backend AGENTS.md: no speculative
  public choices).
- **`db_record` deep re-verification.** V1 stores the locator + hash only;
  verifying "the DB record still says X" is deferred until a real consumer
  (e.g. a Delivery lane) needs it (Root Phase 4 §6 "don't build external
  publish platforms early" is the same restraint).
- **Second storage / content migration.** Forbidden (D3). If `ToolResultStore`
  keys or the acquisition tar layout ever change, the affected rows are
  back-filled; the ledger never owns bytes.
- **Retention-vs-seal interaction, cross-tenant artifact sharing.**
  UNKNOWN — not verifiable in this tree; no consumer exists. Stays out of V1
  by the "no speculative extension" rule.

---

## 9. "Must NOT duplicate" (carried from the audit Q13 avoid-list, restated for the builder)

- No second Runtime; the ledger is written by *existing* services
  (settlement path / review lane) in their own transactions.
- No second blob store, no second workspace ledger, no second git layer —
  `STORAGE_SCHEMES` (§3.2) is a *closed list of existing authorities*;
  extending it is a reviewable decision, not a code path.
- No re-implementation of the deterministic verifier — reuse
  `verification.py:920-1032` (+ two new ref schemes in one resolver switch).
- No new approval channel — `ApprovalRequest` / `AuditLog` stay the
  human-approval fact (audit Q13).
- No new state machine — D4; `SEAL_STATUSES` is a result-code set whose
  consumer (the sealing gate/review) already exists in the review/completion
  lane.
- No changes to the frozen models: `Task`, `AgentToolExecution`,
  `AgentRunEvent`, `WorkspaceFileRevision`, `PublishedPage`, `repositories`
  are untouched — this card is **additive only** (Phase 3 design §5 rule).
- The flagged defect — `TaskCompletionGate._fail_open`
  (`verification.py:634-642`, audit defect section) — is **out of scope here**
  and must be its own fix task with independent review (Root card §5). It is
  named in the handoff so no card accidentally absorbs it.

---

## 10. Risk / Verification notes

- **Read-only design; no code, migration, or config changed on this card**
  (Phase 3 two-stage contract: design precedes build).
- Fresh-DB vs existing-DB: the new model file + one migration must keep
  `create_all` and the migration in lockstep (the f068/f069 "index-lockstep"
  lesson documented in Phase 3 design §9) — builder tests up **and** down.
- **`uq_artifact_records_tenant_ref` collision risk:** two different executions
  may legitimately emit the *same* `storage_ref` (same blob key, e.g. an
  idempotent re-run). The unique key is `(tenant, scheme, ref)` *without*
  execution — so a second execution citing the same locator returns the
  existing record (idempotent-cite, service code `AE_ALREADY_EXISTS` →
  return existing row, the materialization-idempotency pattern from Phase 3 §4
  `PL_TASK_EXISTS`). This is intended: an artifact *is* its locator;
  provenance of "who cited it" lives on the citing side (review rows /
  execution), not on the record.
- **`test_result` payload bounds:** 32 KiB is enough for counts + failing test
  ids + command; a full pytest XML is an *artifact* (`type='test_report'`,
  stored in `ToolResultStore`), and the evidence row cites it — the
  kind/content split is what keeps both tables honest.
- **UNKNOWN inherited, not resolved here:** whether the live DB contains
  rows that "should" be back-filled as artifacts (audit UNKNOWN: no live-DB
  verification on this card). Back-fill, if ever wanted, is a separate
  data-migration task with its own review — not part of V1 schema.
- Verification for this card: §5 matrix covers all 6 areas; §6 justifies every
  new entity against the "no duplicate without justification" acceptance; §9
  restates the avoid-list. Ready for review by card `t_dbb0c0dd` (review
  lane) and the independent reviewer before the builder card starts.

---

## 11. Handoff to downstream cards

- **`t_dbb0c0dd` (Independent Review & Rework state machine):** build on
  `artifact_records` / `evidence_records` as defined here. Concretely:
  - a Review **reads** `artifact_records` (by `task_id` / `project_id`) and
    its bound `evidence_records` — it must never take the Run's
    `final_answer` as proof (audit Q13 + Root §3 "Reviewer 不得直接信
    Builder 自报");
  - a verdict writes **one** `evidence_records` row with `kind='review'`,
    `outcome ∈ {pass, fail, inconclusive}`, `created_by_agent` = the disjoint
    reviewer (invariant 13 reuses `ReviewBinding` REV-1/2, never re-implements
    it);
  - APPROVE → the lane seals the cited artifact rows (`DRAFT`→`SEALED`,
    invariant 10: sealing is the only mutation allowed, and it is one-way);
  - REQUEST_CHANGES / Rework → a *new* artifact row (superseding via
    `superseded_by`, D2) + new `test_result`/`file_revision` evidence rows;
    the old review row is never touched — the "current valid review" query
    is "the latest `kind='review'` row over the *current* (non-superseded)
    artifact set", which the schema makes answerable without any history
    rewrite;
  - the state machine for Review/Rework owns its own status codes (card
    `t_dbb0c0dd`'s job, D4 kept: this card added none).
- **Completion lane (next batch):** Task/WorkPackage/Project completion =
  "SEALED artifact set + approving `kind='review'` evidence row + dependency
  completion + tenant boundary" — every input is now queryable from these two
  tables; the lane only defines the *decision*, not the storage.
- **Builder card (next batch):** implement `models/artifact_evidence.py`
  (§3 + §5 constants), the two DAOs (append-only, §7 invariants 1–7), the
  resolver extension (§4.1, two new schemes), and one migration (§10
  lockstep). Do NOT touch §9's frozen list. The `TaskCompletionGate`
  fail-open defect is a separate fix task with its own review (audit defect
  section; Root §5) — do not fold it into the build.
- **Delivery lane (card `t_fa30ea5d` scope):** Delivery must be built on
  "SEALED + approved + evidenced" — i.e. it may only reference
  `artifact_records` rows that are `SEALED` and carry an approving
  `kind='review'` evidence row. "Agent says done → Delivery" remains
  structurally impossible because no delivery input exists without a sealed
  artifact.

*Design produced by aco-architect, task t_f19aae89, at main = 8f030792
(tag PHASE_3_CLOSED), tracing every decision to
PHASE_4_AUDIT_REPORT_T15e05452.md Q1–Q13.*
