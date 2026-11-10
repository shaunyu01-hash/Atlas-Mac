"""Inventory, phase 1: stock read from purchasing, blind stock counts, adjustments and transfers."""
from __future__ import annotations

import sqlite3

import pytest

from atlaspo import auth, claim_stubs, inventory as inv, item_passes, order_forms, orders, receiving
from atlaspo.db import PermissionDenied, POError

from conftest import NORTH, PASSWORD, SOUTH


@pytest.fixture
def w(world):
    return make_world(world)


def make_world(world):
    """Socks (10 at North, 4 at South) and a 3-piece Tee pack (2 packs at North), issued. Two receivers per branch, an approver."""
    conn = world.conn
    conn.execute("UPDATE system_settings SET value = '' WHERE key = 'inventory.live_since'")
    of_id = world.approved(items=[world.item(cost=7700, alloc={NORTH: 10, SOUTH: 4}, description="Socks", item_code="SOCK-1"),
                                  world.item(cost=6000, uom="3 IN 1", alloc={NORTH: 2}, description="Tee pack", item_code="TEE-3")])
    pos = order_forms.generate_pos(conn, world.buyer, of_id)
    for p in pos:
        orders.issue(conn, world.buyer, p["id"])
    make = lambda name, branch: auth.create_user(conn, world.admin, name, name.title(), PASSWORD, ["RECEIVER"], must_change=False, branch_code=branch)
    world.r1, world.r2, world.s1, world.s2 = make("ana", NORTH), make("ben", NORTH), make("cara", SOUTH), make("dan", SOUTH)
    world.boss2 = auth.create_user(conn, world.admin, "mona", "Mona Manager", PASSWORD, ["APPROVER"], must_change=False)
    world.wh2 = auth.create_user(conn, world.admin, "wally", "Wally Warehouse", PASSWORD, ["WAREHOUSE"], must_change=False)
    world.north, world.south = pos[0], pos[1]
    world.socks_line, world.tee_line = [i["id"] for i in orders.get_po(conn, pos[0]["id"])["items"]]
    world.socks = inv.find_product(conn, "SOCK-1")["id"]
    world.tee = inv.find_product(conn, "TEE-3")["id"]
    return world


def _receive(w, socks="10", tee="6", damaged=None):
    for who in (w.r1, w.r2):
        receiving.save_count(w.conn, who, w.north["id"], {w.socks_line: socks, w.tee_line: tee}, damaged=damaged)


def _row(w, location, product):
    return next((r for r in inv.on_hand(w.conn, w.admin, location) if r["product_id"] == product), None)


# ------------------------------------------------------------------ stock from purchasing

def test_an_agreed_count_puts_goods_on_the_books_good_and_damaged(w):
    receiving.save_count(w.conn, w.r1, w.north["id"], {w.socks_line: "10", w.tee_line: "6"})
    assert _row(w, NORTH, w.socks) is None  # one count isn't a delivery
    receiving.save_count(w.conn, w.r2, w.north["id"], {w.socks_line: "10", w.tee_line: "6"})
    socks, tee = _row(w, NORTH, w.socks), _row(w, NORTH, w.tee)
    assert (socks["good_m"], socks["damaged_m"]) == (10000, 0)
    assert tee["good_m"] == 6000 and tee["unit"] == "PC"  # packs are kept in pieces
    assert tee["value_c"] == 12000  # 6 pieces at 60.00 a pack of 3


def test_damaged_pieces_are_kept_apart_from_stock_for_sale(w):
    _receive(w, damaged={w.socks_line: "2"})
    socks = _row(w, NORTH, w.socks)
    assert (socks["good_m"], socks["damaged_m"]) == (8000, 2000)


def test_orders_received_before_inventory_started_stay_off_the_books(w):
    _receive(w)
    w.conn.execute("UPDATE system_settings SET value = '2999-01-01' WHERE key = 'inventory.live_since'")
    assert inv.on_hand(w.conn, w.admin) == []


