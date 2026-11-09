# Docking RSI-Lab v2：控制契约与交付边界

> 架构更新：本文说明 docking 应用与 v2 兼容契约。通用运行机制现位于 `rsi_lab/`，领域实现位于 `rsi_domains/docking/`，旧 `docking_rsi/` 是兼容入口。示例中的最低 100 项由计划显式冻结，不是所有任务的规则；17/43 是当前 Vina reader 的支持范围。最新边界和非 docking 验收见 [通用核心说明](rsi-lab-core.md)。

这一版把运行中的问题处理做成持久控制状态：Lab 执行任务，一个 Meta-agent 按需切换发现和处置模式，代码控制器负责执行许可、预算、证据交付、版本结算和恢复。目标是让缺口能够被登记、调查、补齐或明确挂起，并在依赖满足后恢复工作。

**当前实现不能据此宣称筛选质量提高、完整成本下降，或已经完成通用 docking 科学验证。** P0/P1 核心和 P2 的持久运行、动作路由、能力验收、分支试验流程已接线并有集成测试。包含只读领域检查与可选的角色探针入口；领域适用性、模型发现质量和大候选库效果须由使用者另行验证。完整目标架构见[系统设计](system-design.html)，其中尚未实现的要求不视为本版交付。

## 1. 兼容边界和阶段状态

控制实现位于 `rsi_lab/`，领域实现位于 `rsi_domains/docking/`；`docking_rsi/` 提供兼容入口，测试位于 `tests/v2/`。新运行使用自己的 `Store(root)`、SQLite schema 2、`v2_*` 投影表和不可变对象目录。`lab_harness/` 和 Vina 执行器提供独立的 v1 接口与合成演示。仓库不分发运行归档，已有运行应按自己的格式和版本读取。

| 阶段 | 本版代码提供什么 | 仍不能宣称什么 |
| --- | --- | --- |
| P0：契约 | 不可变 StudyPlan、完整分片账本、按计划选择、历史边界、故障 fixture | 旧筛选结果可以事后登记成正式研究；正式科学准入已经完成 |
| P1：控制面 | SQLite 事务、Issue/Work/Wait/Effect、预约和结算、依赖唤醒、用户问题、未知调用恢复约束 | 任意外部服务都能自动恢复；重新启动可以直接重发未知调用 |
| P2：角色和验证机制 | 单 Meta-agent 双模式、共享输入构建器、分页与回执、Runtime 封闭动作路由、TrialEngine、实际进程分支、可执行适配器验收；完整离线闭环和恢复测试 | 模型能稳定发现未预告问题；所有领域工具和工程器已接入；一般工程代码受到 OS 沙箱保护 |
| P3：docking 能力 | 保留已有 Vina/原始姿态/身份核验；增加读取真实准备与结构材料的前提检查，并通过 Runtime 登记事实和触发审查 | 新受体准备或修复能力、多状态处理研究、氢键几何分析、靶点校准或独立盲测已经完成 |
| P4：持续研究 | 正式大库的计划/分片/原始结果读取契约 | 生产大库 runner、真实同预算基线比较、多轮改进收益已经交付 |

`native_vina_jobs_v2` 读取路径不使用“20 选 5”上限，但这与已经提供生产大库执行器不同。本版没有 `formal_runner.py`。接入执行器还必须验证分片预约、科学准入、真实资源消耗、重启对账和完整研究成本。

## 2. 控制对象和责任

