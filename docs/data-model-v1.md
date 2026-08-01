# 数据模型设计 v1

版本：v1（G-02 提案）
日期：2026-08-01
业务事实来源：`docs/requirements-v2.md`
配套文档：`docs/architecture-v1.md`

> 本文件是 G-02 待评审提案，未经门禁责任方判定不生效。不含功能代码，DDL 片段仅用于表达约束意图。

---

## 1. 设计的三条主线

整个模型围绕三个不变量组织，其余都是附属：

1. **产品编码唯一且不可变，产品不可删除** → 编码列加唯一约束，无删除入口，状态用字段表达。
2. **任意时刻恰有一个当前版本，历史只追加** → 版本表只插入不更新，"当前"是产品表上的一个指针。
3. **公开标识与内部标识完全解耦** → 独立列、独立生成源、独立唯一约束。

---

## 2. 实体关系

```
   admins                      audit_events
     │ 1                            ▲
     │                              │ 记录所有写操作
     │ 上传/操作                     │
     ▼ N                            │
  products ─────────────────────────┘
     │ 1
     │  current_version_id ──┐  (指针，可为空)
     │ N                     │
     ▼                       │
  pdf_versions ◀─────────────┘
     │ N
     │ 引用物理文件
     ▼ 1
  pdf_files
```

关系要点：

- `products` → `pdf_versions`：一对多，版本永久保留。
- `products.current_version_id` → `pdf_versions`：**指针**，可为空（未上传），非空时必须指向本产品自己的版本。
- `pdf_versions` → `pdf_files`：多对一。多个版本、甚至多个产品可以指向同一物理文件（内容相同则只存一份）。
- `audit_events` 不设外键约束指向业务表，只存标识副本——因为业务记录不可删除，但审计必须在任何情况下都能写入且不被业务约束阻塞。

---

## 3. 产品表 `products`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | bigserial | PK | 内部主键，**绝不出现在公开 URL** |
| `code` | varchar(64) | NOT NULL, UNIQUE | 规范化后的**大写**编码，唯一业务身份 |
| `public_token` | varchar(26) | NOT NULL, UNIQUE | 128 位熵，Base32 无填充 |
| `status` | smallint / enum | NOT NULL, DEFAULT active | `active` / `disabled`，无 `deleted` |
| `current_version_id` | bigint | NULL, FK → pdf_versions | 当前有效版本指针；NULL 表示未上传 |
| `created_at` | timestamptz | NOT NULL | |
| `updated_at` | timestamptz | NOT NULL | |

**关键约束**

```sql
-- 编码唯一（已是规范化大写，直接唯一即可）
UNIQUE (code)
-- 编码格式在应用层与数据库层双重保证
CHECK (code ~ '^[A-Z0-9_-]{1,64}$')
-- 公开标识唯一
UNIQUE (public_token)
-- 指针必须指向本产品自己的版本（复合外键）
FOREIGN KEY (id, current_version_id) REFERENCES pdf_versions (product_id, id)
```

### 为什么这样设计

- **`code` 直接存大写，不保留原始写法**：B-03 与 T-03 已明确覆盖了「原文展示 + 归一化判重」的旧方案。保留两份表示会让展示、判重、文件命名三处随时间分叉——而二维码文件名必须精确等于编码（B-09），分叉的代价是不可逆的。
- **`CHECK` 约束是冗余的，但必须有**：应用层已校验，数据库层再校验一次。理由是编码创建后不可修改、产品不可删除——一条脏数据会永久留在系统里，冗余校验的成本远低于事后无法清理的代价。
- **`public_token` 与 `id`、`code`、`created_at` 无任何推导关系**：T-10 要求 128 位熵。Base32 编码 128 位得 26 字符，URL 安全、无大小写歧义、便于必要时人工转录。**不用 UUIDv4**（122 位有效熵且含版本位）、**不用自增或时间戳派生**。
- **复合外键防止指针跨产品**：只写 `FK current_version_id → pdf_versions(id)` 时，代码缺陷可能让 A 产品的指针指向 B 产品的版本，扫 A 的码看到 B 的资料——这是最严重的一类事故。用复合外键让数据库直接拒绝。
- **没有 `deleted_at`**：产品不可物理删除（v2 编码规则 6），软删除字段会诱导后续实现出现"隐藏"语义，而 B-04 已明确第一期不新增作废/隐藏状态。

