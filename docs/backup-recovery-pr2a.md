# 一致备份与隔离恢复 PR2A

本文是 Issue #36 的操作契约与本地合成演练入口。它不授权访问真实服务器、业务数据、
真实对象存储或真实密钥；真实异地配置、密钥托管、正式恢复与最终 G-17/G-18 均由授权人
执行。本实现不含发布回滚或迁移 downgrade（G-19 属于 PR2B），也不改变
`compose.prod.yaml` 的五服务、端口、稳定卷名或 proxy/db 隔离。

机器可读的唯一实现参数表是
[`deploy/backup/contract.json`](../deploy/backup/contract.json)。程序在任何字段缺失、
留空或偏离安全值时 fail closed。

## 已落定参数与依据

| 项目 | 值 | 依据 |
|---|---|---|
| 业务时区 | `Asia/Shanghai`，同时记录 UTC | 业务与维护责任方位于中国；避免宿主默认时区漂移 |
| 一致封口 | 每天 `02:30` | Issue 已批准每天凌晨一次，避免白天停服 |
| 白天预复制 | `06:30/10:30/14:30/18:30/22:30` | 4 小时粒度限制夜间日增量；只读、不停 app |
| 漏跑 | finalizer 不补跑，只告警并等下一窗口；precopy 启动后补一次 | 防止白天重启自动停扫码 |
| 周/月代际 | 周一开始；周一 02:30 后首个点；每月 1 日 02:30 后首个点 | 选择确定、时区确定、可测边界 |
| 加密 | `age 1.3.1`，age-v1 X25519 recipient，HKDF-SHA-256 + ChaCha20-Poly1305 | 固定版本的认证加密；生产只需公钥 |
| 清单来源认证 | 规范 JSON 的 Ed25519 签名，独立 32-byte 私钥/公钥与 key ID | age recipient 公钥可被 upload 身份取得，不能单独证明发送方；restore/retention 只持公钥，无法签发伪造清单 |
| 上传 | `rclone 1.74.1` 的 S3 接口 | 固定客户端；生产类别为 S3 兼容对象存储 |
| 数据库工具 | PostgreSQL/`pg_dump`/`pg_restore` 16.14 | 与 PR1 PostgreSQL 16 主版本相同 |
| 异地故障域 | 不同主机、不同账号、不同区域、不同存储生命周期 | 生产示例 `ap-east-1`，备份示例 `ap-southeast-1` |
| 保留 | 日 14 天、周 8 周、月 6 月 | Issue 裁决；重叠代际须全部过期才可删除 |
| 停服/RTO | `≤900s` / `≤14400s` | Issue 裁决 |

`age` 的公钥不是秘密，可进入独立的 `.env.backup`；私钥始终由离线授权保管人持有，
仅恢复时以只读 mode-0600 文件短暂挂入 restore 容器的 tmpfs 工作域。`.env.prod`
没有任何 PR2A 密钥、异地身份或恢复身份。manifest signing private key 是独立的
32-byte mode-0600 文件：生产 backup 容器仅以只读文件获得签发权限，upload rclone
身份拿不到它；restore 与删除授权环境只持对应的 32-byte 公钥，密码学上没有签发能力。
成功恢复证明使用另一对 Ed25519 密钥：私钥仅挂入一次性 restore job，生产
backup/upload 身份没有该权限；独立保留环境只持公钥。因此 upload 身份创建的
`verified/*.json` 文件名或内容不能改变唯一受保护恢复点。
路径/key ID 可写入 `.env.backup`，私钥内容不得进入 env、argv、镜像、仓库或远端。

## 代表性容量基线

基线固定为 5,000 产品、1,000,000 数据库行、5 GiB dump、20,000 个当前/历史文件、
250 GiB 文件总量；日增量 500 文件/5 GiB；有效异地带宽 500 Mbit/s、RTT 50 ms，
所有估算保留 25% 安全余量。升级阈值为日增量 1,000 文件或 10 GiB、数据库
2,000,000 行、预计 finalizer 720 秒或预计恢复 11,520 秒。

