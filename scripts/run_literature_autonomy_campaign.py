"""Fresh 100 + 200 molecular campaign through the ordinary Lab/Literature runtime.

The public task does not request source research or prescribe a delegation rate.
Only model-authored, admitted Lab commands may score molecules. The host supplies
lossless, receipt-backed observations and advances work; it never proposes a
candidate, invents a response, or resends an unknown invocation.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import io
import json
import math
from pathlib import Path
import shutil
import sys

from rsi_domains.pablo import adapter as scorer, lookup, oracle
from rsi_lab.controller import Controller
from rsi_lab.packets import present_delivery
from rsi_lab.roles import CodexExecutor, literature_decision_record
from rsi_lab.runtime import Runtime
from rsi_lab.session_context import read_native_snapshot
from rsi_lab.store import Store, digest, encode
from rsi_literature import make_adapters


PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_INITIAL = PROJECT.parent / 'pablo-meta-experiment/upstream/data/guacamol_init_data.csv'
MODEL, EFFORT = 'gpt-6.1-sol', 'high'
RUN_ID, INITIAL_COUNT, NEW_COUNT = 'campaign', 100, 200
ENABLED_PROVIDERS = ['europe_pmc', 'pubmed', 'openalex', 'asta', 'paper2skill', 'poppler']
LOCAL_SOURCE = 'registered_public_facts'
LOCAL_FIELDS = ('content', 'provenance', 'kind', 'content_ref')
GOAL = '''推进这一项持续的分子优化研究：从提供的100个候选及其数值观测出发，
在固定的黑盒评估接口上取得恰好200个此前未测、有效且规范化身份唯一的新候选的真实分数，
尽可能提高分数。依据实际反馈自主选择候选、批次和后续策略；无效或重复候选不占新评估名额。
评估器按候选返回观测，目标的实现对本任务保持不透明。局部结构规律与目标机理猜想必须标成
假设，不能把数值分数当成已证实的真实药效、实验成功或一般适用性。
解释采用或改变策略的依据、数据支持了什么、还缺什么，以及结果可外推的边界。
所有科学判断应区分本次观测、外部依据和未验证推断。保持工作记忆简短，优先保留下一步、
假设、关键证据及避免重复的计划。使用本轮实际提供的完整观测和剩余额度推进同一个任务。
当200个新评估都完成后，提交最终结果与方法限制；更早停止须明确具体阻塞，不得声称任务已完成。'''


def write_json(path, value):
    path.write_bytes(encode(value) + b'\n')


def log(root, kind, **values):
    item = {'time': datetime.now(timezone.utc).isoformat(), 'kind': kind, **values}
    with (root / 'progress.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(item, ensure_ascii=False) + '\n')
    print(json.dumps(item, ensure_ascii=False), flush=True)


def initial_observations(raw):
    rows = list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
    if len(rows) < INITIAL_COUNT or not {'smile', 'adip'}.issubset(rows[0]):
        raise ValueError('The original input must contain the first 100 smile/adip rows')
    candidates = [oracle.canonicalize(row['smile']) for row in rows[:INITIAL_COUNT]]
    scores = [float(row['adip']) for row in rows[:INITIAL_COUNT]]
    if None in candidates or len(set(candidates)) != INITIAL_COUNT or not all(map(math.isfinite, scores)):
        raise ValueError('The original first 100 rows must be valid, unique and finite; no replacement sampling')
    return {'candidates': candidates, 'scores': scores}


def normalize_literature_configuration(configuration):
    """Freeze installed path bindings independently of the caller's cwd."""
    value = json.loads(encode(configuration))
    settings = value.setdefault('connectors', {})
    paper = settings.setdefault('paper2skill', {})
    for key in ('repository', 'python'):
        if paper.get(key):
            path = Path(paper[key]).expanduser()
            # Preserve the venv invocation path: resolving its executable symlink
            # would select the base interpreter instead of the installed venv.
            paper[key] = str(path.absolute() if path.is_absolute() else (PROJECT / path).absolute())
    poppler = settings.setdefault('poppler', {})
    for name in ('pdftotext', 'pdfinfo', 'pdftoppm'):
        configured = poppler.get(name + '_path', name)
        path = shutil.which(configured)
        if path:
            poppler[name + '_path'] = str(Path(path).absolute())
    return value


