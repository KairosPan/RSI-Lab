import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const html = await readFile(resolve(root, "index.html"), "utf8");
const [css, agentCss] = await Promise.all(["styles.css", "agent-canvas.css"].map(file => readFile(resolve(root, "src", file), "utf8")));
const files = await Promise.all([
  "icons.js", "architecture.js", "lab-orchestration.js", "agent-canvas.js", "app.js",
].map(file => readFile(resolve(root, "src", file), "utf8")));
const script = files.map(source => source
  .replace(/^import\s[\s\S]*?from\s+["']\.\/(?:icons|agent-canvas|architecture|lab-orchestration)\.js["'];?\s*$/gm, "")
  .replace(/^export /gm, "")
).join("\n");
const output = html
  .replace('<link rel="stylesheet" href="./src/agent-canvas.css" />', `<style>${agentCss}</style>`)
  .replace('<link rel="stylesheet" href="./src/styles.css" />', `<style>${css}</style>`)
  .replace('<script type="module" src="./src/app.js"></script>', `<script type="module">${script.replace(/<\/script/gi, "<\\/script")}</script>`);

// A build contains application code only. Never include locally imported traces,
// and remove data left by a previous preview/deployment before rebuilding.
await rm(resolve(root, "dist"), { recursive: true, force: true });
await mkdir(resolve(root, "dist"));
await writeFile(resolve(root, "dist/index.html"), output);
console.log(`Built empty workspace: ${resolve(root, "dist/index.html")}`);
