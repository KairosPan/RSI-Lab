"""Audit the SAMtools validation from a consistent, read-only ledger snapshot.

This does not trust summary.json, invoke providers, modify the ledger, or scan
native session history. Only image turns' exact ledger-owned native prefixes
are read. A passing workflow audit is not scientific validation of its answer.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import html
import json
import math
from pathlib import Path
import re
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rsi_lab.images import validate_image_content, verify_image_transport
from rsi_lab.packets import present_delivery
from rsi_lab.session_context import snapshot_from_bytes, verify_snapshot_images
from rsi_lab.store import Store, digest

DOI = '10.1093/bioinformatics/btp352'
TARGET = {'doi:' + DOI, 'pmid:19505943', 'pmcid:PMC2723002'}


class ReadOnlyStore(Store):
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.blobs = self.root / 'objects'
        self.db = sqlite3.connect((self.root / 'ledger.sqlite3').as_uri() + '?mode=ro', uri=True)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA query_only=ON')
        self.db.execute('BEGIN')

    def put_blob(self, value):
        raise RuntimeError('The auditor cannot write ledger objects')

    def mutate(self, *args, **kwargs):
        raise RuntimeError('The auditor cannot mutate the ledger')


def reference(value):
    value = str(value or '').strip()
    value = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', value, flags=re.I)
    value = re.sub(r'^europe_pmc:', '', value, flags=re.I)
    if value.lower().startswith('doi:'):
        return 'doi:' + value[4:].lower()
    if value.startswith('10.'):
        return 'doi:' + value.lower()
    if re.fullmatch(r'(?:PMID:|MED:)?[0-9]+', value, re.I):
        return 'pmid:' + value.rsplit(':', 1)[-1]
    value = re.sub(r'^(?:PMCID:|PMC:)', '', value, flags=re.I)
    if re.fullmatch(r'PMC[0-9]+', value, re.I):
        return 'pmcid:' + value.upper()
    return value.lower()


def identities(paper):
    return {reference(prefix + str(paper[key])) for key, prefix in
            [('doi', 'doi:'), ('pmid', 'PMID:'), ('pmcid', 'PMCID:'),
             ('paper_id', ''), ('provider_id', '')] if paper.get(key)}


def abstract_coverage(paper):
    """Classify abstract content in an already fully delivered metadata result."""
    raw = paper.get('abstract')
    available = isinstance(raw, str) and bool(html.unescape(re.sub(r'<[^>]*>', ' ', raw)).strip())
    complete = available and not paper.get('abstract_truncated')
    status = 'delivered_complete' if complete else 'delivered_truncated' if available else 'not_available'
    return {'abstract_available': available, 'abstract_delivery_status': status,
            'complete_abstract_delivery_certified': complete,
            'summary_constraint': (
                'Abstract text was delivered; scientific support still requires review.' if complete else
                'Only truncated abstract text was delivered; do not claim the complete abstract was read.' if available else
                'No abstract text was supplied. Report metadata only; do not claim this abstract was read.')}


def covered(ranges, total):
    """Require the exact complete interval, permitting overlap but not gaps."""
    end = 0
    for start, stop in sorted(ranges):
        if not (type(start) is int and type(stop) is int and 0 <= start <= stop <= total):
            return False, end
        if start > end:
            return False, end
        end = max(end, stop)
    return bool(total) and end == total, end


def document(root, document_id):
    if not re.fullmatch(r'doc_[a-f0-9]{64}', document_id):
        raise ValueError('Invalid registered document identity')
    folder = root / 'documents' / document_id
    if folder.is_symlink() or any((folder / name).is_symlink() for name in
                                  ('manifest.json', 'source.bin', 'text.txt')):
        raise ValueError('Document artifacts cannot be symbolic links')
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    original, text = (folder / 'source.bin').read_bytes(), (folder / 'text.txt').read_bytes()
    if (manifest['document_id'] != document_id or digest(original) != manifest['source_sha256']
            or digest(text) != manifest['text_sha256']
            or digest({k: v for k, v in manifest.items() if k != 'document_version'}) != manifest['document_version']
            or len(text.decode()) != manifest['text_characters']):
        raise ValueError('Document artifacts differ from their registered manifest')
    return manifest, text.decode()


def audit(root, run_id='real-paper'):
    store = ReadOnlyStore(root / 'ledger')
    try:
        history = store.verify_history(run_id)
        load = lambda ref: json.loads(store.read_blob(ref))
        config = store.run(run_id)['config']
        effects = sorted(store.list(run_id, 'effect'), key=lambda row: row['created_seq'])
        facts = {row['id']: row for row in store.list(run_id, 'fact')}
        proposals = {row['effect_id']: row for row in store.list(run_id, 'proposal')}
        sessions = {row['id']: row for row in store.list(run_id, 'agent_session')}
        tasks = store.list(run_id, 'registration', kind='literature_task')
        task = tasks[0] if len(tasks) == 1 else {}
        task_id = task.get('id')
        checks, errors, models, tools = {}, [], [], []
        for effect in effects:
            packet = load(effect['packet_ref'])
            receipt = load(effect['result_ref']) if effect.get('result_ref') else {}
            if effect['kind'] in {'lab', 'literature', 'meta'}:
                proposal = proposals.get(effect['id'], {})
                model = {'effect': effect, 'receipt': receipt, 'proposal': proposal,
                         'pages': [], 'valid': False, 'image_valid': False}
                try:
                    if (effect['status'] != 'succeeded' or effect.get('accounting_unknown')
                            or receipt.get('protocol_valid') is not True or not receipt.get('provider_thread_id')
                            or receipt.get('never_dispatched') or receipt.get('synthetic')
                            or not receipt.get('terminal_received') or proposal.get('status') != 'applied'):
                        raise ValueError('Model did not complete a real, applied, protocol-valid turn')
                    plan = load(effect['metadata']['plan_ref'])
                    plan['envelope_bytes'] = store.read_blob(effect['packet_ref'])
                    pages = present_delivery(plan, effect_id=effect['id'],
                        sent_packet_sha256=receipt.get('sent_packet_sha256'),
                        sent_prompt_sha256=receipt.get('sent_prompt_sha256'), receipt_ref=effect['receipt_ref'],
                        sent_image_manifest=receipt.get('sent_image_manifest'),
                        sent_image_manifest_sha256=receipt.get('sent_image_manifest_sha256'),
                        image_delivery_verified=receipt.get('image_delivery_verified', False))
                    if pages != load(proposal['delivery_ref']):
                        raise ValueError('Stored delivery differs from admitted packet and actual receipt')
                    model['pages'] = pages
                    expected = packet.get('image_attachments', [])
                    if expected:
                        verify_image_transport(store.root / 'calls' / effect['id'], expected, receipt)
                        context = receipt.get('context_manifest') or {}
                        if context.get('native_image_inputs_verified') is not True:
                            raise ValueError('Native image delivery has not been verified')
                        sid = effect['metadata']['agent_session_id']
                        thread = receipt['provider_thread_id']
                        native = Path(context['native_session_ref'])
                        if (native.is_symlink() or not native.name.endswith('-' + thread + '.jsonl')
                                or sessions[sid].get('provider_thread_id') != thread):
                            raise ValueError('Native image history is not owned by this task session')
                        prefix = context['native_prefix']
                        with native.open('rb') as stream:
                            raw = stream.read(prefix['prefix_bytes'])
                        if digest(raw) != prefix['prefix_sha256']:
                            raise ValueError('Committed native prefix hash mismatch')
                        cwd = str(store.root / 'agents' / sid / 'workspace')
                        prior = (effect['metadata']['agent_session'].get('context_manifest') or {}).get('native_prefix')
                        after = snapshot_from_bytes(raw, expected_thread_id=thread, expected_cwd=cwd, expected_prefix=prior)
                        before_count = (snapshot_from_bytes(raw[:prior['prefix_bytes']], expected_thread_id=thread,
                                        expected_cwd=cwd)['record_count'] if prior else 0)
                        verify_snapshot_images(after, expected, after_record=before_count)
                        model['image_valid'] = True
                    # No intermediate success state survives a later image/native failure.
                    model['valid'] = True
                except (ValueError, KeyError, OSError, TypeError) as exc:
                    model.update(valid=False, image_valid=False, pages=[])
                    errors.append({'effect_id': effect['id'], 'reason': str(exc)})
                models.append(model)
            elif effect['metadata'].get('adapter_id', '').startswith('literature.'):
                tools.append({'effect': effect, 'operation': packet['operation'], 'inputs': packet['inputs'],
                              'result': receipt.get('result') or {}})

        def successful(row):
            return (row['effect']['status'] == 'succeeded' and not row['effect'].get('accounting_unknown')
                    and row['effect']['metadata'].get('literature_task_id') == task_id
                    and row['result'].get('status') in {'ok', 'partial'})

        def operation(name):
            return [row for row in tools if row['operation'] == name and successful(row)]

        child_models = [model for model in models if model['effect']['kind'] == 'literature'
                        and model['effect']['metadata'].get('literature_task_id') == task_id and model['valid']]

        def delivered(fact, after_seq=0, candidates=None):
            matches = []
            for model in child_models if candidates is None else candidates:
                if not model['valid'] or model['effect']['created_seq'] <= after_seq:
                    continue
                for page in model['pages']:
                    if (page['evidence_id'] == fact['id'] and page['complete_source']
                            and page['source_version'] == fact['version']
                            and page['source_sha256'] == fact['content_ref']
                            and page['content'].encode() == store.read_blob(fact['content_ref'])):
                        matches.append({'model_effect_id': model['effect']['id'], 'page_id': page['page_id']})
            return matches

        def observed(row):
            return [match for fact in facts.values() if fact['provenance'].get('effect_id') == row['effect']['id']
                    and fact['provenance'].get('delivery_kind') == 'tool_observation'
                    for match in delivered(fact, row['effect']['created_seq'])]

        searches = [{'effect_id': row['effect']['id'], 'paper': paper, 'delivered': observed(row)}
                    for row in operation('search_papers') for paper in row['result'].get('data', {}).get('papers', [])
                    if 'doi:' + DOI in identities(paper)]
        metadata = operation('get_paper')
        target_metadata = [row for row in metadata if reference(row['inputs'].get('paper_ref')) in TARGET
                           and identities(row['result'].get('data', {}).get('paper', {})) & TARGET]
        checks['search_target_identity_delivered'] = any(row['delivered'] for row in searches)
        checks['target_metadata_confirmed'] = any(observed(row) for row in target_metadata)

        acquisitions = operation('acquire_fulltext')
        target_documents, reads = {}, []
        for row in acquisitions:
            data = row['result'].get('data', {})
            if not data.get('document_id'):
                continue
            manifest, text = document(root, data['document_id'])
            if reference(manifest['source_metadata'].get('paper_ref')) not in TARGET:
                continue
            if data.get('document_version') != manifest['document_version']:
                raise ValueError('Acquisition receipt document version mismatch')
            target_documents[manifest['document_id']] = (manifest, text, row)
        for doc_id, (manifest, text, acquisition) in target_documents.items():
            returned_ranges, delivered_ranges, entries = [], [], []
            for row in operation('read_document'):
                data = row['result'].get('data', {})
                if data.get('document_id') != doc_id or data.get('document_version') != manifest['document_version']:
                    continue
                span = data.get('char_range', [])
                if len(span) != 2 or data.get('text') != text[span[0]:span[1]]:
                    continue
                returned_ranges.append(span)
                passages = data.get('passages', [])
                links, missing = [], []
                for passage in passages:
                    matching = [fact for fact in facts.values() if fact['provenance'].get('effect_id') == row['effect']['id']
                                and fact['provenance'].get('delivery_kind') == 'source_excerpt'
                                and fact['content_ref'] == digest(passage)]
                    found = [(fact, delivered(fact, row['effect']['created_seq'])) for fact in matching]
                    found = [(fact, pages) for fact, pages in found if pages]
                    if found:
                        links.extend({'evidence_id': fact['id'], 'char_range': passage['char_range'], 'deliveries': pages}
                                     for fact, pages in found)
                    else:
                        missing.append({'char_range': passage.get('char_range'), 'locator': passage.get('locator')})
                # Source passages omit separator whitespace; reject uncovered non-whitespace.
                mask = bytearray(max(0, span[1] - span[0]))
                for passage in passages:
                    a, b = passage['char_range']
                    if (passage.get('document_id') != doc_id or passage.get('document_version') != manifest['document_version']
                            or passage.get('source_sha256') != manifest['source_sha256']
                            or not span[0] <= a <= b <= span[1] or passage.get('text') != text[a:b]):
                        missing.append({'invalid_passage': passage.get('locator')})
                        continue
                    mask[a - span[0]:b - span[0]] = b'\x01' * (b - a)
                gaps_are_whitespace = all(flag or char.isspace() for flag, char in zip(mask, text[span[0]:span[1]]))
                if passages and not missing and gaps_are_whitespace:
                    delivered_ranges.append(span)
                entries.append({'effect_id': row['effect']['id'], 'char_range': span, 'next_cursor': data.get('next_cursor'),
                                'source_passages': links, 'missing_passages': missing,
                                'uncovered_text_is_whitespace': gaps_are_whitespace})
            source_complete = (manifest['content_scope'] == 'full_text' and
                               manifest['coverage'].get('normalized_text_complete') is True and
                               manifest['coverage'].get('extraction_status') == 'complete')
            returned, returned_end = covered(returned_ranges, len(text))
            viewed, delivered_end = covered(delivered_ranges, len(text))
            reads.append({'document_id': doc_id, 'document_version': manifest['document_version'],
                          'source_sha256': manifest['source_sha256'], 'media_type': manifest['media_type'],
                          'total_characters': len(text), 'returned_characters': returned_end,
                          'delivered_characters': delivered_end, 'source_extraction_complete': source_complete,
                          'returned_full_text': returned and source_complete,
                          'delivered_full_text': viewed and source_complete and any(e['next_cursor'] is None for e in entries),
                          'reads': entries})
        checks['target_full_text_actually_delivered'] = any(row['delivered_full_text'] for row in reads)

        def original_pdf(document_id):
            for _ in range(8):
                manifest, _ = document(root, document_id)
                if manifest['media_type'].split(';')[0] == 'application/pdf':
                    return document_id
                origin = manifest['source_metadata'].get('derived_from') or {}
                if not origin.get('document_id'):
                    return None
                parent, _ = document(root, origin['document_id'])
                if any(origin.get(key) != parent[key] for key in ('document_version', 'source_sha256')):
                    raise ValueError('Derived text is not bound to its original PDF version/hash')
                document_id = parent['document_id']
            raise ValueError('Derived PDF lineage exceeds its bounded depth')

        citations = {}
        for direction in ('references', 'citing'):
            rows = [row for row in operation('get_citations') if row['inputs'].get('direction') == direction
                    and reference(row['inputs'].get('paper_ref')) in TARGET
                    and row['result'].get('data', {}).get('direction') == ('cited_by' if direction == 'citing' else direction)]
            edges, followups = [], []
            for row in rows:
                for paper in row['result'].get('data', {}).get('papers', []):
                    ids = identities(paper)
                    if not ids or ids & TARGET:
                        continue
                    edges.append({'effect_id': row['effect']['id'], 'identities': sorted(ids), 'title': paper.get('title'),
                                  'delivered': observed(row)})
                    for follow in metadata:
                        detail = follow['result'].get('data', {}).get('paper', {})
                        if (follow['effect']['created_seq'] > row['effect']['created_seq']
                                and reference(follow['inputs'].get('paper_ref')) in ids
                                and identities(detail) & ids and detail.get('title') and observed(row) and observed(follow)):
                            followups.append({'edge_effect_id': row['effect']['id'], 'metadata_effect_id': follow['effect']['id'],
                                              'paper': detail, **abstract_coverage(detail),
                                              'delivered': observed(follow)})
            citations[direction] = {'edges': edges, 'followups': followups}
            checks[direction + '_nonempty_delivered'] = any(edge['delivered'] for edge in edges)
            checks[direction + '_metadata_followup_delivered'] = bool(followups)
            checks[direction + '_abstract_followup_delivered'] = any(
                follow['complete_abstract_delivery_certified'] for follow in followups)
        citation_rows = [row for row in operation('get_citations') if reference(row['inputs'].get('paper_ref')) in TARGET]
        cursors = [(row, row['result'].get('continuation', {}).get('next_cursor')) for row in citation_rows]
        checks['citation_pagination_when_available'] = not any(cursor for _, cursor in cursors) or any(
            later['effect']['created_seq'] > earlier['effect']['created_seq'] and
            later['inputs'].get('direction') == earlier['inputs'].get('direction') and
            later['inputs'].get('cursor') == cursor and observed(later)
            for earlier, cursor in cursors if cursor for later in citation_rows)

        visuals = []
        for row in operation('render_document_page'):
            content = row['result'].get('data', {}).get('image')
            if not content:
                continue
            content = validate_image_content(content)
            doc_id = content['source_document_id']
            if doc_id not in target_documents:
                continue
            manifest, _, acquisition = target_documents[doc_id]
            if (manifest['media_type'].split(';')[0] != 'application/pdf'
                    or manifest['source_sha256'] != content['source_sha256']
                    or original_pdf(row['inputs'].get('document_id')) != doc_id
                    or row['inputs'].get('page') != content['page']
                    or row['result']['data'].get('source_document_version') != manifest['document_version']):
                continue
            delivered_images = []
            for model in child_models:
                if not model['image_valid'] or model['effect']['created_seq'] <= row['effect']['created_seq']:
                    continue
                for page in model['pages']:
                    image = page.get('image') or {}
                    fact = facts.get(page['evidence_id'], {})
                    if (page.get('visual_presented') and fact.get('provenance', {}).get('effect_id') == row['effect']['id']
                            and image.get('sha256') == content['sha256'] and image.get('page') == content['page']
                            and image.get('source_document_id') == doc_id and image.get('source_sha256') == manifest['source_sha256']):
                        delivered_images.append({'model_effect_id': model['effect']['id'], 'page_id': page['page_id'],
                                                 'evidence_id': page['evidence_id'], 'image_manifest_sha256': page['image_manifest_sha256']})
            visuals.append({'render_effect_id': row['effect']['id'], 'document_id': doc_id,
                            'pdf_source_sha256': manifest['source_sha256'], 'page': content['page'],
                            'image_sha256': content['sha256'], 'deliveries': delivered_images})
        checks['target_pdf_image_hash_and_native_delivery'] = any(row['deliveries'] for row in visuals)
        checks['remote_pdf_acquisition_attempted'] = any(row['operation'] == 'acquire_fulltext'
            and row['inputs'].get('provider') == 'europe_pmc' and row['inputs'].get('preferred_format') == 'pdf'
            and reference(row['inputs'].get('paper_ref')) in TARGET and row['effect']['status'] == 'succeeded'
            and row['result'].get('status') in {'ok', 'partial', 'unavailable', 'failed'} for row in tools)
        navigation = []
        effect_index = {effect['id']: effect for effect in effects}
        for visual in visuals:
            for package in operation('build_reading_package'):
                derived_id = package['result'].get('data', {}).get('document_id')
                if (package['inputs'].get('provider') != 'poppler' or not derived_id
                        or package['inputs'].get('document_id') != visual['document_id']
                        or original_pdf(derived_id) != visual['document_id']):
                    continue
                for search in operation('find_passages'):
                    if not (package['effect']['created_seq'] < search['effect']['created_seq'] <
                            effect_index[visual['render_effect_id']]['created_seq']):
                        continue
                    for passage in search['result'].get('data', {}).get('passages', []):
                        locator = passage.get('source_locator') or {}
                        if (passage.get('document_id') != derived_id or not isinstance(locator, dict)
                                or locator.get('page') != visual['page']
                                or not re.search(r'\bFig(?:ure)?\.?\s*1\b', passage.get('text', ''), re.I)):
                            continue
                        for fact in facts.values():
                            if fact['provenance'].get('effect_id') == search['effect']['id'] and fact['content_ref'] == digest(passage):
                                proof = delivered(fact, search['effect']['created_seq'], [model for model in child_models
                                    if model['effect']['created_seq'] < effect_index[visual['render_effect_id']]['created_seq']])
                                if proof:
                                    navigation.append({'package_effect_id': package['effect']['id'],
                                        'find_effect_id': search['effect']['id'], 'render_effect_id': visual['render_effect_id'],
                                        'derived_document_id': derived_id, 'source_pdf_id': visual['document_id'],
                                        'page': visual['page'], 'evidence_id': fact['id'], 'deliveries': proof})
        checks['pdf_reading_package_and_figure_search'] = bool(navigation)
        local_pdfs = [(manifest, acquisition) for manifest, _, acquisition in target_documents.values()
                      if manifest['media_type'].split(';')[0] == 'application/pdf'
                      and acquisition['inputs'].get('provider') == 'local']
        seed = load(facts['host_pdf_source']['content_ref']) if 'host_pdf_source' in facts else {}
        fallback_details = []
        for manifest, acquisition in local_pdfs:
            previous_failures = [row for row in tools if row['operation'] == 'acquire_fulltext'
                and row['inputs'].get('provider') == 'europe_pmc' and row['inputs'].get('preferred_format') == 'pdf'
                and reference(row['inputs'].get('paper_ref')) in TARGET
                and row['effect']['created_seq'] < acquisition['effect']['created_seq']
                and row['effect']['status'] == 'succeeded' and row['result'].get('status') in {'failed', 'unavailable'}]
            fallback_details.append({'document_id': manifest['document_id'], 'acquire_effect_id': acquisition['effect']['id'],
                'source_hash_matches_host_seed': seed.get('sha256') == manifest['source_sha256'],
                'source_id_matches_host_seed': seed.get('source_id') == acquisition['inputs'].get('source_id'),
                'prior_remote_failure_effect_ids': [row['effect']['id'] for row in previous_failures]})
        checks['host_pdf_fallback_explicit_and_ordered'] = all(row['source_hash_matches_host_seed']
            and row['source_id_matches_host_seed'] and row['prior_remote_failure_effect_ids'] for row in fallback_details)

        parent_effect = next((effect for effect in effects if effect['id'] == task.get('parent_effect_id')), {})
        parent_model = next((model for model in models if model['effect']['id'] == parent_effect.get('id')), {})
        checks['real_lab_delegation'] = bool(task and parent_model.get('valid')
            and parent_effect.get('kind') == 'lab' and parent_model['proposal']['command']['action'] == 'request_literature'
            and sessions[parent_effect['metadata']['agent_session_id']]['owner_id'] == task['parent_owner_id'])
        completions = [model for model in child_models if model['proposal']['command']['action'] == 'complete_research']
        checks['literature_completed_sufficient'] = bool(task.get('status') == 'completed' and completions
            and completions[-1]['proposal']['command']['payload']['coverage_status'] == 'sufficient_for_requested_use')
        finals = [model for model in models if model['valid'] and model['effect']['kind'] == 'lab'
                  and model['effect']['work_id'] == task.get('last_result_work_id')
                  and model['proposal']['command']['action'] == 'no_action'
                  and sessions[model['effect']['metadata']['agent_session_id']]['owner_id'] == task.get('parent_owner_id')]
        result_ids = task.get('result_ids', [])
        checks['parent_lab_result_delivered_and_no_action_applied'] = bool(finals and result_ids and any(
            delivered(facts[result_ids[-1]], candidates=[model]) for model in finals))

        budgets = store.list(run_id, 'budget')
        budget_details = []
        for budget in budgets:
            name = budget['id']
            spent = sum(effect.get('actual_costs', {}).get(name, 0) for effect in effects)
            reserved = sum(effect.get('held_reservations', {}).get(name, 0) for effect in effects)
            agrees = (math.isclose(spent, budget['spent'], abs_tol=1e-8) and
                      math.isclose(reserved, budget['reserved'], abs_tol=1e-8)
                      and budget['limit'] == config['budgets'][name]['limit'])
            budget_details.append({'resource': name, 'spent': budget['spent'], 'reserved': budget['reserved'],
                'sum_effect_actual': spent, 'sum_effect_held': reserved, 'limit': budget['limit'], 'hard': budget['hard'],
                'reconciled': agrees, 'within_limit': budget['spent'] + budget['reserved'] <= budget['limit']})
        reconstruction_events = [dict(row) for row in store.db.execute(
            "SELECT * FROM events WHERE run_id=? AND event_type='agent_session_reconstructed'", (run_id,))]
        rebuild_kept_budgets = all(not any(change['kind'] == 'budget' for change in load(event['changes_ref']))
                                  for event in reconstruction_events)
        child_sessions = [session for session in sessions.values() if session['agent_instance_id'] == task.get('agent_instance_id')]
        owners = {session['owner_id'] for session in child_sessions}
        checks['rebuild_preserved_owner_and_cumulative_budget'] = bool(task and len(owners) == 1 and rebuild_kept_budgets
            and all(row['reconciled'] for row in budget_details))
        checks['hard_budgets_within_limits'] = all(row['within_limit'] for row in budget_details if row['hard'])
        child_effects = [effect for effect in effects if effect['metadata'].get('literature_task_id') == task_id]
        child_budget = {resource: sum(effect.get('actual_costs', {}).get(resource, 0)
            + effect.get('held_reservations', {}).get(resource, 0) for effect in child_effects)
            for resource in ('model_calls', 'tool_calls')}
        checks['child_budget_cumulative_across_sessions'] = bool(task and
            len(child_effects) <= task['max_steps'] and all(child_budget[resource] <= task['max_' + resource]
                                                         for resource in child_budget))
        checks['no_unsettled_or_unknown_effects'] = not any(effect['status'] in {'prepared', 'dispatching', 'unknown'}
            or effect.get('accounting_unknown') for effect in effects)
        publications = store.list(run_id, 'registration', kind='adapter_publication')
        checks['all_tool_results_published'] = all(any(pub['effect_id'] == row['effect']['id'] and pub['status'] == 'complete'
            for pub in publications) for row in tools if row['effect']['status'] == 'succeeded')
        checks['all_model_turns_certified'] = bool(models) and all(model['valid'] for model in models)
        return {'audit_schema': 'literature_validation_audit_v1', 'audited_at': datetime.now(timezone.utc).isoformat(),
            'root': str(root), 'run_id': run_id, 'target_doi': DOI, 'history': history,
            'workflow_passed': all(checks.values()) and not errors, 'checks': checks, 'errors': errors,
            'search_matches': searches, 'full_text_reads': reads, 'citations': citations, 'visuals': visuals,
            'figure_navigation': navigation, 'host_pdf_fallbacks': fallback_details,
            'lab_final_reasons': [model['proposal']['command']['payload']['reason'] for model in finals],
            'research_results': [model['proposal']['command']['payload'] for model in completions],
            'research_result_authority': 'Unmodified agent interpretations; not independent scientific validation.',
            'summary_coverage_constraints': [{'direction': direction, 'paper_id': follow['paper'].get('paper_id'),
                'metadata_effect_id': follow['metadata_effect_id'], 'abstract_delivery_status': follow['abstract_delivery_status'],
                'constraint': follow['summary_constraint']} for direction, review in citations.items() for follow in review['followups']],
            'budgets': budget_details, 'child_budget': {**child_budget, 'effect_steps': len(child_effects)},
            'continuity': {'literature_owner_ids': sorted(owners),
                'literature_session_count': len(child_sessions),
                'literature_native_thread_count': len({session['provider_thread_id'] for session in child_sessions if session.get('provider_thread_id')}),
                'literature_rebuild_count': sum(bool(session.get('supersedes_session')) for session in child_sessions),
                'all_rebuilds_preserved_budget_rows': rebuild_kept_budgets},
            'interpretation_limits': [
                'A delivery proves that exact evidence was supplied, not that every sentence was understood.',
                'A successful no_action is a recorded Lab disposition; its wording and scientific acceptance require review.',
                'Neighbor metadata verification never substitutes for missing or truncated abstract text; obey per-paper summary constraints.',
                'The image hash audit proves delivery of the requested PDF page, not correctness of figure interpretation.',
                'Normalized full text excludes unread supplementary files and visual content not separately inspected.',
                'Repository PDF preparation is host supplied; this audit does not classify it as autonomous discovery.']}
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--run', default='real-paper')
    args = parser.parse_args()
    root = Path(args.root).resolve()
    try:
        report = audit(root, args.run)
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as exc:
        report = {'audit_schema': 'literature_validation_audit_v1', 'workflow_passed': False,
                  'root': str(root), 'run_id': args.run, 'audit_error': type(exc).__name__ + ': ' + str(exc)}
    target = root / 'audit.json'
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'audit': str(target), 'workflow_passed': report['workflow_passed'],
                      'checks': report.get('checks'), 'audit_error': report.get('audit_error')}, ensure_ascii=False))
    return 0 if report['workflow_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
