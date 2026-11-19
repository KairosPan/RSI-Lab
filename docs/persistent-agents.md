# Lab / Meta / Literature 持久会话、工作记忆与执行日志

通用 `rsi_lab.Runtime` 把 Lab、Meta 与 Lab 委托的 Literature 子 agent 接到同一 Work/Effect 控制面，为各自保存独立的持久 Codex 会话和工作记忆。模型输出受控命令，Controller 校验后执行已注册工具；模型不能直接修改账本、执行工具或认证科学结论。文献工具配置与子任务协议见 [Literature 工具使用说明](literature-tools.md)。

本页描述当前实现。完整目标契约、长期科研记忆和后续验收见[系统设计 §9](system-design.html#persistent-agent-sessions)。系统设计中的 PS01–PS12 是验收要求，不代表当前 CLI 后端已满足所有底层能力。

## 数据保存在哪里

为每个任务选择独立运行根目录，例如 `runs/my-task`。不要把运行目录与源码目录混在一起。

| 层 | 保存内容 | 权威与共享范围 |
| --- | --- | --- |
| 原生 Codex 会话 | 对话消息、原生历史和可观察运行信息 | 每个 Lab、Meta 和 Literature 实例各用精确的 `provider_thread_id`，不使用 `--last`，也不续接对方 thread。原生文件由 Codex 保存；每次调用的 token 从其中的用量记录读出后记入共享账本，预算与已执行状态仍以共享账本为准 |
| Agent 工作记忆 | 目标引用、计划、尝试、理由摘要、暂定假设、未决问题、下一步和相关来源引用 | SQLite 中追加不可变修订，按 task/run/branch/agent owner 归属；会话重建保留当前 head。内容标记为 `unverified_working_state` |
| 执行日志 | 每次调用的原始 stdout/stderr、事件、输入、输出、错误、用量和终态 | `calls/<effect_id>/journal.jsonl` 保存逐条持久事件与 hash 链；模型无权确认水位或改写旧事件 |
| 共享执行账本 | Work、Effect、Wait、真实回执、来源事实和资源消耗 | `ledger.sqlite3` 与 `objects/`；控制器是唯一写入入口，恢复与去重以这些状态为准 |
| 共享 Scientific Memory | 科研问题、证据、判断、调查、经验和决定 | 基线模式仅有基础事实存储；启用 `research_features.scientific_memory` 后使用有版本和来源绑定的六对象服务，见[通用核心与 Scientific Memory](rsi-lab-core.md#scientific-memory-与独立工作记忆)。工作记忆、Handoff 或日志内容不会自动变成经过支持检查的科研结论 |

运行根目录中的 `agents/<session_id>/workspace/` 是该会话独立的原生工作目录。默认 Codex 认证和原生会话文件仍由使用者自己的 Codex 配置目录管理；软件不会复制登录凭证到任务目录。调用回执记录原生 thread 身份和已核验的 rollout 路径。

工作记忆读取由当前 owner 限定。跨角色协作通过 `AgentState.propose_handoff(...)` 和 `accept_handoff(...)` 显式选择字段、用途和接收方；接纳后创建可追踪的目标 Work。接收方不能借此获得另一 agent 的整个会话、覆盖其工作记忆，或继承旧调用的证据读取权限。控制器/宿主仍是可信进程；这些接口边界不等同于操作系统级保密。

## 调度和会话如何衔接

1. `enqueue_lab(...)` 或 `lab-start` 创建初始 Lab Work。Meta 在已登记观察事件、问题和等待处置边界获得自己的 Work。
2. 准备模型调用时，Runtime 读取所属 agent 的当前工作记忆，构造本轮 packet，核对原生历史前缀和容量，并预约资源。
3. Controller 领取唯一 session turn。并发或过期调用不能越过 session head、fence 或工作记忆 revision 检查。
4. Codex 初次调用创建持久会话；以后使用同一精确 thread 续接。正常 Lab/Meta/Literature 调用不使用 `--ephemeral`。
5. 调用原始事件逐条落盘。已完成执行、JSON/命令是否合法、工作记忆增量是否合法分别处理。
6. 子 Effect 结算后，由父 turn 的幂等提交推进 session head、接纳合法工作记忆增量并释放会话写入权。空增量保持原 head；非法增量保留旧状态和拒绝原因。
7. Lab 可以请求已注册的诊断/科学适配器。控制器执行工具、登记来源事实，再创建 Lab continuation。新来源也可触发 Meta 观察工作。Lab 委托的 Literature 任务则先在子 agent 内接收工具结果，向父 Lab 交付阶段或完成结果；子任务的每次读取不自动触发 Meta。

`run-until-wait` 是有步数上限的合作式驱动器：推进当时可运行的 Lab、Meta、Literature 和已注册 diagnostic，遇到依赖、资源限制、未知调用或无工作时返回。宿主在新输入、真实结果或 deadline 到达后再次调用；它不是隐藏的后台 daemon，也不会因为等待或 `no_action` 就自动宣布科学任务成功。单个 Work 在派发前被拒绝（例如预算不足）时，驱动器把它记入结果的 `skipped` 并继续处理其他 Work，本次调用不再选它；Work 的 Effect 已经派发之后才出错（例如工具结果发布失败）时，改为记入 `step_errors`（含 `work_id`、`effect_id` 和 `reason`），它不影响返回状态；每次跳过、取消和恢复都计入步数上限。选择新 Work 之前，驱动器先处理所有 `prepared` Effect：Lab/Meta/Literature 调用照常派发，本 Runtime 能解析适配器且其能力通过接纳检查的工具调用按已持久保存的输入派发（不再调用 `prepare`）；没有可用适配器或能力未通过接纳检查的记入 `prepared_blocking` 并保持原状，本次调用不再选它，因为接纳失败可能是宿主配置问题，修正后仍可派发；派发本身被拒（例如问题版本已过期）且仍处于 `prepared` 的 Effect 以拒绝原因取消并记入 `cancelled_prepared`；`dispatching` 或 `unknown` 的 Effect 永不重新派发。开头的恢复中单项失败不会中断其余恢复，失败项记入 `recovery_errors`。无事可做时，状态依次取 `finalizing`、`blocked_external`、`blocked_prepared`（存在 `prepared_blocking`）、`blocked_admission`（存在 `skipped`，`reason` 为第一条跳过原因）、`blocked_adapter`、`blocked_recovery`（存在 `recovery_errors`），否则为 `awaiting_producer_or_deadline` 或 `quiescent`。步数上限用尽时状态为 `step_limit_reached`，若此前有 Work 被跳过，`reason` 为第一条跳过原因。Lab 提案在接纳时被拒绝或路由时被阻止，会在同一事务中得到一次修复 Lab Work，其输入带 `routing_error` 和 `repair_of`；修复 Work 自身不再获得修复。工具输入在执行时才被适配器拒绝的 diagnostic 会失败并通知其依赖，Lab 发起的请求同样回到 Lab 并带上拒绝原因。

`run-until-wait` 的结果还带有只读取账本得出的 `report`：

- `settlement_status`：存在 `dispatching`、`unknown` 或用量未结算的 Effect 时为 `blocked_external`；否则本次有 Work 因预算不足被跳过时为 `resource_exhausted`；否则为 `settled`。
- `work_status`：存在 `prepared`、`dispatching` 或 `unknown` 的 Effect 时为 `unresolved_effects`；否则有就绪 Work 时为 `pending_queued`；否则有未结束的等待时为 `parked_dependencies`；否则为 `empty`。
- `attention.stalled_issues`：仍为 `open`、却没有就绪或已领取的 Work、没有未结束的等待、也没有未结算 Effect 的问题，并给出最近一个相关 Work 的身份和状态。常见原因是处置调用失败后没有后继。`model_failure_retries` 须在初始化运行时设置，因为运行配置之后不能修改；对已经停滞的运行，宿主通过 `Controller.enqueue` 为该问题加入新的 `meta_disposition` Work（没有对应的 CLI 命令），或用 `finalize` 结束运行；问题因用量未结算而无法关闭时，先用 `reconcile` 结算。
- `attention.stopped_lab`：没有就绪或已领取的 Lab Work，且最近一个 Lab Work 失败、被阻塞、其已准备的调用被取消，或成功但提议缺失、被拒绝或被阻止。字段给出 Work 身份、状态、提议状态和原因。查看原因后用 `lab-start` 开始新的 Lab Work。
- `attention.sessions_requiring_recovery`：owner 的当前会话处于 `recovery_required`。存在 `unknown` 调用时先用 `reconcile` 对账，再用 `agent-rebuild` 更换会话。
- `attention.capacity_blocked_work`：因会话上下文或 packet 超出容量（`blocked_capacity` 或 `packet_unrepresentable`）而阻塞的 Work。用 `agent-rebuild` 换成新会话，Lab 工作再用 `lab-start` 重新开始。
- `attention.stalled_literature_tasks`：仍为 `active`、却没有就绪或已领取的 Work、也没有仍在执行的 Effect 的 Literature 委托任务，并给出最近一个相关 Work 的身份和状态。正常情况下不会出现：子 agent 的调用明确失败且没有重试名额、或它的动作在路由时被拦，任务都会以 `unresolved` 结果交还所属 Lab。出现时常见原因是子 agent 的调用被操作者隔离；查看原因后由 Lab 重新委托。

被操作者隔离的调用仍保留未知用量的对账义务，所以 `settlement_status` 仍为 `blocked_external`；但它不再算作仍在进行的工作，不计入 `work_status` 的 `unresolved_effects`，也不会让所属问题躲过 `stalled_issues`。

原本会报告 `quiescent` 时，或原本会报告 `awaiting_producer_or_deadline`、但没有处于 `waiting`、`disposition_due` 或 `disposed` 的等待，且每个就绪 Work 都是其 agent 会话不空闲、驱动器因此不会选择的 Lab/Meta Work 时，只要上述 `attention` 中有任何一项非空，状态改为 `stalled`，`reason` 为第一项非空条目的名称（按 `stalled_issues`、`stopped_lab`、`sessions_requiring_recovery`、`capacity_blocked_work`、`stalled_literature_tasks` 的顺序）。`quiescent` 因此只表示干净结束：没有待执行的工作，也没有无人负责的义务。其他状态不变。

各角色的工具权限由本轮命令模式与已注册能力决定，不来自会话里曾出现的文字。当前 Lab 命令为 `request_literature`、`request_diagnostic`、`propose_issue` 和 `no_action`。新版 Lab 将问题、用途、范围、交付物及预算委托给 Literature，不选择具体文献工具或通过 diagnostic 绕过子 agent。Controller 固定当时完整的可用文献目录；Literature 自主选择其中的工具，并向父 Lab 发布阶段结果或完成结果。历史任务保持原工具权限。Meta 的 discovery/disposition 模式仍使用明确命令集合。

## 运行入口

先配置已登录的 Codex CLI、明确的模型/effort、运行预算、任务输入和受信适配器。Codex CLI 必须是内核实测过的版本，见“Codex 版本”。真实模型调用要求运行配置的 `execution_mode` 为 `live` 或 `role_recall`；合成测试不需要模型凭证。

以下是最小运行配置模板。先把 `YOUR_MODEL_ID` 改为自己账户可用且任务明确选择的模型，并将 `goal` 改为实际任务；预算数值只是工程示例，不代表某项科研任务足够。将文件保存为 `runs/my-task/run-config.json`，该目录已被 Git 忽略。

```json
{
  "task_id": "my-task",
  "goal": "检查任务输入条件，并选择下一项获准的工作",
  "execution_mode": "live",
  "model": "YOUR_MODEL_ID",
  "effort": "medium",
  "max_external": 1,
  "max_input_bytes": 131072,
  "max_effective_context_bytes": 262144,
  "max_working_memory_bytes": 16384,
  "model_reservation": {
    "model_calls": 1,
    "tokens": 4096,
    "wall_seconds": 300
  },
  "budgets": {
    "model_calls": {"limit": 12, "hard": true, "settlement_reserve": 1},
    "tool_calls": {"limit": 24, "hard": true},
    "tokens": {"limit": 1000000, "hard": false},
    "wall_seconds": {"limit": 3600, "hard": false}
  }
}
```

可选的 `model_timeout_seconds` 与 `model_reservation` 并列，设定 `meta-step`、`lab-step` 和 `run-until-wait` 中每次模型调用的超时秒数，取值为 1 到 3600 的数字，省略时为 300。超时被终止的调用结算为 `unknown`，处理方法见[未知模型调用的对账](#未知模型调用的对账)。`model_calls` 和 `tool_calls` 是控制器可执行的调用次数限制；token/时间是显式软预约与实际用量结算，不构成 provider 内部的硬停止保证。模型选择、总预算与既有运行配置不可通过恢复偷偷改变；新任务用新的运行目录。

两个可选的失败策略键也写在运行配置顶层，省略时保持原有行为：

- `unknown_usage_policy`：取 `hold`（默认）或 `charge_reservation`，对 Lab、Meta 和 Literature 的模型调用同样生效。模型调用已得到 `succeeded` 或 `failed_definite` 结果、但回执缺少部分用量（例如没有 token 计数）时，`hold` 保留这些资源的预约和对账义务：该调用的提议不会被应用，所属问题也不能关闭。`charge_reservation` 让 Runtime 按仍持有的完整预约结算这些资源，结算回执的 `estimated_resources` 列出这些资源名，`reconciliation.provenance` 为 `run config unknown_usage_policy=charge_reservation`。计费金额等于预约，是估计值：既不是实测值，也不保证是上限，因为 token 和时间预约不会让 provider 停止。预约为 0 的资源永不按此计费，这样的调用仍保留对账义务。从执行日志恢复、但仍缺少 CLI 进程已退出证明的调用暂不计费，直到该证明到达或操作者完成对账。`dispatching` 或 `unknown` 的调用不受影响，仍需按[未知模型调用的对账](#未知模型调用的对账)处理。
- `model_failure_retries`：0 到 5 的整数，默认 0。Lab、Literature、Meta discovery 或 Meta disposition 调用收到 provider 的明确失败（`failed_definite`，回执确认收到终态且调用确实已派发）时，为同一问题和优先级加入一个新的同类 Work，输入带 `retry_of`（这条链第一个 Work 的身份）和 `retry_attempt`；每条链最多重试该次数。失败的 Effect 本身永不重新派发；问题已关闭时不重试；等待到期后的 `disposition` 仍然每个等待周期只尝试一次，不重试。重复执行 `recover` 不会重复加入重试 Work。Literature 调用的重试 Work 绑定同一个委托任务；没有重试名额或名额用尽时，任务以 `unresolved` 结果交还所属 Lab，不会停在 `active`。

其他取值会使 Runtime 拒绝启动该运行。

可选的 `native_capabilities` 也写在顶层，为每个角色打开 Codex 自身的工具：网页搜索、受限终端和子 agent。它按角色分别设置，键只能是 `lab`、`meta` 和 `literature`；没有这个块，或块里没有写某个角色时，该角色全部关闭。没有这个块的运行发出的 Codex 命令、提示词和请求包与没有这项功能时逐字节相同。有这个块时，全部关闭的角色发出的 Codex 命令仍然相同；提示词和请求包只有运行配置的 hash（`decision_dependencies.config_hash` 及由它生成的 token，它们随任务数据写在提示词里）不同，因为运行配置本身不同。完整的运行配置示例见 [`examples/native-capabilities-run.json`](../examples/native-capabilities-run.json)，它在上面的模板上加了这个块，把 `model_timeout_seconds` 设为 1800、`max_effective_context_bytes` 设为 4,194,304，并为使用工具的轮次放大了预算：`model_reservation` 预约 200,000 tokens 和 1800 秒（与超时相同），`budgets.tokens` 为 20,000,000，`budgets.wall_seconds` 为 36,000。块的写法如下，其中的目录是占位符：

```json
"native_capabilities": {
  "lab": {"web_search": true, "terminal": {"readable_roots": ["/abs/path/to/task-data"]}, "subagents": {"max_concurrent": 3}},
  "meta": {"web_search": true, "terminal": true, "subagents": true},
  "literature": {"web_search": true}
}
```

- `web_search`：`true` 或 `false`，打开 Codex 的实时网页搜索。
- `terminal`：`false`、`true` 或 `{"readable_roots": [...]}`。打开后模型可以在自己会话的工作目录里运行命令和编辑文件，不能联网；`readable_roots` 列出它另外可以只读的目录，`true` 表示没有这样的目录。
- `subagents`：`false`、`true` 或 `{"max_concurrent": N}`。打开后模型可以在一轮内派出子 agent，`N` 是同时运行的上限，取 1 到 8 的整数，`true` 取默认值 3。对象里还可以写 `roles`，声明具名的子 agent 角色。

`roles` 的每个键是角色名，值恰好含 `description` 和 `instructions` 两项。下面的文字是占位示例：

```json
"subagents": {"max_concurrent": 3, "roles": {
  "challenge": {"description": "Argues against the leading hypothesis.",
                "instructions": "Find the strongest counter-example to the current hypothesis and report it."}
}}
```

- 角色名必须整体匹配 `[a-z][a-z0-9_-]{0,31}`，不能是 Codex 的内置角色 `default`、`explorer`、`worker`，也不能是 Codex `[agents]` 表的固定键：`max_depth`、`default_subagent_model`、`default_subagent_reasoning_effort`、`max_concurrent_threads_per_session`、`enabled`、`interrupt_message`。
- 最多 8 个角色。
- `description`：一行，1 到 300 个字符，不能是空白。
- `instructions`：1 到 8,000 个字符，不能是空白，可以换行。

`roles` 为空对象时等于没有声明角色。没有声明角色的运行发出的 Codex 命令、提示词和请求包与没有这项功能时相同。角色怎样交给 Codex、提示词多出的一句和回执记下的内容见[Codex 原生工具](#codex-原生工具)。

写错的键或取值会使 `init` 拒绝建立运行；`init` 和之后每次 Runtime 启动都会检查。开启后 Codex 在一轮内自行调用这些工具，Controller 不逐次准入，调用结束后才记录。命令与权限的变化、记录内容、计费和限制见[Codex 原生工具](#codex-原生工具)。

```sh
python3 -m rsi_lab --root runs/my-task --run main \
  init --config runs/my-task/run-config.json
python3 -m rsi_lab --root runs/my-task --run main lab-start --event first-step
python3 -m rsi_lab --root runs/my-task --run main run-until-wait --max-steps 20
python3 -m rsi_lab --root runs/my-task --run main status
```

`lab-start` 返回 Work 身份。若只运行它的一次决策，可改用 `lab-step --work WORK_ID`，将 `WORK_ID` 替换为实际返回值。已有来源可用重复的 `--evidence FACT_ID` 参数加入初始 Lab 工作；ID 必须已存在于当前运行账本，不能只是文件名。需要冻结完整任务规格时，在开始工作前使用 `register-plan --plan /path/to/task-plan.json` 注册该任务计划。

| 命令 | 用途 |
| --- | --- |
| `lab-start` | 为一次明确触发创建 Lab Work，可指定本轮所需的已登记证据 |
| `lab-step` | 只推进指定 Lab Work 的一次模型决策 |
| `meta-step` | 推进指定 Meta Work；Inspector/Worker 模式使用 Meta 自己的会话 |
| `run-until-wait --max-steps 20` | 推进就绪的 Lab/Meta/Literature/工具工作后返回，保留尚未完成的义务 |
| `status` | 查看会话、turn、待执行/阻塞工作、预算、需要对账的 Effect，以及最近一次记录的 Codex 账户快照（`codex_account`） |
| `features` | 只读地打印这次运行启用了哪些科研功能、加载了哪些适配器、各模式下模型可用的动作，以及各角色的 Codex 原生工具设置和与之相关的提醒（`native_warnings`）；见 [启动前检查](rsi-lab-core.md#启动前检查) |
| `recover` | 摄取持久回执，或从所属调用日志恢复已知终态；补做幂等发布/提议处理，不盲目重发未知调用 |
| `reconcile --effect EFFECT_ID --receipt PATH` | 用操作者写的、绑定该 Effect 的回执结算一次 `unknown` 调用，随后执行 `recover`；可加 `--key KEY`，同一回执重复执行无副作用 |
| `agent-checkpoint` | 在当前会话没有活动 turn 的安全边界保存检查点 |
| `agent-rebuild` | 显式隔离可确认已结束的坏 turn 并更换会话，保留同一 owner 的工作记忆与所有共享事实 |
| `finalize --reason TEXT` | 持久登记停止原因并禁止新的普通模型/工具工作，保留已承诺的恢复责任 |

工具适配器由宿主显式注册：CLI 的 `--adapter-factory package.module:function` 是可信本地配置，不能来自模型命令。注册后可使用以下驱动方式：

```sh
python3 -m rsi_lab --root runs/my-task --run main \
  --adapter-factory your_domain.adapters:make_adapter \
  run-until-wait --max-steps 20
```

`your_domain.adapters:make_adapter` 与配置文件路径是使用者提供的占位符，不是本仓库自动安装的能力。

研究闭环的任务语义也由宿主与领域适配器提供。冻结配置中的可选 `research_policy` 可以声明
`recommendation_kinds`、`recommendation_guidance` 和 `final_review_guidance`；省略时内核不假定
搜索任务、评分目标或优化方向。推荐类别进入实际响应 schema、接纳校验与 Effect 元数据；
最终复核只限制可执行动作，不代表目标成功，是否进入该阶段由任务宿主判断。

适配器可以提供 `measure(effect, publication) -> dict`，作为源文件与代码哈希绑定的可信回调。
内核仅在成功执行后调用它，并将有界、有限 JSON 对象放入 `measurement.domain_metrics`，
不读取其中的分数或解释最大化/最小化方向。执行状态、原始回执与 publication 哈希仍由控制器
独立保存，领域指标不能覆盖它们；没有回调就只记录执行凭据。测量与复核 Work 同事务提交，
恢复复用已提交的测量，不重新执行工具或重新解释结果。

Scientific Memory 的领域身份及经验解释继续通过 `scientific_identity_provider` 注入。
内核负责对象修订、来源闭包、交付预算和负结果保留；对象身份规范化、具体指标、来源主题及
结束条件保留在领域模块和任务宿主中。

## Codex 原生工具

运行配置的 `native_capabilities`（写法见[运行入口](#运行入口)）为每个角色打开 Codex 自身的网页搜索、受限终端和子 agent。这些工具由 Codex 在一轮内自行调用，Controller 不逐次准入；调用结束后，系统从事件流和 rollout 记下它们做了什么，并按 Codex 自己的用量记录结算 token。已注册的科学工具不受影响，仍由 Controller 准入、执行和登记。设计与取舍见[设计文档](superpowers/specs/2026-11-17-native-codex-capabilities-design.md)。

通过这些工具得到的内容只是线索，不是证据。命令引用的证据 ID 必须来自本轮请求；要让某项内容可以引用，仍须通过已注册能力取得并交付。

### 可读目录

`readable_roots` 的每一项必须是已存在目录的绝对路径，按解析后的路径保存，重复的只保留第一次出现的位置。位于运行根目录或 Codex 配置目录（`CODEX_HOME`，默认 `~/.codex`）之内的目录会被拒绝。位于日常使用的 Codex 配置目录 `~/.codex`（按 `$HOME` 取）之内的目录也一律拒绝，即使 `CODEX_HOME` 指向另一个目录，错误中写明 `the everyday Codex home`：这时终端的权限配置同样禁止访问 `~/.codex`（见[Codex 命令的变化](#codex-命令的变化)），可读目录不能在其中开口子。是否位于其中按文件身份判断，所以符号链接或改写大小写都绕不过去。目录本身就是共享临时目录（`/tmp`、`/private/tmp`、`/var/tmp`、`/private/var/tmp`）时也拒绝，位于其中的子目录可以。`init` 之后被删除的目录会使每次 Runtime 启动失败，`recover` 也包括在内，直到重新创建该目录。

列出哪些目录由宿主负责：评测的私有代码或答案不能放在可读目录下。可读目录的路径会写进发给该角色的请求。

终端的网络关闭不等于读到的内容出不去。同一角色开着网页搜索时，它读得到的任何内容（工作目录、可读目录、请求本身）都可能被写进搜索词发到外部，例如网页诱导它这样做时；关闭终端网络挡不住这条通道。所以不要把含有凭证的目录（例如主目录、`~/.ssh` 或云服务的配置目录）或私有评测数据列为可读目录。

### 工具服务与查看图片

每个角色还可以声明两项可选设置：`tool_servers`，Codex 在调用中可以使用的工具服务（MCP 服务器）；`view_image`，让模型查看图片文件。不写、`view_image` 为 `false` 或 `tool_servers` 为空对象时，这一项不进入保存的设置，设置与没有这两项之前完全相同。下面的例子还声明了技能文件夹 `skills`，见[技能](#技能)。

```json
"native_capabilities": {
  "lab": {
    "terminal": true,
    "view_image": true,
    "tool_servers": {
      "chem": {"description": "Fingerprints and similarity for SMILES.",
               "command": "/opt/tools/bin/python", "args": ["-m", "chem_server"],
               "env": {"CHEM_MODE": "fast"}, "env_from": ["CHEM_API_KEY"],
               "tool_timeout_seconds": 300},
      "papers": {"description": "Full text of open-access papers.",
                 "url": "https://papers.example.org/mcp",
                 "token_from": "PAPERS_TOKEN",
                 "headers": {"X-Client": "rsi-lab"}}
    },
    "skills": {"root": "/data/research-skills"}
  }
}
```

工具服务有两种，每个只能是其中一种：

- 本地服务写 `command`，由 Codex 在调用时启动。`command` 是可执行文件的绝对路径，`init` 和每次 Runtime 启动时都检查它存在、是普通文件且可执行；按写的原样保存，不解析符号链接（虚拟环境里的解释器就是符号链接，必须保持原样）。`args` 是字符串列表，`env` 是不保密的固定环境变量值，`env_from` 列出变量名，其值在调用时取自 Controller 所在进程的环境。同一个变量不能既在 `env` 又在 `env_from` 中。
- URL 服务写 `url`，由 Codex 连接。`url` 以 `https://` 开头并带主机名；只有主机是 `localhost`、`127.0.0.1` 或 `[::1]` 时可以用 `http://`；不能带用户名或密码，不能含反斜杠，方括号只能括住位于开头的 IPv6 主机地址（各种 URL 解析器对其他写法读出的主机不一致）。`token_from` 可选，指定一个环境变量，Codex 把它的值作为 bearer token 发送。`headers` 是不保密的固定请求头，名称最多 64 个 HTTP token 字符，值为 1 到 1,024 个可打印 ASCII 字符；不能有 `Authorization`（不分大小写），token 用 `token_from` 给出。

两种共同的规则：名称匹配 `[a-z][a-z0-9_]{0,31}` 且不以 `codex` 开头，每个角色最多 8 个；`description` 是一行 1 到 300 个字符、不全是空白；`tool_timeout_seconds` 可选，1 到 3,600 秒，没有默认值；变量名匹配 `[A-Za-z_][A-Za-z0-9_]{0,127}`。

`env_from` 和 `token_from` 只保存变量名：运行配置、Effect、请求包、调用记录、回执和命令行里都没有它们的值，Codex 从它继承的环境里读取。回执里本地服务的 `env` 换成 `env_names`，只记变量名。调用时这样的变量没有设置或为空，调用被拒绝，Codex 不会启动：在写下任何调用文件、询问 Codex 版本之前，错误 `Native settings name environment variables that are not set: <变量名>` 按名称顺序列出这些变量。

通过工具服务得到的结果和其他原生工具一样，只是线索，不是证据。

Codex 每次调用工具服务都会把 thread 与会话的身份、模型和 Codex 版本发给它；会话的工作目录位于 git 仓库内时（运行根目录在仓库内就是这样），还会发送该仓库的本地路径、远程仓库的 URL 和最近一次提交。内核关不掉这一点。服务属于别人时，把运行根目录放在任何 git 仓库之外。

URL 服务和网页搜索一样是一条出口：终端没有网络，但角色读得到的任何内容都可以写进工具调用的参数，离开本机。所以声明了 URL 服务的角色，同样不能读得到私有数据和凭证，见上一节。

`view_image` 只有在该角色开启终端时才能为 `true`，否则配置被拒绝。查看图片只能读终端命令读得到的地方，即工作目录和可读目录；看到的图片同样只是线索。

### 技能

`skills` 为一个角色声明一个技能文件夹。运行配置里只写文件夹的位置：`"skills": {"root": "/data/research-skills"}`。`root` 必须是已存在目录的绝对路径，按解析后的路径保存；它本身可以是符号链接。

文件夹里每个技能占一个子文件夹，其中必须有 `SKILL.md`，其余是辅助文件，可以放在下级目录里：

```
/data/research-skills/
  plot-check/
    SKILL.md
    scripts/check.py
```

`SKILL.md` 以 front matter 开头：第一行是 `---`，到下一行 `---` 为止。

```markdown
---
name: plot-check
description: Checks a plot before it is reported.
---
Open the plot and check its axes and units.
```

front matter 中以 `name:` 或 `description:` 开头、冒号后紧跟空格、制表符或行尾的行，按 `key: value` 读取：值去掉首尾空白，两端是一对相同的引号时再去掉这对引号（所以 `description: "Checks: axes."` 读作 `Checks: axes.`）。每个键只能出现一次，并且只能写在一行内：紧跟其后的缩进行（YAML 会把它接到值上）被拒绝。其余各行不读取。`name` 必须等于子文件夹的名称。`description` 是一行 1 到 300 个字符、不全是空白，不能以 `|` 或 `>` 开头（YAML 的多行写法）。

Codex 按 YAML 读取 front matter，内核没有 YAML 解析器。运行配置在 `init` 之后不能更改，而 Codex 列出的技能与声明的不符时调用无效，所以凡是 YAML 可能读成别的样子的写法，`init` 一律拒绝：

- 不加引号的值：含 `: ` 或 ` #`（YAML 的键分隔与注释），以 `:` 结尾，以 `,[]{}#&*!|>'"%@` 或反引号之一开头，以 `-`、`?` 或 `:` 加空格开头，或者 YAML 会读成 null、布尔值或数字（例如 `null`、`true`、`42`；名为 `null` 的技能也因此要写 `name: "null"`）。这样的值加上引号即可，例如 `description: "Checks: axes."`。
- 加了引号的值：双引号里不能有 `\` 或 `"`，单引号里不能有 `'`，YAML 会把它们读作转义。换一种引号，或改写这段文字。

内核不读取的那些 front matter 行仍然可能不是合法的 YAML，这时 Codex 读不出这个技能；每次调用之后的检查会报告这种情况。

限制：

- 每个角色 1 到 16 个技能；空文件夹被拒绝。
- 技能名称（即子文件夹名称）匹配 `[a-z][a-z0-9_-]{0,31}`。文件夹的顶层只能是技能子文件夹，不能有普通文件。
- 每个技能最多 16 个文件，合计不超过 262,144 字节，`SKILL.md` 也计在内。
- `SKILL.md` 只能在技能子文件夹的顶层；下级目录里名为 `SKILL.md` 的文件被拒绝，因为 Codex 会把它当作另一个技能列出。
- 只能是普通文件和目录；`root` 之下任何位置出现符号链接都被拒绝，命名管道之类的特殊文件同样。
- 名称以 `.` 开头的项在每一层都被跳过，不读取、不计数、不保存，例如 Finder 的 `.DS_Store` 或 `.git`。

`skills` 只在该角色开启终端时接受，否则配置被拒绝：模型要用终端读技能的文件。

`init` 把文件夹读取一次。任何一项不符合，`init` 拒绝这个运行配置，不创建运行，错误信息以 `native_capabilities.<角色>.skills` 开头。符合时，每个文件的内容存进运行根目录的对象存储（`objects/<内容的 SHA-256>`），保存的设置里按名称顺序记下每个技能的 `description` 和它的文件，文件按相对路径的顺序，每个只记 SHA-256：

```json
"skills": {"root": "/data/research-skills",
           "frozen": {"plot-check": {"description": "Checks a plot before it is reported.",
                                     "files": {"SKILL.md": "<SHA-256>", "scripts/check.py": "<SHA-256>"}}}}
```

此后修改、增删甚至删除这个文件夹，都影响不到这次运行，运行用的是存下的副本。Runtime 只接受这种冻结后的形式：没有 `frozen` 的 `skills`（例如不经 `init`、直接交给 Controller 的运行配置）在启动时被拒绝，错误中写明 `must be frozen with the run`。运行配置里已经写了 `frozen` 时，`init` 仍从 `root` 重新读取，读到的必须与之完全相同，否则拒绝，错误中写明 `changed since it was frozen`。回执里的 `skills` 只记每个技能的名称和它的文件表（路径到 SHA-256 的对象）的 hash。

每次调用之前，内核都按存下的副本把技能重新写进该会话的工作目录，写在 `.agents/skills/<name>/<相对路径>`，Codex 从这里列出它们：

- 先从对象存储读出每个文件，`objects/<SHA-256>` 必须是普通文件（不是符号链接），内容的 SHA-256 等于它的名称。有一个不符合（例如对象存储里缺少它，或它的内容变了），调用被拒绝，什么也不发送，工作目录原样不动；错误写明是哪个技能的哪个文件。
- 然后删除工作目录里原有的整个 `.agents`，不论其中是什么：它是符号链接或文件时只删除它本身，是目录时连同内容删除，不跟随其中的符号链接。上一次调用写下的技能也一并删除，所以 `.agents` 里只有这次写出的文件。
- 最后写出各个文件：目录权限 `0o700`，文件 `0o600`。不保留可执行位，技能里的脚本要通过解释器运行，例如 `python3 scripts/check.py`。

终端命令不能写 `.agents`：权限配置把工作目录里的 `.agents` 设为只读，所以模型改不了技能，只有内核写它。替换 `.agents` 只针对声明了技能的角色；没有声明技能的角色，工作目录里出现 `.agents` 时终端调用仍被拒绝，见[终端调用前后的检查](#终端调用前后的检查)。

### 独立的 Codex 主目录

Codex 向模型列出的技能，不只来自会话工作目录里的 `.agents/skills`，还来自另外三个文件夹：调用所用 Codex 主目录（`CODEX_HOME`，默认 `~/.codex`）下的 `skills`、`$HOME/.agents/skills` 和本机的 `/etc/codex/skills`。日常使用的 `~/.codex/skills` 里通常放着主人自己的个人技能。声明了技能的角色必须只看到声明的技能，所以它只能在这三个文件夹都没有任何内容的地方运行。Codex 主目录的 `skills/.system` 不算在内：那是 Codex 自己安装的技能，这样的角色的命令带 `-c skills.bundled.enabled=false`，不列出它们（见[Codex 命令的变化](#codex-命令的变化)）。

内核在三个时刻检查：`init` 时和每次 Runtime 启动时（`recover` 也包括在内）用 `CODEX_HOME` 所指的目录（没有设置时为 `~/.codex`），每次调用之前用该会话实际使用的 Codex 主目录，在询问 Codex 版本、写下任何调用文件之前。只有运行配置里某个角色声明了技能（调用时则是这次调用的设置声明了技能）才检查；其他运行不受影响。检查只列出这三个文件夹的直接条目，不读条目里的内容，也不记下条目的名称。任何一个文件夹里有条目（以 `.` 开头的也算），或者它存在却不是目录（例如一个文件，或指向不存在之处的符号链接），就拒绝：`init` 不建立运行，Runtime 不启动，调用不发出，Codex 不会启动。文件夹不存在则通过；指向某个目录的符号链接按那个目录检查。错误信息只写出文件夹，不写其中的内容：

```
A role with skills needs a separate Codex home with no skills of its own: <文件夹> holds entries. Create an empty folder, run codex login once with CODEX_HOME set to it, and start the Controller or the host with CODEX_HOME set to it. ~/.agents/skills must be empty whatever the Codex home is.
```

所以声明了技能的运行要用一个独立的 Codex 主目录，即主人自选的一个空文件夹。在那里登录一次，之后启动 Controller 或宿主时都把 `CODEX_HOME` 设为它；内核本来就使用 `CODEX_HOME`：

```sh
mkdir /data/separate-codex-home
CODEX_HOME=/data/separate-codex-home codex login
CODEX_HOME=/data/separate-codex-home python3 -m rsi_lab --root runs/my-task --run main \
  init --config runs/my-task/run-config.json
CODEX_HOME=/data/separate-codex-home python3 -m rsi_lab --root runs/my-task --run main run-until-wait --max-steps 20
```

- 内核不创建这个目录，也从不把凭证复制或链接进去；登录由主人自己完成。内核也从不写 Codex 的配置文件。
- 一个运行一直使用它开始时的 Codex 主目录：它的 thread 存在那里，恢复和续接都要在那里找到它们。运行中途不要改变 `CODEX_HOME`。
- 任何运行都可以使用独立的主目录，只有声明了技能的运行必须使用。这样做时它的 thread 也不会出现在日常 Codex 的历史里。开启终端时，权限配置除了禁止访问调用所用的 Codex 主目录，也禁止访问日常的 `~/.codex`，所以这样的角色读不到日常 Codex 的凭证、会话和技能。
- 限制：独立的主目录帮不了 `$HOME/.agents/skills`，不论 Codex 主目录是哪个，Codex 都会搜索它。只要它里面有任何内容，声明了技能的运行就一律被拒绝，`init`、每次启动和每次调用都是如此。
- 检查是在每次启动和每次调用时重新做的：已经建立的运行，在这三个文件夹之一出现条目之后也会被拒绝启动，直到把它清空。

### Codex 命令的变化

某个角色全部关闭时，命令比引入原生工具之前多三处：`-c agents.enabled=false` 和 `--disable skill_mcp_dependency_install`，见下面的“Codex 版本”；以及 `-c skills.include_instructions=false`。没有声明技能的角色，不论开启了哪些工具，每次调用都带最后这一项，紧跟在 `-c agents.enabled=false` 之后（开启子 agent 时紧跟在 `-c suppress_unstable_features_warning=true` 之后）。Codex 因此不向模型列出任何技能：日常 Codex 里的个人技能和 Codex 自带的技能都不出现。这改变了全部关闭的运行的命令和模型看到的内容，是“什么都不开启的运行保持不变”唯一一处有意的例外。开启后：

- 网页搜索：`web_search` 设为 `"live"`。
- 终端：不再传 `--sandbox read-only`，改用名为 `rsi_agent` 的权限配置。其中依次为：Codex 的最小平台目录集合可读；运行根目录、Codex 配置目录和本机存在的各个共享临时目录禁止访问，调用用的 Codex 配置目录不是日常使用的 `~/.codex`（每次调用时按 `$HOME` 取，两者按解析后的路径比较）而 `~/.codex` 作为目录存在时，它也禁止访问，按解析后的路径紧跟在调用的 Codex 配置目录之后，所以在另一个 Codex 配置目录里运行的角色读不到日常 Codex 的凭证、会话和技能；该会话的工作目录 `agents/<session_id>/workspace/` 可读写，但其中的 `.codex`、`.agents`、`AGENTS.md` 和 `AGENTS.override.md` 只读；每个可读目录只读。网络显式关闭。命令只继承核心环境变量，`TMPDIR` 与 `TMPPREFIX` 指向工作目录下的 `.tmp`。项目文档与 exec-policy 规则不加载（`project_doc_max_bytes=0`、`--ignore-rules`），所以模型写进工作目录的文件不会在下一轮变成指令或配置。
- 子 agent：深度为 1，子 agent 不能再派生；同时运行的数量不超过 `max_concurrent`；子 agent 默认使用父调用的模型和 effort。声明了角色时，每次调用前都按冻结的设置重新写出角色文件，每个角色一个：`<运行根目录>/agents/<session_id>/roles/<name>.toml`，其中只有 `name`、`description` 和 `developer_instructions`（即角色的 `instructions`）；该目录里其他的 `.toml` 文件被删除。命令按角色名的顺序为每个角色加上 `-c agents.<name>.description=…` 和 `-c agents.<name>.config_file=…`。这个目录在运行根目录之内，终端命令无权访问，Codex 自己仍读得到。角色文件写不出来时（例如目录或文件是符号链接），调用被拒绝，什么也不发送，Effect 结算为 `failed_definite`，回执标记 `never_dispatched`。
- 查看图片：`view_image` 为 `true` 时不再传 `--disable view_image`。
- 工具服务：按名称顺序，每个服务带若干 `-c mcp_servers.<name>.…`。本地服务依次是 `command`、`args`、`env`、`env_vars`（`env_from` 的变量名），URL 服务依次是 `url`、`bearer_token_env_var`（`token_from` 的变量名）、`http_headers`；空列表、空对象和没写的项不出现。两种服务随后都有 `tool_timeout_sec`（整数秒，只在写了 `tool_timeout_seconds` 时出现）和 `default_tools_approval_mode="approve"`，没有后者 Codex 会拒绝每个没有标为只读的工具。命令行上只有变量名，没有值。上面例子里的两个服务是：

  ```
  -c mcp_servers.chem.command="/opt/tools/bin/python"
  -c mcp_servers.chem.args=["-m","chem_server"]
  -c mcp_servers.chem.env={"CHEM_MODE"="fast"}
  -c mcp_servers.chem.env_vars=["CHEM_API_KEY"]
  -c mcp_servers.chem.tool_timeout_sec=300
  -c mcp_servers.chem.default_tools_approval_mode="approve"
  -c mcp_servers.papers.url="https://papers.example.org/mcp"
  -c mcp_servers.papers.bearer_token_env_var="PAPERS_TOKEN"
  -c mcp_servers.papers.http_headers={"X-Client"="rsi-lab"}
  -c mcp_servers.papers.default_tools_approval_mode="approve"
  ```
- 技能：声明了技能的角色带 `-c skills.bundled.enabled=false` 和 `-c project_root_markers=[]`，Codex 既不列出它自带的技能，也不列出工作目录之上各级文件夹里的技能；这样的角色不带 `-c skills.include_instructions=false`。这两项不取决于技能文件夹在哪里，每次调用都相同。
- 以上各项的 `-c` 值依次为：终端、子 agent、工具服务、技能。
- 始终不开启：apps、plugins、浏览器与电脑控制、图像生成、skill 搜索、hooks、goals、sleep 工具、第二套多 agent 实现，以及让 skill 自行安装它所依赖的 MCP 服务器的功能（`skill_mcp_dependency_install`）；图像查看只在 `view_image` 为 `true` 时开启。命令带 `--ignore-user-config`，用户自己配置的 MCP 服务器不会加载，加载的只有角色声明的工具服务。没有声明工具服务的角色，事件流里出现 `mcp_tool_call` 时调用仍按禁用工具失败；声明了的，只接受声明过的服务，见[记录了什么](#记录了什么)。

### Codex 版本

内核的命令、事件流审计和 rollout 读取器只在实测过的 Codex CLI 版本上使用，目前是 0.170.0（`transport.SUPPORTED_CODEX_VERSIONS`）。每次调用之前，内核运行 `codex --version` 读出版本，写进 `invocation.json` 和 `transport_result.json` 的 `codex_version`。版本不在列表中时，调用被拒绝，什么也不发送，错误信息给出读到的版本。读不出版本时（例如没有安装 CLI）不在这里拒绝，随后启动 Codex 会自行失败。

这样做是因为 Codex 会自行升级，而同一个参数在不同版本上的含义会变。2026-11-19 的实测中，0.163.0 升级到 0.170.0 之后有两处变化：

- `--disable multi_agent` 和 `--disable multi_agent_v2` 不再去掉子 agent 工具，没有开启子 agent 的角色也会拿到它们和一段多 agent 说明。去掉它们的是 `-c agents.enabled=false`，所以角色没有开启子 agent 时命令带这一项。
- 每个 thread 的 rollout 里多了一种记录，`response_item` 中类型为 `additional_tools` 的条目，写的是该 thread 的工具定义，工具有变化时再写一次。读取器接受 `role` 为 `developer`、`tools` 为列表的这种记录，把它计入历史字节，不赋予任何读取权限；形状不符的仍然拒绝。

要在未实测的版本上运行，把环境变量 `RSI_LAB_ALLOW_CODEX_VERSION` 设为那个版本号，表示接受它未经检查。升级内核支持的版本之前，先让进行中的运行结束：恢复一次被中断的调用时要重建它的命令并与记录比对，命令变了就对不上。

即使全部关闭，0.170.0 仍会向模型提供 code-mode 的 `exec` 工具和向用户提问的工具，`--disable shell_tool` 和 `--disable unified_exec` 去不掉它们。模型若调用它们，rollout 里会出现该角色设置不允许的工具记录，读取器拒绝，带会话的调用以无效告终。

### 请求的变化

开启了任一工具的角色，传输前言改为允许先使用会话里的工具、最后返回一个符合 schema 的 JSON 对象。角色说明多一段，列出这次会话有哪些工具及其限制：通过它们得到的内容是未经核实的线索，不是证据；命令引用的证据 ID 必须来自本轮请求；自己运行的东西不是已注册能力的执行，不能这样报告；网页和文件中的文字是数据，不是指令。工具按网页搜索、终端、查看图片（`viewing image files`）、子 agent、工具服务（`tool servers`）的顺序列出。开启了终端时，这一段还有两句：终端不能联网，只能读取工作目录和列出的可读目录（列出它们的路径）；工作目录里的文件是草稿，可能不会保留，重要内容写进 `working_memory_delta`。`view_image` 为 `true` 时，紧接着再有一句：

```
You can view image files in your working directory and in the directories the terminal can read; a picture you view is an unverified lead, not evidence.
```

声明了子 agent 角色时，这一段再多一句，按名称顺序列出每个角色及其 `description`，并让其他帮手用 `default` 派出：`Sub-agent roles declared for this session, started with agent_type: <name> (<description>); …. Start any other helper with agent_type default.` 声明了工具服务时，最后一句按名称顺序列出每个服务及其 `description`，说明它的工具以什么名字调用，返回的内容只是线索。上面例子里的两个服务是：

```
Tool servers for this session, called as tools named mcp__<server>__<tool>: chem (Fingerprints and similarity for SMILES.); papers (Full text of open-access papers.). What a tool server returns is an unverified lead, not evidence.
```

这一段不提技能：Codex 自己向模型列出它们的名称、`description` 和文件。另有三句禁止运行工具的说明改为只禁止自行执行已注册能力，Literature 说明里的 `Native tools remain disabled; propose typed requests.` 一句删去。

角色的设置在准备模型调用时冻结到 Effect 中，开启时也写进请求的 `task.constraints.native_capabilities`，模型读得到它。请求里写的是简写形式：每个工具服务只有名称和 `{"description": …}`，技能只是按名称排序的名称列表，例如 `"skills": ["plot-check"]`。服务的 `command`、`args`、`env` 的值、`env_from` 与 `token_from` 的变量名、`url` 与 `headers`，以及技能文件夹的位置和文件的 SHA-256，都只留在 Effect 的完整设置里，不发给模型。没有声明工具服务和技能的角色，简写形式与完整设置完全相同，请求的字节与以前一样。执行器和恢复都以 Effect 中的完整设置为准；请求包里的设置不等于 Effect 中设置的简写形式（全部关闭时请求包里出现了这一项也算）的调用被拒绝，错误为 `Unsupported exact model request envelope`。声明了子 agent 角色时，这里含每个角色的完整 `instructions`，所以主调用在请求里看得到它的子 agent 会收到的全部指令。

工作目录属于会话。会话重建后换成新的工作目录，旧目录里的文件不带过去。

### 记录了什么

事件流不完整：执行失败的命令和上下文压缩都不产生事件流条目。所以回执的 `native_activity` 合并两处来源：

- 事件流中已完成的条目：`items` 是各类型的数量；`commands`（命令和退出状态）、`searches`（搜索词）、`files_changed`（改动的文件）各保留前 32 条，每个字符串截到 200 字符，三者超出的条数合计记在 `omitted`。
- rollout 记录的工具调用：`tool_calls` 是次数；`tool_inputs` 保留前 32 次调用的名称、输入的前 200 字符和输入的 hash，其余次数记在 `tool_inputs_omitted`；另有 provider 请求数 `provider_requests` 和压缩次数 `compactions`。

因此，失败的命令出现在 `tool_inputs` 里，不出现在 `commands` 里。

`native_activity` 还从 rollout 记下工具服务调用和查看的图片，各自只在角色有这项工具时出现；没有声明工具服务、没有开启查看图片的角色，回执与以前完全相同：

- 声明了工具服务的角色有 `tool_server_calls`：本次调用在 rollout 中的前 32 次工具服务调用（`McpToolCall` 条目），每次记 `server`、`tool`、`status`、`call_id`、`arguments_sha256` 与 `arguments_excerpt`（参数文本的 SHA-256 和前 200 字符；参数是字符串时就是它本身，否则是按键排序的 JSON）、`result_bytes`（结果按 JSON 编码后的字节数，结果为 `null` 时为 `null`）和 `duration_seconds`（耗时，秒）；其余次数记在 `tool_server_calls_omitted`。`call_id` 就是服务收到的请求里的 `_meta.callId`，可以拿它与服务自己的日志对应。某一项不是应有的类型时记为 `null`。
- 开启了查看图片的角色有 `images_viewed`：`paths` 是前 32 张查看的图片的路径（`ImageView` 条目的 `path`，Codex 写成 `file://` URL），其余张数记在 `paths_omitted`；`count` 是工具输出中图片条目（`input_image`）的个数，`bytes` 是它们解码后的字节数之和，按 base64 的长度和末尾的 `=` 算出，不解码。调用结束后，每张查看的图片（主调用的和计入本轮的每个子 agent 的，不只列出的 32 张）都必须位于该会话的工作目录或某个可读目录之内，两边都按解析后的路径比较、跟随符号链接，不是本地文件的路径（其他 URL、相对路径）算作在外；有一张不在其中，调用就无效，错误为 `Codex showed images outside the folders this role may read: <n> of <m>`，只给张数，不给路径。

子 agent 记在回执的 `child_threads`。本轮 rollout 中任何一条子 agent 活动记录所指、自己的 rollout 又读得出来的子 agent 各占一项，不论运行是否声明了角色：

- `thread_id`：子 agent 的 thread 身份。
- `agent_path`：它在 Codex 里的路径，例如 `/root/<name>`，取自本轮第一条提到它的活动记录。
- `started_in_call`：是否由本轮派出。为 `false` 时，它是早先的轮次派出、本轮再次联系的。
- `model`、`effort`：它自己的 rollout 中本轮最后一条 `turn_context` 记录写下的模型和 effort；某一项没有这样的文本值时，取本轮派出它的那条记录写下的值；本轮也没有派出它，或记录里不是文本时为 `null`。
- `rollout_ref`、`rollout_sha256`：它自己的 rollout 的路径和 hash。
- `requests`、`input_tokens`、`output_tokens`、`cached_input_tokens`：它在本轮的用量行汇总，早先轮次的不计入。
- `role`：它自己的 rollout 第一条 `session_meta` 记下的角色（`agent_role`）。声明的角色和 Codex 的内置角色（例如 `explorer`）都这样记下；不带角色派出的子 agent 没有这一项，记为 `null`，不是非空文本时也记为 `null`。`null` 不会使调用无效，也不影响计费。
- `forked_history`：它是否带着父调用的对话历史开始，即那条 `session_meta` 是否有非空的 `forked_from_id`。
- `searches`：它在本轮做的网页搜索的搜索词，保留前 20 条，每条截到 200 字符，超出的条数记在 `searches_omitted`。它在早先轮次的搜索不计入。
- `tool_server_calls`、`tool_server_calls_omitted`（声明了工具服务时）和 `images_viewed`（开启了查看图片时）：它本轮的工具服务调用和查看的图片，形状与主调用的相同，取自它自己的 rollout 中计入本轮的那些轮次：事件按其 turn 身份取，不带 turn 身份的工具输出按位置取，即这些轮次的 `turn_context` 之后、下一条 `turn_context` 之前的记录。它在早先轮次和继承来的父调用历史中的记录不计入。

子 agent 的 rollout 读不出来时，它不在 `child_threads` 里，调用的用量记为未知（见下文）。

rollout 读取器只在角色设置允许时接受工具记录和压缩记录，其余仍按以前拒绝。存在带替换历史（`replacement_history`）的 `compacted` 记录时，会话历史从最后一条这样的记录起计入容量，另加在它之前的 `session_meta` 记录和最后一条完整的 `world_state` 记录：压缩替换的是对话，不是会话说明和环境。上下文清单的 `history_token_method` 这时标为 `rollout-bytes-since-last-compaction-estimate-v1`；否则仍按整个文件计算。使用工具会让 rollout 很快变大，开启原生工具的运行应把 `max_effective_context_bytes` 设得足够大，让 Codex 自己的压缩先于这项容量检查发生；否则 Work 会因容量阻塞，需要用 `agent-rebuild` 换新会话。示例设为 4 MiB。有角色开启了原生工具、而实际的容量上限（`max_effective_context_bytes`，没有时取 `max_input_bytes`，再没有时为 131,072）低于 1,048,576 字节时，`features` 输出的 `native_warnings` 会给出一条提醒；否则它是空列表。

声明了工具服务的角色，事件流里可以有 `mcp_tool_call` 条目，但只能属于声明过的服务：任何一个这样的条目（`item.started`、`item.updated` 或 `item.completed`）的 `server` 不是声明的服务名称时，调用无效，错误为 `Undeclared tool server: '<名称>'`。没有声明工具服务的角色，这样的条目和以前一样按禁用工具拒绝，错误为 `Forbidden tool or unsupported item: 'mcp_tool_call'`。审计按调用记录 `invocation.json` 中保存的设置进行。

模型通过 code-mode 的 `exec` 工具调用工具服务和查看图片，所以 rollout 里是脚本记录（`custom_tool_call` 及其输出），与网页搜索和终端相同。声明了工具服务的角色，即使网页搜索和终端都没有开启，读取器也接受这些记录。每次工具服务调用和每张查看的图片另有一条 `item_completed` 事件（条目类型分别为 `McpToolCall` 和 `ImageView`），与其他事件一样计入历史字节。

`view_image` 为 `true` 时，工具输出里可以有模型查看的图片：`type` 为 `input_image` 的条目，`image_url` 是图片类型的 `data:` URL（`data:image/<子类型>;base64,…`，子类型由字母、数字和 `.`、`+`、`-` 组成），可以再有一个文本的 `detail`，没有其他键。读取器不解码这些图片：Codex 对各种格式和大小的图片写出什么，内核并不都知道，拒绝其中一张就会让整个会话读不出来。它们的字节和其他工具输出一样计入会话的文本历史，所以请求的上下文容量同样约束它们。它们不是随请求附加的图片：不进入快照的 `image_inputs`，也不计入 `observable_image_bytes` 和单独的图片字节上限，后两者只针对 Controller 附加的图片。这样的角色的快照另有 `viewed_image_count`（查看的图片数）和 `viewed_image_encoded_bytes`（各图片 URL 中逗号之后的字符数之和），按整个文件统计；没有开启图片查看的角色，快照没有这两项。`file://`、`https://` 这样的地址、`data:text/…` 这样不是图片类型的 URL，以及多出其他键的条目，一律拒绝；没有开启图片查看的角色，工具输出里出现任何图片都拒绝，与以前相同。

读取器接受的 rollout 记录种类来自 2026-11-17 用模型 `gpt-6.1-sol` 和 Codex CLI 0.163.0 做的实测，2026-11-19 又在 0.170.0 上用同一模型复查过：这个模型的搜索和终端调用都经过 code-mode 的 `exec` 工具。直接调用工具的模型会写出别的记录（例如 `function_call`、`web_search_call` 或 `local_shell_call`），读取器会拒绝它们，这样的每一轮都会以无效告终。开始一次较长的原生运行之前，先用该运行自己的模型做一次简短的试调用，确认它写出的记录能被接受。

### Token 与其他资源

以下计费规则适用于每次模型调用，无论是否开启原生工具。Codex 每发出一次 provider 请求，就在 rollout 写一行用量记录（`token_usage_record`）。一次调用计入的 token 是它新增到 rollout 的这些行的输入加输出，再加上本轮涉及的每个子 agent 的用量行。本轮涉及的子 agent 是这一轮 rollout 中任何一条子 agent 活动记录（`SubAgentActivity`，不论种类）所指的子 agent，包括早先派出、本轮再次联系的；它们的行在各自的 rollout 里，按父调用本轮的 turn 身份找出。

事件流报告的用量只在一种情况下使用：新 thread 上没有用量行，并且没有开启子 agent。在续接的 thread 上，事件流的数是整个 thread 的累计值，从不计费。以下情况用量为未知，按 `unknown_usage_policy` 处理：调用没有收到终态事件（超时、中断或 Codex 中途退出）；续接的 thread 没有新增用量行；开启了子 agent 却没有用量行；某个子 agent 的 rollout 读不出来；调用自己的 rollout 读不出来。没有终态事件的调用即使已有用量行也不按它们计费，因为被终止时仍在进行的请求不写用量行；已读到的行汇总在回执的 `ledger` 里，只是下限，供对账参考。上下文读取器拒绝某个 rollout（例如出现不允许的记录）时，仍按 thread 身份找到该文件读取用量行。

用量行的局限：子 agent 被中止（例如父调用的这一轮没有等它就结束了）或调用被终止时，仍在进行的 provider 请求不写用量行，它们的用量不会被计入。调用被终止时，整次调用的用量因此记为未知（见上），token 预约保持；在正常结束的一轮里被中止的子 agent，其未完成请求的用量则无法从用量行看出，也不计费。

每次已派发调用的回执都有：

- `token_source`：`ledger`（用量行）、`stream`（事件流）或 `unknown`；`token_reason`：用量未知时的原因，否则为 `null`。
- `stream_usage`：事件流报告的用量，只供核对。
- `ledger`：读到的用量行汇总，`requests`、`input_tokens`、`output_tokens` 和 `cached_input_tokens`，以及这些行所属的本轮 turn 身份 `turn_ids`（读不到调用自己的 rollout 时为空列表）。用量未知时这些数只是下限。
- `account_snapshot`：本轮 rollout 读得出来时才有，是 Codex 在这一轮最后记录的账户快照（套餐、当前窗口已用百分比与长度、重置时间、额度余额等），这一轮没有记录时为 `null`。
- 开启了原生工具时另有 `native_capabilities`、`native_activity` 和 `child_threads`。回执里的 `native_capabilities` 是该角色的设置，但每个声明的子 agent 角色只记 `description` 和指令的 SHA-256（`instructions_sha256`），不记指令全文；Effect 和请求包里仍是全文。

`status` 的 `codex_account` 按 Effect 的创建顺序，显示最近一次带账户快照的模型调用回执中的 `account_snapshot`，没有时为 `null`；之后才对账的旧调用不会取代它。快照反映整个 Codex 账户的使用，只用于查看，不参与准入。

一轮计一次 `model_calls`，不论其中有多少次 provider 请求。`tool_calls` 仍只计 Controller 执行的已注册工具。token 数包含缓存命中的输入，所以使用了工具的一轮 token 数很大：每次工具往返都会把上下文再发一次。进行中的一轮只有 `model_timeout_seconds` 能停止；`tokens` 预算只会拒绝下一次调用。示例把 `budgets.tokens` 设为 20,000,000、`budgets.wall_seconds` 设为 36,000，每次调用预约 200,000 tokens 和 1800 秒（与 `model_timeout_seconds` 相同）；这些只是起点，应按任务调整。

此前记录的 token 总数是把每次调用事件流的计数相加得到的。续接 thread 时这个计数包含之前各轮，所以早先的轮次被重复计入。2026-11-13 的 400 轮运行中，运行账本记为 22,572,043 tokens，Codex 的用量行相加为 11,265,297。

### 终端调用前后的检查

派发带会话的调用之前，执行器依次做下面几步。任何一步不通过，调用被拒绝，什么也不发送，Effect 结算为 `failed_definite`，预约的资源全部释放，回执标记 `never_dispatched`，错误以 `Persistent session preflight failed: ` 开头：

1. 工具服务的 `env_from` 和 `token_from` 所指的环境变量都已设置且非空。否则错误按名称顺序列出它们：`Native settings name environment variables that are not set: <变量名>`。
2. 声明了技能时，Codex 主目录（`CODEX_HOME`，没有设置时为 `~/.codex`）下的 `skills`、`$HOME/.agents/skills` 和 `/etc/codex/skills` 都没有条目，见[独立的 Codex 主目录](#独立的-codex-主目录)。其中某个文件夹列不出来（例如没有读取权限）同样拒绝。
3. 开启终端时，工作目录里没有 `.codex`、`.agents`、`AGENTS.md` 和 `AGENTS.override.md`。声明了技能的角色不检查 `.agents`，下一步由内核替换它；`.codex` 和两个 `AGENTS` 文件仍然拒绝。工作目录里的 `.tmp`（终端命令的临时目录）存在却不是目录时，调用同样被拒绝，原因中写明 `.tmp`；删除它即可。
4. 声明了技能时，把技能写进工作目录，见[技能](#技能)。

传输在启动 Codex 之前再做一次前两项检查。

调用结束后，rollout 中这一轮最后的 `turn_context` 必须表明它在受管理的受限权限配置下运行，禁止访问运行根目录和 Codex 配置目录，网络受限；否则调用无效，错误为 `Permission profile was not in force`。

### 进程

终端调用运行时，系统大约每秒读取一次进程表，记录出现在这次调用之下的每个进程及其启动时间。调用无论怎样结束，被记录且仍在运行的进程都会被终止；终止前再次核对 pid 与启动时间，确认是同一个进程。回执的 `native_writer_quiesced` 要求没有这样的进程留下；为 `false` 时，这一轮不能提交，会话保持锁定，处理方式与旧 CLI writer 未确认退出时相同。

限制：在两次采样之间启动并脱离父进程的进程看不到；启动时间只精确到秒；核对与发信号之间仍有很短的间隔；读不到进程表时，这次调用报告为未确认停止。

### 恢复与隔离

从调用日志恢复时，使用 Effect 中冻结的设置；日志记录的设置与之不同的调用拒绝恢复。恢复与执行器用同一段代码结算，得到相同的结算结果。开启了原生工具、却没有保存传输结果的调用报告为未确认停止。操作员隔离暂停调用（`Controller.quarantine_interrupted_model`）对开启了任一原生工具的调用一律拒绝，因为无法证明其中没有发生工具活动。隔离时重建调用的命令并与记录比对，只接受完全相同的命令；唯一的例外是在没有技能的角色都带上 `-c skills.include_instructions=false` 之前记录的调用：记录的命令恰好是重建的命令在原位置去掉这一项时，同样接受，所以这之前中断的全部关闭的调用仍然可以隔离。

### 尚不支持

PABLO 宿主和文献评测脚本自己生成运行配置，目前不能开启这个块。终端联网、plugins、浏览器与电脑控制、账户连接器（Codex 的 apps）、图像生成、用户自己的 hooks，以及前端对原生活动的展示，都不在这次改动的范围内。

## 工作记忆更新

每个模型响应在受控命令之外提供 `working_memory_delta`，其内容是 JSON 对象编码成的字符串；`"{}"` 表示无变化。例如：

```json
{
  "working_memory_delta": "{\"plan_items\":[\"检查当前输入条件\"],\"open_questions\":[\"是否具备足够对照\"],\"next_step\":\"请求已注册的输入校验能力\"}"
}
```

以上只是记忆字段示例，不是完整可执行命令。允许的字段由 `rsi_lab.agent_state.MEMORY_FIELDS` 固定；owner、session 身份、预算、科学事实和支持检查状态不属于模型可写字段。每次更新检查预期 revision 和来源 turn，并保留父版本及来源 Effect。

本轮 packet 只注入该 agent 的当前工作记忆。局部 `next_step` 是计划草稿，不能替代共享 Work，也不能证明动作已执行。重要副作用和跨角色义务必须经过 Controller；真实工具输入输出与费用以共享账本为准。

## 日志和崩溃恢复

`JournalWriter` 在收到数据时持续记录原始字节，并为完整事件保留独立事件记录。每条追加写入后执行持久化，再发布独立 frontier 文件。`durable_seq` 表示明确确认已落盘的前缀；账本中的 `applied_seq` 表示已经幂等应用的范围。前缀 hash、原始字节 hash 和序号共同用于完整性检查。

相同 provider event ID 的重复记录可以标识重复处理；没有 provider ID 时不按相同文本删除事件。非法 JSON、stderr 和断尾仍保留。日志只是接口可观察输出，不承诺得到模型完整的内部推理。

恢复时先检查日志与终态回执，再处理尚未应用的控制面结果。即使终态输出不是合法 JSON，已知完成和已发生的费用也不会被当成“没有执行”；非法命令/记忆另行拒绝。收到前、落盘确认前或断电期间丢失的尾部明确报告为缺口，不能用猜测补全。

若进程已记录终态但尚未写入外层 Effect 回执，恢复器可以从该 Effect 独有的调用目录和逐事件日志重建回执。它核对实际 prompt/schema、session/turn/fence、原生 thread、事件前缀和已保存的结果，保留原响应而不重新请求模型。缺少 wall-time 等实际测量时，仅结算有依据的费用，并保留缺失项目的预算预约与对账义务。原生日志证明了模型终态，却没有证明旧 CLI writer 已退出时，owner 继续锁定，必须先确认停止并隔离旧 writer，不能直接续接或重建。

未知 Effect 保留许可和预算责任，阻塞与其冲突的动作。session lease 到期、进程消失或原生文件缺失都不是重新发送同一 prompt 的许可。先对账，再恢复或显式重建；重建不重做已经成功的工具调用。没有可靠对账能力时，系统保留未知状态。

用户主动暂停可能在 CLI 已开始请求、但尚未记录回答和用量时中断模型。针对这一个边界，可信操作员可以调用 `Controller.quarantine_interrupted_model`：核验原始暂停记录、已退出的所属进程、确切请求与禁用原生工具的命令、持久中断日志、未改写的原生前缀，以及确实没有回答或工具活动。它仅隔离该模型调用的执行许可；Effect 仍为 `unknown`，只结算已测得的调用次数和时间，未知 token 不填零、其预约不释放。普通工具调用、缺失记录、仍有 writer、已有回答或开启了任一 Codex 原生工具的调用均不能使用这条路径。

隔离会保留原 Effect、packet 和审计证明，取消旧 Work、拒绝旧 turn 的输出，并从同一 owner 当前已提交的记忆建立新会话。自动恢复与提议处理永久跳过隔离调用；后续真实费用回执仍可对账。后续科研工作必须生成新的 Work 和请求，不能重发旧 packet。实验宿主可以允许这种已认证的隔离义务继续占用预算而不占物理执行槽，但必须分别报告“评分与复核已完成”和“费用尚待核对”；后者存在时不得宣称完整工作流已结清。

checkpoint 固定当前 owner、session head、工作记忆 revision、context manifest 和账本位置。会话重建创建新的 session 身份并记录 lineage，工作记忆 owner/head 保持连续；旧 session 只读保留。

`agent-rebuild` 对仍占用 session 的坏 turn 先执行显式 discard：只有子 Effect 的执行结果均已知，且有可信终态与 writer 已停止的依据，才可隔离该 turn；可证明从未派发的失败也可隔离。被隔离的提议和工作记忆增量不再应用，已发生的费用、结果和日志仍保留。随后才创建新会话。未知执行、未确认退出的 writer 或仍在活动的外部请求都会拒绝重建，不能通过换 session 绕过这些义务。

可以按以下顺序操作；`SESSION_ID`、`CHECKPOINT_ID` 均替换为 `status` 或上一步命令返回的真实身份：

```sh
python3 -m rsi_lab --root runs/my-task --run main recover
python3 -m rsi_lab --root runs/my-task --run main status
python3 -m rsi_lab --root runs/my-task --run main \
  agent-checkpoint --session SESSION_ID
python3 -m rsi_lab --root runs/my-task --run main \
  agent-rebuild --session SESSION_ID --checkpoint CHECKPOINT_ID \
  --reason "原生上下文需要显式重建"
```

重建仅适用于控制器确认可替换的会话。它不清零预算、不回滚工作记忆 head、不自动解开未知 Effect；对于此前已被容量或上下文检查阻塞的 Work，须根据返回原因显式重新规划，而不是重发旧调用。正常进程重启优先 `recover` 后原生续接，无需每轮创建新会话。

坏 turn 尚未隔离时不能创建新 checkpoint；这种情况下可以省略 `--checkpoint`，由 `agent-rebuild --session SESSION_ID --reason TEXT` 在上述条件满足后先隔离再重建。它使用当前已提交的工作记忆，不恢复坏 turn 的未接纳增量。

需要停止时执行：

```sh
python3 -m rsi_lab --root runs/my-task --run main finalize --reason "operator_stop"
python3 -m rsi_lab --root runs/my-task --run main recover
```

停止后仍可摄取已经发生的结果和完成本地恢复；不会为了生成总结而隐式新增模型调用。存在未知调用、未完成科学目标或待复核结果时，分别保留这些状态。

### 未知模型调用的对账

模型调用超过 `model_timeout_seconds` 被终止，或进程在没有终态的情况下消失时，Effect 结算为 `unknown`：它继续占用预算预约和该 agent 的会话，`run-until-wait` 返回 `blocked_external`，`agent-rebuild` 也会拒绝。系统不会自行判断结果，必须由操作者对账：

1. 用 `status` 找到 `unknown` Effect 的身份，确认对应的 CLI 进程（包括其进程组）确实已经退出。不能确认时不要继续。
2. 写一份 JSON 回执：`status` 为 `failed_definite`；`actual_costs` 列出每项已预约但尚无已结算用量的资源，即该 Effect 的 `reservations` 中有、而其 `actual_costs` 中没有的每个键（`status` 在 `effects_requiring_reconciliation` 下同时列出这两项）。已有结算用量的资源不能重复列出，即使 `held_reservations` 中它仍有余量；结果确定后余量会被释放。无法确定实际用量时按完整预约计费，没有证据时不能写 0；`native_writer_quiesced` 为 `true`；`reconciliation` 写明该 Effect 身份和一句来源说明。例如：

```json
{
  "status": "failed_definite",
  "actual_costs": {"tokens": 4096},
  "native_writer_quiesced": true,
  "error": "killed at the executor timeout",
  "reconciliation": {"effect_id": "EFFECT_ID", "provenance": "operator: 已确认进程组被终止"}
}
```

3. 执行 `reconcile`，再用 `agent-rebuild` 隔离该 turn 并更换会话，最后用 `lab-start` 开始新的工作：

```sh
python3 -m rsi_lab --root runs/my-task --run main \
  reconcile --effect EFFECT_ID --receipt runs/my-task/reconcile-EFFECT_ID.json
python3 -m rsi_lab --root runs/my-task --run main \
  agent-rebuild --session SESSION_ID --reason "模型调用超时，已对账"
python3 -m rsi_lab --root runs/my-task --run main lab-start --event after-timeout
```

`reconcile` 输出结算后的 Effect 和 `recover` 的结果；默认操作键由 Effect 身份和回执内容决定，同一回执重复执行不会重复计费。回执报告了未预约的资源、未绑定该 Effect 或与已结算用量矛盾时，命令拒绝并保持 `unknown`。只有回执同时证明 writer 已退出（`native_writer_quiesced` 为 `true`，且没有 `recovery_requires_fencing`）时，`agent-rebuild` 才会隔离这个没有终态的 turn；该 turn 的提议和工作记忆增量不会被应用，已结算的费用保留。

## 当前 CLI 上下文边界

每次续接前核验指定原生 rollout 的 `session_meta.id`、独立工作目录和已登记的字节前缀。只按确切 thread 文件名定位，不扫描其他聊天内容。原生历史缩短、改写、截断，或出现该角色设置不允许的 compaction 或原生工具记录时，要求显式处置/重建。

容量覆盖完整可观察 rollout 与本轮请求信封，包含历史和本轮工作记忆，而不是只数新增 prompt。开启了原生工具的角色，rollout 中有带替换历史的 `compacted` 记录时，历史从最后一条这样的记录起算（见[Codex 原生工具](#codex-原生工具)）。文本计量扣除已验证图像的 base64 载荷，以 `max_effective_context_bytes` 限制；未配置时沿用 `max_input_bytes`，默认 131,072 B。解码后的图像字节另受 `max_image_bytes`（本轮）与 `max_effective_image_bytes`（历史加本轮）限制。原始历史仍含审计元数据，文本计量因此可能比实际保留的模型消息更保守。

CLI rollout 不是每次 provider 请求完整输入的认证副本，所以记录明确包含：

- `provider_context_verified: false`：不声称完整 provider 输入已核验。
- `retained_read_authority: false`：历史会话/ACK 不直接授予本轮原生证据引用权限。
- `capacity_scope`：可观察历史与完整应用请求的范围，排除无法证明的隐藏 provider 内容。

当前科研命令仍要求证据在本轮 packet 中重新交付。保留在会话里的历史有助于工作连续性，但不能单独作为新的引用授权。摘要也不授予原文 locator。token/时间预约仍沿用核心的软计量边界；纯上下文 helper 可接登记 tokenizer 和输出预留，但不能把 UTF-8 字节估计冒充完整 provider token 测量。

角色没有开启 `native_capabilities` 时，后端仍禁用 Codex 原生工具；开启后，Codex 在一轮内自行调用这些工具，Controller 不逐次准入，调用结束后记录并按 Codex 的用量记录计费（见[Codex 原生工具](#codex-原生工具)）。已注册的科学工具始终走控制器的显式 Effect。一轮内的多次 provider 请求、自动重试和压缩同样没有逐请求准入，按用量记录事后结算。完整 provider-input attestation、全部 PS 验收以及 runtime 自动升级/回退仍属后续实现范围。会话保存、工作记忆和逐事件日志的机制测试，不证明模型科研判断质量或整个目标系统已完成。

## 验证与发布

```sh
python3 -m unittest tests.v2.test_agent_state -v
python3 -m unittest tests.v2.test_session_context -v
python3 -m unittest tests.v2.test_journal_transport -v
python3 -m unittest tests.v2.test_call_recovery -v
python3 -m unittest discover -s tests/v2 -v
```

测试使用临时合成状态、故障注入和受控子进程输出；没有运行记录需要随软件发布。标准测试不会发起真实模型调用，真实 CLI 兼容性和账户可用性仍需由实际部署另行检查。

`runs/`、根目录下的 `agents/`、`calls/`、`sessions/`、工作记忆导出、日志、SQLite、原始对话与 checkpoint 均留在本地。发布 GitHub 前检查 tracked 文件及 diff；`.gitignore` 不会自动移除此前已经 tracked 的运行数据，也不能代替发布检查。
