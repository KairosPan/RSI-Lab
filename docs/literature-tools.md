# Literature 工具使用说明

更新：2026-11-15。文献工具通过 RSI-Lab 原有 Adapter、Work/Effect、预算和恢复路径执行。Lab 可以委托独立 Literature 子 agent，也可以直接调用已注册能力。本文记录当前可执行契约；后续对象与命令设计见 [Literature 设计](literature-agent-design.md)。

当前有 **13 个 `literature.*` 能力定义、15 个 provider 条目：9 个主线、6 个可选**。默认配置启用 Europe PMC、PubMed/PMC、OpenAlex、Asta、Paper2Skill 和 Poppler 六项，其余项目保持可配置但关闭。provider 条目可能是原生 HTTP、MCP、可选 SDK、解析 CLI，或需要宿主部署的研究项目桥。注册成功、包已安装、配置检查通过、真实请求成功分别是不同状态。

## 1. 注册与直接调用

以下命令从仓库根目录执行。原生检索接口和通用控制器使用 Python 标准库；解析 PDF 或调用可选 SDK 时使用安装了相应依赖的解释器。示例宿主配置在 [`examples/literature-tools.json`](../examples/literature-tools.json)，可选依赖版本在 [`requirements-literature.txt`](../requirements-literature.txt)。

需要可选 SDK 时，可用已安装的 Python 3.11 创建主环境：

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements-literature.txt
```

Poppler 是默认启用的本地 PDF 基础后端，需要单独安装系统命令，不能通过 pip requirements 获得。macOS 使用 `brew install poppler`，Debian/Ubuntu 使用 `sudo apt-get install poppler-utils`；安装后应能解析 `pdftotext`、`pdfinfo`、`pdftoppm`。Poppler 本身不调用模型，也不要求 API key；原生扫描页可以渲染，但没有 OCR 文本层时不声称已提取正文。项目与安装来源见 [Poppler 官方网站](https://poppler.freedesktop.org/)。

Paper2Skill 使用独立环境，以下复现本次验证的上游版本与依赖；仓库本身不分发 `.tools/` 下的软件：

```sh
mkdir -p .tools
git clone https://github.com/jmiao24/Paper2Agent.git .tools/Paper2Agent
git -C .tools/Paper2Agent checkout 8c2d059165ef8cdcb70dbea76655b9c2b55b38e6
python3.11 -m venv .tools/paper2skill-venv
.tools/paper2skill-venv/bin/python -m pip install \
  pymupdf==1.28.2 pymupdf4llm==1.28.2 pypdf==6.18.1 pillow==12.2.0
```

需要 PaperQA/Edison 时另外安装 `requirements-literature-optional.txt`。下方 CLI 示例统一使用 `.venv/bin/python`，使宿主进程中的 PDF reader 与已安装 SDK 能使用同一环境；只用原生 HTTP 时可改用系统 Python。安装依赖不代表已经配置模型或开通服务；示例配置不提供模型 ID，由宿主明确选择。

先查看配置需求；该命令不调用文献服务，也不运行模型：

```sh
.venv/bin/python -m rsi_lab --root runs/literature-demo --run literature \
  --literature-config examples/literature-tools.json literature-tools
```

`--literature-config` 本身会注册当前启用 provider 能执行的文献能力；也可以只传 `--literature-tools` 使用默认宿主配置。`literature-tools` 子命令返回 15 个 provider 的本地配置状态及实际注册的能力 ID。状态为 `configured/needs_configuration/disabled`；未列入 `enabled_providers` 的条目会标记 `enabled=false`。`configured` 仅表示本地检查通过，`live_verified` 不会因配置通过而自动变成 `true`。

新版适配器的 manifest 显式声明 `literature_decision_policy: lab_literature_decision_v1`，启用 Lab 的自主委托上下文和简短决策记录。旧版 `rsi_domains.literature` 检索接口保留原契约；仅注册该旧接口不会增加决策记录要求。

默认使用 [`literature-tools.json`](../examples/literature-tools.json)，启用 Europe PMC、PubMed/PMC、OpenAlex、Asta、Paper2Skill、Poppler。主线分类另保留 Unpaywall、GROBID、LangExtract，但因邮箱、服务端点或模型未配置而保持关闭。所有关闭项仍可在目录中查看，模型不能调用。

[`literature-tools-optional.json`](../examples/literature-tools-optional.json) 保留六个可选项目的配置模板，启用项与主线配置相同；可选项目均保持关闭。Semantic Scholar 的标题匹配曾成功，随后仍遭遇 429，因此也不默认启用。凭证或模型配置不会自动启用 provider，须宿主显式编辑 `enabled_providers`。没有可用 provider 的能力不进入工具目录，当前两份配置均注册 11 个能力；启用相应 provider 后才出现 `extract_evidence/run_review`。

```sh
.venv/bin/python -m pip install -r requirements-literature-optional.txt
.venv/bin/python -m rsi_lab --root runs/literature-optional --run optional \
  --literature-config examples/literature-tools-optional.json literature-tools
