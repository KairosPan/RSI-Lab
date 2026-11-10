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

- `prepare`：验证请求并生成具体执行输入。
- `reservations`：为具体输入计算资源预约。
- `execute`：执行已持久保存且处于派发状态的调用，返回实际结果和成本。
- `publication`：把核验结果映射为原始事实、来源陈述、验证结论与应用结果。
- 可选 `measure`：产生有界任务指标，内核保留原始执行凭据，不解释分数方向。

`Runtime(..., adapters=[...])` 接收宿主选择的能力；`execute_adapter(adapter_id, inputs, trigger_id=...)` 负责调用前预约、保存回执与发布。适配器身份、版本、回调与实现 hash 绑定到执行，恢复时不会按任务名称猜测实现。

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

入口为 `python3 -m rsi_lab`。`init` 登记运行配置，`register-plan` 冻结任务计划，`adapter-run` 执行已注册能力，`run-until-wait` 推进就绪工作直到等待、预算或步数边界。只有合法准备态调用能继续派发，未知调用不能因重启而重发。

```sh
python3 -m rsi_lab --help
python3 -m rsi_lab --root runs/tabular-demo --run tabular-portability status
python3 -m rsi_lab --root runs/tabular-demo --run tabular-portability \
  --adapter-factory rsi_domains.tabular.plugin:make_adapter status
```

后两条命令读取前述示例已创建的运行。`--adapter-factory` 是受信宿主参数，不是模型命令。真实模型配置与自定义运行步骤见[持久 Agent 说明](persistent-agents.md)。新任务使用新目录；不要把旧格式或其他任务的账本直接当作新任务输入。

相关实现与验收：

- [Runtime](../rsi_lab/runtime.py)、[Adapter](../rsi_lab/adapters.py)、[TaskPlan](../rsi_lab/plans.py)
- [CSV 能力](../rsi_domains/tabular/plugin.py)、[离线闭环](../rsi_domains/tabular/demo.py)
- [能力替换验收](../tests/generic/test_portability.py)、[任务契约验收](../tests/generic/test_plans.py)
