import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const html = await readFile(resolve(root, "index.html"), "utf8");
const css = await readFile(resolve(root, "src/styles.css"), "utf8");
const files = await Promise.all(
  ["data.js", "icons.js", "runtime.js", "workbench.js", "app.js"].map((file) =>
    readFile(resolve(root, "src", file), "utf8"),
  ),
);
// This prototype has local modules and no third-party dependencies.
// Inline the build so the review artifact also works without a web server.
const script = files
  .map((source) =>
    source
      .replace(
        /^import\s[\s\S]*?from\s+["']\.\/(?:data|icons|runtime|workbench)\.js["'];?\s*$/gm,
        "",
      )
      .replace(/^export /gm, ""),
  )
  .join("\n");
const output = html
  .replace(
    '<link rel="stylesheet" href="./src/styles.css" />',
    `<style>${css}</style>`,
  )
  .replace(
    '<script type="module" src="./src/app.js"></script>',
    `<script type="module">${script.replace(/<\/script/gi, "<\\/script")}</script>`,
  );
await mkdir(resolve(root, "dist"), { recursive: true });
await writeFile(resolve(root, "dist/index.html"), output);
console.log(
  `Built self-contained preview: ${resolve(root, "dist/index.html")}`,
);
