"""Fixes from a whole-system review, and the decisions that followed."""
from __future__ import annotations

import sqlite3

import pytest

from atlaspo import auth, claim_stubs, debit_memos, order_forms, orders, payments, pdf, receiving
from atlaspo.db import POError, PermissionDenied

from conftest import NORTH, PASSWORD, SOUTH
from test_payments import Order


@pytest.fixture(autouse=True)
def accounting(world):
    world.accounting = auth.create_user(world.conn, world.admin, "lorna", "Lorna Tan", PASSWORD, ["ACCOUNTING"], must_change=False)


def test_a_rejected_part_of_a_split_order_waits_for_approval_again(world):
    """R1. 180,000 split into departments: each part waits, and a rejected one can't come back approved on its own smaller amount."""
    draft = world.draft(items=[world.item(dept=1, cost=6000000), world.item(dept=2, cost=6000000), world.item(dept=3, cost=6000000)])
    parts = order_forms.submit_order(world.conn, world.buyer, draft)
    assert [s for _, s in parts] == [orders.PENDING] * 3
    order_forms.reject(world.conn, world.admin, parts[0][0], "Wrong colour")
    assert order_forms.submit(world.conn, world.buyer, parts[0][0]) == orders.PENDING


def test_an_amended_partly_delivered_po_goes_back_to_the_supplier(world):
    """R3. The PO keeps its delivery status, says it hasn't gone out, and can be issued again."""
    o = Order(world, av=8)
    po_id = o.pos[NORTH]["id"]
    po = orders.get_po(world.conn, po_id)
    assert po["status"] == orders.PARTIAL
    orders.amend(world.conn, world.buyer, po_id, {}, [{**i, "qty": "9", "qty_m": 9000} for i in po["items"]], "Supplier can only send 9")
    po = orders.get_po(world.conn, po_id)
    assert orders.unsent(po) and orders.to_issue(po) and po["status"] == orders.PARTIAL
    assert order_forms.search(world.conn, tab=orders.OPEN)[0]["id"] == o.of_id  # back on Print
    orders.issue(world.conn, world.buyer, po_id)
    po = orders.get_po(world.conn, po_id)
    assert not orders.unsent(po) and po["status"] == orders.PARTIAL and po["issued_revision"] == po["revision"]


def test_the_warehouse_copy_has_one_original_per_revision(world, tmp_path):
    """R2. A reprint of the same revision is a copy. A PO amendment raises the OF's revision, so the new figures print as a new original."""
    of_id = world.approved(items=[world.item(cost=10000, alloc={NORTH: 10})])
    assert pdf.print_order_form(world.conn, world.buyer, of_id)[0]
    assert pdf.warehouse_printed(world.conn, of_id)
    po = orders.get_po(world.conn, order_forms.get_of(world.conn, of_id)["pos"][0]["id"])
    orders.amend(world.conn, world.buyer, po["id"], {}, [{**i, "qty": "8", "qty_m": 8000} for i in po["items"]], "Less")
    assert order_forms.get_of(world.conn, of_id)["revision"] == 1 and not pdf.warehouse_printed(world.conn, of_id)
    assert pdf.print_order_form(world.conn, world.buyer, of_id)[0]
    assert pdf.warehouse_printed(world.conn, of_id)


def test_changing_only_internal_notes_keeps_the_suppliers_copy(world):
    po_id = world.po(items=[world.item(cost=10000, alloc={NORTH: 10})])
    orders.issue(world.conn, world.buyer, po_id)
    po = orders.get_po(world.conn, po_id)
    result = orders.amend(world.conn, world.buyer, po_id, {"internal_notes": "Call before delivery"}, po["items"], "Note for us")
    po = orders.get_po(world.conn, po_id)
    assert result["amendment_id"] is None and po["revision"] == 0 and po["status"] == orders.ISSUED and po["internal_notes"] == "Call before delivery"


def test_a_pending_amendment_blocks_a_close_and_cant_be_applied_after(world):
    o = Order(world, av=8)
    po_id = o.pos[NORTH]["id"]
    po = orders.get_po(world.conn, po_id)
    result = orders.amend(world.conn, world.buyer, po_id, {}, [{**i, "qty": "12", "qty_m": 12000} for i in po["items"]], "More")
    assert result["status"] == orders.PENDING, result
    with pytest.raises(POError, match="waiting for approval"):
        orders.close(world.conn, world.buyer, po_id, "Done")
    assert not orders.can_close(world.conn, po_id)


