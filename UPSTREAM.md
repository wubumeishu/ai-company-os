# Clawith 上游溯源（UPSTREAM）

本目录 `clawith/` 是官方 Clawith 源码基线，**不带内嵌 .git**，以子目录形式并入本仓。

| 项 | 值 |
|---|---|
| 上游仓库 | https://github.com/dataelement/Clawith.git（本地 remote `clawith-upstream`，fetch-only，push 已禁用） |
| 基线 commit | `45fc701c366c69f89dff26d91d6a4a9cbc38e6f8`（main，v1.11.4-fix.1+4，PR #1003 "v1.11.5-quality-harness"） |
| 迁入日期 | 2026-09-20 |
| 迁入依据 | 主人拍板：Clawith 基线与 ACO 全部转移至 `I:\project\AI Company OS`（看板 t_3a7dbc04 Phase 0 改判） |
| 原基线路径 | `I:\zero\新建文件夹\jxiang-company-os`（原地保留，未删除） |

## 同步上游的做法

```bash
git -C .. fetch clawith-upstream main          # 本仓根目录执行
git diff --stat 45fc701c366c..clawith-upstream/main -- .  # 需先 checkout 对比
```

改造纪律（Phase 0 第十六节延续）：任何对 `clawith/` 的修改必须是 ACO 设计决定的有意改动，
并在 commit message 中注明 `[clawith]` 前缀，保持与上游 diff 可追溯。
