# Product PDF QR

> [!CAUTION]
> **Phase 1-B 仍不含管理员认证。仅允许在本机开发与 CI 的合成数据环境中运行，禁止对外暴露，禁止部署到任何公网可达环境。**

本仓库当前实现 Phase 1-B 最小业务闭环：创建产品、上传并安全校验 PDF、追加当前版本、生成永久二维码，以及通过公开随机地址访问当前 PDF。产品停用态的公开读判定已经实现；认证、启停管理入口、历史恢复、Excel 导入和部署仍属后续阶段。

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
- OpenAPI：<http://127.0.0.1:8000/docs>

Compose 内部的应用进程监听容器网络，以便 Docker 健康检查和端口转发；宿主机端口明确绑定 `127.0.0.1`，不会监听所有主机接口。直接运行应用时的默认绑定同样是 `127.0.0.1`。

## 本地最小闭环

管理写接口在本阶段刻意不含认证，只能在本机合成数据环境使用。`pdf_versions.uploaded_by` 继续遵守既有 schema 的管理员外键，因此手工演示时先插入一条仅用于本地验证的合成管理员，并使用返回的 `id`：

```bash
docker compose exec db psql -U postgres -d product_pdf_qr_dev -c \
  "SET ROLE app_rw; INSERT INTO admins
   (username, password_hash, password_updated_at, created_at)
   VALUES ('phase1b-synthetic', 'authentication-disabled', now(), now())
   RETURNING id;"
```

创建产品（编码会先去首尾空格、校验，再统一转大写）：

```bash
curl -sS -X POST http://127.0.0.1:8000/api/products \
  -H 'Content-Type: application/json' \
  -d '{"code":" a001_1 "}'
```

响应包含产品 `id`、26 位随机 `public_token`、公开 URL 与二维码下载地址。使用产品 `id` 和上一步合成管理员 `id` 上传 PDF：

```bash
curl -sS -X POST http://127.0.0.1:8000/api/products/1/pdf \
  -F actor_id=1 \
  -F file=@./synthetic.pdf
```

下载二维码与公开扫码访问：

```bash
curl -OJ http://127.0.0.1:8000/api/products/1/qrcode
curl -i http://127.0.0.1:8000/p/REPLACE_WITH_PUBLIC_TOKEN
```

二维码为 PNG、1024×1024、纠错等级 H、静区 4 模块，文件名精确等于规范化大写编码。公开入口对不存在、停用、未上传和正常四态统一返回 200，并始终设置 `Cache-Control: no-store`。

只读孤儿文件对账报告：

```bash
curl -sS http://127.0.0.1:8000/api/storage/orphans
```

该报告只列出正式存储中没有 `pdf_files` 引用的文件，不提供删除、清理或定时任务。

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
├── dependencies.py        # Web 层基础设施依赖
├── errors.py              # 统一安全错误处理
├── main.py                # FastAPI 入口与健康检查
├── templates/             # Jinja2 服务端模板
└── domains/
    ├── public/            # 四态公开扫码与 PDF 流
    ├── product/           # 编码、token、创建与本地管理 API
    ├── version/           # 锁内判重、版本追加与当前指针
    ├── storage/           # 隔离校验、内容寻址与只读对账
    ├── importer/
    ├── qrcode/            # 确定性生成、缓存与失败补偿
    ├── auth/
    └── audit/             # 只追加事件写入
```

Phase 1-B 在 Phase 1-A 的六表 schema、复合外键、只追加触发器与权限矩阵上开发，没有新增或修改 schema。初始 schema 位于 `migrations/versions/`；PostgreSQL 角色与开发/测试数据库初始化位于 `docker/postgres/init/`。

## 当前阶段明确不包含

- 管理员认证、登录或会话逻辑
- 停用、启用的管理入口（公开端停用态读判定已实现）
- Excel 导入、批量 ZIP、历史版本界面或恢复入口
- 完整审计查询界面
- 孤儿文件自动清理、历史版本删除或任何不可逆操作
- 公网反向代理、TLS、生产 Compose 或任何部署配置

G-09 在本阶段为「部分覆盖，未判定通过」：仅覆盖新建即出码、未上传、首次上传和新版替换；恢复、停用、启用管理链路等待 Phase 2。在后续阶段补齐认证并通过相应安全门禁前，本仓库产物始终禁止对外暴露。
