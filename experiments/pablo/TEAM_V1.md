# PABLO 团队运行：team v1

本次只修改代码、文档和离线测试。没有准备、启动或执行过任何团队运行，没有调用模型，也没有使用网络；下面描述的是准备好的配置和宿主检查，不是实验结果。

## 什么是团队运行

`run.py --team` 准备一个新运行，架构为 `pablo_research_pr4_team_v1`，Harness 版本为 `pablo_lab_led_v1`。Lab 领导一个小组：它可以用 `agent_type` 启动 `local`、`challenge` 两个具名子代理，以及 Codex 的 `default` 帮手。子代理只提议，不能评分也不能选点；子代理返回的内容是线索，不是证据。只有 Lab 请求 `pablo.select_candidates` 和 `pablo.evaluate`。

不带 `--team` 的准备与之前完全相同：同样的配置、目标文本、工具和进度报告。旧架构的运行也不变。从团队运行的父运行准备普通运行时，复制来的配置会去掉 `native_capabilities`、`pablo_not_blind`、`max_effective_context_bytes` 和 `prospective_rebuild_history_bytes`，团队设置不会带进普通运行。

## 选项

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `--team` | 关 | 准备团队运行 |
| `--native-capabilities FILE` | 无 | JSON 文件，内容是内核读取的 `native_capabilities` 块；`--team` 必须提供，没有 `--team` 时拒绝 |
| `--challenge-backstop-batches N` | 4 | 后备规则的批次数，1 到 100，或 `off` 关闭；没有 `--team` 时拒绝 |
| `--rebuild-history-bytes N` | 4,194,304 | 空闲会话的原生历史达到这个大小时重建，至少 262,144；没有 `--team` 时拒绝 |

这四个选项只用于新的准备，`--resume` 时一律拒绝。团队运行的每次请求上下文上限 `max_effective_context_bytes` 固定为 4,194,304 字节。

文件中 `lab.subagents` 必须打开（`true` 或对象），不能自己声明 `local` 或 `challenge`：这两个角色由宿主用自己的文字加入。文件里的其他设置和其他角色保留；`subagents` 写成 `true` 时，冻结配置写明内核当前的默认并发数 3。

子代理的 token 记在启动它的 Lab 调用上，所以团队运行需要更大的 token 预算。这个预算由操作者在准备时用 `--additional-tokens` 提高，宿主不会自动调整。

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

## 每次调用记录的子代理信息

团队运行的每次 Lab 调用记录中，`child_threads` 列出能读到的每个子代理：`thread_id`、`agent_path`、`role`、`started_in_call`（本次调用启动，还是联系了更早启动的子代理）、`forked_history`（是否带着 Lab 的历史启动）、`model`、`effort`、请求数和 token 计数，以及它在这次调用中的网络搜索查询（最多 20 条，每条截到 200 个字符，其余计入 `searches_omitted`）。读不到的子代理使这次调用的用量成为未知，并写明原因。

没有用声明角色启动的帮手，记录为 Codex 给它的角色；Codex 没有给出时为 null。

Lab 可以再次联系更早启动的子代理，而不是新开一个；那个子代理记得它之前的工作。这是允许并记录的，不阻止。会话重建会结束它的全部子代理：重建在原生历史达到 `prospective_rebuild_history_bytes` 时发生，所以这个值也是一个实验臂的组成部分。

目标文本请 Lab 用 `fork_turns` `none` 启动 Challenge，并把它需要的 anchor、约束和结果交给它；是否这样做由 `forked_history` 记录，不强制。

宿主的两个原生记录读取点（`run.py` 的 `startup_preflight` 和 `host_support.py` 的 `rebuild_at_boundary`）按会话自己的角色传入原生设置，与内核为该角色准备调用时一致；没有原生设置的运行传入的值与以前相同。

## 网络搜索使运行不再是盲测

网络搜索允许打开。打开后，隐藏目标只靠目标文本中的指令（“Do not search for or infer hidden benchmark identity”）保护，而不是靠 Lab 能接触到什么。`lab.web_search` 为 true 时，冻结配置的 `pablo_not_blind` 是下面这句原文，团队运行写出的每个 `progress.json` 和 `result.json` 都带有同值的 `not_blind`：

> Web search is on for the Lab: the hidden objective is protected by instruction only, and this run is not a blind run.

没有打开网络搜索的团队运行，`pablo_not_blind` 和 `not_blind` 为 null。其他运行的配置和报告没有这两个键。

## 被拒绝的终端可读目录

任何角色的 `terminal.readable_roots` 中，下列目录在准备时被拒绝，运行目录尚未创建：

- 等于、包含或位于以下目录之内（按解析后的路径比较）：宿主所在的仓库 checkout、父运行的冻结源码副本（`--parent-source-root`）、父运行目录、新运行目录；
- 其下能找到名为 `guacamol` 且含 `__init__.py` 的文件夹；
- 检查无法在界限内完成：搜索不跟随符号链接，最多向下 6 层、最多访问 20,000 个文件夹，超出任一界限即视为太大无法检查而拒绝；无法读取的目录同样拒绝。

内核自己的规则仍然适用：可读目录不能是或位于运行目录、Codex home 之内，也不能是共享临时目录。

## 宿主检查

`make_runtime` 拒绝缺少 Lab-led Harness、批次解释屏障（`batch_interpretation_capabilities=["pablo.evaluate"]`）或 Lab 角色中没有 `local` 和 `challenge` 的团队配置。Lab-led 运行注册带 `decision` 契约和派出检查的选点工具；其他运行的工具注册不变。

## 验证

全部是离线测试，使用临时目录和合成数据，没有调用模型、评分器或网络：

- `tests/test_team.py`：角色文字、原生设置块的拒绝条件、可读目录检查、非盲陈述、团队设置，以及一个端到端测试：在真实 Runtime 中注册 Lab-led 工具，用脚本化的 Lab 回答提出声称已派出 Challenge 的选点请求；调用记录中没有 `challenge` 子代理时被拒绝，有时被接收。
- `tests/test_host_team_preflight.py`：`--team` 准备冻结的架构、Harness、设置块、上限和 `pablo_not_blind`；各选项的误用都是参数错误；不带 `--team` 的准备不变；`make_runtime` 拒绝不完整的团队配置；两个记录读取点传入的原生设置。

这些测试证明准备和检查的接线成立，不证明团队方式能提高分数；团队运行至今没有执行过。
