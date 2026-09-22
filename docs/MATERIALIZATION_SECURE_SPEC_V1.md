# SECURE MATERIALIZATION SPEC — Phase 2B-3 (V1)

状态：SPEC（设计规格，无实现代码）
规格卡片：t_c672b2c2（aco-architect）
输入依据：
- 父卡 t_6748fd76（Phase 2B-3 总卡 §0–§21）
- 只读审计 `docs/MATERIALIZATION_WORKSPACE_ISOLATION_AUDIT_V1.md`（t_df317a55，交付于 `wt/t_df317a55` @ cea71516；本卡基线 `wt/t_c672b2c2` @ 5830624 = main，含 Phase 2B-2 intake 合并，审计基线与本基线一致）
- 下游消费卡：t_025cda02（builder，实现）/ t_be706e41（reviewer，独立审查）

> 本文档是 **builder 与 reviewer 两侧共同引用的唯一规格**。M2（键命名）与 M7（staging/发布协议）的全文规则必须原样进入两侧卡片，任何一方不得在实现/审查中重新裁定。
> 证据约定：标注 `FACT(file:line)` 的均为已读代码事实；标注 `DECISION` 的为本卡裁定（含对审计 §5 建议的采纳/否决）。

---

## 0. 摘要：本规格裁定了什么

| GAP | 裁定（DECISION） | 对应审计建议 |
|---|---|---|
| M1 target agent 无持久记录 | V1 调用时参数，不加表；结果 + AuditLog 留痕 | 采纳 |
| M2 物料键命名 | 目标键 `{agent_id}/projects/{project_id}/{material_name}/`；staging 键 `{agent_id}/.materialize-tmp/{repo_id}/` | 采纳（§2 全文） |
| M3 支持后端集合 | V1 声明支持 local + fallback；S3 为 DOCUMENTED LIMITATION（§9） | 采纳 |
| M4 manual 源 | 显式跳过，结果报 `SKIPPED_NO_MATERIAL`，零内容发明 | 采纳 ② |
| M5 覆盖/幂等 | 默认 fail-fast；内容一致收敛；显式 `overwrite` 覆盖 | 采纳 |
| M6 provenance | `WorkspaceFileRevision.group_key = materialize:{project_id}:{repo_id}:{agent_id}` + 每调用一条 AuditLog；**直接构造 revision 行，不改 `record_revision` 签名**（§6） | 采纳 ① + 落点细化 |
| M7 部分失败 | staging 键写入 → 目录级锁内逐文件发布 → 任一冲突整调用失败 → staging 恒清理 | 采纳 |
| M8 人类锁 | 目录级 Redis `workspace_locks` + 发布前逐目标键 `get_active_lock` 双查，命中即整调用 conflict | 采纳 |
| M9 tenant 退化 | 服务入口显式 `verify_tenant_scope` + agent/project 租户相等硬检查；后台调用方必须包 `tenant_context` | 采纳 |

数据库变更：**No schema change required**（卡片 §18）。无新表、无 Alembic migration、无新依赖。

---

## 1. 阶段边界（硬规则，卡片 §1/§13/§14）

1. **只接受 `Project.status == "INITIALIZED"`**。RECEIVED / SOURCES_OK / 任何终态（含 REJECTED）一律拒绝，先于任何 I/O。状态值枚举 FACT：`models/project.py:48-65`。
2. 只对 `verified == True and pending_verifier == False` 的 Repository 行动（卡片 §3 最低要求；`INITIALIZED` 语义上要求全部源已验证 FACT：`project_intake_service.py:431-437`，任一源未过则项目停在 RECEIVED/SOURCES_OK）。逐仓二次检查作为数据完整性防御（defense-in-depth）；违和即 `SOURCE_NOT_READY`。
3. Materialization **不得**：改变 Project 状态（INITIALIZED 不自动变 EXECUTING）、创建 Task、创建 Agent Run、发送 prompt、执行代码、启动 Squad、修改源（host）文件。
4. 交付物唯一效果：文件进入目标 agent 的存储子树，后续 Run 通过 TempWorkspace 自动物化可见（FACT：`agent_tools.py:1689-1777`；运行时无需任何改动，卡片 §12 结论）。

