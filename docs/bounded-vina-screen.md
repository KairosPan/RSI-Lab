# 限时本地 Vina 探索性筛选

`scripts/run_vina_screen.py` 可在冻结的候选表、Vina profile 和绝对截止时间下执行中等规模的本地筛选。它直接运行真实 Vina worker；这条入口没有 Lab 模型决策、Meta 修改或正式筛选资格判定。

运行目录必须先准备 `data/candidates.json`、`data/manifest.json`、受体文件、`frozen/` 中的 worker/adapter/contract，以及通过 `run_vina_batch.py freeze-protocol` 生成的 `protocol.json`。`plan.json` 绑定 protocol 与 driver 的 SHA256，并明确 `candidate_count`、`seeds`、`max_seed_jobs`、`max_external_processes` 和带时区的 `compute_deadline_utc`。当前支持最多 4 个并行 worker，每个 Vina 使用冻结 profile 的 2 个 CPU 线程。候选来源与准备条件需要在派发前审查、留档。

```sh
python scripts/run_vina_screen.py --run RUN_ROOT --vina /absolute/path/to/vina
python scripts/report_vina_screen.py --run RUN_ROOT --out RUN_ROOT/report_final
```

科学 Python 环境需要与协议中记录的版本一致。运行器拒绝已启动的目录，不隐式恢复、重试或覆盖作业。到达绝对截止时间后，它终止自己创建的进程组，保留未完成及未执行状态；未知 CPU 成本和缺失分数不补零。每个 compound 的两个种子均成功、身份保持且通过冻结姿态检查，才能得到用于排序的能量中位数。

报告器核对冻结输入和源码、原始 MODEL 1 分数、姿态文件与检查表、父进程回执和成本汇总，再导出 `ranking.csv`、`all_results.csv`、`report.json`、`REPORT.md`。输出目录必须是新目录。排序表保留所有有效结果，完整结果表保留所有输入及失败。

该入口的 `threshold` 为空，不调用正式 `select-final` gate。输入数量或有效姿态数量不能替代任务冻结的阈值与最低达标数量要求。输入质子化状态、受体缺失结构、晶体水处理及实验条件尚有缺口时，只能在明确记录的探索性范围内解释结果。

截止、中断与回执核验测试：

```sh
python -m unittest discover -s tests -p 'test_vina_screen*.py' -v
```
