"""Durable, append-only capture of observable execution events.

The journal is evidence, not an execution ledger. ``durable_seq`` acknowledges
only frames whose write and fsync completed; applying events to projections is
owned by the controller and uses a separate cursor. No model can acknowledge a
journal record or replace a recorded frame.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import time
import tempfile

from .store import digest, encode


class JournalIntegrityError(ValueError):
    pass


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class JournalWriter:
    """Exclusive writer; every append is durable before it is returned."""
    def __init__(self, path, metadata=None):
        self.path = Path(path).resolve()
        if Path(path).is_symlink():
            raise ValueError('Journal must not be a symlink')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.frontier_path = self.path.with_name(self.path.name + '.frontier.json')
        if self.frontier_path.exists():
            raise FileExistsError('Existing journal frontier cannot be replaced')
        self._stream = self.path.open('xb', buffering=0)
        self.durable_seq = 0
        self.head_hash = '0' * 64
        self._provider_events = {}
        self.failed = False
        try:
            _sync_directory(self.path.parent)
            self.append('control', encode(metadata or {}), kind='attempt_started')
        except BaseException:
            self._stream.close()
            raise

    def append(self, stream, raw, *, kind='stream_chunk', provider_event_id=None):
        if self.failed or self._stream.closed:
            raise OSError('Journal writer is unavailable')
        if stream not in {'control', 'stdout', 'stderr'} or not isinstance(kind, str):
            raise ValueError('Invalid journal stream or event kind')
        if not isinstance(raw, bytes):
            raise TypeError('Journal retains exact raw bytes')
        if provider_event_id is not None and (not isinstance(provider_event_id, str) or not provider_event_id):
            raise ValueError('Provider event identity must be a nonempty string')
        duplicate_of = None
        if provider_event_id is not None:
            prior = self._provider_events.get((stream, provider_event_id))
            if prior is not None:
                if prior[1] != hashlib.sha256(raw).hexdigest():
                    # Record both conflicting frames; the caller must not apply
                    # the conflicting second event as a new fact.
                    kind = 'provider_event_conflict'
                else:
                    duplicate_of = prior[0]
        body = {'seq': self.durable_seq + 1, 'stream': stream, 'kind': kind,
                'received_at_ns': time.time_ns(), 'provider_event_id': provider_event_id,
                'duplicate_of': duplicate_of, 'raw_base64': base64.b64encode(raw).decode('ascii'),
                'raw_sha256': hashlib.sha256(raw).hexdigest(), 'previous_hash': self.head_hash}
        record = {**body, 'event_hash': digest(body)}
        try:
            wire = encode(record) + b'\n'
            view = memoryview(wire)
            while view:
                written = self._stream.write(view)
                if not written:
                    raise OSError('Journal write did not advance')
                view = view[written:]
            os.fsync(self._stream.fileno())
            # A separate durable acknowledgement prevents a complete but not
            # fsync-confirmed tail from being mistaken for an applied prefix.
            marker = encode({'durable_seq': record['seq'], 'head_hash': record['event_hash']})
            fd, temporary = tempfile.mkstemp(prefix='.' + self.frontier_path.name + '-', dir=self.path.parent)
            try:
                with os.fdopen(fd, 'wb') as checkpoint_stream:
                    checkpoint_stream.write(marker); checkpoint_stream.flush(); os.fsync(checkpoint_stream.fileno())
                os.replace(temporary, self.frontier_path)
                _sync_directory(self.path.parent)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        except BaseException:
            self.failed = True
            raise
        self.durable_seq = record['seq']
        self.head_hash = record['event_hash']
        if provider_event_id is not None and duplicate_of is None and kind != 'provider_event_conflict':
            self._provider_events[(stream, provider_event_id)] = (self.durable_seq, record['raw_sha256'])
        return record

    def snapshot(self):
        return {'path': str(self.path), 'durable_seq': self.durable_seq,
                'head_hash': self.head_hash, 'frontier_path': str(self.frontier_path),
                'write_failed': self.failed}

    def close(self):
        self._stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def read_journal(path):
    """Verify the recorded prefix; never silently skip corruption or a torn tail.

    A torn final write is returned as a gap and is not part of the durable
    prefix. A complete but corrupted record raises, including after the tail.
    Callers persist their own ``applied_seq`` in the authoritative ledger.
    """
    path = Path(path)
    marker_path = path.with_name(path.name + '.frontier.json')
    try:
        marker = json.loads(marker_path.read_bytes())
        acknowledged = marker['durable_seq']
        if type(acknowledged) is not int or acknowledged < 1 or not isinstance(marker['head_hash'], str):
            raise ValueError('Invalid frontier fields')
    except FileNotFoundError:
        marker, acknowledged = None, 0
    except (ValueError, KeyError, TypeError) as exc:
        raise JournalIntegrityError('Invalid durable journal frontier') from exc
    records, gaps = [], []
    previous = '0' * 64
    raw_file = path.read_bytes()
    lines = raw_file.splitlines(keepends=True)
    for number, raw_line in enumerate(lines, 1):
        if not raw_line.endswith(b'\n'):
            gaps.append({'kind': 'incomplete_tail', 'after_seq': len(records),
                         'bytes': len(raw_line), 'sha256': hashlib.sha256(raw_line).hexdigest()})
            break
        try:
            record = json.loads(raw_line)
            body = {k: v for k, v in record.items() if k != 'event_hash'}
            raw = base64.b64decode(body['raw_base64'], validate=True)
            if (body['seq'] != number or body['previous_hash'] != previous
                    or record['event_hash'] != digest(body)
                    or body['raw_sha256'] != hashlib.sha256(raw).hexdigest()):
                raise ValueError('Sequence, hash or source bytes differ')
        except (ValueError, KeyError, TypeError) as exc:
            raise JournalIntegrityError(f'Journal integrity failure at record {number}: {exc}') from exc
        records.append(record)
        previous = record['event_hash']
        if record['kind'] in {'stream_incomplete', 'provider_event_conflict'}:
            gaps.append({'kind': record['kind'], 'seq': number})
    if acknowledged > len(records):
        raise JournalIntegrityError('Durable frontier exceeds the available complete prefix')
    if marker is not None and records[acknowledged - 1]['event_hash'] != marker['head_hash']:
        raise JournalIntegrityError('Durable frontier hash differs from journal prefix')
    unconfirmed = records[acknowledged:]
    if unconfirmed:
        gaps.append({'kind': 'unconfirmed_tail', 'after_seq': acknowledged, 'records': len(unconfirmed)})
    records = records[:acknowledged]
    return {'path': str(path.resolve()), 'records': records, 'durable_seq': acknowledged,
            'head_hash': records[-1]['event_hash'] if records else '0' * 64,
            'unconfirmed_records': unconfirmed, 'gaps': gaps}


def raw_stream(snapshot, stream):
    """Reconstruct capture chunks exactly; parsed-frame records are not doubled."""
    return b''.join(base64.b64decode(row['raw_base64']) for row in snapshot['records']
                    if row['stream'] == stream and row['kind'] == 'stream_chunk')


def unapplied_records(snapshot, applied_seq):
    if type(applied_seq) is not int or applied_seq < 0 or applied_seq > snapshot['durable_seq']:
        raise ValueError('Applied cursor is outside the verified journal prefix')
    return [row for row in snapshot['records'] if row['seq'] > applied_seq]