## 2. 键命名规范（M2 + M7，builder/reviewer 双侧全文）

### 2.1 material_name（每仓一个）

- 取 `Repository.display_name`（非空）；否则取 `str(repository.id)[:8]`。
- 校验（服务入口期，fail-fast，先于任何 I/O）：长度 1–64；不含 `/`、`\`、NUL；首字符不是 `.`；不命中 §2.4 保留名集合。违例 → 该仓 `SOURCE_INVALID`（非 retryable），其他仓不受影响。

### 2.2 相对键 rel

- 源文件相对路径以 `/` 归一化（反斜杠转正斜杠），弹出 `.` 段与空段；任何 `..` 段或 NUL → 该仓 `SECURITY_REJECTED`。
- zip 成员的 rel = 其 member name 经 §2.3 守卫后的同一归一化；目录条目（name 以 `/` 结尾）跳过。

### 2.3 目标键（权威拼接公式，唯一合法形式）

```
target_key = f"{agent_id}/projects/{project_id}/{material_name}/{rel}"
staging_key = f"{agent_id}/.materialize-tmp/{repo_id}/{rel}"
```

实现规则（硬）：
- 禁止 f-string 之外再引入任何来源参与拼接；拼接完成后必须再过 `normalize_storage_key`（FACT：`storage_runtime/utils.py:19-24` 的 agent 前缀归一先例）做归一确认，并以 `assert` 级检查确认前缀严格等于 `f"{agent_id}/projects/"` 或 `f"{agent_id}/.materialize-tmp/"`（防御纵深，卡片 routing rule：安全判定只允许调用 §4 列出的守卫，不重写规则）。
- 宿主侧（local/fallback 后端）解析宿主路径必须走 `local_path_for(key)`（FACT：`facade.py:59-64`），不得手工 `WORKSPACE_ROOT / key` 拼接绕过前缀校验（`local.py:42-48` 的 403 穿越拒绝是既有底线）。

### 2.4 保留名（碰撞即拒绝，卡片 §7 风险 6）

`material_name` 与每个 `rel` 的**首段**不得命中：

```
.materialize-tmp  .git  .skill  skills  memory  tasks.json
soul.md  focus.md  HEARTBEAT.md  workspace  projects
```

命中 → 该仓 `SECURITY_REJECTED`（该仓整体失败，不写任何文件）。
理由：`tasks.json` 为受保护路径 FACT（`workspace_collaboration.py:752`）；`skills/` 完整性为硬性风险 FACT（`agent_tools.py:1763-1768`，缺件触发 `SkillSnapshotIncompleteError`）；`workspace/` 撞名会落入 Run 默认物化路径；`projects/` 自身保留防止自嵌套。

### 2.5 布局不变式（reviewer 检查项）

- 目标键永远落在**指定** agent 子树内（前缀 = 该 `agent_id`）——Tenant A 物料不写 Tenant B agent（§5.1）。
- 目标键永不落入 `skills/`、`memory/`、`tasks.json`、`workspace/` 子树。
- 目标键不在 TempWorkspace 默认物化路径（`{skills, memory, workspace, focus.md, soul.md, HEARTBEAT.md}` FACT：`agent_tools.py:1689` 起）内——因此不消耗 500MB Run 物化预算；agent 经工具面（`_storage_list_dir`/读工具）按需取用。
- staging 键点号前缀，与 `local.py:65` 的临时文件隐藏先例同构，但语义独立：本卡定义其生命周期（§3.3），不与宿主 temp 文件混用。

## 3. 源类型行为（卡片 §5，每种源的 V1 最小安全行为）

统一不变式（所有源）：
- **读源只读**：不修改、不删除、不移动宿主源文件/目录。
- **预算**：单文件 ≤ 50MB、单次调用全部文件总和 ≤ 500MB（与 `TOOL_MATERIALIZE_MAX_FILE_BYTES` / `TOOL_MATERIALIZE_MAX_TOTAL_BYTES` 对齐，FACT：`agent_tools.py:150-151`）。超限 → 该仓 `SOURCE_SIZE_LIMIT`（非 retryable）。
- **枚举**：`os.scandir` 递归；**跳过符号链接与非常规文件**（`is_file(follow_symlinks=False)`），host symlink = 逃逸向量（DECISION，最小安全面）。

### 3.1 manual

| 项 | 行为 |
|---|---|
| 物料 | **无**（DECISION M4②：零内容发明；manual 的信息在 Project 本体） |
| 结果行 | `outcome=SKIPPED_NO_MATERIAL, reason_code=SKIPPED_NO_MATERIAL` |
| 写文件 | 0 |

### 3.2 local_folder

- locator：`{"path": "<host absolute>"}`（FACT：`schemas/project_intake.py:56-62`）。
- 入口重放 `check_host_path(raw, source_type="local_folder")`（§4）→ 穿越/敏感根 → `SECURITY_REJECTED`；相对路径 → `SOURCE_INVALID`。
- 目录缺失/不可读/为空 → `SOURCE_NOT_FOUND` / `SOURCE_INVALID` / `SOURCE_INVALID`（与 intake 验证器语义一致，FACT：`project_intake_service.py:520-563`；materialization 时刻源可能已变动，必须重新检查，不得信任 `verified_at` 时刻的状态）。
- 读取：逐文件 `shutil`/`open(..., "rb")` 读入（宿主侧），经 §3 不变式后入 staging。

### 3.3 document

- locator：`{"path": "..."}` XOR `{"storage_key": "..."}`（FACT：`schemas/project_intake.py:64-88`）。
- host 分支：重放 `check_host_path(source_type="document")`；`os.path.isfile` 检查缺失/类型；扩展名检查复用 intake 的 `_DOCUMENT_EXTENSIONS` 白名单（FACT：`project_intake_service.py:594`，模块级常量）——**必须 import 同一常量，不得复制字面量**。
- storage_key 分支：`backend.exists / stat / read_bytes`；`stat.is_dir` → `SOURCE_INVALID`；存储层 `SecurityError` 上抛（映射 409，不得降级为 unreachable）；其他异常 → `SOURCE_UNREACHABLE`（retryable，与 intake 同语义 FACT：`project_intake_service.py:624-634`）。
- rel = 源文件名（去目录部分；文件名本身过 §2.2 归一化与 §2.4 首段保留名检查）。

### 3.4 zip

- locator 同 document（path XOR storage_key）。
- host 分支：`aiofiles` 读全量（同 intake FACT：`project_intake_service.py:666-669`）；storage 分支：`read_bytes`。
- **解包前**必调 `intake_security.check_zip_slip(data)`（§4 单守卫）：不安全成员 → `SECURITY_REJECTED`（整仓）；容器不可读 → `SOURCE_INVALID`。
- 解包：`zipfile.ZipFile(io.BytesIO(data))` **纯内存**；逐成员 `z.read(name)`；目录条目跳过；成员名再过 §2.2 归一化 + 前缀断言（二次纵深——`check_zip_slip` 的通过不是信任凭证，卡片 §5 "逐 entry 检查路径"）。
- 不执行包内任何程序/脚本（zipfile 语义天然满足；不得引入解压命令）。
- 文档限制（写入结果 `limitations`，reviewer 必查）：包内 symlink 条目（外部属性 0xA000）V1 按**普通文件**物化其目标路径字符串，不建立宿主符号链接。

### 3.5 未就绪源（github/gitlab/local_git）

- 卡片 §4：登记存在但无 acquisition → 明确失败 `SOURCE_NOT_READY`，不得伪装成功。
- 构造不变式：V1 下 git 源永远无法使项目达 INITIALIZED（永久 `SOURCE_NOT_SUPPORTED` FACT：`project_intake_service.py:725-731`），故 INITIALIZED 门禁下此类仓**不可达**；保留该结果码仅为防御未来状态机扩展（未知值行为显式定义，符合仓规 "extensible inputs define unknown behavior"）。

## 4. 安全层：单守卫契约（卡片 routing rule）

实现**只能**调用 `intake_security` 暴露的判定，不得在 service 内重写穿越/zip/敏感根规则（routing comment：安全层复用 intake_security 单一守卫）：

| 用途 | 调用 | FACT 位置 |
|---|---|---|
| host 路径形状（local_folder/document/zip host 分支） | `check_host_path(raw, source_type=...)` | `intake_security.py:232` |
| zip 成员名（解包前） | `check_zip_slip(data)` | `intake_security.py:305` |
| 读侧门禁（API 层） | `verify_read_access(user, project)` + `verify_tenant_scope(project.tenant_id, user.tenant_id)` | `intake_security.py:521/537`，`api/projects.py:56-75` 同构 |
| 目标 agent 授权 + 租户 | `check_agent_access(db, current_user, agent_id)`（含跨租户 403 FACT：`permissions.py:557-558`） | `core/permissions.py:519` |
| 目标键归一 | `normalize_storage_key` | `storage_runtime/utils.py` |

**双次 zip 检查说明**（reviewer 不得以"重复检查"驳回）：intake 时刻的通过不是 materialization 时刻的凭证——host 文件可被替换、storage key 可指向新对象。Intake 管"源曾经安全"，Materialization 管"此刻写入的字节安全"。

## 5. 隔离不变式

### 5.1 Tenant

- 门禁链（全过才开工）：`verify_read_access` → `verify_tenant_scope(project)` → `check_agent_access` → **`agent.tenant_id == project.tenant_id`（DECISION M9 第二道闸，fail-closed 显式断言）**。最后一道堵死"跨租户 agent 可访问"的组合缺口：Tenant A 的 Project 永远只能落 A 的 agent 子树。
- 后台/队列调用方（V1 无队列，规则先写死）：必须 `tenant_context(tenant_id)` 包裹再调服务，并在服务入口重验 `verify_tenant_scope`（防 `get_scoped` 无租户上下文的裸 PK 退化 FACT：`dao/base.py:244-246`）。

### 5.2 Agent

- 目标/staging 键前缀**恒等于被授权 agent 的 `agent_id`**（§2.3 公式 + 前缀断言）。不存在任何参数/字段能改变前缀。
- `resolve_agent_visible_path`（FACT：`workspace_paths.py:51-87`）继续是 runtime 侧 agent 可见性唯一解析器；本规格不新造目标侧解析器，只在拼接完成后做前缀断言（§2.3 实现规则），两层不互相替代。

### 5.3 路径

- §2.2/§2.3/§2.4 全部规则 + 宿主侧 `local_path_for` 既有前缀 403 拒绝（FACT：`local.py:42-48`）。
- `local_folder` 源目录内的 symlink 不跟随（§3 不变式）。

## 6. 并发与锁（M8；卡片 §7）

**语义裁定（硬性，reviewer 检查项 12）**：使用 Workspace Lock 防**运行中 agent 写冲突**；**不得**在结果、文档、代码注释中宣称 Project Single Writer（仍无实现）。

### 6.1 锁域 = 目录级（DECISION）

每个被处理的仓（写文件数 > 0 的）取 **恰好一个** Redis 锁：

```
path = "projects/{project_id}/{material_name}"
workspace_locks(agent_id, [path...], tenant_id=agent.tenant_id)
```

- 快速失败：任一 busy → `RuntimeError("Workspace lock busy: ...")`（FACT：`workspace_locking.py:86`）→ 整调用 409 `LOCK_CONFLICT`（retryable），staging 已写部分恒清理（§7）。
- **不逐文件加锁**（DECISION，否决 O(N) 逐文件锁）：N 文件 × 60s TTL 存在中途过期窗口；目录级锁把锁集限定在 ≤ 被处理仓数（有界），一次获取覆盖 staging+publish 全程。TTL 60s（`DEFAULT_LOCK_TTL_SECONDS`）对单次调用（预算上限 500MB）仍可能不足 → **DOCUMENTED LIMITATION**（§9）：极端大调用下锁自然过期，由"发布前逐键 `require_absent`/条件写"兜底，不引入续期机制（最小化）。
- 持锁方不得修改源：锁只保护目标 agent 存储子树。

### 6.2 人类编辑锁（DB）

发布阶段，**每个**目标键写前调 `get_active_lock(db, agent_id=..., path=target_rel)`（FACT：`workspace_collaboration.py:199-213`）；命中（人类 TTL 90s 内编辑该文件）→ 整调用 409 `HUMAN_LOCK_CONFLICT`（retryable），清理后返回。**不静默覆盖人类未提交编辑**（卡片 §4 风险 4）。

### 6.3 发布条件写（与锁正交的第二层）

- 新目标键：`write_bytes_if_match(key, data, condition=WriteCondition(require_absent=True))`（FACT：`base.py:39-41,102-117`）。
- 既有键且 `overwrite=True`：无条件写（锁内，无版本条件——与 move 工具 `overwrite` 语义对齐 FACT：`workspace_collaboration.py:739`）。
- 锁 + 条件写双保险：即使锁过期（§6.1 LIMITATION），并发写仍被 `require_absent` 拦截为 conflict。

## 7. 部分失败协议（M7；卡片 §9）

### 7.1 三阶段

```
[1] 读源 + 校验 + 枚举      → 失败：0 写入（staging 尚未启用）
[2] staging 写入            → {agent_id}/.materialize-tmp/{repo_id}/{rel}，全部用写接口无条件写（staging 键独占该仓）
                               任一失败（磁盘/权限/预算）：该仓 FAILED，delete_tree(staging_key)，调用结果 FAILED/SOURCE_FAILED
