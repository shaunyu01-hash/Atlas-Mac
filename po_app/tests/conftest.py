from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlaspo import auth, backup, db, masters, order_forms, orders  # noqa: E402

PASSWORD = "correct-horse"
NORTH, SOUTH, EAST, WEST = 2, 4, 6, 8


@pytest.fixture(autouse=True)
def free_windows():
    """A closed window stays alive until it's deleted, and a mode switch restyles every live widget, so hundreds left behind by earlier tests take minutes per switch."""
    yield
    if QApplication.instance():
        for w in QApplication.topLevelWidgets():
            w.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture(autouse=True)
def own_settings(tmp_path_factory, monkeypatch):
    """Saved window settings (widths, columns, page size, dark mode) go to a temp file, never the real registry."""
    from PySide6.QtCore import QSettings
    from atlaspo.ui import theme
    path = str(tmp_path_factory.mktemp("settings") / "po.ini")
    monkeypatch.setattr(theme, "_settings", lambda: QSettings(path, QSettings.Format.IniFormat))
    yield


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PO_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def conn(env):
    c = db.open_database()
    yield c
    try:
        c.close()
    except Exception:
        pass


class World:
    """A small business: admin (also approver), one buyer, one supplier, the four active branches."""

    def __init__(self, conn):
        self.conn = conn
        self.admin = auth.create_first_admin(conn, "boss", "Boss Approver", PASSWORD)
        self.buyer = auth.create_user(conn, self.admin, "buyer", "Bea Buyer", PASSWORD, ["BUYER"], must_change=False)
        self.warehouse = auth.create_user(conn, self.admin, "nestor", "Nestor Warehouse", PASSWORD, ["WAREHOUSE"], must_change=False)
        # Most tests count straight away. Waiting for the warehouse to ship first has its own tests, which turn this back on.
        conn.execute("INSERT OR REPLACE INTO system_settings(key, value, updated_at) VALUES ('receiving.after_shipping', '0', datetime('now'))")
        self.supplier = masters.save_supplier(conn, self.admin, {"code": "ABC", "name": "ABC Trading", "payment_terms": "30 D"})

    def header(self, **over):
        return {"supplier_id": self.supplier, "of_date": "2026-11-02", **over}

    def item(self, dept=1, cost=10000, alloc=None, **over):
        """alloc is {branch code: quantity}. By default, one piece at North."""
        return {"department_id": dept, "description": f"Item dept {dept}", "unit_cost_c": cost,
                "allocations": {NORTH: 1} if alloc is None else alloc, **over}

    def draft(self, total_pesos=None, items=None, **over):
        if items is None:
            items = [self.item(cost=(total_pesos or 100) * 100)]
        return order_forms.create_draft(self.conn, self.buyer, self.header(**over), items)

    def approved(self, total_pesos=None, items=None, **over) -> int:
        """A submitted OF, approved by whichever route the policy sends it."""
        of_id = self.draft(total_pesos, items, **over)
        if order_forms.submit(self.conn, self.buyer, of_id) == orders.PENDING:
            order_forms.approve(self.conn, self.admin, of_id)
        return of_id

    def po(self, total_pesos=None, items=None, **over) -> int:
        """One approved OF, its POs generated. Returns the first PO's id."""
        of_id = self.approved(total_pesos, items, **over)
        return order_forms.generate_pos(self.conn, self.buyer, of_id)[0]["id"]


@pytest.fixture
def world(conn):
    return World(conn)


@pytest.fixture
def clock(monkeypatch):
    def set_to(*args):
        for module in (db, order_forms, backup):
            monkeypatch.setattr(module, "now", lambda: datetime(*args))
    return set_to
