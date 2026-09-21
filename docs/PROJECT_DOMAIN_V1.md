# PROJECT_DOMAIN_V1 — Project 概念地图与实体边界（Phase 2A）

状态：DRAFT（设计稿，未实现、无数据库、无代码变更）
基线：Clawith @45fc701c（本仓 834d621）
贡献分工：本文由多张卡共同构建。本文档当前包含 **t_e023caf7 的贡献：概念地图 §1–§5 与实体关系决策 §6–§7**。
后续卡片补充：Intake 流程 / 生命周期状态 / 最小字段（t_1f24e7ca），正式整合与证据复核（t_d222b4f8），一致性审查（t_5b1293ab）。

> 证据约定：每条涉及 Clawith 现状的陈述标注 `FACT`（附 文件:行 证据）或 `DESIGN PROPOSAL`（本文的设计决定，尚无代码）。

---

## 1. Project 是什么（业务定义，非程序员可读）

**Project = 公司正式接手的一件完整事情：有明确的来源、交付目标，以及"做完了"的判定标准。**

类比：真实软件公司接到"修复企业官网登录问题"这一单——这一单就是 Project。
它不是代码（代码是 Repository），不是干活的过程（干活是 Execution），
也不是某个员工的工位（工位是 Workspace）。它是**公司对外承担责任的那个业务对象**：
所有相关任务、交付物、证据、协作，最终都要能归属到它身上。

### 为什么系统需要 Project（存在理由）

- FACT：今天系统里最小的"事情"单位是 Task，而 Task 只挂在单个 Agent 上
  （`backend/app/models/task.py:23`，`agent_id` 为必需外键，无任何项目归属字段）。
- FACT：全仓不存在 Project 实体、不存在项目级 git/仓库接入
  （`docs/PHASE1_CLAWITH_CAPABILITY_AUDIT.md` §8，全仓确认 MISSING）。
- 后果：一个真实项目进来后，公司"不认识"它——只能手工把文件塞进某个员工工作区
  （`docs/PROJECT_TAKEOVER_MODE.md` §一）。
- 设计理由：Project 是"接管真实项目"这条链路的第一等公民。没有它，
  Intake 没有落点、任务没有项目归属、成果证据无法按项目聚合。
  它是业务层的最小问责单位，也是把现有执行底座（Agent/Task/Run/Workspace）
  串成"公司级"流水线的挂钩。

## 2. Project 不是什么（负面空间，防混淆）

Project **不是**以下任何概念。这是本文最重要的防返工约定：

| 容易犯的混淆 | 为什么不是 |
|---|---|
| Project ≠ Repository | 仓库是"代码资产在哪、怎么获取"；Project 是"公司负责的那件事"。一个项目可以没有 git 仓库（V1 允许来源为本地文件夹/文档），也可以有多个仓库。`DESIGN PROPOSAL` |
| Project ≠ Workspace | 工作区是**某个员工名下**的干活空间（FACT：存储子树以 agent_id 为前缀，见 §5）。Project 不拥有物理工作区。`DESIGN PROPOSAL` |
| Project ≠ Execution | 执行是一次具体的干活过程（= 一次 AgentRun）。Project 是跨多次执行的业务对象。`DESIGN PROPOSAL` |
| Project ≠ Task | Task 是"指派给一个员工的一件活"；Project 是"公司接的那一单"。V1 不改动 Task 表。`DESIGN PROPOSAL` |
| Project ≠ Agent | Agent 是员工；Project 是事情。员工跨多个项目，项目由多个员工经手。 |
| Project ≠ 分析结果大 JSON | 深度分析结果属于未来的 Knowledge / Analysis Artifact（见 t_1f24e7ca 与整合卡），不塞进 Project 本体。 |

## 3. 四个概念的区分（基于系统实际能力重定义）

