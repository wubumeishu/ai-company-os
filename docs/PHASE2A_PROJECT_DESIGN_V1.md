# PHASE2A_PROJECT_DESIGN_V1 — Phase 2A 正式设计（Project 基础模型 + Project Intake 边界）

状态：**GATE DRAFT（整合设计稿，设计-only，未实现、无数据库、无代码变更）**
基线：Clawith @45fc701c（本仓 `834d621`；本 worktree HEAD `d345f6c`）
贡献分工：本文由 **t_d222b4f8 整合卡**产出，做四件事：
  1. 整合上游两份源文档 `docs/PROJECT_DOMAIN_V1.md`（t_e023caf7，§1–§7 概念地图 + 实体关系决议）
     与 `docs/PROJECT_INTAKE_V1.md`（t_1f24e7ca，§1–§13 Intake 生命周期 + 最小字段 + 来源矩阵）为一份正式 Phase 2A 设计。
  2. 对两份源文档中**每一条涉及 Clawith 现状的 FACT**，对照 Phase 1 归档报告与**本 worktree 的活体源码**
     （Agent / Task / Workspace / Storage / Git 模块）逐条复核（§附录 C 记录复核结论）。
  3. 裁定两份源文档明确"详细裁定归 t_d222b4f8 整合卡"的开放项：**§17 项目任务图**、**§18 项目团队/小队**、
     **REJECTED 原因码集合 + 重试策略**（§G）。
  4. 回答根任务 §四"为什么现在不写代码"——把"如果现在就建表会卡住的歧义"逐条列出并说明设计如何消解（§H）。

> 证据约定（与两份源文档一致）：凡涉及 Clawith 现状的陈述标注 `FACT`（附 文件:行 证据，且本卡已对照活体源码复核）
> 或 `DESIGN PROPOSAL`（本文/源文档的设计决定，尚无代码）。

本文是 **Phase 2B 实现的门槛（gate）**：只有 §H 列出的所有歧义都已被 §A–§G 的设计消解、且 §G 的三个开放项已裁定，
才允许进入 Phase 2B 的实现卡。

---

## 0. 一句话定位

**Phase 2A = 定义"公司正式接手的一件完整事情（Project）在系统里是什么、边界在哪、生命周期怎么走、
Intake 该做什么不该做什么"，并把所有歧义在设计层消解掉；不写任何代码、不建任何表、不 commit。**

本阶段唯一目标是把"为什么现在不直接写代码"这个问题**变成一份可核验的设计**：
编码者拿到本文后，不应再需要做任何"这个字段放哪 / 这个关系定不定 / 这个状态要不要"的判断。

---

## A. Project 概念与边界（整合自 PROJECT_DOMAIN_V1 §1–§3）

### A.1 Project 是什么（业务定义，非程序员可读）

**Project = 公司正式接手的一件完整事情：有明确的来源、交付目标，以及"做完了"的判定标准。**

类比：真实软件公司接到"修复企业官网登录问题"这一单——这一单就是 Project。
它不是代码（代码是 Repository），不是干活的过程（干活是 Execution），也不是某个员工的工位（工位是 Workspace）。
它是**公司对外承担责任的那个业务对象**：所有相关任务、交付物、证据、协作，最终都要能归属到它身上。

### A.2 为什么系统需要 Project（存在理由）

- `FACT`：今天系统里最小的"事情"单位是 Task，而 Task 只挂在单个 Agent 上
  （`models/task.py:23`，`agent_id` 必需外键、`nullable=False`，无任何项目归属/父任务/依赖字段；本卡 §附录 C-7 已复核）。
- `FACT`：全仓**不存在 Project 域实体**、不存在项目级 git/仓库接入
  （`docs/PHASE1_CLAWITH_CAPABILITY_AUDIT.md` §8 全仓确认 MISSING；本卡 §附录 C-8 已复核——见"假 Project 字段"陷阱说明）。
- 后果：一个真实项目进来后，公司"不认识"它——只能手工把文件塞进某个员工工作区（`docs/PROJECT_TAKEOVER_MODE.md` §一）。
- 设计理由（`DESIGN PROPOSAL`）：Project 是"接管真实项目"这条链路的第一等公民。没有它，
  Intake 没有落点、任务没有项目归属、成果证据无法按项目聚合。它是业务层的最小问责单位，
  也是把现有执行底座（Agent/Task/Run/Workspace）串成"公司级"流水线的挂钩。

### A.3 Project 不是什么（负面空间，防混淆）

| 容易犯的混淆 | 为什么不是 |
|---|---|
| Project ≠ Repository | 仓库是"代码资产在哪、怎么获取"；Project 是"公司负责的那件事"。一个项目可以没有 git 仓库（V1 允许来源为本地文件夹/文档），也可以有多个仓库。`DESIGN PROPOSAL` |
| Project ≠ Workspace | 工作区是**某个员工名下**的干活空间（`FACT`：存储子树以 agent_id 为前缀，见 §B.2 / 附录 C-1/C-2）。Project 不拥有物理工作区。`DESIGN PROPOSAL` |
| Project ≠ Execution | 执行是一次具体的干活过程（= 一次 AgentRun）。Project 是跨多次执行的业务对象。`DESIGN PROPOSAL` |
| Project ≠ Task | Task 是"指派给一个员工的一件活"；Project 是"公司接的那一单"。V1 不改动 Task 表。`DESIGN PROPOSAL` |
| Project ≠ Agent | Agent 是员工；Project 是事情。员工跨多个项目，项目由多个员工经手。 |
| Project ≠ 分析结果大 JSON | 深度分析结果属于未来的 Knowledge / Analysis Artifact（§G.4 / §E），不塞进 Project 本体。 |

### A.4 四个概念的区分（基于系统实际能力重定义）

| 概念 | 一句话（小白版） | 在系统里是什么 |
|---|---|---|
| **Project** | 公司正在负责的一件完整事情 | 未来新增的业务实体（V1 尚未实现）。它是"那一单"的档案。 |
| **Repository** | 这件事的代码/资产在谁那里、怎么拿到 | 未来新增的资产登记对象（V1 尚未实现）。它描述**来源**（github/gitlab/zip/本地目录…），不绑定某一项目。 |
| **Workspace** | 某个员工真正动手工作的地方 | 已存在：每个 Agent 的存储子树 `{STORAGE_LOCAL_ROOT}/{agent_id}/` + 每次 Run 的临时物料化目录。`FACT`（附录 C-1/C-4）。 |
| **Execution** | 员工某一次真正干活的完整过程 | 已存在：一次 AgentRun（含 checkpoint、工具执行、验证、投递）。`FACT`（`docs/PHASE1_CLAWITH_CAPABILITY_AUDIT.md` §2/§4）。 |

关键区分轴：**Project/Repository 是"事与物"（业务层，未来新增）；
Workspace/Execution 是"人与过程"（运行时层，今天已存在）。**
业务层对象永远不向运行时层塞物理路径或执行状态。

---

## B. 实体关系决议（整合自 PROJECT_DOMAIN_V1 §4–§5）

### B.1 决议一：Project ↔ Repository —— `DESIGN PROPOSAL`：**1 Project → N Repository（V1 中 N ≥ 0）**

逐条回答根任务 §八 的六个问题：

1. 对应一个 GitHub 仓库？→ 是，N=1。
2. 对应多个仓库？→ 是。真实例子：Vue 前端 + FastAPI 后端 = 两个仓库，属于同一件事。
   若 V1 写成 1:1，遇到真实多仓项目就必须返工拆模型——**这正是根任务 §八 警告的"偷偷硬绑定"**。
3. 没有 Git 仓库？→ 是。V1 来源可以是本地文件夹、文档，此时 N=0，Project 依然成立。
4. 来源是 ZIP？→ 是。ZIP 登记为一个 Repository（source_type 不同），不需要新实体。
5. 来源是本地文件夹？→ 同上，N=1（source_type=local_folder）。
6. 来源是文档而非代码？→ 是。"事情"不等于"代码"；文档来源时 Repository 可 N=0，
   文档作为 Intake 物料直接进入项目物料区（§E）。

模型形状（V1 概念层，暂不建表）：

