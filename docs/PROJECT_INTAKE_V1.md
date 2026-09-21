# PROJECT_INTAKE_V1 — Project Intake 生命周期与最小数据（Phase 2A）

状态：DRAFT（设计稿，未实现、无数据库、无代码变更）
基线：Clawith @45fc701c（本仓 834d621）
上游依赖：docs/PROJECT_DOMAIN_V1.md（t_e023caf7，§4 Project→N Repository、§5 模型 B、单写者约定）
贡献分工：本文为 **t_1f24e7ca 的贡献**：Intake 定义与边界、生命周期状态、Project V1 最小字段、Source 支持矩阵。
后续卡片：正式整合与证据复核（t_d222b4f8）、一致性审查（t_5b1293ab）。
修订：t_6e8e5c52（M-1 对齐）：git 系来源验证挂起点统一为 **RECEIVED/SOURCES_OK + `pending-verifier` 标记**（有界 `SOURCE_UNREACHABLE` 重试，超限升级终态 REJECTED），清除全部旧「验证 → BLOCKED」表述（§6 / §7 / §10 / §13）。

> 证据约定（与 PROJECT_DOMAIN_V1.md 相同）：每条涉及 Clawith 现状的陈述标注 `FACT`（附 文件:行 证据）或 `DESIGN PROPOSAL`（设计决定，尚无代码）。

---

## 1. Intake 是什么 / 不是什么

### 定义（DESIGN PROPOSAL）

**Project Intake = 把"外部的一件事情"变成"公司认识的一个 Project 实体"的边界动作。**

它只做四件事：

1. **登记来源**：把来源描述（source_type + 定位信息）登记为 Repository 资产记录（§4/§6）。
2. **验证来源**：确认来源真实存在、可读（§6 的逐类型验证动作）。
3. **初始化实体**：创建 Project + Repository 记录，状态机从 RECEIVED 走起（§4）。
4. **移交下一环节**：完成后进入 ANALYZING 或等老板确认，不产生任何执行。

一句话：**Intake 是"接项目"，不是"做项目"，不是"看项目"。**

### 三段职责的区分（根任务 §十三，必须拆开）

| 环节 | 负责什么 | 产出 | V1 状态 |
|---|---|---|---|
| **Intake** | 来源验证、实体初始化、状态进入 | Project + Repository 记录（已验证） | 本文设计 |
| **Project Analysis** | 深度代码/文档分析：技术栈、依赖、启动方式、风险 | Analysis Artifact（独立对象，不塞进 Project 本体） | 只定义边界（§9），不实现 |
| **Project Execution** | 真正的干活 | Task → AgentRun（现有链路） | 现有能力，归 Phase 2B+ |

风险 4（根任务 §二十一）的防线就在这一刀：**Intake 的退出条件里不含任何"理解项目内容"的动作**。
FACT 依据：当前系统里"理解项目"只可能由 Agent 在 Run 内经 `execute_code` 手动做
（`backend/app/services/sandbox/`，7 个后端）——那是员工行为，不是系统 Intake 的职责。

## 2. Intake 输入 / 输出

### 输入（DESIGN PROPOSAL）

```text
IntakeCommand = {
  name            : string        （必须，人类可读的项目名）
  description     : string        （必须，一句话说清"公司要负责什么"）
  goal            : string        （必须，"做完了"的判定标准，可一句话）
  source          : { source_type, locator } × N   （N ≥ 0，见 §7）
  tenant_id       : UUID          （现有租户模型，多租户隔离沿用上现有约定）
  created_by      : user 身份      （沿现有 created_by 模式，FACT: models/task.py:42）
}
```

- source 可缺省（N=0，纯 manual 项目）；此时 Project 依然成立（PROJECT_DOMAIN_V1.md §4 决议 3/6）。
- 输入校验失败（缺 name/goal、locator 缺字段）→ 拒绝受理，不创建任何实体（§10）。

### 输出（DESIGN PROPOSAL）

