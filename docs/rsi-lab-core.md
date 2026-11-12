# RSI-Lab：通用内核、能力接口与任务计划

RSI-Lab 的内核负责可追踪执行、Lab/Meta 协作和可恢复的研究状态。仓库提供通用控制面与一个合成 CSV 能力示例；任务宿主通过接口注册自己的输入语义、计算与成功条件。[系统设计](system-design.html)仍是完整目标规格，自动工程发布、完整 provider 输入认证和记忆语义支持检查等目标不能视为已全部实现。

| 层次 | 代码位置 | 职责 |
| --- | --- | --- |
| 通用核心 | `rsi_lab/` | 工作与效果账本、Lab/Meta 调度、持久会话、独立工作记忆、日志、Scientific Memory、预算、验收和恢复 |
| 通用生产者 | `rsi_lab/producers/` | 按登记请求交付冻结来源、用户回复与工程产物 |
| 可替换能力包 | `rsi_domains/tabular/` | 输入契约、单位与身份检查、实际聚合、结果核验和任务指标 |
| 本次任务的计划 | `rsi_lab/plans.py` 的 `TaskPlan` | 冻结目标、输入、协议、指标、重复评价、成功条件、实验臂、完整分片与预算 |

核心不导入具体能力包，也不固定任务对象、指标方向或最低结果数量。使用者在独立运行目录保存输入、事件和产物；仓库不附带运行归档。

## 接入能力

`rsi_lab.adapters.Adapter` 要求宿主明确注册身份、版本、Effect 类型、输入/输出契约及适用范围，并提供以下回调：

- `prepare`：验证请求并生成具体执行输入。它必须没有副作用：控制器在接纳工具请求时也会调用它试运行，被拒绝的输入在接纳阶段即被退回。
- `reservations`：为具体输入计算资源预约。
- `execute`：执行已持久保存且处于派发状态的调用，返回实际结果和成本。
- `publication`：把核验结果映射为原始事实、来源陈述、验证结论与应用结果。
- 可选 `measure`：产生有界任务指标，内核保留原始执行凭据，不解释分数方向。

`Runtime(..., adapters=[...])` 接收宿主选择的能力；`execute_adapter(adapter_id, inputs, trigger_id=...)` 负责调用前预约、保存回执与发布。适配器身份、版本、回调与实现 hash 绑定到执行，恢复时不会按任务名称猜测实现。每个回调的代码 hash 覆盖其字节码、名称与常量，不随这段代码所在的文件名或行号变化，也不随进程内存中的其他内容变化；列出的源文件仍按绝对路径和整个文件内容绑定，因此移动模块或编辑这些文件中的任何内容都会改变描述符，变更前已准备的 Effect 会被报告为 `blocked_adapter`。

未加载、版本不同或实现不匹配的适配器不能冒充原执行者。尚未生成发布材料时，需要匹配实现核验原始回执；发布材料已保存后，通知和唤醒可直接恢复。未知调用保留对账责任，不能因为换包而重发。

宿主注册工具与动态建设新能力是不同授权路径。需要验收的能力不能仅靠导入模块进入可调用目录；工程生产者提交产物后，由控制器检查真实验收材料。模型提议不能导入任意模块、替换回调或给自己授予权限。本地验收与分支 runner 使用独立进程和临时目录，不声称提供 OS 或网络安全沙箱。

## TaskPlan 冻结任务条件

计划 schema 为 `rsi_task_plan_v1`。

| 字段 | 含义 |
| --- | --- |
| `task_id`、`goal` | 本次任务的身份与目标 |
| `inputs`、`items` | 原始输入 hash 与全部评价对象身份 |
| `protocol`、`capability_versions` | 方法参数、协议与能力版本 |
| `metrics` | 指标名、单位、阈值、方向、聚合方法与全部重复评价身份 |
| `success` | 使用的指标与最少达标对象数 |
| `arms`、`shards` | 实验臂、策略版本、完整对象分片与输出位置 |
| `budget`、`stop_rule`、`missing_rule` | 资源、停止规则与失败/缺失/未开始工作的处理 |

