"""Fixes from the audit to claim stubs and damage found later (AUD-066, 002, 018, 040, 184, 010, 030, 017, 027, 005, 014, 006, 173, 074, 118, 140, 147)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QSpacerItem

from atlaspo import auth, claim_stubs as cs
from atlaspo.db import PermissionDenied
from atlaspo.ui import claim_stub_page as page_mod, theme
from atlaspo.ui.claim_stub_page import ClaimStubDialog, ClaimStubsPage, LateDamagePanel, TintDelegate, editable_tint
from atlaspo.ui.debit_memo_page import C_PIECES as DM_PIECES

from conftest import NORTH, PASSWORD, SOUTH
from test_payments import Order


@pytest.fixture
def app():
    yield QApplication.instance() or QApplication([])


def _receiver(world, branch=NORTH):
    return auth.get_user(world.conn, world.conn.execute("SELECT id FROM users WHERE username = ?", (f"r{branch}x1",)).fetchone()[0])


def _pay(world, of_id, number="880001"):
    """A live check on the order, as if accounting recorded it."""
    day = "2026-11-07"
    check = world.conn.execute("INSERT INTO payment_checks(check_number, source, check_date, supplier_id, amount_c, status, recorded_by, recorded_at) VALUES (?, 'EXTERNAL', ?, ?, 100, 'ACTIVE', ?, ?)",
                               (number, day, world.supplier, world.admin.id, day)).lastrowid
    world.conn.execute("INSERT INTO payment_check_ofs(check_id, of_id, amount_c) VALUES (?, ?, 100)", (check, of_id))


def _stubs(world):
    return world.conn.execute("SELECT COUNT(*) FROM claim_stubs").fetchone()[0]


# ------------------------------------------------------------------ AUD-066: ask before numbering

def test_making_a_claim_stub_for_damage_found_later_asks_first_and_go_back_makes_nothing(app, world, monkeypatch):
    o = Order(world)
    of_number = world.conn.execute("SELECT of_number FROM order_forms WHERE id = ?", (o.of_id,)).fetchone()[0]
    asked = []
    monkeypatch.setattr(page_mod, "confirm", lambda parent, title, message, yes="Yes": asked.append((title, message, yes)) or False)
    panel = LateDamagePanel(world.conn, world.buyer)
    panel.start()
    panel.find(of_number)
    assert panel.table.rowCount() == 2  # one line for each branch PO
    for r in range(2):
        panel.table.item(r, panel.C_PIECES).setText("2")
    panel.reason.setText("Torn when unpacked")
    panel.save()
    assert _stubs(world) == 0 and panel.made == []  # Go back: nothing numbered
    title, message, yes = asked[-1]
    assert yes == "Make claim stub" and of_number in message and "2 claim stubs" in message and "400.00" in message
    assert o.pos[NORTH]["po_number"] in message and o.pos[SOUTH]["po_number"] in message
    assert "next check" not in message  # not paid yet
    # Once the order is paid, it says the amount comes off the next check.
    _pay(world, o.of_id)
    panel.save()
    assert "next check" in asked[-1][1] and _stubs(world) == 0
    # Agreeing makes them.
    monkeypatch.setattr(page_mod, "confirm", lambda *a, **k: True)
    panel.table.item(0, panel.C_PIECES).setText("2")
    panel.table.item(1, panel.C_PIECES).setText("2")
    panel.reason.setText("Torn when unpacked")
    panel.save()
    assert _stubs(world) == 2


def test_nothing_typed_goes_straight_to_the_save_message_without_asking(app, world, monkeypatch):
    from atlaspo.db import POError
    o = Order(world)
    asked = []
    monkeypatch.setattr(page_mod, "confirm", lambda *a, **k: asked.append(a) or True)
    panel = LateDamagePanel(world.conn, world.buyer)
    panel.start()
    panel.find(o.pos[NORTH]["po_number"])
    panel.reason.setText("Torn")
    with pytest.raises(POError, match="at least one product"):
        panel.save()
    assert asked == []


# ------------------------------------------------------------------ AUD-002: the panel opens wide enough to read the product

def test_damage_found_later_opens_wide_enough_to_read_the_product(app, world):
    o = Order(world)
    page = ClaimStubsPage(world.conn, world.buyer)
    page.resize(1154, 740)  # what the page gets in a 1366 x 768 window, beside the sidebar
    page.show()
    app.processEvents()
    page._late()
    app.processEvents()
    page.late.find(o.pos[NORTH]["po_number"])
    app.processEvents()
    sizes = page.split.sizes()
    assert sizes[2] >= sum(sizes) // 2 - 2  # about half, not its 420 px minimum
    assert page.late.table.columnWidth(2) >= 150
    product = page.late.table.item(0, 2)
    assert product.text() and product.toolTip() == product.text()
    page.close()


# ------------------------------------------------------------------ AUD-018: a wrong figure is shown as an error

def test_a_wrong_figure_is_red_names_the_branch_and_blocks_the_button(app, world):
    o = Order(world)
    panel = LateDamagePanel(world.conn, world.buyer)
    panel.start()
    panel.find(o.pos[SOUTH]["po_number"])
    cell = panel.table.item(0, panel.C_PIECES)
    cell.setText("abc")
    assert panel.total.objectName() == "Error" and "South" in panel.total.text() and "abc" in panel.total.text()
    assert cell.background().color().name().lower() == theme.current.RED_PALE.lower()
    assert not panel.make_btn.isEnabled()
    cell.setText("99")
    assert panel.total.objectName() == "Error" and "South" in panel.total.text() and not panel.make_btn.isEnabled()
    cell.setText("2")
    assert panel.total.objectName() == "PanelTitle" and "200.00" in panel.total.text() and panel.make_btn.isEnabled()
    assert cell.background().color().name().lower() == editable_tint().name().lower()


# ------------------------------------------------------------------ AUD-040: Branch reports follow the search and dates

def test_branch_reports_follow_the_search_and_the_dates(app, world):
    from atlaspo.ui import common
    o = Order(world)
    cs.send_report(world.conn, _receiver(world, NORTH), o.pos[NORTH]["po_number"], {orders_item(world, o, NORTH): "1"}, "Torn seams")
    cs.send_report(world.conn, _receiver(world, SOUTH), o.pos[SOUTH]["po_number"], {orders_item(world, o, SOUTH): "1"}, "Wet carton")
    page = ClaimStubsPage(world.conn, world.admin)
    reports_tab = list(page.labels).index(page_mod.REPORTS)
    page.tabs.setCurrentIndex(reports_tab)
    assert page.reports.rowCount() == 2
    page.search.setText("wet")
    assert page.reports.rowCount() == 1 and page.reports.item(0, 1).text() == "South"
    assert page.tabs.tabText(reports_tab).endswith("  1")
    page.search.setText(o.pos[NORTH]["po_number"])
    assert page.reports.rowCount() == 1 and page.reports.item(0, 1).text() == "North"
    page.clear_filters()
    assert page.reports.rowCount() == 2
    common.set_date(page.date_from, "2099-01-01")
    assert page.reports.rowCount() == 0
    page.clear_filters()
    assert page.reports.rowCount() == 2


def orders_item(world, o, branch):
    return world.conn.execute("SELECT id FROM purchase_order_items WHERE po_id = ?", (o.pos[branch]["id"],)).fetchone()[0]


# ------------------------------------------------------------------ AUD-184 and AUD-010: tabs and filters fit a narrow window

def test_every_tab_stays_in_view_on_a_narrow_window_without_arrows(app, world):
    page = ClaimStubsPage(world.conn, world.warehouse)
    page.resize(884, 600)
    page.show()
    app.processEvents()
    bar = page.tabs
    assert not bar.usesScrollButtons()
    width = bar.width()
    assert all(bar.tabRect(i).right() <= width for i in range(bar.count()))
    # Short names, and the open tab, are never shortened; the full name is in the tip; the text itself is unchanged.
    for i in range(bar.count()):
        assert bar.tabToolTip(i) == bar.tabText(i)
        if i == bar.currentIndex() or bar.tabSizeHint(i).width() <= bar.LONGEST_SHORT:
            assert bar.minimumTabSizeHint(i).width() == bar.tabSizeHint(i).width()
    assert bar.tabText(bar.count() - 1).startswith("Branch reports") and bar.tabText(0).startswith("Coming back to")
    page.close()


def test_the_page_fits_a_laptop_window(app, world):
    page = ClaimStubsPage(world.conn, world.admin)
    assert page.minimumSizeHint().width() <= 1100
    # The printing buttons share the hint's row, not the search's.
    search_row = next(page.layout().itemAt(i).layout() for i in range(page.layout().count())
                      if page.layout().itemAt(i).layout() is not None and page.layout().itemAt(i).layout().indexOf(page.search) >= 0)
    assert search_row.indexOf(page.print_ticked_btn) < 0 and search_row.indexOf(page.select_btn) < 0


# ------------------------------------------------------------------ AUD-030, AUD-014, AUD-006, AUD-173

def test_forfeit_looks_dangerous_and_red_buttons_stand_apart(app, world):
    page = ClaimStubsPage(world.conn, world.admin)
    assert page.forfeit_btn.objectName() == "Danger" and page.cancel_btn.objectName() == "Danger"
    for b in (page.forfeit_btn, page.cancel_btn):
        row, found = _place(page.list_side.layout(), b)
        before = row.itemAt(found - 1)
        assert isinstance(before, QSpacerItem) and before.sizeHint().width() >= 16


def _place(layout, widget):
    """The layout a widget sits in, and where, searching every nested layout and every widget's own."""
    for i in range(layout.count()):
        item = layout.itemAt(i)
        if item.widget() is widget:
            return layout, i
        inner = item.layout() or (item.widget().layout() if item.widget() is not None else None)
        if inner is not None:
            found = _place(inner, widget)
            if found:
                return found
    return None


