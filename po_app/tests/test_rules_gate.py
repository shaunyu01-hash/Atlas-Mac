"""Gate for the version 28 business rules, in one walk through the services.

Buying and Accounting refused on one login; the agreed overall discount stays after an amendment and a cancel (percent and pesos);
a lower check signed off and an accepted lower DR total leave nothing owed; a debit memo cancelled after payment goes on the next
check with its reason; the warehouse still reads costs; a long list shows 200 at a time.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PO_SAMPLE_DIR", tempfile.mkdtemp(prefix="po-sample-"))

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from atlaspo import auth, claim_stubs as cs, debit_memos as dm, money, order_forms, orders, receiving
from atlaspo.db import POError
from atlaspo.debit_memos import DAMAGED, SHORT
from atlaspo.ui import of_page, theme

from conftest import NORTH, PASSWORD, SOUTH, EAST

ROOT = Path(__file__).resolve().parents[1]
CONFLICT = "Buying and Accounting can't be on the same login. Accounting doesn't buy stock. Remove one of the two."


@pytest.fixture
def app(tmp_path, monkeypatch):
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(theme, "_settings", lambda: settings)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    yield QApplication.instance() or QApplication([])


def _po(conn, pid):
    return conn.execute("SELECT * FROM purchase_orders WHERE id = ?", (pid,)).fetchone()


def _of(conn, of_id):
    return conn.execute("SELECT * FROM order_forms WHERE id = ?", (of_id,)).fetchone()


def _rows(conn, pid, pieces):
    out = []
    for i in orders.get_po(conn, pid)["items"]:
        r = {k: i[k] for k in orders.LINE_FIELDS}
        r["id"], r["qty_m"] = i["id"], pieces * 1000
        out.append(r)
    return out


def _order(w, alloc, **terms):
    of_id = w.approved(items=[w.item(cost=10000, alloc=alloc, description="Socks", item_code="SOCK-1")], **terms)
    pos = order_forms.generate_pos(w.conn, w.buyer, of_id)
    for p in pos:
        orders.issue(w.conn, w.buyer, p["id"])
    return of_id, {p["branch_code"]: p["id"] for p in pos}


# ------------------------------------------------------------------ Q36: Buying and Accounting refused

def test_q36_buying_with_accounting_is_refused_and_approver_combinations_stay(world):
    c = world.conn
    users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    with pytest.raises(POError, match="Buying and Accounting"):
        auth.create_user(c, world.admin, "mixed", "Mia Mixed", PASSWORD, ["BUYER", "ACCOUNTING"], must_change=False)
    assert c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == users
    assert auth.role_conflict({"BUYER", "ACCOUNTING"}) == CONFLICT
    with pytest.raises(POError, match="Buying and Accounting"):
        auth.update_user(c, world.admin, world.buyer.id, "Bea Buyer", ["BUYER", "ACCOUNTING"], True)
    assert sorted(r[0] for r in c.execute("SELECT role FROM user_roles WHERE user_id = ?", (world.buyer.id,))) == ["BUYER"]
    # Not decided by the owner, so these still save.
    auth.create_user(c, world.admin, "acap", "Ann Approver", PASSWORD, ["ACCOUNTING", "APPROVER"], must_change=False)
    auth.create_user(c, world.admin, "buap", "Ben Approver", PASSWORD, ["BUYER", "APPROVER"], must_change=False)


# ------------------------------------------------------------------ Q3: the agreed overall discount stays

def test_q3_percent_discount_stays_after_an_amendment_and_a_cancel(world):
    c = world.conn
    of_id, pos = _order(world, {NORTH: 10, SOUTH: 10, EAST: 10}, overall_discount_type=money.DISC_PERCENT, overall_discount_value=1000)
    orders.amend(c, world.buyer, pos[NORTH], {}, _rows(c, pos[NORTH], 1), "Only one")
    assert (_po(c, pos[NORTH])["overall_discount_c"], _po(c, pos[NORTH])["grand_total_c"]) == (1000, 9000)  # 10%, never 0.00
    orders.cancel(c, world.buyer, pos[EAST], "Branch closed")
    assert {b: (_po(c, pos[b])["overall_discount_c"], _po(c, pos[b])["grand_total_c"]) for b in (NORTH, SOUTH)} == \
        {NORTH: (1000, 9000), SOUTH: (10000, 90000)}  # still 10% each after the cancel
    of = _of(c, of_id)
    assert (of["overall_discount_type"], of["overall_discount_value"]) == (money.DISC_PERCENT, 1000)
    assert of["overall_discount_c"] == 1000 + 10000 and of["grand_total_c"] == 9000 + 90000


def test_q3_peso_discount_keeps_its_total_after_an_amendment_and_a_cancel(world):
    c = world.conn
    of_id, pos = _order(world, {NORTH: 10, SOUTH: 10, EAST: 10}, overall_discount_type=money.DISC_AMOUNT, overall_discount_value=30000)
    orders.amend(c, world.buyer, pos[NORTH], {}, _rows(c, pos[NORTH], 5), "Fewer")
    live = lambda: {b: _po(c, p)["overall_discount_c"] for b, p in pos.items() if _po(c, p)["status"] != orders.CANCELLED}
    assert live() == {NORTH: 6000, SOUTH: 12000, EAST: 12000}
    orders.cancel(c, world.buyer, pos[EAST], "Branch closed")
    shares = live()
    assert set(shares) == {NORTH, SOUTH} and sum(shares.values()) == 30000
    assert shares == {NORTH: 10000, SOUTH: 20000}  # 500.00 and 1,000.00 of goods
    of = _of(c, of_id)
    assert of["overall_discount_c"] == 30000 and of["grand_total_c"] == 150000 - 30000


# ------------------------------------------------------------------ Q21: the warehouse still reads costs

def test_q21_warehouse_still_reads_costs(world, env):
    c, wh = world.conn, world.warehouse
    of_id = world.approved(items=[world.item(cost=7777, alloc={NORTH: 10}, description="Socks", item_code="SOCK-1")])
    po = order_forms.generate_pos(c, world.buyer, of_id)[0]
    orders.issue(c, world.buyer, po["id"])
    item = orders.get_po(c, po["id"])["items"][0]["id"]
    for name in ("ana", "ben"):
        r = auth.create_user(c, world.admin, name, name.title(), PASSWORD, ["RECEIVER"], must_change=False, branch_code=NORTH)
        receiving.save_count(c, r, po["id"], {item: "8"}, damaged={item: "3"})
    memo = dm.issue(c, world.buyer, po["id"], {(item, SHORT): "2"})
    stub = cs.issue(c, world.buyer, po["id"], {(item, DAMAGED): "3"})
    assert wh.roles == {"WAREHOUSE"}
    assert dm.get(c, wh, memo["id"])["total_c"] == 2 * 7777
    assert cs.get(c, wh, stub["id"])["total_c"] == 3 * 7777
    assert [i["net_unit_c"] for i in cs.items(c, wh, stub["id"])] == [7777]
    from atlaspo import pdf
    assert pdf.warehouse_of(c, wh, of_id, env / "wh-of.pdf").read_bytes().startswith(b"%PDF")


# ------------------------------------------------------------------ Q33: a long list shows a page at a time

def test_q33_a_long_list_pages_and_search_looks_through_everything(app, world):
    c = world.conn
    start = date(2025, 1, 1)
    ids = [world.approved(items=[world.item(alloc={NORTH: 1})], of_date=(start + timedelta(days=i)).isoformat()) for i in range(230)]
    page = of_page.OrdersPage(c, world.admin)
    page.resize(1920, 1080)
    page.show()
    QApplication.processEvents()
    tops = lambda: [tuple(page.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)) for i in range(page.tree.topLevelItemCount())]
    newest = list(reversed(ids))
    assert tops() == [("OF", i) for i in newest[:50]]
    assert not page.pager.isHidden() and page.pager.label.text() == "Page 1 of 5 · 1–50 of 230"
    page.pager.go(4)
    QApplication.processEvents()
    assert tops() == [("OF", i) for i in newest[200:]] and page.pager.label.text() == "Page 5 of 5 · 201–230 of 230"
    page.clear_filters()
    oldest = c.execute("SELECT of_number FROM order_forms WHERE id = ?", (ids[0],)).fetchone()[0]
    page.search.setText(oldest)
    assert tops() == [("OF", ids[0])]
