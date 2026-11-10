"""Audit fixes on the OF screen, the Approvals page and the Print page (AUD-001, 006, 007, 008, 011, 012, 014, 017, 033, 050, 051, 052, 155, 180, 182)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

from atlaspo import auth, db, masters, order_forms, orders, pdf
from atlaspo.db import POError
from atlaspo.ui import common, of_page, pages, po_page, theme
from atlaspo.ui.main_window import MainWindow
from atlaspo.ui.of_page import C_DESC

from conftest import NORTH, PASSWORD, SOUTH


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(theme, "_settings", lambda: settings)
    return settings


@pytest.fixture(autouse=True)
def popups(monkeypatch):
    """Every message box is collected, so a test can say none came up."""
    seen = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: seen.append(("information", a[-1])))
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: seen.append(("warning", a[-1])))
    for module in (common, po_page, of_page, pages):
        monkeypatch.setattr(module, "info", lambda parent, title, msg, *a: seen.append(("info", msg)))
        monkeypatch.setattr(module, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(common, "error", lambda parent, msg: seen.append(("error", msg)))
    monkeypatch.setattr(os, "startfile", lambda *a, **k: None, raising=False)
    return seen


def pump(n=8):
    for _ in range(n):
        QApplication.processEvents()


def shown(world, user, size=(1366, 768)):
    w = MainWindow(world.conn, user)
    w.show()
    w.resize(*size)
    pump()
    return w


# What a page gets in a 1366 x 768 window: the sidebar takes the first 300 px. Pages are measured on their own, because the
# offscreen test platform has no Windows fonts and its main window can't be made as narrow as a real one.
LAPTOP = (1066, 768)


def page_alone(page, size=LAPTOP):
    page.resize(*size)
    page.show()
    pump()
    return page


def rows_fit(grid) -> int:
    return grid.viewport().height() // grid.verticalHeader().defaultSectionSize()


def _pending(world, total=150000, by=None):
    of_id = world.draft(total)
    order_forms.submit(world.conn, by or world.buyer, of_id)
    return of_id


# ------------------------------------------------------------------ OF screen layout (AUD-001, AUD-182, AUD-011, AUD-014)

def test_product_keeps_room_on_a_1366_laptop_and_per_pc_steps_into_more_columns(app, world):
    """AUD-001: the Product column collapsed to about 25 px at 1366 x 768."""
    of_id = world.draft(items=[world.item(alloc={NORTH: 2, SOUTH: 1}, description="Bath Towel")])
    # Six branches: on a 1366 laptop there is no room for every column at its usual width.
    for code, name in ((10, "Lakeside"), (12, "Riverside")):
        masters.save_branch(world.conn, world.admin, {"code": code, "name": name})
    page = of_page.OFPage(world.conn, world.buyer)
    page.open_of(of_id)
    page_alone(page)
    per_pc = page.after["Per pc"]
    assert page.grid.columnWidth(C_DESC) >= 120
    # Color keeps its width from before ('Light Blue' read whole), since it tells apart rows of the same stock code.
    assert not page.grid.isColumnHidden(of_page.C_COLOR) and page.grid.columnWidth(of_page.C_COLOR) >= 86
    # Per pc gave way to the product. More columns brings it back.
    assert page.grid.isColumnHidden(per_pc) and page.footer.isColumnHidden(per_pc)
    page.more.setChecked(True)
    pump()
    assert not page.grid.isColumnHidden(per_pc) and page.grid.columnWidth(C_DESC) >= 120
    page.more.setChecked(False)
    pump()
    # The footer's labels sit in the product column, so they still read.
    fm = page.footer.fontMetrics()
    assert page.footer.columnWidth(C_DESC) >= fm.horizontalAdvance("Amount") + 8
    # On a large screen nothing is hidden.
    page.resize(1620, 1009)
    pump()
    assert not page.grid.isColumnHidden(per_pc) and page.grid.columnWidth(C_DESC) > 300
    page.close()


def test_the_grid_shows_six_rows_at_1366_by_768_even_with_the_returned_notice(app, world):
    """AUD-182: 4 rows at 1366 x 768, and 2 (one hidden under the scroll bar) with the red returned notice."""
    of_id = _pending(world)
    order_forms.reject(world.conn, world.admin, of_id, "Cost is too high. Ask for a better price.")
    page = of_page.OFPage(world.conn, world.buyer)
    page.open_of(of_id)
    page_alone(page)
    assert page.message.isVisible() and "Returned by" in page.message.text()
    assert page.height() <= LAPTOP[1] and rows_fit(page.grid) >= 6
    # The test platform's letters are smaller than Windows', so a shorter page stands in for the laptop's real squeeze.
    # The page keeps the height it is given, not stretched taller by what it holds, and the grid still shows six rows: the page scrolls instead.
    page.resize(LAPTOP[0], 520)
    pump()
    assert page.height() <= 520 and rows_fit(page.grid) >= 6
    page.new_of()
    pump()
    assert rows_fit(page.grid) >= 6
    page.close()


def test_inquiry_number_headings_are_not_cut_from_the_left(app, world):
    """AUD-011: 'Cost per UOM' read as 'ost per UOM' in Inquiry's product list."""
    w = shown(world, world.admin)
    items = w.inquiry.items
    header = items.horizontalHeader()
    for col in (6, 7, 8, 9, 10):
        assert items.columnWidth(col) >= header.sectionSizeFromContents(col).width(), items.horizontalHeaderItem(col).text()
    w.close()


