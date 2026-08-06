# 生产运行面 PR1

本文档说明 Issue #34 交付的生产编排契约。它只用于本地合成验证和后续授权人部署，
不是 G-17 公网验收或 G-18 发布批准，也不授权 Agent 接触真实服务器、DNS、证书或数据。

## 固定拓扑与隔离矩阵

生产 Compose 恰有 `proxy`、`certbot`、`app`、`db`、`migrate` 五个服务。
只有 `proxy` 发布宿主机 80/443；应用的 8000 和数据库的 5432 只存在于容器网络。

| 服务 | `frontend` | `database`（internal） | `acme_egress` | 业务卷 | 数据库卷 | 证书卷 | challenge 卷 |
|---|---:|---:|---:|---:|---:|---:|---:|
| proxy | 172.30.0.10 | — | — | — | — | 只读 | 只读 |
| app | 172.30.0.20 | 172.31.0.20 | — | 读写 | — | — | — |
| db | — | 172.31.0.10 | — | — | 读写 | — | — |
| migrate | — | 172.31.0.30 | — | — | — | — | — |
| certbot | — | — | 172.32.0.10 | — | — | 读写 | 读写 |

`app` 仅绑定 172.30.0.20:8000，因此即使同属数据库网的进程知道 app 容器地址，
也不能通过数据库网接口绕过 Nginx。Uvicorn 只信任固定 proxy 地址
172.30.0.10，禁止通配符可信代理。`frontend` 只有 proxy/app 两个成员。

稳定标识如下，PR2 可直接消费：

- 服务：`app`、`db`
- 网络：`product_pdf_qr_frontend`、`product_pdf_qr_database`
- 业务卷：`product_pdf_qr_files`
- 数据库卷：`product_pdf_qr_postgres`
- readiness：app 容器内 `http://172.30.0.20:8000/health/ready`

所有服务都禁用 privileged、host network、host PID/IPC、设备、Docker socket、
host-gateway 和 capability 增加，并启用 `cap_drop: ALL`、只读根文件系统及
`no-new-privileges`。生产允许的 bind mount 白名单只有：

- Nginx 主配置与模板，只读；
- PostgreSQL 初始化目录与 healthcheck，只读。

它们均为仓库内版本化运行配置，不是业务目录。其他宿主机目录一律禁止。

数据库最终镜像由锁定的官方 PostgreSQL 16.14 Alpine digest 构建；因为服务固定以
UID/GID 70 启动，官方 entrypoint 的 root-only `gosu` 分支不可达，最终阶段删除该
辅助二进制及其独立 Go runtime。除此之外不替换 entrypoint 或 PostgreSQL 内容。
proxy 最终镜像由锁定的 Nginx unprivileged 1.29.4 digest 构建并在构建期应用 Alpine
安全更新；certbot 5.1.0 安装在与 app 相同的锁定 Python 3.12/Alpine 基座。四个
自建 target 均执行双构建内容标识比较，生产节点不执行包升级。

## G-02 显式工程常量

这些值在配置和测试中均为字面量；缺失或改为 Nginx 默认值即验收失败。依据为
`security-design-v1.md` 的既有安全边界、应用现有 50/10 MiB 上限，以及
Nginx 官方超时、缓冲、限流语义下的标准单实例实践。

