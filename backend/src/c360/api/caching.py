"""HTTP conditional-request support for reference data (task 16.2, design §13.1).

Institutional reference data — a knowledge document's metadata, its passages — changes only when
the corpus is re-ingested, not per request and not per customer. That makes it the one part of the
API surface where HTTP caching is both safe and worthwhile: a client (or an intermediary) can hold a
copy and revalidate cheaply with a conditional request.

This module provides the two pieces that need to be consistent across every reference endpoint:

* :func:`content_etag` derives a strong ``ETag`` from the response body, so the tag changes exactly
  when the content does — a re-ingested document version yields a new body and therefore a new tag.
* :func:`conditional_response` compares the client's ``If-None-Match`` against that tag and returns
  a bodyless ``304 Not Modified`` on a hit, or the full body with ``ETag`` and ``Cache-Control`` on
  a miss.

Customer data is deliberately excluded: it is entitlement-scoped and masked per role, so an
``ETag`` shared across roles would be a cache-poisoning hazard and a ``Cache-Control`` header would
risk a shared cache serving one role's masked view to another. Only role-independent, institutional
data is cached here.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse

#: Reference data may be held for this long before revalidation. Short enough that a re-ingest is
#: picked up quickly, long enough to absorb a burst of repeated reads. ``must-revalidate`` means a
#: stale copy is never served without checking the ``ETag`` first.
REFERENCE_MAX_AGE_S: int = 60


def content_etag(payload: Any) -> str:
    """Return a strong ``ETag`` derived from ``payload``'s canonical JSON form.

    The payload is serialized with sorted keys so an equal object always hashes to the same tag
    regardless of field order. A Pydantic model is dumped via ``model_dump`` first. The tag is a
    quoted SHA-256 prefix, which is the strong-validator form ``If-None-Match`` compares against.
    """
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return f'"{digest[:32]}"'


def conditional_response(
    request: Request,
    payload: Any,
    *,
    etag_source: Any = None,
    max_age_s: int = REFERENCE_MAX_AGE_S,
) -> Response:
    """Return a ``304`` when the client's ``If-None-Match`` matches, else the body with caching.

    The tag is derived from ``etag_source`` when given, otherwise from ``payload``. This split
    matters for the response envelope: its ``meta`` carries a per-request ``correlation_id`` and
    ``trace_id`` that change on every call, so hashing the whole envelope would never revalidate.
    Passing the stable ``data`` portion as ``etag_source`` makes the tag change exactly when the
    content does, while the returned body is still the full envelope.

    On a match the response is bodyless and carries the ``ETag`` again, per RFC 9110. On a miss the
    full payload is returned as JSON with ``ETag`` and a private, must-revalidate ``Cache-Control``.
    ``private`` keeps a shared intermediary from caching data behind an authenticated endpoint;
    revalidation still happens against the strong tag.
    """
    etag = content_etag(payload if etag_source is None else etag_source)
    cache_control = f"private, max-age={max_age_s}, must-revalidate"

    if_none_match = request.headers.get("if-none-match")
    if if_none_match and etag in {tag.strip() for tag in if_none_match.split(",")}:
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": cache_control},
        )

    body = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
    return JSONResponse(
        content=body,
        headers={"ETag": etag, "Cache-Control": cache_control},
    )


__all__ = ["REFERENCE_MAX_AGE_S", "conditional_response", "content_etag"]