def code_manifest(adapters, configuration):
    paths = {Path(__file__).resolve()}
    for directory in ('rsi_lab', 'rsi_literature', 'rsi_domains/pablo'):
        paths.update((PROJECT / directory).rglob('*.py'))
    dependencies = {Path(sys.executable).absolute()}
    paper = configuration.get('connectors', {}).get('paper2skill', {})
    if paper.get('repository'):
        scripts = Path(paper['repository']) / 'skills/paper2agent/paper2skill/scripts'
        dependencies.update(scripts / name for name in ('paper_bundle.py', 'pdf_to_skill.py'))
    if paper.get('python'):
        dependencies.add(Path(paper['python']))
        dependencies.add(Path(paper['python']).parent.parent / 'pyvenv.cfg')
    for key in ('pdftotext_path', 'pdfinfo_path', 'pdftoppm_path'):
        path = configuration.get('connectors', {}).get('poppler', {}).get(key)
        if path and Path(path).is_absolute():
            dependencies.add(Path(path))
    return {'source_sha256': {str(path): digest(path.read_bytes()) for path in sorted(paths)},
            'local_dependency_sha256': {str(path): digest(path.read_bytes()) if path.is_file() else None
                                        for path in sorted(dependencies)},
            'python_version': sys.version,
            'adapter_sha256': {adapter.adapter_id: digest(adapter.descriptor()) for adapter in adapters},
            'oracle_fingerprint': oracle.fingerprint()}


def adapters_for(root, configuration):
    return [scorer.make_adapter(), lookup.make_adapter(),
            *make_adapters(root / 'documents', configuration)]


def initialize(root, initial_path, literature_path, max_steps):
    if root.exists():
        raise ValueError('A new campaign requires a fresh directory; use --resume for this exact run')
    raw = initial_path.read_bytes()
    initial = initial_observations(raw)
    literature_config = normalize_literature_configuration(json.loads(literature_path.read_bytes()))
    if set(literature_config.get('enabled_providers', [])) != set(ENABLED_PROVIDERS):
        raise ValueError('This campaign enables exactly the six public/local providers; credential services stay off')
    root.mkdir(parents=True)
    (root / 'inputs').mkdir()
    (root / 'inputs' / 'original-source.csv').write_bytes(raw)
    write_json(root / 'inputs' / 'initial-observations.json', initial)
    write_json(root / 'literature-config.json', literature_config)
    adapters = adapters_for(root, literature_config)
    artifacts = code_manifest(adapters, literature_config)
    store = Store(root / 'ledger')
    initial_ref = store.put_blob(initial)
    manifest = {'schema_version': 'literature_autonomy_campaign_v1', 'model': MODEL, 'effort': EFFORT,
        'initial_input_sha256': digest(raw), 'initial_observations_sha256': initial_ref,
        'initial_count': INITIAL_COUNT, 'new_evaluation_limit': NEW_COUNT,
        'initial_selection': 'First 100 rows, in original file order; no sampling or replacement.',
        'evaluation_unit': 'One previously unseen valid canonical candidate with an actual recorded score.',
        'model_calls_are_not_evaluation_rounds': True,
        'literature_configuration_sha256': digest(literature_config), 'artifacts': artifacts,
        'scheduler': 'Prepared work first; child research/tool continuity; at most one Meta model turn '
            'between opportunities for an eligible Lab turn. All outstanding work remains recorded.',
        'public_goal_sha256': digest(GOAL), 'max_driver_steps': max_steps}
    manifest_ref = store.put_blob(manifest)
    budgets = {'model_calls': {'limit': 200, 'hard': True}, 'tool_calls': {'limit': 1000, 'hard': True},
        'tokens': {'limit': 30000000, 'hard': False}, 'wall_seconds': {'limit': 86400, 'hard': False},
        'cpu_seconds': {'limit': 36000, 'hard': False}}
    config = {'goal': GOAL, 'execution_mode': 'live', 'model': MODEL, 'effort': EFFORT,
        'max_input_bytes': 524288, 'max_effective_context_bytes': 2097152,
        'max_image_bytes': 4194304, 'max_effective_image_bytes': 16777216,
        'max_open_reviews': 2, 'open_review_interval_batches': 5, 'open_review_fact_limit': 4,
        'authorized_sources': [LOCAL_SOURCE],
        'authorized_source_contracts': {LOCAL_SOURCE: {'source_version': 1,
            'object_ids': 'Exact fact IDs already registered in this run, including campaign_contract, '
                          'campaign_state_* and original tool-delivered facts; no paths or arbitrary blob IDs.',
            'fields': list(LOCAL_FIELDS), 'max_objects': 8, 'max_pages': 1, 'max_bytes': 524288,
            'limitations': 'Reads immutable registered fact bytes and provenance only. A summary or pointer '
                           'remains a summary or pointer; the source never supplies private evaluator internals.'}},
        'model_reservation': {'model_calls': 1, 'tokens': 120000, 'wall_seconds': 1200},
        'pablo_run_root': str(store.root), 'pablo_max_new_evaluations': NEW_COUNT,
        'pablo_initial_ref': initial_ref, 'pablo_oracle_fingerprint': artifacts['oracle_fingerprint'],
        'campaign_manifest_ref': manifest_ref}
    controller = Controller(store)
    controller.initialize(RUN_ID, budgets=budgets, max_external=1, config=config)
    write_json(root / 'campaign-manifest.json', manifest)
    write_json(root / 'run-config.json', store.run(RUN_ID)['config'])
    # The column mapping stays in a host-only codebook. Agent input contains the
    # supplied scores but no objective implementation, target name or label.
    write_json(root / 'host-codebook.json', {'input_path': str(initial_path),
        'input_sha256': digest(raw), 'selection': 'first 100 rows', 'candidate_column': 'smile',
        'score_column': 'adip', 'invalid_or_duplicate_slots': 'not consumed',
        'candidate_author': 'Lab model through admitted diagnostic commands only',
        'scientific_claim_authority': 'Measured optimization trajectory; general scientific claims require their own evidence.'})
    contract = {'schema_version': 'pablo_public_campaign_contract_v1',
        'run_root': str(store.root), 'run_id': RUN_ID, 'initial_count': INITIAL_COUNT,
        'new_evaluation_limit': NEW_COUNT, 'initial_input_sha256': digest(raw),
        'score_direction': 'maximize', 'evaluate_capability': scorer.ADAPTER_ID,
        'evaluate_version': scorer.VERSION, 'lookup_capability': lookup.ADAPTER_ID,
        'lookup_version': lookup.VERSION,
        'evaluation_inputs': {'run_root': str(store.root), 'run_id': RUN_ID,
            'candidates': '1 to 20 proposed SMILES strings'},
        'lookup_inputs': {'run_root': str(store.root), 'run_id': RUN_ID,
            'candidates': '1 to 100 proposed SMILES strings'},
        'identity_rule': 'Canonical molecular identity; scoring rechecks all inherited/current/pending identities.',
        'measurement_scope': 'Opaque numerical objective; no direct biological or mechanistic meaning is supplied.'}
    controller.add_fact(RUN_ID, 'campaign_contract', 'source_statement', contract,
        {'delivery_kind': 'host_configuration', 'source': 'frozen-campaign-contract'}, 'campaign-contract')
    runtime = Runtime(store, RUN_ID, adapters=adapters)
    runtime.enqueue_lab('campaign-start', ['campaign_contract'])
    log(root, 'initialized', initial_count=INITIAL_COUNT, requested_new_evaluations=NEW_COUNT,
        model=MODEL, effort=EFFORT, manifest_ref=manifest_ref)
    return runtime


