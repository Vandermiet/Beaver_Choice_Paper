"""The ledger: the rowid a write reports is the row it actually wrote.

Which the provided `create_transaction` cannot promise, because it reads
`last_insert_rowid()` on a second pooled connection. Issue #39 has the
measurements and `ledger._write_and_read_back_the_rowid` has the workaround;
what is asserted here is only the guarantee the rest of the system is entitled
to, since `transaction_links` and `quote_fulfilments` are both keyed on it.
"""

from datetime import date

import pytest
from sqlalchemy import text

from beaver import ledger, starter

BOOKED_ON = date(2025, 4, 1)


@pytest.fixture
def pooled(seeded_db):
    """A seeded database whose pool holds more than one connection.

    Which is every real run of this system: the audit trail writes its own rows
    through the same engine, so a second connection exists by the time the
    first sale is written. A single-connection pool hides the defect entirely,
    which is why the suite never saw it — so the fixture creates the condition
    rather than waiting to be unlucky.
    """
    first, second = seeded_db.connect(), seeded_db.connect()
    first.close()
    second.close()
    return seeded_db


def row_at(rowid: int) -> dict | None:
    with starter.engine().connect() as conn:
        row = conn.execute(
            text(
                "SELECT item_name, transaction_type, units, price, transaction_date "
                "FROM transactions WHERE rowid = :rowid"
            ),
            {"rowid": rowid},
        ).mappings().one_or_none()
    return dict(row) if row else None


def last_rowid() -> int:
    with starter.engine().connect() as conn:
        return conn.execute(text("SELECT MAX(rowid) FROM transactions")).scalar_one()


class TestTheRowidIsTheRowThatWasWritten:
    """Three ways of asking one question, because one link keyed on the wrong
    row is money attributed to a customer who never bought it."""

    async def test_the_reported_rowid_holds_the_row_just_written(self, pooled):
        async with ledger.write_lock:
            rowid = await ledger.write_row("A4 paper", "sales", 250, 12.5, BOOKED_ON)
        assert row_at(rowid) == {
            "item_name": "A4 paper",
            "transaction_type": "sales",
            "units": 250,
            "price": 12.5,
            "transaction_date": BOOKED_ON.isoformat(),
        }

    async def test_the_reported_rowid_is_the_newest_row(self, pooled):
        async with ledger.write_lock:
            rowid = await ledger.write_row("Cardstock", "stock_orders", 300, 35.36, BOOKED_ON)
        assert rowid == last_rowid()

    async def test_consecutive_writes_report_consecutive_rows(self, pooled):
        rowids = []
        async with ledger.write_lock:
            for units in (100, 200, 300):
                rowids.append(
                    await ledger.write_row("Glossy paper", "sales", units, units * 0.2, BOOKED_ON)
                )
        assert rowids == sorted(set(rowids))
        assert [row_at(rowid)["units"] for rowid in rowids] == [100, 200, 300]
