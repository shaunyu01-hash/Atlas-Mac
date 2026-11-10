"""Paying for a finished order: the DR check, the amount, the due date, and recording checks so nothing is paid twice."""
from __future__ import annotations

import sqlite3

import os
from datetime import date
from io import BytesIO

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from pypdf import PdfReader
from PySide6.QtCore import QSettings
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from atlaspo import auth, claim_stubs, db, debit_memos, masters, order_forms, orders, payments, pdf, receiving, schema
from atlaspo.db import POError
from atlaspo.ui.common import date_value
from atlaspo.ui import common, pages, payments_page, theme
from atlaspo.ui.main_window import MainWindow

from conftest import NORTH, PASSWORD, SOUTH


@pytest.fixture
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def accounting(request):
    """Payments belong to accounting, so these tests act as them. Purchasers still do the ordering."""
    if "world" in request.fixturenames:
        world = request.getfixturevalue("world")
        world.accounting = auth.create_user(world.conn, world.admin, "lorna", "Lorna Tan", PASSWORD, ["ACCOUNTING"], must_change=False)


@pytest.fixture
def store(tmp_path, monkeypatch):
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(theme, "_settings", lambda: settings)


def _supplier(world, **over):
    masters.save_supplier(world.conn, world.admin, {"code": "ABC", "name": "ABC Trading", "payment_terms": "60D", **over}, world.supplier, confirm_duplicate=True)