def test_a_shortage_is_charged_after_the_overall_discount(world):
    """The owner: a discounted item comes back at the discounted price."""
    of_id = world.approved(items=[world.item(cost=10000, alloc={NORTH: 10}, description="Socks", item_code="SOCK-1")], overall_discount_type="PERCENT", overall_discount_value=3000)
    po = order_forms.generate_pos(world.conn, world.buyer, of_id)[0]
    orders.issue(world.conn, world.buyer, po["id"])
    item = orders.get_po(world.conn, po["id"])["items"][0]["id"]
    for n in (1, 2):
        who = auth.create_user(world.conn, world.admin, f"r{n}", f"R {n}", PASSWORD, ["RECEIVER"], must_change=False, branch_code=NORTH)
        receiving.save_count(world.conn, who, po["id"], {item: "5"})
    line = debit_memos.outstanding(world.conn, world.buyer, po["id"])[0]
    assert line["amount_c"] == 35000  # 5 pieces at 100.00 less 30%
    money = payments.settlement(world.conn, of_id)
    assert money["pay_c"] == 35000  # what North's 5 pieces came to after the discount


def test_credit_that_covers_an_order_settles_it_without_a_check(world):
    """R5. An overpayment on one check covers the next order, which settles at nothing to pay, signed off by management."""
    first = Order(world)
    group = next(g for g in payments.checks_due(world.conn) if first.of_id in [o["id"] for o in g["orders"]])
    payments.record_check(world.conn, world.accounting, [first.of_id], "100001", group["due"].isoformat(), f"{group['check_c'] / 100 + 5000:.2f}", "Typed high by mistake")
    second = Order(world)
    group = next(g for g in payments.checks_due(world.conn) if second.of_id in [o["id"] for o in g["orders"]])
    assert group["check_c"] == 0 and group["credit_c"] == 200000
    with pytest.raises(POError, match="Settle by credit"):
        payments.record_check(world.conn, world.accounting, [second.of_id], "100002", group["due"].isoformat(), "0")
    check_id = payments.settle_by_credit(world.conn, world.accounting, [second.of_id])
    check = next(c for c in payments.checks(world.conn, payments.ACTIVE) if c["id"] == check_id)
    assert check["bucket"] == payments.CLEARED and check["waiting_signoff"] and check["check_number"].startswith("CREDIT-")
    assert payments.order_row(world.conn, second.of_id)["stage"] == payments.PAID
    assert payments.waiting_signoffs(world.conn) == 2


def test_the_terms_start_when_the_goods_reach_the_central_warehouse(world):
    """The terms run from the delivery to the warehouse, not from the branch's count days later."""
    from atlaspo import warehouse
    o = Order(world)
    for p in o.pos.values():
        world.conn.execute("INSERT INTO warehouse_deliveries(po_id, delivery_no, boxes, received_by, received_at, shipped_by, shipped_at) VALUES (?,1,1,?,?,?,?)",
                           (p["id"], world.warehouse.id, "2026-10-01 09:00:00" if p["branch_code"] == NORTH else "2026-10-05 09:00:00", world.warehouse.id, "2026-10-06 15:00:00"))
    row = o.row()
    # 30 days from the later delivery, October 5, is November 4, a Wednesday, so the check falls on Saturday, November 7.
    assert (row["latest_dr"], row["due"].isoformat()) == ("2026-10-05", "2026-11-07")
    assert payments.central_day(world.conn, o.pos[NORTH]["id"]) == "2026-10-01"


def test_nobody_signs_off_their_own_figures(world):
    both = auth.create_user(world.conn, world.admin, "acap", "Acct And Approver", PASSWORD, ["ACCOUNTING", "APPROVER"], must_change=False)
    o = Order(world)
    group = payments.checks_due(world.conn)[0]
    check_id = payments.record_check(world.conn, both, [o.of_id], "200001", group["due"].isoformat(), f"{group['check_c'] / 100 - 5:.2f}", "Bank charge")
    assert payments.check_waiting(world.conn, check_id)
    with pytest.raises(POError, match="Someone else"):
        payments.sign_off_check(world.conn, both, check_id)
    assert world.conn.execute("SELECT 1 FROM audit_logs WHERE action = 'PAYMENT_SIGNOFF_ASKED'").fetchone()


