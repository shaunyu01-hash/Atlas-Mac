"""Shared dialogs, keyboard, printing and theme (audit fixes AUD-003, 006, 011, 016, 019, 020, 021, 023, 027, 028, 034, 037, 059, 151, 161)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sqlite3

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QEvent, QIODevice, QSettings, Qt, QTimer
from PySide6.QtGui import QColor, QPageSize, QPainter, QPdfWriter
from PySide6.QtPrintSupport import QPrinter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QHBoxLayout, QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
                               QTreeWidgetItem, QWidget)

from atlaspo.db import POError
from atlaspo.ui import common, theme


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Nothing here may touch this PC's saved choices."""
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(theme, "_settings", lambda: settings)
    return settings


@pytest.fixture
def messages(monkeypatch):
    said = []
    monkeypatch.setattr(common, "error", lambda parent, text: said.append(text))
    return said


def pump(app, n=5):
    for _ in range(n):
        app.processEvents()


def contrast(a: str, b: str) -> float:
    def lum(text):
        c = QColor(text)
        return sum(w * (v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4) for w, v in zip((0.2126, 0.7152, 0.0722), (c.redF(), c.greenF(), c.blueF())))
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# ------------------------------------------------------------------ confirm (AUD-003, AUD-023)

def _answer_confirm(app, key=None, click=None):
    seen = {}

    def act():
        box = QApplication.activeModalWidget() or next(w for w in QApplication.topLevelWidgets() if isinstance(w, QMessageBox) and w.isVisible())
        buttons = sorted((b for b in box.findChildren(QPushButton) if b.isVisible()), key=lambda b: b.mapTo(box, b.rect().topLeft()).x())
        seen["order"] = [b.text() for b in buttons]
        seen["primary"] = [b.text() for b in buttons if b.objectName() == "Primary"]
        seen["default"] = box.defaultButton().text() if box.defaultButton() else None
        seen["focus"] = QApplication.focusWidget().text() if isinstance(QApplication.focusWidget(), QPushButton) else None
        if key is not None:
            QTest.keyClick(QApplication.focusWidget() or box, key)
        else:
            next(b for b in buttons if b.text() == click).click()
        QTimer.singleShot(200, lambda: box.isVisible() and (seen.__setitem__("stayed_open", True), box.reject()))

    QTimer.singleShot(100, act)
    result = common.confirm(None, "Restore this backup?", "This replaces everything.", "Restore")
    return result, seen


@pytest.mark.parametrize("key", [Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Escape])
def test_a_stray_enter_or_escape_in_confirm_goes_back(app, key):
    result, seen = _answer_confirm(app, key=key)
    assert result is False and not seen.get("stayed_open")
    assert seen["default"] == "Go back" and seen["focus"] == "Go back"


def test_confirm_has_go_back_then_the_action_in_the_main_color(app):
    result, seen = _answer_confirm(app, click="Restore")
    assert result is True
    assert seen["order"] == ["Go back", "Restore"] and seen["primary"] == ["Restore"]


def test_confirm_action_is_painted_in_the_main_color(app):
    """Judged from the drawn button, not its name: a name given after the box styles it isn't painted."""
    before = (theme.current, app.styleSheet(), app.palette())
    theme.apply(theme.LIGHT_MODE)
    seen = {}

    def act():
        box = QApplication.activeModalWidget()
        go = next(b for b in box.findChildren(QPushButton) if b.text() == "Restore")
        back = next(b for b in box.findChildren(QPushButton) if b.text() == "Go back")
        accent = QColor(theme.current.ACCENT)

        def share(button):
            image = button.grab().toImage()
            hits = sum(image.pixelColor(x, y) == accent for x in range(image.width()) for y in range(image.height()))
            return hits / (image.width() * image.height())

        seen["go"], seen["back"] = share(go), share(back)
        box.reject()

    try:
        QTimer.singleShot(100, act)
        common.confirm(None, "Restore this backup?", "This replaces everything.", "Restore")
    finally:
        theme.current = before[0]
        app.setStyleSheet(before[1])
        app.setPalette(before[2])
    assert seen["go"] > 0.5, seen
    assert seen["back"] < 0.1, seen


