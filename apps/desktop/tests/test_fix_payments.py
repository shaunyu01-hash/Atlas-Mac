"""Payments fixes from an audit: speed of one refresh, odd terms, check numbers, dates and amounts, permissions, and what the check tabs and cash flow say."""
from __future__ import annotations

import os
import re
from datetime import date

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication

from atlaspo import auth, claim_stubs, db, debit_memos, masters, order_forms, orders, payments, receiving, sample_data
from atlaspo.db import POError, PermissionDenied
from atlaspo.ui import payments_page, theme

from conftest import NORTH, PASSWORD, SOUTH


class Order:
    """One order delivered to two branches, 10 pieces each at 100.00, with two receivers at each (as in test_payments)."""

    def __init__(self, world, av=10, ros=10):
        self.w = world
        self.of_id = world.approved(items=[world.item(cost=10000, alloc={NORTH: 10, SOUTH: 10}, description="Socks", item_code=f"SOCK-{world.supplier}")])  # one SKU per supplier
        self.pos = {p["branch_code"]: p for p in order_forms.generate_pos(world.conn, world.buyer, self.of_id)}
        for p in self.pos.values():
            orders.issue(world.conn, world.buyer, p["id"])
        self.count(NORTH, av)
        self.count(SOUTH, ros)

    def count(self, branch, pieces):
        w, po = self.w, self.pos[branch]
        item = orders.get_po(w.conn, po["id"])["items"][0]["id"]
        for n in (1, 2):
            login = f"r{branch}x{n}"
            existing = w.conn.execute("SELECT id FROM users WHERE username = ?", (login,)).fetchone()
            who = auth.get_user(w.conn, existing["id"]) if existing else auth.create_user(w.conn, w.admin, login, f"Receiver {branch} {n}", PASSWORD, ["RECEIVER"], must_change=False, branch_code=branch)
            receiving.save_count(w.conn, who, po["id"], {item: str(pieces)})

    def short_memo(self, branch=NORTH):
        po_id = self.pos[branch]["id"]
        lines = debit_memos.outstanding(self.w.conn, self.w.buyer, po_id)
        return debit_memos.issue(self.w.conn, self.w.buyer, po_id, {(l["po_item_id"], l["reason"]): l["pieces_m"] // 1000 for l in lines}, "Short")

    def enter(self, day="2026-10-28", off_by="0"):
        row = payments.order_row(self.w.conn, self.of_id)
        by_po = {p["po_id"]: p["delivered_c"] for p in row["pos"]}
        receipts = payments.receipts_to_enter(self.w.conn, self.w.accounting, self.of_id)
        payments.save_dr_entries(self.w.conn, self.w.accounting, self.of_id, {d["id"]: (day, f"{by_po[d['po_id']] / 100 + float(off_by):.2f}") for d in receipts})

BALANCE_SQL = "SELECT COALESCE(SUM(amount_c), 0) FROM payment_checks WHERE supplier_id"


@pytest.fixture
def app(tmp_path, monkeypatch):
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(theme, "_settings", lambda: settings)
    yield QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def accounting(request):
    if "world" in request.fixturenames:
        world = request.getfixturevalue("world")
        world.accounting = auth.create_user(world.conn, world.admin, "lorna", "Lorna Tan", PASSWORD, ["ACCOUNTING"], must_change=False)


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    folder = tmp_path_factory.mktemp("payments_sample")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PO_DATA_DIR", str(folder))
        conn = db.open_database(folder / "sample.sqlite")
        sample_data.build(conn)
        yield conn
        conn.close()


class Statements:
    """Counts the statements SQLite runs, and keeps their text."""

    def __init__(self, conn):
        self.conn, self.sql = conn, []

    def __enter__(self):
        self.conn.set_trace_callback(self.sql.append)
        return self

    def __exit__(self, *_):
        self.conn.set_trace_callback(None)


def _figures(groups):
    return [(g["due"], g["supplier_id"], g["amount_c"], g["credit_c"], g["owed_c"], g["check_c"], [o["id"] for o in g["orders"]]) for g in groups]


def _ready(world, of_id):
    if payments.order_row(world.conn, of_id)["stage"] == payments.ENTER_DRS:
        payments.drs_match(world.conn, world.accounting, of_id)
    return next(g for g in payments.checks_due(world.conn) if of_id in [o["id"] for o in g["orders"]])


def _record(world, of_id, number, reason=""):
    group = _ready(world, of_id)
    amount = group["check_c"] / 100 + (10 if reason else 0)
    return payments.record_check(world.conn, world.accounting, [of_id], number, group["due"].isoformat(), f"{amount:.2f}", reason)


# ---------------------------------------------------------------- AUD-141, AUD-143: one refresh, one batch

def test_aud141_one_payments_refresh_works_each_figure_out_once(app, sample):
    user = auth.login(sample, "accounting", "123")
    # The plain answers, worked out the slow way with nothing shared.
    rows = payments.orders_in_payment(sample)
    groups = payments.checks_due(sample, rows=rows)
    flow = payments.cash_flow(sample, rows=rows)
    owed, credit = payments.owed_without_orders(sample, groups), payments.credit_without_orders(sample, groups)
    page = payments_page.PaymentsPage(sample, user)
    with Statements(sample) as seen:
        page.refresh()
    # Before the fix one refresh of the sample ran 2,444 statements: each paid order was settled again for every Saturday and again for the cash flow.
    # The limit grows with the sample: 1,500 was set when it had 21 numbered OFs, about 71 statements an order. The current sample has more suppliers.
    ofs = sample.execute("SELECT COUNT(*) FROM order_forms WHERE status = 'APPROVED' AND of_number IS NOT NULL").fetchone()[0]
    assert len(seen.sql) <= 72 * ofs, (len(seen.sql), ofs)
    balances = [s for s in seen.sql if s.startswith(BALANCE_SQL)]
    # Each supplier's balance is asked once at most: those with checks, and BBB and CCC with orders due too.
    suppliers = sample.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0]
    assert len(balances) <= suppliers and len(balances) == len(set(balances)), balances
    # And every figure is the same as before.
    assert _figures(sorted(page.groups, key=lambda g: (g["due"], g["supplier"]))) == _figures(groups)
    assert page.flow_rows == flow and page.owed_alone == owed and page.credit_alone == credit
    assert {r["id"]: r["pay_c"] for r in page.orders} == {r["id"]: r["pay_c"] for r in rows if r["id"] in {o["id"] for o in page.orders}}
    page.close()


def test_aud141_checks_due_asks_each_suppliers_balance_once(sample):
    rows = payments.orders_in_payment(sample)
    with Statements(sample) as seen:
        groups = payments.checks_due(sample, rows=rows)
    suppliers = {g["supplier_id"] for g in groups}
    assert len(groups) > len(suppliers)  # the sample has one supplier due on several Saturdays
    assert len([s for s in seen.sql if s.startswith(BALANCE_SQL)]) == len(suppliers)


def test_aud141_figures_shared_only_inside_one_look(world):
    o = Order(world)
    with payments.one_look(world.conn):
        first = payments.settlement(world.conn, o.of_id)
        assert payments.settlement(world.conn, o.of_id) is first
    assert payments.settlement(world.conn, o.of_id) is not first  # outside, always fresh
    assert payments._look is None


def test_aud143_a_batch_of_three_checks_works_out_checks_to_make_once(world, monkeypatch):
    ids = []
    for n in range(3):
        supplier = masters.save_supplier(world.conn, world.admin, {"code": f"S{n}", "name": f"{'Alpha Bravo Charlie'.split()[n]} Textiles", "payment_terms": "30 D"}, confirm_duplicate=True)
        world.supplier, keep = supplier, world.supplier
        ids.append(Order(world).of_id)
        world.supplier = keep
    groups = payments.checks_due(world.conn)
    assert len(groups) == 3
    batch = [{"of_ids": [o["id"] for o in g["orders"]], "number": str(330001 + i), "date": g["due"].isoformat(), "amount": f"{g['check_c'] / 100:.2f}"} for i, g in enumerate(groups)]
    calls = []
    real = payments.checks_due
    monkeypatch.setattr(payments, "checks_due", lambda *a, **k: calls.append(1) or real(*a, **k))
    with Statements(world.conn) as seen:
        made = payments.record_checks(world.conn, world.accounting, batch)
    assert len(calls) == 1 and len(made) == 3
    assert len(seen.sql) <= 900, len(seen.sql)
    stored = world.conn.execute("SELECT check_number, amount_c, credit_applied_c FROM payment_checks ORDER BY check_number").fetchall()
    assert [tuple(r) for r in stored] == [(b["number"], g["check_c"], 0) for b, g in zip(batch, groups)]


# ---------------------------------------------------------------- AUD-060: terms no date can come from

def test_aud060_terms_of_three_million_days_hold_up_only_their_own_order(app, world):
    good = Order(world)
    big = masters.save_supplier(world.conn, world.admin, {"code": "BIG", "name": "Big Terms Co", "payment_terms": "30 D"}, confirm_duplicate=True)
    world.supplier, keep = big, world.supplier
    bad = Order(world)
    world.supplier = keep
    # Planted the way an order made before terms were capped would hold it. The lock on a submitted OF is lifted only for this.
    with db.tx(world.conn):
        world.conn.execute("INSERT INTO po_write_guard(token) VALUES ('test')")
        world.conn.execute("UPDATE order_forms SET payment_terms = '3000000D' WHERE id = ?", (bad.of_id,))
        world.conn.execute("DELETE FROM po_write_guard WHERE token = 'test'")
    assert world.conn.execute("SELECT payment_terms FROM order_forms WHERE id = ?", (bad.of_id,)).fetchone()[0] == "3000000D"
    rows = {r["id"]: r for r in payments.orders_in_payment(world.conn)}
    assert rows[bad.of_id]["stage"] == payments.TERMS and rows[bad.of_id]["due"] is None and rows[bad.of_id]["terms_unreadable"]
    assert rows[good.of_id]["stage"] == payments.TO_PAY
    assert [o["id"] for g in payments.checks_due(world.conn) for o in g["orders"]] == [good.of_id]
    assert payments.cash_flow(world.conn)
    with pytest.raises(POError, match="has terms that give no check date, so it can't be paid until they are corrected"):
        payments.record_check(world.conn, world.accounting, [bad.of_id], "440001", "2026-11-14", "2000.00")
    assert rows[bad.of_id]["terms_cause"] == "payment terms" and rows[bad.of_id]["terms_value"] == "3000000D"
    assert rows[good.of_id]["terms_cause"] == ""
    page = payments_page.PaymentsPage(world.conn, world.accounting)
    page._set_tab(page.TO_CHECK)
    row = next(r for r in page.orders if r["id"] == bad.of_id)
    step = page._next_step(row)
    assert step == 'The terms on this order give no check date (payment terms "3000000D"). Admin has to look at it'
    assert "corrects" not in step and "payment terms \"3000000D\"" in page._breakdown(row)
    page.close()


def test_aud060_term_discount_days_that_give_no_date_are_named(app, world):
    import json
    good = Order(world)
    big = masters.save_supplier(world.conn, world.admin, {"code": "BIGD", "name": "Big Discount Co", "payment_terms": "30 D"}, confirm_duplicate=True)
    world.supplier, keep = big, world.supplier
    bad = Order(world)
    world.supplier = keep
    snap = json.loads(world.conn.execute("SELECT supplier_snapshot FROM order_forms WHERE id = ?", (bad.of_id,)).fetchone()[0])
    snap.update(term_discount_bp=200, term_discount_days=3000000)
    with db.tx(world.conn):
        world.conn.execute("INSERT INTO po_write_guard(token) VALUES ('test')")
        world.conn.execute("UPDATE order_forms SET supplier_snapshot = ? WHERE id = ?", (json.dumps(snap), bad.of_id))
        world.conn.execute("DELETE FROM po_write_guard WHERE token = 'test'")
    rows = {r["id"]: r for r in payments.orders_in_payment(world.conn)}
    row = rows[bad.of_id]
    assert row["stage"] == payments.TERMS and row["terms_cause"] == "term discount days" and row["terms_value"] == "3000000"
    assert rows[good.of_id]["stage"] == payments.TO_PAY
    with pytest.raises(POError, match="has terms that give no check date"):
        payments.record_check(world.conn, world.accounting, [bad.of_id], "440002", "2026-11-14", "2000.00")
    page = payments_page.PaymentsPage(world.conn, world.accounting)
    page._set_tab(page.TO_CHECK)
    shown = next(r for r in page.orders if r["id"] == bad.of_id)
    step = page._next_step(shown)
    assert 'term discount days "3000000"' in step and "30D" not in step
    assert 'term discount days "3000000"' in page._breakdown(shown)
    page.close()


# ---------------------------------------------------------------- AUD-077: sign-off on a cancelled check

def test_aud077_a_cancelled_check_cannot_be_signed_off(world):
    o = Order(world)
    check = _record(world, o.of_id, "550001", "Supplier asked for 10 more")
    assert payments.check_waiting(world.conn, check)
    payments.cancel_check(world.conn, world.accounting, check, "Printed wrong")
    with pytest.raises(POError, match="This check was cancelled"):
        payments.sign_off_check(world.conn, world.admin, check)
    assert not world.conn.execute("SELECT 1 FROM audit_logs WHERE action = 'PAYMENT_SIGNOFF' AND entity_id = ?", (check,)).fetchone()


# ---------------------------------------------------------------- AUD-083, AUD-139: check numbers

@pytest.mark.parametrize("number, why", [("²", "digits only"), ("١٢٣", "digits only"), ("12-34", "digits only"), ("7" * 21, "too long"), ("CREDIT-00002", "kept for orders settled"),
                                         ("credit-9", "kept for orders settled")])
def test_aud083_aud139_check_numbers_are_plain_digits(world, number, why):
    o = Order(world)
    with pytest.raises(POError, match=why):
        _record(world, o.of_id, number)
    with pytest.raises(POError, match="digits"):
        payments.check_numbers(number, 2)
    assert payments.check_numbers("0099", 2) == ["0099", "0100"]


def _plant_check(world, number, source="EXTERNAL", status="CANCELLED"):
    world.conn.execute("INSERT INTO payment_checks(check_number, source, check_date, supplier_id, amount_c, credit_applied_c, difference_reason, status, recorded_by, recorded_at, cancelled_by, cancelled_at, cancel_reason)"
                       " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (number, source, "2026-11-14", world.supplier, 100, 0, "", status, world.accounting.id, "2026-11-07 09:00:00",
                                                             world.accounting.id, "2026-11-07 09:00:00", "Planted"))
    world.conn.commit()


