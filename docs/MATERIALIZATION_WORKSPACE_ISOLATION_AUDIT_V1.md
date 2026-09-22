# WORKSPACE & ISOLATION CODE AUDIT — Materialization Preflight (Phase 2B-3)

状态：AUDIT（只读，无代码变更）
审计卡片：t_df317a55（aco-architect）
基线：worktree `wt/t_df317a55` @ 5830624（main，含 Phase 2B-2 intake 合并）
供卡：t_c672b2c2（Materialization 安全设计规格，architect）/ t_025cda02（实现，builder）

> 证据约定：每条现状陈述标注 `FACT`（附 文件:行）或 `DESIGN GAP`（尚无代码，需裁定）。
> 第一原则（t_6748fd76 §0）：全部结论基于真实代码检索，不基于 README/注释/旧报告。

---

## 1. 12 项检查点逐条事实

### 1.1 Project / Repository 当前代码

- `FACT` `backend/app/models/project.py:34-166`：`Project`（10 值状态枚举、`tenant_id` NOT NULL、f067 后含 `rejection_reason/rejection_detail`）、`Repository`（`source_type` 7 值枚举、`locator` JSON 自由 dict、`verified/pending_verifier/retry_count`）。两模型均无 `agent_id`、无物料目标字段。
- `FACT` `backend/app/dao/project_intake_dao.py:28-232`：`ProjectDAO.get_scoped_with_repositories`（`selectinload` repositories）、`list_for_user_scoped`、`add_project_with_repositories`、`transition`、`reject`、`mark_sources_ok`；`RepositoryDAO.mark_verified / mark_pending_verifier / clear_pending_verifier / bump_retry_count`。均继承 `TenantScopedBaseDAO`。
- `FACT` 无任何"物料目标 agent"记录：Materialization 的 `target agent` 不在任何现有表/字段中（DESIGN GAP M1，§3）。

### 1.2 Intake 当前代码

- `FACT` `backend/app/services/project_intake_service.py`：公开面仅 `create_intake` / `validate_sources`（service 层不做 zip 解压、不建 workspace、不建 Task，docstring 明示）。
- `FACT` `backend/app/api/projects.py:56-69`：`_load_authorized_project` = `verify_read_access`（创建者或同租户 admin）+ `verify_tenant_scope`（记录租户 == 操作者租户）。这是 Materialization API 应复用的**同一道读侧门禁**。
- `FACT` 状态门禁事实：Materialization 只应接受 `status == "INITIALIZED"`；`SOURCES_OK` 仍可能带 `pending_verifier` 源，不得物料化。

### 1.3 Agent workspace 实现（Model B 物理布局）

- `FACT` `backend/app/services/agent_tools.py:149`：`WORKSPACE_ROOT = Path(settings.STORAGE_LOCAL_ROOT or settings.AGENT_DATA_DIR)`；`agent_tools.py:2262-2264` `_agent_workspace_root(agent_id) = WORKSPACE_ROOT / str(agent_id)`。
- `FACT` 存储 key 即 `{agent_id}/{相对路径}`（双斜杠分隔、UUID 前缀）；agent 子树内布局惯例（由 seed/工具面归纳，FACT 于 `agent_tools.py:1653-1673, 165-173`）：
  - `soul.md`、`focus.md`、`HEARTBEAT.md`（根文件）
  - `memory/memory.md`
  - `skills/`（Run 沙箱物化必须完整，缺则 `SkillSnapshotIncompleteError`，`agent_tools.py:1763-1768`）
  - `workspace/`（agent 工作区，uploads 落 `workspace/uploads/`，`api/upload.py:66-78`）
  - `tasks.json` 为**受保护路径**：`workspace_collaboration.py:752` 禁止 move。
- `DESIGN GAP M2`：项目物料注入 agent 子树的**子目录命名方案不存在**。候选：`workspace/projects/{project_id}/`（agent 可见 work 区）vs `{agent_id}/projects/{project_id}/`（agent 子树根，与 `workspace/` 平级，不进 TempWorkspace 默认物化路径，避免 500MB 总预算被吞）。裁定归 t_c672b2c2。

