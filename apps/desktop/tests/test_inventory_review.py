"""Inventory: the fixes from the review of phases 1 and 2. Each test is one finding, put right."""
from __future__ import annotations

import dataclasses
import sqlite3
from datetime import timedelta

import pytest

from atlaspo import claim_stubs, inventory as inv, item_passes, masters, order_forms, receiving, reorder, stock_sales as sales
from atlaspo.db import PermissionDenied, POError, today

from conftest import NORTH, SOUTH
from test_inventory import _receive, _row, make_world


def _day(back: int = 0) -> str:
    return (today() - timedelta(days=back)).isoformat()


@pytest.fixture
def w(world):
    w = make_world(world)
    w.conn.execute("UPDATE products SET selling_c = 12000 WHERE item_code = 'SOCK-1'")
    return w


# ------------------------------------------------------------------ stock

def test_a_movement_after_the_start_date_counts_even_when_its_po_was_first_received_before(w, clock):
    clock(2026, 11, 1, 9, 0)
    _receive(w, socks="12")
    w.conn.execute("UPDATE system_settings SET value = '2026-11-05' WHERE key = 'inventory.live_since'")
    assert _row(w, NORTH, w.socks) is None  # received before the start: the opening count will set it
    clock(2026, 11, 6, 9, 0)
    item_passes.issue(w.conn, w.r1, w.north["id"])
    assert _row(w, NORTH, w.socks)["good_m"] == -2000  # the excess going back after the start leaves the books


def test_a_count_is_posted_against_the_books_when_each_product_was_counted(w, clock):
    clock(2026, 11, 9, 7, 0)
    _receive(w)
    clock(2026, 11, 9, 8, 0)
    count = inv.open_count(w.conn, w.buyer, NORTH)
    clock(2026, 11, 9, 9, 0)
    sales.record(w.conn, w.r1, NORTH, "2026-11-09", [{"product_id": w.socks, "qty": "2"}])  # typed in before the counters got there
    clock(2026, 11, 9, 10, 0)
    inv.save_count(w.conn, w.r1, count, {w.socks: "8", w.tee: "6"})
    inv.save_count(w.conn, w.r2, count, {w.socks: "8", w.tee: "6"})
    assert {l["item_code"]: l["diff_m"] for l in inv.count_results(w.conn, w.admin, count)["lines"]} == {"SOCK-1": 0, "TEE-3": 0}
    assert inv.post_count(w.conn, w.boss2, count) == 0
    assert _row(w, NORTH, w.socks)["good_m"] == 8000  # not 6: the sale isn't taken off twice
    assert inv.count_results(w.conn, w.admin, count)["lines"][0]["book_m"] == 8000


def test_transfers_wait_for_a_count_and_a_count_waits_for_transfers(w):
    _receive(w)
    t = inv.send_transfer(w.conn, w.r1, NORTH, SOUTH, {w.socks: "2"})
    with pytest.raises(POError, match="on its way"):
        inv.open_count(w.conn, w.buyer, SOUTH)
    inv.receive_transfer(w.conn, w.s1, t, {w.socks: "2"})
    inv.open_count(w.conn, w.buyer, SOUTH)
    t2 = inv.send_transfer(w.conn, w.r1, NORTH, SOUTH, {w.socks: "1"})
    with pytest.raises(POError, match="being counted"):
        inv.receive_transfer(w.conn, w.s1, t2, {w.socks: "1"})


def test_only_someone_at_the_place_counts_a_transfer_in(w):
    _receive(w)
    t = inv.send_transfer(w.conn, w.r1, NORTH, inv.WAREHOUSE, {w.socks: "1"})
    with pytest.raises(PermissionDenied):
        inv.receive_transfer(w.conn, w.admin, t, {w.socks: "1"})
    inv.receive_transfer(w.conn, w.wh2, t, {w.socks: "1"})


def test_a_receiver_without_a_branch_works_nowhere(w):
    lost = dataclasses.replace(w.r1, branch_code=None)
    assert inv.scope(lost) == set()
    with pytest.raises(POError, match="isn't your branch"):
        sales.record(w.conn, lost, SOUTH, _day(), [{"product_id": w.socks, "qty": "1"}])