[3] 发布（锁内，§6）        → 逐目标键决策（§8 表）；
                               任一 CONFLICT：该仓 FAILED → 清理该仓 staging → 调用结果按 §7.3 汇总
```

### 7.2 清理不变式

- staging 清理挂在**每个退出路径**（成功/失败/异常/cancel）：实现上 `try/finally` 对已启用的 staging 键执行 `delete_tree`（FACT：`base.py:80-81`，local=rmtree、S3=批量删，语义一致）。
- 残留判定：点号前缀 + repo_id 作用域，残留键不碰目标键空间，不构成可见半残品；"成功但半残"被发布协议排除（§7.3：冲突即整调用非 SUCCESS）。
- 清理本身失败：记录进结果 `warnings`，不吞掉主结果（卡片 §9 "不得为简单而默默忽略异常"）。

### 7.3 结果语义（独立结果原则）

- 仓间独立：仓 X 失败不阻断仓 Y 的读源/发布；但**仓内**冲突 = 该仓 0 目标键新写入（发布先于冲突键完成的部分是收敛/已写混合，必须如实报告 `written`/`converged` 计数——不伪装全成或全败）。
- 调用结果：
  - `outcome=SUCCESS`：全部仓 ∈ {SUCCESS, CONVERGED, SKIPPED_NO_MATERIAL}
  - `outcome=PARTIAL`：至少一仓成功类 + 至少一仓 FAILED 类
  - `outcome=FAILED`：无任何成功类仓
- PARTIAL/FAILED 的 HTTP 映射：409（§10），带完整 `repositories[]` 明细；**绝不** 2xx。

## 8. 幂等规则（M5；卡片 §8）

调用前（读目标键现状，无锁下读是探测，写决策在锁内重读）对每个目标键：

| 目标键现状 | overwrite=false（默认） | overwrite=true |
|---|---|---|
| 不存在 | 写入 → `written`，action=`written` | 写入 → `written` |
| 存在，`content_hash_bytes(目标)` == 源内容 hash（FACT：`base.py:144`） | **收敛**：不写 → action=`converged` | 收敛：不写（内容相同，写无意义） |
| 存在，hash 不等 | **conflict**：该仓 FAILED，`reason_code=CONTENT_CONFLICT`，0 新写入（该仓） | 覆盖 → `written`（revision 捕获 before） |

由此：同 Project + 同 Source + 同 Agent 连续两次调用 → 第二次全 `converged`，**无重复文件、无内容漂移**（卡片 §8 全部禁令满足）；源内容变更 + overwrite=false → 显式 conflict（不悄悄换内容）；overwrite=true → 明确覆盖且 revision 记录 before/after。

锁内重读（发布时）与调用前探测不一致（并发写入）→ `require_absent` 拦截为 conflict，同上表处理。

## 9. 结果与 provenance（M6；卡片 §10）

### 9.1 Revision 行（每成功写入/覆盖文件一行）

**直接构造 `WorkspaceFileRevision` 行**（DECISION，否决"扩 `record_revision` 签名"——最小改动面，构造点单处且可审计）：

```
WorkspaceFileRevision(
  agent_id=agent_id, scope_type="agent", scope_id=agent_id,
  path=target_rel,                      # normalize_workspace_path 归一
  operation="write",
  actor_type="system",                  # materialization 是 system 动作（卡片 M8）
  actor_id=current_user.id,             # 可追溯到触发调用的人
  before_content=<读到的既有文本，best-effort；新文件/二进制为 None>,
  after_content=None,                   # 大文件不灌 revision 列；哈希列承载内容身份
  content_hash=content_hash_bytes(data),
  group_key=f"materialize:{project_id}:{repo_id}:{agent_id}",
)
```

- `group_key` 稳定操作身份先例：`user-autosave:`（FACT：`workspace_collaboration.py:257-262`）与 `group runtime`（FACT：`workspace_collaboration.py:386-482`）。同身份重放 → 可按 group_key 聚合查询。
- 二进制大文件：`content_hash` 为权威内容身份（`base.py:144`），`after_content` 不持久化全文（LIMITATION，§9.3）。
- revision 写入与发布同事务（db session 一致）；revision 写失败 = 该仓 FAILED（不得在无法证明 provenance 时宣称成功）。

### 9.2 AuditLog（每调用一行，best-effort 同 intake 先例 FACT：`project_intake_service.py:743-760`）

```
AuditLog(tenant_id=..., user_id=current_user.id,
         action="project_materialization",
         details={project_id, agent_id, repo_results: [{repo_id, source_type, outcome,
                                                        reason_code, written, converged, skipped}],
                  outcome, limitations: [...]})
