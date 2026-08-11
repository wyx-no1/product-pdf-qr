# AO Workflow v2 — 完整流程设计

> 本文记录 AO 流程治理语义。案例依据为 PR #18 的实际观测数据。
> 初稿日期：2026-08-05。SUPERSEDED 裁决日期：2026-08-10。

---

## 0. 案例事实

PR #18 的关键提交序列为：

```text
3ef3e03  fix: close admin security audit gaps             ← changes_requested
f3f9eda  fix: close remaining authentication review gaps  ← 当时未审
2556120  test: stabilize concurrent login regression      ← 最后 CODE，当时未审
53f0644  docs: add PR18 review evidence                    ← METADATA head
```

该案例证明：流程若直接用 PR head 作评审锚点，尾随 Evidence 提交会把评审对象从
真实代码状态移到文档状态。流程必须以当前 PR 提交链中的最后一个真实 CODE
提交为代码锚点。

## 1. 治理裁决

业务负责人于 **2026-08-10** 裁决：

> 受信 reviewer 对最终代码版本的 approved，是其已审阅
> `base...final_code_sha` 完整累积差异的责任声明；该最终全量批准隐含接受
> 当前 PR 提交链中被后续 CODE 状态取代的历史 CODE 提交。

因此，非最终 CODE 自动进入 `SUPERSEDED`，不再分别要求 CI、Review 或额外的
人工历史接受节点。原因不是“后续提交一定删除了旧变更”；Git 是累积状态，
中间提交引入的文件可能原样留在最终树中。安全依据是：

1. GitHub 以服务器事实把 review 版本锚定到精确 `final_code_sha`；
2. 受信 reviewer 的 approved 明确承担完整累积差异的审阅责任；
3. gate 机械重建并验证 `base...final_code_sha` 的累积 changed-files，包含
   历史 CODE 引入的路径；
4. final 仍须满足精确 SHA 的三项 CI success 与有效 approved。

GitHub 能证明版本锚定并提供累积差异，不能证明人逐字看过每个文件；完整审阅
是受信 reviewer 的责任声明，不应表述为平台替人证明。若未来改用只展示单个
commit patch 的评审工具，必须重新裁决此前提，未重评前 fail closed。

该裁决只取代“未审 superseded 历史提交的人工接受”路径，不取消真实业务验收、
已知风险接受、门禁定义变更批准、合并或回滚等人类责任节点。

## 2. 完整状态机

### 2.1 统一锚点

全流程以 `(PR 号, code commit SHA)` 为唯一代码锚点。提交先用 Git tree 分类：

| 类型 | 机械判据 | 是否推进代码锚点 |
|---|---|---:|
| `NOOP` | 单父 commit 的 tree 与 parent tree 相同，且 files 为空 | 否 |
| `METADATA` | tree 改变，且每个 file 的新旧路径都在 `docs/evidence/**` | 否 |
| `CODE` | tree 改变，且任一新路径或 rename 旧路径在 Evidence 外 | 是 |

`files=[]` 不能推出 NOOP；tree 或 parent 取证缺失、根提交、多父 merge、
tree/files 矛盾、畸形 rename 字段均失败关闭。rename 必须同时检查
`filename` 和 `previous_filename`，因此把代码或 CI 文件移入 Evidence 仍是
CODE，反向移出亦然；只有 Evidence 内部 rename 才是 METADATA。

### 2.2 Task 状态机

```text
DRAFT ──★人工定义范围──> READY
READY ──> DISPATCHED ──> IN_PROGRESS ──> AWAITING_GATE
                                          ├──> BLOCKED ★人工
                                          └──> COMPLETED
```

Task 跨越多个 Worker 会话；Worker 不是流程索引键。

### 2.3 Worker 状态机

```text
SPAWNED ──> WORKING ──> PUSHED ──> RELEASED
              └──> FAILED ────────────┘
```

### 2.4 PR 状态机