def _theme_font():
    """The app's font as the theme sets it (10 pt Segoe UI Variable). Offscreen Qt has no fonts of its own here, so it's loaded from Windows."""
    from PySide6.QtGui import QFont, QFontDatabase, QFontInfo
    path = "C:/Windows/Fonts/SegUIVar.ttf"
    if not os.path.exists(path):
        pytest.skip("needs the Windows font to measure text")
    QFontDatabase.addApplicationFont(path)
    font = QFont("Segoe UI Variable")
    font.setPointSizeF(10)
    assert QFontInfo(font).family().startswith("Segoe UI Variable")
    return font


def test_number_and_days_left_columns_are_wide_enough(app, world):
    from PySide6.QtGui import QFontMetrics
    metrics = QFontMetrics(_theme_font())
    page = ClaimStubsPage(world.conn, world.admin)
    # The number beside its tick box (15 px box, its gap, and the cell's 6 px padding each side).
    assert page.table.columnWidth(0) >= metrics.horizontalAdvance("2026-CS-00007") + 15 + 8 + 12
    assert page.table.columnWidth(10) >= metrics.horizontalAdvance("Waiting for payment") + 12


def test_the_search_hint_fits_its_box(app, world):
    from PySide6.QtGui import QFontMetrics
    metrics = QFontMetrics(_theme_font())
    page = ClaimStubsPage(world.conn, world.admin)
    e = page.search
    # The same measure as the audit: the text, the box's padding, and the clear button.
    assert metrics.horizontalAdvance(e.placeholderText()) + 12 + 20 <= 320
    assert e.minimumWidth() == 320


