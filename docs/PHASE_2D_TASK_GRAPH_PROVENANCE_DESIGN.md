# Phase 2D — 最小 Task Graph & Provenance 模型设计（V1）

**任务卡**: t_46f8c7cf（设计卡；持久化实现由 t_650ddd87 依本文落地）
**基线**: `main @ e6237916`（产品基线 `fc233fc1`；单 head = `f068_analysis_persistence`）
**Ground truth**: `docs/PHASE_2D_CODEBASE_AUDIT.md`（t_4b24dd7e @ 4ddba22b）+ 源码
**性质**: 设计文档 + DB schema 草案。本文不含业务实现；实现边界与验证方式见 §11。

---

## 0. 结论（TL;DR）

V1 最小模型 = **一张边表 + 六列 tasks 扩展 + 一个 service 校验/计算层**，不引入新状态值、不引入新状态机、不引入 Workflow Engine：

| 构件 | 形态 |
|---|---|
| Task Graph | 新表 `task_dependencies`（边表，租户作用域，UNIQUE + 无重复边） |
| Provenance | `tasks` 增加 `project_id` / `analysis_run_id` / `finding_id` / `revision_sha` / `created_reason`（`created_by` 已存在，不新增） |
| blocked/ready | **派生计算，不落库**：`ready` = 该 Task 全部前置依赖 `status='done'`；`blocked` = 存在未满足依赖。由 `TaskGraphService` 有界计算，供 API 消费与执行门禁调用 |
| 校验 | 建边/批量建边时 service 层做：自依赖拒绝 + 环检测（有界 DFS，只查入边）+ 同租户同 Project 校验 |
| 执行门禁 | 入队执行前检查 ready；blocked 拒绝入队 + TaskLog 说明。完成后只 recompute + 提示，不自动入队（自动行为归映射/安全卡） |

**非目标**：不做 DAG 调度平台、不做多 Agent 编排、不动 `task_status_enum`（3 值不变）、不在 `tasks` 上加 confirmation/proposal 字段（属 t_3867a0f9 映射与安全卡）、不做 Task→Run 反向 FK（现有 `agent_runs.source_type='task' + source_id` 即 Task→Run 方向的正链，已够用）。

---

## 1. 现状事实（引自审计 + 源码）

| 事实 | 证据 |
|---|---|
| Task 与 Project/Analysis 域零 FK 关联 | 审计 §3.4 / §9.1；`task.py` 全文无 project 字段 |
| Task 无任何依赖字段 | 审计 §C（C_task_graph = NO）；全库无 `task_dependencies` 表 |
| Task 状态机 3 值 `pending/doing/done`，非终态一律回落 `pending` | `task.py:31-35`；`task_completion.py:140-153` |
| `created_by`（user UUID）已存在；无 `created_reason` 闭合码 | `task.py:42`；审计 §I |
| Analysis 域已有 `revision_sha`（typed revision carrier，String(64)）与 `UNIQUE(project_id, revision_sha)` | `analysis.py:119`；f068 迁移 |
| `finding_id` CASCADE：finding 随 run 删除（transient）；knowledge SET NULL（durable） | `analysis.py:155-157,205-207` |
| 001 初始 schema 的 `tasks` 由 `Base.metadata.create_all` 预建 → 新列在 fresh DB 上已由 001 预建（若 model 更新），f068 式幂等 guard 必须覆盖该路径 | f068 迁移注释（L25-28, L158-164） |
| §G.1 约束是 Phase 2A 的 V1 边界（"V1 task graph does not add project fields to Task"）——**Phase 2D 正式解除该约束**，这是本阶段的设计授权 | `project.py:19-21` docstring；Root 卡 §九 |

---

## 2. 设计原则与边界（与 Root 卡对齐）