| 对象/模块 | 责任 |
| --- | --- |
| `store.py` | 原子提交事件与投影，按提交序号排序，内容寻址 blob，重复操作键幂等，历史与投影核验 |
| `runtime.py` | 把真实模型提案路由为请求、交付、问题、能力建设、试验等持久动作，处理继续运行与恢复；按显式命令推进 |
| `branches.py` | 执行事前绑定代码 hash 的 baseline/candidate 分支，独立进程/工作目录，保留测量与实际成本回执 |
| `domain.py` | 只读审查协议、准备材料与原始结构，区分事实、来源陈述和未验证缺口，向 Runtime 提供真实证据 |
| `Issue` | 记录问题、范围、证据、竞争性解释与可推翻假设的检查；证据版本与调度版本分开 |
| `Work` | 具体动作、输入版本、执行资格、尝试和结果；旧版本的工作不能直接派发 |
| `Wait` | 依赖、生产者、已见版本、唤醒条件、截止期与到期动作；普通等待不占外部执行名额 |
| `Request / Delivery` | 请求范围、实际交付页、来源版本、内容 hash、模型收到的页面回执 |
| `Effect` | 调用前保存输入和资源预约；执行后保存完整回执；未知执行继续占用未决责任 |
| `Trial` | 冻结干预版本、配对计划、测量、发布标准、成本与恢复责任；代码结算 |
| `fact` | `raw_fact`、`source_statement`、`hypothesis`、`validation` 分开存储 |

同一个等待周期最多尝试一次到期处置。相关依赖或容量版本变化可以创建新的调度机会；重复告警或时间流逝不能无限续期。科学证据没有变化，也不妨碍容量恢复后的重新调度。

外部调用按 `prepared → dispatching → succeeded / failed_definite / unknown` 管理。数据库预约先于调用；持久回执先于最终结算。只有已证明未执行或具备有效查询/幂等契约的恢复路径才能解除不确定性；缺少这些能力时保留 `blocked_external`，不得把未知结果伪装成失败后自动重试。已知执行结果但缺少成本回执，也仍有成本对账责任。

`Controller.initialize` 的 hard 上限只接受当前接口可以执行的 `model_calls`、`tool_calls` 调用次数。tokens、CPU、墙钟等需要真实回执和适配器约束；仅登记一个数字不会使它成为硬上限。StudyPlan 预算目前负责冻结研究预期、检查 seed-job 额度覆盖全部计划；生产 runner 接入后还需把分片额度绑定到 Effect 预约并验证完整成本。

## 3. 一个 Meta-agent，两种模式

`roles.MetaAgent` 使用同一套模型接口和问题账本：

| 模式 | 输入和触发 | 允许的输出 |
| --- | --- | --- |
| `discovery` | 代码检测异常，或有预算的低频开放审查 | 提出问题、请求现有诊断、说明当前无需动作 |
| `disposition` | 具体问题的证据、依赖或调度变化 | 请求/收窄/重规划证据、询问用户、请求能力、提出试验、挂起或关闭问题 |

模型只提交封闭 schema 的提案，不能自行发布策略、修改分数、结算成本或宣告能力已验收。重要审查先记录只看原始证据的初步判断，再加入 Owner 解释进行核对；这不意味着两次模型判断统计独立。

`PacketBuilder` 对完整应用请求检查字节容量，规划与派发使用同一个构建器。依次尝试完整输入、分页、无损去重、收窄；必要身份和证据依赖仍放不下时返回 `packet_unrepresentable`。此处测量的是实际应用请求字节，不推断服务端 framing 或 token 数。摘要保留摘要身份，不能凭摘要回执宣称已读取原文。

用户回复必须绑定问题身份和版本。系统不能用超时、模型猜测或同名新问题替代尚未得到的实验条件。

## 4. 正式 StudyPlan 的精确输入

`study.register_study(store, run_id, plan, key=...)` 在一个事务中保存不可变计划以及所有臂的全部分片。注册时输出路径必须尚不存在，绑定的 Effect 必须在注册之后创建；前瞻性来自持久提交顺序，不接受调用者自报的时间戳。

`select_study(store, run_id, study_id, arm_id)` 只接受研究和单个实验臂身份，不接受新的 threshold、run 路径列表、top-k 或跨臂拼接参数。导出前后核验历史一致性，重新读取原始归档，不信任调用者提供的成绩表。运行配置的 `execution_mode` 必须是 `live`。

以下是完整字段示例的构造方式。`prepared_data`、`frozen_protocol`、`approved_policy` 是**事先准备并审定的真实输入**，不是本代码生成的科学材料；输出路径 `future_archive` 必须尚不存在。这段示例只构造计划，不执行 docking，不构成科学准入批准。