```text
Project
 ├─ Repository #1   (source_type: github, clone_url, default_branch, 获取方式)
 ├─ Repository #2   (source_type: local_folder, path)
 └─ (可为 0 个)
```

- Repository 是**独立实体**：它拥有"在哪、怎么取、怎么验证完整性"这些来源事实。
  Project 只持有对 Repository 的引用集合，**不把 clone_url/branch/commit 字段抄进 Project**。
  （根任务 §十九：来源会迁移 GitHub→GitLab→ZIP，来源事实跟着 Repository 走，Project 不动。）
- `FACT` 依据：当前代码中**不存在任何 Repository/仓库概念**（全仓检索 `Repository|git_repo|clone_url`
  仅命中 AGENTS.md 文档文字与外部集成元数据；无 git 检出接口，`docs/PHASE1_CLAWITH_CAPABILITY_AUDIT.md` §8；
  本卡 §附录 C-8 已复核）。所以 Repository 与 Project 都是 Phase 2B 新建物，形状可以一次定对。
- 与现有代码资产的关系：`FACT`——`execute_code` 工具（`backend/app/services/sandbox/` 7 个后端）可以在沙箱里手动跑 git 命令，
  但那是"员工自己动手"，不是系统 Intake。Repository 实体未来是 Intake 的登记处，不是 execute_code 的替代品。

### B.2 决议二：Project ↔ Workspace —— `DESIGN PROPOSAL`：**当前阶段模型 B；C 是 B 的自然演进，不是替代**

三个候选（根任务 §九）：

- **模型 A**：Project 拥有一个共享 Workspace，多 Agent 都往里写。
- **模型 B**：Agent 各保留自己的 Workspace（现状），项目物料**注入到各参与 Agent 的 Workspace**。
- **模型 C**：Project → Repository → 各 Agent 临时 Workspace（以仓库快照为源头按 Agent 分发）。

**裁定：当前阶段模型 B；C 是 B 的演进，不是替代。理由（全部基于 Clawith 真实机制，不打分，只说明）：**

1. **哪种方式最容易直接接入现在 Runtime？** → B。
   `FACT` 证据 1：Agent 工作区的物理边界是存储子树前缀——`initialize_agent_workspace`
   在 Agent 创建时向 `{agent_id}/…` 写种子文件（`services/agent_tools.py:1653-1673`，本卡 §附录 C-1 已复核）；
   `resolve_agent_visible_path` 把模型可见路径解析到"该 agent 的 workspace 根"
   （`services/workspace_paths.py:51-87`，本卡 §附录 C-2 已复核），跨 Agent 访问被前缀阻断，绝对路径直接拒绝。
   模型 A 要求新增一个"项目共享根"的存储方案 + 新路径解析 + 新修订/锁作用域，
   直接动 Runtime 的隔离假设——**破坏现有 Agent workspace**（根任务 §九 第二点，A 不合格）。
   B 零改动 Runtime：Intake 把项目物料写进各参与 Agent 的既有子树即可。
2. **哪种不会破坏现有 Agent workspace？** → B（A 会，C 在 B 之上加一层快照分发，对现有机制是叠加而非破坏）。
3. **哪种最适合以后多人/多 Agent 协作？** → 演进路径：B（V1，项目物料按 Agent 分发）
   → 协作冲突出现后引入 C 的要素（Repository 快照作为唯一源，各 Agent 工作区是快照的副本/物料），
   而不是跳到 A 的"共享物理空间"——因为 `FACT` 证据 2：修订表 `workspace_file_revisions` 已有
   `scope_type IN ('agent','group')`（`models/workspace.py:33-48`，本卡 §附录 C-3 已复核）——
   该 group scope **不是预留钩子，而是既有在用契约**：已由 `services/workspace_collaboration.py` 的 group 修订/锁路径
   （`record_group_revision` :336 / `prepare_group_runtime_revision` :386 / `finalize_group_runtime_revision` :485）
   与 `services/group_file_service.py` 的 group 文件修订路径消费；
   未来项目级共享可先走 group scope，无需推翻现有 agent scope 机制。
4. **V1 的风险 5 缓解（多 Agent 改同一项目文件）**：B 下各 Agent 是独立副本，
   并发写同一项目文件会产生分歧。V1 的约定（`DESIGN PROPOSAL`，**注意：本条只定义 Project 层
   调度策略，不依赖、也不宣称任何锁机制已实现该策略；两者的区分见 §B.4**）：
   **同一时刻一个 Project 只有一个活跃执行 Agent（单写者）**；
   多 Agent 并行写同一项目物料是 C 引入快照分发之后才允许打开的场景。这是边界约定，不是 A 式共享物理空间。

**归属关系最终形态（V1）：**

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

**Project 与 Workspace 没有外键归属关系**——Workspace 归 Agent 所有
（`FACT`：修订/锁表均挂 agent_id 外键 + agent_id 前缀存储，附录 C-1/C-3），
Project 只是物料的来源方。这是与"模型 A（Project 拥有 Workspace）"的本质区别。

### B.3 推荐关系图（V1 概念地图总图）

```text
外部项目（GitHub / 本地 / ZIP / 文档）
        │
        ▼
  Project Intake（V1 流程，详见 §E）
        │
        ▼
  Project ──────────────────────────────
   │    （公司负责的"那件事"，业务层一等公民）
   ├── Repository × N（≥0）  ← 资产来源登记（github/local/zip/document/manual）
   ├── Project Knowledge（V1 非目标，未来，见 §G.4）
   └── 项目任务（未来任务图；V1 不动 Task 表，见 §G.1）
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

### B.4 决议三：Workspace Lock（`FACT`）与 Project Single Writer（`DESIGN PROPOSAL`）——两个必须分开的概念

> 本节是"Problem 4"的定稿：把**运行时的文件/工作区锁**（现状事实）与**项目级单写者调度策略**
> （V1 设计提案）钉死为两个正交概念，防止后续卡片/读者把二者混为一谈。

| 维度 | **Workspace Lock**（运行时冲突控制） | **Project Single Writer**（项目级调度策略） |
|---|---|---|
| 状态 | `FACT`：**已实现的现状机制**（Clawith 活体源码） | `DESIGN PROPOSAL`：**V1 设计约定，未实现**，随 Phase 2B 才有载体 |
| 层级 | 运行时 / 存储层：文件与编辑动作 | 业务 / 调度层：Project 的生命周期推进 |
| 控制对象 | 单个工作区文件/路径的**编辑冲突**（谁此刻在改这个文件） | 一个 Project 的**执行者名额**（此刻哪个 Agent 有权推进该项目） |
| 粒度 | per-(agent\|group) scope + per-path | per-Project |
| 作用方式 | 抢占即阻塞：拿不到锁的本**次操作**失败/等待 | 拒绝并指向当前执行者：第二个 Agent 的请求**根本不允许进入 EXECUTING**（§G.3 规则 3） |
| 生命周期 | 毫秒~秒级（Redis TTL 默认 60s；编辑锁带心跳/过期） | 与 Project 状态机绑定（EXECUTING 期间持有，BLOCKED 释放后方可再入） |
| 代码落点 | 见下"FACT 证据"列 | **无代码落点**——V1 无 Project 实体、无调度器；它是 2B 落地 Intake/状态机时才实现的约定 |

**FACT 证据（Workspace Lock 现状，附录 C-3/C-11 已复核）：**

1. **Redis 短锁**：`services/workspace_locking.py`（全文 91 行）——
   `acquire_workspace_lock(agent_id, path, ttl_seconds=60, …)` 以
   `tenant:{t}:workspace-lock:{agent_id}:{path}` 为键做 `SET NX EX` 抢占，
   `workspace_locks` 上下文管理器批量抢占、逆序释放；失败即 `RuntimeError("Workspace lock busy: …")`。
   **作用域是 (agent, path)，且 agent 内互斥——它不是跨 Agent 的锁，更没有 Project 维度。**
2. **持久编辑锁**：`models/workspace.py:71-107` `WorkspaceEditLock`（"Short-lived lock while a
   human is actively editing a workspace file"）——`scope_type IN ('agent','group')` +
   `user_id` + 心跳/过期；由 `services/workspace_collaboration.py` / `group_file_service.py` 的
   group 修订/锁路径消费（附录 C-3）。它是**人正在编辑某文件**的冲突控制，与"项目谁来执行"无关。

**关键裁定（防混淆三条，2B 与后续所有卡片必须遵守）：**

1. **Workspace Lock 存在 ≠ Single Writer 已实现。** 锁机制只回答"此刻这个文件有没有人
   正在写"；它**不阻止**第二个 Agent 被调度进同一个 Project 的 EXECUTING，也**不拥有**
   Project 的任何状态。Single Writer 是项目状态机（§C）层面"同时只有 1 个活跃执行 Agent"的
   调度约定，V1 阶段它**只是约定，不是机制**——没有实体、没有调度器、没有拒绝路径的代码。
2. **Single Writer 的落地载体是 §C 的 EXECUTING 状态 + 执行 Agent 记录**（"进入 EXECUTING
   时记录执行 Agent"），不是复用 Workspace Lock。2B 实现 Intake 状态机时，单写者判定必须发生在
   Project 状态推进处；Workspace Lock 只在"Agent 内部真的去改工作区文件"那一刻才介入。
   两者即使将来同时生效，也是**不同层、不同粒度、不同 owner**，不得把其中任何一方的行为
   当作另一方的实现证据。
3. **命名纪律**：本文（及 2B 代码/文档）里"Workspace Lock / 工作区锁"一律指上表左列的
   现状机制；"Project Single Writer / 项目单写者 / 单写者约定"一律指右列的 V1 调度策略。
   任何把左列机制描述为"已实现单写者"的表述都是**错误**，必须改写。

---

## C. 生命周期（整合自 PROJECT_INTAKE_V1 §4）

**7 个核心状态 + 3 个终态组**（`DESIGN PROPOSAL`；根任务 §十二 要求 6~8 个核心状态，本设计 7 个达标）：

```text
RECEIVED            刚受理：已收到 IntakeCommand，实体未确认
        │
        ├──(验证通过)──► SOURCES_OK
        │                     │
        │                     └──► INITIALIZED      （Project + Repository 已建成）
        │
        └──(验证失败, 不可修复)──► REJECTED      ← 终态：输入不可修复 / 安全检查不过