1. **最小性**：能复用既有结构就不新建。`created_by`、`task_logs`、`agent_runs.source_id` 链均复用；本卡新增物 = 1 表 + 5 列 + 1 枚举 + 1 service + DAO 方法 + API 端点（草案）。
2. **Task ≠ Agent ≠ Run**：本卡不引入 Assignment 实体（Root §八的 Agent Assignment 边界由后续卡设计）；V1 继续用 `tasks.agent_id` 直接绑定。
3. **派生状态不落库**：blocked/ready 是"依赖是否满足"的即时计算，不是新实体、不是新状态机（root AGENTS.md §2：新状态机必须有独立 owner + 需求；这里没有）。`task_status_enum` 保持 3 值，**不加 `blocked` 状态值**——blocked 由 pending + 未满足依赖共同表达。
4. **物理 FK 决策**：DAO 层 AGENTS.md C5 写"无物理 FK"，但 Phase 2B/2C 的 f066-f068 迁移实际使用了物理 `ForeignKeyConstraint`（f068 L149-153 等），且 `Project`/`Repository`/`AnalysisRun` model 也声明了 `ForeignKey(...)`。**f069 沿用 Phase 2C 既成先例**（物理 FK + `create_constraint` 语义 + idempotent guards），不为本阶段单独改宪。此决策在 §10 列为已记录 ADR。
5. **租户安全**：`task_dependencies` 带非空 `tenant_id`（同 f068 全表规范）；建边校验强制两端同租户。`Task.tenant_id` 当前可空（legacy 001 + f060 backfill），端点/Service 对 `tenant_id IS NULL` 的 Task 拒绝建边（fail closed）。
6. **有界数据访问**：图计算限定在"同 tenant + 同 project + 依赖闭包（入边）"内，单次有界 SQL + 应用层拓扑；禁止全租户全表图计算。
7. **与兄弟卡不重叠**：
   - Analysis→Task 转换逻辑（哪些 finding 可直转、可否自动执行、提案/确认落地形态）→ **t_3867a0f9**。本卡只定义 provenance 字段与 `created_reason` 码值，不定义"从 finding 生成 task 的业务规则"。
   - 持久化/迁移实现 → t_650ddd87。
   - 若 t_3867a0f9 为 Task 引入新状态值或 `task_proposals` 表，那是它的 schema 归属；本卡 schema 草案已预留"不冲突"空间（见 §8 风险 R3）。

---

## 3. Task Graph 模型（V1）

### 3.1 图语义

- 节点 = `tasks` 行；边 = `task_dependencies` 行。
- 边语义：`task_id` **depends on** `depends_on_task_id`（箭头指向被依赖者）。
- V1 边**无条件类型**（无 "test-before-deploy" 之类的边属性）——无真实消费者，按最小性不加。
- 依赖只能在**同 project 内**（两端 `project_id` 相同且非空）；跨 project 依赖 V1 不支持（见 §8 R4，留 TODO）。
- 依赖只能挂在 `type='todo'` 的 Task 上：supervision 是周期性督办任务，语义上不进入一次性依赖图。

### 3.2 表草案（f069，SQL 形态）

```sql
CREATE TABLE task_dependencies (
    id                   UUID PRIMARY KEY,
    tenant_id            UUID NOT NULL,
    task_id              UUID NOT NULL,             -- 依赖方（下游）
    depends_on_task_id   UUID NOT NULL,             -- 被依赖方（上游）
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_task_depends_pair UNIQUE (task_id, depends_on_task_id),
    CONSTRAINT ck_task_dep_no_self CHECK (task_id <> depends_on_task_id),
    CONSTRAINT fk_task_dep_tenant   FOREIGN KEY (tenant_id) REFERENCES tenants (id),
    CONSTRAINT fk_task_dep_task     FOREIGN KEY (task_id) REFERENCES tasks (id) ON DELETE CASCADE,
    CONSTRAINT fk_task_dep_depends  FOREIGN KEY (depends_on_task_id) REFERENCES tasks (id) ON DELETE CASCADE
);
CREATE INDEX ix_task_dependencies_task_id ON task_dependencies (task_id);
CREATE INDEX ix_task_dependencies_depends_on_task_id ON task_dependencies (depends_on_task_id);
CREATE INDEX ix_task_dependencies_tenant_id ON task_dependencies (tenant_id);
```

