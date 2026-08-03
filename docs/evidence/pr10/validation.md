# PR #10 审查证据 · 验证记录

生成日期：2026-08-03
证据生成时刻 commit：`587fd99`
比较基准：`origin/main...587fd99`（三点式，merge base `bda51a4`；`docs/evidence/pr10/` 为避免自引用不包含在快照中）

> **本文件记录的是「验证在哪里、结果是什么」，不是对结果有效性的判断。**
> 所有声称均可通过下方给出的位置独立核验。**发现记录与实际不符时，以实际为准。**

---

## 1. Reviewer 审核结果

Reviewer 为 `wyx-no1`，共两批意见，均已产生修复提交。

### 1.1 意见位置

| 类型 | URL |
|---|---|
| 行级评论（`upload_limit.py`） | https://github.com/wyx-no1/product-pdf-qr/pull/10#discussion_r3700862167 |
| Review 记录 | https://github.com/wyx-no1/product-pdf-qr/pull/10#pullrequestreview-4840267450 |
| Review 记录 | https://github.com/wyx-no1/product-pdf-qr/pull/10#pullrequestreview-4840345705 |

```bash
gh api repos/wyx-no1/product-pdf-qr/pulls/10/comments
gh api repos/wyx-no1/product-pdf-qr/pulls/10/reviews
gh api repos/wyx-no1/product-pdf-qr/issues/10/comments
```

### 1.2 第一批（三项）→ 修复提交 `e4755d0`

| 问题 | 要求 | 分支上可核验的位置 |
|---|---|---|
| 上传大小限制执行过晚 | 在可信入口（ASGI）限制；超限尽早返回 413；增加 HTTP 层早终止测试 | `src/product_pdf_qr/upload_limit.py`、`tests/unit/test_upload_limit.py` |
| PDF 结构解析安全 | 在可限制资源的隔离环境执行；增加 CPU/内存/时间限制；增加异常或慢解析测试 | `src/product_pdf_qr/domains/storage/service.py`、`tests/unit/test_storage_domain.py` |
| 取消任务清理 | 覆盖 `CancelledError`；用 `finally` 保证清理；publish 前后取消均需测试；保留必要审计 | `src/product_pdf_qr/domains/version/service.py`、`tests/unit/test_business_services.py` |

### 1.3 第二批（一项）→ 修复提交 `e43eeb6`、`ebddbfe`

| 问题 | 要求 | 分支上可核验的位置 |
|---|---|---|
| 预解析阶段拒绝的大文件上传无 `pdf_upload_rejected` 审计 | Content-Length 超限与 chunked 流超限均须产生审计事件；信息安全；增加 HTTP 层测试 | `src/product_pdf_qr/upload_limit.py`、`tests/unit/test_upload_limit.py` |

### 1.4 Orchestrator 后续核验提出的两项 → 修复提交 `ebddbfe`

以下为 Orchestrator 在核验 `e43eeb6` 后提出的观察。分支已产生后续修复提交 `ebddbfe`；**是否真正闭合仍请读取 `src/product_pdf_qr/upload_limit.py` 与测试原文独立核验。**

**(a) 超限拒绝审计的大小语义**

Content-Length 路径记录 `declared_content_length` 与 `declared_request_body_verified: false`；chunked 路径记录 `received_bytes_before_abort`。两条路径均记录 `complete_pdf_byte_length_known: false`，避免把客户端声明值或提前终止前的接收量误称为完整 PDF 的真实字节长度，同时所有 detail 键均不含 `file_size` 子串。

两类原因分别记录为 `content_length_exceeded` 与 `chunked_stream_exceeded`。中间件无法取得可信操作者身份，因此记录 `actor_type=anonymous`、`actor_id=NULL`；`product_id` 仅从路径解析，不查询数据库。

**(b) 审计写入失败不得阻断 413 响应**

`ebddbfe` 为审计写入增加失败保护与应用错误日志，并新增 HTTP 层回归测试，断言审计失败时仍返回 413，且 Content-Length 超限请求体仍读取 0 字节。

**请 Advisor 独立判断**：实现与测试是否确实满足上述语义。

### 1.5 G-10 字段命名复核 → 修复提交 `587fd99`

| Reviewer 要求 | 修复后字段 | 语义 |
|---|---|---|
| Content-Length 声明值 | `declared_content_length` | 客户端声明且未经验证；不代表真实文件大小 |
| 提前终止前已接收字节数 | `received_bytes_before_abort` | 只代表终止前接收量；不代表完整文件大小 |
| 不使用模糊 `file_size` 字段 | `complete_pdf_byte_length_known: false` | 不含 `file_size` 子串，并明确完整 PDF 字节长度未知 |

