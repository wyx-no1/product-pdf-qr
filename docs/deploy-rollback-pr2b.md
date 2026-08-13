# PR2B 发布回滚与 G-19 本地演练

本文说明 Issue #40 的机械安全边界。它只新增发布/回滚编排，消费 PR1
的 app/proxy 控制面与 PR2A 的共享 owner lease、`backup-run.sh` 和
`restore-run.sh <backup_id>`；不会修改或复制五服务拓扑、备份契约、恢复阶段或业务逻辑。
本仓库内的演练只使用隔离合成数据，不能记为 G-17、G-18 或真实生产 RTO 通过。

## 不可违反的数据语义

唯一可自动恢复发布前数据库和文件的情况，是持久状态仍为
`prepared`、`migrated` 或 `isolated_validated`，proxy 自候选部署前起持续隔离，并且
发布前全量水位没有任何变化。水位包含
`admins/products/pdf_files/pdf_versions/admin_sessions/audit_events` 的稳定有序投影、全部当前和
历史文件的路径/size/SHA-256，以及审计投影。计数、mtime 和“没看见新产品”不是水位。

`public_cutover` 必须在启动 proxy 或切 DNS 前原子持久化，并留下不可逆 marker。状态和 RTO
记录都有独立完整性 seal；状态缺失、损坏、未知、倒退、跳阶段、
竞态、曾经公开或任一水位不同，都会选择 `preserve_forward_data`。因此“已经公开但暂未观察到写入”
也绝不会误走旧库恢复。

两条结果如下：

- `pre_public_restore`：两种情况都先验证并激活与 `B0` 一致的稳定 checkout、五个 PR1 镜像加
  PR2A 镜像、配置和秘密引用；db/certbot 切到稳定 exact image，稳定 app/proxy 只 create 不公开。
  无论迁移 revision 是否变化，随后都只调用该 checkout 内未修改的
  `scripts/backup_recovery/restore-run.sh B0`，使路径一始终精确恢复发布前 B0 数据语义。
- `preserve_forward_data`：兼容迁移固定按“记录 RTO → PR2A 共享锁 → 核验 S/C 产物 → 停 proxy
  → 停 app → 原子切稳定 app/允许配置 → 隔离完整读写 → proxy-last → 外部 readiness”执行。
  数据库 schema/db image、数据库卷、文件卷、证书、当前秘密引用和上线后审计没有可调用的回退
  接口。旧 app 任一验证失败会在 proxy 停止时精确恢复候选 app/config；候选验证通过后，只有路径二
  才恢复公开服务。

完整读写验证会合法地产生产品、版本、文件、session 和追加审计，所以它只能在公开发布前的隔离副本
上完成并固化到 release record 的迁移责任方 exact-SHA 证据中。生产回滚切换不接受运行时计划中的
自报布尔值或任意关系摘要；稳定 app 启动后只执行
`PR2B_NONMUTATING_VALIDATION_COMMAND_JSON` 的只读兼容探测，并在探测前后各采集一次完整
DB/files/audit 水位。两次水位都必须机械地精确等于已观察的 W1，否则保持 proxy 隔离并精确
roll-forward 候选 app。因此真实时间戳、UUID 或追加审计不会被伪装成可预声明 delta，既有 W1 的
任一行、文件或审计丢失也不能通过。

兼容的含义是旧 app 对候选 schema 和候选已写值完成登录、创建、导入、上传、历史恢复、启停、
未上传三态、公开读取、审计追加，并验证约束、默认值、枚举、触发器和权限。判定必须来自迁移责任方，
绑定稳定/候选 commit、migration SHA、Alembic revision、app digest、允许配置摘要和当前
release-specific G-19 run。缺失、`unknown` 或任一身份变化均阻断。

不兼容发布必须在公开前已有绑定 exact release 的已演练 roll-forward，或已批准且限定损失窗口的
数据方案；两者皆无时 record 无法封存。公开后的自动回滚固定以退出码 `78` 和机器状态
`NEEDS_ROLLBACK_DECISION` 停止，不切旧 app、不调用 PR2A、不重置计时。退出选择只有：

1. 在同一 operation/RTO 起点内执行 exact candidate roll-forward；
2. 授权人提供绑定 release identity、operation、environment、`backup_id`、operator、过期时间、
   一次性 challenge 摘要、损失起止、加密现场留存摘要和人工对账办法的批准。该 challenge 成功
   使用后不可重放；验证通过才会给出 PR2A handoff，PR2B 本身仍不实现恢复。

第二种选择只允许人工显式调用：

```sh
./scripts/deploy_rollback/authorized-lossy-run.sh \
  release-40 operation-unique-40 authorized-operator \
  /absolute/authorization.json /absolute/one-time-challenge \
  <authenticated-onsite-retention-sha256>
```

