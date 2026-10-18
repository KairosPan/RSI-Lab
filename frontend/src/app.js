import {
  demoMeta,
  candidates,
  campaigns,
  events,
  harnessVersions,
  evidenceRecords,
  toolCatalog,
} from "./data.js";
import { icon } from "./icons.js";

const app = document.querySelector("#app");
const dialog = document.querySelector("#detail-dialog");
const storageKey = "rsi-lab-workbench-demo-v1";
const activeCampaign = campaigns.find(
  (c) => c.id === demoMeta.activeCampaignId,
);
const routes = ["overview", "campaigns", "candidates", "evidence", "evolution"];
const nav = [
  ["overview", "grid", "工作台概览"],
  ["campaigns", "flask", "实验运行"],
  ["candidates", "molecule", "候选分子"],
  ["evidence", "layers", "证据库"],
  ["evolution", "branch", "Harness 演进"],
];
const saved = loadState();
const state = {
  route: getRoute(),
  query: "",
  verification: "all",
  candidateTab: "all",
  sort: "id",
  evidenceKind: "all",
  selected: new Set(
    saved.selected || candidates.filter((c) => c.shortlisted).map((c) => c.id),
  ),
  locked: saved.locked === true,
  paused: saved.paused === true,
  step: Number.isInteger(saved.step) ? Math.max(0, Math.min(3, saved.step)) : 1,
  drafts: Array.isArray(saved.drafts)
    ? saved.drafts.filter(
        (d) =>
          typeof d?.id === "string" &&
          typeof d?.name === "string" &&
          d.status === "draft" &&
          Number.isFinite(d.poolSize) &&
          Number.isFinite(d.shortlistSize) &&
          Number.isFinite(d.gpuBudget),
      )
    : [],
};
let toastTimeout;
let returnFocusId;