### 产品编码不可修改的数据层保护

「编码创建后不可修改」（v2 编码规则 6）是本系统最强的不变量之一：编码决定二维码文件名、决定判重、决定业务追溯。**只靠"应用层不提供修改入口"是不够的**——一次误写的 UPDATE、一段迁移脚本或一个未来被添加的管理功能都能绕过它。因此在数据层设置多重保护：

**第一层：应用层无入口。** 产品更新操作只允许修改 `status` 与 `updated_at`；ORM 层将 `code` 与 `public_token` 标记为只读字段，禁止出现在任何 UPDATE 语句的赋值列表中。

**第二层：数据库触发器拒绝变更。**

```sql
CREATE FUNCTION reject_immutable_product_columns() RETURNS trigger AS $$
BEGIN
  IF NEW.code IS DISTINCT FROM OLD.code THEN
    RAISE EXCEPTION 'product code is immutable (v2 编码规则 6)';
  END IF;
  IF NEW.public_token IS DISTINCT FROM OLD.public_token THEN
    RAISE EXCEPTION 'public token is immutable (B-07: 永久二维码不轮换)';
  END IF;
  IF NEW.id IS DISTINCT FROM OLD.id THEN
    RAISE EXCEPTION 'product id is immutable';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_products_immutable
  BEFORE UPDATE ON products
  FOR EACH ROW EXECUTE FUNCTION reject_immutable_product_columns();
```

**第三层：权限最小化。** 应用连接使用的数据库角色对 `products` 只需 `SELECT, INSERT, UPDATE`；`DELETE` 权限**直接回收**——产品不可物理删除，应用就不该拥有这个能力。

**为什么把 `public_token` 一并锁死**：B-07 确认二维码地址永久不变、不做轮换。token 若可被修改，等于所有已印刷在实物上的二维码可以被静默作废，这是不可逆的现场事故。它和 `code` 属于同一等级的不变量。

**为什么用触发器而非仅靠 CHECK**：`CHECK` 约束只能校验单行当前值，无法比较"改前 vs 改后"。要表达"这个值不许变"，触发器是 PostgreSQL 中唯一直接的手段。

**代价与例外**：若未来业务确认允许改编码（目前明确禁止），必须先走需求变更流程更新 requirements，再迁移触发器。**触发器的存在使这类变更无法被静默执行**——这正是设置它的目的。

---

## 4. PDF 文件表 `pdf_files`

物理文件，按内容寻址。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | bigserial | PK | |
| `sha256` | char(64) | NOT NULL, UNIQUE | 内容哈希，同时是去重键 |
| `size_bytes` | bigint | NOT NULL | 与 sha256 共同用于 T-05 判重 |
| `storage_path` | text | NOT NULL | 由哈希派生，如 `ab/cd/<sha256>.pdf` |
| `created_at` | timestamptz | NOT NULL | 首次入库时间 |

### 为什么这样设计

- **文件与版本分离**：同一份 PDF 可能被恢复、被多个产品使用。若把文件内容信息塞进版本表，恢复历史版本就得复制文件——而 T-08 明确规定恢复只移动指针、不复制不删除。
- **`storage_path` 完全由 `sha256` 派生**：用户上传的原始文件名**永不参与路径构造**。这是路径穿越防护的根本手段（详见 `security-design-v1.md`），比任何过滤都可靠。
- **`size_bytes` 单独存**：T-05 要求"文件大小与 SHA-256 共同判断"。虽然哈希相同则大小必然相同，但业务规则明确写了两者，实现与验收都按两者比对，避免测试无法直接对应需求条文。
- **没有引用计数、没有删除**：T-08 规定存储只增不减，第一期不设任何删除入口。加引用计数会诱导后续实现出现"孤儿文件清理"，那正是不可逆操作。

---

## 5. PDF 版本表 `pdf_versions`

只追加，永不更新、永不删除。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | bigserial | PK | |
| `product_id` | bigint | NOT NULL, FK → products | |
| `pdf_file_id` | bigint | NOT NULL, FK → pdf_files | |
| `version_no` | int | NOT NULL | 产品内单调递增，从 1 开始 |
| `original_filename` | text | NOT NULL | 管理员上传时的原始名，仅用于下载展示 |
| `uploaded_by` | bigint | NOT NULL, FK → admins | |
| `uploaded_at` | timestamptz | NOT NULL | |

