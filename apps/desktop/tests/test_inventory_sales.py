"""Inventory, phase 2: sales and returns, POS files, sales reports and reorder suggestions."""
from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from atlaspo import inventory as inv, order_forms, reorder, stock_sales as sales
from atlaspo.db import PermissionDenied, POError, today

from conftest import NORTH, SOUTH
from test_inventory import _receive, _row, make_world


def _day(back: int = 0) -> str:
    return (today() - timedelta(days=back)).isoformat()


@pytest.fixture
def stocked(world):
    """North has 10 socks (cost 77.00, selling 120.00) and 6 tee pieces (pack of 3 at 60.00, selling 90.00 a pack)."""
    w = make_world(world)
    _receive(w)
    w.conn.execute("UPDATE products SET selling_c = 12000 WHERE id = ?", (w.socks,))
    w.conn.execute("UPDATE products SET selling_c = 9000 WHERE id = ?", (w.tee,))
    return w


# ------------------------------------------------------------------ sales and returns

def test_sales_take_stock_off_at_the_catalog_price_and_keep_the_cost(stocked):
    w = stocked
    sale = sales.record(w.conn, w.r1, NORTH, _day(), [{"product_id": w.socks, "qty": "3"}, {"product_id": w.tee, "qty": "2"}])
    assert _row(w, NORTH, w.socks)["good_m"] == 7000 and _row(w, NORTH, w.tee)["good_m"] == 4000
    lines = {l["item_code"]: l for l in sales.paper_lines(w.conn, w.buyer, sale)}
    assert (lines["SOCK-1"]["amount_c"], lines["SOCK-1"]["profit_c"]) == (36000, 36000 - 23100)
    assert lines["TEE-3"]["price_c"] == 3000  # a pack of 3 at 90.00 is 30.00 a piece
    assert all(l["cost_c"] is None for l in sales.paper_lines(w.conn, w.r1, sale))  # branch staff never see cost
    card = inv.stock_card(w.conn, w.admin, NORTH, w.socks)["rows"]
    assert card[-1]["kind"] == "SOLD" and card[-1]["ref"].endswith("-SL-00001")


def test_a_typed_price_or_amount_wins_and_returns_go_back_where_chosen(stocked):
    w = stocked
    sale = sales.record(w.conn, w.buyer, NORTH, _day(), [{"product_id": w.socks, "qty": "2", "amount": "200"}])
    assert sales.paper_lines(w.conn, w.buyer, sale)[0]["price_c"] == 10000
    back = sales.record(w.conn, w.r1, NORTH, _day(), [{"product_id": w.socks, "qty": "1", "bucket": "DAMAGED"}], kind=sales.RETURN)
    sales.decide(w.conn, w.boss2, back, True)  # every return pays money out, so management approves it
    socks = _row(w, NORTH, w.socks)
    assert (socks["good_m"], socks["damaged_m"]) == (8000, 1000)
    with pytest.raises(POError, match="stock for sale"):
        sales.record(w.conn, w.r1, NORTH, _day(), [{"product_id": w.socks, "qty": "1", "bucket": "DAMAGED"}])


def test_sales_belong_to_the_place_and_the_day(stocked):
    w = stocked
    with pytest.raises(POError, match="isn't your branch"):
        sales.record(w.conn, w.s1, NORTH, _day(), [{"product_id": w.socks, "qty": "1"}])
    with pytest.raises(PermissionDenied):
        sales.record(w.conn, w.boss2, NORTH, _day(), [{"product_id": w.socks, "qty": "1"}])
    with pytest.raises(POError, match="hasn't come yet"):
        sales.record(w.conn, w.r1, NORTH, (today() + timedelta(days=1)).isoformat(), [{"product_id": w.socks, "qty": "1"}])
    inv.open_count(w.conn, w.buyer, NORTH)
    with pytest.raises(POError, match="already took in"):
        sales.record(w.conn, w.r1, NORTH, _day(3), [{"product_id": w.socks, "qty": "1"}])
    sales.record(w.conn, w.r1, NORTH, _day(), [{"product_id": w.socks, "qty": "1"}])  # the count's own day is fine


def test_management_cancels_wrong_sales_and_the_stock_comes_back(stocked):
    w = stocked
    sale = sales.record(w.conn, w.r1, NORTH, _day(), [{"product_id": w.socks, "qty": "4"}])
    with pytest.raises(PermissionDenied):
        sales.cancel(w.conn, w.buyer, sale, "Typo")
    sales.cancel(w.conn, w.boss2, sale, "Typo")
    assert _row(w, NORTH, w.socks)["good_m"] == 10000
    with pytest.raises(sqlite3.IntegrityError):
        w.conn.execute("UPDATE stock_sales SET status = 'POSTED' WHERE id = ?", (sale,))
    with pytest.raises(sqlite3.IntegrityError):
        w.conn.execute("DELETE FROM stock_sale_lines")