要点：
- **自依赖**：DB `CHECK (task_id <> depends_on_task_id)` + service 双重防线（DB 层兜底脏写，service 层给清晰错误信息）。
- **环**：DB CHECK 无法表达"无环"，环检测只能在 service 层做（§5.2）。`UNIQUE` + `CHECK` 保证重复边与自边在 schema 层 fail closed。
- **CASCADE**：任一端 Task 被删除，边自动消失——图永远不悬挂。
- 租户：端点强制同租户，`tenant_id` 冗余存边表与 f068 表同构（租户过滤自动被 `do_orm_execute` 拾取）。

### 3.3 Model 草案（`backend/app/models/task.py` 增补）

```python
class TaskDependency(Base):
    """One directed edge of the V1 Task Graph: task_id depends on depends_on_task_id.

    Phase 2D minimum model (docs/PHASE_2D_TASK_GRAPH_PROVENANCE_DESIGN.md §3):
    explicit dependencies, self/cycle prevention (service + CHECK), bounded
    blocked/ready computation. No edge attributes, no workflow semantics.
    """

    __tablename__ = "task_dependencies"
    __tenant_scoped__ = True

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    # 依赖方（下游）/ 被依赖方（上游）。两端均 CASCADE：Task 删除即边消失。
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    depends_on_task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("task_id", "depends_on_task_id", name="uq_task_depends_pair"),
        CheckConstraint("task_id <> depends_on_task_id", name="ck_task_dep_no_self"),
    )
```

`tasks` 表增补（§4 Provenance 定义后一并给出 model 草案）。

---

## 4. Provenance 模型（最小字段集 + 论证）

### 4.1 字段清单（全部为 `tasks` 新增列，均可空）

| 列 | 类型 | 可空 | 论证 |
|---|---|---|---|
| `project_id` | UUID FK→projects.id（物理 FK，ondelete CASCADE），索引 | 是 | **必要**。回答"属于哪个 Project"，支撑 "Project X 的所有 Task" 查询；Project 删除时其 Task 一并 CASCADE（业务上 Task 从属于 Project 上下文，与 f068 中 finding CASCADE 于 run 同构）。legacy 手工 Task 无 project 关联 → 留 NULL。 |
| `analysis_run_id` | UUID FK→analysis_runs.id（物理 FK，ondelete SET NULL），索引 | 是 | **必要**。绑定具体一次分析执行；支撑"该 Task 基于哪次分析"与 re-analysis 失效判断。SET NULL 而非 CASCADE：analysis_runs 行删除（或未来 re-analysis 清理）不应摧毁已派生任务——provenance 是"复制不是拥有"（同 f068 knowledge `source_analysis_run_id` SET NULL 先例，`analysis.py:205-207`）。 |
| `finding_id` | UUID FK→analysis_findings.id（物理 FK，ondelete SET NULL），索引 | 是 | **必要**。精确到"哪一条 finding 驱动本 Task"；手工/运行级（无具体 finding）留 NULL。同样 SET NULL：finding 是 transient（随 run CASCADE 死亡），Task 是 durable 执行意图，不得反向销毁。 |
| `revision_sha` | String(64)，索引 | 是 | **必要（denormalized snapshot）**。它本可由 `analysis_run_id → analysis_runs.revision_sha` join 得到，但快照化（不建第三根 FK）有两个真实理由：(a) 回答"基于哪个 Git revision"是最高频追溯查询，免 join；(b) 与 `analysis_run_id`/`finding_id` 的 SET NULL 解耦——即使上游 run 行被删，Task 仍保留"针对哪个 revision"的事实。取值 = 建 Task 时所在 analysis_run.revision_sha（service 层拷贝，保证与 FK 源一致）。 |
| `created_reason` | PG 枚举 `task_created_reason_enum`（闭合 3 值） | 否（server_default='MANUAL'） | **必要**。回答"为什么创建"：`MANUAL`（用户/Agent 直接建）、`ANALYSIS_FINDING`（由某 finding 直转，须有 project_id+analysis_run_id+finding_id 齐备）、`ANALYSIS_PLANNING`（分析上下文产生、无单一 finding，须有 project_id+analysis_run_id，finding_id 可空）。闭合码驱动后续门禁与统计（审计 §I：无闭合码则无法区分自动生成 vs 手工）。 |
| `created_by` | 已存在（users FK） | — | **不新增**。用户身份已由它承载；"系统生成"在 V1 语义上归属触发分析的用户身份，不引入 system-user 伪账号。 |

