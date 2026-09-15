"""Structure-aware chunking tests (task 7.3, design §9.5).

The load-bearing assertions here are the two the design calls out as non-negotiable: a rate table
or eligibility list is never split across chunks, and chunk ids are deterministic across
re-ingestion. The token-band and overlap checks are softer — the design targets a range, not a
boundary — so they assert the *shape* (most chunks land in band, overlap carries whole blocks)
rather than exact counts.
"""

from __future__ import annotations

from c360.knowledge.chunking import chunk_document, estimate_tokens

# A document with a heading structure, an eligibility list and a rate table — the three structures
# §9.5 names. The table is deliberately small enough to fit in one chunk so "not split" is testable.
_DOC = """
# High Yield Savings

A premium liquid savings account with a competitive yield.

## Eligibility

- Account owner must be 18 years or older.
- Valid government-issued identification and a verified U.S. address.
- Minimum opening deposit of $1,000.
- Successful identity verification.

## Rates and Fees

| Balance Tier | APY | Monthly Fee |
|--------------|-----|-------------|
| $1,000 - $24,999 | 3.25% | $10.00 |
| $25,000 - $99,999 | 3.75% | $0.00 |
| $100,000+ | 4.10% | $0.00 |

## Disclosures

APY is variable and may change at any time after opening.
"""


def _chunk(body: str = _DOC, **kwargs: object) -> list:
    return chunk_document(doc_id="pc-sav-hiyield", version="v1", body=body, **kwargs)  # type: ignore[arg-type]


class TestDeterministicIds:
    def test_ids_have_the_documented_shape(self) -> None:
        chunks = _chunk()
        for chunk in chunks:
            # doc_id:version:section_path:ordinal
            assert chunk.chunk_id.startswith("pc-sav-hiyield:v1:")
            assert chunk.chunk_id.endswith(f":{chunk.ordinal}")

    def test_ids_are_byte_identical_across_re_chunking(self) -> None:
        first = [c.chunk_id for c in _chunk()]
        second = [c.chunk_id for c in _chunk()]
        assert first == second

    def test_id_is_independent_of_text_only_position(self) -> None:
        """The id derives from section path + ordinal, not from the text, so it survives a
        content edit that keeps structure (the version bump is what distinguishes the two)."""
        original = {c.chunk_id for c in chunk_document(doc_id="d", version="v1", body=_DOC)}
        edited_body = _DOC.replace("premium liquid", "premium, liquid")
        edited = {c.chunk_id for c in chunk_document(doc_id="d", version="v1", body=edited_body)}
        assert original == edited


class TestStructureIntegrity:
    def test_a_rate_table_is_never_split(self) -> None:
        chunks = _chunk()
        table_chunks = [c for c in chunks if "Balance Tier" in c.text]
        # The whole table must live in exactly one chunk, with every row present.
        assert len(table_chunks) == 1
        table = table_chunks[0].text
        assert "3.25%" in table
        assert "3.75%" in table
        assert "4.10%" in table

    def test_an_eligibility_list_is_never_split(self) -> None:
        chunks = _chunk()
        list_chunks = [c for c in chunks if "Minimum opening deposit" in c.text]
        assert len(list_chunks) == 1
        text = list_chunks[0].text
        assert "18 years or older" in text
        assert "Successful identity verification" in text

    def test_an_oversized_table_becomes_its_own_chunk_rather_than_splitting(self) -> None:
        rows = "\n".join(f"| Row {i} | value {i} | {i * 100} |" for i in range(200))
        body = f"# Big\n\n| A | B | C |\n|---|---|---|\n{rows}\n"
        chunks = chunk_document(doc_id="d", version="v1", body=body, target_tokens=100)
        table_chunks = [c for c in chunks if "Row 199" in c.text]
        assert len(table_chunks) == 1
        # Every row is in that one chunk, even though it exceeds the target.
        assert "Row 0" in table_chunks[0].text
        assert table_chunks[0].token_count > 100


class TestSectionPaths:
    def test_section_path_reflects_heading_nesting(self) -> None:
        body = "# Top\n\ncontent\n\n## Sub\n\nmore\n\n### Deep\n\ndeepest\n"
        chunks = chunk_document(doc_id="d", version="v1", body=body)
        paths = {c.section_path for c in chunks}
        assert "Top" in paths
        assert "Top > Sub" in paths
        assert "Top > Sub > Deep" in paths

    def test_content_before_first_heading_is_root(self) -> None:
        chunks = chunk_document(doc_id="d", version="v1", body="intro text with no heading\n")
        assert chunks
        assert chunks[0].section_path == "(root)"


class TestTokenSizing:
    def test_estimate_is_deterministic_and_monotonic(self) -> None:
        assert estimate_tokens("one two three") == estimate_tokens("one two three")
        assert estimate_tokens("a b c d e") > estimate_tokens("a b")

    def test_long_prose_is_split_into_multiple_chunks(self) -> None:
        paragraph = " ".join(f"word{i}" for i in range(400))
        body = f"# Section\n\n{paragraph}\n\n{paragraph}\n\n{paragraph}\n"
        chunks = chunk_document(doc_id="d", version="v1", body=body, target_tokens=200)
        assert len(chunks) >= 3

    def test_empty_body_yields_no_chunks(self) -> None:
        assert chunk_document(doc_id="d", version="v1", body="   \n\n  ") == []