| 概念 | 一句话（小白版） | 在系统里是什么 |
|---|---|---|
| **Project** | 公司正在负责的一件完整事情 | 未来新增的业务实体（V1 尚未实现）。它是"那一单"的档案。 |
| **Repository** | 这件事的代码/资产在谁那里、怎么拿到 | 未来新增的资产登记对象（V1 尚未实现）。它描述**来源**（github/gitlab/zip/本地目录…），不绑定某一项目。 |
| **Workspace** | 某个员工真正动手工作的地方 | 已存在：每个 Agent 的存储子树 `{STORAGE_LOCAL_ROOT}/{agent_id}/` + 每次 Run 的临时物料化目录。FACT（见 §5 证据 1/2）。 |
| **Execution** | 员工某一次真正干活的完整过程 | 已存在：一次 AgentRun（含 checkpoint、工具执行、验证、投递）。FACT（`docs/PHASE1_CLAWITH_CAPABILITY_AUDIT.md` §2/§4）。 |

关键区分轴：**Project/Repository 是"事与物"（业务层，未来新增）；
Workspace/Execution 是"人与过程"（运行时层，今天已存在）。**
业务层对象永远不向运行时层塞物理路径或执行状态。

## 4. 关系决议一：Project 与 Repository

### 决议：`DESIGN PROPOSAL` — **1 Project → N Repository（V1 中 N ≥ 0）**

逐条回答根任务 §八 的六个问题：

1. 对应一个 GitHub 仓库？→ 是，N=1。
2. 对应多个仓库？→ 是。真实例子：Vue 前端 + FastAPI 后端 = 两个仓库，属于同一件事。
   若 V1 写成 1:1，遇到真实多仓项目就必须返工拆模型——**这正是根任务警告的"偷偷硬绑定"**。
3. 没有 Git 仓库？→ 是。V1 来源可以是本地文件夹、文档，此时 N=0，Project 依然成立。
4. 来源是 ZIP？→ 是。ZIP 登记为一个 Repository（source_type 不同），不需要新实体。
5. 来源是本地文件夹？→ 同上，N=1（source_type=local_folder）。
6. 来源是文档而非代码？→ 是。"事情"不等于"代码"；文档来源时 Repository 可 N=0，
   文档作为 Intake 物料直接进入项目物料区（Intake 细节归 t_1f24e7ca）。

### 模型形状（V1 概念层，暂不建表）

```text
Project
 ├─ Repository #1   (source_type: github, clone_url, default_branch, 获取方式)
 ├─ Repository #2   (source_type: local_folder, path)
 └─ (可为 0 个)
```

- Repository 是**独立实体**：它拥有"在哪、怎么取、怎么验证完整性"这些来源事实。
  Project 只持有对 Repository 的引用集合，**不把 clone_url/branch/commit 字段抄进 Project**。
  （根任务 §十九：独立 Repository 比"Project 直接存 git 字段"合理——来源会迁移
  GitHub→GitLab→ZIP，事实跟着 Repository 走，Project 不动。）
- FACT 依据：当前代码中**不存在任何 Repository/仓库概念**（全仓检索 `Repository`
  仅命中 AGENTS.md 文档文字；无 git 检出接口，`docs/PHASE1_CLAWITH_CAPABILITY_AUDIT.md` §8）。
  所以 Repository 与 Project 都是 Phase 2B 新建物，形状可以一次定对。
- 与现有代码资产的关系：FACT——`execute_code` 工具（`backend/app/services/sandbox/`
  7 个后端）可以在沙箱里手动跑 git 命令，但那是"员工自己动手"，不是系统 Intake。
  Repository 实体未来是 Intake 的登记处，不是 execute_code 的替代品。

## 5. 关系决议二：Project 与 Workspace

### 三个候选（根任务 §九）

- **模型 A**：Project 拥有一个共享 Workspace，多 Agent 都往里写。
- **模型 B**：Agent 各保留自己的 Workspace（现状），项目物料**注入到各参与 Agent 的 Workspace**。
- **模型 C**：Project → Repository → 各 Agent 临时 Workspace（以仓库快照为源头按 Agent 分发）。

