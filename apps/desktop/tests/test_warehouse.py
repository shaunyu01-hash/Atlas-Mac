"""Central Warehouse: boxes for each branch, pieces stickered for goods that come without stickers, then each branch shipped on. The branches still count."""
from __future__ import annotations

import sqlite3

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from atlaspo import auth, debit_memos, masters, order_forms, orders, payments, receiving, warehouse
from atlaspo.db import PermissionDenied, POError
from atlaspo.ui import warehouse_page
from atlaspo.ui.main_window import MainWindow

from conftest import NORTH, PASSWORD, SOUTH


@pytest.fixture
def app():
    yield QApplication.instance() or QApplication([])


def _order(world, stickers=False):
    if stickers:
        masters.save_supplier(world.conn, world.admin, {"code": "ABC", "name": "ABC Trading", "payment_terms": "30 D", "sticker_rate_c": 50}, world.supplier, confirm_duplicate=True)
    of_id = world.approved(items=[world.item(cost=10000, alloc={NORTH: 12, SOUTH: 6}, description="Socks", item_code="SOCK-1"),
                                  world.item(cost=20000, alloc={NORTH: 2}, description="Cap", item_code="CAP-1")])
    pos = {p["branch_code"]: p for p in order_forms.generate_pos(world.conn, world.buyer, of_id)}
    for p in pos.values():
        orders.issue(world.conn, world.buyer, p["id"])
    return of_id, pos


def _receivers(world, branch=NORTH):
    return [auth.create_user(world.conn, world.admin, f"rec{branch}{n}", f"Rec {n}", PASSWORD, ["RECEIVER"], must_change=False, branch_code=branch) for n in (1, 2)]


def _count(world, people, po_id, pieces, boxes=None):
    for who in people:
        sheet = receiving.sheet(world.conn, who, po_id)
        receiving.save_count(world.conn, who, po_id, {line["id"]: str(pieces.get(line["item_code"], 0)) for line in sheet}, boxes=boxes)


def test_an_order_waits_for_its_boxes_then_each_branch_ships(world):
    of_id, pos = _order(world)
    row = warehouse.search(world.conn, world.warehouse)[0]
    assert (row["id"], row["stage"], row["stickers"], len(row["pos"])) == (of_id, warehouse.TO_RECEIVE, False, 2)
    # Only the warehouse records it.
    with pytest.raises(PermissionDenied):
        warehouse.receive(world.conn, world.buyer, of_id, {pos[NORTH]["id"]: "2", pos[SOUTH]["id"]: "1"})
    with pytest.raises(POError, match="South"):
        warehouse.receive(world.conn, world.warehouse, of_id, {pos[NORTH]["id"]: "2"})
    with pytest.raises(POError, match="waiting to ship"):
        warehouse.ship(world.conn, world.warehouse, of_id)
    # A branch that got nothing in this delivery is 0 boxes.
    warehouse.receive(world.conn, world.warehouse, of_id, {pos[NORTH]["id"]: "2", pos[SOUTH]["id"]: "0"})
    assert warehouse.search(world.conn, world.warehouse)[0]["stages"] == {warehouse.TO_SHIP}
    # Until a branch ships, its figure can be corrected. The first receiver stays on record and the log keeps the earlier figure.
    other = auth.create_user(world.conn, world.admin, "wh2", "Second Warehouse", PASSWORD, ["WAREHOUSE"], must_change=False)
    warehouse.receive(world.conn, other, of_id, {pos[NORTH]["id"]: "3", pos[SOUTH]["id"]: "0"}, remarks="One box crushed")
    row = warehouse.search(world.conn, world.warehouse)[0]
    assert row["boxes"] == 3 and {p["received_name"] for p in row["pos"]} == {"Nestor Warehouse"} and row["pos"][0]["remarks"] == "One box crushed"
    assert world.conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action = 'WAREHOUSE_CORRECT'").fetchone()[0] == 2  # both branches, for the remarks
    # Each branch ships on its own.
    warehouse.ship(world.conn, world.warehouse, of_id, [pos[NORTH]["id"]])
    row = warehouse.search(world.conn, world.warehouse)[0]
    assert row["stages"] == {warehouse.TO_SHIP, warehouse.SHIPPED} and warehouse.shipped_on(world.conn, pos[NORTH]["id"]) and not warehouse.shipped_on(world.conn, pos[SOUTH]["id"])
    warehouse.ship(world.conn, world.warehouse, of_id)
    assert warehouse.search(world.conn, world.warehouse)[0]["stages"] == {warehouse.SHIPPED}
    with pytest.raises(POError, match="waiting"):
        warehouse.receive(world.conn, world.warehouse, of_id, {pos[NORTH]["id"]: "9", pos[SOUTH]["id"]: "9"})
    # Shipped stays on record, even through REPLACE.
    for sql in ("UPDATE warehouse_deliveries SET boxes = 9", "DELETE FROM warehouse_deliveries",
                "REPLACE INTO warehouse_deliveries(id, po_id, delivery_no, boxes, received_by, received_at) SELECT id, po_id, delivery_no, 9, received_by, received_at FROM warehouse_deliveries"):
        with pytest.raises(sqlite3.IntegrityError):
            world.conn.execute(sql)
    # The branches still make their own two counts.
    assert pos[NORTH]["id"] in [r["id"] for r in receiving.to_count(world.conn, _receivers(world)[0])]


