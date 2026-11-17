"""Bounded immutable PNG evidence and exact CLI attachment manifests.

Image bytes are never model-selected paths. The ledger stores the bytes by
hash; packets carry their identity and transports materialize that exact blob.
Submission certifies an input modality, not visual understanding or truth.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
import re
import struct
import zlib

MAX_IMAGE_BYTES = 1024 * 1024
MAX_IMAGE_DIMENSION = 2000
# Codex CLI 0.163.0 HIGH_DETAIL resizes above 2500 patches of 32 x 32.
# Keep this transport admission separate from generic PNG/source validation.
# https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/utils/image/src/lib.rs#L73
CODEX_HIGH_DETAIL_PATCH_SIZE = 32
CODEX_HIGH_DETAIL_MAX_PATCHES = 2500
MAX_IMAGES = 4
MAX_TOTAL_IMAGE_BYTES = 4 * MAX_IMAGE_BYTES
MAX_HISTORY_IMAGE_BYTES = 16 * MAX_IMAGE_BYTES
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}\Z')
IMAGE_FIELDS = {'media_type', 'sha256', 'bytes', 'width', 'height', 'page',
                'source_document_id', 'source_sha256'}
ATTACHMENT_FIELDS = IMAGE_FIELDS | {'attachment_id', 'evidence_id', 'source_version', 'blob_ref'}


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def png_dimensions(raw):
    if not isinstance(raw, bytes) or not 45 <= len(raw) <= MAX_IMAGE_BYTES or raw[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('Image must be a bounded PNG byte stream')
    offset, dimensions, seen_data, finished = 8, None, False, False
    while offset + 12 <= len(raw):
        size = struct.unpack('>I', raw[offset:offset + 4])[0]
        kind = raw[offset + 4:offset + 8]
        end = offset + 12 + size
        if end > len(raw) or (zlib.crc32(raw[offset + 4:end - 4]) & 0xffffffff) != struct.unpack('>I', raw[end - 4:end])[0]:
            raise ValueError('PNG chunk length or checksum is invalid')
        if dimensions is None:
            if kind != b'IHDR' or size != 13:
                raise ValueError('PNG must begin with an IHDR')
            width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', raw[offset + 8:end - 4])
            if (not 1 <= width <= MAX_IMAGE_DIMENSION or not 1 <= height <= MAX_IMAGE_DIMENSION
                    or compression != 0 or filtering != 0 or interlace not in (0, 1)
                    or color not in (0, 2, 3, 4, 6) or depth not in (1, 2, 4, 8, 16)):
                raise ValueError('Unsupported PNG header or excessive image dimensions')
            dimensions = (width, height)
        elif kind == b'IHDR' or kind in {b'acTL', b'fcTL', b'fdAT'}:
            raise ValueError('Only a single static PNG page is supported')
        if kind == b'IDAT':
            seen_data = True
        if kind == b'IEND':
            if size or end != len(raw) or not seen_data:
                raise ValueError('PNG has an invalid terminal chunk')
            finished = True
            break
        offset = end
    if not finished:
        raise ValueError('PNG is incomplete')
    return dimensions


def _metadata(value):
    if (value.get('media_type') != 'image/png'
            or any(not isinstance(value.get(k), str) or not _HASH.fullmatch(value[k]) for k in ('sha256', 'source_sha256'))
            or not isinstance(value.get('source_document_id'), str) or not _ID.fullmatch(value['source_document_id'])
            or type(value.get('page')) is not int or value['page'] < 1
            or type(value.get('bytes')) is not int or not 45 <= value['bytes'] <= MAX_IMAGE_BYTES
            or any(type(value.get(k)) is not int or not 1 <= value[k] <= MAX_IMAGE_DIMENSION for k in ('width', 'height'))):
        raise ValueError('Image needs bounded bytes/dimensions and exact page/source identity')


def decoded_image_bytes(value):
    text = value.get('data_base64')
    if not isinstance(text, str) or len(text) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise ValueError('Image base64 is absent or exceeds its allowance')
    try:
        raw = base64.b64decode(text, validate=True)
    except (ValueError, UnicodeError) as exc:
        raise ValueError('Invalid image base64') from exc
    if base64.b64encode(raw).decode('ascii') != text:
        raise ValueError('Image base64 is not canonical')
    _metadata(value)
    if (_hash(raw) != value['sha256'] or len(raw) != value['bytes']
            or png_dimensions(raw) != (value['width'], value['height'])):
        raise ValueError('Image bytes differ from their hash, dimensions or size')
    return raw


def validate_image_content(value):
    if (not isinstance(value, dict) or set(value) != IMAGE_FIELDS | {'format', 'data_base64'}
            or value['format'] != 'rsi_image_evidence_v1'):
        raise ValueError('Image source content must use the closed image evidence contract')
    decoded_image_bytes(value)
    return copy.deepcopy(value)


def image_content_from_bytes(raw, metadata):
    if not isinstance(metadata, dict) or set(metadata) != IMAGE_FIELDS:
        raise ValueError('Provide only registered image metadata, never a path')
    return validate_image_content({'format': 'rsi_image_evidence_v1', **metadata,
                                  'data_base64': base64.b64encode(raw).decode('ascii')})


def image_reference(content):
    content = validate_image_content(content)
    return {'format': 'rsi_image_evidence_ref_v1', **{key: content[key] for key in IMAGE_FIELDS},
            'blob_ref': content['sha256']}


def validate_image_reference(value):
    if (not isinstance(value, dict) or set(value) != IMAGE_FIELDS | {'format', 'blob_ref'}
            or value['format'] != 'rsi_image_evidence_ref_v1' or value['blob_ref'] != value['sha256']):
        raise ValueError('Invalid immutable image reference')
    _metadata(value)
    return copy.deepcopy(value)


def validate_image_attachment(value, *, with_data=False):
    expected = ATTACHMENT_FIELDS | ({'data_base64'} if with_data else set())
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError('Image attachment must use its closed manifest contract')
    _metadata(value)
    patch = CODEX_HIGH_DETAIL_PATCH_SIZE
    patch_count = ((value['width'] + patch - 1) // patch) * ((value['height'] + patch - 1) // patch)
    if patch_count > CODEX_HIGH_DETAIL_MAX_PATCHES:
        raise ValueError('Image exceeds the verified Codex high-detail no-resize limit of 2500 32x32 patches; '
                         're-render with max_dimension <= 1600')
    if (any(not isinstance(value[k], str) or not _ID.fullmatch(value[k]) for k in ('attachment_id', 'evidence_id'))
            or type(value['source_version']) is not int or value['source_version'] < 1
            or value['blob_ref'] != value['sha256']):
        raise ValueError('Image attachment identity is invalid')
    if with_data:
        decoded_image_bytes(value)
    return copy.deepcopy(value)


def attachment_manifest(images):
    if not isinstance(images, list) or len(images) > MAX_IMAGES:
        raise ValueError('Too many image attachments')
    manifest = []
    for item in images:
        checked = validate_image_attachment(item, with_data='data_base64' in item)
        manifest.append({key: checked[key] for key in sorted(ATTACHMENT_FIELDS)})
    if (sum(item['bytes'] for item in manifest) > MAX_TOTAL_IMAGE_BYTES
            or len({item['attachment_id'] for item in manifest}) != len(manifest)):
        raise ValueError('Image attachment bytes or duplicate identities exceed the contract')
    return manifest


def image_manifest_sha256(images):
    return _hash(_encoded(attachment_manifest(images)))


def verify_image_transport(folder, expected, receipt):
    """Verify immutable image bytes, explicit -i arguments and sent identities."""
    manifest = attachment_manifest(expected)
    if not manifest:
        if receipt.get('sent_image_manifest'):
            raise ValueError('Transport attached unrequested image evidence')
        return []
    wanted_hash = image_manifest_sha256(manifest)
    if (receipt.get('sent_image_manifest') != manifest
            or receipt.get('sent_image_manifest_sha256') != wanted_hash):
        raise ValueError('Transport receipt does not certify the requested images')
    folder = Path(folder)
    def read(path):
        if path.is_symlink() or not path.is_file():
            raise ValueError('Missing or unsafe image transport artifact')
        before = path.stat()
        if before.st_size > MAX_IMAGE_BYTES:
            raise ValueError('Image transport artifact exceeds its byte limit')
        raw = path.read_bytes()
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError('Image transport artifact changed during verification')
        return raw
    audit = json.loads(read(folder / 'images.json'))
    invocation = json.loads(read(folder / 'invocation.json'))
    if (set(audit) != {'manifest', 'files'} or audit['manifest'] != manifest
            or len(audit['files']) != len(manifest) or invocation.get('dispatched') is not True
            or invocation.get('sent_image_manifest') != manifest
            or invocation.get('sent_image_manifest_sha256') != wanted_hash):
        raise ValueError('Image audit differs from the dispatched invocation')
    command = invocation.get('command', [])
    image_args = [command[index + 1] for index, arg in enumerate(command[:-1]) if arg in ('-i', '--image')]
    expected_paths = []
    for index, (item, file) in enumerate(zip(manifest, audit['files'])):
        path = folder / 'images' / f'image-{index:03d}.png'
        expected_paths.append(str(path.resolve()))
        if (path.parent.is_symlink() or set(file) != {'attachment_id', 'path', 'sha256', 'bytes'}
                or file != {'attachment_id': item['attachment_id'], 'path': str(path.resolve()),
                            'sha256': item['sha256'], 'bytes': item['bytes']}):
            raise ValueError('Image file audit is not bound to the exact call directory')
        raw = read(path)
        decoded_image_bytes({**item, 'data_base64': base64.b64encode(raw).decode('ascii')})
    if image_args != expected_paths:
        raise ValueError('Codex image arguments differ from the admitted attachment order')
    return manifest
