# RSI-Lab

RSI-Lab 是用于研究 Lab 执行、Meta 调查与受控机制修改的通用框架。它通过持久账本记录工作、依赖、工具结果和资源消耗，任务语义由显式注册的适配器与冻结计划提供。

仓库只保留通用框架、合成 CSV 示例、使用文档、目标架构和机制测试。运行数据库、模型对话、实际任务输入、结果报告与截图保存在使用者自己的运行目录中。

## 代码结构

| 目录 | 职责 |
| --- | --- |
| `rsi_lab/` | SQLite 账本、Lab/Meta 调度、各自持久会话与工作记忆、逐事件日志、上下文交付、Scientific Memory、能力验收和恢复 |
| `rsi_lab/producers/` | 冻结来源交付、用户回复和工程产物交付的通用生产者 |
| `rsi_literature/` | Lab 的 Literature 子 agent 使用的文献检索、原文存储/分段交付及可选研究工具桥 |
| `rsi_domains/tabular/` | 合成 CSV 单位换算、记录身份校验、聚合能力与离线演示 |
| `frontend/` | 架构画布和只读运行记录查看器；默认不加载任何运行 |
| `tests/` | 合成数据、故障注入与契约测试 |

任务的输入、指标、阈值、重复评价和成功条件通过 `TaskPlan` 固定。能力包解释任务参数，内核不固定评分方向、对象数量或领域依赖。

## 当前实现范围

通用核心已有 Work/Effect 执行账本、证据事实、依赖、交付回执与恢复机制。Lab 与 Meta 各自拥有持久 Codex 会话及版本化工作记忆；新调用通过精确 thread 身份续接，原始事件逐条落盘。模型提出受控命令，由 Controller 执行已注册工具。运行器按工作、预算和等待条件推进，队列暂时为空或 `no_action` 不表示任务达标。入口与恢复边界见[持久 Agent 使用说明](docs/persistent-agents.md)。

研究模式提供有来源绑定的 Meta→Lab 建议、工具执行与结果复核闭环，以及六类版本化 Scientific Memory 对象。任务宿主提供推荐类型、指标解释和优化方向。对象格式合法、工具成功或记忆被多次引用，都不代表相关判断已经过语义支持验证。API 与存储边界见[通用核心说明](docs/rsi-lab-core.md)。

Lab 现在也可通过 `request_literature` 委托具有独立持久会话的 Literature 子 agent。子 agent 在任务额度内循环调用文献工具，阶段性或最终结果连同实际交付的原文证据返回所属 Lab。文献包定义 13 个能力，包含 9 个主线与 6 个可选来源、解析器或研究项目的接口/部署桥；默认启用 6 个主线，包括无需模型凭证的 Poppler 文本提取与页面渲染。PDF 阅读沿文本定位、单页渲染、图像实际交付和视觉核对进行；模型不获得全局 shell，运行配置的 `native_capabilities` 为 Literature 开启终端时，它得到的也只是受限终端：可以读写自身工作目录，只读宿主列出的目录，不能联网。需凭证、模型或尚未部署的项目保持关闭，配置通过不表示已经联网验收。原生元数据接口使用标准库；SDK、模型、托管凭证与研究项目部署另行配置。用法、实测范围及内部调用成本的可见性见[文献工具使用说明](docs/literature-tools.md)。

普通 Lab 轮次按外部知识缺口、当前已交付原文、所属子任务与剩余额度自主决定是否委托，任务不必明确要求检索。启用新版文献工具包时，每轮记录可审计的 `literature_decision`；已有充分原文或只需本地观测的任务直接推进。旧版领域检索接口保留原协议。机制与真实正反例、持续 200 次评分验证见[自主决策报告](docs/literature-autonomy-validation.md)。