function loadState() {
  try {
    const value = JSON.parse(localStorage.getItem(storageKey));
    if (!value || typeof value !== "object") return {};
    if (Array.isArray(value.selected))
      value.selected = value.selected.filter((id) =>
        candidates.some((c) => c.id === id),
      );
    else delete value.selected;
    return value;
  } catch {
    return {};
  }
}
function persist() {
  try {
    localStorage.setItem(
      storageKey,
      JSON.stringify({
        selected: [...state.selected],
        locked: state.locked,
        paused: state.paused,
        step: state.step,
        drafts: state.drafts,
      }),
    );
  } catch {
    /* The demo remains usable when browser storage is unavailable. */
  }
}
function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        char
      ],
  );
}
const e = escapeHtml;
const num = (value) => Number(value).toLocaleString("en-US");
function getRoute() {
  const route = location.hash.slice(1);
  return routes.includes(route) ? route : "overview";
}
function pill(label, tone = "gray", glyph = "") {
  return `<span class="pill ${tone}">${glyph ? icon(glyph) : ""}${e(label)}</span>`;
}
function verificationBadge(status) {
  return {
    passed: pill("计算验证通过", "green", "check"),
    pending: pill("待验证", "orange", "clock"),
    failed: pill("协议未通过", "red"),
  }[status];
}
function button(label, action, glyph = "", style = "", extra = "") {
  return `<button class="btn ${style}" type="button" data-action="${action}" ${extra}>${glyph ? icon(glyph) : ""}${label}</button>`;
}
function go(route) {
  if (state.route === route) render();
  else location.hash = route;
}
function toast(message) {
  const node = document.querySelector("#toast");
  node.textContent = message;
  node.classList.add("visible");
  clearTimeout(toastTimeout);
  toastTimeout = setTimeout(() => node.classList.remove("visible"), 3500);
}
function moleculeArt() {
  return `<svg class="hero-art" viewBox="0 0 280 250" fill="none" aria-hidden="true"><defs><radialGradient id="orb"><stop stop-color="#e8efdb"/><stop offset="1" stop-color="#f2f5eb"/></radialGradient></defs><circle cx="161" cy="121" r="91" fill="url(#orb)"/><ellipse class="orbital" cx="161" cy="121" rx="110" ry="80" transform="rotate(-28 161 121)"/><ellipse class="orbital" cx="161" cy="121" rx="100" ry="102" transform="rotate(28 161 121)"/><g stroke="#9bb77d" stroke-width="1.4"><path d="m94 69 49-18 46 31-1 55-48 25-46-31zM94 69l-27-15m0 0-29 16m151 12 45-16 30 29m-76 42 37 33m-85-8-6 43m-40-74-37 26M143 51l5-27M225 170l-10 36m10-36 35 2"/><path d="m103 74 38-14m39 26v44m-42 23-34-21" stroke="#bed0a6"/></g><g fill="#e1ebd3" stroke="#9ab67c" stroke-width="1.3"><circle cx="94" cy="69" r="8"/><circle cx="143" cy="51" r="11"/><circle cx="189" cy="82" r="7"/><circle cx="188" cy="137" r="11"/><circle cx="140" cy="162" r="7"/><circle cx="94" cy="131" r="6"/><circle cx="225" cy="170" r="7"/><circle cx="67" cy="54" r="5"/></g><circle cx="188" cy="137" r="4" fill="#628b48"/><circle cx="143" cy="51" r="4" fill="#759853"/><g fill="#9db785"><circle cx="38" cy="70" r="3"/><circle cx="148" cy="24" r="3"/><circle cx="234" cy="66" r="4"/><circle cx="264" cy="95" r="3"/><circle cx="134" cy="205" r="4"/><circle cx="57" cy="157" r="3"/><circle cx="215" cy="206" r="4"/><circle cx="260" cy="172" r="3"/></g></svg>`;
}
function shell(content) {
  const currentTitle = nav.find((n) => n[0] === state.route)[2];
  return `<div class="app-shell"><button class="mobile-backdrop" data-action="close-nav" aria-label="关闭导航"></button>
    <aside class="sidebar" aria-label="主导航"><a class="brand" href="#overview" aria-label="RSI-Lab 首页"><span class="brand-mark">${icon("molecule")}</span><span class="brand-name">RSI<span>—</span>Lab</span></a>
      <button class="workspace-switch" data-action="about"><span class="workspace-icon">${icon("flask")}</span><span class="workspace-copy"><strong>Compound Discovery</strong><small>研究工作区 · Demo</small></span>${icon("down")}</button>
      <div class="nav-caption">WORKSPACE</div><nav class="nav-list">${nav.map(([id, glyph, label]) => `<a href="#${id}" class="nav-link ${id === state.route ? "active" : ""}" ${id === state.route ? 'aria-current="page"' : ""}>${icon(glyph)}${label}${id === "campaigns" ? `<span class="nav-count">${campaigns.length + state.drafts.length}</span>` : id === "evolution" ? '<span class="nav-count">3</span>' : ""}</a>`).join("")}</nav>
      <div class="sidebar-bottom"><button class="nav-link" data-action="protocol">${icon("settings")}协议与工具</button><div class="sidebar-note"><div class="note-title">${icon("branch")}持续学习，有据可循</div><p>每一次改进，都从可追溯的实验经验开始。</p><a class="small-link" href="#evolution">查看演进路径 ${icon("arrow")}</a></div><div class="profile"><span class="avatar">RL</span><div><strong>RSI-Lab Research</strong><small>LOCAL DEMO WORKSPACE</small></div>${icon("lock")}</div></div>
    </aside><div class="page-shell"><header class="topbar"><div class="breadcrumbs"><button class="icon-button mobile-menu" data-action="toggle-nav" aria-label="打开导航">${icon("menu")}</button><span>工作空间</span>${icon("chevron")}<strong>${currentTitle}</strong></div><div class="topbar-right"><span class="demo-badge"><i class="demo-dot"></i>演示工作区 · 示例数据</span><span class="topbar-line"></span><button class="icon-button" data-action="about" aria-label="关于此原型">${icon("info")}</button><span class="avatar">RL</span></div></header>
      <main class="main" id="main-content" tabindex="-1">${content}<footer class="app-footer"><span>${icon("lock")}本地交互原型 · 未连接计算后端</span><span>RSI-LAB / RESEARCH PREVIEW 01</span></footer></main></div></div>`;
}
function heading(title, subtitle, actions = "") {
  return `<div class="page-heading"><div><h1>${title}</h1><p>${subtitle}</p></div><div class="heading-actions">${actions}</div></div>`;
}
function contextStrip() {
  return `<div class="context-strip"><span>${icon("flask")}<strong>DEV-004</strong></span><span>CDK2 · 示例靶点</span><span>开发集</span><span>Harness H1</span><span class="mono">VERIFY-DEMO-01</span><span class="push-right">${icon(state.locked ? "lock" : "clock")}${state.locked ? "演示名单已锁定" : "暂定名单，可调整"}</span></div>`;
}
function budgetCard() {
  const c = activeCampaign;
  return `<section class="panel budget-panel" aria-label="演示 GPU 预算"><div class="budget-header"><h2 class="panel-title">算力预算</h2><span class="eyebrow">GPU HOURS</span></div><div class="budget-total">${c.gpuUsed.toFixed(1)}<small>/ ${c.gpuBudget.toFixed(1)} h</small></div><div class="budget-meter" role="img" aria-label="已用7.2，已预留1.2，可用3.6 GPU小时"><span class="meter-used" style="width:60%"></span><span class="meter-reserved" style="width:10%"></span></div><dl class="budget-key"><dt><i class="key-dot used"></i>已使用</dt><dd>7.2 h</dd><dt><i class="key-dot reserved"></i>已预留</dt><dd>1.2 h</dd><dt><i class="key-dot"></i>可分配</dt><dd>3.6 h</dd></dl><div class="budget-caption">当前 campaign 的在线计算 · 演示快照</div></section>`;
}
function stats() {
  const statsData = [
    ["候选池", "300", "个", "molecule", "12 条记录可交互预览"],
    [
      "暂定短名单",
      state.selected.size,
      "/ 20",
      "layers",
      state.locked ? "已在本地锁定演示名单" : "当前人工探索视图 · 可调整",
    ],
    [
      "计算验证完成",
      "9",
      "/ 12",
      "checkCircle",
      "7 通过 · 2 未通过 · 演示子集",
    ],
    ["当前 Harness", "H1", "", "branch", "运行版本冻结 · H2 待验证"],
  ];
  return `<div class="stats">${statsData.map(([label, value, suffix, glyph, foot], i) => `<section class="panel stat"><div class="stat-label">${label}${icon(glyph)}</div><div class="stat-value">${value}<small>${suffix}</small>${i === 0 ? '<span class="stat-mini-bars" aria-hidden="true"><i style="height:10px"></i><i style="height:16px"></i><i style="height:12px"></i><i style="height:20px"></i><i style="height:17px"></i><i style="height:25px"></i><i style="height:21px"></i></span>' : ""}</div><p class="stat-foot">${i === 3 ? icon("lock") : ""}${foot}</p></section>`).join("")}</div>`;
}
function flowPanel() {
  const steps = [
    ["terminal", "实验规划", "Lab Codex"],
    ["cpu", "工具执行", "Boltz-2 / Nesso-1"],
    ["layers", "证据整合", "Scientific Memory"],
    ["checkCircle", "候选提交", "固定评测协议"],
  ];
  const notes = [
    "读取公开证据，提出下一批计算请求。",
    "固定演示快照：正在补齐候选的计算证据。",
    "区分预测、验证与执行失败，保留来源引用。",
    "提交前保留不确定性；演示流程不会运行真实计算。",
  ];
  return `<section class="panel flow-panel"><div class="panel-head"><div><h2 class="panel-title">实验执行链</h2><p class="panel-subtitle">由 Lab 决策，Controller 调度</p></div><span class="flow-head-meta">STEP 0${state.step + 1} / 04</span></div><div class="flow-nodes">${steps.map(([glyph, title, subtitle], i) => `<div class="flow-node ${i === state.step ? "active" : i < state.step ? "done" : ""}"><div class="flow-icon">${icon(glyph)}${i === state.step ? '<i class="flow-live"></i>' : ""}</div><div class="flow-title">${title}</div><div class="flow-subtitle">${subtitle}</div></div>`).join("")}</div><div class="flow-note">${icon("info")}${notes[state.step]}</div><div class="panel-footer"><span>仅切换流程示意，不新增结果或资源消耗</span>${button("模拟下一步", "next-step", "arrow", "text", state.paused ? "disabled" : "")}</div></section>`;
}
function activityPanel() {
  const visible = events
    .filter((event) => event.campaignId === activeCampaign.id)
    .slice(0, 4);
  return `<section class="panel activity-panel"><div class="panel-head"><div><h2 class="panel-title">最近活动</h2><p class="panel-subtitle">DEV-004 · 固定演示轨迹</p></div>${icon("clock")}</div><div class="activity-list">${visible.map((event) => `<button class="activity-item" type="button" data-action="event" data-id="${event.id}" style="width:100%;text-align:left"><span class="activity-icon ${event.tone === "warning" ? "orange" : ""}">${icon(event.type === "failure" ? "info" : event.type === "verification" ? "check" : event.type === "memory" ? "layers" : "terminal")}</span><span class="activity-copy"><strong>${e(event.title)}</strong>${e(event.actor)}<span class="activity-time mono" style="display:block">${event.time}</span></span></button>`).join("")}</div><div class="panel-footer"><span>结果保留原始来源</span>${button("查看证据", "go-evidence", "arrow", "text")}</div></section>`;
}
function candidateRows(items, compact = false) {
  if (!items.length)
    return `<tr><td colspan="${compact ? 6 : 8}"><div class="empty-state">${icon("search")}<strong>没有匹配的候选</strong><p>换一个编号，或调整验证状态筛选。</p></div></td></tr>`;
  return items
    .map(
      (c) =>
        `<tr>${compact ? "" : `<td><input class="table-check" type="checkbox" data-candidate="${c.id}" aria-label="将 ${c.id} 加入暂定名单" ${state.selected.has(c.id) ? "checked" : ""} ${state.locked ? "disabled" : ""}></td>`}<td><button class="compound-cell" type="button" data-action="candidate" data-id="${c.id}"><span class="compound-glyph">${icon("molecule")}</span><span><span class="compound-name mono">${c.id}</span><span class="compound-id">${e(c.scaffold.split(" · ")[0])}</span></span></button></td><td class="number">${num(c.ic50Nm)} <span class="muted">nM</span></td>${compact ? "" : `<td>${e(c.tool)}</td><td class="number">${c.prediction.value === null ? '<span title="执行失败，结果未知">—</span>' : c.prediction.value.toFixed(2)}</td>`}<td>${verificationBadge(c.verification)}</td><td>${compact ? `<span class="selected-tag">${icon(state.selected.has(c.id) ? "check" : "plus")}${state.selected.has(c.id) ? "已选入" : "未选入"}</span>` : `<span class="mono">${c.evidence.length} 条</span>`}</td>${compact ? `<td class="number">${e(c.tool)}</td>` : ""}<td><button class="table-action" data-action="candidate" data-id="${c.id}" aria-label="查看 ${c.id} 详情">${icon("arrow")}</button></td></tr>`,
    )
    .join("");
}
function shortlistPreview() {
  const selected = candidates
    .filter((c) => state.selected.has(c.id))
    .slice(0, 4);
  return `<section class="panel table-panel section-space"><div class="panel-head"><div><h2 class="panel-title">候选短名单 <span class="title-count">${state.selected.size} / 20</span></h2><p class="panel-subtitle">${state.locked ? "已锁定的本地演示名单" : "当前暂定 · 持续积累证据"}</p></div>${button("全部候选", "go-candidates", "arrow", "text")}</div><div class="table-wrap"><table aria-label="短名单预览"><thead><tr><th>候选分子</th><th>历史 IC50</th><th>计算验证</th><th>短名单</th><th>预测工具</th><th><span class="sr-only">详情</span></th></tr></thead><tbody>${candidateRows(selected, true)}</tbody></table></div><div class="panel-footer"><span>显示 ${selected.length} 条 · 数值与结果均为演示</span><span class="lock-state">${icon("layers")}证据可追溯</span></div></section>`;
}
function overview() {
  return `${heading("实验工作台", "让每一轮决策，都建立在可追溯的证据之上。", button("导出演示名单", "export", "download") + button("新建实验", "new-campaign", "plus", "primary"))}
    <div class="overview-top"><section class="campaign-hero"><div class="hero-copy"><div class="hero-top"><span class="eyebrow">ACTIVE CAMPAIGN / DEV-004</span>${pill(state.paused ? "演示已暂停" : "演示回放", state.paused ? "gray" : "green", state.paused ? "pause" : "play")}</div><h2>CDK2 · 活性候选筛选</h2><p>在固定预算下探索候选，<br>让新计算证据指导下一步实验。</p><div class="hero-meta"><span>${icon("flask")}Development</span><span>${icon("branch")}Harness H1</span><span>${icon("lock")}协议已冻结 · 示例</span></div><div class="hero-actions">${button("查看候选", "go-candidates", "arrow", "primary")}${button(state.paused ? "继续演示" : "暂停演示", "pause", state.paused ? "play" : "pause", "")}</div></div>${moleculeArt()}<span class="target-visual-label">MOLECULAR NETWORK / 示意</span></section>${budgetCard()}</div>${stats()}
    <div class="overview-mid"><div class="overview-main-column">${flowPanel()}${shortlistPreview()}</div><div>${activityPanel()}<div class="evolution-teaser" style="align-items:flex-start;flex-wrap:wrap"><span class="teaser-icon">${icon("branch")}</span><div class="teaser-copy"><strong>下一次改进，已在路上</strong><p>H2 失败反馈策略 · 等待独立验证</p></div><a class="btn text" style="margin-left:45px" href="#evolution">查看 Harness 演进 ${icon("arrow")}</a></div></div></div>`;
}
function campaignsPage() {
  const all = [...state.drafts, ...campaigns];
  return `${heading("实验运行", "从独立的实验上下文出发，追踪每一轮探索。", button("新建实验", "new-campaign", "plus", "primary"))}<div class="info-banner">${icon("info")}当前为演示工作区。新建操作只保存本地草稿，不会调用模型或启动计算任务。</div><div class="campaign-grid">${all.map((c) => `<article class="panel campaign-card"><div class="campaign-card-head"><span class="eyebrow">${e(c.id)}</span>${pill(c.status === "draft" ? "本地草稿" : `${c.statusLabel} · 示例`, c.status === "running" ? "green" : c.status === "queued" ? "orange" : "gray")}</div><div class="eyebrow">${e(c.target)} / ${e(c.phaseLabel)}</div><h2>${e(c.name)}</h2><p class="description">${c.status === "draft" ? "实验参数已保存在当前浏览器，等待后端接入。" : c.id === "DEV-004" ? "根据当前证据，自适应安排下一批计算。" : c.id === "VAL-003" ? "相同数据快照与预算，独立比较候选版本。" : "保留失败反馈，形成可追溯的开发轨迹。"}</p><dl class="campaign-card-meta"><div><dt>候选池 / 目标名单</dt><dd>${num(c.poolSize)} / ${c.shortlistSize}</dd></div><div><dt>Harness</dt><dd>${e(c.harness)}</dd></div><div><dt>GPU 预算</dt><dd>${c.gpuBudget} h${c.phase === "validation" ? " / 分支" : ""}</dd></div><div><dt>数据快照</dt><dd class="mono">${e(c.snapshot)}</dd></div></dl>${button(c.status === "draft" ? "查看草稿" : "查看实验", "campaign", "arrow", c.id === "DEV-004" ? "soft" : "", `data-id="${e(c.id)}"`)}</article>`).join("")}</div>`;
}
function filteredCandidates() {
  return candidates
    .filter(
      (c) =>
        `${c.id} ${c.label} ${c.scaffold} ${c.tool}`
          .toLowerCase()
          .includes(state.query.toLowerCase()) &&
        (state.verification === "all" ||
          c.verification === state.verification) &&
        (state.candidateTab !== "shortlist" || state.selected.has(c.id)),
    )
    .sort((a, b) =>
      state.sort === "ic50-asc"
        ? a.ic50Nm - b.ic50Nm
        : state.sort === "ic50-desc"
          ? b.ic50Nm - a.ic50Nm
          : a.id.localeCompare(b.id),
    );
}
function candidateTableBody() {
  return candidateRows(filteredCandidates());
}
function candidatesPage() {
  return `${heading("候选分子", "保留每一项证据，也保留尚未解决的不确定性。", button("导出 JSON", "export", "download") + button(state.locked ? "名单已锁定" : "锁定演示名单", "lock", "lock", "primary", state.locked || !state.selected.size ? "disabled" : ""))}${contextStrip()}<section class="panel table-panel"><div class="toolbar"><label class="search-box">${icon("search")}<input id="candidate-search" type="search" placeholder="搜索编号、骨架簇或工具…" aria-label="搜索候选" value="${e(state.query)}"></label><select class="filter-select" id="verification-filter" aria-label="计算验证状态"><option value="all">全部验证状态</option><option value="passed" ${state.verification === "passed" ? "selected" : ""}>计算验证通过</option><option value="pending" ${state.verification === "pending" ? "selected" : ""}>待验证</option><option value="failed" ${state.verification === "failed" ? "selected" : ""}>协议未通过</option></select><select class="filter-select" id="sort-filter" aria-label="候选排序"><option value="id">按候选编号</option><option value="ic50-asc" ${state.sort === "ic50-asc" ? "selected" : ""}>IC50 从低到高</option><option value="ic50-desc" ${state.sort === "ic50-desc" ? "selected" : ""}>IC50 从高到低</option></select><span class="toolbar-spacer"></span><div class="tab-group" role="group" aria-label="名单范围"><button class="tab ${state.candidateTab === "all" ? "active" : ""}" data-action="candidate-tab" data-tab="all" aria-pressed="${state.candidateTab === "all"}">全部 12</button><button class="tab ${state.candidateTab === "shortlist" ? "active" : ""}" data-action="candidate-tab" data-tab="shortlist" aria-pressed="${state.candidateTab === "shortlist"}">已选 ${state.selected.size}</button></div></div><div class="table-wrap"><table aria-label="候选分子与演示证据"><thead><tr><th><span class="sr-only">选入名单</span></th><th>候选分子</th><th>历史 IC50 · 示例</th><th>预测来源</th><th>模拟预测分</th><th>计算验证</th><th>来源证据</th><th><span class="sr-only">详情</span></th></tr></thead><tbody id="candidate-body">${candidateTableBody()}</tbody></table></div><div class="table-summary"><span id="candidate-count">显示 ${filteredCandidates().length} / 12 条演示记录</span><span>已选 ${state.selected.size} / 20 · ${state.locked ? "已锁定" : "暂定名单"}</span></div></section><p class="download-note">模拟预测分为无量纲 UI 示例，不是工具原生输出，不用于跨模型比较。候选池 300 为场景规模，此处只提供 12 条演示记录。</p>`;
}
function evidencePage() {
  const rows = evidenceRecords.filter(
    (record) =>
      state.evidenceKind === "all" || record.kind === state.evidenceKind,
  );
  const categories = [
    ["all", "全部证据"],
    ["measurement", "历史测量"],
    ["prediction", "模型预测"],
    ["verification", "计算验证"],
    ["execution_failure", "执行失败"],
  ];
  return `${heading("证据库", "原始记录与派生解读分开保存，让结论有迹可循。", button("科学记忆示例", "memory", "book"))}${contextStrip()}<div class="toolbar" style="border:0;padding:0 0 19px"><div class="tab-group" role="group" aria-label="证据类型">${categories.map(([kind, label]) => `<button class="tab ${state.evidenceKind === kind ? "active" : ""}" data-action="evidence-tab" data-kind="${kind}" aria-pressed="${state.evidenceKind === kind}">${label}</button>`).join("")}</div><span class="toolbar-spacer"></span><span class="muted" style="font-size:10px">${rows.length} 条记录 · DEV-004 演示子集</span></div><div class="evidence-list">${rows.map((record) => `<article class="panel evidence-card"><span class="evidence-kind-icon">${icon(record.kind === "measurement" ? "file" : record.kind === "prediction" ? "cpu" : record.kind === "verification" ? "checkCircle" : "info")}</span><div class="evidence-body"><h2 class="evidence-title">${e(record.candidateId)} · ${e(record.title)} ${pill(record.kindLabel.split(" · ")[0], record.kind === "execution_failure" ? "orange" : record.kind === "verification" ? "green" : "gray")}</h2><p>${e(record.source)}${record.value !== null && record.kind !== "verification" ? ` · ${e(record.value)} ${e(record.unit)}` : ""}</p><div class="evidence-ref mono">${e(record.id)} / ${record.kind}</div></div>${button("查看", "evidence", "arrow", "text", `data-id="${e(record.id)}"`)}</article>`).join("")}</div>`;
}
function evolutionPage() {
  return `${heading("Harness 演进", "改进机制，验证收益，再交给下一轮实验。", button("发布规则", "gate", "lock"))}<div class="info-banner">${icon("info")}DEV-004 始终使用冻结的 H1。H2 是待验证候选，只有通过固定发布规则后才可用于新的实验。</div><div class="version-layout"><div class="version-list">${[
    ...harnessVersions,
  ]
    .reverse()
    .map(
      (v) =>
        `<article class="panel version-card"><div class="version-top"><div class="version-id">${v.id}${pill(v.statusLabel, v.status === "candidate" ? "orange" : v.status === "released" ? "green" : "gray")}</div><span class="eyebrow">${v.parent ? `${v.parent} → ${v.id}` : "BASELINE"}</span></div><h3>${e(v.name)}</h3><p>${e(v.summary)}</p><div class="version-chain">${["诊断", "修改", "基础检查", "配对验证", "发布"].map((label, i) => `${i ? icon("chevron") : ""}<span class="${v.status === "released" || (v.status === "candidate" && i < 3) ? "completed" : ""}">${label}</span>`).join("")}</div><div class="version-footer"><small>${e(v.validation)}</small>${button("查看版本", "version", "arrow", "text", `data-id="${v.id}"`)}</div></article>`,
    )
    .join(
      "",
    )}</div><aside class="panel principle-panel"><h3>改进的边界</h3><div class="principle-row">${icon("terminal")}<div><strong>修改运行机制</strong><p>提示词、证据整理与失败反馈可以演进。</p></div></div><div class="principle-row">${icon("lock")}<div><strong>保持评测独立</strong><p>模型权重、预算计量与发布条件保持冻结。</p></div></div><div class="principle-row">${icon("layers")}<div><strong>保留每次尝试</strong><p>失败补丁也记录原因、验证与资源开销。</p></div></div><div class="gate-summary">Meta 提交候选修改。<br>固定 Validator / Gate 决定是否发布。<br><br>本页为版本演进示例，未执行真实验证。</div></aside></div>`;
}
function render() {
  const focused = document.activeElement;
  let focusSelector;
  if (focused && app.contains(focused)) {
    if (focused.id) focusSelector = `#${CSS.escape(focused.id)}`;
    else if (focused.dataset.candidate)
      focusSelector = `[data-candidate="${CSS.escape(focused.dataset.candidate)}"]`;
    else if (focused.dataset.action) {
      focusSelector = `[data-action="${CSS.escape(focused.dataset.action)}"]`;
      for (const key of ["id", "tab", "kind"])
        if (focused.dataset[key])
          focusSelector += `[data-${key}="${CSS.escape(focused.dataset[key])}"]`;
    }
  }
  document.title = `RSI-Lab · ${nav.find((n) => n[0] === state.route)[2]}`;
  const views = {
    overview,
    campaigns: campaignsPage,
    candidates: candidatesPage,
    evidence: evidencePage,
    evolution: evolutionPage,
  };
  app.innerHTML = shell(views[state.route]());
  setNav(false, false);
  if (focusSelector)
    app.querySelector(focusSelector)?.focus({ preventScroll: true });
}
function setNav(open, moveFocus = true) {
  const mobile = window.matchMedia("(max-width: 760px)").matches;
  const isOpen = mobile && open;
  const sidebar = document.querySelector(".sidebar");
  const page = document.querySelector(".page-shell");
  const toggle = document.querySelector('[data-action="toggle-nav"]');
  document.body.classList.toggle("nav-open", isOpen);
  sidebar.id = "workspace-nav";
  sidebar.inert = mobile && !isOpen;
  sidebar.setAttribute("aria-hidden", String(mobile && !isOpen));
  page.inert = isOpen;
  toggle.setAttribute("aria-controls", "workspace-nav");
  toggle.setAttribute("aria-expanded", String(isOpen));
  if (moveFocus)
    (isOpen ? sidebar.querySelector(".nav-link.active") : toggle)?.focus();
}
function openDialog(
  title,
  body,
  footer = "",
  eyebrow = "RESEARCH PREVIEW / DEMO",
) {
  returnFocusId = document.activeElement;
  dialog.innerHTML = `<div class="dialog-head"><div><div class="eyebrow">${eyebrow}</div><h2 id="dialog-title">${e(title)}</h2></div><button class="icon-button" data-action="close-dialog" aria-label="关闭弹窗" autofocus>${icon("close")}</button></div><div class="dialog-body">${body}</div>${footer ? `<div class="dialog-bottom">${footer}</div>` : ""}`;
  if (!dialog.open) dialog.showModal();
  dialog.scrollTop = 0;
}
function closeDialog() {
  dialog.close();
  if (returnFocusId?.isConnected) returnFocusId.focus();
}
function candidateDialog(id) {
  const c = candidates.find((candidate) => candidate.id === id);
  if (!c) return;
  openDialog(
    "候选证据详情",
    `<div class="detail-header"><span class="compound-glyph">${icon("molecule")}</span><div><h3 class="mono">${c.id}</h3><p>${e(c.scaffold)} · DEV-004</p></div></div><div class="detail-metrics"><div class="detail-metric"><span>历史 IC50 · 模拟</span><strong>${num(c.ic50Nm)} <small style="display:inline">nM</small></strong><small>独立的公开测量示例</small></div><div class="detail-metric"><span>模型预测 · 模拟</span><strong>${c.prediction.value === null ? "未知" : c.prediction.value.toFixed(2)}</strong><small>${e(c.tool)} · 无量纲演示分</small></div><div class="detail-metric"><span>固定计算验证 · 模拟</span><strong>${verificationBadge(c.verification)}</strong><small>VERIFY-DEMO-01</small></div></div><div class="detail-section"><h4>当前解释与不确定性</h4><p>${e(c.note)}</p></div><div class="detail-section"><h4>原始证据引用</h4>${c.evidence
      .map((id) => {
        const r = evidenceRecords.find((record) => record.id === id);
        return `<button class="source-row" data-action="evidence" data-id="${id}"><span><span class="mono">${id}</span> · ${e(r.kindLabel)}</span>${icon("arrow")}</button>`;
      })
      .join(
        "",
      )}</div><p class="detail-notice">${e(demoMeta.verificationNotice)} 所有记录均为虚构演示数据。</p>`,
    button("关闭", "close-dialog") +
      button(
        state.selected.has(c.id) ? "移出暂定名单" : "加入暂定名单",
        "toggle-candidate",
        state.selected.has(c.id) ? "close" : "plus",
        "primary",
        `data-id="${id}" ${state.locked ? "disabled" : ""}`,
      ),
    `CANDIDATE / ${id}`,
  );
}
function evidenceDialog(id) {
  const record = evidenceRecords.find((r) => r.id === id);
  if (!record) return;
  openDialog(
    record.title,
    `<dl class="metadata"><dt>证据 ID</dt><dd class="mono">${record.id}</dd><dt>候选</dt><dd>${record.candidateId}</dd><dt>记录类型</dt><dd>${e(record.kindLabel)}</dd><dt>来源</dt><dd>${e(record.source)}</dd><dt>值 / 状态</dt><dd>${record.value === null ? "无可用数值" : `${e(record.value)} ${e(record.unit)}`}${record.errorCode ? ` · ${record.errorCode}` : ""}</dd><dt>协议</dt><dd>${e(record.protocol || "不适用；此记录不是固定协议终评")}</dd><dt>数据性质</dt><dd>演示 fixture · 非真实工具输出</dd></dl><div class="detail-section"><h4>记录说明</h4><p>${e(record.detail)}</p></div><div class="detail-section"><h4>结构化原始示例</h4><pre class="code-block">${e(JSON.stringify(record, null, 2))}</pre></div>`,
    button(
      "返回候选",
      "candidate",
      "arrow",
      "",
      `data-id="${record.candidateId}"`,
    ) + button("关闭", "close-dialog"),
    `EVIDENCE / ${record.id}`,
  );
}
function campaignDialog(id) {
  const c = [...campaigns, ...state.drafts].find((c) => c.id === id);
  if (!c) return;
  openDialog(
    c.name,
    `<dl class="metadata"><dt>Campaign</dt><dd class="mono">${e(c.id)}</dd><dt>阶段</dt><dd>${e(c.phaseLabel)}</dd><dt>靶点</dt><dd>${e(c.target)} · 示例</dd><dt>Harness</dt><dd>${e(c.harness)}</dd><dt>候选池 / 名单</dt><dd>${c.poolSize} / ${c.shortlistSize}</dd><dt>预算上限</dt><dd>${c.gpuBudget} GPU·h${c.phase === "validation" ? " / 每个配对分支" : ""}</dd><dt>消耗 / 预留</dt><dd>${c.gpuUsed} / ${c.gpuReserved} GPU·h</dd><dt>数据快照</dt><dd class="mono">${e(c.snapshot)}</dd><dt>固定协议</dt><dd class="mono">${e(c.protocol)}</dd><dt>状态</dt><dd>${e(c.statusLabel)} · ${c.status === "draft" ? "本地未执行草稿" : "演示记录"}</dd></dl><p class="detail-notice">${c.id === activeCampaign.id ? "工作台、候选与证据库对应本 campaign 的演示子集。" : "本原型仅提供此 campaign 的元数据；未加载它的候选与证据，不会混用 DEV-004 的结果。"}</p>`,
    button("关闭", "close-dialog") +
      (c.id === activeCampaign.id
        ? button("打开工作台", "campaign-overview", "arrow", "primary")
        : ""),
  );
}
function newCampaignDialog() {
  openDialog(
    "新建实验草稿",
    `<div class="message-box">配置会保存在当前浏览器。此原型不运行 Codex、预测工具或 GPU 任务。</div><form id="campaign-form"><div class="form-grid"><div class="field full"><label for="campaign-name">实验名称</label><input id="campaign-name" name="name" required maxlength="60" placeholder="例如：失败反馈策略验证" autocomplete="off"></div><div class="field"><label for="campaign-target">蛋白靶点</label><input id="campaign-target" value="CDK2 · 演示靶点" readonly><small>正式靶点由 pilot 确定</small></div><div class="field"><label for="campaign-phase">实验阶段</label><select id="campaign-phase" name="phase"><option value="development">开发 Development</option><option value="validation">验证 Validation</option></select></div><div class="field"><label for="campaign-pool">候选池规模</label><input id="campaign-pool" name="pool" type="number" value="300" min="1" max="10000" step="1" required></div><div class="field"><label for="campaign-shortlist">目标名单大小</label><input id="campaign-shortlist" name="shortlist" type="number" value="20" min="1" max="100" step="1" required></div><div class="field"><label for="campaign-budget">GPU 预算（小时 / 分支）</label><input id="campaign-budget" name="budget" type="number" value="12" min="1" max="100" step="0.5" required></div><div class="field"><label for="campaign-harness">Harness</label><input id="campaign-harness" value="开发 H1 / 验证 H1 对 H2" readonly><small>H2 仅作为配对验证候选</small></div></div><div class="form-error" id="form-error" role="alert"></div></form>`,
    button("取消", "close-dialog") +
      '<button class="btn primary" type="submit" form="campaign-form">保存演示草稿</button>',
    "CAMPAIGN / NEW DRAFT",
  );
}
function versionDialog(id) {
  const v = harnessVersions.find((v) => v.id === id);
  if (!v) return;
  const diff =
    id === "H2"
      ? "- retry every failed request\n+ classify execution failure separately from verification failure\n+ preserve evidence and resource cost for every attempt\n+ retry only when a justified recovery action is available"
      : id === "H1"
        ? "- store a single candidate conclusion\n+ store measurement, prediction and verification separately\n+ link ranking claims to evidence IDs\n+ retain conflicts and unresolved questions"
        : "Baseline H0\nFixed mechanisms across campaigns\nAdaptive decisions and memory updates within each campaign";
  openDialog(
    `${v.id} · ${v.name}`,
    `<div class="tag-row">${pill(v.statusLabel, v.status === "candidate" ? "orange" : "green")}${pill(`父版本 ${v.parent || "无"}`, "gray")}</div><div class="detail-section"><h4>机制变化</h4><p>${e(v.summary)}</p>${v.changes.map((change) => `<div class="source-row">${icon("check")}<span style="flex:1;margin-left:8px">${e(change)}</span></div>`).join("")}</div><div class="detail-section"><h4>变更意图示例 · 非可执行 patch</h4><pre class="code-block">${e(diff)}</pre></div><div class="detail-section"><h4>验证与生效范围</h4><p>${e(v.validation)}。${e(v.releaseScope)}。</p></div><p class="detail-notice">版本记录均为演示。Meta 提出修改，固定 Gate 决定发布；当前 DEV-004 不会切换 Harness。</p>`,
    button("发布规则", "gate", "lock") + button("关闭", "close-dialog"),
    `HARNESS / ${v.id}`,
  );
}
function protocolDialog() {
  openDialog(
    "协议与计算工具",
    `<div class="message-box">VERIFY-DEMO-01 为界面占位协议。工具版本、权重、参数及通过门槛仍需在正式实验前标定并冻结。</div><div class="protocol-list">${toolCatalog.map((tool) => `<div class="protocol-item"><div style="display:flex;justify-content:space-between;gap:10px"><strong>${e(tool.name)}</strong>${pill(tool.category, "gray")}</div><p>${e(tool.description)}</p></div>`).join("")}</div><div class="detail-section"><h4>预算记账</h4><p>上限 12.0 = 已用 7.2 + 已预留 1.2 + 可分配 3.6 GPU·h。此处仅为在线计算演示快照；Meta 与终评费用应独立记录，本原型未提供其结果。</p></div>`,
    button("关闭", "close-dialog"),
  );
}
function memoryDialog() {
  openDialog(
    "科学记忆 · 派生解读示例",
    `<div class="message-box">工作记忆是带来源的解释，可以修订；原始证据保持独立、不可覆写。</div><div class="detail-section"><h4>MEM-001 · 观察</h4><p>CMP-007 的预测信号与固定协议结果不一致，分别保留，不将两者合并为单一综合分。</p><button class="source-row" data-action="evidence" data-id="EV-007-P"><span>来源 EV-007-P · 模型预测</span>${icon("arrow")}</button><button class="source-row" data-action="evidence" data-id="EV-007-V"><span>来源 EV-007-V · 计算验证</span>${icon("arrow")}</button></div><div class="detail-section"><h4>MEM-002 · 未决问题</h4><p>CMP-008 因模拟 OOM 未产生预测。未知状态保留，恢复动作仍需额外证据与预算。</p><button class="source-row" data-action="evidence" data-id="EV-008-F"><span>来源 EV-008-F · 执行失败</span>${icon("arrow")}</button></div><p class="detail-notice">这些条目为固定演示内容，不代表已运行的 agent 推理。</p>`,
    button("关闭", "close-dialog"),
  );
}
function gateDialog() {
  openDialog(
    "固定发布规则",
    `<div class="protocol-list"><div class="protocol-item"><strong>01 · 基础检查</strong><p>修改范围、冻结接口、预算记录和证据来源符合约束。</p></div><div class="protocol-item"><strong>02 · 独立配对验证</strong><p>当前版本与候选版本使用相同 D0、工具与预算，在新的 validation pool 上比较。</p></div><div class="protocol-item"><strong>03 · 预登记的发布条件</strong><p>质量改善；或质量在允许容差内且成本下降。具体门槛须经 pilot 确定，此处不虚构显著性或提升结果。</p></div><div class="protocol-item"><strong>04 · 仅对后续实验生效</strong><p>发布新版本不会修改正在运行的 campaign。H2 当前仍等待验证。</p></div></div>`,
    button("关闭", "close-dialog"),
  );
}
function aboutDialog() {
  openDialog(
    "RSI-Lab 研究工作台",
    `<p class="preview-disclaimer">${e(demoMeta.notice)} ${e(demoMeta.targetNotice)}</p><div class="detail-section"><h4>本版可体验</h4><p>查看实验概览、筛选候选、追溯证据、调整与锁定演示名单、导出 JSON、创建本地实验草稿，以及查看 Harness 演进。</p></div><div class="detail-section"><h4>数据与状态</h4><p>演示名单和草稿仅保存在当前浏览器；不会发送到 GitHub、模型接口或外部计算服务。导出文件包含 isDemo 标记。</p></div><div class="reset-row"><p>恢复初始名单、清除本地草稿<br>原始演示证据不变</p>${button("重置本地演示", "reset", "refresh", "danger")}</div>`,
    button("关闭", "close-dialog"),
  );
}
function toggleCandidate(id) {
  if (state.locked || !candidates.some((c) => c.id === id)) return;
  if (state.selected.has(id)) state.selected.delete(id);
  else state.selected.add(id);
  persist();
  render();
}
function exportShortlist() {
  const payload = {
    isDemo: true,
    notice: demoMeta.notice,
    campaignId: activeCampaign.id,
    protocol: activeCampaign.protocol,
    harness: activeCampaign.harness,
    locked: state.locked,
    targetSize: activeCampaign.shortlistSize,
    missingSlots: activeCampaign.shortlistSize - state.selected.size,
    exportedAt: new Date().toISOString(),
    candidates: candidates.filter((c) => state.selected.has(c.id)),
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], {
    type: "application/json",
  });
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = href;
  anchor.download = "RSI-Lab_DEV-004_demo_shortlist.json";
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(href), 1500);
  toast(`已导出 ${state.selected.size} 个演示候选，文件包含来源与演示标记。`);
}