def test_damaged_goods_move_from_the_branch_to_the_warehouse_and_out_with_the_supplier(w):
    _receive(w, damaged={w.socks_line: "2"})
    cs = claim_stubs.issue(w.conn, w.buyer, w.north["id"], {(w.socks_line, "DAMAGED"): "2"})
    cs_id = cs["id"] if isinstance(cs, dict) else cs
    assert _row(w, NORTH, w.socks)["damaged_m"] == 2000  # still at the branch until the warehouse has it
    line = w.conn.execute("SELECT id FROM claim_stub_items WHERE cs_id = ?", (cs_id,)).fetchone()[0]
    claim_stubs.arrived(w.conn, w.warehouse, cs_id, {line: "2"})
    assert _row(w, NORTH, w.socks)["damaged_m"] == 0
    assert _row(w, inv.WAREHOUSE, w.socks)["held_m"] == 2000


def test_excess_sent_back_is_in_transit_then_held_at_the_warehouse(w):
    _receive(w, socks="12")
    assert _row(w, NORTH, w.socks)["good_m"] == 12000
    ip = item_passes.issue(w.conn, w.r1, w.north["id"])
    ip_id = ip["id"] if isinstance(ip, dict) else w.conn.execute("SELECT MAX(id) FROM item_passes").fetchone()[0]
    assert _row(w, NORTH, w.socks)["good_m"] == 10000
    assert _row(w, inv.WAREHOUSE, w.socks)["transit_m"] == 2000
    line = w.conn.execute("SELECT id FROM item_pass_items WHERE ip_id = ?", (ip_id,)).fetchone()[0]
    item_passes.arrived(w.conn, w.warehouse, ip_id, {line: "2"})
    held = _row(w, inv.WAREHOUSE, w.socks)
    assert (held["transit_m"], held["held_m"]) == (0, 2000)
    item_passes.collected(w.conn, w.warehouse, ip_id, "Driver")
    assert _row(w, inv.WAREHOUSE, w.socks) is None


def test_the_stock_card_shows_each_movement_with_what_was_left(w):
    _receive(w, damaged={w.socks_line: "1"})
    card = inv.stock_card(w.conn, w.admin, NORTH, w.socks)
    assert [(r["kind"], r["bucket"], r["qty_m"]) for r in card["rows"]] == [("RECEIVED", "GOOD", 9000), ("RECEIVED", "DAMAGED", 1000)]
    assert card["closing"] == {"GOOD": 9000, "DAMAGED": 1000, "HELD": 0}


def test_purchased_lines_not_in_the_catalog_are_listed_not_counted(w):
    receiving.save_count(w.conn, w.r1, w.north["id"], {w.socks_line: "10", w.tee_line: "6"}, extras=[{"item_code": "MYST-9", "description": "Mystery box", "pieces": "3"}])
    extra = -w.conn.execute("SELECT id FROM branch_extra_items").fetchone()[0]
    receiving.save_count(w.conn, w.r2, w.north["id"], {w.socks_line: "10", w.tee_line: "6", extra: "3"})
    lost = inv.not_in_catalog(w.conn, w.admin)
    assert [(r["item_code"], r["qty_m"]) for r in lost] == [("MYST-9", 3000)]


# ------------------------------------------------------------------ stock counts