`register_task_plan` 在相关执行前登记，`load_task_plan` 用不可变注册事件核对内容。结果产生后不能更换阈值、最低数量、重复评价集合或输入身份。所有实验臂覆盖全部计划对象，失败与未开始工作仍保留在结果中。

`select_items` 按冻结指标评价所选实验臂的全部对象。即使达到最低数量，未完成的计划对象仍使结果保持 `incomplete`。这是契约选择器：原始输出真实性必须先由能力适配器核验，不会把任意分数表认证为实际执行结果。

最低数量、指标方向与重复评价身份均由任务固定。例如 CSV 示例要求两个站点的聚合误差为零，重复身份是 `aggregation`；其他任务可使用其他明确条件。方法改变结果语义时须创建新协议与研究身份，不能把不同方法的结果混入原冻结计划。

## Scientific Memory 与独立工作记忆

Lab/Meta 的持久会话提供工作连续性，各自的工作记忆保存计划、尝试与未决问题。共享 Work/Effect 账本保存已承诺动作、执行结果和费用。Scientific Memory 保存有来源约束的研究状态，三者不能互相替代。跨角色协作通过显式 Handoff/Work，不能直接读写另一角色的私有工作记忆。

启用冻结配置中的 `research_features.scientific_memory` 后，`ScientificMemory` 在同一 SQLite 的 registration 表保存六类对象：`question`、`evidence`、`claim`、`investigation`、`method_experience` 与 `decision`。修订只追加，head 指向当前版本，来源正文与执行回执仍以原有事实/blob 为权威。它不是一个预填研究结果的独立文件，也不依赖外部向量数据库。

| 接口 | 用途 |
| --- | --- |
| `get(object_id, revision=None)`、`read_history(...)` | 精确读取对象版本与历史 |
| `lookup_exact(identities, conditions)` | 按宿主提供的规范身份与条件查找已有记录 |
| `prepare_context(role, evidence_ids, query=None, budget=16384)` | 选择相关对象及来源闭包，按预算构造本轮上下文 |
| `apply_updates(effect, packet, response)` | 校验本轮实际交付、来源引用和预期修订，接纳合法提议 |
| `mark_source_changed(...)`、`review_report(...)` | 记录来源变化并报告需要复核的解释 |
| `export_bundle()`、`import_bundle(...)` | 通过显式版本包迁移共享科研状态 |

对象身份与条件由受信 `scientific_identity_provider` 提供。模型解释默认保留待支持检查状态；保存合法对象不等于完成语义蕴含验证。新来源与负结果可登记为共享证据，但不会自动变成普遍适用的方法经验。精确源码见 [Scientific Memory](../rsi_lab/scientific_memory.py)，会话、日志与恢复边界见[持久 Agent 说明](persistent-agents.md)。

## Meta 的发现与复核

Meta 使用 discovery 与 disposition 模式，共享问题账本。前者从目标、当时可见事实、来源和能力说明提出问题及区分解释的检查；后者推进查询、重规划、提问、能力建设、试验或明确等待。

领域检查表是工具能力，不是发现模式的完整问题空间。仅复述预先列出的缺口不能证明自主发现。研究模式的 Meta→Lab 建议由来源绑定，实际采用、工具执行、测量与复核分别留痕。推荐类别与最终复核语义由任务宿主提供，内核不推断任务是搜索、最大化或最小化。

## Meta 调整 Lab 编排

在运行配置里声明 `orchestration` 后，控制器会测量 Lab 有没有产出。没有产出时它叫醒 Meta，Meta 可以改 Lab 的四项设置，改动保留还是撤回由代码判定。不声明的运行行为和请求包字节都不变。

```json
"orchestration": {"no_result_steps": 4, "max_changes": 2}
```

