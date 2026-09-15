"""Structure-aware chunking (task 7.3, design §9.5).

A document is split on heading boundaries into chunks of roughly 400—600 tokens with about 15%
overlap, and — the rule that matters most — a rate schedule, fee table or eligibility list is never
split mid-structure. Design §9.5 is blunt about why: "a fragmented rate table is worse than no
retrieval, because it looks authoritative while omitting a row." The chunker treats a Markdown table
block or a contiguous bullet list as an atomic unit that either fits in a chunk whole or forms a
chunk of its own, even if that pushes it past the token target.

Deterministic chunk IDs
------------------------

Every chunk's id is ``<doc_id>:<version>:<section_path>:<ordinal>`` (design §9.3). The id is a pure
function of the document identity and the chunk's position in it, never of its text or of ingestion
order, so re-ingesting an unchanged document produces byte-identical ids — which is what lets a
Phase 11 evaluation fixture stay pinned to "chunk 47" across re-ingestion. The stability test in
:mod:`tests.test_chunking` asserts exactly that.

Token counting
--------------

Tokens are *estimated*, not computed with a model tokenizer. The target is a soft 400—600 band, not
a hard limit a provider enforces, and a deterministic estimate keeps chunk ids — which depend on how
the text divides into chunks — reproducible offline with no tokenizer dependency to pin. The
estimate is words plus a fraction for sub-word splitting, which tracks real tokenizer output closely
enough for a sizing decision (design §9.5 targets a range, not a boundary).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

# ---------------------------------------------------------------- token estimation

#: Rough tokens-per-word multiplier. Sub-word tokenizers split longer and punctuated words into more
#: than one token; 1.3 tracks Titan/BPE output within the tolerance a 400—600 sizing band needs.
_TOKENS_PER_WORD: Final = 1.3

_WORD_RE: Final = re.compile(r"\S+")


def estimate_tokens(text: str) -> int:
    """Estimate the token count of ``text``. Deterministic; see the module docstring for why."""
    words = len(_WORD_RE.findall(text))
    return int(words * _TOKENS_PER_WORD + 0.5)


# ---------------------------------------------------------------- block model


class _BlockKind(StrEnum):
    """The kind of a structural block, which decides whether it may be split."""

    HEADING = "heading"
    TABLE = "table"
    LIST = "list"
    PARAGRAPH = "paragraph"


@dataclass(frozen=True, slots=True)
class _Block:
    """A structural block of a document: a heading, a table, a list, or a paragraph.

    ``atomic`` blocks (tables and lists) are never split across chunks (design §9.5). ``heading``
    blocks carry the current section path forward but are emitted as text at the top of the section
    they open.
    """

    kind: _BlockKind
    text: str
    #: Heading level (1 for ``#``), else 0. Only meaningful for HEADING blocks.
    level: int = 0

    @property
    def atomic(self) -> bool:
        return self.kind in (_BlockKind.TABLE, _BlockKind.LIST)


@dataclass(frozen=True, slots=True)
class Chunk:
    """One chunk of a document, ready to be embedded and stored (design §9.3)."""

    chunk_id: str
    doc_id: str
    version: str
    section_path: str
    ordinal: int
    text: str
    token_count: int


# ---------------------------------------------------------------- block parsing

_HEADING_RE: Final = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_TABLE_ROW_RE: Final = re.compile(r"^\s*\|.*\|\s*$")
_LIST_ITEM_RE: Final = re.compile(r"^\s*([-*+]|\d+[.)])\s+\S")


def _parse_blocks(body: str) -> list[_Block]:
    """Parse a Markdown ``body`` into an ordered list of structural blocks.

    The parser is intentionally small: it recognises headings, pipe tables, bullet/ordered lists and
    everything else as paragraphs. That is enough for the corpus this project ships (design §14: the
    corpus is authored to be structurally realistic, with headings, eligibility lists and rate
    tables), and it keeps the "never split a table or list" guarantee expressible as "a table/list
    is one block".
    """
    lines = body.splitlines()
    blocks: list[_Block] = []
    index = 0
    total = len(lines)

    while index < total:
        line = lines[index]
        stripped = line.strip()

        if not stripped:
            index += 1
            continue

        heading = _HEADING_RE.match(line)
        if heading is not None:
            level = len(heading.group(1))
            blocks.append(_Block(_BlockKind.HEADING, heading.group(2).strip(), level=level))
            index += 1
            continue

        if _TABLE_ROW_RE.match(line):
            start = index
            while index < total and _TABLE_ROW_RE.match(lines[index]):
                index += 1
            blocks.append(_Block(_BlockKind.TABLE, "\n".join(lines[start:index])))
            continue

        if _LIST_ITEM_RE.match(line):
            start = index
            # A list runs until a blank line or a non-list, non-indented line. Indented
            # continuations of a list item stay in the list.
            while index < total:
                current = lines[index]
                if not current.strip():
                    break
                if _LIST_ITEM_RE.match(current) or current.startswith((" ", "\t")):
                    index += 1
                    continue
                break
            blocks.append(_Block(_BlockKind.LIST, "\n".join(lines[start:index])))
            continue

        # Paragraph: consecutive non-blank lines that are not another block kind.
        start = index
        while index < total:
            current = lines[index]
            if (
                not current.strip()
                or _HEADING_RE.match(current)
                or _TABLE_ROW_RE.match(current)
                or _LIST_ITEM_RE.match(current)
            ):
                break
            index += 1
        blocks.append(_Block(_BlockKind.PARAGRAPH, "\n".join(lines[start:index])))

    return blocks


# ---------------------------------------------------------------- section grouping


@dataclass(frozen=True, slots=True)
class _Section:
    """A heading and the blocks beneath it, the unit chunking splits on (design §9.5)."""

    path: str
    blocks: tuple[_Block, ...]


def _section_path(stack: list[tuple[int, str]]) -> str:
    """Render the heading stack into a ``' > '``-joined section path.

    The stack holds ``(level, text)`` for the headings currently in scope. Joining them gives a
    path like ``Rates and Fees`` at the top level or ``Eligibility > Business``, which is both what
    a citation shows the user and part of the deterministic chunk id.
    """
    return " > ".join(text for _level, text in stack) or "(root)"


def _group_sections(blocks: list[_Block]) -> list[_Section]:
    """Group blocks under their governing heading path.

    Content before the first heading is a ``(root)`` section, so a document with no headings still
    chunks. A heading closes any deeper or equal-level headings on the stack, mirroring how Markdown
    nesting reads.
    """
    sections: list[_Section] = []
    stack: list[tuple[int, str]] = []
    current: list[_Block] = []
    current_path = "(root)"

    def flush() -> None:
        nonlocal current
        if current:
            sections.append(_Section(current_path, tuple(current)))
            current = []

    for block in blocks:
        if block.kind is _BlockKind.HEADING:
            flush()
            while stack and stack[-1][0] >= block.level:
                stack.pop()
            stack.append((block.level, block.text))
            current_path = _section_path(stack)
            # The heading text itself leads the section, so a chunk carries its own title.
            current.append(block)
        else:
            current.append(block)

    flush()
    return sections


# ---------------------------------------------------------------- chunk assembly


def _pack_section(
    section: _Section,
    *,
    target_tokens: int,
    overlap_ratio: float,
) -> list[tuple[str, int]]:
    """Pack a section's blocks into ``(text, token_count)`` chunks.

    Blocks are accumulated until adding the next would exceed ``target_tokens``; then the current
    chunk is emitted and the next begins with an overlap of the tail of the previous one. An atomic
    block (table or list) that does not fit is emitted as its own chunk rather than split (design
    §9.5) — which is the one case a chunk legitimately exceeds the target.
    """
    chunks: list[tuple[str, int]] = []
    buffer: list[str] = []
    buffer_tokens = 0

    def emit() -> None:
        nonlocal buffer, buffer_tokens
        if buffer:
            text = "\n\n".join(buffer).strip()
            chunks.append((text, estimate_tokens(text)))
            buffer = []
            buffer_tokens = 0

    for block in section.blocks:
        block_tokens = estimate_tokens(block.text)

        if buffer and buffer_tokens + block_tokens > target_tokens:
            tail = _overlap_tail(buffer, overlap_ratio, target_tokens)
            emit()
            buffer = list(tail)
            buffer_tokens = sum(estimate_tokens(part) for part in buffer)

        if block.atomic and block_tokens > target_tokens:
            # An oversized table/list cannot be split; flush what precedes it and give it its own
            # chunk so it is never fragmented.
            emit()
            chunks.append((block.text.strip(), block_tokens))
            continue

        buffer.append(block.text)
        buffer_tokens += block_tokens

    emit()
    return chunks


def _overlap_tail(buffer: list[str], overlap_ratio: float, target_tokens: int) -> list[str]:
    """The trailing blocks of ``buffer`` to repeat at the head of the next chunk for continuity.

    Overlap is ~15% of the target (design §9.5), so a sentence split across a chunk boundary is
    still retrievable from either side. Whole blocks are carried, not partial text, so the overlap
    never itself fragments a structure.
    """
    if overlap_ratio <= 0:
        return []
    budget = int(target_tokens * overlap_ratio)
    tail: list[str] = []
    accumulated = 0
    for part in reversed(buffer):
        part_tokens = estimate_tokens(part)
        if accumulated + part_tokens > budget and tail:
            break
        tail.insert(0, part)
        accumulated += part_tokens
    return tail


def chunk_document(
    *,
    doc_id: str,
    version: str,
    body: str,
    target_tokens: int = 500,
    overlap_ratio: float = 0.15,
) -> list[Chunk]:
    """Split a document ``body`` into deterministic, structure-aware chunks (task 7.3).

    Args:
        doc_id: The document id (shared across versions).
        version: The document version.
        body: The raw Markdown body.
        target_tokens: Soft per-chunk token target (design §9.5: 400—600, default 500).
        overlap_ratio: Fraction of the target to overlap between adjacent chunks (~0.15).

    Returns:
        Chunks in document order. Chunk ids are ``doc_id:version:section_path:ordinal`` with
        ``ordinal`` restarting at 0 within each section, so the id is a pure function of position
        and re-ingestion is byte-stable.
    """
    sections = _group_sections(_parse_blocks(body))
    chunks: list[Chunk] = []
    for section in sections:
        packed = _pack_section(section, target_tokens=target_tokens, overlap_ratio=overlap_ratio)
        for ordinal, (text, token_count) in enumerate(packed):
            if not text:
                continue
            chunk_id = f"{doc_id}:{version}:{section.path}:{ordinal}"
            chunks.append(
                Chunk(
                    chunk_id=chunk_id,
                    doc_id=doc_id,
                    version=version,
                    section_path=section.path,
                    ordinal=ordinal,
                    text=text,
                    token_count=token_count,
                )
            )
    return chunks


__all__ = ["Chunk", "chunk_document", "estimate_tokens"]
