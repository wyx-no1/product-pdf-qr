# PR #10 审查证据索引（供 DeepSeek Advisor 使用 · G-10 节点）

生成日期：2026-08-03
生成者：Orchestrator
对象：PR #10（Phase 1-B 实现 PDF 二维码最小业务闭环）

---

## ⚠️ 本文件的性质

**本文件只是证据索引，不替代原始代码。**

| 本文件是什么 | 本文件不是什么 |
|---|---|
| 原始证据的**位置清单** | 代码、摘要或转述 |
| 告诉审核者「去哪里看」 | 告诉审核者「看到了什么」 |
| 可核对完整性的目录 | 结论、评价或判断 |

### 对 Advisor 的强制要求

按 `docs/advisor-protocol-v1.md` 第 9 节：

> **审核必须基于原始证据，不得基于总结。**

1. **禁止**仅依据本文件形成判断。本文件不含任何代码内容。
2. **必须**通过第 2 节的位置读取原始文件后再判断。
3. 某项证据**无法访问**时，必须在意见中明确写「该项未核验」，**不得**以索引描述代替核验，也不得跳过该项。
4. 索引与原始证据不一致时，**以原始证据为准**，并在意见中指出索引错误。
5. 七项固定输出的每一项，都应能指明依据的是哪一份原始证据。
6. 第 8 节列出了本索引的**盲区**，请据此判断是否需要补录。

---

## 1. PR 信息

| 项 | 值 |
|---|---|
| PR | https://github.com/wyx-no1/product-pdf-qr/pull/10 |
| 标题 | feat: 实现 Phase 1-B PDF 二维码最小业务闭环 |
| 分支 | `feat/phase1b-business-loop` → `main` |
| 最新 commit | **`e43eeb6`** |
| 基线 | `main` = `bda51a4` |
| 状态 | **OPEN，未合并**，MERGEABLE |
| 关联 Issue | #8（https://github.com/wyx-no1/product-pdf-qr/issues/8） |
| 相关 Governance Issue | #9（https://github.com/wyx-no1/product-pdf-qr/issues/9） |
| 规模 | **39 个文件，+4243 / -26** |

### 提交清单

| SHA | 说明 |
|---|---|
| `e43eeb6` | fix: audit pre-parser upload rejections |
| `e4755d0` | fix: harden PDF upload resource boundaries |
| `f409b3f` | test: use upload response version identifier |
| `70adea8` | test: cover upload and restore pointer race |
| `476f69c` | test: strengthen phase 1b failure and concurrency coverage |
| `a211b10` | feat: implement phase 1b PDF QR business loop |

### 修改文件列表（全部 39 个）

**应用入口与横切**
```
src/product_pdf_qr/main.py
src/product_pdf_qr/config.py
src/product_pdf_qr/dependencies.py
src/product_pdf_qr/upload_limit.py          ← 新增，ASGI 上传限制中间件
```

**业务域**
```
src/product_pdf_qr/domains/product/{__init__,router,service}.py
src/product_pdf_qr/domains/version/{__init__,service}.py
src/product_pdf_qr/domains/storage/{__init__,router,service}.py
src/product_pdf_qr/domains/qrcode/{__init__,router,service}.py
src/product_pdf_qr/domains/public/{__init__,router,service}.py
src/product_pdf_qr/domains/audit/{__init__,service}.py
```

**测试**
```
tests/__init__.py
tests/integration/__init__.py
tests/integration/test_business_loop.py
tests/unit/__init__.py
tests/unit/test_api_contract.py
tests/unit/test_business_services.py
tests/unit/test_config.py
tests/unit/test_management_handlers.py
tests/unit/test_product_domain.py
tests/unit/test_public_api.py
tests/unit/test_public_domain.py
tests/unit/test_qrcode_domain.py
tests/unit/test_storage_domain.py
tests/unit/test_upload_limit.py              ← 新增，HTTP 层上传限制测试
```