### 决议：`DESIGN PROPOSAL` — **当前阶段建议模型 B；C 是 B 的自然演进，不是替代**

理由（全部基于 Clawith 真实机制，不打分，只说明）：

1. **哪种方式最容易直接接入现在 Runtime？** → B。
   FACT 证据 1：Agent 工作区的物理边界是存储子树前缀——`initialize_agent_workspace`
   在 Agent 创建时向 `{agent_id}/…` 写种子文件
   （`backend/app/services/agent_tools.py:1653-1673`）；
   `resolve_agent_visible_path` 把模型可见路径解析到"该 agent 的 workspace 根"
   （`backend/app/services/workspace_paths.py:51-87`），跨 Agent 访问被前缀阻断，
   绝对路径直接拒绝。
   模型 A 要求新增一个"项目共享根"的存储方案 + 新路径解析 + 新修订/锁作用域，
   直接动 Runtime 的隔离假设——**破坏现有 Agent workspace**（根任务问的第二点，A 不合格）。
   B 零改动 Runtime：Intake 把项目物料写进各参与 Agent 的既有子树即可。
2. **哪种不会破坏现有 Agent workspace？** → B（A 会，C 在 B 之上加一层快照分发，
   对现有机制是叠加而非破坏）。
3. **哪种最适合以后多人/多 Agent 协作？** → 演进路径：B（V1，项目物料按 Agent 分发）
   → 协作冲突出现后引入 C 的要素（Repository 快照作为唯一源，各 Agent 工作区是快照的副本/物料），
   而不是跳到 A 的"共享物理空间"——因为 FACT 证据 2：修订表
   `workspace_file_revisions` 已预留 `scope_type IN ('agent','group')`
   （`backend/app/models/workspace.py:33-48`）——**Schema 层已经为"非 Agent 作用域的共享空间"
   留了钩子**（group 作用域 + group_key），未来项目级共享可先走 group scope，
   无需推翻现有 agent scope 机制。
4. **V1 的风险 5 缓解（多 Agent 改同一项目文件）**：B 下各 Agent 是独立副本，
   并发写同一项目文件会产生分歧。V1 的约定（DESIGN PROPOSAL）：
   **同一时刻一个 Project 只有一个活跃执行 Agent（单写者）**；
   多 Agent 并行写同一项目物料是 C 引入快照分发之后才允许打开的场景。
   这是边界约定，不是 A 式共享物理空间。

### 归属关系最终形态（V1）

```text
Project
 ├─ Repository × N          （资产登记，来源事实）
 └─ （不拥有物理 Workspace）
       │
       Intake 分发（未来的服务动作，非实体关系）
       ▼
 Agent 的 Workspace × M      （每个参与 Agent 一份项目物料副本，写入既有 {agent_id}/ 子树）
       │
       每次 Execution = 一次 AgentRun，Run 内临时物料化（TempWorkspace）
```

**Project 与 Workspace 没有外键归属关系**——Workspace 归 Agent 所有（FACT：修订/锁表
均挂 agent_id 外键 + agent_id 前缀存储），Project 只是物料的来源方。
这是与"模型 A（Project 拥有 Workspace）"的本质区别。

## 6. Project 与 Task / Agent 的概念关系（本文档边界内的最小结论）

- Project 未来拥有"项目任务图"，但 **V1 不改动 Task 表**（FACT：Task.agent_id 必需，
  `models/task.py:23`；无父子、无依赖）。三选一（直接挂 Task / 加 Work Item 中间层 /
  Task→Run 两段式）的详细裁定归 t_d222b4f8 整合卡，本文只定边界：
  **Project 层新增的对象不得塞进现有 Task/AgentRun 的字段**，归属通过新增引用表达。
- Project 与 Agent：目标模型是 Project Team/Squad（临时工作组）≠ Department（长期归属）。
  FACT：今天 Department 只是元数据 + 知识可见性（`models/org.py:12`，
  `experience_retrieval.py:91`），Group 有原语（`models/group.py`）但无 Squad 语义。
  接入方式归 t_d222b4f8 整合卡。

