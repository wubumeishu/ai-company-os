# PHASE1_CLAWITH_CAPABILITY_AUDIT

基线：834d621（Clawith @45fc701c，dataelement/Clawith main）
方法：只读静态代码追踪（Inspect → Trace → Verify → Report），未做任何 live 执行、未修改任何源码。
每条结论均给出 文件:行 证据；行号为基线 834d621 中的实际位置。

---

# 1. Executive Summary

Clawith 不是"聊天型 Agent 平台"，它已经具备**部分真正的工作执行能力**，但距离接管一个真实软件项目还差关键环节。

能真正做的（有代码证据）：
- 一次真实的 Agent Run 主链完整：请求 → 持久化命令 → LangGraph 确定性执行 → 模型调用 → 工具执行 → 结果结算 → 验证 → 完成 → 投递，无缺失步骤。
- Agent 能**真正读写磁盘文件**（aiofiles 实盘 I/O、原子写 + 版本守卫 + DB 修订记录），能**真正执行命令**（bubblewrap 沙箱内真实子进程，stdout/stderr/退出码全捕获，bwrap 缺失时 fail closed）。
- Task 既是被管理对象，也是**真实的执行驱动**：Task→Run 与 Run→Task 双向链路都存在且幂等。
- A2A：Agent A 可以把工作交给 Agent B，B 真正执行并把结果返回给 A。

做不了的（代码确认缺失）：
- 没有 Project 业务实体、没有 Project Intake（无 git 检出/仓库接入）。
- 没有统一的 Artifact / Evidence 对象（只是工具结果上的类型化字段 + 修订记录）。
- 没有独立 Review Agent → Rework 闭环（verification gate 不等于 review；且 gate 在内部错误时 **fail open 直接放行**，verification.py:637-645）。
- 没有调度引擎消费 supervision 周期任务（字段已存，无消费者）。

总判定：**PARTIALLY READY**。单 Agent、单任务、预上传文件的修改-执行-验证场景今天就能跑；多 Agent、多仓、项目级的真实软件工作流还不行。

---

# 2. 真实执行链

（只画真实存在的链路，无 [MISSING] 步骤；每一步均有 file:line。）

```
User
 ↓
实际入口：websocket chat (api/websocket.py:851) / task (services/task_executor.py:43)
          / 渠道机器人 / trigger / a2a / heartbeat
 ↓
intake：enqueue_chat_runtime (services/agent_runtime/chat_intake.py:479)
        —— v2 未选中时返回 None，调用方必须 fail closed，无旧路径回退
        （chat_intake.py:504-515；task 侧 task_executor.py:192-195 同样停止不回退）
 ↓
Run 创建：RuntimeCommandIntake.start_run (agent_runtime/adapter.py:245)
        → register_run_with_start (agent_runtime/persistence.py:321)
          原子创建 AgentRun + pending AgentRunCommand + run_created 事件
 ↓
Agent 被调用：RuntimeCommandWorker.run_once (agent_runtime/command_worker.py:928/721)
        PostgreSQL 每线程 advisory lock 串行化
 ↓
AgentRun 执行：LangGraphRuntimeDriver.execute (agent_runtime/langgraph_driver.py:395)
        → build_agent_runtime_graph (agent_runtime/graph.py:241)
          control_guard → compact / model / tool / verify / wait / terminal
          PostgreSQL checkpointer，durability="sync"
 ↓
Model：RuntimeModelStepService.complete_once (agent_runtime/model_step_service.py:1997)
        → complete_llm_once (services/llm/single_step.py:123)
        → create_llm_client 工厂 (services/llm/client.py:2572，provider 无关)
 ↓
Tool：RuntimeToolStepService.execute_pending (agent_runtime/tool_step_service.py:1963)
        逐次预留 AgentToolExecution 行 + lease；execute_builtin_tool_outcome
        (services/agent_tools.py:4123)；未迁移的旧 handler 被拒（typed-only）
 ↓
Workspace / Sandbox：
        文件：agent_tools.py:3165 (读) / :3293 (写) → storage_runtime local(S3/aiofiles)
              + workspace_collaboration.py:536 原子写 + 修订记录
        命令：services/sandbox/ 7 个后端；subprocess 后端跑 bwrap 沙箱子进程
 ↓
Tool Result：_settle_outcome (agent_runtime/tool_step_service.py:1146)
        归一化 ToolExecutionOutcome，私有二进制经 tool_result_store 归档（含内容哈希）
 ↓
Verification：completion gate (agent_runtime/verification.py，1050 行；
        node_executor.py:103 VerificationResult，pass|repair 有界修复预算=2)
        ⚠ 内部错误时 _fail_open → outcome="pass"（verification.py:637-645）
 ↓
Completion：checkpoint 提交后才发布事件 (agent_runtime/checkpoint_side_effects.py)
        终端处理器链 (worker_service.py:304-319)：Task 完成回写
        (task_completion.py:56)、触发器/心跳/A2A 完成处理
 ↓
最终结果：ChannelDeliveryWorker 投递到来源渠道；产品事实由
        product_reconciler.py 对账
```