```text
OPEN ──> CI_RUNNING ──┬──> CI_FAILED ──> Worker
                      └──> CI_PASSED
                              ↓
                       EVIDENCE_PENDING
                              ↓
                       EVIDENCE_READY
                              ↓
                       GATE_EVAL ──┬──> GATE_OK
                                   └──> NEEDS_GATE_DECISION ★人工
                              ↓
                       REVIEW_PENDING ──> UNDER_REVIEW
                              ├──> CHANGES_REQUESTED ──> Worker
                              └──> REVIEW_APPROVED
                              ↓
                       UNDER_ADVISORY ──> ADVISORY_DONE
                              ↓
                       NEEDS_ACCEPTANCE ★真实业务验收
                              ↓
                       MERGE_READY ──★人工──> MERGED
```

`NEEDS_ACCEPTANCE` 在此仅表示真实业务验收或已知风险接受，不承载
superseded 历史提交的额外接受。

### 2.5 Evidence 状态机

```text
ABSENT ──> GENERATING ──> GENERATED ──> COMMITTED
              └──> GEN_FAILED ★人工       └──> SUPERSEDED（新 CODE 出现）
```

Evidence 绑定 CODE，不绑定浮动 head。作者提交的普通 `docs/evidence/**` 文件、
PR/Issue 评论或 commit message 均不是 gate 的可信判据。累积评审输入必须由
GitHub Compare 直接重建；配置的服务器认证 numeric user/app identity 签发的
等价输入只有在其净 changed-files 与同一 base/final 的权威 Compare 完全一致时
才可采用，并同时绑定 base、final SHA、完整 changed-files 及最新成功状态。

### 2.6 Review 状态机

```text
（当前 PR 提交链中的每个 CODE）

非最终 CODE ──> SUPERSEDED
最终 CODE：
NOT_STARTED ──> QUEUED ──> RUNNING ──┬──> DELIVERED(approved)
                                      ├──> DELIVERED(changes_requested)
                                      ├──> FAILED ★人工
                                      └──超时──> STALLED ★人工
```

`final_code_sha` 是最后一个真实 CODE；尾随 METADATA/NOOP 不改变它。final
只有同时满足以下条件才通过：

- `quality`、`database`、`container` 在 final SHA 上的权威最新状态均为
  `success`；
- 受信 GitHub numeric reviewer 的、未 dismissed/pending、精确锚定 final SHA
  的最新 review 正文可唯一解析为 approved；
- 服务器认证的累积评审输入精确绑定 `base...final_code_sha`，提供该范围的净
  changed-path 集合并覆盖最终 tree 仍保留的 PR 新增 CODE 路径，且提交链、
  父关系、tree、merge base、full SHA 与 head 快照一致。

较早 SHA、METADATA SHA 或 NOOP SHA 上的批准不能覆盖 final；较新的可信
changes_requested 会反转较早 approved。读取期间 base/head 推进、API 畸形或
分页不一致一律失败关闭，不返回部分 PASS。

### 2.7 SUPERSEDED 与“不允许静默跳过”

非 final CODE 自动标为：

```text
SUPERSEDED(
  superseded_by=<当前链中的下一 CODE 完整 SHA>,
  prior_verdict=<受信 exact-SHA 历史 verdict 或 missing>
)
```

其自身 CI/review 只用于审计，不阻断；`superseded_by` 只能是下一 CODE，不能是
METADATA、NOOP，也不能把所有历史提交直接指向 final。force-push 后不在当前
commits API 链内的旧 SHA 不参与当前结果。

这不是静默跳过：gate 必须逐提交输出 `SUPERSEDED`、下一 CODE 与可信历史
verdict，并以 final 的完整累积批准承接责任。final 仍显示 `CODE(final)` 和完整
SHA；METADATA 与 NOOP 输出机械分类原因。任何 final 缺口都关联 final SHA，
状态为 `REVIEW_GAP`。

### 2.8 Merge 状态机

```text
MERGE_READY ──★人工──> MERGING ──> MERGED
                          └──> MERGE_FAILED ★人工
```

Evidence 绑定不可被 squash/rebase 重写时，必须使用保持证据 SHA 的合并方式。

## 3. 自动触发规则