def test_aud083_an_odd_number_recorded_before_does_not_break_the_next_number(app, world):
    o = Order(world)
    _record(world, o.of_id, "000123")
    for odd in ("²", "7" * 4301, "١٢٣"):
        _plant_check(world, odd)
    assert payments.next_check_number(world.conn) == "000124"
    assert not payments_page._digits("²") and payments_page._digits("000124")
    second = Order(world)
    group = _ready(world, second.of_id)
    dialog = payments_page.BatchCheckDialog(None, world.conn, world.accounting, [group])
    assert dialog.first.text() == "000124"
    dialog.first.setText("")
    dialog.last.setText("²")  # used to raise ValueError while typing
    assert dialog.status.text().startswith("Every check needs a number")
    dialog.first.setText("000124")
    assert dialog.status.text().startswith("The list ends at 000124, but you typed ².")
    dialog.close()


def test_aud139_settle_by_credit_takes_the_next_free_credit_number(world):
    _plant_check(world, "CREDIT-00002", source="EXTERNAL")  # typed into the number box before such numbers were refused
    assert payments._next_credit_number(world.conn) == "CREDIT-00003"
    # The whole way: a check typed high leaves a credit that covers the next order. Counting CREDIT- numbers gave CREDIT-00002, already used, and failed.
    first = Order(world)
    group = _ready(world, first.of_id)
    payments.record_check(world.conn, world.accounting, [first.of_id], "100001", group["due"].isoformat(), f"{group['check_c'] / 100 + 5000:.2f}", "Typed high by mistake")
    second = Order(world)
    assert _ready(world, second.of_id)["check_c"] == 0
    check = payments.settle_by_credit(world.conn, world.accounting, [second.of_id])
    assert world.conn.execute("SELECT check_number FROM payment_checks WHERE id = ?", (check,)).fetchone()[0] == "CREDIT-00003"


