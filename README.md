# RSI-Lab

Recursive Self-Improvement for In-Silico Compound Screening

RSI-Lab 研究化合物筛选智能体如何在固定计算评测协议与资源预算下，自主安排计算实验，并利用跨轮次执行经验改进提示词、代码和证据处理机制。

**当前阶段：研究设计与编排模板。** 本仓库尚未实现调度器或运行生物计算，不包含已验证的性能结果。消息示例中的数值均为演示数据，正式实验参数将在 pilot 后冻结。

## 文档导航

| 内容 | 文件 |
| --- | --- |
| 项目 proposal | [Proposal v8](output/docx/RSI-Lab_Final_Proposal_Revised_v8.docx) |
| 完整实验方案 | [实验方案 v2](output/experiments/RSI-Lab_Experiment_Plan_v2_zh.md) |
| Agent 角色、调用顺序与信息传递 | [Codex 编排说明](output/experiments/codex_orchestration/README_zh.md) |
| 各角色的输入提示词 | [Prompt 模板](output/experiments/codex_orchestration/prompts/) |
| Lab 输入、决策和工具返回 | [JSON 消息示例](output/experiments/codex_orchestration/examples/) |

## 系统设计

- **Controller**：普通程序，负责状态管理、请求校验、预算计量和任务调度。
- **Lab Codex**：在单个 campaign 内规划计算、解释证据、更新科学记忆并提交候选名单。
- **Meta Codex**：在 campaign 之间诊断运行轨迹，提出小幅、版本化的 Harness 修改。
- **数值工具与外部评估器**：执行计算，保存原始结果、失败记录和实际成本。
- **Validator / Release Gate**：依据固定协议验证候选版本，决定是否在后续 campaign 启用。

底座模型与预测模型权重保持固定。核心对照比较初始固定机制 H0 与经过跨轮次修改和验证的 H*；两者使用相同公开证据、工具及资源上限。计算验证通过不等同于真实生物活性或药效验证。

## 仓库范围

首版保存最新研究文档与编排模板。历史草稿、临时渲染文件、本地凭证、数据集、模型权重和运行产物不纳入版本管理。

后续实现将按实验方案补齐 Controller、工具适配器、证据存储和验证程序，再开展 pilot 与正式对比实验。
