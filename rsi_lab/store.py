"""Transactional projections, append-only events and immutable content-addressed blobs."""
from __future__ import annotations

from contextlib import contextmanager

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time

KINDS = ('issue', 'work', 'wait', 'request', 'delivery', 'effect', 'trial', 'capability',
         'study', 'shard', 'version', 'budget', 'fact', 'proposal', 'dependency',
         'question', 'measurement', 'registration', 'agent_owner', 'agent_session',
         'agent_turn', 'working_memory', 'agent_checkpoint', 'agent_handoff')
META = {'id', 'run_id', 'version', 'created_seq', 'updated_seq'}


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else encode(value)).hexdigest()


def table(kind):
    if kind not in KINDS:
        raise ValueError(f'Unsupported entity kind: {kind}')
    return 'v2_' + kind


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.blobs = self.root / 'objects'
        self.blobs.mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.root / 'ledger.sqlite3', timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        schema = self.db.execute('PRAGMA user_version').fetchone()[0]
        if schema not in (0, 2):
            raise ValueError('Only the independent v2 ledger schema is supported')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, config TEXT NOT NULL, config_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
              run_id TEXT NOT NULL REFERENCES runs(id), seq INTEGER NOT NULL, operation_key TEXT NOT NULL,
              event_type TEXT NOT NULL, input_hash TEXT NOT NULL, payload_ref TEXT NOT NULL,
              result_ref TEXT NOT NULL, changes_ref TEXT NOT NULL, previous_hash TEXT NOT NULL,
              event_hash TEXT NOT NULL, created_at REAL NOT NULL,
              PRIMARY KEY(run_id,seq), UNIQUE(run_id,operation_key));
            CREATE TRIGGER IF NOT EXISTS immutable_events_update BEFORE UPDATE ON events
              BEGIN SELECT RAISE(ABORT,'Events are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_events_delete BEFORE DELETE ON events
              BEGIN SELECT RAISE(ABORT,'Events are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_runs_update BEFORE UPDATE ON runs
              BEGIN SELECT RAISE(ABORT,'Run configuration is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_runs_delete BEFORE DELETE ON runs
              BEGIN SELECT RAISE(ABORT,'Runs are immutable'); END;
        ''')
        for kind in KINDS:
            self.db.execute(f'CREATE TABLE IF NOT EXISTS {table(kind)} (run_id TEXT NOT NULL REFERENCES runs(id), id TEXT NOT NULL, version INTEGER NOT NULL, document TEXT NOT NULL, PRIMARY KEY(run_id,id))')
        self.db.execute('PRAGMA user_version=2')

    def close(self):
        self.db.close()

    def create_run(self, run_id, config):
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError('Run identity required')
        config = json.loads(encode(config))
        self.db.execute('INSERT INTO runs VALUES(?,?,?)', (run_id, encode(config).decode(), digest(config)))
        return {'id': run_id, 'config': config, 'config_hash': digest(config)}

    def run(self, run_id):
        row = self.db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        config = json.loads(row['config'])
        if digest(config) != row['config_hash']:
            raise ValueError('Run configuration changed')
        return {'id': run_id, 'config': config, 'config_hash': row['config_hash']}

    def put_blob(self, value):
        raw = value if isinstance(value, bytes) else encode(value)
        key = digest(raw)
        path = self.blobs / key
        try:
            if path.read_bytes() == raw:
                return key
        except FileNotFoundError:
            pass
        # Any other bytes under this name are a torn remnant of an interrupted
        # write: the name is the hash of the correct content, so republish it.
        # The temporary name is never 64 hex characters, so it is never a blob.
        fd, temporary = tempfile.mkstemp(prefix='.tmp-', dir=self.blobs)
        published = False
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o444)
            os.replace(temporary, path)
            published = True
            directory = os.open(self.blobs, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if not published:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        return key

    def read_blob(self, key):
        if not isinstance(key, str) or len(key) != 64 or any(x not in '0123456789abcdef' for x in key):
            raise ValueError('Invalid content hash')
        raw = (self.blobs / key).read_bytes()
        if digest(raw) != key:
            raise ValueError('Blob content changed')
        return raw

    def get(self, run_id, kind, entity_id):
        row = self.db.execute(f'SELECT document FROM {table(kind)} WHERE run_id=? AND id=?', (run_id, entity_id)).fetchone()
        if row is None:
            raise KeyError((kind, entity_id))
        return json.loads(row[0])

    def list(self, run_id, entity_kind, **filters):
        # Scientific-memory revisions/indexes make repeated exact-id lookups common.
        # Use the existing primary key instead of decoding the entire projection table.
        if isinstance(filters.get('id'), str):
            query = f'SELECT document FROM {table(entity_kind)} WHERE run_id=? AND id=? ORDER BY id'
            parameters = (run_id, filters['id'])
        else:
            query = f'SELECT document FROM {table(entity_kind)} WHERE run_id=? ORDER BY id'
            parameters = (run_id,)
        rows = [json.loads(row[0]) for row in self.db.execute(query, parameters)]
        return [row for row in rows if all(row.get(k) == v for k, v in filters.items())]

    @contextmanager
    def read_snapshot(self):
        """Bind a compound read to one SQLite snapshot, without inventing writes."""
        owned = not self.db.in_transaction
        if owned:
            self.db.execute('BEGIN')
        try:
            yield
        finally:
            if owned:
                self.db.execute('ROLLBACK')

    @contextmanager
    def guard_mutations(self, run_id, guard):
        """Check a trusted precondition inside each new write transaction.

        A guard may return a finalizer to persist its acceptance in the same
        transaction after the operation succeeds. Stable-key replays bypass
        guards: they return a committed result, never authorize new work.
        """
        guards = getattr(self, '_mutation_guards', ())
        self._mutation_guards = guards + ((run_id, guard),)
        try:
            yield
        finally:
            self._mutation_guards = guards

    def mutate(self, run_id, key, event_type, fn, payload=None, *, run_config=None):
        """Commit event, all projection changes and outbox intent in one transaction.

        With run_config, the run row is created in the same transaction when it
        does not exist yet, and must otherwise carry exactly that configuration.
        """
        if not isinstance(key, str) or not key or not isinstance(event_type, str) or not event_type:
            raise ValueError('An operation key and event type are required')
        # Callbacks must not be able to mutate the input identity after it was
        # fingerprinted, including through a captured nested input object.
        payload = json.loads(encode({} if payload is None else payload))
        fingerprint = digest({'event_type': event_type, 'payload': payload})
        if run_config is not None:
            run_config = json.loads(encode(run_config))
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if run_config is not None:
                if self.db.execute('SELECT 1 FROM runs WHERE id=?', (run_id,)).fetchone() is None:
                    self.create_run(run_id, run_config)
                elif self.run(run_id)['config_hash'] != digest(run_config):
                    raise ValueError('Run already exists with a different configuration')
            config = self.run(run_id)
            previous = self.db.execute('SELECT * FROM events WHERE run_id=? AND operation_key=?', (run_id, key)).fetchone()
            if previous is not None:
                if previous['input_hash'] != fingerprint:
                    raise ValueError('Idempotency key reused for different input')
                result = json.loads(self.read_blob(previous['result_ref']))
                self.db.execute('COMMIT')
                return result
            last = self.db.execute('SELECT seq,event_hash FROM events WHERE run_id=? ORDER BY seq DESC LIMIT 1', (run_id,)).fetchone()
            seq, previous_hash = (last['seq'] + 1, last['event_hash']) if last else (1, config['config_hash'])
            tx = Transaction(self, run_id, seq)
            finalizers = [guard(tx, key, event_type)
                          for guarded_run, guard in getattr(self, '_mutation_guards', ())
                          if guarded_run == run_id]
            result = fn(tx)
            for finalize in finalizers:
                if finalize is not None:
                    finalize(tx)
            payload_ref, result_ref, changes_ref = map(self.put_blob, (payload, result, tx.changes))
            body = {'run_id': run_id, 'seq': seq, 'operation_key': key, 'event_type': event_type,
                    'input_hash': fingerprint, 'payload_ref': payload_ref, 'result_ref': result_ref,
                    'changes_ref': changes_ref, 'previous_hash': previous_hash, 'created_at': time.time()}
            self.db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (*[body[k] for k in ('run_id', 'seq', 'operation_key', 'event_type', 'input_hash',
                                    'payload_ref', 'result_ref', 'changes_ref', 'previous_hash')], digest(body), body['created_at']))
            self.db.execute('COMMIT')
            return json.loads(encode(result))
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def verify_history(self, run_id):
        previous_hash = self.run(run_id)['config_hash']
        projections, count = {}, 0
        for raw in self.db.execute('SELECT * FROM events WHERE run_id=? ORDER BY seq', (run_id,)):
            event = dict(raw)
            event_hash = event.pop('event_hash')
            count += 1
            if event['seq'] != count or event['previous_hash'] != previous_hash or digest(event) != event_hash:
                raise ValueError('Event sequence/hash chain changed')
            payload = json.loads(self.read_blob(event['payload_ref']))
            if digest({'event_type': event['event_type'], 'payload': payload}) != event['input_hash']:
                raise ValueError('Event input identity changed')
            self.read_blob(event['result_ref'])
            for change in json.loads(self.read_blob(event['changes_ref'])):
                projections[(change['kind'], change['document']['id'])] = change['document']
            previous_hash = event_hash
        actual = {(kind, row['id']): row for kind in KINDS for row in self.list(run_id, kind)}
        if actual != projections:
            raise ValueError('SQLite projections differ from the immutable event history')
        return {'events': count, 'last_event_hash': previous_hash, 'entities': len(projections)}


class Transaction:
    def __init__(self, store, run_id, seq):
        self.store, self.run_id, self.seq, self.changes = store, run_id, seq, []

    def get(self, kind, entity_id):
        return self.store.get(self.run_id, kind, entity_id)

    def find(self, entity_kind, **filters):
        return self.store.list(self.run_id, entity_kind, **filters)

    def blob(self, value):
        return self.store.put_blob(value)

    def insert(self, kind, entity_id, document):
        if META.intersection(document) or not isinstance(entity_id, str) or not entity_id:
            raise ValueError('Entity metadata is owned by the ledger')
        value = {**document, 'id': entity_id, 'run_id': self.run_id, 'version': 1,
                 'created_seq': self.seq, 'updated_seq': self.seq}
        raw = encode(value).decode()
        self.store.db.execute(f'INSERT INTO {table(kind)} VALUES(?,?,?,?)', (self.run_id, entity_id, 1, raw))
        # Snapshot the exact bytes written to SQLite rather than keeping an
        # alias of the mutable object returned to the caller.
        self.changes.append({'kind': kind, 'document': json.loads(raw)})
        return value

    def update(self, kind, entity_id, patch, expected_version=None):
        if kind == 'fact':
            raise ValueError('Recorded facts are immutable; append new evidence with a new identity')
        if kind in {'working_memory', 'agent_checkpoint'}:
            raise ValueError('Agent revisions and checkpoints are immutable; append a new version')
        if META.intersection(patch):
            raise ValueError('Entity metadata is owned by the ledger')
        old = self.get(kind, entity_id)
        if expected_version is not None and expected_version != old['version']:
            raise ValueError('Stale entity version')
        value = {**old, **patch, 'version': old['version'] + 1, 'updated_seq': self.seq}
        raw = encode(value).decode()
        changed = self.store.db.execute(f'UPDATE {table(kind)} SET version=?,document=? WHERE run_id=? AND id=? AND version=?',
            (value['version'], raw, self.run_id, entity_id, old['version'])).rowcount
        if changed != 1:
            raise ValueError('Concurrent entity change')
        self.changes.append({'kind': kind, 'document': json.loads(raw)})
        return value