def test_aud139_a_cancelled_credit_number_is_not_used_again(world):
    _plant_check(world, "CREDIT-00001", source="PROGRAM", status="ACTIVE")
    _plant_check(world, "CREDIT-00003", source="PROGRAM")
    assert payments._next_credit_number(world.conn) == "CREDIT-00004"  # counting them gave CREDIT-00003, already used


# ---------------------------------------------------------------- AUD-116: who may read the payment figures

def test_aud116_payment_figures_need_payment_view(world):
    o = Order(world)
    check = _record(world, o.of_id, "660001")
    receiver = auth.get_user(world.conn, world.conn.execute("SELECT id FROM users WHERE username = ?", (f"r{NORTH}x1",)).fetchone()[0])
    assert not receiver.can("payment.view")
    calls = {"orders_in_payment": lambda u: payments.orders_in_payment(world.conn, "", user=u), "checks": lambda u: payments.checks(world.conn, payments.ACTIVE, "", user=u),
             "checks_due": lambda u: payments.checks_due(world.conn, "", user=u), "cash_flow": lambda u: payments.cash_flow(world.conn, user=u),
             "supplier_balance": lambda u: payments.supplier_balance(world.conn, world.supplier, user=u), "supplier_credit": lambda u: payments.supplier_credit(world.conn, world.supplier, user=u),
             "credit_without_orders": lambda u: payments.credit_without_orders(world.conn, user=u), "owed_without_orders": lambda u: payments.owed_without_orders(world.conn, user=u),
             "late_claims": lambda u: payments.late_claims(world.conn, world.supplier, user=u), "orders_on_check": lambda u: payments.orders_on_check(world.conn, check, user=u)}
    viewer = auth.create_user(world.conn, world.admin, "vic", "Vic Viewer", PASSWORD, ["VIEWER"], must_change=False)
    for name, call in calls.items():
        with pytest.raises(PermissionDenied):
            call(receiver)
        assert call(viewer) == call(None) == call(world.accounting), name