def verify_frozen(runtime, root):
    store, config = runtime.store, runtime.config
    configuration = json.loads((root / 'literature-config.json').read_bytes())
    manifest = json.loads(store.read_blob(config['campaign_manifest_ref']))
    if (manifest != json.loads((root / 'campaign-manifest.json').read_bytes())
            or digest(configuration) != manifest['literature_configuration_sha256']
            or digest((root / 'inputs' / 'original-source.csv').read_bytes()) != manifest['initial_input_sha256']
            or initial_observations((root / 'inputs' / 'original-source.csv').read_bytes())
                != json.loads(store.read_blob(config['pablo_initial_ref']))
            or code_manifest(list(runtime.adapters.by_id.values()), configuration) != manifest['artifacts']
            or config != json.loads((root / 'run-config.json').read_bytes())):
        raise ValueError('Frozen campaign inputs, configuration, code or adapter identities changed; no automatic migration')
    return manifest


def load_runtime(root):
    if not (root / 'ledger' / 'ledger.sqlite3').is_file():
        raise ValueError('Resume requires an existing initialized campaign ledger')
    configuration = json.loads((root / 'literature-config.json').read_bytes())
    store = Store(root / 'ledger')
    try:
        runtime = Runtime(store, RUN_ID, adapters=adapters_for(root, configuration))
        verify_frozen(runtime, root)
        return runtime
    except BaseException:
        store.close()
        raise


