# RSI-Lab：通用内核、领域能力包与任务计划

RSI-Lab 的内核负责持续的问题处理和可核验执行。Docking 是第一个领域包，不再决定内核的任务类型。当前代码提供通用存储、Meta 调度与注册能力接口。完整 Lab 执行循环、Scientific Memory 六类科研对象和 Codex harness 自动修改仍需按[系统目标设计](system-design.html)接入与验收；设计文档不表示这些能力已经全部实现。

| 层次 | 代码位置 | 负责什么 | 不在这一层固定什么 |
| --- | --- | --- | --- |
| 通用 RSI 核心 | `rsi_lab/` | 问题与工作账本、调度、等待、预算、外部调用、能力验收、Trial、发布和恢复 | pH、compound、Vina、特定筛选数量或种子 |
| 可替换领域包 | `rsi_domains/docking/`、`rsi_domains/tabular/` | 输入契约、领域计算、原始结果核验、证据解释和工具适用范围 | 所有任务必须采用同一个成功条件 |
| 本次任务的冻结计划 | `rsi_lab/plans.py` 的 `TaskPlan` | 用户目标、输入身份、指标、方向、阈值、重复评价参数、成功条件、实验臂、全部分片与预算 | 可在看到结果后替换的阈值或结果集合 |

`docking_rsi/` 保留旧 API 的兼容入口。通用实现已经迁出；旧入口通过明确的格式适配和领域包调用复用内核，不维护第二套调度实现。代码发布不附带运行归档；使用者在自己的运行目录保存输入、事件和结果。

## 1. 内核如何接入领域能力

`rsi_lab.adapters.Adapter` 要求宿主明确注册以下内容：

- `adapter_id`、版本、Effect 类型以及输入、输出和适用范围说明。
- `prepare`：验证请求并生成具体执行输入。
- `reservations`：为具体输入计算资源预约。
- `execute`：执行已经持久保存且处于派发状态的调用，返回实际结果和成本。
- `publication`：把经过核验的结果映射为原始事实、来源陈述、验证结论和应用结果。

内核不根据任务名称猜测应该导入哪个模块。`Runtime(..., adapters=[...])` 接收宿主选择的能力包；`execute_adapter(adapter_id, inputs, trigger_id=...)` 负责调用前预约、保存回执和发布。恢复时根据已记录的适配器身份、版本、实现 hash 和回执找到同一适配器，不在调度代码中写 `if docking ...`。

适配器描述会绑定源文件、回调代码与默认参数，实际源文件也进入不可变归档。未加载、版本不同或实现不匹配的适配器不能冒充原执行者进行恢复。尚未生成发布材料时，需要相匹配的实现核验原始回执；发布材料已保存后，后续通知和唤醒可以直接恢复，不再依赖旧适配器仍被安装。未知外部调用仍保留对账责任，不能因为换包而重发。

加载宿主工具与动态建设新能力是不同的授权路径。需要验收的适配器不能仅靠导入模块进入可调用目录。Meta 可以请求能力建设，工程执行器需要提交真实产物和验收材料；代码控制器核验后才允许后续调用。

这仍是**宿主注册代码**的接口。模型的工具提案不能导入任意模块、替换回调或给自己授予执行权限。审核后的本地适配器验收与分支 runner 使用独立进程和临时目录，并施加相应资源约束；这里不声称提供 OS 或网络安全沙箱。

## 2. TaskPlan 冻结任务条件

通用计划 schema 为 `rsi_task_plan_v1`。主要字段如下：

| 字段 | 含义 |
| --- | --- |
| `task_id`、`goal` | 本次任务的身份与目标 |
| `inputs`、`items` | 原始输入 hash，以及全部受评价对象的身份 |
| `protocol` | 任务方法版本和明确参数；参数的领域含义由能力包验证 |
| `capability_versions` | 本次使用的能力版本 |
| `metrics` | 指标名、单位、阈值、方向、聚合方法和全部重复评价身份 |
| `success` | 采用哪个指标、最少多少个达标对象 |
| `arms`、`shards` | 各实验臂、策略版本、完整对象分片与未来输出位置 |
| `budget`、`stop_rule`、`missing_rule` | 资源、停止规则和失败／缺失／未开始工作的处理 |

`register_task_plan` 在相关执行和输出产生前登记，`load_task_plan` 以原始不可变注册事件核对内容。结果产生后不能更换最少数量、阈值、重复评价集合或输入身份。所有实验臂必须覆盖全部计划对象，失败和未开始的工作保留在结果中。

`select_items` 只按所选实验臂和冻结指标评价全部对象。即使已经达到最少数量，未完成的计划对象仍使结果保持 `incomplete`。它是通用契约选择器：**原始输出真实性必须先由领域适配器核验**，函数不会把任意传入的分数表自动认证为真实执行结果。

Docking 的 `register_study` 已在同一事务中冻结共同 TaskPlan，正式 selector 先保留原有 Effect、分片与原始归档核验，再实际调用共同的选择器。因此这不是只有非 docking 演示才使用的另一套计划机制。

每次任务在计划中冻结自己的最低数量。Docking 任务可以使用 `min_compounds=3` 等要求，通用 CSV 任务可以要求两个站点全部达标；数量不会因领域名称自动变成 100。v1 兼容 selector 的固定 100 项门槛是该入口的限制，不能代表通用 TaskPlan。