def test_a_blind_double_count_corrects_the_books_and_shows_what_was_sold(w):
    _receive(w)
    count_id = inv.open_count(w.conn, w.buyer, NORTH)
    sheet = inv.count_sheet(w.conn, w.r1, count_id)
    assert {l["item_code"] for l in sheet["lines"]} == {"SOCK-1", "TEE-3"}
    assert all("book_m" not in l and "last_cost_c" not in l for l in sheet["lines"])  # blind
    assert inv.save_count(w.conn, w.r1, count_id, {w.socks: "7", w.tee: "6"})["outcome"] == "WAITING"
    with pytest.raises(POError, match="someone else"):
        inv.save_count(w.conn, w.r1, count_id, {w.socks: "7", w.tee: "6"})
    # They disagree on socks: only socks is counted again.
    assert inv.save_count(w.conn, w.r2, count_id, {w.socks: "8", w.tee: "6"}) == {"outcome": "AGAIN", "mismatched": 1}
    assert [l["item_code"] for l in inv.count_sheet(w.conn, w.r2, count_id)["lines"]] == ["SOCK-1"]
    inv.save_count(w.conn, w.r2, count_id, {w.socks: "7"})
    assert inv.save_count(w.conn, w.r1, count_id, {w.socks: "7"})["outcome"] == inv.AGREED
    results = inv.count_results(w.conn, w.buyer, count_id)
    assert {l["item_code"]: l["diff_m"] for l in results["lines"]} == {"SOCK-1": -3000, "TEE-3": 0}
    with pytest.raises(PermissionDenied):
        inv.post_count(w.conn, w.buyer, count_id)
    assert inv.post_count(w.conn, w.boss2, count_id) == 1
    assert _row(w, NORTH, w.socks)["good_m"] == 7000
    assert inv.movement_summary(w.conn, w.admin, "2000-01-01", "2999-12-31", NORTH)[-1]["out_m"] == 3000


def test_every_product_needs_a_figure_and_found_products_join_the_count(w):
    _receive(w)
    count_id = inv.open_count(w.conn, w.buyer, NORTH)
    with pytest.raises(POError, match="every product"):
        inv.save_count(w.conn, w.r1, count_id, {w.socks: "10"})
    with pytest.raises(POError, match="isn't in the product catalog"):
        inv.save_count(w.conn, w.r1, count_id, {w.socks: "10", w.tee: "6"}, found={"NOPE": "1"})
    w.conn.execute("INSERT INTO products(item_code, department_id, description, created_at, updated_at, last_cost_c) VALUES ('CAP-1', 1, 'Cap', '', '', 5000)")
    cap = inv.find_product(w.conn, "CAP-1")["id"]
    inv.save_count(w.conn, w.r1, count_id, {w.socks: "10", w.tee: "6"}, found={"cap-1": "2"})
    # The second person's sheet now lists the cap too, and it needs a figure like the rest.
    with pytest.raises(POError, match="CAP-1 has none"):
        inv.save_count(w.conn, w.r2, count_id, {w.socks: "10", w.tee: "6"})
    assert inv.save_count(w.conn, w.r2, count_id, {w.socks: "10", w.tee: "6", cap: "3"})["outcome"] == "AGAIN"
    assert [l["item_code"] for l in inv.count_sheet(w.conn, w.r1, count_id)["lines"]] == ["CAP-1"]


def test_counting_belongs_to_the_place_and_one_count_at_a_time(w):
    count_id = inv.open_count(w.conn, w.buyer, NORTH)
    with pytest.raises(POError, match="already being counted"):
        inv.open_count(w.conn, w.buyer, NORTH)
    with pytest.raises(POError, match="isn't your branch"):
        inv.count_sheet(w.conn, w.s1, count_id)
    with pytest.raises(PermissionDenied):
        inv.count_results(w.conn, w.r1, count_id)
    with pytest.raises(PermissionDenied):
        inv.open_count(w.conn, w.r1, NORTH)
    assert [c["id"] for c in inv.counts(w.conn, w.s1)] == []
    inv.cancel_count(w.conn, w.buyer, count_id, "Wrong day")
    with pytest.raises(POError, match="finished"):
        inv.count_sheet(w.conn, w.r1, count_id)


def test_the_warehouse_counts_only_the_warehouse(w):
    count_id = inv.open_count(w.conn, w.buyer, inv.WAREHOUSE)
    with pytest.raises(POError, match="Nothing is listed"):
        inv.save_count(w.conn, w.warehouse, count_id, {})
    inv.save_count(w.conn, w.warehouse, count_id, {}, found={"SOCK-1": "5"})
    assert inv.save_count(w.conn, w.wh2, count_id, {w.socks: "5"})["outcome"] == inv.AGREED
    inv.post_count(w.conn, w.admin, count_id)
    assert _row(w, inv.WAREHOUSE, w.socks)["good_m"] == 5000
    north = inv.open_count(w.conn, w.buyer, NORTH)
    with pytest.raises(POError, match="isn't your branch"):
        inv.count_sheet(w.conn, w.warehouse, north)