`no_result_steps`（下称 N）取 2 到 64，`max_changes` 取 0 到 20，两个键都必须写，出现其他键即拒绝。`init` 和 `Runtime` 都做这个检查，运行开始后不能修改。`max_changes` 为 0 时只发信号，Meta 不能改设置。

**步骤与新结果。** 一次 Lab 模型调用算一步。模型服务明确失败、从未派发就被取消、仅做最终复核的调用不算。Lab 请求的工具成功发布，且至少一条事实的内容在更早的事实里没有出现过，这一步才算“新结果”。重复内容、提议被拒或被阻塞、工具拒绝输入、工具明确失败、工具调用在派发前被取消、`no_action` 和 `propose_issue` 都算没有新结果。调用、提议或工具结果还没结清的步骤算未结束。

**信号。** 最近一个新结果之后的步骤构成一段无结果期，用第一步的 Effect 标识命名。期内步骤都结束后，满足以下任一条件就发信号：

- 期内满 N 步；
- Lab 非正常停下，即最后一个 Lab Work 的提议被拒或被阻塞，且修复机会已用完，或它请求的工具明确失败，或它请求的工具调用在派发前被取消。

信号是一条 `raw_fact`，`provenance.source` 为 `controller_orchestration_signal`，内容包括原因（`no_result_streak` 或 `lab_stopped`）和各步的记录。随后控制器用 `observe(..., 'coded_anomaly', ...)` 叫醒 Meta 发现。同一段无结果期最多发一次。有改动在试用、或运行已进入收尾时，不发信号。当前信号出现在 Meta 请求包的证据里时，它是必需证据，不会被分页或省略，`task.constraints.orchestration.signal_fact_id` 给出它的标识；证据里没有它的请求，这个字段是 `null`，这个 Issue 不能改设置。期内有步骤未结束时信号会推迟，`runtime.status()['orchestration']['unfinished_steps']` 列出在等的步骤。

**四项设置。** 都只作用于 Lab。

| 设置 | 作用 | 上限 |
| --- | --- | --- |
| `guidance` | 写进 Lab 请求包的 `task.constraints.orchestration.guidance` | 2000 字符，`null` 表示没有 |
| `step_digest` | 在 `task.constraints.orchestration.recent_steps` 附上 Lab 最近几步的记录 | 16 |
| `recent_results` | 把最近几条工具发布的事实作为可选证据交给 Lab，放不下时分页或省略 | 16 |
| `disabled_tools` | 从 Lab 的能力目录里拿掉这些能力 | 8 个 |

每个 Lab Effect 在 `metadata.orchestration.change_id` 记下当时生效的改动。Lab 的提议按这一步记录的设置校验，所以请求已停用的工具会被拒。已经排队的工具调用照常执行，Meta 自己请求这些工具也不受影响。

**改动。** Meta 处置时可用 `propose_lab_settings` 提交完整的四项设置和 `rationale`。只有 `max_changes` 大于 0 时，这个动作才出现在处置模式的 schema 里；这个开关写在每个 Lab 和 Meta Effect 的 `metadata.orchestration.lab_settings_action`，`CodexExecutor` 据此重算 schema。接纳时代码检查：

- 命令引用了当前无结果期的信号事实；Lab 已经又出了新结果的，信号过期；
- 没有正在试用的改动，已发布次数小于 `max_changes`；
- 取值在上限内，停用的能力在目录里，设置与当前生效的不同。

接纳后，改动记为 `registration`（`kind='orchestration_change'`，状态 `trial`），Issue 挂起在这个改动上，截止时间是提议事件时间加 `wait_timeout_seconds`。如果 Lab 已经停下（没有就绪或已租用的 Lab Work，最后一个 Lab Work 自己的那一步已经结束），控制器补一个 Lab 步骤，它的输入带上信号事实。最后一步还没结清时不补；这一步之后 Lab 没有接着跑、也还没有任何一步用上这次改动时，`advance()` 再补。每个改动最多补一次。

**判定。** 只看记录了这个改动的 Lab 步骤，按顺序取第一条成立的：

