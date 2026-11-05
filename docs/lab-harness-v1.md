# Lab harness v1

这一版把单次 Lab 实验和最小 Meta 修改流程放进同一个 Python 入口。Lab 选择候选、请求计算、提出科学记忆并提交名单；Controller 校验请求、预留预算、登记结果和冻结终评。Meta 从完成的 development 轨迹提出修改，候选版本经配对验证后才产生采用决定。

当前 pilot 使用独立 AutoDock Vina 1.2.7 在 CPU 上运行 CDK2 对接，每轮从 20 个候选中提交 5 项，固定种子为 17 和 43。离线演示使用合成数据；已完成的真实闭环及其边界见[验证记录](validation-v1.md)。私有代码、数据下载与环境准备见[发布与复现说明](publication-and-reproduction.md)。

## 先运行离线演示

在仓库根目录使用 Python 3.10 或更高版本运行；Controller、Meta 管理和演示只使用标准库：

```sh
python3 -m lab_harness --help
python3 -m lab_harness demo --out runs/lab_demo_v1 --with-meta
python3 -m unittest discover -s tests -v
```

每次使用新的输出目录。演示使用明确标记的合成输入、模型响应和计算结果，不调用 Codex 或 Vina，只验证软件流程。演示记录保留相同的请求、证据、预算和冻结检查。

## 正式 docking 筛选

`select-final` 从一个或多个已完成的 live Lab 归档读取独立 Vina CPU 对接证据。输入为清单文件，运行路径相对于清单所在目录。示例 `manifest.json` 如下。

```json
{
  "schema_version": "lab_docking_selection_input_v1",
  "threshold": -7.0,
  "runs": ["runs/completed_live_lab"]
}
```

```sh
python3 -m lab_harness select-final --input manifest.json --out selection.json
```

`threshold` 是预先冻结的能量上限，单位为 kcal/mol。示例值 −7.0 仅演示输入格式，尚未校准。每个种子（17、43）只取 Vina 输出 PDBQT 中排名第一的姿态，从该姿态的 `REMARK VINA RESULT` 读取 affinity。Meeko 将姿态坐标映射回原配体，供 PoseBusters 和分子身份检查使用。两个第一姿态均有效时才计算 affinity 中位数。能量越低越好，中位数小于或等于阈值即达标。准备失败、种子缺失或姿态检查失败都会使该化合物缺少合格能量。

清单中的运行必须采用兼容的冻结 docking 条件。校验器核对归档、原始 batch、产物哈希、两个种子的所选姿态及其检查结果，再从 docking 配体准备记录取得 `canonical_smiles`。筛选覆盖每次运行中全部已审计的在线和 terminal 作业。失败或姿态无效的作业保留记录，不能入选。池中从未对接的候选单列为未评估。

正式入口只接受独立 Vina 的 live 归档。历史 GNINA GPU 归档和 replay 归档均无法用作正式 Vina 证据。旧输入 `lab_final_selection_input_v1` 允许调用者上传评分表，新 CLI 遇到这种文件会提示改用 live 运行归档清单。

筛选保留所有达标的不同化合物。带化学状态 registry 的输入按冻结的 `canonical_parent_identity` 去重；历史单结构输入回退到准备记录的 `canonical_smiles`。同一 parent 的不同 prepared state 不能重复计数，冲突记录会被拒绝。至少 **100 个**才满足正式名单约束。结果不足时报告缺口，均值不能抵消数量要求。退出码 0 表示达标，2 表示少于 100 项，1 表示输入或归档校验失败。每次使用新的输出文件。

每次 pilot 从 20 个候选中锁定 5 项。正式筛选需要足够多的可比 live 对接结果，并在查看最终结果前冻结阈值。

## Lab：一次真实 campaign

