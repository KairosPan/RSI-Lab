# 文献工具精简与接通状态

更新：2026-11-15。本页记录本轮真实检查及当前配置；安装成功、接口可达和完成研究任务分别计量。

## 当前范围与启用策略

- 已移除 ToolUniverse、LLM-NERRE、LLM_organic_synthesis、OpenScholar、PaperPilot、MinerU 的注册、执行分支和专属依赖声明。ToolUniverse 已从本项目主虚拟环境卸载；MinerU 的专用 `.tools/mineru-venv` 已删除；保留其他工具可能共用的依赖。
- 保留 **9 个主线**：Europe PMC、PubMed/PMC、OpenAlex、Asta、Unpaywall、Paper2Skill、GROBID、LangExtract、Poppler。
- 保留 **6 个可选项**：Semantic Scholar、Scite、Starling、PaperQA、Elicit、Edison。
- 按用户要求，需凭证或模型访问的项目保持关闭。未部署的 GROBID、缺邮箱的 Unpaywall 也保持关闭；配置值存在不会自动启用工具。

[`literature-tools.json`](../examples/literature-tools.json) 启用六项主线：Europe PMC、PubMed/PMC、OpenAlex、Asta、Paper2Skill、Poppler。[`literature-tools-optional.json`](../examples/literature-tools-optional.json) 使用同样的启用列表，另保留可选项配置模板。其余项的配置模板保留，需宿主显式修改 `enabled_providers` 才能调用。

两份配置均注册 11 个可调用操作（总定义 13 个）；`extract_evidence`、`run_review` 没有已启用的 provider，因此不会出现在 agent 的工具目录中。对应 provider 启用后才注册这些操作。关闭项仍出现在 `literature-tools` 状态查询中。

## 六个可选项的实际结果

| 工具 | 本轮实际检查 | 当前结果与下一项条件 | 是否启用 |
| --- | --- | --- | --- |
| **Semantic Scholar** | 普通搜索与元数据接口返回 429；新接的显式标题匹配经真实 ProviderClient 返回 200、论文 ID/DOI/摘要/全文候选；之后 Runtime 复测再次返回 429 | **部分能力曾成功，但匿名访问仍受限**。未将标题匹配当作关键词搜索的自动回退；可配置自己的 API key/额度后再验 | 关闭 |
| **Scite** | OAuth 元数据返回 200，项目 MCP 匿名初始化返回 401；桌面 connector 的只读订阅检查成功 | **项目运行时未授权**。需独立项目 OAuth 与 `SCITE_ACCESS_TOKEN`；桌面认证未导出，也不视为运行时凭证 | 关闭 |
| **Elicit** | 官方只读 `/api/v2/usage` 无 key 请求返回 401；adapter 缺凭证预检返回 unavailable、零派发 | 需 `ELICIT_API_KEY` 及对应 API 权限；未创建报告任务 | 关闭 |
| **Edison** | 已安装 SDK 0.17.0；官方只读任务状态接口无 key 返回 401；adapter 缺凭证预检返回 unavailable、零派发 | 需 `EDISON_API_KEY`；未提交研究任务 | 关闭 |
| **PaperQA** | SDK 2026.8.12 可导入，`Docs.aadd/aquery` 的真实异步 API 与适配器一致；检查了宿主配置和常见模型环境变量是否存在 | 缺明确的回答/摘要模型、embedding 和模型访问；未发现可用本地 LLM 服务，未运行真实问答 | 关闭 |
| **Starling** | 官方代码及 Python 3.13 隔离环境已安装，补齐 `sqlalchemy[asyncio]` 的 greenlet 依赖，真实 `starling --help` 通过；核对了后端占位实现和提取入口 | 缺真实语料检索/实体归一化后端、模型及访问配置；固定上游版本的提取循环前还存在 `exit()`，因此尚不能用于真实研究 | 关闭 |

本轮没有提交付费研究任务、创建账号、发起 OAuth 同意或写入凭证值。SDK/CLI 安装与帮助检查不等于模型推理已验证。

## 实现修正与可复现边界

Semantic Scholar 标题匹配使用现有 `literature.search_papers`，显式输入：

```json
{
  "provider": "semantic_scholar",
  "query": "Attention Is All You Need",
  "filters": {"match": "title"},
  "limit": 1
}
```

这是单个最佳标题匹配；不保证标题完全相等或覆盖完整语料。该模式不接受额外过滤器或分页 cursor。未设置 `match` 时仍使用原关键词相关性搜索。当前配置保持该 provider 关闭，因此上述输入只有在宿主明确启用后才会通过 admission。