# ------------------------------------------------------------------ POS files

FILE = b"Item Code,Description,Qty Sold,Net Amount\r\nsock-1,Socks,2,240.00\r\nSOCK-1,Socks,1,120.00\r\nZZZ-9,Unknown,5,50\r\n,blank,,\r\n"


def test_a_pos_file_is_matched_by_sku_added_up_and_unmatched_lines_listed(stocked):
    w = stocked
    read = sales.read_file(w.conn, FILE)
    assert [(l["item_code"], l["qty"], l["amount"]) for l in read["lines"]] == [("SOCK-1", "3", "360.00")]
    assert read["unmatched"] == [{"code": "ZZZ-9", "qty": "5"}]
    out = sales.import_file(w.conn, w.r1, NORTH, _day(), FILE, "pos-today.csv")
    assert out["unmatched"] and _row(w, NORTH, w.socks)["good_m"] == 7000
    with pytest.raises(POError, match="already imported"):
        sales.import_file(w.conn, w.r1, NORTH, _day(), FILE, "pos-today-again.csv")


def test_a_pos_file_needs_sku_and_quantity_columns(stocked):
    with pytest.raises(POError, match="SKU"):
        sales.read_file(stocked.conn, b"name;total\nSocks;10\n")
    semicolons = sales.read_file(stocked.conn, b"SKU;Quantity;Price\nTEE-3;2;30\n")
    assert [(l["item_code"], l["amount"]) for l in semicolons["lines"]] == [("TEE-3", "60.00")]


# ------------------------------------------------------------------ reports

def test_sales_report_movers_and_shrinkage(stocked):
    w = stocked
    sales.record(w.conn, w.r1, NORTH, _day(1), [{"product_id": w.socks, "qty": "6"}])
    report = {r["item_code"]: r for r in sales.sales_report(w.conn, w.admin, _day(30), _day())}
    assert report["SOCK-1"]["amount_c"] == 72000 and report["SOCK-1"]["profit_c"] == 72000 - 46200
    with pytest.raises(PermissionDenied):
        sales.sales_report(w.conn, w.r1, _day(30), _day())
    movers = {r["item_code"]: r for r in sales.movers(w.conn, w.admin, NORTH)}
    assert movers["SOCK-1"]["sold_m"] == 6000 and movers["SOCK-1"]["days_left"] == 40  # 4 left, 0.1 a day over 60 days
    assert not movers["TEE-3"]["not_moving"]  # never sold, but it only just arrived
    count = inv.open_count(w.conn, w.buyer, NORTH)
    inv.save_count(w.conn, w.r1, count, {w.socks: "3", w.tee: "6"})
    inv.save_count(w.conn, w.r2, count, {w.socks: "3", w.tee: "6"})
    inv.post_count(w.conn, w.boss2, count)
    lost = sales.shrinkage(w.conn, w.admin, _day(1), _day())
    assert [(r["item_code"], r["missing_m"], r["missing_c"]) for r in lost] == [("SOCK-1", 1000, 7700)]
    assert sales.value_by_department(w.conn, w.admin)[0]["good_value_c"] > 0


# ------------------------------------------------------------------ reorder

def test_levels_come_from_sales_and_low_stock_is_suggested_up_to_the_target(stocked):
    w = stocked
    w.conn.execute("UPDATE system_settings SET value = '10' WHERE key = 'inventory.sales_days'")
    w.conn.executemany("INSERT OR REPLACE INTO system_settings(key, value, updated_at) VALUES (?, ?, '')",
                       [("inventory.sales_days", "10"), ("inventory.lead_days", "5"), ("inventory.cover_days", "5")])
    sales.record(w.conn, w.r1, NORTH, _day(), [{"product_id": w.socks, "qty": "8"}])  # 0.8 a day: minimum 4, target 8
    rows = {r["item_code"]: r for r in reorder.levels(w.conn, w.buyer, NORTH)}
    socks = rows["SOCK-1"]
    assert (socks["good_m"], socks["min_m"], socks["target_m"], socks["need_m"], socks["order_m"]) == (2000, 4000, 8000, 6000, 6000)
    assert "TEE-3" not in rows  # nothing sold, so nothing to keep up


