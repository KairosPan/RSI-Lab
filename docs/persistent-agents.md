# Lab / Meta 持久会话、工作记忆与执行日志

通用 `rsi_lab.Runtime` 把 Lab 与 Meta 接到同一 Work/Effect 控制面，同时为两者保存独立的持久 Codex 会话和工作记忆。模型输出受控命令，Controller 校验后执行已注册工具；模型不能直接修改账本、执行工具或认证科学结论。

本页描述当前实现。完整目标契约、长期科研记忆和后续验收见[系统设计 §9](system-design.html#persistent-agent-sessions)。系统设计中的 PS01–PS12 是验收要求，不代表当前 CLI 后端已满足所有底层能力。

## 数据保存在哪里

为每个任务选择独立运行根目录，例如 `runs/my-task`。不要把运行目录与源码目录混在一起。

| 层 | 保存内容 | 权威与共享范围 |
| --- | --- | --- |
| 原生 Codex 会话 | 对话消息、原生历史和可观察运行信息 | Lab 与 Meta 各用精确的 `provider_thread_id`，不使用 `--last`，也不续接对方 thread。原生文件由 Codex 保存，不作为预算或已执行状态的权威 |
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
4. Codex 初次调用创建持久会话；以后使用同一精确 thread 续接。正常 Lab/Meta 调用不使用 `--ephemeral`。
5. 调用原始事件逐条落盘。已完成执行、JSON/命令是否合法、工作记忆增量是否合法分别处理。
6. 子 Effect 结算后，由父 turn 的幂等提交推进 session head、接纳合法工作记忆增量并释放会话写入权。空增量保持原 head；非法增量保留旧状态和拒绝原因。
7. Lab 可以请求已注册的诊断/科学适配器。控制器执行工具、登记来源事实，再创建 Lab continuation。新来源也可触发 Meta 观察工作。

`run-until-wait` 是有步数上限的合作式驱动器：推进当时可运行的 Lab、Meta 和已注册 diagnostic，遇到依赖、资源限制、未知调用或无工作时返回。宿主在新输入、真实结果或 deadline 到达后再次调用；它不是隐藏的后台 daemon，也不会因为等待或 `no_action` 就自动宣布科学任务成功。

Lab 和 Meta 的工具权限由本轮命令模式与已注册能力决定，不来自会话里曾出现的文字。当前 Lab 命令为 `request_diagnostic`、`propose_issue` 和 `no_action`。Meta 的 discovery/disposition 模式仍使用明确命令集合。

## 运行入口

先配置已登录的 Codex CLI、明确的模型/effort、运行预算、任务输入和受信适配器。真实模型调用要求运行配置的 `execution_mode` 为 `live` 或 `role_recall`；合成测试不需要模型凭证。

以下是最小运行配置模板。先把 `YOUR_MODEL_ID` 改为自己账户可用且任务明确选择的模型，并将 `goal` 改为实际任务；预算数值只是工程示例，不代表某项科研任务足够。将文件保存为 `runs/my-task/run-config.json`，该目录已被 Git 忽略。

```json
{
  "task_id": "my-task",
  "goal": "检查任务输入条件，并选择下一项获准的工作",
  "execution_mode": "live",
  "model": "YOUR_MODEL_ID",
  "effort": "medium",
  "max_external": 1,
  "max_input_bytes": 65536,
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

`model_calls` 和 `tool_calls` 是控制器可执行的调用次数限制；token/时间是显式软预约与实际用量结算，不构成 provider 内部的硬停止保证。模型选择、总预算与既有运行配置不可通过恢复偷偷改变；新任务用新的运行目录。

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
| `run-until-wait --max-steps 20` | 推进就绪的 Lab/Meta/工具工作后返回，保留尚未完成的义务 |
| `status` | 查看会话、turn、待执行/阻塞工作、预算和需要对账的 Effect |
| `recover` | 摄取持久回执，或从所属调用日志恢复已知终态；补做幂等发布/提议处理，不盲目重发未知调用 |
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

## 当前 CLI 上下文边界

每次续接前核验指定原生 rollout 的 `session_meta.id`、独立工作目录和已登记的字节前缀。只按确切 thread 文件名定位，不扫描其他聊天内容。原生历史缩短、改写、截断、出现不支持的 compaction 或原生工具项目时，要求显式处置/重建。

容量按完整可观察 rollout 原始字节加本轮请求信封计量，包含历史和本轮工作记忆，而不是只数新增 prompt。`max_effective_context_bytes` 控制该容量；未配置时沿用 `max_input_bytes`，默认 131,072 B。整个原始历史含审计元数据，计量因此可能比实际保留的模型消息更保守。

CLI rollout 不是每次 provider 请求完整输入的认证副本，所以记录明确包含：

- `provider_context_verified: false`：不声称完整 provider 输入已核验。
- `retained_read_authority: false`：历史会话/ACK 不直接授予本轮原生证据引用权限。
- `capacity_scope`：可观察历史与完整应用请求的范围，排除无法证明的隐藏 provider 内容。

当前科研命令仍要求证据在本轮 packet 中重新交付。保留在会话里的历史有助于工作连续性，但不能单独作为新的引用授权。摘要也不授予原文 locator。token/时间预约仍沿用核心的软计量边界；纯上下文 helper 可接登记 tokenizer 和输出预留，但不能把 UTF-8 字节估计冒充完整 provider token 测量。

当前后端禁用 Codex 原生工具，实际科学工具走控制器的显式 Effect。原生内部多请求、自动重试、压缩的逐请求准入代理、完整 provider-input attestation、全部 PS 验收以及 runtime 自动升级/回退仍属后续实现范围。会话保存、工作记忆和逐事件日志的机制测试，不证明模型科研判断质量或整个目标系统已完成。

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