HTTP 层测试继续保留原有早终止断言，并对两类 detail 的新字段名、正确值以及不存在 `file_size` 子串进行严格断言。

---

## 2. CI 结果

### 2.1 运行记录（分支 `feat/phase1b-business-loop`）

| Run ID | Commit | 结论 | URL |
|---|---|---|---|
| **30781351800** | **`587fd99`**（当前代码快照） | **success** | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30781351800 |
| 30780345357 | `ebddbfe` | success | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30780345357 |
| 30777551571 | `e43eeb6` | success | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30777551571 |
| 30776557635 | `e4755d0` | success | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30776557635 |
| 30694252828 | `f409b3f` | success | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30694252828 |
| 30694179092 | `70adea8` | **failure** | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30694179092 |
| 30693975039 | `476f69c` | success | https://github.com/wyx-no1/product-pdf-qr/actions/runs/30693975039 |

> **`70adea8` 的失败未被隐藏。** 后续提交 `f409b3f` 转为 success。**该失败的原因是否被真正解决而非绕过，请独立核验**——比对两个提交的差异即可：
> ```bash
> git diff 70adea8..f409b3f
> ```

### 2.2 当前代码快照的三个 job（run `30781351800`）

| Job | 结论 | 开始（UTC） | 结束（UTC） | 覆盖内容 |
|---|---|---|---|---|
| `quality` | success | 03:15:13 | 03:16:10 | 构建、类型检查、lint、单元测试、文档检查 |
| `database` | success | 03:15:12 | 03:15:58 | 真库集成测试（含并发场景） |
| `container` | success | 03:15:13 | 03:17:01 | 镜像构建、三轮净卷启动、非 root、镜像内容、Trivy 扫描 |

### 2.3 CI 产物获取

```bash
gh run view 30781351800 --json jobs          # job 状态
gh run view 30781351800 --log                # 完整日志
gh run download 30781351800 --name quality-reports        # mypy.xml / ruff.xml / 覆盖率
gh run download 30781351800 --name database-reports       # 集成测试报告
gh run download 30781351800 --name clean-start-evidence   # 净卷启动证据
```

---

## 3. 测试结果

### 3.1 PR 描述中声称的门禁证据

以下为 **PR 描述的原文声称**，`gh pr view 10 --json body --jq .body` 可取全文。**请独立核验其是否属实。**

| 门禁 | 声称内容 |
|---|---|
| G-04 构建 | `make build-reproducible` 退出码 0；wheel SHA-256 `2400b671e2c7f99681439e3b2259299bdcc0a1be5e4692880f67d451ae5df70a` |
| G-05 类型检查 | `make typecheck`，mypy **46 个源文件零错误** |
| G-06 lint | `make lint`，ruff check + format check 通过 |
| G-07 单元测试 | `make test-unit`，**88 passed，覆盖率 90.65%**（阈值 90%） |
| G-08 API 契约 | OpenAPI 表面、四态响应、上传/二维码/对账 handler 测试在 `tests/unit/`；真库主线在 `tests/integration/test_business_loop.py` |
| G-11 文件上传检查 | 覆盖 ASGI 早终止、五层校验、隔离解析进程资源上限、路径安全、原子发布、取消清理与失败恢复 |
| G-12 随机标识防枚举 | 覆盖 128 位 CSPRNG、Base32 契约、四态 200 与未命中收紧 |
| G-13 并发一致性 | 真库测试含并发同编码、8 路同 PDF（只新增 1 版）、不同内容同产品、不同产品并发、上传与同锁恢复写入竞态 |
| G-16 容器构建 | CI `container` job 执行镜像构建、三轮净卷启动、非 root、镜像内容与 Trivy 检查 |

> **覆盖率 90.65% 对阈值 90% 的余量仅 0.65 个百分点**，供判断是否稳健。

### 3.2 未标记通过的门禁（PR 原文）

| 门禁 | 声称 |
|---|---|
| **G-09** | **「部分覆盖，未判定通过」**——仅闭合新建即出码 → 未上传 → 首次上传 → 新版替换；恢复、停用、启用管理链路等待 Phase 2 |
| G-10 | 合并前 Advisor 强制复核，尚未触发，不标记通过 |
| G-14 | 完整审计查询不适用；本阶段仅实现要求的事件写入 |
| G-15 | 备份验证不适用 |
| G-17 | 公网验收未触发；本阶段禁止部署 |
| G-18 | 人工发布批准未触发 |
| G-19 | 回滚演练不适用 |

### 3.3 未覆盖用例（依据 Governance Issue #9 裁决）

**T-C-25、T-C-27、T-C-28 依赖 Phase 3 能力（Excel 导入、批量 ZIP），本阶段未覆盖，不声称通过。**

裁决原文：https://github.com/wyx-no1/product-pdf-qr/issues/9