| 类别 | 固定值 | 依据与作用域 |
|---|---|---|
| HSTS | `max-age=31536000; includeSubDomains`，不含 preload | HTTPS 所有状态响应；一年是稳定生产基线，避免未完成 preload 运营承诺 |
| 扫码粗限流 | 每真实客户端 IP `60r/m`、`burst=120`、`nodelay` | 复用安全设计值；429 的 `Retry-After=1` |
| 登录粗限流 | 每真实客户端 IP `30r/m`、`burst=60`、`nodelay` | 请求级 DoS 上限；显著宽于应用 20 次失败规则；429 的 `Retry-After=2` |
| 应用登录精确规则 | 单 IP 15 分钟 20 次失败；账号指数退避 | Nginx 不判断成功/失败，不替代或遮蔽应用规则 |
| header/body timeout | 10s / 15s | 限制慢速请求占用 |
| upstream connect/send/read | 5s / 60s / 60s | 连接快速失败；覆盖 30s 导入解析与 PDF 校验余量 |
| downstream send/keepalive | 60s / 30s，100 请求 | 有界慢客户端与连接复用 |
| 请求缓冲 | 128 KiB 内存；client body 临时 tmpfs 128 MiB | 不依赖默认值；临时磁盘硬边界 |
| 响应缓冲/缓存 | `proxy_buffering off`、`proxy_max_temp_file_size 0`、`proxy_cache off` | PDF 不产生代理缓存或响应临时文件 |
| PDF route body | 51300 KiB | 50 MiB 文件加 100 KiB multipart envelope；文件精确边界仍由应用验证 |
| Excel route body | 10340 KiB | 10 MiB 文件加 100 KiB multipart envelope；解析前应用流式边界仍生效 |
| 日志轮转 | 24h 或 10 MiB，保留 7 份，总上限 70 MiB/源 | systemd 每 5 分钟检查；当前、轮转及压缩代际都使用 token-safe 格式 |

Nginx access log只记录来源 IP、随机 request ID、时间、方法、状态、响应字节数和
固定 server name，不记录 request line、URI、查询串或 Referer。Uvicorn access log
关闭。Nginx error log固定为 `crit`，避免错误上下文写入公开 token。应用审计只记录
允许的来源 IP、计数和关联字段，不记录完整 token。

日志文件固定为服务 UID 所有的 `0600`，目录使用对应非 root UID；脚本以
`umask 077` 轮转。文件不可写时
Nginx 或 app 启动失败，而不是静默丢日志。`rotate-logs.sh` 同时执行份数和总量上限。

## 请求与 TLS 契约

- 80 端口只有正式 Host 的合法
  `/.well-known/acme-challenge/<token>` GET 是静态例外；其他正式 Host 请求 308
  到同域 HTTPS，未知 Host 固定拒绝。
- 443 只接受正式 SNI/Host；未知 SNI 在证书交换前拒绝。
- proxy 用 `$remote_addr` 覆盖 XFF，清除 `Forwarded`/`X-Real-IP`/Referer，固定
  Host、`X-Forwarded-Proto=https` 和随机 request ID。
- `/health/ready` 及安全变体在 proxy 返回 404；容器内 readiness 保持 200/503。
- 所有业务响应强制 `no-store`；Nginx 不挂业务卷，也没有指向业务卷的
  root、alias 或 try_files。

## 启动前校验与密钥边界

从 `.env.prod.example` 复制 `.env.prod`，替换全部占位值后执行：

```sh
chmod 0600 .env.prod
scripts/production/prod-compose.sh up --detach --wait
```

包装器拒绝 symlink、非当前用户所有或不是 0600 的环境文件，先以
`docker compose config --format json` 通过管道送入非回显结构验证器，实际镜像、
拓扑和固定安全值不合规时在拉取/运行任何生产镜像前失败；随后用最终 app 镜像运行
生产 Settings 预检。生产模式、app 前端监听地址和 proxy 信任地址在生产 Compose
中硬编码，不接受 `.env.prod` 覆盖。生产模式要求：

- `PUBLIC_BASE_URL` 精确等于 `https://PUBLIC_DOMAIN`；
- 域名小写、无末尾点、不是 IP/localhost/占位域；
- 无用户信息、端口、路径、尾斜杠、查询、fragment、空白或控制字符；
- 固定 Uvicorn proxy 地址与固定 app bind 地址。

机械 Compose 展开只准使用合成值并直接通过管道送入断言，不保存正文：

```sh
make prod-config-check
make prod-env-check
```

生产环境禁止保存、上传、粘贴或附在 PR/聊天中的真实 `docker compose config`
输出。`.env.prod` 已被 Git 与 Docker build context 排除，不得进入镜像。

