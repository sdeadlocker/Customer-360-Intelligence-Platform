"""Opaque keyset cursors for the list endpoints (design §6.3).

Design §6.3 fixes pagination as "keyset/cursor-based on stable sort keys, not offset". The
repository
layer already speaks keyset — :meth:`CustomerRepository.list_ids` and the scoped variants take an
``after`` value and order by the primary key — so a cursor here is nothing more than that ``after``
value, carried across the wire in a form a client cannot mistake for a page number and cannot use to
probe the id space.

Why encode it at all
--------------------

The raw ``after`` value is a customer id. Handing it back as a bare string would work, but it
invites
a client to construct one by hand and it leaks the fact that the sort key *is* the id. Base64url of
the id keeps the token opaque and single-purpose: the API's contract is "pass back the
``next_cursor``
you were given", not "pass back a customer id". The encoding is reversible and unauthenticated — it
is
not a security boundary, entitlement scoping is (design §7.2) — so a tampered cursor decodes to some
string that simply matches no row after it, never to another principal's data.
"""

from __future__ import annotations

import base64
import binascii

from pydantic import BaseModel, ConfigDict, Field

#: The default and maximum page sizes for a list endpoint. The ceiling bounds both the query and the
#: serialized payload so a single request cannot blow the latency budget by asking for everything.
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100


def encode_cursor(after: str) -> str:
    """Encode a keyset ``after`` value into an opaque, URL-safe cursor token."""
    return base64.urlsafe_b64encode(after.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str | None) -> str | None:
    """Decode a cursor token back to its keyset ``after`` value.

    A ``None`` or empty cursor is the first page. A malformed token decodes to ``None`` rather than
    raising: a cursor is opaque client state, not a request field to validate, and the safe reading
    of a garbled one is "start from the beginning" — it can never widen what the principal sees,
    because entitlement scoping is applied independently in the query.
    """
    if not cursor:
        return None
    try:
        return base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None


class Page[ItemT](BaseModel):
    """One page of results plus the cursor for the next.

    ``next_cursor`` is ``None`` exactly when there are no further results, so a client loops until
    it
    is absent rather than until it receives an empty page — one fewer round trip at the end.
    """

    model_config = ConfigDict(frozen=True)

    items: list[ItemT] = Field(default_factory=list)
    next_cursor: str | None = None


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "Page",
    "decode_cursor",
    "encode_cursor",
]
