# PR #6 审查上下文（供 DeepSeek Advisor 使用）

生成日期：2026-08-01
生成者：Orchestrator
用途：Advisor 无法读取 `feat/phase1a-bootstrap` 分支，本文件把审查所需内容摘录到 main 可读位置。

> **本文件的性质**
>
> 本文件是**摘录与整理**，不是分支内容本身。摘录可能有遗漏或转述失真。
> 若某处判断依赖精确原文，请在意见中指出需要补充的文件，由 Orchestrator 补录，**不要基于本文件的转述做确定性结论**。
> 本文件由 Orchestrator 编写，不代表 Worker 的自述，也不构成门禁通过证据。

---

## 1. PR 基本信息

| 项 | 值 |
|---|---|
| PR 编号 | **#6** |
| 标题 | feat: Phase 1-A 工程初始化与基础环境 |
| 分支 | `feat/phase1a-bootstrap` → `main` |
| 当前 HEAD | `7514f04` |
| 状态 | **OPEN，未合并**，MERGEABLE |
| 对应 Issue | #5（Phase 1-A：工程初始化与基础环境），父任务 #3 |
| CI | run `30689523039` 于 `7514f04`，`quality` / `database` / `container` **三个 job 全绿** |

### 提交历史（自 main 起）

```
7514f04 fix: remove clean-start host tool dependencies      ← Reviewer 第二轮：移除 jq 依赖
4bc915e fix: verify postgres initialization before migrations ← Reviewer 第一轮：healthcheck 误判
d16ca8f fix: wait for final postgres server
a0b9840 fix: remove compose healthcheck warning
410faee feat: bootstrap phase 1a foundation                  ← 主体实现
```

### 修改文件列表（43 个文件，+2944 / -19）

**根级配置**
```
.dockerignore          .env.example       .gitignore
.python-version        alembic.ini        compose.yaml
Dockerfile             Makefile           pyproject.toml
uv.lock                README.md
```

**CI 与容器**
```
.github/workflows/ci.yml
docker/postgres/init/01-roles-and-databases.sh
docker/postgres/healthcheck.sh
```

**迁移**
```
migrations/env.py
migrations/script.py.mako
migrations/versions/20260801_0001_initial_schema.py
```

**源码**（`src/product_pdf_qr/`）
```
__init__.py  __main__.py  config.py  database.py  errors.py  main.py
templates/base.html
domains/__init__.py
domains/{public,product,version,storage,importer,qrcode,auth,audit}/__init__.py
```

**测试**
```
tests/conftest.py
tests/unit/{test_cli,test_config,test_database,test_health}.py
tests/integration/test_initial_schema.py
```

**脚本**
```
scripts/check_markdown_links.py
scripts/verify_reproducible_build.sh
scripts/verify_clean_start.sh
```

**文档（已有文件的改动）**
```
CLAUDE.md                  （1 行，见第 4 节）
docs/quality-gates-v1.md   （补入实际命令）
docs/delivery-status.md    （状态更新）
```

---

## 2. 核心文件内容摘要

### 2.1 Dockerfile

三阶段构建，均基于 `python:3.12.13-alpine3.24` 并**按 digest 固定**（`sha256:6d4370...`）。

- **builder**：`uv sync --frozen --no-dev --no-editable` 装运行时依赖到 `/opt/venv`
- **development**：含全部依赖组、测试与脚本，`USER 10001:10001`
- **runtime**：
  ```dockerfile
  ENV APP_BIND_HOST=127.0.0.1 \
      APP_PORT=8000
  RUN addgroup -g 10001 -S app && adduser -u 10001 -S -D -G app ... \
      && mkdir -p /data/files && chown app:app /data/files
  COPY --from=builder --chown=app:app /opt/venv /opt/venv
  COPY --chown=app:app alembic.ini ./
  COPY --chown=app:app migrations ./migrations
  USER 10001:10001
  EXPOSE 8000
  CMD ["python", "-m", "product_pdf_qr"]
  ```

**关注点**：runtime 镜像**不含 src 目录**（依赖已装入 venv）、不含测试与开发工具；镜像层面的 `APP_BIND_HOST` 默认值是 **`127.0.0.1`**。

### 2.2 compose.yaml

项目名 `product-pdf-qr`，四个服务。