### 1.4 workspace storage（后端实现）

- `FACT` `backend/app/services/storage_runtime/facade.py:30-56`：`get_storage_backend()` 三态：local / s3 / fallback（s3+local 读回迁移）。接口面（`base.py:51-141`）：`exists / is_file / is_dir / list_dir / read_bytes / write_bytes / delete / delete_tree / stat / get_version / write_bytes_if_match / delete_if_match / local_path_for / presign_download_url`。
- `FACT` `local.py:42-48`：`_full_path` 以 `normalize_storage_key` + `resolve()` 前缀校验拒绝穿越（403）。
- `FACT` `local.py:241-261` `_atomic_write_bytes`：temp 文件 + `fsync` + `os.replace` 原子发布；`local.py:196-220` `_mutation_lock`：`fcntl.flock` 于 root 目录 fd，**跨进程**串行化全部 mutation；`local.py:16-23` fcntl 缺失（Windows 开发机）时锁退化 no-op（进程内正确性仍成立）。
- `FACT` `s3.py:278-367`：条件写用 S3 原生 `IfNoneMatch: *`（require_absent）与 `IfMatch`（version_token，依赖 ETag）；无本地文件锁语义（单写者由条件写承担）。
- `FACT` 关键非对称：`delete_tree` 在 local 是 `shutil.rmtree`，在 S3 是 `delete_objects` 批量（`s3.py:221-237`）；**抽象层没有"复制整树"原语**（DESIGN GAP M3，§3）。

### 1.5 workspace lock / isolation（双层锁，语义不同）

- `FACT` **跨进程**：`local.py:_mutation_lock`（fcntl 目录锁，见 1.4）——local 后端所有 mutation 串行。S3 后端无对应，靠条件写。
- `FACT` **逻辑编辑锁（Redis，运行时写冲突）**：`backend/app/services/workspace_locking.py:42-91`——`acquire_workspace_lock(agent_id, path, owner_token, tenant_id, ttl=60s)` = `SET key owner NX EX`；key = `tenant:{tid}:workspace-lock:{agent_id}:{normpath}` 或 `workspace-lock:{agent_id}:{normpath}`（无租户时）；`workspace_locks(agent_id, paths, tenant_id=...)` 上下文：排序、逐个获取、任一 busy 抛 `RuntimeError("Workspace lock busy: {path}")`（**非等待，快速失败**），逆序释放。
- `FACT` 消费方（`workspace_collaboration.py:673, 789, agent_tools.py:1893`）：`delete_workspace_file`、`move_workspace_path`、`flush_temp_workspace` 均在锁内操作。
- `FACT` **人类编辑锁（DB，另一物）**：`WorkspaceEditLock`（`models/workspace.py:93-105`，TTL 90s，`acquire/release/get_active` 于 `workspace_collaboration.py:134-213`）——agent/system actor 写/删/移动前检查"人类正在编辑"（`enforce_human_lock=True` 时）。
- **语义裁定（卡片 §7 硬规则）**：Workspace Lock（上述两层）≠ Project Single Writer。本仓 Project 单写者仍无实现；Materialization 用 workspace lock 防**运行中 agent 写冲突**，但**不得**宣称它提供了项目级排他。Redis 锁 busy 时现状语义 = 抛错（快速失败），不是等待/排队——卡片 §7 的"等待/返回 conflict/失败"三选一，现状代码对应 **conflict（快速失败）**。

### 1.6 agent-specific storage prefix

