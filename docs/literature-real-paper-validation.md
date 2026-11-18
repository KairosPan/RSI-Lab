# Literature 真实论文验收

日期：2026-11-15。论文为 Li et al. (2009), [The Sequence Alignment/Map format and SAMtools](https://doi.org/10.1093/bioinformatics/btp352)，PMID `19505943`，PMCID `PMC2723002`。

本次通过真实 Lab 委派 Literature 子任务，由 Runtime 执行 Europe PMC 和 Poppler 操作，并通过真实 Codex 模型读取来源。未使用模拟检索结果、模拟模型回答或预填图像答案。需要凭证的文献服务保持关闭。

## 验收结果：通过

最终运行：`runs/literature-real-paper-samtools-20261115-v4/`。独立审计 **23 项全部通过**，Literature 的 `complete_research` 和原 Lab 的 `no_action` 均已实际执行。结果不是仅渲染出文件或仅创建待处理的父任务。

| 阶段 | 实际结果 |
| --- | --- |
| 检索 | Europe PMC 标题搜索命中目标，随后核对 DOI、PMID、PMCID |
| 全文阅读 | 9,027 字符 / 9,091 UTF-8 字节 / 36 条原文证据完整交付模型，读至 `next_cursor=null` |
| 看图 | Poppler 定位并渲染 PDF 第 3 页；PNG 与实际模型输入哈希一致；图 1 面板与 `r002` 坐标、CIGAR 核对正确 |
| 后向引用 | 通过真实 cursor 读取两页，共 4 条；跟进 UCSC genome browser (2002)，PMID `12045153`，核对元数据与完整摘要 |
| 前向引用 | 读取首页 3 条；跟进 crimson clover genotyping panel (2026)，PMID `42811592`，核对元数据与完整摘要 |
| 返回 Lab | 原父 Lab 实际收到研究结论及 18 条选定来源，完成限定范围验收 |

最终 Literature 请求为 **323,941 字节 / 56 条来源**；父 Lab 请求为 **168,585 字节 / 18 条来源**。独立复核了全部 17 个模型 Effect 的实际计划，均为完整交付，没有 deferred 或 partial 来源。

本次成功运行包含 **17 次真实模型调用**（Literature 15、Lab 2）、**14 次文献操作**；账本记录 18 个工具调用计量单位，包含操作内部请求。累计模型回执计量为 **1,606,202 tokens**，不是费用估算。这个数是各次调用事件流 token 计数的和，而续接同一原生 thread 的调用，其计数包含此前各轮，所以其中重复计入了早先的轮次。Literature 使用同一 owner 的 12 个原生会话、11 次重建；预算均累计核算，没有重置，全部在本次冻结上限内。完整证据重复投递的开销较高，本配置用于流程验收。

本机证据保存在 `runs/literature-real-paper-samtools-20261115-v4/` 下的 `audit.json`、`summary.json` 与 `report.md`。账本、原始模型对话、论文和图像不随 PR 发布；公开仓库包含复现脚本和此结果概要。该实测使用原源码快照 `ae3dbea`；PR 随后的 `main` 整合只作离线回归，不构成再次真实论文验收。

## 来源与阅读范围

Europe PMC 提供 JATS XML 正文；规范化后为 9,027 个字符、36 条原文证据。验收脚本分别检查工具返回范围和这些原文实际进入模型请求的范围。只有后者完整才算通过全文交付；这不等于保证模型理解了每一句，也不覆盖额外补充文件。

远程 PDF 端点实际返回 HTTP 403。本次备用 PDF 来自 [Harvard DASH 官方仓储](https://dash.harvard.edu/entities/publication/73120378-99d6-6bd4-e053-0100007fdf3b)，由宿主预先下载并登记为 `samtools_repository_pdf`，保留来源 URL、DOI 和 SHA-256：

```text
d5ef9acbe1faca6b44398de03499ebb5083ddf48e32b1651e21207546340f3d3
```

因此，备用 PDF 的自动发现仍未验证。Literature 必须先记录远程失败，再显式调用已登记的本地来源；它没有任意下载 URL 或读取本地路径的权限。

仓储 PDF 有封面，共 3 页。图 1 位于 PDF 第 3 页、印刷页码 2079；通过 Poppler 文本导航定位后渲染，不能把论文印刷页码或 XML 段落位置当作 PDF 页号。图像验收绑定源 PDF 哈希、页码、PNG 字节、派发附件与原生会话 `input_image`。人工复核的具体读数是图 1(b) 中 `r002` 的 `POS=9`、`CIGAR=3S6M1P1I4M`。

引用追踪检查两个方向、真实 cursor 续页，以及从每个方向实际返回的邻接记录中各取一篇核对元数据和摘要。引用边只表示文献引用关系；本次未读取邻接论文全文，也不判定支持、反驳或复现。

## 发现并修正的问题

1. 原 `acquire_fulltext` 默认优先 XML，缺少显式请求 PDF 的接口。本次加入 `preferred_format=xml|pdf`，只选择 provider 已返回的候选；没有匹配格式或响应类型不符时明确返回不可用。
2. 128 KiB 的请求上限无法承载这篇论文的完整原文证据。256 KiB 虽能完成一次全文交付，后续累计证据会触发缩窄，使历史图片和引用只剩目录信息。最终验收使用冻结的 **512 KiB 请求上限、2 MiB 可观测会话文本上限**，每轮派发前要求所有请求来源完整交付；超限就停止。
3. Codex CLI 0.163.0 会把超过 2500 个 32×32 patches 的高精度输入缩放。本次 1411×2000 PNG 实际变为 1311×1859，严格哈希检查正确拒绝。Poppler 长边现限制为 **1600**，附件在启动模型前检查 patch 数；仍保留实际输入哈希核验。[对应官方实现](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/utils/image/src/lib.rs)

输入增长时采用 checkpoint/rebuild；同一 Literature owner、任务、来源和累计预算保持不变，但可能使用多个原生会话。历史内容本身不授予当前引用权限，所需原文和图像必须重新实际交付。

早先受限运行保留在 `runs/literature-real-paper-samtools-20261115{,-v2,-v3}/`：依次暴露完整原文未交付、图像被缩放，以及四阶段已验证但最终汇总无法交回 Lab。未改写旧回执，也未把它们标成完整成功。

## 复现与独立核验

在项目目录、已有 Poppler 与 `.venv` 的环境运行，`--pdf` 指向已取得的官方仓储 PDF。运行使用当前配置的真实 Codex 模型，需要本机模型访问；文献检索本身不需要新凭证。

```sh
PYTHONPATH=. .venv/bin/python scripts/validate_literature_paper.py \
  --root runs/literature-real-paper-new-run \
  --pdf /absolute/path/to/samtools2009-harvard-dash.pdf

.venv/bin/python scripts/audit_literature_validation.py \
  --root runs/literature-real-paper-new-run
```

runner 保存 `progress.jsonl`、`summary.json` 和 `report.md`。独立 auditor 从只读账本快照核对实际来源、交付页、图像输入、双向引用、同父 Lab 的最终接收，以及跨会话预算，输出 `audit.json`；未通过返回非零退出码。运行产物保存在本机、被 Git 忽略。

该阶段回归检查：文献测试 **179 项**、核心 v2 测试 **273 项**通过。