# ------------------------------------------------------------------ adjustments

def test_an_adjustment_waits_for_someone_else_to_approve_it(w):
    _receive(w, damaged={w.socks_line: "2"})
    adj = inv.prepare_adjustment(w.conn, w.r1, NORTH, "WRITE_OFF", [{"product_id": w.socks, "qty": "2"}], "Torn")
    assert _row(w, NORTH, w.socks)["damaged_m"] == 2000
    with pytest.raises(PermissionDenied):
        inv.decide_adjustment(w.conn, w.buyer, adj, True)
    with pytest.raises(POError, match="reason"):
        inv.decide_adjustment(w.conn, w.boss2, adj, False)
    inv.decide_adjustment(w.conn, w.boss2, adj, True)
    assert _row(w, NORTH, w.socks)["damaged_m"] == 0
    with pytest.raises(POError, match="already decided"):
        inv.decide_adjustment(w.conn, w.admin, adj, False, "No")


def test_the_preparer_never_approves_and_found_damaged_moves_stock(w):
    _receive(w)
    adj = inv.prepare_adjustment(w.conn, w.admin, NORTH, "DAMAGED", [{"product_id": w.socks, "qty": "3"}])
    with pytest.raises(POError, match="someone else"):
        inv.decide_adjustment(w.conn, w.admin, adj, True)
    inv.decide_adjustment(w.conn, w.boss2, adj, True)
    socks = _row(w, NORTH, w.socks)
    assert (socks["good_m"], socks["damaged_m"]) == (7000, 3000)
    with pytest.raises(POError, match="Say what happened"):
        inv.prepare_adjustment(w.conn, w.admin, NORTH, "OTHER", [{"product_id": w.socks, "qty": "-1", "bucket": "GOOD"}])


def test_stock_may_go_below_zero_and_is_flagged(w):
    adj = inv.prepare_adjustment(w.conn, w.buyer, SOUTH, "INTERNAL_USE", [{"product_id": w.socks, "qty": "2"}])
    inv.decide_adjustment(w.conn, w.boss2, adj, True)
    rows = inv.on_hand(w.conn, w.admin, SOUTH, below_zero=True)
    assert [(r["item_code"], r["good_m"], r["below_zero"]) for r in rows] == [("SOCK-1", -2000, True)]


# ------------------------------------------------------------------ transfers

def test_a_transfer_leaves_now_and_arrives_when_counted_in_blind(w):
    _receive(w)
    t = inv.send_transfer(w.conn, w.r1, NORTH, SOUTH, {w.socks: "4"})
    assert _row(w, NORTH, w.socks)["good_m"] == 6000
    assert _row(w, SOUTH, w.socks)["transit_m"] == 4000
    assert inv.transfer_lines(w.conn, w.s1, t)[0]["sent_m"] is None  # the receiving end counts blind
    with pytest.raises(POError, match="isn't your branch"):
        inv.receive_transfer(w.conn, w.r2, t, {w.socks: "4"})
    assert inv.receive_transfer(w.conn, w.s1, t, {w.socks: "3"}) == {"short_m": 1000, "over_m": 0}
    south = _row(w, SOUTH, w.socks)
    assert (south["good_m"], south["transit_m"]) == (3000, 0)


def test_a_transfer_on_the_way_can_be_cancelled_by_management(w):
    _receive(w)
    t = inv.send_transfer(w.conn, w.buyer, NORTH, inv.WAREHOUSE, {w.socks: "4"})
    with pytest.raises(PermissionDenied):
        inv.cancel_transfer(w.conn, w.buyer, t, "Mistake")
    inv.cancel_transfer(w.conn, w.boss2, t, "Mistake")
    assert _row(w, NORTH, w.socks)["good_m"] == 10000
    with pytest.raises(POError, match="finished"):
        inv.receive_transfer(w.conn, w.warehouse, t, {w.socks: "4"})