Worker 环境需要独立的 AutoDock Vina 1.2.7 可执行文件，以及可导入 RDKit、Meeko、PoseBusters 的 Python。数据目录包含候选结构、已准备的受体 PDBQT、有效受体 PDB、参考配体和搜索盒参数。Meeko 生成配体 PDBQT，并保存原分子的原子映射。Vina 的受体、配体准备和模型输出格式见[官方基础对接教程](https://autodock-vina.readthedocs.io/en/latest/docking_basic.html)。

先指向实际的数据、仓库提供的 PoseBusters 配置和 Vina 可执行文件，冻结新的 Vina 协议。以下 `data/lab_cdk2_v1` 是目录占位示例，须替换为下载或准备好的 20 项 cohort。

```sh
python3 scripts/run_vina_batch.py freeze-protocol \
  --data data/lab_cdk2_v1 \
  --pose-config scripts/vina_pose_checks.json \
  --vina /path/to/vina \
  --out protocols/lab_vina_v1.json
```

```sh
python3 -m lab_harness init \
  --data data/lab_cdk2_v1 \
  --protocol protocols/lab_vina_v1.json \
  --harness harness/h0 \
  --out runs/lab_dev_v1 \
  --phase development \
  --execution-mode live \
  --model gpt-6-astra \
  --effort medium

python3 -m lab_harness status --run runs/lab_dev_v1
python3 -m lab_harness step --run runs/lab_dev_v1
```

`init` 只冻结输入，不调用模型。`step` 使用已有 Codex CLI 登录发起一次结构化决策；失败和无效响应也计入模型尝试次数。每次调用保存实际输入、提示词、原始事件、响应、错误和观察到的用量。模型配置在初始化时冻结。

Lab 的三个动作是 `tool_batch`、`read_evidence`、`submit`。通过校验的计算请求写入 `pending/<request_id>/request.json`，此时 campaign 等待外部 worker；重复运行 `step` 不会绕过待完成请求继续调用模型。

### 在 CPU worker 上对接并导入结果

`step` 生成请求后，worker 使用同一份冻结数据、协议和 Vina 二进制执行。可以在本机执行，也可以将这些文件与请求送到已配置依赖的 CPU 主机。

```sh
python3 scripts/run_vina_batch.py run \
  --data /worker/data \
  --protocol /worker/lab_vina_v1.json \
  --vina /worker/bin/vina \
  --campaign /worker/online \
  --request /worker/request.json
```

各 campaign 使用独立 worker 目录，online 与 terminal 分开。worker 只执行 Controller 已预留的请求，保存每个种子的原始 PDBQT、命令、日志和姿态检查结果。

取回完整 batch 目录后导入：

```sh
python3 -m lab_harness ingest \
  --run runs/lab_dev_v1 \
  --batch /path/to/downloaded/batch
python3 -m lab_harness step --run runs/lab_dev_v1
```

导入会核对请求身份、冻结协议、逐文件 SHA-256、逐种子结果及汇总。重复导入相同结果幂等；相同请求对应不同内容会被拒绝。未完成或中断的作业不能自动重跑，需先审计其真实执行状态。

### 锁定名单与终评

```sh
python3 -m lab_harness finalize --run runs/lab_dev_v1
```

`submit` 后名单锁定。`finalize` 为尚无结果的名单成员生成 terminal 请求；执行该 Vina 请求时额外传入 `--locked-shortlist /worker/locked_shortlist.json`。导入结果后再次 `finalize`，直到产生 `evaluation.json`。同一 campaign 内可复用已观察的在线结果；终评不再反馈给 Lab，也不能用于重新挑选名单。

### 预算口径

| 资源 | v1 规则 |
| --- | --- |
| 在线候选 | 最多 8 个不同候选，每批 1–4 个；每个候选预留两个种子任务 |
| 失败候选 | 同样占用名额，不自动重试 |
| 模型调用 | 最多 6 次尝试，每次超时 300 秒；失败与修复调用均计数 |
| 模型 tokens | 150,000 为软上限；保留实际 input/output 用量，未知用量不填 0 |
| 终评补算 | 单独记录，只覆盖已锁定名单中尚无结果的候选，最多 5 项 |
| CPU/费用 | 记录实际 CPU、墙钟时长和作业状态。美元费用单独计量 |

模型提交计算请求，由 Controller 校验并记录实际对接分数。科学记忆中的引用、scope、supersedes 和依赖会被校验，但有效引用不等于解释已经获得语义支持。

## Meta：修改、冻结、登记验证计划

Meta 读取已完成的 `development` 运行。`validation` 和 `final` 不可作为本轮 Meta 开发输入。先准备工作区，在试验前冻结父版本、开发证据与 gate 条件：

```sh
python3 -m lab_harness meta prepare \
  --development-run runs/lab_dev_v1 \
  --out runs/meta_v1 \
  --min-pairs 2

python3 -m lab_harness meta propose --workspace runs/meta_v1
```

`prepare` 不调用模型。`propose` 进行一次有记录的 Meta 调用；Meta 可返回 `no_change`，也可提出一个候选修改。候选工作区只允许修改 Lab 提示词和输入视图配置，父版本保持原样。父版本快照位于 `runs/meta_v1/parent`，冻结候选位于 `runs/meta_v1/candidate`。修改清单与内容哈希用于后续验证匹配。

v1 的可改范围是：

| 文件 | 可改行为 |
| --- | --- |
| `lab_planner.md` | Lab 的决策指引、证据解释要求和策略提示 |
| `input_view.json` | 在完整展开、紧凑 JSON、无损去重 JSON 之间选择 |
| `manifest.json` | 候选版本身份；由版本管理逻辑写入 |

输入视图必须可无损还原。Meta 不能删除证据、改变候选池、评分目标、工具、预算、固定约束、原始数据或 gate。v1 不执行 Meta 任意生成的 Python 代码，也不自动改变模型权重、角色数量或运行拓扑。

提出候选后，先登记全部验证配对。此时四个运行目录必须尚不存在；每项依次指定数据目录、协议文件、父版本运行目录和候选运行目录：

```sh
python3 -m lab_harness meta plan \
  --workspace runs/meta_v1 \
  --pair data/validation_01 protocols/validation_01.json \
    runs/validation_01_parent runs/validation_01_candidate \
  --pair data/validation_02 protocols/validation_02.json \
    runs/validation_02_parent runs/validation_02_candidate
```

计划只登记一次，并冻结数据、协议的精确哈希和运行路径。Gate 必须收到计划中的全部配对，不允许事后仅挑选表现好的子集。计划登记本身不调用模型或 Vina。

随后将相同的新验证数据与协议分别交给父版本和冻结候选，以 `--phase validation` 初始化已登记的独立 campaign：父版本使用 `--harness runs/meta_v1/parent`，候选使用 `--harness runs/meta_v1/candidate`。两臂使用相同模型和预算，各自完成真实 Lab 决策与 Vina 对接。每一对使用相同候选池，不同配对的候选池必须互不重叠，并且都与本轮 development 池隔离。不要用同一结果目录或跨臂缓存替代独立执行。live 检查候选 ID，以及 prepared SMILES 和已登记 parent identity 的跨池重复；历史输入使用精确 SMILES。化学等价性仍依赖冻结数据准备，不能仅靠字符串检查推断。

完成预登记的配对运行后：

```sh
python3 -m lab_harness meta gate \
  --workspace runs/meta_v1 \
  --pair runs/validation_01_parent runs/validation_01_candidate \
  --pair runs/validation_02_parent runs/validation_02_candidate \
  --out runs/meta_v1_gate.json
```

Gate 检查全部预登记配对，默认至少 2 对，live 运行的用量须完整。每个实验臂先统计双种子姿态均有效的名单项数，再计算这些项的合格 docking 能量均值。缺失能量不填 0。若任一臂没有有效姿态而无法计算均值，或任何一对的有效姿态候选数下降，则候选版本不具备采用资格。

能量改善量取父版本均值减候选版均值，再对配对取平均。正值表示候选版能量更低。v1 使用以下开发阶段工程门槛。

| 路径 | 默认条件 |
| --- | --- |
| 质量改善 | 有效候选数持平时，平均 docking 能量改善至少 0.1 kcal/mol。另一条路径是平均有效候选数增加至少 0.2 项，且平均能量损失不超过 0.1 kcal/mol |
| 节省 tokens | 平均能量损失不超过 0.1 kcal/mol，且验证运行的 tokens 至少减少 10% |

通过只表示 `eligible_for_next_development_run`，不能据此宣称普遍性能提升。采用仅影响后续新建的 campaign；已经开始的运行始终绑定其原 Harness hash。合成 replay 得到 `demo_only`，不能形成真实版本接受。

## 文件与职责

```text
lab_harness/
  controller.py       固定状态机、预算、工具结果与终评
  transport.py        Codex CLI 调用和原始事件审计
  input_view.py       可逆工作视图
  versions.py         Harness 文件白名单和内容哈希
  meta.py             开发证据、候选冻结和配对 gate
  common_contract.md  不可由 Meta 修改的实验约束
harness/h0/
  manifest.json
  lab_planner.md
  input_view.json
scripts/
  run_vina_batch.py   冻结独立 Vina 协议并执行 CPU batch
runs/<campaign>/
  frozen/             固定数据、协议、提示词和实现哈希
  turns/              逐次模型输入、响应、事件与用量
  pending/            Controller 已预留的外部请求
  batches/            原始数值工具结果及其清单
  state.json
  events.jsonl
  locked_shortlist.json
  evaluation.json
```

Harness 身份与数据、协议和运行身份分别记录。恢复从已保存的 Controller 状态继续；如果模型调用或外部作业在未知结果状态中断，系统要求审计，不承诺外部 exactly-once 执行。

## 与 PABLO 和既有 Lab 的关系

架构参考本地 RSI-PABLO 项目的 `pablo-meta-experiment/experiment/round2_runtime.py`、`codex_model.py` 与 `persistent_research_runtime.py`：策略提出动作，受保护 runtime 负责执行、预算、证据与归因。这里没有导入 PABLO 的任务数据、模型权重或历史成绩，也没有照搬其 Continuity Meta 多轮调查机制。

Controller 和工作视图源自既有 Lab 实现。独立 Vina worker 使用新的协议与原始 PDBQT 结果，历史 GNINA 报告仍对应原来的实验。旧实现的来源哈希见[迁移记录](migration-provenance.json)。

当前前端仍是独立工作台原型，未自动接入这些运行。CLI 禁用工具和审计事件也不是操作系统级文件读取隔离。v1 的姿态检查和连续分数不构成真实生物活性或药效结论；端到端软件测试通过不能代替真实配对实验。
