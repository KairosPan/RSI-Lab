import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";
import { resolve, dirname } from "node:path";

const exec = promisify(execFile);
const here = dirname(fileURLToPath(import.meta.url));
const source = process.argv[2] || process.env.RSI_LAB_RUN_DIR;
if (!source) {
  console.error("Usage: node scripts/import-agent-trace.mjs /absolute/path/to/run");
  process.exitCode = 1;
} else {
  const output = resolve(here, "../data/agent-trace");
  try {
    const { stdout } = await exec(process.env.RSI_LAB_PYTHON || "python3", [
      resolve(here, "read-agent-trace.py"), "--run", resolve(source),
      "--mode", "recorded", "--export", output,
    ], { timeout: 30000, maxBuffer: 1024 * 1024 });
    console.log(stdout.trim());
  } catch {
    console.error("Import failed. Check the configured run and its immutable objects. Original files were not modified.");
    process.exitCode = 1;
  }
}