def bind_observations(runtime, work):
    if work['kind'] != 'lab':
        return work
    state = scorer.read_progress(runtime.store, RUN_ID)
    effects = [effect for effect in runtime.store.list(RUN_ID, 'effect')
               if effect['metadata'].get('adapter_id') == scorer.ADAPTER_ID and effect['status'] == 'succeeded']
    content = {'schema_version': 'pablo_observation_snapshot_v1',
        **{key: state[key] for key in ('initial_count', 'new_evaluations', 'remaining_evaluations',
                                     'candidates', 'scores', 'observations', 'top20', 'trajectory_sha256')},
        'initial_source_ref': runtime.config['pablo_initial_ref'],
        'evaluation_receipts': [{'effect_id': effect['id'], 'receipt_ref': effect['result_ref'],
            'packet_sha256': effect['packet_hash']} for effect in sorted(effects, key=lambda row: row['created_seq'])],
        'coverage': 'All inherited and successfully settled current observations; no score imputation.'}
    fid = 'campaign_state_' + digest(content)[:24]
    def bind(tx):
        current = tx.get('work', work['id'])
        if current['status'] != 'ready' or tx.find('effect', work_id=work['id']):
            raise ValueError('Observation context can only bind before this Work has an Effect')
        if not tx.find('fact', id=fid):
            tx.insert('fact', fid, {'kind': 'raw_fact', 'content_ref': tx.blob(content),
                'provenance': {'delivery_kind': 'measurement_snapshot',
                    'source': 'lossless-ledger-observation-replay',
                    'initial_source_ref': runtime.config['pablo_initial_ref'],
                    'receipt_refs': [row['receipt_ref'] for row in content['evaluation_receipts']]}})
        inputs = dict(current['inputs'])
        for key in ('evidence_ids', 'required_evidence_ids'):
            ids = [value for value in inputs.get(key, []) if not value.startswith('campaign_state_')]
            inputs[key] = list(dict.fromkeys(ids + ['campaign_contract', fid]))
        inputs['campaign_snapshot_id'] = fid
        return tx.update('work', work['id'], {'inputs': inputs})
    return runtime._mutate('campaign-context:' + work['id'] + ':' + fid,
        'campaign_observations_bound', bind, {'work_id': work['id'], 'snapshot_id': fid})


def packet_preflight(runtime, work, root):
    agent, mode, task, options = runtime._context(work)
    plan = agent.prepare(mode, task, **options)
    if plan['status'] != 'ready' or plan['partial_evidence_ids'] or plan['deferred_evidence_ids']:
        raise ValueError('The complete requested source context does not fit the frozen packet budget')
    log(root, 'packet_preflight', work_id=work['id'], role=mode, input_bytes=plan['input_bytes'],
        source_count=len(plan['delivery_pages']), image_count=plan['image_count'])
    return plan, task


def prepare_continuity(runtime, work, root):
    plan, task = packet_preflight(runtime, work, root)
    session = runtime.store.get(RUN_ID, 'agent_session', task['agent_state']['session_id'])
    if not session.get('provider_thread_id'):
        return
    context = session['context_manifest']
    snapshot = read_native_snapshot(context['native_session_ref'],
        expected_thread_id=session['provider_thread_id'],
        expected_cwd=str(runtime.store.root / 'agents' / session['id'] / 'workspace'),
        expected_prefix=context['native_prefix'])
    history = snapshot.get('observable_text_history_bytes', snapshot['observable_history_bytes'])
    if session['committed_turn'] >= 3 or history + plan['input_bytes'] > runtime.config['max_input_bytes']:
        checkpoint = runtime.agents.seal_checkpoint(session['id'], 'campaign-checkpoint:' + session['id'])
        rebuilt = runtime.agents.rebuild_session(session['id'],
            'Bound observable native history; keep the same owner, evidence, working memory and cumulative budgets.',
            'campaign-rebuild:' + session['id'], checkpoint_id=checkpoint['id'])
        log(root, 'session_rebuilt', previous_session=session['id'], session=rebuilt['id'],
            owner_id=rebuilt['owner_id'], checkpoint=checkpoint['id'])
        packet_preflight(runtime, work, root)


