// Optional local browser smoke check. All trace responses are synthetic.
// Start frontend/server.mjs first; no model or scientific work is executed.
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { traceFixture, fixtureDetail } from "./fixtures/trace.mjs";

const base = process.env.RSI_QA_URL || "http://127.0.0.1:4173";
assert.ok(["localhost", "127.0.0.1", "[::1]"].includes(new URL(base).hostname), "Use a local preview server");
const moduleName = process.env.PLAYWRIGHT_MODULE ? pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href : "playwright";
const { chromium } = await import(moduleName);
const output = resolve(process.env.RSI_QA_OUTPUT || "output/playwright/canvas");
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
page.setDefaultTimeout(8000);
const errors = [], results = [], traceRequests = [];
page.on("pageerror", error => errors.push(error.message));
page.on("request", request => { if (/\/(api\/trace|data\/agent-trace)/.test(new URL(request.url()).pathname)) traceRequests.push(request.url()); });
const pattern = "**/data/agent-trace/index.json";
await page.route(pattern, route => route.fulfill({ contentType: "application/json", body: JSON.stringify(traceFixture) }));
await page.route("**/data/agent-trace/calls/*.json", route => {
  const id = new URL(route.request().url()).pathname.split("/").at(-1).replace(/\.json$/, "");
  return route.fulfill({ contentType: "application/json", body: JSON.stringify(fixtureDetail(id)) });
});
async function check(name, action) {
  try { await action(); results.push({ name, passed: true }); }
  catch (error) { results.push({ name, passed: false, error: error.message }); }
}
async function selectAgent(id) { await page.locator(`[data-ac-node="${id}"]`).click(); }
try {
  await page.goto(base, { waitUntil: "networkidle" });
  await check("default is an empty architecture without trace requests", async () => {
    assert.equal(await page.locator("#agent-trace-source").inputValue(), "none");
    assert.equal(await page.locator("[data-ac-run]").textContent(), "No run selected");
    assert.equal(await page.locator("[data-ac-node]").count(), 5);
    assert.equal(await page.locator("[data-ac-call]").count(), 0);
    assert.equal(traceRequests.length, 0);
    assert.equal(await page.locator("[data-ac-alert]").isVisible(), false);
    assert.equal(await page.locator('.nav-link').count(), 1);
  });
  await check("source is loaded only after explicit selection", async () => {
    await page.selectOption("#agent-trace-source", "recorded");
    await page.waitForSelector(".ac-orchestration[data-lo-status]");
    assert.equal(await page.locator("[data-ac-run]").textContent(), traceFixture.run.title);
    assert.match(await page.locator(".lo-hero").innerText(), /Awaiting capability/);
  });
  await check("model input is escaped and remains tied to its component", async () => {
    await selectAgent("meta");
    await page.waitForSelector(".ac-record-heading > code");
    assert.equal(await page.locator(".ac-record-heading > code").textContent(), "fixture-meta");
    await page.locator(".ac-message-input summary").click();
    assert.match(await page.locator(".ac-detail").innerText(), /Fixture input <script>unsafe\(\)<\/script>/);
    assert.equal(await page.locator(".ac-detail script").count(), 0);
    await page.locator('.ac-tabs [data-ac-section="engineering"]').click();
    await page.waitForFunction(() => document.querySelector(".ac-record-heading > code")?.textContent === "fixture-engineer");
    assert.match(await page.locator(".ac-detail").innerText(), /Unknown result/);
  });
  await check("memory displays the synthetic source indexes", async () => {
    await selectAgent("memory");
    assert.equal(await page.locator(".ac-service-records details").count(), 1);
    await page.locator('.ac-tabs [data-ac-section="issues"]').click();
    assert.match(await page.locator(".ac-service-records").textContent(), /Synthetic hypothesis/);
  });
  await check("source and computational tools show separate generic records", async () => {
    await selectAgent("sources");
    assert.match(await page.locator(".ac-capability-view").innerText(), /1 recorded reads/);
    await page.locator('.ac-tabs [data-ac-section="reads"]').click();
    await page.waitForFunction(() => document.querySelector(".ac-record-heading > code")?.textContent === "fixture-source");
    assert.match(await page.locator(".ac-record-scope").innerText(), /recorded tool-role metadata/);
    await selectAgent("tools");
    assert.equal(await page.locator("[data-ac-call]").count(), 2);
    assert.match(await page.locator("[data-ac-heading]").innerText(), /2 tool Effects recorded/);
  });
  await check("source failure clears old data", async () => {
    await page.route(pattern, route => route.fulfill({ status: 503, body: "Unavailable fixture" }));
    await page.locator('[data-ac-action="refresh"]').click();
    await page.waitForFunction(() => document.querySelector("[data-ac-run]")?.textContent === "Source unavailable");
    assert.equal(await page.locator("[data-ac-call]").count(), 0);
    assert.ok(!(await page.locator(".ac-detail").innerText()).includes("Synthetic hypothesis"));
  });
  await check("returning to no source restores a normal empty state", async () => {
    await page.selectOption("#agent-trace-source", "none");
    assert.equal(await page.locator("[data-ac-run]").textContent(), "No run selected");
    assert.equal(await page.locator("[data-ac-alert]").isVisible(), false);
    const count = traceRequests.length;
    await page.locator('[data-ac-action="refresh"]').click();
    assert.equal(traceRequests.length, count);
  });
  await check("desktop and mobile layouts do not overflow", async () => {
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({ path: resolve(output, "empty-desktop.png") });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator('.mobile-menu').click();
    assert.equal(await page.locator('.mobile-menu').getAttribute('aria-expanded'), 'true');
    await page.locator('.nav-link').click();
    assert.equal(await page.locator('.mobile-menu').getAttribute('aria-expanded'), 'false');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({ path: resolve(output, "empty-mobile.png") });
  });
  assert.deepEqual(errors, []);
} finally {
  await browser.close();
  await writeFile(resolve(output, "checks.json"), JSON.stringify({ results, errors }, null, 2));
}
console.log(JSON.stringify({ passed: results.filter(x => x.passed).length, total: results.length, errors, failures: results.filter(x => !x.passed) }));
if (errors.length || results.some(x => !x.passed)) process.exitCode = 1;