# ------------------------------------------------------------------ AUD-017: Print or Print again; Approve when nothing prints

def test_a_stub_never_printed_says_print_and_prints_without_the_reprint_question(app, world, monkeypatch):
    o = Order(world, damaged="2")
    stub = o.stub()
    asked, opened = [], []
    monkeypatch.setattr(page_mod, "confirm_reprint", lambda parent, what: asked.append(what) or True)
    monkeypatch.setattr(page_mod, "open_for_printing", lambda docs, name: opened.append(docs))
    page = ClaimStubsPage(world.conn, world.buyer)
    page.table.selectRow(0)
    assert page._row()["id"] == stub["id"] and page.open_btn.text() == "Print"
    page.print_again()
    assert asked == [] and opened and opened[0][0] == world.conn.execute("SELECT pdf FROM claim_stubs WHERE id = ?", (stub["id"],)).fetchone()[0]
    page.table.selectRow(0)
    assert page.open_btn.text() == "Print again"
    page.print_again()
    assert asked == [stub["cs_number"]]


def test_the_claim_stub_dialog_says_approve_and_enter_keeps_what_was_typed(app, world, monkeypatch):
    o = Order(world, damaged="2")
    po = {"id": o.pos[NORTH]["id"], "po_number": o.pos[NORTH]["po_number"], "branch_name": "North", "supplier": "ABC Trading"}
    monkeypatch.setattr("atlaspo.ui.debit_memo_page.confirm", lambda *a, **k: False)
    d = ClaimStubDialog(None, world.conn, world.buyer, po)
    texts = {b.text() for b in d.findChildren(page_mod.QPushButton)}
    assert "Approve" in texts and "Approve and print" not in texts
    assert d.approve_btn.isDefault() and not d.cancel_btn.autoDefault() and not d.cancel_btn.isDefault()
    d.show()
    d.remarks.setFocus()
    QTest.keyClicks(d.remarks, "Crushed box")
    QTest.keyClick(d.remarks, Qt.Key.Key_Return)
    app.processEvents()
    assert d.isVisible() and d.result() == 0 and d.remarks.text() == "Crushed box" and d.issued is None
    assert _stubs(world) == 0
    # AUD-027: the pieces, the one figure that can be typed over, are tinted and the tint is drawn.
    assert d.table.item(0, DM_PIECES).background().color().name().lower() == editable_tint().name().lower()
    assert isinstance(d.table.itemDelegateForColumn(DM_PIECES), TintDelegate)
    d.close()