```

本次已移除 ToolUniverse、LLM-NERRE、LLM_organic_synthesis、OpenScholar、PaperPilot、MinerU 的注册、执行分支和专属依赖声明；旧配置中的这些 provider 会被明确拒绝。已运行的旧任务仍保留原始回执。新配置或代码更改后使用新的 run，不绕过旧 Effect 的版本核验。

准备一个只运行工具的配置和查询文件：

```sh
mkdir -p runs/literature-demo-inputs
cat > runs/literature-demo-inputs/run.json <<'JSON'
{
  "goal": "检索文献并保存可复核来源",
  "max_external": 1,
  "max_input_bytes": 131072,
  "budgets": {
    "tool_calls": {"limit": 40, "hard": true}
  }
}
JSON
cat > runs/literature-demo-inputs/search.json <<'JSON'
{"provider":"europe_pmc","query":"single cell RNA sequencing","limit":3}
JSON

.venv/bin/python -m rsi_lab --root runs/literature-demo --run literature \
  --literature-config examples/literature-tools.json \
  init --config runs/literature-demo-inputs/run.json

.venv/bin/python -m rsi_lab --root runs/literature-demo --run literature \
  --literature-config examples/literature-tools.json \
  adapter-run --adapter literature.search_papers \
  --inputs runs/literature-demo-inputs/search.json --event first-search
```

`init` 使用新的 run 身份；同一次工具操作保留原 `--event`，新的操作使用新的 event。`adapter-run` 产生真实 Effect、费用记录和来源事实。它不会自动运行 Meta 或 Lab 模型；后续模型工作可以保留在就绪队列。

得到论文标识后，调用 `literature.acquire_fulltext`，输入为 `{"provider":"europe_pmc","paper_ref":"返回的论文标识"}`。取得 `document_id` 后，用 `literature.read_document` 的 `{"document_id":"返回的document_id","max_bytes":8000}` 读取。将这些对象分别写入输入 JSON，再使用相同 `adapter-run` 格式。继续阅读时原样携带返回的 `cursor`；需要固定版本时同时填写 `document_version`。

同一论文需要分别读取 XML 正文和查看 PDF 时，可在远程 `acquire_fulltext` 中显式填写 `preferred_format: "xml"` 或 `"pdf"`。只选择该 provider 返回的匹配候选；没有对应候选或下载失败会明确返回 unavailable，不把 HTML 页面当作 PDF。PDF 响应须有正确的媒体类型和 `%PDF-` 文件签名。省略该参数仍优先 XML；本地 `source_id` 不接受此参数。

已有本地文件通过宿主配置的 `local_sources` 注册，例如 `{"local_sources":{"my-paper":{"path":"/absolute/path/paper.pdf","content_scope":"unknown"}}}`。模型仅提交 `{"source_id":"my-paper"}`，不能提交本地路径。注册时冻结文件 hash；文件改变后需要显式注册新版本。`acquire_fulltext` 返回文档句柄和阅读索引，不隐式把整篇正文交付给 agent。

宿主登记公开仓储备用 PDF 时，可额外填写 `source_url`、`paper_ref` 和 `license`；这些字段随原件归档以保留来源，provider 仍标为 local，不把宿主下载算成 agent 自动发现。

PDF 阅读采用 PDF skill 的“文本定位与页面视觉复核”流程：

1. `acquire_fulltext` 取得原始 PDF 的 `document_id`，再用 `build_reading_package` 的 `{"provider":"poppler","document_id":"原始文档ID"}` 提取带页码文本。
2. 对派生文档调用 `find_passages/read_document`，确定相关段落与页码；双栏、公式、表格或图像内容需回到页面核对。
3. 调用 `render_document_page`，例如 `{"provider":"poppler","document_id":"原始或派生文档ID","page":2}`。页码从 1 开始，每次只渲染一页；运行时将该事实绑定的 PNG 作为真实图像输入交给模型，并记录交付身份。
4. 图像与对应来源实际进入当前模型轮次后，再核对图例、坐标轴、单位、表格关系及正文上下文。分辨率不足或页数未覆盖时继续显式取页；未交付图像的目录指针不能作为已读图表的依据。

这条链路使用受控文献能力。模型不能选择可执行文件、上传任意本地路径或获得全局 shell；PDF skill 是阅读流程，不是开放系统命令的授权。具体端到端测试范围以[接通状态](literature-connectivity.md)为准。

检查与恢复也带上相同文献配置：

```sh
.venv/bin/python -m rsi_lab --root runs/literature-demo --run literature \
  --literature-config examples/literature-tools.json status
