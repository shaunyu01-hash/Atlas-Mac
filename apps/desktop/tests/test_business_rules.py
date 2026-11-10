"""Business rules: who may undo money, recounts, suppliers, receiving outside the program and the supplier balance."""
from __future__ import annotations

import sqlite3

import pytest
from PySide6.QtWidgets import QApplication

from atlaspo import auth, claim_stubs, db, debit_memos, item_passes, masters, order_forms, orders, payments, receiving, schema
from atlaspo.db import POError, PermissionDenied

from conftest import NORTH, PASSWORD, SOUTH
from test_payments import Order


@pytest.fixture(autouse=True)
def people(world):
    world.accounting = auth.create_user(world.conn, world.admin, "lorna", "Lorna Tan", PASSWORD, ["ACCOUNTING"], must_change=False)
    world.approver = auth.create_user(world.conn, world.admin, "ramon", "Ramon Villanueva", PASSWORD, ["APPROVER"], must_change=False)


@pytest.fixture
def app():
    yield QApplication.instance() or QApplication([])


def _pay(world, o, number="990001", extra=0):
    o.enter()
    group = next(g for g in payments.checks_due(world.conn) if any(x["id"] == o.of_id for x in g["orders"]))
    reason = "Typed differently on the check" if extra else ""
    return payments.record_check(world.conn, world.accounting, [x["id"] for x in group["orders"]], number, group["due"].isoformat(), f"{group['check_c'] / 100 + extra:.2f}", reason)


def _receiver(world, branch, n):
    return auth.get_user(world.conn, world.conn.execute("SELECT id FROM users WHERE username = ?", (f"r{branch}x{n}",)).fetchone()["id"])


# ---- 1. Received in full outside the program, until branches count in it.

def _issued(world):
    of_id = world.approved(items=[world.item(cost=10000, alloc={NORTH: 10}, description="Socks", item_code="SOCK-1")])
    po = order_forms.generate_pos(world.conn, world.buyer, of_id)[0]
    orders.issue(world.conn, world.buyer, po["id"])
    return of_id, po["id"]


def test_only_admin_records_a_delivery_received_outside_with_where_it_is_recorded(world):
    of_id, po_id = _issued(world)
    with pytest.raises(PermissionDenied):
        orders.receive_outside(world.conn, world.buyer, po_id, "RR 1234")
    with pytest.raises(PermissionDenied):
        orders.receive_outside(world.conn, world.approver, po_id, "RR 1234")
    with pytest.raises(POError, match="Write where"):
        orders.receive_outside(world.conn, world.admin, po_id, "  ")
    assert orders.can_receive_outside(world.conn, po_id)
    orders.receive_outside(world.conn, world.admin, po_id, "RR 1234")
    po = orders.get_po(world.conn, po_id)
    assert po["status"] == orders.CLOSED and po["items"][0]["received_qty_m"] == po["items"][0]["qty_m"] and "RR 1234" in po["close_reason"]
    assert receiving.taken_as_delivered(world.conn, po_id) and not orders.can_receive_outside(world.conn, po_id)
    # Nothing is paid before the branch receivers counted it, so this order never reaches Payments. It's paid outside the program.
    row = payments.order_row(world.conn, of_id)
    assert row["stage"] == payments.NOT_DONE and of_id not in [r["id"] for r in payments.orders_in_payment(world.conn)]
    assert world.conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action = 'PO_RECEIVED_OUTSIDE' AND po_id = ?", (po_id,)).fetchone()[0] == 1
    # It stays on record for good.
    with pytest.raises(sqlite3.DatabaseError):
        world.conn.execute("DELETE FROM outside_receipts WHERE po_id = ?", (po_id,))


def test_a_counted_po_goes_by_its_count_not_outside(world):
    o = Order(world, av=8)
    po_id = o.pos[NORTH]["id"]
    assert not orders.can_receive_outside(world.conn, po_id)
    with pytest.raises(POError, match="goes by that count"):
        orders.receive_outside(world.conn, world.admin, po_id, "RR 1")


