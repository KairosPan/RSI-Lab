# 真实模型驱动的 Docking 闭环

`scripts/run_agentic_study.py --root <new-study-directory>` 串联真实 Owner/Inspector 调查、一个 development Lab、一次 Meta 提案、两个新 cohort 上的独立 parent/candidate 验证，以及采用 gate 所选策略的新 development Lab。模型通过当前 Codex 登录调用；配体任务使用本地独立 AutoDock Vina。主入口没有全局运行时限，各次模型、worker 调用和 campaign 仍有冻结预算。

研究目录须预先准备 `study_config.json`、四个各20项的候选 cohort、来源证据及化学准备记录。每个 Lab 从20项中提交5项；这不是正式100项筛选。`scripts/build_agentic_dataset.py` 从已有本地 ChEMBL 快照建立不含活性数值及历史 docking 分数的结构输入，排除历史身份，并按准备后结构的骨架划分 cohort。小规模配对 gate 只用于后续 development 版本选择，不构成总体效果或生物活性结论。

已完成的 `agentic_cdk2_live_20261105_r3` 归档与代码分开发布；见[下载、环境与新目录运行说明](publication-and-reproduction.md)和[结果记录](validation-v1.md)。归档中的配置保留原机器路径，仅作证据与配置结构参考；不能直接在新机器上恢复该已完成研究。

## 化学状态准备

`scripts/prepare_chemical_states.py` 使用固定 MolScrub 0.3.2 酸碱规则与两轮互变异构规则。探索性 pH 区间固定7.5–8.0；该区间参考部分历史 assay 描述，不宣称是所有 assay 的共同条件。只有经过 KEEPMAX/KEEPMIN 筛选后恰好一个唯一保留状态、重原子连接及已有立体化学保持的原 compound 才能进入本版单状态任务；多个保留状态、零状态、异常和超时在对接前排除。

`chemical_states.json` 保存 parent → prepared state 映射，相关源代码、规则与依赖版本进入准备方法。候选中的 SMILES 和状态相关特征对应实际准备状态。Controller 冻结这份记录；pose 身份按 prepared state 检查，跨 cohort 去重和正式名单计数按原 compound 身份。Meta 不能更改 pH、状态或 evaluator。

MolScrub 的规则输出不是状态占比预测，唯一返回状态不证明溶液中只有一种状态。上游规则会跳过不合法反应产物，部分酸碱转换清理失败时可能保留输入；通过产物检查不证明每个位点均已得到可靠 pKa 判定。默认互变异构处理是有限的启发式规则；本版不宣称穷举、分子专属 pKa 预测或状态集合的结合自由能。受体仍使用有完整来源记录的旧 1DI8 pilot 准备，缺失结构、水处理和默认残基质子化的限制会提供给 Owner/Inspector 与 Lab。配体准备不等于受体也完成了 pH 推断。

当前工作区将 MolScrub 与轻量依赖安装在 `.runtime/chemical-preparation`，复用已有科学计算 Python 的 RDKit、NumPy 和 Pandas，未修改共享 venv。安装方法为 `uv pip install --python <chemistry-python> --target .runtime/chemical-preparation --no-deps 'https://github.com/forlilab/molscrub/archive/refs/tags/0.3.2.tar.gz' joblib==1.5.2 rich==14.2.0 markdown-it-py==4.0.0 mdurl==0.1.2 Pygments==2.19.2`。每轮记录实际包装脚本、MolScrub 源码/规则内容哈希和依赖版本；没有声称冻结原始下载压缩包。

官方方法来源：[MolScrub](https://github.com/forlilab/molscrub)、[Meeko 配体准备](https://meeko.readthedocs.io/en/develop/lig_prep_basic.html)。

## 运行与证据

Owner 先看到证据目录，自行请求读取；Inspector 独立检查实际交付的内容。用户明确要求的化学准备必须在启动前有证据。尚未产生的验证结果保持未完成状态，只能由已登记的代码检查延后到指定阶段；没有对应检查的缺口仍阻止启动。模型需要用户决定时会返回 `needs_input`，不会把等待或沉默当作回答。

通过调查后，程序冻结 task context 和四份科学协议，再允许 Lab 发起工具任务。每个模型请求、工具批次、记忆更新、名单与原始结果都保存到 campaign。Meta 的真实提案必须有完整调用回执；若提案为 `no_change`，保留 parent，直接继续第二个 development。若有修改，先登记全部两组验证，再独立执行四个验证 arm，完整 gate 后才能选择后续策略。拒绝修改同样是有效结果。

`intake/` 保留模型调查和证据读取回执，`campaigns/` 保存 Lab 原始归档，`meta_01/` 保存提案和全部验证计划，`stage_receipts/` 保存实际阶段检查，`study_state.json` 保存研究进度。失败、未知用量或中断不会自动重试，也不能只凭一份完成摘要继续执行；应先核对原始回执。旧 attempt 的失败记录和成本应与新 attempt 一同报告。
