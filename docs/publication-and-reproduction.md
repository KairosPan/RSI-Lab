# 安装、输入准备与本地运行

本仓库发布通用代码、接口说明和合成测试。运行记录、数据集、模型权重、原始模型对话与结果截图由使用者在本地独立保存，不是安装的前置输入。

## 获取代码与离线检查

```sh
git clone https://github.com/KairosPan/RSI-Lab.git
cd RSI-Lab
python3 -m rsi_lab --help
python3 -m unittest discover -s tests/generic -v
python3 -m unittest discover -s tests/v2 -v
python3 -m unittest discover -s tests -v
```

控制器与合成示例使用 Python 标准库，无需安装领域计算包。按当前 checkout 的测试输出核对失败与跳过项目；文档不保存历史通过数量。

```sh
python3 -c 'from rsi_domains.tabular.demo import run_demo; print(run_demo("runs/tabular-demo"))'
python3 -m rsi_lab --root runs/tabular-demo --run tabular-portability status
```

示例实际执行 CSV 校验、单位换算、能力验收与恢复，角色决策使用固定合成响应。每个新任务使用独立的新目录；已存在运行的恢复须核对持久状态和未决调用，不能通过删除状态重新执行。示例细节见[通用核心说明](rsi-lab-core.md#合成-csv-示例)。

## 可选运行环境

| 用途 | 依赖 |
| --- | --- |
| 前端 | Node.js 20 或更新版本；见[前端说明](../frontend/README.md) |
| 模型决策 | 已配置的 Codex CLI、账户可用的模型，以及明确冻结的 model / effort |
| 自定义能力 | 该适配器声明的依赖、输入契约、资源与恢复方式，由任务宿主显式配置 |

凭证保留在本地登录或环境配置中，不放进任务数据与 Git。对自定义计算环境记录实际版本、执行路径、源码或二进制 hash；不同平台不能直接假定执行实现相同。核心的本地验收与分支执行器使用 Unix 进程及资源控制接口，不提供操作系统或网络安全沙箱。

## 准备自己的输入

任务宿主负责提供以下材料：

- 有稳定身份与来源的输入；需要的单位、格式和适用条件不得猜测补齐。
- 冻结的 `TaskPlan`：输入 hash、协议、能力版本、指标、全部计划对象与成功条件。
- 独立运行目录、明确预算，以及被宿主信任和注册的适配器。
- 需要外部材料时，登记来源范围与交付责任；需要用户回复时，使用有版本和消息身份的提问记录。

`rsi_lab.producers` 提供通用来源、回复与工程交付辅助实现。它们交付来源或产物，不能绕过任务授权、能力验收或把交付成功当作目标达成。第三方来源内容是输入材料，不赋予模型额外执行权限。

初始化、`lab-start`、调度、checkpoint、恢复与停止的完整配置示例见[持久 Agent 使用说明](persistent-agents.md)。真实模型与工具调用会产生实际资源消耗，每次调用保存身份、输入、输出、回执和用量。

## 运行与发布边界

`runs/`、本地输入目录、SQLite 账本、逐事件日志、工作记忆、原始对话、checkpoint 与构建产物留在本地。前端默认没有预载运行，只有显式导入或连接后才显示数据。

分享代码前检查 tracked 文件与待提交 diff；`.gitignore` 只影响未跟踪文件，不能自动移除已经跟踪的记录。合成测试中的常量和固定响应属于机制 fixture，不是实际实验记录。分享运行材料时另行选择有权限分发的内容，核对原始对话、来源及环境信息。