def select_work(runtime, last_model_role):
    prepared = sorted(runtime.store.list(RUN_ID, 'effect', status='prepared'), key=lambda row: row['created_seq'])
    if prepared:
        return runtime.store.get(RUN_ID, 'work', prepared[0]['work_id']), prepared[0]
    ready = [work for work in runtime.controller.ready(RUN_ID)
             if (work['kind'] in {'lab', 'literature', 'meta_discovery', 'meta_disposition', 'disposition'}
                 and runtime._agent_available(work))
             or (work['kind'] == 'diagnostic' and work['inputs'].get('capability_id') in runtime.adapters.by_id)
             or (work['kind'] == 'evidence_retrieve'
                 and work['inputs'].get('query_spec', {}).get('source_id') == LOCAL_SOURCE)]
    children = [work for work in ready if work['inputs'].get('literature_task_id')]
    diagnostics = [work for work in ready if work['kind'] == 'diagnostic']
    sources = [work for work in ready if work['kind'] == 'evidence_retrieve']
    labs = [work for work in ready if work['kind'] == 'lab']
    meta = [work for work in ready if work['kind'] in {'meta_discovery', 'meta_disposition', 'disposition'}]
    ordered = children or diagnostics or sources or (labs if last_model_role == 'meta' and labs else meta or labs)
    return (ordered[0], None) if ordered else (None, None)


def fulfill_registered_source(runtime, work):
    """No path traversal or general blob reader: only an admitted fact scope."""
    query = work['inputs']['query_spec']
    ids, fields = query['object_ids'], query['fields']
    if (query['source_id'] != LOCAL_SOURCE or query['source_version'] != 1
            or not 1 <= len(ids) <= 8 or len(set(ids)) != len(ids)
            or not fields or len(set(fields)) != len(fields) or not set(fields).issubset(LOCAL_FIELDS)
            or query['max_pages'] != 1 or not 1 <= query['max_bytes'] <= 524288):
        raise ValueError('Local source request exceeds its frozen object, field, page or byte contract')
    records = []
    for fid in ids:
        fact = runtime.store.get(RUN_ID, 'fact', fid)
        provenance = fact['provenance'] if isinstance(fact['provenance'], dict) else {}
        if provenance.get('delivery_kind') in {'summary', 'pointer'}:
            raise ValueError('Summary/pointer records cannot be returned as original local evidence')
        content = json.loads(runtime.store.read_blob(fact['content_ref']))
        # Images need the registered render operation and certified attachment
        # route; a JSON image reference is not a new visual observation.
        if isinstance(content, dict) and content.get('format') in {'rsi_image_evidence_v1', 'rsi_image_evidence_ref_v1'}:
            raise ValueError('Use the registered image delivery path for visual evidence')
        public = {**fact, 'content': content}
        records.append({'evidence_id': fid, 'source_fact_version': fact['version'],
                        **{key: public[key] for key in fields}})
    content = {'source_id': LOCAL_SOURCE, 'records': records, 'coverage': 'Exact requested registered fact fields.'}
    if len(encode(content)) > query['max_bytes']:
        raise ValueError('Requested original fact fields exceed the admitted byte bound; no silent truncation')
    return runtime.ingest_delivery(work['inputs']['request_id'], content,
        {'source': LOCAL_SOURCE, 'source_version': 1, 'delivery_kind': 'original_registered_fact_fields',
         'source_fact_ids': ids, 'source_content_refs': [runtime.store.get(RUN_ID, 'fact', fid)['content_ref'] for fid in ids]},
        kind='raw', page=0, source_version=1)


def dispatch_prepared(runtime, effect):
    if effect['kind'] in {'lab', 'literature', 'meta'}:
        raise ValueError('Prepared models use their normal Runtime execution path')
    adapter = runtime.adapters.resolve(effect)
    actual = runtime.controller.dispatch_effect(RUN_ID, effect['id'], adapter.execute)
    if actual['status'] == 'succeeded' and not actual.get('accounting_unknown'):
        return {'effect': actual, 'result': runtime.publish_adapter_effect(actual)}
    if actual['status'] == 'failed_definite' and not actual.get('accounting_unknown'):
        runtime._literature_tool_failure(actual)
    return {'effect': actual, 'result': None}


