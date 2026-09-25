# WORKSPACE ISOLATION & SECURITY BOUNDARY AUDIT — Phase 2E Parallel Audit (Wave 1)

状态：AUDIT（只读，无代码变更）
审计卡片：t_29528ad2（aco-architect）
基线：main @ 7497bf7a（worktree `wt/t_29528ad2`，含 Phase 2D 文档收敛）
上游卡：t_b0bb2f7c（Phase 2E Root）；下游消费卡：t_180370c7（Phase 2E 技术规格）
前置参考：docs/MATERIALIZATION_WORKSPACE_ISOLATION_AUDIT_V1.md（Phase 2B-3，基线 5830624）

> 证据约定：每条现状陈述标注 `FACT`（附 文件:行，均为本会话在当前树上实读）或
> `DESIGN GAP` / `UNKNOWN`（尚无代码或无法从代码裁定）。
> 自 2B-3 审计基线（5830624）以来，本领域有两个产品 commit：
> `845d7750`（materialization service 落地）与 `6912f53a`（git acquisition），
> 影响文件仅 `intake_security.py`（+144 行：credential guard、locator 门禁、
> 状态机闭集）与 `storage_runtime/local.py`（6 行微调）。锁/路径/tenant 注入
> 核心机制未被触碰，2B-3 §1.5/§1.9 的结论在本基线上仍然成立（已逐条复核）。

---

## 1. Agent workspace 如何被 provision（物理布局与 Run 级物化）

- `FACT` 权威源 = 存储 key 命名空间。`storage_runtime/utils.py`：
  `agent_storage_prefix(agent_id)` 即 `{agent_id}/…`；租户共享前缀为
  `enterprise_info_{tenant_id}`（`tenant_storage_prefix`）。
- `FACT` 本地根：`agent_tools.py:148-149` `WORKSPACE_ROOT = Path(settings.STORAGE_LOCAL_ROOT or settings.AGENT_DATA_DIR)`；
  `config.py:113-116` 默认 `STORAGE_BACKEND="local"`、`AGENT_DATA_DIR == STORAGE_LOCAL_ROOT == _default_agent_data_dir()`。
  agent 子树根 = `WORKSPACE_ROOT / {agent_id}`（`_agent_workspace_root`，2B-3 审计锚点 agent_tools.py:2262；本文件 2.8 万行，行号随版本漂移，以符号为准）。
- `FACT` 子树布局惯例（seed/工具面归纳）：`soul.md`、`focus.md`、`HEARTBEAT.md`、
  `memory/`、`skills/`、`workspace/`（uploads 落 `workspace/uploads/`）；
  `agent_tools.py:165` `TEMP_WORKSPACE_DEFAULT_PATHS = ["skills","memory","workspace","focus.md","soul.md","HEARTBEAT.md"]`。
- `FACT` 无显式"创建 agent 时建目录"的独立 provisioning 调用：目录按需创建
  （local.py `_atomic_write_bytes` 内 `path.parent.mkdir(parents=True)`；Run 物化时
  `agent_tools.py:1736-1740` 对 `workspace/memory/skills` 做 `mkdir(parents=True, exist_ok=True)`）。
  即 **workspace = 存储 key 命名空间 + 按需落盘**，无预建/分配步骤。
- `FACT` Run 级物化：每个 tool 调用前 `_prepare_temp_workspace` 将 agent 存储子树物化进
  `tempfile.TemporaryDirectory`（预算 50MB/文件、500MB/总，`agent_tools.py:150-151`）；
  沙箱路径 `services/sandbox/local/run_workspace.py:72-91` `use_run_workspace`
  **per-Run 物化一次**，身份（agent/tenant/session/mode/paths）变化即 `RuntimeError`（防身份漂移）。
  Run 结束 `flush_temp_workspace` 用 `write_bytes_if_match`（物化时 version_token 或 require_absent）
  发布回存储，内容一致视为收敛（幂等，2B-3 §1.7）。
- 含义：Task 执行时"获得正确 Agent workspace" = 写对存储 key `{agent_id}/…` + 过锁 + 记 revision；
  Run 自动在下次物化时看见。**无宿主裸文件系统旁路**（S3/fallback 下宿主只是镜像）。

## 2. 锁（三层，语义不同，勿混用）

