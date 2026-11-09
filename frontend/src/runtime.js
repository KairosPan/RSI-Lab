const NODE_IDS = [
  "controller",
  "lab",
  "executor",
  "gnina",
  "posecheck",
  "memory",
  "meta",
  "validator",
];
const NODE_STATUSES = new Set([
  "running",
  "queued",
  "idle",
  "error",
  "offline",
  "unknown",
]);
const EVENT_LEVELS = new Set(["info", "success", "warning", "error"]);
const MAX_HEARTBEAT_AGE = 30_000;
const MAX_SNAPSHOT_AGE = 60_000;

export const runtimeNodes = [
  {
    id: "controller",
    name: "Controller",
    role: "Orchestration",
    icon: "grid",
    description:
      "Prepares inputs, checks requests, and records budget and execution state.",
    input: ["Task and budget", "Decision requests", "Execution records"],
    output: ["Authorized jobs", "State snapshots", "Resource ledger"],
    group: "campaign",
  },
  {
    id: "lab",
    name: "Lab Codex",
    role: "Campaign decisions",
    icon: "flask",
    description:
      "Plans calculations from available evidence and maintains the candidate shortlist.",
    input: ["Candidates and tools", "Scientific memory", "Budget snapshot"],
    output: ["Action requests", "Shortlist and references", "Memory updates"],
    group: "campaign",
  },
  {
    id: "executor",
    name: "Tool Executor",
    role: "Numerical jobs",
    icon: "terminal",
    description:
      "Runs authorized numerical jobs and records results, failures, and resource use.",
    input: ["Authorized requests", "Frozen tool profiles"],
    output: ["Numerical jobs", "Execution logs", "Resource records"],
    group: "campaign",
  },
  {
    id: "gnina",
    name: "GNINA",
    role: "Docking and rescoring",
    icon: "molecule",
    description:
      "Produces docking energy and CNN scores under the fixed computational protocol. These are computational signals, not wet-lab confirmation.",
    input: ["Prepared ligands", "Receptor and frozen protocol"],
    output: [
      "Docked poses",
      "CNN scores and docking energy",
      "Execution records",
    ],
    group: "campaign",
  },
  {
    id: "posecheck",
    name: "PoseBusters",
    role: "Pose validation",
    icon: "checkCircle",
    description:
      "Checks the validity of generated poses. A failed pose check is not a conclusion about biological activity.",
    input: ["Generated poses", "Frozen validity checks"],
    output: ["Pose validity flags", "Check results", "Failure records"],
    group: "campaign",
  },
  {
    id: "memory",
    name: "Scientific Memory",
    role: "Evidence and interpretation",
    icon: "book",
    description:
      "Keeps source evidence separate from derived interpretations, with review state and history.",
    input: ["Raw tool records", "Decision references", "Memory updates"],
    output: [
      "Traceable evidence",
      "Conflicts and open questions",
      "Development traces",
    ],
    group: "campaign",
  },
  {
    id: "meta",
    name: "Meta Codex",
    role: "Cross-campaign proposals",
    icon: "branch",
    description:
      "Diagnoses permitted development traces and proposes a candidate Harness change. A proposal is not a released version.",
    input: [
      "Completed development traces",
      "Current Harness",
      "Allowed change scope",
    ],
    output: ["Diagnosis", "Candidate change", "Check records"],
    group: "cross-campaign",
  },
  {
    id: "validator",
    name: "Validation Gate",
    role: "Validation and release",
    icon: "checkCircle",
    description:
      "Applies fixed release rules. A new version can affect future campaigns only after an authorized release.",
    input: [
      "Candidate Harness",
      "Independent validation pools",
      "Frozen release rules",
    ],
    output: ["Validation records", "Release or retain decision"],
    group: "cross-campaign",
  },
];

export const runtimeEdges = [
  { from: "controller", to: "lab", label: "Task and state", kind: "campaign" },
  {
    from: "lab",
    to: "executor",
    label: "Controller-authorized requests",
    kind: "campaign",
  },
  {
    from: "executor",
    to: "gnina",
    label: "Docking and rescoring jobs",
    kind: "campaign",
  },
  {
    from: "gnina",
    to: "posecheck",
    label: "Generated poses",
    kind: "campaign",
  },
  {
    from: "gnina",
    to: "memory",
    label: "Scores and execution records",
    kind: "campaign",
  },
  {
    from: "posecheck",
    to: "memory",
    label: "Pose validity evidence",
    kind: "campaign",
  },
  { from: "memory", to: "lab", label: "Evidence feedback", kind: "campaign" },
  {
    from: "memory",
    to: "meta",
    label: "Completed development traces",
    kind: "cross-campaign",
  },
  {
    from: "meta",
    to: "validator",
    label: "Candidate change",
    kind: "cross-campaign",
  },
  {
    from: "validator",
    to: "controller",
    label: "Authorized version for future campaigns",
    kind: "cross-campaign",
  },
];

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function boundedText(value, limit, name) {
  if (typeof value !== "string")
    throw new Error(`Invalid runtime field: ${name}`);
  return value
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, "")
    .trim()
    .slice(0, limit);
}