**关键约束**

```sql
UNIQUE (product_id, version_no)      -- 产品内版本号唯一
UNIQUE (product_id, id)              -- 供 products 复合外键引用
```

### 历史版本内容重复的处理（业务负责人 2026-08-01 确认）

上传文件的内容判重**只与当前版本比较，不与历史版本比较**：

| 上传内容 | 行为 | 数据层结果 |
|---|---|---|
| 与**当前版本**的 size + SHA-256 均相同 | 拒绝，提示「与当前文件相同」 | 无任何写入：不新增 `pdf_files`、不新增 `pdf_versions`、不移动指针 |
| 与**某个历史版本**相同，但不同于当前版本 | **允许**，正常创建新版本 | 新增 `pdf_versions` 行；`pdf_files` **复用**已存在的同哈希行，不重复落盘 |
| 与任何版本都不同 | 允许，正常创建新版本 | 新增 `pdf_files` + `pdf_versions` |
| 任何情况 | —— | **历史版本只追加，永不删除** |

**为什么允许与历史版本重复**：这是一个真实的业务动作——管理员发现新版有误，希望把旧内容重新发布为当前版本。此时有两条路径：用「恢复历史版本」移动指针，或重新上传那份旧文件。第二条路径在业务上完全合理（管理员手上就有那个文件，未必记得它在历史列表的第几版），不应被系统拒绝。

**数据层的关键后果**：`pdf_files` 按 `sha256` 唯一，因此重复内容**不会重复占用存储**；`pdf_versions` 则会出现多行指向同一个 `pdf_file_id`。这是设计预期，不是异常：

```
pdf_versions:  v1 → file_A
               v2 → file_B
               v3 → file_A     ← 内容与 v1 相同，合法，复用 file_A
```

因此**禁止在 `pdf_versions` 上建 `UNIQUE (product_id, pdf_file_id)`**。这样的约束看似"防重复"，实际会让上述合法场景写入失败。

### 历史版本不可删除、不可修改的数据层保护

「历史 PDF 永久保留，只能查看和恢复，不能删除」（v2 PDF 规则 3、补充验收标准 8）与「历史版本只追加、不修改、不删除」（v2 PDF 规则 6）是 T-08 的核心。**与产品编码同理，仅靠"应用层不提供入口"不足以保证**——一次误写的 DELETE、一段清理脚本、一个未来被加入的"归档"功能都能绕过。

**第一层：应用层无入口。** `pdf_versions` 与 `pdf_files` 只暴露 INSERT 与 SELECT 操作，代码中不存在针对这两张表的 UPDATE 或 DELETE 路径。

**第二层：数据库触发器拒绝更新与删除。**

```sql
CREATE FUNCTION reject_version_mutation() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'pdf_versions is append-only: 历史版本不可删除 (v2 PDF 规则 3/6, T-08)';
  END IF;
  RAISE EXCEPTION 'pdf_versions is append-only: 历史版本不可修改 (v2 PDF 规则 6, T-08)';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_versions_append_only
  BEFORE UPDATE OR DELETE ON pdf_versions
  FOR EACH ROW EXECUTE FUNCTION reject_version_mutation();

-- pdf_files 同样受保护：物理文件记录一旦建立即不可变更
CREATE TRIGGER trg_files_append_only
  BEFORE UPDATE OR DELETE ON pdf_files
  FOR EACH ROW EXECUTE FUNCTION reject_version_mutation();
```

**第三层：权限回收。** 应用连接的数据库角色对这两张表只授予 `SELECT, INSERT`，**不授予 `UPDATE` 与 `DELETE`**。

**为什么 UPDATE 也要禁止，而不只是 DELETE**：修改 `pdf_file_id` 可以让一个历史版本"指向另一份内容"——历史记录看似还在，内容却被掉包。这比直接删除更隐蔽，且同样破坏可追溯性。**只防删除不防修改，等于只锁了前门。**

**为什么文件系统层面也不删除**：`pdf_files` 行不可删除，对应的物理文件同样永不删除。第一期不实现任何孤儿文件清理——不存在孤儿，因为没有任何路径能移除版本引用。

