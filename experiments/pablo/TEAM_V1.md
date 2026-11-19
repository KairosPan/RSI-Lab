# PABLO 团队运行：team v1

本次只修改代码、文档和离线测试。没有准备、启动或执行过任何团队运行，没有调用模型，也没有使用网络；下面描述的是准备好的配置和宿主检查，不是实验结果。

## 什么是团队运行

`run.py --team` 准备一个新运行，架构为 `pablo_research_pr4_team_v1`，Harness 版本为 `pablo_lab_led_v1`。Lab 领导一个小组：它可以用 `agent_type` 启动 `local`、`challenge` 两个具名子代理，以及 Codex 的 `default` 帮手。子代理只提议，不能评分也不能选点；子代理返回的内容是线索，不是证据。只有 Lab 请求 `pablo.select_candidates` 和 `pablo.evaluate`。

不带 `--team` 的准备与之前完全相同：同样的配置、目标文本、工具和进度报告。旧架构的运行也不变。父运行是团队运行时，不带 `--team` 的准备在写入任何内容之前被拒绝，这样的父运行只能用 `--team` 继续；`--team` 准备照常进行，原生设置块取自这次给出的文件。

## 选项

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `--team` | 关 | 准备团队运行 |
| `--native-capabilities FILE` | 无 | JSON 文件，内容是内核读取的 `native_capabilities` 块；`--team` 必须提供，没有 `--team` 时拒绝 |
| `--challenge-backstop-batches N` | 4 | 后备规则的批次数，1 到 100，或 `off` 关闭；没有 `--team` 时拒绝 |
| `--rebuild-history-bytes N` | 3,145,728 | 空闲会话的原生历史达到这个大小时重建，262,144 到 3,145,728；没有 `--team` 时拒绝 |

这四个选项只用于新的准备，`--resume` 时一律拒绝。团队运行的每次请求上下文上限 `max_effective_context_bytes` 固定为 4,194,304 字节。内核在历史加上本次请求超过这个上限时拒绝请求，所以重建大小的默认值和最大值都是这个上限减去宿主允许的最大请求（`--max-input-bytes` 的上限 1,048,576），即 3,145,728：会话在下一次请求可能因大小被拒绝之前先被重建。

文件中 `lab.subagents` 必须打开（`true` 或对象），不能自己声明 `local` 或 `challenge`：这两个角色由宿主用自己的文字加入。文件里的其他设置和其他角色保留；`subagents` 写成 `true` 时，冻结配置写明内核当前的默认并发数 3。

子代理的 token 记在启动它的 Lab 调用上，所以团队运行需要更大的 token 预算。这个预算由操作者在准备时用 `--additional-tokens` 提高，宿主不会自动调整。

文件中的每个角色还可以设置内核的三个可选键：`view_image`（看图）、`tool_servers`（由 Codex 在本地启动或通过 URL 访问的工具服务器）和 `skills`（技能文件夹）。宿主把它们原样交给内核校验，内核拒绝的设置在写入任何内容之前被拒绝；看图和技能只接受终端打开的角色。工具服务器使运行不再是盲测（见“网络搜索使运行不再是盲测”）；技能文件夹按终端可读目录的规则检查，并在准备时冻结进运行存储（见“被拒绝的终端可读目录”）。工具服务器用 `env_from`、`token_from` 指名的环境变量要在启动宿主的环境中设好，变量的值不进入配置和记录。

声明了技能的运行要在一个独立的 Codex home 中准备和启动：准备（包括 `--prepare-only`）和每次 `--resume` 都在 `CODEX_HOME` 设为这个 home 的环境中运行 `run.py`，和其他任何运行一样，宿主没有专门的选项。这个 home 由操作者自己建立，并在 `CODEX_HOME` 设为它时用 `codex login` 登录一次；其中的 `skills` 除了 Codex 自己的 `.system` 之外不能有任何条目，而 `~/.agents/skills` 和 `/etc/codex/skills` 不论 Codex home 是哪个都必须为空。内核在宿主的 `make_runtime` 构造 Runtime 时和每次调用之前检查这三个文件夹，有条目就拒绝。准备在运行初始化之后才构造 Runtime，所以这项拒绝发生时运行目录已经写入，要换一个新的运行目录重新准备。