结论：主链**完整**，单一定性脊柱，无并行旧执行路径（v2 未启用时整体停止）。

---

# 3. Agent 能力（能否真正"动手"）

## 读取（READY）
- 工具：`list_files` / `read_file`（builtin_tool_definitions.py 注册）
- 执行函数：`_read_file_outcome`（agent_tools.py:3165）
- 实际访问方式：`LocalStorageBackend.read_bytes`（storage_runtime/local.py，aiofiles 磁盘读）；S3 走 put/get_object。真实磁盘 I/O，typed outcome，有测试覆盖（tests/test_agent_tools_storage_workspace.py）。

## 写入（READY）
- 执行函数：`_write_file_outcome`（agent_tools.py:3293）→ `write_workspace_file`
  （workspace_collaboration.py:536）
- 行为：`write_bytes_if_match` 原子 tmp+rename + 版本守卫 + 本地镜像 + DB 修订行
  （workspace_file_revisions，含 before/after 内容）；人在编辑时 human-lock 拒绝 agent 写。
- **是真正修改磁盘文件，不是生成一段文本给用户。**

## 命令 / 代码（READY）
- `execute_code` → 真实子进程（services/sandbox/local/subprocess_backend.py）
- 沙箱：bubblewrap（pid/ipc/uts unshare）、rlimits（CPU/AS/FSIZE/NOFILE/NPROC）、
  环境变量清洗、stdout 1MB / stderr 500KB 捕获、**检查退出码**、无 bwrap 时 fail closed
- 结果保存：typed ToolExecutionOutcome 落 agent_tool_executions 表；有 WebSocket 流
- 后端注册：subprocess/docker/e2b/judge0/codesandbox/self_hosted/aio_sandbox 共 7 个
  （services/sandbox/registry.py）

## Workspace（READY，隔离成立）
- 每个 Agent 有自己的存储子树：`{STORAGE_LOCAL_ROOT}/{agent_id}/` 前缀
  —— 跨 Agent 访问被前缀阻断；绝对路径在模型可见层直接拒绝；
  路径穿越返回 403；租户共享 enterprise_info 对 agent 只读。

## "改 hello.txt" 模拟（不执行，仅源码确认）

| 步骤 | 状态 | 证据 |
|---|---|---|
| 读取项目文件 | READY | read_file → 磁盘读（agent_tools.py:3165） |
| 理解任务 | READY | Run 的 goal 由入口注入（task 目标见 task_executor.py:29-40） |
| 调用文件修改工具 | READY | write_file（agent_tools.py:3293） |
| 修改真实文件 | READY | 原子版本守卫写 + 修订记录 |
| 读回修改后的文件 | READY | read_file 复读 |
| 确认修改成功 | READY | 复读/execute_code grep + TaskCompletionGate 核对 workspace 证据引用 |

**总体：PARTIALLY READY** —— 三个核心步骤（读/改/验）均可执行可验证；
但真实项目文件必须**预先上传**进该 agent 的 workspace 子树，
因为**不存在** Project 实体或 git 检出入口。

---

# 4. Agent Run

- 创建位置：`register_run_with_start`（persistence.py:321）——intake 在调用方事务内原子创建
  AgentRun + AgentRunCommand(pending) + run_created 事件，**不 commit**（commit 边界归入口）。
- 状态：run_kind ∈ (foreground, background, delegated, orchestration)；
  delivery_status；`agent_runs.source_type` ∈ (chat, trigger, task, a2a, heartbeat)
  （models/agent_run.py:33-44 check constraints）；自引用 parent_run_id 支持嵌套 run。
- 事件：agent_run_events（run_created / 终端事件 / waiting-resumed），
  只在 checkpoint 权威提交后发布（checkpoint_side_effects.py）。
- 结果：settled 工具结果 → 线程消息 + tool_result_store 归档；最终答案经
  渠道投递（channel_delivery.py / delivery.py / answer_stream.py）。