> 该矛盾由 Phase 1-B Worker 在自检中发现并上报，未越界实现。矛盾根源是 Orchestrator 编写 Issue #8 时引用「T-C 全组」而未逐条核对组内依赖。**请独立判断该裁决本身是否成立。**

### 3.4 测试源码位置（在 PR 分支上）

| 测试文件 | 分支路径 | 行数 |
|---|---|---|
| 上传限制（HTTP 层） | `tests/unit/test_upload_limit.py` | 216 |
| 存储与 PDF 解析 | `tests/unit/test_storage_domain.py` | 291 |
| 业务服务（含取消场景） | `tests/unit/test_business_services.py` | 469 |
| 公开 API 四态 | `tests/unit/test_public_api.py` | 134 |
| 管理端 handler | `tests/unit/test_management_handlers.py` | 293 |
| 产品域与 `public_token` | `tests/unit/test_product_domain.py` | 88 |
| 二维码域 | `tests/unit/test_qrcode_domain.py` | 110 |
| **真库集成主线** | `tests/integration/test_business_loop.py` | 543 |

**上表未列出的测试**：`tests/unit/test_api_contract.py`、`tests/unit/test_config.py`、`tests/unit/test_public_domain.py`。三者同样在 PR 分支上，改动见 `diff.patch`。

> **本目录不含源码副本。** 上表只给路径，请在 PR 分支上读取原文。

### 3.5 本地复现

```bash
git fetch origin
git checkout feat/phase1b-business-loop
make typecheck && make lint && make test-unit
docker compose --profile test run --rm test    # 含真库集成测试
```

---

## 4. 建议独立核验的判据

以下为设计文档要求的性质。**请自行确认代码是否满足，不要采信本文件或 PR 描述的措辞。**

### 上传与解析

1. 大小限制是否**早于 multipart 物化**生效
2. 测试是否能**区分「读完才拒绝」与「没读完就拒绝」**，而不只是断言返回 413
3. PDF 解析的资源限制是否**真正终止越界进程**，而非仅放弃等待
4. 解析子进程是否**不写文件、不访问数据库、不联网**

### 取消与审计

5. `CancelledError` 是否被**重新抛出**而非吞掉
6. 取消时的审计写入是否**真的落库**（取消传播中异步写入本身也可能被取消）
7. 审计内容是否**不含文件内容或敏感数据**

### 并发与数据一致性

8. 产品行锁是否**先于**当前版本判重获取，且判重在**锁内重新读取**当前版本
9. 文件移动是否**先于**事务提交，且为同文件系统原子 `rename`、目标已存在时跳过不覆盖
10. 是否存在**孤儿文件自动清理路径**（设计要求：不得存在，只做只读对账）
11. 内容判重是否**只与当前版本比较**；与历史版本相同但非当前版本时是否允许创建新版本（B-13）
12. 是否存在 `UNIQUE (product_id, pdf_file_id)` 之类会破坏 B-13 的约束

### 公开访问

13. 四种状态（不存在/停用/未上传/正常）是否**一律返回 200**，且均设 `Cache-Control: no-store`
14. 三态判定顺序是否为**先停用 → 再判当前版本是否为空 → 正常**
15. 提示页是否**不含产品编码、内部 ID、版本信息**

### 二维码

16. 二维码是否**不参与任何事务**
17. 生成失败时是否**绝不产生占位图、默认图或改名文件**
18. 产品创建是否**永不因二维码失败而回滚**

### 范围边界

19. `domains/auth/` 与 `domains/importer/` 是否仍为空骨架（Phase 1-B 不含认证与 Excel 导入）
20. `migrations/` 是否确实未被修改
21. requirements 两份与四份治理文件是否确实未被修改

---

## 5. 相关文档位置

判定依据文档均在 `main` 上，可直接读取：

```
docs/requirements-v2.md          唯一有效业务事实来源，24 条验收标准
docs/decision-register-v1.md     B-01~B-14、T-01~T-12
docs/architecture-v1.md          锁边界（5.2）、二维码定位（3.5）
docs/data-model-v1.md            表结构、约束、触发器、权限矩阵
docs/security-design-v1.md       四态 200（第 2 节）、上传安全（第 3 节）、审计事务隔离（第 8A 节）
docs/test-plan-v1.md             约 170 条用例与判定标准
docs/quality-gates-v1.md         门禁通过标准
docs/development-plan-v1.md      Phase 1-A / 1-B 拆分与门禁范围
docs/advisor-protocol-v1.md      七项输出、独立性、冲突处理、第 9 节证据链规则
```

**Issue**：#8（范围与验收）、#9（Governance 裁决）
**在线证据索引**：`docs/review-context-pr10.md`