```python
import json
from pathlib import Path
from docking_rsi.study import digest, file_hash, protocol_contract, validate_plan

prepared_data = Path('/absolute/path/to/prepared-data')
frozen_protocol = Path('/absolute/path/to/formal-protocol.json')
approved_policy = Path('/absolute/path/to/approved-policy.json')
future_archive = Path('/absolute/path/to/new-study/arm-a/shard-000').resolve()

candidates = json.loads((prepared_data / 'candidates.json').read_text())['candidates']
manifest = json.loads((prepared_data / 'manifest.json').read_text())
protocol = json.loads(frozen_protocol.read_text())
assert protocol['phase'] == 'formal'
registry = [
    {'compound_id': row['candidate_id'],
     'parent_identity': row['canonical_parent_identity'],
     'prepared_smiles': row['prepared_smiles'],
     'prepared_state_id': row['prepared_state_id']}
    for row in candidates
]
plan = {
    'schema_version': 'docking_study_plan_v2',
    'study_id': 'screen-001',
    'phase': 'formal',
    'compound_registry': registry,
    'identity_rule': {
        'name': 'registered_parent_identity',
        'version': 'prepared-parent-registry-v1',
        'registry_sha256': digest(registry),
    },
    'scientific_protocol': {
        'version': 'science-v1',
        **protocol_contract(protocol, manifest),
    },
    'tool_versions': {
        'vina': '1.2.7',
        'native_archive_adapter': 'native_vina_jobs_v2',
        'chemical_preparation': 'molscrub_single_state_preparation_v1',
    },
    'arms': [{
        'arm_id': 'arm-a',
        'policy_version': 'policy-v1',
        'policy_sha256': file_hash(approved_policy),
        'shards': [{
            'shard_id': 'shard-000',
            'compound_ids': [row['compound_id'] for row in registry],
            'archive_path': str(future_archive),
            'archive_adapter': 'native_vina_jobs_v2',
            'protocol_sha256': file_hash(frozen_protocol),
        }],
    }],
    'threshold': -8.0,
    'direction': 'lower_or_equal',
    'min_compounds': 100,
    'score': {
        'name': 'qualified_median_vina_affinity',
        'units': 'kcal/mol',
        'seeds': [17, 43],
        'pose_rank': 1,
        'requires_both_valid': True,
    },
    'budget': {'seed_jobs': 2 * len(registry), 'model_calls': 0},
    'stop_rule': 'all_planned_shards_terminal_or_explicit_stop',
    'missing_rule': 'retain_all_missing_no_imputation_no_replacement',
}
validate_plan(plan, check_future_paths=True)
```

示例中的 `-8.0` 只是说明固定阈值的字段位置，不能作为跨靶点推荐值。阈值、候选库、准备和评价协议必须在观察正式结果前确定。`digest` 是排序、紧凑、UTF-8 JSON 的 SHA256；`file_hash` 是原始文件字节的 SHA256，两者不能互换。

每个实验臂必须无重叠、无遗漏地覆盖整个注册库；所有臂/分片输出目录互不相同且不能嵌套。种子运行由每个 compound × `[17,43]` 完整确定。当前契约每个父身份只接受一个已准备状态，同一父身份或准备结构不能换 ID 重复计数。多状态评价和跨状态聚合需要新的科学协议和契约，不能在已有计划里择优替换。

选择结果有三种状态：

| 状态 | 含义 |
| --- | --- |
| `accepted` | 当前实验臂所有计划分片已终结，原始测量通过契约，达标的不同父身份数量至少为冻结的最小数量 |
| `insufficient_compounds` | 所有分片已终结，但达标数量不足；失败、无效姿态仍保留在账本和全量观察中 |
| `incomplete` | 仍有未开始、未知或未完整交付的分片/测量；即使已观察到 100 个达标项也不能作为完成的正式名单 |

同一臂的**全部**达标项导出，不截取前 100。`formal_contract_satisfied` 只表示这个计算筛选契约成立，不证明靶点校准有效、实验活性或生物学收益。

## 5. 原始归档和 Effect 的精确接入契约

接入顺序必须是：

