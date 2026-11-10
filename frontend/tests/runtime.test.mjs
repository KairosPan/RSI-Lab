import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, writeFile, rm, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  normalizeRuntime,
  demoRuntime,
  runtimeNodes,
  runtimeEdges,
  fetchRuntime,
} from "../src/runtime.js";
import { handleRuntimeRequest, MAX_RUNTIME_BYTES } from "../runtime-server.mjs";

const NOW = Date.parse("2026-10-20T12:00:00.000Z");
const iso = (offset = 0) => new Date(NOW + offset).toISOString();
const node = (overrides = {}) => ({
  id: "controller",
  status: "running",
  detail: "Orchestrating requests",
  heartbeatAt: iso(),
  task: "Maintain state",
  metrics: [{ label: "Task", value: 1 }],
  events: [],
  ...overrides,
});
const snapshot = (overrides = {}) => ({
  source: "live",
  campaignId: "DEV-004",
  harness: "H1",
  observedAt: iso(),
  nodes: [node()],
  ...overrides,
});
const resultFor = (payload, id = "controller") =>
  normalizeRuntime(payload, NOW).nodes.find((item) => item.id === id);

test("fixed topology fills missing nodes as unknown and ignores unfamiliar IDs", () => {
  const result = normalizeRuntime(
    snapshot({ nodes: [node(), { id: "foreign-agent" }] }),
    NOW,
  );
  assert.equal(result.nodes.length, 8);
  assert.equal(
    result.nodes.find((item) => item.id === "lab").status,
    "unknown",
  );
  assert.deepEqual(result.ignoredNodeIds, ["foreign-agent"]);
  assert.equal(
    result.nodes.some((item) => item.id === "foreign-agent"),
    false,
  );
});

test("unknown status and missing heartbeat cannot appear healthy", () => {
  assert.equal(
    resultFor(snapshot({ nodes: [node({ status: "unexpected" })] })).status,
    "unknown",
  );
  assert.equal(
    resultFor(snapshot({ nodes: [node({ heartbeatAt: null })] })).status,
    "unknown",
  );
});

test("heartbeat older than 30 seconds becomes offline including error nodes", () => {
  assert.equal(
    resultFor(snapshot({ nodes: [node({ heartbeatAt: iso(-30_001) })] }))
      .status,
    "offline",
  );
  assert.equal(
    resultFor(
      snapshot({
        nodes: [node({ status: "error", heartbeatAt: iso(-30_001) })],
      }),
    ).status,
    "offline",
  );
  assert.equal(
    resultFor(snapshot({ nodes: [node({ heartbeatAt: iso(-30_000) })] }))
      .status,
    "running",
  );
});

test("snapshot older than 60 seconds makes every node offline", () => {
  const result = normalizeRuntime(
    snapshot({
      observedAt: iso(-60_001),
      nodes: [node({ heartbeatAt: iso(-60_001) })],
    }),
    NOW,
  );
  assert.equal(result.stale, true);
  assert.equal(
    result.nodes.every((item) => item.status === "offline"),
    true,
  );
});

test("future and inconsistent timestamps are rejected", () => {
  assert.throws(
    () => normalizeRuntime(snapshot({ observedAt: iso(1) }), NOW),
    /in the future/,
  );
  assert.throws(
    () =>
      normalizeRuntime(
        snapshot({ nodes: [node({ heartbeatAt: iso(1) })] }),
        NOW,
      ),
    /in the future/,
  );
  assert.throws(
    () => normalizeRuntime(snapshot({ observedAt: iso(-1) }), NOW),
    /heartbeat is later/,
  );
  assert.throws(
    () =>
      normalizeRuntime(
        snapshot({
          nodes: [
            node({
              events: [{ time: iso(1), message: "future", level: "info" }],
            }),
          ],
        }),
        NOW,
      ),
    /in the future/,
  );
  assert.throws(
    () =>
      normalizeRuntime(snapshot({ observedAt: "2026-02-30T12:00:00Z" }), NOW),
    /timestamp/,
  );
});

test("malformed snapshots and unsafe numeric values are rejected", () => {
  for (const bad of [
    null,
    [],
    {},
    snapshot({ source: "unknown" }),
    snapshot({ nodes: {} }),
    snapshot({ nodes: [node(), node()] }),
  ]) {
    assert.throws(() => normalizeRuntime(bad, NOW));
  }
  for (const overrides of [
    { metrics: null },
    { events: {} },
    { detail: 1 },
    { progress: 101 },
    { progress: -1 },
    { progress: Infinity },
    { metrics: [{ label: "task", value: NaN }] },
    { events: [{ time: iso(), message: "event", level: "arbitrary" }] },
  ])
    assert.throws(() =>
      normalizeRuntime(snapshot({ nodes: [node(overrides)] }), NOW),
    );
});

test("all producer-controlled text is bounded and unrecognized properties are dropped", () => {
  const result = normalizeRuntime(
    snapshot({
      campaignId: "C".repeat(100),
      harness: "H".repeat(100),
      nodes: [
        node({
          detail: "D".repeat(500),
          task: "T".repeat(300),
          metrics: [{ label: "L".repeat(100), value: "V".repeat(200) }],
          events: [{ time: iso(), message: "M".repeat(500), level: "info" }],
          html: "<script>untrusted()</script>",
        }),
      ],
    }),
    NOW,
  );
  const entry = result.nodes[0];
  assert.equal(result.campaignId.length, 64);
  assert.equal(result.harness.length, 64);
  assert.equal(entry.detail.length, 400);
  assert.equal(entry.task.length, 200);
  assert.equal(entry.metrics[0].label.length, 48);
  assert.equal(entry.metrics[0].value.length, 100);
  assert.equal(entry.events[0].message.length, 400);
  assert.equal("html" in entry, false);
});

