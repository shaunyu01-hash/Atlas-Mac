"""Second fix round, package approvals-print: AUD-051 (one approve question), AUD-006 (dragged widths kept on Approvals and the OF screen),
AUD-010 (Inquiry filters and Suppliers fit a 1280 x 688 screen at 150%), AUD-180 (a DR reprint works for those the screen offers it to, stamped as a copy)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from io import BytesIO

import pytest
from pypdf import PdfReader
from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QScrollArea

from atlaspo import auth, delivery_receipts as dr, order_forms, orders, pdf
from atlaspo.db import PermissionDenied
from atlaspo.ui import common, of_page, pages, po_page, theme
from atlaspo.ui.main_window import MainWindow
from atlaspo.ui.of_page import C_CODE

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


def _text(data: bytes) -> str:
    return " ".join(" ".join((p.extract_text() or "") for p in PdfReader(BytesIO(data)).pages).split())


def _pending(world, total=150000):
    of_id = world.draft(total)
    order_forms.submit(world.conn, world.buyer, of_id)
    return of_id


def _drag(header, col, width):
    """What a person does: press on the heading, drag the column's edge, let go."""
    QTest.mousePress(header.viewport(), Qt.MouseButton.LeftButton, pos=header.rect().center())
    header.resizeSection(col, width)
    QTest.mouseRelease(header.viewport(), Qt.MouseButton.LeftButton, pos=header.rect().center())
    pump()


# ------------------------------------------------------------------ AUD-051: one approve question everywhere

def test_the_shared_approve_dialog_asks_with_optional_remarks_and_go_back_changes_nothing(app):
    box = common.ApproveDialog(None, "draft OF #7, ABC Trading, ₱1,500.00")
    assert box.windowTitle() == "Approve" and box.question.text() == "Approve draft OF #7, ABC Trading, ₱1,500.00?"
    assert box.back.text() == "Go back" and box.go.text() == "Approve" and box.go.objectName() == "Primary"
    # Enter in the remarks box starts a new line; neither button takes a stray Enter.
    assert not box.go.autoDefault() and not box.back.autoDefault() and not box.go.isDefault()
    box.open()
    box.remarks.setPlainText("  Checked with the supplier  ")
    box.go.click()
    assert box.answer() == "Checked with the supplier"
    empty = common.ApproveDialog(None, "x")
    empty.open()
    empty.go.click()
    assert empty.answer() == ""  # remarks are optional
    back = common.ApproveDialog(None, "x")
    back.open()
    back.remarks.setPlainText("typed")
    back.back.click()
    assert back.answer() is None


def test_the_row_approve_on_approvals_asks_the_shared_question_and_keeps_the_remarks(app, world, monkeypatch, popups):
    """It was a plain Yes/No with no room for remarks; now it is the same question as Approve with remarks and the OF screen."""
    kept, went_back = _pending(world), _pending(world)
    asked = []
    answers = {kept: "Price agreed by phone", went_back: None}

    def answer(parent, what):
        asked.append(what)
        return answers[int(what.split("#")[1].split(",")[0])]

    monkeypatch.setattr(pages, "ask_approve", answer)
    # The old Yes/No must not be what's asked.
    monkeypatch.setattr(pages, "confirm", lambda *a, **k: pytest.fail("the row's Approve asked a plain Yes/No"))
    w = shown(world, world.admin)
    page = w.approvals
    page._quick_approve(went_back)
    assert order_forms.get_of(world.conn, went_back)["status"] == orders.PENDING
    page._quick_approve(kept)
    assert asked[0].startswith(f"draft OF #{went_back}, ABC Trading, ") and asked[1].startswith(f"draft OF #{kept}, ABC Trading, ")
    assert order_forms.get_of(world.conn, kept)["status"] == orders.APPROVED
    assert order_forms.approval_history(world.conn, kept)[-1]["remarks"] == "Price agreed by phone"
    assert "It is now on Print." in page.done_note.text() and not popups
    w.close()


# ------------------------------------------------------------------ AUD-006: dragged widths stay on Approvals and the OF screen