| 层 | 机制 | 位置 | 语义 |
|---|---|---|---|
| 逻辑编辑锁 | Redis `SET key owner NX EX 60`，key = `tenant:{tid}:workspace-lock:{agent_id}:{normpath}`（无租户则无 `tenant:` 前缀） | `workspace_locking.py:35-91` | 快速失败：`workspace_locks` 任一 busy 即 `RuntimeError("Workspace lock busy: {path}")`，**不等待、不排队**；成功则逆序释放 |
| 跨进程串行锁 | local 后端 fcntl 目录锁（root dir fd 上 `flock(LOCK_EX\|LOCK_NB)` + 10ms 轮询） | `storage_runtime/local.py:196-220` | Unix 上跨进程串行**全部 mutation**；**无 fcntl 平台（Windows dev）退化为 no-op**（进程内正确性仍成立） |
| 人类编辑锁 | DB `workspace_edit_locks`（TTL 90s，`(scope_type, scope_id, path)` 唯一） | `models/workspace.py:71-104`；acquire/release/get_active 于 `workspace_collaboration.py:134-213` | agent/system actor 写前若命中人类锁 → 该文件 busy/skip（`workspace_collaboration.py:558-569`），**不静默覆盖人类未提交编辑** |

- `FACT` 乐观并发兜底（文件级）：`write_bytes_if_match(WriteCondition(require_absent|version_token))`，
  S3 用原生 `IfNoneMatch: *` / `IfMatch` ETag（2B-3 §1.4/§1.8 锚点 s3.py:278-367）。
- `DESIGN GAP W1` **Project 单写者（单写保护）不存在**。上述三层防的是"同文件写冲突"，
  不提供项目/Task 级排他。Root 卡 §十一 明令不得偷造 Project Single Writer；
  现状语义 = **文件级 fast-fail conflict（非等待/非排队）**。

## 3. 隔离层级（tenant / agent / project / workspace）

### 3.1 tenant 层
- `FACT` `core/middleware.py:58-65` `TenantContextMiddleware`：轻量解 JWT `tenant_id` claim → `_tenant_ctx` ContextVar（校验职责在 `get_current_user`）。
- `FACT` `dao/base.py:139-174`：`do_orm_execute` 事件对**所有** tenant-scoped 模型（含 lazy 加载、`include_aliases=True`）自动注入 `tenant_id == _tenant_ctx` SELECT 过滤——漏写业务过滤也不泄露他租户行。
- `FACT` `dao/base.py:218-240` `add_scoped`：写侧 tenant 对齐（对象 tenant ≠ 上下文 tenant → `RuntimeError`）。
- `FACT` 退化路径：`dao/base.py:242-246` `get_scoped` 在 `_tenant_ctx=None`（后台忘包 `tenant_context`）时**退回裸 PK 查询**——跨租户读静默放行（已知窄路径，见 §6 G1）。
- `FACT` `core/permissions.py:556-558`：`agent_obj.tenant_id != user.tenant_id` → **403**（agent 级跨租户硬闸）。
- `FACT` `intake_security.py` `verify_read_access`（创建者或同租户 admin）+ `verify_tenant_scope`（记录租户 == 操作者租户），`api/projects.py:56-69` 已作为 Project 读侧双门禁。

### 3.2 agent 层
- `FACT` `workspace_paths.py:51-87` `resolve_agent_visible_path`：`enterprise_info*` 前缀解析到**本租户**企业共享区（`enterprise_info_{tenant_id}`，租户内共享、只读参考语义），其余一律 `relative_to` 约束在 agent workspace 根内，逃逸 → `WorkspacePathError`。**agent A 的 key 无法拼出 agent B 子树内的目标**。
- `FACT` local 后端 `_full_path`（`local.py:42-48`）：`normalize_storage_key` + `resolve()` 前缀校验，穿越 → **403**。
- `FACT` model 面：`agent_tools.py:232-249` `_agent_relative_path_error` 拒绝绝对路径/URI（`/…`、`C:/`、`scheme://`）——模型可见路径必须 agent-root-relative。
- `FACT` `skills/` 完整性依赖：Run 物化缺 skills 快照 → `SkillSnapshotIncompleteError`（2B-3 §1.3 锚点 agent_tools.py:1763-1768）——项目物料必须排除出 `skills/`（M2 裁定已生效：物料落 agent 子树根级 `projects/` 前缀，见 materialization service）。