.venv/bin/python -m rsi_lab --root runs/literature-demo --run literature \
  --literature-config examples/literature-tools.json recover
```

恢复会核对 adapter 版本、实现描述符和原回执。已成功但未发布的结果补发布，不重新执行 provider。未知执行保留对账责任；不能通过更换 event 来把它当成未执行。恢复时缺少原 adapter 或配置/代码不匹配会明确阻塞。

## 2. 13 个能力定义（按启用 provider 注册）

全部版本为 `1.0.0`；无可用 provider 的操作不会向 agent 注册。实际 `manifest.input_schema` 与 `prepare` 检查决定可接受字段；下面只列主要契约。

| 能力 ID | 主要输入 | 输出及边界 |
| --- | --- | --- |
| `literature.search_papers` | `query`，可选 `provider/filters/cursor/limit` | 候选元数据、摘要、来源及分页；命中不等于已读全文 |
| `literature.get_paper` | `paper_ref`，可选 `provider` | 论文标识与元数据、可用来源入口 |
| `literature.acquire_fulltext` | `paper_ref` 或宿主登记的 `source_id`，二者选一；远程可选 `preferred_format=xml/pdf` | 归档原件、`document_id/version`、访问/解析状态；指定格式不静默回退 |
| `literature.read_document` | `document_id`，可选 `document_version/locator/cursor/max_bytes` | 实际原文段落、范围、来源 hash、下一页 |
| `literature.find_passages` | `document_ids/query`，可选 `limit/max_bytes` | 本地规范文本的词项匹配与定位；不是全语义或穷尽性综述 |
| `literature.get_citations` | `paper_ref/direction`，可选 `provider/cursor/limit` | `direction` 仅为 `references` 或 `citing`；后者映射到 provider 的施引查询 |
| `literature.build_reading_package` | `document_id`，可选 `provider` | 默认本地阅读索引；`provider=poppler` 提取 PDF 文本并保留页码，其他解析器产生各自派生文档；需要再读原文范围 |
| `literature.render_document_page` | `provider=poppler/document_id/page` | Poppler 渲染单个 PDF 页面，返回绑定原件 hash 与页码的图像；图像经 packet 实际交付后才计为视觉材料已提供 |
| `literature.extract_evidence` | `provider/document_ids/prompt`，可选 `examples/parameters/cursors` | 已配置抽取器的解释性结果及来源关联；未定位项不伪造 offset |
| `literature.run_review` | `provider`，按服务使用 `query/document_ids/action/task_id/parameters/cursors` | 托管任务的 start/status/result 或本地研究任务；不同 provider 不是通用可互换协议 |
| `literature.list_providers` | `{}` | 15 项接入方式、配置缺项与本地检查状态 |
| `literature.provider_tools` | `provider`，MCP 可选 `cursor` | Asta/Scite 的允许工具 schema |
| `literature.call_provider_tool` | `provider/tool/arguments` | 调用宿主允许的 MCP 文献工具，参数以实际发现的 schema 为准 |

外层分页 `limit` 为 1–25，`read_document/find_passages` 的 `max_bytes` 为 4–65,536；SDK 文档输入还有自己的更小数量及总字节限制。默认 agent packet 上限为 **131,072 bytes（128 KiB）**，覆盖 schema、任务、目录和证据。持久会话的可观察历史另检查 `max_effective_context_bytes`；宿主应按实际模型容量独立配置它，默认继承 `max_input_bytes` 会较快阻塞续轮。必要证据放不下时返回容量阻塞，不以静默丢弃来源换取成功。

## 3. provider：接入与当前验证范围

以下实测摘要来自 2026-11-15 开发环境的小样检查，不承诺服务以后仍可访问，也不代表上游全部功能均已接入。

| provider | 当前接入 | 宿主配置/运行条件 | 本次验证及边界 |
| --- | --- | --- | --- |
| `europe_pmc` | 原生检索、元数据、全文候选、双向引用 | 标准库 HTTP | 真实检索、JATS 全文获取、连续两页原文/定位、片段检索和双向引用分页通过；全文覆盖仍依具体论文 |
| `pubmed` | PubMed/PMC 原生接口 | 可选 `NCBI_API_KEY`、`NCBI_EMAIL` | 真实元数据请求通过；不把 PubMed 摘要当 PMC 正文 |
| `openalex` | 原生元数据、引用与全文入口 | 部分匿名访问；延续分页/内容可能需要 `OPENALEX_API_KEY` | 真实元数据请求通过；不是全部内容权限验收 |
| `semantic_scholar` | 原生检索、元数据、引用与全文入口 | 可选 `SEMANTIC_SCHOLAR_API_KEY` | 普通搜索和元数据请求 429；新增显式标题匹配曾返回真实论文，Runtime 复测仍 429，目前关闭 |
| `unpaywall` | DOI解析与开放获取位置 | `UNPAYWALL_EMAIL` | 契约测试；没有宣称真实全文获取已通过 |
| `asta` | 允许列表内的 HTTP MCP | 默认官方 endpoint；按访问条件使用 `ASTA_TOOL_KEY` | 真实 `tools/list` 返回 4 个可用 schema，`get_paper` 调用通过；没有据此声称全文或全部工具均通过 |
| `scite` | 允许列表内的 HTTP MCP | `SCITE_ACCESS_TOKEN` 或相应宿主认证配置 | 项目 OAuth metadata 可达，匿名 MCP 请求 401；桌面连接可用但不共享凭证，目前关闭 |
| `grobid` | 原生解析服务 | 已部署 `endpoint` | 解析契约/模拟响应测试；需要可用GROBID服务 |
| `poppler` | 本地 PDF 文本提取与单页 PNG 渲染 | `pdftotext/pdfinfo/pdftoppm` 在 PATH，或三个宿主路径配置；无需模型或凭证 | 默认启用；读取范围、渲染与模型交付分别记录，真实验收见[接通状态](literature-connectivity.md)；不提供 OCR |
| `paper2skill` | Paper2Agent 的 Paper2Skill prepare/extract 阅读管线 | `repository`、独立 `python` | 真实两页PDF prepare/extract通过；接入阅读材料构建，不包含Paper2MCP代码工具生成 |
| `starling` | 原生CLI或明确部署协议 | `command/model/backend`，实际语料后端 | Python 3.13 环境已安装且 CLI help 通过；缺真实后端/模型，且固定上游提取路径有提前 exit()；保持关闭 |
| `langextract` | 隔离SDK抽取 | 已安装 `langextract`，显式 `model`，按模型要求提供凭证 | SDK契约测试；无实际付费模型抽取；精确span匹配与未定位状态分开 |
| `elicit` | 原生搜索与report任务生命周期 | `ELICIT_API_KEY` | 无 key 的官方 usage 请求 401；未提交报告任务，目前关闭 |
| `edison` | Edison Literature/PaperQA3 托管生命周期 | `edison-client`、`EDISON_API_KEY` | SDK 已装，官方只读 task status 请求 401；未提交研究任务，目前关闭 |
| `paperqa` | PaperQA/PaperQA2 本地语料SDK | `paper-qa`、显式 `model/embedding` | SDK/API 核对通过；无 model/embedding 或可用模型访问，保持关闭 |

开发环境已安装用于SDK接口核对的可选依赖；具体版本以 requirements 文件为准。工具连接器示例不填写额外抽取/综述模型ID，相关 provider 保持关闭。阅读运行示例单独指定宿主 Codex 模型；页面理解使用该模型的图像输入能力和已有登录，Poppler 本身无需模型或凭证。Paper2Skill真实检查使用的上游commit为 `8c2d059165ef8cdcb70dbea76655b9c2b55b38e6`，独立Python 3.11环境使用 `pymupdf==1.28.2`、`pymupdf4llm==1.28.2`、`pypdf==6.18.1`、`pillow==12.2.0`；它与主SDK环境分别管理。配置中的 `.tools/` 路径是宿主安装位置，不表示这些上游软件已随仓库分发。

Europe PMC 的检查经过真实 Runtime 与 ledger：全文获取包含元数据和 JATS 下载两次 HTTP，随后只交付两个连续的 1,600-byte 文本页及其 locator；双向引用均返回带 cursor 的 `partial`。这些检查验证了获取、分页、归档和调用结算，没有把取得全文写成已读完整篇正文，也没有运行付费模型综述或抽取。

## 4. 宿主配置与部署协议

多轮阅读建议采用 [`literature-reading-run.json`](../examples/literature-reading-run.json) 初始化新 run：每次文本请求上限 128 KiB，持久会话文本历史与新请求合计上限 512 KiB；每次最多 4 张图片、合计 4 MiB，历史与当前图片合计上限 16 MiB。图片字节不会作为 base64 文本塞进提示词，也不会从容量统计中消失。现有全局默认和旧 run 的预算保持不变；仅使用原来的 128 KiB 总会话上限时，实际 Codex 历史可能在一次工具往返后触及容量限制。图片 token 数不从字节数虚构推算。

上述配置适合少量片段与页面。完整论文加引用追踪的[真实验收脚本](../scripts/validate_literature_paper.py)采用单次文本 512 KiB、会话文本 2 MiB，并在每轮及会话重建后检查来源完整交付，避免汇总时只剩历史目录。脚本自动执行独立交付审计；已验证的容量、调用计量边界与 PDF 备用来源见[真实论文验收报告](literature-real-paper-validation.md)。

宿主配置决定允许来源、解释器、服务端点、已部署命令、模型及本地来源。模型仅选择已登记能力、文档 ID 和受控参数。原生 HTTP provider 只读取表中标准环境变量名，不接受 `credential_env` 别名。MCP/托管桥可用环境变量名配置，例如 `credential_env: "EDISON_API_KEY"`；SDK worker 的 `env_names` 只指定要转交的宿主变量名，不保证这些变量存在，也不把值写入配置。不要把 API key、token 或 Authorization 值放入 JSON。

原生解析、SDK和项目运行使用显式解释器/argv，不使用模型提供的shell命令。SDK worker与项目进程有时间和输出限制，工作目录隔离；这不是操作系统级沙箱。已有虚拟环境解释器保留其调用路径，不解引用成基础Python。

Poppler 的 `connectors.poppler` 可设置 `pdftotext_path/pdfinfo_path/pdftoppm_path`；省略时从宿主 PATH 查找同名命令。`fontconfig_file` 可选，指向宿主 Fontconfig 配置文件；这些路径不由模型输入。默认 `timeout=120` 秒、`max_pages=200`、`max_image_bytes=524288`、`max_dimension=1600`；上限分别为 1800 秒、1000 页、1048576 bytes、1600 pixels。`render_document_page` 按宿主 `max_dimension` 等比渲染，不接受分辨率参数；尺寸和字节限制同时生效，超限明确失败，不交付截断图像。每个 Poppler 操作最多执行两个受控子进程，不运行模型、不联网取模型。

1600 pixels 的渲染上限用于保留交付图片的原始字节：[Codex CLI 0.163.0 的 HIGH_DETAIL 处理](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/utils/image/src/lib.rs#L73)会缩放超过 2500 个 32×32 patches 的图片。附件准入在派发前检查 `ceil(width/32) × ceil(height/32) <= 2500`；1600×1600 正好符合，1601×1601 和 1411×2000 会被拒绝，并提示重新渲染。通用 PNG 归档仍允许长边至 2000，1900×1000 这类符合 patch 限制的图片也仍可交付。运行时继续核对 native 输入与原图片的精确 hash，不把缩放或重编码后的图片当作同一份证据；后续 CLI 版本若改变处理规则，校验仍会明确失败。

Starling 可选择 `native` 上游 CLI 或宿主自有的 `json_file` 部署协议。后者的 `command` 必须显式包含 `{input_file}`、`{output_file}`、`{model}` 占位符，输入为登记文档及来源绑定，输出为 JSON。

Starling 的 `settings.backend` 是宿主声明与检查项，不作为上游 CLI 参数转交。真实语料后端必须由 `command/args` 或部署本身绑定；填一个 backend 字符串不会激活公开的 stub 实现。

PaperQA当前SDK使用同一配置下的 `Docs.aadd/aquery`，显式固定回答/摘要模型和embedding，禁用自动元数据扩展及多模态默认模型。LangExtract的examples为 `{text, extractions:[{extraction_class, extraction_text, attributes}]}`；未返回合法且匹配源文本的 `char_interval` 时标为 `unlocated`。Asta/Scite 先用 `provider_tools` 读取允许列表内的 schema，再通过 `call_provider_tool` 调用。

托管服务的 `run_review` 使用 `action=start/status/result`；`task_id` 对Elicit映射到其session ID，对Edison保留task ID。只有显式的新工具调用会推进状态，不在一个adapter内部无限轮询。任务提交回执未知时，Effect为 `unknown`，不能自动再提交一份任务。

## 5. Lab / Literature 运行协议

Literature是Lab的子agent，拥有独立session、工作记忆及多步循环。父Lab身份、branch、返回目标与子agent实例由真实委托Effect绑定，模型不能指定或改写。Meta可在既有权限内直接读取原文和执行回执、调用共享底层工具进行机制审计；它不成为该子任务的第二个owner。显式的Meta→Lab补查需求命令仍属于后续设计。

当前Lab新增命令 `request_literature` 的payload必须恰好包含：

```json
{
  "question": "比较当前方法的文献依据与反证",
  "intended_use": "支持Lab选择后续验证方案",
  "scope": "任务指定领域与纳入范围",
  "deliverables": ["有原文依据的比较与未解决问题"],
  "capability_ids": ["literature.search_papers", "literature.get_paper", "literature.acquire_fulltext", "literature.read_document"],
  "max_model_calls": 6,
  "max_tool_calls": 12
}
```

以上只是payload示例，不能直接当作完整命令提交。完整命令还有 `mode/action/issue_id/expected_versions/evidence_ids/payload`；Literature命令额外携带当前 `task_id/task_revision`，持久响应另有 `working_memory_delta`。

Literature当前仅有三个动作：

| 动作 | 行为 |
| --- | --- |
| `request_diagnostic` | payload为 `capability_id/capability_version/inputs_json`；只允许此任务固定版本的 `literature.*` 能力；结果回同一子agent |
| `publish_research_update` | 向父Lab交付阶段结果与原文来源，并安排子agent继续研究 |
| `complete_research` | 向父Lab交付结果并完成本次任务，不证明科研目标已达成 |

两种结果动作的payload为 `answer/coverage_status/stop_reason/limitations/claims`；每个claim为 `statement/evidence_ids/limitations`。`coverage_status` 仅取 `sufficient_for_requested_use/partial/unresolved`。引用必须在本轮确实交付，claim引用也必须包含在命令的 `evidence_ids` 中。最终结果作为带 `summary_of` 来源关系的解释性事实保存，原文依赖一并交付Lab。

工具续轮只将新工具事实列为必要交付，旧证据仍保留在目录/请求集合中，可通过现有 `paged/narrowed` 策略显式延后。延后的片段没有本轮引用资格；形成判断时须确保需要的原文再次实际交付，必要时重读。历史记忆或目录指针不能替代原文。

任务登记在 `registration(kind="literature_task")`，当前状态为 `active/completed`；运行/阻塞/未知执行由Work、Effect、session及proposal状态表示。Lab调整/取消任务、追加额度、Literature请求范围变更等专用命令尚未实现。每次新委托创建自己的Literature实例，而不是复用一个默认子会话。

实际驱动模型前，需要另行初始化包含 `execution_mode="live"` 或 `"role_recall"`、明确 `model/effort/model_reservation`、模型预算和合适 `max_effective_context_bytes` 的 run 配置，并配置 Codex CLI 访问。直接工具示例中的 run 不具备这些条件；完整会话配置见 [持久 Agent 使用说明](persistent-agents.md)。对于已配置的模型 run：

```sh
.venv/bin/python -m rsi_lab --root runs/my-task --run main \
  --literature-config examples/literature-tools.json lab-start --event research-start
