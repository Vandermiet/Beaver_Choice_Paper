"""Fixtures shared by the suite.

Every fixture that touches the database seeds a *temporary* `munder_difflin.db`
at seed 137 — the seed every fact in the locked design was established against.
The provided helpers read a module-global engine, so the fixture swaps that
global rather than passing an engine around.
"""

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

import project_starter  # noqa: E402


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    """A freshly seeded database at seed 137, with the helpers pointed at it.

    `init_database` reads `quote_requests.csv` and `quotes.csv` by relative
    path, so the working directory moves to `project/` for the duration.
    """
    monkeypatch.chdir(PROJECT_DIR)
    engine = create_engine(f"sqlite:///{tmp_path / 'munder_difflin.db'}")
    monkeypatch.setattr(project_starter, "db_engine", engine)
    project_starter.init_database(engine)
    return engine


def pytest_collection_modifyitems(config, items):
    """Deselect the live proxy smoke test unless `BEAVER_LIVE=1`.

    It costs a model call and needs a key, so it stays out of the default run.
    """
    import os

    if os.environ.get("BEAVER_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="needs BEAVER_LIVE=1 (hits the Vocareum proxy)")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def trail(seeded_db, tmp_path):
    """A trail on a seeded database that has the seven audit tables alongside its four.

    The `run_id` is fixed rather than minted, so a test can assert on step ids.
    The transcript sidecar is redirected into `tmp_path`, so a test run never
    writes into the repo's own `audit/` directory.
    """
    from beaver.audit import AuditTrail, bootstrap_audit
    from beaver.contract import forget_carried_catalogue

    forget_carried_catalogue()
    bootstrap_audit()
    return AuditTrail(run_id="20260915T120000Z", transcript_dir=tmp_path / "audit")