**db**
```yaml
image: postgres:16.14-alpine3.24@sha256:57c72f...   # digest 固定
ports:
  - "127.0.0.1:${POSTGRES_HOST_PORT:-5432}:5432"    # 宿主机仅回环
volumes:
  - postgres_data:/var/lib/postgresql/data
  - ./docker/postgres/init:/docker-entrypoint-initdb.d:ro
  - ./docker/postgres/healthcheck.sh:/usr/local/bin/app-db-healthcheck:ro
healthcheck:
  test: ["CMD", "/bin/sh", "/usr/local/bin/app-db-healthcheck"]
  interval: 2s
  timeout: 5s
  retries: 30
  start_period: 60s
```

**migrate**
```yaml
build: {context: ., target: runtime}
environment: {DATABASE_URL: ${MIGRATION_DATABASE_URL}}
command: ["alembic", "upgrade", "head"]
depends_on: {db: {condition: service_healthy}}
restart: "no"
```

**app**
```yaml
build: {context: ., target: runtime}
environment:
  DATABASE_URL: ${DATABASE_URL}
  APP_BIND_HOST: "0.0.0.0"        # ← 覆盖镜像默认值，见第 4.3 节
  APP_PORT: "8000"
  STORAGE_ROOT: ${STORAGE_ROOT:-/data/files}
ports:
  - "127.0.0.1:${APP_PORT:-8000}:8000"   # 宿主机仅回环
depends_on: {migrate: {condition: service_completed_successfully}}
healthcheck:                       # 容器内 urllib 请求 /health/ready
restart: unless-stopped
```

**test**（`profiles: ["test"]`，默认不启动）
基于 development 阶段，使用独立的 `TEST_*` 连接串与 `/tmp` 存储根，`depends_on: db: service_healthy`。

**卷**：`postgres_data`、`file_data`

### 2.3 CI workflow（`.github/workflows/ci.yml`）

触发：`pull_request` 与 `push: [main]`；`permissions: contents: read`。三个 job：

**quality**
```
uv sync --frozen --all-groups
make build-reproducible     → G-04
make typecheck              → G-05
make lint                   → G-06
make test-unit
make check-docs
upload-artifact: reports/
```

**database**
```
docker compose up --detach --wait db
make test-integration
  env: TEST_DATABASE_URL (app_rw) / TEST_MIGRATION_DATABASE_URL (app_migrate)
       / TEST_BACKUP_DATABASE_URL (app_backup)
always: docker compose logs db; docker compose down --volumes
```

**container**
```
docker compose build --pull app migrate
make verify-clean-start                       ← 3 轮空卷启动顺序验证
curl --fail http://127.0.0.1:8000/health/ready
Verify non-root process: docker compose exec app id -u != 0
Inspect runtime image contents:
  - assert pytest / mypy 不在镜像内
  - assert 无 /root/.cache、无 .env / *.pem / *.key
Scan runtime image: trivy，severity CRITICAL,HIGH，exit-code 1，ignore-unfixed false
upload-artifact: reports/clean-start/
always: docker compose logs; docker compose down --volumes
```

### 2.4 README

首行是 `> [!CAUTION]` 块：

> **Phase 1-A 不含任何管理员认证或业务接口。仅允许在本机开发与 CI 的合成数据环境中运行，禁止对外暴露，禁止部署到任何公网可达环境。**

**环境要求**（仅两项 + 一条数据约束）：
- Docker Engine 24+（含 Docker Compose v2）
- GNU Make
- 本地开发数据必须为合成数据
- 明确声明：**无需在宿主机安装 Python 或 PostgreSQL**

**一条命令启动**：`make dev`（首次自动从 `.env.example` 复制 `.env`）

README 中关于绑定的原文：

> Compose 内部的应用进程监听容器网络，以便 Docker 健康检查和端口转发；宿主机端口明确绑定 `127.0.0.1`，不会监听所有主机接口。直接运行应用时的默认绑定同样是 `127.0.0.1`。

**数据库角色职责分离表**：`app_rw`（运行时，最小表级与列级权限，无 DDL）/ `app_migrate`（迁移，schema 所有者）/ `app_backup`（全库只读），并写明「不要把迁移或备份账号配置给应用」。

另含迁移正向、回滚、空库重建三条命令，以及不做项清单。

### 2.5 迁移文件（`20260801_0001_initial_schema.py`）

