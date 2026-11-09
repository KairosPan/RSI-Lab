import { icon } from "./icons.js";
import { labOrchestrationView } from "./lab-orchestration.js";
import { architectureComponents, architectureRows, architectureState, architectureCallHome } from "./architecture.js";

export const escapeTrace = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const acCategory = (call) => call.category || (["meta", "engineering_model", "lab", "model"].includes(call.kind) ? "model" : call.kind === "event" ? "event" : "tool");
export function traceCallsForAgent(calls, agentId, tab = "conversation", query = "") {
  const needle = query.trim().toLowerCase();
  return calls.filter((call) => {
    const own = call.agentId === agentId;
    const match = tab === "tools" ? acCategory(call) === "tool" && (own || call.requestedBy === agentId) : own && acCategory(call) !== "tool";
    return match && (!needle || [call.id, call.title, call.summary, call.status, call.kind, call.mode, call.action, call.tool?.name].filter(Boolean).join(" ").toLowerCase().includes(needle));
  });
}
const acRoles = architectureComponents;
const acActors = { meta: "Meta model", engineer: "Engineering executor", lab: "Lab model", tools: "Tool executor", controller: "Controller" };
const acLayout = { lab: [42, 70], meta: [592, 70], memory: [317, 258], literature: [42, 446], tools: [592, 446] };
const acEdges = [["meta", "lab", "Harness improvement"], ["lab", "memory", "Read / write"], ["meta", "memory", "Review / evidence"], ["literature", "memory", "Source evidence"], ["tools", "memory", "Results / receipts"]];
const acNodeWidth = 220, acNodeHeight = 144;
const acStatus = { not_connected: ["warning", "Not connected"], partial: ["warning", "Partial view"], succeeded: ["success", "Completed"], completed: ["success", "Completed"], failed_definite: ["warning", "Execution failed"], failed: ["warning", "Execution failed"], unknown: ["warning", "Unknown result"], blocked: ["warning", "Needs attention"], blocked_external: ["warning", "External reconciliation"], awaiting: ["muted", "Awaiting dependencies"], waiting: ["muted", "Awaiting dependencies"], queued: ["muted", "Awaiting dependencies"], idle: ["muted", "Not started"], not_started: ["muted", "Not started"], not_run: ["muted", "Not started"], recorded: ["muted", "Recorded"], ready: ["muted", "Ready"], prepared: ["muted", "Reserved"], dispatching: ["warning", "Dispatched · Unsettled"], cancelled: ["muted", "Cancelled"] };
const acNum = (n, digits = 0) => typeof n === "number" && Number.isFinite(n) ? n.toLocaleString("en-US", { maximumFractionDigits: digits }) : "—";
const acTime = (value, date = false) => {
  if (value === null || value === undefined) return "Time not recorded";
  const stamp = typeof value === "number" ? value * (value < 1e12 ? 1000 : 1) : value;
  return Number.isFinite(new Date(stamp).getTime()) ? new Date(stamp).toLocaleString("en-US", { timeZone: "America/New_York", ...(date ? { month: "2-digit", day: "2-digit" } : {}), hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }) : "Time not recorded";
};
const acBadge = (status) => { const [tone, label] = acStatus[status] || ["muted", status || "Not recorded"]; return `<span class="ac-status ac-${tone}"><i></i>${escapeTrace(label)}</span>`; };
const acJSON = (v) => typeof v === "string" ? v : JSON.stringify(v ?? null, null, 2);
const acFormatted = (v) => { if (typeof v !== "string") return acJSON(v); try { return JSON.stringify(JSON.parse(v), null, 2); } catch { return v; } };
const acMessageLabel = (label) => ({
  "完整调用输入 / 上下文": "Full call input / context",
  "已保存的请求输入": "Saved request input",
  "模型原始响应": "Original model response",
  "已归档结构化响应": "Archived structured response",
  "未取得最终响应": "No final response received",
})[label] || label;
const acRunTitle = (run) => run?.title || run?.id || "RSI-Lab run";
const acMode = (mode) => ({ discovery: "Review", disposition: "Resolution" })[mode] || mode;
const acOutcome = (status) => status ? `<span class="ac-outcome ${status === "rejected" ? "is-rejected" : ""}">${escapeTrace(({ applied: "Action accepted by controller", rejected: "Action rejected by controller" })[status] || status)}</span>` : "";
const acBtn = (action, label, glyph, extra = "") => `<button type="button" class="ac-icon-button" data-ac-action="${action}" aria-label="${label}" title="${label}" ${extra}>${icon(glyph)}</button>`;
const acSaved = { agent: "lab", tab: "orchestration", source: "none", positions: null };

