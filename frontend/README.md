# RSI-Lab Architecture Workspace

A read-only English interface for **Lab Harness**, **Meta-Harness**, **Scientific Memory**, **Source Tools** and **Computational Tools**. The controller appears separately as infrastructure. Engineering calls are an on-demand backend within Meta-Harness.

The workspace starts with **No run selected**. It includes no experiment outputs, conversations, screenshots or result snapshots. You can explore the architecture before connecting a source. Component relationships describe responsibilities, not an observed execution sequence.

## Run locally

Requires Node.js 20 or later. No npm dependencies are required. Python 3 (standard library only) is needed to read or import SQLite ledgers.

```sh
cd frontend
npm run dev
```

Open `http://127.0.0.1:4173`. Set `PORT` to use a different port.

## Connect your own run

Start the server with the directory containing your `ledger.sqlite3` and `objects/`:

```sh
RSI_LAB_RUN_DIR=/absolute/path/to/your/run npm run dev
```

Select **Connect ledger** in the workspace. `GET /api/trace` reads the ledger and `GET /api/trace/calls/<effect_id>` reads the selected call. `RSI_LAB_PYTHON` can select a Python executable. Reading does not dispatch, resume or retry work, and ledger timestamps are not process heartbeats.

Alternatively, explicitly import a local snapshot:

```sh
npm run import:trace -- /absolute/path/to/your/run
```

Then select **Imported snapshot**. The importer writes ignored files under `data/agent-trace/`; these can contain complete model inputs, responses and tool previews. They remain local unless you deliberately distribute them. The original run is never modified.

The reader copies a consistent DB/WAL snapshot to a temporary directory and queries it read-only. It preserves committed WAL data and rejects unsupported or inconsistent records. The local server binds only to loopback; its APIs validate identifiers and object hashes and do not accept arbitrary file paths. It is not an authenticated public service.

## Read the workspace

- **Lab Harness:** persisted plans, queued work, waits, issues and unknown effects, followed by decisions and linked tools. Missing orchestration fields remain unknown.
- **Meta-Harness:** review/issue decisions, engineering proposals and explicitly attributed tool calls. A proposal is separate from execution and acceptance.
- **Scientific Memory:** the fact and issue projection available in the ledger. This is a partial view; an issue hypothesis is not a validated conclusion.
- **Source Tools:** source reads explicitly identified by `Effect.metadata.tool_role: "source"`. Providers are supplied by the host; recorded reads do not imply a live connection.
- **Computational Tools:** recorded tool effects and receipts. A successful process does not establish scientific acceptance.

Unknown costs stay unknown. Shared tool records can appear in multiple component views; do not sum component totals. A fresh read of the ledger does not prove that an execution process is currently alive.

The graph supports node dragging, panning, zoom, fit and reset. Use the component selector as an alternative to spatial navigation. The inspector moves below the graph on narrow screens. The viewer has no mutation or execution controls.

## Build and verify

```sh
npm run check
npm test
npm run build
```

The build clears `dist/` and produces `dist/index.html` containing application code only. It does not copy `data/`, even after a local import. Serve it with:

```sh
python3 -m http.server 4174 --bind 127.0.0.1 --directory dist
```

An empty static build works without a ledger. To inspect records, use the development server above, or deliberately provision a compatible snapshot at `data/agent-trace/` under a deployment you control. A static server does not supply the ledger APIs.

Tests use synthetic fixtures, temporary SQLite databases and mocked responses. Optional browser checks use a locally installed Playwright package:

```sh
RSI_QA_URL=http://127.0.0.1:4173 node tests/browser-canvas.mjs
```

Set `PLAYWRIGHT_MODULE` to an installed Playwright module path if it is not on Node's module search path. Browser checks inject synthetic trace responses and run no model or scientific computation.

## Optional telemetry endpoint

The independent `/api/runtime` bridge can read a JSON snapshot produced by your own process using `RSI_LAB_RUNTIME_FILE`. `runtime.example.json` documents the schema with explicitly simulated data. The live endpoint rejects simulated or historical sources, and preserves missing/stale state. This bridge is not used to infer liveness from ledger records.

The trace projection is domain-neutral. `diagnostic` Work with a recorded `capability_id` or `adapter_id` is shown as task-tool work; non-model Effects with `metadata.adapter_id` count as registered adapter executions. Names, prompt text and model or engineering readiness do not establish tool execution. The optional telemetry example uses generic analysis and result-validation nodes.
