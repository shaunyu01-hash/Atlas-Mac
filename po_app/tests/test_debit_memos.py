"""Debit memos: charging a supplier for what two receivers agreed was short or damaged."""
from __future__ import annotations

import os
import sqlite3
from io import BytesIO

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from pypdf import PdfReader
from PySide6.QtWidgets import QApplication, QPushButton

from atlaspo import item_passes, auth, db, debit_memos as dm, masters, order_forms, orders, payments, pdf, receiving
from atlaspo.db import POError
from atlaspo.debit_memos import SHORT
from atlaspo.receiving import COMPLETED, TO_CHECK
from atlaspo.ui.debit_memo_page import C_PIECES, DebitMemoDialog
from atlaspo.ui.main_window import MainWindow

from conftest import NORTH, PASSWORD, SOUTH


@pytest.fixture
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def setup(world):
    """Socks: 10 at 77.77 a piece. Tee pack: 6 pieces, 3 IN 1 at 50.00 a pack. Both for North."""
    of_id = world.approved(items=[world.item(cost=7777, alloc={NORTH: 10, SOUTH: 4}, description="Socks", item_code="SOCK-1"),
                                  world.item(cost=5000, uom="3 IN 1", alloc={NORTH: 2, SOUTH: 1}, description="Tee pack", item_code="TEE-3")])
    pos = order_forms.generate_pos(world.conn, world.buyer, of_id)
    for p in pos:
        orders.issue(world.conn, world.buyer, p["id"])
    make = lambda name: auth.create_user(world.conn, world.admin, name, name.title(), PASSWORD, ["RECEIVER"], must_change=False, branch_code=NORTH)
    world.r1, world.r2 = make("ana"), make("ben")
    world.po_id = pos[0]["id"]
    world.socks, world.tee = [i["id"] for i in orders.get_po(world.conn, world.po_id)["items"]]
    return world


def _received(w, socks="10", tee="6", **kw):
    for who in (w.r1, w.r2):
        result = receiving.save_count(w.conn, who, w.po_id, {w.socks: socks, w.tee: tee}, **kw)
    return result


def _tab(w):
    return receiving.po_results(w.conn, w.buyer)[0]["tab"]


def test_only_what_is_short_or_damaged_is_charged_at_net_cost(setup):
    w = setup
    _received(w, "8", "4", damaged={w.tee: "1"})
    lines = dm.outstanding(w.conn, w.buyer, w.po_id)
    assert [(l["description"], l["reason"], l["pieces_m"], l["amount_c"]) for l in lines] == [
        ("Socks", SHORT, 2000, 15554),  # 2 at 77.77
        ("Tee pack", SHORT, 2000, 3333)]  # 2 of a pack of 3 at 50.00 is 33.33, worked out from the pack
    made = dm.issue(w.conn, w.buyer, w.po_id, {(l["po_item_id"], l["reason"]): "2" for l in lines}, "Short on delivery")
    assert made["total_c"] == 15554 + 3333 and made["dm_number"].endswith("-DM-00001") and made["status"] == dm.ISSUED
    assert made["prepared_name"] == "Bea Buyer" and made["pieces_m"] == 4000 and made["po_id"] == w.po_id


def test_a_covered_delivery_moves_to_completed_and_cancelling_brings_it_back(setup):
    w = setup
    _received(w, "8", "6")
    assert _tab(w) == TO_CHECK and dm.can_issue(w.conn, w.po_id)
    # Part of the shortage doesn't settle it.
    first = dm.issue(w.conn, w.buyer, w.po_id, {(w.socks, SHORT): "1"})
    assert _tab(w) == TO_CHECK and [l["pieces_m"] for l in dm.outstanding(w.conn, w.buyer, w.po_id)] == [1000]
    second = dm.issue(w.conn, w.buyer, w.po_id, {(w.socks, SHORT): "1"})
    assert _tab(w) == COMPLETED and not dm.can_issue(w.conn, w.po_id)
    row = receiving.po_results(w.conn, w.buyer)[0]
    assert row["debit_memos"] == f"{first['dm_number']}, {second['dm_number']}"
    with pytest.raises(POError, match="Write the reason"):
        dm.cancel(w.conn, w.admin, second["id"], "  ")
    dm.cancel(w.conn, w.admin, second["id"], "Wrong supplier copy")
    assert _tab(w) == TO_CHECK and [l["pieces_m"] for l in dm.outstanding(w.conn, w.buyer, w.po_id)] == [1000]
    with pytest.raises(POError, match="already cancelled"):
        dm.cancel(w.conn, w.admin, second["id"], "Again")
    # Its number is never used again.
    third = dm.issue(w.conn, w.buyer, w.po_id, {(w.socks, SHORT): "1"})
    assert third["dm_number"].endswith("-DM-00003")
    assert w.conn.execute("SELECT COUNT(*) FROM debit_memos").fetchone()[0] == 3