### 4.2 一致性规则（service 层校验，落库前 fail closed）

```text
created_reason = ANALYSIS_FINDING
  => project_id, analysis_run_id, finding_id, revision_sha 均非空
     且 analysis_run_id.project_id == project_id
     且 finding_id.analysis_run_id == analysis_run_id   （跨表一致性，单条 SQL 校验）
     且 该 run.status = 'AN_COMPLETED'                  （只从已完成 run 派生，审计 §E 门禁）
     且 revision_sha == 该 run.revision_sha
created_reason = ANALYSIS_PLANNING
  => project_id, analysis_run_id, revision_sha 非空；finding_id 可为 NULL
created_reason = MANUAL
  => 分析侧 4 列必须全为 NULL（手工 Task 不携带部分 provenance，避免半溯源脏数据）
task_dependencies 两端 => 同 tenant_id（非空）、同 project_id（非空）、均 type='todo'
```

`ANALYSIS_PLANNING` 是 Root §五"哪些 finding 只能进 planning"的最小落点：planning 产出的 Task 挂到 run 级（finding 可空），不冒充 finding 直转。

### 4.3 完整追溯链的落点（Root §六、审计 §J）

```text
Project ─(1:N)─ AnalysisRun ─(1:N)─ AnalysisFinding
   │                    │                   │
   └── project_id ──────┴─ analysis_run_id ─┴── finding_id   （tasks 5 列 = 一条扁平溯源链）
Task ──(1:N)── AgentRun（agent_runs.source_type='task', source_id=task.id）  ← 现有，不改
AgentRun ── artifact_refs / verdicts（Run 级现状）
```

V1 不建 Task 级 Artifact/Review 表（审计 §J 第 3/4 项）：Task→Run 方向正链已由 `agent_runs.source_id` 提供，Run→Task 反查 = 按 `source_type='task' AND source_id=?` 查即可；Task 级 Review 持久化是后续卡的事。**若未来需要"本 Task 产出的所有 artifact"，先按 Run 聚合，再决定是否上表**（缓存/表引入须有测得的真实需求，AGENTS.md §2）。

### 4.4 `tasks` 列 DDL 草案（f069，SQL 形态）

```sql
ALTER TABLE tasks ADD COLUMN project_id        UUID,
                    ADD COLUMN analysis_run_id UUID,
                    ADD COLUMN finding_id      UUID,
                    ADD COLUMN revision_sha    VARCHAR(64),
                    ADD COLUMN created_reason  task_created_reason_enum
                        NOT NULL DEFAULT 'MANUAL';

CREATE TYPE task_created_reason_enum AS ENUM ('MANUAL','ANALYSIS_FINDING','ANALYSIS_PLANNING');

CREATE INDEX ix_tasks_project_id       ON tasks (project_id) WHERE project_id IS NOT NULL;
CREATE INDEX ix_tasks_analysis_run_id  ON tasks (analysis_run_id) WHERE analysis_run_id IS NOT NULL;
CREATE INDEX ix_tasks_finding_id       ON tasks (finding_id) WHERE finding_id IS NOT NULL;
CREATE INDEX ix_tasks_revision_sha     ON tasks (revision_sha) WHERE revision_sha IS NOT NULL;
CREATE INDEX ix_tasks_created_reason   ON tasks (created_reason);

ALTER TABLE tasks ADD CONSTRAINT fk_tasks_project
    FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE CASCADE;
ALTER TABLE tasks ADD CONSTRAINT fk_tasks_analysis_run
    FOREIGN KEY (analysis_run_id) REFERENCES analysis_runs (id) ON DELETE SET NULL;
ALTER TABLE tasks ADD CONSTRAINT fk_tasks_finding
    FOREIGN KEY (finding_id) REFERENCES analysis_findings (id) ON DELETE SET NULL;
```

