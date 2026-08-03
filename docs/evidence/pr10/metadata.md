# PR #10 审查证据 · 元数据

生成日期：2026-08-03
生成者：Orchestrator
用途：让 DeepSeek Advisor 在审核 PR #10 时，**在同一分支内**即可取得审查所需的辅助材料

---

## ⚠️ 本目录的定位

**Evidence 只是审查辅助材料，不替代源码。**

| 本目录是什么 | 本目录不是什么 |
|---|---|
| 变更范围、验证结果、核验入口的汇总 | 源码副本 |
| 帮助定位「该看哪里」的材料 | 代码内容或摘要 |
| 可核对完整性的清单 | 结论、评价或判断 |

**本目录不复制源码。** Advisor **仍须读取 PR 分支上的源码**进行判断。

审查所需源码就在同一分支的原始位置，例如：

```bash
# Advisor 在 PR #10 分支的 worktree 内可直接读取
src/product_pdf_qr/upload_limit.py
src/product_pdf_qr/domains/storage/service.py
src/product_pdf_qr/domains/version/service.py
src/product_pdf_qr/domains/public/router.py
tests/unit/test_upload_limit.py
tests/integration/test_business_loop.py
```

**权威来源始终是分支上的源码。** 本目录与源码不一致时，**以源码为准**，并请在审查意见中指出本目录的错误。

---

## 1. PR 信息

| 项 | 值 |
|---|---|
| PR 编号 | **#10** |
| 标题 | feat: 实现 Phase 1-B PDF 二维码最小业务闭环 |
| branch | `feat/phase1b-business-loop` |
| commit（证据生成时刻） | **`587fd99`** |
| 目标分支 | `main` |
| merge base | `bda51a4` |
| 状态 | OPEN，未合并 |
| 关联 Issue | #8（范围与验收）、#9（Governance，T-C 用例范围裁决） |
| 规模 | **39 个文件，+4356 / -26** |

> **注意**：`main` 在本 PR 开出后前进过（新增 `docs/review-context-pr10.md`）。本目录的 `diff.patch` 与 `changed-files.md` 均使用**三点式**比较（`origin/main...587fd99`）并排除 `docs/evidence/pr10/`，与该代码快照除证据目录外的 GitHub PR「Files changed」内容一致，不会把 main 的新增内容误报为本 PR 的删除。证据目录为避免自引用，不包含在自身的 `diff.patch` 中。

### 提交清单

| SHA | 说明 |
|---|---|
| `587fd99` | fix: align pre-parser audit field names |
| `ebddbfe` | fix: preserve honest pre-parser rejection audits |
| `e43eeb6` | fix: audit pre-parser upload rejections |
| `e4755d0` | fix: harden PDF upload resource boundaries |
| `f409b3f` | test: use upload response version identifier |
| `70adea8` | test: cover upload and restore pointer race |
| `476f69c` | test: strengthen phase 1b failure and concurrency coverage |
| `a211b10` | feat: implement phase 1b PDF QR business loop |

```bash
git log origin/main..587fd99 -- . ':(exclude)docs/evidence/pr10/**'
git show <sha>
```

---

## 2. 修改文件列表

**完整清单见同目录 `changed-files.md`**（含 39 个文件的变更类型、未修改文件的核验命令、按目录分组）。

概览：

| 分类 | 数量 |
|---|---|
| 应用入口与横切 | 4（含新增 `upload_limit.py`） |
| 业务域 | 15 |
| 测试 | 15（含新增 `test_upload_limit.py`） |
| 配置与文档 | 5 |
| **合计** | **39** |

**本 PR 未修改**：`migrations/`、`docs/requirements-v1.md`、`docs/requirements-v2.md`、`CLAUDE.md`、`docs/quality-gates-v1.md`、`docs/advisor-protocol-v1.md`、`docs/decision-register-v1.md`、`docs/test-plan-v1.md`

`changed-files.md` 提供了可直接执行的核验命令——**这些命令应全部无输出**，若有输出即存在越界改动。

---

## 3. 目录结构

```
docs/evidence/pr10/
├── metadata.md         本文件：PR 信息、定位与使用方式
├── changed-files.md    完整变更文件清单 + 未修改文件核验命令
├── diff.patch          完整 diff（4754 行，三点式）
└── validation.md       Reviewer 结果、CI 结果、测试结果、独立核验判据
```

**配套在线索引**：`docs/review-context-pr10.md`（已在 `main` 上），提供 CI 日志、artifact 下载、Reviewer 线程 URL 等本目录无法内联的在线证据位置。

---

## 4. 建议的审查路径

1. **先读 `changed-files.md`** —— 建立变更范围的整体认识，并执行其中的核验命令确认无越界改动
2. **再读 `validation.md`** —— 了解 Reviewer 两批意见、修复对应关系、CI 结果与已知未闭合项
3. **然后直接读 PR 分支源码** —— 依据 `validation.md` 第 4 节的 21 条判据逐项核验
4. **需要看改动上下文时查 `diff.patch`** —— 它是本 PR 全部改动的完整记录

**第 3 步不可省略。** 本目录不含源码；仅凭本目录形成的判断不满足 `docs/advisor-protocol-v1.md` 第 9 节的要求。

---

## 5. 生成方式（可复现）

```bash
git fetch origin
git diff origin/main...587fd99 -- . ':(exclude)docs/evidence/pr10/**' > diff.patch
git diff --name-status origin/main...587fd99 -- . ':(exclude)docs/evidence/pr10/**'
```

本目录所有内容均由上述命令的输出与 GitHub API 查询结果整理而成，**未经人工编辑或摘要**（`validation.md` 中明确标注为「Orchestrator 观察」的部分除外）。