def test_closing_without_a_count_still_pays_nothing(world):
    """Only the explicit Admin step pays an uncounted new PO. A plain close is still refused."""
    of_id, po_id = _issued(world)
    with pytest.raises(POError, match="cancel it instead"):
        orders.close(world.conn, world.buyer, po_id, "Arrived")


# ---- 2. Only management cancels a debit memo, claim stub or item pass, and never once the order is paid.

def test_only_management_cancels_memos_stubs_and_passes(world):
    o = Order(world, av=8, ros=10, damaged="1")
    memo, stub = o.short_memo(), o.stub()
    for fn, doc in ((debit_memos.cancel, memo), (claim_stubs.cancel, stub)):
        with pytest.raises(PermissionDenied):
            fn(world.conn, world.buyer, doc["id"], "Mistake")
    assert not debit_memos.can_cancel(world.conn, world.buyer, debit_memos.get(world.conn, world.buyer, memo["id"]))
    debit_memos.cancel(world.conn, world.approver, memo["id"], "Supplier delivered the rest")
    claim_stubs.cancel(world.conn, world.admin, stub["id"], "Wrong line")


def test_an_item_pass_is_cancelled_by_management_only(world):
    o = Order(world, av=12)
    made = item_passes.issue(world.conn, _receiver(world, NORTH, 1), o.pos[NORTH]["id"])
    with pytest.raises(PermissionDenied):
        item_passes.cancel(world.conn, world.buyer, made["id"], "Lost")
    item_passes.cancel(world.conn, world.approver, made["id"], "Lost")


def test_nothing_that_changes_the_money_can_be_undone_once_the_check_is_recorded(world):
    o = Order(world, av=8)
    memo = o.short_memo()
    check = _pay(world, o)
    with pytest.raises(POError, match="already pays this order"):
        receiving.reopen(world.conn, world.admin, o.pos[SOUTH]["id"], "Recount")
    po = orders.get_po(world.conn, o.pos[NORTH]["id"])
    with pytest.raises(POError, match="already pays this order"):
        orders.amend(world.conn, world.buyer, po["id"], {"delivery_instructions": "Call first"}, po["items"], "Call first")
    # Cancelling the check opens it up again.
    payments.cancel_check(world.conn, world.accounting, check, "Spoiled")
    orders.amend(world.conn, world.buyer, po["id"], {"delivery_instructions": "Call first"}, po["items"], "Call first")


def test_a_late_delivery_after_the_check_is_owed_on_the_next_check(world):
    """The owner: late deliveries are fine as long as they came. Management cancels the memo, and the supplier gets it on their next check."""
    o = Order(world, av=8)
    memo = o.short_memo()
    _pay(world, o)
    o.count(NORTH, 2)  # the two missing pieces arrive after the check
    row = debit_memos.get(world.conn, world.admin, memo["id"])
    assert debit_memos.arrived_since(world.conn, row) == 2000
    assert debit_memos.can_cancel(world.conn, world.approver, row) and not debit_memos.can_cancel(world.conn, world.buyer, row)
    debit_memos.cancel(world.conn, world.approver, memo["id"], "The missing pieces came after the check")
    assert payments.supplier_balance(world.conn, world.supplier) == -memo["total_c"]
    assert [r["owed_c"] for r in payments.owed_without_orders(world.conn)] == [memo["total_c"]]


# ---- 3. Suppliers: Admin only.

def test_purchasers_look_suppliers_up_but_only_admin_adds_or_changes_one(world):
    with pytest.raises(PermissionDenied):
        masters.save_supplier(world.conn, world.buyer, {"code": "NEW", "name": "New Supplier Ltd"})
    with pytest.raises(PermissionDenied):
        masters.save_supplier(world.conn, world.buyer, {"code": "ABC", "name": "ABC Trading", "term_discount_bp": 500}, world.supplier, confirm_duplicate=True)
    assert masters.search_suppliers(world.conn, "ABC")
    assert masters.save_supplier(world.conn, world.admin, {"code": "NEW", "name": "New Supplier Ltd"})


