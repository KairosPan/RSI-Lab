# RSI-Lab

RSI-Lab 是用于研究 Lab 执行、Meta 调查与受控机制修改的代码框架。它通过持久账本记录工作、依赖、工具结果和资源消耗，并将通用控制面与领域能力分开。

本仓库发布源代码、使用文档、目标架构和合成测试。运行数据库、模型对话、真实实验数据、结果报告与截图由使用者保存在自己的运行目录中。

## 代码结构

| 目录 | 职责 |
| --- | --- |
| `rsi_lab/` | SQLite 事件与实体投影、Work/Wait、预算、上下文交付、Meta 调度、能力验收、Trial 和恢复 |
| `rsi_domains/tabular/` | 使用合成 CSV 的可替换领域能力与离线演示 |
| `rsi_domains/docking/` | Docking 输入契约、领域检查、能力适配和结果核验 |
| `docking_rsi/` | Docking 控制面的兼容 CLI 与离线演示 |
| `lab_harness/`、`harness/` | 独立的 Lab 执行循环、提示模板与有界 Meta 版本流程 |
| `scripts/` | 接受显式输入的数据准备、计算执行与结果核验工具 |
| `frontend/` | 架构画布和只读运行记录查看器；默认不加载任何运行 |
| `tests/` | 合成数据、故障注入与契约测试 |

任务的输入、指标、阈值、重复评价和成功条件通过 `TaskPlan` 固定。领域参数由对应能力包解释。

## 当前实现范围

通用核心已有执行账本、证据事实、依赖、交付回执与恢复机制。SQLite 中的 `fact`、`question` 等实体按当前代码语义使用：例如 `question` 保存向用户提问的记录，并非完整的科研问题模型。

[系统设计 v2.1](docs/system-design.html) 描述目标架构。其六类 Scientific Memory 对象、完整的 Lab 与 Meta 持续协作，以及 Codex harness 自动修改和热切换等契约尚未全部实现。当前通用运行器主要推进 Meta 和已注册工具；独立的 Lab 循环位于 `lab_harness/`，仍需进一步接入通用控制面。

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
3. 通过控制器提交工作、执行工具和摄取结果；保留原始产物及来源。
4. 使用相同运行身份恢复已登记状态，对结果未知的调用先对账。

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

- [通用核心与领域接口](docs/rsi-lab-core.md)
- [Docking 控制面](docs/docking-rsi-v2.md)
- [独立 Lab harness](docs/lab-harness-v1.md)
- [任务运行器](docs/live-agentic-study.md)
- [Vina 执行与核验工具](docs/bounded-vina-screen.md)
- [目标系统设计](docs/system-design.html)

Agent 的局部上下文、共享执行账本和共享科研记忆有不同的归属与权威。角色看到的数据由控制器选择和交付；目录或角色名称本身不构成访问隔离。完整边界见系统设计第 9 节。