```text
register_study
  → dispatch_spec（取回不可变计划确定的绑定）
  → Controller.enqueue / prepare_effect（保存原始 packet 与预约）
  → bind_shard（只接受 prepared Effect，且输出目录还不存在）
  → Controller.dispatch_effect（执行器调用 execution_binding）
  → 保存 native archive 与回执
  → reconcile_shard
  → select_study
```

`prepare_effect(..., metadata={'study_binding': spec})` 中的 `spec` 必须逐字段等于 `dispatch_spec` 返回值，不能自己省略字段。其字段为：

```text
study_id, plan_hash, arm_id, shard_id, phase,
archive_path, archive_adapter, compound_ids,
scientific_protocol, protocol_sha256,
policy_version, policy_sha256, tool_versions
```

执行器在产生结果前写入 `execution_binding(...)` 的完整返回值，保存为 `study_binding.json`：

```python
{
    'effect_id': effect['id'],
    'effect_packet_hash': effect['packet_hash'],
    'binding': spec,
}
```

`native_vina_jobs_v2` 的目录结构如下。每个已开始 job 的 `spec.json` 和 `result.json` 必须符合现有 Vina worker 的真实输出；缺少结果不会被补为成功。

```text
<archive_path>/
  study_binding.json
  protocol.json
  artifact_hashes.json
  frozen/
    protocol.json                 # 如存在，必须与根协议文件字节一致
    vina_worker.py
    run_vina_batch.py
    vina_contract.py
  data/
    candidates.json
    manifest.json
    chemical_states.json
    receptor/
      receptor_manifest.json
      receptor.pdbqt
      receptor_effective.pdb
      reference.sdf
    ...                           # 其余协议绑定的公开输入
  jobs/
    <compound_id>_s17/
      spec.json
      result.json
      ligand.pdbqt
      ligand.sdf
      poses.pdbqt
      top.sdf
      posebusters.csv
      vina.stdout.txt
      vina.stderr.txt
    <compound_id>_s43/
      ...
```

`artifact_hashes.json` 是 `相对路径 → 文件字节 SHA256` 的 JSON 对象，覆盖目录内全部文件，排除文件名为 `artifact_hashes.json` 或 `.lock` 的文件；拒绝遗漏、越界和被更改的文件。Effect 回执中的 `archive_sha256` 是这个清单文件本身的 hash。原始内容、清单、协议和接收回执共同绑定，修改清单不能使已结算的观察被替换。

执行成功回执至少包含以下结构，Controller 保留这个完整回执，`result` 不会被展平。后续补账可以更新 `receipt_ref`，原执行回执保留在 `result_ref`；StudyPlan 对账和选择优先读取 `result_ref`，不把补账凭据误当作原始 archive 结果：

```python
{
    'status': 'succeeded',
    'actual_costs': {
        # 只填写实际测得、且已在 Effect 中预约的资源；不得填估算零值。
        'tool_calls': 1,
        'seed_jobs': measured_seed_jobs,
    },
    'result': {
        'archive_path': spec['archive_path'],
        'archive_sha256': file_hash(Path(spec['archive_path']) / 'artifact_hashes.json'),
        'study_binding': spec,
    },
}
```

成功返回归档不等于其中每个 job 成功。job 失败按缺失处理；有尚未运行或中断的 job 时分片为 `incomplete`。外层调用已证明失败可用 `failed_definite` 结算，整个分片及其候选仍被保留；结果未知则使用 `unknown`，不能绑定替代 Effect 后重新运行。

原始适配器重用现有 `scripts.report_vina_screen.inspect_job`：核验 Vina MODEL 1 原始能量、命令参数、引擎回执、父进程退出回执、重建姿态属性、身份和 PoseBusters 原始 CSV。还要求冻结源代码、完整 receptor/配体输入、严格的化学状态登记、`protocol.phase == 'formal'` 和计划的精确科学 hash。

当前化学读取器仍是 v1 的窄范围：MolScrub 单状态准备和已有探索性 pH 区间契约。它不提供新 pH 条件的判断能力，也不证明一个保留状态就是唯一可能微状态。该读取能力与新多状态研究、受体准备核验或靶点校准不能混为一谈。

