"""Source fidelity, bounded reading and document-scope regressions."""
import base64
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from rsi_literature.documents import DocumentError, DocumentStore, PARSER_VERSION, POPPLER_MEDIA_TYPE


JATS = b'''<?xml version="1.0"?><article><front><article-meta>
<title-group><article-title>Example study</article-title></title-group>
<abstract><p>Abstract claim only.</p></abstract></article-meta></front>
<body><sec id="methods"><title>Methods</title><p>We tested <italic>compound A</italic> in cells <xref ref-type="bibr" rid="R1">[1]</xref>.</p>
<sec><title>Dose</title><p>Concentration was 4 mg.</p></sec></sec>
<sec><title>Results</title><p>Observed effect was small.</p>
<table-wrap id="T1"><caption><p>Measured values</p></caption><table><tr><td>4</td><td>mg</td></tr></table></table-wrap>
<fig id="F1"><label>Figure 1</label><caption><p>A plot.</p></caption><graphic href="figure.png"/></fig></sec></body>
<back><ref-list><ref id="R1"><label>1</label><element-citation><article-title>Prior study</article-title><pub-id pub-id-type="doi">10.1/example</pub-id></element-citation></ref></ref-list></back></article>'''


class DocumentStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = DocumentStore(self.root)

    def ingest(self, text, scope="unknown"):
        return self.store.ingest(text.encode(), "text/plain", {"paper_id": "paper-1", "content_scope": scope})

    def test_jats_sections_citations_and_exact_source_ranges(self):
        manifest = self.store.ingest(JATS, "application/xml", {"provider": "test", "paper_id": "p1"})
        self.assertEqual(manifest["content_scope"], "full_text")
        package = self.store.reading_package(manifest["document_id"])
        methods = next(s for s in package["sections"] if s["title"] == "Methods")
        response = self.store.read(manifest["document_id"], locator=methods["locator"])
        self.assertIn("We tested compound A in cells [1].", response["text"])
        self.assertIn("Concentration was 4 mg.", response["text"])
        self.assertNotIn("Observed effect", response["text"])
        cited = next(p for p in response["passages"] if p["citation_ids"])
        self.assertEqual(cited["citation_ids"], ["R1"])
        self.assertEqual(package["references"][0]["identifiers"]["doi"], "10.1/example")
        normalized = (self.root / manifest["document_id"] / "text.txt").read_text()
        for passage in response["passages"]:
            a, b = passage["char_range"]
            self.assertEqual(passage["text"], normalized[a:b])
            a, b = passage["byte_range"]
            self.assertEqual(passage["text"].encode(), normalized.encode()[a:b])
        self.assertFalse(package["source_text_delivered"])
        self.assertFalse(package["coverage"]["visual_content_interpreted"])

    def test_minified_xml_table_cells_are_not_joined_as_numbers(self):
        manifest = self.store.ingest(JATS, "text/xml", {})
        table = next(b for b in manifest["blocks"] if b["kind"] == "table")
        self.assertIn("4 mg", table["text"])
        self.assertNotIn("values4mg", table["text"])

    def test_poppler_page_word_boxes_survive_utf8_paging_and_keyword_readback(self):
        with patch.dict("sys.modules", {"pypdf": None}):
            source = self.store.ingest(b"%PDF-fixture", "application/pdf", {"content_scope": "full_text"})
        metadata = {"content_scope": "full_text", "derived_from": {key: source[key] for key in
                    ("document_id", "document_version", "source_sha256")}}
        raw = '''<html><body><doc><page width="200" height="300"><flow><block>
<line xMin="10" yMin="20" xMax="90" yMax="30"><word xMin="10" yMin="20" xMax="40" yMax="30">测量😀</word>
<word xMin="50" yMin="20" xMax="90" yMax="30">12.5</word></line></block></flow></page>
<page width="200" height="300"/></doc></body></html>'''.encode()
        manifest = self.store.ingest(raw, POPPLER_MEDIA_TYPE, metadata)
        self.assertEqual(manifest["coverage"]["extraction_status"], "partial")
        self.assertFalse(manifest["coverage"]["normalized_text_complete"])
        self.assertEqual([p["has_text"] for p in manifest["pages"]], [True, False])
        parts, cursor = [], None
        while True:
            read = self.store.read(manifest["document_id"], max_bytes=4, cursor=cursor)
            self.assertEqual(read["status"], "partial")  # Blank page is never silently complete.
            parts.append(read["text"])
            for passage in read["passages"]:
                location = passage["source_locator"]
                self.assertEqual(location["page"], 1)
                self.assertEqual(location["source_sha256"], source["source_sha256"])
                self.assertTrue(all(word["char_range"][0] < passage["char_range"][1] and
                                    word["char_range"][1] > passage["char_range"][0] for word in location["words"]))
                self.assertTrue(all(word["text"] in passage["text"] for word in location["words"]))
            cursor = read["next_cursor"]
            if cursor is None:
                break
        self.assertEqual("".join(parts), "测量😀 12.5")
        passage = self.store.find([manifest["document_id"]], "12.5")["passages"][0]
        self.assertEqual(passage["source_locator"]["words"][1]["bbox"], [50, 20, 90, 30])
        self.assertEqual(self.store.read(manifest["document_id"], locator=passage["locator"])["text"], passage["text"])
        package = self.store.reading_package(manifest["document_id"])
        self.assertEqual(len(package["pages"]), 2)
        self.assertEqual(self.store.read(manifest["document_id"], locator=package["pages"][1]["locator"])["text"], "")

    def test_poppler_partial_word_coordinates_do_not_leak_unread_text(self):
        source = self.ingest("original")
        metadata = {"derived_from": {key: source[key] for key in ("document_id", "document_version", "source_sha256")}}
        raw = b'''<html><doc><page width="200" height="300"><flow><block>
<line xMin="10" yMin="20" xMax="100" yMax="30"><word xMin="10" yMin="20" xMax="100" yMax="30">SECRETTAIL</word></line>
</block></flow></page></doc></html>'''
        manifest = self.store.ingest(raw, POPPLER_MEDIA_TYPE, metadata)
        read = self.store.read(manifest["document_id"], max_bytes=4)
        self.assertEqual(read["text"], "SECR")
        location = read["passages"][0]["source_locator"]
        self.assertNotIn("ETTAIL", json.dumps(location))
        self.assertEqual(location["words"][0]["text"], "SECR")
        self.assertEqual(location["words"][0]["char_range"], [0, 4])
        self.assertEqual(location["words"][0]["bbox_scope"], "original_word")
        continued = self.store.read(manifest["document_id"], cursor=read["next_cursor"], max_bytes=4)
        self.assertEqual(continued["text"], "ETTA")
        self.assertEqual(continued["passages"][0]["source_locator"]["words"][0]["text"], "ETTA")

    def test_poppler_empty_pages_remain_archived_but_not_readable(self):
        source = self.ingest("original")
        metadata = {"derived_from": {key: source[key] for key in ("document_id", "document_version", "source_sha256")}}
        raw = b'<html><body><doc><page width="200" height="300"/></doc></body></html>'
        manifest = self.store.ingest(raw, POPPLER_MEDIA_TYPE, metadata)
        self.assertEqual(manifest["status"], "unavailable")
        self.assertFalse(manifest["coverage"]["normalized_text_complete"])
        self.assertEqual(manifest["pages"][0]["word_count"], 0)
        self.assertEqual(self.store.reading_package(manifest["document_id"])["pages"], manifest["pages"])
        self.assertEqual(self.store.read(manifest["document_id"])["status"], "unavailable")

    def test_poppler_rejects_unbound_or_nonfinite_coordinates(self):
        raw = b'<html><body><doc><page width="NaN" height="300"/></doc></body></html>'
        source = self.ingest("original")
        metadata = {"derived_from": {key: source[key] for key in ("document_id", "document_version", "source_sha256")}}
        for supplied in ({}, metadata):
            manifest = self.store.ingest(raw, POPPLER_MEDIA_TYPE, supplied)
            self.assertEqual(manifest["status"], "unavailable")
            self.assertEqual(manifest["reason"]["code"], "invalid_poppler_output")

    def test_abstract_scope_is_not_upgraded_by_transport_wrapper(self):
        manifest = self.store.ingest(JATS, "application/xml", {"content_scope": "excerpt"})
        self.assertEqual(manifest["content_scope"], "excerpt")
        abstract = self.store.ingest(b"<article><front><abstract><p>Only summary</p></abstract></front></article>", "application/xml", {})
        self.assertEqual(abstract["content_scope"], "abstract")
        title_only = self.store.ingest(b"<article><front><article-title>Title</article-title></front></article>", "application/xml", {})
        self.assertEqual(title_only["content_scope"], "metadata_only")

    def test_utf8_pagination_is_lossless_and_cursors_survive_restart(self):
        manifest = self.ingest("中文😀résumé\n\n尾巴𠮷")
        doc = manifest["document_id"]
        expected = self.store.read(doc)["text"]
        chunks, cursor = [], None
        for _ in range(100):
            # Reopening the store must not invalidate an already returned cursor.
            response = DocumentStore(self.root).read(doc, cursor=cursor, max_bytes=4)
            self.assertLessEqual(response["returned_bytes"], 4)
            self.assertTrue(response["text"])
            chunks.append(response["text"])
            cursor = response["next_cursor"]
            if cursor is None:
                self.assertEqual(response["status"], "complete")
                break
            self.assertEqual(response["status"], "partial")
        else:
            self.fail("Pagination did not terminate")
        self.assertEqual("".join(chunks), expected)

    def test_cursor_cannot_switch_document_version_or_reading_range(self):
        first = self.ingest("Long sentence one.\n\nSecond paragraph.")
        second = self.ingest("Different document")
        locator = first["blocks"][0]["locator"]
        cursor = self.store.read(first["document_id"], locator=locator, max_bytes=4)["next_cursor"]
        with self.assertRaises(DocumentError) as error:
            self.store.read(second["document_id"], cursor=cursor)
        self.assertEqual(error.exception.code, "invalid_cursor")
        with self.assertRaises(DocumentError):
            self.store.read(first["document_id"], locator=first["blocks"][1]["locator"], cursor=cursor)
        payload = bytearray(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        payload[10] ^= 1
        tampered = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        with self.assertRaises(DocumentError):
            self.store.read(first["document_id"], cursor=tampered)

    def test_unknown_document_path_and_locator_rejected(self):
        manifest = self.ingest("Text")
        for bad in ("../../secret", str(self.root / "source.bin"), "doc_" + "0" * 64):
            with self.subTest(bad=bad), self.assertRaises(DocumentError):
                self.store.read(bad)
        with self.assertRaises(DocumentError):
            self.store.read(manifest["document_id"], locator="block:999999")

    def test_symlink_artifacts_are_not_followed(self):
        manifest = self.ingest("Text")
        path = self.root / manifest["document_id"] / "text.txt"
        external = self.root / "other.txt"
        external.write_text("Text")
        path.unlink()
        path.symlink_to(external)
        with self.assertRaises(DocumentError) as error:
            self.store.read(manifest["document_id"])
        self.assertEqual(error.exception.code, "store_integrity")

    def test_ingest_is_idempotent_and_metadata_is_part_of_identity(self):
        first = self.ingest("Text", "abstract")
        self.assertEqual(first, self.ingest("Text", "abstract"))
        second = self.ingest("Text", "full_text")
        self.assertNotEqual(first["document_id"], second["document_id"])
        self.assertEqual(first["source_sha256"], second["source_sha256"])
        self.assertEqual(first["parser_version"], PARSER_VERSION)

    def test_source_and_text_tampering_are_detected_independently(self):
        for filename in ("source.bin", "text.txt", "manifest.json"):
            with self.subTest(filename=filename):
                manifest = self.ingest("Text for " + filename)
                path = self.root / manifest["document_id"] / filename
                if filename == "manifest.json":
                    body = json.loads(path.read_bytes())
                    body["content_scope"] = "full_text"
                    path.write_text(json.dumps(body))
                else:
                    path.write_bytes(b"changed")
                with self.assertRaises(DocumentError) as error:
                    self.store.read(manifest["document_id"])
                self.assertEqual(error.exception.code, "store_integrity")

    def test_search_reports_actual_scope_and_exact_excerpt(self):
        a = self.ingest("No relevant terms.\n\nMolecule showed binding in cells.", "abstract")
        b = self.ingest("Molecule binding binding measured.", "full_text")
        result = self.store.find([a["document_id"], b["document_id"]], "molecule binding", limit=1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["total_matching_passages"], 2)
        self.assertEqual(result["passages"][0]["document_id"], b["document_id"])
        self.assertEqual({x["content_scope"] for x in result["searched_documents"]}, {"abstract", "full_text"})
        passage = result["passages"][0]
        self.assertEqual(self.store.read(b["document_id"], locator=passage["locator"])["text"], passage["text"])

    def test_search_result_is_bounded_and_window_finds_late_unicode_hit(self):
        text = "前文 " * 2000 + "Straße TARGET结果 " + "后文 " * 2000
        manifest = self.ingest(text)
        result = self.store.find([manifest["document_id"]], "strasse", max_bytes=2200)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()), 2200)
        self.assertEqual(result["status"], "partial")
        self.assertIn("Straße", result["passages"][0]["text"])
        self.assertEqual(result["passages"][0]["matched_terms"], ["strasse"])
        self.assertFalse(result["passages"][0]["block_complete"])
        a, b = result["passages"][0]["char_range"]
        self.assertEqual(result["passages"][0]["text"], " ".join(text.split())[a:b])

    def test_search_does_not_hide_unavailable_documents(self):
        good = self.ingest("No match")
        bad = self.store.ingest(b"\xff", "text/plain", {})
        result = self.store.find([good["document_id"], bad["document_id"]], "missing")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["total_matching_passages"], 0)
        self.assertEqual(result["unavailable_documents"][0]["reason"]["code"], "invalid_encoding")
        self.assertEqual(self.store.find([bad["document_id"]], "missing")["status"], "unavailable")

    def test_html_prefers_article_and_ignores_active_and_navigation_content(self):
        html = b"<html><head><title>Site</title></head><body><p>Outside content</p><article><h1>Study</h1><p>A <em>relevant</em> finding.</p><script>injected</script><nav>navigation</nav><h2>Methods</h2><p>Measured carefully.</p></article></body></html>"
        manifest = self.store.ingest(html, "text/html", {"content_scope": "full_text"})
        result = self.store.read(manifest["document_id"])
        self.assertIn("A relevant finding.", result["text"])
        self.assertNotIn("Outside content", result["text"])
        self.assertNotIn("injected", result["text"])
        self.assertNotIn("navigation", result["text"])
        self.assertTrue(any(s["title"] == "Methods" for s in manifest["sections"]))

    def test_entity_xml_multiarticle_and_invalid_input_are_explicit(self):
        cases = [(b'<!DOCTYPE a [<!ENTITY x "expanded">]><article>&x;</article>', "unsafe_xml"),
                 ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE a [<!ENTITY x "expanded">]><article>&x;</article>'.encode("utf-16"), "unsafe_xml"),
                 (b"<articles><article/><article/></articles>", "multiple_documents"),
                 (b"<broken>", "invalid_xml")]
        for raw, code in cases:
            with self.subTest(code=code):
                manifest = self.store.ingest(raw, "application/xml", {})
                self.assertEqual(manifest["status"], "unavailable")
                self.assertEqual(manifest["reason"]["code"], code)
                result = self.store.read(manifest["document_id"])
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["passages"], [])

    def test_pdf_missing_dependency_is_archived_but_not_claimed_readable(self):
        with patch.dict("sys.modules", {"pypdf": None}):
            manifest = self.store.ingest(b"%PDF-1.4 test", "application/pdf", {"content_scope": "full_text"})
        self.assertEqual(manifest["status"], "unavailable")
        self.assertEqual(manifest["reason"]["code"], "dependency_missing")
        self.assertFalse(manifest["coverage"]["normalized_text_complete"])
        self.assertFalse(self.store.reading_package(manifest["document_id"])["operations"])
        self.assertEqual((self.root / manifest["document_id"] / "source.bin").read_bytes(), b"%PDF-1.4 test")

    def test_pdf_text_layer_coverage_reports_unread_empty_pages(self):
        reader = SimpleNamespace(is_encrypted=False, pages=[SimpleNamespace(extract_text=lambda: "Page one text"),
                                                          SimpleNamespace(extract_text=lambda: "")])
        module = SimpleNamespace(__version__="test", PdfReader=lambda _: reader)
        with patch.dict("sys.modules", {"pypdf": module}):
            manifest = self.store.ingest(b"%PDF test", "application/pdf", {})
        self.assertEqual(manifest["status"], "available")
        self.assertEqual(manifest["coverage"]["extraction_status"], "partial")
        self.assertEqual(manifest["content_scope"], "unknown")
        self.assertIn("pypdf=test", manifest["parser_version"])
        self.assertFalse(manifest["coverage"]["visual_content_interpreted"])

    def test_invalid_limits_scope_and_search_capacity(self):
        manifest = self.ingest("Text")
        for limit in (True, 0, 3, 65537):
            with self.subTest(limit=limit), self.assertRaises(DocumentError):
                self.store.read(manifest["document_id"], max_bytes=limit)
        with self.assertRaises(DocumentError):
            self.store.find([manifest["document_id"]], "Text", max_bytes=4)
        with self.assertRaises(DocumentError):
            self.store.ingest(b"Text", "text/plain", {"content_scope": "verified_science"})


if __name__ == "__main__":
    unittest.main()