def test_hand_set_levels_override_and_open_pos_count_as_coming(stocked):
    w = stocked
    reorder.set_levels(w.conn, w.buyer, NORTH, w.tee, "9", "12")
    tee = next(r for r in reorder.levels(w.conn, w.buyer, NORTH) if r["item_code"] == "TEE-3")
    assert tee["manual"] and (tee["need_m"], tee["order_m"]) == (6000, 2000)  # 6 pieces short is 2 packs of 3
    with pytest.raises(POError, match="below the minimum"):
        reorder.set_levels(w.conn, w.buyer, NORTH, w.tee, "9", "3")
    with pytest.raises(PermissionDenied):
        reorder.set_levels(w.conn, w.r1, NORTH, w.tee, "9", "12")
    # South still has 4 socks due on its issued PO, so a minimum of 3 isn't short there.
    reorder.set_levels(w.conn, w.buyer, SOUTH, w.socks, "3", "5")
    assert [r for r in reorder.levels(w.conn, w.buyer, SOUTH) if r["item_code"] == "SOCK-1"] == []
    reorder.set_levels(w.conn, w.buyer, NORTH, w.tee, "", "")
    assert all(r["item_code"] != "TEE-3" for r in reorder.levels(w.conn, w.buyer, NORTH))


def test_suggestions_become_one_draft_order_form_per_supplier(stocked):
    w = stocked
    reorder.set_levels(w.conn, w.buyer, NORTH, w.tee, "9", "12")
    reorder.set_levels(w.conn, w.buyer, NORTH, w.socks, "12", "15")
    picks = [(NORTH, w.tee), (NORTH, w.socks)]
    with pytest.raises(PermissionDenied):
        reorder.make_orders(w.conn, w.boss2, picks)
    [of_id] = reorder.make_orders(w.conn, w.buyer, picks, {(NORTH, w.socks): "7"})
    of = order_forms.get_of(w.conn, of_id)
    assert of["status"] == "DRAFT" and of["supplier_id"] == w.supplier
    got = {i["item_code"]: i["allocations_m"] for i in of["items"]}
    assert got == {"TEE-3": {NORTH: 2000}, "SOCK-1": {NORTH: 7000}}


# ------------------------------------------------------------------ screens

@pytest.fixture
def app():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def test_sales_and_reorder_join_the_inventory_sidebar(stocked, app):
    from atlaspo.ui.main_window import MainWindow
    w = stocked
    sales.record(w.conn, w.r1, NORTH, _day(), [{"product_id": w.socks, "qty": "1"}])
    window = MainWindow(w.conn, w.admin)
    names = [window.inv_nav.item(r).text() for r in window._inv_names]
    assert names[-3:] == ["Sales", "Reorder", "Inventory reports"]
    window.show_system("inventory")
    for row in window._inv_names:
        window.inv_nav.setCurrentRow(row)
    page = window._built("inv_sales")
    assert page.table.rowCount() == 1 and page.lines.columnCount() == 11
    receiver = MainWindow(w.conn, w.r1)
    assert [receiver.inv_nav.item(r).text() for r in receiver._inv_names] == ["Stock counts", "Adjustments", "Transfers", "Sales"]
    receiver.show_system("inventory")
    receiver.inv_nav.setCurrentRow(max(receiver._inv_names))
    assert receiver._built("inv_sales").lines.columnCount() == 9  # no cost or profit


def test_the_import_screen_shows_matches_and_what_was_left_out(stocked, app):
    from atlaspo.ui.sales_page import ImportSalesDialog
    d = ImportSalesDialog(None, stocked.conn, stocked.r1)
    d.load(FILE, "pos.csv")
    assert d.table.rowCount() == 1 and "ZZZ-9" in d.unmatched.text()
    d._save()
    assert d.result["sale_id"]


def test_the_reorder_screen_makes_draft_order_forms(stocked, app, monkeypatch):
    from atlaspo.ui import sales_page
    w = stocked
    reorder.set_levels(w.conn, w.buyer, NORTH, w.tee, "9", "12")
    monkeypatch.setattr(sales_page, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(sales_page, "info", lambda *a, **k: None)
    page = sales_page.ReorderPage(w.conn, w.buyer)
    assert page.table.rowCount() == 1 and page.table.item(0, page.C_ORDER).text() == "2"
    page.table.item(0, page.C_ORDER).setText("3")
    page.make_orders()
    of = w.conn.execute("SELECT id FROM order_forms WHERE status = 'DRAFT' ORDER BY id DESC").fetchone()[0]
    assert order_forms.get_of(w.conn, of)["items"][0]["allocations_m"] == {NORTH: 3000}