单个迁移，`down_revision = None`，全部用 `op.execute` 写原生 SQL。

**表**：`admins`、`products`、`pdf_files`、`pdf_versions`、`admin_sessions`、`audit_events`

**关键约束**
```sql
-- products
code varchar(64) NOT NULL UNIQUE
public_token varchar(26) NOT NULL UNIQUE
CHECK (code ~ '^[A-Z0-9_-]{1,64}$')
CHECK (status IN ('active','disabled'))

-- pdf_versions
UNIQUE (product_id, version_no)
UNIQUE (product_id, id)          -- 供复合外键引用

-- 复合外键：防止当前版本指针跨产品
ALTER TABLE products
ADD CONSTRAINT fk_products_current_version
FOREIGN KEY (id, current_version_id)
REFERENCES pdf_versions (product_id, id)
```

**触发器**
```sql
-- products：拒绝修改 code / public_token / id
RAISE EXCEPTION 'product code is immutable';
RAISE EXCEPTION 'public token is immutable';
RAISE EXCEPTION 'product id is immutable';
CREATE TRIGGER trg_products_immutable BEFORE UPDATE ON products ...

-- pdf_versions / pdf_files：只追加
RAISE EXCEPTION '% is append-only: % is forbidden', TG_TABLE_NAME, TG_OP;
CREATE TRIGGER trg_pdf_versions_append_only BEFORE UPDATE OR DELETE ON pdf_versions ...
CREATE TRIGGER trg_pdf_files_append_only    BEFORE UPDATE OR DELETE ON pdf_files ...
```

**权限**
```sql
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM app_rw, app_backup;
GRANT SELECT, INSERT ON products, pdf_files, pdf_versions, admins, audit_events TO app_rw;
GRANT UPDATE (status, current_version_id, updated_at) ON products TO app_rw;
GRANT UPDATE (password_hash, must_change_password, password_updated_at, last_login_at)
  ON admins TO app_rw;
GRANT SELECT, INSERT, UPDATE, DELETE ON admin_sessions TO app_rw;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_backup;
```

`downgrade()` 存在，按相反顺序 DROP 触发器与表。

**角色创建**（`docker/postgres/init/01-roles-and-databases.sh`）：三个角色均 `NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT`；两个数据库 `OWNER app_migrate`。

### 2.6 `docker/postgres/healthcheck.sh`（第一轮修复产物）

POSIX sh，`set -eu`，通过 **TCP** 执行真实 SQL：

```sh
PGPASSWORD="$POSTGRES_PASSWORD" PGCONNECT_TIMEOUT=2 psql \
  --host=127.0.0.1 --username="$POSTGRES_USER" --dbname="$APP_DATABASE_NAME" \
  --set=ON_ERROR_STOP=1 --tuples-only --no-align <<'SQL'
SELECT
  current_database() = :'app_database_name'
  AND (SELECT count(*) = 2 FROM pg_database db
       JOIN pg_roles owner ON owner.oid = db.datdba
       WHERE db.datname IN (:'app_database_name', :'app_test_database_name')
         AND owner.rolname = 'app_migrate')
  AND (SELECT count(*) = 3 FROM pg_roles
       WHERE rolname IN ('app_migrate','app_rw','app_backup')
         AND NOT rolsuper AND NOT rolcreatedb
         AND NOT rolcreaterole AND NOT rolinherit);
SQL

if [ "$initialization_ready" != "t" ]; then
  echo "PostgreSQL initialization contract is incomplete" >&2; exit 1
fi
echo "tcp-and-initialization-ready"
```

**判据性质**：验的是 init 脚本的**产出物**（两库归属 + 三角色及其权限属性），而非端口可达。

### 2.7 `scripts/verify_clean_start.sh`（第二轮修复产物）

POSIX sh（`#!/bin/sh`），默认 3 轮，**不依赖 jq / python / bash**。

