"""Embedding generation for the knowledge layer (task 7.4, design §9.5, §8.5).

Retrieval's semantic half needs a vector per chunk at ingestion time and a vector per query at
search time. This module defines the port that produces them, two implementations, and the
content-hash cache that makes re-ingesting an unchanged corpus free.

The port, not the vendor
------------------------

:class:`EmbeddingProvider` is a narrow ``Protocol`` — ``embed(texts, dimensions) -> vectors`` — so
the chunker, the ingestion writer and the query redactor depend on the capability, not on Bedrock.
Two implementations satisfy it:

* :class:`MockEmbeddingProvider` derives a deterministic pseudo-embedding from a content hash. It is
  the default for CI, evaluation and offline development (design §8.5): retrieval is fully testable
  with no AWS account and no token spend, and — critically — the same text always embeds to the
  same vector, so a retrieval test is reproducible. Semantically similar texts do *not* land near
  each other, so the mock proves the *plumbing* (fusion, filtering, ranking mechanics), while
  relevance quality is a live-Bedrock concern measured in Phase 11.
* :class:`BedrockEmbeddingProvider` calls Amazon Titan Text Embeddings V2 through the Bedrock
  runtime client. It is imported lazily so this module — and therefore ingestion and the whole
  knowledge package — has no import-time dependency on ``boto3``; a deployment that has not
  installed the AWS SDK still runs the mock path. It arrives in full alongside the rest of the
  Bedrock integration in Phase 8; here it exists so task 7.4's contract is complete and so a live
  ingestion can produce real embeddings when credentials are present.

The content-hash cache
----------------------

:class:`EmbeddingCache` keys a vector by ``sha256(dimensions, model_id, text)``. Design §9.5:
"cached by content_hash, so re-ingesting an unchanged document costs nothing." The model id and
dimensions are in the key because the same text embedded by a different model or at a different
width is a different vector, and serving a stale one would silently corrupt retrieval after a
model switch.
"""

from __future__ import annotations

import hashlib
import struct
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from c360.core.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from c360.core.config import Settings

_logger = get_logger(__name__)

