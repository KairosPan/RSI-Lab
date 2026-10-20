const NODE_IDS = [
  "controller",
  "lab",
  "executor",
  "boltz",
  "nesso",
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
    role: "流程编排",
    icon: "grid",
    description: "组装输入、校验请求、管理预算与执行状态。",
    input: ["任务与预算", "决策请求", "工具执行记录"],
    output: ["授权任务", "状态快照", "资源账本"],
    group: "campaign",
  },
  {
    id: "lab",
    name: "Lab Codex",
    role: "轮内决策",
    icon: "flask",
    description: "根据证据与剩余预算规划下一步，维护候选名单。",
    input: ["候选与工具目录", "科学记忆", "预算快照"],
    output: ["下一步请求", "名单与依据", "记忆增量"],
    group: "campaign",
  },
  {
    id: "executor",
    name: "Tool Executor",
    role: "工具执行",
    icon: "terminal",
    description: "执行 Controller 已授权的任务并记录实际返回状态。",
    input: ["已授权计算请求", "冻结的工具配置"],
    output: ["模型任务", "执行日志", "成本记录"],
    group: "campaign",
  },
  {
    id: "boltz",
    name: "Boltz-2",
    role: "相互作用预测",
    icon: "molecule",
    description: "生成工具支持的结构或预测信号，不承担最终生物活性确认。",
    input: ["受支持的分子输入", "冻结的模型配置"],
    output: ["预测证据", "置信信息", "执行状态"],
    group: "campaign",
  },
  {
    id: "nesso",
    name: "Nesso-1",
    role: "相互作用预测",
    icon: "molecule",
    description: "生成预测信号并保留失败记录；执行失败不能解释为分子无活性。",
    input: ["受支持的分子输入", "冻结的模型配置"],
    output: ["预测证据", "缺失或失败记录"],
    group: "campaign",
  },
  {
    id: "memory",
    name: "Scientific Memory",
    role: "科学记忆",
    icon: "book",
    description: "分别保存观测、预测、计算验证与假设，保留来源和历史。",
    input: ["工具原始结果", "决策依据", "记忆增量"],
    output: ["带来源的证据", "冲突与未决问题", "开发轨迹"],
    group: "campaign",
  },
  {
    id: "meta",
    name: "Meta Codex",
    role: "跨轮次改进",
    icon: "branch",
    description: "从允许的开发轨迹诊断问题，产生候选 Harness 修改。",
    input: ["已完成的开发轨迹", "当前 Harness", "允许修改的范围"],
    output: ["诊断报告", "候选 diff", "检查记录"],
    group: "cross-campaign",
  },
  {
    id: "validator",
    name: "Validation Gate",
    role: "验证与发布",
    icon: "checkCircle",
    description: "用固定规则验证候选修改；发布仅影响后续新 campaign。",
    input: ["候选 Harness", "独立验证集", "冻结的发布规则"],
    output: ["验证结果", "发布或保留决定"],
    group: "cross-campaign",
  },
];

export const runtimeEdges = [
  { from: "controller", to: "lab", label: "任务与状态", kind: "campaign" },
  {
    from: "lab",
    to: "executor",
    label: "经 Controller 校验的请求",
    kind: "campaign",
  },
  { from: "executor", to: "boltz", label: "预测任务", kind: "campaign" },
  { from: "executor", to: "nesso", label: "预测任务", kind: "campaign" },
  { from: "boltz", to: "memory", label: "预测与执行记录", kind: "campaign" },
  { from: "nesso", to: "memory", label: "预测或失败记录", kind: "campaign" },
  { from: "memory", to: "lab", label: "证据反馈", kind: "campaign" },
  {
    from: "memory",
    to: "meta",
    label: "已完成的开发轨迹",
    kind: "cross-campaign",
  },
  { from: "meta", to: "validator", label: "候选版本", kind: "cross-campaign" },
  {
    from: "validator",
    to: "controller",
    label: "后续 campaign 使用的版本",
    kind: "cross-campaign",
  },
];

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function boundedText(value, limit, name) {
  if (typeof value !== "string")
    throw new Error(`运行状态字段格式错误：${name}`);
  return value
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, "")
    .trim()
    .slice(0, limit);
}