def test_goods_coming_back_are_counted_before_anyone_collects_them(world):
    o = Order(world, damaged="3")
    stub = o.stub()
    lines = claim_stubs.items(world.conn, world.buyer, stub["id"])
    with pytest.raises(POError, match="came back"):
        claim_stubs.arrived(world.conn, world.warehouse, stub["id"], {})
    claim_stubs.arrived(world.conn, world.warehouse, stub["id"], {lines[0]["id"]: "2"})
    assert claim_stubs.arrival_counts(world.conn, "CS", [lines[0]["id"]]) == {lines[0]["id"]: 2000}
    note = world.conn.execute("SELECT summary FROM audit_logs WHERE action = 'CS_COUNTED'").fetchone()[0]
    assert "2 came, 3 on the paper" in note
    # Picked up only once the supplier has the check that took the stub off.
    with pytest.raises(POError, match="received the check"):
        claim_stubs.collected(world.conn, world.buyer, stub["id"], "Juan")
    payments.drs_match(world.conn, world.accounting, o.of_id)
    group = next(g for g in payments.checks_due(world.conn) if o.of_id in [x["id"] for x in g["orders"]])
    check = payments.record_check(world.conn, world.accounting, [o.of_id], "300001", group["due"].isoformat(), f"{group['check_c'] / 100:.2f}")
    payments.claim_check(world.conn, world.accounting, check)
    claim_stubs.collected(world.conn, world.buyer, stub["id"], "Juan")


def test_job_titles_and_the_warehouse_login(world):
    assert not auth.warehouse_login_missing(world.conn)
    world.conn.execute("UPDATE users SET active = 0 WHERE id = ?", (world.warehouse.id,))
    assert auth.warehouse_login_missing(world.conn)
    head = auth.create_user(world.conn, world.admin, "whhead", "Wendy Head", PASSWORD, ["WAREHOUSE"], must_change=False, title="Warehouse Head")
    assert head.title == "Warehouse Head" and auth.job_line(head) == "Warehouse Head" and not auth.warehouse_login_missing(world.conn)
    with pytest.raises(POError, match="warehouse login"):
        auth.create_user(world.conn, world.admin, "mix", "Mixed", PASSWORD, ["WAREHOUSE", "BUYER"], must_change=False)
    # Moving a receiver records the branch they left.
    rec = auth.create_user(world.conn, world.admin, "rr", "Rita R", PASSWORD, ["RECEIVER"], must_change=False, branch_code=NORTH)
    auth.update_user(world.conn, world.admin, rec.id, "Rita R", ["RECEIVER"], True, SOUTH)
    previous = world.conn.execute("SELECT previous_value FROM audit_logs WHERE action = 'USER_UPDATE' ORDER BY id DESC").fetchone()[0]
    assert f'"branch_code": {NORTH}' in previous


def test_replace_cant_swap_out_what_stays_on_record(world):
    world.po()
    with pytest.raises(sqlite3.IntegrityError):
        world.conn.execute("REPLACE INTO audit_logs(id, occurred_at, user_name, action, entity_type, summary) SELECT id, occurred_at, user_name, action, entity_type, 'changed' FROM audit_logs LIMIT 1")


def test_a_shortage_waits_for_its_debit_memo_before_payment_and_prints_with_the_final_of(world):
    o = Order(world, av=8)
    orders.close(world.conn, world.buyer, o.pos[NORTH]["id"], "The rest isn't coming")
    row = payments.order_row(world.conn, o.of_id)
    assert row["stage"] == payments.DM_FIRST
    with pytest.raises(POError, match="no debit memo"):
        payments.record_check(world.conn, world.accounting, [o.of_id], "400001", "2026-11-07", "1800.00")
    memo = o.short_memo()
    assert payments.order_row(world.conn, o.of_id)["stage"] != payments.DM_FIRST
    from pypdf import PdfReader
    from io import BytesIO
    final = PdfReader(BytesIO(pdf.office_copy(world.conn, world.accounting, o.of_id)))
    assert memo["dm_number"] in " ".join(page.extract_text() for page in final.pages)


