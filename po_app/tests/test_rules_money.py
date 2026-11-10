"""Money rules: a lower check signed off is final (Q1), a lower DR is what is paid with no sign-off (Q2),
a debit memo cancelled after payment is paid back on the next check (Q4), and recording a check keeps the write lock short (AUD-146).

Every order here is test_payments.Order: socks at P100.00 a piece, 10 to North and 10 to South, so P2,000.00 when all came,
with no stickers and no term discount."""
from __future__ import annotations

import os
import sqlite3
from io import BytesIO

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from pypdf import PdfReader
from PySide6.QtWidgets import QApplication

from atlaspo import auth, debit_memos, payments, pdf
from atlaspo.db import POError

from conftest import NORTH, PASSWORD, SOUTH
from test_payments import Order


@pytest.fixture(autouse=True)
def people(world):
    world.accounting = auth.create_user(world.conn, world.admin, "lorna", "Lorna Tan", PASSWORD, ["ACCOUNTING"], must_change=False)
    world.approver = auth.create_user(world.conn, world.admin, "ramon", "Ramon Villanueva", PASSWORD, ["APPROVER"], must_change=False)


@pytest.fixture
def app():
    yield QApplication.instance() or QApplication([])


def _group(world, o):
    return next(g for g in payments.checks_due(world.conn) if any(x["id"] == o.of_id for x in g["orders"]))


def _record(world, o, number, pesos, reason="", user=None):
    group = _group(world, o)
    return payments.record_check(world.conn, user or world.accounting, [x["id"] for x in group["orders"]], number, group["due"].isoformat(), pesos, reason)


def _balance(world):
    return payments.supplier_balance(world.conn, world.supplier)


# ---------------------------------------------------------------- Q1: a lower check, with a reason and signed off, is final

def test_a_lower_check_signed_off_is_final_and_never_owed(world):
    o = Order(world)
    o.enter()
    assert _group(world, o)["check_c"] == 200000  # P2,000.00
    check = _record(world, o, "880001", "1,900.00", "Supplier agreed to take P100 off for the late delivery")
    assert world.conn.execute("SELECT expected_c FROM payment_checks WHERE id = ?", (check,)).fetchone()[0] == 200000
    # Waiting for the sign-off: today's rule, we still owe the P100.00.
    assert payments.agreed_deduction(world.conn, check) == 0
    assert _balance(world) == -10000
    payments.sign_off_check(world.conn, world.approver, check)
    # Signed off: the P100.00 is an agreed deduction, so nothing is owed and nothing goes on the next check.
    assert payments.agreed_deduction(world.conn, check) == 10000
    assert _balance(world) == 0
    assert not payments.owed_without_orders(world.conn)
    second = Order(world)
    second.enter()
    group = _group(world, second)
    assert (group["amount_c"], group["owed_c"], group["credit_c"], group["check_c"]) == (200000, 0, 0, 200000)


def test_cancelling_a_signed_off_lower_check_drops_its_deduction(world):
    o = Order(world)
    o.enter()
    check = _record(world, o, "880001", "1,900.00", "Agreed P100 off")
    payments.sign_off_check(world.conn, world.approver, check)
    payments.cancel_check(world.conn, world.accounting, check, "Printed on the wrong account")
    assert payments.agreed_deduction(world.conn, check) == 0
    assert _balance(world) == 0
    # The order is back on the list for the whole P2,000.00.
    assert _group(world, o)["check_c"] == 200000


def test_a_higher_check_is_still_a_credit_off_the_next_check(world):
    o = Order(world)
    o.enter()
    check = _record(world, o, "880001", "2,150.00", "Typed high on the check")
    payments.sign_off_check(world.conn, world.approver, check)
    assert payments.agreed_deduction(world.conn, check) == 0
    assert _balance(world) == 15000
    second = Order(world)
    second.enter()
    group = _group(world, second)
    assert (group["credit_c"], group["owed_c"], group["check_c"]) == (15000, 0, 185000)  # P2,000.00 less P150.00


