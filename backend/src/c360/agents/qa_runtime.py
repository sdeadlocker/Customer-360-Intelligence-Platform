"""Q&A conversation memory: the checkpointer and thread lifecycle (task 9.2, design §8.7/§10.2).

A follow-up question ("and their mortgage?") only makes sense if the prior turn's conversation is
still there, so the Q&A graph is compiled with a LangGraph checkpointer that persists each thread's
state to ``checkpoints.db``. LangGraph keys a thread by a ``thread_id``; design §8.7 fixes that key
as ``(session_id, customer_id)`` so two customers in one session never share a memory and switching
customers can drop the prior thread server-side rather than trusting a client to clear it
(requirement 11.9).

Why the connection is opened lazily inside the loop
---------------------------------------------------

``AsyncSqliteSaver`` wraps an ``aiosqlite`` connection, which must be created and ``setup()`` on the
running event loop — not at synchronous app construction. So this holder opens the connection on the
first Q&A request (under a lock so two concurrent firsts do not each open one), runs the one-time
schema setup, and reuses it thereafter. It is disposed at shutdown alongside the other engines.

Thread identity and switching
------------------------------

``thread_id(session_id, customer_id)`` is the single place the key is formed, so the route, the
delete-on-switch call and any test all agree on it. :meth:`QaMemory.switch_customer` deletes the
threads a session held for *other* customers before the new customer's turn runs, which is the
server-side clearing requirement 11.9 asks for.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


#: The synthetic "customer" component of the thread key for the cross-customer search path, where a
#: turn is not scoped to any single customer. It cannot collide with a real customer id (which never
#: contains a space), so a cross-customer conversation keeps its own memory, distinct from every
#: per-customer thread the same session may also hold.
SEARCH_SCOPE = "* all customers *"


def thread_id(session_id: str, customer_id: str) -> str:
    """The LangGraph thread key for a ``(session_id, customer_id)`` pair (design §8.7).

    On the cross-customer search path pass :data:`SEARCH_SCOPE` as ``customer_id`` so the
    conversation has a stable thread of its own, separate from any per-customer thread.
    """
    return f"{session_id}:{customer_id}"


def search_thread_id(session_id: str) -> str:
    """The thread key for the cross-customer search conversation of ``session_id``."""
    return thread_id(session_id, SEARCH_SCOPE)


class QaMemory:
    """Owns the async SQLite checkpointer and the per-session thread bookkeeping (task 9.2).

    One instance per process, held on the agent runtime. The checkpointer is opened once, lazily, on
    the running loop; the session→customers map is kept in memory so a customer switch knows which
    prior threads to delete. The map is process-local, which is correct for a single-process
    deployment and is the same scope the recently-viewed tracker uses.
    """

    __slots__ = ("_conn", "_lock", "_path", "_saver", "_sessions")

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._saver: Any = None
        self._conn: Any = None
        self._lock = asyncio.Lock()
        #: session_id -> set of customer_ids this session currently has a live thread for.
        self._sessions: dict[str, set[str]] = {}

    async def checkpointer(self) -> Any:
        """The process-wide checkpointer, opened and schema-initialised on first use.

        Opened under a lock on the running event loop, because ``aiosqlite`` binds its connection to
        the loop that created it. ``setup()`` creates the checkpoint tables the first time only.
        """
        if self._saver is not None:
            return self._saver
        async with self._lock:
            if self._saver is None:
                import aiosqlite  # noqa: PLC0415 - lazy; only the Q&A path needs it
                from langgraph.checkpoint.sqlite.aio import (  # noqa: PLC0415
                    AsyncSqliteSaver,
                )

                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._conn = await aiosqlite.connect(str(self._path))
                saver = AsyncSqliteSaver(self._conn)
                await saver.setup()
                self._saver = saver
            return self._saver

    async def switch_customer(self, session_id: str, customer_id: str) -> None:
        """Drop this session's threads for any *other* customer before serving ``customer_id``.

        This is the server-side clearing requirement 11.9 mandates: switching customers deletes the
        prior thread rather than relying on the client to forget it, so a follow-up can never resume
        another customer's conversation.
        """
        held = self._sessions.get(session_id)
        if held:
            stale = [other for other in held if other != customer_id]
            for other in stale:
                await self.delete_thread(thread_id(session_id, other))
                held.discard(other)
        self._sessions.setdefault(session_id, set()).add(customer_id)

    async def delete_thread(self, thread: str) -> None:
        """Delete every checkpoint and write for ``thread`` from the checkpoint database.

        ``AsyncSqliteSaver`` in this version does not implement ``adelete_thread`` (it inherits the
        base ``NotImplementedError``), so the two checkpoint tables are cleared directly by their
        ``thread_id`` column — the same deletion the saver would perform, done in SQL. This is the
        server-side thread clearing requirement 11.9 relies on, so it is done explicitly here rather
        than left to an unimplemented API.
        """
        await self.checkpointer()  # ensure the connection and schema exist
        conn = self._conn
        await conn.execute("DELETE FROM writes WHERE thread_id = ?", (thread,))
        await conn.execute("DELETE FROM checkpoints WHERE thread_id = ?", (thread,))
        await conn.commit()

    async def aclose(self) -> None:
        """Close the underlying connection at shutdown, if one was opened."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            self._saver = None


__all__ = ["SEARCH_SCOPE", "QaMemory", "search_thread_id", "thread_id"]