**配置与文档**
```
.env.example    compose.yaml    pyproject.toml    uv.lock    README.md
```

**未修改（可据此核验范围边界）**：`migrations/`、`docs/requirements-v1.md`、`docs/requirements-v2.md`、`CLAUDE.md`、`docs/quality-gates-v1.md`、`docs/advisor-protocol-v1.md`、`docs/decision-register-v1.md`、`docs/test-plan-v1.md`

---

## 2. 原始证据索引

### 2.1 PR diff 位置

**GitHub 网页**
```
完整 diff：https://github.com/wyx-no1/product-pdf-qr/pull/10/files
单文件：   https://github.com/wyx-no1/product-pdf-qr/blob/feat/phase1b-business-loop/<路径>
```

**本地 git（无需切换分支）**
```bash
git fetch origin
git show origin/feat/phase1b-business-loop:<路径>          # 读单个文件
git diff origin/main..origin/feat/phase1b-business-loop    # 完整 diff
git log origin/main..origin/feat/phase1b-business-loop     # 提交历史
git show <sha>                                             # 单个提交
```

### 2.2 CI 运行记录

**列表**：https://github.com/wyx-no1/product-pdf-qr/actions?query=branch%3Afeat%2Fphase1b-business-loop

| Run ID | Commit | 结论 | URL |
|---|---|---|---|
| **30777551571** | `e43eeb6` | success | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30777551571 |
| 30776557635 | `e4755d0` | success | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30776557635 |
| 30694252828 | `f409b3f` | success | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30694252828 |
| 30694179092 | `70adea8` | **failure** | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30694179092 |
| 30693975039 | `476f69c` | success | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30693975039 |

> **`70adea8` 的失败未被隐藏**，列出供审核者自行判断其原因是否已被真正解决，而非绕过。

**三个 job**：`quality`（构建/类型/lint/单元/文档）、`database`（真库集成测试）、`container`（镜像构建、净卷启动、非 root、镜像内容、漏洞扫描）

**命令行读取**
```bash
gh run view 30777551571 --json jobs
gh run view 30777551571 --log              # 完整日志
gh run download 30777551571                # 全部 artifacts
```

### 2.3 测试结果

| 类别 | 位置 | 获取方式 |
|---|---|---|
| 单元测试结果 | CI `quality` job 的 `make test-unit` 输出 | `gh run view 30777551571 --log` |
| 覆盖率报告 | artifact `quality-reports` 中 `reports/` | `gh run download 30777551571 --name quality-reports` |
| 类型检查报告 | `reports/mypy.xml`，同上 artifact | 同上 |
| lint 报告 | `reports/ruff.xml`，同上 artifact | 同上 |
| 集成测试结果 | CI `database` job 输出 | `gh run view --log` |
| 净卷启动证据 | artifact `clean-start-evidence` | `gh run download --name clean-start-evidence` |
| 测试源码 | `tests/unit/`、`tests/integration/`（见 1 节文件列表） | `git show` |

**本地复现**（需 Docker Engine 24+ 与 GNU Make）
```bash
git checkout feat/phase1b-business-loop
make typecheck && make lint && make test-unit
docker compose --profile test run --rm test    # 含真库集成测试
```

### 2.4 Reviewer 审核结果

| 类型 | URL |
|---|---|
| 行级评论（`upload_limit.py`） | https://github.com/wyx-no1/product-pdf-qr/pull/10#discussion_r3700862167 |
| Review 记录 | https://github.com/wyx-no1/product-pdf-qr/pull/10#pullrequestreview-4840267450 |
| Review 记录 | https://github.com/wyx-no1/product-pdf-qr/pull/10#pullrequestreview-4840345705 |

