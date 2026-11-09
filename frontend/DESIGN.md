# Architecture workspace design

The interface starts empty and presents five architectural components, with controller infrastructure below the graph. Users explicitly choose a local imported snapshot or a configured ledger. No result records are part of the application bundle.

## Information hierarchy

The graph explains component responsibilities. The inspector shows source-backed orchestration, calls, tool executions, evidence references and costs. Engineering is nested within Meta-Harness. The same recorded tool can be linked from a harness and from its owning tool component; those views do not create additional executions.

## Source semantics

Missing data is unknown. A saved status is not a live heartbeat, a successful command is not scientific validation, and a recorded hypothesis is not an established result. Unknown token usage and unresolved reservations remain separate. The UI never fabricates missing conversation content.

No-source is a normal initial state. Source failures clear visible run data, never replace it with a demo. Switching sources cancels pending requests so that a late response cannot leak into the newly selected view.

## Interaction and accessibility

Nodes can be dragged independently of the background. The graph supports panning, zooming and fit, while component buttons provide an alternative to spatial navigation. Keyboard focus, tab semantics, expandable details and narrow-screen layouts remain available with or without a connected run.

## Distribution

Application builds remove old build output and include code only. Local imports are separate, ignored assets. Runtime and importer APIs are read-only, bound to explicit user-supplied sources, and do not start models or computations.