## 7. 推荐关系图（V1 概念地图总图）

```text
外部项目（GitHub / 本地 / ZIP / 文档）
        │
        ▼
  Project Intake（V1 流程，详见 t_1f24e7ca / 整合卡）
        │
        ▼
  Project ──────────────────────────────
   │    （公司负责的"那件事"，业务层一等公民）
   ├── Repository × N（≥0）  ← 资产来源登记（github/local/zip/document）
   ├── Project Knowledge（V1 非目标，未来）
   └── 项目任务（未来任务图；V1 不动 Task 表）
                    │
                    ▼
        Agent（员工，现有）── 各自拥有 Workspace（现有，agent 作用域）
                    │          项目物料由 Intake 注入各 Agent 工作区（模型 B）
                    ▼
        Task（现有，Task→Agent）→ Execution = AgentRun（现有，durable）
```

要点：左列（Project/Repository）是 **Phase 2B 新增**；右列（Agent/Task/Run/Workspace）是
**现状复用，字段不动**。两层之间唯一的连接是"物料分发 + 任务归属引用"，
不存在 Project→Workspace 的物理归属，也不存在 Project→git 字段硬绑。

---

## 附录：FACT 证据清单（本文引用的全部代码证据）

1. Agent 工作区前缀隔离 + 种子初始化：
   文件 `backend/app/services/agent_tools.py`，函数 `initialize_agent_workspace`（:1653-1673）——
   Agent 创建时向 `{agent_id}/memory/memory.md`、`{agent_id}/soul.md` 写种子；
   存储键一律以 agent_id 为前缀。
2. 模型可见路径解析到"该 Agent 的 workspace 根"：
   文件 `backend/app/services/workspace_paths.py`，函数 `resolve_agent_visible_path`（:51-87）——
   相对路径解析到 agent workspace；绝对路径拒绝；`enterprise_info` 前缀走企业只读区。
3. 修订/锁的作用域只有 agent 与 group，且 agent 作用域必挂 agent_id 外键：
   文件 `backend/app/models/workspace.py`，`WorkspaceFileRevision`（:28-68）/
   `WorkspaceEditLock`（:71-107）——`scope_type IN ('agent','group')`，
   agent 作用域 `agent_id` 非空。这是未来共享空间的既有 schema 钩子。
4. 每次 Run 的临时物料化（执行级隔离）：
   文件 `backend/app/services/agent_tools.py`，`TempWorkspace`（:1689-1705）与
   `_prepare_temp_workspace`（:1729-1777）——把 agent 存储工作区物料化进 Run 沙箱临时目录，
   结果回刷。
5. 全仓无 Repository / git 检出概念：
   全仓检索 `Repository|git_repo|clone_url|repo_url`（backend/）仅命中 AGENTS.md 文档文字；
   `docs/PHASE1_CLAWITH_CAPABILITY_AUDIT.md` §8 全仓确认 Project/Intake MISSING。
   git 操作只能由 Agent 经 `execute_code` 在沙箱内手动执行（`backend/app/services/sandbox/`，
   7 个后端，`services/sandbox/registry.py`）——属"员工行为"，非系统 Intake。
6. Task 无项目归属：
   文件 `backend/app/models/task.py`（:13-55）——`agent_id` 必需外键（:23），
   无 project/parent/依赖字段。
7. Department 仅元数据：
   文件 `backend/app/models/org.py`（:12 `OrgDepartment`）、
   `backend/app/services/experience_retrieval.py`（:91 部门知识可见性）——
   无调度/派发消费方（`docs/CAPABILITY_CONCEPT_MAP.md` §11 判定 metadata-only）。

*交叉引用：docs/PHASE1_CLAWITH_CAPABILITY_AUDIT.md、docs/CAPABILITY_CONCEPT_MAP.md、
docs/PROJECT_TAKEOVER_MODE.md（Phase 1 归档报告，基线 834d621）。*
