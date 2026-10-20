# Frontend verification

Verified on 2026-10-20. This update imports the completed CDK2 experiment and converts the UI to English. Functional checks use recorded local artifacts or controlled telemetry fixtures; no model or GPU experiment is started.

## Automated checks

- JavaScript syntax checks and the self-contained HTML build passed.
- All **26 automated tests passed**: 10 recorded-data checks and 16 runtime checks.
- Data checks cover deterministic import, 40 source hashes, recorded hash bindings, post-audit artifact tampering, 20-candidate coverage, 44 evidence records, arm isolation, missing values, shortlist metrics and separate Meta cost accounting.
- Runtime checks cover schema validation, unknown components, stale heartbeats/snapshots, invalid/future timestamps, read-only HTTP handling, size limits and rejecting recorded/demo data as live telemetry.
- Fresh-clone behavior was checked without the ignored local experiment archive: 6 portable-snapshot tests pass and 4 source-artifact checks explicitly skip.
- No Chinese characters remain in frontend text sources or the generated HTML.

## Browser checks

**51 interaction checks passed** against the development preview, plus **12 checks passed** against the final self-contained build served over local HTTP.

Coverage includes recorded canvas state, all three arm selections, actual comparison metrics, 20 candidates, 12 uncomputed candidates per arm, five-compound frozen shortlists, search, evidence isolation, per-seed results, version/gate/protocol dialogs, provenance and arm-specific JSON download. The final build exposes all 40 sources, distinguishing 38 recorded hash bindings and 2 local import roots.

All five views and their result dialogs were checked for English content. Desktop screenshots were reviewed at 1440 × 1080. Mobile workbench, comparison and candidate views were checked at 390 × 844 with no page-level horizontal overflow; the component canvas intentionally supports panning. Mobile navigation makes the page background inert.

The unavailable live endpoint correctly returns 503 and leaves all eight components unknown. Switching back restores the historical results. There were no JavaScript exceptions; the expected 503 request is recorded during the disconnection check.

Current screenshots: [Workbench](docs/workbench.png), [Run comparison](docs/comparison.png), [Mobile](docs/mobile.png).

## Scope

The default snapshot is historical evidence, not current process telemetry. No heartbeat producer has been added to the experiment controllers. Model/GPU execution, remote availability, authorization, biological outcomes and statistical claims are outside this frontend verification. Self-contained HTML is browser-tested over local HTTP; direct `file:` automation is not claimed.
