import test from "node:test";
import assert from "node:assert/strict";
import { escapeTrace, traceCallsForAgent } from "../src/agent-canvas.js";

// Synthetic records exercise rendering/filtering only; never live model evidence.
const records = [
  { id: "meta-first", agentId: "meta", category: "model", kind: "meta", title: "Inspect receptor evidence", summary: "A bounded investigation", status: "succeeded" },
  { id: "engineer-first", agentId: "engineer", category: "model", kind: "engineering_model", title: "Implement adapter", summary: "Source delivery", status: "succeeded" },
  { id: "unknown-engineer", agentId: "engineer", category: "model", kind: "engineering_model", title: "Unresolved build", status: "unknown", usage: { tokens: null }, heldReservations: { tokens: 25 } },
  { id: "meta-tool", agentId: "tools", requestedBy: "meta", category: "tool", kind: "source_retrieval", title: "Read source", summary: "Pinned source", status: "succeeded" },
  { id: "engineer-tool", agentId: "tools", requestedBy: "engineer", category: "tool", kind: "validation", title: "Validate adapter", summary: "Check inputs", status: "failed_definite" },
  { id: "unattributed-tool", agentId: "tools", category: "tool", kind: "validation", title: "Unattributed check", status: "succeeded" },
  { id: "controller-event", agentId: "controller", category: "event", kind: "budget_updated", title: "Budget changed", status: "recorded" },
];
const ids = (rows) => rows.map((row) => row.id);

test("conversation records remain isolated to the selected agent", () => {
  assert.deepEqual(ids(traceCallsForAgent(records, "meta", "conversation")), ["meta-first"]);
  assert.deepEqual(ids(traceCallsForAgent(records, "engineer", "conversation")), ["engineer-first", "unknown-engineer"]);
  assert.deepEqual(traceCallsForAgent(records, "lab", "conversation"), []);
});

test("agent tools require explicit requester attribution", () => {
  assert.deepEqual(ids(traceCallsForAgent(records, "meta", "tools")), ["meta-tool"]);
  assert.deepEqual(ids(traceCallsForAgent(records, "engineer", "tools")), ["engineer-tool"]);
  assert.deepEqual(ids(traceCallsForAgent(records, "tools", "tools")), ["meta-tool", "engineer-tool", "unattributed-tool"]);
});

test("query matching is case-insensitive and cannot bypass agent scope", () => {
  assert.deepEqual(ids(traceCallsForAgent(records, "meta", "conversation", "RECEPTOR")), ["meta-first"]);
  assert.deepEqual(traceCallsForAgent(records, "meta", "conversation", "adapter"), []);
  assert.deepEqual(ids(traceCallsForAgent(records, "engineer", "tools", "INPUTS")), ["engineer-tool"]);
});

test("legacy model kinds are recognized without category", () => {
  const legacy = [
    { id: "legacy-meta", agentId: "meta", kind: "meta", title: "Review" },
    { id: "legacy-engineer", agentId: "engineer", kind: "engineering_model", title: "Build" },
  ];
  assert.deepEqual(ids(traceCallsForAgent(legacy, "meta", "conversation")), ["legacy-meta"]);
  assert.deepEqual(ids(traceCallsForAgent(legacy, "engineer", "conversation")), ["legacy-engineer"]);
});

test("filtering preserves missing usage, original records and input order", () => {
  const before = JSON.stringify(records);
  const result = traceCallsForAgent(records, "engineer", "conversation");
  assert.equal(result[1].status, "unknown");
  assert.equal(result[1].usage.tokens, null);
  assert.equal(result[1].heldReservations.tokens, 25);
  assert.equal(JSON.stringify(records), before);
});

test("HTML escaping treats raw conversation and tool content as text", () => {
  const raw = '<script>alert("x")</script> & <img src=x onerror=alert(1)> \'quoted\'';
  const escaped = escapeTrace(raw);
  assert.ok(!escaped.includes("<script>"));
  assert.ok(!escaped.includes("<img"));
  assert.ok(escaped.includes("&lt;script&gt;"));
  assert.ok(escaped.includes("&amp;"));
  assert.ok(escaped.includes("&quot;"));
  assert.ok(escaped.includes("&#39;") || escaped.includes("&#x27;"));
  assert.equal(escapeTrace(null), "");
});