test("demo uses a generic tool topology but remains explicitly simulated", () => {
  const demo = normalizeRuntime(demoRuntime(1));
  assert.equal(demo.source, "demo");
  assert.equal(demo.harness, "Demo Harness");
  assert.equal(
    demo.nodes.find((item) => item.id === "resultcheck").status,
    "error",
  );
  assert.match(
    demo.nodes.find((item) => item.id === "resultcheck").detail,
    /does not resolve the task hypothesis/,
  );
  assert.equal(
    demo.nodes.every((item) => item.metrics[0].label.includes("Demo")),
    true,
  );
  assert.deepEqual(
    runtimeNodes.map((item) => item.id),
    [
      "controller",
      "lab",
      "executor",
      "analysis",
      "resultcheck",
      "memory",
      "meta",
      "validator",
    ],
  );
  assert.equal(
    runtimeEdges.filter((edge) => edge.kind === "cross-campaign").length,
    3,
  );
});

function responseCapture() {
  return {
    status: null,
    headers: {},
    body: undefined,
    writeHead(status, headers) {
      this.status = status;
      this.headers = headers;
    },
    end(body) {
      this.body = body;
    },
  };
}

test("runtime endpoint reads configured JSON, supports HEAD, and never permits writes", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "rsi-runtime-test-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const path = join(directory, "runtime.json");
  await writeFile(path, JSON.stringify(snapshot()));
  const before = await readFile(path, "utf8");
  const get = responseCapture();
  await handleRuntimeRequest({ method: "GET" }, get, {
    runtimeFile: path,
    now: NOW,
  });
  assert.equal(get.status, 200);
  assert.equal(JSON.parse(get.body).nodes.length, 8);
  assert.equal(get.headers["Cache-Control"], "no-store");
  const head = responseCapture();
  await handleRuntimeRequest({ method: "HEAD" }, head, {
    runtimeFile: path,
    now: NOW,
  });
  assert.equal(head.status, 200);
  assert.equal(head.body, undefined);
  const post = responseCapture();
  await handleRuntimeRequest({ method: "POST" }, post, {
    runtimeFile: path,
    now: NOW,
  });
  assert.equal(post.status, 405);
  assert.equal(post.headers.Allow, "GET, HEAD");
  assert.equal(await readFile(path, "utf8"), before);
});

test("endpoint failures hide configured paths and stack traces", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "rsi-runtime-test-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const path = join(directory, "private-runtime.json");
  for (const runtimeFile of ["", path, directory]) {
    const response = responseCapture();
    await handleRuntimeRequest({ method: "GET" }, response, {
      runtimeFile,
      now: NOW,
    });
    assert.equal(response.status, 503);
    assert.equal(response.body.includes(directory), false);
    assert.equal(response.body.includes("stack"), false);
  }
  for (const contents of [
    "not-json",
    JSON.stringify({ nodes: [] }),
    " ".repeat(MAX_RUNTIME_BYTES + 1),
  ]) {
    await writeFile(path, contents);
    const response = responseCapture();
    await handleRuntimeRequest({ method: "GET" }, response, {
      runtimeFile: path,
      now: NOW,
    });
    assert.equal(response.status, 503);
    assert.deepEqual(Object.keys(JSON.parse(response.body)), ["error"]);
  }
});

test("real endpoint rejects historical or demo snapshots even when structurally valid and fresh", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "rsi-runtime-test-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const path = join(directory, "demo.json");
  for (const source of ["demo", "recorded"]) {
    const demo = snapshot({ source });
    assert.equal(normalizeRuntime(demo, NOW).source, source);
    await writeFile(path, JSON.stringify(demo));
    for (const method of ["GET", "HEAD"]) {
      const response = responseCapture();
      await handleRuntimeRequest({ method }, response, {
        runtimeFile: path,
        now: NOW,
      });
      assert.equal(response.status, 503);
      if (method === "HEAD") assert.equal(response.body, undefined);
      else assert.deepEqual(Object.keys(JSON.parse(response.body)), ["error"]);
    }
  }
});

test("example snapshot follows the accepted schema", async () => {
  const example = JSON.parse(
    await readFile(new URL("../runtime.example.json", import.meta.url), "utf8"),
  );
  assert.equal(example.source, "demo");
  assert.equal(normalizeRuntime(example, NOW).nodes.length, 8);
});

test("fetchRuntime accepts live snapshots and rejects demo without claiming a real connection", async () => {
  const previousLocation = globalThis.location;
  const previousFetch = globalThis.fetch;
  globalThis.location = { protocol: "http:" };
  let reply = demoRuntime();
  globalThis.fetch = async (url, options) => {
    assert.equal(url, "/api/runtime");
    assert.equal(options.method, "GET");
    assert.equal(options.cache, "no-store");
    return { ok: true, json: async () => reply };
  };
  try {
    await assert.rejects(fetchRuntime(), /cannot establish a live connection/);
    reply = snapshot({ source: "recorded" });
    await assert.rejects(fetchRuntime(), /cannot establish a live connection/);
    reply = { ...demoRuntime(), source: "live" };
    assert.equal((await fetchRuntime()).source, "live");
  } finally {
    globalThis.fetch = previousFetch;
    if (previousLocation === undefined) delete globalThis.location;
    else globalThis.location = previousLocation;
  }
});

test("fetchRuntime refuses file previews explicitly", async () => {
  const previous = globalThis.location;
  globalThis.location = { protocol: "file:" };
  try {
    await assert.rejects(fetchRuntime(), /File previews/);
  } finally {
    if (previous === undefined) delete globalThis.location;
    else globalThis.location = previous;
  }
});