def test_damage_found_later(world):
    """Damage seen after the check goes on a claim stub, typed by the purchaser, and comes off the supplier's next check."""
    o = Order(world)
    payments.drs_match(world.conn, world.accounting, o.of_id) if payments.order_row(world.conn, o.of_id)["stage"] == payments.ENTER_DRS else None
    group = next(g for g in payments.checks_due(world.conn) if o.of_id in [x["id"] for x in g["orders"]])
    check = payments.record_check(world.conn, world.accounting, [o.of_id], "500001", group["due"].isoformat(), f"{group['check_c'] / 100:.2f}")
    payments.claim_check(world.conn, world.accounting, check)
    lines = claim_stubs.late_lines(world.conn, world.buyer, o.pos[NORTH]["po_number"])
    assert [(l["branch_name"], l["room_m"]) for l in lines] == [("North", 10000)]
    with pytest.raises(POError, match="how the damage was found"):
        claim_stubs.issue_late(world.conn, world.buyer, o.pos[NORTH]["po_number"], {lines[0]["po_item_id"]: "2"}, " ")
    with pytest.raises(POError, match="only 10"):
        claim_stubs.issue_late(world.conn, world.buyer, o.pos[NORTH]["po_number"], {lines[0]["po_item_id"]: "11"}, "Torn")
    made = claim_stubs.issue_late(world.conn, world.buyer, o.pos[NORTH]["po_number"], {lines[0]["po_item_id"]: "2"}, "Found torn when unpacked")
    assert made[0]["total_c"] == 20000 and claim_stubs.is_late(world.conn, made[0]["id"])
    # The order still counts as settled, and the supplier now owes us what the stub takes off: it comes off the next check.
    assert payments.supplier_balance(world.conn, world.supplier) == 20000
    assert [c["cs_number"] for c in payments.late_claims(world.conn, world.supplier)] == [made[0]["cs_number"]]
    # The goods wait at the warehouse for that next check before the supplier can pick them up.
    claim_stubs.arrived(world.conn, world.warehouse, made[0]["id"], claim_stubs.as_on_paper(world.conn, made[0]["id"]))
    with pytest.raises(POError, match="received the check"):
        claim_stubs.collected(world.conn, world.buyer, made[0]["id"], "Juan")


def test_claim_stub_and_memo_pages_find_by_date_and_have_an_all_tab_and_a_late_damage_panel(world):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from atlaspo.ui.main_window import MainWindow
    from atlaspo.ui import common
    o = Order(world, av=8, damaged="2")
    o.stub()
    w = MainWindow(world.conn, world.buyer)
    page = w.claim_stubs
    all_tab = list(page.labels).index("ALL")
    page.tabs.setCurrentIndex(all_tab)
    assert page.tabs.tabText(all_tab) == "All  1" and page.table.rowCount() == 1
    common.set_date(page.date_from, "2099-01-01")
    assert page.table.rowCount() == 0 or not page.table.isVisibleTo(page)
    page.clear_filters()
    assert page.table.rowCount() == 1 and not common.date_value(page.date_from)
    page._late()
    assert page.late.isVisibleTo(page)
    # Part of the number is offered in full; picking it loads the products, and the amount follows the pieces typed.
    assert o.pos[SOUTH]["po_number"] in page.late.completer.model().stringList()
    page.late.find(o.pos[SOUTH]["po_number"])
    assert page.late.table.rowCount() == 1
    page.late.table.item(0, page.late.C_PIECES).setText("2")
    assert page.late.table.item(0, page.late.C_AMOUNT).text() == "200.00" and "200.00" in page.late.total.text()
    page.late.narrow.setText("nothing like it")
    assert page.late.table.isRowHidden(0)
    page.late.closed.emit()
    assert not page.late.isVisibleTo(page)
    memos = w.debit_memos
    assert any(memos.tabs.tabText(i).startswith("All") for i in range(memos.tabs.count()))
    w.close()