**唯一的例外边界**：数据库备份轮转会删除过期的**备份副本**，那是备份介质上的历史快照，不是业务数据本身。此边界须在运维手册中写明（见 `security-design-v1.md` 第 8 节），避免实现者把"不删除"误推广，或反过来把"备份可清理"误推广到业务表。

**与恢复的区别必须保持清晰**：

| 动作 | 版本表 | 指针 | 审计 |
|---|---|---|---|
| 重新上传旧内容 | **新增一行**（version_no 递增） | 指向新行 | `pdf_upload` |
| 恢复历史版本 | **不新增** | 指向已有旧行 | `version_restore`（含 from/to 版本号） |

两者业务结果相似（当前内容相同），但版本历史与审计记录不同。测试必须分别覆盖，不能因结果相似而合并用例。

### 为什么这样设计

- **恢复历史版本不在此表新增记录**：T-08 明确"恢复只移动当前指针"。恢复动作记录在 `audit_events` 中（含来源版本与目标版本），而不是伪造一条新版本。这样版本表严格等于"上传过的文件序列"，语义干净；"当前指向哪一版"由产品表回答；"谁在何时切换过"由审计表回答。三个问题三张表，互不污染。
- **`version_no` 产品内单调**：给管理员一个稳定的人类可读标识（"恢复到 v3"）。用全局自增 `id` 展示会让管理员看到跳跃的数字。
- **`original_filename` 只用于展示**：绝不参与存储路径或二维码命名。
- **没有 `is_current` 字段**：当前版本由 `products.current_version_id` 单点表达。若在版本表加布尔标志，就出现了两个真相来源，并发下极易出现"零个当前版本"或"两个当前版本"——G-13 正是针对这一点设了断言。**单点指针使这类缺陷在结构上不可能发生。**

---

## 6. 管理员表 `admins`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | bigserial | PK | |
| `username` | varchar(64) | NOT NULL, UNIQUE | |
| `password_hash` | text | NOT NULL | Argon2id，含算法参数与盐 |
| `must_change_password` | boolean | NOT NULL, DEFAULT true | 首次登录强制修改 |
| `password_updated_at` | timestamptz | NOT NULL | |
| `last_login_at` | timestamptz | NULL | |
| `created_at` | timestamptz | NOT NULL | |

配套 `admin_sessions`（服务端会话，支持改密后即时失效）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | uuid / bigserial | PK |
| `admin_id` | bigint | FK → admins |
| `token_hash` | char(64) | 会话令牌的哈希，**不存令牌本身** |
| `issued_at` / `expires_at` | timestamptz | |
| `revoked_at` | timestamptz NULL | 改密或登出时置位 |

### 为什么这样设计

- **表结构支持多行，但第一期只有一行**：v2 明确单管理员、不做多管理员与权限（第一阶段不做第 3 项）。设计成单行表（如固定 id=1）会让未来扩展变成迁移；设计成多行表则只是"当前只有一条数据"。**不建角色表、不建权限表**——那属于第一期明确不做的范围。
- **`must_change_password` 是必需的**：T-07 禁止硬编码默认密码，初始密码由部署时 CLI 设置。没有这个字段，"强制首登修改"就只能靠约定，无法被 G-10 验证。
- **会话存服务端且只存哈希**：密码重置或修改后必须能立即使旧会话失效。纯签名 Cookie 做不到即时吊销。`token_hash` 而非明文令牌，是为了在数据库泄露时不等于会话泄露。
- **没有 `email`、没有找回令牌表**：B-06 与 T-07 明确不提供网页或邮箱找回。**不建这些字段本身就是一种防护**——不存在的入口无法被攻击。

---

## 7. 操作记录表 `audit_events`

只追加。数据库层面应回收该表的 UPDATE / DELETE 权限。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | bigserial | PK | |
| `occurred_at` | timestamptz | NOT NULL | |
| `actor_type` | varchar(16) | NOT NULL | `admin` / `system` / `anonymous` |
| `actor_id` | bigint | NULL | 管理员 id；系统或匿名为空 |
| `action` | varchar(48) | NOT NULL | 见下方动作清单 |
| `target_type` | varchar(24) | NULL | `product` / `version` / `admin` / `import` |
| `target_id` | bigint | NULL | |
| `product_code` | varchar(64) | NULL | 冗余存编码，便于产品被停用后仍可检索 |
| `result` | varchar(16) | NOT NULL | `success` / `failure` |
| `request_id` | uuid | NULL | 串联同一次请求的多条事件 |
| `detail` | jsonb | NULL | 结构化补充，**禁止写入密码、令牌、PDF 内容** |