def test_a_width_dragged_on_approvals_stays_across_a_refresh_and_next_time(app, world):
    _pending(world)
    w = shown(world, world.admin)
    page = w.approvals
    w.stack.setCurrentWidget(page)
    pump()
    header = page.ofs.header()
    _drag(header, 1, 177)  # Date
    page.refresh()
    pump()
    assert header.sectionSize(1) == 177
    w.close()
    again = shown(world, world.admin)
    again.stack.setCurrentWidget(again.approvals)
    again.approvals.refresh()
    pump()
    assert again.approvals.ofs.header().sectionSize(1) == 177
    again.close()


def test_a_width_dragged_on_rejected_and_amendments_is_kept_too(app, world):
    rejected = _pending(world)
    order_forms.reject(world.conn, world.admin, rejected, "Too dear")
    w = shown(world, world.admin)
    page = w.approvals
    w.stack.setCurrentWidget(page)
    pump()
    _drag(page.amend.header(), 1, 199)
    page.refresh()
    pump()
    assert page.amend.header().sectionSize(1) == 199
    w.close()
    again = shown(world, world.admin)
    again.stack.setCurrentWidget(again.approvals)
    again.approvals.refresh()
    pump()
    assert again.approvals.amend.header().sectionSize(1) == 199
    again.close()


def test_a_width_dragged_on_the_of_screen_stays_when_the_of_opens_again(app, world):
    of_id = world.draft(items=[world.item(alloc={NORTH: 2, SOUTH: 1}, item_code="SKU-1", description="Polo")])
    w = shown(world, world.buyer)
    w.open_of(of_id)
    pump()
    grid = w.of_page.grid
    _drag(grid.horizontalHeader(), C_CODE, 141)
    assert grid.columnWidth(C_CODE) == 141
    w.open_of(of_id)  # opening it again lays the columns out again
    pump()
    assert grid.columnWidth(C_CODE) == 141 and w.of_page.footer.columnWidth(C_CODE) == 141
    w.close()
    again = shown(world, world.buyer)
    again.open_of(of_id)
    pump()
    assert again.of_page.grid.columnWidth(C_CODE) == 141
    again.close()


def test_column_chooser_still_keeps_and_forgets_widths(app, store):
    table = common.make_table(["Number", "Supplier", "Amount", "Note"])
    chooser = common.smart_columns(table, "fix2-list")
    table.resize(700, 200)
    table.show()
    pump()
    _drag(table.horizontalHeader(), 1, 210)
    assert chooser.kept == {1: 210} and store.value("widths/fix2-list") == "1:210"
    chooser.reset()
    assert chooser.kept == {} and store.value("widths/fix2-list") is None
    table.close()


# ------------------------------------------------------------------ AUD-010: Inquiry's filters and Suppliers fit a small screen

def test_inquiry_all_ofs_filters_take_two_short_rows_so_the_window_need_not_widen(app, world):
    w = shown(world, world.admin)
    inq = w.inquiry
    w.stack.setCurrentWidget(inq)
    one_row = inq.minimumSizeHint().width()
    inq.tabs.setCurrentIndex(list(inq.MODES).index(inq.ALL))
    pump()
    assert inq.more.isVisibleTo(inq)
    # Supplier, Department, Branch on one line; Status, From, To under them.
    assert inq.status.y() > inq.supplier.y() + inq.supplier.height() // 2
    # Within a couple of pixels: the month picker beside To is a combo box, a pixel taller than a date box.
    assert abs(inq.date_to.y() - inq.status.y()) <= 2 and inq.branch.y() == inq.supplier.y()
    # The filters no longer decide the page's width: on Windows they took 1087 px of a 1127 px page.
    # One row could never be narrower than its six boxes side by side; two rows are.
    six = sum(x.minimumSizeHint().width() for x in (inq.supplier, inq.department, inq.branch, inq.status, inq.date_from, inq.date_to))
    assert inq.more.minimumSizeHint().width() < six
    assert inq.minimumSizeHint().width() <= max(one_row, inq.more.minimumSizeHint().width() + 40)
    w.close()