`lab_vina_archive_v1` 是保留原始核验的兼容读取器：只有在新的计划与 Effect 前瞻绑定下、符合 phase、policy、科学输入和完整池要求的新归档才可能被接纳。已经存在的 v1 输出会在注册时被拒绝，历史文件不得补写绑定后冒充新的正式研究。

## 6. 能力接入和 Trial 结算

缺信息走授权证据检索、现有诊断或用户提问；缺能力走 `CapabilityRegistry` 的建设请求和实际执行验收。只更新工具名字或提示词不能通过验收。当前验收 runner 对明确 hash 授权、已审查的单文件 Python 适配器运行正常和失败案例，核验实际输出、身份、来源回执及资源，记录代码与包版本。

该 runner 的隔离是独立进程和工作目录，**不是 OS/网络安全沙箱**；不能据此允许任意生成代码自行联网或安装依赖。影响科学结果的能力要求新科学协议和新研究身份，旧结果保留其原始含义。

`branches.BranchExecutor` 对 baseline/candidate 的可执行代码 hash、输入 hash、arm、run 身份、三类版本和 trial 规格再次校验，实际创建独立进程/工作目录并测量本地资源。它与能力验收使用同一外部 Effect 预约/结算链。当前分支 profile 只支持已审查并精确 hash 授权的单文件 Python 适配器；不把进程隔离称为 OS 安全沙箱，也不自行接入大型真实 docking 研究。

`TrialEngine` 预先冻结 policy/capability/scientific_protocol 三类版本、实验对、最低信息要求、指标、保护条件、预算、发布标准和恢复责任。测量来自对应 Effect 的完整回执，不能把模型解释当作测量。结算把执行有效性、证据是否达到标准、版本动作分开；缺失不补零，不达标不等于证明无效，恢复基线不等于证明有害。

重放回答控制正确性问题；重新调用角色回答判断质量问题；同一起点的隔离真实分支才可能回答后续任务收益问题。两个 docking seed 是同一分子的评价重复，不能作为两个独立策略实验。

## 7. 验证与下一步验收

运行新机制的软件测试：

```bash
python3 -m unittest discover -s tests/v2 -p 'test_*.py' -v
```

正式选择器测试包括 105 个全部导出、99 个不足、已有 100 个但仍有未执行分片、跨臂拼接、改变协议或阈值、删除计划分片、已存在输出的事后登记、未知 Effect 重发、重复结算、replay 冒充正式结果，以及不 mock 原始解析器的 Vina/身份/姿态文件 fixture。**测试里的分数与化学文件只是临时软件 fixture，不是筛选结果。**

第一项系统集成目标是：仅提供 docking 目标，系统遇到未预告的前提缺口，能够发现、登记、开展有界调查，区分缺信息和缺能力，取得适用证据或验收可执行能力，随后恢复依赖工作；无法解决时明确挂起并保留责任。`demo.run_demo` 已把“登记问题 → 等待与原文交付 → 请求能力 → 执行正常/失败验收案例 → 唤醒 → 恢复本地任务”接成完整离线流程。它使用明确标注的合成 Meta 决策和元数据 fixture，能力适配器和验收进程则实际执行；**这证明控制流可以运转，不证明模型的科学发现质量**。

后续独立评价需要记录漏诊、误报、发现延迟、调查是否解除阻塞；再与随机选样、简单规则等同预算基线比较任务结果。完整成本包括调查、Meta、工程能力建设、失败、恢复和结算，不能只统计成功 docking。所有比较均允许如实结算为无收益、数量不足、未完成或证据不足。

## 8. CLI 与只读 Python 用法

所有命令从仓库根目录执行。每个新运行使用独立目录。`demo` 不调用真实模型或 docking；完成后再次执行读取已保存的报告，未完成时须先检查恢复状态。`run-until-wait --max-steps 20` 会连续推进可执行的 Meta 工作，直到需要来源/工程/Lab 生产者、达到预算或步数边界；它不是后台守护进程，收到交付或截止期到达后可再次调用。普通等待不阻塞其他可执行工作；prepared 调用复用原 packet，unknown 调用不能重发。