（`created_reason` 有默认值 → 现有全部行自动为 MANUAL，无 backfill，符合 DDL-only 规则。）

### 4.5 Model 草案（`tasks` 增补，与 §3.3 同文件）

```python
TASK_CREATED_REASONS = ("MANUAL", "ANALYSIS_FINDING", "ANALYSIS_PLANNING")

# Task 类增补列：
project_id: Mapped[uuid.UUID | None] = mapped_column(
    UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
)
analysis_run_id: Mapped[uuid.UUID | None] = mapped_column(
    UUID(as_uuid=True), ForeignKey("analysis_runs.id", ondelete="SET NULL"), nullable=True, index=True
)
finding_id: Mapped[uuid.UUID | None] = mapped_column(
    UUID(as_uuid=True), ForeignKey("analysis_findings.id", ondelete="SET NULL"), nullable=True, index=True
)
revision_sha: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
created_reason: Mapped[str] = mapped_column(
    Enum(*TASK_CREATED_REASONS, name="task_created_reason_enum", create_constraint=False),
    default="MANUAL",
    nullable=False,
)
```

---

## 5. 校验与 blocked/ready 计算

### 5.1 校验契约（`TaskGraphService`，service 层——DAO 不放业务逻辑）

| 操作 | 前置校验（按序 fail closed） | 拒绝结果 |
|---|---|---|
| `add_edge(t, u, tenant)` | 1) t、u 同租户（非空 tenant_id）；2) t≠u；3) 两端均 `type='todo'`；4) 两端 `project_id` 相同且非空；5) **无环**（§5.2）；6) 边不存在（UNIQUE 兜底） | 400 + 明确 reason 码（SELF / MISMATCH_TENANT / MISMATCH_PROJECT / SUPERVISION_NOT_ALLOWED / CYCLE / EXISTS） |
| `bulk_add_edges(t, [u...], tenant)` | 上述逐条 + 一次性批内环检测（同 §5.2，边集 = 现有入边 ∪ 候选边） | 同上映射；任一条失败整体不写（单事务） |
| `remove_edge(t, u, tenant)` | 边存在 | 404 |
| `is_ready(t)` / `ready_states(tasks, tenant, project)` | 仅计算，无写 | — |

### 5.2 环检测（有界 DFS，只查入边）

插入边 `t ← u`（t 依赖 u）产生环 ⇔ u 已经（传递地）依赖 t，即 **t 在 u 的入边可达集内**。

```text
1. 取 u 的全部直接入边 predecessors(u)（一条有界 SQL，同 tenant 过滤）
2. 从每个 predecessor 出发，沿入边做 DFS，仅跟随 project_id = u.project_id 的节点
   （边只在同 project 内存在，天然有界）
3. 可达集含 t => 拒绝（CYCLE）；否则写入
```

- 复杂度 O(该 project 内依赖边数)，项目级图远小于全表；不做全租户拓扑。
- 校验与写入在同一事务内完成（读后写窗口内的并发写由 `UNIQUE + CHECK + FK` schema 防线兜底；最终一致 = 无环，因任何环的"最后一条边"插入时都会被上述可达性检查拒绝——同租户同 project 并发写最坏情形下需要事务内重复检查，实现细节归 t_650ddd87 单测覆盖）。

### 5.3 blocked/ready 计算（派生，不落库）

```text
ready(t)   := t.status = 'pending' AND t 的所有直接依赖中 status='done' 者
              （依赖集为空 => 平凡 ready）
blocked(t) := t.status = 'pending' AND NOT ready(t)
             （即：存在任一直接依赖 status ∈ {pending, doing}）
```

