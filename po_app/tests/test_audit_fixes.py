"""The loopholes found in an audit stay closed. """
from __future__ import annotations

import sqlite3

import pytest

from atlaspo import auth, backup, db, debit_memos, item_passes, masters, order_forms, orders, payments, pdf, receiving
from atlaspo.db import POError, PermissionDenied

from conftest import NORTH, PASSWORD, SOUTH
from test_payments import Order


@pytest.fixture(autouse=True)
def accounting(world):
    world.accounting = auth.create_user(world.conn, world.admin, "lorna", "Lorna Tan", PASSWORD, ["ACCOUNTING"], must_change=False)


class HalfCounted(Order):
    """The same order, with only North counted. South's PO is issued and nothing has come."""

    def __init__(self, world):
        self.w = world
        self.of_id = world.approved(items=[world.item(cost=10000, alloc={NORTH: 10, SOUTH: 10}, description="Socks", item_code="SOCK-1")])
        self.pos = {p["branch_code"]: p for p in order_forms.generate_pos(world.conn, world.buyer, self.of_id)}
        for p in self.pos.values():
            orders.issue(world.conn, world.buyer, p["id"])
        self.count(NORTH, 10)


def _receiver(world, branch, n=1):
    return auth.get_user(world.conn, world.conn.execute("SELECT id FROM users WHERE username = ?", (f"r{branch}x{n}",)).fetchone()["id"])


# ---- F1: closing a PO nobody counted no longer pays it.

def test_f1_a_po_nobody_counted_cant_be_closed_and_paid(world):
    o = HalfCounted(world)
    with pytest.raises(POError, match="cancel it instead"):
        orders.close(world.conn, world.buyer, o.pos[SOUTH]["id"], "Supplier can't deliver")
    # Cancelling is the way, and then the order pays only what came.
    orders.cancel(world.conn, world.buyer, o.pos[SOUTH]["id"], "Supplier can't deliver")
    assert o.row()["pay_c"] == 100000


def test_f1_a_po_cant_close_while_a_count_is_open(world):
    o = Order(world, av=8)
    north = o.pos[NORTH]["id"]
    item = orders.get_po(world.conn, north)["items"][0]["id"]
    receiving.save_count(world.conn, _receiver(world, NORTH), north, {item: "2"})  # the rest arrives, and only one person has counted it
    assert receiving.progress(world.conn, north)
    with pytest.raises(POError, match="still being counted"):
        orders.close(world.conn, world.buyer, north, "Done")


def test_f1_a_po_from_before_receiving_still_closes_and_counts_as_delivered(world, monkeypatch):
    monkeypatch.setattr(receiving, "live_since", lambda conn: "9999")
    o = HalfCounted(world)
    orders.close(world.conn, world.buyer, o.pos[SOUTH]["id"], "Received the old way")
    assert o.row()["pay_c"] == 200000


# ---- F2: no check for an order that isn't finished.

def test_f2_no_dr_or_check_for_an_unfinished_order(world):
    o = HalfCounted(world)
    assert o.row()["stage"] == payments.NOT_DONE
    with pytest.raises(POError, match="fully delivered"):
        o.enter()
    with pytest.raises(POError, match="fully delivered"):
        payments.record_check(world.conn, world.accounting, [o.of_id], "990001", "2026-11-14", "2000.00")


# ---- F3: a failed commit doesn't leave every later save unsaved.

def test_f3_a_save_that_fails_to_commit_rolls_back_and_the_next_save_is_written(world):
    world.conn.execute("PRAGMA busy_timeout = 100")
    other = sqlite3.connect(db.db_path(), timeout=0.1)
    other.execute("BEGIN")
    other.execute("SELECT COUNT(*) FROM suppliers").fetchone()  # holds a read lock, as a backup or another PC would
    with pytest.raises(sqlite3.OperationalError):
        masters.save_supplier(world.conn, world.admin, {"code": "LCK", "name": "Locked Out Ltd"})
    assert not world.conn.in_transaction
    other.rollback()
    masters.save_supplier(world.conn, world.admin, {"code": "OK1", "name": "Saved Afterwards Ltd"})
    assert other.execute("SELECT 1 FROM suppliers WHERE code = 'OK1'").fetchone()
    other.close()


# ---- F4: charges follow agreed counts on live POs, and a PO under a charge stays as it is.