### 3.3 project / workspace 层
- `FACT` `project_materialization_service.py:297-323`（`845d7750` 落地）：service 入口即 fail-closed 三连闸——
  `verify_tenant_scope(project.tenant_id, user.tenant_id)`；
  agent 租户缺失或 ≠ project 租户 → `MaterializationSecurity("target agent tenant does not match the project tenant")`；
  **任一 tenant 字段为 None → `MaterializationSecurity("materialization has no tenant context; refusing to write")`（G9 建议已实现为硬闸，非文档化约定）**；
  且仅 `INITIALIZED` 可物料化、未 verified / 未过 git-acq 门禁的 repo → `SOURCE_NOT_READY`（防御纵深：坏行 fail closed，不连坐兄弟 repo）。
- `FACT` 物料写入走 `intake_security` 单一守卫（host-path 形状 / Zip Slip 检测式判定，
  `intake_security.py:443-557` credential guard + 封闭原因码）——与 Intake 同一守卫契约，service 内不重写规则。
- `FACT` 留痕：`WorkspaceFileRevision`（`models/workspace.py:28-68`，agent/group 双 scope，CHECK 约束锁定 scope 身份）+
  materialization `actor_type="system"` revision + `AuditLog`（`project_materialization_service.py:886,981-1005`，审计写失败不吞主结果——窄 catch + 说明，主结果保留）。

## 4. 并发裁定：多个 Agent 能否安全修改同一 workspace？

**结论：不需要在现有三层之上再引入外部锁层；但"同一 workspace"的含义必须拆开。**

1. **不同 Agent（不同 `{agent_id}` 子树）**：物理隔离 + 路径解析双闸（§3.2），
   `Task A → Agent A 的 X` 与 `Task B → Agent B 的 Y` **可安全并行**（root 卡 §十 情形 1 成立）。
2. **同一 Agent、多个 Run/Task 并发**：
   - 每个 Run 物化进**独立** `TemporaryDirectory`（`use_run_workspace` per-Run + 身份校验），Run 间无共享可变态；
   - 发布回存储 = 文件级条件写（version_token / require_absent）+ 内容一致幂等收敛（2B-3 §1.7）；
   - Unix local 部署：fcntl 目录锁把所有 mutation 跨进程串行化（`local.py:196-220`）；
   - 因此同 agent 多 Run **无数据撕裂风险**，冲突以"条件写 conflict / 锁 busy"形式上报而非静默覆盖。
3. **同一文件多写者（跨进程/跨 Run 同时写）**：Redis `workspace_locks` 快速失败——
   输家拿 `RuntimeError("Workspace lock busy: {path}")`（`workspace_locking.py:86`），
   **现状语义 = conflict（fail-fast），不是等待/排队**。对 Phase 2E 的含义：
   Task Execute 命中 busy = `WORKSPACE_CONFLICT`（root 卡 §十三 failure 分类之一），
   属 transient 可重试（root 卡 §十四 允许），但**系统内不存在等待/排队设施**——
   排队语义若要引入属净新增（超出 V1 最小边界）。
4. **平台差异（UNKNOWN W2）**：无 fcntl 平台（Windows dev host）跨进程锁退化为 no-op
   （`local.py:20-23,200-204` 注释明示）——单进程正确性仍由条件写/乐观并发兜底，
   但"跨进程串行"保证仅在 Unix 部署形态成立。生产部署形态为 UNKNOWN（不发明假设，同 2B-3 UNKNOWN 1 逻辑）。

## 5. Fail-closed 行为清单（未授权跨租户 / 跨 workspace 访问）

| 攻击面 | 现状行为 | 证据 |
|---|---|---|
| 跨租户读任意 tenant-scoped 行 | ContextVar 注入 SELECT 过滤；行不存在 = 404 语义（None），不泄露 | `dao/base.py:139-174` |
| 跨租户写 | `add_scoped` tenant 不匹配 → `RuntimeError` | `dao/base.py:218-240` |
| 访问他租户 agent | `check_agent_access` → **403** | `core/permissions.py:556-558` |
| 读他租户 Project | `verify_read_access` + `verify_tenant_scope` 双门禁 → 403/拒绝 | `intake_security.py:498-540`（ReadForbidden/TenantScopeViolation 异常）+ `api/projects.py:56-69` |
| 物料注入到异租户 agent | 租户不匹配或缺租户上下文 → `MaterializationSecurity` 拒写 | `project_materialization_service.py:297-302` |
| 跨 agent 子树路径拼接 | `resolve_agent_visible_path` → `WorkspacePathError`（逃逸即拒） | `workspace_paths.py:51-87` |
| 存储 key 穿越（`../` 等） | local 后端前缀校验 → **403** | `local.py:42-48` |
| 模型面绝对路径/URI | `_agent_relative_path_error` 拒绝，不进存储 | `agent_tools.py:232-249` |
| 人类正在编辑的文件被 agent/system 写 | 人类锁命中 → 该文件 busy/skip，不覆盖 | `workspace_collaboration.py:558-569` |
| 无租户上下文的后台路径读 | **非 fail-closed**：`get_scoped` 退回裸 PK（静默放行）——已知残余风险，靠调用侧显式 `verify_tenant_scope`/`tenant_context` 补闸 | `dao/base.py:242-246`（G1） |