def _gap_after(button: QPushButton, siblings) -> int:
    right = [b for b in siblings if b.isVisible() and b is not button and b.x() > button.x() and abs(b.y() - button.y()) < 6]
    nxt = min(right, key=lambda b: b.x())
    return nxt.mapTo(button.window(), nxt.rect().topLeft()).x() - (button.mapTo(button.window(), button.rect().topLeft()).x() + button.width())


def test_red_cancel_of_stands_apart_from_the_everyday_buttons(app, world):
    """AUD-014: Cancel OF sat 6 px from Save draft and from Open and finish."""
    of_id = world.draft()
    w = shown(world, world.buyer)
    w.open_of(of_id)
    pump()
    buttons = [b for b in w.of_page.findChildren(QPushButton) if b.isVisible()]
    cancel = next(b for b in buttons if b.text() == "Cancel OF")
    assert _gap_after(cancel, buttons) >= 20
    w.of_page.back.click()
    pump()
    r = w.orders_page
    r.tree.setCurrentItem(r.item_for(("OF", of_id)))
    pump()
    buttons = [b for b in r.findChildren(QPushButton) if b.isVisible()]
    assert r.cancel_btn.isVisible() and _gap_after(r.cancel_btn, buttons) >= 20
    w.close()


# ------------------------------------------------------------------ messages after submit and approve (AUD-052, AUD-155, AUD-051)

def test_an_of_approved_on_submit_says_it_is_on_print_with_no_box(app, world, popups):
    """AUD-052: the message said 'You can now generate its branch POs' (no such button). AUD-155: and it was a box to press OK on."""
    of_id = world.draft(100)
    w = shown(world, world.buyer)
    w.open_of(of_id)
    pump()
    w.of_page.submit_of()
    page = w.of_page
    assert page.of["status"] == orders.APPROVED
    assert "It is now on Print." in page.message.text() and page.of["of_number"] in page.message.text() and "generate" not in page.message.text().lower()
    assert page.message.isVisible() and not popups
    w.close()


def test_sent_for_approval_is_said_on_the_screen(app, world, popups):
    of_id = world.draft(150000)
    w = shown(world, world.buyer)
    w.open_of(of_id)
    pump()
    w.of_page.submit_of()
    assert w.of_page.of["status"] == orders.PENDING and "Sent for approval" in w.of_page.message.text() and not popups
    w.close()


def test_every_approve_route_asks_the_same_question_passes_the_remarks_and_needs_no_ok(app, world, monkeypatch, popups):
    """AUD-051 and AUD-155: the row's Approve, Approve with remarks and the OF screen's Approve all ask the one shared question
    (common.ask_approve: which OF, from whom, for how much, optional remarks), and nothing needs an OK afterwards."""
    first, second, third = _pending(world), _pending(world), _pending(world)
    asked = []

    def answer(parent, what):
        asked.append(what)
        return "Checked with the supplier"

    monkeypatch.setattr(pages, "ask_approve", answer)
    monkeypatch.setattr(of_page, "ask_approve", answer)
    w = shown(world, world.admin)
    page = w.approvals
    # Approve with remarks, below the list.
    page.ofs.setCurrentItem(next(page.ofs.topLevelItem(i) for i in range(page.ofs.topLevelItemCount()) if page.ofs.topLevelItem(i).data(0, 256) == first))
    page._approve()
    # The OF screen's Approve.
    w.open_of(second)
    pump()
    w.of_page.approve_of()
    assert "It is now on Print." in w.of_page.message.text()
    # The row's own Approve: the same question, then the status line.
    w.stack.setCurrentWidget(page)
    page.refresh()
    page._quick_approve(third)
    assert len(asked) == 3
    for what, of_id in zip(asked, (first, second, third)):
        assert what.startswith(f"draft OF #{of_id}, ABC Trading, ")
    for of_id in (first, second, third):
        assert order_forms.approval_history(world.conn, of_id)[-1]["remarks"] == "Checked with the supplier"
    assert order_forms.get_of(world.conn, third)["status"] == orders.APPROVED
    assert page.done_note.isVisibleTo(page) and "It is now on Print." in page.done_note.text()
    assert not popups
    w.close()