def audit(runtime):
    store = runtime.store
    progress = scorer.read_progress(store, RUN_ID)
    effects = sorted(store.list(RUN_ID, 'effect'), key=lambda row: row['created_seq'])
    proposals = {row['effect_id']: row for row in store.list(RUN_ID, 'proposal')}
    models, decisions, errors, finals, score_receipts = [], [], [], [], []
    frozen_valid = False
    try:
        verify_frozen(runtime, store.root.parent)
        frozen_valid = True
    except (ValueError, KeyError, TypeError, OSError) as exc:
        errors.append({'scope': 'frozen_campaign', 'error': str(exc)})
    for effect in effects:
        receipt = json.loads(store.read_blob(effect['result_ref'])) if effect.get('result_ref') else {}
        packet = json.loads(store.read_blob(effect['packet_ref']))
        if effect['kind'] in {'lab', 'literature', 'meta'}:
            proposal = proposals.get(effect['id'], {})
            valid = False
            try:
                if (effect['status'] != 'succeeded' or effect.get('accounting_unknown')
                        or receipt.get('protocol_valid') is not True or receipt.get('synthetic')
                        or effect.get('actual_costs', {}).get('model_calls') != 1
                        or not receipt.get('provider_thread_id') or not receipt.get('terminal_received')
                        or proposal.get('status') != 'applied' or packet['model'] != MODEL or packet['effort'] != EFFORT):
                    raise ValueError('Expected a real, applied, protocol-valid model turn with the frozen model and effort')
                plan = json.loads(store.read_blob(effect['metadata']['plan_ref']))
                plan['envelope_bytes'] = store.read_blob(effect['packet_ref'])
                if plan['partial_evidence_ids'] or plan['deferred_evidence_ids']:
                    raise ValueError('Actual model request did not carry its complete requested source context')
                pages = present_delivery(plan, effect_id=effect['id'], sent_packet_sha256=receipt.get('sent_packet_sha256'),
                    sent_prompt_sha256=receipt.get('sent_prompt_sha256'), receipt_ref=effect['receipt_ref'],
                    sent_image_manifest=receipt.get('sent_image_manifest'),
                    sent_image_manifest_sha256=receipt.get('sent_image_manifest_sha256'),
                    image_delivery_verified=receipt.get('image_delivery_verified', False))
                runtime._verify_image_receipt(effect, receipt)
                if pages != json.loads(store.read_blob(proposal['delivery_ref'])):
                    raise ValueError('Delivery differs from its frozen source packet')
                if effect['kind'] == 'lab':
                    decision = literature_decision_record(receipt['response'], plan)
                    decisions.append({'effect_id': effect['id'], 'command': proposal['command'], 'decision': decision})
                    snapshots = [json.loads(page['content']) for page in pages
                                 if page['evidence_id'].startswith('campaign_state_') and page['complete_source']]
                    if not snapshots:
                        raise ValueError('Lab did not receive the complete current observation snapshot')
                    prior_scores = [row for row in effects if row['metadata'].get('adapter_id') == scorer.ADAPTER_ID
                                    and row['status'] == 'succeeded' and row['created_seq'] < effect['created_seq']]
                    expected_receipts = [{'effect_id': row['id'], 'receipt_ref': row['result_ref'],
                                          'packet_sha256': row['packet_hash']} for row in prior_scores]
                    count = sum(len(json.loads(store.read_blob(row['result_ref']))['result']['evaluations'])
                                for row in prior_scores)
                    for snapshot in snapshots:
                        end = INITIAL_COUNT + count
                        if (snapshot['new_evaluations'] != count or snapshot['remaining_evaluations'] != NEW_COUNT - count
                                or snapshot['candidates'] != progress['candidates'][:end]
                                or snapshot['scores'] != progress['scores'][:end]
                                or snapshot['observations'] != progress['observations'][:count]
                                or snapshot['evaluation_receipts'] != expected_receipts
                                or snapshot['trajectory_sha256'] != digest({'candidates': progress['candidates'][:end],
                                                                           'scores': progress['scores'][:end]})):
                            raise ValueError('Lab observation snapshot differs from all prior settled score receipts')
                    if (proposal['command']['action'] == 'no_action'
                            and any(row['new_evaluations'] == NEW_COUNT and row['remaining_evaluations'] == 0
                                    and row['trajectory_sha256'] == progress['trajectory_sha256'] for row in snapshots)):
                        finals.append({'effect_id': effect['id'], 'reason': proposal['command']['payload']['reason']})
                valid = True
            except (ValueError, KeyError, TypeError, OSError) as exc:
                errors.append({'effect_id': effect['id'], 'error': str(exc)})
            models.append({'effect_id': effect['id'], 'role': effect['kind'], 'valid': valid,
                           'actual_costs': effect.get('actual_costs', {})})
        elif effect['metadata'].get('adapter_id') == scorer.ADAPTER_ID and effect['status'] == 'succeeded':
            try:
                folder = store.root / 'pablo_evaluations' / effect['id']
                saved = json.loads((folder / 'receipt.json').read_bytes())
                journal_raw = (folder / 'evaluations.jsonl').read_bytes()
                journal = [json.loads(line) for line in journal_raw.splitlines()]
                expected = [{'effect_id': effect['id'], 'packet_sha256': effect['packet_hash'], **row}
                            for row in receipt['result']['evaluations']]
                if (saved != receipt or journal != expected
                        or digest(journal_raw) != receipt['execution']['journal_sha256']
                        or packet['oracle_fingerprint'] != runtime.config['pablo_oracle_fingerprint']
                        or receipt['execution']['scored_candidates'] != len(expected)):
                    raise ValueError('Scoring receipt, journal or frozen evaluator binding differs')
                score_receipts.append({'effect_id': effect['id'], 'count': len(expected),
                                       'journal_sha256': digest(journal_raw)})
            except (ValueError, KeyError, TypeError, OSError) as exc:
                errors.append({'effect_id': effect['id'], 'error': str(exc)})
    tasks = store.list(RUN_ID, 'registration', kind='literature_task')
    pending_work = [{key: row.get(key) for key in ('id', 'kind', 'status')} for row in store.list(RUN_ID, 'work')
                    if row['status'] not in {'succeeded', 'cancelled', 'failed'}]
    checks = {'frozen_inputs_code_and_configuration': frozen_valid,
        'initial_100': progress['initial_count'] == INITIAL_COUNT,
        'new_200': progress['new_evaluations'] == NEW_COUNT,
        'total_300_valid_unique': len(progress['candidates']) == INITIAL_COUNT + NEW_COUNT
            and len(set(progress['candidates'])) == INITIAL_COUNT + NEW_COUNT
            and all(oracle.canonicalize(candidate) == candidate for candidate in progress['candidates']),
        'all_200_have_score_journals': sum(row['count'] for row in score_receipts) == NEW_COUNT,
        'all_model_turns_certified': bool(models) and all(row['valid'] for row in models),
        'final_lab_received_200': bool(finals),
        'literature_tasks_finished': all(task['status'] == 'completed' for task in tasks),
        'literature_results_returned': all(any(effect['work_id'] == task.get('last_result_work_id')
            and proposals.get(effect['id'], {}).get('status') == 'applied' for effect in effects) for task in tasks),
        'no_unsettled_effects': not any(effect['status'] in {'prepared', 'dispatching', 'unknown'}
            or effect.get('accounting_unknown') for effect in effects),
        'all_tool_publications_finished': not runtime.pending_adapter_publications(),
        'no_pending_controller_work': not pending_work,
        'hard_budgets_respected': all(not row['hard'] or row['spent'] + row['reserved'] <= row['limit']
                                     for row in store.list(RUN_ID, 'budget')),
        'budget_totals_reconcile': all(math.isclose(row['spent'], sum(effect.get('actual_costs', {}).get(row['id'], 0)
            for effect in effects), abs_tol=1e-7) and math.isclose(row['reserved'],
            sum(effect.get('held_reservations', {}).get(row['id'], 0) for effect in effects), abs_tol=1e-7)
            for row in store.list(RUN_ID, 'budget'))}
    return {'schema_version': 'literature_autonomy_campaign_audit_v1',
        'workflow_passed': all(checks.values()) and not errors, 'checks': checks, 'errors': errors,
        'progress': progress, 'models': models, 'lab_decisions': decisions,
        'literature_tasks': tasks, 'final_lab_dispositions': finals, 'score_receipts': score_receipts,
        'budgets': store.list(RUN_ID, 'budget'), 'history': store.verify_history(RUN_ID),
        'outstanding_work': pending_work,
        'interpretation_limits': ['These checks verify the measured optimization trajectory and orchestration.',
            'No delegation frequency is required; whether a scientific gap warrants research is context dependent.',
            'Model explanations are interpretations, not independent biological or mechanistic validation.',
            'This run alone does not estimate false-positive or false-negative literature-routing rates.']}


