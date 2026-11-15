# Bounded method literature retrieval

`literature.search` version `1.0` performs a real HTTP GET against the [Europe PMC Articles REST API](https://europepmc.org/RestfulWebService), using the production `https://www.ebi.ac.uk/europepmc/webservices/rest/search` endpoint. Official documentation was checked on 2026-11-09; the [official development documentation mirror](https://dev.europepmc.org/RestfulWebService) describes JSON `resultType=core` as publication metadata including available abstracts and full-text links. This adapter does **not** fetch those full-text links. No API key or additional Python dependency is required.

This is a PABLO-compatible public method-query profile, not arbitrary free-text literature search. It never sends candidate molecules, observed scores, hidden objective identity, private memory, run IDs or local paths to the provider. Models choose one topic and a result count; the only outbound search text is the fixed, inspectable `TOPICS` catalog in `adapter.py`:

- `black_box_optimization`
- `sample_efficient_search`
- `molecular_diversity`
- `duplicate_filtering`
- `surrogate_uncertainty`
- `matched_molecular_pairs`
- `representation_bias`
- `budgeted_search`

Enable the adapter explicitly in a new run's immutable configuration:

```python
config.update(
    research_features={"literature": True},
    literature_run_root=str(run_root.resolve()),
    literature_max_calls=8,
)
from rsi_domains.literature.adapter import make_adapter
runtime = Runtime(store, run_id, adapters=[..., make_adapter()])
```

Preserve any other entries in `research_features`. Lab or Meta uses the existing `request_diagnostic` command with `capability_id="literature.search"`, `capability_version="1.0"`, and JSON-encoded inputs:

```json
{"run_root":"/ABSOLUTE/PATH/TO/NEW-RUN","run_id":"new-run-id","topic":"sample_efficient_search","max_results":3}
```

The host can use `runtime.execute_adapter` with the same inputs. Runtime owns admission, budgeting, dispatch, notification and publication. The adapter owns no scheduler or model loop. Its effect kind is `literature_search`. It publishes one `source_statement` fact, with `trust=untrusted_external_source_statement`, `current_task_validation=false` and no conclusions. External text must be treated as source content, never instructions or validation of the current scientific task. A relevant abstract can motivate a bounded method hypothesis; it does not prove that a method helps this hidden objective.

Each query retrieves only the first relevance-sorted page, with 1–5 records. The returned data explicitly records total provider hits, returned count, query-result completeness, absent/truncated abstracts, and `full_text_retrieved=false`. `content_status` is `empty`, `partial` or `results` for a successful API operation; even `results` means only the requested metadata/abstract scope. A HTTP error, malformed JSON or response-byte overflow is a definite failed operation. A transport timeout/disconnection is `unknown`, never an empty search. Failed/unknown operations publish no scientific evidence.

A dispatch reserves one tool call, 40 wall seconds and 5 CPU seconds. Actual local elapsed/CPU time and the single attempted tool call are settled through the ordinary controller. Socket operations time out after 5 seconds; the read loop checks a 30-second request deadline. OS-level DNS scheduling and host overhead are not represented as a hard real-time guarantee. At most 1 MiB plus one overflow-detection byte is retained. No redirects, pagination, secondary fetches or automatic retry occur. Each topic causes at most one original external request per run. A repeated topic reuses its successful settled immutable receipt, including an empty successful search, without another HTTP request. Failed or unknown attempts are never silently retried; unresolved earlier execution blocks new requests. The immutable `literature_max_calls` cap limits original external requests; cached calls still consume one ordinary tool call and measured local time/CPU, so the total tool budget bounds reuse.

`literature_calls/<effect_id>/` contains the pre-dispatch binding marker, exact `response.bin`, and `receipt.json`. The receipt includes provider, exact URL/query, UTC start/end times, HTTP status, selected response headers, raw byte length/SHA-256, parsed records and measured costs. The ledger retains the terminal receipt and published source fact. The model publication distinguishes the raw archival file from article full text.

Run ledgers, raw responses, model calls and local experiment reports are stored in the operator's chosen directories and are not distributed with this repository. For the integrated workflow, see the [PABLO research entrypoint](../../docs/pablo-research.md).

Cached packets pin the original effect ID, packet/receipt hashes, raw-response hash and source group; execution rechecks that the original effect is successful with settled accounting. Smaller result requests select a bounded prefix after validating the original response against its original packet. Larger requests return the available prefix and an explicit partial/no-refetch reason instead of issuing another HTTP request. Both roles may receive distinct delivery facts, but `source_group_id`, `source_effect_id`, original retrieval timestamps and raw hash are retained; `cache_hit=true`, `external_requests=0` and `independent_evidence=false` prevent a second delivery from being presented as new independent evidence. The first retrieval records `external_requests=1`.

A started directory without a complete terminal receipt is never resent. `recover_receipt(run_root, run_id, effect_id)` returns only a complete known local receipt after checking effect/input binding, source implementation, raw bytes/hash, parsed records and cost shape. Pass that receipt to `Controller.reconcile` with an explicit recovery key, then call `Runtime.publish_adapter_effect`. `None` means the outcome remains unknown and needs operator disposition; it does not authorize a retry. Repeated publication of an already settled effect is idempotent through Runtime.

## Verification

```sh
python -m unittest tests.test_literature_adapter -q
python -m rsi_domains.literature.smoke --root /ABSOLUTE/PATH/TO/FRESH-SMOKE-DIRECTORY --topic sample_efficient_search --max-results 3
```

Run these commands from the repository root after replacing the absolute-path placeholder. The smoke uses a new standalone ledger with zero model-call budget and exactly one real external tool-call budget. It does not load a historical PABLO run or substitute a fixture response. The 2026-11-09 smoke succeeded: Europe PMC reported 76 hits, three metadata records were returned, two included abstracts and one did not. It was accurately published as `partial`, with no full text retrieved. These counts describe that verification, not a fixed expectation for future queries.

This verifies real HTTP retrieval, provenance, settlement and publication. It does not establish that the retrieved method improves PABLO optimization; that requires subsequent model use and scientific evaluation.