def test_nobody_can_charge_more_than_is_outstanding(setup):
    w = setup
    _received(w, "8", "6")
    with pytest.raises(POError, match="only 2 pieces"):
        dm.issue(w.conn, w.buyer, w.po_id, {(w.socks, SHORT): "3"})
    with pytest.raises(POError, match="nothing to charge"):
        dm.issue(w.conn, w.buyer, w.po_id, {(w.socks, SHORT): "0"})
    # A product that isn't short can't be charged, even by asking.
    with pytest.raises(POError, match="nothing to charge"):
        dm.issue(w.conn, w.buyer, w.po_id, {(w.tee, SHORT): "1"})
    with pytest.raises((POError, ValueError)):
        dm.issue(w.conn, w.buyer, w.po_id, {(w.socks, SHORT): "-1"})
    assert not w.conn.execute("SELECT 1 FROM debit_memos").fetchone()
    assert not w.conn.execute("SELECT 1 FROM dm_number_sequences").fetchone()  # a refused memo uses no number


def test_it_waits_until_the_counts_agree(setup):
    w = setup
    receiving.save_count(w.conn, w.r1, w.po_id, {w.socks: "8", w.tee: "6"})
    assert not dm.can_issue(w.conn, w.po_id)
    with pytest.raises(POError, match="still being counted"):
        dm.outstanding(w.conn, w.buyer, w.po_id)
    with pytest.raises(POError, match="still being counted"):
        dm.issue(w.conn, w.buyer, w.po_id, {(w.socks, SHORT): "1"})


def test_extra_pieces_are_not_charged_and_stay_to_check_until_returned(setup):
    w = setup
    _received(w, "11", "6")
    assert not dm.can_issue(w.conn, w.po_id) and _tab(w) == TO_CHECK
    item_passes.issue(w.conn, w.r1, w.po_id)
    assert _tab(w) == COMPLETED


def test_a_later_delivery_that_covers_a_charged_shortage_goes_back_to_check(setup):
    w = setup
    _received(w, "8", "6")
    made = dm.issue(w.conn, w.buyer, w.po_id, {(w.socks, SHORT): "2"})
    assert _tab(w) == COMPLETED
    _received(w, "2", "0")
    # The supplier delivered after all, so the charge no longer matches until someone cancels it.
    assert _tab(w) == TO_CHECK and not dm.can_issue(w.conn, w.po_id)
    dm.cancel(w.conn, w.admin, made["id"], "Supplier delivered the rest")
    assert _tab(w) == COMPLETED


def test_the_document_is_kept_and_shows_only_what_the_supplier_should_see(setup):
    w = setup
    _received(w, "8", "6")
    made = dm.issue(w.conn, w.buyer, w.po_id, {(w.socks, SHORT): "2"}, "See your DR")
    stored = dm.document(w.conn, w.buyer, made["id"])
    text = PdfReader(BytesIO(stored)).pages[0].extract_text()
    for wanted in ("DEBIT MEMO", "Approved by", made["dm_number"], "ABC Trading", "Socks", "SOCK-1", "Short", "155.54", "See your DR", orders.get_po(w.conn, w.po_id)["po_number"]):
        assert wanted in text, wanted
    assert "Tee pack" not in text  # nothing was charged for it
    assert w.conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action = 'DM_PRINT'").fetchone()[0] == 1
    # A cancelled memo keeps the exact file it was issued with.
    dm.cancel(w.conn, w.admin, made["id"], "Wrong")
    assert w.conn.execute("SELECT pdf FROM debit_memos WHERE id = ?", (made["id"],)).fetchone()[0] == stored