def test_confirm_reprint_uses_the_same_order(app):
    seen = {}

    def act():
        box = QApplication.activeModalWidget()
        buttons = sorted((b for b in box.findChildren(QPushButton) if b.isVisible()), key=lambda b: b.mapTo(box, b.rect().topLeft()).x())
        seen["order"] = [b.text() for b in buttons]
        box.reject()

    QTimer.singleShot(100, act)
    assert common.confirm_reprint(None, "2026-PO-00001") is False
    assert seen["order"] == ["Go back", "Print again"]


# ------------------------------------------------------------------ date boxes (AUD-016, AUD-020)

def _date_host(app, date):
    host = QWidget()
    lay = QHBoxLayout(host)
    first, after = QLineEdit(), QLineEdit()
    for w in (first, date, after):
        lay.addWidget(w)
    host.show()
    host.activateWindow()
    pump(app)
    return host, first, after


def test_tab_leaves_a_date_box_that_says_not_set(app):
    d = common.date_edit(None, optional=True)
    host, first, after = _date_host(app, d)
    d.setFocus()
    pump(app)
    assert app.focusWidget() is d and d.is_unset()
    QTest.keyClick(d, Qt.Key.Key_Tab)
    pump(app)
    assert app.focusWidget() is after
    d.setFocus()
    pump(app)
    QTest.keyClick(d, Qt.Key.Key_Backtab)
    pump(app)
    assert app.focusWidget() is first
    assert d.is_unset() and common.date_value(d) is None
    host.close()


def _type(app, keys, start=None, optional=True, refused=None):
    d = common.date_edit(start, optional=optional)
    host, first, after = _date_host(app, d)
    d.setFocus()
    pump(app)
    QTest.keyClicks(d, keys)
    after.setFocus()
    pump(app)
    try:
        value = common.date_value(d)
    except POError as exc:  # a refused date stops a save until the person is back in the box
        if refused is None:
            raise
        refused.append(str(exc))
        d.setFocus()
        pump(app)
        value = common.date_value(d)
    host.close()
    return value


def test_dates_show_four_digit_years(app):
    d = common.date_edit("2026-11-07")
    assert d.displayFormat() == "MM-dd-yyyy" and d.text() == "11-07-2026"


@pytest.mark.parametrize("keys, want", [("110526", "2026-11-05"), ("11052026", "2026-11-05"), ("02292024", "2024-02-29"), ("022924", "2024-02-29"),
                                        ("123126", "2026-12-31"), ("010127", "2027-01-01")])
def test_typed_real_dates_go_in_as_typed(app, messages, keys, want):
    assert _type(app, keys) == want
    assert messages == []


@pytest.mark.parametrize("keys, start, shown", [("02292025", None, "02-29-2025"), ("022925", None, "02-29-2025"), ("04312026", "2026-03-10", "04-31-2026")])
def test_typed_impossible_dates_are_refused_not_changed(app, messages, keys, start, shown):
    refused = []
    assert _type(app, keys, start, refused=refused) == (start if start else None)
    assert len(messages) == 1 and shown in messages[0] and "isn't a real date" in messages[0]
    assert len(refused) == 1 and shown in refused[0] and "nothing was saved" in refused[0]


# ------------------------------------------------------------------ Esc while typing in a cell (AUD-019)

def test_escape_while_typing_in_a_cell_only_cancels_the_typing(app):
    host = QWidget()
    lay = QHBoxLayout(host)
    table = QTableWidget(1, 1)
    table.setItem(0, 0, QTableWidgetItem("5"))
    table.setEditTriggers(QAbstractItemView.EditTrigger.AllEditTriggers)
    lay.addWidget(table)
    went_back = []
    common.escape_shortcut(host, lambda: went_back.append(True))
    host.show()
    host.activateWindow()
    pump(app)
    table.setCurrentCell(0, 0)
    table.editItem(table.item(0, 0))
    pump(app)
    editor = app.focusWidget()
    assert table.state() == QAbstractItemView.State.EditingState and isinstance(editor, QLineEdit)
    editor.setText("9")
    host.escape_shortcut.activated.emit()
    pump(app)
    assert table.state() != QAbstractItemView.State.EditingState
    assert table.item(0, 0).text() == "5" and went_back == []
    # Not typing, Esc goes back as before.
    host.escape_shortcut.activated.emit()
    assert went_back == [True]
    host.close()