def test_suppliers_details_scroll_so_the_page_is_not_taller_than_a_small_screen(app, world):
    w = shown(world, world.admin)
    page = w.suppliers
    w.stack.setCurrentWidget(page)
    pump()
    assert isinstance(page.details.widget(0), QScrollArea)
    # It needed 684 px, more than a 1280 x 688 screen at 150% has once the title bar is off.
    assert page.minimumSizeHint().height() < 400
    w.close()


# ------------------------------------------------------------------ AUD-180: the DR reprint the screen offers works, and is a copy

def _receipt(world):
    of_id = world.approved(items=[world.item(cost=12345, alloc={NORTH: 6}, description="Socks", item_code="SOCK-1")])
    po = order_forms.generate_pos(world.conn, world.buyer, of_id)[0]
    orders.issue(world.conn, world.buyer, po["id"])
    return dr.get(world.conn, dr.ensure_first(world.conn, world.buyer, po["id"]))


def _dr_prints(world, user_id):
    return world.conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action = 'DR_PRINT' AND user_id = ?", (user_id,)).fetchone()[0]


def test_the_original_dr_needs_issue_rights_but_a_reprint_is_open_to_the_office_and_stamped(world):
    receipt = _receipt(world)
    viewer = auth.create_user(world.conn, world.admin, "vic", "Vic Viewer", PASSWORD, ["VIEWER"], must_change=False)
    accounting = auth.create_user(world.conn, world.admin, "acc", "Acc Ounting", PASSWORD, ["ACCOUNTING"], must_change=False)
    receiver = auth.create_user(world.conn, world.admin, "rec", "Rec Eiver", PASSWORD, ["RECEIVER"], must_change=False, branch_code=NORTH)
    # The original: only someone who may issue POs. Nothing is recorded for a refusal.
    for user in (viewer, accounting, receiver):
        with pytest.raises(PermissionDenied):
            pdf.delivery_receipt(world.conn, user, receipt["id"])
    assert dr.get(world.conn, receipt["id"])["prints"] == 0
    original = _text(pdf.delivery_receipt(world.conn, world.buyer, receipt["id"]))
    assert "PRINTED AGAIN" not in original
    # Printing again: what the Print page offers a view-only login, marked as a copy with the name, and recorded under them.
    copy = _text(pdf.delivery_receipt(world.conn, viewer, receipt["id"]))
    assert "COPY 2. PRINTED AGAIN" in copy and "BY VIC VIEWER" in copy
    assert dr.get(world.conn, receipt["id"])["prints"] == 2 and _dr_prints(world, viewer.id) == 1
    assert "COPY 3. PRINTED AGAIN" in _text(pdf.delivery_receipt(world.conn, accounting, receipt["id"]))
    # A receiver counts blind and never gets the DR, not even a copy.
    with pytest.raises(PermissionDenied):
        pdf.delivery_receipt(world.conn, receiver, receipt["id"])
    assert dr.get(world.conn, receipt["id"])["prints"] == 3 and _dr_prints(world, receiver.id) == 0


def test_print_dr_again_from_the_print_page_works_for_a_view_only_login(app, world, monkeypatch, popups):
    receipt = _receipt(world)
    pdf.delivery_receipt(world.conn, world.buyer, receipt["id"])  # the original, taken by purchasing
    viewer = auth.create_user(world.conn, world.admin, "vic", "Vic Viewer", PASSWORD, ["VIEWER"], must_change=False)
    printed = []
    monkeypatch.setattr(pages, "confirm_reprint", lambda *a, **k: True)
    monkeypatch.setattr(pages, "open_for_printing", lambda documents, name: printed.append((documents, name)))
    w = shown(world, viewer)
    page = w.print_pos
    w.stack.setCurrentWidget(page)
    page.tabs.setCurrentIndex(list(page.TABS).index(page.RECEIPTS))
    page.receipt_table.selectRow(0)
    pump()
    assert page.print_btn.isVisibleTo(page)
    page.print_receipt()  # was refused with a permission message
    assert len(printed) == 1 and printed[0][1] == receipt["dr_number"]
    assert "COPY 2. PRINTED AGAIN" in _text(printed[0][0][0])
    assert dr.get(world.conn, receipt["id"])["prints"] == 2
    w.close()
