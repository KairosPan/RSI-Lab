import { icon } from "./icons.js";
import { experiment } from "./experiment-data.js";
import {
  runtimeNodes,
  runtimeEdges,
  demoRuntime,
  recordedRuntime,
  fetchRuntime,
  normalizeRuntime,
} from "./runtime.js";

const canvasPositions = {
  controller: [28, 132],
  lab: [290, 132],
  executor: [552, 132],
  gnina: [814, 38],
  posecheck: [814, 234],
  memory: [552, 408],
  meta: [290, 408],
  validator: [28, 408],
};
const canvasLabels = {
  running: "Running",
  queued: "Queued",
  idle: "Idle",
  error: "Error",
  offline: "Stale heartbeat",
  unknown: "Unknown",
  completed: "Completed",
};
const canvasState = {
  mode: "recorded",
  selected: "lab",
  tab: "overview",
  problems: false,
  tick: 0,
  playing: false,
  positions: structuredClone(canvasPositions),
};
const canvasEscape = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const canvasTime = (v) =>
  Number.isFinite(Date.parse(v))
    ? new Date(v).toLocaleTimeString("en-GB", {
        hour12: false,
        timeZone: "UTC",
      }) + " UTC"
    : "—";
const canvasBadge = (s, label) =>
  `<span class="runtime-status status-${s}"><i></i>${canvasEscape(label || canvasLabels[s] || "Unknown")}</span>`;
const canvasButton = (action, label, glyph, extra = "") =>
  `<button type="button" class="canvas-icon-btn" data-canvas="${action}" aria-label="${label}" title="${label}" ${extra}>${icon(glyph)}</button>`;