INITIALIZED
        ├──► ANALYZING               （深度分析中，未来环节，见 §G.4）
        └──► PENDING_CONFIRMATION    （等老板确认：目标/范围/来源是否 OK）
PENDING_CONFIRMATION
        ├──► EXECUTING             （确认放行）
        ├──► REJECTED              （老板否决）
        └──► ANALYZING            （老板要求先分析）
ANALYZING ──(分析产出 Artifact)──► PENDING_CONFIRMATION
EXECUTING
        ├──► BLOCKED               （缺资源：凭据、访问、外部依赖）
        ├──► COMPLETED             （交付目标达成，§COMPLETED 见下）
        └──► REJECTED              （中途否决）
BLOCKED ──(资源到位)──► EXECUTING
COMPLETED ──(冷却期后)──► ARCHIVED
ARCHIVED ← 终态
REJECTED ← 终态
```

| 状态 | 命名 | 说明 |
|---|---|---|
| 1 | `RECEIVED` | 刚接进来，只有命令，没有已验证实体 |
| 2 | `SOURCES_OK` | 来源验证通过的中间态（可合并进 RECEIVED→INITIALIZED，保留它是因为验证可能异步/可重试） |
| 3 | `INITIALIZED` | 实体建成，可被分配、可开始流转 |
| 4 | `ANALYZING` | 深度分析中（V1 只定义边界，见 §G.4） |
| 5 | `PENDING_CONFIRMATION` | 等老板确认，人工门 |
| 6 | `EXECUTING` / `BLOCKED` | 执行中 / 执行阻塞（Project 单写者约定：同时只有 1 个活跃执行 Agent；这是 §B.4 的调度约定，**不是**既有锁机制） |
| 7 | `COMPLETED` / `ARCHIVED` / `REJECTED` | 终态组 |

约定（不可违反）：

- **EXECUTING 单写者**（§B.2 决议沿用，§B.4 定义）：进入 EXECUTING 时记录执行 Agent，
  同一 Project 同时只有 1 个活跃执行 Agent；BLOCKED 释放后才可再进 EXECUTING。
  这是 **Project 调度策略（`DESIGN PROPOSAL`）**，由状态机自身执行；
  与既有 Workspace Lock（`FACT`，§B.4 / 附录 C-3/C-11）正交，不得互为实现证据。
- **REJECTED 带原因码**（§G.3），不是黑洞：被拒项目保留记录与原因，可人工重建新 Intake。
- **BLOCKED 必须有 blocker 描述 + 期望解除条件**，否则不许挂起（防"永久阻塞"假状态）。

---

## D. Project V1 最小数据（整合自 PROJECT_INTAKE_V1 §5）

### D.1 必须（Must Have）

```text
id                 UUID       主键
name               string     项目名（唯一性按 tenant 域内约定，不强制全局唯一）
description        string     一句话说清公司负责什么
goal               string     "做完了"的判定标准（验收依据的最小形式）
status             enum       §C 状态机
repositories       ref[]      → Repository × N（引用，不内联 git 字段；§B.1）
tenant_id          UUID       沿用现有租户隔离
created_by         UUID       创建者（沿 task.py:42 模式）
created_at / updated_at / status_changed_at
```

理由（逐条）：

- `goal` 必须有：没有"做完标准"的 Project 无法在 PENDING_CONFIRMATION / COMPLETED 上形成判定，
  生命周期后半段就是空转。这是一等业务字段，不是文档字段。
- `repositories` 是引用集合：来源事实全在 Repository 侧（clone_url/branch/path 随来源迁移，
  Project 不动）——根任务 §十九 的决议，避免"偷偷硬绑定"。

### D.2 以后再加（Future，非 V1）

| 字段 | 何时需要 | 归谁 |
|---|---|---|
| acceptance_criteria[]（结构化验收项） | goal 一句话不够用时 | 独立对象，挂 Project 引用 |
| delivery_deadline | 出现排期/调度需求时 | Project 或 Scheduler（Request Budget）侧 |
| 指派 Agent / Squad（project_lead_agent_id 等） | 团队接入落地时 | §G.2 裁定 |
| knowledge_refs / analysis_artifact_refs | Analysis 环节启动后 | §G.4 |
| department_id（归属部门） | 部门有调度语义时 | 现在 Department 只是元数据（`FACT`: models/org.py:12，附录 C-6），挂了也无消费方 |
| budget / priority 业务权重 | 商业/资源约束出现时 | Resource Manager 侧 |

### D.3 不应该放进 Project（Excluded，负面清单）

| 候选 | 排除理由 | 事实归属 |
|---|---|---|
| clone_url / branch / commit / remote | 来源事实属于 Repository，且来源会迁移（github→gitlab→zip），Project 不动 | Repository 实体（§B.1） |
| workspace 路径 / 项目工作目录 | Project 不拥有物理工作区（§B.2 模型 B）；物理路径属于 Agent 工作区 | `{agent_id}/…` 存储子树（`FACT`: storage_runtime/utils.py:19-21，附录 C-9） |
| 分析结果 JSON / 技术栈 / 目录结构 / 依赖清单 | 分析结果是 Analysis Artifact，塞进 Project 就是根任务风险 3 | 未来 Analysis 模块（§G.4） |
| 任务列表 / 任务图 | V1 不动 Task 表（`FACT`: models/task.py:23，agent_id 必需、无项目字段，附录 C-7）；项目任务图归 §G.1 | 未来任务图（§G.1） |
| Agent 执行状态 / Run 引用 | 执行状态属于 AgentRun；Project 层经"归属引用"看执行，不内联 | 现有 AgentRun（Phase 1 审计 §2/§4） |
| budget / 薪资 / 绩效 / OKR | 根任务 §二十二 非目标 | 永不 |

---

## E. Project Intake 边界（整合自 PROJECT_INTAKE_V1 §1–§3、§8、§12）

### E.1 Intake 是什么 / 不是什么

**定义（`DESIGN PROPOSAL`）：Project Intake = 把"外部的一件事情"变成"公司认识的一个 Project 实体"的边界动作。**

它只做四件事：

1. **登记来源**：把来源描述（source_type + 定位信息）登记为 Repository 资产记录（§F）。
2. **验证来源**：确认来源真实存在、可读（§F 的逐类型验证动作）。
3. **初始化实体**：创建 Project + Repository 记录，状态机从 RECEIVED 走起（§C）。
4. **移交下一环节**：完成后进入 ANALYZING 或等老板确认，**不产生任何执行**。

一句话：**Intake 是"接项目"，不是"做项目"，不是"看项目"。**

三段职责的区分（根任务 §十三，必须拆开）：

| 环节 | 负责什么 | 产出 | V1 状态 |
|---|---|---|---|
| **Intake** | 来源验证、实体初始化、状态进入 | Project + Repository 记录（已验证） | §E–§F 设计 |
| **Project Analysis** | 深度代码/文档分析：技术栈、依赖、启动方式、风险 | Analysis Artifact（独立对象，不塞进 Project 本体） | 只定义边界（§G.4），不实现 |
| **Project Execution** | 真正的干活 | Task → AgentRun（现有链路） | 现有能力，归 Phase 2B+ |

风险 4（根任务 §二十一）的防线就在这一刀：**Intake 的退出条件里不含任何"理解项目内容"的动作**。
`FACT` 依据：当前系统里"理解项目"只可能由 Agent 在 Run 内经 `execute_code` 手动做
（`services/sandbox/`，7 个后端）——那是员工行为，不是系统 Intake 的职责。

### E.2 Intake 输入 / 输出

输入（`DESIGN PROPOSAL`）：

```text
IntakeCommand = {
  name            : string        （必须，人类可读的项目名）
  description     : string        （必须，一句话说清"公司要负责什么"）
  goal            : string        （必须，"做完了"的判定标准，可一句话）
  source          : { source_type, locator } × N   （N ≥ 0，见 §F.2）
  tenant_id       : UUID          （现有租户模型，多租户隔离沿用上现有约定）
  created_by      : user 身份      （沿现有 created_by 模式，FACT: models/task.py:42）
}
```

- source 可缺省（N=0，纯 manual 项目）；此时 Project 依然成立（§B.1 决议 3/6）。
- 输入校验失败（缺 name/goal、locator 缺字段、未知 source_type）→ 拒绝受理，不创建任何实体（§G.3）。

输出（`DESIGN PROPOSAL`）：

1. **Project 记录**：id、name、description、goal、status=INITIALIZED、created_at。
2. **Repository 记录 × N**：每条含 source_type、locator（§F）、验证结果 verified=true。
3. **状态机推进记录**：RECEIVED → … → INITIALIZED 的每一步留痕（审计最小集，§C）。
4. **移交信号**：Project 进入"待分析 / 待确认"，Intake 动作结束。**不创建任何 Task、不指派任何 Agent、不写任何工作区。**

### E.3 Intake 最小流程（逐步）

```text
1. 受理        接受 IntakeCommand，字段校验            → RECEIVED
2. 登记来源    按 source_type 生成 Repository 记录     → 仍在 RECEIVED（实体未确认前不算建成）
3. 验证来源    逐条执行 §F.1 的验证动作
               全部通过 ────────────────────────────→ SOURCES_OK
               任一失败 → 走 §G.3 错误处理（REJECTED 或挂起重试）