def test_the_branch_prints_a_pass_for_damaged_goods_on_a_claim_stub(world, monkeypatch):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from pypdf import PdfReader
    from io import BytesIO
    from atlaspo.ui import receiving_page
    from atlaspo.ui.main_window import MainWindow
    o = Order(world, damaged="3")
    stub = o.stub()
    who = auth.get_user(world.conn, world.conn.execute("SELECT id FROM users WHERE username = ?", (f"r{NORTH}x1",)).fetchone()[0])
    rows = claim_stubs.passes_for_branch(world.conn, who)
    assert [r["cs_number"] for r in rows] == [stub["cs_number"]] and rows[0]["printed_at"] is None
    first = PdfReader(BytesIO(claim_stubs.print_pass(world.conn, who, stub["id"])))
    text = " ".join(p.extract_text() for p in first.pages)
    assert "DAMAGED GOODS PASS" in text and stub["cs_number"] in text and "ORIGINAL" in text and "DUPLICATE" in text and "₱" not in text
    again = " ".join(p.extract_text() for p in PdfReader(BytesIO(claim_stubs.print_pass(world.conn, who, stub["id"]))).pages)
    assert "PRINTED AGAIN" in again
    other = auth.get_user(world.conn, world.conn.execute("SELECT id FROM users WHERE username = ?", (f"r{SOUTH}x1",)).fetchone()[0])
    with pytest.raises(POError, match="isn't for your branch"):
        claim_stubs.print_pass(world.conn, other, stub["id"])
    # On the receiver's screen, under Item passes.
    monkeypatch.setattr(receiving_page, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(receiving_page, "open_for_printing", lambda docs, name: None)
    w = MainWindow(world.conn, who)
    page = w.receiving
    page.tabs.setCurrentIndex(2)
    assert page.cs_waiting.rowCount() == 1 and page.print_cs_btn.isVisibleTo(page)
    page.print_cs_pass()
    w.close()


def test_a_branch_reports_damage_and_accounting_or_management_approves_it(world, monkeypatch):
    """One receiver reports damage found after the count; Accounting or management approves or rejects it; then the branch prints the pass."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from atlaspo.ui import claim_stub_page, receiving_page
    from atlaspo.ui.main_window import MainWindow
    o = Order(world)
    who = auth.get_user(world.conn, world.conn.execute("SELECT id FROM users WHERE username = ?", (f"r{NORTH}x1",)).fetchone()[0])
    number = o.pos[NORTH]["po_number"]
    assert number in claim_stubs.report_numbers(world.conn, who) and o.pos[SOUTH]["po_number"] not in claim_stubs.report_numbers(world.conn, who)
    lines = claim_stubs.report_lines(world.conn, who, number)
    assert "net_unit_c" not in lines[0] and "cost_c" not in lines[0]  # receivers never see costs
    with pytest.raises(POError, match="only 10"):
        claim_stubs.send_report(world.conn, who, number, {lines[0]["po_item_id"]: "11"}, "Torn")
    # On the receiver's screen.
    w = MainWindow(world.conn, who)
    page = w.receiving
    page.tabs.setCurrentIndex(3)
    page.find_damage(number)
    page.damage_grid.item(0, 5).setText("3")
    page.damage_reason.setText("Torn seams found when unpacking")
    page.send_damage()
    assert page.damage_sent.item(0, 4).text() == "Waiting for approval"
    w.close()
    # Its pieces wait, so nobody can report or charge them twice.
    assert claim_stubs.report_lines(world.conn, who, number)[0]["room_m"] == 7000
    report = claim_stubs.reports(world.conn, world.accounting)[0]
    assert report["amount_c"] == 30000 and report["status"] == claim_stubs.PENDING
    with pytest.raises(PermissionDenied):
        claim_stubs.approve_report(world.conn, world.buyer, report["id"])
    # Accounting approves it from Claim stubs, Branch reports.
    monkeypatch.setattr(claim_stub_page, "confirm", lambda *a, **k: True)
    office = MainWindow(world.conn, world.accounting)
    assert "(1)" in office.nav.item(office.claims_row).text()
    stubs = office.claim_stubs
    stubs.tabs.setCurrentIndex(list(stubs.labels).index(claim_stub_page.REPORTS))
    assert stubs.reports_view.isVisibleTo(stubs) and stubs.approve_report_btn.isVisibleTo(stubs)
    stubs.approve_report()
    made = claim_stubs.reports(world.conn, world.accounting)[0]
    assert made["status"] == claim_stubs.APPROVED and made["cs_number"]
    office.close()
    # The branch now prints the pass for it.
    assert [r["cs_number"] for r in claim_stubs.passes_for_branch(world.conn, who)] == [made["cs_number"]]
    # A second report is rejected, and the branch sees why.
    claim_stubs.send_report(world.conn, who, number, {lines[0]["po_item_id"]: "1"}, "Stained")
    second = claim_stubs.reports(world.conn, world.approver if hasattr(world, "approver") else world.admin, claim_stubs.PENDING)[0]
    claim_stubs.reject_report(world.conn, world.admin, second["id"], "That's the display sample, not damage")
    mine = claim_stubs.reports(world.conn, who)
    assert mine[0]["status"] == claim_stubs.REJECTED and "display sample" in mine[0]["decision_reason"] and "amount_c" not in mine[0]