种子也不是内核规则。当前 Vina 原始归档 reader 只能验证 17/43 种子与 MODEL 1 契约；这是该能力版本的适用范围。要求其他种子的任务需要相应适配器支持，旧 reader 会明确拒绝，不会偷偷套用旧结果。通用 TaskPlan 把重复评价身份作为显式参数，既可以是任意种子标签，也可以是 `aggregation` 这样的计算重复身份。

## 3. Meta 的开放发现范围

一个 Meta-agent 使用 discovery 和 disposition 两种模式，共享问题账本。发现模式从任务目标、当时可见事实、来源与能力说明提出前提、竞争性解释及能区分解释的检查；处置模式推进具体问题，选择查询、收窄、重规划、提问、能力建设、试验或明确等待。

领域检查表是可提供的工具能力，不是发现模式的完整问题空间。内核仍保留代码异常和有预算的低频开放审查两条入口。只向首次审查提供原始材料，与随后提供已有解释的核对调用分开；不能因为角色名称不同就宣称判断独立。

不能把“领域包提前列出 pH 缺口，模型复述告警”算作自主发现验收。新的领域任务应检验模型是否能从目标、材料和真实能力边界发现未预告问题，以及调查是否实际解除阻塞；当前非 docking 演示的角色响应是固定 fixture，并未进行这个模型质量评估。

## 4. 合成 CSV 演示与验收

`rsi_domains.tabular.demo.run_demo` 使用含有 Wh/kWh 混合读数的 CSV。初始数据没有单位映射，演示会执行以下通用流程：

1. 登记单位证据缺口，请求记录身份对应的单位映射，持久等待。
2. 关闭并重新打开账本，交付单位映射，唤醒同一问题。
3. 请求聚合能力；实际执行四个验收案例：混合单位正常输入、缺少单位、不支持的单位、重复记录身份。
4. 使用相同 TrialEngine 和隔离 runner，在一个冻结的验证 CSV 上比较直接求和与单位换算，检查误差和记录丢失。
5. 冻结任务计划并实际聚合原 CSV。模拟成功回执后、发布前中断；恢复只完成发布，不再次计算。

测试 fixture 的预期结果是 North = 3.5 kWh、South = 2.0 kWh，并保留四条输入记录身份。演示使用合成角色决策，不调用模型；这些数值是软件测试期望值，不是研究结果。

可以同时加载 docking 包来验证它不会污染另一个任务的能力或调度路径：

```python
from rsi_domains.tabular.demo import run_demo
from rsi_domains.docking.plugin import make_adapter

report = run_demo(
    "runs/rsi_tabular_portability",
    additional_adapters=[make_adapter()],
)
print(report["aggregation"])
print(report["trial_outcome"])
```

此演示的实际成功条件为两个站点的绝对误差都为零。它没有 compound、Vina 分数或 docking 调用；测试同时检查执行前后的内核文件 hash 未改变、通用 Runtime 没有 `inspect_docking` 方法，以及恢复后只有一次最终聚合 Effect。

这建立了一个具体的可替换领域验收：新增工具与任务契约即可复用问题、等待、能力验收、Trial 和恢复。单个验证 CSV 的结果仅支持已声明输入范围的换算正确性；它不是通用智能、任意领域自动发现能力或普遍性能收益的证明。

运行 `python3 -m unittest discover -s tests/generic -v` 检查当前 checkout 的机制测试。

## 5. 通用入口与兼容边界

通用命令入口为 `python -m rsi_lab`。宿主可以通过 `--adapter-factory pkg.module:make_adapter` 显式提供已审核的能力工厂；这个参数是宿主代码接入，不接受 Meta 从任务材料中擅自创建。

```sh
python3 -m rsi_lab --help
python3 -m rsi_lab --root runs/rsi_task --run task1 status
python3 -m rsi_lab --root runs/rsi_task --run task1 \
  --adapter-factory rsi_domains.docking.plugin:make_adapter status
```

`init` 登记运行配置，`register-plan` 冻结任务计划，`adapter-run` 执行显式注册的能力，`run-until-wait` 持续推进可执行工作直到等待、预算或步数边界。具体参数以该命令的 `--help` 为准。只有合法准备态调用能继续派发；未知调用不能因重启或重新加载能力包而重发。

新的中性 wire schema 和旧 `docking_*` wire schema 通过兼容层区分。SQLite 投影格式与 schema 名称是两件事：已有事件、blob 和旧任务计划不会原地迁移或重写。新运行应使用独立目录。旧 `docking_rsi` 调用可继续通过其兼容入口读取历史格式，而新的应用应直接使用 `rsi_lab` 和显式领域包。

旧 Trial／能力合同中 `scientific_protocol` 等历史字段名仍保留兼容含义：它标识会改变任务评价结果的方法协议版本，并不要求任务一定是分子科学。领域能力一旦改变结果语义，仍须创建新的协议和研究身份，不能把不同方法的结果混入原冻结计划。

相关实现和验收：

- [通用 Runtime](../rsi_lab/runtime.py)、[Adapter 接口](../rsi_lab/adapters.py)、[TaskPlan](../rsi_lab/plans.py)
- [Docking 能力包](../rsi_domains/docking/plugin.py)、[Docking StudyPlan 适配](../rsi_domains/docking/study.py)
- [CSV 能力包](../rsi_domains/tabular/plugin.py)、[非 docking 闭环演示](../rsi_domains/tabular/demo.py)
- [领域替换验收](../tests/generic/test_portability.py)、[任务契约验收](../tests/generic/test_plans.py)
- [Docking 控制契约与实现边界](docking-rsi-v2.md)