def test_the_supplier_page_is_read_only_for_purchasers_and_folds_away(app, world):
    from atlaspo.ui.pages import SuppliersPage
    page = SuppliersPage(world.conn, world.buyer)
    page.table.selectRow(0)
    assert not page.new_btn.isEnabled() and not page.save_btn.isEnabled() and page.f["name"].isReadOnly() and not page.vat_mode.isEnabled()
    assert page.f["name"].text() == "ABC Trading"
    page._toggle_details()
    assert page.details.isHidden() and page.fold_btn.text() == "Show details"
    # Remembered for this person.
    again = SuppliersPage(world.conn, world.buyer)
    assert again.details.isHidden()
    again._toggle_details()
    assert not again.details.isHidden()
    admin = SuppliersPage(world.conn, world.admin)
    assert admin.save_btn.isEnabled() and not admin.f["name"].isReadOnly()


# ---- 4. Recounts: matched is finished, the home office reopens once, and nobody counts forever.

def test_a_matched_delivery_is_reopened_only_by_management_and_only_once(world):
    o = Order(world, av=10)
    po_id = o.pos[NORTH]["id"]
    r1, r2 = _receiver(world, NORTH, 1), _receiver(world, NORTH, 2)
    item = orders.get_po(world.conn, po_id)["items"][0]["id"]
    with pytest.raises(POError, match="Only the home office"):
        receiving.save_count(world.conn, r1, po_id, {item: "9"}, recount_of=world.conn.execute("SELECT MAX(id) FROM branch_counts WHERE po_id = ?", (po_id,)).fetchone()[0])
    with pytest.raises(PermissionDenied):
        receiving.reopen(world.conn, world.buyer, po_id, "Found more")
    with pytest.raises(POError, match="Write why"):
        receiving.reopen(world.conn, world.approver, po_id, " ")
    assert receiving.reopen_label(world.conn, po_id) == "Reopen for recount"
    receiving.reopen(world.conn, world.approver, po_id, "One carton was South's")
    assert not payments.order_row(world.conn, o.of_id)["stage"] == payments.TO_PAY  # nothing gets paid while it's reopened
    receiving.save_count(world.conn, r1, po_id, {item: "9"})
    receiving.save_count(world.conn, r2, po_id, {item: "9"})
    assert orders.get_po(world.conn, po_id)["items"][0]["received_qty_m"] == 9000
    with pytest.raises(POError, match="already reopened once"):
        receiving.reopen(world.conn, world.admin, po_id, "Again")
    # The permission was used by that recount, and stays on record.
    with pytest.raises(sqlite3.DatabaseError):
        world.conn.execute("UPDATE count_reopens SET used_by_count_id = NULL")
    with pytest.raises(sqlite3.DatabaseError):
        world.conn.execute("DELETE FROM count_reopens")


def test_a_delivery_with_a_memo_stub_or_pass_cant_be_reopened_until_management_cancels_it(world):
    o = Order(world, av=8)
    memo = o.short_memo()
    po_id = o.pos[NORTH]["id"]
    assert not receiving.reopen_label(world.conn, po_id)
    with pytest.raises(POError, match="cancel those first"):
        receiving.reopen(world.conn, world.admin, po_id, "Found 2")
    debit_memos.cancel(world.conn, world.admin, memo["id"], "Found 2 in another box")
    receiving.reopen(world.conn, world.admin, po_id, "Found 2 in another box")