.venv/bin/python -m rsi_lab --root runs/my-task --run main \
  --literature-config examples/literature-tools.json run-until-wait --max-steps 20
```

也可使用 `literature-step --work WORK_ID` 单步运行已经由Lab委托产生的Literature工作；`WORK_ID`必须取自当前run。它不创建脱离Lab的独立研究任务。`run-until-wait`在单次驱动额度用尽或无法继续时返回，不是后台常驻服务。

## 6. 证据、成本与恢复边界

ToolResult使用 `ok/partial/unavailable/failed`，包含 `data/provenance/coverage/continuation/errors`；执行是否未知由真实Effect及必要的 `execution_unknown` 标识区分。已知“无凭证”“全文不可取得”“限流”等观察仍可发布给agent，不能伪装成空的成功检索结果。

原件获取、解析文本、真正交付的片段与页面图像分别记录。规范文本的字符/字节范围绑定文档版本与来源hash；PDF文本层读取不代表OCR、图像或表格结构都已正确理解。页面 PNG 的原件 hash、页码及图像 hash 保留在证据中；生成了 PNG 不等于模型已收到，收到图像也不等于完成了科学核验。检索命中、引用图的一条边、第三方report及生成skill均不自动构成对科研命题的支持。出处核验与科学推断复核是两个步骤；重复来源也不能自动计为独立证据。

每个模型/工具 Effect 沿用 run 预算原子预约。子任务另限制 `max_model_calls>=2`、`max_tool_calls>=1`，步数上限为二者之和。子额度按实际已记账成本加尚未释放的预约计算；原生嵌套 HTTP 请求会计数，恢复发布不再计一次调用。子工具额度无法容纳新调用，或没有后续子模型额度阅读其结果时，返回明确的未解决结果；run 级预约拒绝仍按现有预算阻塞处理。

**SDK和高层研究项目的内部LLM、embedding、检索请求不完全可观测。** `cost_visibility` 标示可见的外层SDK/worker/项目调用与未知内部数量；这些不是“内部0次调用”，也不声称run预算能硬控所有第三方内部费用。宿主仍需配置供应商侧额度和选定模型，系统不会替用户选择或购买模型。

unknown 执行保留调用身份、可获得的远程 handle 和结算义务。重启先恢复已持久回执，再做幂等发布与原任务续步。服务未配置等已知状态可以发布为 `unavailable` 后回到 agent 处理；必要来源超限、原件变更、未知执行或版本不匹配按对应检查阻塞，不能用重新启动子 agent 绕过。

## 7. 验证

```sh
.venv/bin/python -m unittest discover -s tests/literature -v
.venv/bin/python -m unittest tests.v2.test_literature_runtime -v
```

常规测试使用假模型、模拟HTTP/SDK或真实fake子进程，不调用付费模型。本次可选工具连接记录见 [接通状态](literature-connectivity.md)。

集成测试使用真实 `make_adapters + Runtime` 覆盖Lab父子关系、原文分段事实、完整能力目录容量、未配置服务状态、任务handle路由和崩溃恢复。上述小样联网/真实解析结果单独说明，不替代这些离线契约测试。
