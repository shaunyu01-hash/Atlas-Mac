"""The DRs of an order open as a panel on the right of Payments, not a pop-up. Pick the DR that differs, and on each product
that differs type what the supplier's DR says, pieces and/or unit price. The DR's total is worked out from the products, and each product that
differs is kept, so the difference and its sign-off show which products differ and by how much.

The order is test_payments.Order: socks (SOCK-1) at P100.00 a piece, 10 to North and 10 to South, one DR for each branch."""
from __future__ import annotations

import os
import sqlite3

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QDialog, QWidget

from atlaspo import auth, db, payments, schema
from atlaspo.db import POError
from atlaspo.ui import payments_page, theme
from atlaspo.ui.main_window import MainWindow

from conftest import NORTH, PASSWORD, SOUTH
from test_payments import Order


@pytest.fixture(autouse=True)
def people(world):
    world.accounting = auth.create_user(world.conn, world.admin, "lorna", "Lorna Tan", PASSWORD, ["ACCOUNTING"], must_change=False)
    world.approver = auth.create_user(world.conn, world.admin, "ramon", "Ramon Villanueva", PASSWORD, ["APPROVER"], must_change=False)


@pytest.fixture
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def store(tmp_path, monkeypatch):
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(theme, "_settings", lambda: settings)


def _dr(world, o, branch):
    return next(d for d in payments.receipts_to_enter(world.conn, world.accounting, o.of_id) if d["po_id"] == o.pos[branch]["id"])


def _item(world, o, branch):
    return payments.dr_lines(world.conn, world.accounting, o.of_id, _dr(world, o, branch)["id"])["lines"][0]["po_item_id"]


def _save(world, o, branch, pieces="", unit="", day="2026-10-28"):
    payments.save_dr_lines(world.conn, world.accounting, o.of_id, {_dr(world, o, branch)["id"]: (day, {_item(world, o, branch): (pieces, unit)})})


def _entry(world, o, branch):
    return world.conn.execute("SELECT dr_date, total_c FROM dr_entries WHERE dr_id = ?", (_dr(world, o, branch)["id"],)).fetchone()


# ---------------------------------------------------------------- backend

def test_a_drs_products_show_at_the_orders_cost(world):
    o = Order(world)
    info = payments.dr_lines(world.conn, world.accounting, o.of_id, _dr(world, o, NORTH)["id"])
    assert info["base_c"] == 100000
    line = info["lines"][0]
    assert (line["item_code"], line["pieces_m"], line["unit_c"], line["amount_c"]) == ("SOCK-1", 10000, 10000, 100000)
    assert (line["dr_pieces_m"], line["dr_unit_c"]) == (None, None)


def test_fewer_pieces_on_the_dr_work_out_its_total(world):
    o = Order(world)
    _save(world, o, NORTH, pieces="9")
    # 9 pieces at P100.00 = P900.00, P100.00 less than ours.
    assert tuple(_entry(world, o, NORTH)) == ("2026-10-28", 90000)
    rows = world.conn.execute("SELECT po_item_id, pieces_m, unit_c, save_no FROM dr_line_differences").fetchall()
    assert [tuple(r) for r in rows] == [(_item(world, o, NORTH), 9000, None, 1)]
    log = world.conn.execute("SELECT summary FROM audit_logs WHERE action = 'DR_LINES'").fetchone()[0]
    assert log.endswith("supplier's total ₱900.00: SOCK-1 9 pcs (ours 10), -₱100.00")
    row = Order.row(o)
    # Lower than ours: paid as the DR says at once, with no acceptance or sign-off.
    assert row["stage"] == payments.TO_PAY and row["off_c"] == 10000 and row["dr_lower_c"] == 10000 and row["pay_c"] == 190000
    diff = row["line_diffs"][_item(world, o, NORTH)]
    assert (diff["dr_pieces_m"], diff["dr_unit_c"], diff["dr_amount_c"], diff["off_c"]) == (9000, 10000, 90000, -10000)


