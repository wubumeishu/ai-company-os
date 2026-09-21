# PHASE2A_PROJECT_DESIGN_V1 — 一致性审查记录（aco-reviewer / t_5b1293ab）

- 交叉引用（2026-09-21）：本记录 §2 的 M-1 / M-2 / L-1 三处修正已由收口卡 **t_ba795cc8**（aco-builder，design-only 文档修正）应用进 gate 文档 `PHASE2A_PROJECT_DESIGN_V1.md`；行号引证已对照活体基线 d345f6c 核实。

- 被审对象：`docs/PHASE2A_PROJECT_DESIGN_V1.md`（t_d222b4f8 产出，667 行，基线 d345f6c）
- 审查方式：只读活体源码复核（本 worktree d345f6c）+ 全文逐节比对，不修改被审文档、不 commit。
- 结论：**APPROVE（附 2 Medium + 1 Low 修正项，均不阻塞 2B 门槛，但 2B 卡落地前应修入文档）**

---

## 1. 门槛三项核验（任务 body 要求的三个判定）

### 1.1 Project/Workspace 模型是否破坏现有 AgentRun / Workspace / File 存储机制

**判定：不破坏。** 逐条依据（活体复核）：

| 设计决议 | 现有机制 | 复核结论 |
|---|---|---|
| §B.2 模型 B（物料写入 `{agent_id}/` 子树） | `storage_runtime/utils.py:19-21 agent_storage_prefix`、`agent_tools.py:1653-1673 initialize_agent_workspace`、`workspace_paths.py:51-87 resolve_agent_visible_path` | ✅ 分发走既有 facade（`facade.py:30 get_storage_backend`）+ 既有前缀，零 Runtime 改动，成立 |
| §B.2 "group scope 是预留钩子" | `models/workspace.py:33-48`（`ck_workspace_file_revisions_scope_type`，`scope_type IN ('agent','group')`） | ⚠️ 约束确实存在，**但 group scope 已有活体消费者**（见 M-2）——"预留钩子"表述不准，实际是已在用的契约 |
| §G.1/§G.2 Task 表零改动 | `models/task.py:13-55`：`agent_id` 必需外键，grep `project\|parent\|depend\|child\|work_item` **零命中** | ✅ 设计与现状一致，V1 不动 Task 表可行 |
| §B.2 单写者约定（EXECUTING 同时 1 个活跃执行 Agent） | 现有 per-agent 锁（`workspace_locking.py`，Redis `tenant:{t}:workspace-lock:{agent_id}:...`）与项目级单写者是**两个正交层级** | ✅ 不冲突：项目级单写者是新增业务约定，不要求改现有锁机制；但 2B 实现时须明确单写者判定的权威落点（Project 表状态 + 记录执行 Agent），文档 §C 约定已足够 |

### 1.2 Intake 边界是否渗入 Analysis / Execution

**判定：边界定义清晰，未渗入。** 依据：

- §E.1 四职责（登记/验证/初始化/移交）+ §E.5 负面清单（❌分析、❌拆任务、❌组队、❌执行、❌建工作区、❌git 获取能力本身）逐条与 §G.1/G.2/G.4 的"V1 只划边界"裁定对齐，无矛盾。
- §E.3 关键约定"步骤 4 之前不落 Project 实体"把"验证通过"钉在实体出生前，Intake 退出条件里确实不含任何"理解项目内容"动作。
- §E.4 把三段物理动作（Intake 登记验证 / 物料分发 / TempWorkspace Run 内物化）分开归属，`TempWorkspace`（agent_tools.py:1689-1705）确认为 Run 沙箱既有行为，与 Intake 互不重叠，成立。

### 1.3 7 状态机是否可在 Durable Agent Run 架构内实现、且不产生与 Phase 1 FACT 冲突的即时迁移

**判定：可实现，迁移面正确（纯新增），但有一处状态落点表述需要修正（M-1）。**

- 状态机载体是**新增 Project 表**（§I.2 第 2 条），与 `AgentRun`（`models/agent_run.py:27`，`source_type IN ('chat','trigger','task','a2a','heartbeat')` 等封闭枚举）无交集：2B 首批不建 Project→Run 触发链（归 2B+，§E.1 第 3 段职责表），因此**不需要**扩展 AgentRun 的 source_type 封闭枚举——没有 Phase 1 FACT 冲突。
- 2B 首批迁移 = 两张新表（projects / repositories）+ 迁移脚本，Task 表零改动；对现有数据**纯增量**，无破坏性迁移。可行。
- §C 状态图与 §G.3 原因码的重试落点存在表述矛盾（M-1，见下）——属于文档自洽性问题，不是架构不可实现问题。

---

## 2. Findings

### M-1（Medium）git 来源"停在 BLOCKED"的状态落点与 §C 状态图矛盾

