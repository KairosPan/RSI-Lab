/** Local, read-only ledger bridge. No model/tool execution or arbitrary paths. */
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const exec = promisify(execFile);
const reader = resolve(dirname(fileURLToPath(import.meta.url)), "scripts/read-agent-trace.py");
const ID = /^effect_[a-f0-9]{24}$/;
const SHA = /^[a-f0-9]{64}$/;

function json(req, res, status, value, headers = {}) {
  const body = JSON.stringify(value);
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
    "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", ...headers,
  });
  res.end(req.method === "HEAD" ? undefined : body);
}

export async function handleTraceRequest(req, res, options = {}) {
  if (!["GET", "HEAD"].includes(req.method)) {
    json(req, res, 405, { error: "Trace endpoints are read-only." }, { Allow: "GET, HEAD" });
    return;
  }
  const source = options.runDir ?? process.env.RSI_LAB_RUN_DIR;
  if (typeof source !== "string" || !source.trim()) {
    json(req, res, 503, { error: "No ledger source configured. Use the recorded snapshot or configure RSI_LAB_RUN_DIR." });
    return;
  }
  let pathname;
  try { pathname = decodeURIComponent(new URL(req.url, "http://localhost").pathname); }
  catch { json(req, res, 400, { error: "Invalid trace URL." }); return; }
  const parts = pathname.split("/").filter(Boolean);
  const args = [reader, "--run", resolve(source), "--mode", "ledger"];
  let artifact = false;
  if (pathname === "/api/trace") {
    // Index only.
  } else if (parts.length === 4 && parts[0] === "api" && parts[1] === "trace" && parts[2] === "calls" && ID.test(parts[3])) {
    args.push("--call", parts[3]);
  } else if (parts.length === 5 && parts[0] === "api" && parts[1] === "trace" && parts[2] === "artifacts" && ID.test(parts[3]) && SHA.test(parts[4])) {
    args.push("--call", parts[3], "--artifact", parts[4]);
    artifact = true;
  } else {
    json(req, res, 400, { error: "Invalid trace identity." });
    return;
  }
  try {
    const { stdout } = await exec(options.python ?? process.env.RSI_LAB_PYTHON ?? "python3", args, {
      timeout: 20000, maxBuffer: artifact ? 64 * 1024 * 1024 : 12 * 1024 * 1024,
      encoding: artifact ? "buffer" : "utf8",
    });
    if (artifact) {
      res.writeHead(200, {
        "Content-Type": "application/octet-stream", "Content-Length": stdout.length,
        "Content-Disposition": `attachment; filename="${parts[3]}-${parts[4].slice(0, 12)}.txt"`,
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
      });
      res.end(req.method === "HEAD" ? undefined : stdout);
    } else {
      json(req, res, 200, JSON.parse(stdout));
    }
  } catch {
    json(req, res, 503, { error: "Ledger data is unavailable or changed during reading. No calls were executed; retry the read." });
  }
}