1. **Project 记录**：id、name、description、goal、status=INITIALIZED、created_at。
2. **Repository 记录 × N**：每条含 source_type、locator（§7）、验证结果 verified=true。
3. **状态机推进记录**：RECEIVED → … → INITIALIZED 的每一步留痕（审计最小集，§4）。
4. **移交信号**：Project 进入"待分析 / 待确认"，Intake 动作结束。**不创建任何 Task、不指派任何 Agent、不写任何工作区。**

## 3. Intake 最小流程（逐步）

DESIGN PROPOSAL。每步标注它在状态机上的落点：

```text
1. 受理        接受 IntakeCommand，字段校验            → RECEIVED
2. 登记来源    按 source_type 生成 Repository 记录     → 仍在 RECEIVED（实体未确认前不算建成）
3. 验证来源    逐条执行 §6 的验证动作
               全部通过 ────────────────────────────→ SOURCES_OK
               任一失败 → 走 §10 错误处理（REJECTED 或挂起重试）
4. 初始化实体  创建 Project（status=INITIALIZED）
               绑定已验证的 Repository 记录集合
5. 移交        进入 ANALYZING 或 PENDING_CONFIRMATION
               （由老板/配置选择；Intake 自身到此为止）
```

关键约定：

- **步骤 4 之前不落 Project 实体**（或仅以 RECEIVED 草稿态存在）。来源未验证的"假项目"不进入公司视野——这是风险 6（不同来源身份不统一）的防线：Project 的出生时刻 = 来源验证通过时刻，身份从一开始就是"已验证来源 + 业务目标"。
- 步骤 5 的"移交"是一个**动作**，不是一个 Intake 子流程：分析属于 Analysis 环节，确认属于老板，Intake 对两者都不负责。

## 4. 生命周期状态（7 个，DESIGN PROPOSAL）