# ------------------------------------------------------------------ sorting (AUD-021, AUD-034)

def test_any_year_sorts_as_a_date(app):
    texts = ["Jan 1, 3001", "Dec 31, 1969", "Nov 7, 2026 10:30", "Nov 7, 2026", "Jan 1, 1900", "Dec 31, 9999"]
    assert sorted(texts, key=common.sort_key) == ["Jan 1, 1900", "Dec 31, 1969", "Nov 7, 2026", "Nov 7, 2026 10:30", "Jan 1, 3001", "Dec 31, 9999"]
    tree = common.make_tree(["Date"])
    for text in texts:
        tree.addTopLevelItem(QTreeWidgetItem([text]))
    tree.setProperty("sort_col", 0)
    tree.setProperty("sort_order", "desc")
    common.resort(tree)  # raised OSError for 1969 on Windows before
    assert tree.topLevelItem(0).text(0) == "Dec 31, 9999" and tree.topLevelItem(5).text(0) == "Jan 1, 1900"


@pytest.mark.parametrize("order", ["asc", "desc"])
def test_a_pinned_total_row_stays_last_in_a_table(app, order):
    table = common.make_table(["Supplier", "Amount"], numeric=(1,))
    common.fill_table(table, [["Bravo", "200.00"], ["Alpha", "300.00"], ["Total", "600.00"], ["Charlie", "100.00"]])
    total = next(r for r in range(table.rowCount()) if table.item(r, 0).text() == "Total")
    table.item(total, 0).setData(common.PIN_BOTTOM_ROLE, True)
    for col in (0, 1):
        table.setProperty("sort_col", col)
        table.setProperty("sort_order", order)
        common.resort(table)
        names = [table.item(r, 0).text() for r in range(table.rowCount())]
        assert names[-1] == "Total" and table.item(3, 1).text() == "600.00"
        assert len(names) == 4 and sorted(names[:3]) == ["Alpha", "Bravo", "Charlie"]
    assert names[:3] == (["Charlie", "Bravo", "Alpha"] if order == "asc" else ["Alpha", "Bravo", "Charlie"])


@pytest.mark.parametrize("order", ["asc", "desc"])
def test_a_pinned_total_row_stays_last_in_a_tree(app, order):
    tree = common.make_tree(["Supplier", "Amount"])
    for name, amount in (("Bravo", "2"), ("Total", "9"), ("Alpha", "3")):
        tree.addTopLevelItem(QTreeWidgetItem([name, amount]))
    tree.topLevelItem(1).setData(0, common.PIN_BOTTOM_ROLE, True)
    tree.setProperty("sort_col", 1)
    tree.setProperty("sort_order", order)
    common.resort(tree)
    names = [tree.topLevelItem(i).text(0) for i in range(3)]
    assert names == (["Bravo", "Alpha", "Total"] if order == "asc" else ["Alpha", "Bravo", "Total"])


# ------------------------------------------------------------------ printing (AUD-161, AUD-059)

def _pdf_bytes() -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    writer = QPdfWriter(buffer)
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    painter = QPainter(writer)
    painter.drawText(100, 100, "Item pass")
    painter.end()
    buffer.close()
    return bytes(data)


def _file_printer(path) -> QPrinter:
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(path))
    return printer


def test_a_printer_that_cannot_start_says_nothing_was_printed(app, tmp_path):
    with pytest.raises(POError, match="The printer did not start. Nothing was printed."):
        common.print_once(_file_printer(tmp_path / "no such folder" / "out.pdf"), _pdf_bytes())
    # A working printer still prints.
    common.print_once(_file_printer(tmp_path / "out.pdf"), _pdf_bytes())
    assert (tmp_path / "out.pdf").stat().st_size > 0