```sh
attempts="${1:-3}"
evidence_directory="${EVIDENCE_DIRECTORY:-reports/clean-start}"

while [ "$attempt" -le "$attempts" ]; do
  docker compose down --volumes --remove-orphans     # 每轮清空卷，强制走 initdb
  started_at="$(date +%s)"
  docker compose up --detach --wait                  # 输出存证据文件

  docker events --since ... --until ... --filter type=container \
    --format '{{.TimeNano}}|{{.Action}}|{{.Actor.ID}}|{{index .Actor.Attributes "com.docker.compose.service"}}'

  # shell read 逐行解析，取首个 health_status: healthy 与 migrate 的 start
  db_health_status="$(docker inspect --format '{{.State.Health.Status}}' "$db_container")"
  migrate_exit_code="$(docker inspect --format '{{.State.ExitCode}}' "$migrate_container")"
  health_output="$(docker inspect --format '{{range .State.Health.Log}}{{.Output}}{{end}}' "$db_container")"

  # 五类断言
  [ -z "$db_healthy_at" ] || [ -z "$migrate_started_at" ]  → 缺事件即失败
  [ "$db_healthy_at" -gt "$migrate_started_at" ]           → 顺序反了即失败
  [ "$db_health_status" != "healthy" ]                     → 未保持健康即失败
  [ "$migrate_exit_code" != "0" ]                          → 迁移失败即失败
  health_output 不含 "tcp-and-initialization-ready"        → 未证明初始化即失败
done
```

由 `make verify-clean-start` 调用，并已接入 CI 的 `container` job（取代原先的 `docker compose up --wait`），因此成为每次 CI 的回归保护。

---

## 3. 对照检查

### 3.1 对照 Phase 1-A 范围（Issue #5）

| 范围项 | 落地情况 |
|---|---|
| 项目目录结构 | `src/product_pdf_qr/domains/` 下八个域，均为**仅含 docstring 的空骨架** |
| 后端框架初始化 | FastAPI 应用入口、健康检查端点、`errors.py` 统一错误处理骨架 |
| 数据库连接 | `database.py` 连接池；三角色分离 |
| migration 机制 | Alembic（`alembic.ini`、`migrations/env.py`），支持正向与回滚 |
| 初始 schema 创建 | 六张表 + 约束 + 复合外键 + 三个触发器 + 权限矩阵 |
| 基础配置管理 | `config.py`；`.env.example`；`.env` 已在 `.gitignore` |
| Docker 开发环境 | 三阶段 Dockerfile + compose 四服务 |
| 测试框架初始化 | pytest 单元 + 集成，测试库 `product_pdf_qr_test` 与开发库隔离 |
| README 启动说明 | 环境要求、一条命令、迁移三路径、角色边界、不做项 |

**「不包含」清单核查**：产品创建、PDF 上传、二维码生成、扫码访问、业务接口——**八个域目录均为空骨架，未发现实现**。

### 3.2 对照 Issue #5 六条验收标准

| # | 验收标准 | 证据 |
|---|---|---|
| 1 | 新环境可启动 | `make dev`；CI `container` job 中 `curl --fail /health/ready` 通过 |
| 2 | 数据库可初始化 | 三角色 + 两库由 init 脚本创建；healthcheck 以 SQL 验证其存在与属性 |
| 3 | migration 可执行 | README 给出正向 / 回滚 / 空库重建三条命令；`tests/integration/test_initial_schema.py`（281 行）自动验证三路径 |
| 4 | 测试框架可运行 | `make test-unit` / `make test-integration`，CI 两个 job 分别执行 |
| 5 | Docker 环境可启动 | `make verify-clean-start` 三轮空卷验证；CI 断言容器非 root |
| 6 | README 可指导搭建 | `make check-docs` 校验 Markdown 链接 |

### 3.3 对照适用门禁

| 门禁 | 验证命令 | CI 位置 |
|---|---|---|
| **G-04 构建** | `make build-reproducible`：固定 `SOURCE_DATE_EPOCH`，连续两次构建 sdist 与 wheel，比对 wheel SHA-256 与 sdist 解包后逐文件 SHA-256 | `quality` |
| **G-05 类型检查** | `make typecheck`：`uv run mypy` 覆盖 `src` 与 `tests`，报告 `reports/mypy.xml` | `quality` |
| **G-06 代码规范** | `make lint`：`ruff check` + `ruff format --check` | `quality` |
| **G-16 Docker 构建** | 多阶段、digest 固定基础镜像、非 root 断言、镜像内容检查（无 pytest/mypy、无 `.env`/`*.pem`/`*.key`）、trivy CRITICAL+HIGH 零容忍 | `container` |