**PR 描述与全部评论**
```bash
gh pr view 10 --json body --jq .body
gh api repos/wyx-no1/product-pdf-qr/issues/10/comments
gh api repos/wyx-no1/product-pdf-qr/pulls/10/comments
gh api repos/wyx-no1/product-pdf-qr/pulls/10/reviews
```

**Reviewer 提出的问题批次**（供核验修复是否真正闭合）：

1. 第一批三项：上传大小限制执行过晚、PDF 结构解析无资源限制、取消任务清理 → 由 `e4755d0` 修复
2. 第二批一项：预解析阶段拒绝的大文件上传无 `pdf_upload_rejected` 审计 → 由 `e43eeb6` 修复

---

## 3. Phase 1-B 范围核对

范围定义见 Issue #8 与 `docs/development-plan-v1.md`。**请独立核对实现是否与范围一致**。

### 3.1 应包含（对应实现位置）

| 范围项 | 主要位置 |
|---|---|
| 产品创建 | `domains/product/service.py`、`domains/product/router.py` |
| 产品编码校验 | `domains/product/service.py` |
| `public_token` 生成 | `domains/product/service.py` |
| PDF 上传 | `domains/storage/service.py`、`domains/version/service.py` |
| PDF 安全校验 | `domains/storage/service.py` |
| 当前版本管理 | `domains/version/service.py` |
| 二维码生成 | `domains/qrcode/service.py`、`domains/qrcode/router.py` |
| 公开扫码访问 | `domains/public/service.py`、`domains/public/router.py` |
| 四态统一 200 | `domains/public/router.py` |
| 审计事件写入 | `domains/audit/service.py` |

### 3.2 应不包含（请核验确无实现）

- **管理员认证**（登录、会话、密码哈希）
- **Excel 导入**
- **历史版本管理界面**（版本列表、版本详情）
- **停用/恢复管理入口**（停用启用切换、恢复历史版本）
- 批量 ZIP 下载
- 孤儿文件自动清理
- 完整审计查询界面

**核验方式**：`domains/auth/` 与 `domains/importer/` 是否仍为 Phase 1-A 留下的空骨架；`git diff` 中是否出现上述能力的实现。

### 3.3 门禁标注

PR 声称适用九项：G-04、G-05、G-06、G-07、G-08、G-11、G-12、G-13、G-16。

**G-09 声称为「部分覆盖，未判定通过」**——请核验该标注是否属实、是否有任何等价于「通过」的表述。

**T-C-25、T-C-27、T-C-28 声称「依赖 Phase 3 能力，本阶段未覆盖」**——依据 Governance Issue #9 的裁决。请核验裁决本身是否成立。

---

## 4. 安全证据位置

以下四项为 Reviewer 两批意见的修复重点。**请读取原始代码与测试后独立判断修复是否真正有效。**

| 主题 | 实现位置 | 测试位置 |
|---|---|---|
| **上传大小限制** | `src/product_pdf_qr/upload_limit.py`（ASGI 中间件）；`domains/storage/service.py`（服务层纵深防御） | `tests/unit/test_upload_limit.py` |
| **PDF 解析隔离** | `domains/storage/service.py` | `tests/unit/test_storage_domain.py`、`tests/unit/test_business_services.py` |
| **CancelledError 清理** | `domains/version/service.py`、`domains/storage/service.py` | `tests/unit/test_business_services.py`、`tests/unit/test_management_handlers.py` |
| **超限拒绝审计** | `src/product_pdf_qr/upload_limit.py` | `tests/unit/test_upload_limit.py` |

### 建议独立核验的判据

以下是设计文档要求的性质，**请自行确认代码是否满足，不要采信本索引的措辞**：

1. 大小限制是否**早于 multipart 物化**生效，而非在端点内
2. 测试是否能**区分「读完才拒绝」与「没读完就拒绝」**，而不只是断言返回 413
3. PDF 解析的资源限制是否**真正终止越界进程**，而非仅放弃等待
4. `CancelledError` 是否被**重新抛出**而非吞掉
5. 取消时的审计写入是否**真的落库**（取消传播中异步写入本身也可能被取消）
6. 超限拒绝审计中的**大小语义**是否如实（见第 7 节开放项）