Poppler 替代 MinerU 作为默认本地 PDF 基础后端，使用 `pdftotext` 提取文本、`pdfinfo` 检查页数、`pdftoppm` 渲染单页。默认从 PATH 找命令，也可在宿主配置中填写三个 `*_path`；不需要模型或凭证。模型通过 `render_document_page` 请求页面图像，图像沿审计和 packet 路径作为真实视觉输入交付；文本提取、图像生成、实际交付及科学理解分别记录。接入采用 PDF skill 的阅读流程，不开放全局 shell。

Poppler 26.04.0 已完成真实解析、渲染和模型交付验收。原生文字 PDF 保留页码/行词坐标；混合文本与纯图形的 PDF 将无文字页标为未读，仍能按页渲染。macOS 未嵌入字体的替换问题通过每次进程独立 Fontconfig 配置处理，没有修改系统设置；临时 PDF 与字体缓存会清理。

端到端验收使用两页合成 PDF，第 2 页仅有栅格随机编号和红绿柱状图。Literature 的两个真实 Codex 调用分别主动请求页面、读取附件并完成研究：编号与柱高关系均正确，编号没有出现在提取文本或提示词中。PNG 的 CLI 参数、文件、回执与原生会话 `input_image` 哈希一致；结果返回原 Lab，父任务的下一份 packet 也带有相同图片。此结果不代表通用 OCR、复杂表格或公式准确率已经验证。

多轮模型验收采用 [`literature-reading-run.json`](../examples/literature-reading-run.json) 的会话容量设置：单次文本 128 KiB、会话文本 512 KiB、单次图片合计 4 MiB、历史图片合计 16 MiB。检查过程中修正了当前 Codex 新增审计帧的识别；原有 128 KiB 总会话上限曾在工具往返后明确阻止派发，旧失败/受限回执继续保留，没有重发或改写旧 Effect。

验证：文献测试 169 项、核心 v2 回归 271 项通过。额外运行 generic 测试时，既有 docking 独立几何检查因此环境没有 RDKit 而报错；本次未安装或修改该领域依赖。

Starling 检查固定在 commit `123ba92699146fdfe2ae3569078c7f78a5d171d3`，其 `src/starling/cli/extraction.py:395` 在提取任务循环前调用 `exit()`。本项目没有修改上游。适配器已修正官方要求的 PMID JSON 数组和必需 spec 文件；空提取产物会明确失败并保留执行未知状态，不能当作成功研究结果。

主线 SDK/PDF 依赖见 [`requirements-literature.txt`](../requirements-literature.txt)，PaperQA/Edison 的可选依赖见 [`requirements-literature-optional.txt`](../requirements-literature-optional.txt)。旧运行的 adapter 描述符仍按原版本核验，新配置使用新 run。

开发环境的原始回执保存在被 Git 忽略的 `runs/literature-connectivity/`：`services.json`、`mineru-connectivity-summary.json`、`projects-summary.json`、`runtime-summary.json`。这些历史回执仍明确记录标题匹配复测的 429 及已移除解析器的原有检查；不把早先成功响应覆盖为持续可用承诺。

Poppler 新回执：`poppler-native-v1/summary.json` 记录原生解析/视觉检查；`poppler-integration-summary.json` 记录真实模型调用与父 Lab 交接；完整成功运行位于 `poppler-live/run-485f8edd/`。运行产物不随源码发布。

## 真实论文完整流程验收

已用 SAMtools 2009 论文完成真实 Lab → Literature → Lab 流程：检索、9,027 字符规范化全文实际交付、PDF 第 3 页图像输入与读数核对、双向引用及邻接摘要跟进。独立审计 23 项全部通过；本次回归为文献 179 项、核心 v2 273 项。具体来源、运行数据和复现命令见[真实论文验收报告](literature-real-paper-validation.md)。

远程 PDF 返回 403，本次使用宿主预先取得并登记的 Harvard 官方仓储文件，尚未验证自动备用 PDF 发现。真实论文验收采用 512 KiB 单次请求、2 MiB 可观测会话文本，并要求当前轮完整重投递已有来源；上文 128 KiB/512 KiB 配置仍是较小的合成材料验收记录。Poppler 渲染上限已设为 1600，附件派发前检查 Codex 的高精度 patch 上限，保持实际输入哈希严格验证。
