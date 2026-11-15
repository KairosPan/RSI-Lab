"""Contract fixtures exercise native dispatch without third-party installations."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import venv

from rsi_literature.documents import DocumentStore
from rsi_literature.parsers import PAPER2SKILL_SCRIPT, _execute, _executable, run_parser
from rsi_literature.poppler import render_document_page


TEI = b'''<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader><fileDesc><titleStmt>
<title>Registered Paper</title></titleStmt></fileDesc><profileDesc><abstract><p>Abstract.</p></abstract></profileDesc></teiHeader>
<text><body><div><head>Results</head><p xml:id="p1" coords="1,10,20,30,40">Measured effect <ref type="bibr" target="#b0">[1]</ref>.</p></div></body>
<back><div><listBibl><biblStruct xml:id="b0"><analytic><title>Earlier paper</title></analytic><monogr><date>2020</date></monogr>
<idno type="DOI">10.1000/test</idno></biblStruct></listBibl></div></back></text></TEI>'''


BBOX = b'''<html xmlns="http://www.w3.org/1999/xhtml"><body><doc>
<page width="612" height="792"><flow><block><line xMin="10" yMin="20" xMax="100" yMax="30">
<word xMin="10" yMin="20" xMax="50" yMax="30">Measured</word>
<word xMin="60" yMin="20" xMax="100" yMax="30">12.5</word>
</line></block></flow></page><page width="612" height="792"/></doc></body></html>'''


def synthetic_pdf(second_page_text=False):
    """Two ordinary PDF pages; the second is optionally graphics-only."""
    streams = [b'BT /F1 12 Tf 40 140 Td (PAGE-ONE measurement 12.5) Tj ET',
               b'BT /F1 12 Tf 40 140 Td (PAGE-TWO measurement 8.0) Tj ET' if second_page_text
               else b'0.2 0.4 0.8 rg 40 40 120 80 re f']
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 5 0 R >> >> /Contents 6 0 R >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 5 0 R >> >> /Contents 7 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    objects += [b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream' for stream in streams]
    raw, offsets = b'%PDF-1.4\n', [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(raw)); raw += str(index).encode() + b' 0 obj\n' + obj + b'\nendobj\n'
    xref = len(raw)
    raw += b'xref\n0 8\n0000000000 65535 f \n'
    raw += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
    return raw + f'trailer\n<< /Size 8 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.store = DocumentStore(self.base / "store")
        self.raw = b"%PDF-1.4\nregistered fixture"
        with patch.dict(sys.modules, {"pypdf": None}):
            self.source = self.store.ingest(self.raw, "application/pdf", {"content_scope": "full_text",
                "paper_id": "doi:10.1000/source", "source_url": "https://example.org/source.pdf", "provider": "fixture"})

    def run_parser(self, provider, settings, effect="effect-1", document_id=None):
        return run_parser(provider, settings, self.store, document_id or self.source["document_id"], effect)

    def fixture_command(self, code):
        script = self.base / "native_fixture.py"
        script.write_text(code)
        return [sys.executable, str(script)]

    def poppler_settings(self, code=None):
        script = self.base / "poppler-fixture"
        if code is None:
            code = f'''import sys, pathlib
if "-bbox-layout" in sys.argv:
    assert sys.argv[sys.argv.index("-f") + 1] == "1"
    assert sys.argv[sys.argv.index("-l") + 1] == "2"
    assert pathlib.Path(sys.argv[-2]).read_bytes().startswith(b"%PDF-")
    pathlib.Path(sys.argv[-1]).write_bytes({BBOX!r})
else:
    print("Pages: 2\\nEncrypted: no")
'''
        script.write_text("#!" + sys.executable + "\n" + code)
        script.chmod(0o700)
        return {"pdfinfo_path": str(script), "pdftotext_path": str(script), "pdftoppm_path": str(script)}

    def paper_repository(self, script):
        repository = self.base / "paper2agent"
        path = repository / PAPER2SKILL_SCRIPT
        path.parent.mkdir(parents=True)
        path.write_text(script)
        (path.parent / "pdf_to_skill.py").write_text("# Fixture module identity\n")
        return {"repository": str(repository), "python": sys.executable}

    def test_grobid_uploads_verified_original_and_preserves_tei_citations(self):
        with patch("rsi_literature.parsers.remote.http_request", return_value={"status": 200, "body": TEI}) as request:
            result = self.run_parser("grobid", {"endpoint": "http://localhost:8070"})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["provenance"]["attempt_count"], 1)
        method, endpoint, headers, body, _, _ = request.call_args.args
        self.assertEqual((method, endpoint), ("POST", "http://localhost:8070/api/processFulltextDocument"))
        self.assertIn(self.raw, body)
        self.assertIn(b'name="consolidateHeader"\r\n\r\n0', body)
        self.assertIn(b'name="consolidateCitations"\r\n\r\n0', body)
        self.assertIn("multipart/form-data; boundary=", headers["Content-Type"])
        manifest = self.store.manifest(result["data"]["document_id"])
        self.assertEqual(manifest["references"][0]["identifiers"]["doi"], "10.1000/test")
        paragraph = next(b for b in manifest["blocks"] if b["citation_ids"])
        self.assertEqual(paragraph["citation_ids"], ["b0"])
        self.assertEqual(paragraph["source_locator"]["coords"], "1,10,20,30,40")
        self.assertEqual(manifest["source_metadata"]["derived_from"]["source_sha256"], self.source["source_sha256"])
        self.assertEqual(manifest["source_metadata"]["paper_id"], "doi:10.1000/source")
        self.assertEqual(self.store.manifest(self.source["document_id"]), self.source)

    def test_abstract_source_is_never_promoted_by_tei_body(self):
        with patch.dict(sys.modules, {"pypdf": None}):
            source = self.store.ingest(self.raw, "application/pdf", {"content_scope": "abstract"})
        with patch("rsi_literature.parsers.remote.http_request", return_value={"status": 200, "body": TEI}):
            result = self.run_parser("grobid", {"endpoint": "http://127.0.0.1:8070/api"}, document_id=source["document_id"])
        self.assertEqual(result["coverage"]["content_scope"], "abstract")

    def test_invalid_and_missing_configuration_dispatch_nothing(self):
        with patch("rsi_literature.parsers.remote.http_request") as request:
            for settings in ({}, {"endpoint": "http://not-local.example"}, {"endpoint": "https://user:secret@example.org"}):
                result = self.run_parser("grobid", settings)
                self.assertNotIn(result["status"], {"ok", "partial"})
                self.assertEqual(result["provenance"]["attempt_count"], 0)
            request.assert_not_called()
        result = self.run_parser("poppler", {"pdfinfo_path": "/not-installed/pdfinfo"})
        self.assertEqual(result["data"]["readiness"], "needs_configuration")
        self.assertEqual(result["status"], "unavailable")
        result = self.run_parser("paper2skill", {"repository": str(self.base), "python": sys.executable})
        self.assertEqual(result["errors"][0]["code"], "needs_configuration")

    def test_grobid_http_errors_and_non_tei_never_become_readable_documents(self):
        for status, data, code in [(204, b"", "no_parseable_document"), (503, b"", "service_busy"),
                                   (401, b"", "authorization_required"), (200, b"<html>Error</html>", "invalid_response"),
                                   (200, b'<!DOCTYPE TEI [<!ENTITY a "x">]><TEI>&a;</TEI>', "invalid_response")]:
            with self.subTest(code=code), patch("rsi_literature.parsers.remote.http_request", return_value={"status": status, "body": data}):
                result = self.run_parser("grobid", {"endpoint": "http://localhost:8070"})
                self.assertEqual(result["errors"][0]["code"], code)
                self.assertNotIn("document_id", result["data"])
                self.assertEqual(result["provenance"]["attempt_count"], 1)

    def test_tampered_source_and_model_paths_are_rejected_before_upload(self):
        with patch("rsi_literature.parsers.remote.http_request") as request:
            result = self.run_parser("grobid", {"endpoint": "http://localhost:8070"}, document_id="../../secret")
            self.assertEqual(result["errors"][0]["code"], "invalid_document_id")
            (self.store.root / self.source["document_id"] / "source.bin").write_bytes(b"tampered")
            result = self.run_parser("grobid", {"endpoint": "http://localhost:8070"})
            self.assertEqual(result["errors"][0]["code"], "store_integrity")
            request.assert_not_called()

    def test_poppler_preserves_coordinates_partial_pages_and_source_binding(self):
        settings = self.poppler_settings()
        result = self.run_parser("poppler", settings)
        self.assertEqual(result["status"], "partial", result)
        self.assertEqual(result["provenance"]["attempt_count"], 2)
        self.assertEqual(result["coverage"]["unread_pages"], [2])
        self.assertFalse(result["coverage"]["normalized_text_complete"])
        derived = result["data"]["document_id"]
        read = self.store.read(derived)
        self.assertEqual(read["status"], "partial")
        location = read["passages"][0]["source_locator"]
        self.assertEqual(location["page"], 1)
        self.assertEqual(location["source_sha256"], self.source["source_sha256"])
        self.assertEqual(location["words"][1]["bbox"], [60, 20, 100, 30])
        self.assertEqual(self.store.find([derived], "12.5")["passages"][0]["source_locator"]["page"], 1)
        self.assertEqual(list((self.store.root / "tmp/pdfs").iterdir()), [])
        repeated = self.run_parser("poppler", settings)
        self.assertEqual(repeated["errors"][0]["code"], "effect_already_exists")
        self.assertEqual(repeated["provenance"]["attempt_count"], 0)

    def test_removed_mineru_never_dispatches(self):
        result = self.run_parser("mineru", {"command": [sys.executable]})
        self.assertEqual(result["errors"][0]["code"], "unsupported_provider")
        self.assertEqual(result["provenance"]["attempt_count"], 0)

    def test_host_virtual_environment_is_preserved_during_native_dispatch(self):
        environment = self.base / "host-venv"
        venv.EnvBuilder(with_pip=False).create(environment)
        command = self.fixture_command('import pathlib, sys; assert pathlib.Path(sys.prefix).resolve() == pathlib.Path(sys.argv[1]).resolve()')
        provenance = {"attempt_count": 0}
        _execute(_executable([str(environment / "bin/python"), command[1], str(environment)]), self.base, {}, provenance)
        self.assertEqual(provenance["attempt_count"], 1)

    def test_poppler_rejects_missing_oversize_and_symlink_outputs(self):
        for index, action in enumerate(["pass", 'pathlib.Path(sys.argv[-1]).symlink_to(sys.argv[-2])',
                                       'pathlib.Path(sys.argv[-1]).write_bytes(b"x" * 2048)']):
            code = 'import pathlib, sys\nif "-bbox-layout" in sys.argv:\n    ' + action + '\nelse:\n    print("Pages: 2")\n'
            result = self.run_parser("poppler", {**self.poppler_settings(code), "max_output_bytes": 1024}, effect=f"effect-{index}")
            self.assertEqual(result["status"], "failed", result)
            self.assertIn(result["errors"][0]["code"], {"missing_artifact", "invalid_artifact", "output_limit"})
            self.assertEqual(list((self.store.root / "tmp/pdfs").iterdir()), [])

    def test_parser_timeout_and_log_limit_are_bounded(self):
        result = self.run_parser("poppler", {**self.poppler_settings("import time; time.sleep(20)"), "timeout": 1})
        self.assertEqual(result["errors"][0]["code"], "parser_timeout")
        result = self.run_parser("poppler", {**self.poppler_settings('print("x" * 3000)'), "max_log_bytes": 1024}, effect="effect-2")
        self.assertEqual(result["errors"][0]["code"], "output_limit")
        self.assertEqual(list((self.store.root / "tmp/pdfs").iterdir()), [])

    def test_effect_path_traversal_and_symlink_jobs_are_rejected(self):
        settings = self.poppler_settings("raise RuntimeError('must not run')")
        result = self.run_parser("poppler", settings, effect="../escape")
        self.assertEqual(result["errors"][0]["code"], "invalid_effect_id")
        (self.store.root / "jobs").symlink_to(self.base)
        result = self.run_parser("poppler", settings)
        self.assertEqual(result["errors"][0]["code"], "invalid_artifact")
        self.assertEqual(result["provenance"]["attempt_count"], 0)

    def test_render_rejects_paths_out_of_range_and_invalid_limits(self):
        settings = self.poppler_settings()
        for inputs, code in [({"document_id": "../../outside", "page": 1}, "invalid_document_id"),
                             ({"document_id": self.source["document_id"], "page": 1, "path": "/tmp/x"}, "invalid_input"),
                             ({"document_id": self.source["document_id"], "page": True}, "invalid_configuration"),
                             ({"document_id": self.source["document_id"], "page": 1, "dpi": 201}, "invalid_input")]:
            result = render_document_page(settings, inputs, self.store, "invalid-render")
            self.assertEqual(result["errors"][0]["code"], code)
            self.assertEqual(result["provenance"]["attempt_count"], 0)
        result = render_document_page(settings, {"document_id": self.source["document_id"], "page": 3}, self.store, "outside-page")
        self.assertEqual(result["errors"][0]["code"], "page_out_of_range")
        self.assertEqual(result["provenance"]["attempt_count"], 1)
        self.assertNotIn("image", result["data"])

    def test_render_rejects_forged_derivation_and_tampered_original(self):
        settings = self.poppler_settings()
        derived = self.store.ingest(b"derived", "text/plain", {"derived_from": {
            "document_id": self.source["document_id"], "source_sha256": "0" * 64,
            "document_version": self.source["document_version"]}})
        result = render_document_page(settings, {"document_id": derived["document_id"], "page": 1}, self.store, "forged")
        self.assertEqual(result["errors"][0]["code"], "store_integrity")
        self.assertEqual(result["provenance"]["attempt_count"], 0)
        (self.store.root / self.source["document_id"] / "source.bin").write_bytes(b"tampered")
        result = render_document_page(settings, {"document_id": self.source["document_id"], "page": 1}, self.store, "tampered")
        self.assertEqual(result["errors"][0]["code"], "store_integrity")
        self.assertEqual(result["provenance"]["attempt_count"], 0)

    def test_poppler_dimension_guard_rejects_cli_resizing_before_dispatch(self):
        for dimension in (1601, 2000):
            with self.subTest(dimension=dimension):
                result = render_document_page({"max_dimension": dimension},
                    {"document_id": self.source["document_id"], "page": 1}, self.store, "oversized-render")
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["errors"][0]["code"], "invalid_configuration")
                self.assertIn("1600", result["errors"][0]["detail"])
                self.assertEqual(result["provenance"]["attempt_count"], 0)
        self.assertFalse((self.store.root / "jobs").exists())

    @unittest.skipUnless(all(shutil.which(name) for name in ("pdfinfo", "pdftotext", "pdftoppm")), "system Poppler is not installed")
    def test_real_poppler_text_graphics_page_coordinates_and_bounded_render(self):
        source = self.store.ingest(synthetic_pdf(), "application/pdf", {"content_scope": "full_text"})
        parsed = self.run_parser("poppler", {}, document_id=source["document_id"])
        self.assertEqual(parsed["status"], "partial", parsed)
        self.assertEqual(parsed["coverage"]["unread_pages"], [2])
        self.assertEqual(parsed["provenance"]["attempt_count"], 2)
        derived = parsed["data"]["document_id"]
        match = self.store.find([derived], "12.5")["passages"][0]
        self.assertEqual(match["source_locator"]["page"], 1)
        self.assertTrue(any(w["text"] == "12.5" for w in match["source_locator"]["words"]))
        self.assertEqual(self.store.read(derived, locator=match["locator"])["text"], match["text"])
        for identifier, effect, page in [(source["document_id"], "render-original", 1), (derived, "render-derived", 2)]:
            result = render_document_page({}, {"document_id": identifier, "page": page}, self.store, effect)
            self.assertEqual(result["status"], "ok", result)
            self.assertEqual(result["provenance"]["attempt_count"], 2)
            image = result["data"]["image"]
            self.assertEqual(image["source_sha256"], source["source_sha256"])
            self.assertEqual(image["page"], page)
            self.assertLessEqual(max(image["width"], image["height"]), 1600)
            self.assertLessEqual(image["bytes"], 512 * 1024)
            self.assertEqual(hashlib.sha256(Path(image["artifact_path"]).read_bytes()).hexdigest(), image["sha256"])
            self.assertFalse(result["coverage"]["visual_content_interpreted"])
        oversized = render_document_page({"max_image_bytes": 1024}, {"document_id": derived, "page": 1}, self.store, "render-too-large")
        self.assertEqual(oversized["errors"][0]["code"], "output_limit")
        self.assertFalse((self.store.root / "jobs/render-too-large/page.png").exists())
        self.assertEqual(list((self.store.root / "tmp/pdfs").iterdir()), [])

    def test_paper2skill_only_prepares_extracts_and_preserves_unreviewed_coverage(self):
        settings = self.paper_repository('''import hashlib, json, pathlib, sys
args = sys.argv[1:]
stage = args[0]
assert stage in {"prepare", "extract"}
work = pathlib.Path(args[args.index("--work") + 1])
def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
if stage == "prepare":
    raw = pathlib.Path(args[1]).read_bytes()
    write(work / "inventory.json", {"sources": [{"id": "s001-source", "kind": "pdf", "sha256": hashlib.sha256(raw).hexdigest(), "snapshot": "originals/source.pdf"}]})
    (work / "originals").mkdir(); (work / "originals/source.pdf").write_bytes(raw)
else:
    assert args[args.index("--ocr") + 1] == "never"
    source = json.loads((work / "inventory.json").read_text())["sources"][0]
    root = work / "documents/s001-source"
    write(root / "plan.json", {"source": {"sha256": source["sha256"], "normalized_sha256": source["sha256"], "pages": 2},
         "pages": [{"page": 1, "file": "pages/page-0001.json"}, {"page": 2, "file": "pages/page-0002.json"}]})
    (root / "source.pdf").write_bytes((work / "originals/source.pdf").read_bytes())
    write(root / "pages/page-0001.json", {"page": 1, "mode": "native", "reviewed": False,
         "items": [{"id": "p0001-b000", "kind": "text", "markdown": "Observed result."}]})
    write(root / "pages/page-0002.json", {"page": 2, "mode": "image", "reviewed": False,
         "items": [{"id": "p0002-image", "kind": "figure", "markdown": ""}]})
''')
        result = self.run_parser("paper2skill", settings)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["provenance"]["attempt_count"], 2)
        self.assertEqual(result["coverage"]["review_status"], "needs_agent_review")
        self.assertEqual(result["coverage"]["unread_pages"], [2])
        self.assertEqual(result["coverage"]["content_scope"], "excerpt")
        derived = self.store.manifest(result["data"]["document_id"])
        self.assertTrue(any(b["source_locator"] == "pdf-page-1:p0001-b000" for b in derived["blocks"]))
        self.assertEqual(derived["source_metadata"]["parser_identity"]["stages"], ["prepare", "extract"])
        self.assertEqual(list(self.store.root.rglob("SKILL.md")), [])

    def test_paper2skill_wrong_source_or_out_of_scope_page_is_rejected(self):
        settings = self.paper_repository('''import json, pathlib, sys
args = sys.argv[1:]
work = pathlib.Path(args[args.index("--work") + 1]); work.mkdir(exist_ok=True)
(work / "inventory.json").write_text(json.dumps({"sources": [{"id": "../../outside", "kind": "pdf", "sha256": "wrong"}]}))
''')
        result = self.run_parser("paper2skill", settings)
        self.assertEqual(result["errors"][0]["code"], "source_mismatch")
        self.assertNotIn("document_id", result["data"])


if __name__ == "__main__":
    unittest.main()