export function agentCanvasPage() {
  return `<section class="agent-observatory" aria-label="System architecture canvas">
    <div class="ac-heading"><div><div class="eyebrow">RSI-LAB / SYSTEM OBSERVATORY</div><h1>Inside RSI-Lab<span>.</span></h1><p>Explore the harnesses, shared memory and tools behind each recorded run.</p></div><div class="ac-heading-actions"><label class="ac-source-label">Source<select id="agent-trace-source" aria-label="Source"><option value="none" ${acSaved.source === "none" ? "selected" : ""}>No run selected</option><option value="recorded" ${acSaved.source === "recorded" ? "selected" : ""}>Imported snapshot</option><option value="ledger" ${acSaved.source === "ledger" ? "selected" : ""}>Connect ledger</option></select></label><button type="button" class="btn" data-ac-action="refresh">${icon("refresh")}Refresh</button><button type="button" class="btn ac-export" data-ac-action="export">${icon("download")}Export index</button></div></div>
    <div class="ac-run-strip"><span class="ac-run-glyph">${icon("flask")}</span><div class="ac-run-copy"><strong data-ac-run>No run selected</strong><span data-ac-source>Select a source to inspect your own run</span></div><div class="ac-run-stats" data-ac-stats></div></div>
    <div class="ac-alert" data-ac-alert role="status" hidden></div>
    <div class="ac-workspace">
      <div class="ac-board"><div class="ac-board-header"><div>${icon("branch")}<strong>System architecture</strong><span>5 COMPONENTS</span></div><span class="ac-readonly">${icon("lock")}Read-only</span></div>
        <div class="ac-viewport" tabindex="0" role="region" aria-label="Draggable and zoomable architecture graph" aria-describedby="ac-canvas-help"><div class="ac-world"><div class="ac-lane ac-lane-harness"><span>HARNESSES / Task execution & improvement</span></div><div class="ac-lane ac-lane-capabilities"><span>TOOLS / Sources & computation</span></div><svg class="ac-edges" viewBox="0 0 850 640" width="850" height="640" aria-hidden="true"><defs><marker id="ac-arrowhead" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M1 1 9 5 1 9" fill="none" stroke="currentColor" stroke-width="1.5"/></marker></defs><g data-ac-edges></g></svg><div data-ac-nodes></div></div>
        <div class="ac-canvas-tip">${icon("info")}Select a component to inspect its records</div><div class="ac-controls">${acBtn("zoom-out", "Zoom out", "minus")}<output data-ac-zoom>100%</output>${acBtn("zoom-in", "Zoom in", "plus")}<i></i>${acBtn("fit", "Fit canvas (0)", "fit")}${acBtn("reset", "Reset layout", "refresh")}</div></div>
        <div class="ac-board-footer"><span><i></i>Architecture relationships · Not an execution sequence</span><span id="ac-canvas-help">Drag to pan · + / − to zoom</span></div>
        <div class="ac-agent-picker" aria-label="Select an architecture component"></div>
        <button type="button" class="ac-infrastructure" data-ac-select="controller"><span class="ac-infrastructure-icon">${icon("settings")}</span><span><strong>Runtime infrastructure</strong><small>Controller · Scheduling · Budget · Validation · Recovery</small></span><span data-ac-infra-count>Events</span>${icon("arrow")}</button>
        <div class="ac-activity-head"><span>${icon("clock")}Recent activity</span><small>America/New_York</small></div><div class="ac-activity" data-ac-activity></div>
      </div>
      <button class="ac-inspector-backdrop" data-ac-action="expand" aria-label="Close expanded view" hidden></button>
      <aside class="ac-inspector" aria-label="Component details"><div class="ac-inspector-heading" data-ac-heading></div><div class="ac-tabs" role="tablist" aria-label="Component records"></div>
      <div id="ac-tab-panel" role="tabpanel" class="ac-tab-panel"><div class="ac-call-browser"><label class="ac-search">${icon("search")}<input id="ac-call-search" type="search" placeholder="Search records, actions or call IDs…" aria-label="Search this node’s records"></label><div class="ac-call-list" data-ac-calls></div></div><div class="ac-detail" data-ac-detail aria-live="polite"></div></div><div class="ac-inspector-footer">${icon("lock")}Source records · No simulated conversations</div></aside>
    </div><div class="ac-bottom-note"><span>${icon("layers")}Harnesses are modules · Model calls appear inside them</span><span data-ac-updated>Loading…</span></div><div class="ac-toast" role="status" hidden></div>
  </section>`;
}