export function workbenchPage(armId = "H1") {
  return `<section class="runtime-workbench" aria-label="Experiment workbench">
    <div class="page-heading canvas-heading"><div><div class="eyebrow">LAB OBSERVATORY</div><h1>Experiment workbench<span class="heading-dot"></span></h1><p>Trace the completed experiment, from decisions to recorded evidence.</p></div><div class="monitor-source"><span class="source-caption">Data source</span><select id="runtime-source" aria-label="Data source"><option value="recorded" ${canvasState.mode === "recorded" ? "selected" : ""}>Recorded experiment</option><option value="live" ${canvasState.mode === "live" ? "selected" : ""}>Live telemetry</option><option value="demo" ${canvasState.mode === "demo" ? "selected" : ""}>Demo workflow</option></select><button class="btn" data-canvas="refresh">${icon("refresh")}Refresh</button></div></div>
    <div class="monitor-context"><div class="monitor-campaign">${icon("flask")}<strong data-runtime-campaign>${canvasEscape(experiment.id)}</strong><span class="context-divider"></span><span data-runtime-harness>Arm ${canvasEscape(armId)}</span></div><div class="monitor-connection" role="status" data-runtime-connection></div></div>
    <div class="monitor-summary" data-runtime-summary></div>
    <div class="canvas-workspace">
      <section class="canvas-board" aria-label="Component graph">
        <div class="canvas-toolbar"><div class="canvas-view-label">${icon("grid")}<strong>Component graph</strong><span class="canvas-count">08 COMPONENTS</span></div><button class="canvas-filter" data-canvas="problems" aria-pressed="${canvasState.problems}">${icon("filter")}Needs attention <span data-problem-count>1</span></button></div>
        <div class="graph-viewport" tabindex="0" role="region" aria-label="Interactive component canvas" aria-describedby="canvas-help">
          <div class="graph-world"><div class="graph-lane lane-main"><span>01 / LAB HARNESS</span><small>Campaign decisions and evidence</small></div><div class="graph-lane lane-meta"><span>02 / META HARNESS</span><small>Cross-campaign diagnosis and validation</small></div><svg class="graph-edges" width="1060" height="580" aria-hidden="true"><defs><marker id="canvas-arrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0 0 7 3.5 0 7" fill="none" stroke="currentColor"/></marker></defs><g data-edges></g></svg><div data-graph-nodes></div></div>
          <div class="canvas-map" aria-hidden="true"><span>WORKSPACE</span><svg viewBox="0 0 1060 580" data-mini-map></svg></div>
          <div class="canvas-controls">${canvasButton("zoom-out", "Zoom out", "minus")}<output data-zoom aria-label="Zoom level">100%</output>${canvasButton("zoom-in", "Zoom in", "plus")}<span></span>${canvasButton("fit", "Fit graph (0)", "fit")}${canvasButton("reset", "Reset layout", "refresh")}</div>
        </div>
        <div class="canvas-legend"><span><i class="legend-line"></i>Campaign messages</span><span><i class="legend-line dashed"></i>Cross-campaign handoff</span><span class="canvas-mobile-hint">Drag to pan · Choose a component below</span><span id="canvas-help">Drag nodes or canvas · + / − zoom · 0 fit</span></div>
      </section>
      <aside class="runtime-inspector" aria-label="Component details"><div class="inspector-header"><span class="eyebrow">INSPECTOR</span><label class="sr-only" for="runtime-node-select">Select a component</label><select id="runtime-node-select">${runtimeNodes.map((n) => `<option value="${n.id}" ${n.id === canvasState.selected ? "selected" : ""}>${canvasEscape(n.name)}</option>`).join("")}</select></div><div data-node-heading></div><div class="inspector-tabs" role="group" aria-label="Inspector view"><button data-canvas="tab" data-tab="overview" aria-pressed="true">Overview</button><button data-canvas="tab" data-tab="io" aria-pressed="false">Input / output</button></div><div data-node-detail></div><div class="inspector-foot" data-inspector-mode>${icon("lock")}Recorded results · Not a live heartbeat</div></aside>
    </div>
    <section class="runtime-events"><div class="runtime-events-head"><div>${icon("terminal")}<h2>Event log</h2><span data-event-source>Demo events</span></div><div><span class="event-poll" data-poll-label></span><button class="btn text" data-canvas="play">${icon("play")}Play demo</button></div></div><div data-runtime-events></div></section>
    <div class="runtime-note">${icon("info")}<span data-runtime-note>Recorded results are historical evidence, not a live heartbeat.</span></div>
  </section>`;
}