def test_a_memo_keeps_its_own_copy_of_the_product_and_cost(setup):
    w = setup
    _received(w, "8", "6")
    made = dm.issue(w.conn, w.buyer, w.po_id, {(w.socks, SHORT): "2"})
    line = dm.items(w.conn, w.buyer, made["id"])[0]
    assert (line["description"], line["reason"], line["pieces_m"], line["net_unit_c"], line["amount_c"]) == ("Socks", SHORT, 2000, 7777, 15554)


def test_records_can_not_be_altered_even_in_the_database(setup):
    w = setup
    _received(w, "8", "6")
    made = dm.issue(w.conn, w.buyer, w.po_id, {(w.socks, SHORT): "2"})
    for sql in ("UPDATE debit_memos SET total_c = 1", "UPDATE debit_memos SET dm_number = 'X'", "UPDATE debit_memos SET pdf = x'00'", "UPDATE debit_memos SET po_id = 99", "DELETE FROM debit_memos",
                "UPDATE debit_memo_items SET amount_c = 1", "DELETE FROM debit_memo_items", "UPDATE dm_number_sequences SET last_number = 0", "DELETE FROM dm_number_sequences"):
        with pytest.raises(sqlite3.Error):
            w.conn.execute(sql)
    assert made


def test_external_number_is_saved_and_can_be_searched(setup):
    w = setup
    _received(w, "8", "6")
    made = dm.issue(w.conn, w.buyer, w.po_id, {(w.socks, SHORT): "2"})
    dm.save_external_number(w.conn, w.buyer, made["id"], " 7501 ")
    assert dm.search(w.conn, w.buyer, "7501")[0]["legacy_dm_number"] == "7501"
    assert not dm.search(w.conn, w.buyer, "nothing-like-this")
    assert [r["id"] for r in dm.search(w.conn, w.buyer, status=dm.ISSUED)] == [made["id"]] and not dm.search(w.conn, w.buyer, status=dm.CANCELLED)


def test_who_can_do_what(setup, world):
    w = setup
    _received(w, "8", "6")
    viewer = auth.create_user(w.conn, w.admin, "vic", "Vic", PASSWORD, ["VIEWER"], must_change=False)
    approver = auth.create_user(w.conn, w.admin, "amy", "Amy", PASSWORD, ["APPROVER"], must_change=False)
    for who in (viewer, approver, w.r1):
        with pytest.raises(db.PermissionDenied):
            dm.issue(w.conn, who, w.po_id, {(w.socks, SHORT): "1"})
        with pytest.raises(db.PermissionDenied):
            dm.outstanding(w.conn, who, w.po_id)
    made = dm.issue(w.conn, w.admin, w.po_id, {(w.socks, SHORT): "1"})
    with pytest.raises(db.PermissionDenied):
        dm.cancel(w.conn, viewer, made["id"], "No")
    assert dm.search(w.conn, viewer)  # the home office can read them
    for fn, args in ((dm.search, ()), (dm.items, (made["id"],)), (dm.document, (made["id"],)), (dm.get, (made["id"],))):
        with pytest.raises(db.PermissionDenied):
            fn(w.conn, w.r1, *args)


