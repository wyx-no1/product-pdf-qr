# PR #6 审核证据索引

生成日期：2026-08-01
生成者：Orchestrator
对象：PR #6（Phase 1-A 工程初始化与基础环境）

---

## ⚠️ 本文件的性质与使用方式

**本文件只是证据索引，不是证据本身。**

| 本文件是什么 | 本文件不是什么 |
|---|---|
| 原始证据的**位置清单** | 原始文件的替代品 |
| 告诉审核者「去哪里看」 | 告诉审核者「看到了什么」 |
| 可核对完整性的目录 | 内容摘要、转述或结论 |

### 对 DeepSeek Advisor 的强制要求

> **审核必须基于原始证据，不得基于总结。**

具体而言：

1. **禁止**仅依据本文件的描述形成判断。本文件不含任何文件内容。
2. **必须**通过第 2 节给出的位置读取原始文件后再做判断。
3. 若某项原始证据**无法访问**，必须在意见中明确指出「该项未核验」，**不得**以本索引的描述代替核验，也不得因此跳过该项。
4. 若发现本索引与原始证据不一致，**以原始证据为准**，并在意见中指出索引错误。
5. 七项固定输出中的每一项，都应能指明其依据的是哪一份原始证据。

### 为什么这样设计

前一版证据包（已废弃）包含大量文件内容摘录，存在结构性缺陷：**Advisor 只能看到 Orchestrator 决定摘录的内容，而 Advisor 的全部价值在于独立**。摘录本身即构成信息过滤层，且摘录者正是被审查计划的制定方。

改为纯索引后，Orchestrator 只负责**告知位置**，不参与**决定内容**。

---

## 1. PR #6 基本信息

| 项 | 值 |
|---|---|
| PR | https://github.com/wyx-no1/product-pdf-qr/pull/6 |
| 标题 | feat: Phase 1-A 工程初始化与基础环境 |
| 分支 | `feat/phase1a-bootstrap` → `main` |
| HEAD commit | `7514f04` |
| 基线 | `main` = `9456e6e` |
| 状态 | OPEN，未合并 |
| 关联 Issue | #5（https://github.com/wyx-no1/product-pdf-qr/issues/5），父任务 #3 |
| 规模 | 43 个文件，+2944 / -19 |

### 提交清单

| SHA | 说明 |
|---|---|
| `7514f04` | fix: remove clean-start host tool dependencies |
| `4bc915e` | fix: verify postgres initialization before migrations |
| `d16ca8f` | fix: wait for final postgres server |
| `a0b9840` | fix: remove compose healthcheck warning |
| `410faee` | feat: bootstrap phase 1a foundation |

---

## 2. PR #6 原始文件位置

### 2.1 访问方式

**方式一：GitHub 网页**
```
https://github.com/wyx-no1/product-pdf-qr/blob/feat/phase1a-bootstrap/<路径>
```

**方式二：本地 git（无需切换分支）**
```bash
git show origin/feat/phase1a-bootstrap:<路径>
```

**方式三：完整 diff**
```bash
git diff origin/main..origin/feat/phase1a-bootstrap
# 或 https://github.com/wyx-no1/product-pdf-qr/pull/6/files
```

### 2.2 文件清单（全部 43 个）

**容器与编排**
```
Dockerfile
compose.yaml
.dockerignore
docker/postgres/init/01-roles-and-databases.sh
docker/postgres/healthcheck.sh
```

**CI**
```
.github/workflows/ci.yml
```

**数据库迁移**
```
alembic.ini
migrations/env.py
migrations/script.py.mako
migrations/versions/20260801_0001_initial_schema.py
```

**源码**
```
src/product_pdf_qr/__init__.py
src/product_pdf_qr/__main__.py
src/product_pdf_qr/config.py
src/product_pdf_qr/database.py
src/product_pdf_qr/errors.py
src/product_pdf_qr/main.py
src/product_pdf_qr/templates/base.html
src/product_pdf_qr/domains/__init__.py
src/product_pdf_qr/domains/public/__init__.py
src/product_pdf_qr/domains/product/__init__.py
src/product_pdf_qr/domains/version/__init__.py
src/product_pdf_qr/domains/storage/__init__.py
src/product_pdf_qr/domains/importer/__init__.py
src/product_pdf_qr/domains/qrcode/__init__.py
src/product_pdf_qr/domains/auth/__init__.py
src/product_pdf_qr/domains/audit/__init__.py
```