function requiredText(value, limit, name) {
  const text = boundedText(value, limit, name);
  if (!text) throw new Error(`运行状态缺少字段：${name}`);
  return text;
}

function arrayField(value, limit, name) {
  if (!Array.isArray(value) || value.length > limit)
    throw new Error(`运行状态列表格式错误：${name}`);
  return value;
}

function timestamp(value, now, name) {
  if (
    typeof value !== "string" ||
    value.length > 32 ||
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z$/.test(value)
  ) {
    throw new Error(`运行状态时间格式错误：${name}`);
  }
  const time = Date.parse(value);
  if (!Number.isFinite(time)) throw new Error(`运行状态时间格式错误：${name}`);
  const iso = new Date(time).toISOString();
  // Reject date rollover such as February 30 instead of silently normalizing it.
  if (iso.slice(0, 19) !== value.slice(0, 19))
    throw new Error(`运行状态时间格式错误：${name}`);
  if (time > now) throw new Error(`运行状态包含未来时间：${name}`);
  return { time, iso };
}

/**
 * Normalize an externally produced snapshot into the fixed eight-node topology.
 * Missing nodes and unfamiliar statuses are unknown. Structural mistakes reject
 * the entire snapshot; stale data can never retain a running/healthy status.
 */
export function normalizeRuntime(payload, now = Date.now()) {
  if (!Number.isFinite(now) || now < 0) throw new Error("运行状态校验时间无效");
  if (!isRecord(payload) || !["live", "demo"].includes(payload.source)) {
    throw new Error("运行状态快照格式错误");
  }
  const campaignId = requiredText(payload.campaignId, 64, "campaignId");
  const harness = requiredText(payload.harness, 64, "harness");
  const observed = timestamp(payload.observedAt, now, "observedAt");
  const stale = now - observed.time > MAX_SNAPSHOT_AGE;
  const byId = new Map();
  const ignoredNodeIds = [];

  for (const node of arrayField(payload.nodes, 64, "nodes")) {
    if (!isRecord(node)) throw new Error("运行节点格式错误");
    const rawId = requiredText(node.id, 64, "node.id");
    if (!NODE_IDS.includes(rawId)) {
      ignoredNodeIds.push(rawId);
      continue;
    }
    if (byId.has(rawId)) throw new Error("运行节点重复");
    const rawStatus = requiredText(node.status, 32, "node.status");
    const detail = boundedText(node.detail, 400, "node.detail");
    const task = boundedText(node.task, 200, "node.task");
    const heartbeat =
      node.heartbeatAt === null
        ? null
        : timestamp(node.heartbeatAt, now, "node.heartbeatAt");
    if (heartbeat && heartbeat.time > observed.time) {
      throw new Error("节点心跳晚于快照时间");
    }
    let status = NODE_STATUSES.has(rawStatus) ? rawStatus : "unknown";
    if (!heartbeat) status = "unknown";
    if (stale || (heartbeat && now - heartbeat.time > MAX_HEARTBEAT_AGE))
      status = "offline";
    const metrics = arrayField(node.metrics, 12, "node.metrics").map(
      (metric) => {
        if (!isRecord(metric)) throw new Error("运行指标格式错误");
        const label = requiredText(metric.label, 48, "metric.label");
        const value =
          typeof metric.value === "number"
            ? Number.isFinite(metric.value)
              ? metric.value
              : null
            : boundedText(metric.value, 100, "metric.value");
        if (value === null) throw new Error("运行指标数值无效");
        return { label, value };
      },
    );
    const events = arrayField(node.events, 20, "node.events").map((event) => {
      if (!isRecord(event)) throw new Error("运行事件格式错误");
      const time = timestamp(event.time, now, "event.time");
      if (time.time > observed.time) throw new Error("运行事件晚于快照时间");
      const level = requiredText(event.level, 16, "event.level");
      if (!EVENT_LEVELS.has(level)) throw new Error("运行事件级别无效");
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
    if (node.progress !== undefined) {
      if (
        typeof node.progress !== "number" ||
        !Number.isFinite(node.progress) ||
        node.progress < 0 ||
        node.progress > 100
      ) {
        throw new Error("运行进度必须介于 0 到 100");
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
          ? "快照已过期，当前状态不可确认。"
          : "快照未报告此节点，当前状态未知。",
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
  const status = {
    controller: "running",
    lab: stage === 0 ? "running" : "idle",
    executor: stage === 1 ? "running" : "queued",
    boltz: stage === 1 ? "running" : stage === 2 ? "idle" : "queued",
    nesso: "error",
    memory: stage === 2 ? "running" : "idle",
    meta: "queued",
    validator: "idle",
  };
  const details = {
    controller: "DEV-004 使用已冻结的 H1，正在整理任务状态。",
    lab: stage === 0 ? "根据证据缺口规划下一批计算。" : "等待工具与记忆反馈。",
    executor:
      stage === 1 ? "执行已授权的 Boltz-2 任务。" : "等待下一批已授权请求。",
    boltz:
      stage === 1
        ? "CMP-004 的预测任务进行中。"
        : stage === 2
          ? "模拟预测记录已返回。"
          : "CMP-004 的预测任务等待执行。",
    nesso: "CMP-008 任务遇到 OOM；预测缺失，不代表分子无活性。",
    memory:
      stage === 2
        ? "关联预测与失败证据，更新未决问题。"
        : "原始证据与派生结论分别保存。",
    meta: "H2 候选等待独立验证，不修改活动中的 DEV-004。",
    validator: "尚未开始候选版本的配对验证。",
  };
  const tasks = {
    controller: "维护 DEV-004 状态",
    lab: "补齐候选证据",
    executor: "等待或分派授权任务",
    boltz: "CMP-004 · 模拟预测",
    nesso: "CMP-008 · 模拟失败记录",
    memory: "证据追溯与冲突保留",
    meta: "H2 · 失败反馈机制候选修改",
    validator: "VAL-003 · 待开始",
  };
  const nodeMetrics = {
    controller: { label: "冻结版本（模拟）", value: "H1" },
    lab: { label: "未决问题（模拟）", value: "2 issues" },
    executor: {
      label: "任务队列（模拟）",
      value: stage === 1 ? "1 running" : "1 queued",
    },
    boltz: {
      label: "预测任务（模拟）",
      value: stage === 1 ? "48%" : stage === 2 ? "returned" : "queued",
    },
    nesso: { label: "执行异常（模拟）", value: "OOM" },
    memory: { label: "证据记录（模拟）", value: "33 records" },
    meta: { label: "候选版本（模拟）", value: "H2" },
    validator: { label: "验证批次（模拟）", value: "VAL-003" },
  };
  const eventAges = {
    nesso: 2_000,
    lab: 5_000,
    controller: 8_000,
    executor: 12_000,
    boltz: 16_000,
    memory: 20_000,
    meta: 24_000,
    validator: 28_000,
  };
  const eventMessages = {
    nesso: "CMP-008 遇到 OOM，保留失败记录与未知预测，不推断分子无活性。",
    lab: "已提出补齐 CMP-004 证据的下一批计算计划。",
    controller: "已校验并授权任务，DEV-004 保持冻结的 H1。",
  };
  return {
    source: "demo",
    campaignId: "DEV-004",
    harness: "H1",
    observedAt,
    nodes: NODE_IDS.map((id) => ({
      id,
      status: status[id],
      detail: details[id],
      heartbeatAt: observedAt,
      task: tasks[id],
      ...(id === "boltz" && stage === 1 ? { progress: 48 } : {}),
      metrics: [nodeMetrics[id]],
      events: [
        {
          time: new Date(now - eventAges[id]).toISOString(),
          message: eventMessages[id] ?? details[id],
          level: id === "nesso" ? "error" : "info",
        },
      ],
    })),
  };
}

export async function fetchRuntime(signal) {
  if (!globalThis.location || globalThis.location.protocol === "file:") {
    throw new Error("本地文件预览无法连接实时状态，请通过项目服务器打开页面。");
  }
  if (!["http:", "https:"].includes(globalThis.location.protocol)) {
    throw new Error("当前页面协议不支持实时状态。");
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
        ? "尚未接入可用的运行状态文件。"
        : "无法读取运行状态，请稍后重试。",
    );
  }
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error("运行状态服务返回了无效数据。");
  }
  if (!isRecord(payload) || payload.source !== "live") {
    throw new Error("运行状态服务返回的是演示或未知来源，不能作为真实连接。");
  }
  return normalizeRuntime(payload);
}
