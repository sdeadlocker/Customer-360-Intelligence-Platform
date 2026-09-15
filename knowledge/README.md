# Knowledge source documents

Source documents for the retrieval layer (Phase 7): product catalog, lending and account policy,
KYC/AML and fraud procedure, offer terms and conditions, advisor playbooks and compliance
disclosure language. These are the ground truth for grounded answers — the ingestion pipeline
(`c360 ingest-knowledge`) chunks them into `data/knowledge.db` (FTS5 + `sqlite-vec`), and this
directory holds the originals so a chunk can always be traced back to the document and section it
came from.

## Layout

```
knowledge/
  manifest.json        one entry per document: id, title, domain, version, effective dates,
                       jurisdiction, access level, product/business-group tags, source path
  product_catalog/     product sheets (INTERNAL)
  policy/              lending and deposit policy (INTERNAL)
  procedure/           KYC/AML, fraud, dispute, delinquency procedure (RISK_ONLY)
  offer_terms/         offer and campaign terms and conditions (PUBLIC)
  playbook/            advisor conversation guides (INTERNAL)
  compliance/          disclosure language, advice restrictions (COMPLIANCE_ONLY)
```

## Document format

Each document is a Markdown file. Metadata lives in `manifest.json`, not in the file, so a document
body is pure content and the same body can be re-versioned without editing front matter. Headings
(`#`, `##`, `###`) drive structure-aware chunking (design §9.5): the chunker splits on heading
boundaries and never splits a rate schedule, fee table or eligibility list mid-structure.

## Versioning

A changed document becomes a **new version** with a new `manifest.json` entry sharing the same
`doc_id`; the prior version is retained (its `effective_to` is set) so retrieval can filter to the
version effective as of a query date (requirement 17.3). At least one document ships a superseded
prior version to exercise that path.

## Access levels

`PUBLIC` · `INTERNAL` · `RISK_ONLY` · `COMPLIANCE_ONLY`. Retrieval pre-filters on
`access_level IN principal.knowledge_levels` in SQL before ranking (design §9.4 step 2), so a
document a role cannot see never enters the candidate set and its existence is not revealed
(requirement 17.8).

## Adversarial content

Two documents contain instruction-like text embedded in their body, used by the Phase 11
knowledge-borne injection suite (requirement 19.6). Retrieval wraps every passage in a delimited
untrusted-reference block, so such text is treated as data and cannot alter agent behaviour
(requirement 17.11).