def test_f4_no_debit_memo_on_a_po_nobody_counted(world):
    o = HalfCounted(world)
    assert not debit_memos.can_issue(world.conn, o.pos[SOUTH]["id"])
    with pytest.raises(POError, match="agreed at the branch"):
        debit_memos.issue(world.conn, world.buyer, o.pos[SOUTH]["id"], {})


def test_f4_a_counted_po_cant_be_cancelled_and_a_charged_line_cant_be_lowered(world):
    o = Order(world, av=0)  # both receivers agreed nothing came, so the PO is still Issued
    o.short_memo()
    north = o.pos[NORTH]["id"]
    with pytest.raises(POError, match="counted at the branch"):
        orders.cancel(world.conn, world.buyer, north, "Not needed")
    po = orders.get_po(world.conn, north)
    items = [{**{k: po["items"][0][k] for k in (*orders.ITEM_FIELDS, "id")}, "qty_m": 9000}]
    with pytest.raises(POError, match="debit memo or claim stub"):
        orders.amend(world.conn, world.buyer, north, {}, items, "Fewer")


# ---- F5: a closed PO still needs its damage on a claim stub.

def test_f5_closing_doesnt_skip_damage(world):
    o = Order(world, av=10, damaged=4)
    orders.close(world.conn, world.buyer, o.pos[NORTH]["id"], "Nothing more coming")
    assert not receiving.of_delivery_complete(world.conn, o.of_id)
    o.stub()
    assert receiving.of_delivery_complete(world.conn, o.of_id)


# ---- F7: each PO's DR is compared on its own.

def test_f7_a_dr_over_at_one_branch_cant_hide_one_under_at_another(world):
    o = Order(world)
    row = o.row()
    by_po = {p["po_id"]: p["delivered_c"] for p in row["pos"]}
    receipts = payments.receipts_to_enter(world.conn, world.accounting, o.of_id)
    off = {o.pos[NORTH]["id"]: 50000, o.pos[SOUTH]["id"]: -50000}
    payments.save_dr_entries(world.conn, world.accounting, o.of_id, {d["id"]: ("2026-10-28", f"{(by_po[d['po_id']] + off[d['po_id']]) / 100:.2f}") for d in receipts})
    row = o.row()
    assert row["difference_c"] == 0 and row["stage"] == payments.DIFFERENCE
    payments.accept_difference(world.conn, world.accounting, o.of_id, "Supplier swapped two cartons between branches")
    assert o.row()["stage"] == payments.SIGN_OFF
    payments.sign_off_difference(world.conn, world.admin, o.of_id)
    assert o.row()["stage"] == payments.TO_PAY


# ---- F8: permanent records stay permanent, even edited directly in the file.

def test_f8_the_file_itself_refuses_to_revive_or_rewrite_records(world):
    o = Order(world, av=8)
    memo = o.short_memo()
    debit_memos.cancel(world.conn, world.admin, memo["id"], "Wrong")
    with pytest.raises(sqlite3.Error, match="stays cancelled"):
        world.conn.execute("UPDATE debit_memos SET status = 'ISSUED' WHERE id = ?", (memo["id"],))
    o.short_memo()
    o.enter()
    check = payments.record_check(world.conn, world.accounting, [o.of_id], "990001", "2026-11-14", f"{o.row()['pay_c'] / 100:.2f}")
    for sql in ("DELETE FROM payment_check_ofs", "UPDATE payment_check_ofs SET amount_c = 1", f"UPDATE payment_checks SET check_date = '2026-01-01' WHERE id = {check}",
                f"UPDATE payment_checks SET credit_applied_c = 5 WHERE id = {check}", "UPDATE branch_counts SET status = 'WAITING' WHERE status = 'APPROVED'"):
        with pytest.raises(sqlite3.Error):
            world.conn.execute(sql)


# ---- F9: clearing a typed DR leaves a trace.

def test_f9_clearing_a_typed_dr_is_logged_with_what_it_was(world):
    o = Order(world)
    o.enter(off_by="5")
    dr_id = payments.receipts_to_enter(world.conn, world.accounting, o.of_id)[0]["id"]
    payments.save_dr_entries(world.conn, world.accounting, o.of_id, {dr_id: (None, "")})
    row = world.conn.execute("SELECT previous_value FROM audit_logs WHERE action = 'DR_ENTRY_CLEARED'").fetchone()
    assert row and '"total_c"' in row[0]


# ---- F13: a check needs a real date and an amount.