- 是否持久化：**是**。PostgreSQL checkpointer 每步同步落盘（durability="sync"），
  命令收据 `mark_command_applied`（persistence.py:763）可幂等对账。

---

# 5. Task

Task **既是管理记录，也是真实执行驱动**（双向链路已验证）：

- 谁创建：API `POST /agents/{agent_id}/tasks`（api/tasks.py:63，todo 任务在请求内同步
  入队，兜底 `asyncio.create_task(execute_task)` tasks.py:103-106）；agent 自带工具
  `_manage_tasks`（agent_tools.py:9822-9898，DB + 仅当 tasks.json 已存在时同步）。
- 谁负责：`Task.agent_id` 为必需 FK（models/task.py:23）。`assignee` 列（task.py:41）
  **未被 Runtime 消费**——入口用 `created_by` 作为 origin/actor（task_executor.py:109-110）。
- 状态：pending/doing/done + TaskLog（task_logs）；
- 完成判定：`TaskRuntimeCompletionHandler`（task_completion.py:56，接线于
  worker_service.py:316）——终端 checkpoint：todo→done + completed_at；
  failed/cancelled→回 pending；supervision→pending 可再触发；uuid5 幂等收据。
- 与 Agent Run 关系：正向 `enqueue_task_runtime`（task_executor.py:43）
  构造 `StartRunCommand(source_type="task", source_execution_id=f"task:{task.id}")`，
  唯一索引 uq_agent_runs_source_execution 保证幂等；任务字段（标题/描述/监督信息）
  经 `_task_goal`（task_executor.py:29-40）成为 Run 的 goal，**真正驱动模型**。
- 缺失：无 Project 归属、无父子任务、无依赖图；supervision 周期字段
  （remind_schedule 等，task.py:46-49）只有存储 + 手工 `POST /trigger`
  （tasks.py:163-185，"for testing"），**没有调度引擎消费** → 周期执行 MISSING。

判定：Task = 真实执行任务（不是纯待办），但不是 Project Task System。

---

# 6. Agent-to-Agent

- A2A 存在且**真正执行**：builtin 工具 `send_message_to_agent`
  （builtin_tool_definitions.py:583）→ `RuntimeA2AService.execute`
  （agent_runtime/a2a_runtime.py:774），`agent_runs.source_type` 含 'a2a'；
  完成语义 a2a_completion.py + A2ARuntimeCompletionHandler；测试
  test_agent_runtime_a2a.py（11 个测试函数）。
- 即：A 把消息交给 B → B 以自己的 Run 真正执行 → 结果经完成处理回到 A 的线程。
  不是"发一句、回一句"。
- ⚠ 双路径风险：旧版 `api/advanced.py`（/collaborate/delegate、/handover）
  与 Runtime A2A 并存，需 ADR 裁定权威路径。

---

# 7. Verification（Completion Gate ≠ Code Review）

- 验证对象：**任务是否真的产生结果** + **外部证据是否存在**——
  LLM 独立判定原始 run goal vs 候选最终答案 + 证据引用
  （workspace:/published-page:/imagekit:/http，经 reference_exists 校验，
  verification.py:580-620）；`pass|repair`，修复预算有界
  （node_executor.py:571 max_verification_repairs=2，超限即 fail）。
- 它不等于独立 Review：没有"第二个 Agent 审查代码/测试/文档"的闭环。
- **风险（本报告最重要的 flag）**：任何内部错误路径 `_fail_open`
  直接返回 `outcome="pass"`（verification.py:637-645）——gate 会被绕过，
  对"真实成果"可信度构成架构级风险。
- 现有 retry 语义：修复 episode 在同一 Run 内重跑工具/模型（上限 2）；
  无 "review 反馈 → 新 execution" 的返工路径。

---

# 8. Project

**MISSING。** 全仓确认（基线 834d621）：
- 无 Project 模型 / 无 project 表 / 无 Project API / 无项目级 git 仓库接入。
- Task 的 FK 只有 agent_id；不存在"项目 → 任务"归属。
- 相关基础件已存在，可作为 Project Intake 的原材料：文件上传/工作区读写、
  execute_code（可跑 git 命令但无内建检出流程）、workspace、agent 工具、
  experience/skills 知识库。
- 判定：Project 业务实体 = MISSING；Project Intake = MISSING（基础件 PARTIAL）。

---

# 9. Artifact / Evidence

**PARTIAL —— 没有统一体系，只有可作证据的分散事实。**