首次 `up` 时包装器先只启动隔离的 certbot，检查证书卷中的 active 证书；若卷为空或
证书对不完整，会自动调用 `bootstrap-certificate.sh` 生成两天有效的合成启动证书，
再启动完整栈。因此全新卷的上述 `up --wait` 命令可重复执行。该证书只用于建立
HTTP challenge 所需的冷启动链路，必须在任何公网开放或 G-17 前由授权人换成验证
通过的正式证书。

## ACME、续期与回滚

证书控制链不向任何容器挂 Docker socket：

1. `prod-compose.sh up/start/restart` 在 active 证书缺失时自动调用
   `bootstrap-certificate.sh`，后者只生成两天有效的本地/启动临时证书；
2. 授权人启动正式 Host 的 HTTP challenge 路由后运行 `issue-certificate.sh`；
3. certbot 只通过独立出站网写 certificate/challenge 卷；
4. 新证书必须通过有效期、主机名、公私钥匹配检查，才原子切换 active 目录；
5. 宿主机固定脚本先执行 `nginx -t`，再只对 `proxy` 发优雅 reload；
6. 任一步失败均恢复旧 active 证书、禁止 reload，并向固定 webhook 发送一次不含
   域名、凭据、私钥或 token 的通用告警。

`product-pdf-qr-cert-renew.timer` 每日两次运行上述固定链。真实启用 timer、真实
DNS/ACME、首张正式证书和 G-17/G-18 全部留给 PR3 授权人。

本地端到端 ACME 使用 `compose.prod.test.yaml` 的 Pebble 与 challtestsrv。该 overlay
把 ACME 网改为 internal，所有域名、CA、证书和告警均为一次性合成数据；生产
Compose 单独展开时不含 Pebble、challtestsrv、alert sink 或测试网络。只有检测到
Pebble CA 时，`renew-and-reload.sh --force-synthetic-renewal` 才允许强制续期；
相同参数对任何非 Pebble CA 都会在续期前拒绝。

## PR2 与 PR3 交接

PR2 可以只停止 `app`，等待 readiness 消失，此时 `db` 继续运行；受限临时备份容器
可只读挂业务卷并仅加入内部数据库网，用既有 `app_backup` 角色执行 `pg_dump`，
随后启动同一 app 服务并等待 readiness。PR1 不提供备份容器或维护模式。

PR3 必须由授权人在 IPv4/IPv6 同时验证：公网仅 80/443，8000/5432/Docker API
不可达，SSH 仅授权来源或 VPN，未知 Host/SNI 不到达应用，真实证书链和域名正确。
本地结果不得表述为这些检查或 G-17/G-18 已通过。

## G-16 与 A19 命令基线

应用、加固数据库、proxy 与 certbot 镜像都从锁定 Dockerfile target 构建两次并
比较内容标识，再发布到受控 registry，将脚本输出的 `APP_IMAGE`、`DB_IMAGE`、
`PROXY_IMAGE`、`CERTBOT_IMAGE` 完整 `tag@sha256` 引用写入 `.env.prod`。生产节点
只拉取不可变引用，不现场构建。

```sh
scripts/production/prepare-local-image.sh
```

```sh
make lint
make typecheck
make test-unit
make test-integration
make prod-env-check
make prod-config-check
docker compose --env-file .env.prod -f compose.prod.yaml pull
scripts/production/prod-compose.sh up --detach --wait
scripts/production/prod-compose.sh exec --no-TTY app \
  python -c "import urllib.request; urllib.request.urlopen('http://172.30.0.20:8000/health/ready', timeout=2)"
git diff --check
```

G-10 继续执行仓库规定的 Bandit、冻结 runtime requirements、pip-audit strict 和
gitleaks v8.24.3。G-16 对 app/migrate/db/proxy/certbot 每个最终镜像逐一执行：
非 root UID、实际 capabilities/NoNewPrivileges/只读根文件系统/挂载负向探测、
镜像内容检查、health/command smoke，以及 Trivy 0.72.0
`--ignore-unfixed=false --severity CRITICAL,HIGH --exit-code 1`。完整命令、环境、
提交 SHA、退出码和脱敏证据路径记录到 PR，不保存 Compose 展开正文。