def test_editable_cells_use_the_theme_color_and_are_drawn(app, world):
    o = Order(world)
    panel = LateDamagePanel(world.conn, world.buyer)
    panel.start()
    panel.find(o.pos[NORTH]["po_number"])
    expected = getattr(theme.current, "EDITABLE", None) or theme.current.ACCENT_PALE
    assert panel.table.item(0, panel.C_PIECES).background().color().name().lower() == expected.lower()
    assert isinstance(panel.table.itemDelegateForColumn(panel.C_PIECES), TintDelegate)
    dialog = page_mod.CountGoodsDialog(None, "2026-CS-00001", [{"id": 1, "item_code": "A", "description": "Socks", "size": "", "color": "", "pieces_m": 1000}])
    assert dialog.table.item(0, 5).background().color().name().lower() == expected.lower()
    assert isinstance(dialog.table.itemDelegateForColumn(5), TintDelegate)


# ------------------------------------------------------------------ AUD-074: the 90 days run from the check

def test_the_documentation_says_the_90_days_run_from_the_check():
    for text in (cs.__doc__, cs.days_left.__doc__, cs.forfeit.__doc__, cs.arrived.__doc__):
        assert "check" in text
    assert "that starts the supplier's 90 days" not in cs.__doc__ and "which starts the supplier's 90 days" not in cs.arrived.__doc__


def test_the_page_hints_say_the_90_days_run_from_the_check(app, world):
    """The screen says the same as the code: arriving at the warehouse doesn't start the 90 days."""
    assert "starts the supplier's 90 days" not in (page_mod.ClaimStubsPage.arrived.__doc__ or "")
    for user in (world.warehouse, world.buyer, world.admin):
        page = ClaimStubsPage(world.conn, user)
        for i in range(page.tabs.count()):
            page.tabs.setCurrentIndex(i)
            app.processEvents()
            hint = page.prompt.text()
            assert "That starts the supplier's 90 days" not in hint
            if page.tab == cs.ISSUED:
                assert "check that took the claim stub off" in hint
        page.close()


# ------------------------------------------------------------------ AUD-118: only the warehouse counts goods in

