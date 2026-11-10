"""Main window, start-up and the Settings user tools (audit fixes AUD-006, 009, 010, 013, 014, 022, 031, 032, 035, 036, 037, 065, 067, 082, 112, 142)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sqlite3
import sys

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFormLayout, QFrame, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QTabWidget

from atlaspo import auth, backup, db, orders
from atlaspo.db import POError
from atlaspo.ui import common, main_window as mw, of_page as of_module, po_page as po_module, settings_page as sp_module
from atlaspo.ui.main_window import LoginDialog, MainWindow
from atlaspo.ui.of_page import C_COST, C_DEPT, C_DESC

from conftest import NORTH, PASSWORD


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def boxes(monkeypatch):
    """Every message box is collected instead of waiting for a click."""
    seen = []
    for kind in ("critical", "warning", "information"):
        monkeypatch.setattr(QMessageBox, kind, lambda *a, _k=kind, **k: seen.append((_k, a[1] if len(a) > 1 else "", a[2] if len(a) > 2 else "")))
    monkeypatch.setattr(common, "error", lambda parent, text: seen.append(("error", "", text)))
    monkeypatch.setattr(common, "info", lambda parent, title, text: seen.append(("info", title, text)))
    monkeypatch.setattr(sp_module, "info", lambda parent, title, text: seen.append(("info", title, text)))
    monkeypatch.setattr(sp_module, "error", lambda parent, text: seen.append(("error", "", text)))
    return seen


def pump(app, n=4):
    for _ in range(n):
        app.processEvents()


def _type_of(page, world):
    common.select_data(page.supplier, world.supplier)
    page.grid.item(0, C_DEPT).setText("1 Grocery")
    page.grid.item(0, C_DESC).setText("Polo")
    page.grid.item(0, C_COST).setText("100")
    page.grid.item(0, page.branch_cols[NORTH]).setText("10")


# ------------------------------------------------------------------ AUD-142: pages are built when first opened

def test_login_builds_only_the_first_page_and_fills_it_once(app, world, monkeypatch):
    counts = {}
    for cls in (mw.OrdersPage, mw.ApprovalsPage, mw.PrintPage, mw.InquiryPage, mw.SuppliersPage, mw.SellingPricesPage):
        original = cls.refresh

        def counted(self, *a, _orig=original, _name=cls.__name__, **k):
            counts[_name] = counts.get(_name, 0) + 1
            return _orig(self, *a, **k)
        monkeypatch.setattr(cls, "refresh", counted)
    w = MainWindow(world.conn, world.buyer)
    assert w.stack.count() == 1 and w.stack.currentWidget() is w.orders_page
    assert counts == {"OrdersPage": 1}  # filled once, not three times, and nothing else was filled
    assert w._built("approvals") is None and w._built("of_page") is None and w._built("reports") is None
    # The sidebar counts don't need the pages built.
    world.approved(items=[world.item(alloc={NORTH: 1})])
    w.refresh_all()
    assert w.nav.item(w.print_row).text() == "Print  (1)" and w._built("print_pos") is None
    # Another page is built as it's opened, and filled once.
    w.nav.setCurrentRow(w.approvals_row)
    assert w.stack.currentWidget() is w._built("approvals") and counts["ApprovalsPage"] == 1 and w.stack.count() == 2
    w.close()


def test_pages_built_later_still_pass_changes_to_each_other(app, world, monkeypatch):
    w = MainWindow(world.conn, world.admin)
    # A change on a page whose neighbour isn't built yet builds nothing.
    w.claim_stubs.changed.emit()
    assert w._built("received") is None
    received = w.received
    seen = []
    monkeypatch.setattr(received, "refresh", lambda *a: seen.append("received"))
    w.claim_stubs.changed.emit()
    w.central.changed.emit()
    assert seen == ["received", "received"]
    # Orders still opens documents, and the document comes back to Orders.
    of_id = world.draft()
    w.orders_page.open_of.emit(of_id)
    assert w.stack.currentWidget() is w.of_page and w.of_page.of_id == of_id
    w.of_page.closed.emit()
    assert w.stack.currentWidget() is w.orders_page
    # Settings changes still reach the Order Form layout once it's built.
    applied = []
    monkeypatch.setattr(w.of_page, "apply_layout", lambda: applied.append(1))
    w.settings.changed.emit()
    assert applied == [1]
    w.close()


def test_every_page_is_still_reachable_for_every_role(app, world):
    for user in (world.admin, world.buyer, world.warehouse):
        w = MainWindow(world.conn, user)
        for row, page in w.nav_rows.items():
            w.nav.setCurrentRow(row)
            assert w.stack.currentWidget() is page
        w.close()


# ------------------------------------------------------------------ AUD-065, AUD-032: leaving a document through the sidebar

def test_sidebar_and_ctrl_shift_n_ask_before_dropping_a_typed_of(app, world, monkeypatch):
    asked = []
    answer = [False]
    monkeypatch.setattr(of_module, "confirm", lambda *a, **k: (asked.append(a[1]), answer[0])[1])
    w = MainWindow(world.conn, world.buyer)
    orders_row = w.nav.currentRow()
    w.new_of()
    page = w.of_page
    _type_of(page, world)
    assert page._dirty()
    # Sidebar: asked, and Go back keeps the typing and the screen.
    w.nav.setCurrentRow(w.approvals_row)
    assert asked == ["Leave this OF"] and w.stack.currentWidget() is page and w.nav.currentRow() == orders_row
    assert page.grid.item(0, C_DESC).text() == "Polo"
    # Ctrl+Shift+N: asked too, nothing replaced.
    w.new_shortcut.activated.emit()
    assert len(asked) == 2 and page.grid.item(0, C_DESC).text() == "Polo"
    # Leave: the sidebar goes where it was asked.
    answer[0] = True
    w.nav.setCurrentRow(w.approvals_row)
    assert len(asked) == 3 and w.stack.currentWidget() is w.approvals and w.nav.currentRow() == w.approvals_row
    w.close()


def test_sidebar_asks_before_dropping_a_po_amendment(app, world, monkeypatch):
    asked = []
    monkeypatch.setattr(po_module, "confirm", lambda *a, **k: (asked.append(a[1]), False)[1])
    po_id = world.po()
    orders.issue(world.conn, world.buyer, po_id)
    w = MainWindow(world.conn, world.buyer)
    w.open_po(po_id)
    w.po_page.start_amendment()
    w.nav.setCurrentRow(w.approvals_row)
    assert asked == ["Leave this PO"] and w.stack.currentWidget() is w.po_page and w.po_page.amending
    w.close()


def test_clicking_the_highlighted_sidebar_row_leaves_the_document(app, world, monkeypatch):
    monkeypatch.setattr(of_module, "confirm", lambda *a, **k: True)
    of_id = world.draft()
    w = MainWindow(world.conn, world.buyer)
    w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    w.show()
    pump(app)
    row = w.nav.currentRow()
    w.open_of(of_id)
    assert w.stack.currentWidget() is w.of_page and w.nav.currentRow() == row
    item = w.nav.item(row)
    QTest.mouseClick(w.nav.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, w.nav.visualItemRect(item).center())
    pump(app)
    assert w.stack.currentWidget() is w.orders_page
    w.hide()
    w.close()


# ------------------------------------------------------------------ AUD-009, AUD-010: sidebar and window size on a laptop

def test_sidebar_fits_without_scrolling_and_badges_are_short(app, world, monkeypatch):
    monkeypatch.setattr(mw.payment_service, "waiting_signoffs", lambda conn: 2)
    w = MainWindow(world.conn, world.admin)
    assert all(w.nav.item(i).sizeHint().height() <= 26 for i in range(w.nav.count()))
    assert w.nav.item(w.payments_row).text() == "Payments  (2)" and w.nav.item(w.payments_row).toolTip() == "2 to sign off"
    w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    w.show()
    # 689 px is what a maximised window leaves on a 1366 x 768 laptop once the taskbar and title bar are taken off.
    w.resize(1366, 689)
    pump(app)
    assert w.nav.verticalScrollBar().maximum() == 0
    # The first-run warning for a missing Warehouse login is one short line, with the full advice on hover, so it doesn't push entries out of sight.
    w.warehouse_warning.show()
    pump(app)
    assert w.nav.verticalScrollBar().maximum() == 0
    assert w.warehouse_warning.text() == "No Warehouse login yet" and "Settings, Users" in w.warehouse_warning.toolTip()
    # Every sidebar button shows its whole wording. Only checked with real fonts: the offscreen stand-in font is far wider than Windows' own.
    if app.platformName() != "offscreen":
        for b in w.findChild(QFrame, "Sidebar").findChildren(QPushButton):
            assert b.width() >= b.sizeHint().width(), b.text()
    assert w.findChild(QFrame, "Sidebar").findChildren(QPushButton)
    w.hide()
    w.close()


def test_window_minimum_follows_the_page_on_screen(app, world):
    w = MainWindow(world.conn, world.admin)
    w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    w.show()
    pump(app)
    sizes = {}
    for row in sorted(w.nav_rows):
        w.nav.setCurrentRow(row)
        pump(app)
        w.layout().activate()
        sizes[row] = w.minimumSizeHint()
    assert max(s.height() for s in sizes.values()) < 688
    widest = max(sizes, key=lambda r: sizes[r].width())
    narrowest = min(sizes, key=lambda r: sizes[r].width())
    # A wide page no longer keeps the window wide once someone leaves it.
    w.nav.setCurrentRow(widest)
    pump(app)
    w.nav.setCurrentRow(narrowest)
    pump(app)
    w.layout().activate()
    assert w.minimumSizeHint().width() < sizes[widest].width()
    w.hide()
    w.close()


# ------------------------------------------------------------------ AUD-022, AUD-036: login dialog

def test_sample_login_hint_is_fully_visible(app, conn, monkeypatch):
    monkeypatch.setattr(mw.sample_data, "is_sample", lambda c: True)
    d = LoginDialog(conn)
    d.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    d.show()
    pump(app)
    hint = next(l for l in d.findChildren(QLabel) if l.text().startswith("Sample data"))
    assert hint.height() >= hint.heightForWidth(hint.width()) - 1
    d.hide()


def test_login_shows_a_message_for_a_database_error(app, conn, boxes, monkeypatch):
    d = LoginDialog(conn)
    d.username.setText("boss")
    d.password.setText("whatever")

    def broken(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(mw.auth, "login", broken)
    d._login()
    assert d.user is None and d.problem.text() and any(kind == "critical" for kind, _, _ in boxes)
    assert "disk I/O error" in (db.data_dir() / "errors.log").read_text(encoding="utf-8")

    def busy(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(mw.auth, "login", busy)
    d._login()
    assert d.problem.text() == common.BUSY_MESSAGE


# ------------------------------------------------------------------ AUD-037, AUD-067: start-up

def test_errors_outside_buttons_are_logged_and_shown(app, env, boxes, monkeypatch):
    import atlaspo.__main__ as entry
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    entry.install_error_handler()
    assert sys.excepthook is common.excepthook
    box = QLineEdit()

    def broken_search(text):
        raise RuntimeError("search went wrong")
    box.textChanged.connect(broken_search)
    box.setText("abc")
    pump(app)
    assert "search went wrong" in (db.data_dir() / "errors.log").read_text(encoding="utf-8")
    assert any(kind == "critical" and "search went wrong" in text for kind, _, text in boxes)


def test_a_database_from_a_newer_version_is_explained_at_start(app, env, boxes):
    import atlaspo.__main__ as entry
    c = sqlite3.connect(db.data_dir() / "po.sqlite")
    c.execute("PRAGMA user_version = 99")
    c.commit()
    c.close()
    assert entry.Session(app).start() is False
    assert boxes and boxes[0][0] == "critical" and boxes[0][2]


def test_a_damaged_database_is_explained_and_logged_at_start(app, env, boxes):
    import atlaspo.__main__ as entry
    (db.data_dir() / "po.sqlite").write_bytes(b"this is not a sqlite database" * 100)
    session = entry.Session(app)
    assert session.start() is False and session.conn is None
    assert boxes and boxes[0][0] == "critical" and "errors.log" in boxes[0][2]
    assert (db.data_dir() / "errors.log").exists()


def test_any_open_failure_is_caught(app, env, boxes, monkeypatch):
    import atlaspo.__main__ as entry

    def refused():
        raise POError("This database was made by a newer version.")
    monkeypatch.setattr(entry.db, "open_database", refused)
    assert entry.Session(app).start() is False
    assert boxes == [("critical", "Can't open the data", "This database was made by a newer version.")]


# ------------------------------------------------------------------ Settings: AUD-082, 035, 112, 013, 006, 014, 031

def _settings(world):
    return sp_module.SettingsPage(world.conn, world.admin)


def test_vat_rate_must_be_from_0_to_100(app, world, boxes):
    page = _settings(world)
    for bad in ("-5", "150"):
        page.vat_rate.setText(bad)
        with pytest.raises(POError):
            page.save_company()
    page.vat_rate.setText("12")
    page.save_company()
    assert db.get_setting(world.conn, "tax.vat_rate_bp") == "1200"


def test_a_refused_new_user_keeps_the_dialog_open_with_the_typing(app, world, boxes, monkeypatch):
    page = _settings(world)
    tries = []

    def run(dialog):
        dialog.username.setText("buyer")  # already taken
        dialog.full_name.setText("Second Buyer")
        dialog.password.setText("long-enough-pass")
        dialog.roles["BUYER"].setChecked(True)
        dialog.buttons.accepted.emit()
        tries.append((dialog.result(), dialog.username.text(), dialog.full_name.text()))
        dialog.username.setText("buyer2")
        dialog.buttons.accepted.emit()
        tries.append((dialog.result(), dialog.username.text(), dialog.full_name.text()))
        return dialog.result()
    monkeypatch.setattr(sp_module.UserDialog, "exec", run)
    page.new_user()
    assert tries[0] == (0, "buyer", "Second Buyer") and any(kind == "error" for kind, _, _ in boxes)
    assert tries[1][0] == 1 and any(u["username"] == "buyer2" for u in auth.list_users(world.conn))


def test_reset_password_is_hidden_and_kept_exactly(app, world, boxes, monkeypatch):
    page = _settings(world)
    for r in range(page.users.rowCount()):
        if page.users.item(r, 0).text() == "buyer":
            page.users.setCurrentCell(r, 0)
    modes = []
    monkeypatch.setattr(QInputDialog, "getMultiLineText", lambda *a, **k: pytest.fail("a plain text box shows the password"))
    monkeypatch.setattr(QInputDialog, "getText", lambda parent, title, prompt, mode=QLineEdit.EchoMode.Normal, *a, **k: (modes.append(mode), ("  spaced pass  ", True))[1])
    page.reset_password()
    assert modes == [QLineEdit.EchoMode.Password]
    assert auth.login(world.conn, "buyer", "  spaced pass  ").username == "buyer"


def test_users_list_says_inactive(app, world):
    buyer = next(u for u in auth.list_users(world.conn) if u["username"] == "buyer")
    auth.update_user(world.conn, world.admin, buyer["id"], buyer["full_name"], buyer["roles"], False)
    page = _settings(world)
    status = {page.users.item(r, 0).text(): page.users.item(r, 4).text() for r in range(page.users.rowCount())}
    assert status["buyer"] == "Inactive" and status["boss"] == "Active"


def test_backup_list_and_buttons_have_room(app, world):
    backup.manual_backup(world.conn, world.admin)
    page = _settings(world)
    page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    page.resize(1100, 700)
    page.findChild(QTabWidget).setCurrentIndex(4)
    page.show()
    pump(app)
    table = page.backups
    assert table.rowCount() >= 1
    fm = table.fontMetrics()
    assert table.columnWidth(1) >= fm.horizontalAdvance(table.item(0, 1).text()) + 16
    buttons = {b.text(): b for b in page.findChildren(QPushButton)}
    back_up, restore = buttons["Back up now"], buttons["Restore selected"]
    assert restore.mapTo(page, restore.rect().topLeft()).x() - back_up.mapTo(page, back_up.rect().topRight()).x() >= 20
    page.hide()


def test_user_dialog_labels_line_up_like_other_forms(app, world):
    d = sp_module.UserDialog(None, [])
    form = d.findChild(QFormLayout)
    assert form.labelAlignment() & Qt.AlignmentFlag.AlignRight