- `FACT` `storage_runtime/utils.py:19-24`：`agent_storage_prefix(agent_id) = normalize_storage_key(agent_id)`；`tenant_storage_prefix(tenant_id) = "enterprise_info_{tenant_id}"`。
- `FACT` `workspace_paths.py:51-87` `resolve_agent_visible_path`：以 `enterprise_info` 开头的相对路径解析到 `enterprise_info_{tenant_id}/` 企业共享区，其余一律约束在 agent workspace 根内（`relative_to` 拒绝逃逸）。跨 agent 前缀写入在此被阻断——这是"agent A 的物料不得写入 agent B 子树"的既有事实基础。
- `FACT` 企业区共享语义：`workspace_collaboration.py` 的 `record_group_revision`（scope_type=group）与 `agent_tools.py:8192` `_tool_storage_key` 的 enterprise 分支证明 `enterprise_info_{tenant_id}` 是租户内共享前缀——**Materialization 的目标是 agent 子树，不是企业区**（Model B），企业区仅租户级只读参考。

### 1.7 TempWorkspace（Run 级物化，既有行为）

- `FACT` `agent_tools.py:1689-1777`：`_prepare_temp_workspace` 把 `{skills, memory, workspace, focus.md, soul.md, HEARTBEAT.md}` 物化进 `tempfile.TemporaryDirectory`；预算：单文件 50MB、总量 500MB（`agent_tools.py:150-151`）；`manifest` 记录每文件 `storage_key/base_version_token/base_hash/size`。
- `FACT` `agent_tools.py:1877-1937` `flush_temp_workspace`：把 Run 内的改动**发布回存储**——`workspace_locks` 内逐文件 `write_bytes_if_match`，条件 = 物化时 `base_version_token`（失败模式）或 `require_absent`；`isolated_output` 模式走 `overwrite`（`workspace_policy.py:44-47` 定义 publication_conflict_mode）。
- `FACT` `agent_tools.py:180-229`（`flush` 后半段收敛逻辑）+ `_stable_identical_storage_version`：冲突后若内容一致视为收敛（幂等收敛既成事实，`agent_tools.py:1931` 起）。
- **对 Materialization 的含义**：Run 启动时 TempWorkspace 从存储**重新物化**——只要物料写进的是 `{agent_id}/…` 存储 key，下次 Run 自动可见，**零 Runtime 改动**（与 `PROJECT_INTAKE_V1.md` §8 一致）。持久写 = 唯一的注入路径；临时文件系统（bwrap 等）不落盘。

### 1.8 文件复制/写入能力（现成 API 清单）

| 能力 | API | 位置 | 性质 |
|---|---|---|---|
| 单 key 原子写 | `write_bytes / write_text` | `base.py:71-75`（local 原子实现 `local.py:241`） | 现成 |
| 条件写（幂等/冲突） | `write_bytes_if_match(WriteCondition)` | `base.py:102-117`；S3 原生 If-None-Match/If-Match `s3.py:278-327` | 现成 |
| 条件删 | `delete_if_match` | `base.py:119-135` | 现成 |
| 树遍历读 | `list_dir` + `read_bytes` 递归 | `agent_tools.py:1802-1842` `_materialize_storage_path_with_budget` 已是范本 | 现成（范本） |
| 宿主目录树读 | `shutil.copy2/copytree` | `subprocess_backend.py:808-818` `_clone_workspace_to_staging`（仅沙箱内部，**非公共 API**） | 宿主侧 |
| 整树"复制/移动"原语 | **不存在** | 全仓检索 `copy_to_storage/copy_tree` 零命中（非 test） | **DESIGN GAP M3** |
| 本地物化辅助 | `ensure_local_path(key)` | `facade.py:59-64` | 现成 |
| 字节 hash | `content_hash_bytes` | `base.py:144` | 现成（幂等比较用） |
| 人类锁检查 | `get_active_lock(db, agent_id, path)` | `workspace_collaboration.py:199-213` | 现成 |

**结论**：Materialization 的"复制"必须**组合**现成件：宿主侧 `shutil`/`zipfile` 安全解包 → 逐文件 `list_dir/read_bytes/write_bytes_if_match`（存储侧）+ `workspace_locks` + revision 记录。无现成"一键复制树"服务，但**不需要新造文件系统框架**——所有原语齐全。

### 1.9 权限 / tenant 模式

