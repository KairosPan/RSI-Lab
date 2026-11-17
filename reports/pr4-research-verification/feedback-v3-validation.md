# Feedback v3 离线验收 — 2026-11-13

本次修改和完整边界说明见 [FEEDBACK_V3.md](../../experiments/pablo/FEEDBACK_V3.md)。未准备或启动新实验，未调用真实模型或联网检索。新增全链路回归使用合成模型回答、明确标记的合成文献和评分桩；原有部分领域回归继续使用它们的本地评分测试样例。

## 最终验证

| 检查 | 结果 |
| --- | --- |
| 通用内核全量：`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests` | **639/639 通过**，Python 3.14，43.026 秒 |
| PABLO 全量：`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=experiments/pablo:. /Users/pan/Documents/ChatGPT/RSI-PABLO/pablo-meta-experiment/.venv/bin/python -m unittest discover -s experiments/pablo/tests -t .` | **145/145 通过**，Python 3.11，19.140 秒 |
| 派发、恢复、编排及逐批解释相关回归 | 97/97 通过，包含于内核全量 |
| JSON 契约边界回归 | Python 3.14 与 PABLO Python 3.11 均 18/18 通过；3.14 结果包含于内核全量 |
| `git diff --check` | 通过 |
| 独立只读接线审查 | 未发现阻断：新旧配置、冻结源码、真实执行器 schema、Meta 方法契约、实际选点与评分绑定、逐批解释与派发检查 |

关键新增测试文件：

- [内核逐批解释及真实交错](../../tests/v2/test_batch_interpretations.py)：16 项。包括旧 A 已执行未发布 → B 完成解释 → C 已决策并 prepare → 派发时恢复发布 A；C 的执行器不被调用、按已知零成本结算并释放预留，A 的解释仍可运行。
- [Scientific Memory 检索](../../tests/v2/test_scientific_memory.py)：保留相关历史经验和负结果，完整来源及强制对象不越过字节预算。
- [数值方法](../../experiments/pablo/tests/test_methods.py)：14 项，真实选点计算、确定性、来源、状态、精确计划、伪造结果和排队候选检查。
- [Lab Harness](../../experiments/pablo/tests/test_harness.py)：16 项，原节奏、停滞与重置、结构约束、继承轨迹及实际评分信用。
- [宿主全链路](../../experiments/pablo/tests/test_harness_runtime.py)：4 项，真实 Runtime 的选点 → 评分 → 解释 → challenge 及拒绝路径。
- [v3 启动与收尾](../../experiments/pablo/tests/test_host_v3_preflight.py)：6 项，缺能力/钩子/配置拒绝、实际零工具解释 schema、评分达标仍不能绕过解释。

最终全量验证前修正了旧派发故障注入 fixture 对新增关键字参数的兼容性；解释屏障使用专门异常，普通派发拒绝仍保留原取消/恢复流程。没有通过放宽断言绕过故障。

## 历史实验保持原样

以下三个账本 SHA-256 与修复前相同；只直接读取文件计算 hash，没有以可写 Store 打开它们。

| 运行目录 | `ledger.sqlite3` SHA-256 |
| --- | --- |
| `pablo_400_research_sol_high_pr4_20261112` | `8ef0690a1187453ab15048a03371cdef4fc70e3508b28baa62fe459c2c59bb49` |
| `pablo_400_checkpoint200_feedback_v2_high_20261113` | `a26bc068fa746454d36781fa69abc8da98bb7fe05b128195809dc9d394c30fe7` |
| `pablo_400_complete_feedback_v2_high_20261113` | `3b6af602ed3a6bc4494920859cf631209c10533769d4a18a4cac4a94939be8fe` |

`pablo_task/oracle.py` 也仍与原导入版本字节一致：`f5327b4e5aaa4cd48525617fbb09b729cecd332044f776cf401fe7fee766074e`。

这些结果证明机械执行约束、数据交付和恢复路径通过回归，不证明最佳分数将提高。实际收益尚未测量。
