"""Owner decisions, Nov 8 (Q32, Q33): the shared page-at-a-time helper for long lists, and the multi-PC bits in ui/common.py."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sqlite3
from datetime import datetime

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QTreeWidgetItem, QVBoxLayout, QWidget

from atlaspo import db
from atlaspo.db import DatabaseBusy, POError
from atlaspo.ui import common


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def messages(monkeypatch):
    said = []
    monkeypatch.setattr(common, "error", lambda parent, text: said.append(text))
    return said


@pytest.fixture
def crashes(monkeypatch):
    shown = []
    monkeypatch.setattr(common.QMessageBox, "critical", lambda *a: shown.append(a))
    return shown


def pump(app, n=5):
    for _ in range(n):
        app.processEvents()


# ------------------------------------------------------------------ Pager

def _host(app, pager):
    host = QWidget()
    QVBoxLayout(host).addWidget(pager)
    host.resize(1366, 60)
    host.show()
    pump(app)
    return host


def test_page_sizes_and_the_default(app):
    assert common.PAGE_SIZES == (50, 100, 200) and common.PAGE_SIZE == 50
    pager = common.Pager(lambda: None)
    assert pager.page_size == 50 and pager.offset == 0 and pager.limit == 50
    assert [pager.size_box.itemData(i) for i in range(pager.size_box.count())] == [50, 100, 200]
    assert pager.prev_button.text() == "\u2039 Previous" and pager.next_button.text() == "Next \u203a"
    assert common.Pager(lambda: None, page_size=100).limit == 100


def test_nothing_is_shown_when_every_row_fits(app):
    pager = common.Pager(lambda: None)
    host = _host(app, pager)
    rows = list(range(40))
    assert pager.take(rows) == rows
    assert pager.isHidden()
    host.close()


def test_pages_next_previous_label_and_reset(app):
    rows = list(range(120))
    calls = []
    pager = common.Pager(lambda: calls.append(pager.take(rows)))
    host = _host(app, pager)
    assert pager.take(rows) == rows[:50]
    assert pager.label.text() == "Page 1 of 3 \u00b7 1\u201350 of 120" and not pager.isHidden()
    assert pager.label.toolTip() == "Search and filters look through everything, not only this page."
    assert not pager.prev_button._wanted and pager.next_button._wanted

    pager.next_button.click()
    assert calls[-1] == rows[50:100] and pager.label.text() == "Page 2 of 3 \u00b7 51\u2013100 of 120"
    assert pager.prev_button._wanted and pager.next_button._wanted
    pager.go(1)  # a second click this soon is ignored by the button's double-click lock, so step on directly
    assert calls[-1] == rows[100:] and pager.label.text() == "Page 3 of 3 \u00b7 101\u2013120 of 120"
    assert not pager.next_button._wanted
    pager.go(1)  # already on the last page: nothing happens
    assert len(calls) == 2
    pager.prev_button.click()
    assert calls[-1] == rows[50:100]

    pager.reset()
    assert pager.take(rows) == rows[:50] and pager.label.text().startswith("Page 1 of 3")
    host.close()


def test_choosing_a_page_size_is_remembered_and_keeps_the_top_row(app):
    rows = list(range(450))
    calls = []
    pager = common.Pager(lambda: calls.append(pager.take(rows)))
    host = _host(app, pager)
    pager.take(rows)
    pager.go(1); pager.go(1)  # page 3: rows 100-149
    assert calls[-1] == rows[100:150]
    pager.size_box.setCurrentIndex(pager.size_box.findData(100))
    assert pager.page_size == 100 and calls[-1] == rows[100:200]  # row 100 is still at the top
    assert pager.label.text() == "Page 2 of 5 \u00b7 101\u2013200 of 450"
    assert common.saved_page_size() == 100 and common.Pager(lambda: None).page_size == 100
    host.close()


def test_a_shrinking_list_never_leaves_an_empty_page(app):
    pager = common.Pager(lambda: None)
    rows = list(range(300))
    pager.take(rows)
    pager.page = 5
    assert pager.take(rows[:120]) == list(range(100, 120)) and pager.label.text().startswith("Page 3 of 3")


def test_show_counts_for_lists_counted_in_the_database(app):
    pager = common.Pager(lambda: None)
    host = _host(app, pager)
    pager.show_counts(50, 1234)
    assert pager.label.text() == "Page 1 of 25 \u00b7 1\u201350 of 1,234" and not pager.isHidden()
    host.close()


def test_take_keeps_the_order_given(app):
    pager = common.Pager(lambda: None, page_size=3)
    assert pager.take(["c", "a", "b", "d"]) == ["c", "a", "b"]
    assert pager.take(iter(["z", "y"])) == ["z", "y"] and pager.isHidden()


def test_a_new_page_starts_at_the_top_of_the_list(app):
    from PySide6.QtWidgets import QTableWidget
    table = QTableWidget(300, 1)
    rows = list(range(300))
    def refill():
        with common.keep_place(table):
            pager.take(rows)
    pager = common.Pager(refill)
    pager.take(rows)
    table.setCurrentCell(10, 0)
    pager.go(1)
    assert table.verticalScrollBar().value() == 0 and not table.selectedItems()


def test_one_line_high_and_fits_a_1366_screen(app):
    pager = common.Pager(lambda: None)
    host = _host(app, pager)
    pager.show_counts(50, 123456)
    pump(app)
    assert pager.sizePolicy().verticalPolicy() == pager.sizePolicy().Policy.Fixed
    assert pager.sizeHint().height() <= 2 * pager.next_button.sizeHint().height()
    assert pager.minimumSizeHint().width() < 1000  # fits beside the sidebar on a 1366 screen
    host.close()


# ------------------------------------------------------------------ keep_place

def test_keep_place_keeps_the_selected_row_and_scroll_in_a_tree(app):
    tree = common.make_tree(["Number", "Supplier"])
    tree.resize(400, 200)

    def fill(n):
        tree.clear()
        for i in range(n):
            item = QTreeWidgetItem([f"PO {i}", "Someone"])
            item.setData(0, Qt.ItemDataRole.UserRole, i)
            tree.addTopLevelItem(item)

    fill(200)
    tree.show()
    pump(app)
    tree.setCurrentItem(tree.topLevelItem(120))
    tree.verticalScrollBar().setValue(90)
    pump(app)
    with common.keep_place(tree):
        fill(400)
    pump(app)
    assert tree.currentItem().data(0, Qt.ItemDataRole.UserRole) == 120
    assert tree.verticalScrollBar().value() == 90
    tree.close()


def test_keep_place_keeps_the_selected_row_and_scroll_in_a_table(app):
    table = common.make_table(["Number", "Supplier"])
    table.resize(400, 200)

    def fill(n):
        common.fill_table(table, [[f"PO {i}", "Someone"] for i in range(n)], keys=list(range(n)))

    fill(200)
    table.show()
    pump(app)
    table.setCurrentCell(150, 0)
    table.verticalScrollBar().setValue(100)
    pump(app)
    with common.keep_place(table):
        fill(400)
    pump(app)
    assert common.selected_key(table) == 150
    assert table.verticalScrollBar().value() == 100
    table.close()


# ------------------------------------------------------------------ print files on a shared folder

@pytest.fixture
def started(monkeypatch):
    opened = []
    monkeypatch.setattr(common.os, "startfile", lambda path: opened.append(path), raising=False)
    monkeypatch.setattr(common.pdf, "combine", lambda documents: b"".join(documents))
    monkeypatch.setattr(common, "computer_name", lambda: "FRONT-DESK")
    return opened


class _Clock(datetime):
    fixed = datetime(2026, 11, 8, 9, 30, 15, 123456)

    @classmethod
    def now(cls, tz=None):
        return cls.fixed


def test_print_names_carry_milliseconds_and_the_computer(app, env, started, monkeypatch):
    monkeypatch.setattr(common, "datetime", _Clock)
    common.open_for_printing([b"one"], "Claim stubs")
    assert started[0].name == "Claim stubs 2026-11-08 093015-123 FRONT-DESK.pdf"
    # The same second and millisecond on this PC again: a new name, the first file untouched.
    common.open_for_printing([b"two"], "Claim stubs")
    assert started[1] != started[0] and started[1].name.startswith("Claim stubs 2026-11-08 093015-123 FRONT-DESK")
    assert started[0].read_bytes() == b"one" and started[1].read_bytes() == b"two"


def test_a_print_file_open_on_another_pc_gets_a_fresh_name(app, env, started, monkeypatch):
    monkeypatch.setattr(common, "datetime", _Clock)
    real_open = open
    refused = []

    def locked_once(path, mode="r", *a, **k):
        if "x" in mode and not refused:
            refused.append(path)
            raise PermissionError(13, "The process cannot access the file because it is being used by another process", str(path))
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr("builtins.open", locked_once)
    common.open_for_printing([b"batch"], "OF and PO batch")
    assert refused and len(started) == 1 and started[0] != refused[0]
    assert started[0].read_bytes() == b"batch"


def test_a_print_folder_that_refuses_everything_says_so(app, env, started, monkeypatch):
    real_open = open

    def locked(path, mode="r", *a, **k):
        if "x" in mode:
            raise PermissionError(13, "denied", str(path))
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr("builtins.open", locked)
    with pytest.raises(POError, match="print file couldn't be saved"):
        common.open_for_printing([b"batch"], "OF and PO batch")
    assert started == []


def test_computer_name_is_safe_for_a_file_name(monkeypatch):
    monkeypatch.setenv("COMPUTERNAME", 'OFFICE/PC:2*')
    assert common.computer_name() == "OFFICE-PC-2-"
    monkeypatch.setenv("COMPUTERNAME", "")
    assert common.computer_name()


# ------------------------------------------------------------------ errors.log on a shared folder

def test_errors_log_names_the_computer(app, env, messages, crashes, monkeypatch):
    monkeypatch.setattr(common, "computer_name", lambda: "BACK-OFFICE")

    def broken():
        raise KeyError("search broke")

    common.guarded(None, broken)
    log = (env / "errors.log").read_text(encoding="utf-8")
    assert log.splitlines()[0].endswith("on BACK-OFFICE") and "search broke" in log
    (env / "errors.log").unlink()

    def locked():
        raise sqlite3.OperationalError("database is locked")

    common.guarded(None, locked)
    log = (env / "errors.log").read_text(encoding="utf-8")
    assert "on BACK-OFFICE" in log and "another computer was saving" in log and len(log.splitlines()) == 1


# ------------------------------------------------------------------ busy while a list refreshes

def test_common_uses_the_databases_own_busy_message():
    assert common.BUSY_MESSAGE == db.BUSY_MESSAGE


def test_busy_during_a_refresh_shows_the_busy_message(app, env, messages, crashes):
    def refresh():
        try:
            raise sqlite3.OperationalError("database is locked")
        except sqlite3.OperationalError as exc:
            raise RuntimeError("the list couldn't be filled") from exc

    assert common.guarded(None, refresh) is None
    assert messages == [db.BUSY_MESSAGE] and crashes == []

    # Outside any button, such as a list refreshing on a timer.
    try:
        refresh()
    except RuntimeError as exc:
        common.excepthook(type(exc), exc, exc.__traceback__)
    try:
        raise sqlite3.OperationalError("database table is locked")
    except sqlite3.OperationalError as exc:
        common.excepthook(type(exc), exc, exc.__traceback__)
    common.excepthook(DatabaseBusy, DatabaseBusy(db.BUSY_MESSAGE), None)
    assert messages == [db.BUSY_MESSAGE] * 4 and crashes == []
    log = (env / "errors.log").read_text(encoding="utf-8")
    assert "Traceback" not in log


def test_other_database_errors_still_show_as_unexpected(app, env, messages, crashes):
    def refresh():
        raise sqlite3.OperationalError("no such table: nothing")

    common.guarded(None, refresh)
    try:
        raise RuntimeError("other") from ValueError("plain")
    except RuntimeError as exc:
        common.excepthook(type(exc), exc, exc.__traceback__)
    assert messages == [] and len(crashes) == 2