- `FACT` `dao/base.py:139-174`：`do_orm_execute` 事件对**所有** `tenant_id NOT NULL` 模型自动注入 `where tenant_id == _tenant_ctx`（SELECT 全覆盖，含 lazy 加载，`with_loader_criteria include_aliases=True`）。`Project/Repository` 自动受此保护。
- `FACT` `dao/base.py:218-240` `add_scoped`：写入时 tenant 对齐（对象 tenant ≠ 上下文 tenant → RuntimeError）。
- `FACT` 后台任务必须 `tenant_context(tenant_id)` 包裹（`dao/base.py:177-193`）——**Materialization 若由 API 触发走中间件自动注入；若将来队列化，忘包 tenant_context 即退化为无租户过滤**（`get_scoped` 在 tenant=None 时退回 `super().get` 裸 PK 查询！`dao/base.py:244-246`）。
- `FACT` agent 侧访问：`check_agent_access(db, current_user, agent_id)`（`core/permissions.py:519`，files API 全部端点用它，`api/files.py:210-216`）。Materialization 目标 agent 的授权应**复用此门禁**（创建者/同租户 admin 语义），不得新造。
- `FACT` `intake_security.verify_read_access / verify_tenant_scope`（`intake_security.py:521-556`）：Project 读侧双门禁，`api/projects.py:68-69` 已用——Materialization API 直接沿用。

### 1.10 既有 materialization / staging 能力

- `FACT` 全仓不存在"来源物料 → agent 存储"的注入能力（检索 zero-hit，1.8）。现存的两个"物化"互不相关：
  - TempWorkspace（1.7）：agent 存储 → 临时沙箱，Run 级；
  - `_clone_workspace_to_staging`（1.8）：宿主临时目录间复制，沙箱执行隔离用。
- 即 **Materialization 是净新增能力**，但构建块全部现成。

### 1.11 当前 Agent Run 如何找到 workspace

- `FACT` `agent_tools.py:2285+` `_run_with_temp_workspace`：每个 tool 调用前 `_prepare_temp_workspace(agent_id, tenant_id, paths)`；`_agent_workspace_root` 仅在需要宿主路径时解析（`agent_tools.py:2262`）。
- `FACT` `sandbox/local/run_workspace.py:72-91` `use_run_workspace`：**per-Run 物化一次**（module 级 `_run_workspace_tasks` + identity 变化即 `RuntimeError`），legacy（无 run_id）路径一次性物化后立即 cleanup。
- `FACT` 沙箱内 guest 路径映射：`workspace_policy.py:36-42`（`/workspace/…` = agent 存储相对路径），publish 目标 `workspace/output/{session_id}`。
- 含义：物料只要进了存储 key `{agent_id}/…`，Run 自动看见（1.7 结论）；**宿主裸文件系统里的 agent 目录不是权威源**（S3/fallback 后端下宿主只是镜像，`workspace_collaboration.py:75-77` `_should_mirror_to_local_filesystem`）。

### 1.12 A2A / Agent runtime 如何访问项目文件

- `FACT` 检索 a2a / agent_runtime 目录：无 project/material 相关引用；agent 访问文件的唯一通道 = tool 面（`_tool_storage_key` / `resolve_agent_visible_path` / files API / TempWorkspace 物化）。
- 含义：Materialization 的交付物对 runtime **完全透明**——不需要任何 runtime 侧改动，只要写对了 key、过了锁、记了 revision。

---

## 2. 可用 API 清单（Materialization 可直接调用）

**存储写**：`get_storage_backend()` → `write_bytes_if_match(key, data, condition)`；`WriteCondition(require_absent=True)`（不覆盖）/ `version_token`（乐观并发）；冲突返回 `ConditionalWriteResult(ok=False, conflict=True, current_version)`。