| 情况 | 结果 | 原因码 |
| --- | --- | --- |
| 其中一步出了新结果 | 保留 | `new_result` |
| 已有 N 步结束，都没有新结果 | 撤回 | `no_result` |
| 记录了这个改动的一步让 Lab 非正常停下，或 Lab Work 因容量阻塞 | 撤回 | `lab_stopped` |
| 到截止时间，或运行进入收尾 | 撤回 | `no_conclusion` |

其余情况继续等，不写任何事件。结算写一条 `validation` 事实（`provenance.source='code_settlement'`），内容固定带 `benefit_claim: "not_evaluated"`。Issue 还开着时，这条事实加进它的证据，然后更新依赖，Meta 处置被叫醒。撤回只影响之后构建的 Lab 请求包；这次改动下产生的事实、费用和回执都保留，也不补 Lab 步骤。保留的设置一直生效。

**接入。** `run_until_wait` 每轮先调用 `runtime.orchestration.advance()` 再 `tick`，`finalize` 登记收尾后也调用一次。不经过 `run_until_wait` 的宿主要自己调用 `advance()`，命令行的 `tick` 子命令不调用它。所有写入都走 `Store.mutate`，键里只有标识，崩溃后重复调用返回已提交的结果。

**局限。**

- 判定只说明 Lab 又出了结果，不证明科研收益。
- 四项是一起判定的，分不清是哪一项起了作用。
- 工具输出带时间戳或随机数时，重复请求也会被算成新结果。
- Lab 已在会话里读过的内容，撤回后也抹不掉。
- 有些步骤不会自己结束，例如工具成功但结果发布被拒（驱动报告 `blocked_adapter`）。它所在的无结果期在宿主修好之前不发信号。
- 宿主排了多条 Lab 链时，是否“非正常停下”只看最新的一条。

设计与理由见 [设计文档](superpowers/specs/2026-11-10-meta-orchestration-policy-design.md)，实现见 [Orchestration](../rsi_lab/orchestration.py)，验收见 [测试](../tests/v2/test_orchestration.py)。

## 启动前检查

科研功能是否生效，取决于运行配置里的键和传给 `Runtime(...)` 的参数。漏掉其中一项时运行照常启动，内核测试照常通过，只是发给模型的请求包里没有对应内容。宿主应该在构造 `Runtime` 之后、第一次模型调用之前调用 `preflight`，把这次运行应有的功能写成期望；不满足时在这里报错，不会带着缺口开始调用模型。

| 功能 | 由什么启用 | 启用后模型能看到什么 |
| --- | --- | --- |
| Scientific Memory | 运行配置 `research_features.scientific_memory` 为 `true` | `task.scientific_memory`，响应字段 `scientific_memory_updates` |
| 领域身份 | `Runtime(..., scientific_identity_provider=...)`，传一个函数。不传时 Scientific Memory 照常工作，但发布的结果没有领域身份 | 不进请求包 |
| Meta→Lab 反馈闭环 | 运行配置 `research_features.meta_lab` 为 `true` | `task.constraints.research_loop`；Meta 多出动作 `recommend_change` 和响应字段 `improvement_reviews`，Lab 多出响应字段 `recommendation_responses` |
| 领域指标 | 适配器的 `measure` 回调，并且反馈闭环已启用。没有时闭环的测量只有执行凭据 | 不进请求包 |
| 推荐类别与复核指引 | 运行配置 `research_policy`，至少一个字段 | `task.constraints.research_policy` |
| Meta 调整 Lab 编排 | 运行配置里有 `orchestration` 块 | `task.constraints.orchestration`；`max_changes` 大于 0 时处置模式多出动作 `propose_lab_settings` |
| 工具 | `Runtime(..., adapters=[...])`，命令行用 `--adapter-factory`。`requires_acceptance=True` 的适配器还要有验收记录 | `task.capability_catalog` |

两个 `research_features` 开关只认 JSON 布尔值 `true`，写成 `"true"` 或 `1` 不生效。运行配置在 `init` 时冻结，续跑沿用同一份配置，所以缺的键只能靠新建运行补上；`Runtime(...)` 的参数则每次构造都要传。

