# Product PDF QR

> [!CAUTION]
> **V1 已实现单管理员认证，但仍未提供生产反向代理、TLS、密钥管理或部署配置。仅允许在本机开发与 CI 的合成数据环境中运行，未经完整发布门禁不得对外暴露。**

本仓库当前实现 V1 最小业务闭环：管理员登录、创建与搜索产品、上传并安全校验 PDF、追加当前版本、生成永久二维码，以及通过公开随机地址访问当前 PDF。产品停用态的公开读判定已经实现；启停管理入口、历史恢复、Excel 导入和生产部署仍属后续阶段。

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

首次启动后，通过受控 CLI 创建唯一管理员。密码由终端无回显交互读取并要求再次确认，不能作为命令行参数传入：

```bash
docker compose run --rm app \
  python -m product_pdf_qr.cli create-admin --username <用户名>
```

自动化环境可从 stdin 读取一行密码，仍不得把密码写进命令参数：

```bash
docker compose run --rm -T app \
  python -m product_pdf_qr.cli create-admin \
  --username <用户名> --password-stdin < /受保护路径/密码文件
```

遗忘密码时由有服务器访问权限的负责人执行重置；该操作会吊销全部现有会话，并在下次登录时重新强制改密：

```bash
docker compose run --rm app \
  python -m product_pdf_qr.cli reset-password --username <用户名>
```

随后在浏览器打开 <http://127.0.0.1:8000/admin>：

1. 使用 CLI 创建的用户名和临时密码登录；
2. 首次登录按页面要求设置不同的新密码；
3. 使用“产品编码 + 产品名称”创建产品；编码会去首尾空格并统一为大写；
4. 在产品详情页上传合成 PDF，并检查二维码和公开地址；
5. 返回列表可按编码/名称搜索，并按“已上传 / 未上传”筛选。

管理页面与 `/api/**` 均要求有效会话。产品创建和 PDF 上传的操作者 ID 来自当前会话，客户端不再传递 `actor_id`。所有状态变更请求还必须携带会话绑定的 CSRF 令牌；管理页面会自动为表单和原生 `fetch` 请求添加，不能用未认证的裸 `curl` 调用替代。

上传入口在 multipart 解析前限制整个请求体，PDF 本体在隔离区再次执行 50 MB 精确限制。结构解析只在可终止子进程中运行，默认墙钟 5 秒、CPU 3 秒、地址空间 512 MiB；地址空间约为最大文件的十倍，为正常解析保留余量，同时限制紧凑恶意文件的放大效应。以上值由 `MAX_PDF_BYTES`、`PDF_VALIDATION_TIMEOUT_SECONDS`、`PDF_VALIDATION_CPU_SECONDS` 和 `PDF_VALIDATION_MEMORY_BYTES` 集中配置。

公开扫码地址 `/p/{public_token}` 不要求登录。取得产品详情页显示的公开 URL 后可直接验证：

```bash
curl -i http://127.0.0.1:8000/p/REPLACE_WITH_PUBLIC_TOKEN
```

二维码为 PNG、1024×1024、纠错等级 H、静区 4 模块，文件名精确等于规范化大写编码。公开入口对不存在、停用、未上传和正常四态统一返回 200，并始终设置 `Cache-Control: no-store`。

认证后的只读端点 `/api/storage/orphans` 只列出正式存储中没有 `pdf_files` 引用的文件，不提供删除、清理或定时任务。

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

V1 沿用 Phase 1-A 的六表 schema、复合外键、只追加触发器与权限矩阵，并通过后续 migration 为产品增加可空的 `name` 字段以兼容历史数据。初始 schema 与后续 migration 位于 `migrations/versions/`；PostgreSQL 角色与开发/测试数据库初始化位于 `docker/postgres/init/`。

## 当前阶段明确不包含

- 用户注册、多管理员、角色权限、网页/邮箱密码找回
- 停用、启用的管理入口（公开端停用态读判定已实现）
- Excel 导入、批量 ZIP、历史版本界面或恢复入口
- 完整审计查询界面
- 孤儿文件自动清理、历史版本删除或任何不可逆操作
- 公网反向代理、TLS、生产 Compose 或任何部署配置

G-09 在本阶段为「部分覆盖，未判定通过」：仅覆盖新建即出码、未上传、首次上传和新版替换；恢复、停用、启用管理链路等待 Phase 2。在通过完整发布与安全门禁前，本仓库产物始终禁止对外暴露。