# ------------------------------------------------------------------ Approve only for those who may (AUD-017)

def _row_has_buttons(page, of_id) -> bool:
    tree = page.ofs
    item = next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount()) if tree.topLevelItem(i).data(0, 256) == of_id)
    return tree.itemWidget(item, tree.columnCount() - 1) is not None


def test_an_admin_not_on_the_approver_list_is_not_offered_approve(app, world):
    approver = auth.create_user(world.conn, world.admin, "ana", "Ana Approver", PASSWORD, ["APPROVER"], must_change=False)
    db.set_settings(world.conn, world.admin, {"approval.approver_user_ids": str(approver.id)})
    of_id = _pending(world)
    w = shown(world, world.admin)
    page = w.approvals
    page.refresh()
    page.ofs.setCurrentItem(page.ofs.topLevelItem(0))
    assert not _row_has_buttons(page, of_id) and not page.approve_btn.isVisibleTo(page) and not page.reject_btn.isVisibleTo(page)
    w.open_of(of_id)
    pump()
    texts = {b.text() for b in w.of_page.findChildren(QPushButton) if b.isVisibleTo(w.of_page)}
    assert not {"Approve", "Reject"} & texts
    w.close()
    # The approver on the list still has them all.
    w = shown(world, approver)
    page = w.approvals
    page.ofs.setCurrentItem(page.ofs.topLevelItem(0))
    assert _row_has_buttons(page, of_id) and page.approve_btn.isVisibleTo(page) and page.reject_btn.isVisibleTo(page)
    w.open_of(of_id)
    pump()
    assert "Approve" in {b.text() for b in w.of_page.findChildren(QPushButton) if b.isVisibleTo(w.of_page)}
    w.close()


def test_a_buyer_approver_is_not_offered_approve_on_an_of_they_worked_on(app, world):
    """With self-approval off, BUYER+APPROVER saw row Approve, Approve with remarks and the OF screen's Approve on their own OF."""
    combo = auth.create_user(world.conn, world.admin, "buyappr", "Buy Appr", PASSWORD, ["BUYER", "APPROVER"], must_change=False)
    db.set_settings(world.conn, world.admin, {"approval.approver_user_ids": f"{world.admin.id},{combo.id}"})
    own = order_forms.create_draft(world.conn, combo, world.header(), [world.item(cost=150000 * 100)])
    order_forms.submit(world.conn, combo, own)
    other = _pending(world)
    w = shown(world, combo)
    page = w.approvals
    assert not _row_has_buttons(page, own) and _row_has_buttons(page, other)
    for of_id, expected in ((own, False), (other, True)):
        page.ofs.setCurrentItem(next(page.ofs.topLevelItem(i) for i in range(page.ofs.topLevelItemCount()) if page.ofs.topLevelItem(i).data(0, 256) == of_id))
        assert page.approve_btn.isVisibleTo(page) is expected and page.reject_btn.isVisibleTo(page) is expected
        w.open_of(of_id)
        pump()
        assert ("Approve" in {b.text() for b in w.of_page.findChildren(QPushButton) if b.isVisibleTo(w.of_page)}) is expected
        w.stack.setCurrentWidget(page)
    # Orders' button for the row: their own OF waits for someone else, not 'Review and approve' to a screen with no Approve.
    r = w.orders_page
    w.stack.setCurrentWidget(r)
    r.refresh()
    pump()
    for of_id, expected in ((own, ("Waiting for approval", False)), (other, ("Review and approve", True))):
        r.tree.setCurrentItem(r.item_for(("OF", of_id)))
        pump()
        label, _, allowed = r._next_action()
        assert (label, allowed) == expected
    w.close()