示例（路径和数值均为示意）：

```sh
python experiments/pablo/run.py \
  --root /absolute/path/to/runs/pablo_team_example \
  --run-id pablo-team-example \
  --parent-root /absolute/path/to/runs/settled_parent \
  --parent-run-id settled-parent \
  --parent-source-root /absolute/path/to/the/parent/frozen/checkout \
  --new-evaluations 40 --max-model-calls 120 --additional-tokens 50000000 \
  --team --native-capabilities /absolute/path/to/team-native.json --prepare-only
```

```json
{"lab": {"subagents": {"max_concurrent": 3}, "web_search": false,
         "terminal": {"readable_roots": ["/absolute/path/to/public-reference-data"]}}}
```

## 角色

| 角色 | 描述 | 指令 |
| --- | --- | --- |
| `local` | Proposes a batch that refines the leading structural series. | `harness.ROLE_INSTRUCTIONS["local"]`，后接共同结尾 |
| `challenge` | Proposes a batch that argues against the leading structural series. | `harness.ROLE_INSTRUCTIONS["challenge"]`，后接共同结尾 |

共同结尾告诉子代理：它是 Lab 的子代理，不能评分或选点；返回从给定的合格 anchor 中选出的一个 anchor、一组候选 SMILES、假设，以及能证伪它的结果；不要预测分数，也不要猜隐藏目标。文字定义在 `pablo_task/team.py`。

## 谁决定批次角色，后备规则是什么

每个评分批次由 Lab 决定：跑 local 还是 challenge，要不要问 Challenge，采用哪个提议。批次序号不再决定角色。两个停滞信号（连续无改善的批次数和评分数）仍在 Harness 上下文中给出，但只是事实，不决定角色。

唯一的强制是后备规则。计数 `unchallenged_stale_batches` 是本运行中连续的、既没有超过最好分数、也不是至少评了一个候选的 challenge 批次的已结算评分批次数。新运行从 0 开始，不看继承的历史；一个超过最好分数的批次，或一个至少评了一个候选的 challenge 批次，使它归零。计数达到 `--challenge-backstop-batches` 时，下一批的 `required_role` 是 `challenge`：Lab 必须派出 Challenge，并以 challenge 批次运行它的提议。设为 `off` 时从不强制。

## Lab 记录的 decision

每个 `pablo.select_candidates` 请求带一个 `decision`：

- `challenge_dispatched`：这次 Lab 调用是否启动或再次联系了 Challenge 子代理；
- `challenge_adopted`：本批是否运行 Challenge 的提议，只有在 `proposal_role` 为 `challenge` 且 `challenge_dispatched` 为 true 时才能为 true；
- `reason`：一行，1 到 300 个字符。

`decision` 是 Lab 自己的陈述，不属于 Meta 建议的内容。它和选点计划一起保存，消费该计划的评分请求从计划中取得它。

选点请求被接收时，`challenge_dispatched` 与发出这个请求的 Lab 调用的记录比对：为 true 时，该调用的 `child_threads` 中至少要有一个角色为 `challenge` 的子代理（本次启动或再次联系都算）；为 false 时一个都不能有；读不出角色的子代理不算。不一致时请求被拒绝，Lab 像看到其他被拒的工具请求一样看到拒绝原因。操作者直接运行工具（内核命令 `adapter-run`，即 `Runtime.execute_adapter`）不经过这项比对，只检查 `decision` 的格式和后备规则。

这项比对看不到 rollout 读不到的子代理：这样的子代理不在调用记录的 `child_threads` 中（这次调用的用量随之成为未知），所以即使 Lab 确实派出了 Challenge，它在这次调用中如实写出的 `challenge_dispatched: true` 也会被拒绝。

## 每次调用记录的子代理信息

团队运行的每次 Lab 调用记录中，`child_threads` 列出能读到的每个子代理：`thread_id`、`agent_path`、`role`、`started_in_call`（本次调用启动，还是联系了更早启动的子代理）、`forked_history`（是否带着 Lab 的历史启动）、`model`、`effort`、请求数和 token 计数，以及它在这次调用中的网络搜索查询（最多 20 条，每条截到 200 个字符，其余计入 `searches_omitted`）。读不到的子代理使这次调用的用量成为未知，并写明原因。