def test_pieces_and_price_both_differ(world):
    o = Order(world)
    _save(world, o, SOUTH, pieces="9", unit="95.00")
    # 9 x P95.00 = P855.00. The backend total is ours (P1,000.00) plus the product's change (P855.00 - P1,000.00).
    assert _entry(world, o, SOUTH)["total_c"] == 85500
    info = payments.dr_lines(world.conn, world.accounting, o.of_id, _dr(world, o, SOUTH)["id"])
    assert sum(line["dr_amount_c"] for line in info["lines"]) == 85500
    assert payments.dr_total(info, {line["po_item_id"]: (line["dr_pieces_m"], line["dr_unit_c"]) for line in info["lines"]}) == 85500


def test_typing_what_we_have_saves_nothing(world):
    o = Order(world)
    _save(world, o, NORTH, pieces="10", unit="100.00")
    assert _entry(world, o, NORTH) is None
    assert world.conn.execute("SELECT COUNT(*) FROM dr_line_differences").fetchone()[0] == 0


def test_clearing_the_products_takes_the_dr_as_matching_and_keeps_the_history(world):
    o = Order(world)
    _save(world, o, NORTH, pieces="9")
    _save(world, o, NORTH, pieces="", unit="")
    assert _entry(world, o, NORTH) is None
    assert payments.current_lines(world.conn, _dr(world, o, NORTH)["id"]) == {}
    # Nothing is removed: the first set and an empty second set.
    assert [tuple(r) for r in world.conn.execute("SELECT save_no, pieces_m FROM dr_line_differences ORDER BY id")] == [(1, 9000), (2, None)]
    assert world.conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action = 'DR_ENTRY_CLEARED'").fetchone()[0] == 1


def test_the_product_differences_never_change_or_go(world):
    o = Order(world)
    _save(world, o, NORTH, pieces="9")
    with pytest.raises(sqlite3.IntegrityError, match="stays on record"):
        world.conn.execute("UPDATE dr_line_differences SET pieces_m = 1")
    with pytest.raises(sqlite3.IntegrityError, match="stays on record"):
        world.conn.execute("DELETE FROM dr_line_differences")


def test_a_total_typed_on_its_own_replaces_the_products(world):
    o = Order(world)
    _save(world, o, NORTH, pieces="9")
    dr = _dr(world, o, NORTH)["id"]
    payments.save_dr_entries(world.conn, world.accounting, o.of_id, {dr: ("2026-10-28", "980.00")})
    assert _entry(world, o, NORTH)["total_c"] == 98000 and payments.current_lines(world.conn, dr) == {}


def test_a_date_change_alone_keeps_the_products(world):
    o = Order(world)
    _save(world, o, NORTH, pieces="9")
    _save(world, o, NORTH, pieces="9", day="2026-10-27")
    assert tuple(_entry(world, o, NORTH)) == ("2026-10-27", 90000)
    assert world.conn.execute("SELECT MAX(save_no) FROM dr_line_differences").fetchone()[0] == 1


def test_what_cant_be_read_is_refused(world):
    o = Order(world)
    with pytest.raises(POError, match="isn't a number of pieces"):
        _save(world, o, NORTH, pieces="nine")
    with pytest.raises(POError, match="isn't an amount"):
        _save(world, o, NORTH, unit="abc")
    with pytest.raises(POError, match="type the date"):
        _save(world, o, NORTH, pieces="9", day=None)
    with pytest.raises(POError, match="isn't on this DR"):
        payments.save_dr_lines(world.conn, world.accounting, o.of_id, {_dr(world, o, NORTH)["id"]: ("2026-10-28", {999999: ("1", "")})})
    assert _entry(world, o, NORTH) is None


def test_lower_by_products_is_paid_at_once_with_no_sign_off(world):
    o = Order(world)
    _save(world, o, NORTH, pieces="9")
    with pytest.raises(POError, match="no difference to accept"):
        payments.accept_difference(world.conn, world.accounting, o.of_id, "One pair missing from the supplier's DR")
    assert payments.settlement(world.conn, o.of_id)["pay_c"] == 190000  # P2,000.00 - P100.00
    assert next(g for g in payments.checks_due(world.conn) if any(x["id"] == o.of_id for x in g["orders"]))["check_c"] == 190000


def test_higher_by_products_still_waits_for_a_sign_off(world):
    o = Order(world)
    _save(world, o, NORTH, unit="110.00")  # 10 x P110.00 = P1,100.00, P100.00 more than ours
    assert o.row()["stage"] == payments.DIFFERENCE and o.row()["higher_c"] == 10000
    payments.accept_difference(world.conn, world.accounting, o.of_id, "Price went up")
    assert o.row()["stage"] == payments.SIGN_OFF
    payments.sign_off_difference(world.conn, world.approver, o.of_id)
    assert o.row()["stage"] == payments.TO_PAY and payments.settlement(world.conn, o.of_id)["pay_c"] == 200000  # ours is paid


