# 真实 Git 项目接管方式说明

回答一个问题：**"把一个真实 Git 项目交给妗湘公司以后，公司究竟怎样接管它？"**

依据：Phase 1《Clawith 真实工作能力审计》（`docs/PHASE1_CLAWITH_CAPABILITY_AUDIT.md`，基线 834d621 / 官方 Clawith @45fc701c）。所有结论带代码证据，区分"今天就能跑"与"需要新建"。

---

## 一句话结论

> **今天没有"原生接管"——没有 Project 实体、没有 git 仓库接入（Intake）。**
> 一个真实 Git 项目要进妗湘公司，只能走**手工搭桥**：把代码弄进某个 Agent 的 workspace，公司就能在它上面真正读、改、跑、验；但项目本身不被系统"认识"，成果也没有统一证据对象，更没有独立审查闭环。

接管能力 = **执行力真实存在 × 项目层整体缺失**。

---

## 一、今天的实际接管流程（手工搭桥版，每步有代码证据）

```
① 准备项目代码          ② 创建/选择员工        ③ 下达任务              ④ 员工真正干活
  clone 到你机器/        Agent = 数字员工，       POST /agents/{id}/      读/写/执行/验证
  上传到 Agent 的        有凭据、技能、人设       tasks → 真实入队       全部发生在
  workspace 子树           (models/agent.py)      (api/tasks.py:63)      Agent 自己的工作区
                                                          ↓
⑧ 成果与证据             ⑦ A2A 协作              ⑥ 完成判定             ⑤ 执行主链
  4 类分散证据表          员工A 派活给 员工B，     completion gate        请求→持久化命令→
  (见 §三)                B 以自己的 Run 真       (verification.py)      LangGraph→模型→工具
  无统一 Artifact        正执行并回传结果         ⚠ 内部错误 fail-open   →结果结算→投递
                          (a2a_runtime.py:774)      (verification.py:637)
```

### ① 项目代码进公司（唯一的手工环节）

- 系统**没有** Project 模型、没有 project 表、没有 git 检出接口（审计 §8，全仓确认 MISSING）。
- 替代路径两条：
  1. **文件上传**：把代码文件传进目标 Agent 的 workspace（`{STORAGE_LOCAL_ROOT}/{agent_id}/` 子树）；
  2. **机器层预 clone + 员工在沙箱里跑 git**：Agent 的 `execute_code` 是真实子进程（bubblewrap 沙箱，无 bwrap 时 Windows 主机回退 subprocess），可以跑 `git clone`、跑构建、跑测试——**但这是员工自己手抖出来的，不是系统的 Intake 流程**，没有任何"项目注册"动作。
- 因此"把项目交给公司"目前 = **把它交给某一个员工的工作区**，项目不是公司的一等公民。

### ②③ 员工与任务（真实存在）

- Agent 即数字员工：有权限、凭据、模板、技能（`models/agent.py:19`，注释原文 "Digital employee (Agent)"）。
- Task 是**真实执行驱动**，不是待办空壳：`POST /agents/{id}/tasks` 同步入队 `enqueue_task_runtime`（task_executor.py:43），任务字段变成 Run 的 goal 真正驱动模型；Run 结束时 `TaskRuntimeCompletionHandler`（task_completion.py:56）回写 Task 状态，双向幂等。
- 局限：Task 只挂在单个 Agent 上（FK agent_id），**没有"项目→任务"归属**，没有父子任务、没有依赖图。

### ④⑤ 动手能力（审计最强结论：READY）

| 动作 | 是否真实 | 证据 |
|---|---|---|
| 读项目文件 | ✅ 真磁盘 I/O | `read_file` → aiofiles（agent_tools.py:3165） |
| 改项目文件 | ✅ 原子写 + 版本守卫 + 修订记录 | `write_file`（agent_tools.py:3293 → workspace_collaboration.py:536） |
| 跑构建/测试/命令 | ✅ 真子进程，stdout/stderr/退出码全捕获 | `execute_code` → bubblewrap 沙箱（无 bwrap 时 Windows 回退 subprocess，隔离变弱） |
| 结果落库 | ✅ typed outcome + 归档 + 内容哈希 | `agent_tool_executions`（tool_result_store.py:94） |