触及任一阈值时先告警和 fail closed，不允许等实际超过 15 分钟才发现。250 GiB 在
500 Mbit/s 的纯传输理论值约 1.2 小时；加入下载、授权等待、解密、现场留存、数据库、
文件、离线校验、隔离功能校验和 25% 余量后仍须在本地/候选容量演练中实测
`≤4h`。本地结果只能证明实现与估算方法，不能宣称生产 RTO/RPO 已通过。

## 逻辑 bundle 与原子成功

bundle 是逻辑恢复点，不是一个必须重新打包数百 GiB 的单文件：

1. 白天容器只读遍历文件卷，对每个常规文件重新计算 SHA-256。
2. 对不存在的内容，先在 mode-0700 状态卷生成一份持久、仅含密文的 CAS publication
   checkpoint，再以同一密文字节发布
   `objects/sha256/<plaintext-sha256>/<recipient-key-id>.age` 和认证 metadata。
   任一不可变对象发布后中断时，重试验证检查点与已存在对象并只补缺失一侧；确认两侧
   都存在后才清理检查点。名称、大小和 mtime 从不作为相等判据；symlink 和非常规对象
   被拒绝。
3. 凌晨 app 完全 stopped 后重新生成**完整**源清单，按内容摘要补齐/纠正对象。
   文件清单冻结完成后才执行 `pg_dump --format=custom --no-owner`；ACL/default ACL
   一并纳入，以便恢复 `app_rw/app_backup` 权限矩阵。
4. dump 与版本化配置 tar 从生产源直接进入 age 管道，不生成完整明文 dump/tar。
   所有持久 staging 只允许密文；tmpfs 单分块上限 64 MiB。
5. 清单包含唯一 `backup_id`、开始/冻结/完成时间、commit、全部镜像 digest、
   Compose/Nginx/config SHA-256、Alembic revision、数据库及工具版本、稳定卷/库名、
   recipient/key ID、数据库/配置/所有当前和历史文件对象的明文与密文 SHA-256；备份
   生成端对不含 authentication 字段的规范 JSON 计算 Ed25519 签名，再把 key ID/
   signature 加入清单并整体 age 加密。
6. 每个对象上传后重新读取并核对远端大小和密文 SHA-256。最后才写不可变
   `complete/<backup_id>.json`；这是恢复候选的唯一入口。代表性 5 GiB dump 的远端
   复核使用 1 MiB 分块流式摘要，不把 `rclone cat` 全量捕获到内存；控制对象读取另有
   8 MiB 硬上限。

预复制、部分上传、缺完成标记、仅上传未核验或失败 finalizer 都不是成功点，不能更新
`last-success.json`、不能进入恢复/轮转候选，也不能移动 RPO 时间。RPO 只从最近一个
成功完成“加密 + 传输 + 远端大小/密文摘要核验”的点之 `frozen_at` 计算。

## 容器与权限边界

[`compose.backup.yaml`](../compose.backup.yaml) 是显式 overlay：

- `backup` 与 `restore` 都有 profile，普通 PR1 `up` 不会启动；
- 同一个新增最终镜像锁定 Python Alpine digest 和 age/rclone/PG16 包版本；
- UID/GID `10002:10002`、只读根、非 privileged、`cap_drop: ALL`、
  `no-new-privileges`，没有 host network/PID/IPC/device/Docker socket/宿主端口；
- 镜像把 `/var/lib/backup` 预置为 `10002:10002`、mode 0700，并放入 owner marker；
  Docker 首次挂载空 `backup_state` named volume 时通过 copy-up 保留此所有权，不依赖
  root-owned 空卷的默认权限；
