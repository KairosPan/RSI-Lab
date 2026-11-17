"""Host-configured third-party extraction of registered immutable documents.

GROBID uses its fulltext REST endpoint. Poppler preserves native PDF text boxes
and provides bounded page rendering. Paper2Skill uses Paper2Agent's supported
``paper_bundle.py prepare/extract`` stages, never build or skill installation.
No command, repository, path, or endpoint is accepted from document content.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shutil
import signal
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET

from .documents import DocumentError, MAX_SOURCE_BYTES
from . import remote


ADAPTER_VERSION = "rsi-literature-parsers-v1"
PAPER2SKILL_SCRIPT = "skills/paper2agent/paper2skill/scripts/paper_bundle.py"
_EFFECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


class _ParserError(ValueError):
    def __init__(self, code, detail, status="failed", retryability="no"):
        super().__init__(detail)
        self.code, self.status, self.retryability = code, status, retryability


def _configuration(detail):
    raise _ParserError("needs_configuration", detail, "unavailable", "after_configuration")


def _number(settings, key, default, lower, upper):
    value = settings.get(key, default)
    if type(value) not in {int, float} or not lower <= value <= upper:
        raise _ParserError("invalid_configuration", f"{key} must be between {lower} and {upper}")
    return value


def _read_file(root, relative, maximum=MAX_SOURCE_BYTES):
    """Read only a regular, bounded artifact under the already selected root."""
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise _ParserError("invalid_artifact", "Invalid parser artifact path")
    rel = PurePosixPath(relative)
    if rel.is_absolute() or ".." in rel.parts or "." in rel.parts:
        raise _ParserError("invalid_artifact", "Parser artifact leaves its job directory")
    root = Path(root)
    if root.is_symlink():
        raise _ParserError("invalid_artifact", "Artifact roots cannot be symbolic links")
    path = root
    for part in rel.parts:
        path = path / part
        if path.is_symlink():
            raise _ParserError("invalid_artifact", "Parser artifacts cannot be symbolic links")
    if not path.is_file():
        raise _ParserError("missing_artifact", "The parser did not create its documented output")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise _ParserError("output_limit", "Parser artifact exceeds its byte limit")
    return raw


def _source(store, document_id):
    manifest = store.manifest(document_id)
    raw = _read_file(store.root, document_id + "/source.bin")
    if hashlib.sha256(raw).hexdigest() != manifest["source_sha256"]:
        raise _ParserError("store_integrity", "Immutable source changed before parser dispatch")
    return manifest, raw


def _job(store, effect_id):
    if not isinstance(effect_id, str) or not _EFFECT.fullmatch(effect_id):
        raise _ParserError("invalid_effect_id", "Use a bounded host effect identifier")
    jobs = store.root / "jobs"
    if jobs.is_symlink():
        raise _ParserError("invalid_artifact", "Job directory cannot be a symbolic link")
    jobs.mkdir(exist_ok=True)
    job = jobs / effect_id
    try:
        job.mkdir()
    except FileExistsError as exc:
        raise _ParserError("effect_already_exists", "Parser job already exists; use the host effect ledger to resume") from exc
    return job


def _executable(command):
    if (not isinstance(command, list) or not command or len(command) > 16
            or any(not isinstance(arg, str) or not arg or len(arg) > 4096 or "\x00" in arg for arg in command)):
        _configuration("Configure the parser command as a nonempty host-owned argv list")
    executable = shutil.which(command[0])
    if not executable:
        _configuration("The configured parser executable is not installed or executable")
    # A venv's python is commonly a symlink. Resolving that symlink silently
    # selects the base interpreter and loses the explicitly configured packages.
    return [os.path.abspath(executable), *command[1:]]


def _terminate(process):
    # Native parsers may spawn workers. A bounded job must terminate the group.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()
    process.wait()


def _execute(argv, job, settings, provenance, env=None, artifact_limits=None):
    timeout = _number(settings, "timeout", 120, 1, 1800)
    log_limit = int(_number(settings, "max_log_bytes", 65536, 1024, 1024 * 1024))
    try:
        process = subprocess.Popen(argv, cwd=job, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, env=env, start_new_session=True)
    except (FileNotFoundError, PermissionError) as exc:
        _configuration("The configured parser executable could not be started")
    provenance["attempt_count"] += 1
    output = bytearray()
    deadline = time.monotonic() + timeout
    def check_artifacts():
        for path, maximum in (artifact_limits or {}).items():
            if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_size > maximum)):
                raise _ParserError("output_limit", "Parser artifact exceeds its host byte budget")
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                check_artifacts()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _ParserError("parser_timeout", "Parser exceeded its host time budget", retryability="after_configuration")
                for key, _ in selector.select(min(remaining, 0.2)):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                    else:
                        output.extend(data)
                        if len(output) > log_limit:
                            raise _ParserError("output_limit", "Parser logs exceeded their byte budget")
            remaining = deadline - time.monotonic()
            try:
                code = process.wait(timeout=max(remaining, 0.001))
            except subprocess.TimeoutExpired as exc:
                raise _ParserError("parser_timeout", "Parser exceeded its host time budget") from exc
            check_artifacts()
    except BaseException:
        _terminate(process)
        raise
    finally:
        process.stdout.close()
    # Raw logs are deliberately not returned: third-party exceptions may print
    # host paths, API keys, or untrusted document instructions.
    if code:
        if re.search(rb"ModuleNotFoundError|ImportError|No module named|models? (?:not found|missing)|missing.*(?:model|dependenc)", output, re.I):
            _configuration("Parser dependencies/models are missing in its configured runtime")
        raise _ParserError("parser_exit", f"Native parser exited with status {code}", retryability="after_configuration")
    return bytes(output)


def _multipart(raw):
    boundary = "rsi-" + uuid.uuid4().hex
    parts = []
    fields = [("consolidateHeader", "0"), ("consolidateCitations", "0"), ("consolidateFunders", "0"),
              ("includeRawCitations", "1"), ("segmentSentences", "1")]
    fields += [("teiCoordinates", name) for name in ("ref", "biblStruct", "p", "s", "figure")]
    for name, value in fields:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="input"; filename="source.pdf"\r\nContent-Type: application/pdf\r\n\r\n'.encode())
    parts.extend((raw, f"\r\n--{boundary}--\r\n".encode()))
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


def _grobid(settings, raw, provenance):
    if not settings.get("endpoint"):
        _configuration("Configure a GROBID service endpoint")
    try:
        base = remote.validate_endpoint(settings["endpoint"]).rstrip("/")
    except (TypeError, ValueError) as exc:
        raise _ParserError("invalid_configuration", "GROBID requires HTTPS or an explicit localhost HTTP endpoint") from exc
    endpoint = base if base.endswith("/api/processFulltextDocument") else base + ("/processFulltextDocument" if base.endswith("/api") else "/api/processFulltextDocument")
    body, content_type = _multipart(raw)
    headers = {**remote.environment_headers(settings), "Content-Type": content_type, "Accept": "application/xml"}
    maximum = int(_number(settings, "max_response_bytes", 16 * 1024 * 1024, 1024, MAX_SOURCE_BYTES))
    timeout = _number(settings, "timeout", 120, 1, 1800)
    provenance["attempt_count"] += 1
    provenance["parser_identity"] = {"provider": "grobid", "entrypoint": "api/processFulltextDocument",
                                     "adapter_version": ADAPTER_VERSION, "runtime_version": settings.get("version", "unreported")}
    reply = remote.http_request("POST", endpoint, headers, body, timeout, maximum)
    status = reply["status"]
    if status in {401, 403}:
        raise _ParserError("authorization_required", "GROBID requires host authentication", "unavailable", "after_configuration")
    if status in {204, 400, 415, 422}:
        raise _ParserError("no_parseable_document", "GROBID could not extract this PDF", "unavailable")
    if status == 429 or status == 503:
        raise _ParserError("service_busy", "GROBID is busy; retry through the host budget", "unavailable", "yes")
    if status != 200:
        raise _ParserError("http_error", "GROBID returned HTTP " + str(status), retryability="unknown")
    result = reply["body"]
    if not isinstance(result, bytes) or len(result) > maximum:
        raise _ParserError("output_limit", "GROBID response exceeds its byte budget")
    if b"<!ENTITY" in result.replace(b"\x00", b"").upper():
        raise _ParserError("invalid_response", "GROBID returned unsupported XML entities")
    try:
        root = ET.fromstring(result)
    except (ET.ParseError, ValueError) as exc:
        raise _ParserError("invalid_response", "GROBID did not return valid TEI XML") from exc
    if root.tag.rsplit("}", 1)[-1] != "TEI":
        raise _ParserError("invalid_response", "GROBID response is not a TEI document")
    return result, "application/xml", {"review_status": "unreviewed", "ocr_performed": False,
            "limitations": ["GROBID extracts document structure and references; scanned-image content and visual accuracy are not verified."]}


def _json_artifact(root, relative):
    try:
        value = json.loads(_read_file(root, relative))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise _ParserError("invalid_artifact", "Parser JSON artifact is invalid") from exc
    if not isinstance(value, dict):
        raise _ParserError("invalid_artifact", "Parser JSON artifact must be an object")
    return value


def _paper2skill(settings, raw, store, effect_id, provenance):
    python = settings.get("python")
    command = _executable([python] if isinstance(python, str) else python)
    repository = settings.get("repository")
    if not isinstance(repository, str) or not Path(repository).expanduser().is_dir():
        _configuration("Configure an installed jmiao24/Paper2Agent repository")
    repository = Path(repository).expanduser().resolve()
    try:
        script_bytes = _read_file(repository, PAPER2SKILL_SCRIPT)
        engine_bytes = _read_file(repository, str(PurePosixPath(PAPER2SKILL_SCRIPT).parent / "pdf_to_skill.py"))
    except _ParserError as exc:
        if exc.code == "missing_artifact":
            _configuration("The configured repository lacks the supported Paper2Skill scripts")
        raise
    script = repository / PAPER2SKILL_SCRIPT
    job = _job(store, effect_id)
    source, work = job / "source.pdf", job / "review"
    source.write_bytes(raw)
    provenance["parser_identity"] = {"provider": "paper2skill", "entrypoint": PAPER2SKILL_SCRIPT,
            "adapter_version": ADAPTER_VERSION, "script_sha256": hashlib.sha256(script_bytes).hexdigest(),
            "pdf_engine_sha256": hashlib.sha256(engine_bytes).hexdigest(), "stages": ["prepare", "extract"]}
    _execute([*command, str(script), "prepare", str(source), "--work", str(work), "--name", "registered-paper", "--main", str(source)],
             job, settings, provenance)
    _execute([*command, str(script), "extract", "--work", str(work), "--ocr", "never"], job, settings, provenance)
    inventory = _json_artifact(job, "review/inventory.json")
    sources = inventory.get("sources")
    if not isinstance(sources, list) or len(sources) != 1 or sources[0].get("kind") != "pdf":
        raise _ParserError("invalid_artifact", "Paper2Skill did not inventory the single registered PDF")
    source_record = sources[0]
    if source_record.get("sha256") != hashlib.sha256(raw).hexdigest():
        raise _ParserError("source_mismatch", "Paper2Skill source hash differs from the registered document")
    snapshot = source_record.get("snapshot")
    if not isinstance(snapshot, str) or hashlib.sha256(_read_file(job, "review/" + snapshot)).hexdigest() != source_record["sha256"]:
        raise _ParserError("source_mismatch", "Paper2Skill original snapshot differs from the registered document")
    source_id = source_record.get("id")
    if not isinstance(source_id, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", source_id):
        raise _ParserError("invalid_artifact", "Paper2Skill source ID is invalid")
    prefix = "review/documents/" + source_id + "/"
    plan = _json_artifact(job, prefix + "plan.json")
    pages = plan.get("pages")
    if (plan.get("source", {}).get("sha256") != source_record["sha256"] or not isinstance(pages, list)
            or not pages or len(pages) > 10000 or plan.get("source", {}).get("pages") != len(pages)):
        raise _ParserError("invalid_artifact", "Paper2Skill page plan is incomplete or belongs to another source")
    if hashlib.sha256(_read_file(job, prefix + "source.pdf")).hexdigest() != plan["source"].get("normalized_sha256"):
        raise _ParserError("source_mismatch", "Paper2Skill normalized PDF hash does not match its page plan")
    article = ET.Element("article")
    body = ET.SubElement(article, "body")
    unread_pages, warnings, text_bytes, item_ids = [], 0, 0, set()
    for index, entry in enumerate(pages, 1):
        if not isinstance(entry, dict) or entry.get("page") != index:
            raise _ParserError("invalid_artifact", "Paper2Skill page order is incomplete")
        state = _json_artifact(job, prefix + entry["file"])
        if state.get("page") != index or not isinstance(state.get("items"), list):
            raise _ParserError("invalid_artifact", "Paper2Skill page state differs from the plan")
        section = ET.SubElement(body, "sec", {"id": f"pdf-page-{index}"})
        ET.SubElement(section, "title").text = f"PDF page {index}"
        has_text = False
        for item in state["items"]:
            item_id = item.get("id")
            if not isinstance(item_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", item_id) or item_id in item_ids:
                raise _ParserError("invalid_artifact", "Paper2Skill item IDs must be unique and bounded")
            item_ids.add(item_id)
            text = item.get("markdown", "")
            if not isinstance(text, str):
                raise _ParserError("invalid_artifact", "Paper2Skill item text is invalid")
            if text.strip():
                has_text = True
                text_bytes += len(text.encode("utf-8"))
                if text_bytes > MAX_SOURCE_BYTES:
                    raise _ParserError("output_limit", "Paper2Skill extracted text exceeds its byte limit")
                # Include unreviewed omit suggestions too; no reading content is
                # silently removed merely because a classifier called it a footer.
                locator = f"pdf-page-{index}:" + item_id
                ET.SubElement(section, "p", {"id": locator}).text = text
        if not has_text or state.get("mode") == "image":
            unread_pages.append(index)
        warnings += len(state.get("warnings", []))
    if not text_bytes:
        raise _ParserError("no_readable_text", "Paper2Skill preserved images but returned no readable text", "unavailable")
    return ET.tostring(article, encoding="utf-8"), "application/jats+xml", {
        "review_status": "needs_agent_review", "pages_requested": len(pages), "unread_pages": unread_pages,
        "extractor_warning_count": warnings, "ocr_performed": False,
        "limitations": ["Paper2Skill prepare/extract ran without source or page review. No verified skill was built or installed.",
                        "Text items retain PDF page/item IDs; figures, formulas and tables still require visual review. OCR was disabled."]}


def run_parser(provider, settings, document_store, document_id, effect_id):
    """Return a ToolResult-shaped dict; configuration is supplied only by the host."""
    provenance = {"provider": provider, "source_document_id": document_id, "attempt_count": 0,
                  "adapter_version": ADAPTER_VERSION}
    result = {"status": "failed", "data": {}, "provenance": provenance,
              "coverage": {"scope": "registered_source", "source_text_delivered": False, "visual_content_interpreted": False},
              "continuation": {"next_cursor": None, "complete_for_query": False}, "errors": []}
    try:
        if not isinstance(provider, str) or provider not in {"grobid", "poppler", "paper2skill"}:
            raise _ParserError("unsupported_provider", "No native parser is registered for this provider", "unavailable")
        if not isinstance(settings, dict):
            raise _ParserError("invalid_configuration", "Parser settings must be a host configuration object")
        manifest, raw = _source(document_store, document_id)
        provenance["source_sha256"] = manifest["source_sha256"]
        if manifest["media_type"].split(";", 1)[0].strip().lower() != "application/pdf" or not raw.lstrip().startswith(b"%PDF-"):
            raise _ParserError("unsupported_source", "This connector currently accepts registered PDF sources", "unavailable")
        if provider == "grobid":
            extracted, media_type, coverage = _grobid(settings, raw, provenance)
        elif provider == "poppler":
            from .poppler import extract_document
            extracted, media_type, coverage = extract_document(settings, raw, document_store, effect_id, provenance)
        else:
            extracted, media_type, coverage = _paper2skill(settings, raw, document_store, effect_id, provenance)
        metadata = {key: value for key, value in manifest["source_metadata"].items() if key in {"paper_id", "source_url", "provider", "title"}}
        metadata.update(content_scope=manifest["content_scope"], derived_from={"document_id": document_id,
                        "document_version": manifest["document_version"], "source_sha256": manifest["source_sha256"]},
                        parser_identity=provenance["parser_identity"], extraction_coverage=coverage)
        if coverage.get("unread_pages"):
            metadata["content_scope"] = "excerpt"
        derived = document_store.ingest(extracted, media_type, metadata)
        result["coverage"].update(coverage)
        result["data"] = {"document_id": derived["document_id"], "document_version": derived["document_version"],
                          "source_document_id": document_id, "text_bytes": derived["text_bytes"],
                          "section_count": len(derived["sections"]), "reference_count": len(derived["references"]),
                          "pages": derived.get("pages", [])}
        if derived["status"] == "unavailable":
            result["coverage"].update(extraction_available=False, all_source_modalities_read=False,
                                      normalized_text_complete=False, review_required=True)
            result["continuation"]["next_operation"] = "render_document_page"
            raise _ParserError("no_readable_text", "Parser output was archived but has no readable text", "unavailable")
        result["status"] = "partial"  # Unreviewed extraction never certifies source fidelity.
        result["coverage"].update(content_scope=derived["content_scope"], extraction_available=True,
                                  normalized_text_complete=derived["coverage"]["normalized_text_complete"],
                                  all_source_modalities_read=False, review_required=True)
        result["continuation"] = {"next_cursor": None, "complete_for_query": True, "next_operation": "read_document",
                                  "document_id": derived["document_id"]}
    except _ParserError as exc:
        result["status"] = exc.status
        result["errors"].append({"code": exc.code, "detail": str(exc), "retryability": exc.retryability})
        if exc.code == "needs_configuration":
            result["data"]["readiness"] = "needs_configuration"
    except DocumentError as exc:
        result["errors"].append({"code": exc.code, "detail": str(exc), "retryability": "no"})
    except remote.RemoteError as exc:
        result["status"] = "unavailable"
        result["errors"].append({"code": exc.code, "detail": str(exc), "retryability": "unknown"})
    except (OSError, TypeError, ValueError, KeyError, AttributeError) as exc:
        result["errors"].append({"code": "invalid_parser_output", "detail": "Native parser output or runtime did not match its documented contract", "retryability": "unknown"})
    return result
