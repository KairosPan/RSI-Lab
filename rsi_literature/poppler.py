"""Local Poppler text coordinates and bounded rendering of registered PDFs.

Host configuration selects installed executables and optional font configuration.
Document content and model inputs cannot supply paths, commands or font settings.
Rendering creates an image artifact; it does not claim that anyone viewed it.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import struct
import sys
import tempfile
import xml.etree.ElementTree as ET
import zlib

from .documents import DocumentError, MAX_SOURCE_BYTES, POPPLER_MEDIA_TYPE


VERSION = "rsi-poppler-v1"
MAX_IMAGE_BYTES = 1024 * 1024
# A 1600 x 1600 image is at most 2500 32-pixel patches, so Codex CLI
# 0.163.0 HIGH_DETAIL preserves its PNG bytes instead of resizing them.
# https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/utils/image/src/lib.rs#L73
MAX_DIMENSION = 1600


def _helpers():
    # Lazy import keeps parsers.run_parser as the public parsing entry point.
    from . import parsers
    return parsers


def _integer(settings, key, default, lower, upper):
    value = settings.get(key, default)
    if type(value) is not int or not lower <= value <= upper:
        raise _helpers()._ParserError("invalid_configuration", f"{key} must be an integer from {lower} to {upper}")
    return value


def _command(settings, name):
    value = settings.get(name + "_path", name)
    if not isinstance(value, str):
        _helpers()._configuration(name + "_path must name a host-installed executable")
    return _helpers()._executable([value])


@contextmanager
def _scratch(store):
    parent = store.root
    for name in ("tmp", "pdfs"):
        parent = parent / name
        if parent.is_symlink():
            raise _helpers()._ParserError("invalid_artifact", "PDF scratch directories cannot be symbolic links")
        parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="poppler-", dir=parent) as directory:
        yield Path(directory)


def _environment(settings, scratch):
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["XDG_CACHE_HOME"] = str(scratch / "cache")
    configured = settings.get("fontconfig_file")
    font_mode = "host_default"
    if configured:
        if not isinstance(configured, str):
            _helpers()._configuration("fontconfig_file must be a host file path")
        path = Path(configured).expanduser().absolute()
        if not path.is_file() or path.stat().st_size > 1024 * 1024:
            _helpers()._configuration("The configured Fontconfig file is absent or exceeds 1 MiB")
        env["FONTCONFIG_FILE"] = str(path)
        font_mode = "host_configuration"
    elif sys.platform == "darwin" and Path("/System/Library/Fonts/Supplemental/Arial.ttf").is_file():
        # Homebrew's default Helvetica substitution can render empty glyphs on
        # macOS. Isolate the known system-font fallback to this single job.
        root = ET.Element("fontconfig")
        ET.SubElement(root, "dir").text = "/System/Library/Fonts/Supplemental"
        ET.SubElement(root, "dir").text = "/System/Library/Fonts"
        ET.SubElement(root, "cachedir").text = str(scratch / "font-cache")
        for family, replacement in (("Helvetica", "Arial"), ("Times", "Times New Roman"),
                                    ("Courier", "Courier New"), ("sans-serif", "Arial")):
            alias = ET.SubElement(root, "alias")
            ET.SubElement(alias, "family").text = family
            ET.SubElement(ET.SubElement(alias, "prefer"), "family").text = replacement
        path = scratch / "fonts.conf"
        path.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))
        env["FONTCONFIG_FILE"] = str(path)
        font_mode = "isolated_macos_system_font_fallback"
    return env, font_mode


def _info(settings, source, scratch, provenance, env):
    output = _helpers()._execute([*_command(settings, "pdfinfo"), "-enc", "UTF-8", str(source)],
                                 scratch, settings, provenance, env)
    match = re.search(rb"(?m)^Pages:\s+(\d+)\s*$", output)
    if not match:
        raise _helpers()._ParserError("invalid_parser_output", "pdfinfo did not report a page count")
    pages = int(match[1])
    maximum = _integer(settings, "max_pages", 200, 1, 1000)
    if not 1 <= pages <= maximum:
        raise _helpers()._ParserError("page_limit", f"PDF exceeds the host limit of {maximum} pages", "unavailable")
    if re.search(rb"(?mi)^Encrypted:\s+yes\b", output):
        raise _helpers()._ParserError("encrypted_pdf", "Encrypted PDFs are not supported", "unavailable")
    return pages


def extract_document(settings, raw, store, effect_id, provenance):
    """Return immutable bbox XML and coverage; caller archives the derived source."""
    api = _helpers()
    _command(settings, "pdfinfo")
    text_command = _command(settings, "pdftotext")
    api._job(store, effect_id)
    maximum = _integer(settings, "max_output_bytes", 16 * 1024 * 1024, 1024, MAX_SOURCE_BYTES)
    provenance["parser_identity"] = {"provider": "poppler", "entrypoint": "pdftotext -bbox-layout",
        "adapter_version": VERSION, "runtime_version": settings.get("version", "unreported")}
    with _scratch(store) as scratch:
        source, output = scratch / "source.pdf", scratch / "document.xhtml"
        source.write_bytes(raw)
        env, font_mode = _environment(settings, scratch)
        total_pages = _info(settings, source, scratch, provenance, env)
        api._execute([*text_command, "-f", "1", "-l", str(total_pages), "-enc", "UTF-8", "-bbox-layout",
                      str(source), str(output)], scratch, settings, provenance, env, {output: maximum})
        result = api._read_file(scratch, "document.xhtml", maximum)
        if b"<!ENTITY" in result.replace(b"\x00", b"").upper():
            raise api._ParserError("invalid_parser_output", "Poppler output contains unsupported XML entities")
        try:
            root = ET.fromstring(result)
        except ET.ParseError as exc:
            raise api._ParserError("invalid_parser_output", "Poppler returned invalid bbox XML") from exc
        pages = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "page"]
        if len(pages) != total_pages:
            raise api._ParserError("incomplete_pages", "Poppler output does not cover the reported PDF pages")
        unread = [i for i, page in enumerate(pages, 1) if not any(
            node.tag.rsplit("}", 1)[-1] == "word" and (node.text or "").strip() for node in page.iter())]
    return result, POPPLER_MEDIA_TYPE, {"review_status": "unreviewed", "pages_requested": total_pages,
        "page_count": total_pages, "unread_pages": unread, "text_layer_complete": not unread,
        "ocr_performed": False, "page_coordinates_available": True, "font_configuration": font_mode,
        "limitations": ["Native text and PDF word boxes only; images, tables, equations and reading order need visual review.",
                        "Pages without text may contain scans, graphics or be blank; no OCR was performed."]}


def _original_pdf(store, document_id):
    """Resolve only hash-bound registered derivations; never follow source paths."""
    api = _helpers()
    seen = set()
    for _ in range(9):
        if document_id in seen:
            raise api._ParserError("invalid_lineage", "Document derivation contains a cycle")
        seen.add(document_id)
        manifest, raw = api._source(store, document_id)
        if manifest["media_type"].split(";", 1)[0].strip().lower() == "application/pdf":
            if not raw.lstrip().startswith(b"%PDF-"):
                raise api._ParserError("unsupported_source", "Registered source is not a PDF", "unavailable")
            return manifest, raw
        origin = manifest["source_metadata"].get("derived_from")
        if not isinstance(origin, dict) or not isinstance(origin.get("document_id"), str):
            raise api._ParserError("unsupported_source", "This document has no registered original PDF", "unavailable")
        parent = store.manifest(origin["document_id"])
        if any(origin.get(key) != parent[key] for key in ("document_version", "source_sha256")):
            raise api._ParserError("store_integrity", "Derived document does not bind the original PDF version")
        document_id = origin["document_id"]
    raise api._ParserError("invalid_lineage", "Document derivation exceeds eight links")


def _png_dimensions(raw, maximum):
    api = _helpers()
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise api._ParserError("invalid_image", "Renderer did not return a PNG")
    at, dimensions, ended, has_data = 8, None, False, False
    while at + 12 <= len(raw):
        length = struct.unpack(">I", raw[at:at + 4])[0]
        kind = raw[at + 4:at + 8]
        end = at + 12 + length
        if end > len(raw) or zlib.crc32(raw[at + 4:end - 4]) & 0xffffffff != struct.unpack(">I", raw[end - 4:end])[0]:
            raise api._ParserError("invalid_image", "PNG is truncated or has an invalid checksum")
        if at == 8:
            if kind != b"IHDR" or length != 13:
                raise api._ParserError("invalid_image", "PNG header is invalid")
            dimensions = struct.unpack(">II", raw[at + 8:at + 16])
        if kind == b"IEND":
            ended = length == 0 and end == len(raw)
            break
        if kind == b"IDAT" and length:
            has_data = True
        at = end
    if not ended or not has_data or dimensions is None or not all(1 <= value <= maximum for value in dimensions):
        raise api._ParserError("image_dimensions", "PNG dimensions or complete image structure violate the host limit")
    return dimensions


def render_document_page(settings, inputs, document_store, effect_id):
    """Return a host-owned PNG descriptor for a later visual evidence attachment."""
    api = _helpers()
    provenance = {"provider": "poppler", "attempt_count": 0, "adapter_version": VERSION}
    result = {"status": "failed", "data": {}, "provenance": provenance,
        "coverage": {"visual_content_interpreted": False, "image_delivered": False, "ocr_performed": False},
        "continuation": {"next_cursor": None, "complete_for_query": False}, "errors": []}
    output = None
    try:
        if not isinstance(settings, dict) or not isinstance(inputs, dict) or set(inputs) != {"document_id", "page"}:
            raise api._ParserError("invalid_input", "Render accepts only document_id and page")
        page = _integer(inputs, "page", 1, 1, 1000)
        maximum = _integer(settings, "max_image_bytes", 512 * 1024, 1024, MAX_IMAGE_BYTES)
        dimension = _integer(settings, "max_dimension", 1600, 128, MAX_DIMENSION)
        command = _command(settings, "pdftoppm")
        _command(settings, "pdfinfo")
        manifest, raw = _original_pdf(document_store, inputs["document_id"])
        provenance.update(source_document_id=manifest["document_id"], source_sha256=manifest["source_sha256"],
                          requested_document_id=inputs["document_id"], page=page)
        job = api._job(document_store, effect_id)
        output = job / "page.png"
        with _scratch(document_store) as scratch:
            source = scratch / "source.pdf"
            source.write_bytes(raw)
            env, font_mode = _environment(settings, scratch)
            total_pages = _info(settings, source, scratch, provenance, env)
            if page > total_pages:
                raise api._ParserError("page_out_of_range", "Requested page is outside the registered PDF")
            api._execute([*command, "-f", str(page), "-l", str(page), "-singlefile",
                          "-scale-to", str(dimension), "-png", str(source), str(job / "page")],
                         scratch, settings, provenance, env, {output: maximum})
            image = api._read_file(job, "page.png", maximum)
            width, height = _png_dimensions(image, dimension)
        result["status"] = "ok"
        result["data"]["image"] = {"artifact_path": str(output), "media_type": "image/png",
            "sha256": hashlib.sha256(image).hexdigest(), "bytes": len(image), "width": width, "height": height,
            "page": page, "source_document_id": manifest["document_id"], "source_sha256": manifest["source_sha256"]}
        result["data"].update(source_document_version=manifest["document_version"])
        result["coverage"].update(page=page, page_count=total_pages, rendered=True, font_configuration=font_mode,
            limitations=["Rendering is a page image, not an OCR result or a verified interpretation.",
                         "Nonembedded fonts may be substituted; scaling is bounded by the host image dimensions."])
        result["continuation"]["complete_for_query"] = True
    except api._ParserError as exc:
        result["status"] = exc.status
        result["errors"].append({"code": exc.code, "detail": str(exc), "retryability": exc.retryability})
    except DocumentError as exc:
        result["errors"].append({"code": exc.code, "detail": str(exc), "retryability": "no"})
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result["errors"].append({"code": "render_failed", "detail": "Local PDF rendering failed", "retryability": "unknown"})
    if result["status"] != "ok" and output is not None and output.is_file() and not output.is_symlink():
        output.unlink()
    return result
