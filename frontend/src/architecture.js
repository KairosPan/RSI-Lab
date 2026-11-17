// Architecture is a view over recorded work, not a census of autonomous agents.
// This projection targets agent_trace_v1 from read-agent-trace.py. In that
// contract, requestedBy is set only after an exact recorded request/build link.
// Never infer ownership, source retrieval or execution from prompt text.
export const architectureComponents = {
  lab: {
    id: "lab", name: "Lab Harness", subtitle: "Task planning & execution", icon: "flask",
    description: "The task-facing harness combines decision prompts, context, memory rules and tool requests under the frozen task protocol.",
    sections: [{ id: "orchestration", label: "Orchestration", type: "overview" }, { id: "conversation", label: "Decisions", type: "calls" }, { id: "tools", label: "Linked tools", type: "calls" }, { id: "cost", label: "Cost & source", type: "cost" }],
    scopeNote: "Orchestration reflects persisted plans, issues, work, waits and unresolved calls, not a process heartbeat or a new admission decision. Zero Lab model calls does not mean there is no task orchestration.",
  },
  meta: {
    id: "meta", name: "Meta-Harness", subtitle: "Review, improve & validate", icon: "branch",
    description: "Reviews assumptions and advances issues. Capability-building model calls are an on-demand backend within this workflow, not a third top-level agent.",
    sections: [{ id: "conversation", label: "Meta decisions", type: "calls" }, { id: "engineering", label: "Engineering backend", type: "calls" }, { id: "tools", label: "Linked tools", type: "calls" }, { id: "cost", label: "Cost & source", type: "cost" }],
    scopeNote: "Engineering calls return proposals; host review, execution and capability acceptance remain separate. Linked tools require explicit recorded attribution. Costs include each linked call once here and may also appear in a tool module; module totals must not be added together.",
  },
  memory: {
    id: "memory", name: "Scientific Memory", subtitle: "Evidence, hypotheses & issues", icon: "book",
    description: "Connects immutable evidence with unresolved scientific questions. This view exposes the recorded fact and issue indexes.",
    sections: [{ id: "facts", label: "Evidence index", type: "records" }, { id: "issues", label: "Open questions", type: "records" }, { id: "cost", label: "Cost & source", type: "cost" }],
    scopeNote: "Partial view: fact rows contain references, not the original evidence; issue hypotheses are not verified conclusions. Full working-memory history and storage overhead are not separately exposed or metered in this trace.",
  },
  sources: {
    id: "sources", name: "Source Tools", subtitle: "Inputs, documents & retrieval", icon: "search",
    description: "Source providers deliver task inputs and supporting records. Only tools explicitly recorded with the source role are shown here.",
    sections: [{ id: "overview", label: "Connection & scope", type: "overview" }, { id: "reads", label: "Source reads", type: "calls" }, { id: "cost", label: "Cost & source", type: "cost" }],
    scopeNote: "A recorded source read does not prove a provider is currently connected or that its content is valid. Source ownership requires explicit tool-role metadata; names and prompt text are not evidence.",
  },
  tools: {
    id: "tools", name: "Computational Tools", subtitle: "Preparation, diagnostics & evaluation", icon: "cpu",
    description: "Actual computational, validation and host-tool executions with their original arguments, outputs and receipts. Source reads are shown separately.",
    sections: [{ id: "executions", label: "Executions", type: "calls" }, { id: "cost", label: "Cost & source", type: "cost" }],
    scopeNote: "A successful process does not prove scientific acceptance. Tool records and heterogeneous billing units are different quantities. Attributed calls may also appear under their requesting harness.",
  },
  controller: {
    id: "controller", name: "Infrastructure", subtitle: "Controller, budget & recovery", icon: "grid", infrastructure: true,
    description: "Code infrastructure records dispatch, reservations, delivery, settlement and recovery. It supports the architecture rather than acting as a research agent.",
    sections: [{ id: "events", label: "Controller events", type: "records" }, { id: "cost", label: "Cost & source", type: "cost" }],
    scopeNote: "Controller events are not model conversations. Infrastructure overhead is not separately metered; no call rows here must not be read as zero infrastructure cost.",
  },
};

