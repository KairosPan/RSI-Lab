"""Immutable, local literature sources and bounded, source-addressable reading.

Only the host supplies bytes and the store directory. Public reads accept registered
hash identifiers, never paths or URLs. Normalized text is an extraction artifact:
its scope and losses are reported separately from successful byte acquisition.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from html.parser import HTMLParser
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import xml.etree.ElementTree as ET


PARSER_VERSION = "rsi-literature-document-v2"
POPPLER_MEDIA_TYPE = "application/vnd.rsi.poppler-bbox+xml"
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
DEFAULT_READ_BYTES = 8000
_DOC_ID = re.compile(r"doc_[0-9a-f]{64}\Z")
_SCOPES = {"full_text", "abstract", "excerpt", "metadata_only", "unknown"}


class DocumentError(ValueError):
    """A local contract, cursor, or artifact-integrity failure."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _norm(text):
    return " ".join(text.split())


def _tag(node):
    return node.tag.rsplit("}", 1)[-1] if isinstance(node.tag, str) else ""


def _words(node):
    if node is None:
        return ""
    # XML can be minified: structural siblings need boundaries even when their
    # text nodes have no whitespace. Inline markup retains its original joins.
    boundaries = {"p", "title", "label", "caption", "sec", "tr", "td", "th", "table", "table-wrap",
                  "ref", "list-item", "article-title", "source", "year", "volume", "fpage", "lpage",
                  "head", "div", "biblStruct", "persName", "forename", "surname", "date", "biblScope", "cell", "row"}
    parts, stack = [], [(node, 0)]
    while stack:
        value, depth = stack.pop()
        if isinstance(value, str):
            parts.append(value)
            continue
        if depth > 64:
            raise DocumentError("xml_capacity", "XML nesting exceeds the supported depth")
        if value.text:
            parts.append(value.text)
        for child in reversed(list(value)):
            if child.tail:
                stack.append((child.tail, depth))
            if _tag(child) in boundaries:
                stack.append((" ", depth))
            stack.append((child, depth + 1))
            if _tag(child) in boundaries:
                stack.append((" ", depth))
    return _norm("".join(parts))


