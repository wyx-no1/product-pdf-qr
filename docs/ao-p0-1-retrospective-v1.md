# AO P0-1 阶段复盘 v1

> 状态：阶段复盘，**不进入新开发**。
> 覆盖范围：Issue #11 / PR #12、Issue #13 / PR #15，截至 2026-08-04。
> 本文档只总结已完成工作，不提出新的实现要求。

---

## 1. P0-1 最初目标

> **Evidence 可信 + Advisor 可信。**

拆开是两个独立命题：

| 命题 | 含义 |
|---|---|
| **Evidence 可信** | 审核所依据的证据，本身是可验证的，而不是任何人都能编造的一份说明 |
| **Advisor 可信** | 审核意见确实是在**应该被审的那份代码**上形成的，而不是在当前工作目录或 `main` 上 |

两者的共同点：**都不能靠约定，只能靠机制。**

---

## 2. 已完成

| 阶段 | Issue | PR | 合并 | 内容 |
|---|---|---|---|---|
| **Evidence Trust Layer** | #11 | **#12** | `8849cf6`（merge commit） | Evidence Snapshot 自动生成 + Advisor Workspace Resolver |
| **Advisor Validation Layer** | #13 | **#15** | `33b78cf` | Advisor opinion validation（门禁侧校验） |

### 2.1 PR #12 — Evidence Trust Layer

26 文件，+10727 / −8。经**六轮信任边界 Review**，建立四层防护：

| 层 | 内容 |
|---|---|
| 1 | **P0 安全隔离** —— `workflow_run` 信任拆分：可执行代码取自默认分支，PR 检出物只作数据 |
| 2 | **Evidence 完整性** —— 服务端 attestation + 内容重算 |
| 3 | **Trusted CI Definition** —— 门禁定义 hash 比对 |
| 4 | **Trusted Execution Surface** —— 按**实际执行路径**判定可信文件范围 |

合并采用 merge commit，以保证 Evidence 绑定的 code commit `fbe0bae` 在 main 上仍可解析——**squash 会让该 SHA 消失，本 PR 亲手建立的可信链会在自己合并的瞬间断掉**。

### 2.2 PR #15 — Advisor Validation Layer

4 文件，+839 / −118。实现 Issue #13 第一版四项，并修复一处绕过（见 §4.1）。

采用**路径 B（门禁侧校验）**：

| | |
|---|---|
| **不控制** | 谁启动 Advisor |
| **控制** | 什么 Advisor 结果可以被 G-10 接受 |

理由：AO daemon 位于仓库之外，仓库代码无法可靠强制其调用方式。**与其试图控制调用方，不如让不合规的路径产不出可被接受的结果。**

opinion 验证共 7 项测试：1 条正常路径（反退化）+ 6 条拒绝路径。

### 2.3 配套治理产出

| 产出 | 说明 |
|---|---|
| `docs/governance-change-control-v1.md` | 因 PR #12 停滞而产生——机制能标记 `requires-re-review`，但标记之后由谁审、通过标准是什么，此前没有规则 |
| Issue #16 | Review 生命周期错误绑定 Worker 生命周期（**记录，未修复**） |

---

## 3. 架构演进

```
代码
  → Evidence          （把「审什么」固定下来：PR + commit SHA + CI 绑定）
  → Trusted Surface   （把「门禁定义本身」纳入保护）
  → Advisor           （在与 Evidence 绑定的 commit 上审核）
  → Validation        （只接受可验证的审核结果）
```

### 贯穿全程的一条主线

> **被约束方不得控制约束本身。**

这条不是事先设计出来的，是**被六轮 Review 逐层逼出来的**。每一轮都是同一个问题换一个位置出现：

| 轮次 | 问题 |
|---|---|
| 1 | 谁的代码在写权限下执行（pwn request） |
| 2 | 门禁挂在哪个 commit 上 |
| 3 | skip 判据验证的是什么性质 |
| 4 | 门禁定义本身是否受 PR 控制 |
| 5 | 工具配置能否被嵌套文件覆盖 |
| 6 | 解释器启动路径是否被覆盖 |
| 7 | 治理规则变更由谁审核（→ `governance-change-control-v1.md`） |
| 8 | Advisor 在哪份代码上审核（→ PR #15） |

**每次修法的方向也一致：结构式防御优于枚举式防御。**