def test_only_the_warehouse_can_record_goods_counted_in(world):
    o = Order(world, damaged="2")
    stub = o.stub()
    viewer = auth.create_user(world.conn, world.admin, "vic", "Vic", PASSWORD, ["VIEWER"], must_change=False)
    lines = cs._lines(world.conn, stub["id"])
    for who in (viewer, world.buyer, world.admin):
        with pytest.raises(PermissionDenied):
            cs.record_arrival(world.conn, who, "CS", stub["cs_number"], stub["po_id"], lines, cs.as_on_paper(world.conn, stub["id"]))
    assert not world.conn.execute("SELECT 1 FROM goods_arrivals").fetchone()
    cs.arrived(world.conn, world.warehouse, stub["id"], cs.as_on_paper(world.conn, stub["id"]))
    assert cs.is_counted(world.conn, stub["id"])


# ------------------------------------------------------------------ AUD-140: "printed already?" is read inside the write lock

def _count_reads_in_tx(conn, action):
    seen = []
    conn.set_trace_callback(lambda sql: seen.append(conn.in_transaction) if "COUNT(*) FROM audit_logs" in sql and action in sql else None)
    return seen


def test_printed_already_is_read_inside_the_transaction_that_records_the_print(world):
    o = Order(world, damaged="2")
    stub = o.stub()
    seen = _count_reads_in_tx(world.conn, "CS_PRINT")
    first = cs.document(world.conn, world.buyer, stub["id"])
    world.conn.set_trace_callback(None)
    assert seen == [True]
    assert first == world.conn.execute("SELECT pdf FROM claim_stubs WHERE id = ?", (stub["id"],)).fetchone()[0]
    assert cs.document(world.conn, world.buyer, stub["id"]) != first  # the second is marked as a copy
    receiver = _receiver(world, NORTH)
    seen = _count_reads_in_tx(world.conn, "CS_PASS_PRINT")
    cs.print_pass(world.conn, receiver, stub["id"])
    world.conn.set_trace_callback(None)
    assert seen == [True]
    assert world.conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action = 'CS_PASS_PRINT' AND entity_id = ?", (stub["id"],)).fetchone()[0] == 1


# ------------------------------------------------------------------ AUD-147: numbers offered once each, in the same order

def _old_numbers(rows, keys):
    out = []
    for r in rows:
        for number in (r[keys[0]], r[keys[1]]):
            if number and number not in out:
                out.append(number)
    return out


def test_numbers_offered_are_each_listed_once_in_the_same_order(world):
    first, second = Order(world), Order(world)
    numbers = cs.late_numbers(world.conn, world.buyer)
    rows = world.conn.execute("SELECT DISTINCT f.of_number, p.po_number, p.id FROM purchase_orders p LEFT JOIN order_forms f ON f.id = p.of_id WHERE p.status <> 'CANCELLED'"
                              " AND EXISTS (SELECT 1 FROM branch_counts b WHERE b.po_id = p.id AND b.status = 'APPROVED') ORDER BY p.id DESC").fetchall()
    assert numbers == _old_numbers(rows, ("of_number", "po_number"))
    assert len(numbers) == len(set(numbers)) == 6  # two OFs, each with two branch POs
    receiver = _receiver(world, NORTH)
    mine = cs.report_numbers(world.conn, receiver)
    assert mine == [second.pos[NORTH]["po_number"], _of(world, second), first.pos[NORTH]["po_number"], _of(world, first)]


def _of(world, order):
    return world.conn.execute("SELECT of_number FROM order_forms WHERE id = ?", (order.of_id,)).fetchone()[0]


def test_many_numbers_are_listed_quickly(world):
    """20,000 POs: the old list check took seconds (quadratic); listing each once takes a blink."""
    import time

    class Rows:
        def execute(self, *a):
            return self

        def fetchall(self):
            return [{"of_number": f"2026-OF-{n // 4:05d}", "po_number": f"2026-2-{n:05d}", "id": n} for n in range(20000, 0, -1)]

    start = time.perf_counter()
    numbers = cs.late_numbers(Rows(), world.buyer)
    assert time.perf_counter() - start < 1.0
    assert len(numbers) == 20000 + 5001 and numbers[:3] == ["2026-OF-05000", "2026-2-20000", "2026-OF-04999"]
