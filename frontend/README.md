# RSI-Lab experiment workbench

An English research UI for the completed CDK2 comparison and a component canvas. Built with HTML, CSS and local JavaScript modules; no third-party runtime dependencies.

![Recorded experiment workbench](docs/workbench.png)

[Run comparison](docs/comparison.png) · [Mobile preview](docs/mobile.png) · [Design notes](DESIGN.md) · [Verification](QA.md)

## Preview and build

Requires Node.js 20 or later. No package installation is needed.

```sh
cd frontend
npm run dev
```

Open `http://127.0.0.1:4173`. Set `PORT` to use a different port.

```sh
npm run check
npm test
npm run build
```

The build produces a self-contained `dist/index.html`. Recorded results and the demo work offline; live telemetry requires the local HTTP server.

## Recorded experiment

The default view uses the completed `lab_comparison_v2` experiment, generated from local source artifacts:

- 20 CDK2 compounds from assay `CHEMBL1250281`.
- Fixed baseline, H0 and H1: 8 evaluated candidates and 16 independent physical jobs per arm.
- Actual tools: GNINA 1.3.3 and PoseBusters 0.6.5. Boltz-2 and Nesso-1 were not used in this comparison.
- Frozen five-compound shortlists, per-seed results, historical IC50, pose checks, measured resource use and evidence references.
- H1 is an evaluated compact-JSON candidate, **not authorized for release**. Meta development cost is reported separately.

The global arm selector updates the canvas, candidate results and evidence scope. Candidate selection is read-only because these are completed, frozen experimental results. Exports preserve source hashes and interpretation limits.

`src/experiment-data.js` is a portable, committed snapshot. It contains 20 candidates, 44 evidence records and a source manifest. It does not depend on the ignored `runs/` directory at runtime. To reimport after a new local readout is available:

```sh
npm run import:experiment
npm run check
npm test
npm run build
```

Importing reads existing files only; it never starts a model or GPU job. The readout timestamp comes from the source, rather than the import clock. The importer checks critical source references, retains missing values and projects only the fields needed for the UI. Source entries distinguish verified recorded hash bindings from local import roots. Source-artifact tests run when the local archive is available and explicitly skip on a fresh clone; snapshot checks always run. Repository-relative source paths identify the original artifacts; the full experiment archive is not copied into the frontend.

## Canvas and data modes

The canvas supports node dragging, background panning, zoom, fit, layout reset, a node selector and an inspector with task, input/output contract and event information. Keyboard controls: focus the canvas, then use arrows to pan, `+`/`-` to zoom and `0` to fit. Ctrl/Command + wheel also zooms.

- **Recorded experiment** is the default. Completion and pending review describe the imported run; they are not current heartbeats.
- **Live telemetry** polls the read-only endpoint every five seconds. It never falls back to demo data on failure.
- **Demo workflow** is explicitly simulated and never starts tasks.

The real topology uses Controller, Lab Codex, Tool Executor, GNINA, PoseBusters, Scientific Memory, Meta Codex and Validation Gate. Edges describe message relationships, not proof of an in-flight message.

## Live telemetry integration

The existing read-only bridge reads a JSON file written by a genuine external producer:

```sh
RSI_LAB_RUNTIME_FILE=/absolute/path/to/runtime.json npm run dev
```

The producer should use actual observations and atomically replace the file every 5–10 seconds. The browser does not infer liveness from historical `running` fields. Existing experiment scripts have not been modified to produce heartbeats.

See `runtime.example.json` for the contract. The example deliberately declares `source: "demo"`; the live endpoint rejects it. Only an actual producer should emit `source: "live"` with real timestamps.

Heartbeats older than 30 seconds and snapshots older than 60 seconds are stale. Missing components remain unknown. Invalid data or a disconnected endpoint clears current status rather than displaying healthy demo nodes. The endpoint accepts GET/HEAD, caps snapshots at 1 MiB, hides configured file paths in errors, and listens on the loopback interface. It is not an authenticated public service.

## Scientific interpretation

Paired-valid shortlist coverage is a pose-validity readout, not biological hit rate. CNNscore is not activity probability. Qualified values use two valid seed results; uncomputed or unqualified values are never filled with zero. Historical IC50 was visible to the Lab. GPU active time and dollar cost are unknown; numerical wall time is not GPU billing time.

This is one pool and one model trajectory per agent arm, run in a fixed order. Two docking seeds are not independent agent repetitions. H1 has a slightly higher mean CNNscore and a less favorable mean empirical energy; the results do not establish generalization, statistical significance, biological efficacy or a release-ready improvement. The Meta call must be included when considering development cost.
