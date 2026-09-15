"""Labelled retrieval pairs -- query → relevant document (task 11.1, 11.8; design §14.2).

The ``eval_retrieval`` dataset (design §14.2) is a set of labelled query → relevant chunk pairs
across all six knowledge domains. Unlike the customer ground truth, these labels do not come from
the generator -- the knowledge corpus is authored, not generated -- so they are curated here against
the corpus manifest. Each :class:`RetrievalLabel` names a natural query a role would ask and the
``doc_id`` that answers it; the retrieval scorer (dimension 12) checks that the retriever surfaces a
chunk of that document in its top-k.

Labels are asserted against the manifest at build time (:func:`retrieval_labels`), so a label naming
a document that has been renamed or removed fails loudly here rather than silently scoring zero
recall against a document that no longer exists. Attribution is at the *document* level: a chunk id
is ``doc_id:version:section_path:ordinal`` (task 7.3), so a retrieved chunk is "correct" for a label
when its document id matches, which is the granularity a citation is judged at.

Access level matters: a query labelled to a ``RISK_ONLY`` procedure is only expected to succeed for
a role entitled to that level. Each label carries the document's access level so the scorer can run
it under an appropriately-entitled principal and so the access-control expectation is explicit.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from c360.knowledge.corpus import DEFAULT_CORPUS_DIR, load_corpus
from c360.knowledge.models import KnowledgeDomain


@dataclass(frozen=True, slots=True)
class RetrievalLabel:
    """One labelled retrieval pair (design §14.2 ``eval_retrieval``).

    ``query`` is redaction-safe -- it carries only institutional qualifiers, never customer identity
    (requirement 17.9), because retrieval queries are stripped of PII before embedding. ``doc_id``
    is the relevant document; ``domain`` and ``access_level`` let the scorer run under the right
    entitlement and report per-domain recall.
    """

    query: str
    doc_id: str
    domain: KnowledgeDomain
    access_level: str


# The curated query set, one or more per domain, keyed to a document that answers it. Kept as data
# so the coverage across the six domains is reviewable in one place and the manifest assertion can
# verify every target exists.
_RAW_LABELS: tuple[tuple[str, str], ...] = (
    # ---------------------------------------------------------------- product_catalog
    ("What are the monthly fees and waiver conditions for everyday checking?", "pc-dda-everyday"),
    ("What is the interest rate and minimum balance on high yield savings?", "pc-sav-hiyield"),
    ("What term and early withdrawal penalty applies to the 12 month certificate?", "pc-cd-12m"),
    ("What are the eligibility requirements for the 30 year fixed mortgage?", "pc-mtg-30f"),
    ("What annual fee and rewards rate does the travel elite card carry?", "pc-cc-travel"),
    ("What is the management fee for the managed investment portfolio?", "pc-inv-managed"),
    # ---------------------------------------------------------------- policy
    ("What is the consumer lending policy on debt-to-income limits?", "pol-lending"),
    ("What approval authority is required to raise a credit limit?", "pol-credit-limit-authority"),
    ("How long before an account is treated as dormant?", "pol-dormant-account"),
    ("What are the funds availability holds on a deposited check?", "pol-funds-availability"),
    ("What KYC documents are required to onboard a new customer?", "pol-kyc-onboarding"),
    # ---------------------------------------------------------------- procedure (RISK_ONLY)
    ("What are the steps to file a suspicious activity report?", "proc-sar-filing"),
    (
        "How is a delinquent account treated at each days-past-due stage?",
        "proc-delinquency-treatment",
    ),
    ("What is the procedure to block and reissue a card after fraud?", "proc-card-fraud-block"),
    ("How should a politically exposed person review be escalated?", "proc-pep-review"),
    ("What are the timelines for resolving a transaction dispute?", "proc-dispute-resolution"),
    # ---------------------------------------------------------------- offer_terms (PUBLIC)
    ("What is the promotional rate and term on the home equity line offer?", "off-heloc-promo"),
    (
        "What bonus does the new checking account offer pay and how is it earned?",
        "off-new-checking-bonus",
    ),
    ("What is the special rate on the certificate offer?", "off-cd-special-rate"),
    (
        "What sign-up bonus does the travel card offer and what spend is required?",
        "off-travel-card-bonus",
    ),
    # ---------------------------------------------------------------- playbook (INTERNAL)
    ("How should an advisor open a retirement planning conversation?", "play-retirement-planning"),
    ("What talking points guide a mortgage refinance conversation?", "play-refinance"),
    ("How should an advisor approach a delinquency outreach call?", "play-delinquency-outreach"),
    ("What is the playbook for a first-time homebuyer conversation?", "play-first-home"),
    # ---------------------------------------------------------------- compliance (COMPLIANCE_ONLY)
    ("What disclosures are required when opening a deposit account?", "comp-deposit-disclosures"),
    (
        "What lending disclosures must be provided under TILA-style rules?",
        "comp-lending-disclosures",
    ),
    ("What are the investment advice suitability restrictions?", "comp-advice-suitability"),
    ("What privacy and information-sharing rules apply under GLBA?", "comp-privacy-glba"),
)


def retrieval_labels(corpus_dir: Path = DEFAULT_CORPUS_DIR) -> tuple[RetrievalLabel, ...]:
    """Build the labelled retrieval pairs, verifying every target document exists (task 11.8).

    Reads the corpus manifest so each label's domain and access level are taken from the document
    itself rather than transcribed, keeping the label and the corpus in step. A label naming a
    document not present in the manifest raises, so a corpus edit that drops a labelled document
    fails here rather than degrading recall silently.

    Raises:
        ValueError: a label names a ``doc_id`` that is not in the corpus.
    """
    corpus = load_corpus(corpus_dir)
    # A doc_id may have several versions; take the earliest effective_from as the canonical entry so
    # a superseded version does not shadow the current one when reading domain/access level.
    meta_by_id: dict[str, tuple[KnowledgeDomain, str]] = {}
    for document in corpus:
        if document.doc_id not in meta_by_id:
            meta_by_id[document.doc_id] = (document.domain, document.access_level)

    labels: list[RetrievalLabel] = []
    missing: list[str] = []
    for query, doc_id in _RAW_LABELS:
        meta = meta_by_id.get(doc_id)
        if meta is None:
            missing.append(doc_id)
            continue
        domain, access_level = meta
        labels.append(
            RetrievalLabel(query=query, doc_id=doc_id, domain=domain, access_level=access_level)
        )
    if missing:
        raise ValueError(f"retrieval labels name unknown documents: {sorted(set(missing))}")
    return tuple(labels)


__all__ = ["RetrievalLabel", "retrieval_labels"]
