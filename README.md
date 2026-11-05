# RSI-Lab

Recursive Self-Improvement for In-Silico Compound Screening

RSI-Lab 研究化合物筛选智能体如何在固定计算评测协议与资源预算下，自主安排计算实验，并利用跨轮次执行经验改进提示词、代码和证据处理机制。

**当前分支：Lab harness v1。** 提供本地 Python Controller、Owner/Inspector 前置调查、版本化 H0、Codex 调用、MolScrub 配体状态准备、独立 AutoDock Vina 1.2.7 对接、科学记忆及有范围限制的 Meta 修改。自动执行器串联 Lab、验证预登记、配对 gate 和后续 Lab；具体运行结果以独立归档为准，实现本身不证明 Meta 改进收益。

```sh
python3 -m lab_harness demo --out runs/lab_demo_v1 --with-meta
python3 -m unittest discover -s tests -v
```

正式筛选使用独立 AutoDock Vina 的 docking 能量。运行 `python3 -m lab_harness select-final --input manifest.json --out selection.json` 可从已完成的 live Lab 归档核验原始对接结果。两个种子各取 Vina 排名第一的姿态。两者都有效且 PDBQT 中的 Vina affinity 中位数小于或等于冻结阈值，化合物才入选。按原 compound 身份去重后至少须有 **100 项**；准备后的不同状态不能重复计数。每次 20 个候选选 5 项的流程用于 pilot。

运行方式、Meta 范围、固定 CDK2 profile 和预算口径见 [Lab harness v1 运行说明](docs/lab-harness-v1.md)。

代码位于私有 [GitHub 仓库](https://github.com/KairosPan/RSI-Lab/tree/codex/lab-harness-v1)，数据与运行证据位于私有 [Hugging Face 数据集](https://huggingface.co/datasets/Kairoszhe/RSI-Lab-docking)。访问需要相应权限；下载、环境与重新运行步骤见[发布与复现说明](docs/publication-and-reproduction.md)。

2026-11-05 的真实探索性闭环完成了 6 个 Lab campaign、22 次模型调用及 92 个 seed 作业，覆盖 35 个不同的已对接 compound。两组配对验证中，候选 Harness 的 Lab input+output tokens 合计下降 31.3%，通过了预登记的成本路径；这不包含 Meta 提案成本，也不证明普遍收益或正式 100 项名单达标。结果口径见[验证记录](docs/validation-v1.md)。

## 前端预览

[RSI-Lab 科研工作台](frontend/README.md) 包含实验概览、候选筛选、证据追溯和 Harness 演进。所有操作均为本地演示，未连接计算后端。

```sh
cd frontend
npm run dev
```

打开 `http://127.0.0.1:4173`。无需安装第三方依赖；运行 `npm run build` 可生成独立 HTML 预览。

## 文档导航

| 内容 | 文件 |
| --- | --- |
| Lab 与 Meta 运行入口 | [Lab harness v1](docs/lab-harness-v1.md) |
| 代码、数据下载与环境复现 | [发布与复现说明](docs/publication-and-reproduction.md) |
| 当前真实运行与历史软件验证 | [验证记录](docs/validation-v1.md) |
| 真实模型驱动的完整运行与化学准备 | [Live agentic study](docs/live-agentic-study.md) |
| 限时本地探索性筛选与结果核验 | [Bounded Vina screen](docs/bounded-vina-screen.md) |
| 面向 AutoDock Vina 的专用编排设计 | [Docking 专用设计 v1](docs/docking-agentic-design.md) |
| 项目 proposal | [Proposal v8](output/docx/RSI-Lab_Final_Proposal_Revised_v8.docx) |
| 完整实验方案 | [实验方案 v2](output/experiments/RSI-Lab_Experiment_Plan_v2_zh.md) |
| Agent 角色、调用顺序与信息传递 | [Codex 编排说明](output/experiments/codex_orchestration/README_zh.md) |
| 各角色的输入提示词 | [Prompt 模板](output/experiments/codex_orchestration/prompts/) |
| Lab 输入、决策和工具返回 | [JSON 消息示例](output/experiments/codex_orchestration/examples/) |

## 系统设计

- **Controller**：普通程序，负责状态管理、请求校验、预算计量和任务调度。
- **Lab Codex**：在单个 campaign 内规划计算、解释证据、更新科学记忆并提交候选名单。
- **Meta Codex**：在 campaign 之间诊断运行轨迹，提出小幅、版本化的 Harness 修改。
- **Docking 评估器**执行 Vina CPU 对接，记录各个种子的姿态、能量、失败状态与实际成本。
- **Validator / Release Gate**：依据固定协议验证候选版本，决定是否在后续 campaign 启用。

底座模型与预测模型权重保持固定。核心对照比较初始固定机制 H0 与经过跨轮次修改和验证的 H*；两者使用相同公开证据、工具及资源上限。计算验证通过不等同于真实生物活性或药效验证。

## 仓库范围

仓库保存研究文档、编排模板、前端原型和 Lab/Meta harness。数据集、模型权重、本地凭证和运行产物不纳入版本管理。新 worktree 不自动复制原 checkout 的未提交实验数据或历史结果。

第一版固定沿用 CDK2/Vina 执行 profile。自动云端传输、受体 pH 状态推断、相互作用几何分析、任意代码级 Meta 修改及具有统计支持的正式 H0/H* 研究仍需后续完成。