- PR1 文件由 UID 10001 在 umask 077 下创建。首次启用前，必须运行无网络、无 secret、
  非 root UID 10001、`cap_drop: ALL` 的 `backup-file-access-init` profile，为现存目录/
  文件赋予 UID 10002 named ACL，并为所有目录设置 UID 10001/10002 双向默认继承 ACL；
  app 的 PDF upload temp 全程保持 `mkstemp(0600)` 且 `temporary/` 被备份清单排除，
  只有验证成功并正式原子发布前才恢复 group-class ACL mask；二维码 cache 同样只在
  发布路径恢复 mask，恢复文件原子发布前显式赋予 UID 10001 ACL；named ACL 只让 UID 10002 获得
  所需访问，无关 UID 仍被拒绝，不得用 0777 替代；
- backup 只加入 database 网、只读挂业务卷、只注入 `app_backup` pgpass；
- restore 是默认关闭的一次性能力，写挂业务卷、使用数据库 owner `app_migrate`，
  只在隔离验证期加入 frontend；高权材料仅为一次性只读 secret；
- 两者都不常驻，退出即撤下私钥/授权/数据库密码挂载。

`app_backup` 可以 `pg_dump`，但写、DDL、建库必须失败。恢复不需要超级用户：

- 现有固定栈：PR1 的 `app_migrate` 是数据库和 schema owner；现场留存成功后，以一次性
  owner secret 执行 `pg_restore --clean --if-exists --no-owner
  --role=app_migrate`；
- 新灾备栈：授权人先按 PR1 初始化脚本创建非 superuser 的
  `app_migrate/app_rw/app_backup` 和新库，然后以同一一次性 `app_migrate` 恢复；
- 如果未来 PostgreSQL 变更确实要求超级用户，必须另立安全评审；当前工具和配置拒绝
  将其放入镜像、env、argv、日志或定时服务。

恢复后 ACL/default ACL 仍来自 dump 和 PR1 初始化契约；须复测 `app_backup`
写/DDL/建库失败、`app_rw` 修改/删除既有审计失败。

首次安装（以及确认文件树全部仍由 UID 10001 所有后）先执行一次：

```shell
docker compose --env-file .env.prod --env-file .env.backup \
  -f compose.prod.yaml -f compose.backup.yaml \
  --profile backup-volume-init run --rm backup-file-access-init
```

该命令只修改稳定 `file_data` 卷的 POSIX ACL，不改变 owner、不连接网络、不接触数据库/
异地/密钥。演练先以 UID 10001 和 umask 077 在真实 named volume 创建 0700/0600 对象，
再证明初始化前提、默认 ACL 继承、UID 10002 读写和无关 UID 拒绝；状态卷另以真实空
named volume 验证 copy-up 后为 `10002:10002:0700` 且 UID 10002 可写。

G-16 镜像证据使用 `make backup-image-reproducible` 从零构建两个 OCI archive；
`SOURCE_DATE_EPOCH=1754006400` 与 `rewrite-timestamp=true` 固定 layer 元数据后比较完整
archive SHA-256。`make backup-image-scan` 使用 digest 锁定的 Trivy 0.72.0，以
`ignore-unfixed=false` 扫描 CRITICAL/HIGH 并在任一发现时非零退出。

## 调度、互斥和失败语义

systemd timer 固定写出 `Asia/Shanghai`。precopy timer 为 `Persistent=true`，只补读操作；
finalizer 明确 `Persistent=false`，所以白天重启不会停 app。所有 precopy/finalizer/
restore/授权轮转共享一个 owner lease 锁。活 owner 返回退出码 75；死 owner不被自动删除，
须授权人核对具体 PID/作业状态后只移除确切锁目录，避免误杀仍在运行的任务。

finalizer 的宿主控制顺序：

1. 安装 cleanup trap，并在发出 stop 前设置 `app_needs_recovery=1`；再停稳定 `app`，
   等待 Docker state 为 `Running=false`；
2. 确认 migrate 不运行，database 网络只有 db 和当前命名的一次性 backup job；
3. 以 `app_backup` 查询 `pg_stat_activity`，拒绝 `app_rw/app_migrate` 会话、写事务；
4. 文件封口期间每个对象前再次查询数据库静默性；
5. dump、配置与加密清单成功，远端完整核验并原子发布；
6. 正常路径显式启动同一稳定 app 并等待 health；cleanup trap 在 stop 部分成功但返回
   非零、普通失败或信号中断时执行同一恢复；