```

### 9.3 明示 LIMITATION（卡片 §10：不得假装统一 Evidence）

1. 无"一次 materialization 的原子文件清单"持久化——清单 = 调用响应 + AuditLog 行 + 按 group_key 聚合的 revision 行。
2. `before_content` 仅 best-effort 捕获文本（`_record_scoped_revision` 的 NUL 清理先例同样适用：二进制不落 revision 列）。
3. 支持后端 = local + fallback（M3）；**S3 为 DOCUMENTED LIMITATION**：条件写可用（S3 原生 If-None-Match/If-Match，FACT：`s3.py:278-367`，无 ETag 时 RuntimeError），但无跨进程 mutation 锁（fcntl 缺失），多写者风险由单写者约定承担；且 6.1 的 60s TTL 在大调用下的过期窗口在 S3 无锁兜底，靠条件写收敛。
4. zip symlink 条目按普通文件物化（§3.4）。

## 10. API 契约（卡片 §12；最小增量，风格同 `api/projects.py`）

### 10.1 端点

```
POST /api/projects/{project_id}/materialize/{agent_id}
```

- 请求体（Pydantic，`schemas/project_intake.py` 同族新模型或同文件追加）：

```python
class MaterializeRequest(BaseModel):
    overwrite: bool = False
```

- 响应 201：

```python
class MaterializationRepoResult(BaseModel):
    repo_id: UUID
    source_type: str
    outcome: str        # 闭集: "SUCCESS" | "CONVERGED" | "SKIPPED_NO_MATERIAL"
                        #       | "FAILED"
    reason_code: str | None   # 闭集: None | CONTENT_CONFLICT | LOCK_CONFLICT
                              #   | HUMAN_LOCK_CONFLICT | SOURCE_NOT_READY
                              #   | SOURCE_FAILED | SOURCE_SIZE_LIMIT | SOURCE_INVALID
                              #   | SECURITY_REJECTED | SKIPPED_NO_MATERIAL
    written: int
    converged: int
    skipped: int

