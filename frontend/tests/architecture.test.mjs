import test from "node:test";
import assert from "node:assert/strict";
import { traceFixture as snapshot } from "./fixtures/trace.mjs";
import { architectureComponents, architectureRows, architectureCallHome, architectureState } from "../src/architecture.js";

const ids = (rows) => rows.map((r) => r.id);

test("architecture retains five modules and places controller in infrastructure", () => {
  assert.deepEqual(Object.values(architectureComponents).filter((c) => !c.infrastructure).map((c) => c.name), ["Lab Harness", "Meta-Harness", "Scientific Memory", "Literature Tool Calling", "Computational Tools"]);
  assert.equal(architectureComponents.engineer, undefined);
  assert.equal(architectureComponents.controller.infrastructure, true);
});

test("synthetic trace separates Meta decisions from its engineering backend without changing calls", () => {
  const meta = architectureRows(snapshot, "meta", "conversation");
  const engineering = architectureRows(snapshot, "meta", "engineering");
  assert.equal(meta.length, 1);
  assert.equal(engineering.length, 1);
  assert.equal(architectureRows(snapshot, "lab", "conversation").length, 0);
  for (const call of engineering) {
    assert.equal(call, snapshot.calls.find((c) => c.id === call.id));
    assert.deepEqual(architectureCallHome(call), { id: "meta", section: "engineering" });
    assert.ok(!meta.includes(call));
  }
  assert.equal(architectureState(snapshot, "meta").status, "unknown");
  assert.equal(architectureState(snapshot, "lab").status, "partial");
  assert.equal(architectureState(snapshot, "lab").label, "Awaiting capability");
});

test("Lab defaults to persisted orchestration and never infers it from empty model calls", () => {
  assert.deepEqual(architectureComponents.lab.sections[0], { id: "orchestration", label: "Orchestration", type: "overview" });
  assert.deepEqual(architectureRows(snapshot, "lab", "orchestration"), []);
  assert.equal(snapshot.labOrchestration.counts.labModelCalls, 0);
  assert.ok(snapshot.labOrchestration.counts.pendingWorks > 0);
  assert.match(architectureState(snapshot, "lab").countLabel, /pending work/);
  assert.equal(architectureState({ ...snapshot, labOrchestration: undefined }, "lab").status, "unknown");
  assert.equal(architectureState({ ...snapshot, labOrchestration: { schemaVersion: "lab_orchestration_v1", projectionComplete: false } }, "lab").status, "unknown");
  assert.equal(architectureState({ calls: [] }, "lab").status, "unknown");
});

test("frozen source reads and computational executions are disjoint and exhaustive", () => {
  const sources = architectureRows(snapshot, "literature", "frozen_sources");
  const computations = architectureRows(snapshot, "tools", "executions");
  assert.equal(sources.length, 1);
  assert.equal(computations.length, 2);
  assert.equal(new Set(ids([...sources, ...computations])).size, 3);
  assert.ok(!computations.some((call) => sources.includes(call)));
  assert.deepEqual(architectureRows(snapshot, "literature", "overview"), []);
  assert.equal(architectureState(snapshot, "literature").status, "not_connected");
  assert.match(architectureState(snapshot, "tools").note, /No Vina calls in this run/);
});

test("module costs deduplicate linked calls and retain unresolved reservation and null usage", () => {
  const duplicated = { ...snapshot, calls: [...snapshot.calls, ...snapshot.calls] };
  const rows = architectureRows(duplicated, "meta", "cost");
  assert.equal(rows.length, 4); // One Meta, one engineering and two attributed tools.
  assert.equal(new Set(ids(rows)).size, rows.length);
  const unresolved = rows.find((c) => c.status === "unknown");
  assert.equal(unresolved.usage.tokens, null);
  assert.equal(unresolved.usage.reservedTokens, 25);
  assert.equal(rows.reduce((n, c) => n + (c.usage.tokens ?? 0), 0), snapshot.summary.knownTokens);
  assert.deepEqual(architectureRows(snapshot, "memory", "cost"), []);
  assert.deepEqual(architectureRows(snapshot, "controller", "cost"), []);
});

test("attribution and tool classification use explicit fields, never keywords", () => {
  const data = { calls: [
    { id: "build", kind: "engineering_model", agentId: "meta", category: "model" },
    { id: "build-by-role", kind: "other", agentId: "engineer", category: "model" },
    { id: "unlinked", kind: "future_tool", category: "tool", title: "Meta literature PubMed", summary: "engineering request" },
    { id: "linked", kind: "future_tool", category: "tool", requestedBy: "engineer" },
    { id: "source", kind: "frozen_local_source_retrieval", category: "tool" },
  ] };
  assert.deepEqual(ids(architectureRows(data, "meta", "engineering")), ["build", "build-by-role"]);
  assert.deepEqual(ids(architectureRows(data, "meta", "tools")), ["linked"]);
  assert.deepEqual(ids(architectureRows(data, "literature", "frozen_sources")), ["source"]);
  assert.deepEqual(architectureCallHome(data.calls[2]), { id: "tools", section: "executions" });
  assert.deepEqual(architectureCallHome(data.calls[4]), { id: "literature", section: "frozen_sources" });
});

test("memory keeps original issue fields and is explicitly partial; missing sources stay unknown", () => {
  assert.equal(architectureRows(snapshot, "memory", "facts"), snapshot.facts);
  assert.equal(architectureRows(snapshot, "memory", "issues"), snapshot.issues);
  assert.equal(snapshot.facts.length, 1);
  assert.equal(snapshot.issues.length, 1);
  assert.deepEqual(Object.keys(snapshot.issues[0]).sort(), ["hypothesis", "id", "scope", "status"]);
  assert.equal(architectureState(snapshot, "memory").status, "partial");
  for (const id of Object.keys(architectureComponents)) assert.equal(architectureState(null, id).status, "unknown");
  assert.equal(architectureState({ calls: [] }, "memory").status, "unknown");
  assert.deepEqual(architectureRows(snapshot, "bogus", "cost"), []);
  assert.deepEqual(architectureRows(snapshot, "meta", "bogus"), []);
});