7. 从 stop 发出前到 app 重新 healthy 共用同一个 900 秒绝对 deadline；13 分钟 work
   budget 为重启预留时间，最终窗口只在 app healthy 后结算；
8. app 无法恢复或超过 deadline 时不掩盖，发出带关联事件的 critical 告警。

实现的备份故障注入点固定为 `files/dump/manifest/encryption/upload`；恢复点为
`decrypt/preflight/site_retention/database/files/offline_validation/
isolated_functional_validation/proxy`。备份任一点失败不得创建 complete，上一成功点及
时间不变。恢复任一点失败不越过 checkpoint；proxy 立即/持续 stopped，app 在文件和
离线校验完成前不能启动。`database_restored/files_incomplete` 明确可识别，重新执行
files 阶段按内容摘要幂等续跑，不会删除备份点之后的追加文件。

## 同类失败模式系统审计

本轮对生产/备份/恢复运行路径与本地隔离演练做了五类全量搜索；AO 治理工具按任务纪律
只审不改：

- **持久临时发布**：checkpoint `atomic_write` 与 synthetic `LocalRemote` 均改用
  `mkstemp` 随机排他名，不依赖 PID；先 fsync 文件，`replace` 后再 fsync 父目录。
  固定 `.NAME.restore` 在共享恢复锁下只删除常规残留，非常规对象 fail closed；现场
  retention 的 operation staging 在无 checkpoint 时安全清理重建；下载 cache 会对
  已存在对象重算 size/SHA-256，损坏常规文件删除重下。PDF upload、QR cache 使用随机
  `mkstemp`，崩溃残留不与后续运行同名；一次性构建/演练目录使用 `mktemp -d` 随机名和
  cleanup trap。PDF upload temp 始终私有且整个 `temporary/` namespace 不进入预复制、
  最终备份或现场留存清单。文件 CAS 的持久密文 checkpoint 能从 age-only 或
  ciphertext-checkpoint-only 中断续跑；随机加密/atomic-write 残留按受控模式清理。
  生产 S3 路径 completion-last。
- **停服与计时**：唯一日常停服窗口是 `backup-run.sh finalize`，同一个绝对 deadline
  覆盖 stop、完全停止确认、静默确认、finalize、start 与 health；恢复的四小时 RTO 从
  authorization-bound `declare` 持久化到 proxy 外部 readiness，包含预检、停服、恢复、
  app/proxy health 与功能验证，不在重试时重置。
- **破坏性/需恢复动作**：backup 在调用 app stop 前置
  `app_needs_recovery=1`。restore 在调用 proxy/app stop 前置
  `services_need_recovery=1`，并先记录二者原始 running/health 状态；若现场留存前/期间失败，
  只恢复原本运行的服务，app 先启动并 healthy 后才允许启动 proxy。proxy 不能 healthy
  时立即重新停止并告警；原本停止的服务绝不越权启动。调用数据库 restore 前置
  `destructive_restore_started=1`，之后任一失败保持 proxy 隔离并告警，不能把部分恢复
  公开。
- **`/data/files` 权限**：业务写入点只有 PDF upload 与 QR cache。PDF upload temp
  保持 0600 到验证通过，正式发布 descriptor 才 `fchmod(0660)`；QR cache 在发布前恢复
  group-class ACL mask；恢复写入点在原子
  replace 前设置 0660 与 UID 10001 named/default ACL。初始化、现存文件修复与新目录
  继承由 `backup-file-access-init` 覆盖；无关 UID 在有/无 ACL 两种情况下都不会因 mode
  位获得访问。
- **远端删除信任与单向性**：全量检查 completion、verified、deleting 与 data namespace
  的读写/删除路径。upload 可控 completion 只作一致性输入；age 解密只证明密文未损坏，
  不被误当成发送方认证。恢复在停服/目标写入前，以及后续每次清单重载时，必须以
  Ed25519 公钥验证签名；删除键和代际也只能由同一公钥验证通过的 manifest 推导；
  控制 namespace 和其他 backup point 不能成为数据删除键。deleting journal 每次在
  manifest 删除前都重新认证，且只能向删除完成前进，任何路径都不能把它复制回
  completion。manifest 删除后只允许清理已失去所有 control marker 的 journal 自身。

