import { icon } from "./icons.js";
import { agentCanvasPage, mountAgentCanvas } from "./agent-canvas.js";

const app = document.querySelector("#app");
app.innerHTML = `<div class="app-shell agent-shell">
  <button class="mobile-backdrop" data-action="close-nav" aria-label="Close navigation"></button>
  <aside class="sidebar" aria-label="Main navigation">
    <a class="brand" href="#agents" aria-label="RSI-Lab home"><span class="brand-mark">${icon("layers")}</span><span class="brand-name">RSI<span>—</span>Lab</span></a>
    <div class="workspace-switch"><span class="workspace-icon">${icon("branch")}</span><span class="workspace-copy"><strong>System workspace</strong><small>Harnesses · Memory · Tools</small></span></div>
    <div class="nav-caption">RESEARCH WORKSPACE</div>
    <nav class="nav-list"><a class="nav-link active" href="#agents" aria-current="page">${icon("branch")}Architecture</a></nav>
    <div class="sidebar-bottom"><div class="sidebar-note"><div class="note-title">${icon("layers")}Your research workspace</div><p>Select a source to inspect your run.<br>No experiment records are bundled.</p></div><div class="profile"><span class="avatar">RL</span><div><strong>RSI-Lab</strong><small>READ-ONLY SYSTEM VIEW</small></div>${icon("lock")}</div></div>
  </aside>
  <div class="page-shell"><header class="topbar"><div class="breadcrumbs"><button class="icon-button mobile-menu" data-action="toggle-nav" aria-label="Open navigation" aria-expanded="false">${icon("menu")}</button><span>Workspace</span>${icon("chevron")}<strong>Architecture</strong></div><div class="topbar-right"><span class="demo-badge"><i class="demo-dot"></i>Read-only workspace</span></div></header>
  <main class="main agent-main" id="main-content" tabindex="-1">${agentCanvasPage()}<footer class="app-footer"><span>${icon("lock")}Source records retain their provenance</span><span>RSI-LAB / SYSTEM WORKSPACE</span></footer></main></div>
</div>`;
function closeNavigation() {
  document.body.classList.remove("nav-open");
  app.querySelector('[data-action="toggle-nav"]').setAttribute("aria-expanded", "false");
}
app.addEventListener("click", (event) => {
  const action = event.target.closest("[data-action]")?.dataset.action;
  if (action === "toggle-nav") {
    const open = document.body.classList.toggle("nav-open");
    app.querySelector('[data-action="toggle-nav"]').setAttribute("aria-expanded", String(open));
  }
  if (action === "close-nav" || event.target.closest(".nav-link")) closeNavigation();
});
document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeNavigation(); });
const unmount = mountAgentCanvas(app);
window.addEventListener("pagehide", unmount, { once: true });