# ------------------------------------------------------------------ Print page (AUD-180, AUD-033, AUD-007, AUD-008, AUD-050, AUD-012)

def _issued(world):
    of_id = world.approved(items=[world.item(cost=12345, alloc={NORTH: 6, SOUTH: 3}, description="Socks", item_code="SOCK-1")])
    for po in order_forms.generate_pos(world.conn, world.buyer, of_id):
        orders.issue(world.conn, world.buyer, po["id"])
    return of_id


def test_print_dr_needs_the_right_to_issue_only_for_the_first_print(app, world, popups):
    """AUD-180: a view-only login could press Print DR and take the original DR. The first print needs the right to issue;
    a DR already printed may be printed again by anyone."""
    _issued(world)
    viewer = auth.create_user(world.conn, world.admin, "vic", "Vic Viewer", PASSWORD, ["VIEWER"], must_change=False)

    def receipts_page(user):
        w = shown(world, user)
        page = w.print_pos
        w.stack.setCurrentWidget(page)
        page.tabs.setCurrentIndex(list(page.TABS).index(page.RECEIPTS))
        page.receipt_table.selectRow(0)
        pump()
        assert page._selected_receipt() is not None
        return w, page

    # Never printed: only someone who may issue gets Print DR (and Another DR).
    for user, expected in ((viewer, False), (world.buyer, True)):
        w, page = receipts_page(user)
        assert not page._selected_receipt()["prints"]
        assert page.print_btn.isVisibleTo(page) is expected and page.another_btn.isVisibleTo(page) is expected
        if not expected:
            with pytest.raises(POError):
                page.print_receipt()
        w.close()

    # Once printed, the viewer may print it again; Another DR still needs the right to issue.
    w, page = receipts_page(world.buyer)
    pdf.delivery_receipt(world.conn, world.buyer, page._selected_receipt()["id"])
    w.close()
    w, page = receipts_page(viewer)
    assert page._selected_receipt()["prints"] == 1
    assert page.print_btn.isVisibleTo(page) and page.print_btn.text() == "Print DR"
    assert not page.another_btn.isVisibleTo(page)
    w.close()


def test_columns_on_an_empty_tab_opens_that_tabs_columns(app, world, monkeypatch, popups):
    """AUD-033: Columns on an empty tab raised StopIteration ('Something went wrong')."""
    opened = []
    monkeypatch.setattr(common.ColumnChooser, "open_under", lambda self, widget: opened.append(self))
    w = shown(world, world.buyer)
    page = w.print_pos
    w.stack.setCurrentWidget(page)
    for tab in (page.TO_PRINT, page.TO_EMAIL, page.RECEIPTS):
        page.tabs.setCurrentIndex(list(page.TABS).index(tab))
        pump()
        assert not page.table.isVisible() and not page.email_table.isVisible() and not page.receipt_table.isVisible()
        page._choose_columns()  # what the Columns button runs
        assert opened[-1] is page.choosers[page._tab_table()]
    assert len(opened) == 3 and not popups
    w.close()


def test_a_search_with_no_match_keeps_the_header_says_so_and_keeps_the_counts(app, world):
    """AUD-007: the header floated down the page. AUD-008: the text said nothing was waiting, and the tab counts fell to 0."""
    world.approved(100)
    world.approved(200)
    w = shown(world, world.buyer)
    page = w.print_pos
    w.stack.setCurrentWidget(page)
    pump()
    before_y, before_tabs = page.search.mapTo(page, page.search.rect().topLeft()).y(), [page.tabs.tabText(i) for i in range(page.tabs.count())]
    page.search.setText("zzqqxx")
    pump()
    assert page.empty.isVisible() and page.empty.text() == "No match for 'zzqqxx'. Clear the search to see all 2."
    assert [page.tabs.tabText(i) for i in range(page.tabs.count())] == before_tabs and before_tabs[0].endswith("2")
    assert abs(page.search.mapTo(page, page.search.rect().topLeft()).y() - before_y) <= 2
    page.search.clear()
    pump()
    assert page.table.isVisible() and page.table.rowCount() == 2
    w.close()


def test_the_new_dr_button_is_called_another_dr(app, world):
    """AUD-050 (naming only; whether a second delivery always needs its own DR is still to be decided)."""
    _issued(world)
    w = shown(world, world.buyer)
    assert w.print_pos.another_btn.text() == "Another DR"
    w.close()


