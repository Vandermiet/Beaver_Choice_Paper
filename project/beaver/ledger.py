"""The single door to `create_transaction`: one lock, one row, one link.

Two agents write to the transaction registry — sales takes money in and
replenishment lays money out — and `CONTEXT.md`'s *Ownership* rule clears that
as two questions over one table rather than an overlap. But it leaves them one
thing they cannot each own a copy of: **the lock.**

`create_transaction` runs `INSERT` and then `SELECT last_insert_rowid()` as two
statements through a pooled engine, and `last_insert_rowid()` is
per-connection. Two writers overlapping inside those two statements leave one of
them holding a rowid that belongs to the other's row. A lock per agent would
serialise each agent against itself and neither against the other, which is the
one arrangement that looks safe and is not — so the lock lives here, where there
is exactly one of it, and both agents hold the same one.

`link_to_request` is here for the same reason the lock is: every row either
agent writes must be traceable to the request that caused it, and a row whose
link was left to a later step would be money nobody could trace if that step
never came. Sales writes a second link of its own — `quote_fulfilments`, the
offer it honoured — and that stays in sales, because only sales has a quote.

This module is deliberately not a registry *tool*. It adds no rule, takes no
decision and belongs to no agent; it is the mechanics both writers share.
"""

import asyncio
import weakref
from datetime import date

from sqlalchemy import Connection, text

from beaver import starter

class _PerLoopLock:
    """One `asyncio.Lock` per event loop, resolved when it is entered.

    A single module-level `asyncio.Lock` looks like the obvious thing and is a
    latent bug here. `asyncio.Lock` binds itself to the running loop the first
    time it is actually contended — the uncontended fast path never touches the
    loop at all — and raises on every later use from a different one. The
    harness calls `asyncio.run()` **once per request**, so a lock that two
    writers contended on request 3 would raise on request 4, and only on the
    runs where the contention happened. Failing rarely and by request index is
    worse than failing always.

    Keyed weakly, so a finished loop's lock goes with it. Within one loop this
    is exactly the process-wide lock it replaces: every writer entering it
    waits for the one holding it.
    """

    def __init__(self) -> None:
        self._locks: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, asyncio.Lock
        ] = weakref.WeakKeyDictionary()

    def _current(self) -> asyncio.Lock:
        """The lock belonging to the loop we are running on."""
        loop = asyncio.get_running_loop()
        lock = self._locks.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[loop] = lock
        return lock

    async def __aenter__(self) -> None:
        await self._current().acquire()

    async def __aexit__(self, *exc_info: object) -> None:
        # The same loop, so the same lock: resolved again rather than held on
        # the instance, which two concurrent writers would overwrite.
        self._current().release()


#: Serialises `create_transaction` across the whole process, for every writer.
#: Nothing calls two writers concurrently today, and the lock is what makes
#: that a choice rather than a load-bearing assumption.
write_lock = _PerLoopLock()


async def write_row(
    item_name: str,
    transaction_type: str,
    units: int,
    total_price: float,
    booked_on: date,
) -> int:
    """Write one `transactions` row and return its rowid. Hold `write_lock` first.

    Blocking, and deliberately run off the event loop: the lock is what
    serialises it, so a second caller waits at the lock rather than
    interleaving inside the helper's two statements.

    Args:
        item_name: The exact carried-catalogue name.
        transaction_type: `sales` or `stock_orders`.
        units: The quantity moving.
        total_price: The **line total**, never a unit price: `get_cash_balance`
            sums this column directly, so a unit price here would misstate cash
            by the quantity.
        booked_on: The date the row is dated, per the as-of-date convention —
            the whole simulated timeline is as-of, and dating a row today would
            put it outside the window the balance is read over.

    Returns:
        The `transactions` rowid of the row just written.
    """
    return await asyncio.to_thread(
        starter.create_transaction,
        item_name,
        transaction_type,
        units,
        total_price,
        booked_on.isoformat(),
    )


def link_to_request(
    rowid: int,
    run_id: str,
    request_id: str,
    step_id: str,
    conn: Connection | None = None,
) -> None:
    """Join one written row back to the request and the step that caused it.

    `transactions.id` is NULL for every row written at runtime — the seeded
    schema has the column and `create_transaction` never fills it — so the
    rowid is the only key there is to join on.

    Args:
        rowid: The `transactions` rowid.
        run_id: The run the row belongs to.
        request_id: The request the row belongs to.
        step_id: The delegation that wrote it.
        conn: A transaction to write inside, for a caller with a second link to
            write about the same row — sales has one, `quote_fulfilments`, and
            the two facts must land together or not at all. Omitted, this opens
            its own transaction, which is what a caller with one link wants.
    """
    if conn is None:
        with starter.engine().begin() as own:
            _insert_link(own, rowid, run_id, request_id, step_id)
    else:
        _insert_link(conn, rowid, run_id, request_id, step_id)


def _insert_link(
    conn: Connection, rowid: int, run_id: str, request_id: str, step_id: str
) -> None:
    """The `transaction_links` insert itself, on whichever transaction it is given."""
    conn.execute(
            text(
                """
                INSERT INTO transaction_links (
                  transaction_rowid, run_id, request_id, step_id
                ) VALUES (:transaction_rowid, :run_id, :request_id, :step_id)
                """
            ),
            {
                "transaction_rowid": rowid,
                "run_id": run_id,
                "request_id": request_id,
                "step_id": step_id,
            },
        )