# ---------------------------------------------------------------- AUD-153: clearing a bank statement's checks together

def _claimed(world, n):
    ids = []
    for i in range(n):
        o = Order(world)
        check = _record(world, o.of_id, str(770001 + i))
        payments.claim_check(world.conn, world.accounting, check, "2026-11-02")
        ids.append(check)
    return ids


def test_aud153_mark_several_checks_cleared_with_one_date(world):
    ids = _claimed(world, 3)
    payments.mark_cleared_many(world.conn, world.accounting, ids, "2026-11-05")
    assert [r[0] for r in world.conn.execute("SELECT cleared_on FROM check_clears ORDER BY check_id")] == ["2026-11-05"] * 3
    assert world.conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action = 'CHECK_CLEAR'").fetchone()[0] == 3


def test_aud153_one_check_that_cannot_clear_stops_them_all(world):
    ids = _claimed(world, 2)
    not_claimed = _record(world, Order(world).of_id, "779999")
    with pytest.raises(POError, match="779999 has to be claimed"):
        payments.mark_cleared_many(world.conn, world.accounting, [*ids, not_claimed], "2026-11-05")
    with pytest.raises(POError, match="can't have cleared before"):
        payments.mark_cleared_many(world.conn, world.accounting, ids, "2026-11-01")
    with pytest.raises(POError, match="Tick"):
        payments.mark_cleared_many(world.conn, world.accounting, [], "2026-11-05")
    with pytest.raises(PermissionDenied):
        payments.mark_cleared_many(world.conn, world.buyer, ids, "2026-11-05")
    assert world.conn.execute("SELECT COUNT(*) FROM check_clears").fetchone()[0] == 0
    assert world.conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action = 'CHECK_CLEAR'").fetchone()[0] == 0


