import { open } from "node:fs/promises";
import { normalizeRuntime } from "./src/runtime.js";

export const MAX_RUNTIME_BYTES = 1024 * 1024;

function sendJson(req, res, status, value, extraHeaders = {}) {
  const body = JSON.stringify(value);
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    ...extraHeaders,
  });
  res.end(req.method === "HEAD" ? undefined : body);
}

async function readBoundedSnapshot(path) {
  const file = await open(path, "r");
  try {
    const metadata = await file.stat();
    if (!metadata.isFile() || metadata.size > MAX_RUNTIME_BYTES)
      throw new Error("Invalid telemetry file");
    // Bound the read even if an external producer changes the file after stat().
    const buffer = Buffer.allocUnsafe(MAX_RUNTIME_BYTES + 1);
    let total = 0;
    while (total < buffer.length) {
      const { bytesRead } = await file.read(
        buffer,
        total,
        buffer.length - total,
        total,
      );
      if (bytesRead === 0) break;
      total += bytesRead;
    }
    if (total > MAX_RUNTIME_BYTES) throw new Error("Telemetry file too large");
    const json = new TextDecoder("utf-8", { fatal: true }).decode(
      buffer.subarray(0, total),
    );
    return JSON.parse(json);
  } finally {
    await file.close();
  }
}

/**
 * Read-only endpoint adapter. Producers write the configured file externally.
 * This handler neither starts agents nor scans processes nor changes files.
 */
export async function handleRuntimeRequest(req, res, options = {}) {
  if (!["GET", "HEAD"].includes(req.method)) {
    sendJson(
      req,
      res,
      405,
      { error: "This endpoint is read-only." },
      { Allow: "GET, HEAD" },
    );
    return;
  }
  const runtimeFile = options.runtimeFile ?? process.env.RSI_LAB_RUNTIME_FILE;
  if (typeof runtimeFile !== "string" || !runtimeFile.trim()) {
    sendJson(req, res, 503, { error: "No runtime source is configured." });
    return;
  }
  try {
    const payload = await readBoundedSnapshot(runtimeFile);
    if (!payload || payload.source !== "live")
      throw new Error("Live telemetry source required");
    const normalized = normalizeRuntime(payload, options.now ?? Date.now());
    sendJson(req, res, 200, normalized);
  } catch {
    // Never expose file paths, stack traces, or raw producer content in errors.
    sendJson(req, res, 503, {
      error:
        "Runtime telemetry is unavailable. Check the snapshot format and timestamps.",
    });
  }
}
