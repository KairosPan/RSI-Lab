import { experiment } from "./experiment-data.js";
import { icon } from "./icons.js";
import { workbenchPage, mountWorkbench } from "./workbench.js";

const app = document.querySelector("#app");
const dialog = document.querySelector("#detail-dialog");
const routes = ["overview", "campaigns", "candidates", "evidence", "evolution"];
const nav = [
  ["overview", "grid", "Workbench"],
  ["campaigns", "flask", "Run comparison"],
  ["candidates", "molecule", "Candidates"],
  ["evidence", "layers", "Evidence"],
  ["evolution", "branch", "Harness evolution"],
];
const state = {
  route: getRoute(),
  arm: "H1",
  query: "",
  scope: "all",
  sort: "id",
  evidenceKind: "all",
};
let unmountWorkbench;
let returnFocus;
let toastTimeout;
const e = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        char
      ],
  );
const number = (value, digits = 0) =>
  value === null || value === undefined || !Number.isFinite(value)
    ? "—"
    : value.toLocaleString("en-US", {
        maximumFractionDigits: digits,
        minimumFractionDigits: digits,
      });
const time = (value) =>
  Number.isFinite(Date.parse(value))
    ? new Date(value).toLocaleString("en-GB", {
        timeZone: "UTC",
        year: "numeric",
        month: "short",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      }) + " UTC"
    : "Not recorded";
const arm = () => experiment.arms.find((a) => a.id === state.arm);
const result = (candidate) => candidate.results[state.arm];
const pill = (label, tone = "gray") =>
  `<span class="pill ${tone}">${e(label)}</span>`;
const button = (label, action, glyph = "", style = "", extra = "") =>
  `<button class="btn ${style}" type="button" data-action="${action}" ${extra}>${glyph ? icon(glyph) : ""}${label}</button>`;