没有用声明角色启动的帮手，记录为 Codex 给它的角色；Codex 没有给出时为 null。

Lab 可以再次联系更早启动的子代理，而不是新开一个；那个子代理记得它之前的工作。这是允许并记录的，不阻止。会话重建会结束它的全部子代理：重建在步骤之间的边界进行，对象是 rollout 文件已达到 `prospective_rebuild_history_bytes` 的空闲会话，所以这个值也是一个实验臂的组成部分。

目标文本请 Lab 用 `fork_turns` `none` 启动 Challenge，并把它需要的 anchor、约束和结果交给它；是否这样做由 `forked_history` 记录，不强制。

宿主的两个原生记录读取点（`run.py` 的 `startup_preflight` 和 `host_support.py` 的 `rebuild_at_boundary`）按会话自己的角色传入原生设置，与内核为该角色准备调用时一致；没有原生设置的运行传入的值与以前相同。

## 网络搜索使运行不再是盲测

网络搜索允许打开。打开后，隐藏目标只靠目标文本中的指令（“Do not search for or infer hidden benchmark identity”）保护，而不是靠 Lab 能接触到什么。设置块按内核自己的读法读取（`rsi_lab.native.capabilities`），任何角色（`lab`、`meta`、`literature`）的网络搜索打开时，冻结配置的 `pablo_not_blind` 是下面这句，团队运行写出的每个 `progress.json` 和 `result.json` 都带有同值的 `not_blind`：

> Web search is on for <who>: the hidden objective is protected by instruction only, and this run is not a blind run.

`<who>` 按 lab、meta、literature 的顺序列出打开的角色，写作 `the Lab`、`Meta`、`Literature`，用逗号连接，最后两项之间用 `and`，例如 `the Lab`、`Meta`、`the Lab and Meta`、`the Lab, Meta and Literature`。只有 Lab 打开时，原文是：

> Web search is on for the Lab: the hidden objective is protected by instruction only, and this run is not a blind run.

工具服务器同样使运行不再是盲测，因为服务器能接触到任何东西。任何角色声明了工具服务器（`tool_servers`，本地启动的和通过 URL 访问的都算）时，`pablo_not_blind` 带有下面这句，`<who>` 按同样的顺序和写法列出声明了工具服务器的角色：

> Tool servers are on for <who>: the hidden objective is protected by instruction only, and this run is not a blind run.

两种情况都有时，网络搜索那句在前，工具服务器那句在后，中间隔一个空格，两句各自列出自己的角色。例如 Lab 打开网络搜索并声明了工具服务器时：

> Web search is on for the Lab: the hidden objective is protected by instruction only, and this run is not a blind run. Tool servers are on for the Lab: the hidden objective is protected by instruction only, and this run is not a blind run.

只打开看图（`view_image`）或声明技能（`skills`）不会使运行记为非盲测。既没有任何角色打开网络搜索、也没有任何角色声明工具服务器的团队运行，`pablo_not_blind` 和 `not_blind` 为 null。其他运行的配置和报告没有这两个键。

## 被拒绝的终端可读目录

任何角色的 `terminal.readable_roots` 中，下列目录被拒绝：

- 等于、包含或位于以下目录之内（按解析后的路径比较）：宿主所在的仓库 checkout、父运行的冻结源码副本（`--parent-source-root`）、父运行目录、`--prior-attempt-root`、新运行目录；
- 没有列出的地方按内容识别，在同一次有界的遍历中：其下（包括目录本身的名字）有名字以 `guacamol` 开头的任何条目（文件夹、文件或符号链接，不区分大小写，例如 `guacamol`、`guacamol-0.5.2.dist-info`、`Guacamol.egg-link`、`guacamol.zip`）；其下有 `pablo_task` 文件夹中的 `oracle.py`，即某个 checkout 或冻结源码副本中的评分器源码；其下有运行的存储，即内核 `Store` 在每个运行目录中创建的 `ledger.sqlite3`，这样更早的祖先运行也能被认出。拒绝原因写明找到了什么、在哪里；
- 检查无法在界限内完成：遍历不跟随符号链接，最多向下 6 层、最多访问 20,000 个文件夹，超出任一界限即视为太大无法检查而拒绝；无法读取的目录同样拒绝。