```text
RECEIVED            刚受理：已收到 IntakeCommand，实体未确认
        │
        ├──(验证通过)──► SOURCES_OK
        │                     │
        │                     └──► INITIALIZED      （Project + Repository 已建成）
        │
        └──(验证失败)──► REJECTED      ← 终态：输入不可修复 / 安全检查不过

INITIALIZED
        ├──► ANALYZING               （深度分析中，未来环节，见 §9）
        └──► PENDING_CONFIRMATION    （等老板确认：目标/范围/来源是否 OK）
PENDING_CONFIRMATION
        ├──► EXECUTING             （确认放行）
        ├──► REJECTED              （老板否决）
        └──► ANALYZING            （老板要求先分析）
ANALYZING ──(分析产出 Artifact)──► PENDING_CONFIRMATION
EXECUTING
        ├──► BLOCKED               （缺资源：凭据、访问、外部依赖）
        ├──► COMPLETED             （交付目标达成）
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
| 4 | `ANALYZING` | 深度分析中（V1 只定义边界，见 §9） |
| 5 | `PENDING_CONFIRMATION` | 等老板确认，人工门 |
| 6 | `EXECUTING` / `BLOCKED` | 执行中 / 执行阻塞（单写者约定：同时只有 1 个活跃执行 Agent） |
| 7 | `COMPLETED` / `ARCHIVED` / `REJECTED` | 终态组 |

核心流转 6~8 条边（上表），**状态总数 7（不含终态组展开）**，满足根任务"6~8 个核心状态"的要求。

约定：

- **EXECUTING 单写者**（PROJECT_DOMAIN_V1.md §5 决议沿用）：进入 EXECUTING 时记录执行 Agent，
  同一 Project 同时只有 1 个活跃执行 Agent；BLOCKED 释放后才可再进 EXECUTING。
- **REJECTED 带原因码**（§10），不是黑洞：被拒项目保留记录与原因，可人工重建新 Intake。
- **BLOCKED 必须有 blocker 描述 + 期望解除条件**，否则不许挂起（防"永久阻塞"假状态）。

## 5. Project V1 最小字段（三分类）

### 必须（Must Have）

```text
id                 UUID       主键
name               string     项目名（唯一性按 tenant 域内约定，不强制全局唯一）
description        string     一句话说清公司负责什么
goal               string     "做完了"的判定标准（验收依据的最小形式）
status             enum       §4 状态机
repositories       ref[]      → Repository × N（引用，不内联 git 字段；PROJECT_DOMAIN_V1.md §4）
tenant_id          UUID       沿用现有租户隔离
created_by         UUID       创建者（沿 task.py:42 模式）
created_at / updated_at / status_changed_at
```

理由（逐条）：

- `goal` 必须有：没有"做完标准"的 Project 无法在 PENDING_CONFIRMATION / COMPLETED 上形成判定，
  生命周期后半段就是空转。这是一等业务字段，不是文档字段。
- `repositories` 是引用集合：来源事实全在 Repository 侧（clone_url/branch/path 随来源迁移，
  Project 不动）——根任务 §十九的决议，避免"偷偷硬绑定"。

### 以后再加（Future）

| 字段 | 何时需要 | 归谁 |
|---|---|---|
| acceptance_criteria[]（结构化验收项） | goal 一句话不够用时 | 独立对象，挂 Project 引用 |
| delivery_deadline | 出现排期/调度需求时 | Project 或 Scheduler（Request Budget）侧 |
| 指派 Agent / Squad（project_lead_agent_id 等） | 团队接入落地时 | t_d222b4f8 整合卡裁定（根任务 §十八） |
| knowledge_refs / analysis_artifact_refs | Analysis 环节启动后 | Analysis 模块（§9） |
| department_id（归属部门） | 部门有调度语义时 | 现在 Department 只是元数据（FACT: models/org.py:12），挂了也无消费方 |
| budget / priority 业务权重 | 商业/资源约束出现时 | Resource Manager 侧 |

### 不应该放进 Project（Excluded，负面清单）

| 候选 | 排除理由 | 事实归属 |
|---|---|---|
| clone_url / branch / commit / remote | 来源事实属于 Repository，且来源会迁移（github→gitlab→zip），Project 不动 | Repository 实体（§6） |
| workspace 路径 / 项目工作目录 | Project 不拥有物理工作区（PROJECT_DOMAIN_V1.md §5 模型 B）；物理路径属于 Agent 工作区 | `{agent_id}/…` 存储子树（FACT: storage_runtime/utils.py:19-21） |
| 分析结果 JSON / 技术栈 / 目录结构 / 依赖清单 | 分析结果是 Analysis Artifact，塞进 Project 就是根任务风险 3 | 未来 Analysis 模块 |
| 任务列表 / 任务图 | V1 不动 Task 表（FACT: models/task.py:23，agent_id 必需、无项目字段）；项目任务图归整合卡 | 未来任务图（§17 裁定） |
| Agent 执行状态 / Run 引用 | 执行状态属于 AgentRun；Project 层经"归属引用"看执行，不内联 | 现有 AgentRun（Phase 1 审计 §2/§4） |
| budget / 薪资 / 绩效 / OKR | 根任务 §二十二 非目标 | 永不 |

## 6. Repository 接入边界

### 实体形状（DESIGN PROPOSAL）

```text
Repository = {
  id, project_id (ref),
  source_type : enum（§7）
  locator     : 按 source_type 的结构化定位信息
  verified    : bool + verified_at
}
```

- Intake 对 Repository 的全部职责 = **登记 + 验证**（§1 第 1/2 项）。
- Repository 的"获取"（拉取/复制物料）是 Intake **之后**的动作，归"物料分发"（§8），不属于 Intake 本身。

### 逐 source_type 的验证动作（DESIGN PROPOSAL）

| source_type | locator | V1 验证动作 | 备注 |
|---|---|---|---|
| `local_folder` | 绝对路径 | 路径存在 + 可读 + 非空（目录）；记录条目数 | 宿主机的"存在"由运维约定，系统只校验传入值合法 |
| `manual` | 无（或上传附件 id） | 记录创建事实即可（纯登记，无外部依赖） | 最低成本来源 |
| `document` | 文件路径 / 上传件 | 文件存在 + 可读 + 大小在界内 | 文档作为 Intake 物料直接进项目物料区（PROJECT_DOMAIN_V1.md §4 决议 6） |
| `github` / `gitlab` | owner/repo + 默认分支 | 仓库可达（HEAD 可取）+ 凭据可用 | **依赖尚未存在的 git 获取能力**（§7 风险说明） |
| `zip` | 文件路径 / 上传件 | 文件存在 + 可解开 + 顶层非空 + 无路径穿越（zip-slip 检查） | |
| `local_git` | 本地路径 | 路径是 git 工作树 + HEAD 可读 | |

- 验证失败分类：**可修复（transient，凭据过期 / 网络临时不可达 → 留在 RECEIVED/SOURCES_OK 挂起，打 `pending-verifier` 标记，有界 `SOURCE_UNREACHABLE` 重试）** vs **不可修复（permanent，路径不存在 → REJECTED + 原因码）**（§10 明细；M-1 对齐：验证失败永不落 BLOCKED，BLOCKED 仅执行态）。
- FACT 约束：当前代码里**不存在**任何 git clone/仓库接入能力（全仓检索 `clone_url|git_repo|repository_url`
  无命中；sandbox 里的 `subprocess_backend.py:808 _clone_workspace_to_staging` 是工作区目录 staging 复制，
  与 git 无关）。所以 §7 的 V1 支持矩阵直接由这条 FACT 决定。

## 7. Project Source 支持矩阵（V1 / 未来 / 暂不考虑）

DESIGN PROPOSAL，受 §6 的 FACT 约束：

| source_type | V1 支持 | 理由 |
|---|---|---|
| `manual` | ✅ 必须支持 | 零外部依赖，纯登记；也是 N=0 来源项目的形态 |
| `local_folder` | ✅ 必须支持 | 验证 = 目录存在可读，无需新能力；宿主文件夹是真实接管场景 |
| `document` | ✅ 必须支持 | 文件可读性检查，无新能力 |
| `zip` | ✅ 必须支持 | 同上（加 zip-slip 安全检查，§11） |
| `github` / `gitlab` | ⏳ 未来（Phase 2B 首批） | **验证需要"仓库可达 + 凭据"，这要求 git 获取能力——当前系统没有**（§6 FACT）。V1 枚举保留、流程放行：git 系来源验证不可完成 → **留在 RECEIVED/SOURCES_OK 挂起（打 `pending-verifier` 标记），有界 `SOURCE_UNREACHABLE` 重试，超限升级终态 REJECTED**；"缺 git 能力"是**环境能力缺口**，不是 5 个封闭原因码之一，**不产生 BLOCKED 迁移**（§10 明细；M-1 对齐），不会误报"已接入" |
| `local_git` | ⏳ 未来（Phase 2B 同批） | 需要 git 工作树检测 + 快照逻辑 |

约定：

- **source_type 是开放枚举**：V1 只实现 4 个的验证器；未知值在 Intake 命令校验阶段直接拒绝
  （根任务"状态/协议变体是封闭的"原则——对"未知 source_type"行为 = 显式拒绝，不猜测）。
- github/gitlab 登记进 Repository 记录不受影响（登记只是存 locator），受影响的只有"验证可达"这一步。
  这意味着**未来补 git 能力时，历史 Project 记录无需返工**——locator 早就存好了。

## 8. Workspace 初始化边界

DESIGN PROPOSAL，承接 PROJECT_DOMAIN_V1.md §5 模型 B：

- **Intake 不创建任何物理工作区。** Project 不拥有工作区（§5 Excluded 第 2 条）。
- **物料分发是 Intake 之后的显式动作**：把"已验证来源的物料"复制进**参与 Agent 的既有 `{agent_id}/` 子树**。
  FACT 落点：写入经 `get_storage_backend().write_bytes/write_text`
  （`backend/app/services/storage_runtime/facade.py:30`；接口见 `storage_runtime/base.py:51-77`），
  key 前缀经 `agent_storage_prefix(agent_id)`（`storage_runtime/utils.py:19-21`）——与 Agent 种子初始化
  （`agent_tools.py:1653-1673`）走同一条物理路径，**零 Runtime 改动**。
- 分发的目标 Agent 集合 = 当时被指派的 Agent（V1 单写者下通常 1 个）；
  分发结果（写了哪些 key）留痕在 Intake 审计记录里。
- Run 级临时物料化（`TempWorkspace`，`agent_tools.py:1689-1705`）是**执行环节**的既有行为，
  与 Intake/分发互不重叠：分发写持久存储，TempWorkspace 是 Run 沙箱的物化。

边界一句话：**Intake 管"登记与验证"，分发管"物料进 Agent 区"，TempWorkspace 管"Run 内物化"。三段各归各位。**

## 9. Analysis 下阶段边界（只定义，不实现）

DESIGN PROPOSAL（根任务 §十六的三分类预判，供整合卡裁定）：

| 分析产出 | 归属 | 理由 |
|---|---|---|
| 技术栈 / 目录结构 / 依赖清单 | **Analysis Artifact**（独立对象，带 generated_at + 生成 Run 引用） | 会过期、可重生成，不是项目本体事实 |
| 启动方式 / 测试入口 / 数据库 / API 约定 | **Project Knowledge**（未来沉淀区，挂 project_id 引用） | 长期有效的项目事实，沉淀复用 |
| 风险清单 | Analysis Artifact | 随分析轮次变化 |
| Project metadata（§5 必须字段里的任何一项被分析"顺手"更新） | ❌ 不允许 | 分析不改业务字段，只能产出引用 |

- Analysis 的触发 = 状态机 INITIALIZED → ANALYZING 边（§4）；
  完成 = 产出 Artifact + 状态进 PENDING_CONFIRMATION。
- **分析结果绝不回写进 Project 本体**（根任务风险 3 防线）。

## 10. 错误情况

DESIGN PROPOSAL。原则（AGENTS.md"misconfiguration fails at the earliest authoritative point"）：

| 失败点 | 错误 | 处理 |
|---|---|---|
| 命令校验 | 缺 name/goal；未知 source_type；locator 缺字段 | 拒绝受理，不建实体（RECEIVED 内失败，不落库） |
| 来源验证 | 路径不存在 / 文件损坏 / 仓库不可达（且无法修复） | `REJECTED` + 原因码（`SOURCE_NOT_FOUND` / `SOURCE_INVALID`） |
| 来源验证 | 凭据过期 / 网络临时不可达 | 挂起可重试（**留在 RECEIVED/SOURCES_OK**，打 `pending-verifier` 标记，有界重试；超限 → REJECTED `SOURCE_UNREACHABLE`。M-1 对齐：验证挂起永不落 BLOCKED） |
| 物料分发 | 写存储失败 | 分发是独立动作：失败 → BLOCKED（`DISTRIBUTION_FAILED`），不拖 Project 状态倒退；已分发部分留痕可重发 |
| 单写者冲突 | 第二个 Agent 请求进 EXECUTING | 拒绝并指向现有执行者（约定级冲突，非数据损坏） |

原因码是封闭集（V1：`SOURCE_NOT_FOUND` / `SOURCE_INVALID` / `SOURCE_UNREACHABLE` / `DISTRIBUTION_FAILED` / `SECURITY_REJECTED`），
新增需走整合卡。

## 11. 安全边界

DESIGN PROPOSAL + FACT 落点：

1. **路径穿越**：`local_folder`/`zip` 的 locator 过 `normalize_storage_key`（FACT: `storage_runtime/utils.py:4-16`
   拒绝 `..` 语义）；zip 解包做 zip-slip 检查（§6）。
2. **凭据不落 Project/Repository 记录**：git 来源的 token 走现有配置/密钥渠道（沿 backend 凭据惯例），
   Repository 只存 locator；验证记录存"验证过"，不存凭据值。
3. **写入前缀隔离**：物料分发只能写 `{agent_id}/…` 前缀下（FACT: 同上 utils.py:19-21 +
   模型可见路径解析 `workspace_paths.py:51-87` 的跨 Agent 前缀阻断假设）——分发动作不得绕过该前缀写其他 Agent 区。
4. **拒绝是终态**：`REJECTED` + `SECURITY_REJECTED`（如 zip 内路径穿越）不复用、不自动重试。

## 12. V1 非目标（Intake 明确不做的事）

根任务 §十五清单 + 本文补充，全部归"以后"：

- ❌ 深度代码分析 / 业务需求理解（§9，Analysis 环节）
- ❌ 自动拆任务 / 自动建任务图（Task 表 V1 不动，§17 归整合卡）
- ❌ 自动组队（Squad）/ 自动指派 Agent
- ❌ 自动开始修改代码、自动部署、自动 Review
- ❌ 物理项目工作区的创建（§8：分发 ≠ 建工作区）
- ❌ 共享物理空间 / 多 Agent 并行写（模型 A/C，PROJECT_DOMAIN_V1.md §5）
- ❌ git 获取能力本身（github/gitlab/local_git 的可达验证依赖它，Phase 2B 首批）

## 13. 验收核对（本文对照根任务 §二十）

"企业官网（GitHub）"示例走一遍：

```text
IntakeCommand{name=企业官网, goal=修复登录问题, source=[github:example/company-site, default_branch=main]}
  → RECEIVED → 登记 Repository(github, locator) → 验证：
      V1 现状：git 获取能力未落地 → 验证挂起（**留在 RECEIVED/SOURCES_OK，打 `pending-verifier` 标记，有界 `SOURCE_UNREACHABLE` 重试；超限 → 终态 REJECTED**）
      Phase 2B 后：HEAD 可达 + 凭据 OK → SOURCES_OK → INITIALIZED
  → PENDING_CONFIRMATION（老板确认范围）→ EXECUTING（单写者 Agent）
  → （Analysis 在确认前或后按 §9 边界运行，产出 Artifact，不回写 Project）
  → COMPLETED → ARCHIVED
