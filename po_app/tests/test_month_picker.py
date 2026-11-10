"""A whole month in one pick, instead of typing From and To."""
from __future__ import annotations

import pytest
from PySide6.QtCore import QDate
from PySide6.QtWidgets import QApplication

from atlaspo.ui.common import date_edit, date_value, month_picker, set_date


@pytest.fixture
def app():
    yield QApplication.instance() or QApplication([])


def test_a_month_fills_from_and_to_in_one_refresh(app):
    a, b = date_edit(optional=True), date_edit(optional=True)
    refreshes = []
    a.dateChanged.connect(lambda *_: refreshes.append((date_value(a), date_value(b))))
    b.dateChanged.connect(lambda *_: refreshes.append((date_value(a), date_value(b))))
    box = month_picker(a, b)
    assert box.currentText() == "Month" and box.count() > 12
    first = QDate.currentDate()
    last_month = QDate(first.year(), first.month(), 1).addMonths(-1)
    index = box.findData(last_month)
    box.setCurrentIndex(index)
    box.activated.emit(index)
    end = last_month.addMonths(1).addDays(-1)
    assert (date_value(a), date_value(b)) == (last_month.toString("yyyy-MM-dd"), end.toString("yyyy-MM-dd"))
    assert refreshes == [(date_value(a), date_value(b))]
    # Typing a date by hand puts it back to Whole month; clearing does too.
    set_date(a, last_month.addDays(3).toString("yyyy-MM-dd"))
    assert box.currentText() == "Month"
    set_date(a, last_month.toString("yyyy-MM-dd"))
    assert box.currentData() == last_month  # the dates make up that month again
    set_date(a, None)
    set_date(b, None)
    assert box.currentText() == "Month"


def test_every_date_filter_has_a_month_picker(app, world):
    from atlaspo.ui.main_window import MainWindow
    win = MainWindow(world.conn, world.admin)
    for page in (win.debit_memos, win.claim_stubs):
        assert page.month.count() > 12
    win.close()