def test_issue_from_the_received_page(app, setup, monkeypatch):
    w = setup
    opened = []
    monkeypatch.setattr("atlaspo.ui.common.os.startfile", lambda p: opened.append(p), raising=False)
    monkeypatch.setattr("atlaspo.ui.debit_memo_page.confirm", lambda *a, **k: True)
    _received(w, "8", "6")
    win = MainWindow(w.conn, w.buyer)
    assert "Debit memos" in {t for t, _ in win.pages}
    page = win.received
    assert not page.dm_btn.isEnabled()
    page.table.selectRow(0)
    assert page.dm_btn.isEnabled()
    d = DebitMemoDialog(page, w.conn, w.buyer, page.rows[0])
    assert d.table.rowCount() == 1 and d.table.item(0, C_PIECES).text() == "2" and "155.54" in d.total.text()
    # Lowering a figure updates the amount, and only the pieces can be typed in.
    d.table.item(0, C_PIECES).setText("1")
    assert d.table.item(0, 7).text() == "77.77" and "77.77" in d.total.text()
    d.table.item(0, C_PIECES).setText("2")
    d.remarks.setText("Short")
    d.issue()
    assert d.issued["total_c"] == 15554 and len(opened) == 1
    page.refresh()
    assert page.tabs.tabText(0) == "To check  0" and page.tabs.tabText(1) == "Completed delivery  1"
    page.tabs.setCurrentIndex(1)
    assert page.table.item(0, 8).text() == d.issued["dm_number"] and page.table.item(0, 10).text() == "Completed with debit memo"

    memos = win.debit_memos
    memos.refresh()
    assert memos.tabs.tabText(0) == "Approved  1" and memos.table.item(0, 0).text() == d.issued["dm_number"] and memos.table.item(0, 7).text() == "155.54"
    assert memos.table.item(0, 9).text() == "Bea Buyer" and memos.table.horizontalHeaderItem(9).text() == "Approved by"
    memos.table.selectRow(0)
    # Cancelling gives money back to the supplier, so a Purchaser isn't offered it.
    assert memos.lines.rowCount() == 1 and memos.lines.item(0, 4).text() == "Short" and not memos.cancel_btn.isEnabled()
    assert memos.note.text().startswith("Approved by Bea Buyer")
    monkeypatch.setattr("atlaspo.ui.common.confirm", lambda *a, **k: True)
    memos.print_again()
    assert len(opened) == 2 and memos.table.item(0, 10).text() == "2 times"
    memos.external.setText("7501")
    memos.save_external()
    assert memos.table.item(0, 8).text() == "7501"
    win.close()
    # Management cancels it.
    win = MainWindow(w.conn, w.admin)
    memos = win.debit_memos
    memos.refresh()
    monkeypatch.setattr("atlaspo.ui.debit_memo_page.ask_text", lambda *a, **k: "Wrong")
    memos.table.selectRow(0)
    assert memos.cancel_btn.isEnabled()
    memos.cancel()
    assert memos.tabs.tabText(0) == "Approved  0" and memos.tabs.tabText(1) == "Cancelled  1" and memos.table.isHidden()
    win.received.refresh()
    assert win.received.tabs.tabText(0) == "To check  1"
    win.close()