## 异地身份与轮转

生产 upload identity 只允许 Put/List/Get/GetAttributes，不允许 DeleteObject、
DeleteObjectVersion、生命周期/保留期修改或绕过对象锁，也不能覆盖已完成的
`backup_id`。bucket 必须开启版本化和至少 14 天对象锁。策略示例的具体 bucket、账号和
区域由授权部署人填写，检查器拒绝同主机、同生产盘生命周期、同账号/同区域反例。

删除身份只存在于异地平台生命周期或独立授权环境；不在生产机、`.env.backup`、定时
容器或 backup 镜像。其显式最小权限为 ListBucket/GetObject/PutObject/DeleteObject：
PutObject 只用于发布 retention deletion journal；禁止 DeleteObjectVersion、
PutObjectRetention、BypassGovernanceRetention 及生命周期修改。独立工具
[`scripts/retention/rotate.py`](../scripts/retention/rotate.py) 默认 dry-run，要求：

```shell
PR2A_DELETE_AUTHORIZED_ENVIRONMENT=1 uv run python -m scripts.retention.rotate \
  --config /run/secrets/delete-rclone.conf \
  --identity /run/offline/age-identity.txt \
  --manifest-verification-key /run/offline/manifest-verification-public.key \
  --manifest-authentication-key-id manifest-auth-2026-01 \
  --restore-verification-key /run/offline/restore-verification-public.key \
  --restore-verification-key-id restore-verification-2026-01 \
  --remote s3-delete:bucket --prefix production \
  --now 2026-08-07T03:00:00Z
```

实际删除还必须显式加 `--apply`。同一 backup 可同时属于日/周/月，只有所有层级都过期
才删除；时钟倒退/跳跃由运行平台先阻断。上传失败不推进代际。`verified` 只有从异地
下载开始、完整预检/现场留存/数据库/文件/离线/隔离 app/proxy-last/readiness 全链成功
后生成。无论一次轮转前有一个还是多个 verified，轮转后都必须至少保留一个（多个同时
过期时保护最新 verified）；CAS 只在所有保留 completion marker 都不再引用时才成为
删除目标。

轮转绝不把 upload identity 可写的 completion 字段当成删除授权。独立授权环境使用
离线 age identity 解密该 `backup_id` 的固定路径 manifest，再以独立、operator-owned、
mode-0600 的 32-byte Ed25519 公钥核对 key ID 与 signature。age recipient 公钥和
upload credential 即使同时泄漏，也无法生成通过签名验证的清单。完整 schema/signature
校验后，仅从其中推导本点的 database/config/manifest 固定键和 SHA-256 CAS
age/metadata 成对键；completion 的时间、代际和键集合必须与该认证结果完全相同，
否则在任何写/删前 fail closed。age identity 是一次性人工授权输入，不进入定时容器、
backup 镜像或远端；签名私钥只读进入 backup 生成端，公钥只读进入 restore 与独立删除
授权环境。私钥不进入镜像、env、argv、日志或远端，upload rclone 身份不可读取。

apply 在隐藏 completion 前，由删除身份把上述认证结果写入 `deleting/` 并重新读取
核验。续跑时只要 manifest 仍在，就再次认证解密并要求 journal 完全一致；同时重新
确认该点仍满足过期策略。删除顺序固定为 completion、非 manifest 数据、可删除 verified、
manifest、journal，因此 manifest 在所有可由键集合引导的删除完成前始终可用于重新
认证。若崩溃发生在 manifest 删除后，只允许在 completion/verified 均已不存在时删除
journal 自身，不再信任或执行其中任何键。journal 永远单向用于继续删除，绝不复制或
复原为 completion；若 journal 点成为最新/唯一 verified，立即 fail closed，绝不让
可能不完整的点重新可选。