```python
runtime = Runtime(store, run_id, adapters=[make_adapter()], scientific_identity_provider=identities)
expect = {"scientific_memory": True, "identity_provider": True, "meta_lab": True,
          "orchestration": True, "capabilities": ["your_domain.tool"]}
runtime.preflight(expect)                          # 构造之后：核对配置与参数
runtime.enqueue_lab("start")
for work in runtime.controller.ready(run_id):      # 第一次模型调用之前：再核对就绪 Lab Work 的实际请求包
    if work["kind"] == "lab":
        runtime.preflight(expect, work_id=work["id"])
```

`make_adapter`、`identities` 和 `your_domain.tool` 是宿主自己的占位符，`capabilities` 里写适配器的 `adapter_id`。两点容易出错：

- 带 `requires_acceptance=True` 的适配器要先完成验收才进能力目录，仓库里的 `tabular.aggregate_energy` 就是这样。验收之前把它写进 `capabilities` 会报错。
- 续跑时不要拿 `enqueue_lab` 的返回值去核对请求包。同一个触发再次登记时，它返回第一次登记时的记录，那个 Work 可能早已完成。从 `controller.ready(run_id)` 取当前就绪的 Lab Work；没有就绪的，就只做配置层的核对。

| 接口 | 作用 |
| --- | --- |
| `runtime.feature_report()` | 报告三项功能各自是否在配置里声明（`declared`）、服务是否已创建（`service`），是否传入了可调用的身份提供者，配置里是否有 `research_policy`，已加载的适配器及其是否进入能力目录，以及 Lab、discovery、disposition 三种模式下模型可用的动作和 `command` 之外的响应字段 |
| `runtime.preflight(expect, work_id=None)` | 逐项核对期望。全部满足时返回 `feature_report()` 的结果，给了 `work_id` 时结果里多一项 `request`；否则抛 `ValueError`，每条一行，写明缺什么、该改运行配置的哪个键或 `Runtime(...)` 的哪个参数 |
| `runtime.request_report(work_id)` | 对一个就绪的 Lab 或 Meta Work 按真实路径构建请求包但不准备 Effect，报告请求包里实际有什么 |

`expect` 里没写的键不检查。写了的键含义如下，拼错的键和类型不对的值会被拒绝，不会被当作没写：

| 键 | 取值 | 满足条件 |
| --- | --- | --- |
| `scientific_memory`、`meta_lab`、`orchestration` | `True` 或 `False` | `True`：配置已声明且服务已创建；`False`：两者都没有，用于基线或对照运行 |
| `identity_provider` | `True` 或 `False` | `True`：传入了可调用的身份提供者，并且 Scientific Memory 已启用；`False`：没有传入 |
| `research_policy` | `True` 或 `False` | 配置里有、或没有非空的 `research_policy` |
| `capabilities` | 能力标识的列表 | 每一个都在能力目录里，并且它的适配器已加载 |
| `measure` | 适配器标识的列表 | 每一个都已加载、在能力目录里、带 `measure` 回调，并且反馈闭环已启用。只有反馈闭环会调用这个回调 |

`request_report` 返回的 `status` 为 `ready` 时，请求包可以发出；为 `packet_unrepresentable` 或 `blocked` 时发不出去，`reason` 是原因，`actions`、`extra_fields`、`input_bytes` 和 `packet_sha256` 为 `null`。`present` 给出 `task.scientific_memory`、`task.constraints.research_loop`、`task.constraints.orchestration` 和 `task.constraints.research_policy` 是否在请求包里，`capabilities` 是这个请求的能力目录，`actions` 和 `extra_fields` 取自这个请求的响应 schema。`packet_sha256` 是请求包的 hash；这之后到真实调用之间账本没有变化时，它等于那次调用的 Effect 的 `packet_hash`。