- 枚举式：列出所有可能的位置。清单永远可能漏，新增目录就要重审。
- 结构式：让「这件事发生」本身不可能。例如 `--config` 钉死消除就近发现、可执行代码固定取自 base 分支、内容重算、服务端 attestation。

---

## 4. 关键问题与处理

### 4.1 metadata-only Evidence 绕过

**问题**：Advisor opinion validator 仅校验「`metadata.md` 在预期路径存在」，于是**仅含 metadata 的目录**会被当作完整 Evidence Snapshot。

**性质**：判据验证了**错误的性质**——检查的是「文件在不在」，而需要检查的是「这份 Evidence 是否可信」。

**处理**（commit `17d317c`）：校验恰好五个 Evidence 文件、从 PR / CI / review 重算内容、验证 `AO / evidence-snapshot` attestation。**直接复用 `evidence.py` 的 `verify_evidence_head`，未自造判据，`evidence.py` 本身未改动。**

> ⚠️ **同类模式第二次出现。** PR #12 第三轮的 `_evidence_only_head()` 判据是「该提交只碰了 `docs/evidence/**`」，同样只验证「改了哪里」而非「是否可信」。
> **两次修法一致：不自造判据，接入已有可信链。**
> 这是本阶段最值得记住的一条经验——**「改了哪里」永远不能替代「是否可信」**。

### 4.2 Trusted Gate 变更

**问题**：`TRUSTED_CI_TREE_PATHS` 包含整棵 `scripts/` 与 `tests/`，因此**任何 AO 工具改动都会改变定义 hash**，被判定为 `requires-re-review`。

**后果**：AO 自身的每一次开发都成为 L3 变更，都需业务负责人逐次显式批准。PR #12 与 PR #15 都是如此。

**处理**：**不加自我豁免。** 两个 PR 都如实自我标记，均未通过修改 `trust.py` 清单来规避。

**关键区分（每次批准材料都必须写明）**：

> **在 surface 内 ≠ 门禁被削弱。**
> hash 变化反映「surface 内的文件变了」，而非「门禁变松了」。两个 PR 的门禁参数（覆盖率阈值、mypy strict、ruff 规则）、可信清单、Evidence 核心机制**均为零改动**。

**代价如实记录**：这使 AO 开发流程显著变重。若日后认为过重，调整 surface 定义**本身也是 L3**，同样须走 Governance Change Control，不能顺手改掉。

### 4.3 Review 绑定问题

**问题**：`ao review` 的四个子命令（`trigger` / `ls` / `submit` / `cancel`）**全部以 worker-session-id 寻址**，没有以 PR 寻址的入口。

**实测到的失效模式**：

| 模式 | 状态 |
|---|---|
| **A. 绑定从未建立** | ✅ 已实测——PR #15 一度无法启动 review，而会话 `product-pdf-qr-7` **仍然存活** |
| **B. 绑定随会话结束丢失** | ⚠️ 未证实——缺少「会话已终止 + PR 仍 OPEN」的样本 |

**结构性问题**：用**生命周期短于 PR** 的句柄（Worker 会话）去索引长生命周期的对象（PR）。

**处理**：记录为 **Issue #16**，未修复。修复涉及仓库之外的 `ao` CLI。

> 本阶段就 PR #15 而言，绑定在实践中已解开（`ao review ls` 由 `No reviews found` 变为 `up_to_date`），**但架构缺口本身仍在**。

### 4.4 附带记录的治理缺口（均未解决）

| 缺口 | 状态 |
|---|---|
| 治理文件保护是**软约束** | 无 CODEOWNERS / 分支保护 / CI 断言；`CLAUDE.md` 自身也在被保护清单内 |
| 批准链由 Orchestrator **转述** | 非业务负责人本人的 GitHub 操作 |
| **Reviewer 角色从未正式定义** | PR #15 批准时以 AO 内部记录作为**过渡**审核依据，GitHub `reviewDecision` 仍为空 |
| `agent-orchestrator.yaml` 从未验证 | 不确定 AO 是否真的读取它 |
| AO 工具代码**无覆盖率下限** | `coverage.run.source = ["product_pdf_qr"]`，`scripts/ao/**`（`workspace.py` 已达 1171 行）不计入 90% 门槛 |
| `requires-re-review` 时**不产出任何快照** | 机制既不签发可信 Evidence，也不产出「不可信但可供人工审阅」的快照，而人工审核恰在此时最需要证据包 |

---