```bash
python3 -m docking_rsi --root runs/new_v2_demo --run offline-demo demo
python3 -m docking_rsi --root runs/new_v2_demo --run offline-demo status
python3 -m docking_rsi --root runs/new_v2_demo --run offline-demo recover
```

真实运行先准备配置 JSON，例如以下字段；预算是新运行的事前配置，不是对既有执行成本的重写。领域审查命令会预约 1 个 tool call、60 秒 CPU/墙钟额度，故配置要容纳这项检查。只有 `meta-step` 或新建 `role-probe` 会主动调用模型。

```json
{
  "execution_mode": "live",
  "goal": "Inspect docking prerequisites and advance a bounded investigation",
  "model": "gpt-6-astra",
  "effort": "medium",
  "policy_version": "policy-v2",
  "tool_version": "registered-tools-v2",
  "scientific_protocol_version": "science-v1",
  "max_external": 1,
  "max_open_reviews": 2,
  "open_review_interval_batches": 1,
  "authorized_sources": ["local_preparation_archive"],
  "budgets": {
    "model_calls": {"limit": 4, "hard": true, "settlement_reserve": 1},
    "tool_calls": {"limit": 4, "hard": true, "settlement_reserve": 1},
    "tokens": {"limit": 200000, "hard": false, "settlement_reserve": 10000},
    "cpu_seconds": {"limit": 300, "hard": false, "settlement_reserve": 60},
    "wall_seconds": {"limit": 1500, "hard": false, "settlement_reserve": 60}
  },
  "model_reservation": {"model_calls": 1, "tokens": 40000, "wall_seconds": 300}
}
```

保存为 `v2-config.json`，另将显式授权的补充路径保存为 `inspection-sources.json`，再逐步执行：

```bash
python3 -m docking_rsi --root runs/new_v2_run --run run1 init --config v2-config.json
python3 -m docking_rsi --root runs/new_v2_run --run run1 inspect-docking \
  --data /absolute/path/to/prepared-data --protocol /absolute/path/to/protocol.json \
  --sources inspection-sources.json --scope exploratory --event preparation-review-001
python3 -m docking_rsi --root runs/new_v2_run --run run1 status
python3 -m docking_rsi --root runs/new_v2_run --run run1 meta-step
```

`inspect-docking` 通过真实 Effect 保存只读审查回执，分别登记 raw facts、source statements 与代码校验结论，并把实际材料交给 `Runtime.observe`。代码结论不混入原始证据冒充模型独立发现。需要 RDKit 的图检查应在已安装依赖的科学 Python 环境调用同一 CLI；环境缺失会保留未验证结果。

其余持久操作按状态中的对象身份推进：

```bash
python3 -m docking_rsi --root runs/new_v2_run --run run1 tick
python3 -m docking_rsi --root runs/new_v2_run --run run1 reply \
  --question QUESTION_ID --version 1 --text 'Actual user reply' --message-id UNIQUE_MESSAGE_ID
python3 -m docking_rsi --root runs/new_v2_run --run run1 deliver \
  --request REQUEST_ID --content delivery.json --provenance provenance.json --source-version 1
python3 -m docking_rsi --root runs/new_v2_run --run run1 register-study --plan study-plan.json
python3 -m docking_rsi --root runs/new_v2_run --run run1 select-study --study screen-001 --arm arm-a
```

运行新的真实两调用探针，需要 `probe-sources.json` 提供 1–16 个 `source_id → 原文文件路径`。创建新目录会产生真实模型成本；已有目录只允许核对相同冻结材料并恢复/检查，不会重新发起已知或未知的模型调用。

```bash
python3 -m docking_rsi --root runs/new_v2_role_probe role-probe --sources probe-sources.json
```

Vina 执行适配器要求协议显式提供 `vina_binary_path` 和匹配的 `vina_sha256`。它不会从个人目录搜索二进制。参考配体相关工程提示需要调用者明确提供带 SHA-256 的来源材料；缺失来源时保留未满足前提状态。
