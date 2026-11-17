"""Bounded live validation of one real paper through Lab and Literature.

The optional repository PDF is explicit host preparation, not agent discovery.
All research decisions, provider calls, source reads and image inputs use the
registered Runtime. Never resend an unknown effect or rewrite a failed receipt.
Run with PYTHONPATH=. and the configured literature virtual environment.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil

from rsi_lab.controller import Controller
from rsi_lab.roles import CodexExecutor
from rsi_lab.runtime import Runtime
from rsi_lab.session_context import read_native_snapshot
from rsi_lab.store import Store
from rsi_literature import make_adapters
from rsi_literature.documents import DocumentStore
from scripts.audit_literature_validation import audit


CAPABILITIES = ['literature.' + name for name in (
    'search_papers', 'get_paper', 'acquire_fulltext', 'read_document',
    'find_passages', 'build_reading_package', 'render_document_page', 'get_citations')]
RUN_ID = 'real-paper'
DOI = '10.1093/bioinformatics/btp352'
TITLE = 'The Sequence Alignment/Map format and SAMtools'
PDF_URL = 'https://dash.harvard.edu/bitstreams/7312037c-6b3b-6bd4-e053-0100007fdf3b/download'
GOAL = f'''完成一篇真实论文的 Literature 工具验收：{TITLE} (2009)。
Lab 先委派一个 Literature 子任务，分配 max_model_calls=18、max_tool_calls=36，
工具目录由宿主固定，Lab 不枚举或启用工具；将以下验收要求和限制交给子任务。
1. 先通过 Europe PMC search_papers 按标题检索(limit=2)，核对论文身份，再 get_paper。
2. acquire_fulltext(provider=europe_pmc, preferred_format=xml) 获取全文，
   read_document(max_bytes=24000) 从头读到 next_cursor 为空，覆盖全文而不只摘要。
3. 显式尝试一次 preferred_format=pdf 的远程获取；若失败，使用宿主已登记的
   source_id=samtools_repository_pdf。务必说明该备用PDF是宿主预先取得、不是本次自动发现。
4. 对取得的PDF调用 build_reading_package(provider=poppler)，用 find_passages 定位 Figure 1，
   请求 render_document_page(provider=poppler) 查看其所在PDF页。PDF可能有仓储封面，不能猜页号。
   结合实际图片核对图1面板(a)(b)(c)的对应关系，以及 r002 行的起始坐标与 CIGAR 字符串。
5. get_citations 两个方向 references 和 citing，各 limit=3；如有cursor，至少一个方向再取一页。
   从实际返回的每个方向各选一篇，用 get_paper 核对标题、标识和摘要。
   说明当前论文引用谁、谁引用当前论文；引用边不等于支持或复现。
6. 用中文返回简短研究结果：SAM/BAM的作用、图像核对结果、一条后向与一条前向追踪，
   以及全文/图片/引用分别覆盖了什么。所有关键结论引用本轮实际交付的 evidence IDs。
不要读取相邻论文全文，不要发起第二项研究，也不要尝试需要凭证的服务。
保留各document_id、页码、cursor、已完成步骤和后续计划到 working_memory_delta 的
attempted_steps/next_step/evidence_refs 中；工作记忆保持简短，不复制正文。
最终只选择少量关键原文、图像与引用证据回传Lab，避免把全部目录当成结论引用。
宿主可能在已结算的轮次之间checkpoint/rebuild；预算、任务和证据不重置。
子任务完成后Lab核对四阶段是否确有证据，以no_action简述验收结果，不再委派。'''


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def log(root, kind, **data):
    item = {'time': datetime.now(timezone.utc).isoformat(), 'kind': kind, **data}
    with (root / 'progress.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(item, ensure_ascii=False) + '\n')
    print(json.dumps(item, ensure_ascii=False), flush=True)


def initialize(root, pdf):
    if root.exists():
        raise ValueError('Use a fresh output directory; existing runs require explicit --resume')
    root.mkdir(parents=True)
    (root / 'inputs').mkdir()
    target = root / 'inputs' / 'repository-paper.pdf'
    shutil.copyfile(pdf, target)
    if not target.read_bytes().startswith(b'%PDF-'):
        raise ValueError('Host fallback must be a real PDF')
    config = {'default_provider': 'europe_pmc', 'enabled_providers': ['europe_pmc', 'poppler'],
        'timeout': 45, 'connectors': {'poppler': {'max_dimension': 1600, 'max_image_bytes': 1048576}},
        'local_sources': {'samtools_repository_pdf': {'path': str(target), 'content_scope': 'full_text',
            'source_url': PDF_URL, 'paper_ref': 'DOI:' + DOI}}}
    write_json(root / 'literature-config.json', config)
    run_config = {'goal': GOAL, 'execution_mode': 'live', 'model': 'gpt-6-astra', 'effort': 'medium',
        'max_input_bytes': 524288, 'max_effective_context_bytes': 2097152,
        'max_image_bytes': 4194304, 'max_effective_image_bytes': 16777216,
        'model_reservation': {'model_calls': 1, 'tokens': 60000, 'wall_seconds': 240}}
    budgets = {'model_calls': {'limit': 20, 'hard': True}, 'tool_calls': {'limit': 40, 'hard': True},
        'tokens': {'limit': 1800000, 'hard': False}, 'wall_seconds': {'limit': 5400, 'hard': False}}
    write_json(root / 'run-config.json', {**run_config, 'budgets': budgets})
    store = Store(root / 'ledger')
    controller = Controller(store)
    controller.initialize(RUN_ID, budgets=budgets, config=run_config)
    seed = 'host_pdf_source'
    controller.add_fact(RUN_ID, seed, 'source_statement', {
        'title': TITLE, 'source_id': 'samtools_repository_pdf', 'source_url': PDF_URL,
        'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
        'preparation': 'Host downloaded an openly accessible official university repository PDF before this run. '
                       'Use only as the declared fallback after a failed remote PDF acquisition. '
                       'No paper text, figure answer, or citation lookup results are supplied by this seed.'},
        {'delivery_kind': 'host_configuration', 'source': 'host-registered-repository-file'}, 'register-host-source')
    return store, seed


def select_work(runtime):
    tasks = runtime.store.list(RUN_ID, 'registration', kind='literature_task')
    ready = runtime.controller.ready(RUN_ID)
    if not tasks:
        return next((work for work in ready if work['kind'] == 'lab'), None)
    if len(tasks) != 1:
        raise ValueError('Validation permits exactly one Literature task')
    task = tasks[0]
    if task['status'] == 'completed':
        return next((work for work in ready if work['id'] == task['last_result_work_id']), None)
    return next((work for work in ready if work['kind'] == 'diagnostic'
                 and work['inputs'].get('literature_task_id') == task['id']), None) or next(
        (work for work in ready if work['kind'] == 'literature'
         and work['inputs'].get('literature_task_id') == task['id']), None)


def complete_packet(runtime, work, root):
    agent, mode, task, options = runtime._context(work)
    planned = agent.prepare(mode, task, **options)
    if planned['status'] != 'ready':
        raise ValueError('Current source evidence does not fit the bounded request; inspect saved ledger')
    if planned['deferred_evidence_ids'] or planned['partial_evidence_ids']:
        raise ValueError('This end-to-end validation requires complete evidence replay for synthesis; '
                         'the frozen packet budget cannot carry all requested sources')
    log(root, 'packet_preflight', work_id=work['id'], strategy=planned['strategy'],
        input_bytes=planned['input_bytes'], delivered_sources=len(planned['delivery_pages']),
        image_count=planned['image_count'])
    return planned, task


def prepare_continuity(runtime, work, root):
    planned, task = complete_packet(runtime, work, root)
    session = runtime.store.get(RUN_ID, 'agent_session', task['agent_state']['session_id'])
    if not session.get('provider_thread_id'):
        return
    prior = session['context_manifest']
    snapshot = read_native_snapshot(prior['native_session_ref'], expected_thread_id=session['provider_thread_id'],
        expected_cwd=str(runtime.store.root / 'agents' / session['id'] / 'workspace'),
        expected_prefix=prior['native_prefix'])
    size = snapshot.get('observable_text_history_bytes', snapshot['observable_history_bytes'])
    if session['committed_turn'] >= 3 or size + planned['input_bytes'] > runtime.config['max_input_bytes']:
        checkpoint = runtime.agents.seal_checkpoint(session['id'], 'paper-checkpoint:' + session['id'])
        rebuilt = runtime.agents.rebuild_session(session['id'],
            'Bound native history before the next paper-reading turn; preserve owner, evidence, memory and cumulative budgets',
            'paper-rebuild:' + session['id'], checkpoint_id=checkpoint['id'])
        log(root, 'session_rebuilt', previous_session=session['id'], session=rebuilt['id'],
            previous_text_bytes=size, checkpoint=checkpoint['id'])
        # Rebuilding changes session metadata in the actual request. Recheck
        # that exact context as well, before Runtime prepares any Effect.
        complete_packet(runtime, work, root)


def summarize(runtime, root, failure=None):
    store = runtime.store
    effects = sorted(store.list(RUN_ID, 'effect'), key=lambda row: row['created_seq'])
    tools, models = [], []
    for effect in effects:
        receipt = json.loads(store.read_blob(effect['result_ref'])) if effect.get('result_ref') else {}
        packet = json.loads(store.read_blob(effect['packet_ref']))
        if effect['kind'] in {'lab', 'literature', 'meta'}:
            models.append({'effect_id': effect['id'], 'role': effect['kind'], 'status': effect['status'],
                'protocol_valid': receipt.get('protocol_valid'), 'actual_costs': receipt.get('actual_costs'),
                'image_delivery_verified': receipt.get('image_delivery_verified', False),
                'response': receipt.get('response'), 'context_manifest': receipt.get('context_manifest')})
        elif effect['metadata'].get('adapter_id', '').startswith('literature.'):
            result = receipt.get('result') or {}
            tools.append({'effect_id': effect['id'], 'operation': packet['operation'], 'inputs': packet['inputs'],
                'effect_status': effect['status'], 'status': result.get('status'), 'errors': result.get('errors'),
                'data': result.get('data'), 'coverage': result.get('coverage'), 'provenance': result.get('provenance')})
    tasks = store.list(RUN_ID, 'registration', kind='literature_task')
    results = [json.loads(store.read_blob(store.get(RUN_ID, 'fact', fid)['content_ref']))
               for task in tasks for fid in task['result_ids']]
    searched = [row for row in tools if row['operation'] == 'search_papers' and row['status'] in {'ok', 'partial'}]
    cites = {direction: [row for row in tools if row['operation'] == 'get_citations'
             and row['inputs']['direction'] == direction and row['status'] in {'ok', 'partial'}]
             for direction in ('references', 'citing')}
    full_read = []
    documents = DocumentStore(root / 'documents')
    for doc_id in {row['inputs']['document_id'] for row in tools if row['operation'] == 'read_document'}:
        manifest = documents.manifest(doc_id)
        spans = sorted(row['data']['char_range'] for row in tools if row['operation'] == 'read_document'
            and row['inputs']['document_id'] == doc_id and row.get('data') and 'char_range' in row['data'])
        end = 0
        for start, stop in spans:
            if start > end:
                break
            end = max(end, stop)
        full_read.append({'document_id': doc_id, 'media_type': manifest['media_type'],
            'characters': manifest['text_characters'], 'covered_characters': end,
            'complete': bool(end) and end == manifest['text_characters'], 'source_metadata': manifest['source_metadata']})
    # Image bytes stay in the immutable ledger, not duplicated into a report.
    for row in tools:
        if isinstance(row.get('data'), dict) and isinstance(row['data'].get('image'), dict):
            row['data']['image'].pop('data_base64', None)
    summary = {'paper': {'title': TITLE, 'doi': DOI}, 'root': str(root), 'failure': failure,
        'checks': {'search_returned': bool(searched), 'complete_text_read': any(row['complete'] for row in full_read),
            'image_sent_and_native_verified': any(row['image_delivery_verified'] and
                (row.get('context_manifest') or {}).get('native_image_inputs_verified') for row in models),
            'references_returned': bool(cites['references']), 'citing_returned': bool(cites['citing']),
            'literature_completed': bool(tasks) and all(task['status'] == 'completed' for task in tasks),
            'lab_received': bool(tasks) and any(proposal['status'] == 'applied'
                and proposal.get('command', {}).get('action') == 'no_action'
                and store.get(RUN_ID, 'effect', proposal['effect_id'])['work_id']
                    == tasks[0].get('last_result_work_id')
                for proposal in store.list(RUN_ID, 'proposal')),
            'no_unknown_effects': not any(effect['status'] in {'unknown', 'dispatching'} for effect in effects)},
        'full_text_reads': full_read, 'tools': tools, 'models': models, 'research_results': results,
        'pending_publications': runtime.pending_adapter_publications(),
        'budgets': store.list(RUN_ID, 'budget'), 'history': store.verify_history(RUN_ID),
        'pdf_access_boundary': 'Repository PDF supplied and registered by host; remote PDF failure and fallback remain explicit.'}
    write_json(root / 'summary.json', summary)
    lines = [f'# Literature real-paper validation: {TITLE}', '', f'DOI: https://doi.org/{DOI}', '',
             '## Checks', '', *[f'- {key}: {value}' for key, value in summary['checks'].items()], '',
             '## Literature result', '']
    for result in results:
        lines.extend([result['answer'], '', *['- ' + item for item in result['limitations']], ''])
    if failure:
        lines.extend(['## Run limitation', '', failure, ''])
    lines.extend(['## PDF acquisition boundary', '', summary['pdf_access_boundary'], ''])
    (root / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--pdf')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if args.resume:
        store, seed = Store(root / 'ledger'), None
    else:
        if not args.pdf:
            parser.error('--pdf is required for a fresh validation')
        store, seed = initialize(root, Path(args.pdf).resolve())
    config = json.loads((root / 'literature-config.json').read_text())
    adapters = [adapter for adapter in make_adapters(root / 'documents', config) if adapter.adapter_id in CAPABILITIES]
    runtime = Runtime(store, RUN_ID, adapters=adapters)
    if seed:
        runtime.enqueue_lab('real-paper-start', [seed])
    executor = CodexExecutor(store.root / 'calls', timeout_seconds=240)
    failure = None
    try:
        for _ in range(45):
            if any(effect['status'] in {'unknown', 'dispatching'} for effect in store.list(RUN_ID, 'effect')):
                raise ValueError('An unresolved effect requires reconciliation; no automatic resend')
            work = select_work(runtime)
            if work is None:
                break
            if work['kind'] == 'diagnostic':
                inputs = json.loads(work['inputs']['inputs_json'])
                outcome = runtime.execute_adapter(work['inputs']['capability_id'], inputs,
                    trigger_id=work['id'], work_id=work['id'])
                log(root, 'tool', capability=work['inputs']['capability_id'], inputs=inputs,
                    effect_id=outcome['effect']['id'], status=(outcome.get('result') or {}).get('status'),
                    errors=(outcome.get('result') or {}).get('errors', []))
            else:
                prepare_continuity(runtime, work, root)
                outcome = runtime.run_meta(work['id'], executor, runtime.config['model_reservation'])
                proposal = outcome.get('proposal') or {}
                log(root, 'model', role=work['kind'], effect_id=outcome.get('effect', {}).get('id'),
                    status=proposal.get('status'), command=proposal.get('command'), error=proposal.get('error'))
                if proposal.get('status') != 'applied':
                    raise ValueError('Model command was not applied; inspect exact receipt before continuing')
        summary = summarize(runtime, root)
        if not all(summary['checks'].values()) or summary['pending_publications']:
            failure = 'Not all requested stages have verified evidence; see checks and exact receipts.'
    except Exception as exc:
        failure = type(exc).__name__ + ': ' + str(exc)
        log(root, 'blocked', reason=failure)
    finally:
        try:
            try:
                audited = audit(root)
            except Exception as exc:
                audited = {'workflow_passed': False, 'audit_error': type(exc).__name__ + ': ' + str(exc)}
            write_json(root / 'audit.json', audited)
            if not audited['workflow_passed']:
                failure = failure or 'Independent delivery/identity/parent handoff audit did not pass; see audit.json.'
            summary = summarize(runtime, root, failure)
            log(root, 'finished', checks=summary['checks'], workflow_passed=audited['workflow_passed'],
                failure=failure, report=str(root / 'report.md'), audit=str(root / 'audit.json'))
        finally:
            store.close()
    return 1 if failure else 0


if __name__ == '__main__':
    raise SystemExit(main())