# ------------------------------------------------------------------ who sees what, and what can't change

def test_receivers_and_the_warehouse_never_see_the_books(w):
    for who in (w.r1, w.warehouse):
        with pytest.raises(PermissionDenied):
            inv.on_hand(w.conn, who)
        with pytest.raises(PermissionDenied):
            inv.stock_card(w.conn, who, NORTH, w.socks)
    assert inv.locations(w.conn, w.r1) == [{"code": NORTH, "name": inv.place_name(w.conn, NORTH)}]
    assert [r["code"] for r in inv.locations(w.conn, w.warehouse)] == [inv.WAREHOUSE]


def test_movements_and_numbers_are_permanent(w):
    _receive(w)
    t = inv.send_transfer(w.conn, w.buyer, NORTH, SOUTH, {w.socks: "1"})
    for sql in ("UPDATE stock_movements SET qty_m = 5", "DELETE FROM stock_movements", "DELETE FROM stock_transfers",
                "UPDATE stock_number_sequences SET last_number = 0"):
        with pytest.raises(sqlite3.IntegrityError):
            w.conn.execute(sql)
    with pytest.raises(sqlite3.IntegrityError):
        w.conn.execute("INSERT INTO stock_transfer_lines(transfer_id, product_id, sent_m) VALUES (?, ?, 1)", (t, w.tee))
    assert inv.transfers(w.conn, w.admin)[0]["transfer_number"].endswith("-ST-00001")


# ------------------------------------------------------------------ screens

@pytest.fixture
def app():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def _entries(window):
    return [window.inv_nav.item(r).text() for r in window._inv_names]


def test_the_switch_opens_inventory_and_goes_back_to_purchasing(w, app):
    from atlaspo.ui.main_window import MainWindow
    _receive(w)
    window = MainWindow(w.conn, w.admin)
    assert window.nav.isVisibleTo(window) and not window.inv_nav.isVisibleTo(window)
    window.show_system("inventory")
    assert window.inv_nav.isVisibleTo(window) and not window.nav.isVisibleTo(window)
    assert window.inv_switch.isChecked() and not window.po_switch.isChecked()
    assert _entries(window) == ["Stock on hand", "Stock counts", "Adjustments", "Transfers", "Sales", "Reorder", "Inventory reports"]
    for row in window._inv_names:
        window.inv_nav.setCurrentRow(row)
        assert window.stack.currentWidget() is window._built(window._inv_names[row])
    stock = window._built("inv_stock")
    assert stock.table.rowCount() == 2
    stock.table.selectRow(0)
    assert stock.card.rowCount() == 1
    window.show_system("po")
    assert window.nav.isVisibleTo(window) and window.po_switch.isChecked() and not window.inv_switch.isChecked()


def test_receivers_get_only_counting_adjustments_transfers_and_sales(w, app):
    from atlaspo.ui.main_window import MainWindow
    window = MainWindow(w.conn, w.r1)
    assert _entries(window) == ["Stock counts", "Adjustments", "Transfers", "Sales"]
    window.show_system("inventory")
    counts = window._built("inv_counts")
    assert counts.lines.columnCount() == 6  # their own figures only, never the books


def test_a_count_sheet_saves_from_the_screen(w, app, monkeypatch):
    from atlaspo.ui.inventory_page import CountSheetDialog
    _receive(w)
    count_id = inv.open_count(w.conn, w.buyer, NORTH)
    d = CountSheetDialog(None, w.conn, w.r1, count_id)
    for r in range(d.table.rowCount()):
        d.table.item(r, 5).setText("10" if d.table.item(r, 0).text() == "SOCK-1" else "6")
    from atlaspo.ui import inventory_page
    monkeypatch.setattr(inventory_page, "confirm", lambda *a, **k: True)
    d._save()
    assert d.outcome["outcome"] == "WAITING"