class Order:
    """One order delivered to two branches, 10 pieces each at 100.00, with two receivers at each."""

    def __init__(self, world, av=10, ros=10, **damage):
        self.w = world
        self.of_id = world.approved(items=[world.item(cost=10000, alloc={NORTH: 10, SOUTH: 10}, description="Socks", item_code="SOCK-1")])
        self.pos = {p["branch_code"]: p for p in order_forms.generate_pos(world.conn, world.buyer, self.of_id)}
        for p in self.pos.values():
            orders.issue(world.conn, world.buyer, p["id"])
        self.count(NORTH, av, **damage)
        self.count(SOUTH, ros)

    def count(self, branch, pieces, **kw):
        w, po = self.w, self.pos[branch]
        item = orders.get_po(w.conn, po["id"])["items"][0]["id"]
        kw = {"damaged": {item: kw["damaged"]}} if "damaged" in kw else {}
        for n in (1, 2):
            login = f"r{branch}x{n}"
            existing = w.conn.execute("SELECT id FROM users WHERE username = ?", (login,)).fetchone()
            who = auth.get_user(w.conn, existing["id"]) if existing else auth.create_user(w.conn, w.admin, login, f"Receiver {branch} {n}", PASSWORD, ["RECEIVER"], must_change=False, branch_code=branch)
            receiving.save_count(w.conn, who, po["id"], {item: str(pieces)}, **kw)

    def short_memo(self, branch=NORTH):
        po_id = self.pos[branch]["id"]
        lines = debit_memos.outstanding(self.w.conn, self.w.buyer, po_id)
        return debit_memos.issue(self.w.conn, self.w.buyer, po_id, {(l["po_item_id"], l["reason"]): l["pieces_m"] // 1000 for l in lines}, "Short")

    def stub(self, branch=NORTH):
        po_id = self.pos[branch]["id"]
        lines = claim_stubs.outstanding(self.w.conn, self.w.buyer, po_id)
        return claim_stubs.issue(self.w.conn, self.w.buyer, po_id, {(l["po_item_id"], l["reason"]): l["pieces_m"] // 1000 for l in lines}, "Damaged")

    def enter(self, day="2026-10-28", off_by="0"):
        row = payments.order_row(self.w.conn, self.of_id)
        by_po = {p["po_id"]: p["delivered_c"] for p in row["pos"]}
        receipts = payments.receipts_to_enter(self.w.conn, self.w.accounting, self.of_id)
        extra = float(off_by)
        payments.save_dr_entries(self.w.conn, self.w.accounting, self.of_id, {d["id"]: (day, f"{by_po[d['po_id']] / 100 + extra:.2f}") for d in receipts})

    def row(self):
        return payments.order_row(self.w.conn, self.of_id)


@pytest.mark.parametrize("day, expected", [("2026-11-02", "2026-11-07"), ("2026-11-07", "2026-11-07"), ("2026-11-08", "2026-11-14")])
def test_a_check_falls_on_the_next_saturday(day, expected):
    assert payments.next_saturday(date.fromisoformat(day)).isoformat() == expected


@pytest.mark.parametrize("terms, days", [("60D", 60), ("7D", 7), ("None", 0), ("", 0), ("COD", None), ("TT 50% down", None)])
def test_terms_are_read_only_when_they_are_days(terms, days):
    assert payments.terms_days(terms) == days


def _printed_check(world, day, number="770001"):
    order = Order(world)
    order.enter()
    group = payments.checks_due(world.conn)[0]
    return payments.record_check(world.conn, world.accounting, [o["id"] for o in group["orders"]], number, day, f"{group['check_c'] / 100:.2f}"), group["check_c"]


def test_a_check_is_future_then_due_then_claimed(world, monkeypatch):
    check_id, _ = _printed_check(world, "2030-06-01")
    bucket = lambda: payments.checks(world.conn)[0]["bucket"]
    assert bucket() == payments.FUTURE and payments.checks(world.conn)[0]["days"] > 0
    monkeypatch.setattr(payments, "today", lambda: date(2030, 5, 31))
    assert bucket() == payments.FUTURE and payments.checks(world.conn)[0]["days"] == 1
    monkeypatch.setattr(payments, "today", lambda: date(2030, 6, 1))
    assert bucket() == payments.DUE  # due on its own date
    monkeypatch.setattr(payments, "today", lambda: date(2030, 6, 9))
    assert bucket() == payments.DUE and payments.checks(world.conn)[0]["days"] == -8
    with pytest.raises(POError, match="future"):
        payments.claim_check(world.conn, world.accounting, check_id, "2030-06-10")
    payments.claim_check(world.conn, world.accounting, check_id, "2030-06-08", " Juan ")
    row = payments.checks(world.conn)[0]
    assert (row["bucket"], row["claimed_on"], row["claimed_by_name"]) == (payments.CLAIMED, "2030-06-08", "Juan")
    with pytest.raises(POError, match="already claimed"):
        payments.claim_check(world.conn, world.accounting, check_id)
    # The supplier has it, so it can't simply be cancelled. If it comes back, the claim is undone first.
    with pytest.raises(POError, match="already has check"):
        payments.cancel_check(world.conn, world.accounting, check_id, "No")
    with pytest.raises(POError, match="why"):
        payments.undo_claim(world.conn, world.accounting, check_id, " ")
    payments.undo_claim(world.conn, world.accounting, check_id, "Came back for correction")
    assert bucket() == payments.DUE
    payments.cancel_check(world.conn, world.accounting, check_id, "Wrong date")
    with pytest.raises(POError, match="cancelled"):
        payments.claim_check(world.conn, world.accounting, check_id)
    assert world.conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action IN ('CHECK_CLAIM', 'CHECK_UNCLAIM')").fetchone()[0] == 2


def test_a_post_dated_check_can_be_claimed_early(world):
    check_id, _ = _printed_check(world, "2030-06-01")
    payments.claim_check(world.conn, world.accounting, check_id)
    assert payments.checks(world.conn)[0]["bucket"] == payments.CLAIMED


def test_cash_flow_adds_up_what_has_to_be_covered_by_date(world):
    first, amount = _printed_check(world, "2030-06-01", "770001")
    second, other = _printed_check(world, "2030-06-08", "770002")
    third, more = _printed_check(world, "2030-06-08", "770003")
    claimed, gone = _printed_check(world, "2030-06-15", "770004")
    payments.claim_check(world.conn, world.accounting, claimed)
    waiting = Order(world)
    waiting.enter()
    make = payments.checks_due(world.conn)[0]
    rows = payments.cash_flow(world.conn)
    printed = {r["date"]: r for r in rows if r["printed_n"]}
    assert printed[date(2030, 6, 1)]["printed_c"] == amount and printed[date(2030, 6, 8)]["printed_n"] == 2 and printed[date(2030, 6, 8)]["printed_c"] == other + more
    assert date(2030, 6, 15) not in printed  # claimed, so it isn't waiting to be claimed
    assert {r["date"]: r["claimed_c"] for r in rows if r["claimed_n"]} == {date(2030, 6, 15): gone}  # but the money hasn't left yet
    assert sum(r["to_make_c"] for r in rows) == make["check_c"] and sum(r["to_make_n"] for r in rows) == 1
    assert rows == sorted(rows, key=lambda r: r["date"])
    assert rows[-1]["running_c"] == amount + other + more + gone + make["check_c"]
    running = 0
    for r in rows:
        running += r["total_c"]
        assert r["running_c"] == running
    # Once the bank has paid it, it stops needing cash.
    cleared_on = date.today().isoformat()
    payments.clear_check(world.conn, world.accounting, claimed, cleared_on)
    after = payments.cash_flow(world.conn)
    assert date(2030, 6, 15) not in {r["date"] for r in after} and after[-1]["running_c"] == amount + other + more + make["check_c"]
    assert first and second and third


def test_a_check_clears_only_after_it_is_claimed(world):
    check_id, _ = _printed_check(world, "2020-06-06")
    with pytest.raises(POError, match="has to be claimed"):
        payments.clear_check(world.conn, world.accounting, check_id)
    payments.claim_check(world.conn, world.accounting, check_id, "2026-10-20")
    with pytest.raises(POError, match="can't have cleared before"):
        payments.clear_check(world.conn, world.accounting, check_id, "2026-10-19")
    with pytest.raises(POError, match="future"):
        payments.clear_check(world.conn, world.accounting, check_id, "2099-01-01")
    payments.clear_check(world.conn, world.accounting, check_id, "2026-10-22")
    row = payments.checks(world.conn)[0]
    assert (row["bucket"], row["cleared_on"]) == (payments.CLEARED, "2026-10-22")
    with pytest.raises(POError, match="already cleared"):
        payments.clear_check(world.conn, world.accounting, check_id)
    with pytest.raises(POError, match="cleared the bank"):
        payments.undo_claim(world.conn, world.accounting, check_id, "No")
    with pytest.raises(POError, match="why"):
        payments.undo_clear(world.conn, world.accounting, check_id, " ")
    payments.undo_clear(world.conn, world.accounting, check_id, "Bank statement was misread")
    assert payments.checks(world.conn)[0]["bucket"] == payments.CLAIMED
    assert world.conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action IN ('CHECK_CLEAR', 'CHECK_UNCLEAR')").fetchone()[0] == 2


def test_purchasers_do_no_accounting(app, world):
    assert not world.buyer.can("payment.edit") and not world.buyer.can("payment.view")
    check_id, _ = _printed_check(world, "2030-06-01")
    for call in (lambda: payments.claim_check(world.conn, world.buyer, check_id), lambda: payments.cancel_check(world.conn, world.buyer, check_id, "No"),
                 lambda: payments.record_check(world.conn, world.buyer, [1], "1", "2030-06-01", "1.00"), lambda: payments.save_dr_entries(world.conn, world.buyer, 1, {})):
        with pytest.raises(db.PermissionDenied):
            call()
    win = MainWindow(world.conn, world.buyer)
    names = [t for t, _ in win.pages]
    assert "Payments" not in names and win.payments is None and "Orders" in names and "Received" in names
    win.refresh_all()
    win.close()
    # Management can look, and not change anything.
    approver = auth.create_user(world.conn, world.admin, "amy", "Amy", PASSWORD, ["APPROVER"], must_change=False)
    win = MainWindow(world.conn, approver)
    assert "Payments" in [t for t, _ in win.pages] and not win.payments.can_edit
    win.close()


def test_check_tabs_and_cash_flow_on_screen(app, world, monkeypatch):
    future, _ = _printed_check(world, "2030-06-01", "770001")
    due, amount = _printed_check(world, "2020-06-06", "770002")
    win = MainWindow(world.conn, world.accounting)
    page = win.payments
    assert page.tabs.tabText(2) == "Future checks  1" and page.tabs.tabText(3) == "Unclaimed  1" and page.tabs.tabText(4) == "Claimed  0" and page.tabs.tabText(5) == "Cleared  0" and page.tabs.tabText(7) == "Cash flow"
    assert f"Unclaimed {payments.fmt_peso(amount)}" in page.totals.text()
    page.tabs.setCurrentIndex(3)
    assert cell(page, page.paid, "Check number") == "770002" and cell(page, page.paid, "When").endswith("days ago") and page.primary_btn.text() == "Mark claimed"
    dialog = payments_page.ClaimDialog(page, world.conn, world.accounting, page._selected_check())
    dialog.who.setText("Juan")
    dialog.save()
    page.refresh()
    assert page.tabs.tabText(3) == "Unclaimed  0" and page.tabs.tabText(4) == "Claimed  1"
    page.tabs.setCurrentIndex(4)
    assert cell(page, page.paid, "When").endswith("Juan") and page.primary_btn.text() == "Mark cleared" and page.second_btn.text() == "Undo claim" and not page.cancel_btn.isVisibleTo(page)
    clearing = payments_page.ClearDialog(page, world.conn, world.accounting, page._selected_check())
    clearing.save()
    page.refresh()
    assert page.tabs.tabText(4) == "Claimed  0" and page.tabs.tabText(5) == "Cleared  1"
    page.tabs.setCurrentIndex(5)
    # Cleared waits to be asked, so the history doesn't pile up on screen.
    assert page.paid.topLevelItemCount() == 0 and page.show_all_btn.isVisibleTo(page) and "Search" in page.empty.text() and page.tabs.tabText(5).endswith("  1")
    page.search.setText("770002")
    assert cell(page, page.paid, "When").startswith("Cleared") and not page.show_all_btn.isVisibleTo(page)
    page.search.clear()
    assert page.paid.topLevelItemCount() == 0
    page.show_all_btn.click()
    assert cell(page, page.paid, "When").startswith("Cleared") and page.primary_btn.text() == "Undo cleared"
    page.tabs.setCurrentIndex(2)
    assert cell(page, page.paid, "When").startswith("In ") and cell(page, page.paid, "Check number") == "770001"
    page.tabs.setCurrentIndex(7)
    assert page.flow.rowCount() == 2 and page.flow.item(1, 0).text() == "Total" and page.flow.item(0, 1).text() != "" and "hasn't cleared" in page.note.text() and not page.primary_btn.isVisibleTo(page)
    assert future and due
    win.close()


def test_accounting_handles_payments_and_received_and_no_ordering(app, world):
    acct = auth.create_user(world.conn, world.admin, "acct", "Lorna Tan", PASSWORD, ["ACCOUNTING"], must_change=False)
    assert auth.ROLES["ACCOUNTING"] == "Accounting"
    for permission in ("payment.edit", "receiving.review"):
        assert acct.can(permission)
    for permission in ("of.edit", "of.approve", "of.generate", "po.edit", "po.issue", "po.amend", "master.edit", "dm.issue", "receiving.count", "settings.edit"):
        assert not acct.can(permission), permission
    check_id, _ = _printed_check(world, "2030-06-01")
    payments.claim_check(world.conn, acct, check_id)
    win = MainWindow(world.conn, acct)
    assert [t for t, _ in win.pages] == ["Payments", "Central Warehouse", "Received", "Debit memos", "Claim stubs", "Item passes"]
    assert win.stack.currentWidget() is win.payments and win.payments.can_edit
    win.new_of()
    win.refresh_all()
    assert win.stack.currentWidget() is win.payments
    assert win.received.dm_btn.parent() is None and win.debit_memos.cancel_btn.parent() is None
    win.close()


def test_a_clean_delivery_pays_what_was_signed_with_no_second_paper(world):
    _supplier(world, sticker_rate_c=50)
    o = Order(world)
    s = payments.settlement(world.conn, o.of_id)
    # 20 pieces at 100.00, less 0.50 a piece.
    assert (s["total_c"], s["sticker_c"], s["pay_c"], s["signed_c"]) == (200000, 1000, 199000, 199000)
    assert not s["changed"] and not payments.needs_final_of(world.conn, o.of_id)


def test_a_debit_memo_lowers_the_payment_and_the_stickers_and_needs_a_final_of(world):
    _supplier(world, sticker_rate_c=50)
    o = Order(world, av=8)
    assert not receiving.of_delivery_complete(world.conn, o.of_id)  # the shortage isn't settled yet
    o.short_memo()
    s = payments.settlement(world.conn, o.of_id)
    # 2 short at 100.00 is a memo for 200.00. Stickers only on the 18 that came.
    assert (s["charged_c"], s["sticker_c"], s["pay_c"]) == (20000, 900, 200000 - 20000 - 900)
    assert s["signed_c"] == 199000 and s["changed"] and payments.needs_final_of(world.conn, o.of_id)
    text = " ".join(PdfReader(BytesIO(pdf.render_office_of(world.conn, o.of_id))).pages[0].extract_text().split())
    assert "FINAL COPY" in text and "Stickers on the 18 pcs received" in text and "1,791.00" in text
    assert "Final deduction" not in text  # worked out now, not left blank


def test_a_claim_stub_comes_off_but_damaged_pieces_still_get_stickers(world):
    _supplier(world, sticker_rate_c=50)
    o = Order(world, damaged="3")
    o.stub()
    s = payments.settlement(world.conn, o.of_id)
    assert (s["charged_c"], s["sticker_c"], s["pay_c"]) == (30000, 1000, 200000 - 30000 - 1000) and s["changed"]


def test_a_closed_po_with_no_memo_still_doesnt_pay_for_what_never_came(world):
    o = Order(world, av=7)
    orders.close(world.conn, world.buyer, o.pos[NORTH]["id"], "Supplier won't send the rest")
    assert receiving.of_delivery_complete(world.conn, o.of_id)
    s = payments.settlement(world.conn, o.of_id)
    assert (s["unclaimed_c"], s["pay_c"], s["delivered_c"]) == (30000, 170000, 170000) and s["changed"]


def test_a_cancelled_po_stops_holding_up_the_order_and_isnt_paid(world):
    of_id = world.approved(items=[world.item(cost=10000, alloc={NORTH: 10, SOUTH: 10}, description="Socks", item_code="SOCK-1")])
    pos = {p["branch_code"]: p for p in order_forms.generate_pos(world.conn, world.buyer, of_id)}
    for p in pos.values():
        orders.issue(world.conn, world.buyer, p["id"])
    item = orders.get_po(world.conn, pos[NORTH]["id"])["items"][0]["id"]
    for n in (1, 2):
        who = auth.create_user(world.conn, world.admin, f"a{n}", f"A {n}", PASSWORD, ["RECEIVER"], must_change=False, branch_code=NORTH)
        receiving.save_count(world.conn, who, pos[NORTH]["id"], {item: "10"})
    assert not receiving.of_delivery_complete(world.conn, of_id)  # South is still out, so the whole order waits
    orders.cancel(world.conn, world.buyer, pos[SOUTH]["id"], "Won't arrive")
    assert receiving.of_delivery_complete(world.conn, of_id)
    s = payments.settlement(world.conn, of_id)
    assert s["pay_c"] == 100000 and s["changed"] and len(s["pos"]) == 1


def test_a_term_discount_shortens_the_days_and_comes_off_and_the_supplier_signs_it(world, monkeypatch):
    _supplier(world, term_discount_bp=500, term_discount_days=14)
    o = Order(world)
    of = order_forms.get_of(world.conn, o.of_id)
    assert of["term_discount_c"] == 10000 and of["net_payable_c"] == 190000
    assert "Less term discount, 5%" in " ".join(PdfReader(BytesIO(pdf.render_warehouse_of(world.conn, o.of_id))).pages[0].extract_text().split())
    assert payments.settlement(world.conn, o.of_id)["pay_c"] == 190000
    o.enter("2026-10-28")
    # Oct 28 plus 14 days is Nov 11, so the next Saturday is Nov 14, not 60 days out.
    assert o.row()["due"].isoformat() == "2026-11-14"


def test_due_date_counts_from_the_latest_dr(world):
    _supplier(world)
    o = Order(world)
    receipts = payments.receipts_to_enter(world.conn, world.accounting, o.of_id)
    payments.save_dr_entries(world.conn, world.accounting, o.of_id, {receipts[0]["id"]: ("2026-10-01", "1000"), receipts[1]["id"]: ("2026-10-20", "1000")})
    # Oct 20 plus 60 days is Dec 19, itself a Saturday.
    assert o.row()["due"].isoformat() == "2026-12-19" and o.row()["stage"] == payments.TO_PAY


def test_terms_typed_by_hand_are_flagged(world):
    _supplier(world, payment_terms="TT 50% down")
    o = Order(world)
    o.enter()
    assert o.row()["terms_by_hand"] and o.row()["due"].isoformat() == "2026-10-31"


def test_a_clean_delivery_goes_straight_to_its_check_with_nothing_typed(world, clock):
    _supplier(world)
    clock(2026, 10, 20)
    o = Order(world)
    row = o.row()
    assert row["why"] == [] and row["stage"] == payments.TO_PAY and row["entered"] == 0 and row["pay_c"] == 200000
    # Oct 20, the day the counts agreed, plus 60 days is Dec 19, itself a Saturday.
    assert row["due"].isoformat() == "2026-12-19"
    assert [g["check_c"] for g in payments.checks_due(world.conn)] == [200000]


def test_one_dr_that_differs_pulls_a_clean_order_back_and_only_that_dr_is_compared(world):
    o = Order(world)
    receipts = payments.receipts_to_enter(world.conn, world.accounting, o.of_id)
    assert all(d["suggested_date"] for d in receipts)
    # North's PO comes to 1,000.00, and the supplier wrote 1,250.00. South's stays blank.
    payments.save_dr_entries(world.conn, world.accounting, o.of_id, {receipts[0]["id"]: ("2026-10-28", "1250"), receipts[1]["id"]: (receipts[1]["suggested_date"], "")})
    row = o.row()
    assert row["stage"] == payments.DIFFERENCE and row["difference_c"] == 25000 and payments.checks_due(world.conn) == []
    # Corrected to what the order says, it goes back by itself.
    payments.save_dr_entries(world.conn, world.accounting, o.of_id, {receipts[0]["id"]: ("2026-10-28", "1000")})
    assert o.row()["stage"] == payments.TO_PAY and o.row()["entered"] == 1
    # Cleared again, it's as if nobody had typed anything.
    payments.save_dr_entries(world.conn, world.accounting, o.of_id, {receipts[0]["id"]: ("2026-10-28", "")})
    assert o.row()["entered"] == 0 and o.row()["stage"] == payments.TO_PAY


def test_a_dr_can_be_corrected_until_its_check_is_recorded(world):
    o = Order(world)
    receipt = payments.receipts_to_enter(world.conn, world.accounting, o.of_id)[0]
    payments.save_dr_entries(world.conn, world.accounting, o.of_id, {receipt["id"]: ("2026-10-28", "1000")})
    _recorded_as_is(world, o)
    with pytest.raises(POError, match="Cancel the check"):
        payments.save_dr_entries(world.conn, world.accounting, o.of_id, {receipt["id"]: ("2026-10-28", "900")})


def _recorded_as_is(world, o):
    group = payments.checks_due(world.conn)[0]
    return payments.record_check(world.conn, world.accounting, [x["id"] for x in group["orders"]], "880070", group["due"].isoformat(), f"{group['check_c'] / 100:.2f}")


@pytest.mark.parametrize("damage, pieces, why", [({"damaged": "3"}, 10, "damaged"), ({}, 12, "excess")])
def test_damage_or_excess_means_someone_compares_the_drs(world, damage, pieces, why):
    o = Order(world, av=pieces, **damage)
    assert why in o.row()["why"]


def test_a_short_delivery_needs_one_press_and_nothing_typed(world):
    o = Order(world, av=8)
    o.short_memo()
    row = o.row()
    assert set(row["why"]) == {"deductions", "short"} and row["stage"] == payments.ENTER_DRS and payments.checks_due(world.conn) == []
    payments.drs_match(world.conn, world.accounting, o.of_id)
    row = o.row()
    assert row["stage"] == payments.TO_PAY and row["difference_c"] == 0 and row["dr_total_c"] == row["delivered_c"] == 180000
    logged = world.conn.execute("SELECT user_name FROM audit_logs WHERE action = 'DRS_MATCH' AND of_id = ?", (o.of_id,)).fetchone()
    assert logged["user_name"] == "Lorna Tan"
    with pytest.raises(POError, match="isn't waiting"):
        payments.drs_match(world.conn, world.accounting, o.of_id)


def test_a_viewer_cant_press_drs_match(world):
    o = Order(world, av=8)
    o.short_memo()
    viewer = auth.create_user(world.conn, world.admin, "vic2", "Vic", PASSWORD, ["VIEWER"], must_change=False)
    with pytest.raises(POError):
        payments.drs_match(world.conn, viewer, o.of_id)
    assert o.row()["stage"] == payments.ENTER_DRS


def test_an_amended_or_closed_po_means_someone_compares_the_drs(world):
    o = Order(world)
    orders.close(world.conn, world.buyer, o.pos[NORTH]["id"], "Done")
    assert o.row()["why"] == ["PO closed"] and o.row()["stage"] == payments.ENTER_DRS


def test_the_page_offers_dr_differs_on_a_clean_order_and_asks_which_when_a_check_holds_several(app, world, store, monkeypatch):
    _supplier(world)
    first, second = Order(world), Order(world)
    win = MainWindow(world.conn, world.accounting)
    page = win.payments
    assert page.tabs.tabText(0) == "Check the DRs  0" and "goes straight" in page.empty.text() and page.tabs.tabText(1) == "Checks to make  1"
    page.tabs.setCurrentIndex(1)
    assert page.differs_btn.isVisibleTo(page) and page.primary_btn.text() == "Record check" and cell(page, page.to_pay, "Orders").count("OF") == 2
    opened = []
    monkeypatch.setattr(page, "_enter_drs", lambda order: opened.append(order["id"]))
    monkeypatch.setattr(payments_page.QInputDialog, "getItem", staticmethod(lambda *a, **k: (second.row()["of_number"], True)))
    page._dr_differs()
    assert opened == [second.of_id] and first
    page.tabs.setCurrentIndex(2)
    assert not page.differs_btn.isVisibleTo(page)
    win.close()


def test_the_dr_panel_starts_on_the_count_date_and_types_with_no_mouse(app, world, store, monkeypatch):
    # The DRs open as a panel on the right of Payments, with the chosen DR's products.
    o = Order(world)
    win = MainWindow(world.conn, world.accounting)
    panel = win.payments.dr_panel
    win.payments._enter_drs(o.row())
    receipts = panel.receipts
    first, second = (panel.dates[d["id"]] for d in receipts)
    assert panel.drs.item(0, 3).text() == "" and date_value(first) == receipts[0]["suggested_date"]
    assert first.nextInFocusChain() is not None
    # Pick only the DR that differs, and the rest stay blank.
    win.show()
    second.setFocus()
    app.processEvents()
    QTest.keyClicks(second, "102826")
    panel.drs.selectRow(1)
    panel.lines.item(0, panel.C_DR_UNIT).setText("110.00")
    assert panel.drs.item(1, 3).text() == "1,100.00"
    panel.save()
    row = o.row()
    assert row["entered"] == 1 and row["stage"] == payments.DIFFERENCE and row["difference_c"] == 10000
    assert next(d for d in row["receipts"] if d["dr_date"])["dr_date"] == "2026-10-28"
    win.close()


@pytest.mark.parametrize("typed, shown, stored", [("110526", "11-05-26", "2026-11-05"), ("033126", "03-31-26", "2026-03-31"), ("123125", "12-31-25", "2025-12-31")])
def test_a_date_types_month_day_year_with_no_mouse(app, typed, shown, stored):
    # Starting from a short month, and with nothing set yet, both work.
    for box in (common.date_edit("2026-02-10"), common.date_edit(optional=True)):
        box.show()
        box.setFocus()
        app.processEvents()
        QTest.keyClicks(box, typed)
        assert box.text() == shown and date_value(box) == stored and box.calendarPopup()
        box.close()


@pytest.mark.parametrize("entry, message", [((None, "1000"), "type the date"), (("2026-10-28", "abc"), "isn't an amount"), (("2999-01-01", "1000"), "future")])
def test_dr_entries_are_checked(world, entry, message):
    o = Order(world)
    receipt = payments.receipts_to_enter(world.conn, world.accounting, o.of_id)[0]
    with pytest.raises(POError, match=message):
        payments.save_dr_entries(world.conn, world.accounting, o.of_id, {receipt["id"]: entry})


def test_a_dr_that_doesnt_match_needs_a_reason_and_comes_back_if_it_changes(world):
    o = Order(world)
    o.enter(off_by="250")
    assert o.row()["stage"] == payments.DIFFERENCE and o.row()["difference_c"] == 50000 and payments.checks_due(world.conn) == []
    with pytest.raises(POError, match="Write why"):
        payments.accept_difference(world.conn, world.accounting, o.of_id, " ")
    payments.accept_difference(world.conn, world.accounting, o.of_id, "Supplier billed freight. Told them.")
    assert o.row()["stage"] == payments.SIGN_OFF  # Admin or management gives the final check
    payments.sign_off_difference(world.conn, world.admin, o.of_id)
    assert o.row()["stage"] == payments.TO_PAY and o.row()["pay_c"] == 200000  # the payment still follows the order's costs
    o.enter(off_by="300")
    assert o.row()["stage"] == payments.DIFFERENCE


def test_within_a_peso_counts_as_matching(world):
    o = Order(world)
    o.enter(off_by="0.50")
    assert o.row()["stage"] == payments.TO_PAY


def test_one_check_per_supplier_per_saturday(world):
    first = Order(world)
    second = world.approved(items=[world.item(cost=10000, alloc={NORTH: 5}, description="Socks", item_code="SOCK-2")])
    pos = order_forms.generate_pos(world.conn, world.buyer, second)
    orders.issue(world.conn, world.buyer, pos[0]["id"])
    item = orders.get_po(world.conn, pos[0]["id"])["items"][0]["id"]
    for n in (1, 2):
        receiving.save_count(world.conn, auth.get_user(world.conn, world.conn.execute("SELECT id FROM users WHERE username = ?", (f"r{NORTH}x{n}",)).fetchone()["id"]), pos[0]["id"], {item: "5"})
    first.enter("2026-10-26")
    receipts = payments.receipts_to_enter(world.conn, world.accounting, second)
    payments.save_dr_entries(world.conn, world.accounting, second, {receipts[0]["id"]: ("2026-10-27", "500")})
    groups = payments.checks_due(world.conn)
    assert len(groups) == 1 and len(groups[0]["orders"]) == 2 and groups[0]["check_c"] == 250000


def _recorded(world, o, number="880001"):
    o.enter()
    group = payments.checks_due(world.conn)[0]
    return payments.record_check(world.conn, world.accounting, [x["id"] for x in group["orders"]], number, group["due"].isoformat(), f"{group['check_c'] / 100:.2f}")


def test_an_order_cant_be_paid_twice_and_a_reprint_means_cancelling_first(world):
    o = Order(world)
    check = _recorded(world, o)
    assert o.row()["stage"] == payments.PAID and payments.checks_due(world.conn) == []
    with pytest.raises(POError, match="cancel that check first"):
        payments.record_check(world.conn, world.accounting, [o.of_id], "880002", "2026-12-19", "2000")
    with pytest.raises(POError, match="Cancel the check"):
        payments.save_dr_entries(world.conn, world.accounting, o.of_id, {})
    with pytest.raises(POError, match="why"):
        payments.cancel_check(world.conn, world.accounting, check, "")
    payments.cancel_check(world.conn, world.accounting, check, "Printed badly")
    assert o.row()["stage"] == payments.TO_PAY and len(payments.checks_due(world.conn)) == 1
    with pytest.raises(POError, match="already cancelled"):
        payments.cancel_check(world.conn, world.accounting, check, "Again")
    # The old number stays burned, and the new one goes through.
    with pytest.raises(POError, match="never used twice"):
        payments.record_check(world.conn, world.accounting, [o.of_id], "880001", "2026-12-19", "2000")
    payments.record_check(world.conn, world.accounting, [o.of_id], "880002", "2026-12-19", "2000")
    assert o.row()["check_number"] == "880002"


def test_a_check_for_the_wrong_amount_needs_a_reason(world):
    o = Order(world)
    o.enter()
    with pytest.raises(payments.CheckDifference, match="worked out"):
        payments.record_check(world.conn, world.accounting, [o.of_id], "880001", "2026-12-19", "1900")
    assert payments.checks(world.conn) == []
    payments.record_check(world.conn, world.accounting, [o.of_id], "880001", "2026-12-19", "1900", "The check used an older cost. Fixing next run.")
    assert payments.checks(world.conn)[0]["difference_reason"].startswith("The check")


def test_recorded_checks_cant_be_changed_or_deleted(world):
    o = Order(world)
    check = _recorded(world, o)
    with pytest.raises(Exception, match="cannot be changed"):
        world.conn.execute("UPDATE payment_checks SET amount_c = 1 WHERE id = ?", (check,))
    with pytest.raises(Exception, match="cannot be deleted"):
        world.conn.execute("DELETE FROM payment_checks WHERE id = ?", (check,))


def test_paying_too_much_comes_off_the_next_check(world):
    """A paid delivery can't be recounted any more, so credit now comes from a check that paid more than the order, with its reason on record."""
    o = Order(world, av=10)
    o.enter()
    group = payments.checks_due(world.conn)[0]
    payments.record_check(world.conn, world.accounting, [o.of_id], "880001", group["due"].isoformat(), f"{group['check_c'] / 100 + 200:.2f}", "Typed 200 more on the check")
    assert payments.supplier_credit(world.conn, world.supplier) == 20000
    # The next order for this supplier pays 200.00 less, and uses the credit up.
    next_order = Order(world)
    next_order.enter()
    group = payments.checks_due(world.conn)[0]
    assert (group["amount_c"], group["credit_c"], group["check_c"]) == (200000, 20000, 180000)
    payments.record_check(world.conn, world.accounting, [next_order.of_id], "880009", group["due"].isoformat(), "1800")
    assert payments.supplier_credit(world.conn, world.supplier) == 0


def test_a_viewer_can_look_but_not_change_anything(world):
    o = Order(world)
    viewer = auth.create_user(world.conn, world.admin, "vic", "Vic", PASSWORD, ["VIEWER"], must_change=False)
    with pytest.raises(POError):
        payments.receipts_to_enter(world.conn, viewer, o.of_id)
    with pytest.raises(POError):
        payments.record_check(world.conn, viewer, [o.of_id], "1", "2026-12-19", "1")


def test_a_receiver_never_sees_payments(app, world, store):
    o = Order(world)
    login = auth.get_user(world.conn, world.conn.execute("SELECT id FROM users WHERE username = ?", (f"r{NORTH}x1",)).fetchone()["id"])
    win = MainWindow(world.conn, login)
    assert win.payments is None and "Payments" not in [text for text, _ in win.pages] and o
    win.close()


def test_the_payments_page_walks_an_order_from_drs_to_paid(app, world, store, monkeypatch):
    o = Order(world, av=8)
    o.short_memo()
    opened = []
    monkeypatch.setattr(os, "startfile", lambda p: opened.append(p), raising=False)
    monkeypatch.setattr(payments_page, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(payments_page, "ask_text", lambda *a, **k: "Reason")
    win = MainWindow(world.conn, world.accounting)
    page = win.payments
    assert page.tabs.tabText(0) == "Check the DRs  1" and page.primary_btn.text() == "DRs match" and page.differs_btn.isVisibleTo(page) and cell(page, page.to_check, "DRs typed") == "0 of 2"
    assert "Compare the DRs" in cell(page, page.to_check, "Next step") and "short" in cell(page, page.to_check, "Next step")
    # The DR panel opens beside the list. Every DR matches the order here, so nothing is typed, and DRs match takes them as they are.
    page._dr_differs()
    assert page.dr_panel.isVisibleTo(page) and page.dr_panel.drs.rowCount() == 2
    page.dr_panel.closed.emit()
    page._primary()
    page.refresh()
    assert page.tabs.tabText(0) == "Check the DRs  0" and page.tabs.tabText(1) == "Checks to make  1" and not page.empty.isHidden()
    page.tabs.setCurrentIndex(1)
    # Deductions changed what was signed, so the Final OF comes first, and printing it makes the check ready.
    assert page.primary_btn.text() == "Print Final OF" and cell(page, page.to_pay, "Final OF") == "Print" and "to pay" in page.breakdown.text()
    page._primary()
    assert page.primary_btn.text() == "Record check" and page.second_btn.text() == "Print Final OF again" and cell(page, page.to_pay, "Final OF") == "Printed"
    assert opened and "FINAL COPY" in PdfReader(opened[0]).pages[0].extract_text()
    check = payments_page.CheckDialog(page, world.conn, world.accounting, page._selected_group())
    check.number.setText("880001")
    assert check.amount.text() == "1,800.00"
    check.save()
    page.refresh()
    assert page.tabs.tabText(1) == "Checks to make  0"
    on_tab = {payments.FUTURE: 2, payments.DUE: 3}[payments.checks(world.conn)[0]["bucket"]]
    assert page.tabs.tabText(on_tab).endswith("  1")
    page.tabs.setCurrentIndex(on_tab)
    assert cell(page, page.paid, "Check number") == "880001" and page.cancel_btn.isVisibleTo(page) and "cancel it first" in page.note.text() and page.primary_btn.text() == "Mark claimed"
    page.cancel_check()
    assert page.tabs.tabText(1) == "Checks to make  1" and page.tabs.tabText(6) == "Cancelled checks  1"
    page.tabs.setCurrentIndex(6)
    assert cell(page, page.paid, "Check number") == "880001" and not page.cancel_btn.isVisibleTo(page)
    win.close()


def test_a_difference_on_the_page_asks_for_a_reason(app, world, store, monkeypatch):
    o = Order(world)
    o.enter(off_by="100")
    # The difference shows as a table: each PO with its DR, the order and the gap, opening to its products with the unit price.
    dialog = payments_page.DifferenceDialog(None, payments.order_row(world.conn, o.of_id), ask_reason=True)
    top = dialog.tree.topLevelItem(0)
    assert top.text(5) == "1,100.00" and top.text(4) == "1,000.00" and top.text(6) == "+100.00" and top.child(0).text(3) == "100.00"
    dialog._ok()
    assert not dialog.result()  # a reason is needed
    def accept(self):
        self.reason = "Checked with the supplier"
        return True
    monkeypatch.setattr(payments_page.DifferenceDialog, "exec", accept)
    win = MainWindow(world.conn, world.accounting)
    page = win.payments
    assert page.primary_btn.text() == "Accept the difference" and page.second_btn.text() == "DR differs" and "differs" in cell(page, page.to_check, "Next step")
    page._primary()
    # Accounting accepted it. Admin or management gives the final check before it can be paid.
    assert page.tabs.tabText(1) == "Checks to make  0" and "sign off" in cell(page, page.to_check, "Next step") and not page.primary_btn.isVisibleTo(page)
    win.close()
    boss = MainWindow(world.conn, world.admin)
    page = boss.payments
    assert page.primary_btn.text() == "Sign off" and page.primary_btn.isVisibleTo(page)
    page._primary()
    assert page.tabs.tabText(1) == "Checks to make  1"
    boss.close()


def test_only_an_order_that_changed_needs_a_final_of(app, world, store):
    clean, changed = Order(world), Order(world, av=8)
    changed.short_memo()
    assert not payments.needs_final_of(world.conn, clean.of_id) and payments.needs_final_of(world.conn, changed.of_id)
    win = MainWindow(world.conn, world.accounting)
    # Payments is the one place it prints.
    assert "Final OF" not in [win.print_pos.tabs.tabText(i) for i in range(win.print_pos.tabs.count())]
    win.close()


def test_the_supplier_screen_saves_a_term_discount(app, world, store, monkeypatch):
    monkeypatch.setattr(pages, "info", lambda *a, **k: None)
    win = MainWindow(world.conn, world.admin)
    page = win.suppliers
    page.table.selectRow(0)
    page.term_discount.setText("5")
    page.term_days.setText("14")
    page.save()
    row = world.conn.execute("SELECT term_discount_bp, term_discount_days FROM suppliers WHERE id = ?", (world.supplier,)).fetchone()
    assert tuple(row) == (500, 14)
    win.close()


def test_a_database_from_before_payments_gains_them(env):
    path = env / "old.sqlite"
    c = db.open_database(path)
    boss = auth.create_first_admin(c, "boss", "Boss Man", PASSWORD)
    sup = masters.save_supplier(c, boss, {"code": "SEA", "name": "Sea Trading", "payment_terms": "60D"})
    plain = masters.save_supplier(c, boss, {"code": "PLN", "name": "Plain Works", "payment_terms": "60D"})
    # A real version 16 file has none of the later guards, so the one on late_damage that reads payment_checks goes too (V28 makes it again).
    c.executescript("BEGIN;DROP TRIGGER late_damage_paid_before_known;DROP TABLE payment_check_ofs;DROP TABLE payment_checks;DROP TABLE payment_differences;DROP TABLE dr_entries;"
                    "ALTER TABLE suppliers DROP COLUMN term_discount_bp;ALTER TABLE suppliers DROP COLUMN term_discount_days;PRAGMA user_version = 16;COMMIT;")
    c.close()
    conn = db.open_database(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
    assert list((env / "backups").glob("po-before-migration-v16-*.sqlite"))
    assert tuple(conn.execute("SELECT term_discount_bp, term_discount_days FROM suppliers WHERE id = ?", (sup,)).fetchone()) == (0, 0)
    assert tuple(conn.execute("SELECT term_discount_bp, term_discount_days FROM suppliers WHERE id = ?", (plain,)).fetchone()) == (0, 0)
    assert auth.login(conn, "boss", PASSWORD).id == boss.id and payments.checks(conn) == []
    conn.close()


from PySide6.QtCore import Qt  # noqa: E402


def cell(page, tree, header, row=0):
    return _rows(tree)[row].text(page.columns[tree].index(header))


def _rows(tree):
    """The rows of a list, leaving out the section headings on Checks to make."""
    return [item for item in (tree.topLevelItem(i) for i in range(tree.topLevelItemCount())) if not isinstance(item.data(0, Qt.ItemDataRole.UserRole), str)]


# ---- Business rules

def test_the_term_discount_is_on_the_goods_kept_only(world):
    """Not on freight or other charges, and not on goods sent back on a claim stub."""
    _supplier(world, term_discount_bp=200, term_discount_days=7)
    o = Order.__new__(Order)
    o.w = world
    o.of_id = world.approved(items=[world.item(cost=10000, alloc={NORTH: 10, SOUTH: 10}, description="Socks", item_code="SOCK-1")], other_charges_c=50000, other_charges_note="Freight")
    o.pos = {p["branch_code"]: p for p in order_forms.generate_pos(world.conn, world.buyer, o.of_id)}
    for p in o.pos.values():
        orders.issue(world.conn, world.buyer, p["id"])
    of = order_forms.get_of(world.conn, o.of_id)
    assert of["grand_total_c"] == 250000 and of["term_discount_c"] == 4000  # 2% of the 2,000.00 of goods, not of the 500.00 freight
    o.count(NORTH, 10, damaged="2")
    o.count(SOUTH, 10)
    o.stub()
    money = payments.settlement(world.conn, o.of_id)
    assert money["cs_c"] == 2000 * 10 and money["term_c"] == (money["delivered_c"] - 50000 - money["cs_c"]) * 2 // 100


def test_a_check_for_a_different_amount_waits_for_admin_or_management(world):
    o = Order(world)
    o.enter()
    group = payments.checks_due(world.conn)[0]
    ids = [x["id"] for x in group["orders"]]
    check_id = payments.record_check(world.conn, world.accounting, ids, "660001", "2030-06-01", f"{group['check_c'] / 100 - 500:.2f}", "Supplier asked for 500 less")
    assert payments.checks(world.conn)[0]["waiting_signoff"]
    with pytest.raises(POError, match="sign it off"):
        payments.claim_check(world.conn, world.accounting, check_id)
    with pytest.raises(db.PermissionDenied):
        payments.sign_off_check(world.conn, world.accounting, check_id)
    payments.sign_off_check(world.conn, world.admin, check_id)
    assert not payments.checks(world.conn)[0]["waiting_signoff"]
    with pytest.raises(POError, match="isn't waiting"):
        payments.sign_off_check(world.conn, world.admin, check_id)
    payments.claim_check(world.conn, world.accounting, check_id)
    # The sign-off stays on record, even edited in the file.
    with pytest.raises(sqlite3.IntegrityError):
        world.conn.execute("DELETE FROM payment_signoffs")
    # A check matching what the program worked out needs nobody.
    other = Order(world)
    other.enter()
    group = next(g for g in payments.checks_due(world.conn) if other.of_id in [x["id"] for x in g["orders"]])
    plain = payments.record_check(world.conn, world.accounting, [x["id"] for x in group["orders"]], "660002", "2030-06-08", f"{group['check_c'] / 100:.2f}")
    assert not payments.check_waiting(world.conn, plain)


def test_admin_accepting_a_difference_signs_it_off_at_once(world):
    o = Order(world)
    o.enter(off_by="250")
    payments.accept_difference(world.conn, world.admin, o.of_id, "Checked it myself")
    assert o.row()["stage"] == payments.TO_PAY


def test_a_po_says_when_its_dr_differs(app, world):
    o = Order(world)
    assert payments.dr_differs(world.conn, o.pos[NORTH]["id"]) is None  # nothing typed yet
    o.enter(off_by="250")
    assert payments.dr_differs(world.conn, o.pos[NORTH]["id"]) == 25000
    o.enter(off_by="0.50")
    assert payments.dr_differs(world.conn, o.pos[NORTH]["id"]) is None  # within a peso is the same
    o.enter(off_by="-300")
    win = MainWindow(world.conn, world.buyer)
    win.open_po(o.pos[NORTH]["id"])
    assert "DR differs" in win.po_page.message.text() and "300.00 less" in win.po_page.message.text()
    win.close()