def test_a_claim_stub_cancelled_at_the_warehouse_leaves_its_goods_there_as_damaged(w):
    _receive(w, damaged={w.socks_line: "2"})
    cs = claim_stubs.issue(w.conn, w.buyer, w.north["id"], {(w.socks_line, "DAMAGED"): "2"})
    cs_id = cs["id"] if isinstance(cs, dict) else cs
    line = w.conn.execute("SELECT id FROM claim_stub_items WHERE cs_id = ?", (cs_id,)).fetchone()[0]
    claim_stubs.arrived(w.conn, w.warehouse, cs_id, {line: "2"})
    claim_stubs.cancel(w.conn, w.admin, cs_id, "Supplier credited it instead")
    held = _row(w, inv.WAREHOUSE, w.socks)
    assert (held["held_m"], held["damaged_m"]) == (0, 2000)


def test_pieces_per_unit_cant_change_once_the_product_has_stock(w):
    _receive(w)
    p = dict(inv.find_product(w.conn, "TEE-3"))
    with pytest.raises(POError, match="pieces per unit"):
        masters.save_product(w.conn, w.buyer, {**p, "pieces_per_unit": 1}, p["id"])


def test_decided_papers_cant_be_edited_even_directly(w):
    _receive(w)
    adj = inv.prepare_adjustment(w.conn, w.buyer, NORTH, "WRITE_OFF", [{"product_id": w.socks, "qty": "1", "bucket": "GOOD"}])
    inv.decide_adjustment(w.conn, w.boss2, adj, True)
    with pytest.raises(sqlite3.IntegrityError):
        w.conn.execute("UPDATE stock_adjustments SET decision_note = 'changed' WHERE id = ?", (adj,))
    with pytest.raises(sqlite3.IntegrityError):
        w.conn.execute("INSERT INTO stock_movements(location_code, product_id, bucket, qty_m, kind, ref_id, moved_by, moved_at)"
                       " VALUES (1, ?, 'GOOD', 5000, 'ADJUSTMENT', 999, 1, '2026-01-01')", (w.socks,))


# ------------------------------------------------------------------ sales

def test_a_sale_far_below_the_catalog_price_waits_for_management(w):
    _receive(w)
    sale = sales.record(w.conn, w.r1, NORTH, _day(), [{"product_id": w.socks, "qty": "9", "amount": "0"}])
    assert sales.is_waiting(w.conn, sale) and _row(w, NORTH, w.socks)["good_m"] == 10000
    assert sales.waiting(w.conn, w.boss2) == 1
    with pytest.raises(PermissionDenied):
        sales.decide(w.conn, w.r2, sale, True)
    sales.decide(w.conn, w.boss2, sale, True)
    assert _row(w, NORTH, w.socks)["good_m"] == 1000
    normal = sales.record(w.conn, w.r1, NORTH, _day(), [{"product_id": w.socks, "qty": "1"}])
    assert not sales.is_waiting(w.conn, normal)


def test_a_return_to_stock_for_sale_waits_and_a_rejected_one_never_counts(w):
    _receive(w)
    sales.record(w.conn, w.r1, NORTH, _day(), [{"product_id": w.socks, "qty": "2", "price": "60"}])
    back = sales.record(w.conn, w.r1, NORTH, _day(), [{"product_id": w.socks, "qty": "2"}], kind=sales.RETURN)
    assert sales.paper_lines(w.conn, w.admin, back)[0]["price_c"] == 6000  # what it last sold for here, not the catalog's 120.00
    assert _row(w, NORTH, w.socks)["good_m"] == 8000
    with pytest.raises(POError, match="Give a reason"):
        sales.decide(w.conn, w.boss2, back, False)
    sales.decide(w.conn, w.boss2, back, False, "No such customer")
    assert _row(w, NORTH, w.socks)["good_m"] == 8000
    state = {p["id"]: p["state"] for p in sales.papers(w.conn, w.admin)}
    assert state[back] == sales.REJECTED
    damaged = sales.record(w.conn, w.r1, NORTH, _day(), [{"product_id": w.socks, "qty": "1", "bucket": "DAMAGED"}], kind=sales.RETURN)
    assert not sales.is_waiting(w.conn, damaged)