def test_admins_own_lower_check_is_signed_when_recorded_so_it_is_final(world):
    o = Order(world)
    o.enter()
    check = _record(world, o, "880001", "1,950.50", "Agreed with the supplier", user=world.admin)
    assert not payments.check_waiting(world.conn, check)
    assert payments.agreed_deduction(world.conn, check) == 4950  # P2,000.00 - P1,950.50
    assert _balance(world) == 0


def test_an_unsigned_lower_check_carries_what_we_owe_to_the_next_check(world):
    o = Order(world)
    o.enter()
    _record(world, o, "880001", "1,700.00", "Check typo")
    second = Order(world)
    second.enter()
    group = _group(world, second)
    assert (group["owed_c"], group["check_c"]) == (30000, 230000)  # P2,000.00 + P300.00


def test_a_check_recorded_before_the_upgrade_has_no_agreed_deduction(world):
    o = Order(world)
    o.enter()
    check = _record(world, o, "880001", "1,900.00", "Agreed P100 off", user=world.admin)
    # As if recorded before the program kept its figure (no readable log line at the upgrade).
    row = dict(world.conn.execute("SELECT * FROM payment_checks WHERE id = ?", (check,)).fetchone())
    row["expected_c"] = None
    assert payments.agreed_deduction(world.conn, row) == 0


def test_the_record_check_dialog_says_a_lower_check_is_final(app, world):
    from atlaspo.ui import payments_page
    o = Order(world)
    o.enter()
    dialog = payments_page.CheckDialog(None, world.conn, world.accounting, _group(world, o))
    assert not dialog.lower.isVisibleTo(dialog) and dialog.lower.text() == ""
    dialog.amount.setText("1,900.00")
    assert dialog.lower.text() == "Lower than the program's ₱2,000.00. Once signed off, the ₱100.00 difference is final and is not added to the next check."
    dialog.amount.setText("2,100.00")
    assert dialog.lower.text() == ""
    dialog.close()