## 6. DESIGN GAP / UNKNOWN（移交 t_180370c7 裁定）

- **G1 — 非 tenant-context 退化读**。`get_scoped` 无上下文时裸 PK（`dao/base.py:244-246`）。
  缓解已在 materialization 落地为入口硬闸（§3.3），但 Phase 2E 的 **Task/Run/Agent 读路径
  若由后台队列触发，规格必须把"显式 `tenant_context` + 入口 `verify_tenant_scope`"写成强制前置**，
  不能依赖中间件（中间件只覆盖 HTTP 请求）。
- **G2 — 锁 busy 无排队语义**。现状 = fast-fail conflict。规格需明确 Task Execute 命中
  `WORKSPACE_CONFLICT` 时：reject / 显式重试（transient，有界）/ 或引入排队（净新增，非 V1）。
  建议 V1 = 有界重试 + 审计，不排队（对齐 root §十四）。
- **G3 — 平台锁保证集**。fcntl no-op 平台无跨进程串行（§4.4）。规格应声明 V1 保证集合 =
  local(Unix) + 条件写兜底，Windows dev 不作为并发正确性声明面。
- **G4 — Project/Task 级单写者不存在**（W1）。同 agent 多 Task 并行时，文件级冲突靠
  §4.2/§4.3 机制兜底；**不得**为 Phase 2E 偷造 Project Single Writer（root §十一）。
  规格应写明：同 agent 并行 Task 的冲突面 = 文件级 conflict + 有界重试；不同 agent 天然并行安全。
- **G5 — S3 条件写依赖 ETag**（2B-3 §1.4）：无 ETag → `RuntimeError`（s3.py:311-312）。
  V1 若部署 S3/fallback，Task→Run 的发布路径需单独验收。
- **UNKNOWN W2 — 生产部署形态**（local vs s3+fallback）不发明假设；规格按"后端集合 = 已验收集合"表述。

## 7. 对 Phase 2E Execute 链路的架构建议（供规格卡采纳）

1. **前置检查（fail-closed）**：Execute 入口复用 §5 表中的既有门禁——
   `check_agent_access`（agent 归属）+ `verify_tenant_scope`（task/project 租户）+
   材料就绪（materialization 完成，`SOURCE_NOT_READY` 同码复用）+ 目标 workspace 锁探测。
   任一失败 → 不 enqueue（root §五 "不要偷偷 enqueue"）。
2. **失败分类落位**（root §十三）：`WORKSPACE_CONFLICT`（Redis busy / 人类锁命中，transient，
   有界重试）、`ASSIGNMENT_FAILED`（agent 不存在/异租户，**不可重试**）、
   `QUEUE_FAILED`（enqueue 本身失败，transient）。
3. **审计**：Execute/Assign 动作写 `AuditLog`（project/task/agent/actor/action/timestamp/result，
   materialization 的 `actor_type="system"` + 窄 catch 保主结果先例可复用，
   `project_materialization_service.py:886,981-1005`）；不记 credential。
4. **证据链**：Task → Agent → Run 的可回答性 = `WorkspaceFileRevision.group_key`
   稳定操作身份模式（2B-3 §1.8 先例：`materialize:{project_id}:{repo_id}:{agent_id}`）
   + Run 既有 result/verification——不建第二套 Artifact 系统（root §十五）。

---

## 8. 回归边界

本卡未修改任何产品代码。审计覆盖面 = `workspace_locking.py` / `workspace_paths.py` /
`workspace_collaboration.py`（lock 相关段）/ `storage_runtime/{local,facade}.py` /
`dao/base.py` / `core/permissions.py` / `core/middleware.py` / `intake_security.py` /
`project_materialization_service.py` / `sandbox/local/run_workspace.py` / `models/workspace.py`。
下游回归面 = `test_workspace_reconciliation.py` + `test_files_api_storage.py` +
materialization 测试 + ruff/pyright（root §二十六 回归基线）。