## 5. 明确后置

以下**不在 P0-1 范围**，已作为后续增强项记录：

| 后置项 | 影响 |
|---|---|
| **Resolver Attestation**（不可伪造） | 决定第一版能力边界——见 §6.2 |
| **身份签名** | 同上 |
| **自动授权**（P0-3） | —— |
| **手机通知**（P2-8） | —— |

后置**不等于遗忘**：前两项的缺失直接决定了 Advisor Validation Layer 当前能验证什么、不能验证什么，已写入 `scripts/ao/README.md` 的能力边界声明。

---

## 6. 记录原则

> **发现风险不等于立即阻塞。必须区分当前真实风险与未来假设风险。**

这是本阶段最重要的方法论产出。以下是它在本阶段的三次具体应用。

### 6.1 判定「不做」也是结论——递归扫描

第六轮 Review 提出：是否应递归扫描所有目录下的 `conftest.py`？

**判定：不做。**

依据：pytest 的发现边界由 `pyproject.toml` 的 `testpaths = ["tests"]` 决定，而 `pyproject.toml` **本身就在 Trusted Execution Surface 内**——PR 无法在不改变可信 hash 的前提下悄悄扩大发现范围。因此 `src/conftest.py` 这类文件**在当前配置下不可达**。

继续加扫描会：增加维护面、让更多正常 PR 被标记需重审，**却不减少任何真实风险**。

> 文档中的原话：would **degrade the mechanism into an always-manual gate without increasing protection**。

**六轮里唯一结论是「不改代码」的一轮**，改为把「为什么不做」论证清楚并写入文档。

### 6.2 如实陈述能力边界——不制造安全假象

Advisor Validation Layer 第一版的记录由**本地 Resolver 产生，没有外部公证方**，与服务端 attestation 有本质区别：

| 断言 | 可否机械验证 |
|---|---|
| 审核代码版本与 Evidence 一致 | ✅ 可验证 |
| 生命周期记录存在且自洽 | ✅ 可验证 |
| **workspace 确实由 Resolver 创建** | ⚠️ **不可强验证** |

**威胁模型**：第一版防的是「误用当前工作目录」「默认读 main」「审错代码版本」这类**流程性失效**，**不防**具备仓库写权限者的蓄意伪造。

该边界已如实写入 `scripts/ao/README.md`，**未被描述为防伪造能力**。

依据的是 PR #12 第三轮已确立的原则——**制造安全假象比没有防护更危险**（当时的原话：「committer 身份不是证明」，因为 `git config user.name` 是自由文本）。

### 6.3 区分「现存弱点」与「本 PR 引入的风险」

批准材料中必须分开两件事：

- **拒绝本 PR 会保留的现存弱点**（例如 main 上 `Makefile` 未钉死配置时，任何人加 `src/ruff.toml` 即可削弱 lint 而 CI 仍绿）
- **批准本 PR 才会引入的新风险**（例如 `evidence-publish.yml` 带来的写权限面，该 workflow 在 main 上原本不存在）

因此 PR #12 的批准材料中明确写了：**「拒绝是安全的选项」**——代价是自动化停滞与保留一个已知弱点，而**不是**引入风险。

> 把两者混为一谈，会让「不批准」显得比实际更危险，从而对批准形成不当压力。**批准材料的作用是支持判断，不是推动通过。**

### 6.4 三条可复用的判据

| 判据 | 说明 |
|---|---|
| **可达性** | 该风险在**当前配置**下是否真的可达？不可达的路径不值得用防护换取流程成本 |
| **谁能触发** | 风险需要什么权限？超出威胁模型的攻击者（如本项目唯一的仓库所有者）不应驱动设计 |
| **防护是否会退化机制** | 若一项防护会让正常变更也持续需要人工审核，它可能在**取消**机制而非强化它——因此每层防护都配了反退化测试 |

---

## 7. 阶段状态

| 项 | 状态 |
|---|---|
| Issue #11 / PR #12 | ✅ 完成并合并 |
| Issue #13 / PR #15 | ✅ 完成并合并 |
| Issue #14 治理设计 | ✅ 设计交付（`governance-change-control-v1.md`），实施后置 |
| Issue #16 Review 生命周期 | 📋 已记录，未修复 |
| P0-1 整体 | ✅ **完成** |

**本文档不提出新的实现要求。** 后续工作是否推进、以何顺序推进，由业务负责人决定。