def test_a_receiver_redoes_their_own_waiting_count_once(world):
    of_id, po_id = _issued(world)
    make = lambda name: auth.create_user(world.conn, world.admin, name, name.title(), PASSWORD, ["RECEIVER"], must_change=False, branch_code=NORTH)
    r1, r2 = make("ana"), make("ben")
    item = orders.get_po(world.conn, po_id)["items"][0]["id"]
    first = receiving.save_count(world.conn, r1, po_id, {item: "9"})["count_id"]
    assert receiving.can_redo(world.conn, r1, first) and not receiving.can_redo(world.conn, r2, first)
    second = receiving.save_count(world.conn, r1, po_id, {item: "10"}, recount_of=first)["count_id"]
    assert not receiving.can_redo(world.conn, r1, second)
    with pytest.raises(POError, match="already counted this again once"):
        receiving.save_count(world.conn, r1, po_id, {item: "11"}, recount_of=second)
    # Their own earlier figure is there to see, and only theirs.
    assert receiving.sheet(world.conn, r1, po_id, second)[0]["previous"] == (10000, 0)
    assert receiving.sheet(world.conn, r2, po_id)[0]["previous"] is None
    assert receiving.save_count(world.conn, r2, po_id, {item: "10"})["outcome"] == receiving.APPROVED


def test_counts_that_never_agree_stop_after_two_rounds_and_each_allowance_is_one_more(world):
    of_id, po_id = _issued(world)
    make = lambda name: auth.create_user(world.conn, world.admin, name, name.title(), PASSWORD, ["RECEIVER"], must_change=False, branch_code=NORTH)
    r1, r2 = make("ana"), make("ben")
    item = orders.get_po(world.conn, po_id)["items"][0]["id"]
    for a, b in (("9", "8"), ("9", "7")):
        receiving.save_count(world.conn, r1, po_id, {item: a})
        receiving.save_count(world.conn, r2, po_id, {item: b})
    assert receiving.progress(world.conn, po_id).endswith("Counting stopped") and not receiving.to_count(world.conn, r1)
    receiving.reopen(world.conn, world.approver, po_id, "Asked the branch to recount slowly")
    receiving.save_count(world.conn, r1, po_id, {item: "9"})
    receiving.save_count(world.conn, r2, po_id, {item: "8"})
    # One more round only: it stops again.
    assert receiving.progress(world.conn, po_id).endswith("Counting stopped")
    with pytest.raises(POError, match="counting has stopped"):
        receiving.save_count(world.conn, r1, po_id, {item: "9"})


def test_back_button_and_earlier_count_on_the_count_sheet(app, world, monkeypatch):
    from atlaspo.ui.main_window import MainWindow
    from atlaspo.ui.receiving_page import C_BEFORE, C_PIECES
    of_id, po_id = _issued(world)
    make = lambda name: auth.create_user(world.conn, world.admin, name, name.title(), PASSWORD, ["RECEIVER"], must_change=False, branch_code=NORTH)
    r1, r2 = make("ana"), make("ben")
    item = orders.get_po(world.conn, po_id)["items"][0]["id"]
    receiving.save_count(world.conn, r1, po_id, {item: "9"})
    receiving.save_count(world.conn, r2, po_id, {item: "8"})
    win = MainWindow(world.conn, r1)
    page = win.receiving
    assert not page.back_btn.isVisibleTo(page)
    page.po_list.cellClicked.emit(0, 0)
    assert page.back_btn.isVisibleTo(page) and not page.grid.isColumnHidden(C_BEFORE) and page.grid.item(0, C_BEFORE).text() == "9"
    page.grid.item(0, C_PIECES).setText("10")
    monkeypatch.setattr("atlaspo.ui.receiving_page.confirm", lambda *a, **k: True)
    page._clear_pressed()
    assert page.po_id is not None and page.grid.item(0, C_PIECES).text() == ""  # still on the sheet
    page._back_pressed()
    assert page.po_id is None and page.po_list.isVisibleTo(page) and not page.back_btn.isVisibleTo(page)
    win.close()