- 无 Artifact 模型/表。Artifacts 以**类型化字段**存在于工具结果上：
  `artifact_refs` / `artifact_content_hash`（agent_runtime/tool_result_store.py:94，
  存于 agent_tool_executions），被 verification gate 消费。
- 可作证据的现有事实：
  1. `agent_tool_executions`（typed outcome + 结果归档 + 内容哈希）
  2. `workspace_file_revisions`（before/after 内容的修订行）
  3. `agent_run_events`（run_created/终端事件，checkpoint 提交后发布）
  4. `published_pages`（可发布成果）
- 与"AI 说完成了"的区别：系统目前能证明到 B 类（Run→ToolExec→文件变更→命令结果→
  证据引用）的**中间态**——链路存在，但没有把证据聚合成可独立审计的 Artifact 对象；
  且 completion gate fail open 削弱了证据链的终局可信度。

---

# 10. Review / Rework

- 独立 Review（Agent 审查 Agent 的提交/代码/测试）：**MISSING**。
  没有 review/reviewer 模型、没有退回-修改-重查闭环。
- 返工能力现状（只记录事实，不设计）：
  - Run 内验证修复：有界 repair episode（≤2 次，node_executor.py:571/1209/1226）——
    重跑的是失败任务的验证循环，不是 review 反馈驱动的新 execution。
  - Run 级重试：command 收据幂等对账（persistence.py:763/253）；
    失败任务回 pending（task_completion.py），可再次入队新 Run（手工/工具触发）。
  - "Review feedback → new execution → Review again"：**MISSING**。

---

# 11. Knowledge（当前知识来源，逐项区分）

| 来源 | 状态 | 证据 |
|---|---|---|
| 对话历史 | 存在 | runtime thread messages + 冻结输入快照（langgraph_driver.py:88 ContextBuilder） |
| Agent Persona/技能 | 存在 | active-skill prompt 注入（model_step_service.py `_with_runtime_tools` :418-440） |
| Workspace 文件 | 存在 | 文件工具 + 项目文件可被模型读取 |
| 数据库 | 存在 | 各模型表；经验条目/技能文件 |
| Experience（结构化经验库） | 存在 | experience_entries/references；retrieval 走 experience_retrieval.py（部门可见性 :91） |
| Skills | 存在 | skills/skill_files + ClawHub 安装（api/skills.py） |
| 向量 RAG / 项目文档知识 | **缺失** | 基线无向量检索；项目级文档知识无落点 |

结论：知识 = 结构化条目 + 技能 + 会话上下文；**无向量 RAG**，且没有"项目文档"这一类知识。

---

# 12. 能力对照表（目标模型 vs 现状）

判定仅用 EXISTS / PARTIAL / MISSING / UNKNOWN。

| 未来需要的能力 | Clawith 当前状态 | 真实代码证据 | 判定 |
|---|---|---|---|
| Employee | Agent 即"数字员工"（models/agent.py:20 "Digital employee (Agent)"）；有权限/凭据/模板 | models/agent.py；agent_credentials；/agents API | EXISTS |
| Agent | 完整 durable runtime（LangGraph 主链） | 见 §2 全链 | EXISTS |
| Department | 仅元数据：org_departments/org_members，Feishu 同步，无 CRUD API，仅知识可见性消费 | models/org.py:12；org_sync_service.py；experience_retrieval.py:91 | PARTIAL |
| Squad | 组原语存在：groups/group_members + 群聊/群文件服务；orchestration run 带 system_role='group_planning' | models/group.py:24,59；group_chat_service.py；agent_run.py:65-66 | PARTIAL |
| Project | 无实体/表/API | 全仓无 Project model（§8） | MISSING |
| Task | 真实执行驱动，双向 Task↔Run；单 agent 作用域，无父子/依赖 | task_executor.py:43；task_completion.py:56；models/task.py:23 | EXISTS |
| Execution | 完整主链，checkpoint 持久化 | §2 | EXISTS |
| Artifact | 无 owner 对象；仅工具结果上的类型化字段 | tool_result_store.py:94 | PARTIAL |
| Evidence | 分散证据事实（工具结果/修订行/run 事件/证据引用），无统一审计对象 | §9 | PARTIAL |
| Review | 无独立 Review Agent/闭环 | §10 | MISSING |
| Rework | 有界 run 内修复(≤2) + 失败回 pending 可再入队；无 review 反馈驱动返工 | node_executor.py:571；task_completion.py | PARTIAL |
| Knowledge | 结构化经验库 + 技能 + 会话上下文；无向量 RAG | §11 | PARTIAL |
| Manager | 无 Manager 实体；但存在 planning 层原语：run_kind='orchestration' + group_planning topology（graph.py:96-103） | agent_run.py:38,65；graph.py:96 | PARTIAL |
| Project Intake | 无 git 检出/仓库接入；文件上传与 workspace 基础件在 | §8 | MISSING |