def test_nobody_cancels_their_own_sales_or_sales_a_later_count_took_in(w, clock):
    clock(2026, 11, 8, 9, 0)
    _receive(w)
    clock(2026, 11, 8, 18, 0)
    sale = sales.record(w.conn, w.admin, NORTH, "2026-11-08", [{"product_id": w.socks, "qty": "1"}])
    with pytest.raises(POError, match="someone else cancels"):
        sales.cancel(w.conn, w.admin, sale, "Typo")
    clock(2026, 11, 9, 8, 0)
    inv.open_count(w.conn, w.buyer, NORTH)
    with pytest.raises(POError, match="already took these"):
        sales.cancel(w.conn, w.boss2, sale, "Typo")


def test_a_count_of_one_department_only_blocks_that_departments_older_sales(w, clock):
    clock(2026, 11, 9, 8, 0)
    _receive(w)
    inv.open_count(w.conn, w.buyer, NORTH, department_id=2)
    sales.record(w.conn, w.r1, NORTH, "2026-11-08", [{"product_id": w.socks, "qty": "1"}])  # socks are department 1


def test_lines_with_no_cost_have_no_profit_and_transfer_losses_are_missing_stock(w):
    _receive(w)
    w.conn.execute("UPDATE products SET last_cost_c = NULL WHERE id = ?", (w.tee,))
    sales.record(w.conn, w.r1, NORTH, _day(), [{"product_id": w.tee, "qty": "1", "price": "30"}])
    tee = next(r for r in sales.sales_report(w.conn, w.admin, _day(1), _day()) if r["item_code"] == "TEE-3")
    assert tee["amount_c"] == 3000 and (tee["cost_c"], tee["profit_c"], tee["margin_bp"]) == (None, None, None)
    t = inv.send_transfer(w.conn, w.r1, NORTH, SOUTH, {w.socks: "3"})
    inv.receive_transfer(w.conn, w.s1, t, {w.socks: "1"})
    lost = sales.shrinkage(w.conn, w.admin, _day(1), _day(), SOUTH)
    assert [(r["how"], r["item_code"], r["missing_m"]) for r in lost] == [("TRANSFER", "SOCK-1", 2000)]
    assert sales.shrinkage(w.conn, w.admin, _day(1), _day(), NORTH) == []


def test_by_department_counts_each_product_once(w):
    _receive(w)
    inv.send_transfer(w.conn, w.r1, NORTH, SOUTH, {w.socks: "1"})
    w.conn.execute("UPDATE system_settings SET value = '' WHERE key = 'inventory.live_since'")
    total = sum(r["products"] for r in sales.value_by_department(w.conn, w.admin))
    assert total == 2


# ------------------------------------------------------------------ reorder and cost

def test_goods_on_a_draft_order_form_are_not_suggested_again(w):
    _receive(w)
    reorder.set_levels(w.conn, w.buyer, NORTH, w.socks, "12", "15")
    reorder.make_orders(w.conn, w.buyer, [(NORTH, w.socks)])
    socks = [r for r in reorder.levels(w.conn, w.buyer, NORTH, only_low=False) if r["product_id"] == w.socks][0]
    assert socks["on_order_m"] == 5000 and not socks["low"]
    with pytest.raises(POError, match="no longer needs ordering"):
        reorder.make_orders(w.conn, w.buyer, [(NORTH, w.socks)])


def test_an_inactive_supplier_is_named_and_not_orderable(w):
    _receive(w)
    reorder.set_levels(w.conn, w.buyer, NORTH, w.socks, "12", "15")
    w.conn.execute("UPDATE suppliers SET status = 'INACTIVE' WHERE id = ?", (w.supplier,))
    socks = [r for r in reorder.levels(w.conn, w.buyer, NORTH) if r["product_id"] == w.socks][0]
    assert not socks["orderable"] and socks["why_not"] == "Supplier is inactive"
    with pytest.raises(POError, match="supplier is inactive"):
        reorder.make_orders(w.conn, w.buyer, [(NORTH, w.socks)])


