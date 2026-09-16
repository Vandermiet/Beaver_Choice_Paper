"""The ledger: the rowid a write reports is the row it actually wrote.

The provided `create_transaction` returns `last_insert_rowid()` read through
`pd.read_sql` — a *second* connection out of the pool, whose last insert was
some other writer's. The number it hands back is therefore not reliably the row
it just wrote, and `transaction_links` and `quote_fulfilments` are both keyed on
it: the ticket 109 evaluation run found links pointing at seeded rows dated
2025-01-01 and real sales with no link at all.

The helper is used as-is, per `beaver.starter` — so the workaround lives here,
in the tool that wraps it.
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
    first sale is written. SQLAlchemy's queue pool hands them out first-in
    first-out, so the connection `pd.read_sql` gets for `last_insert_rowid()`
    is *not* the one `to_sql` inserted on — and the number comes back one write
    behind, or zero on the first write of all.
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