def _finish(w):
    """Every other PO on the OF arrives exactly as ordered."""
    of_id = orders.get_po(w.conn, w.po_id)["of_id"]
    for po in order_forms.get_of(w.conn, of_id)["pos"]:
        if po["id"] != w.po_id:
            make = lambda name: auth.create_user(w.conn, w.admin, name, name.title(), PASSWORD, ["RECEIVER"], must_change=False, branch_code=po["branch_code"])
            lines = {i["id"]: i["qty_m"] * i["pieces_per_unit"] // 1000 for i in orders.get_po(w.conn, po["id"])["items"]}
            for who in (make("rosa1"), make("rosa2")):
                receiving.save_count(w.conn, who, po["id"], lines)
    return of_id


def test_the_office_of_waits_until_the_whole_delivery_is_complete(setup):
    w = setup
    of_id = orders.get_po(w.conn, w.po_id)["of_id"]
    assert not receiving.of_delivery_complete(w.conn, of_id)
    with pytest.raises(POError, match="once its delivery is complete"):
        pdf.office_copy(w.conn, w.buyer, of_id)
    _received(w, "8", "6")
    # One branch counted and short, and the other not counted: not yet.
    assert not receiving.of_delivery_complete(w.conn, of_id)
    _finish(w)
    assert not receiving.of_delivery_complete(w.conn, of_id)  # the shortage isn't settled
    with pytest.raises(POError, match="once its delivery is complete"):
        pdf.office_copy(w.conn, w.buyer, of_id)
    made = dm.issue(w.conn, w.buyer, w.po_id, {(w.socks, SHORT): "2"})
    assert receiving.of_delivery_complete(w.conn, of_id) and pdf.office_ready(w.conn, of_id)
    assert not pdf.office_printed_at(w.conn, of_id)
    text = PdfReader(BytesIO(pdf.office_copy(w.conn, w.admin, of_id))).pages[0].extract_text()
    # Final figures: the debit memo shows, and comes off the total.
    assert "FINAL COPY" in text and made["dm_number"] in text and "155.54" in text and "OF total less debit memos and claim stubs" in text
    total = order_forms.get_of(w.conn, of_id)["grand_total_c"]
    assert f"{(total - 15554) / 100:,.2f}" in text
    assert pdf.office_printed_at(w.conn, of_id)
    # Cancelling the memo means it isn't settled any more.
    dm.cancel(w.conn, w.admin, made["id"], "Wrong")
    assert not pdf.office_ready(w.conn, of_id)


def test_the_office_of_ends_on_one_figure_with_stickers_too(setup):
    w = setup
    masters.save_supplier(w.conn, w.admin, {"code": "ABC", "name": "ABC Trading", "sticker_rate_c": 50}, w.supplier, confirm_duplicate=True)
    of_id = w.approved(items=[w.item(cost=10000, alloc={NORTH: 10}, description="Bags")])
    po = order_forms.generate_pos(w.conn, w.buyer, of_id)[0]
    orders.issue(w.conn, w.buyer, po["id"])
    item = orders.get_po(w.conn, po["id"])["items"][0]["id"]
    for who in (w.r1, w.r2):
        receiving.save_count(w.conn, who, po["id"], {item: "8"})
    dm.issue(w.conn, w.buyer, po["id"], {(item, SHORT): "2"})
    text = " ".join(PdfReader(BytesIO(pdf.office_copy(w.conn, w.admin, of_id))).pages[0].extract_text().split())
    # 1,000.00 less 200.00 for the 2 short, less 4.00 of stickers on the 8 pieces that came.
    assert "To pay after stickers, debit memos and claim stubs" in text and "796.00" in text and "Stickers on the 8 pcs received" in text and "OF total less" not in text


def test_a_cancelled_of_keeps_its_office_copy_as_a_record(setup):
    w = setup
    fresh = w.approved(items=[w.item(alloc={NORTH: 1}, description="Other")])
    assert not pdf.office_ready(w.conn, fresh)
    order_forms.cancel(w.conn, w.buyer, fresh, "No longer needed")
    assert pdf.office_ready(w.conn, fresh)


def test_office_of_on_screen(app, setup, monkeypatch):
    w = setup
    opened = []
    monkeypatch.setattr("atlaspo.ui.common.os.startfile", lambda p: opened.append(p), raising=False)
    of_id = orders.get_po(w.conn, w.po_id)["of_id"]
    win = MainWindow(w.conn, w.buyer)
    win.open_of(of_id)
    # There's no Final OF button until the delivery is complete.
    assert not any(b.text() == "Final OF PDF" for b in win.of_page.findChildren(QPushButton))
    # The Final OF prints from Payments only, so neither the Print tab nor the OF screen has it.
    page = win.print_pos
    assert "Final OF" not in [page.tabs.tabText(i) for i in range(page.tabs.count())]
    _received(w)
    _finish(w)
    win.open_of(of_id)
    assert not any(b.text() == "Final OF PDF" for b in win.of_page.findChildren(QPushButton))
    # A clean delivery needs no second paper: the signed Warehouse OF stands.
    assert not payments.needs_final_of(w.conn, of_id)
    win.close()


def test_view_only_users_see_memos_without_the_buttons(app, setup):
    w = setup
    _received(w, "8", "6")
    dm.issue(w.conn, w.buyer, w.po_id, {(w.socks, SHORT): "2"})
    viewer = auth.create_user(w.conn, w.admin, "vic", "Vic", PASSWORD, ["VIEWER"], must_change=False)
    win = MainWindow(w.conn, viewer)
    assert win.debit_memos.table.rowCount() == 1 and win.debit_memos.cancel_btn.parent() is None
    assert win.received.dm_btn.parent() is None
    win.close()
