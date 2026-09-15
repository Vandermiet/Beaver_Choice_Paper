"""The seed-137 facts the whole design was established against.

If any of these move, a fact on the wayfinder map has gone stale and the
design's arithmetic needs re-tracing — so they are asserted once, here.
"""

from sqlalchemy import text

import project_starter


def test_starting_cash_is_45059_70(seeded_db):
    """The seed rows are stamped `2025-01-01T00:00:00` and the helper compares
    dates as strings, so a bare `2025-01-01` excludes them. Ask for the 2nd."""
    balance = project_starter.get_cash_balance("2025-01-02")
    assert round(balance, 2) == 45059.70


def test_exactly_18_items_are_carried(seeded_db):
    with seeded_db.connect() as conn:
        carried = conn.execute(text("SELECT COUNT(*) FROM inventory")).scalar()
    assert carried == 18


def test_no_item_starts_below_its_own_minimum(seeded_db):
    with seeded_db.connect() as conn:
        rows = conn.execute(
            text("SELECT item_name, current_stock, min_stock_level FROM inventory")
        ).fetchall()
    below = [r.item_name for r in rows if r.current_stock < r.min_stock_level]
    assert below == []
