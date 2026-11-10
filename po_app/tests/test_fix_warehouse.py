"""Central Warehouse fixes from the 2026-11-07 audit: amended POs stay on the list, two people saving the same delivery, more stickered than ordered,
Save and ship, a calmer search, empty-list wording, notes for people without the buttons, absurd figures, column widths and a clearer button name."""
from __future__ import annotations

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from atlaspo import auth, masters, order_forms, orders, warehouse
from atlaspo.db import POError
from atlaspo.ui import warehouse_page
from atlaspo.ui.warehouse_page import CentralWarehousePage

from conftest import NORTH, PASSWORD, SOUTH


@pytest.fixture
def app():
    yield QApplication.instance() or QApplication([])


def _order(world, stickers=False):
    if stickers:
        masters.save_supplier(world.conn, world.admin, {"code": "ABC", "name": "ABC Trading", "payment_terms": "30 D", "sticker_rate_c": 50}, world.supplier, confirm_duplicate=True)
    of_id = world.approved(items=[world.item(cost=10000, alloc={NORTH: 12, SOUTH: 6}, description="Socks", item_code="SOCK-1")])
    pos = {p["branch_code"]: p for p in order_forms.generate_pos(world.conn, world.buyer, of_id)}
    for p in pos.values():
        orders.issue(world.conn, world.buyer, p["id"])
    return of_id, pos