**测试**
```
tests/conftest.py
tests/unit/test_cli.py
tests/unit/test_config.py
tests/unit/test_database.py
tests/unit/test_health.py
tests/integration/test_initial_schema.py
```

**脚本**
```
scripts/verify_reproducible_build.sh
scripts/verify_clean_start.sh
scripts/check_markdown_links.py
```

**项目配置**
```
pyproject.toml
uv.lock
Makefile
.python-version
.gitignore
.env.example
```

**文档（本 PR 的改动）**
```
README.md
CLAUDE.md
docs/quality-gates-v1.md
docs/delivery-status.md
```

### 2.3 审核重点文件（建议优先读取原文）

以下文件与 Issue #5 的验收标准和已知风险点直接相关。**排序不代表重要性判断，仅为便于定位**：

| 文件 | 相关的审核问题 |
|---|---|
| `compose.yaml` | 服务编排、healthcheck、depends_on、端口绑定 |
| `Dockerfile` | 多阶段构建、非 root、镜像内容 |
| `docker/postgres/healthcheck.sh` | 数据库初始化就绪判据 |
| `scripts/verify_clean_start.sh` | 启动顺序验证的断言强度 |
| `scripts/verify_reproducible_build.sh` | G-04 可重复构建判据 |
| `migrations/versions/20260801_0001_initial_schema.py` | schema、约束、触发器、权限矩阵 |
| `docker/postgres/init/01-roles-and-databases.sh` | 角色创建与权限分离 |
| `tests/integration/test_initial_schema.py` | schema 约束是否真被验证 |
| `src/product_pdf_qr/domains/*/__init__.py` | 是否存在业务功能提前实现 |
| `src/product_pdf_qr/config.py` | 默认绑定地址 |
| `.github/workflows/ci.yml` | 门禁与 CI 的对应关系 |
| `README.md` | 环境要求、安全警示、不做项清单 |
| `CLAUDE.md`（diff） | 治理文件被修改的内容与范围 |

---

## 3. 判定依据文档位置

审核所需的需求、设计与门禁标准**均已在 `main` 上**，可直接读取：

| 文档 | 用途 |
|---|---|
| `docs/requirements-v2.md` | 唯一有效业务事实来源，24 条验收标准 |
| `docs/decision-register-v1.md` | B-01~B-14 业务决策、T-01~T-12 技术默认值 |
| `docs/architecture-v1.md` | 架构、模块划分、锁边界、文件移动顺序、二维码定位 |
| `docs/data-model-v1.md` | 表结构、约束、触发器、权限矩阵 |
| `docs/security-design-v1.md` | 访问控制、状态码策略、上传安全、审计事务隔离 |
| `docs/test-plan-v1.md` | 约 170 条测试用例与判定标准 |
| `docs/quality-gates-v1.md` | 19 个强制门禁 + 2 个告警门禁的通过标准 |
| `docs/development-plan-v1.md` | 阶段拆分、Worker 工作原则、审核流程 |
| `docs/advisor-protocol-v1.md` | 参谋职责、七项固定输出、独立性、冲突处理 |
| `docs/delivery-status.md` | 当前交付状态与 G-02 判定记录 |

**Issue #5 原文**：https://github.com/wyx-no1/product-pdf-qr/issues/5
（含 Phase 1-A 范围、不包含清单、六条验收标准、门禁范围、环境硬约束）

---

## 4. CI 产物位置

### 4.1 运行记录

**列表**：https://github.com/wyx-no1/product-pdf-qr/actions?query=branch%3Afeat%2Fphase1a-bootstrap