function requiredText(value, limit, name) {
  const text = boundedText(value, limit, name);
  if (!text) throw new Error(`Missing runtime field: ${name}`);
  return text;
}

function arrayField(value, limit, name) {
  if (!Array.isArray(value) || value.length > limit)
    throw new Error(`Invalid runtime list: ${name}`);
  return value;
}

function timestamp(value, now, name) {
  if (
    typeof value !== "string" ||
    value.length > 32 ||
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z$/.test(value)
  ) {
    throw new Error(`Invalid runtime timestamp: ${name}`);
  }
  const time = Date.parse(value);
  if (!Number.isFinite(time))
    throw new Error(`Invalid runtime timestamp: ${name}`);
  const iso = new Date(time).toISOString();
  // Reject date rollover such as February 30 instead of silently normalizing it.
  if (iso.slice(0, 19) !== value.slice(0, 19))
    throw new Error(`Invalid runtime timestamp: ${name}`);
  if (time > now)
    throw new Error(`Runtime timestamp is in the future: ${name}`);
  return { time, iso };
}

/**
 * Normalize an externally produced snapshot into the fixed eight-node topology.
 * Missing nodes and unfamiliar statuses are unknown. Structural mistakes reject
 * the entire snapshot. Only live data is subject to heartbeat expiry; recorded
 * completion is historical evidence and never establishes process liveness.
 */
export function normalizeRuntime(payload, now = Date.now()) {
  if (!Number.isFinite(now) || now < 0)
    throw new Error("Invalid runtime validation time");
  if (
    !isRecord(payload) ||
    !["live", "demo", "recorded"].includes(payload.source)
  ) {
    throw new Error("Invalid runtime snapshot");
  }
  const campaignId = requiredText(payload.campaignId, 64, "campaignId");
  const harness = requiredText(payload.harness, 64, "harness");
  const observed = timestamp(payload.observedAt, now, "observedAt");
  const isLive = payload.source === "live";
  const stale = isLive && now - observed.time > MAX_SNAPSHOT_AGE;
  const byId = new Map();
  const ignoredNodeIds = [];

  for (const node of arrayField(payload.nodes, 64, "nodes")) {
    if (!isRecord(node)) throw new Error("Invalid runtime node");
    const rawId = requiredText(node.id, 64, "node.id");
    if (!NODE_IDS.includes(rawId)) {
      ignoredNodeIds.push(rawId);
      continue;
    }
    if (byId.has(rawId)) throw new Error("Duplicate runtime node");
    const rawStatus = requiredText(node.status, 32, "node.status");
    const detail = boundedText(node.detail, 400, "node.detail");
    const task = boundedText(node.task, 200, "node.task");
    const heartbeat =
      node.heartbeatAt === null
        ? null
        : timestamp(node.heartbeatAt, now, "node.heartbeatAt");
    if (heartbeat && heartbeat.time > observed.time) {
      throw new Error("Node heartbeat is later than the snapshot");
    }
    if (isLive && rawStatus === "completed") {
      throw new Error(
        "Completed is a recorded-result status, not a live process state",
      );
    }
    let status =
      NODE_STATUSES.has(rawStatus) || (!isLive && rawStatus === "completed")
        ? rawStatus
        : "unknown";
    if (isLive && !heartbeat) status = "unknown";
    if (
      isLive &&
      (stale || (heartbeat && now - heartbeat.time > MAX_HEARTBEAT_AGE))
    )
      status = "offline";
    const metrics = arrayField(node.metrics, 12, "node.metrics").map(
      (metric) => {
        if (!isRecord(metric)) throw new Error("Invalid runtime metric");
        const label = requiredText(metric.label, 48, "metric.label");
        const value =
          typeof metric.value === "number"
            ? Number.isFinite(metric.value)
              ? metric.value
              : null
            : boundedText(metric.value, 100, "metric.value");
        if (value === null) throw new Error("Invalid runtime metric value");
        return { label, value };
      },
    );
    const events = arrayField(node.events, 20, "node.events").map((event) => {
      if (!isRecord(event)) throw new Error("Invalid runtime event");
      const time = timestamp(event.time, now, "event.time");
      if (time.time > observed.time)
        throw new Error("Runtime event is later than the snapshot");
      const level = requiredText(event.level, 16, "event.level");
      if (!EVENT_LEVELS.has(level))
        throw new Error("Invalid runtime event level");
      return {
        time: time.iso,
        message: requiredText(event.message, 400, "event.message"),
        level,
      };
    });
    const normalized = {
      id: rawId,
      status,
      detail,
      heartbeatAt: heartbeat?.iso ?? null,
      task,
      metrics,
      events,
    };
    if (!isLive && node.statusLabel !== undefined) {
      normalized.statusLabel = requiredText(
        node.statusLabel,
        40,
        "node.statusLabel",
      );
    }
    if (node.progress !== undefined) {
      if (
        typeof node.progress !== "number" ||
        !Number.isFinite(node.progress) ||
        node.progress < 0 ||
        node.progress > 100
      ) {
        throw new Error("Runtime progress must be between 0 and 100");
      }
      normalized.progress = node.progress;
    }
    byId.set(rawId, normalized);
  }

  const nodes = NODE_IDS.map(
    (id) =>
      byId.get(id) || {
        id,
        status: stale ? "offline" : "unknown",
        detail: stale
          ? "The snapshot is stale; current state is unconfirmed."
          : "This node was not reported; its state is unknown.",
        heartbeatAt: null,
        task: "",
        metrics: [],
        events: [],
      },
  );
  return {
    source: payload.source,
    campaignId,
    harness,
    observedAt: observed.iso,
    stale,
    ageMs: now - observed.time,
    ignoredNodeIds,
    nodes,
  };
}

