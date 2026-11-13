"""Fault probes for domain-neutral adapter dispatch and publication boundaries."""
import ast
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab.adapters import Adapter, AdapterRegistry
from rsi_lab.controller import Controller, identity
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store


EXECUTIONS = []


def prepare(inputs):
    if not isinstance(inputs,dict) or set(inputs)!={'value'}:
        raise ValueError('Exact numeric fixture input required')
    return {'value':inputs['value']}


def reservations(packet):
    return {'tool_calls':1}


def execute(effect,raw):
    EXECUTIONS.append(effect['id'])
    return {'status':'succeeded','actual_costs':{'tool_calls':1},
            'result':{'value':json.loads(raw)['value']}}


def execute_alternative(effect,raw):
    EXECUTIONS.append('different:'+effect['id'])
    return {'status':'succeeded','actual_costs':{'tool_calls':1},
            'result':{'value':-json.loads(raw)['value']}}


def publication(effect,receipt):
    return {'facts':[{'id':identity('fixture_fact',effect['id']),'kind':'raw_fact',
                     'content':receipt['result'],'provenance':{'effect_id':effect['id']}}],
            'conclusions':[],'data':receipt['result']}


def publication_alternative(effect,receipt):
    return {'facts':[{'id':identity('fixture_fact',effect['id']),'kind':'raw_fact',
                     'content':{'unbound':True},'provenance':{'effect_id':effect['id']}}],
            'conclusions':[],'data':{'unbound':True}}


def fixture_adapter(*,version='1',adapter_id='numeric.fixture',effect_kind='numeric_fixture',source_paths=()):
    return Adapter(adapter_id=adapter_id,version=version,effect_kind=effect_kind,
        manifest={'scope':'Numerical software fixture','input_contract':'one value','output_contract':'same value'},
        prepare=prepare,reservations=reservations,execute=execute,publication=publication,source_paths=source_paths)


class AdapterBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.store=Store(self.root/'ledger');self.addCleanup(self.store.close)
        self.controller=Controller(self.store)
        self.controller.initialize('r',budgets={'tool_calls':{'limit':10,'hard':True}},
                                   config={'open_review_interval_batches':1})
        self.adapter=fixture_adapter()
        self.runtime=Runtime(self.store,'r',adapters=[self.adapter])
        EXECUTIONS.clear()

    def interrupted_publication(self, runtime=None):
        runtime=runtime or self.runtime
        with patch.object(runtime,'publish_adapter_effect',side_effect=KeyboardInterrupt('crash after receipt persisted')):
            with self.assertRaises(KeyboardInterrupt):
                runtime.execute_adapter(self.adapter.adapter_id,{'value':7},trigger_id='input1')
        self.assertEqual(len(EXECUTIONS),1)
        self.assertEqual(self.store.list('r','fact'),[])
        return self.store.list('r','effect')[0]

    def test_restart_publishes_committed_result_without_reexecuting_adapter(self):
        effect=self.interrupted_publication()
        resumed=Runtime(self.store,'r',adapters=[self.adapter])
        result=resumed.recover()
        self.assertEqual(result['adapter_results'],[{'effect_id':effect['id'],'status':'published'}])
        resumed.recover()
        self.assertEqual(len(EXECUTIONS),1)
        self.assertEqual(len(self.store.list('r','fact')),1)
        self.assertEqual(self.store.get('r','budget','tool_calls')['spent'],1)
        self.store.verify_history('r')

    def test_missing_plugin_blocks_recovery_publication_and_later_load_resumes(self):
        effect=self.interrupted_publication()
        blocked=Runtime(self.store,'r').recover()
        self.assertEqual(blocked['adapter_results'][0]['status'],'blocked_adapter')
        self.assertEqual(self.store.list('r','fact'),[])
        self.assertEqual(len(EXECUTIONS),1)
        resumed=Runtime(self.store,'r',adapters=[self.adapter]).recover()
        self.assertEqual(resumed['adapter_results'][0]['effect_id'],effect['id'])
        self.assertEqual(resumed['adapter_results'][0]['status'],'published')

    def test_cooperative_driver_reports_unpublished_missing_plugin_obligation(self):
        self.interrupted_publication()
        missing=Runtime(self.store,'r')
        def model_must_not_run(effect,packet):
            raise AssertionError('No Meta work should be invented to replace the missing executable')
        result=missing.run_until_wait(model_must_not_run,{'tool_calls':1},max_steps=1)
        self.assertNotEqual(result['status'],'quiescent')
        self.assertTrue(any(row['status']=='blocked_adapter' for row in missing.recover()['adapter_results']))
        self.assertEqual(len(EXECUTIONS),1)

    def test_completed_publication_does_not_require_permanent_plugin_installation(self):
        completed=self.runtime.execute_adapter(self.adapter.adapter_id,{'value':7},trigger_id='complete')
        self.assertEqual(completed['result'],{'value':7})
        for adapters in ([],[replace(self.adapter,version='2')]):
            with self.subTest(adapters=adapters):
                result=Runtime(self.store,'r',adapters=adapters).recover()
                self.assertFalse(any(row['status']=='blocked_adapter' for row in result['adapter_results']))
                self.assertEqual(len(self.store.list('r','fact')),1)
                self.assertEqual(len(EXECUTIONS),1)

    def test_committed_evidence_finishes_notifications_after_plugin_uninstall(self):
        with patch.object(self.runtime,'observe',side_effect=KeyboardInterrupt('crash before notification')):
            with self.assertRaises(KeyboardInterrupt):
                self.runtime.execute_adapter(self.adapter.adapter_id,{'value':7},trigger_id='notification-crash')
        effect=self.store.list('r','effect')[0]
        publication_id=identity('publication',effect['id'])
        self.assertEqual(self.store.get('r','registration',publication_id)['status'],'published')
        self.assertEqual(len(self.store.list('r','fact')),1)
        resumed=Runtime(self.store,'r')
        self.assertEqual(resumed.pending_adapter_publications()[0]['status'],'notification_pending')
        result=resumed.recover()
        self.assertEqual(result['adapter_results'],[{'effect_id':effect['id'],'status':'published'}])
        self.assertEqual(self.store.get('r','registration',publication_id)['status'],'complete')
        self.assertEqual(resumed.pending_adapter_publications(),[])
        discoveries=self.store.list('r','work',kind='meta_discovery')
        self.assertEqual(len(discoveries),1)
        resumed.recover()
        self.assertEqual(self.store.list('r','work',kind='meta_discovery'),discoveries)
        self.assertEqual(len(self.store.list('r','fact')),1)
        self.assertEqual(len(EXECUTIONS),1)
        self.assertEqual(self.store.get('r','budget','tool_calls')['spent'],1)
        self.store.verify_history('r')

    def test_adapter_requiring_acceptance_cannot_dispatch_before_registration(self):
        adapter=replace(self.adapter,requires_acceptance=True)
        runtime=Runtime(self.store,'r',adapters=[adapter])
        self.assertNotIn(adapter.adapter_id,runtime.capability_catalog())
        with self.assertRaisesRegex(ValueError,'verified artifact acceptance'):
            runtime.execute_adapter(adapter.adapter_id,{'value':7},trigger_id='unaccepted')
        self.assertEqual(self.store.list('r','effect'),[])
        self.assertEqual(EXECUTIONS,[])
        self.assertEqual(self.store.get('r','budget','tool_calls')['reserved'],0)

    def test_accepted_identity_requires_exact_artifact_hash_and_version(self):
        # The acceptance executor has separate integration coverage. Seed its
        # persisted projection here to isolate the runtime admission boundary.
        artifact_ref=self.store.put_blob(b'accepted numeric fixture artifact')
        self.store.mutate('r','fixture-accepted','fixture_capability_accepted',lambda tx:tx.insert(
            'capability','accepted-numeric',{'status':'verified','capability_id':self.adapter.adapter_id,
            'capability_version':'1','artifact_ref':artifact_ref}),payload={'artifact_ref':artifact_ref})
        accepted=replace(self.adapter,requires_acceptance=True,
                         manifest={**self.adapter.manifest,'artifact_sha256':artifact_ref})
        for changed in (replace(accepted,version='2'),
                        replace(accepted,manifest={**accepted.manifest,'artifact_sha256':'0'*64})):
            with self.subTest(version=changed.version,manifest=changed.manifest):
                runtime=Runtime(self.store,'r',adapters=[changed])
                with self.assertRaisesRegex(ValueError,'accepted artifact version or identity'):
                    runtime.execute_adapter(changed.adapter_id,{'value':7},trigger_id='wrong-artifact')
                self.assertEqual(self.store.list('r','effect'),[])
                self.assertEqual(EXECUTIONS,[])
        runtime=Runtime(self.store,'r',adapters=[accepted])
        self.assertEqual(runtime.capability_catalog()[accepted.adapter_id]['admission'],'verified_artifact_adapter')
        result=runtime.execute_adapter(accepted.adapter_id,{'value':7},trigger_id='accepted')
        self.assertEqual(result['result'],{'value':7})
        self.assertEqual(len(EXECUTIONS),1)

    def test_changed_version_or_dependency_source_blocks_publication(self):
        marker=self.root/'adapter_dependency.txt';marker.write_text('version-one')
        self.adapter=fixture_adapter(source_paths=(marker,))
        self.runtime=Runtime(self.store,'r',adapters=[self.adapter])
        self.interrupted_publication()
        newer=replace(self.adapter,version='2')
        self.assertEqual(Runtime(self.store,'r',adapters=[newer]).recover()['adapter_results'][0]['status'],'blocked_adapter')
        marker.write_text('version-two')
        self.assertEqual(Runtime(self.store,'r',adapters=[self.adapter]).recover()['adapter_results'][0]['status'],'blocked_adapter')
        self.assertEqual(self.store.list('r','fact'),[])
        self.assertEqual(len(EXECUTIONS),1)

    def test_switching_callback_within_same_file_changes_frozen_implementation(self):
        self.interrupted_publication()
        for modified in (replace(self.adapter,execute=execute_alternative),
                         replace(self.adapter,publication=publication_alternative)):
            with self.subTest(callback=modified):
                self.assertNotEqual(modified.descriptor(),self.adapter.descriptor())
                result=Runtime(self.store,'r',adapters=[modified]).recover()
                self.assertEqual(result['adapter_results'][0]['status'],'blocked_adapter')
        self.assertEqual(self.store.list('r','fact'),[])

    def test_publication_rejects_a_forged_effect_or_unreconciled_result(self):
        effect=self.interrupted_publication()
        forged={**effect,'result_ref':self.store.put_blob({'status':'succeeded','actual_costs':{'tool_calls':1},
                                                       'result':{'value':'forged'}})}
        with self.assertRaises(ValueError):self.runtime.publish_adapter_effect(forged)
        self.assertEqual(self.store.list('r','fact'),[])
        self.store.mutate('r','fixture-unknown','fixture_external_unknown',lambda tx:tx.update('effect',effect['id'],
            {'status':'unknown','accounting_unknown':True}),payload={'effect_id':effect['id']})
        unknown=self.store.get('r','effect',effect['id'])
        with self.assertRaises(ValueError):self.runtime.publish_adapter_effect(unknown)
        self.assertEqual(self.store.list('r','fact'),[])

    def test_unknown_invocation_is_not_retried_by_execute_or_recover(self):
        def unavailable(effect,raw):
            EXECUTIONS.append(effect['id'])
            return {'status':'unknown','actual_costs':{},'reason':'provider outcome unavailable'}
        adapter=replace(self.adapter,execute=unavailable)
        runtime=Runtime(self.store,'r',adapters=[adapter])
        first=runtime.execute_adapter(adapter.adapter_id,{'value':7},trigger_id='unknown-input')
        self.assertEqual(first['effect']['status'],'unknown')
        self.assertIsNone(first['result'])
        runtime.execute_adapter(adapter.adapter_id,{'value':7},trigger_id='unknown-input')
        runtime.recover()
        self.assertEqual(len(EXECUTIONS),1)
        self.assertEqual(self.store.list('r','fact'),[])
        self.assertEqual(self.store.get('r','budget','tool_calls')['reserved'],1)

    def test_registries_are_per_runtime_and_do_not_mutate_global_dispatch(self):
        second=fixture_adapter(adapter_id='text.fixture',effect_kind='text_fixture')
        other=Runtime(self.store,'r',adapters=[second])
        self.assertEqual(set(self.runtime.capability_catalog()),{'numeric.fixture'})
        self.assertEqual(set(other.capability_catalog()),{'text.fixture'})
        with self.assertRaises(ValueError):other.execute_adapter('numeric.fixture',{'value':3},trigger_id='wrong-registry')
        self.assertEqual(self.store.list('r','effect'),[])

    def test_plugin_cannot_claim_controller_owned_legacy_effects(self):
        for reserved in ('meta','capability_acceptance'):
            with self.subTest(reserved=reserved),self.assertRaises(ValueError):
                AdapterRegistry([replace(self.adapter,legacy_effect_kinds=(reserved,))])

    def test_core_runtime_and_adapter_registry_import_no_domain_package(self):
        root=Path(__file__).resolve().parents[2]/'rsi_lab'
        for path in root.rglob('*.py'):
            tree=ast.parse(path.read_text())
            for node in ast.walk(tree):
                modules=([node.module or ''] if isinstance(node,ast.ImportFrom)
                         else [a.name for a in node.names] if isinstance(node,ast.Import) else [])
                for name in modules:
                    self.assertFalse(name.startswith(('rsi_domains','tests','scripts')),(path.name,name))
        self.assertIsNotNone(AdapterRegistry([self.adapter]).get('numeric.fixture'))


if __name__=='__main__':unittest.main()
