"""Scheduled and branded report exports (Phase 18).

Server-generated branded PDF packs, a "prepare-for-meeting" briefing and emailed digests, driven
from a new writable ``reports.db`` store created the same way as ``audit.db`` / ``signals.db``. The
read-only ``customer.db`` is never touched: every report is assembled from the same masked,
entitlement-scoped service reads the REST API uses, so a report can never contain data the
requesting role may not see.
"""

from __future__ import annotations

__all__: list[str] = []
