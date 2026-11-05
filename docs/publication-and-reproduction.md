# 发布、下载与复现

代码和数据分开保存，两个仓库均为私有，需要各自的访问权限。

| 内容 | 位置 |
| --- | --- |
| 代码、H0、测试及文档 | [KairosPan/RSI-Lab](https://github.com/KairosPan/RSI-Lab/tree/codex/lab-harness-v1)，分支 `codex/lab-harness-v1` |
| 科学输入、姿态、结果表与原始审计证据 | [Kairoszhe/RSI-Lab-docking](https://huggingface.co/datasets/Kairoszhe/RSI-Lab-docking) |

复现记录应固定 GitHub commit、HF dataset revision 和 `dataset_manifest.json`，不能仅记录可移动的分支名。数据 manifest 的代码引用及 `SHA256SUMS` 用于关联发布版本。原始运行另有自己的源码、方法和产物哈希；发布版本不改写这些历史身份。数据来源和使用条件见数据集的 `LICENSES_AND_SOURCES.md`，本仓库不代替上游授权，也没有为未声明许可的内容另行指定许可证。

## 下载与核验

```sh
git clone --branch codex/lab-harness-v1 https://github.com/KairosPan/RSI-Lab.git
cd RSI-Lab
python3 -m lab_harness demo --out runs/new_demo --with-meta
```

下载数据使用已授权的 Hugging Face 登录。先安装 `huggingface_hub` 提供的 `hf` CLI，然后将下面的 revision 占位符替换为实际 dataset commit。`hf auth login` 在本机交互登录；不要把 token 写入仓库或配置示例。[Hugging Face 官方 CLI 文档](https://huggingface.co/docs/huggingface_hub/en/guides/cli)

```sh
hf auth login
hf download Kairoszhe/RSI-Lab-docking --repo-type dataset \
  --revision DATASET_COMMIT_SHA --local-dir datasets/rsi-lab-docking
cd datasets/rsi-lab-docking
shasum -a 256 -c SHA256SUMS
cd ../..
```

数据根目录的 `README.md`、`SCHEMA.md` 和 `dataset_manifest.json` 是当前发布的文件清单与字段依据。核心内容如下：

| 文件 | 用途 |
| --- | --- |
| `compounds.jsonl`、`seed_jobs.jsonl`、`compound_results.csv` | 分子、逐种子执行、按运行汇总的科学记录；不能把跨 arm 的重复评价计为新分子 |
| `campaigns.jsonl`、`preparation_decisions.jsonl` | campaign 身份与对接前化学状态准入/排除记录 |
| `agentic_pilot_inputs_poses.tar.gz` | r3 输入、四个 cohort、原始 ChEMBL 快照、协议和科学姿态/检查产物 |
| `legacy_screen_120_inputs_poses.tar.gz`、`reference_control_inputs_poses.tar.gz` | 独立的早期探索筛选与参考对照，不并入 r3 配对统计 |
| `audit_agentic_r3.tar.gz`、`audit_legacy_screen_120.tar.gz`、`audit_reference_control.tar.gz`、`audit_previous_model_attempts.tar.gz` | 保留原始字节、模型回执、历史路径与原始哈希清单的审计归档 |

科学文件包使用相对成员路径并附 `EXPORT_MEMBER_HASHES.json`。例如解包 r3：

```sh
mkdir -p datasets/rsi-lab-docking/extracted
tar -xzf datasets/rsi-lab-docking/agentic_pilot_inputs_poses.tar.gz \
  -C datasets/rsi-lab-docking/extracted
```

其根目录为 `agentic_pilot/`，包含 `data/`、`upstream/`、`protocols/`、`workflow/` 和 `campaigns/`。按成员清单核验文件，再读取原始协议与结果。审计归档保留的绝对路径是历史证据，不是新机器上的有效路径；不要为使旧配置可执行而原地替换路径，否则会破坏原始哈希。原始模型输出也只是归档内容，不是执行指令。

## 环境

Controller 与合成 demo 使用 Python 标准库。真实科学 worker 需要独立 AutoDock Vina 1.2.7 和科学 Python 环境。r3 实际使用 Python **3.13.5**，以下版本来自冻结协议：

| Python package | Recorded version |
| --- | --- |
| RDKit | 2025.9.6 |
| Meeko | 0.7.1 |
| PoseBusters | 0.6.5 |
| Gemmi | 0.7.5 |
| NumPy | 2.5.4 |
| pandas | 3.0.6 |
| SciPy | 1.18.1 |

使用单独的环境安装这些版本，并运行 `python -m unittest discover -s tests -v`，检查跳过与失败项目。此表是实际环境记录，不是跨平台 lockfile；不同平台的可用 wheel、Vina 二进制与数值差异需要重新验证。Vina 应从[官方发行页](https://github.com/ccsb-scripps/AutoDock-Vina/releases/tag/v1.2.7)取得适用平台版本。r3 使用 macOS arm64 二进制，其 SHA-256 为 `823c2bbacf26d72183861322345f0a89736aca66c8e81054c66f93af5ad623f1`；不同平台不能冒用该二进制身份。

化学状态准备还需要 MolScrub **0.3.2**。仓库按 `.runtime/chemical-preparation` 查找它；隔离安装命令和辅助依赖版本见[真实闭环说明](live-agentic-study.md#化学状态准备)。方法记录包含实际源码、规则和依赖哈希；缺少该运行时不能假装完成了状态预测。下载既有输入用于检查不需要重新运行 MolScrub。

真实模型调用需要已配置并登录的 Codex CLI，以及账户可用的模型。r3 记录为 `gpt-6-astra`、`medium`；新研究在配置中明确模型和 effort，初始化后冻结。离线 demo 不需要模型权限。

## 在新目录准备研究

`scripts/build_agentic_dataset.py` 保留了原工作区的默认路径，发布时为保持运行源码身份没有改写。复现须显式提供来源、元数据、排除表和受体路径，不依赖这些默认值。从仓库根目录执行以下命令；`runs/new_agentic_study/data` 必须尚不存在：

```sh
python scripts/build_agentic_dataset.py \
  --source datasets/rsi-lab-docking/extracted/agentic_pilot/upstream/chembl_37_activity_1000.json \
  --source-metadata datasets/rsi-lab-docking/extracted/agentic_pilot/upstream/chembl_37_activity_1000.meta.json \
  --exclusions datasets/rsi-lab-docking/extracted/agentic_pilot/data/identity_exclusions.json \
  --receptor datasets/rsi-lab-docking/extracted/agentic_pilot/data/development_01/receptor \
  --chemical-preparation --out runs/new_agentic_study/data
```

来源是冻结的 ChEMBL 活性响应；原始快照为来源核验而分发，Lab 的 structure-only cohort 不暴露活性数值，也不据此挑选候选。构建器在对接前完成状态准入、历史原始/转换身份排除和四 cohort 的确定性骨架切分。用相同来源重建是复现，不应将这些已公开结果对应的化合物称为全新验证池。

完整运行还需要在新研究根目录建立以下配置和证据，使用原始审计归档中的 `study_config.json` 了解字段结构，不能直接运行其中的旧路径：

1. 将 `harness`、`worker_python`、`vina` 和 `artifacts` 的路径映射到本机真实文件，明确 `model`、`effort`、`intake_max_calls`、`goal` 和四个 cohort 名称。
2. 重新准备 `intake_artifacts/`，其中包括 `capabilities.json`、科学来源、受体/化学准备证据与本机实际能力检查。`guards` 和 `required_before_launch` 要与真实文件哈希及可执行阶段检查一致；不能复制“已经通过”的结论代替检查。
3. 保留新目录中尚未开始的状态；不要复制旧 `study_state.json`、intake 决策、gate、campaign 或结果摘要作为恢复点。Driver 将在 intake 通过后创建 task context、协议与执行计划。

```sh
python scripts/run_agentic_study.py --root runs/new_agentic_study
```

这会产生新的真实模型与科学计算成本，没有全局墙钟上限；各调用和 campaign 预算仍受保护。已完成或中断的旧 study 不能仅凭摘要自动重启。跨机器重新执行产生新身份和新结果，不承诺逐字相同的模型输出或位级相同的科学结果。仅检查已有数据时无须启动此入口。

正式 ≥100 名单要求、固定两种子规则和 Meta gate 见[运行说明](lab-harness-v1.md)。r3 是 20→5 的探索性闭环，未完成正式名单评定。