### 相关设计依据

| 文档 | 相关章节 |
|---|---|
| `docs/security-design-v1.md` | 第 2 节（四态统一 200）、第 3 节（上传安全）、第 6 节（路径穿越）、第 8A 节（审计事务隔离） |
| `docs/architecture-v1.md` | 5.2（上传锁边界、文件移动顺序、孤儿策略）、3.5（二维码派生缓存） |
| `docs/decision-register-v1.md` | T-01、T-02、T-05、T-09、T-10、T-12、B-13、B-14 |

---

## 5. 数据模型证据位置

**Phase 1-B 未修改数据库 schema**——`migrations/` 目录在本 PR 中无变更。schema 由 Phase 1-A（PR #6）建立。

| 主题 | schema 定义 | 使用位置 |
|---|---|---|
| **`public_token`** | `migrations/versions/20260801_0001_initial_schema.py`（`products.public_token`，唯一约束） | 生成：`domains/product/service.py`；消费：`domains/public/service.py` |
| **当前版本指针** | 同上（`products.current_version_id` + 复合外键 `FOREIGN KEY (id, current_version_id) REFERENCES pdf_versions (product_id, id)`） | 移动：`domains/version/service.py`；读取：`domains/public/service.py` |
| **PDF 版本关系** | 同上（`pdf_versions` → `pdf_files` 多对一；只追加触发器） | `domains/version/service.py`、`domains/storage/service.py` |

### 建议独立核验的判据

1. `public_token` 是否为 CSPRNG、≥128 位熵、与产品编码/自增 ID/创建时间**无可推导关系**（T-10）
2. 当前版本指针的移动是否**始终在产品行锁内**，且判重在锁内重新读取（架构 5.2）
3. 内容判重是否**只与当前版本比较**，与历史版本相同但非当前版本时是否允许创建新版本（B-13）
4. 是否存在 `UNIQUE (product_id, pdf_file_id)` 之类会破坏 B-13 的约束
5. 历史版本是否严格只追加，代码中是否存在对 `pdf_versions`/`pdf_files` 的 UPDATE 或 DELETE

**schema 原文**
```bash
git show origin/main:migrations/versions/20260801_0001_initial_schema.py
```

---

## 6. 判定依据文档位置

全部已在 `main` 上，可直接读取：

| 文档 | 用途 |
|---|---|
| `docs/requirements-v2.md` | 唯一有效业务事实来源，24 条验收标准 |
| `docs/decision-register-v1.md` | B-01~B-14 业务决策、T-01~T-12 技术默认值 |
| `docs/architecture-v1.md` | 架构、锁边界、文件移动顺序、二维码定位 |
| `docs/data-model-v1.md` | 表结构、约束、触发器、权限矩阵、并发要点 |
| `docs/security-design-v1.md` | 访问控制、状态码策略、上传安全、审计事务隔离 |
| `docs/test-plan-v1.md` | 约 170 条用例与判定标准 |
| `docs/quality-gates-v1.md` | 门禁通过标准 |
| `docs/development-plan-v1.md` | Phase 1-A / 1-B 拆分与门禁范围 |
| `docs/advisor-protocol-v1.md` | 参谋职责、七项输出、独立性、冲突处理、第 9 节证据链规则 |
| `docs/delivery-status.md` | 当前交付状态 |

**Issue 原文**
```
Issue #8（Phase 1-B 范围与验收）：https://github.com/wyx-no1/product-pdf-qr/issues/8
Issue #9（Governance，T-C 用例范围裁决）：https://github.com/wyx-no1/product-pdf-qr/issues/9
```

---

## 7. ⚠️ 已知未闭合项（Orchestrator 主动披露）

