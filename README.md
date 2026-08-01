# Product PDF QR

> [!CAUTION]
> **Phase 1-A 不含任何管理员认证或业务接口。仅允许在本机开发与 CI 的合成数据环境中运行，禁止对外暴露，禁止部署到任何公网可达环境。**

本仓库当前只提供产品 PDF 二维码系统的工程基础：Python 3.12 + FastAPI、PostgreSQL 16、服务端渲染模板骨架、数据库迁移、隔离测试环境与 Docker 开发环境。产品创建、PDF 上传、二维码生成、扫码访问、管理员认证等业务能力均尚未实现。

## 环境要求

- Docker Engine 24+（含 Docker Compose v2）
- GNU Make
- 本地开发数据必须为合成数据；不得连接正式数据库或放入真实业务文件

无需在宿主机安装 Python 或 PostgreSQL。

## 一条命令启动

```bash
make dev
```

首次执行会从 `.env.example` 复制一份仅供本机使用的 `.env`，随后构建镜像、启动 PostgreSQL、执行迁移并启动应用。示例凭据只用于本机合成数据环境，不得复用。

服务只通过宿主机回环地址开放：

- 存活检查：<http://127.0.0.1:8000/health/live>
- 就绪检查：<http://127.0.0.1:8000/health/ready>

Compose 内部的应用进程监听容器网络，以便 Docker 健康检查和端口转发；宿主机端口明确绑定 `127.0.0.1`，不会监听所有主机接口。直接运行应用时的默认绑定同样是 `127.0.0.1`。

停止环境：

```bash
make down
```

若需要连同本地合成数据库和文件卷一起清空，可明确执行 `docker compose down --volumes`。该命令会删除本项目的本地 Docker 卷。

## 数据库与迁移

数据库连接职责严格分离：

| 角色 | 用途 | 权限边界 |
|---|---|---|
| `app_rw` | 应用运行时 | 最小表级与列级读写权限，无 DDL |
| `app_migrate` | Alembic 迁移 | schema 所有者，仅迁移时使用 |
| `app_backup` | 备份 | 全库只读 |

迁移容器只接收 `MIGRATION_DATABASE_URL`，应用容器只接收 `DATABASE_URL`，只读备份连接单独保存在 `BACKUP_DATABASE_URL`。不要把迁移或备份账号配置给应用。

正向迁移：

```bash
docker compose run --rm migrate alembic upgrade head
```

回滚到空 schema：

```bash
docker compose run --rm migrate alembic downgrade base
```

再次从空 schema 重建：

```bash
docker compose run --rm migrate alembic upgrade head
```

以下命令在独立的 `product_pdf_qr_test` 测试库中自动验证正向、回滚、删除 schema 后空库重建三条路径，同时验证复合外键、触发器和权限矩阵：

```bash
docker compose --profile test run --rm test make test-integration
```

测试库和开发库分别为 `product_pdf_qr_test` 与 `product_pdf_qr_dev`，初始化脚本会创建两个独立数据库。测试命令拒绝在缺少显式测试连接配置时运行。

## 开发检查

完整测试（含隔离 PostgreSQL 集成测试）：

```bash
docker compose --profile test run --rm test
```

从空卷连续验证三次完整启动顺序：

```bash
make verify-clean-start
```

该验证命令会删除并重建本项目的本地数据库与文件卷，每次确认数据库初始化健康事件早于 migration 容器启动，并将带时间戳的证据保存到 `reports/clean-start/`。

本机已安装 Python 3.12 与 [uv](https://docs.astral.sh/uv/) 时，也可运行：

```bash
uv sync --frozen --all-groups
make build-reproducible
make typecheck
make lint
make test-unit
make check-docs
```

依赖由 `pyproject.toml` 声明并由 `uv.lock` 锁定。禁止不更新锁文件地修改依赖。

## 工程结构

```text
src/product_pdf_qr/
├── config.py              # 集中配置
├── database.py            # app_rw 连接池
├── errors.py              # 统一错误处理骨架
├── main.py                # FastAPI 入口与健康检查
├── templates/             # Jinja2 服务端模板骨架
└── domains/
    ├── public/
    ├── product/
    ├── version/
    ├── storage/
    ├── importer/
    ├── qrcode/
    ├── auth/
    └── audit/
```

八个域目前只有模块边界，没有任何业务实现。初始 schema 位于 `migrations/versions/`；PostgreSQL 角色与开发/测试数据库初始化位于 `docker/postgres/init/`。

## 当前阶段明确不包含

- 产品创建或任何业务表读写逻辑
- PDF 上传、校验或文件移动
- 二维码生成或下载
- 公开扫码访问
- 管理员认证、登录或会话逻辑
- Excel 导入、批量 ZIP、历史恢复、启停或审计写入逻辑
- 公网反向代理、TLS、生产 Compose 或任何部署配置

在后续阶段补齐认证并通过相应安全门禁前，本仓库产物始终禁止对外暴露。
