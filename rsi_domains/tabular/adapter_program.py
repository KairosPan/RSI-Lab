"""Reviewed standalone CSV adapter; supports explicit Wh/kWh metadata only.

The bounded domain contract rejects missing units, duplicate identities,
unknown units and non-finite/negative numbers. No unit is inferred from size.
This file is copied intact to isolated local acceptance/branch processes.
"""
from decimal import Decimal, InvalidOperation
import csv
import hashlib
import io
import json
import sys

ALGORITHM = 'convert_units'
FACTORS = {'Wh': Decimal('0.001'), 'kWh': Decimal(1)}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else encoded(value)).hexdigest()


class InputError(ValueError):
    def __init__(self, code, text):
        self.code = code
        super().__init__(text)


def aggregate(value):
    if not isinstance(value, dict) or set(value) != {'dataset_id', 'csv_text', 'units'}:
        raise InputError('invalid_input', 'Expected dataset identity, CSV text and explicit per-record units')
    if not isinstance(value['dataset_id'], str) or not value['dataset_id'] or len(value['dataset_id']) > 128:
        raise InputError('invalid_identity', 'Dataset identity must be explicit and bounded')
    if not isinstance(value['csv_text'], str) or len(value['csv_text'].encode()) > 262144:
        raise InputError('invalid_input', 'CSV input exceeds the supported bounded text profile')
    if not isinstance(value['units'], dict) or not value['units']:
        raise InputError('missing_units', 'Explicit per-record units are required')
    csv.field_size_limit(8192)
    reader = csv.DictReader(io.StringIO(value['csv_text']))
    if reader.fieldnames != ['record_id', 'group', 'energy']:
        raise InputError('invalid_columns', 'Expected record_id,group,energy columns in that order')
    seen, totals, counts, row_receipts = set(), {}, {}, []
    for index, row in enumerate(reader):
        if index >= 1000 or set(row) != {'record_id', 'group', 'energy'} or any(x is None for x in row.values()):
            raise InputError('invalid_rows', 'CSV row count or shape is outside this adapter profile')
        rid, group = row['record_id'], row['group']
        if not rid or not group or len(rid) > 128 or len(group) > 128:
            raise InputError('invalid_identity', 'Each record and group needs a bounded nonempty identity')
        if rid in seen:
            raise InputError('duplicate_record_id', 'Duplicate record identity: ' + rid)
        seen.add(rid)
        if rid not in value['units']:
            raise InputError('missing_units', 'Missing unit for record: ' + rid)
        unit = value['units'][rid]
        if not isinstance(unit, str) or unit not in FACTORS:
            raise InputError('unsupported_unit', 'Only explicit Wh and kWh are supported')
        try:
            number = Decimal(row['energy'])
        except InvalidOperation as exc:
            raise InputError('invalid_number', 'Energy must be a finite nonnegative decimal') from exc
        if not number.is_finite() or number < 0 or number > Decimal('1000000000000'):
            raise InputError('invalid_number', 'Energy must be finite, nonnegative and within the declared range')
        normalized = number * FACTORS[unit] if ALGORITHM == 'convert_units' else number
        totals[group] = totals.get(group, Decimal(0)) + normalized
        counts[group] = counts.get(group, 0) + 1
        row_receipts.append({'record_id': rid, 'group': group, 'unit': unit, 'energy_kwh': float(normalized)})
    if not seen or set(value['units']) != seen:
        raise InputError('unit_identity_mismatch', 'Unit metadata must identify exactly the CSV records')
    return {'dataset_id': value['dataset_id'], 'record_ids': sorted(seen), 'row_count': len(seen),
            'groups': [{'group': group, 'row_count': counts[group], 'energy_kwh': float(totals[group])}
                       for group in sorted(totals)], 'row_receipts': row_receipts, 'output_unit': 'kWh'}


def main():
    packet = json.load(sys.stdin)
    if packet.get('schema_version') == 'rsi_trial_input_v2':
        value = packet['input']
        outcome = aggregate(value['dataset'])
        actual = {row['group']: row['energy_kwh'] for row in outcome['groups']}
        expected = value['expected_totals_kwh']
        error = max(abs(actual.get(group, float('inf')) - total) for group, total in expected.items())
        response = {'schema_version': 'rsi_trial_output_v2', 'identity': packet['identity'],
                    'metrics': {'max_absolute_error_kwh': error, 'row_loss': len(value['expected_record_ids']) - outcome['row_count']},
                    'execution_status': 'complete', 'input_receipt_sha256': digest(value)}
    elif packet.get('schema_version') in {'capability_input_v2', 'tabular_aggregate_input_v1'}:
        value = packet['input']
        try:
            outcome = aggregate(value)
            response = {'schema_version': 'capability_response_v2', 'ok': True, 'output': outcome,
                'identity': value['dataset_id'], 'source_receipts': [{'kind': 'input', 'sha256': digest(value)}], 'error': None}
        except (InputError, csv.Error) as exc:
            response = {'schema_version': 'capability_response_v2', 'ok': False, 'output': None, 'identity': None,
                'source_receipts': [{'kind': 'input', 'sha256': digest(value)}],
                'error': {'code': getattr(exc, 'code', 'invalid_rows'), 'message': str(exc)}}
    else:
        raise InputError('unsupported_envelope', 'Unsupported versioned adapter input envelope')
    json.dump(response, sys.stdout, allow_nan=False)


if __name__ == '__main__':
    main()