def test_the_print_panel_leaves_the_supplier_readable(app, world):
    """AUD-012: with the panel open at 1366, Supplier fell to 32 px while the always-blank Printed kept 130."""
    world.approved(100)
    page = page_alone(pages.PrintPage(world.conn, world.admin))
    page.table.cellDoubleClicked.emit(0, 1)
    pump(12)
    assert page.panel.isVisibleTo(page)
    assert page.table.isColumnHidden(7) and page.table.columnWidth(3) >= 140
    page._close_panel()
    pump()
    assert not page.table.isColumnHidden(7) and not page.table.isColumnHidden(5)
    page.close()


# ------------------------------------------------------------------ Approvals at the smallest window (AUD-006)

def test_approvals_keeps_product_reason_and_what_readable_at_the_smallest_window(app, world):
    """AUD-006: the product and the rejection reason were cut to a few letters. The fix must not push the row's Approve out of view."""
    for _ in range(2):
        _pending(world)
    rejected = _pending(world)
    order_forms.reject(world.conn, world.admin, rejected, "Cost is 10% above the last order. Ask for a better price.")
    # The offscreen test platform's font runs much wider than Windows' (its page can't even shrink to 986). At 6 pt its widths are
    # close to the real ones at 1286 x 692 and 1366 x 768, so the room on the page is like the real program's.
    base = QFont(app.font())
    small = QFont(base)
    small.setPointSizeF(6)
    app.setFont(small)
    try:
        # The smallest window, 1286 wide, less the sidebar; then 1366.
        page = page_alone(pages.ApprovalsPage(world.conn, world.admin), (986, 640))
        tree = page.ofs
        for width in (986, 1066):
            page.resize(width, 640)
            pump()
            assert page.width() == width
            # The product keeps a readable width, and the row's Approve stays in view: no sideways scroll on the waiting list.
            assert tree.columnWidth(4) >= 100
            assert tree.horizontalScrollBar().maximum() == 0, width
            host = tree.itemWidget(tree.topLevelItem(0), tree.columnCount() - 1)
            approve = next(b for b in host.findChildren(QPushButton) if b.text() == "Approve")
            right = approve.mapTo(tree.viewport(), approve.rect().topRight()).x()
            assert 0 < right <= tree.viewport().width(), (width, right, tree.viewport().width())
        # A cut product, supplier or stock code reads in full on its tooltip.
        top = tree.topLevelItem(0)
        assert all(top.toolTip(c) == top.text(c) != "" for c in (2, 4)) and tree.headerItem().toolTip(3) == "Stock code"
        # With room to spare, the product gets its full 160 and the supplier its full width again.
        page.resize(1500, 640)
        pump()
        assert tree.columnWidth(4) >= 160 and tree.columnWidth(2) == page.product_floor.natural[2]
        page.close()
        # Rejected holds fixed widths sized for Windows' letters; the offscreen letters are no wider than Windows' at 4 pt. Measured on
        # Windows, the list gets 1032 px at 1286 x 692 and 1112 px at 1366 x 768; these page widths give the same lists here.
        smaller = QFont(base)
        smaller.setPointSizeF(4)
        app.setFont(smaller)
        page = page_alone(pages.ApprovalsPage(world.conn, world.admin), (1074, 640))
        page.tabs.setCurrentIndex(1)
        pump()
        table = page.rejected
        assert table.viewport().width() == 1032
        fm = table.fontMetrics()
        # The reason keeps at least 160 on the smallest window (it was 131-147), with the full text on its tooltip, and no sideways scroll.
        assert table.columnWidth(7) >= 158 and table.item(0, 7).toolTip() == table.item(0, 7).text()
        assert table.horizontalScrollBar().maximum() == 0
        assert table.columnWidth(1) >= fm.horizontalAdvance("PO amendment") + 12
        # No other column is cut: each keeps the room its text needs.
        for c in (0, 2, 3, 4, 5, 6, 8):
            texts = [table.horizontalHeaderItem(c).text()] + [table.item(r, c).text() for r in range(table.rowCount())]
            assert table.columnWidth(c) >= max(fm.horizontalAdvance(t) for t in texts) + 12, c
        # On 1366, the reason gets its full 200.
        page.resize(1154, 640)
        pump()
        assert table.columnWidth(7) >= 198 and table.horizontalScrollBar().maximum() == 0
        page.close()
    finally:
        app.setFont(base)