**路径校验（三层，用途不同，勿混用）**：
1. `normalize_storage_key`（`utils.py:4-16`）：`..` 段弹出式归一化——**用于 key 归一，不用于检测**（弹出会掩盖穿越，`intake_security.py:15-22` 模块 docstring 明示这一设计取舍）。
2. `intake_security.check_host_path / check_zip_slip / path_traversal_detail`（`intake_security.py:189-336`）：**检测式**安全判定，封闭原因码。Materialization 复用 `check_zip_slip` 于解包前（与 Intake 同一守卫，单守卫契约）。
3. `resolve_path_within_root / resolve_agent_visible_path`（`workspace_paths.py:21-87`）：目标侧"必须落在合法根内"的硬约束——Materialization 目标 key 拼接后应过此校验（防御纵深）。

**锁**：`workspace_locks(agent_id, paths, tenant_id=...)`（快速失败语义，1.5）；`get_active_lock`（人类编辑锁，写前检查，1.8 表）。

**幂等比较**：`content_hash_bytes`（sha256，`base.py:144`）+ `StorageVersion.token`（etag/mtime/size 复合，`base.py:33-35`）。

**provenance 记录**：`record_revision(db, agent_id, path, operation, actor_type, actor_id, before/after)`（`workspace_collaboration.py:306-333`）+ `WorkspaceFileRevision.group_key`（既有"稳定操作身份"槽位，见 `prepare_group_runtime_revision` `workspace_collaboration.py:386-482` 的用法——group runtime 用它承载 Tool Ledger 身份；agent scope 下 `group_key` 目前仅 user-autosave 合并用）。

**预算约束（必须遵守）**：`TOOL_MATERIALIZE_MAX_FILE_BYTES=50MB`、`TOOL_MATERIALIZE_MAX_TOTAL_BYTES=500MB`（`agent_tools.py:150-151`）——物料落 `workspace/` 子树时会被 Run 物化预算截断；DESIGN GAP M2 的命名选择与此直接相关。

---

## 3. DESIGN GAP 汇总（归 t_c672b2c2 裁定，本卡不裁）

**M1 — "target agent" 无持久记录。** 现有模型（Project/Repository）不含指派 agent 字段（`models/project.py` 全文确认）。卡片 §12 的 API（`project_id + target agent`）意味着指派是**调用时参数**而非持久事实。后果：(a) 同一 Project 可被重复注入不同 agent；(b) 无法回答"该 Project 曾注入过谁"。裁定点：V1 是否允许调用时指定 agent（最小）；还是加最小 `project_assignments`/`agent_lead` 记录（卡片 §11 允许提最小新模型，但必须说明现有模型为何不足）。

**M2 — 物料子目录命名方案不存在。** 候选见 §1.3：`workspace/projects/{project_id}/`（进 TempWorkspace 默认物化、受 500MB 预算约束）vs `{agent_id}/projects/{project_id}/`（agent 子树根、agent 用工具可见、不进默认物化预算）。两者隔离/可见性权衡不同，必须单卡裁定并写进两侧 body。

**M3 — 无"整树复制"存储原语。** 需指定组合规则（遍历 → 逐文件条件写 + 锁 + revision），且回答：S3/fallback 后端下 Materialization 是否 V1 支持（S3 无原子树发布；local 的 fcntl 锁在 S3 路径不存在）。建议 V1 声明支持后端集合 = 实现所及（UNKNOWN 2）。

**M4 — `manual` 源无物料可注入（UNKNOWN，卡片 §5 明示）。** `manual` locator = None/{}（`schemas/project_intake.py:48-54` 强制）；`_validate_manual` 纯登记（`project_intake_service.py:488-490`）。三选一裁定：① 仅 metadata（写 Project 描述类占位文件，来源 = Project 本体）；② V1 不物料化 manual（显式跳过，结果里报告 `skipped=manual`）；③ 写 Project 名/目标生成的说明文件（内容需来自 Project 记录，非发明新数据源）。最小实现原则倾向 ②（零内容发明）。

