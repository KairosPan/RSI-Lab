// Hand-authored synthetic records for UI tests only. No experiment data.
const time = "2024-01-01T12:00:00.000Z";
const call = (id, fields) => ({ id, title: `Synthetic ${id}`, createdAt: time, status: "succeeded", usage: { tokens: 0, wallSeconds: 1 }, ...fields });
export const traceFixture = {
  schemaVersion: "agent_trace_v1", fixture: true,
  run: { id: "fixture-run", title: "Synthetic test run", source: "recorded", heartbeat: false, updatedAt: time, eventCount: 1 },
  summary: { effects: 5, modelCalls: 2, toolEffects: 3, knownTokens: 10, reservedUnknownTokens: 25 },
  agents: ["lab", "meta", "engineer", "tools", "controller"].map(id => ({ id })),
  calls: [
    call("fixture-meta", { agentId: "meta", kind: "meta", category: "model", usage: { tokens: 10, wallSeconds: 1 } }),
    call("fixture-engineer", { agentId: "engineer", kind: "engineering_model", category: "model", status: "unknown", usage: { tokens: null, reservedTokens: 25 } }),
    call("fixture-source", { agentId: "tools", kind: "frozen_local_source_retrieval", category: "tool", requestedBy: "meta" }),
    call("fixture-validation", { agentId: "tools", kind: "validation", category: "tool", requestedBy: "engineer" }),
    call("fixture-unlinked", { agentId: "tools", kind: "validation", category: "tool" }),
  ],
  facts: [{ id: "fixture-fact", kind: "raw_fact", contentRef: "fixture-reference" }],
  issues: [{ id: "fixture-issue", scope: "Synthetic scope", hypothesis: "Synthetic hypothesis", status: "open" }],
  events: [{ id: "fixture-event", eventType: "fixture", eventSequence: 1 }],
  labOrchestration: {
    schemaVersion: "lab_orchestration_v1", projectionComplete: true, status: "awaiting_capability", label: "Awaiting capability", note: "Synthetic state for rendering tests.",
    snapshotAt: time, snapshotBasis: { eventSequence: 1 },
    task: { objective: "Synthetic task only", plans: [] },
    counts: { labModelCalls: 0, pendingWorks: 1, activeWaits: 0, openIssues: 1, unknownEffects: 1 },
    readiness: { execution: { labReadyWorkCount: 0, automaticResumeConfirmed: false }, scientific: { status: "unresolved", label: "Synthetic unresolved issue" } },
    pendingWorks: [{ id: "fixture-work", kind: "capability_build", role: "engineering", status: "ready", requesterCallId: "fixture-meta" }],
    activeWaits: [], issues: [], unknownEffects: [], stages: [], resumeRequirements: [],
  },
};
export function fixtureDetail(id) {
  const row = traceFixture.calls.find(call => call.id === id);
  return row && { ...row, messages: [{ role: "user", label: "Synthetic input", content: "Fixture input <script>unsafe()</script>", truncated: false }, ...(row.status === "unknown" ? [] : [{ role: "assistant", label: "Synthetic response", content: "Fixture response only", truncated: false }])], artifacts: [] };
}