- **位置**：§F.3 github/gitlab 行、§F.4 末段、§H 第 11 行——均写"真实项目会停在 BLOCKED（缺 git 获取资源）"。
- **问题**：按 §C 状态图与状态表，`BLOCKED` 的唯一入边是 `EXECUTING → BLOCKED`（第 6 行状态表："执行中 / 执行阻塞"）。而 git 来源验证失败发生在 **RECEIVED/SOURCES_OK 阶段**（来源验证），此刻项目尚未 INITIALIZED，更未进 EXECUTING。§G.3 规则 4 也写明"验证重试留在 RECEIVED/SOURCES_OK 之前"。
- **风险**：2B 实现者照字面实现会在 RECEIVED 阶段非法跳进 BLOCKED，或误以为"缺 git 能力"是一种 BLOCKED 原因码（而 §G.3 的 5 个封闭码里没有"缺系统能力"这一类）。上游卡风险标注"git-based sources stay BLOCKED until git-fetch lands"若被 2B UI/API 照抄，会放大此错误。
- **建议修复**（二选一，推荐 a）：
  a. 改述为：git 来源在 V1 下**验证不可完成 → 留在 RECEIVED/SOURCES_OK 挂起（等待 2B 首批 git 能力），有界重试超限后按 `SOURCE_UNREACHABLE` 升级为 REJECTED**；"缺 git 能力"是**环境能力缺口**，不是 5 个原因码之一，也不产生 BLOCKED 迁移。
  b. 若确要把"缺资源"前置化，需给 `INITIALIZED → BLOCKED` 增补一条入边并修订 §C 状态表——但这属于设计变更，须重新走整合卡。

### M-2（Medium）§G.2/§B.2 把 group scope 说成"预留钩子"，实际已有活体消费者

- **位置**：§B.2 决议二证据 2（"Schema 层已经为'非 Agent 作用域的共享空间'留了钩子"）、§G.2（"Group 有原语但无 Squad 语义"）。
- **问题**：活体复核发现 `services/workspace_collaboration.py` 已实现完整的 group-scope 修订/锁路径：`record_group_revision`（:336，"without creating a second history table"）、`prepare_group_runtime_revision`（:386，`scope_type='group'` + Tool Ledger execution ID 作 `group_key`）、`finalize_group_runtime_revision`（:485）；`WorkspaceEditLock`（workspace.py:71-107）同样有 `scope_type IN ('agent','group')` + group 分支。
- **为什么重要**：设计裁定本身仍然成立（Squad 复用 Group 原语、不新建 Department/Manager——✅ 与现状一致且更稳了），但"预留钩子"的措辞会让 2B 实现者误判 group scope 是未启用死代码，可能走错路去新建表/新机制。
- **建议修复**：§B.2 证据 2 与 §G.2 现状段改为"既有**在用**契约：`workspace_file_revisions`/`workspace_edit_locks` 的 group scope 已由 `services/workspace_collaboration.py` 的 group 修订路径消费；Squad 复用此既有契约"。

### L-1（Low）附录 C-3 "修订/锁表"引用指向不精确

- `models/workspace.py` 实际含两张表：`WorkspaceFileRevision`（:28，约束 :33-48）与 `WorkspaceEditLock`（:71，同型约束 :82-90）；另有 Redis 后端锁 `services/workspace_locking.py`。附录 C-3 只引了 `models/workspace.py:33-48`，建议补注第二张表与 Redis 锁，使"锁"的证据完整。

---

## 3. FACT 证据复核（本文独立重查，均在本 worktree d345f6c 执行）

| 项 | 结论 |
|---|---|
| C-1 `initialize_agent_workspace`（agent_tools.py:1653-1673） | ✅ |
| C-2 `resolve_agent_visible_path`（workspace_paths.py:51-87） | ✅ |
| C-3 group scope 约束（workspace.py:33-48；同型约束亦在 :82-90） | ✅（见 L-1） |
| C-4 `TempWorkspace`（agent_tools.py:1689-1705） | ✅ |
| C-5 全仓无 git 获取 | ✅ backend/app 内 `clone_url/pull_repo/fetch_repo/git_repo` 零命中 |
| C-6 Department 仅元数据 | ✅ org.py:13 `org_departments`；experience_retrieval.py:91 部门可见性检索 |
| C-7 task.py 无项目/父任务字段 | ✅ grep 零命中 |
| C-8 假 Project 陷阱 | ✅ agent_tools.py:25957 Vercel `/v9/projects`；tool_result_store.py:53 / tool_execution.py:155 `project_id` 为工具结果通用元数据 |
| C-9 storage facade/接口/前缀（facade.py:30、base.py:51-77、utils.py:4-24） | ✅（utils 全文 24 行，非 21） |
| C-10 `_clone_workspace_to_staging` 是 shutil 复制（subprocess_backend.py:808-822） | ✅ |
| 新增：group scope 活体消费者（workspace_collaboration.py:232-500） | ✅（M-2） |
| 新增：`task_executor.py` / `agent_runtime/task_completion.py` 双向链路存在 | ✅ |
| 新增：AgentRun 封闭枚举（agent_run.py:34-51）不被 2B 首批触碰 | ✅ |

---

## 4. 最终判定

- 门槛 §I.1 第 3 条的三项（不破坏现有机制 / Intake 边界清晰 / 状态机可实现且无冲突迁移）**全部通过**。
- M-1 / M-2 / L-1 为文档修正项：**不阻塞 2B 门禁放行**（裁定本身无误，误的只是表述精度），但建议 2B 卡开工前由整合卡把 M-1（git 来源挂起点 = RECEIVED/SOURCES_OK 而非 BLOCKED）与 M-2（group scope 为在用契约）修订进 PHASE2A_PROJECT_DESIGN_V1.md，避免 2B 实现者按错误落点编码。
- 本审查未改被审文档（reviewer 只审不修）；修正动作请由 orchestrator 路由回 t_d222b4f8 所属卡片。