document.addEventListener("click", (event) => {
  if (event.target.closest(".skip-link")) {
    event.preventDefault();
    document.querySelector("#main-content").focus();
    return;
  }
  const node = event.target.closest("[data-action]");
  if (!node || node.disabled) return;
  const action = node.dataset.action;
  if (action === "close-dialog") closeDialog();
  else if (action === "candidate") candidateDialog(node.dataset.id);
  else if (action === "evidence") evidenceDialog(node.dataset.id);
  else if (action === "version") versionDialog(node.dataset.id);
  else if (action === "campaign") campaignDialog(node.dataset.id);
  else if (action === "new-campaign") newCampaignDialog();
  else if (action === "about") aboutDialog();
  else if (action === "protocol") protocolDialog();
  else if (action === "memory") memoryDialog();
  else if (action === "gate") gateDialog();
  else if (action === "export") exportShortlist();
  else if (action === "go-candidates") go("candidates");
  else if (action === "go-evidence") go("evidence");
  else if (action === "campaign-overview") {
    closeDialog();
    go("overview");
  } else if (action === "toggle-candidate") {
    toggleCandidate(node.dataset.id);
    candidateDialog(node.dataset.id);
  } else if (action === "pause") {
    state.paused = !state.paused;
    persist();
    render();
    toast(
      state.paused
        ? "演示流程已暂停，数据快照保持不变。"
        : "可继续模拟下一步；不会调用实际工具。",
    );
  } else if (action === "next-step") {
    if (!state.paused) {
      state.step = (state.step + 1) % 4;
      persist();
      render();
      toast("已切换流程示意。证据和 GPU 用量未改变。");
    }
  } else if (action === "toggle-nav")
    setNav(!document.body.classList.contains("nav-open"));
  else if (action === "close-nav") setNav(false);
  else if (action === "candidate-tab") {
    state.candidateTab = node.dataset.tab;
    render();
  } else if (action === "evidence-tab") {
    state.evidenceKind = node.dataset.kind;
    render();
  } else if (action === "event") {
    const record = events.find((record) => record.id === node.dataset.id);
    openDialog(
      record.title,
      `<dl class="metadata"><dt>事件来源</dt><dd>${e(record.actor)}</dd><dt>Campaign</dt><dd>${record.campaignId}</dd><dt>演示时间</dt><dd>${record.time}</dd><dt>事件类型</dt><dd>${record.type}</dd></dl><div class="detail-section"><h4>记录内容</h4><p>${e(record.detail)}</p></div>${record.evidence.map((id) => `<button class="source-row" data-action="evidence" data-id="${id}"><span>${id}</span>${icon("arrow")}</button>`).join("")}`,
      button("关闭", "close-dialog"),
    );
  } else if (action === "lock") {
    openDialog(
      "锁定演示名单",
      `<p class="preview-disclaimer">当前已选择 <strong>${state.selected.size}</strong> 个候选，目标为 ${activeCampaign.shortlistSize} 个，剩余 <strong>${activeCampaign.shortlistSize - state.selected.size}</strong> 个空位将保留。锁定后当前浏览器的名单只读，仍可导出；可通过“关于此原型”重置演示。</p><div class="detail-section"><p>此操作不向评估器提交，不会补全空位或产生新的计算结果。</p></div>`,
      button("取消", "close-dialog") +
        button("确认锁定本地名单", "confirm-lock", "lock", "primary"),
    );
  } else if (action === "confirm-lock") {
    state.locked = true;
    persist();
    closeDialog();
    render();
    toast("本地演示名单已锁定。");
  } else if (action === "reset") {
    state.selected = new Set(candidates.map((c) => c.id));
    state.drafts = [];
    state.locked = false;
    state.paused = false;
    state.step = 1;
    state.query = "";
    state.verification = "all";
    state.candidateTab = "all";
    state.sort = "id";
    state.evidenceKind = "all";
    persist();
    closeDialog();
    render();
    toast("已恢复初始演示状态。");
  }
});
document.addEventListener("input", (event) => {
  if (event.target.id === "candidate-search") {
    state.query = event.target.value;
    document.querySelector("#candidate-body").innerHTML = candidateTableBody();
    document.querySelector("#candidate-count").textContent =
      `显示 ${filteredCandidates().length} / 12 条演示记录`;
  }
});
document.addEventListener("change", (event) => {
  if (event.target.id === "verification-filter") {
    state.verification = event.target.value;
    render();
  }
  if (event.target.id === "sort-filter") {
    state.sort = event.target.value;
    render();
  }
  if (event.target.matches("[data-candidate]"))
    toggleCandidate(event.target.dataset.candidate);
});
document.addEventListener("submit", (event) => {
  if (event.target.id !== "campaign-form") return;
  event.preventDefault();
  const form = new FormData(event.target);
  const name = form.get("name").trim();
  const poolSize = Number(form.get("pool")),
    shortlistSize = Number(form.get("shortlist")),
    gpuBudget = Number(form.get("budget"));
  const error = document.querySelector("#form-error");
  if (!name) {
    error.textContent = "请输入实验名称。";
    return;
  }
  if (
    !Number.isInteger(poolSize) ||
    !Number.isInteger(shortlistSize) ||
    shortlistSize > poolSize
  ) {
    error.textContent = "名单大小需为整数，且不能超过候选池规模。";
    return;
  }
  const phase = form.get("phase");
  const id = `DRAFT-${String(state.drafts.length + 1).padStart(3, "0")}`;
  state.drafts.unshift({
    id,
    name,
    phase,
    phaseLabel: phase === "development" ? "开发" : "验证",
    target: "CDK2",
    harness: phase === "development" ? "H1" : "H1 / H2 候选",
    poolSize,
    shortlistSize,
    gpuBudget,
    gpuUsed: 0,
    gpuReserved: 0,
    status: "draft",
    statusLabel: "本地草稿",
    snapshot: "待配置",
    protocol: "VERIFY-DEMO-01",
    isDemo: true,
  });
  persist();
  closeDialog();
  go("campaigns");
  toast(`${id} 已保存在当前浏览器，未启动计算。`);
});
dialog.addEventListener("click", (event) => {
  if (event.target === dialog) {
    const rect = dialog.getBoundingClientRect();
    if (
      event.clientX < rect.left ||
      event.clientX > rect.right ||
      event.clientY < rect.top ||
      event.clientY > rect.bottom
    )
      closeDialog();
  }
});
window.addEventListener("hashchange", () => {
  state.route = getRoute();
  render();
  window.scrollTo(0, 0);
  document.querySelector("#main-content").focus({ preventScroll: true });
});
window.addEventListener("keydown", (event) => {
  if (!document.body.classList.contains("nav-open")) return;
  if (event.key === "Escape") {
    setNav(false);
    return;
  }
  if (event.key === "Tab") {
    const nodes = [
      ...document.querySelectorAll(
        ".sidebar a, .sidebar button:not(:disabled)",
      ),
    ];
    const first = nodes[0],
      last = nodes.at(-1);
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }
});
window
  .matchMedia("(max-width: 760px)")
  .addEventListener("change", () => setNav(false, false));
render();