**M5 — 覆盖/重复语义未定（卡片 §8 幂等）。** 现成机制：`WriteCondition(require_absent=True)` 拒绝已存在；`version_token` 乐观；`flush_temp_workspace` 的"内容一致即收敛"（`agent_tools.py:1931+`）。需裁定：重复 Materialization 对**已存在的同名文件**是 fail-fast（require_absent + 内容不一致 → conflict）还是 overwrite？建议：默认 fail-fast + 显式 `overwrite` 参数（对齐 move 工具的 `overwrite` 语义，`workspace_collaboration.py:739`）。

**M6 — provenance 落点。** `WorkspaceFileRevision` 无 `project_id/repository_id` 列（`models/workspace.py:50-66`）。两个方案：① 最小：`group_key` 槽位承载稳定操作身份 `materialize:{project_id}:{repo_id}:{agent_id}` + `actor_type="system"`（零迁移，复用既有稳定身份模式，`workspace_collaboration.py:448-452` 注释先例）；② 加列/最小表（卡片 §11）。建议 ①，并在结果里明示 LIMITATION：per-file revision 只到文件粒度，无"一次 materialization 的原子清单"持久化（清单 = 调用结果 + AuditLog 行，`AuditLog` 为现成留痕对象）。

**M7 — 部分失败语义（卡片 §9）。** 逐文件条件写天然是 PARTIAL 的：需裁定发布协议——建议 **staging 键 + 原子发布**：先写 `{agent_id}/.materialize-tmp/{repo_id}/*`（临时键，`local.py:65` 的 `_TEMP_FILE_PREFIX` 是隐藏先例但语义不同，需自定义临时键前缀规则），全部成功后以 `require_absent`/条件写逐文件迁入目标并删临时键；失败即清临时键 + 返回 `PARTIAL/FAILED` + 已写清单。临时键命名与清理责任属本 GAP（新写协议 = 新约定，须写入两侧 body）。

**M8 — 人类编辑锁交互（UNKNOWN）。** `write_workspace_file` 对 system actor 检查人类锁（`workspace_collaboration.py:558-569`）。Materialization 是 system 动作：写前若命中人类锁，现状语义 = 返回 busy 消息（非异常）。裁定：Materialization 走同一"命中人类锁 → 该文件 skip/conflict 上报"路径，还是走 `workspace_locks`（Redis 运行时锁）+ 人类锁双查？建议双查（与既有 write 路径一致）。

**M9 — 非 tenant-context 退化风险。** `TenantScopedBaseDAO.get_scoped` 在 `_tenant_ctx=None` 时退回裸 PK 查询（`dao/base.py:244-246`）。API 路径由中间件保证非空；但 Materialization 若在后台/队列触发且忘包 `tenant_context`，跨租户读取**静默放行**。裁定：Materialization 服务入口必须显式 `verify_tenant_scope`（intake_security，fail-closed）作为第二道闸，与 `intake_security.py:521-534` docstring 的"丢失租户上下文的窄路径"防线对齐。

---

## 4. 风险清单（架构影响）

1. **最危险路径 = 目标侧 key 拼接。** 若实现者自行 `f"{agent_id}/{zip_internal_path}"` 而不复用 `normalize_storage_key` + `resolve_agent_visible_path`，zip 内部路径/`local_folder` 相对名都可能越出 agent 子树。既有防线齐全（§2），实现只需**全部走守卫**——reviewer 15 项检查第 4/5 条与此对应。
2. **`skills/` 完整性依赖。** 物料若落 `skills/` 下，下次 Run 的 `SkillSnapshotIncompleteError` 路径（`agent_tools.py:1763-1768`）会被物料大小触发——命名方案（M2）必须把项目物料**排除在 `skills/` 之外**（硬性，非建议）。
3. **S3 后端语义漂移。** 条件写依赖 ETag（`s3.py:311-312` 无 ETag 直接 RuntimeError）；无 fcntl 锁。M3 的"支持后端集合"裁定要写清：V1 若只保证 local，S3 下 `write_bytes_if_match` 行为须单独验收。
4. **人类锁 90s TTL 竞争。** 人类正在编辑目标文件时 materialize：命中 `WorkspaceEditLock` → skip/冲突（M8）。不得静默覆盖人类未提交的编辑（`before_content` 必须非空捕获，既有 revision 模式已覆盖）。
5. **Redis 锁 busy = 快速失败**（§1.5）：Run 沙箱持有锁期间 Materialization 会 conflict。这是正确行为（防写冲突），但 API 需把 conflict 映射为可重试信号（对齐 intake 的 `retryable` 语义，卡片 §7 第三选择"失败"）。
6. **`tasks.json` 等受保护路径**（`workspace_collaboration.py:752`）：物料目标键若与受保护名碰撞（`soul.md`/`HEARTBEAT.md`/`focus.md`/`memory/`/`skills/` 根），须拒绝——M2 命名方案天然隔离，但 `local_folder`/`zip` 顶层名仍可能撞上 `soul.md` 等：目标键 = 命名前缀 + 内部相对名，碰撞检查归实现。