def test_a_second_delivery_gets_its_own_record(world):
    of_id, pos = _order(world)
    warehouse.receive(world.conn, world.warehouse, of_id, {pos[NORTH]["id"]: "2", pos[SOUTH]["id"]: "0"})
    warehouse.ship(world.conn, world.warehouse, of_id)
    row = warehouse.search(world.conn, world.warehouse)[0]
    assert row["can_more"]
    with pytest.raises(POError, match="Type the boxes"):
        warehouse.receive(world.conn, world.warehouse, of_id, {}, another=True)
    warehouse.receive(world.conn, world.warehouse, of_id, {pos[SOUTH]["id"]: "1"}, another=True)
    row = warehouse.search(world.conn, world.warehouse)[0]
    south = next(p for p in row["pos"] if p["id"] == pos[SOUTH]["id"])
    assert (south["stage"], south["delivery_no"], south["boxes"], len(south["deliveries"])) == (warehouse.TO_SHIP, 2, 1, 2)
    warehouse.ship(world.conn, world.warehouse, of_id)
    assert warehouse.boxes_sent(world.conn, pos[SOUTH]["id"]) == 1 and warehouse.boxes_sent(world.conn, pos[NORTH]["id"]) == 2


def test_finished_and_paid_orders_leave_to_receive(world):
    _order(world)
    rows = warehouse.search(world.conn, world.warehouse)
    assert len(rows) == 1
    # Once a branch counted it without the warehouse, it isn't on its way to the warehouse any more.
    of_id, pos = _order(world)
    _count(world, _receivers(world), pos[NORTH]["id"], {"SOCK-1": 12, "CAP-1": 2})
    row = next(r for r in warehouse.search(world.conn, world.warehouse) if r["id"] == of_id)
    assert [p["id"] for p in row["pos"]] == [pos[SOUTH]["id"]]
    # Paid orders leave too.
    world.conn.execute("INSERT INTO payment_checks(check_number, source, check_date, supplier_id, amount_c, status, recorded_by, recorded_at) VALUES ('1', 'EXTERNAL', '2026-11-07', ?, 100, 'ACTIVE', ?, '2026-11-06')",
                       (world.supplier, world.admin.id))
    world.conn.execute("INSERT INTO payment_check_ofs(check_id, of_id, amount_c) VALUES (1, ?, 100)", (of_id,))
    assert of_id not in [r["id"] for r in warehouse.search(world.conn, world.warehouse)]


def test_goods_without_stickers_are_counted_as_they_are_stickered(world):
    of_id, pos = _order(world, stickers=True)
    row = warehouse.search(world.conn, world.warehouse)[0]
    assert row["stickers"]
    lines = warehouse.lines(world.conn, world.warehouse, of_id)
    assert [(l["branch_name"], l["description"], l["ordered_m"]) for l in lines] == [("North", "Socks", 12000), ("North", "Cap", 2000), ("South", "Socks", 6000)]
    boxes = {pos[NORTH]["id"]: "2", pos[SOUTH]["id"]: "1"}
    with pytest.raises(POError, match="stickered"):
        warehouse.receive(world.conn, world.warehouse, of_id, boxes, {lines[0]["po_item_id"]: "12"})
    warehouse.receive(world.conn, world.warehouse, of_id, boxes, {lines[0]["po_item_id"]: "12", lines[1]["po_item_id"]: "2", lines[2]["po_item_id"]: "5"})
    assert [l["stickered_m"] for l in warehouse.lines(world.conn, world.warehouse, of_id)] == [12000, 2000, 5000]
    # A correction keeps the old figure in the log, per product.
    warehouse.receive(world.conn, world.warehouse, of_id, boxes, {lines[0]["po_item_id"]: "11", lines[1]["po_item_id"]: "2", lines[2]["po_item_id"]: "5"})
    logged = world.conn.execute("SELECT previous_value, new_value FROM audit_logs WHERE action = 'WAREHOUSE_CORRECT'").fetchone()
    assert "12" in logged[0] and "11" in logged[1]
    # The sticker decision comes from the supplier as it was when the OF was approved, as the payment does.
    masters.save_supplier(world.conn, world.admin, {"code": "ABC", "name": "ABC Trading", "payment_terms": "30 D", "sticker_rate_c": 0}, world.supplier, confirm_duplicate=True)
    assert warehouse.search(world.conn, world.warehouse)[0]["stickers"]