## 密钥生命周期

每个清单固定 `recipient_key_id`。生产机只持对应公钥；私钥由恢复授权保管人离线、独立
于生产机和异地上传账号保管。轮换流程为：

1. 先生成 K-new 并登记 key ID/保管人/可用副本；
2. 预复制和新点改用 K-new，K-old 保持可取回；
3. 从异地分别取回一个 K-old 和 K-new 点，以对应钥匙完整预检；
4. 交叉错钥必须认证失败；私钥退出容器后不得残留在 tmpfs、env、argv 或日志；
5. K-old 最后负责的所有日/周/月点全部过期且不再受 verified 保护后才可销毁。

一份私钥副本遗失必须告警并走离线备份恢复；不得改写清单、跳过认证或用新钥匙“代替”
旧钥匙。最老仍保留点的私钥可用性是轮换批准条件。

manifest Ed25519 key pair 与 age key 分离，私钥/公钥均固定 32 bytes；私钥仅挂入
backup 容器，公钥进入一次性 restore 和独立删除授权环境。upload identity 不得读取
私钥。当前 contract 使用一个固定 key ID；需要轮换时须先扩展并验证显式
key-ID→public-key keyring，在所有旧 key ID 的点过期前保留旧公钥。禁止静默替换同一
key ID，验证失败只允许阻断恢复/轮转，不能退回信任 completion/journal 或仅凭 age
解密。

## 恢复前置与强确认

恢复入口无默认目标、不接受相对/根路径、不接受空变量，不以 `--force`/`YES` 代替授权。
环境 marker 是独立 mode-0600 文件。生产（本地测试只模拟
`synthetic-production`）授权 JSON 必须同时绑定：

```json
{
  "environment_id": "synthetic-production-01",
  "backup_id": "20260807T023000Z-0123456789abcdef0123456789abcdef",
  "operator_id": "operator-7",
  "approved_data_loss_window": "2026-08-06T02:30Z..2026-08-07T02:30Z",
  "authorization_record": "change-1234",
  "expires_at": "2026-08-07T04:00:00Z",
  "one_time_challenge": "random-target-and-backup-bound-value"
}
```

challenge 必须与 `.env.backup` 的一次性输入完全相同，且授权未过期。缺环境身份、环境
不匹配、源=目标、未知 backup_id、app 未停、proxy 未隔离、缺授权/损失窗口/challenge
均在第一次破坏性写之前拒绝。工具不是“永远拒绝 production”：授权材料完整、现场留存
通过时，授权人可执行正式恢复。

## 预检、现场留存与恢复状态机

运行（真实值只能由授权人准备）：

```shell
cp .env.backup.example .env.backup
chmod 0600 .env.backup
scripts/backup_recovery/restore-run.sh \
  20260807T023000Z-0123456789abcdef0123456789abcdef
```

顺序不可交换：

1. host wrapper 在第一次预检前，以授权记录、目标、备份点、损失窗口、到期时间和
   one-time challenge 摘要生成不含秘密的 `operation_id`，原子持久化本次恢复的 RTO
   起点；同一授权下的所有重试复用该 operation 与起点，直到外部 readiness 完成，
   不得通过重跑重置四小时窗口；以后再次恢复同一 `backup_id` 必须签发新的授权记录和
   challenge，由新的 operation 从 `declared` 开始，历史 checkpoint/audit 只读保留，
   绝不复用其 destructive stage；
2. 在不停服务、未写目标时从异地下载 completion 和全部密文；
3. 核对密文大小/SHA-256，age 认证解密，核对清单/全部明文 SHA-256、backup_id 同源、
   key ID，并把清单的完整 `source_commit`、五镜像 digest、逐文件配置 SHA-256 与当前
   干净部署 checkout/环境逐项绑定，再检查 PG/age 兼容、秘密引用、cache 与目标空间；
   resume 仅按尚未缓存且摘要未验证的密文计算空间和 25% 余量，已验证 cache 不重复
   预留，损坏 cache 删除后重新计入；目标卷会逐个哈希已存在文件，只有 size/SHA-256
   完全匹配才按零新增空间处理，缺失或不匹配对象按同目录原子替换所需的完整字节和
   inode（含缺失目录）预留；
