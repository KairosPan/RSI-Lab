"""Execute one real, public Europe PMC query in a fresh standalone ledger.

Run: python -m rsi_domains.literature.smoke --root /absolute/new/directory
This smoke makes no model call and never loads a historical scientific run.
"""
import argparse
import json
from pathlib import Path

from rsi_lab.controller import Controller
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store, digest
from . import adapter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--topic', choices=sorted(adapter.TOPICS), default='sample_efficient_search')
    parser.add_argument('--max-results', type=int, default=3)
    args = parser.parse_args()
    if not args.root.is_absolute() or args.root.exists():
        parser.error('--root must be a new absolute directory')
    root = args.root.resolve()
    store = Store(root)
    run_id = 'literature-live-smoke'
    try:
        Controller(store).initialize(run_id,
            budgets={key: {'limit': value, 'hard': key.endswith('_calls')} for key, value in
                {'model_calls': 0, 'tokens': 0, 'tool_calls': 1, 'wall_seconds': 60, 'cpu_seconds': 10}.items()},
            config={'research_features': {'literature': True}, 'literature_run_root': str(root),
                    'literature_max_calls': 1, 'execution_mode': 'live', 'purpose': 'One real public literature retrieval smoke'})
        runtime = Runtime(store, run_id, adapters=[adapter.make_adapter()])
        outcome = runtime.execute_adapter(adapter.ADAPTER_ID,
            {'run_root': str(root), 'run_id': run_id, 'topic': args.topic, 'max_results': args.max_results},
            trigger_id='one-real-public-method-query')
        effect = outcome['effect']
        receipt = json.loads(store.read_blob(effect['result_ref']))
        result = receipt.get('result', {})
        summary = {'run_id': run_id, 'run_root': str(root), 'status': effect['status'],
            'adapter_id': adapter.ADAPTER_ID, 'adapter_version': adapter.VERSION,
            'descriptor': adapter.make_adapter().descriptor(), 'effect_id': effect['id'],
            'provider': adapter.PROVIDER, 'query': result.get('query'), 'request_url': result.get('request_url'),
            'response_sha256': result.get('response_sha256'), 'content_status': result.get('content_status'),
            'returned_records': result.get('returned_records'), 'provider_total_hits': result.get('provider_total_hits'),
            'full_text_retrieved': False, 'actual_costs': effect['actual_costs'],
            'source_fact_ids': [f['id'] for f in store.list(run_id, 'fact')],
            'records': result.get('records', []), 'error': result.get('error'),
            'history': store.verify_history(run_id), 'receipt_sha256': digest(receipt)}
        (root / 'smoke-summary.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False) + '\n')
        print(json.dumps({k: summary[k] for k in ('run_root', 'status', 'effect_id', 'returned_records',
            'provider_total_hits', 'content_status', 'response_sha256', 'error')}, ensure_ascii=False))
        return 0 if effect['status'] == 'succeeded' and result.get('returned_records', 0) > 0 else 2
    finally:
        store.close()


if __name__ == '__main__':
    raise SystemExit(main())