def save_report(runtime, root, failure):
    report = audit(runtime)
    report['failure'] = failure
    write_json(root / 'audit.json', report)
    state = report['progress']
    initial_best = max(state['scores'][:INITIAL_COUNT])
    lines = ['# Continuous molecular campaign with autonomous Literature decisions', '',
        f"Workflow passed: {report['workflow_passed']}",
        f"Initial observations: {state['initial_count']}; new evaluations: {state['new_evaluations']}/{NEW_COUNT}.",
        f"Best initial score: {initial_best}; best final observed score: {max(state['scores'])}.",
        f"Real model turns: {len(report['models'])}; Literature tasks: {len(report['literature_tasks'])}.", '',
        *[f'- {key}: {value}' for key, value in report['checks'].items()], '',
        'The task did not instruct Lab to query publications or use Literature. No delegation quota was imposed.', '',
        *report['interpretation_limits']]
    if failure:
        lines += ['', 'Stop reason: ' + failure]
    (root / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    write_json(root / 'trajectory.json', {'candidates': state['candidates'], 'scores': state['scores'],
        'observations': state['observations'], 'trajectory_sha256': state['trajectory_sha256']})
    return report


def run(runtime, root, max_steps):
    runtime.recover()
    executor = CodexExecutor(runtime.store.root / 'calls', timeout_seconds=1800)
    previous = sorted((row for row in runtime.store.list(RUN_ID, 'effect')
                       if row['kind'] in {'lab', 'literature', 'meta'}), key=lambda row: row['created_seq'])
    last_role = previous[-1]['kind'] if previous else None
    for _ in range(max_steps):
        runtime.controller.tick(RUN_ID)
        if any(effect['status'] in {'dispatching', 'unknown'} or effect.get('accounting_unknown')
               for effect in runtime.store.list(RUN_ID, 'effect')):
            raise ValueError('An unresolved execution or accounting duty blocks new dispatch; no automatic resend')
        work, prepared = select_work(runtime, last_role)
        if work is None:
            return
        if work['kind'] == 'evidence_retrieve':
            delivery = fulfill_registered_source(runtime, work)
            log(root, 'local_source_delivered', work_id=work['id'], delivery_id=delivery['id'])
        elif work['kind'] == 'diagnostic':
            outcome = dispatch_prepared(runtime, prepared) if prepared else runtime.execute_adapter(
                work['inputs']['capability_id'], json.loads(work['inputs']['inputs_json']),
                trigger_id=work['id'], work_id=work['id'])
            effect = outcome.get('effect') or {}
            state = scorer.read_progress(runtime.store, RUN_ID)
            log(root, 'tool', capability=work['inputs']['capability_id'], effect_id=effect.get('id'),
                status=effect.get('status'), new_evaluations=state['new_evaluations'],
                remaining_evaluations=state['remaining_evaluations'])
            if effect.get('status') not in {'succeeded', 'failed_definite'}:
                raise ValueError('Tool execution is unresolved; preserve its exact attempt before continuing')
            if work['inputs']['capability_id'] == scorer.ADAPTER_ID and effect['status'] == 'succeeded':
                batches = sum(row['metadata'].get('adapter_id') == scorer.ADAPTER_ID and row['status'] == 'succeeded'
                              for row in runtime.store.list(RUN_ID, 'effect'))
                runtime.observe('campaign-boundary:' + effect['id'], 'batch_boundary', batches)
        else:
            if prepared is None:
                work = bind_observations(runtime, work)
                prepare_continuity(runtime, work, root)
            outcome = runtime.run_meta(work['id'], executor, runtime.config['model_reservation'])
            proposal = outcome.get('proposal') or {}
            last_role = outcome.get('effect', {}).get('kind')
            log(root, 'model', role=last_role, effect_id=outcome.get('effect', {}).get('id'),
                status=proposal.get('status'), command=proposal.get('command'), error=proposal.get('error'))
            if proposal.get('status') != 'applied':
                raise ValueError('The model command was not applied; inspect its recorded response and protocol result')
    raise ValueError('The prospectively bounded driver step allowance was reached')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--initial-csv', default=str(DEFAULT_INITIAL))
    parser.add_argument('--literature-config', default=str(PROJECT / 'examples/literature-tools.json'))
    parser.add_argument('--max-driver-steps', type=int, default=1200)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--initialize-only', action='store_true')
    parser.add_argument('--audit-only', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.max_driver_steps <= 2000:
        parser.error('--max-driver-steps must be between 1 and 2000')
    if args.audit_only and not args.resume:
        parser.error('--audit-only requires --resume')
    root = Path(args.root).resolve()
    runtime = load_runtime(root) if args.resume else initialize(root, Path(args.initial_csv).resolve(),
        Path(args.literature_config).resolve(), args.max_driver_steps)
    failure = None
    try:
        if args.initialize_only:
            return 0
        frozen = json.loads(runtime.store.read_blob(runtime.config['campaign_manifest_ref']))
        if not args.audit_only:
            run(runtime, root, min(args.max_driver_steps, frozen['max_driver_steps']))
    except Exception as exc:
        failure = type(exc).__name__ + ': ' + str(exc)
        log(root, 'blocked', reason=failure)
    finally:
        try:
            if not args.initialize_only:
                report = save_report(runtime, root, failure)
                log(root, 'finished', workflow_passed=report['workflow_passed'],
                    new_evaluations=report['progress']['new_evaluations'], failure=failure,
                    audit=str(root / 'audit.json'))
        finally:
            runtime.store.close()
    return 0 if failure is None and report['workflow_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