以下两项由 Orchestrator 在核验 `e43eeb6` 时发现，**截至本索引生成时仍未产生新提交**。披露于此，供 Advisor 独立判断严重性与是否影响 G-10。

### 7.1 超限拒绝审计未记录文件大小

Reviewer 要求记录「文件大小（如果可获得）」。`upload_limit.py` 的审计 `detail` 中目前只有 `reason`、`stage`、`trigger`，**未记录声明大小或已接收字节数**。

相关的语义要求（Orchestrator 已提出，尚未落地）：两种情况的「大小」含义完全不同——Content-Length 是**客户端声明值、未经验证**；chunked 情况下可得的是**截断前已接收字节数，不是文件真实大小**（读取被故意提前终止）。若要记录，必须显式区分，否则会形成误导性记录。

**请 Advisor 判断**：不记录大小（当前状态）与记录但需精确区分语义，哪种更可取？

### 7.2 审计写入失败会阻断 413 响应

`upload_limit.py` 中的调用顺序为「先写审计、后发拒绝响应」。若审计写入抛出异常，**413 将无法返回**——安全拒绝被审计拖垮。

**请 Advisor 判断**：该顺序是否构成实际风险，以及是否应在 G-10 通过前修复。

> **说明**：以上两项是 Orchestrator 的观察，**不是结论**。请读取 `src/product_pdf_qr/upload_limit.py` 原文自行核验，若判断与此处不同，以你的核验为准。

---

## 8. 本索引的完整性声明

### 已列出

- PR #10 全部 39 个修改文件的路径与三种访问方式
- 六个提交的 SHA 与说明
- 五次 CI 运行的 ID 与 URL（含一次失败）、三个 job、artifact 下载命令
- Reviewer 的一条行级评论、两条 review、评论读取命令
- 四类安全证据与五类测试结果的位置
- 数据模型三项证据的 schema 与使用位置
- 十份判定依据文档与两个 Issue 的位置
- **两项已知未闭合项**（第 7 节）

### 未列出（本索引不提供）

- 任何代码内容、摘要或节选
- 任何关于代码质量、修复是否有效、是否应合并的判断
- Orchestrator 对证据的解读（第 7 节的两项观察已明确标注为观察而非结论）

### 若发现索引缺漏

若审核中发现某项必需证据未被列出，请在意见中指明，由 Orchestrator 补充索引条目。**不要因索引未列出而认为该证据不存在。**

---

## 9. 本次审查的定位

按 `docs/advisor-protocol-v1.md`：

- Phase 1-B 涵盖**公开访问面、随机标识、文件上传、并发写入与不可逆数据保护**，属**高风险合并**。按协议第 3 节，**G-10 节点的参谋复核为强制，不可跳过**。
- 参谋须产出**七项完整输出**：当前决定或方案 / 支持理由 / 可能忽略的风险 / 其他可选方案 / 判断错误后是否容易恢复 / 推荐结论（接受/调整/暂停）/ 是否必须由业务负责人确认。**缺任一项视为未产出，门禁不得通过。**
- 参谋须**独立阅读**当前需求与客观证据，不得只重复 Orchestrator 的结论。
- 参谋**只读、无执行权与决定权**：不改文件、不指挥 Worker、不提交或合并 PR、不替业务负责人决定业务规则。
- 参谋意见**不构成门禁通过**；门禁责任方仍须结合全部证据独立判定。
- 参谋与 Orchestrator 存在**未解决冲突**时，门禁判定为「未通过—冲突未解决」，须转普通语言说明后交业务负责人裁决。
- 记录须写明参谋身份、Orchestrator 身份、实施 Worker 身份，并给出「无角色重叠」核验结论。

**本项目参谋**：DeepSeek V4 Flash（只读、无执行权）。
**实施 Worker**：`product-pdf-qr-5`（Codex）。
**Orchestrator**：本会话（Claude）。
三者无角色重叠。