export function mountAgentCanvas(root) {
  const host = root.querySelector(".agent-observatory");
  if (!host) return () => {};
  const $ = (sel) => host.querySelector(sel);
  const viewport = $(".ac-viewport"), world = $(".ac-world"), panel = $(".ac-inspector");
  const abort = new AbortController();
  const on = (target, type, fn, options = {}) => target.addEventListener(type, fn, { ...options, signal: abort.signal });
  let index = null, source = acSaved.source, selected = acSaved.agent, tab = acSaved.tab, query = "", callId = null;
  let disposed = false, indexRequest, detailRequest, generation = 0, detailGeneration = 0, detail = null, expanded = false, returnFocus;
  let poll, toastTimer, loading = false, drag;
  const cache = new Map();
  const positions = structuredClone(acSaved.positions || acLayout);
  const camera = { x: 0, y: 0, scale: 1 };
  const component = (id = selected) => acRoles[id];
  const sections = () => component().sections;
  const currentSection = () => sections().find((section) => section.id === tab) || sections()[0];
  const componentState = (id = selected) => source === "none" ? { status: "unknown", label: "No run selected", note: "Select an imported snapshot or connect your own ledger to inspect recorded work.", countLabel: "—" } : architectureState(index, id);
  const componentBadge = (id = selected) => { const state = componentState(id); const tone = ["unknown", "not_connected", "partial", "awaiting_capability", "blocked", "awaiting_prerequisites", "blocked_external"].includes(state.status) ? "warning" : "muted"; return `<span class="ac-status ac-${tone}"><i></i>${escapeTrace(state.label)}</span>`; };
  const allCalls = () => index?.calls || [];
  const serviceRecords = () => currentSection().type === "records" ? architectureRows(index, selected, tab) : null;
  let serviceLimit = 30;
  const visibleCalls = () => architectureRows(index, selected, tab).filter((call) => !query.trim() || [call.id, call.title, call.summary, call.status, call.kind, call.mode, call.action, call.tool?.name].filter(Boolean).join(" ").toLowerCase().includes(query.trim().toLowerCase())).sort((a, b) => (b.sequence ?? b.createdSeq ?? 0) - (a.sequence ?? a.createdSeq ?? 0) || String(b.createdAt || "").localeCompare(String(a.createdAt || "")));
  const announce = (text) => { $(".ac-toast").textContent = text; $(".ac-toast").hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => { $(".ac-toast").hidden = true; }, 2600); };
  const alert = (text = "") => { $("[data-ac-alert]").hidden = !text; $("[data-ac-alert]").textContent = text; };
  const safeUrl = (url) => { const parsed = new URL(url, location.href); if (parsed.origin !== location.origin || !/^\/(data\/agent-trace\/calls|api\/trace\/calls)\/[a-zA-Z0-9_.-]+(?:\.json)?$/.test(parsed.pathname) || parsed.search) throw new Error("Invalid record address"); return parsed.href; };
  function transform() { world.style.transform = `translate(${camera.x}px,${camera.y}px) scale(${camera.scale})`; $("[data-ac-zoom]").textContent = `${Math.round(camera.scale * 100)}%`; viewport.style.backgroundSize = `${22 * camera.scale}px ${22 * camera.scale}px`; viewport.style.backgroundPosition = `${camera.x}px ${camera.y}px`; }
  function fit() { const points = Object.values(positions); const minX = Math.min(...points.map((p) => p[0])) - 28, minY = Math.min(...points.map((p) => p[1])) - 35; const w = Math.max(...points.map((p) => p[0])) + acNodeWidth + 28 - minX, h = Math.max(...points.map((p) => p[1])) + acNodeHeight + 28 - minY; camera.scale = Math.max(.32, Math.min(1.2, (viewport.clientWidth - 36) / w, (viewport.clientHeight - 65) / h)); camera.x = (viewport.clientWidth - w * camera.scale) / 2 - minX * camera.scale; camera.y = (viewport.clientHeight - h * camera.scale) / 2 - minY * camera.scale - 12; transform(); }
  function zoom(factor, x = viewport.clientWidth / 2, y = viewport.clientHeight / 2) { const old = camera.scale; camera.scale = Math.max(.3, Math.min(2, old * factor)); camera.x = x - (x - camera.x) * camera.scale / old; camera.y = y - (y - camera.y) * camera.scale / old; transform(); }
  function drawGraph() {
    const descriptions = { lab: index?.labOrchestration ? "Orchestration, dependencies & decisions" : "Task policy, context & decisions", meta: "Review, trials & capability building", memory: "Facts, hypotheses & open issues", literature: "Search and citation capabilities", tools: "Evaluation, diagnostics & acceptance" };
    $("[data-ac-nodes]").innerHTML = Object.entries(acLayout).map(([id]) => {
      const role = component(id), state = componentState(id);
      return `<button type="button" class="ac-node ${selected === id ? "is-selected" : ""} ${id === "literature" ? "ac-node-unconnected" : ""}" data-ac-node="${id}" aria-pressed="${selected === id}" style="left:${positions[id][0]}px;top:${positions[id][1]}px"><i class="ac-node-port ac-port-left"></i><i class="ac-node-port ac-port-right"></i><span class="ac-node-top"><span class="ac-node-icon">${icon(role.icon)}</span><span><strong>${escapeTrace(role.name)}</strong><small>${["lab", "meta"].includes(id) ? "HARNESS" : id === "memory" ? "SHARED MEMORY" : "TOOL COMPONENT"}</small></span></span><span class="ac-node-description">${descriptions[id]}</span><span class="ac-node-foot">${componentBadge(id)}<span>${escapeTrace(state.countLabel)}</span></span></button>`;
    }).join("");
    drawEdges();
    $(".ac-agent-picker").innerHTML = Object.keys(acLayout).map((id) => `<button type="button" data-ac-select="${id}" class="${id === selected ? "is-selected" : ""}" aria-pressed="${id === selected}">${icon(component(id).icon)}${escapeTrace(component(id).name)}</button>`).join("");
    $(".ac-infrastructure").classList.toggle("is-selected", selected === "controller");
    $(".ac-infrastructure").setAttribute("aria-pressed", String(selected === "controller"));
    $("[data-ac-infra-count]").textContent = index ? `${acNum(index.events?.length)} events` : "No snapshot";
  }
  function drawEdges() {
    $("[data-ac-edges]").innerHTML = acEdges.map(([from, to, label]) => {
      const [sx, sy] = positions[from], [tx, ty] = positions[to];
      let x1, y1, x2, y2, path;
      if (Math.abs(sx - tx) < 60) { const below = ty > sy; x1 = sx + acNodeWidth / 2; y1 = sy + (below ? acNodeHeight : 0); x2 = tx + acNodeWidth / 2; y2 = ty + (below ? 0 : acNodeHeight); path = `M${x1},${y1} C${x1},${(y1 + y2) / 2} ${x2},${(y1 + y2) / 2} ${x2},${y2}`; }
      else { const right = tx > sx; x1 = sx + (right ? acNodeWidth : 0); y1 = sy + acNodeHeight / 2; x2 = tx + (right ? 0 : acNodeWidth); y2 = ty + acNodeHeight / 2; path = `M${x1},${y1} C${(x1 + x2) / 2},${y1} ${(x1 + x2) / 2},${y2} ${x2},${y2}`; }
      const active = selected === from || selected === to;
      return `<g class="${active ? "ac-edge-selected" : ""} ${from === "literature" ? "ac-edge-unconnected" : ""}"><path d="${path}"/><text x="${(x1 + x2) / 2}" y="${(y1 + y2) / 2 - 9}" text-anchor="middle">${label}</text></g>`;
    }).join("");
  }
  function renderHeader() {
    const r = component(), state = componentState();
    const vinaCount = architectureRows(index, "tools", "executions").filter((call) => call.kind === "docking_vina_batch").length;
    const scopeHighlight = source === "none" ? "Select a source to inspect this component" : !index ? "Source snapshot unavailable" : selected === "tools" ? vinaCount ? `${vinaCount} Vina calls recorded in this run.` : "No Vina calls in this run." : selected === "memory" ? "Partial view · Evidence references and open issues" : selected === "literature" ? "Literature search and citation following: not connected" : selected === "meta" ? "Engineering is an on-demand backend of this harness" : selected === "controller" ? "Code infrastructure · Supports all components" : index?.labOrchestration?.label || "Orchestration snapshot unavailable";
    $("[data-ac-heading]").innerHTML = `<div class="ac-inspector-label"><span>${selected === "controller" ? "RUNTIME INFRASTRUCTURE" : "COMPONENT INSPECTOR"}</span>${acBtn("expand", expanded ? "Close expanded view" : "Expand details", expanded ? "close" : "fit")}</div><div class="ac-agent-identity"><span class="ac-selected-icon">${icon(r.icon)}</span><div><h2>${escapeTrace(r.name)}</h2><p>${escapeTrace(r.subtitle)}</p></div>${componentBadge()}</div><p class="ac-agent-description">${escapeTrace(r.description)}</p><div class="ac-scope-highlight">${icon("info")}${escapeTrace(scopeHighlight)}</div><details class="ac-component-scope"><summary>${icon("info")}Scope & capability status ${icon("down")}</summary><p>${escapeTrace(state.note)}</p></details>`;
    $(".ac-tabs").classList.toggle("has-many", sections().length > 3);
    $(".ac-tabs").innerHTML = sections().map((section) => { const count = architectureRows(index, selected, section.id).length; return `<button id="ac-tab-${section.id}" type="button" role="tab" data-ac-section="${section.id}" aria-controls="ac-tab-panel" aria-selected="${section.id === tab}" tabindex="${section.id === tab ? 0 : -1}">${escapeTrace(section.label)}${["calls", "records"].includes(section.type) ? `<span>${index ? acNum(count) : "—"}</span>` : ""}</button>`; }).join("");
    const records = serviceRecords();
    $("#ac-call-search").placeholder = records ? "Search records, IDs or evidence hashes…" : "Search records, actions or call IDs…";
    $("#ac-tab-panel").setAttribute("aria-labelledby", `ac-tab-${tab}`);
    $(".ac-call-browser").hidden = ["cost", "overview"].includes(currentSection().type);
    $("[data-ac-calls]").hidden = Boolean(records);
  }
  function renderCalls() {
    const calls = visibleCalls();
    $("[data-ac-calls]").innerHTML = calls.map((c) => `<button class="ac-call ${c.id === callId ? "is-selected" : ""}" type="button" data-ac-call="${escapeTrace(c.id)}" aria-pressed="${c.id === callId}"><span class="ac-call-marker">${icon(acCategory(c) === "tool" ? "terminal" : "file")}</span><span class="ac-call-copy"><strong>${escapeTrace(c.title || c.action || c.kind || c.id)}</strong><small>${acTime(c.createdAt)}<i>·</i>${escapeTrace(acMode(c.mode) || (acCategory(c) === "tool" ? "Tool execution" : "Model calls"))}</small></span>${acBadge(c.status)}</button>`).join("") || `<div class="ac-no-calls">${query ? "No matching records" : loading ? "Loading…" : "No records of this type for this node"}</div>`;
  }
  function renderServiceRecords() {
    if (source === "none") { noSourceDetail(); return; }
    const needle = query.trim().toLowerCase();
    const rows = [...(serviceRecords() || [])].filter((row) => !needle || JSON.stringify(row).toLowerCase().includes(needle)).reverse();
    const note = selected === "controller" ? "Recorded control events with timestamps, types and payload hashes. These are infrastructure events, not model conversations." : tab === "issues" ? "Open research issues and recorded hypotheses. These remain hypotheses, not validated scientific conclusions." : "Recorded facts and source references. This is a partial memory view; hashes point to evidence, not a complete working-memory implementation.";
    $("[data-ac-detail]").innerHTML = `<div class="ac-section-title">${escapeTrace(currentSection().label.toUpperCase())}</div><p class="ac-service-note">${note}</p><div class="ac-service-records">${rows.slice(0, serviceLimit).map((row) => `<details><summary><span><strong>${escapeTrace(row.kind || row.scope || row.id)}</strong><small>${row.createdAt ? `${acTime(row.createdAt, true)} ET` : escapeTrace(row.status || "Recorded")}</small></span>${icon("down")}</summary><pre>${escapeTrace(acJSON(row))}</pre></details>`).join("") || '<div class="ac-no-calls">No matching records</div>'}</div>${rows.length > serviceLimit ? `<button class="btn ac-load-more" type="button" data-ac-action="more-events">Show more (${Math.min(serviceLimit, rows.length)} / ${rows.length})</button>` : ""}`;
  }
  function renderOverview() {
    if (source === "none") { noSourceDetail(); return; }
    if (selected === "lab") { $("[data-ac-detail]").innerHTML = labOrchestrationView(index); return; }
    if (!index) { $("[data-ac-detail]").innerHTML = '<div class="ac-empty"><h3>Source unavailable</h3><p>Connect a valid source snapshot to inspect this component.</p></div>'; return; }
    const sources = architectureRows(index, "literature", "frozen_sources");
    $("[data-ac-detail]").innerHTML = `<div class="ac-capability-view"><div class="ac-section-title">LITERATURE TOOL CALLING</div><h3>Literature retrieval is not connected.</h3><p>This run has no connected literature-search or citation-following adapter. The architecture includes the component; this trace does not establish that capability.</p><div class="ac-capability-row"><span>${icon("search")}Literature search</span>${acBadge("not_connected")}</div><div class="ac-capability-row"><span>${icon("book")}Citation follow-up</span>${acBadge("not_connected")}</div><div class="ac-capability-row"><span>${icon("file")}Frozen source reader</span><strong>${index ? `${sources.length} recorded reads` : "No snapshot"}</strong></div><p class="ac-service-note">Frozen source reads deliver previously saved files. They do not search papers, follow citations or independently answer scientific questions.</p><button type="button" class="btn" data-ac-section="frozen_sources">${icon("arrow")}Inspect frozen source reads</button></div>`;
  }
  function noSourceDetail() {
    $("[data-ac-detail]").innerHTML = '<div class="ac-empty"><h3>No run selected</h3><p>Explore the architecture, then choose an imported snapshot or connect your own ledger from the Source menu.</p></div>';
  }
  function emptyDetail() {
    if (source === "none") { noSourceDetail(); return; }
    if (currentSection().type === "overview") { renderOverview(); return; }
    const explanation = !index ? "A valid source snapshot is required to inspect records." : query ? "Try an action name, status or call ID." : selected === "lab" ? "No corresponding Lab calls are recorded in this run. Open Orchestration to inspect the recorded prerequisites, queue and dependencies." : "No matching execution is attributed to this component in the source ledger. Recorded work remains available through its owning component.";
    $("[data-ac-detail]").innerHTML = `<div class="ac-empty">${icon(query ? "search" : "layers")}<h3>${query ? "No matching records" : !index ? "Source unavailable" : "No records in this run"}</h3><p>${explanation}</p></div>`;
  }
  function renderCost() {
    if (source === "none") { noSourceDetail(); return; }
    const metered = index && !["memory", "controller"].includes(selected);
    const own = architectureRows(index, selected, "cost"), models = own.filter((c) => acCategory(c) === "model");
    const used = own.reduce((sum, c) => sum + (typeof c.usage?.tokens === "number" ? c.usage.tokens : 0), 0);
    const unknown = models.filter((c) => c.usage?.tokens == null);
    const reserved = own.reduce((sum, c) => sum + (c.usage?.reservedTokens || 0), 0);
    const wall = own.reduce((sum, c) => sum + (typeof c.usage?.wallSeconds === "number" ? c.usage.wallSeconds : 0), 0);
    const tools = own.filter((c) => acCategory(c) === "tool");
    $("[data-ac-detail]").innerHTML = `<div class="ac-cost-view"><div class="ac-section-title">RECORDED COMPONENT COST</div><div class="ac-cost-grid"><div><span>Known tokens</span><strong>${metered ? acNum(used) : "—"}</strong></div><div><span>Model calls</span><strong>${index ? acNum(models.length) : "—"}</strong></div><div><span>Attributed tools</span><strong>${index ? acNum(tools.length) : "—"}</strong></div><div><span>Call wall time</span><strong>${metered ? acNum(wall, 1) : "—"}<small> s</small></strong></div></div>${unknown.length ? `<div class="ac-uncertainty">${icon("info")}${unknown.length} ${unknown.length === 1 ? "call has" : "calls have"} no token receipt. Totals include known usage only. Another ${acNum(reserved)} tokens remain reserved, not confirmed as spent.</div>` : ""}<p class="ac-cost-note">${["memory", "controller"].includes(selected) ? "This component is not separately metered. " : ""}${selected === "meta" ? "Includes Meta decisions, engineering model calls and explicitly linked tools. " : selected === "literature" ? "These costs belong to frozen source reads, not literature search. " : ""}Component views may link to the same tool receipts. Do not add component totals together; the run totals count each Effect once. Unrecorded work and host intervention are not measured as zero.</p><dl class="ac-provenance"><div><dt>Run</dt><dd>${escapeTrace(index?.run?.id || "—")}</dd></div><div><dt>Source</dt><dd>${source === "ledger" ? "Connected ledger · Read-only snapshot" : "Imported snapshot · Historical records"}</dd></div><div><dt>Snapshot time</dt><dd>${acTime(index?.run?.updatedAt, true)} ET</dd></div><div><dt>Cost in USD</dt><dd>Not recorded</dd></div></dl><button class="btn" type="button" data-ac-action="export-agent">${icon("download")}Export component index</button></div>`;
  }
  function messageBlock(message, n) {
    const content = acJSON(message.content ?? message.text ?? ""), role = message.role || "record";
    const roleName = acMessageLabel(message.label) || ({ system: "System instructions", user: "Input context", assistant: "Agent response", tool: "Tool response" })[role] || role;
    const open = role === "assistant" || role === "tool" || (detail?.messages?.length === 1);
    return `<details class="ac-message ac-message-${role === "assistant" ? "assistant" : "input"}" ${open ? "open" : ""}><summary><span>${icon(role === "assistant" ? "branch" : "file")}<strong>${escapeTrace(roleName)}</strong></span><small>${acNum(content.length)} chars ${icon("down")}</small></summary><div class="ac-message-body"><div class="ac-message-actions"><span>${message.truncated ? "Truncated preview · See source file" : message.redacted ? "Full structure · Sensitive content redacted" : message.format === "json" ? "Full content · Formatted JSON" : "Complete record"}</span><button type="button" data-ac-copy="${n}">Copy</button></div><pre>${escapeTrace(acFormatted(content))}</pre></div></details>`;
  }
  function renderDetail(record) {
    detail = record;
    const c = allCalls().find((item) => item.id === callId) || record;
    const messages = Array.isArray(record.messages) ? record.messages : [];
    const tool = record.tool;
    const usage = record.usage || c.usage || {};
    const truncation = Object.entries(tool?.truncation || {}).filter(([, value]) => value.truncated);
    const toolError = record.error || tool?.error;
    const files = record.artifacts || record.provenance?.artifacts || [];
    const artifactRows = Array.isArray(files) ? files : [];
    const home = architectureCallHome(c);
    $("[data-ac-detail]").innerHTML = `<div class="ac-record-heading"><div><span class="ac-section-title">${acCategory(c) === "tool" ? "TOOL EXECUTION" : "CONVERSATION"}</span>${acBadge(record.status || c.status)}</div><p class="ac-record-path">${escapeTrace(home ? component(home.id)?.name || home.id : "Unmapped component")} / ${escapeTrace(acActors[c.agentId] || c.agentId)}</p><h3>${escapeTrace(c.title || c.kind || "Call record")}</h3><code>${escapeTrace(record.id || callId)}</code><div class="ac-record-metrics"><span>${icon("clock")}${acNum(usage.wallSeconds, 2)} s</span><span>${icon("cpu")}${usage.tokens == null ? "Token usage not recorded" : `${acNum(usage.tokens)} tokens`}</span>${c.mode ? `<span>${escapeTrace(acMode(c.mode))}</span>` : ""}${c.model ? `<span>${escapeTrace(c.model)}</span>` : ""}<span>${acTime(c.createdAt, true)} ET</span></div></div>
      ${(record.status || c.status) === "unknown" ? '<div class="ac-uncertainty">'+icon("info")+'The result of this call is unknown. No completion receipt is confirmed; resource reservations and reconciliation remain open.</div>' : ""}
      ${tab === "engineering" ? '<div class="ac-record-scope">Engineering model delivery inside Meta-Harness. Code execution and acceptance are separate tool receipts; this model response alone does not establish acceptance.</div>' : ""}
      ${c.kind === "frozen_local_source_retrieval" ? '<div class="ac-record-scope">Frozen local source read. This is not a literature search or citation traversal.</div>' : ""}
      ${acOutcome(record.outcomeStatus || c.outcomeStatus)}
      ${c.summary ? `<details class="ac-call-summary"><summary>Action summary ${icon("down")}</summary><p>${escapeTrace(c.summary)}</p></details>` : ""}
      ${(record.actions || []).filter((action) => action.error).map((action) => `<div class="ac-uncertainty"><strong>Action not applied</strong><pre>${escapeTrace(acJSON(action.error))}</pre></div>`).join("")}
      <div class="ac-messages">${messages.map(messageBlock).join("")}${tool ? `<div class="ac-tool-banner">${icon("terminal")}<div><strong>${escapeTrace(tool.name || c.title || "Tool execution")}</strong><span>${escapeTrace(tool.command || "Arguments, results and logs from the execution receipt")}</span></div></div>${[ ["Arguments", tool.args ?? tool.arguments, "args"], ["Result", tool.result, "result"], ["Standard output", tool.stdout, "stdout"], ["Standard error", tool.stderr, "stderr"] ].filter(([,v]) => v !== null && v !== undefined && v !== "").map(([label, v, key]) => `<details class="ac-tool-section" ${label === "Result" ? "open" : ""}><summary><span>${label}${tool.truncation?.[key]?.truncated ? `<small>Truncated preview · ${acNum(tool.truncation[key].shownCharacters)} / ${acNum(tool.truncation[key].characters)} chars</small>` : ""}</span>${icon("down")}</summary><pre>${escapeTrace(acFormatted(v))}</pre></details>`).join("")}` : ""}${!messages.length && !tool ? `<p class="ac-detail-note">No model response or tool output is available in this record. ${escapeTrace(record.error || c.summary || "")}</p>` : ""}</div>
      ${toolError ? `<div class="ac-uncertainty"><strong>Execution error</strong><pre>${escapeTrace(acJSON(toolError))}</pre></div>` : ""}
      ${record.truncated || tool?.truncated || truncation.length ? '<div class="ac-uncertainty">This tool record includes truncated previews. Source hashes are retained; a preview is not the full delivery. Connect the ledger to download complete files below.</div>' : ""}
      ${(record.relatedCallIds || []).length ? `<div class="ac-related"><span>Related calls</span>${record.relatedCallIds.map((id) => allCalls().find((related) => related.id === id)).filter(Boolean).map((related) => `<button type="button" data-ac-jump="${escapeTrace(related.id)}">${icon("arrow")}${escapeTrace(acActors[related.agentId] || related.agentId)} · ${escapeTrace(related.title)}</button>`).join("")}</div>` : ""}
      <div class="ac-source-details"><details><summary>Source & original evidence ${icon("down")}</summary><pre>${escapeTrace(acJSON(record.provenance || {}))}</pre>${artifactRows.map((a) => `<div class="ac-artifact"><strong>${escapeTrace(a.name || a.label || a.sha256 || "Source file")}</strong><code>${escapeTrace(a.sha256 || a.ref || "")}</code>${source === "ledger" && /^\/api\/trace\/artifacts\/[a-zA-Z0-9_-]+\/[a-f0-9]{64}$/.test(a.url || a.downloadUrl || "") ? `<a href="${escapeTrace(a.url || a.downloadUrl)}" download>Download complete file ${icon("download")}</a>` : ""}</div>`).join("")}</details><button class="ac-detail-download" type="button" data-ac-action="download-call">${icon("download")}Download record JSON</button></div>`;
  }
  async function loadDetail(id) {
    detailRequest?.abort(); const seq = ++detailGeneration; detail = null;
    const c = allCalls().find((item) => item.id === id);
    if (!c) { emptyDetail(); return; }
    $("[data-ac-detail]").innerHTML = '<div class="ac-loading"><i></i>Loading source record…</div>';
    const key = source + ":" + id;
    if (cache.has(key)) { renderDetail(cache.get(key)); return; }
    detailRequest = new AbortController();
    try {
      const path = c.detailUrl || (source === "ledger" ? `/api/trace/calls/${encodeURIComponent(id)}` : `/data/agent-trace/calls/${encodeURIComponent(id)}.json`);
      const response = await fetch(safeUrl(path), { signal: detailRequest.signal, cache: "no-store" });
      if (!response.ok) throw new Error(`Could not load record (${response.status})`);
      const result = await response.json();
      if (result.id !== id) throw new Error("Returned record identity does not match");
      if (disposed || seq !== detailGeneration || callId !== id) return;
      cache.set(key, result); renderDetail(result);
    } catch (err) { if (err.name === "AbortError" || disposed || seq !== detailGeneration) return; $("[data-ac-detail]").innerHTML = `<div class="ac-empty">${icon("info")}<h3>Could not load this record</h3><p>${escapeTrace(err.message)}</p><button class="btn" data-ac-action="retry-detail" type="button">Retry</button></div>`; }
  }
  function showTab(next, keep = false) {
    tab = sections().some((section) => section.id === next) ? next : sections()[0].id;
    acSaved.tab = tab; detailRequest?.abort(); ++detailGeneration; detail = null;
    renderHeader();
    if (currentSection().type === "cost") { renderCost(); return; }
    if (currentSection().type === "overview") { callId = null; renderOverview(); return; }
    if (serviceRecords()) { callId = null; renderServiceRecords(); return; }
    const calls = visibleCalls(); if (!keep || !calls.some((c) => c.id === callId)) callId = calls[0]?.id || null;
    renderCalls(); if (callId) loadDetail(callId); else emptyDetail();
  }
  function chooseAgent(id, targetCall) {
    if (!acRoles[id]) return;
    const focusNode = document.activeElement?.hasAttribute("data-ac-node");
    selected = id; acSaved.agent = id; query = ""; serviceLimit = 30; $("#ac-call-search").value = "";
    if (targetCall) { callId = targetCall.id; tab = architectureCallHome(targetCall).section; }
    else { callId = null; tab = sections()[0].id; }
    drawGraph(); showTab(tab, Boolean(targetCall));
    if (focusNode) host.querySelector(`[data-ac-node="${id}"]`)?.focus({ preventScroll: true });
    if (matchMedia("(max-width: 1000px)").matches) panel.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
  }
  function renderIndex() {
    $("[data-ac-run]").textContent = acRunTitle(index.run);
    $("[data-ac-source]").textContent = source === "ledger" ? "Ledger snapshot · Refreshes every 10s · Not a heartbeat" : "Imported snapshot · Saved records";
    const models = allCalls().filter((c) => acCategory(c) === "model");
    const known = models.reduce((sum, c) => sum + (typeof c.usage?.tokens === "number" ? c.usage.tokens : 0), 0);
    const unknown = allCalls().filter((c) => c.status === "unknown").length;
    const toolCount = allCalls().filter((c) => acCategory(c) === "tool").length;
    $("[data-ac-stats]").innerHTML = `<div><strong>${acNum(models.length)}</strong><span>Model calls</span></div><div><strong>${acNum(toolCount)}</strong><span>Tool records</span></div><div><strong>${acNum(known)}</strong><span>Known tokens</span></div><div class="${unknown ? "ac-stat-warning" : ""}"><strong>${acNum(unknown)}</strong><span>Unresolved</span></div>`;
    $("[data-ac-updated]").textContent = `As of ${acTime(index.run?.updatedAt, true)} ET · ${acNum(index.run?.eventCount)} ledger events`;
    drawGraph(); renderHeader();
    const latest = [...allCalls()].sort((a,b) => (b.sequence ?? b.createdSeq ?? 0) - (a.sequence ?? a.createdSeq ?? 0) || String(b.createdAt || "").localeCompare(String(a.createdAt || ""))).slice(0, 4);
    $("[data-ac-activity]").innerHTML = latest.map((c) => `<button type="button" data-ac-jump="${escapeTrace(c.id)}"><span class="ac-activity-dot ${c.status === "unknown" || c.status === "failed_definite" ? "is-warning" : ""}"></span><span><strong>${escapeTrace(`${(architectureCallHome(c) ? component(architectureCallHome(c).id).name : "Unmapped record")} · ${acActors[c.agentId] || c.agentId}`)}<small>${acTime(c.createdAt)}</small></strong><span>${escapeTrace(c.title || c.summary || c.kind)}</span></span>${icon("arrow")}</button>`).join("") || '<p class="ac-no-calls">No activity recorded</p>';
    showTab(tab, true);
  }
  async function refresh(background = false) {
    if (source === "none") {
      indexRequest?.abort(); ++generation; detailRequest?.abort(); ++detailGeneration;
      loading = false; index = null; detail = null; callId = null; cache.clear();
      $("[data-ac-action=refresh]").disabled = false;
      $("[data-ac-run]").textContent = "No run selected";
      $("[data-ac-source]").textContent = "Select a source to inspect your own run";
      $("[data-ac-stats]").innerHTML = ""; $("[data-ac-activity]").innerHTML = '<p class="ac-no-calls">No source selected</p>';
      $("[data-ac-updated]").textContent = "No run data loaded";
      alert(); drawGraph(); renderHeader(); renderCalls(); noSourceDetail(); return;
    }
    if (loading && background) return;
    indexRequest?.abort(); const seq = ++generation; indexRequest = new AbortController(); loading = true;
    const button = $("[data-ac-action=refresh]"); button.disabled = true;
    if (!background) alert();
    try {
      const response = await fetch(source === "ledger" ? "/api/trace" : "/data/agent-trace/index.json", { signal: indexRequest.signal, cache: "no-store" });
      if (!response.ok) throw new Error(source === "ledger" ? "The ledger is unavailable. Configure RSI_LAB_RUN_DIR on the local server and retry." : "No imported snapshot is available. Import your own run with npm run import:trace, then retry.");
      const value = await response.json();
      if (!value.run || !Array.isArray(value.agents) || !Array.isArray(value.calls)) throw new Error("Unsupported record format");
      if (disposed || seq !== generation) return;
      const unchanged = background && index?.run?.eventCount === value.run.eventCount && index?.run?.updatedAt === value.run.updatedAt;
      index = value; alert(); if (!unchanged) { cache.clear(); renderIndex(); }
    } catch (err) {
      if (err.name === "AbortError" || disposed || seq !== generation) return;
      index = null; detailRequest?.abort(); ++detailGeneration; detail = null; callId = null;
      $("[data-ac-run]").textContent = "Source unavailable"; $("[data-ac-source]").textContent = "No simulated data substituted"; $("[data-ac-stats]").innerHTML = ""; $("[data-ac-activity]").innerHTML = ""; $("[data-ac-updated]").textContent = "No valid snapshot available";
      alert(err.message); drawGraph(); renderHeader(); renderCalls(); emptyDetail();
    } finally { if (seq === generation && !disposed) { loading = false; button.disabled = false; } }
  }
  function download(value, filename) { const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: "application/json" })); const a = document.createElement("a"); a.href = url; a.download = filename; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
  function expand() { expanded = !expanded; host.classList.toggle("ac-expanded", expanded); $(".ac-inspector-backdrop").hidden = !expanded; panel.setAttribute("role", expanded ? "dialog" : "complementary"); if (expanded) { returnFocus = document.activeElement; panel.setAttribute("aria-modal", "true"); } else panel.removeAttribute("aria-modal"); renderHeader(); if (expanded) panel.querySelector("button")?.focus(); else (returnFocus?.isConnected ? returnFocus : panel.querySelector("[data-ac-action=expand]"))?.focus(); }
  on(host, "click", (event) => {
    const button = event.target.closest("button"); if (!button) return;
    if (button.dataset.acNode && event.detail === 0) chooseAgent(button.dataset.acNode);
    if (button.dataset.acSelect) chooseAgent(button.dataset.acSelect);
    if (button.dataset.acSection) { query = ""; $("#ac-call-search").value = ""; serviceLimit = 30; showTab(button.dataset.acSection); }
    if (button.dataset.acCall) { callId = button.dataset.acCall; renderCalls(); loadDetail(callId); }
    if (button.dataset.loIssue) { chooseAgent("memory"); query = button.dataset.loIssue; $("#ac-call-search").value = query; showTab("issues"); }
    if (button.dataset.loEvidence) { chooseAgent("memory"); query = button.dataset.loEvidence; $("#ac-call-search").value = query; showTab("facts"); }
    if (button.dataset.loSection) { chooseAgent(button.dataset.loComponent || "lab"); showTab(button.dataset.loSection); }
    if (button.dataset.acJump) { const c = allCalls().find((c) => c.id === button.dataset.acJump); if (c && architectureCallHome(c)) chooseAgent(architectureCallHome(c).id, c); }
    if (button.dataset.acCopy !== undefined && detail) { const message = detail.messages[Number(button.dataset.acCopy)]; if (!navigator.clipboard) announce("Copy unavailable. Download the record instead."); else navigator.clipboard.writeText(acJSON(message.content ?? message.text)).then(() => announce("Source content copied")).catch(() => announce("Copy unavailable. Download the record instead.")); }
    switch (button.dataset.acAction) {
      case "more-events": serviceLimit += 30; renderServiceRecords(); break; case "refresh": refresh(); break; case "fit": fit(); break; case "reset": Object.assign(positions, structuredClone(acLayout)); drawGraph(); fit(); break;
      case "zoom-in": zoom(1.2); break; case "zoom-out": zoom(1/1.2); break; case "expand": expand(); break;
      case "retry-detail": if (callId) loadDetail(callId); break;
      case "download-call": if (detail) download(detail, `${callId}.json`); break;
      case "export": if (index) download(index, `${index.run.id}-trace-index.json`); else announce("No records to export"); break;
      case "export-agent": if (index) download({ run: index.run, component: component(), calls: architectureRows(index, selected, "cost"), facts: selected === "memory" ? index.facts : undefined, issues: selected === "memory" ? index.issues : undefined, events: selected === "controller" ? index.events : undefined, orchestration: selected === "lab" ? index.labOrchestration : undefined, note: "Call index. Download original conversations and logs from each call’s detail view." }, `${selected}-trace-index.json`); break;
    }
  });
  on($("#ac-call-search"), "input", (event) => { query = event.target.value; if (serviceRecords()) { serviceLimit = 30; renderServiceRecords(); return; } const rows = visibleCalls(); if (!rows.some((c) => c.id === callId)) { callId = rows[0]?.id || null; detailRequest?.abort(); ++detailGeneration; if (callId) loadDetail(callId); else emptyDetail(); } renderCalls(); });
  on($("#agent-trace-source"), "change", (event) => { source = event.target.value; acSaved.source = source; index = null; cache.clear(); detailRequest?.abort(); ++detailGeneration; callId = null; drawGraph(); renderHeader(); renderCalls(); emptyDetail(); refresh(); });
  on(viewport, "pointerdown", (event) => { if (event.button !== 0 || event.target.closest("[data-ac-action]")) return; const node = event.target.closest("[data-ac-node]"); drag = { id: node?.dataset.acNode, x: event.clientX, y: event.clientY, origin: node ? [...positions[node.dataset.acNode]] : [camera.x, camera.y], moved: false, pointerId: event.pointerId }; viewport.setPointerCapture(event.pointerId); });
  on(viewport, "pointermove", (event) => { if (!drag) return; const dx = event.clientX - drag.x, dy = event.clientY - drag.y; if (Math.hypot(dx, dy) > 4) drag.moved = true; if (!drag.moved) return; viewport.classList.add("is-dragging"); if (drag.id) { positions[drag.id] = [drag.origin[0] + dx / camera.scale, drag.origin[1] + dy / camera.scale]; const node = host.querySelector(`[data-ac-node="${drag.id}"]`); node.style.left = positions[drag.id][0] + "px"; node.style.top = positions[drag.id][1] + "px"; drawEdges(); } else { camera.x = drag.origin[0] + dx; camera.y = drag.origin[1] + dy; transform(); } });
  const endDrag = (event) => { if (!drag) return; const previous = drag; drag = null; viewport.classList.remove("is-dragging"); if (viewport.hasPointerCapture(previous.pointerId)) viewport.releasePointerCapture(previous.pointerId); if (event.type === "pointerup" && !previous.moved && previous.id) chooseAgent(previous.id); acSaved.positions = structuredClone(positions); };
  on(viewport, "pointerup", endDrag); on(viewport, "pointercancel", endDrag);
  on(viewport, "wheel", (event) => { if (!event.ctrlKey && !event.metaKey) return; event.preventDefault(); const box = viewport.getBoundingClientRect(); zoom(Math.exp(-event.deltaY * .005), event.clientX-box.left, event.clientY-box.top); }, { passive: false });
  on(host, "keydown", (event) => {
    if (expanded && event.key === "Escape") { event.preventDefault(); expand(); return; }
    if (expanded && event.key === "Tab") { const focusable = [...panel.querySelectorAll('button:not(:disabled), input, select, summary, a[href], [tabindex="0"]')].filter((el) => el.getClientRects().length); const first = focusable[0], last = focusable.at(-1); if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); } }
    if (event.target.matches('[role="tab"]') && ["ArrowLeft", "ArrowRight"].includes(event.key)) { event.preventDefault(); const tabs = sections().map((section) => section.id); const next = tabs[(tabs.indexOf(tab) + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length]; showTab(next); host.querySelector(`#ac-tab-${next}`).focus(); return; }
    if (event.target !== viewport) return;
    if (["+", "=", "-", "0", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) event.preventDefault();
    if (event.key === "0") fit(); else if (["+", "="].includes(event.key)) zoom(1.2); else if (event.key === "-") zoom(1/1.2); else { const delta = { ArrowLeft: [35,0], ArrowRight: [-35,0], ArrowUp: [0,35], ArrowDown: [0,-35] }[event.key]; if (delta) { camera.x += delta[0]; camera.y += delta[1]; transform(); } }
  });
  const resize = new ResizeObserver(() => { if (!drag) fit(); }); resize.observe(viewport);
  drawGraph(); renderHeader(); renderCalls(); $("[data-ac-detail]").innerHTML = '<div class="ac-loading"><i></i>No run selected</div>'; requestAnimationFrame(fit); refresh();
  poll = setInterval(() => { if (source === "ledger" && !document.hidden && !expanded) refresh(true); }, 10000);
  return () => { disposed = true; ++generation; ++detailGeneration; indexRequest?.abort(); detailRequest?.abort(); abort.abort(); resize.disconnect(); clearInterval(poll); clearTimeout(toastTimer); acSaved.positions = structuredClone(positions); };
}
