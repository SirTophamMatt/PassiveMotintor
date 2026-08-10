"""Test bootstrap.

`app.config` resolves BASE_DIR (config.json, unified_monitor.db, backups) at
import time, so the redirect to a throwaway directory has to happen before any
`app.*` module is imported — hence module level, not a fixture. Without this a
test run would touch the real database.
"""
import os
import tempfile

os.environ["UM_DATA_DIR"] = tempfile.mkdtemp(prefix="um-tests-")

import pytest  # noqa: E402

from app import database  # noqa: E402

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture(name):
    """Raw bytes of an HTML fixture, as the scraper would receive them."""
    with open(os.path.join(FIXTURE_DIR, name), "rb") as fh:
        return fh.read()


@pytest.fixture
def db(monkeypatch, tmp_path):
    """A fresh, fully-migrated database per test."""
    path = str(tmp_path / "unified_monitor.db")
    monkeypatch.setattr(database, "DB_FILE", path)
    monkeypatch.setattr(database, "BACKUP_DIR", str(tmp_path / "backups"))
    database.init_db()
    return path