---

## 5. 裁定建议（供 t_c672b2c2 采纳或驳回）

| GAP | 建议 | 理由 |
|---|---|---|
| M1 | V1 调用时指定 agent，不加表 | 最小闭环；指派事实暂由调用方持有；`project_assignments` 留待团队接入（与 §5 Future 字段表一致） |
| M2 | `{agent_id}/projects/{project_id}/{display_name}/`（agent 子树根级） | 避开 500MB Run 物化预算与 `skills/` 完整性；agent 工具面（`_storage_list_dir`）可见；reviewer 检查 3（Repository≠Workspace）有清晰边界 |
| M3 | V1 支持 = local + fallback；S3 声明"条件写路径可用但无跨进程锁，多写者风险由单写者约定承担" | 与现有部署形态一致（UNKNOWN 1 同逻辑：不发明部署假设） |
| M4 | ② 显式跳过并上报 `skipped` | 零内容发明；manual 的信息在 Project 本体（name/description/goal），不复制进物料 |
| M5 | 默认 fail-fast（require_absent）+ 显式 `overwrite` | 对齐 move 工具既有语义；幂等 = 内容一致跳过（hash 比较，`content_hash_bytes`） |
| M6 | ① `group_key = "materialize:{project_id}:{repo_id}:{agent_id}"` + `actor_type="system"` + AuditLog 行 | 零迁移；稳定操作身份先例存在（`workspace_collaboration.py:448-452`）；明示 LIMITATION：无原子清单持久化 |
| M7 | staging 键 `{agent_id}/.materialize-tmp/{repo_id}/*` → 原子迁入 → 清临时；失败清临时 + PARTIAL 上报 | 与 `_TEMP_FILE_PREFIX` 隐藏惯例同构；杜绝"成功但半残" |
| M8 | Redis `workspace_locks`（文件级）+ 人类锁双查，命中即该文件 conflict 上报 | 与既有 write/delete/move 路径完全一致，不发明第三种并发语义 |
| M9 | 服务入口显式 `verify_tenant_scope` + 后台路径强制 `tenant_context` 文档化 | fail-closed 第二道闸（intake_security 既有防线） |

---

## 6. 对下游卡片的接口移交（决策所有权）

- **t_c672b2c2（architect，规格）**：本文 §3 全部 GAP 的裁定权 + §5 建议的否决权；规格必须写出 M2/M7 的键命名规范全文（两侧 body 都要有，防止 builder 与 reviewer 各裁一次）。
- **t_025cda02（builder，实现）**：消费 t_c672b2c2 规格；本文 §2 API 清单 + §4 风险为必读；安全层**只调 `intake_security` 单一守卫**（卡片 routing rule），不得在 service 内重写穿越/zip 规则。
- **regression 边界**：本文未修改任何代码；下游实现的回归面 = `test_project_intake_service.py`（58）+ `test_workspace_reconciliation.py` + `test_files_api_storage.py` + ruff/pyright（卡片 §17 已列）。