| Run ID | Commit | 结论 | URL |
|---|---|---|---|
| **30689523039** | `7514f04` | success | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30689523039 |
| 30689025108 | `4bc915e` | success | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30689025108 |
| 30688264357 | `d16ca8f` | success | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30688264357 |
| 30687662174 | `a0b9840` | success | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30687662174 |
| 30687559307 | `410faee` | success | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30687559307 |

### 4.2 最新 run 的三个 job

| Job | 结论 | 说明 |
|---|---|---|
| `quality` | success | 构建、类型检查、lint、单元测试、文档检查 |
| `database` | success | 迁移与 schema 集成测试 |
| `container` | success | 容器构建、clean-start 验证、非 root 断言、镜像内容检查、漏洞扫描 |

**命令行读取**：
```bash
gh run view 30689523039 --json jobs
gh run view 30689523039 --log          # 完整日志
```

### 4.3 CI 上传的产物（artifacts）

| Artifact 名 | 来源 job | 内容 |
|---|---|---|
| `quality-reports` | quality | `reports/`（mypy、ruff 等机器可读报告） |
| `database-reports` | database | `reports/`（集成测试报告） |
| `clean-start-evidence` | container | `reports/clean-start/`（每轮启动日志与 docker events） |

**下载**：
```bash
gh run download 30689523039 --name clean-start-evidence
gh run download 30689523039 --name quality-reports
gh run download 30689523039 --name database-reports
```

---

## 5. Reviewer 记录位置

Reviewer 共两轮，均由 `wyx-no1` 提出。

### 5.1 行级评论线程

| 时间（UTC） | 文件 | URL |
|---|---|---|
| 06:41:08 | `compose.yaml` | https://github.com/wyx-no1/product-pdf-qr/pull/6#discussion_r3694829181 |
| 06:44:43 | `compose.yaml` | https://github.com/wyx-no1/product-pdf-qr/pull/6#discussion_r3694838268 |
| 07:17:10 | `scripts/verify_clean_start.sh` | https://github.com/wyx-no1/product-pdf-qr/pull/6#discussion_r3694949081 |
| 07:22:20 | `scripts/verify_clean_start.sh` | https://github.com/wyx-no1/product-pdf-qr/pull/6#discussion_r3694977709 |

### 5.2 Review 记录

```
https://github.com/wyx-no1/product-pdf-qr/pull/6#pullrequestreview-4833888734
https://github.com/wyx-no1/product-pdf-qr/pull/6#pullrequestreview-4833897372
https://github.com/wyx-no1/product-pdf-qr/pull/6#pullrequestreview-4834015311
https://github.com/wyx-no1/product-pdf-qr/pull/6#pullrequestreview-4834043924
https://github.com/wyx-no1/product-pdf-qr/pull/6#pullrequestreview-4834074327
```

### 5.3 PR 评论（含修复说明）

| 时间（UTC） | URL |
|---|---|
| 06:27:14 | https://github.com/wyx-no1/product-pdf-qr/pull/6#issuecomment-5150184868 |
| 07:08:41 | https://github.com/wyx-no1/product-pdf-qr/pull/6#issuecomment-5150333392 |

**PR 描述**（含实现内容、门禁标注、不适用门禁清单）：
```bash
gh pr view 6 --json body --jq .body
```

### 5.4 命令行读取

```bash
gh api repos/wyx-no1/product-pdf-qr/pulls/6/comments   # 行级评论
gh api repos/wyx-no1/product-pdf-qr/pulls/6/reviews    # review 记录
gh api repos/wyx-no1/product-pdf-qr/issues/6/comments  # PR 评论
```

---

## 6. 验证产物位置