4. 初始化实体  创建 Project（status=INITIALIZED）
               绑定已验证的 Repository 记录集合
5. 移交        进入 ANALYZING 或 PENDING_CONFIRMATION
               （由老板/配置选择；Intake 自身到此为止）
```

关键约定：

- **步骤 4 之前不落 Project 实体**（或仅以 RECEIVED 草稿态存在）。来源未验证的"假项目"不进入公司视野
  ——这是风险 6（不同来源身份不统一）的防线：Project 的出生时刻 = 来源验证通过时刻，
  身份从一开始就是"已验证来源 + 业务目标"。
- 步骤 5 的"移交"是一个**动作**，不是一个 Intake 子流程：分析属于 Analysis 环节，确认属于老板，
  Intake 对两者都不负责。

### E.4 Workspace 初始化边界（承接 §B.2 模型 B）

- **Intake 不创建任何物理工作区。** Project 不拥有工作区（§D.3 Excluded 第 2 条）。
- **物料分发是 Intake 之后的显式动作**：把"已验证来源的物料"复制进**参与 Agent 的既有 `{agent_id}/` 子树**。
  `FACT` 落点：写入经 `get_storage_backend().write_bytes/write_text`
  （`services/storage_runtime/facade.py:30`；接口 `storage_runtime/base.py:51-77`，本卡 §附录 C-9 已复核），
  key 前缀经 `agent_storage_prefix(agent_id)`（`storage_runtime/utils.py:19-21`）
  ——与 Agent 种子初始化（`agent_tools.py:1653-1673`，附录 C-1）走同一条物理路径，**零 Runtime 改动**。
- 分发的目标 Agent 集合 = 当时被指派的 Agent（V1 Project 单写者下通常 1 个，§B.4）；分发结果（写了哪些 key）留痕在 Intake 审计记录里。
- Run 级临时物料化（`TempWorkspace`，`agent_tools.py:1689-1705`，附录 C-4）是**执行环节**的既有行为，
  与 Intake/分发互不重叠：分发写持久存储，TempWorkspace 是 Run 沙箱的物化。

边界一句话：**Intake 管"登记与验证"，分发管"物料进 Agent 区"，TempWorkspace 管"Run 内物化"。三段各归各位。**

### E.5 Intake 明确不做的事（V1 非目标，根任务 §十五 + 补充）

- ❌ 深度代码分析 / 业务需求理解（§G.4，Analysis 环节）
- ❌ 自动拆任务 / 自动建任务图（Task 表 V1 不动，§G.1）
- ❌ 自动组队（Squad）/ 自动指派 Agent（§G.2）
- ❌ 自动开始修改代码、自动部署、自动 Review
- ❌ 物理项目工作区的创建（§E.4：分发 ≠ 建工作区）
- ❌ 共享物理空间 / 多 Agent 并行写（模型 A/C，§B.2）
- ❌ git 获取能力本身（github/gitlab/local_git 的可达验证依赖它，§F.3 Phase 2B 首批）

---

## F. Repository 接入边界 + Source 支持矩阵（整合自 PROJECT_INTAKE_V1 §6–§7）

### F.1 Repository 实体形状（`DESIGN PROPOSAL`）

```text
Repository = {
  id, project_id (ref),
  source_type : enum（§F.2）
  locator     : 按 source_type 的结构化定位信息
  verified    : bool + verified_at
}
```

- Intake 对 Repository 的全部职责 = **登记 + 验证**（§E.1 第 1/2 项）。
- Repository 的"获取"（拉取/复制物料）是 Intake **之后**的动作，归"物料分发"（§E.4），不属于 Intake 本身。

### F.2 逐 source_type 的验证动作（`DESIGN PROPOSAL`，受 §F.4 FACT 约束）

| source_type | locator | V1 验证动作 | 备注 |
|---|---|---|---|
| `local_folder` | 绝对路径 | 路径存在 + 可读 + 非空（目录）；记录条目数 | 宿主机的"存在"由运维约定，系统只校验传入值合法 |
| `manual` | 无（或上传附件 id） | 记录创建事实即可（纯登记，无外部依赖） | 最低成本来源 |
| `document` | 文件路径 / 上传件 | 文件存在 + 可读 + 大小在界内 | 文档作为 Intake 物料直接进项目物料区（§B.1 决议 6） |
| `zip` | 文件路径 / 上传件 | 文件存在 + 可解开 + 顶层非空 + 无路径穿越（zip-slip 检查） | 安全防线见 §G.5 |
| `github` / `gitlab` | owner/repo + 默认分支 | 仓库可达（HEAD 可取）+ 凭据可用 | **依赖尚未存在的 git 获取能力**（§F.4） |
| `local_git` | 本地路径 | 路径是 git 工作树 + HEAD 可读 | 同上，依赖 git 能力 |

### F.3 Project Source 支持矩阵（V1 / 未来 / 暂不考虑）

`DESIGN PROPOSAL`，受 §F.4 的 FACT 约束：

| source_type | V1 支持 | 理由 |
|---|---|---|
| `manual` | ✅ 必须支持 | 零外部依赖，纯登记；也是 N=0 来源项目的形态 |
| `local_folder` | ✅ 必须支持 | 验证 = 目录存在可读，无需新能力；宿主文件夹是真实接管场景 |
| `document` | ✅ 必须支持 | 文件可读性检查，无新能力 |
| `zip` | ✅ 必须支持 | 同上（加 zip-slip 安全检查，§G.5） |
| `github` / `gitlab` | ⏳ 未来（Phase 2B 首批） | **验证需要"仓库可达 + 凭据"，这要求 git 获取能力——当前系统没有**（§F.4）。V1 枚举保留、流程放行：git 系来源在 V1 验证不可完成 → 挂在 **RECEIVED/SOURCES_OK**（带 source=git/pending-verifier 标记），等待 2B 首批 git 获取能力落地；有界 `SOURCE_UNREACHABLE` 重试超限后按 §G.3 升级为终态 REJECTED——"缺 git 能力"是**环境能力缺口**，不属于 §G.3 的 5 个封闭原因码，也不产生 BLOCKED 迁移，不会误报"已接入" |
| `local_git` | ⏳ 未来（Phase 2B 同批） | 需要 git 工作树检测 + 快照逻辑 |

约定：

- **source_type 是开放枚举**：V1 只实现 4 个验证器；未知值在 Intake 命令校验阶段**显式拒绝**
  （AGENTS.md"状态/协议变体是封闭的"原则——对"未知 source_type"行为 = 显式拒绝，不猜测）。
- github/gitlab 登记进 Repository 记录不受影响（登记只是存 locator），受影响的只有"验证可达"这一步。
  这意味着**未来补 git 能力时，历史 Project 记录无需返工**——locator 早就存好了。

### F.4 关键 `FACT`：当前代码无任何 git 仓库接入/获取能力

- 全仓检索 `clone_url|git_repo|repository_url|git clone|pull_repo|fetch_repo`（`backend/app/`）**零命中**。
- 唯一形近命中是 `services/sandbox/local/subprocess_backend.py:808 _clone_workspace_to_staging`——
  该函数是**目录 staging 复制**（`shutil.copy2/copytree` 把 Agent 工作目录复制到暂存目录，本卡 §附录 C-10 已复核），
  与 git 语义无关。
- 所以 §F.3 的 V1 支持矩阵直接由这条 FACT 决定：git 系来源只能登记、不能"验证可达"；
  git 系来源在 V1 验证不可完成 → 挂在 **RECEIVED/SOURCES_OK**（带 source=git/pending-verifier 标记），等待 2B 首批 git 获取能力落地；
  有界 `SOURCE_UNREACHABLE` 重试超限后按 §G.3 升级为终态 REJECTED，而非误报已接入。
  "缺 git 能力"是**环境能力缺口**，不属于 §G.3 的 5 个封闭原因码，也不产生 BLOCKED 迁移。

---

## G. 整合卡裁定（两份源文档"详细裁定归 t_d222b4f8"的开放项 —— 本文的增量价值）

> 两份源文档都把这些项显式甩给了整合卡。本节逐条给出**裁定**（`DESIGN PROPOSAL`，含理由与边界），
> 使 Phase 2B 不再需要重新辩论这些问题。

### G.1 项目任务图（根任务 §十七：Project ↔ Task 怎么连？）

**现状 `FACT`**：Task 只挂在单个 Agent 上（`models/task.py:23` agent_id 必需外键），
无 project/parent/dependency 字段（附录 C-7）；Task↔AgentRun 已是真实双向执行链路
（`task_executor.py:43` 正向 / `task_completion.py:56` 反向，Phase 1 审计 §5）。

**三个候选**（根任务 §十七）：

- 甲：Project 直接拥有 Task（Project → Task A/B/C，给 Task 表加 project_id 字段）。
- 乙：Project → Work Item → Task（中间层）。
- 丙：Project → Task → AgentRun（两段式，复用现有 Task→Run）。

**裁定（本文定稿）：目标模型 = 乙（Work Item 中间层）；V1 不建任务图、不动 Task 表、不建 Work Item 表。**

理由（逐条排除法）：

- **排除甲（Project 直连 Task / 加 project_id 到 Task 表）**：Task 是"单 Agent 作用域的执行驱动"
  （§C 与 Phase 1 审计 §5 一致），给它加 project 字段 = 根任务 §八 警告的"偷偷硬绑定"——
  一个 Task 既可以是项目内一步、也可以是独立 todo，两种身份塞进同一张表会返工。
  且甲无法表达"一个项目步骤要多个 Task/多个员工分工"。
- **排除丙（Project→Task→Run 两段式）**：丙只是把现有 Task→Run 挂到 Project 下，
  同样无法表达"项目步骤 ↔ 多个执行"的多对多，且直接污染 Task 表。
- **选乙（Work Item 中间层）**：Work Item = "公司这件事里的一个可交付步骤"，它比 Task 高一层，
  可以 1:N 到 Task（多员工分工），也可以 1:N 到 AgentRun（多次执行）。
  它满足 AGENTS.md"新状态机需要独立 owner + 真实需求"——**Work Item 只在"任务分解"这个消费方出现时建表**，
  而任务分解是 V1 非目标（§E.5 ❌自动拆任务），所以 **V1 只定义 Work Item 概念边界，不建表**。
- **V1 的归属表达**（本卡最终约定，与 §D.3 Excluded 第 4 条一致）：
  **Project 层新增的对象不得塞进现有 Task/AgentRun 的字段；归属通过新增引用（future: `WorkItem.project_id` / `project_work_item_agent_task_ref`）表达。**
  Phase 2B 若要落地任务图，先落 Work Item 表 + 归属引用，Task 表保持零改动。

**给 Phase 2B 的门槛**：任务图不是 2B 首批（2B 首批是 git 获取，见 §I），
任务分解是 2B 之后的"公司级调度"能力；届时按乙建 Work Item，不复用/不改 Task 表。

### G.2 项目团队 / 小队（根任务 §十八：Project Team ↔ Department / Group / A2A）

**现状 `FACT`**：Department 只是元数据 + 知识可见性（`models/org.py:12 OrgDepartment` +
`experience_retrieval.py:91`，附录 C-6），**无调度/派发/权限消费方**；
Group 有原语且在用（`models/group.py`；`workspace_file_revisions` / `WorkspaceEditLock` 的 group scope 已由 `workspace_collaboration.py` / `group_file_service.py` 的 group 修订/锁路径消费，附录 C-3）但**无 Squad 语义**；
A2A 已存在且真实执行（`a2a_runtime.py:774`，Phase 1 审计 §6）。

**裁定（本文定稿）：目标模型 Project Team/Squad ≠ Department；V1 不建 Squad 实体、不自动组队、不建 Manager 实体。**

- **Department = 长期组织归属**（员工在哪），现有系统里它是 Feishu 同步的元数据 + 知识可见性门，
  **不承载任何"临时为一件事干活"的语义**——所以 Squad 不能用 Department 表达。
- **Squad / Project Team = 项目临时工作组**（为这件事临时集合的若干 Agent），
  目标模型里它挂在 Project 下，**复用现有 Group 原语**（group scope 是既有**在用**契约：`workspace_file_revisions` /
  `WorkspaceEditLock` 的 group 作用域修订/锁已由 `services/workspace_collaboration.py` / `services/group_file_service.py` 的 group 修订/锁路径消费，附录 C-3），
  Squad 复用此既有契约，共享空间走 group scope，
  不新建 Department、不新建 Manager 实体、不动 A2A 路径。
- **接入方式（目标模型）**：Project 持有 squad 成员引用（`agent_id` 集合 + 角色标签），
  协作走既有 A2A / group 原语；Squad 的"并写同一项目物料"仍受 §B.2 Project 单写者边界约束
  （要真正并写需先引入 §B.2 模型 C 的快照分发；单写者 = §B.4 的调度策略，与既有 Workspace Lock 无关）。
- **V1 边界**：§E.5 已把"自动组队/自动指派"列为非目标；§D.2 把 `project_lead_agent_id` 等指派字段
  列入 Future。所以 V1 **只定义 Squad 概念边界与"复用 Group 原语"的接入约定，不建表、不自动组队**。

**给 Phase 2B 的门槛**：Squad 实体化是 2B 之后的"多 Agent 协作"能力，
落地时挂 Project 成员引用 + 复用 group scope，**禁止**新建 Department 或 Manager 实体来绕过 §B.2 Project 单写者边界（§B.4）。

### G.3 REJECTED 原因码集合 + 重试策略（两份源文档"需整合卡定稿"项）

`DESIGN PROPOSAL`。根原则（AGENTS.md "misconfiguration fails at the earliest authoritative point" +
"封闭集"原则）：**原因码是封闭集，V1 定 5 个；每个码归属"可重试（transient）"还是"终态（permanent）"是明确的。**

| 原因码 | 含义 | 触发点 | 重试属性 | 落点 |
|---|---|---|---|---|
| `SOURCE_NOT_FOUND` | 路径不存在 / 仓库 404 且无法修复 | 来源验证 | **permanent** | `REJECTED`（终态） |
| `SOURCE_INVALID` | 文件损坏 / 结构非法 / 缺必填字段 | 命令校验 + 来源验证 | **permanent** | `REJECTED`（终态） |
| `SECURITY_REJECTED` | 安全检查不过（如 zip-slip 路径穿越） | 来源验证（§G.5） | **permanent** | `REJECTED`（终态，不复用、不自动重试） |
| `SOURCE_UNREACHABLE` | 凭据过期 / 网络**临时**不可达 | 来源验证 | **transient（有界重试）** | 重试期内留在 RECEIVED/BLOCKED + 重试标记；**超限 → 升级为 `REJECTED`（原因码仍记 `SOURCE_UNREACHABLE`）** |
| `DISTRIBUTION_FAILED` | 物料分发写存储失败 | 分发动作（§E.4） | **transient（独立可重发）** | `BLOCKED`（不拖 Project 状态倒退；已分发部分留痕可重发） |

**关键规则（防止"假重试"与"假终态"）**：

1. **transient vs permanent 的区别在于"能不能靠外部资源变化自愈"，不在于"这次失败了什么"**：
   `SOURCE_UNREACHABLE` 这次可能是凭据过期（换凭据就好）也可能是仓库真没了（404）——
   若 404 但分类器误判为可重试，靠"重试上限"兜底：达到上限后同一个码升级为 `REJECTED`。
   **即：码本身不变，变的是"还在重试窗口内"还是"已超限落终态"这个迁移条件。**
2. **`REJECTED` 是终态，带原因码 + 原因描述**，可人工重建新 Intake，但系统不自动重试 REJECTED。
3. **单写者冲突**（第二个 Agent 请求进 EXECUTING）：**不属于上面 5 个原因码**——
   它是约定级冲突（§B.4 的 Project 单写者策略，由状态机自身执行，与既有 Workspace Lock 无关），
   处理 = "拒绝该请求并指向当前执行者"，不是来源/分发失败，不落 REJECTED 也不落 BLOCKED。
4. **可重试的失败不拖 Project 状态倒退**：验证重试留在 RECEIVED/SOURCES_OK 之前；
   分发失败落 BLOCKED（独立动作，§E.4）。
5. **closed-set 扩展**：新增原因码必须走整合卡 + 一致性审查（t_5b1293ab），不允许实现侧私自加码。

**"为什么现在不写代码"相关**：原因码集 + 重试属性是封闭契约，2B 实现 Intake/验证器时直接照此映射，
不需要在代码里重新决定"这个失败该重试还是该拒"。

### G.4 Analysis 下阶段边界（根任务 §十六 三分类，本文定稿归位）

`DESIGN PROPOSAL`（根任务 §十六 的三分类预判，整合卡定稿）：

| 分析产出 | 归属 | 理由 |
|---|---|---|
| 技术栈 / 目录结构 / 依赖清单 / 风险清单 | **Analysis Artifact**（独立对象，带 generated_at + 生成 Run 引用） | 会过期、可重生成，不是项目本体事实 |
| 启动方式 / 测试入口 / 数据库 / API 约定 | **Project Knowledge**（未来沉淀区，挂 project_id 引用） | 长期有效的项目事实，沉淀复用 |
| 项目 metadata（§D.1 必须字段里任何一项被分析"顺手"更新） | ❌ 不允许 | 分析不改业务字段，只能产出引用 |

- Analysis 的触发 = §C 的 INITIALIZED → ANALYZING 边；完成 = 产出 Artifact + 状态进 PENDING_CONFIRMATION。
- **分析结果绝不回写进 Project 本体**（根任务风险 3 防线；也是 §D.3 Excluded 第 3 条）。
- `FACT`：当前系统"理解项目"只能由 Agent 在 Run 内经 `execute_code` 手动做（sandbox 7 后端），
  无独立 Analysis 模块、无向量 RAG（Phase 1 审计 §11）——所以 Analysis 是纯未来环节，V1 只划边界。

### G.5 安全边界（整合自 PROJECT_INTAKE_V1 §11 + 根任务 §21 风险，`DESIGN PROPOSAL` + `FACT` 落点）

1. **路径穿越**：`local_folder`/`zip` 的 locator 过 `normalize_storage_key`
   （`FACT`: `storage_runtime/utils.py:4-16` 拒绝 `..` 语义，附录 C-9）；zip 解包做 zip-slip 检查（§F.2）。
2. **凭据不落 Project/Repository 记录**：git 来源的 token 走现有配置/密钥渠道（沿 backend 凭据惯例），
   Repository 只存 locator；验证记录存"验证过"，不存凭据值。
3. **写入前缀隔离**：物料分发只能写 `{agent_id}/…` 前缀下（`FACT`: utils.py:19-21 +
   `workspace_paths.py:51-87` 的跨 Agent 前缀阻断假设，附录 C-2/C-9）——分发动作不得绕过该前缀写其他 Agent 区。
4. **拒绝是终态**：`REJECTED` + `SECURITY_REJECTED`（如 zip 内路径穿越）不复用、不自动重试（§G.3 规则 2）。

---

## H. "为什么现在不写代码" —— 歧义消解清单（根任务 §四）

> 根任务 §四 指出：现在最容易犯的错是"看到没有 Project → 马上建 `projects` 表"，
> 然后被一串"Git 放哪 / workspace 放哪 / Agent 怎么加 / Task 怎么连 / 项目知识放哪 /
> Project 和 Repository 什么关系 / 能不能多仓 / 分析结果是不是 Project 数据 / 运行环境归谁"逼得不停返工数据库。
> 本节把这些歧义逐条钉死，**每一条都给出设计如何消解它**。这是本文作为 2B 门槛的核心交付物。

| # | 如果不设计就卡住的歧义 | 本文如何消解（设计决议） | 出处 |
|---|---|---|---|
| 1 | "Git / clone_url / branch / commit 放哪里？" | **不进 Project 本体**；独立 Repository 实体持有来源事实，Project 只持引用集合。来源迁移（github→gitlab→zip）时事实跟 Repository 走，Project 不动。 | §B.1 / §D.3 |
| 2 | "workspace / 项目工作目录放哪里？" | **Project 不拥有物理工作区**（模型 B）。物料由 Intake 分发进各参与 Agent 的既有 `{agent_id}/` 子树，零 Runtime 改动。 | §B.2 / §E.4 |
| 3 | "Agent 怎么加入一个项目？" | V1 **不自动组队**；目标模型 Squad 复用现有 Group 原语（group scope），Project 持成员引用。Project 单写者边界不变（§B.4）。 | §G.2 / §B.4 |
| 4 | "Task 怎么和 Project 关联？" | **V1 不动 Task 表、不建任务图**；目标模型三段式 Work Item（中间层），归属用新引用表达，禁止给 Task 表加 project 字段。 | §G.1 |
| 5 | "项目知识 / 分析结果放哪里？" | **绝不回写 Project 本体**：技术栈/目录/依赖/风险 → Analysis Artifact；长期事实 → Project Knowledge（独立对象，挂 project_id 引用）。 | §G.4 / §D.3 |
| 6 | "Project 和 Repository 是什么关系？" | **1 Project → N Repository（N≥0）**。一个项目可以 0/1/N 个仓；1:1 被"偷偷硬绑定"警告排除。 | §B.1 |
| 7 | "一个 Project 能不能有多个仓库？" | 能（N≥0），Vue 前端 + FastAPI 后端是同一件事的两个 Repository。 | §B.1 |
| 8 | "项目分析结果是不是 Project 数据？" | 不是。分析结果是 Artifact/Knowledge 独立对象（§G.4），塞进 Project 是根任务风险 3。 | §G.4 / §D.3 |
| 9 | "项目运行环境属于 Project 还是 Workspace？" | **属于 Workspace（现有 agent 作用域机制）**。Project 是业务层，不碰物理运行环境。 | §A.4 / §B.2 |
| 10 | "不同来源（github/zip/local/document）身份怎么统一？" | **来源未验证不落 Project 实体**；Project 出生时刻 = 来源验证通过时刻，身份 = "已验证来源 + 业务目标"（§E.3 关键约定）。 | §E.3 / §G.3 |
| 11 | "github/gitlab 现在接不进（无 git 能力）怎么办？会不会误报已接入？" | 枚举先行、能力后补：git 系来源 V1 只登记 locator，验证挂起 → 真实项目挂在 **RECEIVED/SOURCES_OK**（带 source=git/pending-verifier 标记），等待 2B 首批 git 获取能力落地；有界 `SOURCE_UNREACHABLE` 重试超限后按 §G.3 升级为终态 REJECTED 而非误报已接入（"缺 git 能力"是环境能力缺口，不是 5 个封闭原因码之一，也不产生 BLOCKED 迁移）；补 git 能力时**生命周期形状零改动，只加验证器**（§F.3 / §I）。 | §F.3 / §F.4 / §I |
| 12 | "来源失败该重试还是该拒？" | **封闭原因码集 + 明确的 transient/permanent 属性 + 有界重试上限**（§G.3），码不变、迁移条件决定终态。 | §G.3 |
| 13 | "Workspace Lock 和 Project Single Writer 是一回事吗？会不会把锁当成单写者已实现？" | **两个正交概念，§B.4 钉死区分**：Workspace Lock = `FACT` 现状（per-agent/per-path 的运行时文件编辑冲突控制，Redis 短锁 + 持久编辑锁，无 Project 维度）；Project Single Writer = `DESIGN PROPOSAL` 调度策略（V1 未实现，载体是 §C 状态机本身）。**锁机制存在不蕴含单写者已实现**，命名纪律见 §B.4 关键裁定 3。 | §B.4 / 附录 C-3/C-11 |

**结论**：上表 13 条歧义全部已被 §A–§G 的设计消解。
只要这 13 条成立，"现在就建 `projects` 表"就会立刻在 #1（git 字段）、#4（Task 字段）、#5（分析 JSON）
三处被迫做硬决策而返工——**这就是"为什么先设计不写代码"的可核验版本**：
设计把这些硬决策提前到了 §B/§G，且每一条都有 `FACT` 依据或明确的 `DESIGN PROPOSAL` 理由，
编码者 2B 时无需再判断"放哪"。

---

## I. Phase 2B 门槛与最小实现批次

> 本文是 2B 的 gate。2B 开始前必须满足以下全部条件；本文**不执行 2B**（根任务 §二十八 停止条件）。

### I.1 进入 2B 的门槛条件（必须全部成立）

1. §H 的 13 条歧义已由 §A–§G 消解（本文完成）。
2. §G.1（任务图）、§G.2（Squad）、§G.3（原因码集 + 重试策略）三个开放项已由整合卡裁定（本文完成）。
3. 一致性审查（t_5b1293ab）确认：本设计的 Project/Workspace 模型不破坏现有 AgentRun / Workspace / File 存储机制，
   Intake 边界清晰（不渗入 Analysis / Execution），7 状态机可在现有 Durable Agent Run 架构内实现且**不需要**
   与 Phase 1 FACT 冲突的即时数据库迁移。
4. 任何 2B 卡都不得违反 §A.3（负面空间）、§B（实体关系）、§D.3（Excluded）、§E.5（非目标）。

### I.2 Phase 2B 最小实现批次（本文给 2B 的第一批，只列顺序，不实现）

1. **git 获取能力 + 3 个来源验证器（github / gitlab / local_git）** —— 这是 §F.4 的唯一硬阻塞。
   落地它之后，§F.3 矩阵里"⏳未来"的三行即可从挂起（RECEIVED/SOURCES_OK）走通到验证通过的 SOURCES_OK；
   **生命周期形状零改动，只加验证器**（§F.3 约定 / §H #11）。
2. **Project + Repository 两张表的 schema + 迁移**（按 §D.1 必须字段 + §F.1 Repository 形状），
   **Task 表保持零改动**（§G.1），不建 Work Item / Squad / Analysis Artifact 表（各自消费方未出现，
   遵守 AGENTS.md 独立 owner 规则）。
3. **Intake 服务 + 状态机推进 + 原因码映射**（§E/§C/§G.3），4 个 V1 验证器（manual/local_folder/document/zip）。

### I.3 明确不属于 2B 首批（继续设计层）

- 任务图 / Work Item（§G.1，等任务分解消费方出现）。
- Squad 实体化 / 多 Agent 并写（§G.2 / §B.2 模型 C，等协作冲突真出现）。
- Analysis 模块 / Project Knowledge 落库（§G.4，等分析环节启动）。
- 统一 Artifact/Evidence、独立 Review→Rework、supervision 调度、completion gate fail-closed
  （Phase 1 审计 §13 的其他 4 大缺口，非本 Phase 主线）。

---

## 附录 C. FACT 证据清单（本文引用的全部代码证据 + 本卡复核结论）

> 下表汇总两份源文档 §附录 的 1–9 号证据，并附本卡（t_d222b4f8）在 worktree `d345f6c` 上逐条复核的结论；
> C-10/C-11 为本卡（Problem 4 卡 t_f95e046e）新增的复核证据（Workspace Lock 现状 + 与 Project 单写者的正交性）。
> 复核方式：只读活体源码（Agent/Task/Workspace/Storage/Git 模块）+ 全仓 grep。

| # | 陈述 | 证据（文件:行） | 本卡复核结论 |
|---|---|---|---|
| C-1 | Agent 工作区前缀隔离 + 种子初始化（存储键以 agent_id 为前缀，创建时写 `{agent_id}/memory/memory.md`、`{agent_id}/soul.md`） | `services/agent_tools.py:1653-1673 initialize_agent_workspace` | ✅ 命中：`mem_key = normalize_storage_key(f"{agent_id}/memory/memory.md")`、`soul_key=...{agent_id}/soul.md`，走 `get_storage_backend().write_text` |
| C-2 | 模型可见路径解析到"该 Agent 的 workspace 根"，跨 Agent 前缀阻断、绝对路径拒绝、enterprise_info 前缀走企业只读区 | `services/workspace_paths.py:51-87 resolve_agent_visible_path` | ✅ 命中：`resolve_path_within_root(agent_workspace, ...)`，`enterprise_info` 前缀走 `enterprise_info_root`；越界抛 `WorkspacePathError` |
| C-3 | 修订/锁作用域只有 agent 与 group，agent 作用域必挂 agent_id 外键（group scope 是既有在用契约，非预留钩子） | `models/workspace.py:33-48`（WorkspaceFileRevision 表约束）+ `models/workspace.py:71-107`（WorkspaceEditLock 表，同型约束 :82-90）+ Redis 后端锁 `services/workspace_locking.py`（per-agent 短锁 `tenant:{t}:workspace-lock:{agent_id}:…`） | ✅ 命中：`scope_type IN ('agent','group')` + `(agent→agent_id NOT NULL AND scope_id=agent_id) OR (group→agent_id IS NULL)`，`group_key` 字段存在；`WorkspaceEditLock`（:71-107）同型约束 :82-90 命中；group scope 有活体消费者（`workspace_collaboration.py` :336/:386/:485 + `group_file_service.py` group 修订/锁路径） |
| C-4 | 每次 Run 的临时物料化（执行级隔离） | `services/agent_tools.py:1689-1705 TempWorkspace` + `_prepare_temp_workspace`（约 :1729+） | ✅ 命中：`TempWorkspace` dataclass（temp_dir/manifest/publish_paths），Run 沙箱物化既有行为 |
| C-5 | 全仓无 Repository / git 检出概念 | `docs/PHASE1_CLAWITH_CAPABILITY_AUDIT.md` §8 + 全仓 grep `Repository\|git_repo\|clone_url` | ✅ 复核：`backend/app/` 内 `class Repository` 无命中；唯一形近命中是 `_clone_workspace_to_staging`（见 C-10），非 git |
| C-6 | Department 仅元数据 + 知识可见性，无调度/派发消费方 | `models/org.py:12 OrgDepartment` + `services/experience_retrieval.py:91` | ✅ 命中：`OrgDepartment` 表名 `org_departments`（name/parent_id/path/member_count）；`experience_retrieval` 部门作用域检索 |
| C-7 | Task 无项目归属：agent_id 必需外键，无 project/parent/依赖字段 | `models/task.py:13-55`（agent_id :23；created_by :42） | ✅ 命中：`agent_id ... ForeignKey("agents.id"), nullable=False`；grep `project\|parent\|depend\|child\|work_item` 在 task.py **零命中** |
| C-8 | 全仓不存在 Project 域实体 / project 表 / Project Intake | `docs/PHASE1_CLAWITH_CAPABILITY_AUDIT.md` §8 + 全仓 grep `class Project\|__tablename__="projects"\|project_id` | ✅ 复核（**关键陷阱**）：`project_id`/`projects` **有命中但全部是外部集成元数据，非 Clawith 域 Project 实体**：① `services/agent_tools.py:25957 _get_vercel_quota_summary` 调 `api.vercel.com/v9/projects`（Vercel/Neon 部署助手读外部平台项目）；② `services/agent_runtime/tool_result_store.py:53` 与 `tool_execution.py:155` 的 `project_id/project_name` 是工具结果的**通用元数据追踪字段**（与 `database_name/region/git_ref/linked_repo` 并列，记录"这次工具操作动了外部项目的什么"），非域实体；③ 无 `class Project`、无 `projects` 表、无 Project Intake 服务（grep `project_intake\|intake_project` 零命中）。→ **Project 域实体 = MISSING 成立，且"看起来像 Project 字段"的 `project_id` 已被识别并排除** |
| C-9 | 物料写入的物理通道（分发实现载体，已存在）：存储 facade + write 接口 + key 前缀 | `services/storage_runtime/facade.py:30 get_storage_backend` + `base.py:51-77 StorageBackend`（write_bytes/write_text/read_bytes/list_dir）+ `utils.py:19-21 agent_storage_prefix/tenant_storage_prefix` + `utils.py:4-16 normalize_storage_key`（拒绝 `..`） | ✅ 命中：`get_storage_backend()` 单例 facade；`StorageBackend` 接口完整；`agent_storage_prefix(agent_id)` / `tenant_storage_prefix=enterprise_info_{tenant_id}`；`normalize_storage_key` 显式 pop `..` 段 |
| C-10 | 无 git 仓库接入能力；唯一形近命中 `_clone_workspace_to_staging` 是目录 staging 复制，非 git | `services/sandbox/local/subprocess_backend.py:808-822` | ✅ 命中：`_clone_workspace_to_staging` 用 `shutil.copy2`/`shutil.copytree` 把 work_path 复制到暂存 temp_dir（跳过 .venv/.tmp/_exec_tmp），**纯目录复制，无任何 git 语义**；全仓 `git clone\|pull_repo\|fetch_repo\|clone_url` 零命中 |
| C-11 | Workspace Lock 是运行时文件/编辑冲突控制（per-agent/per-path，无 Project 维度），**不构成任何 Project 级单写者机制** | `services/workspace_locking.py`（全文 91 行，Redis `SET NX EX` 短锁，键 `tenant:{t}:workspace-lock:{agent_id}:{path}`，TTL 默认 60s）+ `models/workspace.py:71-107`（`WorkspaceEditLock`：`scope_type IN ('agent','group')` + `user_id` + 心跳/过期） | ✅ 命中：Redis 锁的键空间只有 (tenant, agent, path) 三元组，**全仓 grep 无 `project` 维度**；编辑锁绑定 user 编辑会话（"Short-lived lock while a human is actively editing a workspace file"），消费者为 `workspace_collaboration.py` / `group_file_service.py` 的 group 修订/锁路径（C-3）。→ **既有锁 = `FACT` 现状；Project Single Writer = §B.4 的 `DESIGN PROPOSAL`，二者正交，锁的存在不蕴含单写者已实现** |

### 本卡复核说明（方法与边界）

- **复核对象**：两份源文档的全部 9 条 FACT + §F.4 的关键"无 git 能力"陈述。
- **复核手段**：只读 `read_file` / 全仓 `grep`，不修改任何源码、不 live 执行、不 commit（符合根任务 §二十六/§二十八 验收标准）。
- **发现的两处需 2B 注意的证据细节**：
  1. **`project_id` 假实体陷阱（C-8）**：`project_id`/`projects` 字串确实出现在代码里，但全部是
     Vercel/Neon 外部集成助手与工具结果元数据，**不是** Clawith 域 Project。2B 建 Project 表时
     **不得**误以为已有 `project_id` 字段可复用——那些字段属于外部工具元数据，语义完全不同。
  2. **`_clone_workspace_to_staging` 假 git 陷阱（C-10）**：名字带 "clone"，实为 `shutil` 目录复制。
     2B 补 git 获取能力时**不得**把此函数当 git 能力复用，它是沙箱 staging 的既有机制，语义正交。

*交叉引用：`docs/PROJECT_DOMAIN_V1.md`（§1–§7）、`docs/PROJECT_INTAKE_V1.md`（§1–§13）、
`docs/PHASE1_CLAWITH_CAPABILITY_AUDIT.md`（§2/§4/§5/§6/§8/§11/§13）、`docs/CAPABILITY_CONCEPT_MAP.md`、
`docs/PROJECT_TAKEOVER_MODE.md`（Phase 1 归档报告，基线 834d621）。*

---

## 本文作为 2B gate 的最终判定

- **§H 的 13 条歧义已全部由 §A–§G 消解**：#1 git 字段→独立 Repository；#2 工作区→模型 B 分发；
  #3 Agent 加入→Squad 复用 Group 原语（不自动组队）；#4 Task 关联→三段式 Work Item（V1 不动 Task 表）；
  #5 分析结果→Artifact/Knowledge 独立对象（不回写 Project）；#6–#7 关系→1:N≥0；#8 分析数据→非 Project；
  #9 运行环境→Workspace；#10 来源身份→"验证通过才落实体"；#11 git 缺口→RECEIVED/SOURCES_OK 挂起（pending-verifier）不误报、有界重试超限→REJECTED；#12 失败策略→封闭原因码 + 有界重试；#13 **Workspace Lock ≠ Single Writer**→§B.4 钉死两概念正交（锁 = `FACT` 运行时冲突控制；单写者 = `DESIGN PROPOSAL` 调度策略，V1 未实现）。
- **三个整合开放项（§G.1 任务图 / §G.2 Squad / §G.3 原因码+重试）已定稿**，2B 无需重辩。
- **11 条 FACT 证据（C-1~C-11）已对照活体源码复核通过**，并识别并排除了 2 处"形似 Project/形似 git"的陷阱（C-8 / C-10）；C-11 额外钉死"既有锁机制不构成 Project 单写者实现"。
- **门槛**：满足 §I.1 四条 + 通过一致性审查（t_5b1293ab）后，方可进入 §I.2 的 2B 首批（git 获取 + 三验证器 + Project/Repository 表 + Intake 服务，Task 表零改动）。