def test_aud153_the_claimed_tab_ticks_checks_and_marks_them_cleared_together(app, world, monkeypatch):
    ids = _claimed(world, 3)
    page = payments_page.PaymentsPage(world.conn, world.accounting)
    page._set_tab(page.CLAIMED)
    items = [page.paid.topLevelItem(i) for i in range(page.paid.topLevelItemCount())]
    assert len(items) == 3 and all(item.checkState(0) == Qt.CheckState.Unchecked for item in items)
    page.paid.setCurrentItem(items[0])
    assert page.primary_btn.text() == "Mark cleared"  # one at a time still works
    items[0].setCheckState(0, Qt.CheckState.Checked)
    items[1].setCheckState(0, Qt.CheckState.Checked)
    assert page.primary_btn.text() == "Mark 2 cleared" and "2 ticked" in page.note.text()
    ticked = page._ticked_checks()
    dialog = payments_page.ClearTickedDialog(page, world.conn, world.accounting, ticked)
    dialog.save()
    page.refresh()
    assert world.conn.execute("SELECT COUNT(*) FROM check_clears").fetchone()[0] == 2
    assert page.paid.topLevelItemCount() == 1 and page.primary_btn.text() == "Mark cleared" and not page.clear_ticked
    assert set(ids) >= {c["id"] for c in ticked}
    page.close()


