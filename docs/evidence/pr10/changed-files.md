# PR #10 变更文件清单

生成日期：2026-08-03
快照 commit：`587fd99`
比较基准：`git diff origin/main...587fd99 -- . ':(exclude)docs/evidence/pr10/**'`（三点式，与该代码快照除证据目录外的 GitHub PR「Files changed」一致）
merge base：`bda51a4`

> 本清单只列出**文件路径与变更类型**，不含任何代码内容。
> 完整改动见同目录 `diff.patch`；源码请读取 PR 分支。

## 汇总

| 项 | 值 |
|---|---|
| 变更文件数 | **39** |
| 新增行 | **4356** |
| 删除行 | **26** |

## 完整清单

| 文件 | 变更类型 |
|---|---|
| `.env.example` | 修改 |
| `README.md` | 修改 |
| `compose.yaml` | 修改 |
| `pyproject.toml` | 修改 |
| `src/product_pdf_qr/config.py` | 修改 |
| `src/product_pdf_qr/dependencies.py` | 新增 |
| `src/product_pdf_qr/domains/audit/__init__.py` | 修改 |
| `src/product_pdf_qr/domains/audit/service.py` | 新增 |
| `src/product_pdf_qr/domains/product/__init__.py` | 修改 |
| `src/product_pdf_qr/domains/product/router.py` | 新增 |
| `src/product_pdf_qr/domains/product/service.py` | 新增 |
| `src/product_pdf_qr/domains/public/__init__.py` | 修改 |
| `src/product_pdf_qr/domains/public/router.py` | 新增 |
| `src/product_pdf_qr/domains/public/service.py` | 新增 |
| `src/product_pdf_qr/domains/qrcode/__init__.py` | 修改 |
| `src/product_pdf_qr/domains/qrcode/router.py` | 新增 |
| `src/product_pdf_qr/domains/qrcode/service.py` | 新增 |
| `src/product_pdf_qr/domains/storage/__init__.py` | 修改 |
| `src/product_pdf_qr/domains/storage/router.py` | 新增 |
| `src/product_pdf_qr/domains/storage/service.py` | 新增 |
| `src/product_pdf_qr/domains/version/__init__.py` | 修改 |
| `src/product_pdf_qr/domains/version/service.py` | 新增 |
| `src/product_pdf_qr/main.py` | 修改 |
| `src/product_pdf_qr/upload_limit.py` | 新增 |
| `tests/__init__.py` | 新增 |
| `tests/integration/__init__.py` | 新增 |
| `tests/integration/test_business_loop.py` | 新增 |
| `tests/unit/__init__.py` | 新增 |
| `tests/unit/test_api_contract.py` | 新增 |
| `tests/unit/test_business_services.py` | 新增 |
| `tests/unit/test_config.py` | 修改 |
| `tests/unit/test_management_handlers.py` | 新增 |
| `tests/unit/test_product_domain.py` | 新增 |
| `tests/unit/test_public_api.py` | 新增 |
| `tests/unit/test_public_domain.py` | 新增 |
| `tests/unit/test_qrcode_domain.py` | 新增 |
| `tests/unit/test_storage_domain.py` | 新增 |
| `tests/unit/test_upload_limit.py` | 新增 |
| `uv.lock` | 修改 |

## 本 PR **未修改**的文件（供范围与边界核验）

以下文件在本 PR 中**无任何改动**，可据此核验 Phase 1-B 是否越界：

| 文件 / 目录 | 意义 |
|---|---|
| `migrations/` | 数据库 schema 未变更；schema 由 Phase 1-A（PR #6）建立 |
| `docs/requirements-v1.md` | 历史版需求，须保持字节级不变 |
| `docs/requirements-v2.md` | 唯一有效业务事实来源 |
| `CLAUDE.md` | 治理文件（PR #7 起禁止 Worker 修改） |
| `docs/quality-gates-v1.md` | 治理文件 |
| `docs/advisor-protocol-v1.md` | 治理文件 |
| `docs/decision-register-v1.md` | 治理文件 |
| `docs/test-plan-v1.md` | 测试判定标准，禁止为适配实现而修改 |

**核验方式**（在 PR 分支上执行）：

```bash
git diff origin/main...587fd99 -- migrations/
git diff origin/main...587fd99 -- docs/requirements-v1.md docs/requirements-v2.md
git diff origin/main...587fd99 -- CLAUDE.md docs/quality-gates-v1.md docs/advisor-protocol-v1.md docs/decision-register-v1.md docs/test-plan-v1.md
```

以上命令**应全部无输出**。若有输出，说明存在越界改动，请在审查意见中指出。

## 按目录分组

### 应用入口与横切（4）
`src/product_pdf_qr/main.py`、`config.py`、`dependencies.py`、`upload_limit.py`（新增）

### 业务域（15）
`domains/product/{__init__,router,service}.py`
`domains/version/{__init__,service}.py`
`domains/storage/{__init__,router,service}.py`
`domains/qrcode/{__init__,router,service}.py`
`domains/public/{__init__,router,service}.py`
`domains/audit/{__init__,service}.py`

### 测试（15）
`tests/__init__.py`、`tests/integration/{__init__,test_business_loop}.py`
`tests/unit/{__init__,test_api_contract,test_business_services,test_config,test_management_handlers,test_product_domain,test_public_api,test_public_domain,test_qrcode_domain,test_storage_domain,test_upload_limit}.py`

### 配置与文档（5）
`.env.example`、`compose.yaml`、`pyproject.toml`、`uv.lock`、`README.md`