| 验证项 | 产物位置 | 获取方式 |
|---|---|---|
| 可重复构建（G-04） | CI `quality` job 日志中 `make build-reproducible` 步骤输出（含 `wheel_sha256`、`sdist_manifest_sha256`） | `gh run view 30689523039 --log` |
| 类型检查（G-05） | `reports/mypy.xml`，artifact `quality-reports` | `gh run download` |
| lint（G-06） | `reports/ruff.xml`，artifact `quality-reports` | `gh run download` |
| 容器构建（G-16） | CI `container` job 日志（非 root 断言、镜像内容检查、trivy 扫描结果） | `gh run view --log` |
| **clean start 三轮验证** | `reports/clean-start/attempt-{1,2,3}.log` 与 `attempt-{1,2,3}-events.log`，artifact `clean-start-evidence` | `gh run download 30689523039 --name clean-start-evidence` |
| 迁移三路径验证 | `tests/integration/test_initial_schema.py` 的执行结果，CI `database` job | `gh run view --log`；测试源码见 2.2 |
| 单元测试 | CI `quality` job 的 `make test-unit` 输出 | `gh run view --log` |

**本地复现**（需 Docker Engine 24+ 与 GNU Make）：
```bash
git checkout feat/phase1a-bootstrap
make verify-clean-start        # 3 轮空卷启动顺序验证
make build-reproducible        # G-04
make typecheck                 # G-05
make lint                      # G-06
docker compose --profile test run --rm test    # 完整测试
```

---

## 7. G-02 判定状态

**G-02 技术方案评审已通过，判定日期 2026-08-01，判定方为业务负责人（门禁责任方）。**

完整记录与审核链路见 `docs/delivery-status.md` 的「G-02 门禁判定记录」与「审核链路」两节，其中包含三轮参谋审查各自的发现、修正内容与可追溯位置。

### 参谋输出的留存位置

| 阶段 | 留存位置 |
|---|---|
| G-01 需求确认 | `automation/outputs/advisor-g01-full.txt`、`automation/outputs/advisor-g01-final.txt`；已收编进 `docs/advisor-review-g01.md` |
| G-02 技术方案 | 三轮审查的**发现与修正**可定位到设计文档具体章节并在 PR #4 提交历史中追溯；**参谋原始输出文件未留存在 `automation/outputs/`** |

**如实说明**：G-02 的参谋原始输出未按 `docs/advisor-protocol-v1.md` 的留存规则落文件。若审核认为该缺失影响 G-02 判定的可核验性，请在意见中指出——**这不是 Orchestrator 可以代为判断的事项**。

---

## 8. 本索引的完整性声明

### 已列出

- PR #6 全部 43 个修改文件的路径与三种访问方式
- 五次 CI 运行的 ID 与 URL、三个 job、三类 artifact 及下载命令
- 两轮 Reviewer 的四条行级评论、五条 review 记录、两条 PR 评论的完整 URL
- 七类验证产物的位置与获取方式
- 十份判定依据文档的位置
- G-02 判定状态与参谋留存位置

### 未列出（本索引不提供）

- 任何文件的内容、摘要或节选
- 任何关于代码质量、风险高低、是否应合并的判断
- Orchestrator 对上述任何证据的解读

### 若发现索引缺漏

若审核过程中发现某项必需证据未被本索引列出，请在意见中指明，由 Orchestrator 补充索引条目。**不要因索引未列出而认为该证据不存在。**

---

## 9. 审核依据的协议要求

按 `docs/advisor-protocol-v1.md`：

- 参谋须产出**七项完整输出**：当前决定或方案 / 支持理由 / 可能忽略的风险 / 其他可选方案 / 判断错误后是否容易恢复 / 推荐结论（接受/调整/暂停）/ 是否必须由业务负责人确认。**缺任一项视为未产出。**
- 参谋须**独立阅读**当前需求（`docs/requirements-v2.md`）与客观证据，不得只重复 Orchestrator 的结论。
- 参谋**只读、无执行权与决定权**：不改文件、不指挥 Worker、不提交或合并 PR、不替业务负责人决定业务规则。
- 参谋意见**不构成门禁通过**；门禁责任方仍须结合全部证据独立判定。
- 参谋与 Orchestrator 存在**未解决冲突**时，门禁判定为「未通过—冲突未解决」，须转普通语言说明后交业务负责人裁决。
- 记录须写明参谋身份、Orchestrator 身份、实施 Worker 身份，并给出「无角色重叠」核验结论。

**本项目参谋角色**：DeepSeek V4 Flash（只读、无执行权）。