- **判定只看直接依赖**：上游未 done 则下游连锁未 ready，无需传递闭包；一次 `SELECT depends_on_task_id FROM task_dependencies WHERE task_id = ?` + 一次 `SELECT id, status FROM tasks WHERE id IN (...)` 即可（有界 2 条 SQL）。
- **失败/取消的处理**：审计 §3.2 中 run 失败/取消把 todo 回落 `pending` —— 故"依赖失败"表现为"依赖未 done"，下游继续 blocked，人工可处置（重跑上游 / 移除边）。**不引入"依赖失败则下游终态"的语义**（那是 workflow engine 的行为，V1 不做）。
- `done` 是终态（审计：todo done 无 reopen 路径），依赖一旦 done 不再生变。
- supervision 任务无依赖边（§3.1），`ready` 语义不适用，API 对其返回 `N/A`。

### 5.4 执行门禁（与现有 handoff 的接缝）

现状 handoff（审计 §3.1）：`task_executor.enqueue_task_runtime` 建 AgentRun 并置 `doing`。V1 在入队调用前插入一个门禁（`TaskGraphService.ensure_ready(task)`）：

- blocked → **不建 Run**；`status` 保持 `pending`；追加一条 TaskLog（"⛔ 依赖未满足，暂不可执行：未 done 前置 = [ids]"）。
- ready → 按现有路径入队（行为不变）。
- 门禁调用点选择归实现卡：放在 `create_task` 自动入队分支 + 手工触发路径两处。
- **完成后行为（V1 最小）**：todo 置 done 时（`TaskRuntimeCompletionHandler` 成功路径），recompute 其所有直接下游的 ready 状态并写一条 TaskLog（"▶ 下游 T1,T2 已 ready，可触发执行"）。**只提示，不自动入队**——"分析产出的 Task 是否自动执行/是否需人确认"归 t_3867a0f9 的安全边界决策；本卡把"可执行性"做成门禁 + 查询能力，行为策略留到彼。

---

## 6. Service / DAO 契约（供 API 消费，Root §七"可被真实 API/service 使用"）

```text
app/services/task_graph_service.py  (新增，或并入 task_executor 同域 service —— 归实现卡定)
  add_edge(task_id, depends_on_task_id, tenant_id) -> TaskDependency
  remove_edge(task_id, depends_on_task_id, tenant_id) -> None
  bulk_add_edges(task_id, [depends_on_task_id...], tenant_id) -> list[TaskDependency]
  is_ready(task_id, tenant_id) -> bool
  ready_states(task_ids: list, tenant_id, project_id) -> dict[task_id, "ready"|"blocked"]
  ensure_ready(task) -> raises TaskBlockedError(reason)   # 执行门禁

app/dao/task_dao.py (扩展现有 tasks DAO)
  list_dependencies(task_id, tenant_id) -> [TaskDependency]
  add_dependency / remove_dependency / bulk_add_dependencies  (flush 不 commit)
  graph_read(task_id, tenant_id, project_id) -> 有界子图（入边 + 节点 status）
  provenance_consistency(task) -> 单 SQL 校验 §4.2 跨表规则
```

**API 草案**（挂在现有 tasks 路由族下，`/agents/{agent_id}/tasks` 同域；tenant 取自会话）：

```text
POST   /agents/{agent_id}/tasks/{task_id}/dependencies        { "depends_on_task_ids": [...] }  -> 批量建边（单条 = 长度 1）
DELETE /agents/{agent_id}/tasks/{task_id}/dependencies/{dep_task_id}
GET    /agents/{agent_id}/tasks/{task_id}/graph               -> { "task": ..., "ready": bool|"N/A",
                                                                    "direct_dependencies": [{id, status, ready}...],
                                                                    "blocking": [...] }
```

Task 创建/更新请求体（现有 schema）增加可选字段：
`project_id, analysis_run_id, finding_id, revision_sha, created_reason`（§4 规则校验后落库；`created_reason` 缺省 = MANUAL 且分析侧必须全空）。

> 端点路径前缀最终以实现卡对现有路由的实查为准；语义与载荷不变。

---

## 7. 迁移草案（f069_task_graph_provenance，DDL-only）