def test_the_home_office_reopens_from_the_received_page(app, world, monkeypatch):
    from atlaspo.ui.main_window import MainWindow
    o = Order(world, av=10)
    buyer_page = MainWindow(world.conn, world.buyer).received
    assert not buyer_page.reopen_btn.isVisibleTo(buyer_page)
    win = MainWindow(world.conn, world.approver)
    page = win.received
    page._set_tab(receiving.COMPLETED)
    row = next(i for i, r in enumerate(page.rows) if r["id"] == o.pos[NORTH]["id"])
    page.table.selectRow(row)
    assert page.reopen_btn.isEnabled() and page.reopen_btn.text() == "Reopen for recount"
    monkeypatch.setattr("atlaspo.ui.receiving_page.ask_text", lambda *a, **k: "Found a carton")
    page.reopen()
    assert receiving.progress(world.conn, o.pos[NORTH]["id"]) == "Reopened for recount"
    win.close()


# ---- 5. One running balance per supplier.

def test_a_cancelled_check_puts_its_credit_back_once(world):
    first = Order(world)
    _pay(world, first, "990001", extra=200)
    assert payments.supplier_balance(world.conn, world.supplier) == 20000
    second = Order(world)
    check = _pay(world, second, "990002")
    assert payments.supplier_balance(world.conn, world.supplier) == 0
    # The second check is spoiled: the credit comes back, once.
    payments.cancel_check(world.conn, world.accounting, check, "Spoiled")
    assert payments.supplier_balance(world.conn, world.supplier) == 20000
    group = payments.checks_due(world.conn)[0]
    assert (group["credit_c"], group["owed_c"], group["check_c"]) == (20000, 0, group["amount_c"] - 20000)


def test_what_we_owe_a_supplier_is_shown_and_added_to_their_next_check(world):
    first = Order(world)
    _pay(world, first, "990001", extra=-300)
    assert payments.supplier_balance(world.conn, world.supplier) == -30000
    assert payments.supplier_credit(world.conn, world.supplier) == 0
    assert [(s["code"], s["owed_c"]) for s in payments.owed_without_orders(world.conn)] == [("ABC", 30000)]
    second = Order(world)
    second.enter()
    group = payments.checks_due(world.conn)[0]
    assert (group["credit_c"], group["owed_c"], group["check_c"]) == (0, 30000, group["amount_c"] + 30000)
    assert not payments.owed_without_orders(world.conn)
    payments.record_check(world.conn, world.accounting, [second.of_id], "990002", group["due"].isoformat(), f"{group['check_c'] / 100:.2f}")
    assert payments.supplier_balance(world.conn, world.supplier) == 0
    stored = world.conn.execute("SELECT credit_applied_c FROM payment_checks WHERE check_number = '990002'").fetchone()[0]
    assert stored == -30000


def test_payments_page_shows_we_owe(app, world):
    from atlaspo.ui.main_window import MainWindow
    first = Order(world)
    _pay(world, first, "990001", extra=-300)
    win = MainWindow(world.conn, world.accounting)
    page = win.payments
    win.stack.setCurrentWidget(page)
    page._set_tab(page.TO_PAY) if hasattr(page, "_set_tab") else None
    page.refresh()
    assert "We still owe" in page.breakdown.text() and "300.00" in page.breakdown.text()
    win.close()


# ---- Migration.

def test_migration_22_adds_its_tables_with_a_backup(env):
    # env points the data folder, and so the backup, at a temporary folder.
    tmp_path = env
    path = tmp_path / "old.sqlite"
    conn = db.connect(path)
    db.initialize(conn)
    conn.executescript("DROP TABLE count_reopens; DROP TABLE outside_receipts; PRAGMA user_version = 21;")
    conn.close()
    conn = db.connect(path)
    db.initialize(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION  # and on through the later ones
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name IN ('count_reopens', 'outside_receipts')").fetchone()[0] == 2
    assert list((tmp_path / "backups").glob("*"))
    conn.close()