def test_a_product_knows_its_of_line_so_other_drs_can_find_it(world):
    o = Order(world)
    north, south = (payments.dr_lines(world.conn, world.accounting, o.of_id, _dr(world, o, b)["id"])["lines"][0] for b in (NORTH, SOUTH))
    assert north["po_item_id"] != south["po_item_id"] and north["of_item_id"] == south["of_item_id"] is not None


def test_the_new_table_is_in_every_version_28_file(world):
    assert {"dr_line_differences_fixed", "dr_line_differences_no_delete"} <= set(schema.V28_TRIGGERS)
    assert db._expected_columns()["dr_line_differences"] == ["id", "dr_id", "save_no", "po_item_id", "pieces_m", "unit_c", "created_by", "created_at"]


# ---------------------------------------------------------------- the panel

def _open_panel(world, o):
    win = MainWindow(world.conn, world.accounting)
    page = win.payments
    page.tabs.setCurrentIndex(1)  # Checks to make
    page._dr_differs()
    return win, page, page.dr_panel


def _pick(panel, world, o, branch):
    row = next(i for i, d in enumerate(panel.receipts) if d["po_id"] == o.pos[branch]["id"])
    panel.drs.selectRow(row)
    return row


def test_the_drs_open_as_a_panel_on_the_right_and_esc_closes_it(app, world, store):
    o = Order(world)
    o.enter()
    win, page, panel = _open_panel(world, o)
    assert isinstance(panel, QWidget) and not isinstance(panel, QDialog) and page.isAncestorOf(panel)
    assert panel.isVisibleTo(page) and page.split.indexOf(panel) == 1
    assert panel.title.text() == f"DRs for {o.row()['of_number']}"
    assert [panel.drs.horizontalHeaderItem(c).text() for c in range(4)] == ["DR number", "Branch", "Date on the DR", "Supplier's total"]
    assert [panel.lines.horizontalHeaderItem(c).text() for c in range(9)] == ["SKU", "Product", "Counted", "PO unit cost", "PO amount", "DR pieces", "DR unit cost", "DR amount", "Difference"]
    assert all(panel.lines.horizontalHeaderItem(c).toolTip() for c in range(2, 9))
    page.close_shortcut.activated.emit()
    assert not panel.isVisibleTo(page)
    win.close()


def test_typing_a_products_pieces_works_out_the_drs_total_and_saves_it(app, world, store):
    o = Order(world)
    o.enter()
    win, page, panel = _open_panel(world, o)
    row = _pick(panel, world, o, NORTH)
    assert panel.lines.rowCount() == 1 and panel.lines.item(0, 0).text() == "SOCK-1"
    assert [panel.lines.item(0, c).text() for c in (2, 3, 4)] == ["10", "100.00", "1,000.00"]
    panel.lines.item(0, panel.C_DR_PIECES).setText("9")
    assert panel.lines.item(0, panel.C_DR_AMOUNT).text() == "900.00"
    assert panel.drs.item(row, 3).text() == "900.00"
    assert "₱100.00 less than ours" in panel.total.text()
    # Moving to the other DR and back keeps what was typed.
    _pick(panel, world, o, SOUTH)
    assert panel.lines.item(0, panel.C_DR_PIECES).text() == ""
    _pick(panel, world, o, NORTH)
    assert panel.lines.item(0, panel.C_DR_PIECES).text() == "9"
    panel.save_btn.click()
    assert not panel.isVisibleTo(page)
    # South's DR was left alone, so it keeps what it had (Order.enter typed our figure on both).
    assert _entry(world, o, NORTH)["total_c"] == 90000 and _entry(world, o, SOUTH)["total_c"] == 100000
    assert o.row()["stage"] == payments.TO_PAY  # lower: paid as the DR says, no sign-off
    win.close()