def test_f13_a_check_needs_a_date_and_an_amount_above_nothing(world):
    o = Order(world)
    with pytest.raises(POError, match="isn't a date"):
        payments.record_check(world.conn, world.accounting, [o.of_id], "990001", "12/05/2026", "2000.00")
    with pytest.raises(POError, match="more than nothing"):
        payments.record_check(world.conn, world.accounting, [o.of_id], "990001", "2026-12-05", "-5")


# ---- F17, F18, F19: approval can't be got around.

def test_f17_only_an_administrator_changes_settings(world):
    with pytest.raises(PermissionDenied):
        db.set_settings(world.conn, world.buyer, {"approval.threshold_c": "99999999999"})
    with pytest.raises(POError, match="active user with the Approver"):
        db.set_settings(world.conn, world.admin, {"approval.approver_user_ids": str(world.buyer.id)})


def test_f18_an_approver_who_rewrote_a_draft_cant_approve_it(world):
    of_id = world.draft(200000)
    order_forms.update_draft(world.conn, world.admin, of_id, world.header(), [world.item(cost=36000000)])
    assert order_forms.submit(world.conn, world.buyer, of_id) == orders.PENDING
    with pytest.raises(POError, match="worked on this OF"):
        order_forms.approve(world.conn, world.admin, of_id)


def test_f19_a_dearer_amendment_needs_approval_even_when_a_quantity_drops(world):
    items = [world.item(cost=10000, alloc={NORTH: 10}, description="Shirt"), world.item(cost=10000, alloc={NORTH: 10}, description="Belt")]
    of_id = world.approved(items=items)
    dearer = [world.item(cost=18000, alloc={NORTH: 10}, description="Shirt"), world.item(cost=10000, alloc={NORTH: 1}, description="Belt")]
    assert order_forms.amend(world.conn, world.buyer, of_id, {}, dearer, "New price")["status"] == orders.PENDING


# ---- F25, F29: users.

def test_f25_a_receiver_has_no_other_role(world):
    with pytest.raises(POError, match="can't have another role"):
        auth.create_user(world.conn, world.admin, "mix", "Mixed Up", PASSWORD, ["RECEIVER", "VIEWER"], must_change=False, branch_code=NORTH)


def test_f29_resetting_a_missing_users_password_fails(world):
    with pytest.raises(POError, match="no longer exists"):
        auth.reset_password(world.conn, world.admin, 999999, PASSWORD)


# ---- F27: the first Final OF needs Accounting or a Purchaser.

def test_f27_a_viewer_cant_print_the_first_final_of_but_can_reprint(world):
    o = Order(world)
    viewer = auth.create_user(world.conn, world.admin, "vic", "Vic Viewer", PASSWORD, ["VIEWER"], must_change=False)
    with pytest.raises(PermissionDenied):
        pdf.office_copy(world.conn, viewer, o.of_id)
    assert pdf.office_copy(world.conn, world.admin, o.of_id)
    assert pdf.office_copy(world.conn, viewer, o.of_id)


# ---- F33: a pass that didn't print can be printed again, under a new number.

def test_f33_a_pass_that_didnt_print_is_cancelled_and_printed_again(world):
    o = Order(world, av=12)
    receiver = _receiver(world, NORTH)
    made = item_passes.issue(world.conn, receiver, o.pos[NORTH]["id"])
    other = _receiver(world, NORTH, 2)
    with pytest.raises(POError, match="home office"):
        item_passes.print_failed(world.conn, other, made["id"], "Paper jam")
    item_passes.print_failed(world.conn, receiver, made["id"], "Paper jam")
    assert world.conn.execute("SELECT status, cancel_reason FROM item_passes WHERE id = ?", (made["id"],)).fetchone()[:] == ("CANCELLED", "Didn't print: Paper jam")
    again = item_passes.issue(world.conn, receiver, o.pos[NORTH]["id"])
    assert again["ip_number"] != made["ip_number"]


# ---- F50: practice data can't replace real records.

def test_f50_a_sample_backup_cant_be_restored_over_real_data(world, tmp_path):
    copy = tmp_path / "sample-copy.sqlite"
    target = sqlite3.connect(copy)
    world.conn.backup(target)
    target.execute("INSERT OR REPLACE INTO system_settings(key, value, updated_at) VALUES ('sample.data', '1', '2026-11-06')")
    target.commit()
    target.close()
    with pytest.raises(POError, match="practice data"):
        backup.restore(world.conn, world.admin, copy, db.db_path())