const closeButton = () => button("Close", "close-dialog");
function getRoute() {
  const id = location.hash.slice(1);
  return routes.includes(id) ? id : "overview";
}
function heading(title, subtitle, actions = "") {
  return `<div class="page-heading"><div><div class="eyebrow">COMPARISON V2 / RECORDED RESULTS</div><h1>${title}</h1><p>${subtitle}</p></div><div class="heading-actions">${actions}</div></div>`;
}
function toast(message) {
  const node = document.querySelector("#toast");
  node.textContent = message;
  node.classList.add("visible");
  clearTimeout(toastTimeout);
  toastTimeout = setTimeout(() => node.classList.remove("visible"), 3500);
}
function shell(content) {
  const title = nav.find((item) => item[0] === state.route)[2];
  return `<div class="app-shell"><button class="mobile-backdrop" data-action="close-nav" aria-label="Close navigation"></button>
  <aside class="sidebar" aria-label="Main navigation"><a class="brand" href="#overview" aria-label="RSI-Lab home"><span class="brand-mark">${icon("molecule")}</span><span class="brand-name">RSI<span>—</span>Lab</span></a>
  <button class="workspace-switch" data-action="sources"><span class="workspace-icon">${icon("flask")}</span><span class="workspace-copy"><strong>CDK2 Discovery</strong><small>20 compounds · 3 arms</small></span>${icon("down")}</button>
  <div class="nav-caption">RESEARCH WORKSPACE</div><nav class="nav-list">${nav.map(([id, glyph, label]) => `<a class="nav-link ${state.route === id ? "active" : ""}" href="#${id}" ${state.route === id ? 'aria-current="page"' : ""}>${icon(glyph)}${label}${id === "campaigns" ? '<span class="nav-count">3</span>' : ""}</a>`).join("")}</nav>
  <div class="sidebar-bottom"><button class="nav-link" data-action="protocol">${icon("settings")}Protocol & tools</button><div class="sidebar-note"><div class="note-title">${icon("checkCircle")}Evidence, in context</div><p>One completed comparison.<br>Every result has a source.</p><button class="small-link" data-action="sources">View provenance ${icon("arrow")}</button></div><div class="profile"><span class="avatar">RL</span><div><strong>RSI-Lab Research</strong><small>LOCAL EXPERIMENT RECORDS</small></div>${icon("lock")}</div></div></aside>
  <div class="page-shell"><header class="topbar"><div class="breadcrumbs"><button class="icon-button mobile-menu" data-action="toggle-nav" aria-label="Open navigation">${icon("menu")}</button><span>Workspace</span>${icon("chevron")}<strong>${title}</strong></div><div class="topbar-right"><label class="arm-picker">Run arm<select id="arm-select" aria-label="Run arm">${experiment.arms.map((a) => `<option value="${a.id}" ${a.id === state.arm ? "selected" : ""}>${e(a.label)}</option>`).join("")}</select></label><span class="demo-badge"><i class="demo-dot"></i>Local research workspace</span><button class="icon-button" data-action="sources" aria-label="Experiment provenance">${icon("info")}</button></div></header>
  <main class="main ${state.route === "overview" ? "canvas-main" : ""}" id="main-content" tabindex="-1">${content}<footer class="app-footer"><span>${icon("lock")}Read-only records · ${e(experiment.id)}</span><span>RSI-LAB / EXPERIMENT WORKSPACE</span></footer></main></div></div>`;
}
function contextStrip() {
  return `<div class="context-strip"><span>${icon("flask")}<strong>${e(arm().label)}</strong></span><span>CDK2</span><span class="mono">${e(experiment.assayId)}</span><span>${experiment.poolSize} candidates</span><span class="push-right">${icon("lock")}Final shortlist frozen · ${experiment.shortlistSize} compounds</span></div>`;
}
function metric(label, value, foot, glyph = "chart") {
  return `<section class="panel stat"><div class="stat-label">${label}${icon(glyph)}</div><div class="stat-value">${value}</div><p class="stat-foot">${foot}</p></section>`;
}
function overview() {
  const a = arm();
  return `${workbenchPage(state.arm)}<section class="recorded-readout"><div class="panel-head"><div><h2 class="panel-title">${e(a.label)} · completed run readout</h2><p class="panel-subtitle">Historical results, not live process telemetry</p></div><a class="btn text" href="#campaigns">Compare all arms ${icon("arrow")}</a></div><div class="stats recorded-stats">${metric("Paired-valid final candidates", `${a.quality.validCount}<small>/ ${a.quality.shortlistSize}</small>`, "Two docking seeds with valid poses", "checkCircle")}${metric("Mean qualified CNNscore", number(a.quality.meanCnn, 6), "Mean of candidate medians · pose score")}${metric("Observed Lab tokens", number(a.usage.totalTokens), `${a.usage.calls} calls · Meta cost reported separately`, "terminal")}${metric("Physical jobs", `${a.numerical.jobs}<small>/ 16</small>`, `${a.numerical.onlineCandidates} candidates · seeds 17 and 43`, "cpu")}</div><div class="info-banner">${icon("info")}This is one development comparison. Pose validity and CNNscore do not establish biological activity or a validated affinity pass rate.</div></section>`;
}
function comparisonTable() {
  const rows = [
    [
      "Final paired-valid coverage",
      (a) => `${a.quality.validCount} / ${a.quality.shortlistSize}`,
    ],
    ["Mean qualified CNNscore", (a) => number(a.quality.meanCnn, 6)],
    [
      "Mean empirical energy · kcal/mol",
      (a) => number(a.quality.meanEnergy, 5),
    ],
    ["Mean CNNscore seed range", (a) => number(a.quality.meanCnnRange, 6)],
    ["Maximum CNNscore seed range", (a) => number(a.quality.maxCnnRange, 6)],
    [
      "Online candidates / physical jobs",
      (a) => `${a.numerical.onlineCandidates} / ${a.numerical.jobs}`,
    ],
    [
      "Valid job poses / completed jobs",
      (a) => `${a.numerical.poseValidJobs} / ${a.numerical.completedJobs}`,
    ],
    ["Lab calls", (a) => number(a.usage.calls)],
    ["Input + output tokens", (a) => number(a.usage.totalTokens)],
    ["Model wall time · s", (a) => number(a.usage.llmWallSeconds, 3)],
    [
      "Numerical batch wall time · s",
      (a) => number(a.numerical.batchWallSeconds, 3),
    ],
    ["Worker CPU time · CPU-s", (a) => number(a.numerical.cpuSeconds, 3)],
    ["Terminal jobs", (a) => number(a.numerical.terminalJobs)],
    [
      "Actual cost · USD",
      (a) =>
        a.usage.costUsd === null ? "Unknown" : number(a.usage.costUsd, 2),
    ],
  ];
  return `<div class="table-wrap"><table class="comparison-table" aria-label="Three-arm experiment comparison"><thead><tr><th>Readout</th>${experiment.arms.map((a) => `<th class="${a.id === state.arm ? "highlight-col" : ""}">${e(a.label)}</th>`).join("")}</tr></thead><tbody>${rows.map(([label, get]) => `<tr><td>${label}</td>${experiment.arms.map((a) => `<td class="mono ${a.id === state.arm ? "highlight-col" : ""}">${get(a)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}
function campaignsPage() {
  const h0 = experiment.arms.find((a) => a.id === "H0"),
    h1 = experiment.arms.find((a) => a.id === "H1");
  const total = h1.usage.totalTokens + experiment.meta.totalTokens;
  return `${heading("Run comparison", "A completed, prospective comparison on one 20-compound CDK2 pool.", button("Export experiment", "export", "download"))}<div class="campaign-grid">${experiment.arms.map((a) => `<article class="panel campaign-card"><div class="campaign-card-head"><span class="eyebrow">${e(a.id)}</span>${pill("Completed", "green")}</div><h2>${e(a.label)}</h2><p class="description">${a.id === "baseline" ? "Fixed IC50 top-8 selection, followed by the frozen computational ranking rule." : a.id === "H0" ? "Adaptive Lab decisions with the original expanded JSON input presentation." : "Adaptive Lab decisions with lossless compact JSON input presentation."}</p><dl class="campaign-card-meta"><div><dt>Final paired-valid coverage</dt><dd>${a.quality.validCount} / ${a.quality.shortlistSize}</dd></div><div><dt>Observed Lab tokens</dt><dd>${number(a.usage.totalTokens)}</dd></div><div><dt>Online candidate slots</dt><dd>${a.numerical.onlineCandidates} / 8</dd></div><div><dt>Input presentation</dt><dd class="mono">${e(a.presentationMode)}</dd></div></dl>${button("Inspect this arm", "select-arm", "arrow", a.id === state.arm ? "soft" : "", `data-arm="${a.id}"`)}</article>`).join("")}</div><section class="panel comparison-panel"><div class="panel-head"><div><h2 class="panel-title">Quality and measured resource use</h2><p class="panel-subtitle">Equal opportunity limits; independent physical executions</p></div>${pill("Descriptive · n = 1 per arm")}</div>${comparisonTable()}<div class="comparison-footnote">Quality means use only paired-valid shortlisted candidates. Missing results are not zero-filled. Seed ranges are not confidence intervals. Numerical wall time includes CPU search, GPU rescoring and checks; it is not active GPU time.</div></section><div class="readout-grid"><section class="panel cost-panel"><div class="panel-head"><div><h2 class="panel-title">Include the cost of improvement</h2><p class="panel-subtitle">Observed CLI input + output tokens</p></div>${icon("terminal")}</div><div class="cost-bars">${[
    ["H0 Lab", h0.usage.totalTokens, ""],
    ["H1 Lab", h1.usage.totalTokens, ""],
    ["Meta + H1", total, "meta-total"],
  ]
    .map(
      ([label, value, cls]) =>
        `<div><span>${label}</span><div class="cost-track"><i class="${cls}" style="width:${(value / total) * 100}%"></i></div><strong>${number(value)}</strong></div>`,
    )
    .join(
      "",
    )}</div><p>H1 used ${number(h0.usage.totalTokens - h1.usage.totalTokens)} fewer Lab tokens. Including the single Meta call, Meta + H1 used ${number(total - h0.usage.totalTokens)} more tokens than H0. Development overhead has not been recovered in this comparison.</p></section><section class="panel interpretation-panel"><h2 class="panel-title">Read the differences together</h2><p>All three final shortlists have paired-valid coverage of 5/5. H1 has a slightly higher mean CNNscore and a less favorable mean empirical energy, with one different final candidate.</p><p>Baseline and H0 overlap in 5/5 candidates; H0 and H1 overlap in 4/5. These are descriptive observations, not evidence of equivalence or a stable improvement.</p>${button("View frozen protocol", "protocol", "arrow", "text")}</section></div>`;
}
function filteredCandidates() {
  return experiment.candidates
    .filter(
      (c) =>
        `${c.id} ${c.scaffold} ${c.smiles}`
          .toLowerCase()
          .includes(state.query.toLowerCase()) &&
        (state.scope === "all" ||
          (state.scope === "shortlist" && result(c)?.shortlisted) ||
          (state.scope === "computed" && result(c)) ||
          (state.scope === "uncomputed" && !result(c))),
    )
    .sort((a, b) =>
      state.sort === "ic50"
        ? a.ic50Nm - b.ic50Nm
        : state.sort === "cnn"
          ? (result(b)?.cnnMedian ?? -Infinity) -
              (result(a)?.cnnMedian ?? -Infinity) || a.id.localeCompare(b.id)
          : a.id.localeCompare(b.id),
    );
}
function candidateRows() {
  const rows = filteredCandidates();
  return rows.length
    ? rows
        .map((c) => {
          const r = result(c);
          return `<tr><td><button class="compound-cell" data-action="candidate" data-id="${c.id}"><span class="compound-glyph">${icon("molecule")}</span><span><span class="compound-name mono">${c.id}</span><span class="compound-id">${e(c.scaffold)}</span></span></button></td><td class="mono">${number(c.ic50Nm, 1)} <small>nM</small></td><td class="mono">${number(r?.cnnMedian, 6)}</td><td class="mono">${number(r?.energyMedian, 4)}</td><td>${r ? pill(`${r.validRepeats} / 2 valid poses`, r.pairedValid ? "green" : "orange") : pill("Not computed")}</td><td>${r?.shortlisted ? pill(`Final #${r.rank}`, "green") : '<span class="muted">—</span>'}</td><td>${button("Details", "candidate", "arrow", "text", `data-id="${c.id}"`)}</td></tr>`;
        })
        .join("")
    : '<tr><td colspan="7"><div class="empty-state"><strong>No matching candidates</strong><p>Try another ID or selection filter.</p></div></td></tr>';
}
function candidatesPage() {
  return `${heading("Candidate explorer", "Historical assay measurements and computational evidence, kept separate.", button("Export arm results", "export-arm", "download"))}${contextStrip()}<section class="panel table-panel"><div class="toolbar"><label class="search-box">${icon("search")}<input id="candidate-search" type="search" placeholder="Search compound, scaffold or SMILES…" aria-label="Search candidates" value="${e(state.query)}"></label><select class="filter-select" id="scope-filter" aria-label="Candidate scope">${[
    ["all", "All 20 candidates"],
    ["shortlist", "Final shortlist · 5"],
    ["computed", "Computed · 8"],
    ["uncomputed", "Not computed · 12"],
  ]
    .map(
      ([id, label]) =>
        `<option value="${id}" ${state.scope === id ? "selected" : ""}>${label}</option>`,
    )
    .join(
      "",
    )}</select><select class="filter-select" id="sort-filter" aria-label="Candidate sorting">${[
    ["id", "Compound ID"],
    ["ic50", "Historical IC50: low first"],
    ["cnn", "Qualified CNNscore: high first"],
  ]
    .map(
      ([id, label]) =>
        `<option value="${id}" ${state.sort === id ? "selected" : ""}>${label}</option>`,
    )
    .join(
      "",
    )}</select></div><div class="table-wrap"><table aria-label="Candidates for selected experiment arm"><thead><tr><th>Compound</th><th>Historical IC50</th><th>Qualified CNNscore</th><th>Energy · kcal/mol</th><th>Two-seed pose checks</th><th>Frozen shortlist</th><th><span class="sr-only">Details</span></th></tr></thead><tbody id="candidate-body">${candidateRows()}</tbody></table></div><div class="table-summary"><span id="candidate-count">Showing ${filteredCandidates().length} / ${experiment.poolSize} candidates</span><span>${e(arm().label)} · Read-only final results</span></div></section><p class="download-note">Scores and energies are medians across seeds 17 and 43, qualified only when both poses are valid. Uncomputed or unqualified values stay blank. Historical IC50 was available to the Lab; it is not a hidden outcome.</p>`;
}
function visibleEvidence() {
  return experiment.evidence.filter(
    (r) =>
      (!r.arm || r.arm === "shared" || r.arm === state.arm) &&
      (state.evidenceKind === "all" || r.kind === state.evidenceKind),
  );
}
function evidencePage() {
  const rows = visibleEvidence();
  const kinds = [...new Set(experiment.evidence.map((r) => r.kind))];
  return `${heading("Evidence ledger", "Inspect recorded measurements and the actual outputs behind each decision.", button("Source manifest", "sources", "file"))}${contextStrip()}<div class="toolbar evidence-toolbar"><div class="tab-group" role="group" aria-label="Evidence type"><button class="tab ${state.evidenceKind === "all" ? "active" : ""}" data-action="evidence-tab" data-kind="all" aria-pressed="${state.evidenceKind === "all"}">All records</button>${kinds.map((kind) => `<button class="tab ${state.evidenceKind === kind ? "active" : ""}" data-action="evidence-tab" data-kind="${e(kind)}" aria-pressed="${state.evidenceKind === kind}">${kindLabel(kind)}</button>`).join("")}</div><span class="toolbar-spacer"></span><span class="muted">${rows.length} records</span></div><div class="evidence-list">${rows.map((r) => `<article class="panel evidence-card"><span class="evidence-kind-icon">${icon(r.kind.includes("measurement") ? "file" : "cpu")}</span><div class="evidence-body"><h2 class="evidence-title">${e(r.candidateId)} · ${e(r.title)}</h2><p>${e(r.detail)}</p><div class="evidence-ref mono">${e(r.source)}</div></div>${button("Inspect", "evidence", "arrow", "text", `data-id="${e(r.id)}"`)}</article>`).join("")}</div>`;
}
function kindLabel(kind) {
  return kind.includes("measurement")
    ? "Historical measurements"
    : kind.includes("features")
      ? "Structure features"
      : "Computational results";
}
function evolutionPage() {
  const m = experiment.meta;
  return `${heading("Harness evolution", "One proposed presentation change, evaluated in a new-pool development comparison.", button("Release boundary", "gate", "lock"))}<div class="info-banner">${icon("lock")}H1 was evaluated in this comparison. It is not authorized for release. No H2 version is part of this experiment.</div><div class="version-layout"><div class="version-list"><article class="panel version-card"><div class="version-top"><div class="version-id">H1 ${pill("Evaluated · not released", "orange")}</div><span class="eyebrow">H0 → H1</span></div><h3>Lossless compact JSON presentation</h3><p>${e(m.summary)}</p><div class="version-chain"><span class="completed">Diagnosed</span>${icon("chevron")}<span class="completed">Proposed</span>${icon("chevron")}<span class="completed">Compared</span>${icon("chevron")}<span>Not released</span></div><div class="detail-section">${m.changes.map((change) => `<p class="change-line">${icon("check")}${e(change)}</p>`).join("")}</div><div class="version-footer"><small>Input presentation only · frozen scientific and budget rules</small>${button("Inspect change", "version", "arrow", "text")}</div></article><article class="panel version-card"><div class="version-top"><div class="version-id">H0 ${pill("Reference harness")}</div><span class="eyebrow">EXPANDED_JSON_V1</span></div><h3>Original task input presentation</h3><p>The same task data and response contract, using expanded JSON. Lab decisions and memory updates remain adaptive within the run.</p><div class="version-footer"><small>4 Lab calls · 162,096 observed tokens</small>${button("Inspect H0 results", "select-arm", "arrow", "text", 'data-arm="H0"')}</div></article></div><aside class="panel principle-panel"><h3>Meta development cost</h3><div class="meta-token-value">${number(m.totalTokens)}<small>observed tokens · one call</small></div><dl class="metadata"><dt>Input tokens</dt><dd>${number(m.inputTokens)}</dd><dt>Output tokens</dt><dd>${number(m.outputTokens)}</dd><dt>Wall time</dt><dd>${number(m.wallSeconds, 3)} s</dd><dt>USD cost</dt><dd>Unknown</dd><dt>Release authorized</dt><dd>${m.releaseAuthorized ? "Yes" : "No"}</dd></dl><div class="gate-summary">Meta used old-pool development material. It did not read the new comparison pool. One trajectory per arm is insufficient to establish a stable improvement.</div></aside></div><section class="panel limitations-panel"><h2 class="panel-title">Interpretation limits</h2><ul>${experiment.limitations.map((item) => `<li>${e(item)}</li>`).join("")}</ul></section>`;
}
function render() {
  const active = document.activeElement;
  const focusId = active && app.contains(active) ? active.id : null;
  unmountWorkbench?.();
  document.title = `RSI-Lab · ${nav.find((item) => item[0] === state.route)[2]}`;
  const views = {
    overview,
    campaigns: campaignsPage,
    candidates: candidatesPage,
    evidence: evidencePage,
    evolution: evolutionPage,
  };
  app.innerHTML = shell(views[state.route]());
  unmountWorkbench =
    state.route === "overview" ? mountWorkbench(app, state.arm) : undefined;
  setNav(false, false);
  if (focusId) document.getElementById(focusId)?.focus({ preventScroll: true });
}
function setNav(open, moveFocus = true) {
  const mobile = matchMedia("(max-width: 760px)").matches;
  const isOpen = mobile && open;
  const sidebar = app.querySelector(".sidebar"),
    page = app.querySelector(".page-shell"),
    toggle = app.querySelector('[data-action="toggle-nav"]');
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
  footer = closeButton(),
  eyebrow = "RECORDED EXPERIMENT",
) {
  if (!dialog.open) returnFocus = document.activeElement;
  dialog.innerHTML = `<div class="dialog-head"><div><div class="eyebrow">${e(eyebrow)}</div><h2 id="dialog-title">${e(title)}</h2></div><button class="icon-button" data-action="close-dialog" aria-label="Close dialog" autofocus>${icon("close")}</button></div><div class="dialog-body">${body}</div><div class="dialog-bottom">${footer}</div>`;
  if (!dialog.open) dialog.showModal();
  dialog.scrollTop = 0;
}
function candidateDialog(id) {
  const c = experiment.candidates.find((c) => c.id === id);
  if (!c) return;
  const r = result(c);
  const refs = experiment.evidence.filter(
    (record) =>
      record.candidateId === c.id &&
      (!record.arm || record.arm === "shared" || record.arm === state.arm),
  );
  openDialog(
    c.id,
    `<div class="detail-header"><span class="compound-glyph">${icon("molecule")}</span><div><h3>${e(arm().label)} · ${r?.shortlisted ? `Final rank ${r.rank}` : "Not in final shortlist"}</h3><p>Assay ${e(c.assayId)} · ${e(c.scaffold)}</p></div></div><div class="detail-metrics"><div class="detail-metric"><span>Historical IC50</span><strong>${number(c.ic50Nm, 1)} <small>nM</small></strong><small>Public input, not a hidden label</small></div><div class="detail-metric"><span>Qualified CNNscore median</span><strong>${number(r?.cnnMedian, 6)}</strong><small>Pose score · not biological activity</small></div><div class="detail-metric"><span>Two-seed valid poses</span><strong>${r ? `${r.validRepeats} / 2` : "Not computed"}</strong><small>${r ? `${r.completedRepeats} completed seed jobs` : "No score imputed"}</small></div></div><dl class="metadata"><dt>Energy median</dt><dd>${number(r?.energyMedian, 5)} kcal/mol</dd><dt>CNNscore seed range</dt><dd>${number(r?.cnnRange, 6)}</dd><dt>Activity IDs</dt><dd>${e(c.activityIds.join(", "))}</dd><dt>Document IDs</dt><dd>${e(c.documentIds.join(", "))}</dd></dl><div class="detail-section"><h4>Recorded seed jobs</h4>${r?.jobs?.length ? `<pre class="code-block">${e(JSON.stringify(r.jobs, null, 2))}</pre>` : "<p>No computational job result in this arm.</p>"}</div><div class="detail-section"><h4>Evidence references</h4>${refs.map((record) => `<button class="source-row" data-action="evidence" data-id="${e(record.id)}"><span>${e(record.title)}<small class="source-path">${e(record.source)}</small></span>${icon("arrow")}</button>`).join("")}</div><div class="detail-section"><h4>Recorded SMILES</h4><pre class="code-block">${e(c.smiles)}</pre></div><p class="detail-notice">A completed job, a valid pose and a biological result are different outcomes. Official affinity pass thresholds remain uncalibrated.</p>`,
    closeButton(),
    `CANDIDATE / ${state.arm}`,
  );
}
function evidenceDialog(id) {
  const record = experiment.evidence.find((r) => r.id === id);
  if (!record) return;
  openDialog(
    record.title,
    `<dl class="metadata"><dt>Compound</dt><dd>${e(record.candidateId)}</dd><dt>Arm</dt><dd>${e(record.arm || "Shared historical input")}</dd><dt>Evidence kind</dt><dd>${kindLabel(record.kind)}</dd><dt>Source artifact</dt><dd class="mono source-path">${e(record.source)}</dd></dl><div class="detail-section"><h4>Interpretation</h4><p>${e(record.detail)}</p></div><div class="detail-section"><h4>Recorded fields</h4><pre class="code-block">${e(JSON.stringify(record.record, null, 2))}</pre></div>`,
    button(
      "Back to compound",
      "candidate",
      "arrow",
      "",
      `data-id="${e(record.candidateId)}"`,
    ) + closeButton(),
    `EVIDENCE / ${record.id}`,
  );
}
function sourcesDialog() {
  openDialog(
    "Experiment provenance",
    `<div class="message-box">Imported from completed local experiment artifacts. This snapshot preserves recorded result values; it does not certify current process health.</div><dl class="metadata"><dt>Comparison</dt><dd>${e(experiment.id)}</dd><dt>Readout generated</dt><dd>${time(experiment.generatedAt)}</dd><dt>Target / assay</dt><dd>${e(experiment.target)} / ${e(experiment.assayId)}</dd><dt>Model / effort</dt><dd>${e(experiment.model)} / ${e(experiment.effort)}</dd><dt>Artifact audit</dt><dd>${experiment.audit.passed ? "Passed" : "Not confirmed"} · not a biological validation</dd><dt>Protocol SHA-256</dt><dd class="mono source-path">${e(experiment.protocolHash)}</dd></dl><div class="detail-section"><h4>Source manifest</h4><p>Paths are relative to the repository root. Hashes identify the exact imported files.</p><p>${e(experiment.audit.sourceHashScope)}</p>${experiment.sources.map((source) => `<div class="manifest-row"><strong class="mono">${e(source.path)}</strong><small>${source.verification === "recorded_binding" ? "Recorded hash binding checked" : "Local import root · import hash only"}</small><small class="mono">${e(source.sha256)}</small></div>`).join("")}</div>`,
    button("Export snapshot", "export", "download") + closeButton(),
  );
}
function protocolDialog() {
  openDialog(
    "Frozen protocol & actual tools",
    `<div class="message-box">${e(experiment.protocolId)} · 20 candidates · final shortlist of 5 per arm</div><div class="protocol-list"><div class="protocol-item"><strong>GNINA 1.3.3 + PoseBusters 0.6.5</strong><p>Docking and rescoring with seeds 17 and 43, plus pose-validity checks. RDKit 2025.9.6 supplies molecular features. Boltz-2 and Nesso-1 were not run in this comparison.</p></div><div class="protocol-item"><strong>Equal maximum opportunity</strong><p>8 online candidates / 16 physical jobs per arm; at most 4 candidates per batch. H0 and H1 allow up to 6 Lab calls, 300 seconds per call, and a soft total-token limit of 150,000. A call may cross the soft limit.</p></div><div class="protocol-item"><strong>Independent physical execution</strong><p>Each arm ran its own jobs without cross-arm result reuse. Terminal completion allows up to 5 additional candidates / 10 jobs, with no feedback to the Lab. This comparison used no terminal jobs.</p></div><div class="protocol-item"><strong>Separate scientific readouts</strong><p>CNNscore is a pose score. Empirical energy and historical IC50 remain separate. Affinity thresholds are uncalibrated; no official affinity pass rate is reported.</p></div></div><p class="detail-notice">Artifact audit passed. The single-pool, single-trajectory design does not establish statistical significance, generalization or biological efficacy.</p>`,
  );
}
function versionDialog() {
  openDialog(
    "H0 → H1: input presentation only",
    `<div class="tag-row">${pill("Evaluated", "green")}${pill("Not released", "orange")}</div><div class="detail-section"><h4>Recorded Meta proposal</h4><p>${e(experiment.meta.summary)}</p></div><pre class="code-block">- input_view_mode: expanded_json_v1\n+ input_view_mode: compact_json_v1\n\nOnly whitespace outside JSON strings is removed.\nKeys, order, values, arrays, source references and missingness are preserved.</pre><div class="detail-section"><h4>Scope</h4><p>The response interface, model settings, scientific evidence, tools, ranking objectives and budget accounting stay fixed. This is not a new model weight update.</p></div><dl class="metadata"><dt>Meta source</dt><dd class="source-path mono">${e(experiment.meta.source)}</dd><dt>Release authorized</dt><dd>${experiment.meta.releaseAuthorized ? "Yes" : "No"}</dd></dl>`,
    button("View release boundary", "gate", "lock") + closeButton(),
  );
}
function gateDialog() {
  openDialog(
    "Release is not authorized",
    `<div class="message-box">The recorded release_authorized value is false.</div><div class="detail-section"><h4>What was checked</h4><p>The proposed input compaction passed implementation checks and was evaluated as H1 in this development comparison. Artifact, schema and reversible-presentation checks do not prove every scientific statement is correct.</p></div><div class="detail-section"><h4>What remains unproven</h4><p>One trajectory per arm is insufficient to establish a stable improvement or broad generalization. The comparison does not automatically authorize release. Meta cost must remain part of the resource accounting.</p></div><div class="detail-section"><h4>Scientific memory</h4><p>H0 has 9 memory entries and H1 has 8; all remain pending in the live memory ledger. External statement review does not rewrite that status.</p></div>`,
  );
}
function exportData(selectedArm = false) {
  const payload = selectedArm
    ? {
        source: "recorded_experiment",
        comparisonId: experiment.id,
        asOf: experiment.generatedAt,
        arm: arm(),
        candidates: experiment.candidates.map((c) => ({
          ...c,
          results: c.results[state.arm]
            ? { [state.arm]: c.results[state.arm] }
            : {},
        })),
        sources: experiment.sources,
        limitations: experiment.limitations,
      }
    : { source: "recorded_experiment", ...experiment };
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = `RSI-Lab_${experiment.id}${selectedArm ? `_${state.arm}` : ""}.json`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
  toast(
    "Recorded results exported with source hashes and interpretation limits.",
  );
}
function selectArm(id) {
  if (!experiment.arms.some((a) => a.id === id)) return;
  state.arm = id;
  render();
}

document.addEventListener("click", (event) => {
  if (event.target.closest(".skip-link")) {
    event.preventDefault();
    document.querySelector("#main-content").focus();
    return;
  }
  const node = event.target.closest("[data-action]");
  if (!node || node.disabled) return;
  switch (node.dataset.action) {
    case "close-dialog":
      dialog.close();
      break;
    case "candidate":
      candidateDialog(node.dataset.id);
      break;
    case "evidence":
      evidenceDialog(node.dataset.id);
      break;
    case "sources":
      sourcesDialog();
      break;
    case "protocol":
      protocolDialog();
      break;
    case "version":
      versionDialog();
      break;
    case "gate":
      gateDialog();
      break;
    case "export":
      exportData();
      break;
    case "export-arm":
      exportData(true);
      break;
    case "select-arm":
      selectArm(node.dataset.arm);
      location.hash = "candidates";
      break;
    case "toggle-nav":
      setNav(!document.body.classList.contains("nav-open"));
      break;
    case "close-nav":
      setNav(false);
      break;
    case "evidence-tab":
      state.evidenceKind = node.dataset.kind;
      render();
      app
        .querySelector(
          `[data-action="evidence-tab"][data-kind="${CSS.escape(state.evidenceKind)}"]`,
        )
        ?.focus();
      break;
  }
});
document.addEventListener("input", (event) => {
  if (event.target.id !== "candidate-search") return;
  state.query = event.target.value;
  document.querySelector("#candidate-body").innerHTML = candidateRows();
  document.querySelector("#candidate-count").textContent =
    `Showing ${filteredCandidates().length} / ${experiment.poolSize} candidates`;
});
document.addEventListener("change", (event) => {
  if (event.target.id === "arm-select") selectArm(event.target.value);
  if (event.target.id === "scope-filter") {
    state.scope = event.target.value;
    render();
  }
  if (event.target.id === "sort-filter") {
    state.sort = event.target.value;
    render();
  }
});
dialog.addEventListener("close", () => {
  if (returnFocus?.isConnected) returnFocus.focus();
});
dialog.addEventListener("click", (event) => {
  if (event.target !== dialog) return;
  const rect = dialog.getBoundingClientRect();
  if (
    event.clientX < rect.left ||
    event.clientX > rect.right ||
    event.clientY < rect.top ||
    event.clientY > rect.bottom
  )
    dialog.close();
});
window.addEventListener("hashchange", () => {
  state.route = getRoute();
  render();
  scrollTo(0, 0);
  document.querySelector("#main-content").focus({ preventScroll: true });
});
window.addEventListener("keydown", (event) => {
  if (!document.body.classList.contains("nav-open")) return;
  if (event.key === "Escape") {
    setNav(false);
    return;
  }
  if (event.key !== "Tab") return;
  const nodes = [
      ...app.querySelectorAll(".sidebar a, .sidebar button:not(:disabled)"),
    ],
    first = nodes[0],
    last = nodes.at(-1);
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
});
matchMedia("(max-width: 760px)").addEventListener("change", () =>
  setNav(false, false),
);
render();