def test_the_paid_list_names_the_agreed_deduction(app, world, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings
    from atlaspo.ui import theme
    from atlaspo.ui.main_window import MainWindow
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(theme, "_settings", lambda: settings)
    o = Order(world)
    o.enter()
    check = _record(world, o, "880001", "1,900.00", "Agreed P100 off")
    payments.sign_off_check(world.conn, world.approver, check)
    win = MainWindow(world.conn, world.accounting)
    page = win.payments
    tab = {payments.FUTURE: page.FUTURE, payments.DUE: page.DUE}[payments.checks(world.conn)[0]["bucket"]]
    page._set_tab(tab)
    top = page.paid.topLevelItem(0)
    note = top.text(page.columns[page.paid].index("Note"))
    assert "Agreed deduction 100.00, final" in note and "agreed deduction" in top.toolTip(0)
    win.close()


# ---------------------------------------------------------------- Q2: a lower DR is what is paid, at once, with no sign-off
# A DR lower than our PO amount is paid at the DR amount automatically, with no "accept difference" and no
# management sign-off; the DR's products show why. Only a DR higher than ours waits for Accounting and a sign-off, and ours is paid then.
# Mixed (one DR lower, one higher on the same order): the lower part comes off at once, and the order waits for the higher part's sign-off only.

def _type_drs(world, o, north, south):
    receipts = payments.receipts_to_enter(world.conn, world.accounting, o.of_id)
    by_branch = {o.pos[NORTH]["id"]: north, o.pos[SOUTH]["id"]: south}
    payments.save_dr_entries(world.conn, world.accounting, o.of_id, {d["id"]: ("2026-10-28", by_branch[d["po_id"]]) for d in receipts})


def _asked(world):
    return world.conn.execute("SELECT COUNT(*) FROM payment_signoffs WHERE kind = 'DIFFERENCE'").fetchone()[0]


def test_a_lower_dr_is_paid_at_the_dr_amount_with_no_sign_off(world):
    o = Order(world)
    _type_drs(world, o, "950.00", "1,000.00")  # North's DR is P50.00 lower than its P1,000.00
    row = o.row()
    assert row["stage"] == payments.TO_PAY and (row["off_c"], row["lower_c"], row["higher_c"]) == (5000, 5000, 0)
    # Nothing to accept and nothing to sign off.
    with pytest.raises(POError, match="no difference to accept"):
        payments.accept_difference(world.conn, world.accounting, o.of_id, "Supplier billed P50 less")
    assert _asked(world) == 0 and payments.waiting_signoffs(world.conn) == 0
    money = payments.settlement(world.conn, o.of_id)
    assert (money["dr_lower_c"], money["pay_c"]) == (5000, 195000)
    assert _group(world, o)["check_c"] == 195000  # the DR total, P1,950.00
    # Recording it needs no reason, and nothing is owed afterwards.
    check = _record(world, o, "880001", "1,950.00")
    assert not payments.check_waiting(world.conn, check)
    assert _balance(world) == 0
    assert world.conn.execute("SELECT amount_c FROM payment_check_ofs WHERE check_id = ?", (check,)).fetchone()[0] == 195000
    # What the check paid for the lower DR is kept with the order, with no reason since nobody had to accept it.
    kept = world.conn.execute("SELECT difference_c, reason, dr_lower_c FROM payment_differences WHERE of_id = ?", (o.of_id,)).fetchone()
    assert tuple(kept) == (0, "", 5000)
    # The DR's save is the audit trail.
    assert world.conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action = 'DR_ENTRY'").fetchone()[0] == 2


def test_a_lower_dr_corrected_before_the_check_pays_what_it_says_now(world):
    o = Order(world)
    _type_drs(world, o, "950.00", "1,000.00")
    _type_drs(world, o, "980.00", "1,000.00")
    assert payments.settlement(world.conn, o.of_id)["pay_c"] == 198000
    _type_drs(world, o, "1,000.00", "1,000.00")
    assert payments.settlement(world.conn, o.of_id)["pay_c"] == 200000


def test_higher_drs_still_wait_for_a_sign_off_and_pay_the_programs_figure(world):
    o = Order(world)
    _type_drs(world, o, "1,080.00", "1,000.00")
    row = o.row()
    assert row["stage"] == payments.DIFFERENCE and (row["higher_c"], row["lower_c"]) == (8000, 0) and payments.checks_due(world.conn) == []
    payments.accept_difference(world.conn, world.accounting, o.of_id, "Supplier added delivery")
    assert o.row()["stage"] == payments.SIGN_OFF and payments.checks_due(world.conn) == []
    payments.sign_off_difference(world.conn, world.approver, o.of_id)
    assert o.row()["stage"] == payments.TO_PAY
    assert world.conn.execute("SELECT dr_lower_c FROM payment_differences WHERE of_id = ?", (o.of_id,)).fetchone()[0] == 0
    assert _group(world, o)["check_c"] == 200000


def test_one_dr_lower_and_one_higher_takes_the_lower_part_off_and_waits_for_the_higher(world):
    o = Order(world)
    _type_drs(world, o, "970.00", "1,040.00")  # -P30.00 and +P40.00
    row = o.row()
    # The lower DR comes off at once; the higher one holds the order for Accounting and a sign-off, on its own P40.00.
    assert row["stage"] == payments.DIFFERENCE and (row["lower_c"], row["higher_c"], row["off_c"]) == (3000, 4000, 7000)
    assert payments.settlement(world.conn, o.of_id)["pay_c"] == 197000
    payments.accept_difference(world.conn, world.accounting, o.of_id, "Mixed")
    assert o.row()["stage"] == payments.SIGN_OFF and payments.signoff(world.conn, "DIFFERENCE", o.of_id)["amount_c"] == 4000
    payments.sign_off_difference(world.conn, world.approver, o.of_id)
    assert o.row()["stage"] == payments.TO_PAY
    assert _group(world, o)["check_c"] == 197000  # P2,000.00 - P30.00: the higher DR is paid at ours
    # Correcting only the lower DR needs no new sign-off: the higher part signed off is unchanged.
    _type_drs(world, o, "990.00", "1,040.00")
    assert o.row()["stage"] == payments.TO_PAY and _group(world, o)["check_c"] == 199000
    # Changing the higher one does.
    _type_drs(world, o, "990.00", "1,060.00")
    assert o.row()["stage"] == payments.DIFFERENCE


def test_a_difference_signed_off_under_the_first_rule_still_counts(world):
    """Before this rule a difference was accepted and signed off on the whole gap, lower and higher together. While nothing changes it still counts."""
    o = Order(world)
    _type_drs(world, o, "920.00", "1,040.00")  # -P80.00 and +P40.00: P120.00 in all
    stamp = "2026-11-08T09:00:00"
    world.conn.execute("INSERT INTO payment_differences(of_id, difference_c, reason, accepted_by, accepted_at, dr_lower_c) VALUES (?,?,?,?,?,NULL)",
                       (o.of_id, 12000, "Old acceptance", world.accounting.id, stamp))
    world.conn.execute("INSERT INTO payment_signoffs(kind, ref_id, amount_c, requested_by, requested_at, signed_by, signed_at) VALUES ('DIFFERENCE',?,?,?,?,?,?)",
                       (o.of_id, 12000, world.accounting.id, stamp, world.approver.id, stamp))
    assert o.row()["stage"] == payments.TO_PAY
    assert payments.settlement(world.conn, o.of_id)["pay_c"] == 192000
    # Paid, with nothing kept (accepted under version 27): read from the DRs as they stand.
    check = _record(world, o, "880001", "1,920.00")
    world.conn.execute("UPDATE payment_differences SET dr_lower_c = NULL WHERE of_id = ?", (o.of_id,))
    assert payments.settlement(world.conn, o.of_id)["pay_c"] == 192000 and not payments.check_waiting(world.conn, check)


def test_the_final_of_shows_the_lower_drs_and_adds_up(world):
    o = Order(world)
    _type_drs(world, o, "950.00", "1,000.00")
    text = PdfReader(BytesIO(pdf.office_copy(world.conn, world.accounting, o.of_id))).pages[0].extract_text()
    assert "DR total lower" in text and "50.00" in text.split("DR total lower", 1)[1][:40]
    assert "1,950.00" in text


# ---------------------------------------------------------------- Q4: a debit memo cancelled after payment is paid back

def _paid_short(world):
    """North got 8 of 10: a P200.00 memo, and the order paid P1,800.00."""
    o = Order(world, av=8)
    memo = o.short_memo()
    o.enter()
    check = _record(world, o, "880001", "1,800.00")
    return o, memo, check


def test_a_memo_cancelled_after_payment_goes_on_the_next_check_once(world):
    o, memo, check = _paid_short(world)
    assert memo["total_c"] == 20000
    debit_memos.cancel(world.conn, world.approver, memo["id"], "Supplier showed the pieces were in the carton")
    assert world.conn.execute("SELECT paid_back_check_id FROM debit_memos WHERE id = ?", (memo["id"],)).fetchone()[0] == check
    summary = world.conn.execute("SELECT summary FROM audit_logs WHERE action = 'DM_CANCEL'").fetchone()[0]
    assert summary == f"{memo['dm_number']} cancelled: Supplier showed the pieces were in the carton. Check 880001 already paid the order; ₱200.00 goes on the supplier's next check"
    assert _balance(world) == -20000
    shown = payments.paid_back_memos(world.conn, world.supplier)
    assert [(m["dm_number"], m["total_c"], m["cancel_reason"], m["paid_by"]) for m in shown] == [(memo["dm_number"], 20000, "Supplier showed the pieces were in the carton", "880001")]
    # Nothing due yet: it stays in sight.
    assert [s["owed_c"] for s in payments.owed_without_orders(world.conn)] == [20000]
    # The order doesn't go back to "debit memo first".
    assert payments.settlement(world.conn, o.of_id)["unclaimed_c"] == 0
    second = Order(world)
    second.enter()
    group = _group(world, second)
    assert (group["owed_c"], group["check_c"]) == (20000, 220000)  # P2,000.00 + P200.00
    payments.record_check(world.conn, world.accounting, [second.of_id], "880002", group["due"].isoformat(), "2,200.00")
    # Paid once: nothing more is owed, and it's no longer listed.
    assert _balance(world) == 0
    assert payments.paid_back_memos(world.conn, world.supplier) == []
    third = Order(world)
    third.enter()
    assert _group(world, third)["check_c"] == 200000


def test_pieces_counted_later_are_not_paid_twice(world):
    o, memo, check = _paid_short(world)
    o.count(NORTH, 2)  # the 2 missing pieces arrive after the check
    debit_memos.cancel(world.conn, world.approver, memo["id"], "The pieces came")
    # P200.00 owed in all, not P400.00: the pieces are paid as delivered, and nothing more as paid back.
    assert _balance(world) == -20000
    assert payments.settlement(world.conn, o.of_id)["paid_back_c"] == 0


def test_a_memo_cancelled_before_payment_is_unchanged(world):
    o = Order(world, av=8)
    memo = o.short_memo()
    debit_memos.cancel(world.conn, world.approver, memo["id"], "Issued by mistake")
    assert world.conn.execute("SELECT paid_back_check_id FROM debit_memos WHERE id = ?", (memo["id"],)).fetchone()[0] is None
    # Short with no memo again, as before: not ready to pay, and the shortage counts again.
    assert o.row()["stage"] != payments.TO_PAY and payments.settlement(world.conn, o.of_id)["unclaimed_c"] == 20000
    assert payments.paid_back_memos(world.conn, world.supplier) == []


def test_which_check_paid_it_back_never_changes(world):
    o, memo, check = _paid_short(world)
    debit_memos.cancel(world.conn, world.approver, memo["id"], "Wrong")
    with pytest.raises(sqlite3.IntegrityError):
        world.conn.execute("UPDATE debit_memos SET paid_back_check_id = NULL WHERE id = ?", (memo["id"],))


def test_checks_to_make_shows_the_memo_number_and_why(app, world, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings
    from atlaspo.ui import theme
    from atlaspo.ui.main_window import MainWindow
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(theme, "_settings", lambda: settings)
    o, memo, check = _paid_short(world)
    debit_memos.cancel(world.conn, world.approver, memo["id"], "Supplier proved delivery")
    second = Order(world)
    second.enter()
    win = MainWindow(world.conn, world.accounting)
    page = win.payments
    page._set_tab(page.TO_PAY)
    item = next(page.to_pay.topLevelItem(i) for i in range(page.to_pay.topLevelItemCount()) if isinstance(page.to_pay.topLevelItem(i).data(0, 256), int))
    page.to_pay.setCurrentItem(item)
    assert f"{memo['dm_number']} ₱200.00 (check 880001 had paid it)" in page.breakdown.text() and "Supplier proved delivery" in page.breakdown.text()
    win.close()


# ---------------------------------------------------------------- AUD-146: the long working-out happens before the write lock

def test_record_check_works_out_before_taking_the_lock(world, monkeypatch):
    o = Order(world)
    o.enter()
    group = _group(world, o)
    calls = []
    real = payments.order_row

    def counting(conn, *a, **k):
        calls.append(conn.in_transaction)
        return real(conn, *a, **k)

    monkeypatch.setattr(payments, "order_row", counting)
    payments.record_check(world.conn, world.accounting, [o.of_id], "880001", group["due"].isoformat(), "2,000.00")
    # Every order worked out while nobody held the lock; nothing was worked out again inside it.
    assert calls and not any(calls)


def test_a_save_by_another_pc_in_between_makes_it_work_out_again(world, env, monkeypatch):
    o = Order(world)
    o.enter()
    group = _group(world, o)
    other = sqlite3.connect(world.conn.execute("PRAGMA database_list").fetchone()[2], isolation_level=None)
    real = payments._expected
    state = {"n": 0}

    def expected(conn, rows, groups=None):
        state["n"] += 1
        if state["n"] == 1:
            # Another PC saves while this one works out the check.
            other.execute("INSERT INTO system_settings(key, value, updated_at) VALUES ('test.other_pc', 'saved', '2026-11-08 10:00:00')")
        return real(conn, rows, groups)

    monkeypatch.setattr(payments, "_expected", expected)
    payments.record_check(world.conn, world.accounting, [o.of_id], "880001", group["due"].isoformat(), "2,000.00")
    other.close()
    assert state["n"] == 2