export function mountWorkbench(root, armId = "H1") {
  const host = root.querySelector(".runtime-workbench");
  if (!host) return () => {};
  const viewport = host.querySelector(".graph-viewport");
  const world = host.querySelector(".graph-world");
  const nodesHost = host.querySelector("[data-graph-nodes]");
  const select = (selector) => host.querySelector(selector);
  const camera = { x: 0, y: 0, scale: 1 };
  let snapshot =
    canvasState.mode === "recorded"
      ? recordedRuntime(experiment, armId)
      : canvasState.mode === "demo"
        ? demoRuntime(canvasState.tick)
        : null;
  let lastPayload = null;
  let connection =
    canvasState.mode === "recorded"
      ? "recorded"
      : canvasState.mode === "demo"
        ? "demo"
        : "waiting";
  let problem = "";
  let disposed = false;
  let fetching = false;
  let request;
  let generation = 0;
  let drag;
  let suppressClick = false;
  const listeners = new AbortController();
  const listen = (target, name, fn, options = {}) =>
    target.addEventListener(name, fn, { ...options, signal: listeners.signal });
  const unknownNodes = () =>
    runtimeNodes.map((n) => ({
      id: n.id,
      status: "unknown",
      detail: "No telemetry has been received for this component.",
      task: "—",
      metrics: [],
      events: [],
    }));
  const currentNodes = () => snapshot?.nodes || unknownNodes();
  const nodeState = (id) =>
    currentNodes().find((n) => n.id === id) ||
    unknownNodes().find((n) => n.id === id);
  const isProblem = (n) =>
    ["error", "offline", "unknown"].includes(n.status) ||
    (canvasState.mode === "recorded" && n.status === "queued");

  nodesHost.innerHTML = runtimeNodes
    .map(
      (n) =>
        `<button class="runtime-node" type="button" data-node="${n.id}" aria-pressed="false"><span class="node-port port-in"></span><span class="node-port port-out"></span><span class="node-top"><span class="node-icon">${icon(n.icon)}</span><span class="node-identity"><strong>${canvasEscape(n.name)}</strong><small>${canvasEscape(n.role)}</small></span></span><span class="node-detail"></span><span class="node-footer"><span class="node-state"></span><span class="node-meta"></span></span></button>`,
    )
    .join("");

  function transform() {
    world.style.transform = `translate(${camera.x}px, ${camera.y}px) scale(${camera.scale})`;
    select("[data-zoom]").textContent = `${Math.round(camera.scale * 100)}%`;
    viewport.style.backgroundSize = `${20 * camera.scale}px ${20 * camera.scale}px`;
    viewport.style.backgroundPosition = `${camera.x}px ${camera.y}px`;
    drawMap();
  }
  function fit() {
    const coordinates = Object.values(canvasState.positions);
    const minX = Math.min(...coordinates.map(([x]) => x)) - 28;
    const minY = Math.min(...coordinates.map(([, y]) => y)) - 38;
    const width = Math.max(...coordinates.map(([x]) => x)) + 236 - minX;
    const height = Math.max(...coordinates.map(([, y]) => y)) + 162 - minY;
    camera.scale = Math.max(
      0.1,
      Math.min(
        1.15,
        (viewport.clientWidth - 32) / width,
        (viewport.clientHeight - 70) / height,
      ),
    );
    camera.x =
      (viewport.clientWidth - width * camera.scale) / 2 - minX * camera.scale;
    camera.y =
      (viewport.clientHeight - height * camera.scale) / 2 -
      minY * camera.scale -
      10;
    transform();
  }
  function zoom(
    factor,
    x = viewport.clientWidth / 2,
    y = viewport.clientHeight / 2,
  ) {
    const old = camera.scale;
    camera.scale = Math.max(0.1, Math.min(1.8, old * factor));
    camera.x = x - ((x - camera.x) * camera.scale) / old;
    camera.y = y - ((y - camera.y) * camera.scale) / old;
    transform();
  }
  function drawMap() {
    select("[data-mini-map]").innerHTML =
      runtimeNodes
        .map((n) => {
          const [x, y] = canvasState.positions[n.id];
          return `<rect x="${x}" y="${y}" width="208" height="126" rx="12" class="map-node ${n.id === canvasState.selected ? "selected" : ""}"/>`;
        })
        .join("") +
      `<rect class="map-viewport" x="${-camera.x / camera.scale}" y="${-camera.y / camera.scale}" width="${viewport.clientWidth / camera.scale}" height="${viewport.clientHeight / camera.scale}" rx="8"/>`;
  }
  function drawLayout() {
    for (const n of runtimeNodes) {
      const node = nodesHost.querySelector(`[data-node="${n.id}"]`);
      const [x, y] = canvasState.positions[n.id];
      node.style.transform = `translate(${x}px, ${y}px)`;
    }
    select("[data-edges]").innerHTML = runtimeEdges
      .map((edge) => {
        const [sx, sy] = canvasState.positions[edge.from];
        const [tx, ty] = canvasState.positions[edge.to];
        const vertical =
          edge.from === "validator" ||
          (edge.from === "memory" && edge.to === "lab");
        const left = tx < sx;
        let x1 = sx + (left ? 0 : 208),
          y1 = sy + 63,
          x2 = tx + (left ? 208 : 0),
          y2 = ty + 63;
        let path;
        if (edge.from === "gnina" && edge.to === "posecheck") {
          x1 = sx + 104;
          y1 = sy + 126;
          x2 = tx + 104;
          y2 = ty;
          path = `M ${x1} ${y1} C ${x1} ${y1 + 30}, ${x2} ${y2 - 30}, ${x2} ${y2}`;
        } else if (vertical) {
          x1 = sx + 104;
          y1 = sy;
          x2 = tx + 104;
          y2 = ty + 126;
          path = `M ${x1} ${y1} C ${x1} ${y1 - 62}, ${x2} ${y2 + 62}, ${x2} ${y2}`;
        } else {
          const bend = Math.max(30, Math.abs(x2 - x1) * 0.5);
          path = `M ${x1} ${y1} C ${x1 + (left ? -bend : bend)} ${y1}, ${x2 + (left ? bend : -bend)} ${y2}, ${x2} ${y2}`;
        }
        const isMeta =
          edge.kind === "cross-campaign" ||
          edge.from === "meta" ||
          edge.to === "meta" ||
          edge.from === "validator";
        const selected =
          edge.from === canvasState.selected ||
          edge.to === canvasState.selected;
        return `<g class="graph-edge ${isMeta ? "meta-edge" : ""} ${selected ? "edge-selected" : ""}"><path d="${path}" marker-end="url(#canvas-arrow)"/><title>${canvasEscape(edge.label)}</title></g>`;
      })
      .join("");
    drawMap();
  }
  function detailView() {
    const meta = runtimeNodes.find((n) => n.id === canvasState.selected);
    const n = nodeState(meta.id);
    select("#runtime-node-select").value = n.id;
    select("[data-node-heading]").innerHTML =
      `<div class="inspector-node-title"><span class="inspector-icon">${icon(meta.icon)}</span><div><h2>${canvasEscape(meta.name)}</h2><span>${canvasEscape(meta.role)}</span></div></div><div class="inspector-node-status">${canvasBadge(n.status, n.statusLabel)}<span class="mono">${canvasEscape(meta.id)}</span></div>`;
    host
      .querySelectorAll("[data-canvas='tab']")
      .forEach((button) =>
        button.setAttribute(
          "aria-pressed",
          String(button.dataset.tab === canvasState.tab),
        ),
      );
    const textList = (value) =>
      Array.isArray(value) ? value.join(" · ") : value;
    const age = n.heartbeatAt
      ? Math.max(0, Math.floor((Date.now() - Date.parse(n.heartbeatAt)) / 1000))
      : null;
    select("[data-node-detail]").innerHTML =
      canvasState.tab === "overview"
        ? `
      <p class="inspector-description">${canvasEscape(meta.description)}</p>
      <div class="inspector-task ${isProblem(n) ? "task-warning" : ""}"><span class="eyebrow">${canvasState.mode === "recorded" ? "RECORDED ACTIVITY" : "CURRENT TASK"}</span><strong>${canvasEscape(n.task || "No task information")}</strong><p>${canvasEscape(n.detail)}</p></div>
      <dl class="inspector-facts"><div><dt>Campaign</dt><dd>${canvasEscape(snapshot?.campaignId || "—")}</dd></div><div><dt>${canvasState.mode === "recorded" ? "Arm" : "Harness"}</dt><dd>${canvasEscape(snapshot?.harness || "—")}</dd></div><div><dt>${canvasState.mode === "recorded" ? "Arm completed" : canvasState.mode === "demo" ? "Demo time" : "Last heartbeat"}</dt><dd>${canvasState.mode === "recorded" ? canvasEscape(new Date(snapshot.completedAt).toLocaleString("en-US", { timeZoneName: "short" })) : canvasState.mode === "demo" ? "Simulated timestamp" : age === null ? "Not reported" : age < 60 ? `${age} seconds ago` : `${Math.floor(age / 60)} minutes ago`}</dd></div>${(
        n.metrics || []
      )
        .slice(0, 3)
        .map(
          (m) =>
            `<div><dt>${canvasEscape(m.label)}</dt><dd>${canvasEscape(m.value)}</dd></div>`,
        )
        .join("")}</dl>
      <div class="node-recent"><h3>${canvasState.mode === "recorded" ? "Recorded summary" : "Recent records"}</h3>${
        n.events?.length
          ? n.events
              .slice(0, 2)
              .map(
                (event) =>
                  `<div><i class="event-dot ${event.level === "error" ? "error" : ""}"></i><p>${canvasEscape(event.message)}<small class="mono">${canvasTime(event.time)}${canvasState.mode === "recorded" ? " · Readout time" : canvasState.mode === "demo" ? " · Demo" : ""}</small></p></div>`,
              )
              .join("")
          : '<p class="inspector-empty">No events reported.</p>'
      }</div>`
        : `
      <p class="inspector-description">These are interface contracts; observed messages are recorded separately.</p><div class="io-section"><h3>${icon("arrow")}Input contract</h3><p>${canvasEscape(textList(meta.input))}</p></div><div class="io-section"><h3>${icon("external")}Output contract</h3><p>${canvasEscape(textList(meta.output))}</p></div><div class="io-section"><h3>${icon("branch")}Message relationships</h3>${runtimeEdges
        .filter((edge) => edge.from === n.id || edge.to === n.id)
        .map(
          (edge) =>
            `<div class="io-connection"><span class="mono">${canvasEscape(edge.from)} → ${canvasEscape(edge.to)}</span><small>${canvasEscape(edge.label)}</small></div>`,
        )
        .join("")}</div>`;
  }
  function update() {
    const nodes = currentNodes();
    const counts = Object.fromEntries(
      Object.keys(canvasLabels).map((s) => [
        s,
        nodes.filter((n) => n.status === s).length,
      ]),
    );
    select("[data-runtime-campaign]").textContent =
      snapshot?.campaignId || "Waiting for telemetry";
    select("[data-runtime-harness]").textContent = snapshot
      ? `${canvasState.mode === "recorded" ? "Arm" : "Harness"} ${snapshot.harness}`
      : "Harness —";
    select("[data-runtime-connection]").innerHTML =
      connection === "recorded"
        ? '<i class="source-dot demo"></i>Recorded results <span>· Not a live heartbeat</span>'
        : connection === "demo"
          ? '<i class="source-dot demo"></i>Demo snapshot <span>· Not actual execution</span>'
          : connection === "connected"
            ? `<i class="source-dot ${snapshot.stale ? "disconnected" : ""}"></i>${snapshot.stale ? "Snapshot stale" : "Telemetry connected"} <span>· Observed at ${canvasTime(snapshot.observedAt)}</span>`
            : `<i class="source-dot disconnected"></i>${connection === "loading" ? "Reading telemetry…" : connection === "waiting" ? "Waiting for connection" : "Telemetry disconnected"}<span>· Component state unconfirmed</span>`;
    select("[data-runtime-summary]").innerHTML =
      `<span class="summary-total"><strong>08</strong> components</span>${(canvasState.mode ===
      "recorded"
        ? [
            ["completed", "Completed"],
            ["queued", "Review / proposal"],
            ["idle", "Not active"],
            ["error", "Incomplete"],
            ["unknown", "Unknown"],
          ]
        : [
            ["running", "Running"],
            ["queued", "Queued"],
            ["idle", "Idle"],
            ["completed", "Completed"],
            ["error", "Error"],
            ["offline", "Stale"],
            ["unknown", "Unknown"],
          ]
      )
        .map(
          ([key, label]) =>
            `<span class="summary-status status-${key}"><i></i>${label}<strong>${counts[key]}</strong></span>`,
        )
        .join(
          "",
        )}<span class="summary-caption">${canvasState.mode === "recorded" ? "RECORDED RESULTS" : canvasState.mode === "demo" ? "SIMULATED SNAPSHOT" : "READ-ONLY TELEMETRY"}</span>`;
    select("[data-problem-count]").textContent = nodes.filter(isProblem).length;
    select("[data-canvas='problems']").setAttribute(
      "aria-pressed",
      String(canvasState.problems),
    );
    for (const n of nodes) {
      const node = nodesHost.querySelector(`[data-node="${n.id}"]`);
      node.className = `runtime-node status-${n.status} ${n.id === canvasState.selected ? "selected" : ""} ${canvasState.problems && !isProblem(n) ? "node-muted" : ""}`;
      node.setAttribute("aria-pressed", String(n.id === canvasState.selected));
      node.setAttribute(
        "aria-label",
        `${runtimeNodes.find((m) => m.id === n.id).name}, ${n.statusLabel || canvasLabels[n.status]}, ${n.detail}`,
      );
      node.querySelector(".node-detail").textContent =
        n.detail || "Waiting for component telemetry";
      node.querySelector(".node-state").innerHTML = canvasBadge(
        n.status,
        n.statusLabel,
      );
      node.querySelector(".node-meta").textContent =
        n.metrics?.[0]?.value ?? "—";
    }
    const allEvents = nodes
      .flatMap((n) =>
        (n.events || []).map((event) => ({ ...event, node: n.id })),
      )
      .sort((a, b) => Date.parse(b.time) - Date.parse(a.time))
      .slice(0, 3);
    const eventMarkup = allEvents.length
      ? allEvents
          .map(
            (event) =>
              `<button class="runtime-event" data-select-node="${event.node}"><time class="mono">${canvasTime(event.time)}</time><span class="event-actor">${canvasEscape(runtimeNodes.find((n) => n.id === event.node).name)}</span><span class="event-level ${event.level}">${event.level === "error" ? "ERROR" : event.level === "warning" ? "WARN" : "INFO"}</span><span class="event-message">${canvasEscape(event.message)}</span>${icon("arrow")}</button>`,
          )
          .join("")
      : '<div class="events-empty">No events available. Live component records appear after a telemetry source is connected.</div>';
    const eventContainer = select("[data-runtime-events]");
    if (eventContainer.innerHTML !== eventMarkup) {
      const focusedNode = eventContainer.contains(document.activeElement)
        ? document.activeElement.dataset.selectNode
        : null;
      eventContainer.innerHTML = eventMarkup;
      if (focusedNode)
        eventContainer
          .querySelector(`[data-select-node="${focusedNode}"]`)
          ?.focus({ preventScroll: true });
    }
    select("[data-event-source]").textContent =
      canvasState.mode === "recorded"
        ? "Recorded summaries"
        : canvasState.mode === "demo"
          ? "Demo events"
          : "Component reports";
    select("[data-poll-label]").textContent =
      canvasState.mode === "recorded"
        ? "Historical record · No polling"
        : canvasState.mode === "demo"
          ? canvasState.playing
            ? "Demo advances every 5 s"
            : "Fixed demo · Play to advance"
          : "Polling every 5 s";
    select("[data-canvas='play']").hidden = canvasState.mode !== "demo";
    select("[data-canvas='play']").innerHTML =
      `${icon(canvasState.playing ? "pause" : "play")}${canvasState.playing ? "Pause demo" : "Play demo"}`;
    select("[data-canvas='refresh']").disabled = fetching;
    select("[data-runtime-note]").textContent =
      canvasState.mode === "recorded"
        ? "Recorded results from the completed experiment. These are historical summaries, not live heartbeats or a replay of exact event timing. A proposed Meta change is not an authorized release."
        : canvasState.mode === "demo"
          ? "Demo workflow only. Edges show message relationships, not active transfers. Computational errors do not establish molecular inactivity."
          : problem ||
            "Read-only telemetry: heartbeats expire after 30 s and snapshots after 60 s. Missing heartbeats are unknown. Recorded experiment results remain separate.";
    select("[data-inspector-mode]").innerHTML =
      `${icon("lock")}${canvasState.mode === "recorded" ? "Recorded results · Not a live heartbeat" : canvasState.mode === "demo" ? "Demo only · No actual execution" : "Read-only telemetry · No execution or restart"}`;
    detailView();
    drawLayout();
  }
  async function refresh() {
    if (canvasState.mode === "recorded") {
      snapshot = recordedRuntime(experiment, armId);
      connection = "recorded";
      problem = "";
      update();
      return;
    }
    if (canvasState.mode === "demo") {
      snapshot = demoRuntime(canvasState.tick);
      connection = "demo";
      problem = "";
      update();
      return;
    }
    if (fetching) return;
    const version = generation;
    fetching = true;
    const activeRequest = new AbortController();
    request = activeRequest;
    const timeout = setTimeout(() => activeRequest.abort(), 4500);
    connection = snapshot ? connection : "loading";
    update();
    try {
      const result = await fetchRuntime(activeRequest.signal);
      if (disposed || version !== generation) return;
      lastPayload = result;
      snapshot = result;
      connection = "connected";
      problem = "";
    } catch (error) {
      if (disposed || version !== generation) return;
      connection = "disconnected";
      problem =
        location.protocol === "file:"
          ? "Standalone HTML supports recorded results and the demo. For live telemetry, open the local preview server and configure RSI_LAB_RUNTIME_FILE."
          : "No valid live telemetry was received. Configure RSI_LAB_RUNTIME_FILE and have the Controller write current snapshots. This interface does not start jobs.";
      snapshot = null;
      lastPayload = null;
    } finally {
      clearTimeout(timeout);
      if (!disposed && version === generation) {
        fetching = false;
        update();
      }
    }
  }
  function choose(id, center = false) {
    canvasState.selected = id;
    if (center) {
      const [x, y] = canvasState.positions[id];
      camera.scale = Math.max(0.8, camera.scale);
      camera.x = viewport.clientWidth / 2 - (x + 104) * camera.scale;
      camera.y = viewport.clientHeight / 2 - (y + 63) * camera.scale;
      transform();
    }
    update();
  }
  listen(host, "click", (event) => {
    const node = event.target.closest("[data-node]");
    if (node) {
      if (!suppressClick) choose(node.dataset.node);
      suppressClick = false;
      return;
    }
    const eventNode = event.target.closest("[data-select-node]");
    if (eventNode) {
      choose(eventNode.dataset.selectNode, true);
      return;
    }
    const target = event.target.closest("[data-canvas]");
    if (!target) return;
    switch (target.dataset.canvas) {
      case "fit":
        fit();
        break;
      case "zoom-in":
        zoom(1.2);
        break;
      case "zoom-out":
        zoom(1 / 1.2);
        break;
      case "reset":
        canvasState.positions = structuredClone(canvasPositions);
        drawLayout();
        fit();
        break;
      case "refresh":
        refresh();
        break;
      case "problems":
        canvasState.problems = !canvasState.problems;
        update();
        break;
      case "play":
        canvasState.playing = !canvasState.playing;
        update();
        break;
      case "tab":
        canvasState.tab = target.dataset.tab;
        detailView();
        break;
    }
  });
  listen(select("#runtime-source"), "change", (event) => {
    generation++;
    request?.abort();
    fetching = false;
    canvasState.mode = event.target.value;
    canvasState.playing = false;
    snapshot = null;
    lastPayload = null;
    connection = "waiting";
    refresh();
  });
  listen(select("#runtime-node-select"), "change", (event) =>
    choose(event.target.value, true),
  );
  listen(viewport, "pointerdown", (event) => {
    if (
      event.button !== 0 ||
      event.target.closest(".canvas-controls,.canvas-map")
    )
      return;
    const node = event.target.closest("[data-node]");
    drag = {
      pointer: event.pointerId,
      id: node?.dataset.node,
      x: event.clientX,
      y: event.clientY,
      origin: node
        ? [...canvasState.positions[node.dataset.node]]
        : [camera.x, camera.y],
      moved: false,
    };
    viewport.setPointerCapture(event.pointerId);
    viewport.classList.add("dragging");
  });
  listen(viewport, "pointermove", (event) => {
    if (!drag || event.pointerId !== drag.pointer) return;
    const dx = event.clientX - drag.x,
      dy = event.clientY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 4) drag.moved = true;
    if (!drag.moved) return;
    if (drag.id) {
      canvasState.positions[drag.id] = [
        Math.max(0, Math.min(1500, drag.origin[0] + dx / camera.scale)),
        Math.max(0, Math.min(1000, drag.origin[1] + dy / camera.scale)),
      ];
      drawLayout();
    } else {
      camera.x = drag.origin[0] + dx;
      camera.y = drag.origin[1] + dy;
      transform();
    }
  });
  function endDrag(event) {
    if (!drag || event.pointerId !== drag.pointer) return;
    const previous = drag;
    drag = null;
    suppressClick = previous.moved;
    if (!previous.moved && previous.id && event.type === "pointerup")
      choose(previous.id);
    if (viewport.hasPointerCapture(event.pointerId))
      viewport.releasePointerCapture(event.pointerId);
    viewport.classList.remove("dragging");
    setTimeout(() => {
      suppressClick = false;
    }, 0);
  }
  listen(viewport, "pointerup", endDrag);
  listen(viewport, "pointercancel", endDrag);
  listen(
    viewport,
    "wheel",
    (event) => {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      const rect = viewport.getBoundingClientRect();
      zoom(
        Math.exp(-event.deltaY * 0.004),
        event.clientX - rect.left,
        event.clientY - rect.top,
      );
    },
    { passive: false },
  );
  listen(viewport, "keydown", (event) => {
    if (event.target !== viewport) return;
    if (
      [
        "+",
        "=",
        "-",
        "0",
        "ArrowLeft",
        "ArrowRight",
        "ArrowUp",
        "ArrowDown",
      ].includes(event.key)
    )
      event.preventDefault();
    if (["+", "="].includes(event.key)) zoom(1.2);
    if (event.key === "-") zoom(1 / 1.2);
    if (event.key === "0") fit();
    if (event.key.startsWith("Arrow")) {
      camera.x +=
        event.key === "ArrowLeft" ? 40 : event.key === "ArrowRight" ? -40 : 0;
      camera.y +=
        event.key === "ArrowUp" ? 40 : event.key === "ArrowDown" ? -40 : 0;
      transform();
    }
  });
  listen(nodesHost, "focusin", (event) => {
    const node = event.target.closest("[data-node]");
    if (!node || drag) return;
    const bounds = node.getBoundingClientRect(),
      frame = viewport.getBoundingClientRect();
    if (
      bounds.left < frame.left ||
      bounds.right > frame.right ||
      bounds.top < frame.top ||
      bounds.bottom > frame.bottom
    )
      choose(node.dataset.node, true);
  });
  const observer = new ResizeObserver(() => {
    fit();
    if (viewport.clientWidth < 600) choose(canvasState.selected, true);
  });
  observer.observe(viewport);
  const timer = setInterval(() => {
    if (document.hidden || disposed) return;
    if (canvasState.mode === "live") refresh();
    else if (canvasState.mode === "demo" && canvasState.playing) {
      canvasState.tick++;
      refresh();
    }
  }, 5000);
  const freshnessTimer = setInterval(() => {
    if (
      canvasState.mode === "live" &&
      lastPayload &&
      !disposed &&
      !document.hidden
    ) {
      try {
        snapshot = normalizeRuntime(lastPayload);
        update();
      } catch {
        snapshot = null;
        connection = "disconnected";
        update();
      }
    }
  }, 1000);
  update();
  fit();
  if (canvasState.mode === "live") refresh();
  return () => {
    disposed = true;
    request?.abort();
    clearInterval(timer);
    clearInterval(freshnessTimer);
    observer.disconnect();
    listeners.abort();
  };
}