4. 任一下载、错钥、截断、翻转、字段缺失、不兼容、inode/空间不足立即清理短暂明文并
   fail closed；
5. 预检全过后，先记录 app/proxy 原始 running/health 状态，在 stop 前标记 services needing
   recovery，再停止 proxy 和 app 并分别确认完全 stopped；现场留存尚未进入数据库
   破坏性恢复时失败，只恢复原本运行的服务。app 必须先 healthy，之后才允许启动并确认
   proxy healthy；任一健康检查失败都重新停止 proxy，原本停止的服务保持停止。数据库
   restore 发出前先标记 destructive，之后失败则公开入口保持不可达；
6. 现场留存当前数据库、全文件摘要清单、配置/镜像身份和审计投影，使用同一公钥认证
   加密并复核密文；留存在同卷一次性 staging 目录生成，全部核验后原子改名发布。未有
   checkpoint 的 staging/成品是可丢弃半成品，重试先安全清理再重建；留存失败禁止下一步；
7. 数据库 → 文件 → 离线全量校验；文件恢复只覆盖清单引用的内容寻址目标，不删除现场
   中备份点之后的追加文件；共享恢复锁排除活跃写入者后，硬中断留下的常规
   `.NAME.restore` 临时文件在容量预检中计为可回收空间并在重写前删除，符号链接、目录
   或其他非常规临时对象一律 fail closed；
8. 仅启动 app，proxy 仍 stopped；隔离网络验证三态、v1/v2 指针切换、二维码不变、
   停用下一请求即时失效及 `no-store`、审计只追加；
9. 功能 evidence 全部为真后生成 proxy authorization，最后启动 proxy、检查外部
   readiness，才记录本 operation 的 RTO 完成并产生 verified marker。marker 是每个
   `backup_id` 的不可变“至少成功恢复过一次”证明；以后新授权再次恢复同一 backup 时，
   必须重新走完整恢复/readiness，但可复用已通过签名且绑定同一 authenticated manifest
   摘要的 marker，不要求旧证明伪装成当前 operation，也不覆盖旧证明。

若“库好、文件未好”，checkpoint 停在 `database_restored`，app/proxy 启动守卫拒绝；
使用同一授权再次执行 host wrapper，会重做非破坏性预检并跳过本 operation 已完成的
破坏性 checkpoint，再从失败阶段幂等续跑；一个新灾难/恢复必须签发新授权，因而即使
选择同一有效 `backup_id` 也会执行新的现场留存、数据库与文件恢复。也可用本 operation
已验证的现场留存回退。proxy 开放/readiness 失败时 app 仍隔离，发 critical 告警，
不能记录 RTO 完成。

固定卷模式在现有 `product_pdf_qr_files` 内恢复引用对象且保留额外追加对象；数据库由
owner 原位 clean/restore。新灾备栈模式使用新的 Compose project/卷初始化同一服务与
稳定契约，验证后由授权人切 DNS/入口；两种方式都不假设 Docker 卷可原子重命名，也不
现场手改 PR1 Compose。切换中断时旧入口保持关闭或仍指向未被修改的旧栈，可按 checkpoint
续跑。

配置本体随加密 config tar 保存；灾备恢复先按 `source_commit` 取回不可变仓库，再以
manifest 的逐文件 SHA-256 校验 Compose/Nginx/systemd/脚本。秘密只保存引用/指纹：
数据库密码、证书重签、ACME、age 私钥、上传/删除身份由各自授权保管人恢复，取回等待均
计入 RTO。

## G-15 完整校验

离线阶段逐项复算：