**必须记录的动作**（T-11）：

`login_success`、`login_failure`、`password_change`、`password_reset`、`product_create`、`product_import`、`product_disable`、`product_enable`、`pdf_upload`、`pdf_upload_rejected`、`version_restore`。

其中 `version_restore` 的 `detail` 必须包含 `from_version_no` 与 `to_version_no`——这是 T-08 明确要求的，也是恢复动作唯一的留痕位置。

`product_import` 的 `detail` 记录三个计数（成功新增 / 重复跳过 / 格式错误）与批次结果；**格式错误导致整批失败时，审计事件仍要写入**——审计记录不是"产品数据"，B-08 的"零持久化"约束的是产品表，不是审计表。这一点必须在实现与测试中明确区分。

### 为什么这样设计

- **不设指向业务表的外键**：审计写入不能因为业务约束（如产品尚未提交）而失败。冗余存 `product_code` 而非仅存 `product_id`，是为了在事后排查时不必反查已停用的产品。
- **`detail` 用 JSONB 而非固定列**：不同动作需要的补充信息差异很大。但**必须有明确的禁写清单**，否则 JSONB 会变成敏感信息的垃圾场。
- **`login_failure` 也记录**：单管理员系统一旦失守即全站失守，登录失败序列是唯一能发现暴力破解的信号（G-10、G-14）。
- **权限层面回收 UPDATE/DELETE**：只靠"约定不修改"在审计场景下没有意义。应用连接的数据库角色对该表只应有 INSERT 与 SELECT。

---

## 8. 关键字段速查

| 关注点 | 字段 | 保证方式 |
|---|---|---|
| 编码唯一不可变 | `products.code` | UNIQUE + CHECK + 无更新入口 |
| 公开不可枚举 | `products.public_token` | 128 位 CSPRNG + UNIQUE |
| 三态判定 | `products.status` + `products.current_version_id` | 先判 status，后判指针是否为空 |
| 恰一个当前版本 | `products.current_version_id` | 单点指针 + 复合外键 |
| 历史不丢失 | `pdf_versions` | 只 INSERT |
| 内容判重 | `pdf_files.sha256` + `size_bytes` | UNIQUE(sha256) |
| 路径安全 | `pdf_files.storage_path` | 由哈希派生，用户输入不参与 |
| 可追溯 | `audit_events` | 只追加 + 权限回收 |

---

## 9. 索引建议

```sql
-- 公开读路径，最热
CREATE UNIQUE INDEX ON products (public_token);
-- 导入判重与管理检索
CREATE UNIQUE INDEX ON products (code);
-- 产品详情页拉版本历史
CREATE INDEX ON pdf_versions (product_id, version_no DESC);
-- 上传判重（先按哈希定位物理文件）
CREATE UNIQUE INDEX ON pdf_files (sha256);
-- 审计检索
CREATE INDEX ON audit_events (occurred_at DESC);
CREATE INDEX ON audit_events (product_code, occurred_at DESC);
```

---

## 9A. 数据库权限保护策略

不变量必须由权限层兜底，而不只依赖应用代码的自律。原则是：**应用连接只被授予它真正需要的动词**，凡是业务规则禁止的操作，在权限层就不存在。

### 角色划分

| 角色 | 用途 | 说明 |
|---|---|---|
| `app_rw` | 应用运行时连接 | 权限最小化，见下表 |
| `app_migrate` | 迁移专用 | 具备 DDL 权限；**仅在迁移期间使用，不用于运行时** |
| `app_backup` | 备份专用 | 全库只读 |
| 超级用户 | 人工运维 | 仅业务负责人经服务器使用，不供应用连接 |

### `app_rw` 的表级权限