`docs/quality-gates-v1.md` 中原「具体命令待补」占位**已全部替换为实际命令**，声明未改变通过标准（此声明需 Advisor 核验其是否属实）。

### 3.4 PR 中标注为不适用 / 未触发的门禁

PR 描述列出 12 项，理由如下（原文转述）：

- G-07 单元测试：不适用（本阶段无业务/安全逻辑；**基础设施自测仅证明测试框架可运行**）
- G-08 API 测试：不适用（无业务 API）
- G-09 端到端：不适用（无业务主线）
- G-10 安全与高风险：不适用（Issue #5 明确本阶段只适用四项门禁）
- G-11 文件上传：不适用（未实现上传）
- G-12 随机标识：不适用（未实现标识生成）
- G-13 并发一致性：不适用（未实现业务写入）
- G-14 审计日志：不适用（仅建表，未实现审计写入）
- G-15 备份：未触发（仅建立只读备份角色，无部署/备份演练）
- G-17 公网验收：未触发且禁止触发
- G-18 人工发布批准：未触发（本阶段禁止发布）
- G-19 回滚演练：未触发（**migration 回滚验证不等同于发布回滚演练**）

PR 另注明：依赖漏洞诊断结果**不用于**把 G-10 标记通过。

---

## 4. 四项明确结论

### 4.1 是否存在业务功能提前实现

**未发现。**

依据：
- 八个域目录（`public` / `product` / `version` / `storage` / `importer` / `qrcode` / `auth` / `audit`）的 `__init__.py` **仅含一行 docstring**，无任何实现
- 源码仅 `main.py`（应用入口 + 健康检查）、`config.py`、`database.py`、`errors.py`、`__main__.py`、一个 `base.html` 模板
- 无产品创建、PDF 上传、二维码生成、扫码访问、认证的任何端点或逻辑

**需 Advisor 判断的边界问题**：迁移创建了**全部六张业务表**（含 `audit_events`、`admin_sessions`）。Issue #5 已明确把「初始 schema 创建」列入范围，并声明「建表属 schema 定义，不属于业务功能实现」。**该边界划分是否成立，请独立判断**——尤其 `audit_events` 与 `admin_sessions` 分别服务于后续阶段的审计与认证功能。

### 4.2 是否修改 requirements

**未修改。**

- `docs/requirements-v1.md`：blob 哈希 `90c15926a4e781a478ddf479a53f92e21d18e6a5`，与 `main` **完全一致**（字节级未变）
- `docs/requirements-v2.md`：`git diff origin/main..origin/feat/phase1a-bootstrap` **为空**

**但本 PR 确实改动了三份其他文档，须一并审查**：

| 文件 | 改动 | 说明 |
|---|---|---|
| `CLAUDE.md` | 1 行 | 原文「只有文档，没有功能代码……技术栈尚未选择」在 Phase 1-A 后失效，改为描述当前状态，并补入「Phase 1-A 不含认证和业务逻辑，产物禁止对外暴露或部署到公网可达环境」 |
| `docs/quality-gates-v1.md` | 命令占位替换 | G-04/05/06 的「具体命令待补」替换为实际命令；开头「当前技术栈未定」改为「技术栈已确定」 |
| `docs/delivery-status.md` | 状态更新 | Phase 1-A 进展 |

**需 Advisor 判断**：`CLAUDE.md` 是治理文件而非 requirements，Worker 修改它是否越界？留着一句已失效的描述是否风险更大？

### 4.3 是否存在公网暴露风险

这是本 PR **唯一已知的、未被修复的争议点**，两轮 Reviewer 意见均未涉及它。

**事实：**

| 层 | 配置 | 效果 |
|---|---|---|
| Dockerfile runtime | `ENV APP_BIND_HOST=127.0.0.1` | 镜像默认值安全 |
| `config.py` | `app_bind_host: str = "127.0.0.1"` | 直接运行时默认安全 |
| **compose.yaml app 服务** | **`APP_BIND_HOST: "0.0.0.0"`** | **容器内监听所有接口** |
| compose.yaml 端口发布 | `"127.0.0.1:${APP_PORT:-8000}:8000"` | 宿主机仅回环可达 |
| compose.yaml db 端口 | `"127.0.0.1:${POSTGRES_HOST_PORT:-5432}:5432"` | 宿主机仅回环可达 |
| README | `> [!CAUTION]` 首行警示 + 绑定说明 | 已显著标注 |