```

结论：模型闭环，且**V1 与 2B 的差异只体现在"验证挂起"这一点上**——状态机与实体形状 2B 无需改动，
只补验证器实现。这验证了 §7 矩阵设计（枚举先行、能力后补）不产生返工。

---

## 附录：本文新增 FACT 证据（在 PROJECT_DOMAIN_V1.md 附录 1-7 之外）

8. 无 git 仓库接入能力（全仓）：
   检索 `clone_url|git_repo|repository_url|clone`（backend/app/）仅命中
   `backend/app/services/sandbox/local/subprocess_backend.py:808/837/1229 _clone_workspace_to_staging`
   ——该函数是**目录 staging 复制**（把 Agent 工作目录复制到暂存目录），与 git 语义无关。
   git 操作只能由 Agent 经 `execute_code` 在沙箱内手动执行（沿附录 5）。
9. 物料写入的物理通道（分发动作的实现载体，已存在）：
   `backend/app/services/storage_runtime/facade.py:30 get_storage_backend`；
   `base.py:51-77 StorageBackend`（write_bytes/write_text/read_bytes/list_dir）；
   key 前缀：`utils.py:19-21 agent_storage_prefix / tenant_storage_prefix`、`utils.py:4-16 normalize_storage_key`
   （拒绝 `..` 穿越语义——§11 第 1 条安全防线的事实基础）。

*交叉引用：docs/PROJECT_DOMAIN_V1.md（§4/§5 决议）、docs/PHASE1_CLAWITH_CAPABILITY_AUDIT.md §8、
docs/PROJECT_TAKEOVER_MODE.md。*