def _amend_lower(world, po_id):
    item = world.conn.execute("SELECT * FROM purchase_order_items WHERE po_id = ?", (po_id,)).fetchone()
    items = [{k: item[k] for k in item.keys()} | {"qty": "9", "qty_m": 9000}]
    orders.amend(world.conn, world.buyer, po_id, {}, items, "Supplier short")
    assert world.conn.execute("SELECT status FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()[0] == orders.APPROVED


class _Confirms:
    """Stands in for the confirm box: records each question and gives the answers in turn."""

    def __init__(self, *answers):
        self.answers, self.asked = list(answers), []

    def __call__(self, parent, title, message, yes="Yes"):
        self.asked.append((title, message, yes))
        return self.answers.pop(0) if self.answers else True


# ---------------------------------------------------------------- AUD-057

def test_amended_po_with_goods_at_central_stays_listed_and_waits_to_be_printed_again(app, world, monkeypatch):
    of_id, pos = _order(world)
    av, ro = pos[NORTH]["id"], pos[SOUTH]["id"]
    warehouse.receive(world.conn, world.warehouse, of_id, {av: "2", ro: "1"})
    _amend_lower(world, av)
    listed = {p["id"]: p for p in warehouse._pos(world.conn, of_id)}
    assert listed[av]["stage"] == warehouse.TO_SHIP and listed[av]["reissue"]
    assert warehouse.search(world.conn, world.warehouse)[0]["stages"] == {warehouse.TO_SHIP}
    # Asked for by name, it's refused with the reason.
    with pytest.raises(POError, match="Waiting to be printed again"):
        warehouse.ship(world.conn, world.warehouse, of_id, [av])
    # The screen shows the reason, and no Ship button for it.
    page = CentralWarehousePage(world.conn, world.warehouse)
    page._set_tab(warehouse.TO_SHIP)
    r = [p["id"] for p in page.pos].index(av)
    assert page.boxes.item(r, 5).text() == "Changed by Purchasing. Waiting to be printed again."
    assert page.boxes.cellWidget(r, 6) is None and page.boxes.cellWidget(1 - r, 6) is not None
    assert [p["id"] for p in page._to_ship()] == [ro]
    page.close()
    # Shipping the whole order sends the others and leaves it.
    warehouse.ship(world.conn, world.warehouse, of_id)
    assert warehouse.shipped_on(world.conn, ro) and not warehouse.shipped_on(world.conn, av)
    with pytest.raises(POError, match="Waiting to be printed again"):
        warehouse.ship(world.conn, world.warehouse, of_id)
    # Printed again, it ships.
    orders.issue(world.conn, world.buyer, av)
    warehouse.ship(world.conn, world.warehouse, of_id)
    assert warehouse.shipped_on(world.conn, av)


def test_approved_po_never_printed_is_not_listed(world):
    of_id = world.approved(items=[world.item(cost=10000, alloc={NORTH: 3}, description="Socks", item_code="SOCK-1")])
    order_forms.generate_pos(world.conn, world.buyer, of_id)
    assert warehouse._pos(world.conn, of_id) == []


# ---------------------------------------------------------------- AUD-090

def test_two_people_saving_the_same_first_receipt(app, world):
    of_id, pos = _order(world)
    av, ro = pos[NORTH]["id"], pos[SOUTH]["id"]
    other = auth.create_user(world.conn, world.admin, "wh2", "Second Warehouse", PASSWORD, ["WAREHOUSE"], must_change=False)
    # Both opened the order while nothing was recorded.
    seen = {p["id"]: (p["current"]["mark"] if p["current"] else None) for p in warehouse._pos(world.conn, of_id)}
    warehouse.receive(world.conn, world.warehouse, of_id, {av: "4", ro: "1"}, expect_delivery=seen)
    with pytest.raises(POError, match="Someone else saved this delivery. Open it again."):
        warehouse.receive(world.conn, other, of_id, {av: "9", ro: "1"}, expect_delivery=seen)
    assert warehouse._pos(world.conn, of_id)[0]["boxes"] == 4
    # Opened again, the correction goes through.
    seen = {p["id"]: p["current"]["mark"] for p in warehouse._pos(world.conn, of_id)}
    warehouse.receive(world.conn, other, of_id, {av: "9", ro: "1"}, expect_delivery=seen)
    # A bare delivery id says nothing about corrections, so it's never taken as up to date.
    with pytest.raises(POError, match="Someone else saved"):
        warehouse.receive(world.conn, other, of_id, {av: "3", ro: "1"}, expect_delivery={p["id"]: p["current"]["id"] for p in warehouse._pos(world.conn, of_id)})
    assert {p["id"]: p["boxes"] for p in warehouse._pos(world.conn, of_id)}[av] == 9


def test_the_screen_refuses_a_save_over_someone_elses(app, world):
    of_id, pos = _order(world)
    av, ro = pos[NORTH]["id"], pos[SOUTH]["id"]
    page = CentralWarehousePage(world.conn, world.warehouse)
    other = auth.create_user(world.conn, world.admin, "wh2", "Second Warehouse", PASSWORD, ["WAREHOUSE"], must_change=False)
    warehouse.receive(world.conn, other, of_id, {av: "4", ro: "1"})
    for r in range(page.boxes.rowCount()):
        page.boxes.item(r, warehouse_page.C_BOXES).setText("9")
    with pytest.raises(POError, match="Someone else saved"):
        page.save()
    assert {p["id"]: p["boxes"] for p in warehouse._pos(world.conn, of_id)} == {av: 4, ro: 1}
    page.close()


def test_two_people_correcting_the_same_delivery(world):
    of_id, pos = _order(world, stickers=True)
    av, ro = pos[NORTH]["id"], pos[SOUTH]["id"]
    other = auth.create_user(world.conn, world.admin, "wh2", "Second Warehouse", PASSWORD, ["WAREHOUSE"], must_change=False)
    pieces = {i["po_item_id"]: "1" for i in warehouse.lines(world.conn, world.warehouse, of_id)}
    warehouse.receive(world.conn, world.warehouse, of_id, {av: "2", ro: "1"}, pieces)
    # Both opened the delivery after it was received; the first correction goes through, even within the same second.
    seen = {p["id"]: p["current"]["mark"] for p in warehouse._pos(world.conn, of_id)}
    warehouse.receive(world.conn, world.warehouse, of_id, {av: "5", ro: "1"}, pieces, expect_delivery=seen)
    with pytest.raises(POError, match="Someone else saved this delivery. Open it again."):
        warehouse.receive(world.conn, other, of_id, {av: "7", ro: "1"}, pieces, expect_delivery=seen)
    assert {p["id"]: p["boxes"] for p in warehouse._pos(world.conn, of_id)}[av] == 5
    # A correction to the stickered pieces alone is caught too.
    seen = {p["id"]: p["current"]["mark"] for p in warehouse._pos(world.conn, of_id)}
    warehouse.receive(world.conn, world.warehouse, of_id, {av: "5", ro: "1"}, {k: "2" for k in pieces}, expect_delivery=seen)
    with pytest.raises(POError, match="Someone else saved"):
        warehouse.receive(world.conn, other, of_id, {av: "7", ro: "1"}, pieces, expect_delivery=seen)
    # Opened again, it goes through.
    seen = {p["id"]: p["current"]["mark"] for p in warehouse._pos(world.conn, of_id)}
    warehouse.receive(world.conn, other, of_id, {av: "7", ro: "1"}, pieces, expect_delivery=seen)
    assert {p["id"]: p["boxes"] for p in warehouse._pos(world.conn, of_id)}[av] == 7


def test_the_screen_refuses_a_correction_over_someone_elses(app, world):
    of_id, pos = _order(world)
    av, ro = pos[NORTH]["id"], pos[SOUTH]["id"]
    warehouse.receive(world.conn, world.warehouse, of_id, {av: "2", ro: "1"})
    page = CentralWarehousePage(world.conn, world.warehouse)
    page._set_tab(warehouse.TO_SHIP)
    other = auth.create_user(world.conn, world.admin, "wh2", "Second Warehouse", PASSWORD, ["WAREHOUSE"], must_change=False)
    seen = {p["id"]: p["current"]["mark"] for p in warehouse._pos(world.conn, of_id)}
    warehouse.receive(world.conn, other, of_id, {av: "5", ro: "1"}, expect_delivery=seen)
    r = [p["id"] for p in page.pos].index(av)
    page.boxes.item(r, warehouse_page.C_BOXES).setText("7")
    with pytest.raises(POError, match="Someone else saved"):
        page.save()
    assert {p["id"]: p["boxes"] for p in warehouse._pos(world.conn, of_id)}[av] == 5
    page.close()


# ---------------------------------------------------------------- AUD-061 (confirm only; refusing is Q6)

def test_more_stickered_than_ordered_asks_first(app, world, monkeypatch):
    of_id, pos = _order(world, stickers=True)
    page = CentralWarehousePage(world.conn, world.warehouse)
    for r in range(page.boxes.rowCount()):
        page.boxes.item(r, warehouse_page.C_BOXES).setText("1")
    ordered = {i["po_item_id"]: i["ordered_m"] // 1000 for i in page.items}
    for r, i in enumerate(page.items):
        page.stickers.item(r, warehouse_page.C_STICKERED).setText("120" if i["po_id"] == pos[NORTH]["id"] else str(ordered[i["po_item_id"]]))
    answers = _Confirms(False)
    monkeypatch.setattr(warehouse_page, "confirm", answers)
    assert page.save() is False
    assert answers.asked[0][1] == "You typed more stickered pieces than were ordered. Save anyway?"
    assert world.conn.execute("SELECT COUNT(*) FROM warehouse_deliveries").fetchone()[0] == 0
    # Within what was ordered, nothing is asked.
    answers = _Confirms()
    monkeypatch.setattr(warehouse_page, "confirm", answers)
    for r, i in enumerate(page.items):
        page.stickers.item(r, warehouse_page.C_STICKERED).setText(str(ordered[i["po_item_id"]]))
    assert page.save() is True and answers.asked == []
    page.close()


def test_more_stickered_than_ordered_saves_when_confirmed(app, world, monkeypatch):
    of_id, pos = _order(world, stickers=True)
    page = CentralWarehousePage(world.conn, world.warehouse)
    for r in range(page.boxes.rowCount()):
        page.boxes.item(r, warehouse_page.C_BOXES).setText("1")
    for r in range(page.stickers.rowCount()):
        page.stickers.item(r, warehouse_page.C_STICKERED).setText("120")
    monkeypatch.setattr(warehouse_page, "confirm", _Confirms(True))
    assert page.save() is True
    assert world.conn.execute("SELECT COUNT(*) FROM warehouse_deliveries").fetchone()[0] == 2
    page.close()


# ---------------------------------------------------------------- AUD-157

def test_ship_after_an_unsaved_correction_offers_save_and_ship(app, world, monkeypatch):
    of_id, pos = _order(world)
    av, ro = pos[NORTH]["id"], pos[SOUTH]["id"]
    warehouse.receive(world.conn, world.warehouse, of_id, {av: "2", ro: "1"})
    page = CentralWarehousePage(world.conn, world.warehouse)
    page._set_tab(warehouse.TO_SHIP)
    r = [p["id"] for p in page.pos].index(av)
    page.boxes.item(r, warehouse_page.C_BOXES).setText("3")
    assert of_id in page.drafts
    # Go back: nothing saved, nothing shipped.
    answers = _Confirms(False)
    monkeypatch.setattr(warehouse_page, "confirm", answers)
    page.ship_one(page.pos[r])
    assert answers.asked[0][2] == "Save and ship" and not warehouse.shipped_on(world.conn, av)
    assert {p["id"]: p["boxes"] for p in warehouse._pos(world.conn, of_id)}[av] == 2
    # Save and ship: one confirm saves the 3 and ships it.
    answers = _Confirms(True)
    monkeypatch.setattr(warehouse_page, "confirm", answers)
    page.ship_one(page.pos[r])
    assert len(answers.asked) == 1
    got = {p["id"]: p for p in warehouse._pos(world.conn, of_id)}
    assert got[av]["boxes_shipped"] == 3 and warehouse.shipped_on(world.conn, av) and not warehouse.shipped_on(world.conn, ro)
    page.close()


def test_mark_shipped_after_an_unsaved_correction_saves_and_ships(app, world, monkeypatch):
    of_id, pos = _order(world)
    av, ro = pos[NORTH]["id"], pos[SOUTH]["id"]
    warehouse.receive(world.conn, world.warehouse, of_id, {av: "2", ro: "1"})
    page = CentralWarehousePage(world.conn, world.warehouse)
    page._set_tab(warehouse.TO_SHIP)
    page.remarks.setText("One box crushed")
    page.remarks.textEdited.emit("One box crushed")
    monkeypatch.setattr(warehouse_page, "confirm", _Confirms(True))
    page.ship()
    assert warehouse.shipped_on(world.conn, av) and warehouse.shipped_on(world.conn, ro)
    assert world.conn.execute("SELECT remarks FROM warehouse_deliveries WHERE po_id = ?", (av,)).fetchone()[0] == "One box crushed"
    page.close()


def test_save_and_ship_from_the_to_receive_tab_still_ships(app, world, monkeypatch):
    # North received alone and is ready; South is still to receive. Saving South moves the order off the To receive tab.
    of_id, pos = _order(world)
    av, ro = pos[NORTH]["id"], pos[SOUTH]["id"]
    warehouse.receive(world.conn, world.warehouse, of_id, {av: "2"}, only=[av])
    page = CentralWarehousePage(world.conn, world.warehouse)
    page._set_tab(warehouse.TO_RECEIVE)
    ids = [p["id"] for p in page.pos]
    assert page.boxes.cellWidget(ids.index(av), 6) is not None
    page.boxes.item(ids.index(ro), warehouse_page.C_BOXES).setText("1")
    assert of_id in page.drafts
    told = []
    monkeypatch.setattr(warehouse_page, "confirm", _Confirms(True))
    monkeypatch.setattr(warehouse_page, "info", lambda parent, title, message: told.append(message))
    page.ship_one(page.pos[ids.index(av)])
    assert {p["id"]: p["boxes"] for p in warehouse._pos(world.conn, of_id)}[ro] == 1
    assert warehouse.shipped_on(world.conn, av) and not warehouse.shipped_on(world.conn, ro)
    assert told == []
    page.close()


def test_save_and_ship_says_so_when_nothing_is_left_to_ship(app, world, monkeypatch):
    of_id, pos = _order(world)
    av, ro = pos[NORTH]["id"], pos[SOUTH]["id"]
    warehouse.receive(world.conn, world.warehouse, of_id, {av: "2", ro: "1"})
    page = CentralWarehousePage(world.conn, world.warehouse)
    page._set_tab(warehouse.TO_SHIP)
    r = [p["id"] for p in page.pos].index(av)
    page.boxes.item(r, warehouse_page.C_BOXES).setText("3")
    # Someone else ships North meanwhile; the save is refused, so nothing ships and the error explains.
    warehouse.ship(world.conn, world.warehouse, of_id, [av])
    monkeypatch.setattr(warehouse_page, "confirm", _Confirms(True))
    with pytest.raises(POError, match="Someone else saved"):
        page.ship_one(page.pos[r])
    page.close()
    # Saved, but the branch can no longer ship (printed again pending): the person is told.
    of_id, pos = _order(world)
    av, ro = pos[NORTH]["id"], pos[SOUTH]["id"]
    warehouse.receive(world.conn, world.warehouse, of_id, {av: "2", ro: "1"})
    page = CentralWarehousePage(world.conn, world.warehouse)
    page._set_tab(warehouse.TO_SHIP)
    page.table.selectRow([x["id"] for x in page.rows].index(of_id))
    page._selected()
    r = [p["id"] for p in page.pos].index(av)
    page.boxes.item(r, warehouse_page.C_BOXES).setText("3")
    told = []
    monkeypatch.setattr(warehouse_page, "info", lambda parent, title, message: told.append(message))
    real_save = page.save

    def save_then_amend():
        ok = real_save()
        _amend_lower(world, av)
        return ok
    monkeypatch.setattr(page, "save", save_then_amend)
    page.ship_one(page.pos[r])
    assert told == ["Saved. Nothing left to ship for these branches."]
    assert {p["id"]: p["boxes"] for p in warehouse._pos(world.conn, of_id)}[av] == 3 and not warehouse.shipped_on(world.conn, av)
    page.close()


def test_note_names_a_branch_waiting_to_be_printed_again(app, world):
    of_id, pos = _order(world)
    av, ro = pos[NORTH]["id"], pos[SOUTH]["id"]
    warehouse.receive(world.conn, world.warehouse, of_id, {av: "2", ro: "1"})
    _amend_lower(world, av)
    page = CentralWarehousePage(world.conn, world.warehouse)
    page._set_tab(warehouse.TO_SHIP)
    assert "1 boxes ready" in page.note.text() and f"{pos[NORTH]['po_number']} waits to be printed again." in page.note.text()
    page.close()


# ---------------------------------------------------------------- AUD-145

def test_search_waits_for_a_pause_in_typing(app, world, monkeypatch):
    _order(world)
    page = CentralWarehousePage(world.conn, world.warehouse)
    calls = []
    real = warehouse_page.service.search
    monkeypatch.setattr(warehouse_page.service, "search", lambda *a, **k: calls.append(1) or real(*a, **k))
    for ch in "abc":
        page.search.setText(page.search.text() + ch)
    assert calls == []
    QTest.qWait(450)
    assert calls
    page.close()


# ---------------------------------------------------------------- AUD-007 / AUD-008

def test_no_match_names_the_search_and_keeps_the_header(app, world):
    _order(world)
    page = CentralWarehousePage(world.conn, world.warehouse)
    page.resize(1280, 800)
    page.show()
    app.processEvents()
    y = page.search.mapTo(page, page.search.rect().topLeft()).y()
    page.search.setText("zzqqxx")
    page.refresh()
    app.processEvents()
    assert page.empty.isVisible()
    assert page.empty.text() == 'Nothing matches "zzqqxx". Clear the search to see all 1.'
    assert page.search.mapTo(page, page.search.rect().topLeft()).y() == y
    # The Coming back tab says the same.
    page._set_tab(warehouse_page.BACK)
    assert page.empty.text() == 'Nothing matches "zzqqxx".'
    page.search.setText("")
    page.refresh()
    assert page.empty.text() == "Nothing on its way back from the branches."
    page.close()


# ---------------------------------------------------------------- AUD-017

def test_people_without_the_buttons_arent_told_to_press_them(app, world):
    of_id, pos = _order(world)
    page = CentralWarehousePage(world.conn, world.admin)
    assert not page.can_edit and page.rows
    assert "Save received" not in page.note.text() and "Waiting for the warehouse" in page.note.text()
    page.close()
    warehouse.receive(world.conn, world.warehouse, of_id, {pos[NORTH]["id"]: "2", pos[SOUTH]["id"]: "1"})
    page = CentralWarehousePage(world.conn, world.admin)
    page._set_tab(warehouse.TO_SHIP)
    assert "Mark shipped" not in page.note.text() and "corrected" not in page.hint.text()
    page.close()
    page = CentralWarehousePage(world.conn, world.warehouse)
    assert "Save received" in page.note.text() or not page.rows
    page._set_tab(warehouse.TO_SHIP)
    assert "Mark shipped" in page.note.text()
    page.close()


# ---------------------------------------------------------------- AUD-081

def test_absurd_boxes_and_pieces_are_refused(world):
    of_id, pos = _order(world, stickers=True)
    av, ro = pos[NORTH]["id"], pos[SOUTH]["id"]
    with pytest.raises(POError, match="far too many"):
        warehouse.receive(world.conn, world.warehouse, of_id, {av: str(10 ** 9 + 1), ro: "1"}, {})
    lines = warehouse.lines(world.conn, world.warehouse, of_id)
    with pytest.raises(POError, match="far too many"):
        warehouse.receive(world.conn, world.warehouse, of_id, {av: "1", ro: "1"}, {i["po_item_id"]: "100000000000000000" for i in lines})
    assert world.conn.execute("SELECT COUNT(*) FROM warehouse_deliveries").fetchone()[0] == 0
    # The limit itself is fine.
    warehouse.receive(world.conn, world.warehouse, of_id, {av: str(10 ** 9), ro: "1"}, {i["po_item_id"]: "6" for i in lines})


# ---------------------------------------------------------------- AUD-006

def test_stickers_and_supplier_columns_fit_their_text(app, world):
    page = CentralWarehousePage(world.conn, world.warehouse)
    assert page.table.columnWidth(4) >= page.table.fontMetrics().horizontalAdvance("Warehouse stickers") + 20
    assert page.returns.columnWidth(3) >= page.returns.fontMetrics().horizontalAdvance("Supplier") + 30
    page.close()


# ---------------------------------------------------------------- AUD-050 (rename only; flagging Print is Q24)

def test_another_delivery_button_says_where(app, world):
    page = CentralWarehousePage(world.conn, world.warehouse)
    assert page.another_btn.text() == "Another delivery at the warehouse"
    page.close()