**技术必然性**：Docker 端口发布的目标是容器网络命名空间。若应用只绑容器内 `127.0.0.1`，宿主机端口映射与 Docker 健康检查均不可达。因此在使用端口发布的前提下，容器内绑定 `0.0.0.0` 是**无法避免**的。Worker 在 README 中明确记录了这一取舍，未隐瞒。

**残余风险**：当前保护完全落在 compose 的**一行端口映射**上。若有人把 `"127.0.0.1:8000:8000"` 改为 `"8000:8000"`，服务立即对所有网络接口暴露，且**没有第二道防线**——因为应用本身绑的是 `0.0.0.0`。若应用绑容器内 localhost，改端口映射不会造成暴露（而是直接不通，故障明显）。

**已有的缓解**：Phase 1-A 明令不可部署；仓库中无面向公网的部署配置（无生产 compose、无反向代理配置、无 TLS 挂载）；README 首行 CAUTION；`.env.example` 中的凭据标注为仅本机合成环境使用。

**请 Advisor 判断**：在「Phase 1-A 无认证、仅本机与 CI 运行、明令不可部署」的前提下，该残余风险是否可接受？是否需要在合并前增加防护（例如在 compose 中显式注释警示、或在 CI 中断言端口映射前缀为 `127.0.0.1`）？

### 4.4 其他供 Advisor 关注的点（Orchestrator 未做结论）

1. `db` 与 `app` 服务设 `restart: unless-stopped`，在开发环境中会随 Docker 守护进程重启而自动拉起，是否符合「本机开发」定位？
2. CI `database` job 的连接串中硬编码了与 `.env.example` 一致的示例口令（`local-runtime-only` 等），数据库为临时容器，是否可接受？
3. `verify_clean_start.sh` 依赖 `docker events` 的 `--since/--until` 时间窗与事件顺序，在高负载 CI 上是否存在事件丢失或乱序的可能？
4. healthcheck 以超级用户身份执行 SQL（`POSTGRES_USER` + `PGPASSWORD`），是否需要降权？
5. 迁移为单文件全量 SQL（251 行 `op.execute` 原生语句），未使用 Alembic 的 ORM 抽象，是否影响后续可维护性与回滚可靠性？

---

## 5. 供审查的完整判断依据清单

若需要以下任一文件的**完整原文**，请在意见中指明，由 Orchestrator 补录到本文件或单独提供：

```
Dockerfile                                    已摘录（近乎全文）
compose.yaml                                  已摘录（全文）
.github/workflows/ci.yml                      已摘录（全文）
README.md                                     部分摘录（前 70 行）
migrations/versions/20260801_0001_initial_schema.py   部分摘录（表结构、约束、触发器、权限）
docker/postgres/healthcheck.sh                已摘录（全文）
docker/postgres/init/01-roles-and-databases.sh 部分摘录（角色与库创建）
scripts/verify_clean_start.sh                 已摘录（核心逻辑）
scripts/verify_reproducible_build.sh          未摘录
src/product_pdf_qr/*.py                       未摘录
tests/integration/test_initial_schema.py      未摘录（281 行）
pyproject.toml / uv.lock                      未摘录
Makefile                                      未摘录
.env.example                                  未摘录
```

---

## 6. 本次审查的定位

按 `docs/advisor-protocol-v1.md`：

- Phase 1-A **不含业务逻辑与公开访问面**，PR 描述据 Issue #5 认为不属于 G-10 高风险合并，**参谋复核非强制**。本次审查为主动请求。
- 参谋须产出**七项完整输出**，缺任一项视为未产出。
- 参谋只读、无执行权与决定权，**意见不构成门禁通过**。
- 若参谋与 Orchestrator 存在未解决冲突，门禁判定为「未通过—冲突未解决」，须转为普通语言说明后交业务负责人裁决。

**Orchestrator 当前立场**（供参谋对照与质疑）：三处硬约束（复合外键、只追加触发器、权限最小化）已正确落地；两轮 Reviewer 问题的修复均实质有效且未削弱验证强度；未发现业务功能越界或 requirements 改动。**唯一保留意见是 4.3 的 `0.0.0.0` 残余风险，我认为技术上不可避免但值得在审核时明示，未自行判定其可接受性。**
