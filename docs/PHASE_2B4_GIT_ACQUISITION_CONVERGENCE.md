# Phase 2B-4 · Git 来源获取 — 整合收口报告

- 编制: 2026-09-24,基于 aco 看板 4 卡真实产出(非口头汇报)
- 看板: ai-company-os · 根卡 `t_18e2c495`(all done)
- 位置: 实现与文档在 worktree 分支 `wt/t_4874c3e7`(`6912f53a` + `22e0dc45`),**尚未 merge 进 main**

## 1. 卡链与结论

| 卡 | 角色 | 产出 | 结论 |
|---|---|---|---|
| t_82ac3524 | architect | `docs/GIT_ACQ_DESIGN_V1.md` @ 4008e194(设计,docs-only) | done |
| t_4874c3e7 | builder | `6912f53a` feat: git source acquisition(~1100 行新服务 + 2 API + E2E 套件) | done |
| t_31f91b3a | reviewer | `docs/GIT_ACQ_SECURITY_AUDIT_T31F91B3A.md` @ 893943f1 | **REQUEST_CHANGES**(F1 High / F2 Medium / F3 Low 记录项) |
| t_2a1a8481 | builder | `22e0dc45` F1+F2 定点返修 + 回归测试(7 道门禁全绿,不动 main) | done |
| t_9e4cd47f | reviewer | 独立复验:`TOTALLY_BOGUS`/`""`/`42` 直探 + 非 main 默认分支实仓驱动 | **APPROVE** |

最终判定: **APPROVE(2 项阻塞缺陷已修复并复验)**。

## 2. 交付内容

- **新增** `backend/app/services/git_acquisition_service.py`:有界 git 获取流水线(纯校验 → 凭证解析 → 有界 clone → 获取区后置检查 → 单 tar 发布 → 记录)。
- **新增 API**(挂在既有 `_load_authorized_project` 门后):
  `POST/GET /projects/{pid}/repositories/{rid}/acquire/{agent_id}`
- **共享守卫上收** `intake_security.py`:`is_unsafe_host` / `git_url_detail`(https-only + SSRF)/ `normalize_rel`(全仓唯一一套成员路径规则;materialization 的旧副本已删除,委托共享版)。
- **无 schema 变更**:元数据走 `repositories.locator` JSON,凭证走既有 `agent_credentials`;alembic 单一 head `f067` 保持。
- **回归**:2B-1/2/3 全绿(133 passed);未获取的 git 源维持 fail-closed(`SOURCE_NOT_READY` / `SOURCE_NOT_SUPPORTED`)。

## 3. 安全审计要点(reviewer 逐条执行复验)

- **PASS(14 项不变量)**:SSRF 21 向量(云元数据/RFC1918/回环/映射 v6 全拒);https-only;arg-list 子进程无 shell;ref 注入门;local 路径边界;成员路径穿越双层守卫;symlink 跳过;submodules fail-closed;token 仅存于 git 子进程 env、绝不落 locator/日志/审计;租户隔离(跨租户 404 而非 403);重试仅限 2 个瞬时码;300s 超时+两段式回收;50/500 MiB 尺寸界;获取不派生任何 Agent/Run;无迁移文件。
- **F1 [High]**:`GET /acquire` status 路径对用户可注册的越集 `acq_result`("TOTALLY_BOGUS"/""/非字符串)抛 `ValueError` → 客户端可达 500。修复:出集值降级为安全读(pending, `code=None`, retryable),闭环守卫保留给服务自产代码。回归测试 4 参数化。
- **F2 [Medium]**:`_default_branch` 双重死码:①`ls-remote --symref HEAD url` 参数顺序颠倒(真实 git 二进制复现 exit 128,被 `allow_failure` 吞掉);②tab 分隔行解析错误(`'acqmain\tHEAD'` 过不了 ref 正则)。修复:重排 argv + 按 tab 首字段解析并回过 `_REF_RE`。回归测试用非 main 默认分支实仓驱动。
- **F3 [Low, 记录不改码]**:CGNAT `100.64.0.0/10` IP 字面量可过 host 门(Python `ipaddress` 不归类);云元数据 169.254.169.254 已拒。判定在卡片威胁模型之外,留待未来威胁模型卡。

## 4. 验收证据(battery 摘要)

| # | 命令(backend/) | 结果 |
|---|---|---|
| 1 | pytest test_git_acquisition_service.py | 返修后 132 passed, 1 skipped(GitHub egress) |
| 2 | pytest test_intake_security.py | 43 passed |
| 3 | 2B-2/2B-3 回归三套件 | 133 passed |
| 4 | 真库 E2E(scratch Postgres `clawith_t4874c3e7_e2e`,alembic 001→f067) | 39 passed |
| 5 | pyright 7 个 touched app 文件 | 0 errors |
| 6 | ruff | 仅既有 B008 baseline |
| 7 | alembic heads | 单 head f067 |

E2E 核心回合:local_git 实仓 acquire→`ACQ_OK`/artifact tar(仅工作树、无 .git)→repo verified → materialize 读同一 tar(不重 clone),且下游 0 新增 session/task/schedule、恰好 1 条 `git_acquisition` 审计行。

## 5. 已知边界(记录,不属本卡)

- 本机无 github.com 出网:GitHub E2E 由 skip-guard + 本报告记录为证据边界,有出网的主机重跑 `-k github`。
- 容器化部署的 git+出口网络属 ops 契约;git 子进程 rlimit(Linux);私有库 token 轮换 — 均留后续。

## 6. 收口待办

1. `git merge wt/t_4874c3e7`(含 6912f53a+22e0dc45)+ `wt/t_31f91b3a`(审计报告文档)进 main;三份文档归档 main `docs/`。
2. 清 ~36 个 done 任务 worktree/分支(pitfall 40/48)。
3. 推 GitHub(main 现与 origin/main 0/0 同步,推后即收口)。

### 源文档(绝对路径)

- 设计: `I:\project\AI Company OS\.worktrees\t_82ac3524\docs\GIT_ACQ_DESIGN_V1.md`
- E2E 验收: `I:\project\AI Company OS\.worktrees\t_4874c3e7\docs\GIT_ACQ_E2E_ACCEPTANCE_T4874C3E7.md`
- 安全审计: `I:\project\AI Company OS\.worktrees\t_31f91b3a\docs\GIT_ACQ_SECURITY_AUDIT_T31F91B3A.md`