# ---------------------------------------------------------------- AUD-087, AUD-081: dates and amounts the services take

def test_aud087_dates_must_be_real_and_are_kept_one_way(world):
    o = Order(world)
    group = _ready(world, o.of_id)
    with pytest.raises(POError, match="isn't a date"):
        payments.record_check(world.conn, world.accounting, [o.of_id], "880001", "2026-02-30", f"{group['check_c'] / 100:.2f}")
    with pytest.raises(POError, match="isn't a date"):
        payments.record_check(world.conn, world.accounting, [o.of_id], "880001", "2026-W45-1", f"{group['check_c'] / 100:.2f}")
    check = payments.record_check(world.conn, world.accounting, [o.of_id], "880001", "20261101", f"{group['check_c'] / 100:.2f}")
    assert world.conn.execute("SELECT check_date FROM payment_checks WHERE id = ?", (check,)).fetchone()[0] == "2026-11-01"
    with pytest.raises(POError, match="isn't a date"):
        payments.claim_check(world.conn, world.accounting, check, "garbage")
    payments.claim_check(world.conn, world.accounting, check, "20261101")
    assert world.conn.execute("SELECT claimed_on FROM check_claims WHERE check_id = ?", (check,)).fetchone()[0] == "2026-11-01"
    with pytest.raises(POError, match="isn't a date"):
        payments.clear_check(world.conn, world.accounting, check, "2026-13-01")
    payments.clear_check(world.conn, world.accounting, check, "20261105")  # used to be refused: "20261101" sorted after it as text
    assert world.conn.execute("SELECT cleared_on FROM check_clears WHERE check_id = ?", (check,)).fetchone()[0] == "2026-11-05"


def test_aud087_aud081_dr_dates_and_totals(world):
    o = Order(world, av=9)  # short, so the DRs are compared
    o.short_memo()
    receipts = payments.receipts_to_enter(world.conn, world.accounting, o.of_id)
    dr = receipts[0]["id"]
    for day, total, why in [("2026-02-30", "100.00", "isn't a date"), ("garbage", "100.00", "isn't a date"), ("2026-11-02", "-5", "below nothing"),
                            ("2026-11-02", "1e17", "too large"), ("2026-11-02", "NaN", "isn't an amount"), ("2026-11-02", "Infinity", "isn't an amount")]:
        with pytest.raises(POError, match=why):
            payments.save_dr_entries(world.conn, world.accounting, o.of_id, {dr: (day, total)})
    payments.save_dr_entries(world.conn, world.accounting, o.of_id, {dr: ("20261102", "900.00")})
    assert tuple(world.conn.execute("SELECT dr_date, total_c FROM dr_entries WHERE dr_id = ?", (dr,)).fetchone()) == ("2026-11-02", 90000)


@pytest.mark.parametrize("amount, why", [("1e17", "too large"), ("100000000000.00", "too large"), ("-5", "more than nothing"), ("abc", "isn't an amount"), ("NaN", "isn't an amount")])
def test_aud081_absurd_check_amounts_are_refused(world, amount, why):
    o = Order(world)
    group = _ready(world, o.of_id)
    with pytest.raises(POError, match=why):
        payments.record_check(world.conn, world.accounting, [o.of_id], "880002", group["due"].isoformat(), amount, "Big")
    assert not world.conn.execute("SELECT 1 FROM payment_checks").fetchone()