def _limit(value, name="max_bytes", maximum=MAX_OUTPUT_BYTES, minimum=4):
    if type(value) is not int or not minimum <= value <= maximum:
        raise DocumentError("invalid_limit", f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _decode(raw, media_type):
    charset = re.search(r"charset\s*=\s*[\"']?([\w-]+)", media_type, re.I)
    encoding = charset.group(1).lower() if charset else "utf-8-sig"
    if not charset and raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    if encoding not in {"utf-8", "utf8", "utf-8-sig", "ascii", "us-ascii", "utf-16", "utf-16le", "utf-16be", "iso-8859-1", "latin-1", "windows-1252"}:
        raise DocumentError("unsupported_encoding", "The source charset is not supported")
    try:
        return raw.decode(encoding)
    except UnicodeError as exc:
        raise DocumentError("invalid_encoding", "Source cannot be decoded without character loss") from exc


class _Builder:
    def __init__(self):
        self.blocks, self.sections, self.references = [], [], []
        self.limitations = []
        self.scope = "unknown"
        self.source_body_present = False
        self.extraction_status = "complete"
        self.pages = []

    def section(self, title, parent=None):
        item = {"locator": f"section:{len(self.sections) + 1:06d}", "title": _norm(title) or "Untitled",
                "parent": parent["locator"] if parent else None,
                "path": (parent["path"] if parent else []) + [_norm(title) or "Untitled"],
                "start_block": len(self.blocks), "end_block": len(self.blocks)}
        self.sections.append(item)
        return item

    def close(self, section):
        section["end_block"] = len(self.blocks)

    def add(self, text, kind, section, *, citation_ids=(), source_locator=None):
        text = _norm(text)
        if not text:
            return None
        block = {"locator": f"block:{len(self.blocks) + 1:06d}", "text": text, "kind": kind,
                 "section": section["locator"], "section_path": section["path"],
                 "citation_ids": list(dict.fromkeys(citation_ids)), "source_locator": source_locator}
        self.blocks.append(block)
        return block

    def finish(self):
        char_at, byte_at = 0, 0
        for i, block in enumerate(self.blocks):
            if i:
                char_at += 2
                byte_at += 2
            block["char_range"] = [char_at, char_at + len(block["text"])]
            block["byte_range"] = [byte_at, byte_at + len(block["text"].encode("utf-8"))]
            char_at, byte_at = block["char_range"][1], block["byte_range"][1]
        for section in self.sections:
            selected = self.blocks[section.pop("start_block"):section.pop("end_block")]
            section["char_range"] = [selected[0]["char_range"][0], selected[-1]["char_range"][1]] if selected else [0, 0]
            section["block_locators"] = [block["locator"] for block in selected]
        return "\n\n".join(block["text"] for block in self.blocks)


def _citations(node):
    return [rid for child in node.iter() if _tag(child) == "xref" and child.get("ref-type") == "bibr"
            for rid in child.get("rid", "").split()]


def _tei_document(root):
    """Normalize GROBID TEI while retaining local reference IDs and PDF locators."""
    builder = _Builder()
    builder.limitations.append("TEI text and supplied coordinates are parser output; figures, OCR accuracy and citation-graph completeness are not verified.")
    xml_id = "{http://www.w3.org/XML/1998/namespace}id"

    def add(node, kind, section):
        targets = [target.lstrip("#") for ref in node.iter() if _tag(ref) == "ref" and ref.get("type") == "bibr"
                   for target in ref.get("target", "").split()]
        location = {key: value for key, value in (("xml_id", node.get(xml_id)), ("coords", node.get("coords"))) if value}
        return builder.add(_words(node), kind, section, citation_ids=targets, source_locator=location or None)

    def walk(node, section, depth=0):
        if depth > 64:
            raise DocumentError("xml_capacity", "XML nesting exceeds the supported depth")
        for child in node:
            name = _tag(child)
            if name == "div":
                title = next((n for n in child if _tag(n) == "head"), None)
                nested = builder.section(_words(title) or child.get("type") or "Untitled section", section)
                walk(child, nested, depth + 1)
                builder.close(nested)
            elif name in {"p", "head", "figure", "formula", "table", "item", "note"}:
                add(child, {"head": "heading", "figure": "figure_caption", "item": "list_item"}.get(name, name), section)
            elif name != "listBibl":
                if not list(child) and _words(child):
                    add(child, "text", section)
                else:
                    walk(child, section, depth + 1)

    title_stmt = next((n for n in root.iter() if _tag(n) == "titleStmt"), None)
    if title_stmt is not None:
        title = next((n for n in title_stmt if _tag(n) == "title"), None)
        if title is not None:
            section = builder.section("Title")
            add(title, "title", section)
            builder.close(section)
    for abstract in (n for n in root.iter() if _tag(n) == "abstract"):
        section = builder.section("Abstract")
        if list(abstract):
            walk(abstract, section)
        else:
            add(abstract, "paragraph", section)
        builder.close(section)
        builder.scope = "abstract"
    body = next((n for n in root.iter() if _tag(n) == "body"), None)
    if body is not None:
        section = builder.section("Body")
        walk(body, section)
        builder.close(section)
        builder.source_body_present = bool(_words(body))
        if builder.source_body_present:
            builder.scope = "full_text"
    refs = [ref for node in root.iter() if _tag(node) == "listBibl" for ref in node if _tag(ref) in {"biblStruct", "bibl"}]
    if refs:
        section = builder.section("References")
        for i, ref in enumerate(refs):
            block = add(ref, "reference", section)
            ids = {n.get("type", "unknown").lower(): _words(n) for n in ref.iter() if _tag(n) == "idno"}
            builder.references.append({"reference_id": ref.get(xml_id) or f"reference-{i + 1}",
                                       "text": _words(ref), "identifiers": ids,
                                       "locator": block["locator"] if block else None})
        builder.close(section)
    if builder.scope == "unknown":
        builder.scope = "metadata_only"
    return builder


def _xml_document(raw):
    if b"<!ENTITY" in raw.replace(b"\x00", b"").upper():
        raise DocumentError("unsafe_xml", "Embedded XML entity declarations are not supported")
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, ValueError) as exc:
        raise DocumentError("invalid_xml", "Source is not readable XML") from exc
    if sum(1 for _ in root.iter()) > 100000:
        raise DocumentError("xml_capacity", "XML source exceeds the supported element count")
    if _tag(root) == "TEI":
        return _tei_document(root)
    builder = _Builder()
    article = root if _tag(root) == "article" else next((n for n in root.iter() if _tag(n) == "article"), None)
    if article is None:
        section = builder.section("Document")
        builder.add(_words(root), "text", section)
        builder.close(section)
        builder.limitations.append("Generic XML was flattened; document semantics are unknown.")
        return builder
    if sum(_tag(n) == "article" for n in root.iter()) > 1:
        raise DocumentError("multiple_documents", "Ingest each article separately to preserve source identity")
    front = next((n for n in article if _tag(n) == "front"), None)
    if front is not None:
        title = next((n for n in front.iter() if _tag(n) == "article-title"), None)
        if title is not None:
            section = builder.section("Title")
            builder.add(_words(title), "title", section, source_locator="front/article-title")
            builder.close(section)

    def walk(container, section, depth=0):
        if depth > 64:
            raise DocumentError("xml_capacity", "XML nesting exceeds the supported depth")
        if container.text and container.text.strip():
            builder.add(container.text, "paragraph", section)
        for child in container:
            name = _tag(child)
            if name == "sec":
                title = next((n for n in child if _tag(n) == "title"), None)
                nested = builder.section(_words(title) or "Untitled section", section)
                if title is not None:
                    builder.add(_words(title), "heading", nested)
                # The section title has already been emitted.
                for n in child:
                    if _tag(n) != "title":
                        emit(n, nested, depth + 1)
                builder.close(nested)
            elif name == "title":
                builder.add(_words(child), "heading", section)
            else:
                emit(child, section, depth + 1)
            if child.tail and child.tail.strip():
                builder.add(child.tail, "paragraph", section)

    def emit(node, section, depth):
        name = _tag(node)
        kinds = {"p": "paragraph", "list-item": "list_item", "disp-quote": "quotation",
                 "fig": "figure_caption", "table-wrap": "table", "disp-formula": "formula"}
        if name in kinds:
            builder.add(_words(node), kinds[name], section, citation_ids=_citations(node),
                        source_locator=node.get("id"))
            if name in {"fig", "table-wrap", "disp-formula"}:
                builder.limitations.append("Figures, tables and formulas contain extracted XML text only; images and visual values are not interpreted.")
        elif name == "sec":
            holder = ET.Element("container")
            holder.append(node)
            walk(holder, section, depth)
        elif name not in {"graphic", "media", "supplementary-material"}:
            walk(node, section, depth)
        else:
            builder.limitations.append("Linked media and supplementary files were not acquired by this parser.")

    if front is not None:
        for abstract in (n for n in front.iter() if _tag(n) == "abstract"):
            section = builder.section("Abstract")
            walk(abstract, section)
            builder.close(section)
            builder.scope = "abstract"
    body = next((n for n in article if _tag(n) == "body"), None)
    if body is not None:
        builder.source_body_present = bool(_words(body))
        section = builder.section("Body")
        walk(body, section)
        builder.close(section)
        if builder.source_body_present:
            builder.scope = "full_text"
    back = next((n for n in article if _tag(n) == "back"), None)
    if back is not None:
        refs = [n for n in back.iter() if _tag(n) == "ref"]
        if refs:
            section = builder.section("References")
            for i, ref in enumerate(refs):
                ref_id = ref.get("id") or f"reference-{i + 1}"
                ids = {n.get("pub-id-type", "unknown"): _words(n) for n in ref.iter() if _tag(n) == "pub-id"}
                block = builder.add(_words(ref), "reference", section, source_locator=ref_id)
                builder.references.append({"reference_id": ref_id, "text": _words(ref), "identifiers": ids,
                                           "locator": block["locator"] if block else None})
            builder.close(section)
        for child in back:
            if _tag(child) != "ref-list":
                section = builder.section("Back matter")
                emit(child, section, 0)
                builder.close(section)
    if builder.scope == "unknown":
        builder.scope = "metadata_only"
    return builder