def test_a_price_that_cant_be_read_stops_the_save(app, world, store):
    o = Order(world)
    o.enter()
    win, page, panel = _open_panel(world, o)
    _pick(panel, world, o, NORTH)
    panel.lines.item(0, panel.C_DR_UNIT).setText("abc")
    assert "isn't a unit price" in panel.total.text() and not panel.save_btn.isEnabled()
    panel.lines.item(0, panel.C_DR_UNIT).setText("95")
    assert panel.save_btn.isEnabled() and panel.lines.item(0, panel.C_DR_AMOUNT).text() == "950.00"
    win.close()


def test_the_difference_and_sign_off_show_which_product_differs(app, world, store):
    o = Order(world)
    _save(world, o, NORTH, unit="110.00")
    row = o.row()
    dialog = payments_page.DifferenceDialog(None, row, ask_reason=True)
    north = next(p for p in row["pos"] if p["po_id"] == o.pos[NORTH]["id"])
    top = next(dialog.tree.topLevelItem(i) for i in range(dialog.tree.topLevelItemCount()) if dialog.tree.topLevelItem(i).text(0) == north["po_number"])
    child = next(top.child(k) for k in range(top.childCount()) if top.child(k).text(0).startswith("SOCK-1"))
    assert (child.text(5), child.text(6), child.text(7)) == ("1,100.00", "+100.00", "110.00 a unit") and child.font(0).bold()
    dialog.close()
    payments.accept_difference(world.conn, world.accounting, o.of_id, "Price went up")
    win = MainWindow(world.conn, world.approver)
    page = win.payments
    order = page._selected_order()
    assert order["stage"] == payments.SIGN_OFF
    assert "Products that differ on the DR: SOCK-1 ₱110.00 a unit (ours ₱100.00), +₱100.00" in page.breakdown.text()
    win.close()


# ---------------------------------------------------------------- The DR beside the PO, one price on all DRs, the date in full

def test_the_difference_column_shows_how_the_dr_compares_with_the_po(app, world, store):
    o = Order(world)
    o.enter()
    win, page, panel = _open_panel(world, o)
    _pick(panel, world, o, NORTH)
    diff = lambda: panel.lines.item(0, panel.C_DIFFERENCE)
    colour = lambda: diff().foreground().color().name() if diff().data(Qt.ItemDataRole.ForegroundRole) is not None else None
    assert diff().text() == "" and colour() is None  # unchanged: blank
    assert panel.lines.horizontalHeaderItem(panel.C_DIFFERENCE).toolTip().startswith("DR amount less PO amount")
    # Lower than the PO: green, and paid as the DR says.
    panel.lines.item(0, panel.C_DR_PIECES).setText("9")
    assert (diff().text(), colour()) == ("-100.00", theme.current.GREEN.lower())
    assert "Lower: paid as the DR says, no sign-off needed." in panel.total.text()
    # Higher than the PO: red, and it waits for a sign-off.
    panel.lines.item(0, panel.C_DR_PIECES).setText("")
    panel.lines.item(0, panel.C_DR_UNIT).setText("110")
    assert (diff().text(), colour()) == ("+100.00", theme.current.RED.lower())
    assert panel.lines.item(0, panel.C_DR_AMOUNT).text() == "1,100.00" and "Higher:" in panel.total.text()
    # Typed back to what the PO says: blank again, with no colour left behind.
    panel.lines.item(0, panel.C_DR_UNIT).setText("100.00")
    assert (diff().text(), colour()) == ("", None)
    win.close()