# ---------------------------------------------------------------- AUD-098: cash flow marks what may not go out as dated

def test_aud098_cash_flow_marks_checks_waiting_for_sign_off_and_dates_passed(app, world):
    waiting = _record(world, Order(world).of_id, "990001", "Supplier asked for 10 more")
    plain = Order(world)
    group = _ready(world, plain.of_id)
    passed = payments.record_check(world.conn, world.accounting, [plain.of_id], "990002", "2020-06-06", f"{group['check_c'] / 100:.2f}")
    rows = payments.cash_flow(world.conn)
    by_date = {r["date"]: r for r in rows}
    old = by_date[date(2020, 6, 6)]
    assert (old["passed_n"], old["passed_c"], old["printed_c"]) == (1, group["check_c"], group["check_c"])
    marked = next(r for r in rows if r["waiting_n"])
    amount = world.conn.execute("SELECT amount_c FROM payment_checks WHERE id = ?", (waiting,)).fetchone()[0]
    assert marked["waiting_c"] == amount and marked["printed_c"] == amount
    # Amounts unchanged: the totals still add every check that hasn't cleared.
    assert rows[-1]["running_c"] == sum(r["amount_c"] for r in payments.checks(world.conn, payments.ACTIVE))
    assert sum(m["passed_n"] for m in payments.cash_flow_by_month(rows)) == 1
    page = payments_page.PaymentsPage(world.conn, world.accounting)
    page._set_tab(page.FLOW)
    notes = [page.flow.item(r, 6).text() for r in range(page.flow.rowCount())]
    assert any("date passed" in n for n in notes) and any("waiting for sign-off" in n for n in notes)
    assert passed
    page.close()


# ---------------------------------------------------------------- AUD-094: a check whose orders changed after it

def test_aud094_check_tabs_say_what_changed_since_the_check(app, world, monkeypatch):
    o = Order(world)
    check = _record(world, o.of_id, "500001")
    payments.claim_check(world.conn, world.accounting, check, "2026-11-02")
    lines = claim_stubs.late_lines(world.conn, world.buyer, o.pos[NORTH]["po_number"])
    claim_stubs.issue_late(world.conn, world.buyer, o.pos[NORTH]["po_number"], {lines[0]["po_item_id"]: "3"}, "Found torn when unpacked")
    page = payments_page.PaymentsPage(world.conn, world.accounting)
    page._set_tab(page.CLAIMED)
    top = page.paid.topLevelItem(0)
    note = top.text(page.columns[page.paid].index("Note"))
    assert note == "Changed since this check: -300.00", note
    assert "The check paid" in top.toolTip(0)
    # The check's own amount is still what was paid.
    assert top.text(page.columns[page.paid].index("Check amount")) == payments_page.fmt_plain(world.conn.execute("SELECT amount_c FROM payment_checks WHERE id = ?", (check,)).fetchone()[0])
    page.close()


def test_aud094_no_note_when_nothing_changed(app, world):
    o = Order(world)
    check = _record(world, o.of_id, "500002")
    payments.claim_check(world.conn, world.accounting, check, "2026-11-02")
    page = payments_page.PaymentsPage(world.conn, world.accounting)
    page._set_tab(page.CLAIMED)
    assert page.paid.topLevelItem(0).text(page.columns[page.paid].index("Note")) == ""
    page.close()


def test_waiting_signoffs_can_reuse_the_rows_already_worked_out(world):
    o = Order(world, av=9)
    o.short_memo()
    o.enter(off_by="50")
    payments.accept_difference(world.conn, world.accounting, o.of_id, "Delivery charge on the DR")
    rows = payments.orders_in_payment(world.conn)
    assert payments.waiting_signoffs(world.conn) == payments.waiting_signoffs(world.conn, rows) == 1
    assert re.match(r"SIGN_OFF", payments.order_row(world.conn, o.of_id)["stage"])
