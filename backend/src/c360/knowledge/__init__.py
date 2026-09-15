"""The knowledge base and RAG layer (Phase 7).

Institutional knowledge — product sheets, policy, procedure, offer terms, playbooks and compliance
language — lives in a *separate* SQLite database (``data/knowledge.db``) from the customer data, is
opened read-only at runtime, and is searched by a hybrid of FTS5 BM25 and ``sqlite-vec`` cosine
similarity fused with Reciprocal Rank Fusion (design §9).

The boundary this package enforces (design §9.1): retrieval supplies **rules, procedures, criteria
and language — never customer values**. A numeric or customer-specific claim resolves to the typed
fact layer, never to a retrieved passage; the Phase 8 claim validator makes that a hard gate.
"""
