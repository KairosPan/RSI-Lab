import { createServer } from "node:http";
import { readFile, stat } from "node:fs/promises";
import { dirname, resolve, extname, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { handleRuntimeRequest } from "./runtime-server.mjs";
import { handleTraceRequest } from "./trace-server.mjs";

const root = dirname(fileURLToPath(import.meta.url));
const port = Number(process.env.PORT || 4173);
const mime = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
};

const server = createServer(async (req, res) => {
  try {
    if (!["GET", "HEAD"].includes(req.method)) {
      res.writeHead(405);
      res.end();
      return;
    }
    const pathname = decodeURIComponent(
      new URL(req.url, "http://localhost").pathname,
    );
    if (pathname === "/api/trace" || pathname.startsWith("/api/trace/")) {
      await handleTraceRequest(req, res);
      return;
    }
    if (pathname === "/api/runtime") {
      await handleRuntimeRequest(req, res);
      return;
    }
    const file = resolve(
      root,
      `.${pathname === "/" ? "/index.html" : pathname}`,
    );
    if (
      !file.startsWith(root + sep) ||
      pathname.split("/").some((part) => part.startsWith("."))
    ) {
      res.writeHead(403);
      res.end("Forbidden");
      return;
    }
    if (!(await stat(file)).isFile()) {
      res.writeHead(404);
      res.end("Not found");
      return;
    }
    const body = await readFile(file);
    res.writeHead(200, {
      "Content-Type": mime[extname(file)] || "application/octet-stream",
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
    });
    res.end(req.method === "HEAD" ? undefined : body);
  } catch (error) {
    res.writeHead(error.code === "ENOENT" ? 404 : 400);
    res.end(error.code === "ENOENT" ? "Not found" : "Bad request");
  }
});
server.on("error", (error) => {
  console.error(error.message);
  process.exitCode = 1;
});
server.listen(port, "127.0.0.1", () =>
  console.log(`RSI-Lab preview: http://127.0.0.1:${port}`),
);
for (const signal of ["SIGINT", "SIGTERM"])
  process.on(signal, () => server.close(() => process.exit(0)));
