"""The single import point for the helpers provided in `project_starter.py`.

The helpers above the marker are used **as-is** — not refactored, not
reimplemented, not corrected. Their quirks are worked around by the tools that
wrap them.

Importing `project_starter` by name, rather than reaching into `__main__`,
keeps every tool module independent of how the harness was launched. When the
harness runs as `python project_starter.py` this does import a second copy of
the module; that copy defines functions and a lazy engine and runs nothing, so
it is inert. It is also why `project_starter.py` imports this package from
*inside* `run_test_scenarios()` rather than at module level — a module-level
import would close the cycle.
"""

from sqlalchemy import Engine

import project_starter as _starter

db_engine = _starter.db_engine
paper_supplies = _starter.paper_supplies

create_transaction = _starter.create_transaction
generate_financial_report = _starter.generate_financial_report
get_all_inventory = _starter.get_all_inventory
get_cash_balance = _starter.get_cash_balance
get_stock_level = _starter.get_stock_level
get_supplier_delivery_date = _starter.get_supplier_delivery_date
init_database = _starter.init_database
search_quote_history = _starter.search_quote_history

__all__ = [
    "db_engine",
    "engine",
    "paper_supplies",
    "create_transaction",
    "generate_financial_report",
    "get_all_inventory",
    "get_cash_balance",
    "get_stock_level",
    "get_supplier_delivery_date",
    "init_database",
    "search_quote_history",
]


def engine() -> Engine:
    """The live SQLAlchemy engine the provided helpers read and write through.

    A function rather than the `db_engine` re-export above, because the helpers
    resolve `db_engine` as a module global on every call — so a test that swaps
    `project_starter.db_engine` for a temporary database moves the helpers but
    not a binding captured at import time. Anything issuing its own SQL must go
    through here to stay pointed at the same database as the helpers.

    Returns:
        The engine `project_starter` currently holds.
    """
    return _starter.db_engine