export function demoRuntime(tick = 0) {
  const now = Date.now();
  const observedAt = new Date(now).toISOString();
  const stage = Math.abs(Math.trunc(Number.isFinite(tick) ? tick : 0)) % 3;
  const statuses = {
    controller: "running",
    lab: stage === 0 ? "running" : "idle",
    executor: stage === 1 ? "running" : "queued",
    gnina: stage === 1 ? "running" : stage === 2 ? "completed" : "queued",
    posecheck: "error",
    memory: stage === 2 ? "running" : "idle",
    meta: "queued",
    validator: "idle",
  };
  const details = {
    controller: "Simulated campaign state; no real jobs are started.",
    lab:
      stage === 0
        ? "Planning the next simulated calculation request."
        : "Waiting for simulated evidence.",
    executor:
      stage === 1
        ? "Simulating an authorized GNINA job."
        : "Waiting for a simulated authorized request.",
    gnina:
      stage === 1
        ? "Simulated docking is in progress."
        : stage === 2
          ? "Simulated docking output returned."
          : "Simulated docking job is queued.",
    posecheck:
      "Simulated pose check failed. This does not establish molecular inactivity.",
    memory:
      stage === 2
        ? "Linking simulated evidence and open questions."
        : "Keeping simulated evidence separate from interpretations.",
    meta: "A simulated candidate change awaits independent validation.",
    validator: "No simulated release has been authorized.",
  };
  const ages = {
    posecheck: 2000,
    lab: 5000,
    controller: 8000,
    executor: 12000,
    gnina: 16000,
    memory: 20000,
    meta: 24000,
    validator: 28000,
  };
  return {
    source: "demo",
    campaignId: "DEMO-WORKFLOW",
    harness: "Demo Harness",
    observedAt,
    nodes: NODE_IDS.map((id) => ({
      id,
      status: statuses[id],
      detail: details[id],
      heartbeatAt: null,
      task: "Simulated workflow only",
      metrics: [{ label: "Demo state", value: statuses[id] }],
      events: [
        {
          time: new Date(now - ages[id]).toISOString(),
          message: details[id],
          level: id === "posecheck" ? "error" : "info",
        },
      ],
    })),
  };
}

export async function fetchRuntime(signal) {
  if (!globalThis.location || globalThis.location.protocol === "file:") {
    throw new Error(
      "File previews cannot connect to live telemetry. Open the project server instead.",
    );
  }
  if (!["http:", "https:"].includes(globalThis.location.protocol)) {
    throw new Error("This page protocol does not support live telemetry.");
  }
  const response = await fetch("/api/runtime", {
    method: "GET",
    signal,
    cache: "no-store",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(
      response.status === 503
        ? "No usable runtime snapshot is connected."
        : "Unable to read runtime state. Try again later.",
    );
  }
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error("The runtime service returned invalid data.");
  }
  if (!isRecord(payload) || payload.source !== "live") {
    throw new Error(
      "The runtime service did not return a live source; it cannot establish a live connection.",
    );
  }
  return normalizeRuntime(payload);
}