#: A single embedding vector.
Vector = list[float]


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Produces one vector per input text, all of the requested dimensionality.

    Implementations must be deterministic for a given ``(text, dimensions)`` within a process run so
    the content-hash cache is sound, and must return vectors in the same order as ``texts``.
    """

    model_id: str

    def embed(self, texts: Sequence[str], *, dimensions: int) -> list[Vector]:
        """Return one L2-usable vector per text, in input order."""
        ...


# ---------------------------------------------------------------- mock provider


class MockEmbeddingProvider:
    """Deterministic pseudo-embeddings from a content hash (design §8.5).

    Each vector is generated from a SHA-256 seed over the text so it is stable across runs and
    across processes, then L2-normalised so cosine distance behaves. This is enough to exercise the
    retrieval pipeline deterministically; it is not a semantic model, and retrieval *quality* is
    measured against live Bedrock in Phase 11, never against this.
    """

    #: A recognisable sentinel model id so a cached vector or a stored embedding is traceable to the
    #: mock rather than being mistaken for a Titan output.
    model_id = "mock-embed-v1"

    def embed(self, texts: Sequence[str], *, dimensions: int) -> list[Vector]:
        return [self._one(text, dimensions) for text in texts]

    @staticmethod
    def _one(text: str, dimensions: int) -> Vector:
        # Expand a SHA-256 digest into `dimensions` floats by hashing (text, block) per 8-float
        # block, unpacking each 32-bit word into [-1, 1). Deterministic and independent of platform
        # float formatting.
        values: list[float] = []
        block = 0
        while len(values) < dimensions:
            digest = hashlib.sha256(f"{text}|{block}".encode()).digest()
            # 32 bytes -> 8 unsigned 32-bit ints
            for word in struct.unpack(">8I", digest):
                values.append((word / 0xFFFFFFFF) * 2.0 - 1.0)
                if len(values) >= dimensions:
                    break
            block += 1
        return _l2_normalise(values[:dimensions])


def _l2_normalise(vector: Vector) -> Vector:
    norm = sum(component * component for component in vector) ** 0.5
    if norm == 0.0:
        return vector
    return [component / norm for component in vector]


# ---------------------------------------------------------------- bedrock provider


class BedrockEmbeddingProvider:
    """Amazon Titan Text Embeddings V2 via the Bedrock runtime client (task 7.4, design §8.2).

    ``boto3`` is imported inside ``__init__`` rather than at module scope so the knowledge package
    has no hard dependency on the AWS SDK: the mock path, CI and evaluation never construct this
    class, so they never import ``boto3``. Credentials come from the default AWS credential chain —
    never from configuration (Phase 0 rule). Requests are batched by the caller (the ingestion
    writer) and retried by botocore's adaptive mode.

    This class is deliberately thin in Phase 7; its throttling, timeout and circuit-breaker
    behaviour is completed with the rest of the Bedrock provider in Phase 8 (task 8.2).
    """

    def __init__(self, *, region: str, model_id: str, max_retries: int = 3) -> None:
        import boto3  # noqa: PLC0415 - lazy so the SDK is optional
        from botocore.config import Config  # noqa: PLC0415

        self.model_id = model_id
        self._client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=Config(retries={"max_attempts": max_retries, "mode": "adaptive"}),
        )

    def embed(self, texts: Sequence[str], *, dimensions: int) -> list[Vector]:
        import json  # noqa: PLC0415

        vectors: list[Vector] = []
        for text in texts:
            body = json.dumps({"inputText": text, "dimensions": dimensions, "normalize": True})
            response = self._client.invoke_model(modelId=self.model_id, body=body)
            payload = json.loads(response["body"].read())
            vectors.append([float(component) for component in payload["embedding"]])
        return vectors


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    """Select the embedding provider from configuration (task 7.4).

    Bound to ``LLM_PROVIDER``: ``mock`` gives the deterministic provider (the CI/eval/offline path),
    anything else gives Bedrock Titan. Kept as one selector so the same switch that routes agent
    generation routes embeddings, and a deployment cannot end up with a mock generator and a live
    embedder or vice versa.
    """
    from c360.core.config import LlmProvider  # noqa: PLC0415 - avoid import cycle at module load

    if settings.llm_provider is LlmProvider.MOCK:
        return MockEmbeddingProvider()
    return BedrockEmbeddingProvider(
        region=settings.aws_region,
        model_id=settings.bedrock_embed_model_id,
        max_retries=settings.bedrock_max_retries,
    )


# ---------------------------------------------------------------- content-hash cache


def embedding_cache_key(text: str, *, dimensions: int, model_id: str) -> str:
    """The cache key for one text: ``sha256(model_id | dimensions | text)`` (design §9.5).

    Model id and dimensions are in the key because the same text under a different model or width is
    a different vector; serving a stale one after a model switch would corrupt retrieval silently.
    """
    return hashlib.sha256(f"{model_id}|{dimensions}|{text}".encode()).hexdigest()


class EmbeddingCache:
    """A content-hash embedding cache wrapping an :class:`EmbeddingProvider` (task 7.4).

    On :meth:`embed_cached` a text already seen (same content, model and dimensions) is served from
    the cache; only the misses are sent to the underlying provider, batched. Re-ingesting an
    unchanged corpus is therefore all hits and costs nothing (design §9.5). The cache is an
    in-memory dict scoped to one ingestion run — persistence across runs is the ``content_hash``
    column on ``kb_chunk`` / ``kb_document``, which lets ingestion skip unchanged documents before
    it ever reaches embedding.
    """

    __slots__ = ("_hits", "_misses", "_provider", "_store")

    def __init__(self, provider: EmbeddingProvider) -> None:
        self._provider = provider
        self._store: dict[str, Vector] = {}
        self._hits = 0
        self._misses = 0

    @property
    def model_id(self) -> str:
        return self._provider.model_id

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses

    def embed_cached(self, texts: Sequence[str], *, dimensions: int) -> list[Vector]:
        """Return vectors for ``texts``, serving repeats from the cache and batching the misses."""
        keys = [
            embedding_cache_key(text, dimensions=dimensions, model_id=self._provider.model_id)
            for text in texts
        ]
        missing_indices = [i for i, key in enumerate(keys) if key not in self._store]
        if missing_indices:
            fresh = self._provider.embed([texts[i] for i in missing_indices], dimensions=dimensions)
            for i, vector in zip(missing_indices, fresh, strict=True):
                self._store[keys[i]] = vector
            self._misses += len(missing_indices)
        self._hits += len(texts) - len(missing_indices)
        return [self._store[key] for key in keys]


__all__ = [
    "BedrockEmbeddingProvider",
    "EmbeddingCache",
    "EmbeddingProvider",
    "MockEmbeddingProvider",
    "Vector",
    "build_embedding_provider",
    "embedding_cache_key",
]