const apModelKinds = new Set(["meta", "engineering_model", "lab", "lab_model", "model"]);
const apCategory = (call) => call.category || (apModelKinds.has(call.kind) || ["meta", "engineer", "lab"].includes(call.agentId) ? "model" : call.kind === "event" ? "event" : "tool");
const apEngineering = (call) => call.kind === "engineering_model" || (apCategory(call) === "model" && call.agentId === "engineer");
const apMeta = (call) => apCategory(call) === "model" && !apEngineering(call) && (call.kind === "meta" || call.agentId === "meta");
const apLab = (call) => apCategory(call) === "model" && !apEngineering(call) && (call.agentId === "lab" || ["lab", "lab_model"].includes(call.kind));
const apTool = (call) => apCategory(call) === "tool";
const apSource = (call) => apTool(call) && call.tool?.role === "source";
const apMetaTool = (call) => apTool(call) && ["meta", "engineer"].includes(call.requestedBy);
const apLabTool = (call) => apTool(call) && (call.requestedBy === "lab" || call.agentId === "lab");
const apUnique = (rows) => {
  const seen = new Set();
  return rows.filter((row) => {
    const key = row.id ?? row;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
};

/** Original objects, stable input order; never edits evidence or missing usage. */
export function architectureRows(index, id, section) {
  if (!index || !architectureComponents[id]?.sections.some((s) => s.id === section)) return [];
  if (id === "lab" && section === "orchestration") return [];
  if (id === "memory") return section === "facts" ? index.facts || [] : section === "issues" ? index.issues || [] : [];
  if (id === "controller") return section === "events" ? index.events || [] : [];
  const calls = Array.isArray(index.calls) ? index.calls : [];
  let predicate;
  if (id === "lab") predicate = section === "conversation" ? apLab : section === "tools" ? apLabTool : (c) => apLab(c) || apLabTool(c);
  if (id === "meta") predicate = section === "conversation" ? apMeta : section === "engineering" ? apEngineering : section === "tools" ? apMetaTool : (c) => apMeta(c) || apEngineering(c) || apMetaTool(c);
  if (id === "sources") predicate = section === "overview" ? () => false : apSource;
  if (id === "tools") predicate = (c) => apTool(c) && !apSource(c);
  return predicate ? apUnique(calls.filter(predicate)) : [];
}

/** Canonical navigation target; an associated tool may also appear in a harness. */
export function architectureCallHome(call) {
  if (!call || typeof call !== "object") return null;
  if (apEngineering(call)) return { id: "meta", section: "engineering" };
  if (apMeta(call)) return { id: "meta", section: "conversation" };
  if (apLab(call)) return { id: "lab", section: "conversation" };
  if (apSource(call)) return { id: "sources", section: "reads" };
  if (apTool(call)) return { id: "tools", section: "executions" };
  if (apCategory(call) === "event") return { id: "controller", section: "events" };
  return null; // An unfamiliar model role has no proven architecture ownership.
}

/** Scope/record availability, never a process-health or scientific-validity badge. */
export function architectureState(index, id) {
  const component = architectureComponents[id];
  const missing = !index || !Array.isArray(index.calls)
    || id === "memory" && (!Array.isArray(index.facts) || !Array.isArray(index.issues))
    || id === "controller" && !Array.isArray(index.events);
  if (!component || missing) return { status: "unknown", label: "Unknown", note: "Source records are unavailable. No execution or connection state can be established.", countLabel: "—" };
  if (id === "lab") {
    const orchestration = index.labOrchestration;
    if (!orchestration || orchestration.schemaVersion !== "lab_orchestration_v1" || !orchestration.projectionComplete) return { status: "unknown", label: "Orchestration unavailable", note: "This snapshot has no complete orchestration projection. Model-call counts cannot establish whether task work exists or is dispatchable.", countLabel: "—" };
    const status = ["awaiting_capability", "investigating"].includes(orchestration.status) ? "partial" : orchestration.status === "recorded" ? "recorded" : "unknown";
    return { status, label: orchestration.label || "Orchestration recorded", note: orchestration.note,
      countLabel: `${orchestration.counts?.pendingWorks ?? "—"} pending work · ${orchestration.counts?.activeWaits ?? "—"} ${orchestration.counts?.activeWaits === 1 ? "wait" : "waits"}` };
  }
  const rows = architectureRows(index, id, "cost");
  const unknown = rows.some((c) => ["unknown", "dispatching", "blocked_external"].includes(c.status) || c.usage?.accountingUnknown);
  if (id === "sources") {
    const count = architectureRows(index, id, "reads").length;
    return { status: unknown ? "unknown" : count ? "recorded" : "not_started", label: unknown ? "Unresolved call" : count ? "Recorded reads" : "No recorded reads", note: component.scopeNote, countLabel: `${count} source reads` };
  }
  if (id === "memory") return { status: "partial", label: "Partial view", note: component.scopeNote, countLabel: `${index.facts.length} evidence entries · ${index.issues.length} issues` };
  if (id === "controller") return { status: index.events.length ? "recorded" : "not_started", label: index.events.length ? "Recorded events" : "No recorded events", note: component.scopeNote, countLabel: `${index.events.length} events` };
  let countLabel, note = component.scopeNote;
  if (id === "meta") countLabel = `${architectureRows(index, id, "conversation").length} Meta · ${architectureRows(index, id, "engineering").length} engineering calls`;
  if (id === "tools") {
    countLabel = `${rows.length} executions`;
    note = `${rows.length} tool Effect${rows.length === 1 ? "" : "s"} recorded. ${note}`;
  }
  return { status: unknown ? "unknown" : rows.length ? "recorded" : "not_started", label: unknown ? "Unresolved call" : rows.length ? "Recorded work" : "No calls in this run", note, countLabel };
}