[系统设计 v2.1](docs/system-design.html) 是完整目标规格。科学记忆的完整语义支持验证、全自动工程修复发布，以及可逐请求拦截 Codex 内部工具/压缩/重试的 runtime 尚未全部实现。运行配置没有 `native_capabilities` 时，CLI 后端禁用 Codex 原生工具；有这个块时，按角色开启网页搜索、受限终端和子 agent，由 Codex 在一轮内自行调用，调用结束后记录，并按 Codex 自己的用量记录计费，不逐次经过 Controller 准入。开启子 agent 时还可以声明具名角色（`subagents.roles`），每次调用前写成 Codex 的角色文件；回执的 `child_threads` 记下每个子 agent 的角色、是否带着父调用的历史开始、模型与 effort，以及它在本轮的搜索词和用量。每个角色还可以声明工具服务（由 Codex 在本地启动或按 URL 连接的 MCP 服务器）、查看图片和一个冻结的技能文件夹；本地工具服务的进程在调用结束时被终止；Controller 中途退出时，恢复一确认调用属于该 Effect 就终止它们，早于报告日志缺口和结算，见[工具服务与查看图片](docs/persistent-agents.md#工具服务与查看图片)和[技能](docs/persistent-agents.md#技能)。示例配置见 [native-capabilities-run.json](examples/native-capabilities-run.json)，说明见[持久 Agent 使用说明](docs/persistent-agents.md#codex-原生工具)。容量检查覆盖可观察的会话历史与本轮输入，不声称已认证完整 provider 输入或取得模型全部内部推理。

## 离线运行

从仓库根目录执行。通用核心与 CSV 示例使用 Python 标准库；演示使用合成输入和固定角色响应，无需模型凭证或额外计算依赖。

```sh
python3 -m rsi_lab --help
python3 -c 'from rsi_domains.tabular.demo import run_demo; print(run_demo("runs/tabular-demo"))'
python3 -m rsi_lab --root runs/tabular-demo --run tabular-portability status
```

每个新演示使用新的输出目录。产物写入被 Git 忽略的 `runs/`；演示验证等待、能力验收、实际聚合、Trial 和恢复，不证明真实模型的判断质量。

## 接入自己的任务

1. 准备输入、方法协议与任务计划，显式指定来源、工具版本和成功条件。
2. 初始化独立运行目录及预算，注册获准的适配器。
3. 第一次调用模型之前，在宿主脚本里调用 `runtime.preflight(...)`，或用 `features` 子命令，核对这次运行确实启用了要用的科研功能和工具。做法见[启动前检查](docs/rsi-lab-core.md#启动前检查)。
4. 用 `lab-start` 提交初始 Lab 工作，再用 `run-until-wait` 推进已注册的 Lab/Meta/工具处理器。
5. 重启后使用相同运行身份执行 `recover`，先核对已登记结果、费用与未知调用，再继续就绪工作。会话重建保留工作记忆，不重做已完成工具动作。

真实模型运行需要自行配置 Codex CLI 与模型访问。内核只在实测过的 Codex CLI 版本上发起调用，目前是 0.170.0，其他版本会被拒绝并说明原因。配置模板见[持久 Agent 使用说明](docs/persistent-agents.md)，环境与文件管理见[安装与运行](docs/publication-and-reproduction.md)。

文献阅读可使用 [运行配置](examples/literature-reading-run.json) 与 [工具配置](examples/literature-tools.json)。前者分别限制单次文本输入、持久会话历史和图片容量，适用于连续的请求页面、看图与回传结果；先按任务修改目标并在工具配置登记本地论文。初始化本身不调用模型：

```sh
python3 -m rsi_lab --root runs/literature-reading --run reading \
  --literature-config examples/literature-tools.json \
  init --config examples/literature-reading-run.json
```

## 架构画布

前端使用 Node.js 20 或更新版本，无需安装第三方运行依赖。

```sh
cd frontend
npm run dev
```

打开 `http://127.0.0.1:4173`。初始页面显示架构与 **No run selected**，可显式连接自己的账本或导入快照。`npm run build` 只生成应用代码。配置和导入方法见[前端说明](frontend/README.md)。

## 检查

```sh
python3 -m unittest discover -s tests/generic -v
python3 -m unittest discover -s tests/v2 -v
python3 -m unittest discover -s tests/literature -v
python3 -m unittest discover -s tests -v

cd frontend
npm run check
npm test
npm run build
```

标准测试使用合成状态与受控子进程，不调用真实模型服务。按当前 checkout 的输出核对结果。

## 文档

- [持久会话、工作记忆与逐事件日志](docs/persistent-agents.md)
- [文献工具、9 个主线与 6 个可选接入](docs/literature-tools.md)
- [Literature 子 agent：已实现协议与后续设计](docs/literature-agent-design.md)
- [真实论文：检索、全文、看图与引用追踪验收](docs/literature-real-paper-validation.md)
- [Lab 自主文献决策：正反例与新 200 次评估验证](docs/literature-autonomy-validation.md)
- [通用核心、Scientific Memory 与任务接口](docs/rsi-lab-core.md)
- [安装、输入准备与本地运行](docs/publication-and-reproduction.md)
- [目标系统设计](docs/system-design.html)

每个 agent 的工作记忆保存自己的计划、尝试和未决问题；共享账本保存执行事实和费用；Scientific Memory 保存受来源与版本约束的研究状态。工作记忆或会话历史不能覆盖共享事实。跨角色传递使用显式 Handoff/Work，接收方重新获得本轮来源；目录或角色名称本身不构成操作系统级保密。
