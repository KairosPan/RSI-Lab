# RSI-Lab

RSI-Lab 是用于研究 Lab 执行、Meta 调查与受控机制修改的代码框架。它通过持久账本记录工作、依赖、工具结果和资源消耗，并将通用控制面与领域能力分开。

本仓库发布源代码、使用文档、目标架构和合成测试。运行数据库、模型对话、真实实验数据、结果报告与截图由使用者保存在自己的运行目录中。

## 代码结构

| 目录 | 职责 |
| --- | --- |
| `rsi_lab/` | SQLite 账本、Lab/Meta 调度、各自持久会话与工作记忆、逐事件日志、上下文交付、能力验收和恢复 |
| `rsi_domains/tabular/` | 使用合成 CSV 的可替换领域能力与离线演示 |
| `rsi_domains/docking/` | Docking 输入契约、领域检查、能力适配和结果核验 |
| `rsi_domains/pablo/` | 分子黑箱评分、精确身份查询、领域测量与研究策略 |
| `rsi_domains/literature/` | 有界的公开方法文献检索、原始回执与来源复用 |
| `docking_rsi/` | Docking 控制面的兼容 CLI 与离线演示 |
| `lab_harness/`、`harness/` | 独立的 Lab 执行循环、提示模板与有界 Meta 版本流程 |
| `scripts/` | 接受显式输入的数据准备、计算执行与结果核验工具 |
| `frontend/` | 架构画布和只读运行记录查看器；默认不加载任何运行 |
| `tests/` | 合成数据、故障注入与契约测试 |

任务的输入、指标、阈值、重复评价和成功条件通过 `TaskPlan` 固定。领域参数由对应能力包解释。

## 当前实现范围

通用核心已有执行账本、证据事实、依赖、交付回执与恢复机制。SQLite 中的 `fact`、`question` 等实体按当前代码语义使用：例如 `question` 保存向用户提问的记录，并非完整的科研问题模型。

通用运行器现在可调度 Lab、Meta 和已注册工具。Lab 与 Meta 各自拥有持久 Codex 会话及版本化工作记忆；新调用通过精确 thread 身份续接，原始事件逐条落盘。模型只提出受控命令，工具由 Controller 执行；工具结果登记后可安排 Lab 的下一步。运行器以明确的工作、预算和等待条件推进，不把 `no_action` 或队列暂时为空视为科学任务达标。入口、恢复和存储边界见[持久 Agent 使用说明](docs/persistent-agents.md)。

研究模式还提供有来源绑定的 Meta→Lab 建议、实际工具执行和结果复核闭环，以及六类版本化 Scientific Memory 对象。领域适配器提供推荐类型、指标解释和优化方向；内核不假定分子搜索或分数最大化。PABLO 宿主将这些接口与真实文献检索接在一起，见 [PABLO 研究入口](docs/pablo-research.md)。记忆对象合法或工具成功不代表科研判断已获验证。

[系统设计 v2.1](docs/system-design.html) 仍是完整目标规格。科学记忆的语义支持验证、全自动工程修复发布，以及可逐请求拦截 Codex 内部工具/压缩/重试的 runtime 尚未全部实现。当前 CLI 后端禁用原生工具，容量检查覆盖可观察的会话历史与本轮输入；它不声称已证明完整 provider 输入或取得模型的全部内部推理。旧 `lab_harness/` 保留为兼容执行器，不与新核心的会话或工作记忆混用。

## 离线运行

从仓库根目录执行。通用核心使用 Python 标准库；以下演示使用合成输入和固定角色响应，无需模型凭证或 Vina。

```sh
python3 -m rsi_lab --help
python3 -m docking_rsi --help
python3 -m lab_harness --help

python3 -c 'from rsi_domains.tabular.demo import run_demo; print(run_demo("runs/tabular-demo"))'
python3 -m docking_rsi --root runs/docking-demo --run demo demo
python3 -m docking_rsi --root runs/docking-demo --run demo status
```

每次新演示使用一个新的输出目录。运行产物写入被 Git 忽略的 `runs/`；演示检查机制行为，不证明真实模型的科研判断质量。

## 接入自己的任务

1. 准备输入文件、协议和任务计划，显式指定文件位置和工具版本。
2. 初始化独立运行目录及预算，注册获准的领域适配器。
3. 用 `lab-start` 提交初始 Lab 工作，再用 `run-until-wait` 推进已注册的 Lab/Meta/工具处理器；保留原始产物及来源。
4. 使用相同运行身份恢复已登记状态；外层回执缺失时可从所属调用日志核验已知终态，缺失费用继续保留对账义务。结果未知或旧 writer 尚未确认停止时先处理这些义务；检查点和显式会话重建保留工作记忆，不重做已完成工具动作。

真实模型运行需要用户自行配置 Codex CLI 和模型访问。Docking 的化学准备与计算工具另需 RDKit、Meeko 等依赖以及明确配置的 Vina 可执行文件；具体依赖和输入见 [使用说明](docs/publication-and-reproduction.md)。本仓库不附带凭证、模型权重、受体或实验数据集。

## 架构画布

前端使用 Node.js 20 或更新版本，无需安装第三方运行依赖。

```sh
cd frontend
npm run dev
```

打开 `http://127.0.0.1:4173`。初始页面显示架构与 **No run selected**，可显式连接自己的账本或导入快照。`npm run build` 只生成应用代码，不打包本地运行数据。配置和导入方法见 [前端说明](frontend/README.md)。

## 检查

```sh
python3 -m unittest discover -s tests/generic -v
python3 -m unittest discover -s tests/v2 -v
python3 -m unittest discover -s tests -v

cd frontend
npm run check
npm test
npm run build
```

科学工具测试可能需要可选依赖；测试输出会区分缺失依赖导致的跳过。正常测试不调用真实模型服务。

## 文档

- [持久会话、工作记忆与逐事件日志](docs/persistent-agents.md)
- [通用核心与领域接口](docs/rsi-lab-core.md)
- [PABLO 研究闭环、科学记忆与文献检索](docs/pablo-research.md)
- [Docking 控制面](docs/docking-rsi-v2.md)
- [独立 Lab harness](docs/lab-harness-v1.md)
- [任务运行器](docs/live-agentic-study.md)
- [Vina 执行与核验工具](docs/bounded-vina-screen.md)
- [目标系统设计](docs/system-design.html)

每个 agent 的工作记忆保存自己的计划、尝试和未决问题；共享账本保存执行事实和费用；Scientific Memory 保存受来源与版本约束的科研状态。工作记忆或会话历史不能直接覆盖共享事实。跨角色传递使用显式 Handoff/Work，接收方重新获得本轮来源；目录或角色名称本身不构成操作系统级保密。