class MaterializationOut(BaseModel):
    project_id: UUID
    agent_id: UUID
    outcome: str            # 闭集: "SUCCESS" | "PARTIAL" | "FAILED"
    retryable: bool
    repositories: list[MaterializationRepoResult]
    limitations: list[str] = []   # §9.3 的适用项
```

### 10.2 状态码映射（handler 只做映射，业务在服务层，AGENTS.md transport 规则）

| 情形 | 码 |
|---|---|
| 成功 / PARTIAL | 201 |
| 项目/agent 不存在（租户域内） | 404 |
| 读门禁 403（creator/admin/租户） | 403 |
| 请求体非法 | 422 |
| 调用 FAILED/PARTIAL 中的 409 类（见下） | 409，detail 同 intake 风格 `{code, message, retryable}` |

409 触发：调用前门禁失败——`project.status != "INITIALIZED"`（`reason_code=SOURCE_NOT_READY`，retryable=False）、仓 `pending_verifier`（同上）、**任一**仓 `CONTENT_CONFLICT`（retryable=False；提示 overwrite 或重试）、`LOCK_CONFLICT` / `HUMAN_LOCK_CONFLICT`（retryable=True，卡片 §7 三选一的"conflict(快速失败)"落点）、`SECURITY_REJECTED`（retryable=False）。
注：仓间混合时（部分仓成功、另一仓冲突）= PARTIAL → 409 但响应带全部明细（§7.3）。

### 10.3 同步执行

V1 同步（请求内跑完，有界预算 500MB）。不引入队列/scheduler（卡片 §7 末句"不要新造 scheduler"）。后台化时点已用 §5.1 tenant_context 规则预留。

## 11. 测试要求移交（卡片 §15/§16 → t_025cda02 必做清单）

1. 状态门禁：RECEIVED / SOURCES_OK / REJECTED / ANALYZING 各自 409。
2. Zip Slip：真实构造恶意 zip（`../../escape.txt`、绝对路径 `C:\evil`、前导 `/etc/x`）→ `SECURITY_REJECTED`，0 写入。
3. 路径穿越：`local_folder` locator 含 `..` 段 → 拒绝；敏感根（`/etc`、`c:\windows`）→ 拒绝。
4. workspace 边界：构造 `rel` 首段命中保留名（`skills/`、`tasks.json`、`workspace/`、`.materialize-tmp`）→ 拒绝；目标键前缀断言单测。
5. 租户隔离：Tenant A project + Tenant B agent → 403（§5.1 第四道闸断言命中）。
6. agent 隔离：材料键前缀恒等于被授权 agent_id（断言键字符串，非仅结果存在）。
7. 权限失败：源目录不可读（chmod 000 等价）→ 正确失败类。
8. 既有同名文件：内容不同 + overwrite=false → `CONTENT_CONFLICT` 且 0 新写入；overwrite=true → 覆盖且 revision 含 before。
9. 重复执行：第二次全 `converged`，文件数不变、内容 hash 不变。
10. 部分失败：制造中途失败（如发布第二个仓时锁 busy / 目标键 hash 冲突）→ 结果 PARTIAL/FAILED + staging 键 `delete_tree` 被调用且残留为空（断言 `exists(staging_key)==False`）。
11. 人类锁：`WorkspaceEditLock` 行存在时发布 → `HUMAN_LOCK_CONFLICT`，不覆盖。
12. 未就绪源：构造 `pending_verifier=True` 的仓（在合法状态构造后）→ `SOURCE_NOT_READY`；git 源恒不可达的构造断言（§3.5）。
13. E2E（真实 Postgres + 真实文件系统，卡片 §16）：`INITIALIZED` → 三仓（local_folder/document/zip）→ 目标键实际存在且内容 hash 匹配 → revision/AuditLog 行存在。
14. 回归（卡片 §17）：`test_project_intake_service.py`（58）+ `test_workspace_reconciliation.py` + `test_files_api_storage.py` + ruff + pyright 全绿；不改旧测试迁就。

## 12. 实现接口面（builder 参考，非强制命名）

- 服务：`app/services/project_materialization_service.py`（建议单文件；方法 `materialize(db, project_id, agent_id, overwrite, current_user)`，内部按 §7 三阶段组织）。
- API：`api/projects.py` 追加端点（同 router，复用 `_load_authorized_project`）。
- 只调既有 API：§2/§4/§6/§7/§8/§9 所引函数，无新存储原语、无新依赖、无 migration。
- 安全层**零新规则**：除 §4 表格外的任何穿越/zip/敏感根逻辑 = 违规（reviewer 检查项）。

## 13. 被否决的备选（Rejected Alternatives）

1. **`workspace/projects/{project_id}/` 注入（M2 候选 A，否决）**：进 TempWorkspace 默认物化 → 受 500MB Run 预算截断 + 撞 `workspace/` 惯例命名空间；`projects/` 根级键（本规格）两者皆避。
2. **`project_assignments` 新表（M1 候选 B，否决）**：V1 无"指派"业务消费者；最小原则。团队接入后按需提单。
3. **逐文件 Redis 锁（否决）**：O(N) 获取 + TTL 中途过期窗口，无收益（目录级已覆盖目标键空间）。
4. **新 Materialization 结果表 / Artifact-Evidence 系统（卡片 §10/§11 禁止项）**：group_key + AuditLog + revision 三件套已覆盖 V1 provenance，LIMITATION 明示。
5. **S3 专属原子树发布（否决，V1 不支持）**：发明部署假设；S3 走 §9.3 声明式 LIMITATION。
6. **manual 源写 Project 说明占位文件（M4 候选 ①/③，否决）**：内容发明，违反最小实现原则。

## 14. 验收自测（本卡交付物）

- 所有 FACT 文件:行已在本 worktree（@ 5830624 = main，含 2B-2 合并）逐一核对：`models/project.py`、`schemas/project_intake.py`、`intake_security.py:189-336,521-556`、`storage_runtime/base.py:33-145`、`workspace_locking.py:42-91`、`workspace_paths.py:51-87`、`workspace_collaboration.py:199-482`、`core/permissions.py:519-558`、`api/projects.py:56-75`、`project_intake_service.py:431-731`。
- 审计 GAP M1–M9 全部有 DECISION；审计 §5 建议 9/9 采纳（其中 M6 落点细化为直接构造 revision 行、M8 锁域细化为目录级 + 文档化 TTL 局限）。
- 无代码、无 migration、无依赖变更。