| 表 | SELECT | INSERT | UPDATE | DELETE | 理由 |
|---|---|---|---|---|---|
| `products` | ✅ | ✅ | ✅ 仅限 `status`、`current_version_id`、`updated_at` | ❌ | 产品不可物理删除；`code`/`public_token`/`id` 另由触发器锁死 |
| `pdf_files` | ✅ | ✅ | ❌ | ❌ | 只追加 |
| `pdf_versions` | ✅ | ✅ | ❌ | ❌ | 只追加，历史不可删改 |
| `admins` | ✅ | ✅ | ✅ | ❌ | 改密需 UPDATE；账号不删除 |
| `admin_sessions` | ✅ | ✅ | ✅ | ✅ | **运维数据**，过期会话可物理清理 |
| `audit_events` | ✅ | ✅ | ❌ | ❌ | 只追加，不可篡改 |

**列级 UPDATE 权限**：PostgreSQL 支持 `GRANT UPDATE (col1, col2) ON products TO app_rw`。对 `products` 使用列级授权，使应用**在权限层就无法**对 `code` 或 `public_token` 发起 UPDATE，触发器成为第二道而非唯一一道防线。

```sql
REVOKE ALL ON products, pdf_files, pdf_versions, admins, audit_events FROM app_rw;
GRANT SELECT, INSERT ON products, pdf_files, pdf_versions, admins, audit_events TO app_rw;
GRANT UPDATE (status, current_version_id, updated_at) ON products TO app_rw;
GRANT UPDATE (password_hash, must_change_password,
              password_updated_at, last_login_at) ON admins TO app_rw;
GRANT SELECT, INSERT, UPDATE, DELETE ON admin_sessions TO app_rw;
```

### 为什么这样设计

- **`admin_sessions` 是唯一可 DELETE 的表**，且这是刻意的：它是运维数据不是业务数据。把它与业务表放在同一张权限表里对照，正是为了让"哪些能删、哪些不能删"一目了然，避免实现者凭直觉推广。
- **迁移权限与运行时权限分离**：若应用连接自带 DDL 权限，那么所有触发器与权限约束都可被应用自身绕过——防护形同虚设。
- **权限 + 触发器双层**：权限层挡住"应用发起的操作"，触发器挡住"任何来源的操作"（包括误用超级用户执行的脚本）。两层的覆盖面不同，缺一不可。
- **可验证性**：权限配置可直接查询系统表断言，属于 G-10 可判定的检查项，不依赖代码审查。

### 与审计写入的相互作用

`audit_events` 对 `app_rw` **只有 INSERT 与 SELECT**，因此审计记录在权限层就无法被应用修改或删除。这一约束与第 9B 节的审计事务隔离策略配合：审计既写得进去，又改不掉。

---

## 10. 并发与一致性要点（对应 G-13）

| 场景 | 保护手段 |
|---|---|
| 并发创建同一编码 | `UNIQUE(code)` + 捕获唯一冲突转为"重复跳过" |
| 并发导入含相同编码 | 同上；同文件内重复在阶段一去重（首次为候选） |
| 并发上传同一产品 | `SELECT ... FOR UPDATE` 锁产品行**先于**当前版本判重；当前版本读取与 size + SHA-256 比对**全部在锁内完成**，再移动指针 |
| 并发上传**内容相同**的 PDF | 同上。判重结论在锁外读取即已过期，锁内必须重新读取当前版本并复核，否则两个请求会各自创建一个内容相同的版本（详见 `architecture-v1.md` 锁边界一节） |
| 上传与恢复并发 | 同上，指针移动串行化 |
| 导入格式错误 | 阶段一不开写事务；阶段二整体提交，失败即回滚 |

**导入必须是两阶段**：全量校验阶段不持有写事务（避免长事务锁表），写入阶段在单一短事务内完成。若把校验放进写事务，5000 行的校验时间会显著拉长锁持有时间。

---

## 11. 本模型暴露的待决问题

1. **审计事件保留期与访问控制**未定义（T-11 影响项）：只追加意味着无限增长，需定义归档策略，但归档不得成为删除入口。
2. **`admin_sessions` 过期会话的清理**：属于运维数据而非业务数据，可定期物理删除，但需与"不物理删除"原则明确区分边界，避免实现者误推广到业务表。
3. **备份范围必须同时覆盖数据库与文件卷**：两者分离存储，只备份其一会导致恢复后版本指针指向不存在的文件（G-15 需就此设断言）。

### 已于本轮关闭

- **历史版本内容重复**（原待决第 1 项，G-02 降级待办·风险 3）：业务负责人 2026-08-01 确认，规则见第 5 节。
