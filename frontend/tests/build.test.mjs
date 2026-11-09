import test from "node:test";
import assert from "node:assert/strict";
import { cp, mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
const exec = promisify(execFile);

test("build excludes local imported traces and removes stale distribution records", async () => {
  const temp = await mkdtemp(resolve(tmpdir(), "rsi-build-fixture-"));
  try {
    await mkdir(resolve(temp, "scripts"));
    await cp(new URL("../src", import.meta.url), resolve(temp, "src"), { recursive: true });
    await cp(new URL("../index.html", import.meta.url), resolve(temp, "index.html"));
    await cp(new URL("../scripts/build.mjs", import.meta.url), resolve(temp, "scripts/build.mjs"));
    await mkdir(resolve(temp, "data/agent-trace/calls"), { recursive: true });
    await mkdir(resolve(temp, "dist/data/agent-trace/calls"), { recursive: true });
    const marker = "LOCAL_TRACE_MUST_NOT_SHIP_123";
    await writeFile(resolve(temp, "data/agent-trace/index.json"), JSON.stringify({ marker }));
    await writeFile(resolve(temp, "data/agent-trace/calls/private.json"), marker);
    await writeFile(resolve(temp, "dist/data/agent-trace/calls/old.json"), marker);
    await exec(process.execPath, [resolve(temp, "scripts/build.mjs")]);
    assert.deepEqual(await readdir(resolve(temp, "dist")), ["index.html"]);
    const html = await readFile(resolve(temp, "dist/index.html"), "utf8");
    assert.ok(!html.includes(marker));
    assert.match(html, /No run selected/);
    const script = html.match(/<script type="module">([\s\S]*?)<\/script>/)?.[1];
    assert.ok(script);
    await writeFile(resolve(temp, "bundle.mjs"), script);
    await exec(process.execPath, ["--check", resolve(temp, "bundle.mjs")]);
    assert.ok(!html.includes("experiment-data.js"));
    assert.equal(await readFile(resolve(temp, "data/agent-trace/calls/private.json"), "utf8"), marker);
  } finally { await rm(temp, { recursive: true, force: true }); }
});