### 3.1 Evidence

仅新 CODE 且其所需 CI 成功、尚无可信 Evidence 时生成；METADATA/NOOP 不触发。
生成、校验和 attestation 必须绑定不可变 SHA 并保持幂等。

### 3.2 Review

Review target 必须是 `final_code_sha`，输入必须是 `base...final_code_sha` 完整
累积差异，不得取浮动 PR head 或只取 last-commit patch。

### 3.3 重新 Review

| 触发 | 动作 |
|---|---|
| 新 CODE | 旧 CODE 自动 SUPERSEDED；对新 final 发起全量 Review |
| METADATA/NOOP | 不推进 final，不重新 Review |
| STALLED | 不自动重跑，进入人工处置 |
| 环境/瞬时失败 | 最多自动重试 2 次，之后人工 |
| 门禁定义变更获授权人 bootstrap 批准 | Review 仍锚 final 完整累积差异 |

### 3.4 其他自动动作

- final SHA 三项 CI 取证；
- Git tree、commit 链、head 首尾快照与累积 changed-files 校验；
- 受信 exact-SHA Review API verdict 解析；
- Advisor 调用、超时检测、状态对账与通知；
- 逐提交可审计分类输出。

### 3.5 CI observation

同名 Checks 先于 Statuses。较新非终态 rerun 不能掩盖已有终态；较新终态失败
取代旧成功；同时间以服务器 observation ID 决胜。只有字符串严格等于
`success` 才通过。

### 3.6 跳过检测

每次 head 推进后重建当前提交链。历史 CODE 由下一 CODE 显式 supersede，
最终 CODE 必须满足 §2.6；METADATA/NOOP 不得成为代码取代依据。该检测不再
创建 superseded 专用人工接受节点。

## 4. 人工介入节点

| ★ | 节点 | 为什么不能自动 |
|---|---|---|
| 任务范围定义 | 业务边界需要判断 |
| 门禁定义变更批准 | 门禁不能批准自己的信任根 |
| 真实业务验收 | CI/Review 不能代替真实运行验收 |
| 已知缺口接受 | 风险责任必须由授权人承担 |
| STALLED/环境失败处置 | 需判断重跑、换环境或终止 |
| 合并、终止、回滚 | 不可逆或高影响 |

门禁/Trusted Execution Surface 修改必须由门禁责任人或仓库维护者以人类身份，
对最终 SHA、完整 Trusted Surface diff 和三项 CI 留下服务器认证的明确
bootstrap approval/override。Coordinator/Agent 只汇总业务裁决、旧 gate 结果、
`requires-re-review` 和最终证据，不能批准自身。中间返工不得要求改写历史，
也不得静默放行。

## 5. 环境与恢复

AO Runtime 与 Business Runtime 隔离。AO 工具需要 `git`、`gh`、Python 3.12、
`uv` 和必要的 Docker CLI，但不接触业务数据库或业务文件；业务运行时不持有
AO/GitHub 写权限。依赖缺失立即失败，不以降级结论继续。

Review 超过 30 分钟无可解析 verdict 进入 STALLED。网络/API 失败可有限重试；
取证缺失、JSON/分页畸形、tree/files 矛盾、身份或 SHA 不匹配立即
indeterminate/error。所有自动动作以 `(PR, code commit SHA)` 幂等寻址。

## 6. 交付与后续验证

本裁决的实现 PR 改动 `scripts/ao/**`，属于 Trusted Execution Surface，必须走
§4 的人类 bootstrap，不能由新 gate 自证。合并后从默认分支以同一冻结 fixture
只读重放 PR #37：历史 `3ce074…` 应为 SUPERSEDED，下一 CODE 为 `0141f56…`，
final `fd7024…` 在三项 CI success 与 exact-SHA approved 下 PASS；不得修改
PR #37 或其历史。

## 7. 设计原则

自动化负责搬运、取证与机械检查；人类负责业务判断与后果。门禁默认失败关闭，
作者可写内容不进入信任根，元数据不得改变代码锚点，最终批准不得解释为单
commit patch。