它先在共享 lease 内验证 decision audit、operation-specific 环境确认、稳定 checkout 和完整有损
授权（尚不消费 challenge）；全部通过后才停止 proxy/app、把 challenge 标为一次性使用，最后调用
未修改的 PR2A `restore-run.sh`。PR2A 仍独立执行自己的全部授权/preflight/
现场留存/恢复/验证/proxy-last 状态机。恢复后必须精确等于 W0，并在库外审计标为
`COMPLETED_AUTHORIZED_DATA_LOSS`，不会伪装成普通回滚成功；W1 的加密现场留存和对账责任仍保留。

## 发布前准备

先在仍运行稳定身份的干净 checkout 执行即时 PR2A 一致备份：

```sh
./scripts/deploy_rollback/prepare-release-backup.sh \
  /absolute/append-only-evidence/pre-release-backup.txt \
  /absolute/immutable-evidence/W0.json
```

该入口先在共享 lease 内停止 proxy，并在整个候选部署期间保持隔离；随后顺序消费 PR2A 的
`precopy` 与 `finalize`，两次均使用原共享 lease。独立的 `compose.rollback.yaml` profile 不覆盖
任何 PR1/PR2A 服务，只挂只读 file volume、database network 和 `app_backup` pgpass；它在 repeatable
read-only DB snapshot 内逐关系投影并对完整文件 inventory 取摘要，生成 W0，且不挂备份签名、
rclone、恢复或业务写权限。把输出中 completion-last、远端复核成功的 `backup_id`、manifest 身份和
`g19_watermark_sha256` 写入 release record；publication prepare 会再次要求 W0 与这个 B0 绑定摘要
完全相同。最多 24 小时前
的日常点不能替代这个发布前即时点；record validator 要求它在声明前一小时内完成、已加密签名、
completion-last、远端核验且 PR2A preflight 可取回。

release record 是按 `release_id` 只创建一次的 JSON，本体旁有 SHA-256 封印；冲突覆盖、串用批准、
回放旧 G-19 或缺少任一字段都会拒绝。它必须包含：

- stable/candidate exact commit、migration SHA、Alembic revision；
- app、migrate、proxy、db、certbot、PR2A 的 `tag@sha256`，以及 registry digest、本地 image ID、
  已预取和覆盖批准回滚窗口的保留证据；
- 可取回的非密钥 recovery 配置和仅 app 可回退配置的 base64 本体、摘要与保留期；
- 只含版本化引用的数据库/session/ACME 等秘密记录，不含秘密值；
- `B0` 的 exact stable identity、发布批准、迁移责任方批准、stable 隔离 smoke、当前 exact
  release 的 G-19 run；不兼容时还含发布前处置方案和其批准本体。

封存并准备状态：

```sh
python -m scripts.deploy_rollback.cli seal-release \
  --record /absolute/release-40.json \
  --store /absolute/immutable-release-store
./scripts/deploy_rollback/publication-run.sh \
  prepare release-40 publication-operation-40 authorized-operator
```

publication 入口与备份/回滚复用同一个 PR2A owner lease；迁移和隔离验证命令分别通过
`PR2B_MIGRATION_COMMAND_JSON`、`PR2B_ISOLATED_VALIDATION_COMMAND_JSON` 的无 shell argv 运行，
成功后状态才能逐级推进。候选隔离验证完成后执行：

```sh
./scripts/deploy_rollback/publication-run.sh \
  public_cutover release-40 publication-operation-40 authorized-operator
```

入口先重新 inspect S/C 全部 exact 本地 image ID 和封存证据，再不可逆地写入
`public_cutover`，随后授权并运行 `PR2B_PUBLICATION_COMMAND_JSON` 中的 proxy/DNS argv。公开命令
失败也不会把状态倒回公开前，因此随后只会走保数据路径二。CLI 状态 mutation 还会核对
调用进程确实属于共享 lease owner 的同一进程组，不能用普通 direct call 绕过发布/备份互斥。

## 回滚入口、锁与 RTO

`rollback-run.sh` 需要绝对路径的 release store、publication state、全量 watermark、RTO state、
库外 audit、原子 runtime identity、结果文件和稳定 checkout。验证命令以 JSON argv 数组注入，
从不经过 shell；它们分别生成完整业务验证、水位和外部 readiness 证据。示意变量名：

```text
PR2B_RELEASE_STORE
PR2B_PUBLICATION_STATE
PR2B_WATERMARK_FILE
PR2B_RTO_STATE
PR2B_AUDIT_LOG
PR2B_RUNTIME_IDENTITY
PR2B_RESULT
PR2B_ENVIRONMENT_MARKER
PR2B_ENVIRONMENT_CONFIRMATION
PR2B_STABLE_CHECKOUT
PR2B_PROXY_CONTINUOUSLY_ISOLATED=yes|no
PR2B_NONMUTATING_VALIDATION_COMMAND_JSON
PR2B_CANDIDATE_VALIDATION_COMMAND_JSON
PR2B_WATERMARK_COMMAND_JSON
PR2B_EXTERNAL_READINESS_COMMAND_JSON
PR2B_PROXY_AUTHORIZATION_PATH
PR2B_PUBLICATION_FENCE_STATE
PR2B_MIGRATION_COMMAND_JSON
PR2B_ISOLATED_VALIDATION_COMMAND_JSON
PR2B_PUBLICATION_COMMAND_JSON
```

