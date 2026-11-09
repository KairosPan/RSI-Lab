# Docking 研究运行器

`scripts/run_agentic_study.py --root <new-study-directory>` 串联 Owner/Inspector 调查、development Lab、Meta 提案、独立 parent/candidate 验证和后续 development。模型通过 Codex CLI 调用，配体任务由独立 AutoDock Vina 执行。该入口属于固定 profile 的 v1 流程；通用控制面与完整目标架构见[核心说明](rsi-lab-core.md)和[系统设计](system-design.html)。

## 输入与配置

研究目录须由使用者准备，仓库不附带研究结果或可恢复的运行快照：

- `study_config.json`：实际 harness、worker Python、Vina、来源文件位置，模型、effort、调用额度、目标和 cohort 名称。
- 四个各 20 项的候选 cohort，满足当前 v1 的结构、身份和来源契约；每次 Lab 提交 5 项。
- 来源原文、能力描述、科学准备记录及对应哈希。
- `guards`、`required_before_launch` 与实际可执行的阶段检查。

`build_agentic_dataset.py` 可从明确提供的来源建立结构输入，并按固定方法分配 cohort。参数和环境见[安装与输入准备](publication-and-reproduction.md)。该工具不自动取得科学授权，也不自动证明候选池独立性。

```sh
python3 scripts/run_agentic_study.py --root runs/new_study
```

每次使用独立的新目录。主入口没有全局墙钟上限，各模型、worker 调用和 campaign 仍受各自冻结预算约束。不得把已有的 study state、通过标记或结果摘要当作新运行的初始输入。

## 化学状态准备

`scripts/prepare_chemical_states.py` 使用固定 MolScrub 0.3.2 规则。当前能力限定于单状态准入与脚本定义的 pH 范围；使用者须确认该范围适用于任务，不能把它推广为所有 assay 的共同条件。多个保留状态、无状态、异常和超时应在对接前保留明确的未准入结果。

`chemical_states.json` 保存 parent 到 prepared state 的映射；准备步骤、规则与依赖版本进入方法记录。Controller 冻结对应数据，姿态身份按 prepared state 核验，跨 cohort 去重与名单计数按 parent 身份核验。Meta 不能在运行中更改科学评价方法。

MolScrub 规则输出不是状态占比预测；唯一返回状态不证明溶液中只有一种微状态。受体完整性、质子化、水/辅因子、搜索盒和协议适用性仍需各自来源与检查，不能由配体准备成功代替。

当前包装脚本按 `.runtime/chemical-preparation` 查找可选 MolScrub 依赖。安装与版本配置由使用者完成，运行时记录实际源码和依赖 hash。方法来源：[MolScrub](https://github.com/forlilab/molscrub)、[Meeko 配体准备](https://meeko.readthedocs.io/en/develop/lig_prep_basic.html)。

## 运行与证据

Owner 从证据目录请求材料；Inspector 检查实际交付内容。必须满足的科学要求在执行前有可核对的证据。需要用户输入时保存明确状态，不把超时、猜测或沉默当作回答。

调查通过后冻结 task context 和科学协议，再允许 Lab 请求工具。Meta 可返回 `no_change`；有候选修改时，先登记所有验证配对，再独立执行各臂并结算 gate。采用只影响约定的后续工作，不能修改已产生的事实或费用。

运行器生成以下本地数据：

| 路径 | 内容 |
| --- | --- |
| `intake/` | 调查输入、响应和证据读取回执 |
| `campaigns/` | Lab 决策、计算、科学记忆、锁定名单与原始产物 |
| `meta_01/` | 修改提案、候选版本与验证计划 |
| `stage_receipts/` | 实际阶段检查 |
| `study_state.json` | 当前进度及恢复所需状态 |

失败、未知用量或中断不自动重试。恢复前核对原始回执和未决义务；历史消耗不能被重建状态清零。软件 gate 是固定协议内的采用判定，不等于普遍改进或生物活性证明。