def test_orders_already_issued_still_open_when_a_later_one_fails(app, monkeypatch):
    opened = []
    monkeypatch.setattr(common, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(common, "open_for_printing", lambda documents, name: opened.append(list(documents)))
    calls = []

    def render(conn, user, of_id, paper_forms):
        calls.append(of_id)
        if of_id == 2:
            raise RuntimeError("render failed")
        return b"first OF", []

    monkeypatch.setattr(common.pdf, "print_order_form", render)
    with pytest.raises(RuntimeError):
        common.print_order_forms(None, None, None, [1, 2, 3])
    assert calls == [1, 2] and opened == [[b"first OF"]]


# ------------------------------------------------------------------ errors (AUD-151, AUD-037)

def test_a_locked_database_says_another_computer_is_saving(app, env, messages, monkeypatch):
    shown = []
    monkeypatch.setattr(common.QMessageBox, "critical", lambda *a: shown.append(a))

    def save():
        raise sqlite3.OperationalError("database is locked")

    assert common.guarded(None, save) is None
    assert messages == ["Another computer is saving right now. Try again in a moment."] and shown == []
    # One line is kept so repeated waits leave a trace, but no crash details.
    log = (env / "errors.log").read_text(encoding="utf-8")
    assert "another computer was saving" in log and "locked" in log and "Traceback" not in log and len(log.splitlines()) == 1
    (env / "errors.log").unlink()

    def broken():
        raise sqlite3.OperationalError("no such table: nothing")

    common.guarded(None, broken)
    assert len(shown) == 1 and "Traceback" in (env / "errors.log").read_text(encoding="utf-8")


def test_an_error_outside_a_button_is_shown_and_logged(app, env, messages, monkeypatch):
    shown = []
    monkeypatch.setattr(common.QMessageBox, "critical", lambda parent, title, text: shown.append((title, text)))
    try:
        raise KeyError("search broke")
    except KeyError as exc:
        common.excepthook(type(exc), exc, exc.__traceback__)
    assert shown and shown[0][0] == "Something went wrong" and "errors.log" in shown[0][1]
    log = (env / "errors.log").read_text(encoding="utf-8")
    assert "KeyError" in log and "search broke" in log and "test_an_error_outside_a_button" in log
    common.excepthook(POError, POError("That PO is closed."), None)
    assert messages == ["That PO is closed."]


# ------------------------------------------------------------------ columns and headings (AUD-006, AUD-011)

def test_column_widths_a_person_sets_are_kept(app, store):
    def make():
        table = common.make_table(["Number", "Supplier", "Amount", "Note"])
        chooser = common.smart_columns(table, "test-list-ana")
        table.resize(700, 200)
        table.show()
        table.setColumnWidth(1, 90)  # the page's own starting width
        pump(app)
        return table, chooser

    table, chooser = make()
    header = table.horizontalHeader()
    assert header.sectionSize(1) == 90
    QTest.mousePress(header.viewport(), Qt.MouseButton.LeftButton, pos=header.rect().center())
    header.resizeSection(1, 222)
    QTest.mouseRelease(header.viewport(), Qt.MouseButton.LeftButton, pos=header.rect().center())
    pump(app)
    table.close()
    again, _ = make()
    assert again.horizontalHeader().sectionSize(1) == 222
    common.set_widths(again, {1: 80, 2: 70})
    assert again.columnWidth(1) == 222 and again.columnWidth(2) == 70
    _.reset()
    third, _ = make()
    assert third.horizontalHeader().sectionSize(1) == 90
    again.close()
    third.close()


def test_headings_end_in_dots_and_read_only_boxes_show_the_start(app):
    table = common.make_table(["Name", "Cost per UOM"], numeric=(1,))
    tree = common.make_tree(["Name", "Cost per UOM"], numeric=(1,))
    assert table.horizontalHeader().textElideMode() == Qt.TextElideMode.ElideRight
    assert tree.header().textElideMode() == Qt.TextElideMode.ElideRight
    box = QLineEdit()
    box.setReadOnly(True)
    common.show_text(box, "Deliver to the back gate before 10 in the morning, and call ahead")
    assert box.cursorPosition() == 0


# ------------------------------------------------------------------ theme (AUD-027, AUD-028)

@pytest.mark.parametrize("mode", theme.PALETTES)
def test_box_edges_and_the_dimmed_main_button_can_be_seen(mode):
    p = theme.PALETTES[mode]
    for around in (p.PANEL, p.BG):
        assert contrast(p.CONTROL, around) >= 3, (p.CONTROL, around)
    assert contrast(p.PRIMARY_OFF_TEXT, p.PRIMARY_OFF) >= 3


@pytest.mark.parametrize("mode", theme.PALETTES)
def test_cells_to_type_in_stand_out_and_stay_readable(mode, monkeypatch):
    p = theme.PALETTES[mode]
    monkeypatch.setattr(theme, "current", p)
    assert theme.EDITABLE == p.EDITABLE
    # Light reaches 3 to 1. Dark can't reach 3 to 1 while its light text stays at 4.5 to 1, so it goes as near as it can.
    assert contrast(p.EDITABLE, p.PANEL) >= (3 if mode == theme.LIGHT_MODE else 2.9)
    assert contrast(p.TEXT, p.EDITABLE) >= 4.5
    assert contrast(p.EDITABLE, p.BRANCH_TINT) > 2.5  # not mistaken for the old faint tint


def test_a_date_still_being_typed_is_read_as_typed(app, messages):
    """A shortcut can save before the box loses focus: what's read is what was typed, not Qt's half-way guess."""
    d = common.date_edit(None, optional=True)
    host, first, after = _date_host(app, d)
    d.setFocus()
    pump(app)
    QTest.keyClicks(d, "022924")
    assert common.date_value(d) == "2024-02-29" and messages == []
    host.close()


def _host_with_save(app, d):
    """A date box and a Save button whose slot reads the box the way the program's dialogs do."""
    host, first, after = _date_host(app, d)
    saved = []
    save = QPushButton("Save", host)
    host.layout().addWidget(save)
    save.clicked.connect(lambda: common.guarded(host, lambda: saved.append(common.date_value(d))))
    host.show()
    pump(app)
    return host, save, saved


def test_an_impossible_date_then_save_saves_nothing(app, messages):
    """02-29-2025 typed over 11-07-2026 and Save clicked straight away: not the old date saved and a message after, but nothing saved."""
    d = common.date_edit("2026-11-07")
    host, save, saved = _host_with_save(app, d)
    d.setFocus()
    pump(app)
    QTest.keyClicks(d, "02292025")
    QTest.mouseClick(save, Qt.MouseButton.LeftButton)
    pump(app, 10)
    assert saved == []
    assert len(messages) == 1 and "02-29-2025" in messages[0] and "nothing was saved" in messages[0]
    save.click()  # still refused until it's typed again
    pump(app, 10)
    assert saved == [] and len(messages) == 2
    d.setFocus()
    pump(app)
    QTest.keyClicks(d, "11052026")
    QTest.mouseClick(save, Qt.MouseButton.LeftButton)
    pump(app, 10)
    assert saved == ["2026-11-05"] and len(messages) == 2
    host.close()


def test_a_save_shortcut_while_an_impossible_date_is_typed_saves_nothing(app, messages):
    d = common.date_edit("2026-11-07")
    host, save, saved = _host_with_save(app, d)
    save.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # like a shortcut: the box keeps the focus
    d.setFocus()
    pump(app)
    QTest.keyClicks(d, "02292025")
    save.click()
    pump(app, 10)
    assert saved == [] and len(messages) == 1 and "nothing was saved" in messages[0]
    host.close()


def test_a_refused_date_is_forgotten_when_the_page_sets_the_box(app, messages):
    d = common.date_edit("2026-11-07")
    host, save, saved = _host_with_save(app, d)
    d.setFocus()
    pump(app)
    QTest.keyClicks(d, "02292025")
    QTest.mouseClick(save, Qt.MouseButton.LeftButton)
    pump(app, 10)
    common.set_date(d, "2026-12-01")
    save.click()
    pump(app, 10)
    assert saved == ["2026-12-01"]
    host.close()


def test_a_one_digit_year_is_not_guessed(app, messages):
    refused = []
    assert _type(app, "11052", "2026-03-10", optional=False, refused=refused) == "2026-03-10"
    assert len(refused) == 1 and len(messages) == 1 and "11-05-2" in messages[0] and "isn't a real date" in messages[0]