def test_pieces_lost_between_the_warehouse_and_the_branch_are_not_the_suppliers(world):
    """The warehouse stickered 12 Socks for North; the branch counted 8. The supplier delivered 12, so nothing is short on their side."""
    of_id, pos = _order(world, stickers=True)
    lines = warehouse.lines(world.conn, world.warehouse, of_id)
    warehouse.receive(world.conn, world.warehouse, of_id, {pos[NORTH]["id"]: "3", pos[SOUTH]["id"]: "1"},
                      {lines[0]["po_item_id"]: "12", lines[1]["po_item_id"]: "2", lines[2]["po_item_id"]: "6"})
    warehouse.ship(world.conn, world.warehouse, of_id)
    people = _receivers(world)
    # The branch types the boxes too, once the warehouse shipped it.
    with pytest.raises(POError, match="boxes"):
        receiving.save_count(world.conn, people[0], pos[NORTH]["id"], {line["id"]: "1" for line in receiving.sheet(world.conn, people[0], pos[NORTH]["id"])})
    _count(world, people, pos[NORTH]["id"], {"SOCK-1": 8, "CAP-1": 2}, boxes="2")
    line = next(l for l in receiving.lines_of(world.conn, pos[NORTH]["id"]) if l["item_code"] == "SOCK-1")
    assert (line["received_m"], line["left_central_m"], line["supplier_m"], line["transit_m"]) == (8000, 12000, 12000, 4000)
    assert receiving.boxes_check(world.conn, pos[NORTH]["id"]) == {"sent": 3, "arrived": 2, "missing": 1, "disagree": []}
    assert debit_memos._outstanding(world.conn, pos[NORTH]["id"]) == []
    money = payments.settlement(world.conn, of_id)
    assert next(p for p in money["pos"] if p["po_id"] == pos[NORTH]["id"])["unclaimed_c"] == 0


def test_orders_printed_before_the_warehouse_started_arent_asked_for(world):
    _order(world)
    world.conn.execute("UPDATE purchase_orders SET issued_at = '2020-01-01 09:00:00'")
    world.conn.execute("UPDATE system_settings SET value = '2021-01-01' WHERE key = 'warehouse.live_since'")
    world.conn.execute("PRAGMA recursive_triggers = OFF")
    world.conn.execute("DROP TRIGGER issued_versions_no_update")
    world.conn.execute("UPDATE po_issued_versions SET issued_at = '2020-01-01 09:00:00'")
    assert warehouse.search(world.conn, world.warehouse) == []


