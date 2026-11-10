# 安装、输入准备与本地运行

本仓库发布代码、接口说明和合成测试。运行记录、数据集、模型权重、原始模型对话与结果截图由使用者在本地独立保存，不是软件安装的前置输入。

## 获取代码与离线检查

```sh
git clone https://github.com/KairosPan/RSI-Lab.git
cd RSI-Lab
python3 -m rsi_lab --help
python3 -m unittest discover -s tests/generic -v
python3 -m unittest discover -s tests/v2 -v
python3 -m unittest discover -s tests -v
```

控制器与合成 demo 使用 Python 标准库。按当前 checkout 的测试输出核对失败和跳过项目；文档不保存固定的历史通过数量。

```sh
python3 -m docking_rsi --root runs/new_demo --run demo demo
python3 -m docking_rsi --root runs/new_demo --run demo status
python3 -m lab_harness demo --out runs/new_v1_demo --with-meta
```

每个示例使用独立的新目录。合成响应与测试数值仅用于机制验收，不是科学结果。已存在运行的恢复须核对持久状态与未决外部调用，不能通过删除状态重新执行。

## 可选运行环境

| 用途 | 依赖 |
| --- | --- |
| 前端 | Node.js；见[前端说明](../frontend/README.md) |
| 模型决策 | 已配置的 Codex CLI、当前账户可用的模型，以及明确冻结的 model / effort |
| Vina worker | 独立 AutoDock Vina 1.2.7，及含 RDKit、Meeko、PoseBusters 的科学 Python 环境 |
| 化学状态准备 | 与准备脚本契约兼容的 MolScrub 及其依赖；见[研究运行器](live-agentic-study.md) |
| PABLO 评分与宿主 | Python 3.11 和 [requirements-pablo.txt](../requirements-pablo.txt) 中的可选科学依赖；见 [PABLO 研究入口](pablo-research.md) |
| 文献检索 | Python 标准库与 Europe PMC HTTPS 访问，不需要另设 API key |

外部环境由使用者配置。记录实际依赖版本、执行路径、源码和二进制哈希；不同平台不复用他人的二进制身份。协议中的 `vina_binary_path` 必须指向实际文件，并与 `vina_sha256` 匹配。凭证保留在本地登录或环境配置中，不放进任务数据和 Git。

PABLO 的可选环境可单独安装；宿主使用 `fcntl`、`resource` 等 Unix 接口。以下版本组合已在 macOS 的 Python 3.11 上验证，不是通用内核的安装要求：

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements-pablo.txt
.venv/bin/python -m pip install --force-reinstall --no-deps rdkit==2024.9.6
.venv/bin/python -m unittest discover -s tests -v
```

该环境包含 GuacaMol、RDKit、NumPy、SciPy 和 Pandas。GuacaMol 的传递依赖会引入旧的 `rdkit-pypi` 包，因此安装后重新安装已固定版本的 `rdkit`，确保实际导入的共享模块来自该版本。评分使用前四项，宿主还将 Pandas 版本写入运行环境记录。缺少科学依赖时，PABLO 的真实评分与身份测试明确跳过；通用控制面、合成闭环与文献回执测试仍可离线运行。全套测试还包括依赖 NumPy 与 RDKit 的独立几何检查，运行完整测试集前应安装科学环境。真实模型调用另需本地 Codex 登录及相应模型访问。

## 准备自己的输入

`scripts/build_agentic_dataset.py` 接受调用者提供的来源、元数据和受体。以下路径均为占位符；输出目录必须尚不存在。

```sh
python3 scripts/build_agentic_dataset.py \
  --source /path/to/source.json \
  --source-metadata /path/to/source.meta.json \
  --receptor /path/to/prepared-receptor \
  --out runs/new_study/data
```

构建器保留固定 cohort 分配和结构准入规则，它不是通用数据下载器。输入内容必须满足该脚本的契约。需要化学状态准备时，先配置相应依赖，再显式添加 `--chemical-preparation`。

过去已使用的对象可由 `--prior-data`、可重复的 `--prior-run-root` / `--exclude-catalog` 或明确的 `--exclusions` 文件登记。默认不会扫描个人工作区或自动引入其他运行；未提供排除材料时不能声称输入与既有研究独立。

运行前固定来源授权、科学条件、评价方法和预算。计算可执行不等于科学前提已满足，具体准入见[Lab 使用说明](lab-harness-v1.md)、[Docking 控制契约](docking-rsi-v2.md)。

## 运行与文件管理

新研究先准备 `study_config.json`、来源证据和冻结输入，再按[研究运行器](live-agentic-study.md)启动。真实模型与计算调用会产生实际资源消耗；每次调用保存身份、输入、输出、回执和用量。

`runs/`、本地输入目录、SQLite 账本、日志与构建产物不纳入版本管理。分享代码时使用干净源码；分享运行记录时另行选择有权限分发的材料，并检查其中的原始对话、来源与环境信息。