带 `work_id` 的 `preflight` 用同一份期望核对这个请求包：它能发出去；期望为 `True` 的功能在请求包里有对应内容，期望为 `False` 的没有；期望的能力在这个请求的目录里。只看配置发现不了的情况在这里暴露，例如编排设置对 Lab 停用了某个工具，或者这是一次目录为空的最终复核请求。

三个接口都只读账本：不写事件，不创建会话，不登记 Scientific Memory 的根问题，Work 保持就绪。未启用任何功能的运行，调用它们前后发出的请求包字节不变。

局限：

- 检查只说明功能已经接通，不说明模型会用它们，也不说明结果有科研收益。
- `request_report` 报告的是请求包的内容，不预判这次调用会不会被接纳。持久会话上下文容量、预算、并发容量、会话是否空闲、Issue 版本是否已变、运行是否已进入收尾，都在真正派发时才检查。
- 命令行的 `features` 子命令只打印 `feature_report()`。命令行传不了身份提供者，所以它的输出里 `identity_provider` 是 `false`；要核对身份提供者和请求包，得在宿主脚本里调用 `preflight`。

实现见 [preflight](../rsi_lab/preflight.py)，验收见 [测试](../tests/v2/test_preflight.py)。

## 合成 CSV 示例

`rsi_domains.tabular.demo.run_demo` 使用 Wh/kWh 混合读数 CSV。初始输入没有单位映射，演示实际执行以下过程：

1. 登记单位证据缺口，请求按记录身份交付单位映射，持久等待。
2. 关闭再打开账本，交付映射并唤醒同一问题。
3. 请求聚合能力，执行混合单位、缺少单位、不支持的单位与重复身份四个验收案例。
4. 使用共同 TrialEngine 与独立 runner，在冻结验证 CSV 上比较直接求和与单位换算，检查误差和记录丢失。
5. 冻结 TaskPlan，实际聚合原 CSV；模拟成功回执后、发布前中断，恢复完成发布而不重新计算。

```python
from rsi_domains.tabular.demo import run_demo

report = run_demo("runs/tabular-demo")
print(report["aggregation"])
print(report["trial_outcome"])
```

合成期望值为 North = 3.5 kWh、South = 2.0 kWh，并保留四条输入记录身份。角色决策使用固定 fixture，不调用模型。单个验证 CSV 支持已声明输入范围的换算正确性，不证明任意任务的自动发现能力或普遍性能收益。

## 命令与运行边界

入口为 `python3 -m rsi_lab`。`init` 登记运行配置，`register-plan` 冻结任务计划，`adapter-run` 执行已注册能力，`run-until-wait` 推进就绪工作直到等待、预算或步数边界，`features` 打印这次运行启用了哪些科研功能（见[启动前检查](#启动前检查)）。只有合法准备态调用能继续派发，未知调用不能因重启而重发。

```sh
python3 -m rsi_lab --help
python3 -m rsi_lab --root runs/tabular-demo --run tabular-portability status
python3 -m rsi_lab --root runs/tabular-demo --run tabular-portability \
  --adapter-factory rsi_domains.tabular.plugin:make_adapter status
python3 -m rsi_lab --root runs/tabular-demo --run tabular-portability \
  --adapter-factory rsi_domains.tabular.plugin:make_adapter features
```

后三条命令读取前述示例已创建的运行。`--adapter-factory` 是受信宿主参数，不是模型命令。真实模型配置与自定义运行步骤见[持久 Agent 说明](persistent-agents.md)。新任务使用新目录；不要把旧格式或其他任务的账本直接当作新任务输入。

相关实现与验收：

- [Runtime](../rsi_lab/runtime.py)、[Adapter](../rsi_lab/adapters.py)、[TaskPlan](../rsi_lab/plans.py)
- [CSV 能力](../rsi_domains/tabular/plugin.py)、[离线闭环](../rsi_domains/tabular/demo.py)
- [能力替换验收](../tests/generic/test_portability.py)、[任务契约验收](../tests/generic/test_plans.py)