- 文件名：`v1_11_5_f069_task_graph_provenance.py`；`revision = "f069_task_graph_provenance"`。
- **down_revision 草案 = `f068_analysis_persistence`（当前单 head）**。若 t_3867a0f9 的 schema 先行 landing 占掉该 slot，则按 alembic AGENTS.md §1.3 用 `alembic merge heads` 出 merge revision，**不得改写已发布迁移的 down_revision**。
- upgrade：guard 式幂等（复制 f068 的 `_existing_tables` / `_present_enum_types` guard 骨架）：
  1. `CREATE TYPE task_created_reason_enum`（若不存在）
  2. `ALTER TABLE tasks ADD COLUMN ...`（逐列 guard：`_existing_columns`）
  3. `CREATE TABLE task_dependencies` + 索引 + 约束（guard 表名）
  4. 列索引 + 3 个物理 FK（guard 约束名）
- downgrade（对称）：drop FK → drop 索引 → drop `task_dependencies` → drop 列 → drop 枚举（仅无列引用时，同 f068 收尾逻辑）。
- **无数据操作**：`created_reason` 走 `server_default='MANUAL'`，现有行零 backfill（DDL-only 规则合规）。
- fresh DB 路径：model 元数据经 001 `create_all` 预建全部新表/列/约束 → guards 全命中 no-op（与 f068 同构，注释写明）。

---

## 8. 风险与记录决策

| # | 风险/决策 | 处置 |
|---|---|---|
| R1 | 并发建边绕过环检测（read-then-write 窗口） | 事务内重复检查 + 实现卡加并发单测（两个会话同时插互逆边）；最坏由"最后一边"的可达性检查兜底 |
| R2 | legacy Task `tenant_id` 为 NULL | 建边两端任一 NULL 即拒绝（fail closed）；§f060 backfill 已覆盖存量，新任务带 tenant |
| R3 | t_3867a0f9 可能引入 Task 新状态值 / `task_proposals` 表，与本卡 schema 同文件（task.py / migrations） | 编排规则：两卡的 schema 改动**同 slot 串行**（本卡先占 f069，映射卡占 f070，或反向但必须单一 down_revision 链）；本卡 schema 对"Task 加确认态"零假设（status 枚举不动） |
| R4 | 跨 project 依赖需求（多 project 协同） | V1 显式不支持；出现真实消费者后再开新卡（最小性规则） |
| R5 | `finding_id` SET NULL：上游 run 被删后溯源断链 | 接受——`revision_sha` 快照 + `project_id` 保留主要事实；finding 本就是 transient（§4.1） |
| R6 | DB CHECK/UNIQUE 只能防自边与重边，环在 service | 已记录：schema 是下限防线，service 是权威门禁；两者必须同存 |
| ADR-1 | 物理 FK（违 DAO AGENTS.md C5 字面） | 以 Phase 2B/2C f066-f068 既成先例为准；不改宪（改宪 = 独立任务） |
| ADR-2 | blocked/ready 派生不落库、不加状态值 | 新状态机必须有独立 owner+需求（root AGENTS.md §2），本卡无此需求；实现成本 = 2 条有界 SQL |
| ADR-3 | 不建 Task→Run 反向 FK | `agent_runs.source_type/source_id` 已提供正向链；反向是查询模式不是事实归属 |
| ADR-4 | 完成后只提示不自动入队 | 自动行为 = 安全策略，归映射/安全卡；本卡提供门禁能力 |

---

## 9. 与 §G.1 约束的关系

`project.py` docstring 的 "§G.1: the V1 task graph does not add project fields to Task" 是 **Phase 2A 阶段的**设计边界。Phase 2D Root 卡 §一/§六/§七 明确授权设计最小 Task Graph + Provenance（含 `project_id` 等字段）。故本设计 = **在 Phase 2D 解除该 V1 约束**，builder 落地时应同步更新 `project.py` docstring 中该句的阶段限定（§G.1 → 标记为 Phase 2A V1 边界，由 Phase 2D 取代），避免文档与代码矛盾（AGENTS.md §2：code / note / commit 三者对齐）。