生产拓扑的 watermark argv 使用
`["/absolute/checkout/scripts/deploy_rollback/capture-watermark.sh"]`；本地纯合成夹具也可直接给
watermark module 注入 loopback `PR2B_DATABASE_URL` 与临时 `PR2B_FILE_ROOT`。

环境 marker 必须绑定 environment、非 default Docker context、Compose project、resource prefix 和
synthetic/production 类型。合成环境的 context/project/resource 均须以 `synthetic-` 开头且确认串为
`synthetic:<environment>:<operation>`；授权生产环境使用
`production:<release>:<operation>:<environment>:<operator>`。通用 `YES`、`--force`、空目标、
default context、类型混用或符号链接 marker 都在读取秘密/停止服务前拒绝。

调用形式是：

```sh
./scripts/deploy_rollback/rollback-run.sh \
  release-40 operation-unique-40 authorized-operator
```

operation 在等待锁、取回配置或秘密之前持久化唯一 RTO 起点。重试、进程重启、路径一转路径二和人工
等待复用同一文件；elapsed 只增不减。只有 proxy-last 的外部 readiness 才完成计时。
`elapsed <= 14400` 通过，`> 14400` 记录关联同一 release/operation 的 `RTO_EXCEEDED` 并使 G-19
失败，但绝不触发自动有损动作。

app-only 路径在取得 `scripts/backup_recovery/lock.sh` 的 owner lease 后执行；活锁竞争退出 75，
死/损坏 owner 必须人工核对，cleanup 只释放自身 lease。路径一先在共享 lease 内停
proxy/app 并重算完整 W0；若水位竞态变化，就在同一 operation 转路径二而不恢复旧库。W0 冻结后
释放准备 lease，再由原 `restore-run.sh` 从 preflight 到 proxy-last 全程持有同一 lease，避免
嵌套死锁。调用 PR2A 前还必须启用 release approval 和 environment marker 共同绑定的持久
publication fence；它阻断全部客户流量，只允许 exact readiness 探针。PR2A 即使启动 compose
proxy，客户仍不能写入或观察恢复结果；若 PR2B 重新取 lease 失败，fence 保持启用。PR2A 返回后，
PR2B 会重新取得 lease、证明 fence 仍启用并再次隔离 compose proxy，重新采集恢复后的真实
DB/files/audit 水位；只有它精确等于 publication state 中的 B0，才写入库外验证审计并重新
启动 proxy、执行 fence 内 readiness，并由已批准 fence 命令原子完成最终公开与 readiness。
普通和人工获批有损路径都不能用保存的 B0 文件冒充恢复结果。
路径一的持久围栏只在锁内停止 app/proxy、重采水位且再次确认仍可恢复 B0 后启用；若最后一笔写入
使冻结判定转为兼容路径二，app-only 切换不会继承未发布的 restore 围栏，也不会误报已恢复但客户
流量仍被阻断。
外部 readiness 的完成时间在返回瞬间固定；随后写审计的等待不会错误延长或重置 RTO。

库外审计是 mode 0600 的 JSONL hash chain 加持久 head/count anchor。成功、拒绝、失败、重试、路径、
操作者、S/C digest、backup、兼容结论、水位、失败阶段和人工决定都只追加；修改、截断或删除旧事件
会 fail closed。审计禁止 challenge、token、秘密、私钥、凭据和 PDF 内容。

## G-03 / G-19 与 A19

Issue #40 评论中的 T40-01 至 T40-37 不增删。实现级测试按同一编号覆盖 release record、发布门禁、
保守选路、两条路径、人工节点、有损授权、proxy-last、精确前滚、共享锁、不可重置 RTO、故障清理、
原子状态、全量水位和库外审计；PR2A 的八阶段失败/恢复与 owner lease 测试继续由现有 PR2A 测试消费，
没有复制。

本地回滚演练连续执行两轮独立资源。每轮启动真实隔离 PostgreSQL 与文件树，执行真实
`publication-run.sh`、`rollback-run.sh`、`authorized-lossy-run.sh` 和 PR2A
`restore-run.sh`；只把 Docker 服务控制与外部 HTTPS 边界替换为记录 exact identity/阶段的本地
适配器。每轮机械断言路径一恢复后真实水位等于 B0、兼容路径二的生产只读探测前后水位精确等于
W1、有损授权路径在 PR2A 前创建稳定 stopped identity 且恢复后为 B0；两条 PR2A 路径还断言
publication fence 从 restore 前持续到 B0 验证和最终原子发布。

```sh
make deploy-rollback-rehearsal
```

PR 的 A19 必须在同一待测 commit 记录以下原命令、起止 UTC、退出码和结果：

```sh
make lint
make typecheck
make test-unit
make test-integration
make deploy-rollback-rehearsal
git diff --check
```

本地结果只表示 G-03/G-19 的实现候选证据，仍由测试负责人验证、发布负责人批准。