**所以"员工能不能真干活"——能，改 hello.txt 到跑测试，单员工单任务闭环今天就是通的。**

### ⑥ 完成判定（最大风险点）

- completion gate 会独立判定"任务是否真产生结果 + 外部证据是否存在"（verification.py:580-620，修复预算 ≤2 次）。
- ⚠️ **任何内部错误路径 `_fail_open` 直接放行 `pass`（verification.py:637-645）**——出错时反而判通过，接管可信度的架构级风险。
- 且 gate ≠ 代码审查：没有"第二个员工审第一人的提交/测试"的闭环。

### ⑦ 多员工协作（真实存在）

- `send_message_to_agent` → `RuntimeA2AService`（a2a_runtime.py:774）：员工 B 以自己的 Run 真正执行，结果回传 A。不是发个消息就算。
- 部门（org_departments）只有元数据 + 知识可见性；群组/planning 原语（run_kind=orchestration）存在但无 Manager 实体。

### ⑧ 成果去向（分散、无统一对象）

| 证据事实 | 位置 |
|---|---|
| 工具结果（typed + 哈希） | `agent_tool_executions` |
| 文件改动 before/after | `workspace_file_revisions` |
| 运行事件 | `agent_run_events` |
| 可发布成果 | `published_pages` |

没有 Artifact/Evidence 统一对象——**"员工干了什么"要跨 4 张表人工拼**，且因 gate fail-open，终局可信度打折。

---

## 二、"公司接管"的完整图景（今天 vs 目标）

```
真实 Git 项目
   │
   ├─ 今天 (PARTIALLY READY) ────────────────────────────────────
   │   手工上传/预 clone → 某员工 workspace → 该员工读改跑验
   │   （单员工、单任务、项目不被系统认识、成果分散、无审查）
   │
   └─ 目标 (需要新建) ──────────────────────────────────────────
       项目 Intake（git 检出/仓库接入，项目=一等公民）
       项目级任务图（归属/父子/依赖，跨员工分派）
       独立 Review → Rework → Review 闭环
       统一 Artifact/Evidence（可独立审计的成果对象）
       completion gate fail-closed
       supervision 调度引擎（周期/监督任务今天只存不跑）
```

---

## 三、五个缺口按接管链路排序（审计 §13）

| # | 缺口 | 对"接管真实项目"的直接影响 | 现状 |
|---|---|---|---|
| 1 | **Project 实体 + Project Intake** | 没有它，"把项目交给公司"第一步就不成立 | MISSING |
| 2 | **独立 Review → Rework 闭环** | 成果没有第二双眼睛，无法真正审查 | MISSING |
| 3 | **completion gate fail-open** | 出错反而放行，证据链终局不可信 | 风险（verification.py:637-645） |
| 4 | **统一 Artifact/Evidence 对象** | 跨 4 表拼证据，无法独立审计"员工产出了什么" | PARTIAL |
| 5 | **supervision 调度引擎** | 周期/监督任务只存不跑 | MISSING（字段在，无消费者） |

另有双路径债务：旧 A2A（api/advanced.py）与 Runtime A2A 并存，需 ADR 裁定权威路径。

---

## 四、可立即复用的底座（审计 §15）

- 完整 durable Agent Run 主链（PostgreSQL checkpointer 每步同步落盘，命令收据幂等对账）→ **执行底座**
- 真实文件读写 + 命令执行 + workspace 隔离 + 修订记录 → **动手能力**
- Task 双向驱动 + 幂等 → **任务底座**
- A2A 执行路径 + 经验库/技能 + 群/planning 原语 → **协作与知识底座**

**结论：地基是实的，缺的是"项目层"——把上面的地基接进"项目→任务→审查→交付"这条公司级链路，正是 Phase 2 的主线。**

---

*交叉引用：docs/PHASE1_CLAWITH_CAPABILITY_AUDIT.md（14 节全量审计）、docs/CAPABILITY_CONCEPT_MAP.md（11 概念代码定位）。*
