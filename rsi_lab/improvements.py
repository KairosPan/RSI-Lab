"""Bounded, evidence-bound Meta -> Lab -> executed result -> Meta feedback.

Recommendations are proposals, never scientific facts or executable code. The
controller measures a real adapter publication; neither adoption nor successful
execution proves causal benefit. All transitions have replay-stable event keys.
"""
from __future__ import annotations

import json

from .controller import identity
from .store import digest, encode


def enabled(config, name):
    return isinstance(config.get('research_features'), dict) and config['research_features'].get(name) is True


class ImprovementLoop:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store, self.run_id = runtime.store, runtime.run_id

    def rows(self):
        return self.store.list(self.run_id, 'registration', kind='research_recommendation')

    def pending(self):
        return [r for r in self.rows() if r['status'] not in {'declined', 'deferred_closed', 'reviewed'}]

    def context(self, work, mode):
        """Return public typed state plus FULL required original evidence ids."""
        rows = self.rows()
        if mode == 'lab':
            selected = sorted((r for r in rows if r['status'] in {'proposed', 'deferred'}),
                              key=lambda r: r['created_seq'])[:1]
        else:
            selected = [r for r in rows if r['id'] in work['inputs'].get('improvement_review_ids', [])]
        facts = sorted((f for f in self.store.list(self.run_id, 'fact')
                        if f['kind'] == 'raw_fact' and f['provenance'].get('effect_id')),
                       key=lambda f: f['created_seq'])
        latest = [facts[-1]['id']] if facts else []
        required = list(dict.fromkeys([fid for r in selected for fid in
            r['source_evidence_ids'] + [r['recommendation_fact_id']] + r.get('response_fact_ids', [])
            + r.get('result_evidence_ids', []) + ([r['measurement_fact_id']] if r.get('measurement_fact_id') else [])]
            + (latest if selected else [])))
        public = [{k: r[k] for k in ('id', 'version', 'status', 'recommendation', 'source_evidence_ids',
                                    'recommendation_fact_id', 'defer_count')}
                  | {k: r[k] for k in ('tool_work_id', 'executed_effect_id', 'measurement', 'measurement_fact_id') if k in r}
                  for r in selected]
        return {'recommendations': public if mode == 'lab' else [],
                'improvement_reviews': public if mode != 'lab' else [],
                'recommendation_allowance': max(0, self.runtime.config.get('max_recommendations', 8) - len(rows)),
                'causal_benefit': 'not_established_without_a_controlled_comparison'}, required

    @staticmethod
    def _delivered(packet):
        return {p['evidence_id'] for p in packet['delivery_pages'] if p['complete_source']}

    def validate(self, response, packet):
        mode = packet['envelope']['mode']
        scope = packet['task']['constraints']['research_loop']
        complete = self._delivered(packet)
        command = response['command']
        action = command['action']
        if action == 'recommend_change':
            if mode == 'lab' or scope['improvement_reviews']:
                raise ValueError('A result review cannot recursively create another recommendation')
            if not scope['recommendation_allowance'] or any(r['status'] in {'proposed','deferred','adopted'} for r in self.rows()):
                raise ValueError('Recommendation allowance exhausted or a recommendation is still pending')
            refs = command['evidence_ids']
            if not refs or not set(refs).issubset(complete):
                raise ValueError('Recommendations require complete delivered original evidence')
            if not any(self.store.get(self.run_id,'fact',fid)['kind'] in {'raw_fact','source_statement'} for fid in refs):
                raise ValueError('Recommendation must have original source support')
            p = command['payload']
            cap = self.runtime.capability_catalog().get(p['capability_id'])
            if not cap or cap['version'] != p['capability_version']:
                raise ValueError('Recommendation targets an unregistered capability version')
            fingerprint = digest({'payload':p, 'evidence_ids':sorted(refs)})
            if any(r['fingerprint'] == fingerprint for r in self.rows()):
                raise ValueError('An unchanged recommendation cannot be repeated')
        expected = scope['recommendations'] if mode == 'lab' else scope['improvement_reviews']
        responses = response.get('recommendation_responses' if mode == 'lab' else 'improvement_reviews', [])
        ids = [r['recommendation_id'] for r in responses]
        if len(ids) != len(set(ids)) or set(ids) != {r['id'] for r in expected}:
            raise ValueError('Every delivered recommendation or result review needs exactly one explicit response')
        for item in responses:
            if not isinstance(item['reason'],str) or not item['reason'].strip() or len(item['reason']) > 12000:
                raise ValueError('A bounded nonempty response rationale is required')
            row = next(r for r in expected if r['id'] == item['recommendation_id'])
            current = self.store.get(self.run_id, 'registration', row['id'])
            if item['expected_version'] != row['version'] or current['version'] != row['version']:
                raise ValueError('Recommendation changed after packet admission')
            if not item['evidence_ids'] or not set(item['evidence_ids']).issubset(complete):
                raise ValueError('Recommendation response must cite actually delivered complete sources')
            if mode == 'lab' and item['decision'] == 'adopt':
                p, rec = command['payload'], row['recommendation']
                if action != 'request_diagnostic' or (p['capability_id'],p['capability_version']) != (rec['capability_id'],rec['capability_version']):
                    raise ValueError('Adoption must bind the same-call diagnostic to the recommended capability')
            if mode != 'lab':
                if row['status'] != 'executed' or row['measurement_fact_id'] not in item['evidence_ids']:
                    raise ValueError('Meta review must cite the executed result measurement')
                if action != 'no_action':
                    raise ValueError('Bounded improvement review ends with no_action; new work needs a new trigger')
        return {'recommendation_responses': responses if mode == 'lab' else [],
                'improvement_reviews': responses if mode != 'lab' else []}

    def recommend(self, proposal):
        command = proposal['command']
        rid = identity('recommendation', proposal['id'])
        fid = identity('recommendation_statement', rid)
        def apply(tx):
            tx.insert('fact', fid, {'kind':'source_statement', 'content_ref':tx.blob({
                'authority':'unverified_meta_recommendation', 'recommendation_id':rid,
                'recommendation':command['payload'], 'evidence_ids':command['evidence_ids']}),
                'provenance':{'source':'meta_recommendation','proposal_id':proposal['id'],
                              'effect_id':proposal['effect_id'], 'not_scientific_verification':True}})
            row = tx.insert('registration', rid, {'kind':'research_recommendation', 'status':'proposed',
                'source_proposal_id':proposal['id'], 'recommendation':command['payload'],
                'source_evidence_ids':command['evidence_ids'], 'recommendation_fact_id':fid,
                'defer_count':0, 'response_fact_ids':[], 'fingerprint':digest({
                    'payload':command['payload'],'evidence_ids':sorted(command['evidence_ids'])})})
            if not any(w['status'] in {'ready','leased'} for w in tx.find('work',kind='lab')):
                self.runtime.controller._work(tx,None,'lab','recommendation:'+rid,
                    inputs={'evidence_ids':command['evidence_ids'], 'required_evidence_ids':command['evidence_ids']})
            return row
        return self.runtime._mutate('route:'+proposal['id']+':recommendation','recommendation_proposed',apply,
                                    {'proposal_id':proposal['id']})

    def apply_responses(self, proposal, result):
        extension = proposal.get('research_response', {})
        responses = extension.get('recommendation_responses', [])
        reviews = extension.get('improvement_reviews', [])
        if not responses and not reviews:
            return
        def apply(tx):
            for item in responses:
                row = tx.get('registration',item['recommendation_id'])
                if row['version'] != item['expected_version']:
                    raise ValueError('Recommendation response became stale before its durable transition')
                decision = item['decision']
                fid = identity('lab_recommendation_response',proposal['id'],row['id'])
                tx.insert('fact',fid,{'kind':'source_statement','content_ref':tx.blob(item),
                    'provenance':{'source':'lab_explicit_recommendation_response','effect_id':proposal['effect_id'],
                                  'proposal_id':proposal['id'],'not_scientific_verification':True}})
                change = {'status':{'adopt':'adopted','decline':'declined','defer':'deferred'}[decision],
                          'lab_proposal_id':proposal['id'], 'lab_effect_id':proposal['effect_id'],
                          'response_fact_ids':row['response_fact_ids']+[fid]}
                if decision == 'adopt':
                    work = tx.get('work',result['id'])
                    if work['kind'] != 'diagnostic' or work['inputs'].get('source_proposal_id') != proposal['id']:
                        raise ValueError('Adoption has no exact same-call diagnostic work')
                    change['tool_work_id'] = work['id']
                elif decision == 'defer':
                    change['defer_count'] = row['defer_count']+1
                    final = tx.get('effect',proposal['effect_id'])['metadata'].get('final_review_only')
                    if change['defer_count'] >= 2 or final:
                        change['status'] = 'deferred_closed'
                    elif proposal['command']['action'] != 'request_diagnostic' and not any(w['status'] == 'ready' for w in tx.find('work',kind='lab')):
                        self.runtime.controller._work(tx,None,'lab','recommendation-reconsider:'+row['id'],
                            inputs={'evidence_ids':row['source_evidence_ids']})
                tx.update('registration',row['id'],change)
            for item in reviews:
                row = tx.get('registration',item['recommendation_id'])
                if row['version'] != item['expected_version']:
                    raise ValueError('Recommendation review became stale before its durable transition')
                fid = identity('improvement_review',row['id'])
                tx.insert('fact',fid,{'kind':'hypothesis','content_ref':tx.blob(item),
                    'provenance':{'source':'meta_result_review','effect_id':proposal['effect_id'],
                                  'scientific_status':'unverified','causal_benefit':'not_established'}})
                tx.update('registration',row['id'],{'status':'reviewed','review_proposal_id':proposal['id'],
                    'review_fact_id':fid,'review':item,'causal_benefit':'not_established'})
        self.runtime._mutate('route:'+proposal['id']+':research-responses','research_responses_applied',apply,
                             {'proposal_id':proposal['id']})

    def publication(self, effect, publication):
        rows = [r for r in self.rows() if r.get('tool_work_id') == effect['work_id']]
        for row in rows:
            if row.get('measurement_fact_id'):
                recorded = row['measurement']
                if (row['executed_effect_id'] != effect['id']
                        or recorded['publication_sha256'] != digest(publication)
                        or recorded['result_ref'] != effect['result_ref']
                        or recorded['input_packet_sha256'] != effect['packet_hash']):
                    raise ValueError('Existing measurement belongs to a different execution publication')
                # The measurement and its review Work were committed together.
                # Recovery reuses that result without reinterpreting the domain.
                continue
            measure = {'execution_status':effect['status'], 'executed_effect_id':effect['id'],
                       'input_packet_sha256':effect['packet_hash'], 'result_ref':effect['result_ref'],
                       'publication_sha256':digest(publication), 'causal_benefit':'not_established'}
            if effect['status'] == 'succeeded':
                adapter = self.runtime.adapters.resolve(effect)
                if adapter.measure is not None:
                    # Source-pinned domain code owns interpretation, metric
                    # names and optimization directions. Never merge its fields
                    # into the controller-owned execution/provenance namespace.
                    metrics = adapter.measure(json.loads(encode(effect)), json.loads(encode(publication)))
                    if not isinstance(metrics, dict):
                        raise ValueError('Adapter measurements must be a JSON object')
                    raw = encode(metrics)
                    if len(raw) > 65536:
                        raise ValueError('Adapter measurements exceed the bounded result size')
                    measure['domain_metrics'] = json.loads(raw)
            fid = identity('improvement_measurement',row['id'],effect['id'])
            result_ids = [r['id'] for r in publication['facts']]
            def apply(tx):
                current = tx.get('registration',row['id'])
                if current['status'] != 'adopted':
                    raise ValueError('Only an adopted recommendation can acquire its first execution result')
                tx.insert('fact',fid,{'kind':'raw_fact','content_ref':tx.blob(measure),
                    'provenance':{'source':'controller_execution_measurement','effect_id':effect['id'],
                                  'recommendation_id':row['id'],'not_causal_evidence':True}})
                tx.update('registration',row['id'],{'status':'executed','executed_effect_id':effect['id'],
                    'measurement':measure,'measurement_fact_id':fid,'result_evidence_ids':result_ids})
                required = list(dict.fromkeys(row['source_evidence_ids']+[row['recommendation_fact_id']]
                    +row['response_fact_ids']+result_ids+[fid]))
                work = self.runtime.controller._work(tx,None,'meta_discovery','improvement-result:'+row['id'],
                    inputs={'evidence_ids':required,'required_evidence_ids':required,
                            'improvement_review_ids':[row['id']], 'trigger':{'kind':'executed_recommendation'}}, priority=0)
                return work
            self.runtime._mutate('improvement-measure:'+row['id']+':'+effect['id'],
                'improvement_execution_measured',apply,{'recommendation_id':row['id'],'effect_id':effect['id']})

    def failed_execution(self, effect):
        if effect['status'] != 'failed_definite' or effect.get('accounting_unknown'):
            return
        if not any(r.get('tool_work_id') == effect['work_id'] for r in self.rows()):
            return
        fid = identity('improvement_execution_failure',effect['id'])
        content = json.loads(self.store.read_blob(effect['result_ref']))
        self.runtime._mutate('improvement-failure:'+effect['id'],'improvement_failure_published',
            lambda tx:tx.insert('fact',fid,{'kind':'raw_fact','content_ref':tx.blob(content),
                'provenance':{'source':'certified_adapter_failure','effect_id':effect['id']}}),
            {'effect_id':effect['id'],'result_ref':effect['result_ref']})
        self.publication(effect,{'facts':[{'id':fid}], 'conclusions':[], 'data':{'failure':content}})