def test_reorder_drafts_carry_the_box_size(w):
    _receive(w)
    w.conn.execute("UPDATE products SET box_size = 12 WHERE id = ?", (w.socks,))
    reorder.set_levels(w.conn, w.buyer, NORTH, w.socks, "12", "15")
    [of_id] = reorder.make_orders(w.conn, w.buyer, [(NORTH, w.socks)])
    assert order_forms.get_of(w.conn, of_id)["items"][0]["box_size"] == 12


def test_an_approved_order_updates_the_catalog_cost(w):
    w.approved(items=[w.item(cost=9900, alloc={NORTH: 1}, description="Socks", item_code="SOCK-1")])
    assert inv.find_product(w.conn, "SOCK-1")["last_cost_c"] == 9900
    w.approved(items=[w.item(cost=6600, uom="3 IN 1", alloc={NORTH: 1}, description="Tee pack", item_code="TEE-3")])
    assert inv.find_product(w.conn, "TEE-3")["last_cost_c"] == 6600


def test_short_pieces_charged_on_a_debit_memo_are_not_still_coming(w):
    from atlaspo import debit_memos
    for who in (w.s1, w.s2):
        receiving.save_count(w.conn, who, w.south["id"], {i["id"]: "1" for i in w.conn.execute("SELECT id FROM purchase_order_items WHERE po_id = ?", (w.south["id"],))})
    assert reorder.on_order(w.conn).get((SOUTH, w.socks)) == 3000
    line = w.conn.execute("SELECT id FROM purchase_order_items WHERE po_id = ?", (w.south["id"],)).fetchone()[0]
    debit_memos.issue(w.conn, w.buyer, w.south["id"], {(line, "SHORT"): "3"})
    assert reorder.on_order(w.conn).get((SOUTH, w.socks)) is None


# ------------------------------------------------------------------ screens

@pytest.fixture
def app():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def test_enter_in_the_product_box_adds_a_line_and_never_saves(w, app):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from atlaspo.ui.inventory_page import AdjustmentDialog
    _receive(w)
    d = AdjustmentDialog(None, w.conn, w.buyer)
    d.show()
    d.lines.find.setText("SOCK-1")
    QTest.keyClick(d.lines.find, Qt.Key.Key_Return)
    assert d.lines.table.rowCount() == 1 and d.isVisible()
    assert w.conn.execute("SELECT COUNT(*) FROM stock_adjustments").fetchone()[0] == 0
    d.done(0)


def test_escape_asks_before_throwing_typing_away(w, app, monkeypatch):
    from atlaspo.ui import inventory_page
    _receive(w)
    asked = []
    monkeypatch.setattr(inventory_page, "confirm", lambda *a, **k: asked.append(a) or False)
    d = inventory_page.AdjustmentDialog(None, w.conn, w.buyer)
    d.show()
    d.reject()  # nothing typed: closes without asking
    assert not asked and not d.isVisible()
    d = inventory_page.AdjustmentDialog(None, w.conn, w.buyer)
    d.show()
    d.lines.find.setText("SOCK-1")
    d.lines.add()
    d.reject()
    assert asked and d.isVisible()
    d.done(0)


def test_a_new_order_form_from_inventory_brings_the_purchasing_sidebar_back(w, app):
    from atlaspo.ui.main_window import MainWindow
    window = MainWindow(w.conn, w.admin)
    window.show_system("inventory")
    window.new_of()
    assert window.system == "po" and window.po_switch.isChecked() and not window.inv_nav.isVisibleTo(window)
    window.show_system("inventory")
    assert window.system == "inventory"


def test_reorder_keeps_what_was_typed_when_the_list_refreshes(w, app):
    from atlaspo.ui.sales_page import ReorderPage
    _receive(w)
    reorder.set_levels(w.conn, w.buyer, NORTH, w.tee, "9", "12")
    page = ReorderPage(w.conn, w.buyer)
    page.table.item(0, page.C_ORDER).setText("5")
    page.everything.setChecked(True)
    page.everything.setChecked(False)
    assert page.table.item(0, page.C_ORDER).text() == "5"
    viewer = ReorderPage(w.conn, w.boss2)
    from PySide6.QtWidgets import QAbstractItemView
    assert viewer.table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers  # the approver can't make OFs, so can't type here