class _HTMLBlocks(HTMLParser):
    _boundaries = {"p", "div", "section", "article", "main", "li", "blockquote", "pre", "tr", "figcaption", "h1", "h2", "h3", "h4", "h5", "h6"}
    _excluded = {"script", "style", "head", "nav", "footer", "noscript", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.records, self.buffer, self.skip = [], [], []
        self.article_depth = self.main_depth = 0
        self.kind, self.level, self.links = "paragraph", 0, []

    def flush(self):
        text = _norm("".join(self.buffer))
        if text:
            self.records.append((text, self.kind, self.level, list(self.links), bool(self.article_depth), bool(self.main_depth)))
        self.buffer, self.links = [], []

    def handle_starttag(self, tag, attrs):
        if self.skip:
            if tag in self._excluded:
                self.skip.append(tag)
            return
        if tag in self._excluded:
            self.flush()
            self.skip.append(tag)
            return
        if tag in self._boundaries:
            self.flush()
            self.kind = "heading" if re.fullmatch(r"h[1-6]", tag) else "table" if tag == "tr" else "figure_caption" if tag == "figcaption" else "paragraph"
            self.level = int(tag[1]) if self.kind == "heading" else 0
        if tag == "article":
            self.article_depth += 1
        if tag == "main":
            self.main_depth += 1
        if tag in {"br", "td", "th"}:
            self.buffer.append(" ")
        if tag == "a":
            href = dict(attrs).get("href", "")
            if href.startswith("#"):
                self.links.append(href[1:])

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
            return
        if tag in self._boundaries:
            self.flush()
            self.kind, self.level = "paragraph", 0
        if tag == "article":
            self.article_depth = max(0, self.article_depth - 1)
        if tag == "main":
            self.main_depth = max(0, self.main_depth - 1)

    def handle_data(self, data):
        if not self.skip:
            self.buffer.append(data)


def _html_document(raw, media_type):
    parser = _HTMLBlocks()
    parser.feed(_decode(raw, media_type))
    parser.close()
    parser.flush()
    records = parser.records
    if any(row[4] for row in records):
        records = [row for row in records if row[4]]
    elif any(row[5] for row in records):
        records = [row for row in records if row[5]]
    builder = _Builder()
    builder.limitations.append("HTML text extraction does not interpret images, scripts or supplementary files; anchor links are not verified bibliographic citations.")
    stack = [(0, builder.section("Document"))]
    for text, kind, level, links, *_ in records:
        if kind == "heading":
            while len(stack) > 1 and stack[-1][0] >= level:
                builder.close(stack.pop()[1])
            stack.append((level, builder.section(text, stack[-1][1])))
        builder.add(text, kind, stack[-1][1], source_locator=None)
    while stack:
        builder.close(stack.pop()[1])
    return builder


def _plain_document(raw, media_type):
    text = _decode(raw, media_type)
    builder = _Builder()
    section = builder.section("Document")
    for paragraph in re.split(r"\n\s*\n", text):
        builder.add(paragraph, "paragraph", section)
    builder.close(section)
    builder.limitations.append("Plain text has no independently verified article structure or original page coordinates.")
    return builder


def _poppler_document(raw, metadata):
    """Normalize Poppler bbox XHTML without losing PDF page/line/word locations."""
    if b"<!ENTITY" in raw.replace(b"\x00", b"").upper():
        raise DocumentError("unsafe_xml", "Embedded XML entities are not supported")
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, ValueError) as exc:
        raise DocumentError("invalid_xml", "Poppler did not return readable bbox XML") from exc
    if sum(1 for _ in root.iter()) > 100000:
        raise DocumentError("xml_capacity", "Poppler output exceeds the element capacity")
    pages = [node for node in root.iter() if _tag(node) == "page"]
    if not 1 <= len(pages) <= 1000:
        raise DocumentError("invalid_poppler_output", "Poppler output must contain one to 1000 pages")
    origin = metadata.get("derived_from", {})
    if (not isinstance(origin, dict) or not _DOC_ID.fullmatch(str(origin.get("document_id", "")))
            or not re.fullmatch(r"[0-9a-f]{64}", str(origin.get("source_sha256", "")))):
        raise DocumentError("invalid_poppler_output", "Poppler locations require an immutable PDF source binding")
    builder = _Builder()
    builder.limitations.append("Native PDF text only; no OCR, semantic table/formula reconstruction or citation linking. Word boxes use PDF points from the top-left; visual content requires page rendering.")

    def number(node, key):
        try:
            value = float(node.attrib[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise DocumentError("invalid_poppler_output", "Missing or invalid PDF coordinates") from exc
        if not math.isfinite(value) or abs(value) > 100000:
            raise DocumentError("invalid_poppler_output", "PDF coordinates exceed supported bounds")
        return value

    def bbox(node):
        box = [number(node, key) for key in ("xMin", "yMin", "xMax", "yMax")]
        if box[0] > box[2] or box[1] > box[3]:
            raise DocumentError("invalid_poppler_output", "PDF coordinate bounds are reversed")
        return box

    for page_number, page in enumerate(pages, 1):
        width, height = number(page, "width"), number(page, "height")
        if width <= 0 or height <= 0:
            raise DocumentError("invalid_poppler_output", "PDF page dimensions must be positive")
        section = builder.section(f"Page {page_number}")
        word_count = 0
        for line_number, line in enumerate((node for node in page.iter() if _tag(node) == "line"), 1):
            words, parts, offset = [], [], 0
            for word in (node for node in line if _tag(node) == "word"):
                text = _norm("".join(word.itertext()))
                if not text:
                    continue
                if parts:
                    offset += 1
                words.append({"text": text, "bbox": bbox(word), "char_range": [offset, offset + len(text)]})
                parts.append(text)
                offset += len(text)
            if not words:
                continue
            word_count += len(words)
            builder.add(" ".join(parts), "pdf_line", section, source_locator={
                "page": page_number, "line": line_number, "bbox": bbox(line), "words": words,
                "page_width": width, "page_height": height, "coordinate_system": "pdf_points_top_left",
                "source_document_id": origin["document_id"], "source_sha256": origin["source_sha256"]})
        builder.close(section)
        builder.pages.append({"page": page_number, "width": width, "height": height,
                              "word_count": word_count, "has_text": bool(word_count), "locator": section["locator"]})
        if not word_count:
            builder.extraction_status = "partial"
            builder.limitations.append(f"Page {page_number} has no extractable text; it may contain scans, graphics or be blank. Render it for visual review.")
    return builder


def _pdf_document(raw):
    try:
        import pypdf
    except ImportError as exc:
        raise DocumentError("dependency_missing", "PDF extraction requires optional pypdf; no PDF text, OCR or image reading was performed") from exc
    builder = _Builder()
    builder.limitations.append("pypdf extracts the text layer only; no OCR, figure interpretation or verified table reconstruction. Empty pages may contain unread images.")
    try:
        reader = pypdf.PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            raise DocumentError("encrypted_pdf", "Encrypted PDF content is unavailable")
        for i, page in enumerate(reader.pages):
            section = builder.section(f"Page {i + 1}")
            text = page.extract_text() or ""
            for paragraph in re.split(r"\n\s*\n", text):
                builder.add(paragraph, "paragraph", section, source_locator=f"page:{i + 1}")
            builder.close(section)
            builder.pages.append({"page": i + 1, "has_text": bool(text.strip()), "locator": section["locator"]})
            if not text.strip():
                builder.extraction_status = "partial"
                builder.limitations.append(f"Page {i + 1} has no extractable text layer.")
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError("invalid_pdf", "PDF text extraction failed") from exc
    builder.pdf_parser_version = str(pypdf.__version__)
    return builder


class DocumentStore:
    """Host-configured content store; instantiate with an explicit local root."""

    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        key_path = self.root / ".cursor_key"
        try:
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(os.urandom(32))
        if key_path.is_symlink() or not key_path.is_file():
            raise DocumentError("store_integrity", "Cursor key is not a regular local file")
        self._cursor_key = key_path.read_bytes()
        if len(self._cursor_key) != 32:
            raise DocumentError("store_integrity", "Cursor key is invalid")

    def _directory(self, document_id):
        if not isinstance(document_id, str) or not _DOC_ID.fullmatch(document_id):
            raise DocumentError("invalid_document_id", "Use a registered document hash identifier")
        path = self.root / document_id
        if path.is_symlink():
            raise DocumentError("store_integrity", "Document directories cannot be symbolic links")
        return path

    def ingest(self, raw_bytes, media_type, source_metadata):
        """Archive supplied bytes and parsed text, returning an immutable manifest."""
        if not isinstance(raw_bytes, bytes) or len(raw_bytes) > MAX_SOURCE_BYTES:
            raise DocumentError("invalid_source", "Supply bytes within the 32 MiB source capacity")
        if not isinstance(media_type, str) or not 1 <= len(media_type) <= 200:
            raise DocumentError("invalid_media_type", "An explicit bounded media type is required")
        if not isinstance(source_metadata, dict):
            raise DocumentError("invalid_metadata", "Source metadata must be a JSON object")
        try:
            metadata_bytes = _json(source_metadata)
        except (TypeError, ValueError, UnicodeError) as exc:
            raise DocumentError("invalid_metadata", "Source metadata must be finite JSON") from exc
        if len(metadata_bytes) > MAX_OUTPUT_BYTES:
            raise DocumentError("invalid_metadata", "Source metadata exceeds 64 KiB")
        metadata = json.loads(metadata_bytes)
        declared_scope = metadata.get("content_scope", "unknown")
        if not isinstance(declared_scope, str) or declared_scope not in _SCOPES:
            raise DocumentError("invalid_scope", "Unsupported content_scope")
        base_type = media_type.split(";", 1)[0].strip().lower()
        builder, reason = None, None
        try:
            if base_type == POPPLER_MEDIA_TYPE:
                builder = _poppler_document(raw_bytes, metadata)
            elif base_type in {"application/xml", "text/xml", "application/jats+xml"}:
                builder = _xml_document(raw_bytes)
            elif base_type in {"text/html", "application/xhtml+xml"}:
                builder = _html_document(raw_bytes, media_type)
            elif base_type in {"text/plain", "text/markdown"}:
                builder = _plain_document(raw_bytes, media_type)
            elif base_type == "application/pdf":
                builder = _pdf_document(raw_bytes)
            else:
                raise DocumentError("unsupported_media_type", "No parser is registered for this media type")
        except DocumentError as exc:
            reason = {"code": exc.code, "message": str(exc)}
            builder = _Builder()
        parser_version = PARSER_VERSION + (";pypdf=" + builder.pdf_parser_version if hasattr(builder, "pdf_parser_version") else "")
        normalized = builder.finish()
        text_bytes = normalized.encode("utf-8")
        content_scope = builder.scope if builder.scope != "unknown" else declared_scope
        # Explicitly supplied excerpts/abstracts remain excerpts/abstracts even
        # when their transport container happens to contain a JATS body node.
        if declared_scope in {"abstract", "excerpt", "metadata_only"}:
            content_scope = declared_scope
        identity = {"source_sha256": _sha(raw_bytes), "parser_version": parser_version,
                    "media_type": media_type, "metadata_sha256": _sha(metadata_bytes)}
        document_id = "doc_" + _sha(_json(identity))
        manifest = {"schema_version": "literature_document_v1", "document_id": document_id,
                    **identity, "source_metadata": metadata, "content_scope": content_scope,
                    "status": "available" if builder.blocks else "unavailable",
                    "reason": reason or (None if builder.blocks else {"code": "no_readable_text", "message": "Source contains no readable text"}),
                    "text_sha256": _sha(text_bytes), "text_bytes": len(text_bytes), "text_characters": len(normalized),
                    "source_artifact": {"name": "source.bin", "sha256": _sha(raw_bytes), "bytes": len(raw_bytes)},
                    "text_artifact": {"name": "text.txt", "sha256": _sha(text_bytes), "bytes": len(text_bytes)},
                    "coverage": {"content_scope": content_scope, "source_body_present": builder.source_body_present,
                                 "normalized_text_complete": reason is None and builder.extraction_status == "complete" and bool(builder.blocks),
                                 "extraction_status": builder.extraction_status if builder.blocks else "unavailable",
                                 "visual_content_interpreted": False, "supplementary_files_read": False,
                                 "limitations": list(dict.fromkeys(builder.limitations))},
                    "blocks": builder.blocks, "sections": builder.sections, "references": builder.references,
                    "pages": builder.pages}
        manifest["document_version"] = _sha(_json(manifest))
        with self._lock:
            path = self._directory(document_id)
            if path.exists():
                old, _ = self._load(document_id)
                if old != manifest:
                    raise DocumentError("store_integrity", "Stored parsing differs for the same document identity")
                return old
            staged = Path(tempfile.mkdtemp(prefix=".ingest-", dir=self.root))
            try:
                (staged / "source.bin").write_bytes(raw_bytes)
                (staged / "text.txt").write_bytes(text_bytes)
                (staged / "manifest.json").write_bytes(_json(manifest))
                try:
                    staged.rename(path)
                except FileExistsError:
                    old, _ = self._load(document_id)
                    if old != manifest:
                        raise DocumentError("store_integrity", "Concurrent ingestion produced a different manifest")
            finally:
                if staged.exists():
                    shutil.rmtree(staged)
        return manifest

    def _load(self, document_id):
        path = self._directory(document_id)
        if not path.is_dir():
            raise DocumentError("unknown_document", "Document is not registered in this store")
        for name in ("manifest.json", "source.bin", "text.txt"):
            if (path / name).is_symlink() or not (path / name).is_file():
                raise DocumentError("store_integrity", "Document artifacts must be regular local files")
        try:
            manifest = json.loads((path / "manifest.json").read_bytes())
            original = (path / "source.bin").read_bytes()
            text_bytes = (path / "text.txt").read_bytes()
            version = manifest["document_version"]
            identity = {key: manifest[key] for key in ("source_sha256", "parser_version", "media_type", "metadata_sha256")}
            if (manifest["document_id"] != document_id or "doc_" + _sha(_json(identity)) != document_id
                    or version != _sha(_json({k: v for k, v in manifest.items() if k != "document_version"}))
                    or _sha(_json(manifest["source_metadata"])) != manifest["metadata_sha256"]
                    or _sha(original) != manifest["source_sha256"] or len(original) != manifest["source_artifact"]["bytes"]
                    or _sha(text_bytes) != manifest["text_sha256"] or len(text_bytes) != manifest["text_bytes"]):
                raise DocumentError("store_integrity", "Document source, text or manifest hash does not match")
            text = text_bytes.decode("utf-8")
            if len(text) != manifest["text_characters"]:
                raise DocumentError("store_integrity", "Document text length does not match")
        except (KeyError, TypeError, json.JSONDecodeError, UnicodeError) as exc:
            raise DocumentError("store_integrity", "Document manifest is invalid") from exc
        return manifest, text

    def manifest(self, document_id):
        return self._load(document_id)[0]

    def _cursor(self, payload):
        raw = _json(payload)
        signature = hmac.new(self._cursor_key, raw, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(raw + signature).decode("ascii").rstrip("=")

    def _uncursor(self, token, manifest):
        try:
            if not isinstance(token, str) or not 1 <= len(token) <= 2000:
                raise ValueError()
            raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
            payload_bytes, signature = raw[:-32], raw[-32:]
            if not hmac.compare_digest(hmac.new(self._cursor_key, payload_bytes, hashlib.sha256).digest(), signature):
                raise ValueError()
            value = json.loads(payload_bytes)
            if (set(value) != {"document_id", "document_version", "start", "end", "offset", "locator"}
                    or value["document_id"] != manifest["document_id"] or value["document_version"] != manifest["document_version"]
                    or any(type(value[k]) is not int for k in ("start", "end", "offset"))
                    or not 0 <= value["start"] <= value["offset"] <= value["end"] <= manifest["text_characters"]):
                raise ValueError()
            return value
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise DocumentError("invalid_cursor", "Cursor does not bind this document, version and reading range") from exc

    @staticmethod
    def _passage(manifest, text, block, start, end):
        location = block["source_locator"]
        if isinstance(location, dict) and "words" in location:
            relative_start, relative_end = start - block["char_range"][0], end - block["char_range"][0]
            words = []
            for word in location["words"]:
                low, high = max(word["char_range"][0], relative_start), min(word["char_range"][1], relative_end)
                if low < high:
                    words.append({**word, "text": word["text"][low - word["char_range"][0]:high - word["char_range"][0]],
                                  "char_range": [low, high], "bbox_scope": "original_word"})
            location = {**location, "words": words, "bbox_scope": "original_line",
                        "word_char_ranges_relative_to": "block"}
        return {"text": text[start:end], "locator": block["locator"],
                "document_id": manifest["document_id"], "document_version": manifest["document_version"],
                "source_sha256": manifest["source_sha256"], "text_sha256": manifest["text_sha256"],
                "parser_version": manifest["parser_version"], "content_scope": manifest["content_scope"],
                "char_range": [start, end], "byte_range": [len(text[:start].encode("utf-8")), len(text[:end].encode("utf-8"))],
                "section": block["section"], "section_path": block["section_path"], "kind": block["kind"],
                "citation_ids": block["citation_ids"], "source_locator": location,
                "source_metadata": manifest["source_metadata"],
                "block_complete": [start, end] == block["char_range"]}

    def read(self, document_id, locator=None, cursor=None, max_bytes=DEFAULT_READ_BYTES):
        """Read a normalized source range; max_bytes bounds returned text bytes."""
        _limit(max_bytes)
        manifest, text = self._load(document_id)
        if manifest["status"] == "unavailable":
            return {"status": "unavailable", "document_id": document_id, "reason": manifest["reason"],
                    "passages": [], "text": "", "next_cursor": None, "coverage": manifest["coverage"]}
        if cursor is not None:
            selection = self._uncursor(cursor, manifest)
            if locator is not None and locator != selection["locator"]:
                raise DocumentError("invalid_cursor", "A continuation cannot change its selected locator")
        else:
            start, end = 0, len(text)
            if locator is not None:
                match = next((item for item in manifest["blocks"] + manifest["sections"] if item["locator"] == locator), None)
                if match is None:
                    raise DocumentError("unknown_locator", "Locator is outside this document")
                start, end = match["char_range"]
            selection = {"document_id": document_id, "document_version": manifest["document_version"],
                         "start": start, "end": end, "offset": start, "locator": locator}
        start, stop = selection["offset"], selection["end"]
        chunk = text[start:stop].encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
        end = start + len(chunk)
        passages = [self._passage(manifest, text, block, max(start, block["char_range"][0]), min(end, block["char_range"][1]))
                    for block in manifest["blocks"] if block["char_range"][0] < end and block["char_range"][1] > start]
        complete = end == stop
        return {"status": "complete" if complete and manifest["coverage"]["extraction_status"] == "complete" else "partial", "document_id": document_id,
                "document_version": manifest["document_version"], "text": chunk, "returned_bytes": len(chunk.encode("utf-8")),
                "char_range": [start, end], "selection": {"locator": selection["locator"], "char_range": [selection["start"], stop]},
                "passages": passages, "next_cursor": None if complete else self._cursor({**selection, "offset": end}),
                "coverage": {**manifest["coverage"], "selected_range_complete": complete,
                             "entire_normalized_text_returned": complete and start == 0 and stop == len(text)}}

    def find(self, document_ids, query, limit=5, max_bytes=DEFAULT_READ_BYTES):
        """Deterministic keyword ranking over registered extracted text only.

        max_bytes bounds the complete serialized response, including provenance.
        This is passage retrieval, not a scientific answer or an exhaustive review.
        """
        _limit(max_bytes)
        _limit(limit, "limit", 50, 1)
        if (not isinstance(document_ids, (list, tuple)) or not 1 <= len(document_ids) <= 50
                or any(not isinstance(value, str) for value in document_ids)
                or len(set(document_ids)) != len(document_ids)):
            raise DocumentError("invalid_documents", "Supply one to fifty distinct registered document IDs")
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise DocumentError("invalid_query", "Supply a nonempty query of at most 2000 characters")
        terms = list(dict.fromkeys(re.findall(r"\w+", query.casefold())))
        if not terms:
            raise DocumentError("invalid_query", "Query must contain searchable words")
        results, searched, unavailable = [], [], []
        for document_id in document_ids:
            manifest, text = self._load(document_id)
            if manifest["status"] != "available":
                unavailable.append({"document_id": document_id, "reason": manifest["reason"]})
                continue
            searched.append({"document_id": document_id, "document_version": manifest["document_version"],
                             "content_scope": manifest["content_scope"], "text_sha256": manifest["text_sha256"]})
            for block in manifest["blocks"]:
                folded = block["text"].casefold()
                hits = [term for term in terms if term in folded]
                if hits:
                    score = sum(folded.count(term) for term in hits)
                    results.append((len(hits), score, manifest, text, block, hits))
        results.sort(key=lambda row: (-row[0], -row[1], row[2]["document_id"], row[4]["char_range"][0]))
        response = {"status": "complete", "query": query, "search_method": "casefolded_keyword_substring",
                    "searched_documents": searched, "unavailable_documents": unavailable,
                    "total_matching_passages": len(results), "passages": [], "truncated": False,
                    "coverage": "Registered normalized text only; no external search or citation expansion was performed."}
        for _, score, manifest, text, block, hits in results[:limit]:
            start, end = block["char_range"]
            passage = {**self._passage(manifest, text, block, start, end), "score": score, "matched_terms": hits}
            candidate = {**response, "passages": response["passages"] + [passage]}
            if len(_json(candidate)) > max_bytes:
                # Map a folded match back to its original character offset;
                # e.g. the two letters in casefold('ß') still occupy one source
                # character. The returned text is always an exact source slice.
                folded_offset = min(block["text"].casefold().find(term) for term in hits)
                accumulated, original_offset = 0, 0
                for original_offset, character in enumerate(block["text"]):
                    accumulated += len(character.casefold())
                    if accumulated > folded_offset:
                        break
                anchor = start + original_offset
                window_start = max(start, anchor - 100)
                lo, hi, best = 0, end - window_start, None
                while lo <= hi:
                    n = (lo + hi) // 2
                    clipped = {**self._passage(manifest, text, block, window_start, window_start + n), "score": score,
                               "matched_terms": [term for term in hits if term in text[window_start:window_start + n].casefold()]}
                    trial = {**response, "passages": response["passages"] + [clipped], "status": "partial", "truncated": True}
                    if n and len(_json(trial)) <= max_bytes:
                        best, lo = clipped, n + 1
                    else:
                        hi = n - 1
                if best is not None and best["char_range"][1] > anchor:
                    response["passages"].append(best)
                response.update(status="partial", truncated=True)
                break
            response["passages"].append(passage)
        if unavailable or len(response["passages"]) < len(results):
            response.update(status="partial", truncated=response["truncated"] or len(response["passages"]) < len(results))
        if not searched:
            response["status"] = "unavailable"
        if len(_json(response)) > max_bytes:
            raise DocumentError("output_capacity", "max_bytes cannot hold the search provenance; increase it")
        return response

    def reading_package(self, document_id):
        """Return an index for deliberate reading, without claiming it was read."""
        manifest, _ = self._load(document_id)
        return {key: manifest[key] for key in ("status", "reason", "document_id", "document_version", "source_sha256",
                "text_sha256", "parser_version", "media_type", "source_metadata", "content_scope", "coverage",
                "text_bytes", "text_characters", "source_artifact", "text_artifact", "sections", "references")} | {
                    "operations": ["read", "find"] if manifest["status"] == "available" else [],
                    "pages": manifest.get("pages", []),
                    "citation_graph_available": False, "source_text_delivered": False}