def test_the_warehouse_screen(app, world, monkeypatch):
    of_id, pos = _order(world, stickers=True)
    monkeypatch.setattr(warehouse_page, "confirm", lambda *a, **k: True)
    win = MainWindow(world.conn, world.warehouse)
    assert [t for t, _ in win.pages] == ["Central Warehouse", "Claim stubs", "Item passes"]
    page = win.central
    assert page.tabs.tabText(0) == "To receive  1" and page.save_btn.isVisibleTo(page) and not page.ship_btn.isVisibleTo(page) and page.stickers.isVisibleTo(page)
    for r, n in enumerate(("2", "1")):
        page.boxes.item(r, warehouse_page.C_BOXES).setText(n)
    # What's typed survives a trip to another tab.
    page.tabs.setCurrentIndex(2)
    page.tabs.setCurrentIndex(0)
    assert page.boxes.item(0, warehouse_page.C_BOXES).text() == "2"
    for r, n in enumerate(("12", "2", "6")):
        page.stickers.item(r, warehouse_page.C_STICKERED).setText(n)
    page.save()
    assert page.tabs.tabText(1) == "To ship  1"
    page.tabs.setCurrentIndex(1)
    assert page.ship_btn.isVisibleTo(page) and page.save_btn.text() == "Save changes" and page.boxes.item(0, warehouse_page.C_BOXES).text() == "2"
    # Ship only North.
    page.boxes.clearSelection()
    page.boxes.selectRow(0)
    assert page.ship_btn.text() == "Mark 1 shipped"
    page.ship()
    assert warehouse.shipped_on(world.conn, pos[NORTH]["id"]) and not warehouse.shipped_on(world.conn, pos[SOUTH]["id"])
    page.tabs.setCurrentIndex(1)
    page.ship()
    page.tabs.setCurrentIndex(2)
    assert page.tabs.tabText(2) == "Shipped  1" and not page.save_btn.isVisibleTo(page) and "shipped" in page.note.text()
    assert not page.boxes.item(0, warehouse_page.C_BOXES).flags() & Qt.ItemFlag.ItemIsEditable
    # Another delivery for a branch that isn't finished.
    assert page.another_btn.isVisibleTo(page)
    page._start_another()
    assert page.save_btn.isVisibleTo(page) and page.boxes.item(1, warehouse_page.C_BOXES).flags() & Qt.ItemFlag.ItemIsEditable
    win.close()
    # The office and Accounting see it and can't change it.
    accounting = auth.create_user(world.conn, world.admin, "acct", "Ann Accounts", PASSWORD, ["ACCOUNTING"], must_change=False)
    for who in (world.buyer, accounting):
        office = MainWindow(world.conn, who)
        assert "Central Warehouse" in [t for t, _ in office.pages] and office.central.save_btn.parent() is None
        office.close()


def test_each_branch_ready_to_ship_has_its_own_ship_button(app, world, monkeypatch):
    of_id, pos = _order(world)
    warehouse.receive(world.conn, world.warehouse, of_id, {pos[NORTH]["id"]: "2", pos[SOUTH]["id"]: "1"})
    monkeypatch.setattr(warehouse_page, "confirm", lambda *a, **k: True)
    win = MainWindow(world.conn, world.warehouse)
    page = win.central
    page.tabs.setCurrentIndex(1)
    assert page.boxes.cellWidget(0, 6) is not None and page.boxes.cellWidget(1, 6) is not None
    page.ship_one(next(p for p in page.pos if p["id"] == pos[SOUTH]["id"]))
    assert warehouse.shipped_on(world.conn, pos[SOUTH]["id"]) and not warehouse.shipped_on(world.conn, pos[NORTH]["id"])
    win.close()


def test_branch_buttons_show_one_branch_and_receive_it_alone(app, world):
    of_id, pos = _order(world)
    win = MainWindow(world.conn, world.warehouse)
    page = win.central
    assert page.branch_buttons[SOUTH].text().endswith("1") and page.branch_buttons[None].text().endswith("1")
    page._set_branch(SOUTH)
    assert [p["id"] for p in page.pos] == [pos[SOUTH]["id"]]
    page.boxes.item(0, warehouse_page.C_BOXES).setText("1")
    page.save()
    rows = warehouse.search(world.conn, world.warehouse)
    stages = {p["id"]: p["stage"] for p in rows[0]["pos"]}
    assert stages == {pos[SOUTH]["id"]: warehouse.TO_SHIP, pos[NORTH]["id"]: warehouse.TO_RECEIVE}
    win.close()


def test_goods_coming_back_are_counted_on_central_warehouse_too(app, world, monkeypatch):
    from atlaspo import claim_stubs, item_passes
    from atlaspo.ui import claim_stub_page
    of_id, pos = _order(world)
    people = _receivers(world)
    for who in people:
        sheet = receiving.sheet(world.conn, who, pos[NORTH]["id"])
        receiving.save_count(world.conn, who, pos[NORTH]["id"], {l["id"]: "14" if l["item_code"] == "SOCK-1" else "2" for l in sheet})
    item_passes.issue(world.conn, people[0], pos[NORTH]["id"])
    win = MainWindow(world.conn, world.warehouse)
    page = win.central
    assert page.tabs.tabText(3) == "Coming back  1"
    page.tabs.setCurrentIndex(3)
    assert page.returns.item(0, 0).text() == "Item pass" and page.count_btn.isVisibleTo(page)
    monkeypatch.setattr(claim_stub_page, "count_goods", lambda parent, number, lines: {i["id"]: str(i["pieces_m"] // 1000) for i in lines})
    page.count_return()
    assert page.tabs.tabText(3) == "Coming back  0" and item_passes.search(world.conn, world.warehouse)[0]["status"] == item_passes.AT_WAREHOUSE
    win.close()