---

## 10. 实现交接清单（t_650ddd87）

1. Model：`task.py` 增 `TaskDependency` + `Task` 5 列（§3.3/§4.5）。
2. 迁移 f069（§7 草案），`alembic heads` 单 head 验证 + downgrade/upgrade 本地回环。
3. Service：`TaskGraphService`（§6），含 §4.2 一致性规则 + §5.2 环检测 + §5.3 计算 + §5.4 门禁接入 `enqueue_task_runtime` 两处调用点。
4. DAO：`task_dao` 增依赖/图读方法（flush 不 commit；无跨 DAO 调用）。
5. API：§6 三端点 + task 创建/更新 schema 增 5 字段。
6. `project.py` docstring §G.1 句更新（§9）。
7. 测试（§11）。

## 11. 测试矩阵（正例 + 反例，AGENTS.md §4）

| 类 | 用例 |
|---|---|
| 自依赖 | `add_edge(t,t)` → 拒绝 SELF（service）+ 直接 SQL 插入被 CHECK 拒（schema 兜底） |
| 重复边 | 同对二次插入 → UNIQUE 拒 / EXISTS 错误码 |
| 环（2 环） | A→B 已有，插 B→A → 拒绝 CYCLE |
| 环（3+ 环） | A→B→C 已有，插 C→A → 拒绝 |
| 同 tenant 内无环 | 菱形（A←B, A←C, B←D）全部插入成功 |
| 跨租户建边 | 两端 tenant 不同 / 一端 NULL → 拒绝 |
| 跨 project 建边 | 两端 project_id 不同 → 拒绝 |
| supervision 建边 | 任一端 type=supervision → 拒绝 |
| ready 计算 | 无依赖 pending → ready；1/2 依赖 done → blocked；全 done → ready |
| done 不复活 | 依赖置 done 后阻塞解除；上游失败回落 pending → 下游重新 blocked |
| 执行门禁 | blocked 入队 → 不建 Run + TaskLog；ready 入队 → 建 Run（回归现有 handoff） |
| provenance 一致性 | ANALYSIS_FINDING 缺任一分析列 / ANALYSIS_PLANNING 带 finding_id 之外的矛盾组合 / MANUAL 带 analysis_run_id → 全部拒绝 |
| 级联 | 删 project → 其 tasks 与边消失；删 analysis_run → task 的 run 列 SET NULL、task 存活 |
| 并发 | 两会话同时插入互逆依赖，至多单边成功且无环（R1） |
| 回归 | 现有 `/agents/{id}/tasks` CRUD + auto-enqueue 路径行为不变（无 provenance 字段 = 全默认 MANUAL，全部旧用例不破坏） |

## 12. 验证方式

- 设计卡交付：本文 + f069 SQL 草案。
- **已执行冒烟**：`scripts/f069_ddl_smoke.py` 在本地 Postgres 的 scratch DB（`clawith_t46f8c7cf_f069_smoke`，事务回滚 + schema 删除，不触碰产品库）上验证了 §3.2/§4.4 的 DDL 与约束语义，结果 = ALL DDL SMOKE CHECKS PASSED：
  - 合法边插入 / 无依赖 => ready / 全依赖 done => ready / 任一非 done 直接依赖 => blocked（§5.3 派生计算 SQL 形态验证通过）
  - 自边被 `ck_task_dep_no_self` 拒（CheckViolation）
  - 重边被 `uq_task_depends_pair` 拒（UniqueViolation）
  - Task 删除 => 引用它的边全部 CASCADE 消失
  - analysis_run 删除 => task 保留、`analysis_run_id` SET NULL、`revision_sha` 快照保留（§4.1 R5 语义验证通过）
  - project 删除 => 其 tasks 全部 CASCADE 消失
- 实现证据（f069 迁移回环 `alembic upgrade/downgrade`、service 环检测单测、API 契约测试、并发用例 R1）归 t_650ddd87 产出，独立 review 后并入。smoke 脚本作为实现卡的参照保留。
