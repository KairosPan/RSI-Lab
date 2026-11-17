"""Generic claim lifecycle, atomic exclusion, and receipt-backed coverage."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from rsi_lab.controller import Controller
from rsi_lab.store import Store
from rsi_lab import work_claims as claims


class WorkClaimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'run'
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        Controller(self.store).initialize('r', budgets={})
        self.serial = 0

    def mutate(self, callback):
        self.serial += 1
        return self.store.mutate('r', 'test-' + str(self.serial), 'fixture', callback)

    @staticmethod
    def enqueue(tx, owner, values, namespace='adapter'):
        result = claims.acquire(tx, namespace, owner, values)
        if result['status'] == 'acquired' and not tx.find('work', id=owner):
            tx.insert('work', owner, {'kind': 'diagnostic', 'status': 'ready',
                      'inputs': {}, 'effect_id': None, 'priority': 10})
        return result

    def queue(self, owner, values, namespace='adapter'):
        return self.mutate(lambda tx: self.enqueue(tx, owner, values, namespace))

    def claim_rows(self):
        return self.store.list('r', 'registration', kind=claims.KIND)

    def effect(self, owner, status, *, unknown_cost=False):
        eid = 'effect-' + owner
        def apply(tx):
            document = {'kind': 'tool', 'work_id': owner, 'status': status,
                        'accounting_unknown': unknown_cost, 'result_ref': tx.blob({'status': status})}
            if tx.find('effect', id=eid):
                tx.update('effect', eid, document)
            else:
                tx.insert('effect', eid, document)
            tx.update('work', owner, {'status': 'succeeded' if status == 'succeeded' else 'leased', 'effect_id': eid})
            return claims.reconcile_all(tx)
        return self.mutate(apply)

    def publication(self, owner, covered, status='complete'):
        def apply(tx):
            pid = 'publication-' + owner
            document = {'kind': 'adapter_publication', 'status': status,
                        'effect_id': 'effect-' + owner, 'publication_ref': tx.blob({'covered': covered}),
                        'covered_claim_identities': covered}
            if tx.find('registration', id=pid):
                tx.update('registration', pid, document)
            else:
                tx.insert('registration', pid, document)
            return claims.reconcile_all(tx)
        return self.mutate(apply)

    def test_pending_overlap_is_all_or_none_and_replay_is_idempotent(self):
        first = self.queue('first', ['b', 'a', 'a'])
        before = self.claim_rows()
        replay = self.queue('first', ['a', 'b'])
        self.assertEqual(first, replay)
        self.assertEqual(before, self.claim_rows())
        second = self.queue('second', ['b', 'c'])
        self.assertEqual(second['status'], 'waiting_existing')
        self.assertEqual(second['owner_work_ids'], ['first'])
        self.assertEqual(second['uncovered_identities'], ['c'])
        self.assertFalse(self.store.list('r', 'work', id='second'))
        self.assertEqual(before, self.claim_rows())

    def test_enqueue_and_claims_roll_back_together(self):
        def crash(tx):
            self.enqueue(tx, 'orphan', ['a'])
            raise RuntimeError('after outbox insertion')
        with self.assertRaises(RuntimeError):
            self.mutate(crash)
        self.assertEqual(self.claim_rows(), [])
        self.assertEqual(self.store.list('r', 'work'), [])

    def test_two_writers_cannot_own_one_identity(self):
        def writer(owner):
            store = Store(self.root)
            try:
                return store.mutate('r', owner, 'concurrent_enqueue',
                                    lambda tx: self.enqueue(tx, owner, ['shared']))
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(writer, ['writer-a', 'writer-b']))
        self.assertEqual(sorted(r['status'] for r in results), ['acquired', 'waiting_existing'])
        self.assertEqual(len(self.claim_rows()), 1)
        self.assertEqual(len(self.store.list('r', 'work')), 1)
        self.store.verify_history('r')

    def test_prepared_dispatching_unknown_and_unknown_accounting_hold(self):
        self.queue('owner', ['a'])
        for status, unknown in [('prepared', False), ('dispatching', False), ('unknown', False),
                                ('failed_definite', True), ('succeeded', True)]:
            with self.subTest(status=status, unknown=unknown):
                self.effect('owner', status, unknown_cost=unknown)
                self.assertEqual(self.queue('competitor', ['a'])['status'], 'waiting_existing')
                self.assertEqual(self.claim_rows()[0]['status'], 'held')

    def test_failure_and_cancellation_release_for_new_work(self):
        self.queue('failed', ['a'])
        self.effect('failed', 'failed_definite')
        self.assertEqual(self.claim_rows()[0]['status'], 'released')
        second = self.queue('retry', ['a'])
        self.assertEqual(second['status'], 'acquired')
        self.assertEqual(self.claim_rows()[0]['generation'], 2)
        self.mutate(lambda tx: (tx.update('work', 'retry', {'status': 'cancelled'}), claims.reconcile_all(tx)))
        self.assertEqual(self.claim_rows()[0]['status'], 'released')
        self.assertEqual(self.queue('after-cancel', ['a'])['status'], 'acquired')

    def test_complete_publication_covers_only_explicit_actual_subset(self):
        self.queue('owner', ['scored', 'clipped'])
        self.effect('owner', 'succeeded')
        self.assertTrue(all(r['status'] == 'held' for r in self.claim_rows()))
        self.publication('owner', ['scored'], status='published')
        self.assertTrue(all(r['status'] == 'held' for r in self.claim_rows()))
        self.publication('owner', ['scored'])
        statuses = {r['identity']: r['status'] for r in self.claim_rows()}
        self.assertEqual(statuses, {'scored': 'covered', 'clipped': 'released'})
        covered = self.queue('later', ['scored', 'new'])
        self.assertEqual(covered['status'], 'covered')
        self.assertEqual(covered['uncovered_identities'], ['new'])
        self.assertEqual(covered['owner_work_ids'], ['owner'])
        self.assertTrue(covered['conflicts'][0]['publication_ref'])
        self.assertFalse(self.store.list('r', 'work', id='later'))
        self.assertEqual(self.queue('clip-retry', ['clipped'])['status'], 'acquired')

    def test_invalid_coverage_rolls_back_and_does_not_release_claim(self):
        self.queue('owner', ['a'])
        self.effect('owner', 'succeeded')
        with self.assertRaisesRegex(ValueError, 'not owned'):
            self.publication('owner', ['not-owned'])
        self.assertEqual(self.claim_rows()[0]['status'], 'held')
        self.assertFalse(self.store.list('r', 'registration', id='publication-owner'))

    def test_namespace_isolation_and_context_bound_do_not_weaken_checks(self):
        self.queue('a', ['same'], 'adapter-a')
        self.assertEqual(self.queue('b', ['same'], 'adapter-b')['status'], 'acquired')
        context = claims.snapshot(self.store, 'r', max_bytes=1)
        self.assertFalse(context['complete'])
        self.assertEqual(context['omitted_claims'], 2)
        self.assertEqual(self.queue('other', ['same'], 'adapter-a')['status'], 'waiting_existing')

    def test_bounds_reject_without_writes_and_empty_claims_are_supported(self):
        for values in ([1], [''], ['x' * 4097], ['x'] * 1001, ['x' * 4000] * 20, 'not-a-list'):
            with self.subTest(values_type=type(values).__name__), self.assertRaises(ValueError):
                self.queue('invalid', values)
        self.assertEqual(self.claim_rows(), [])
        self.assertEqual(self.queue('no-claims', [])['status'], 'acquired')
        self.assertEqual(self.claim_rows(), [])