def test_same_price_on_all_drs_puts_the_price_on_every_dr_and_saves_them_all(app, world, store):
    o = Order(world)
    o.enter()
    win, page, panel = _open_panel(world, o)
    av_row = _pick(panel, world, o, NORTH)
    ros_row = 1 - av_row
    assert not panel.same_btn.isEnabled()  # nothing typed yet
    panel.lines.setCurrentCell(0, panel.C_DR_PIECES)
    panel.lines.item(0, panel.C_DR_PIECES).setText("9")  # North's own pieces: these stay on North
    assert not panel.same_btn.isEnabled()  # pieces alone: no price to copy
    panel.lines.item(0, panel.C_DR_UNIT).setText("95")
    assert panel.same_btn.isEnabled()
    panel.same_btn.click()
    # South's total shows at once: its own 10 pieces at P95.00. North's: 9 at P95.00.
    assert panel.drs.item(ros_row, 3).text() == "950.00" and panel.drs.item(av_row, 3).text() == "855.00"
    assert panel.same_note.text() == f"₱95.00 put on SOCK-1 on 1 other DR: {panel.receipts[ros_row]['dr_number']}. Save DRs to keep it."
    # Nothing saved yet.
    assert _entry(world, o, SOUTH)["total_c"] == 100000 and world.conn.execute("SELECT COUNT(*) FROM dr_line_differences").fetchone()[0] == 0
    _pick(panel, world, o, SOUTH)
    assert [panel.lines.item(0, c).text() for c in (panel.C_DR_PIECES, panel.C_DR_UNIT, panel.C_DR_AMOUNT, panel.C_DIFFERENCE)] == ["", "95", "950.00", "-50.00"]
    panel.save_btn.click()
    assert not panel.isVisibleTo(page)
    # Both DRs saved: one set of product differences and one audit row for each.
    assert _entry(world, o, NORTH)["total_c"] == 85500 and _entry(world, o, SOUTH)["total_c"] == 95000
    rows = {r["dr_id"]: tuple(r)[1:] for r in world.conn.execute("SELECT dr_id, save_no, pieces_m, unit_c FROM dr_line_differences")}
    assert rows == {_dr(world, o, NORTH)["id"]: (1, 9000, 9500), _dr(world, o, SOUTH)["id"]: (1, None, 9500)}
    assert world.conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action = 'DR_LINES'").fetchone()[0] == 2
    win.close()


def test_same_price_says_so_when_no_other_dr_has_the_product(app, world, store):
    o = Order(world)
    o.enter()
    win, page, panel = _open_panel(world, o)
    _pick(panel, world, o, NORTH)
    panel.lines.setCurrentCell(0, panel.C_DR_UNIT)
    panel.lines.item(0, panel.C_DR_UNIT).setText("95")
    # Pretend South's product came from another OF line.
    ros = _dr(world, o, SOUTH)["id"]
    panel._load(ros)["lines"][0]["of_item_id"] = -1
    panel.same_price_on_all()
    assert panel.same_note.text() == "No other DR of this order has SOCK-1." and ros not in panel.touched
    win.close()


def test_typing_on_other_drs_is_kept_until_saved_even_mid_typing(app, world, store):
    o = Order(world)
    o.enter()
    win, page, panel = _open_panel(world, o)
    win.show()
    _pick(panel, world, o, SOUTH)
    panel.lines.item(0, panel.C_DR_PIECES).setText("8")
    _pick(panel, world, o, NORTH)
    # A cell still being typed in when another DR is picked keeps what was typed.
    item = panel.lines.item(0, panel.C_DR_UNIT)
    panel.lines.setCurrentItem(item)
    panel.lines.editItem(item)
    app.processEvents()
    editor = next(w for w in panel.lines.viewport().findChildren(payments_page.QLineEdit) if not w.isHidden())
    editor.setText("96")
    _pick(panel, world, o, SOUTH)
    assert panel.lines.item(0, panel.C_DR_PIECES).text() == "8"
    _pick(panel, world, o, NORTH)
    assert panel.lines.item(0, panel.C_DR_UNIT).text() == "96"
    panel.save_btn.click()
    assert _entry(world, o, SOUTH)["total_c"] == 80000 and _entry(world, o, NORTH)["total_c"] == 96000
    win.close()


@pytest.mark.parametrize("size", [(1920, 1080), (1366, 768)])
def test_the_date_on_the_dr_shows_in_full(app, world, store, size):
    o = Order(world)
    o.enter()
    win, page, panel = _open_panel(world, o)
    win.resize(*size)
    win.show()
    app.processEvents()
    assert panel.drs.horizontalScrollBar().maximum() == 0  # the whole DR table fits
    for box in panel.dates.values():
        assert box.text() == "10-28-2026"
        assert panel.drs.columnWidth(panel.C_DATE) >= box.width() >= max(box.sizeHint().width(), panel.date_width())
        # The widest date the box can show fits beside its calendar arrow.
        assert box.lineEdit().width() >= box.fontMetrics().horizontalAdvance("88-88-8888")
    # The DR number beside it isn't cut short either.
    number = panel.drs.item(0, 0)
    assert panel.drs.columnWidth(0) >= panel.drs.fontMetrics().horizontalAdvance(number.text()) + 12
    win.close()
