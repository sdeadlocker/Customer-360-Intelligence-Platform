"""Reading the ``knowledge/`` source corpus and its manifest (task 7.1 support for 7.2—7.5).

The corpus on disk is the source of truth: a ``manifest.json`` listing one entry per document
version, and a Markdown body per entry. This module parses the manifest into typed
:class:`CorpusDocument` records and reads each body, so ingestion works from validated data rather
than from raw JSON. A manifest entry that names a missing body, or a body with no manifest entry, is
an error surfaced here rather than a chunk silently missing at query time.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from c360.knowledge.models import KnowledgeDomain

#: Repository root: src/c360/knowledge/corpus.py -> knowledge -> c360 -> src -> backend -> root.
_PROJECT_ROOT: Final = Path(__file__).resolve().parents[4]


def _default_corpus_dir() -> Path:
    """Resolve the knowledge corpus directory.

    In a source checkout the `knowledge/` directory sits at the repository root, four parents up
    from this file. In a packaged/container install the source may live under a different prefix
    (e.g. a venv), so `parents[4]` no longer points at the repo root — it can resolve to `/`. To
    keep the corpus location correct in any layout, an explicit `C360_KNOWLEDGE_CORPUS_DIR` env var
    overrides the path-based default (the Docker image sets it to `/app/knowledge`).
    """
    override = os.environ.get("C360_KNOWLEDGE_CORPUS_DIR", "").strip()
    if override:
        return Path(override)
    return _PROJECT_ROOT / "knowledge"


#: Default corpus location, overridable via `C360_KNOWLEDGE_CORPUS_DIR` for non-checkout layouts.
DEFAULT_CORPUS_DIR: Final = _default_corpus_dir()

_MANIFEST_NAME: Final = "manifest.json"

_ACCESS_LEVELS: Final = frozenset({"PUBLIC", "INTERNAL", "RISK_ONLY", "COMPLIANCE_ONLY"})


class CorpusError(RuntimeError):
    """Raised when the corpus or manifest is malformed or inconsistent with the files on disk."""


@dataclass(frozen=True, slots=True)
class CorpusDocument:
    """One document version: its manifest metadata plus its body text.

    A frozen record so ingestion cannot mutate a document between reading and writing it. The
    ``body`` is the raw Markdown; the chunker turns it into chunks. ``adversarial`` is carried
    through for the Phase 11 knowledge-borne injection suite but has no effect on ingestion — an
    adversarial document is stored and retrieved exactly like any other, because the containment is
    at retrieval time (the delimited untrusted-reference block), not at ingestion.
    """

    doc_id: str
    title: str
    domain: KnowledgeDomain
    version: str
    effective_from: str
    effective_to: str | None
    jurisdiction: str | None
    access_level: str
    product_code: str | None
    business_group: str | None
    source_path: str
    body: str
    adversarial: bool = False


def load_corpus(corpus_dir: Path = DEFAULT_CORPUS_DIR) -> list[CorpusDocument]:
    """Read every document version listed in the manifest, with its body.

    Raises:
        CorpusError: the manifest is missing, is not valid JSON, has a malformed entry, names a
            body file that does not exist, or declares a duplicate ``(doc_id, version)``.
    """
    manifest_path = corpus_dir / _MANIFEST_NAME
    if not manifest_path.is_file():
        raise CorpusError(f"manifest not found at {manifest_path}")

    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CorpusError(f"manifest is not valid JSON: {exc}") from exc

    entries = raw.get("documents")
    if not isinstance(entries, list):
        raise CorpusError("manifest must have a 'documents' array")

    documents: list[CorpusDocument] = []
    seen: set[tuple[str, str]] = set()
    for index, entry in enumerate(entries):
        document = _parse_entry(entry, index, corpus_dir)
        key = (document.doc_id, document.version)
        if key in seen:
            raise CorpusError(f"duplicate document version: {document.doc_id}@{document.version}")
        seen.add(key)
        documents.append(document)

    if not documents:
        raise CorpusError("manifest declares no documents")
    return documents


def _parse_entry(entry: object, index: int, corpus_dir: Path) -> CorpusDocument:
    if not isinstance(entry, dict):
        raise CorpusError(f"manifest entry {index} is not an object")

    def require(field: str) -> str:
        value = entry.get(field)
        if not isinstance(value, str) or not value:
            raise CorpusError(f"manifest entry {index} is missing required string '{field}'")
        return value

    def optional(field: str) -> str | None:
        value = entry.get(field)
        if value is None:
            return None
        if not isinstance(value, str):
            raise CorpusError(f"manifest entry {index} field '{field}' must be a string or null")
        return value

    domain_raw = require("domain")
    try:
        domain = KnowledgeDomain(domain_raw)
    except ValueError as exc:
        raise CorpusError(f"manifest entry {index} has unknown domain '{domain_raw}'") from exc

    access_level = require("access_level")
    if access_level not in _ACCESS_LEVELS:
        raise CorpusError(f"manifest entry {index} has unknown access_level '{access_level}'")

    source_path = require("source_path")
    body_path = corpus_dir / source_path
    if not body_path.is_file():
        raise CorpusError(f"manifest entry {index} names a missing body file: {body_path}")

    return CorpusDocument(
        doc_id=require("doc_id"),
        title=require("title"),
        domain=domain,
        version=require("version"),
        effective_from=require("effective_from"),
        effective_to=optional("effective_to"),
        jurisdiction=optional("jurisdiction"),
        access_level=access_level,
        product_code=optional("product_code"),
        business_group=optional("business_group"),
        source_path=source_path,
        body=body_path.read_text(encoding="utf-8"),
        adversarial=bool(entry.get("adversarial", False)),
    )


def content_hash(text: str) -> str:
    """The SHA-256 hex digest of ``text``, used to detect an unchanged document or chunk."""
    import hashlib  # noqa: PLC0415 - local, single use

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = ["CorpusDocument", "CorpusError", "content_hash", "load_corpus"]