检查在两个时候进行。准备时，在写入任何内容之前检查，并把受保护的路径（解析后的绝对路径）冻结在配置的 `pablo_team_protected_roots` 中。之后宿主每次启动（`make_runtime`，包括 `--resume`）对每个角色的每个可读目录按这些路径重新检查一遍，因为目录中的内容在准备之后可能变化；有一个不通过就拒绝启动并写明原因。不是团队运行的运行不做这项检查。

任何角色的技能文件夹（`skills.root`）同样按上面三条检查，因为技能在每次调用前被写进会话的工作区，终端能读到它们。拒绝原因以 `native_capabilities.<role>.skills.root:` 开头，例如某个技能里有 `pablo_task/oracle.py`，或技能文件夹位于仓库 checkout 之内。技能文件夹只在准备时检查：写入任何内容之前，宿主按内核的方式读取并冻结它（不存储），然后做这项检查；创建运行存储之后、初始化运行之前，宿主再读一遍，把每个文件按它的 SHA-256 存入运行存储，文件夹在两次读取之间有变化就拒绝，此时什么也不存。运行用的是存储里的副本，之后文件夹的变化进不了运行，所以每次启动只重新检查可读目录，不检查技能文件夹。

内核自己的规则仍然适用：可读目录不能是或位于运行目录、Codex home 之内，也不能是共享临时目录。

## 宿主检查

`make_runtime` 拒绝缺少 Lab-led Harness、批次解释屏障（`batch_interpretation_capabilities=["pablo.evaluate"]`）、`pablo_team_protected_roots`，或 Lab 角色中没有 `local` 和 `challenge` 的团队配置，并重新检查可读目录（见上一节）。它也拒绝 Harness 为 Lab-led 版本的 v3 架构配置。Lab-led 运行注册带 `decision` 契约和派出检查的选点工具；其他运行的工具注册不变。

这些检查只在宿主自己的 `make_runtime` 中进行。在它之外构造的 Runtime，例如内核命令行自己的步进命令或 `adapter-run`，既没有派出检查，也没有启动时对可读目录的重新检查。

## 验证

全部是离线测试，使用临时目录和合成数据，没有调用模型、评分器或网络：

- `tests/test_team.py`：角色文字、原生设置块的拒绝条件、可读目录检查的每个拒绝原因、按角色组合的非盲陈述（网络搜索、工具服务器以及两者都有）、三个新键经过内核校验和技能文件夹的冻结、暴露评分器的技能文件夹被拒绝、团队设置，以及一个端到端测试：在真实 Runtime 中注册 Lab-led 工具，用脚本化的 Lab 回答提出声称已派出 Challenge 的选点请求；调用记录中没有 `challenge` 子代理时被拒绝，有时被接收；另一个端到端测试在真实 Runtime 中走完一个 Lab-led 批次：Lab 的选点请求、选点执行、Lab 的评分请求、评分执行（分数是合成值），然后 `harness.context` 重放这个批次的角色、`decision` 和计数。
- `tests/test_host_team_preflight.py`：`--team` 准备冻结的架构、Harness、设置块、上限和 `pablo_not_blind`；各选项的误用都是参数错误；不带 `--team` 的准备不变；父运行是团队运行时，不带 `--team` 的准备被拒绝，`--team` 准备成功；`make_runtime` 拒绝不完整的团队配置和 Harness 为 Lab-led 版本的 v3 配置；`research-report.json` 带有 `not_blind`；`--prior-attempt-root` 受保护；每次启动重新检查可读目录，准备后变得暴露的目录使启动被拒绝；两个记录读取点传入的原生设置；带技能文件夹和工具服务器的 `--team` 准备把两个技能文件存进运行存储、在配置中冻结它们并记下工具服务器那句非盲陈述，Codex home 有自己的技能时启动被拒绝；技能文件夹在检查之后、存储之前变化时，准备在初始化运行之前被拒绝。

这些测试证明准备和检查的接线成立，不证明团队方式能提高分数；团队运行至今没有执行过。