---

# 13. 当前最大缺口（只列影响"真实项目接管/执行/成果/审查/交付"的）

1. **Project 实体 + Project Intake（git/仓库/文件接入）** —— 没有它，"把一个真实软件项目交给 Clawith"的第一步就不成立。
2. **独立 Review → Rework → Review 闭环** —— 现在只有完成验证（且 fail open），没有第二双眼睛，无法对"成果"做真正审查。
3. **TaskCompletionGate fail open（verification.py:637-645）** —— 错误路径直接放行 pass，证据链终局可信度受损。
4. **统一 Artifact / Evidence 对象** —— 证据分散在 4 类表中，无法独立审计"Agent 真正产出了什么"。
5. **supervision 调度引擎** —— 周期/监督任务只存不跑。
6. **双路径债务** —— 旧版 A2A（api/advanced.py）与 legacy 内联 task 执行并存，权威路径未裁定。

---

# 14. 核心十问（直接回答）

1. **Agent 能不能真正读取文件？** 能。agent_tools.py:3165 → storage_runtime 磁盘读。
2. **能不能真正修改文件？** 能。agent_tools.py:3293 → 原子版本守卫写 + 修订记录（:536）。
3. **能不能真正执行代码/命令？** 能。bubblewrap 沙箱内真实子进程，stdout/stderr/退出码全捕获；无 bwrap fail closed。
4. **能不能真正产生可验证的执行结果？** 能到中间态：typed 结果 + 修订行 + 事件 + 证据引用链存在；但没有统一 Artifact/Evidence 对象，且 completion gate fail open。
5. **Task 是否真正驱动 Agent Execution？** 是。Task→Run 正向（task_executor.py:43）+ Run→Task 反向（task_completion.py:56）双向存在且幂等；任务字段构成 Run goal。
6. **能不能把工作交给另一个 Agent 并让对方真正执行？** 能。send_message_to_agent → RuntimeA2AService（a2a_runtime.py:774），B 以自己的 Run 执行并回传结果。
7. **有没有 Project 这个真正的业务实体？** 没有。MISSING（§8）。
8. **有没有 Artifact / Evidence 体系？** 没有统一体系；有分散证据（§9）。PARTIAL。
9. **有没有独立 Review → Rework → Review 闭环？** 没有。有 run 内有界修复（≤2），无独立审查。MISSING。
10. **距离目标链还缺哪些核心环节？** Project、Project Intake、独立 Review、Review 驱动 Rework、统一 Artifact/Evidence、Manager 实体化（现仅有 planning 原语）。

---

# 15. 当前结论

**Clawith 现在可以真正做：**
- 单 Agent 单任务的文件修改-命令执行-结果验证闭环（hello.txt 场景 READY）。
- 多渠道驱动的持久化 Agent Run（chat/trigger/task/a2a/heartbeat）。
- Agent 间真实委派（A2A 执行 + 结果回传）。
- 结构化经验/技能的知识注入与沉淀。

**Clawith 还不能真正做：**
- 接管一个真实软件项目（无 Project、无 git/仓库 intake，文件须预上传）。
- 对项目成果做独立审查与返工闭环。
- 周期性/监督性自动工作。
- 对"成果"给出可独立审计的证据体系（gate 还会 fail open）。

**可直接作为《媪溪 AI Company OS》基础的能力：**
- 完整 durable Agent Run 主链（§2）——执行底座。
- 真实文件系统/命令执行 + workspace 隔离 + 修订记录——动手能力。
- Task 双向执行驱动 + 幂等收据——任务底座。
- A2A 执行路径、经验库/技能、group/planning 原语——协作与知识底座。

**后续必须改造/新建的：**
- Project 实体 + Project Intake；统一 Artifact/Evidence；独立 Review→Rework 闭环；
  supervision 调度；completion gate fail-closed；双路径债务清理（ADR）。

---

*来源交叉引用：docs/AGENT_RUN_EXECUTION_CHAIN.md（t_70a95b59）、
docs/CAPABILITY_CONCEPT_MAP.md（t_d018ea9c）、t_7f257285 / t_fd1f93f0 /
t_90165532 证据评论。本报告未提交 git（按任务要求 Do not commit）。*
