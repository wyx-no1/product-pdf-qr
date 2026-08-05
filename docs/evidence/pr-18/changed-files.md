# Changed files for PR #18

Comparison: `origin/main...2556120bad9a955147df33a90f1c447e35c96bfe` (three-dot)

| Metric | Value |
|---|---:|
| Merge base | `8e6fd10410a021b369be2c9b566467c5ef305f93` |
| Changed files | 34 |
| Added lines | 4640 |
| Deleted lines | 125 |

## Added files

- `migrations/versions/20260804_0002_product_name.py`
- `src/product_pdf_qr/admin.py`
- `src/product_pdf_qr/auth_middleware.py`
- `src/product_pdf_qr/cli.py`
- `src/product_pdf_qr/domains/auth/rate_limit.py`
- `src/product_pdf_qr/domains/auth/service.py`
- `src/product_pdf_qr/templates/admin.html`
- `src/product_pdf_qr/templates/change_password.html`
- `src/product_pdf_qr/templates/login.html`
- `tests/unit/test_admin_auth_handlers.py`
- `tests/unit/test_admin_ui.py`
- `tests/unit/test_auth_domain.py`
- `tests/unit/test_auth_middleware.py`
## Modified files

- `.env.example`
- `README.md`
- `compose.yaml`
- `pyproject.toml`
- `src/product_pdf_qr/config.py`
- `src/product_pdf_qr/dependencies.py`
- `src/product_pdf_qr/domains/auth/__init__.py`
- `src/product_pdf_qr/domains/product/__init__.py`
- `src/product_pdf_qr/domains/product/router.py`
- `src/product_pdf_qr/domains/product/service.py`
- `src/product_pdf_qr/main.py`
- `src/product_pdf_qr/templates/base.html`
- `src/product_pdf_qr/upload_limit.py`
- `tests/integration/test_business_loop.py`
- `tests/integration/test_initial_schema.py`
- `tests/unit/test_api_contract.py`
- `tests/unit/test_business_services.py`
- `tests/unit/test_cli.py`
- `tests/unit/test_config.py`
- `tests/unit/test_management_handlers.py`
- `uv.lock`
## Deleted files

- None

## Unmodified-boundary verification

Run the following command from any worktree for this repository. It must produce no
output; any output means a protected boundary changed:

```bash
git diff origin/main...2556120bad9a955147df33a90f1c447e35c96bfe -- migrations/ docs/requirements-v1.md docs/requirements-v2.md CLAUDE.md docs/quality-gates-v1.md docs/advisor-protocol-v1.md docs/decision-register-v1.md docs/test-plan-v1.md
```

The complete changed-path list can be reproduced without relying on this file:

```bash
git diff --name-status origin/main...2556120bad9a955147df33a90f1c447e35c96bfe -- . ':(exclude)docs/evidence/**'
```
