"""Claim stubs: damaged goods charged to the supplier, held at the warehouse, then collected or forfeited."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta
from io import BytesIO

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from pypdf import PdfReader
from PySide6.QtWidgets import QApplication

from atlaspo import auth, claim_stubs as cs, db, debit_memos as dm, order_forms, orders, receiving
from atlaspo.db import POError
from atlaspo.debit_memos import DAMAGED, SHORT
from atlaspo.receiving import COMPLETED, TO_CHECK
from atlaspo.ui.claim_stub_page import ClaimStubDialog
from atlaspo.ui.debit_memo_page import C_PIECES
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
        receiving.save_count(w.conn, who, w.po_id, {w.socks: socks, w.tee: tee}, **kw)


def _tab(w):
    return receiving.po_results(w.conn, w.buyer)[0]["tab"]


def _stub(w, pieces="3"):
    return cs.issue(w.conn, w.buyer, w.po_id, {(w.socks, DAMAGED): pieces})


def _at(monkeypatch, moment):
    for module in (db, order_forms):
        monkeypatch.setattr(module, "now", lambda: moment)
    monkeypatch.setattr(db, "today", lambda: moment.date())
    monkeypatch.setattr(cs, "today", lambda: moment.date())


def test_shortage_goes_on_a_debit_memo_and_damage_on_a_claim_stub(setup):
    w = setup
    _received(w, "8", "6", damaged={w.socks: "3"})
    assert [(l["description"], l["reason"], l["pieces_m"]) for l in dm.outstanding(w.conn, w.buyer, w.po_id)] == [("Socks", SHORT, 2000)]
    assert [(l["description"], l["reason"], l["pieces_m"], l["amount_c"]) for l in cs.outstanding(w.conn, w.buyer, w.po_id)] == [("Socks", DAMAGED, 3000, 23331)]
    # A debit memo can't charge damage any more.
    with pytest.raises(POError, match="nothing to charge"):
        dm.issue(w.conn, w.buyer, w.po_id, {(w.socks, DAMAGED): "3"})
    # Both are needed before the delivery is settled.
    assert _tab(w) == TO_CHECK
    dm.issue(w.conn, w.buyer, w.po_id, {(w.socks, SHORT): "2"})
    assert _tab(w) == TO_CHECK
    made = _stub(w)
    assert _tab(w) == COMPLETED and not cs.can_issue(w.conn, w.po_id)
    assert made["cs_number"].endswith("-CS-00001") and made["total_c"] == 23331 and made["approved_name"] == "Bea Buyer" and made["status"] == cs.ISSUED
    row = receiving.po_results(w.conn, w.buyer)[0]
    assert made["cs_number"] in row["claim_stubs"]


def test_a_stub_only_charges_what_is_damaged_and_outstanding(setup):
    w = setup
    _received(w, damaged={w.socks: "3"})
    with pytest.raises(POError, match="only 3 damaged"):
        _stub(w, "4")
    with pytest.raises(POError, match="nothing to charge"):
        _stub(w, "0")
    assert not w.conn.execute("SELECT 1 FROM cs_number_sequences").fetchone()  # a refused stub uses no number
    _stub(w, "1")
    assert _tab(w) == TO_CHECK and [l["pieces_m"] for l in cs.outstanding(w.conn, w.buyer, w.po_id)] == [2000]
    _stub(w, "2")
    assert _tab(w) == COMPLETED


def test_it_waits_until_the_counts_agree(setup):
    w = setup
    receiving.save_count(w.conn, w.r1, w.po_id, {w.socks: "10", w.tee: "6"}, damaged={w.socks: "3"})
    assert not cs.can_issue(w.conn, w.po_id)
    with pytest.raises(POError, match="still being counted"):
        cs.outstanding(w.conn, w.buyer, w.po_id)


def _paid(w, day="2027-01-01"):
    """The supplier received the check that takes the claim stub off, which is when they may pick the goods up."""
    of_id = w.conn.execute("SELECT of_id FROM purchase_orders WHERE id = ?", (w.po_id,)).fetchone()[0]
    check = w.conn.execute("INSERT INTO payment_checks(check_number, source, check_date, supplier_id, amount_c, status, recorded_by, recorded_at) VALUES ('777001', 'EXTERNAL', ?, ?, 100, 'ACTIVE', ?, ?)",
                           (day, w.supplier, w.admin.id, day)).lastrowid
    w.conn.execute("INSERT INTO payment_check_ofs(check_id, of_id, amount_c) VALUES (?, ?, 100)", (check, of_id))
    w.conn.execute("INSERT INTO check_claims(check_id, claimed_on, claimed_by_name, recorded_by, recorded_at) VALUES (?, ?, 'Juan', ?, ?)", (check, day, w.admin.id, day))


def test_goods_come_back_to_the_warehouse_then_the_supplier_collects(setup):
    w = setup
    _received(w, damaged={w.socks: "3"})
    made = _stub(w)
    # They can't be collected from a branch.
    with pytest.raises(POError, match="once its goods are at Central Warehouse"):
        cs.collected(w.conn, w.buyer, made["id"], "Juan")
    cs.arrived(w.conn, w.warehouse, made["id"], cs.as_on_paper(w.conn, made["id"]))
    row = cs.get(w.conn, w.buyer, made["id"])
    # Not until the supplier has the check that takes the stub off. Their 90 days start then.
    assert row["status"] == cs.AT_WAREHOUSE and row["paid_on"] is None and row["days_left"] is None
    with pytest.raises(POError, match="received the check"):
        cs.collected(w.conn, w.buyer, made["id"], "Juan")
    with pytest.raises(POError):
        cs.arrived(w.conn, w.warehouse, made["id"], cs.as_on_paper(w.conn, made["id"]))
    _paid(w, cs.today().isoformat())
    assert cs.get(w.conn, w.buyer, made["id"])["days_left"] == 90
    with pytest.raises(POError, match="who collected"):
        cs.collected(w.conn, w.buyer, made["id"], " ")
    cs.collected(w.conn, w.buyer, made["id"], "Juan Dela Cruz")
    row = cs.get(w.conn, w.buyer, made["id"])
    assert (row["status"], row["collected_by_name"], row["closed_name"], row["days_left"]) == (cs.COLLECTED, "Juan Dela Cruz", "Bea Buyer", None)
    # Finished: it can't be forfeited or cancelled, and the delivery stays settled.
    with pytest.raises(POError):
        cs.forfeit(w.conn, w.buyer, made["id"])
    with pytest.raises(POError, match="finished"):
        cs.cancel(w.conn, w.admin, made["id"], "No")
    assert _tab(w) == COMPLETED


def test_after_90_days_the_goods_can_be_forfeited_for_sale(setup, monkeypatch):
    w = setup
    _received(w, damaged={w.socks: "3"})
    made = _stub(w)
    with pytest.raises(POError, match="while its goods are waiting"):
        cs.forfeit(w.conn, w.buyer, made["id"])  # the clock hasn't started
    start = datetime(2027, 1, 1, 9)
    _at(monkeypatch, start)
    cs.arrived(w.conn, w.warehouse, made["id"], cs.as_on_paper(w.conn, made["id"]))
    with pytest.raises(POError, match="haven't started"):
        cs.forfeit(w.conn, w.buyer, made["id"])
    _paid(w, "2027-01-01")
    # Day 90 after the supplier got the check is still theirs.
    _at(monkeypatch, start + timedelta(days=90))
    assert cs.get(w.conn, w.buyer, made["id"])["days_left"] == 0
    with pytest.raises(POError, match="can't be forfeited yet"):
        cs.forfeit(w.conn, w.buyer, made["id"])
    _at(monkeypatch, start + timedelta(days=91))
    assert cs.get(w.conn, w.buyer, made["id"])["days_left"] == -1
    assert not cs.for_sale(w.conn, w.buyer)
    cs.forfeit(w.conn, w.buyer, made["id"])
    assert cs.get(w.conn, w.buyer, made["id"])["status"] == cs.FORFEITED
    sale = cs.for_sale(w.conn, w.buyer)
    assert [(s["description"], s["pieces_m"], s["cs_number"]) for s in sale] == [("Socks", 3000, made["cs_number"])]
    with pytest.raises(POError):
        cs.collected(w.conn, w.buyer, made["id"], "Too late")


def test_cancelling_brings_the_delivery_back_and_the_number_is_never_reused(setup):
    w = setup
    _received(w, damaged={w.socks: "3"})
    first = _stub(w)
    assert _tab(w) == COMPLETED
    with pytest.raises(POError, match="Write the reason"):
        cs.cancel(w.conn, w.admin, first["id"], " ")
    cs.cancel(w.conn, w.admin, first["id"], "Wrong supplier")
    assert _tab(w) == TO_CHECK and cs.can_issue(w.conn, w.po_id)
    assert _stub(w)["cs_number"].endswith("-CS-00002")


def test_the_document_is_kept_and_tells_the_supplier_where_and_when(setup):
    w = setup
    _received(w, damaged={w.socks: "3"})
    made = cs.issue(w.conn, w.buyer, w.po_id, {(w.socks, DAMAGED): "3"}, "Crushed carton")
    stored = cs.document(w.conn, w.buyer, made["id"])
    text = PdfReader(BytesIO(stored)).pages[0].extract_text()
    for wanted in ("CLAIM STUB", made["cs_number"], "ABC Trading", "Socks", "SOCK-1", "233.31", "Crushed carton", "Central Warehouse", "90 days", "forfeited", "Approved by"):
        assert wanted in text, wanted
    assert "Tee pack" not in text
    cs.cancel(w.conn, w.admin, made["id"], "Wrong")
    assert w.conn.execute("SELECT pdf FROM claim_stubs WHERE id = ?", (made["id"],)).fetchone()[0] == stored


def test_records_can_not_be_altered_even_in_the_database(setup):
    w = setup
    _received(w, damaged={w.socks: "3"})
    _stub(w)
    for sql in ("UPDATE claim_stubs SET total_c = 1", "UPDATE claim_stubs SET cs_number = 'X'", "UPDATE claim_stubs SET pdf = x'00'", "UPDATE claim_stubs SET po_id = 99", "DELETE FROM claim_stubs",
                "UPDATE claim_stub_items SET amount_c = 1", "DELETE FROM claim_stub_items", "UPDATE cs_number_sequences SET last_number = 0", "DELETE FROM cs_number_sequences"):
        with pytest.raises(sqlite3.Error):
            w.conn.execute(sql)


def test_every_search_finds_by_supplier_code(app, setup):
    from atlaspo import selling
    w = setup
    _received(w, "8", "6", damaged={w.socks: "3"})
    dm.issue(w.conn, w.buyer, w.po_id, {(w.socks, SHORT): "2"})
    _stub(w)
    for found, missed in ((receiving.po_results(w.conn, w.buyer, "ABC"), receiving.po_results(w.conn, w.buyer, "ZZZ")), (dm.search(w.conn, w.buyer, "abc"), dm.search(w.conn, w.buyer, "ZZZ")),
                          (cs.search(w.conn, w.buyer, "ABC"), cs.search(w.conn, w.buyer, "ZZZ")), (selling.list_items(w.conn, "ABC"), selling.list_items(w.conn, "ZZZ")),
                          (order_forms.search(w.conn, text="ABC"), order_forms.search(w.conn, text="ZZZ")), (orders.search(w.conn, text="ABC"), orders.search(w.conn, text="ZZZ"))):
        assert found and not missed
    win = MainWindow(w.conn, w.buyer)
    assert "code" in win.inquiry.search.placeholderText() and "code" in win.orders_page.search.placeholderText() and "code" in win.print_pos.search.placeholderText()
    win.inquiry.search.setText("ABC")
    assert win.inquiry.tree.topLevelItemCount() == 1
    win.close()


def test_who_can_do_what(setup):
    w = setup
    _received(w, damaged={w.socks: "3"})
    viewer = auth.create_user(w.conn, w.admin, "vic", "Vic", PASSWORD, ["VIEWER"], must_change=False)
    for who in (viewer, w.r1):
        with pytest.raises(db.PermissionDenied):
            cs.issue(w.conn, who, w.po_id, {(w.socks, DAMAGED): "1"})
    made = _stub(w)
    for fn, args in ((cs.arrived, ()), (cs.collected, ("X",)), (cs.forfeit, ()), (cs.cancel, ("X",))):
        with pytest.raises(db.PermissionDenied):
            fn(w.conn, viewer, made["id"], *args)
    assert cs.search(w.conn, viewer)
    with pytest.raises(db.PermissionDenied):
        cs.search(w.conn, w.r1)


def test_external_number_is_saved_and_can_be_searched(setup):
    w = setup
    _received(w, damaged={w.socks: "3"})
    made = _stub(w)
    cs.save_external_number(w.conn, w.buyer, made["id"], " 35700 ")
    assert cs.search(w.conn, w.buyer, "35700")[0]["id"] == made["id"] and not cs.search(w.conn, w.buyer, "nothing-like-this")


def test_on_screen_from_received_to_collected(app, setup, monkeypatch):
    w = setup
    opened = []
    monkeypatch.setattr("atlaspo.ui.common.os.startfile", lambda p: opened.append(p), raising=False)
    monkeypatch.setattr("atlaspo.ui.debit_memo_page.confirm", lambda *a, **k: True)
    monkeypatch.setattr("atlaspo.ui.claim_stub_page.confirm", lambda *a, **k: True)
    _received(w, damaged={w.socks: "3"})
    win = MainWindow(w.conn, w.buyer)
    assert "Claim stubs" in {t for t, _ in win.pages}
    page = win.received
    page.table.selectRow(0)
    assert page.cs_btn.isEnabled() and not page.dm_btn.isEnabled()
    d = ClaimStubDialog(page, w.conn, w.buyer, page.rows[0])
    assert d.table.rowCount() == 1 and d.table.item(0, C_PIECES).text() == "3" and "233.31" in d.total.text()
    d.remarks.setText("Crushed")
    d.issue()
    assert d.issued["total_c"] == 23331 and len(opened) == 0  # it prints when needed, from Claim stubs
    page.refresh()
    assert page.tabs.tabText(1) == "Completed delivery  1"
    page.tabs.setCurrentIndex(1)
    assert page.table.item(0, 8).text() == d.issued["cs_number"] and page.table.item(0, 10).text() == "Completed with claim stub"

    stubs = win.claim_stubs
    stubs.refresh()
    assert stubs.tabs.tabText(0) == "Coming back to Central Warehouse  1"
    # Not printed yet: Select all not printed ticks it, and Print ticked prints it.
    assert stubs.table.item(0, 11).text() == "Not printed" and stubs.select_btn.isVisibleTo(stubs)
    stubs.select_unprinted()
    assert stubs.print_ticked_btn.text() == "Print 1 ticked"
    stubs.print_ticked()
    assert len(opened) == 1 and stubs.table.item(0, 11).text() == "Printed"
    stubs.table.selectRow(0)
    assert stubs.arrived_btn.parent() is None and not stubs.collected_btn.isEnabled() and not stubs.forfeit_btn.isEnabled()  # the warehouse says when they arrive
    yard = MainWindow(w.conn, w.warehouse)
    assert [t for t, _ in yard.pages] == ["Central Warehouse", "Claim stubs", "Item passes"]
    yard.claim_stubs.table.selectRow(0)
    assert yard.claim_stubs.arrived_btn.isEnabled() and yard.claim_stubs.collected_btn.parent() is not None  # the warehouse may record Collected too
    monkeypatch.setattr("atlaspo.ui.claim_stub_page.count_goods", lambda parent, number, lines: {i["id"]: str(i["pieces_m"] // 1000) for i in lines})
    yard.claim_stubs.arrived()
    yard.close()
    stubs.refresh()
    assert stubs.tabs.tabText(1) == "At Central Warehouse  1" and stubs.table.rowCount() == 0
    stubs.tabs.setCurrentIndex(1)
    assert stubs.table.item(0, 10).text() == "Waiting for payment"
    _paid(w, cs.today().isoformat())
    stubs.refresh()
    assert stubs.table.item(0, 10).text() == "90"
    stubs.table.selectRow(0)
    assert stubs.collected_btn.isEnabled() and not stubs.forfeit_btn.isEnabled()
    monkeypatch.setattr("atlaspo.ui.claim_stub_page.ask_text", lambda *a, **k: "Juan")
    stubs.collected()
    assert stubs.tabs.tabText(2) == "Collected by supplier  1"
    stubs.tabs.setCurrentIndex(2)
    stubs.table.selectRow(0)
    assert "Collected by Juan" in stubs.note.text() and not stubs.cancel_btn.isEnabled()
    win.close()


def test_view_only_users_see_stubs_without_the_buttons(app, setup):
    w = setup
    _received(w, damaged={w.socks: "3"})
    _stub(w)
    viewer = auth.create_user(w.conn, w.admin, "vic", "Vic", PASSWORD, ["VIEWER"], must_change=False)
    win = MainWindow(w.conn, viewer)
    assert win.claim_stubs.table.rowCount() == 1 and win.claim_stubs.arrived_btn.parent() is None
    assert win.received.cs_btn.parent() is None
    win.close()
