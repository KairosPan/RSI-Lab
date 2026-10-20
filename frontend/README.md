# RSI-Lab 前端工作台

中文科研工作台，使用 HTML、CSS 和 JavaScript 模块实现，无第三方运行依赖。

![RSI-Lab 画布工作台，截图为演示数据](docs/workbench.png)

[手机布局](docs/mobile.png) · [设计说明](DESIGN.md) · [验证记录](QA.md)

## 本地预览

需要 Node.js 20 或更高版本，无需安装软件包：

```sh
cd frontend
npm run dev
```

打开 `http://127.0.0.1:4173`。端口可通过 `PORT` 调整。

```sh
npm run check
npm test
npm run build
```

构建生成独立的 `dist/index.html`，可以直接用浏览器打开。独立文件支持演示模式；真实遥测需要通过 HTTP 预览服务器访问。

## 画布工作台

首页展示 Controller、Lab Codex、Tool Executor、Boltz-2、Nesso-1、Scientific Memory、Meta Codex、Validation Gate 八个组件。实线表示单轮消息关系，虚线表示跨轮次交接；连线不代表此刻正在传输。

- 拖动节点调整布局，拖动空白处平移；工具栏支持缩放、适应视图、重置布局。
- 点击节点，或用详情面板下拉框选择组件，查看状态、任务、心跳、指标、最近事件与输入输出约定。
- “关注异常”突出执行失败、心跳过期与未知节点；点击事件可定位相关组件。
- 聚焦画布后使用方向键平移，`+` / `-` 缩放，`0` 适应视图；Ctrl / Command + 滚轮也可缩放。
- 手机默认聚焦所选组件，可拖动查看其他部分，或使用下拉框切换。选择“适应画布”可查看整体关系。
- “播放演示”每 5 秒切换示例状态；不启动实验、不调用模型、不消耗 GPU。

## 接入真实遥测

“状态来源”可以切换至“本地遥测”。页面每 5 秒只读访问同源 `GET /api/runtime`，服务端读取指定 JSON 文件：

```sh
RSI_LAB_RUNTIME_FILE=/absolute/path/to/runtime.json npm run dev
```

Controller 或独立采集器负责从实际运行系统生成该文件，建议每 5–10 秒通过临时文件加原子重命名更新。前端不扫描进程，也不从历史 `running` 字段猜测进程是否存活。当前仓库已有的实验结果文件未自动转换为心跳源。

完整字段见 [runtime.example.json](runtime.example.json)。该文件刻意标记 `source: "demo"`，实时接口会拒绝它；只有实际采集器生成的记录应使用 `source: "live"`。

```text
source: "live"
campaignId: 当前 campaign 标识
harness: 活动 Harness 版本
observedAt: 本次采集的 UTC ISO 时间
nodes[]:
  id: controller | lab | executor | boltz | nesso | memory | meta | validator
  status: running | queued | idle | error | offline | unknown
  detail, task: 状态说明与当前任务（字符串）
  heartbeatAt: 实际心跳的 UTC ISO 时间，未收到时为 null
  metrics: [{label, value}]
  events: [{time, message, level: info | success | warning | error}]
  progress: 可选，0–100
```

判断规则：

- 心跳超过 30 秒标记“心跳过期”；快照超过 60 秒时全部标记过期。
- 未上报的节点或心跳显示“未知”。任务失败与心跳过期分别显示。
- 接口中断、格式错误或演示来源被拒绝时，清除当前运行状态并显示未知，不回退为演示。
- 源时间不会被刷新请求改写。将来的时间、重复节点、无效字段会被拒绝。
- 仅接受 GET / HEAD；快照限制为 1 MiB，错误响应不暴露文件路径。页面只显示固定八类节点。
- `source: "live"` 是生产者契约，不是系统对进程存活的独立证明；采集器需要如实报告心跳。该接口没有鉴权，只监听回环地址，不能直接用作公网服务。

## 其他页面与数据边界

候选分子、实验列表、证据库与 Harness 演进继续使用虚构演示数据，不会随首页遥测自动切换。`DEV-004`、CDK2、测量、预测、预算和版本均为 UI 示例。

支持候选搜索与排序、证据追溯、名单选择与锁定、JSON 导出、实验草稿和版本查看。名单与草稿保存在当前浏览器。OOM 表示执行失败与预测缺失，不解释为分子无活性。H2 是候选版本，不替换活动 campaign 的 H1。

## 文件组织

```text
src/app.js             页面、导航与本地状态
src/workbench.js       画布、节点详情、遥测订阅与清理
src/runtime.js         拓扑定义、演示状态、严格校验与获取适配
src/styles.css         视觉与响应式布局
src/data.js            其他页面的演示数据
src/icons.js           本地 SVG 图标
server.mjs             回环地址预览服务器
runtime-server.mjs     只读遥测文件接口
runtime.example.json   示例契约（不可当成真实遥测）
tests/runtime.test.mjs  遥测与接口边界测试
scripts/build.mjs      生成独立 HTML
```