- admins/products/pdf_files/pdf_versions/admin_sessions/audit_events 总数等于冻结点；
- 产品状态、current 指针、全部当前和历史版本关系的稳定有序投影 SHA-256 相同；
- 遍历每个当前和历史版本的 storage_path，物理文件存在且 size/SHA-256 匹配；
- bundle 文件清单双向无缺失/篡改；备份点后文件保留但不伪装为 bundle 成员；
- 审计数量、顺序、操作者、目标、动作、结果、关联 ID 与 detail 投影摘要完全相同；
- `app_rw` 不能改/删既有审计，恢复自身运维事件不含密码/token/PDF 内容。

隔离 app 阶段验证未上传、启用当前 v2、停用优先、切 v1/再切 v2且版本数不变、二维码
URL 不变、停用后同 URL 下一请求即时失效且 `Cache-Control: no-store`。只有这些全过，
proxy 才能最后开放。

## 本地合成演练与 A19 命令

先创建/选择名称以 `pr2a-synthetic-` 开头的独立 Docker context。脚本拒绝 default、
production 标记、稳定生产卷名和空 project，并只创建带唯一 `run_id` 的容器/网络/目录：

```shell
export PR2A_DOCKER_CONTEXT=pr2a-synthetic-local
make backup-recovery-rehearsal
```

正式 PR 的 A19 记录必须列出同一待测 commit 下的原命令、起止时间、退出码和 evidence
路径，至少包括：

```shell
make lint
make typecheck
make test-unit
make test-integration
make prod-config-check
make backup-contract-check
make backup-config-check
make backup-recovery-rehearsal
make backup-image-build
docker image inspect product-pdf-qr-backup-recovery:local
docker run --rm --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true \
  product-pdf-qr-backup-recovery:local validate-contract
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
  aquasec/trivy:0.72.0 image --exit-code 1 --ignore-unfixed=false \
  --severity CRITICAL,HIGH product-pdf-qr-backup-recovery:local
```

镜像须双构建比较 image ID/digest，检查 UID 非 0、无秘密/开发 cache/pytest/mypy、本地
文件、PG 主版本匹配、只读根/cap_drop/no-new-privileges/mount/network/一次性 smoke。
Trivy 必须是 CRITICAL/HIGH 0 且 `ignore-unfixed=false`。本地合成 evidence 不能写真实
secret、PDF 内容或可复用凭据，也不能标作 G-17/G-18。

## G03 覆盖索引

| 清单 | 实现/证据入口 |
|---|---|
| P01 | contract checker + 本文参数/容量/角色/模式 |
| P02 | rehearsal context/resource guard |
| AC1-01～07 | timer、SHA inventory、宿主静默守卫、共享锁、900s 硬门、app trap |
| AC2-01～03 | 五阶段注入、不可变 Local/S3 publish、app critical alert |
| AC3-01～04 | manifest schema/同源校验/config allowlist/completion-last |
| AC4-01～04 | 流式 age、64 MiB tmpfs、错钥/截断/篡改、key ID 代际流程 |
| AC5-01～05 | S3 跨账号/区域契约、upload 无删除、completion-last、远端复读、restore download |
| AC6-01～04 | 14/8/6 边界测试、共享锁、verified 来源、独立 rotate 工具 |
| AC7-01～06 | RestoreGuard、全量预检、加密现场留存、app_migrate、profile overlay |
| AC8-01～04 | proxy-last 状态机、files checkpoint、八阶段注入、固定卷/新灾备栈 |
| AC9-01～05 | 全文件/关系/审计投影、隔离 evidence、统一 RTO/RPO 起止 |
| AC10-01～04 | Make 命令、两轮 rehearsal、独立新镜像 G-16、PR evidence index |

每次 rehearsal 在 `reports/backup-recovery/<run_id>/` 写 `evidence-index.json`、阶段 JSON、
失败矩阵、命令/退出码、单调与墙钟时间、commit/image digest 和敏感扫描结果。第二次从
全新资源运行；除 backup_id/时间外语义清单须相同。46 项任一缺 evidence、任何半成品
成为成功、proxy 提前开放、唯一 verified 被删、生产侧出现私钥/删除身份或完整明文持久
落盘，G-15 结论必须是 FAIL。
