"""Audit fixes on Debit memos, Item passes and Selling prices (AUD-005, 006, 007, 008, 014, 017, 024, 027, 030, 038, 049, 062, 173)."""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PO_SAMPLE_DIR", tempfile.mkdtemp(prefix="po-sample-"))

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

from atlaspo import auth, db, debit_memos, payments, sample_data
from atlaspo.ui import common, theme
from atlaspo.ui import debit_memo_page as dmp
from atlaspo.ui import claim_stub_page as csp

from conftest import NORTH, PASSWORD, SOUTH, World
from test_payments import Order


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def own_settings(tmp_path, monkeypatch):
    """Column choices and the mode go to a file in the test's folder, never the Windows user's settings."""
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(theme, "_settings", lambda: settings)
    seen = []
    monkeypatch.setattr(common, "error", lambda parent, msg: seen.append(msg))
    monkeypatch.setattr(os, "startfile", lambda *a, **k: None, raising=False)
    return seen


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    folder = tmp_path_factory.mktemp("sample")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PO_DATA_DIR", str(folder))
        sample_data.ensure(folder)
        conn = db.open_database()
        assert str(folder) in str(db.data_dir())
        yield conn
        conn.close()


def _world(conn):
    w = World(conn)
    w.accounting = auth.create_user(conn, w.admin, "lorna", "Lorna", PASSWORD, ["ACCOUNTING"], must_change=False)
    w.approver = auth.create_user(conn, w.admin, "ramon", "Ramon", PASSWORD, ["APPROVER"], must_change=False)
    return w


FAKE_LINE = {"po_item_id": 1, "item_code": "X1", "description": "Test item", "size": "M", "color": "Red", "uom": "PC", "reason": "SHORT",
             "pieces_m": 2000, "net_unit_c": 10000, "cost_c": 10000, "share": (1, 1), "pieces_per_unit": 1, "amount_c": 20000}
PO = {"id": 1, "po_number": "PO-X", "branch_name": "North", "supplier": "SUP"}


def _dialog(cls, svc, conn, user, monkeypatch):
    monkeypatch.setattr(svc, "outstanding", lambda c, u, i: [dict(FAKE_LINE)])
    return cls(None, conn, user, PO)


# ---------------------------------------------------------------- AUD-038 refused cancel

def test_a_refused_debit_memo_cancel_keeps_the_selection_and_changes_nothing(app, conn, monkeypatch, own_settings):
    w = _world(conn)
    o = Order(w, av=8, ros=8)
    o.short_memo(NORTH)
    o.short_memo(SOUTH)
    page = dmp.DebitMemosPage(conn, w.admin)
    page.show()
    page.table.selectRow(1)
    chosen = page._row()["id"]
    emitted = []
    page.changed.connect(lambda: emitted.append(1))
    monkeypatch.setattr(dmp, "ask_text", lambda *a, **k: "")
    page.cancel_btn.click()
    assert own_settings == ["Write the reason for cancelling this debit memo."]
    assert page.table.currentRow() == 1 and page._row()["id"] == chosen
    assert emitted == []
    assert all(r["status"] == debit_memos.ISSUED for r in debit_memos.search(conn, w.admin))
    # A real cancel still goes through, and says so.
    monkeypatch.setattr(dmp, "ask_text", lambda *a, **k: "Came after all")
    page.cancel()  # the button stays locked a moment after a click, so straight to its action
    assert emitted == [1] and debit_memos.get(conn, w.admin, chosen)["status"] == debit_memos.CANCELLED
    page.close()


def test_a_memo_never_printed_offers_print_not_print_again(app, conn):
    """AUD-017: Print again only once it has been printed."""
    w = _world(conn)
    o = Order(w, av=8)
    o.short_memo(NORTH)
    page = dmp.DebitMemosPage(conn, w.buyer)
    page.table.selectRow(0)
    assert page.open_btn.text() == "Print"
    page.print_again()
    page.table.selectRow(0)
    assert page.open_btn.text() == "Print again"


# ---------------------------------------------------------------- AUD-005, AUD-017, AUD-027 the issuing dialog

def test_enter_in_the_memo_dialog_asks_to_approve_instead_of_closing(app, conn, monkeypatch):
    w = _world(conn)
    asked = []
    monkeypatch.setattr(dmp, "confirm", lambda *a, **k: asked.append(a[1]) or False)
    d = _dialog(dmp.DebitMemoDialog, debit_memos, conn, w.buyer, monkeypatch)
    d.show()
    defaults = [b.text() for b in d.findChildren(QPushButton) if b.isDefault()]
    cancel = next(b for b in d.findChildren(QPushButton) if b.text() == "Cancel")
    assert defaults == ["Approve and print"] and not cancel.autoDefault()
    d.remarks.setFocus()
    QTest.keyClicks(d.remarks, "note")
    QTest.keyClick(d.remarks, Qt.Key.Key_Return)
    assert asked == ["Approve debit memo"]  # Enter went to Approve, which asks first
    assert d.isVisible() and d.remarks.text() == "note"  # still open, nothing lost
    d.close()


def test_the_claim_stub_dialog_does_not_promise_a_print(app, conn, monkeypatch):
    from atlaspo import claim_stubs
    w = _world(conn)
    d = _dialog(csp.ClaimStubDialog, claim_stubs, conn, w.buyer, monkeypatch)
    assert not d.print_on_approve and d.approve_btn.text() == "Approve" and d.approve_btn.isDefault()
    d.close()


def _drawn(table, row, col):
    """The colour actually painted near the left edge of a cell, where a number on the right leaves only background."""
    rect = table.visualRect(table.model().index(row, col))
    return table.viewport().grab().toImage().pixelColor(rect.left() + 3, rect.center().y())


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_only_the_pieces_cells_show_the_typing_colour(app, conn, monkeypatch, mode):
    """AUD-027: checked on what is drawn, with the program's style sheet on, since that leaves out a cell's own background unless it is painted."""
    w = _world(conn)
    before, palette, sheet = theme.current, app.palette(), app.styleSheet()
    theme.apply(mode)
    try:
        d = _dialog(dmp.DebitMemoDialog, debit_memos, conn, w.buyer, monkeypatch)
        d.resize(900, 400)
        d.show()
        d.table.clearSelection()
        d.table.setCurrentItem(None)
        app.processEvents()
        pieces = _drawn(d.table, 0, dmp.C_PIECES)
        others = [_drawn(d.table, 0, c) for c in range(d.table.columnCount()) if c != dmp.C_PIECES]
        assert pieces == QColor(theme.current.EDITABLE), (pieces.name(), theme.current.EDITABLE)
        assert all(o != pieces for o in others), [o.name() for o in others]
        assert d.table.item(0, dmp.C_PIECES).flags() & Qt.ItemFlag.ItemIsEditable
        assert not any(d.table.item(0, c).flags() & Qt.ItemFlag.ItemIsEditable for c in range(d.table.columnCount()) if c != dmp.C_PIECES)
        d.close()
    finally:
        theme.current = before
        app.setPalette(palette)
        app.setStyleSheet(sheet)


# ---------------------------------------------------------------- AUD-014 gap before the red button

def test_cancel_debit_memo_stands_apart_from_save_external_number(app, conn):
    w = _world(conn)
    Order(w, av=8).short_memo(NORTH)
    page = dmp.DebitMemosPage(conn, w.admin)
    page.resize(1366, 700)
    page.show()
    page.table.selectRow(0)
    app.processEvents()
    assert page.cancel_btn.isVisible() and page.external_btn.isVisible()
    gap = page.cancel_btn.geometry().left() - page.external_btn.geometry().right()
    assert gap >= 24, gap
    page.close()


# ---------------------------------------------------------------- AUD-062 cancel record

def _paid(w):
    o = Order(w, av=8)
    memo = o.short_memo()
    o.enter()
    group = next(g for g in payments.checks_due(w.conn) if any(x["id"] == o.of_id for x in g["orders"]))
    payments.record_check(w.conn, w.accounting, [x["id"] for x in group["orders"]], "990001", group["due"].isoformat(), f"{group['check_c'] / 100:.2f}")
    return o, memo


def _summary(conn):
    return conn.execute("SELECT summary FROM audit_logs WHERE action = 'DM_CANCEL' ORDER BY id DESC").fetchone()[0]


def test_cancelling_a_paid_memo_records_what_happened_and_the_payback(conn):
    # Overturned the old rule (the money side stayed unchanged): a memo cancelled after payment is paid back on the next check.
    w = _world(conn)
    o, memo = _paid(w)
    before = payments.supplier_balance(conn, w.supplier)
    debit_memos.cancel(conn, w.approver, memo["id"], "supplier proved delivery")
    text = _summary(conn)
    assert text == f"{memo['dm_number']} cancelled: supplier proved delivery. Check 990001 already paid the order; ₱200.00 goes on the supplier's next check"
    assert payments.supplier_balance(conn, w.supplier) == before - memo["total_c"]


def test_one_piece_reads_as_one_piece(conn):
    w = _world(conn)
    memo = Order(w, av=9).short_memo()
    debit_memos.cancel(conn, w.admin, memo["id"], "Wrong")
    assert _summary(conn) == f"{memo['dm_number']} cancelled: Wrong. Its 1 piece counts as short again"


def test_cancelling_after_the_pieces_came_says_they_came(conn):
    w = _world(conn)
    o, memo = _paid(w)
    o.count(NORTH, 10)
    debit_memos.cancel(conn, w.approver, memo["id"], "The missing pieces came after the check")
    text = _summary(conn)
    assert text.endswith("Check 990001 already paid the order; ₱200.00 goes on the supplier's next check. 2 of its pieces were delivered since")


def test_cancelling_an_unpaid_memo_says_the_pieces_are_short_again(conn):
    w = _world(conn)
    memo = Order(w, av=7).short_memo()
    debit_memos.cancel(conn, w.admin, memo["id"], "Wrong")
    assert _summary(conn) == f"{memo['dm_number']} cancelled: Wrong. Its 3 pieces count as short again"


# ---------------------------------------------------------------- AUD-006, AUD-173 room for text

MEASURE = r'''
"""Run on the real Windows platform, because the offscreen one has no fonts and measures text with made-up widths. Temp folders only."""
import json, os, sys, tempfile
from pathlib import Path
os.environ["PO_DATA_DIR"] = tempfile.mkdtemp(prefix="fix-dm-ip-data-")
os.environ["PO_SAMPLE_DIR"] = tempfile.mkdtemp(prefix="fix-dm-ip-sample-")
sys.path.insert(0, sys.argv[1])
from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QApplication, QMessageBox, QStyle, QStyleOptionViewItem
app = QApplication([])
from atlaspo.ui import theme
theme._settings = lambda: QSettings(str(Path(os.environ["PO_DATA_DIR"]) / "ui.ini"), QSettings.Format.IniFormat)
for n in ("information", "warning", "critical", "question"):
    setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
from atlaspo import auth, db, sample_data
from atlaspo.ui import common
from atlaspo.ui.main_window import MainWindow
assert os.environ["PO_DATA_DIR"] in str(db.data_dir())
conn = db.open_database(Path(os.environ["PO_DATA_DIR"]) / "m.sqlite")
sample_data.build(conn)
theme.apply("light")
pump = lambda: [app.processEvents() for _ in range(4)]

def fits(view, row, col):
    index = view.model().index(row, col)
    opt = QStyleOptionViewItem()
    view.itemDelegate().initStyleOption(opt, index)
    opt.rect = view.visualRect(index)
    if not opt.text or opt.rect.width() <= 0:
        return True
    st = view.style()
    # The style sheet pads every table cell 6 px a side (theme.py, QTableWidget::item), on top of the text margin the style keeps. The selected row is bold.
    room = opt.rect.width() - 2 * 6 - 2 * (st.pixelMetric(QStyle.PixelMetric.PM_FocusFrameHMargin, None, view) + 1)
    font = QFont(opt.font)
    font.setBold(font.bold() or view.selectionModel().isRowSelected(row, index.parent()))
    return QFontMetrics(font).horizontalAdvance(opt.text) <= room

out = {"placeholders": [], "cells": [], "date": None}
KEY = {"selling": ("OF number", "Product", "Color", "Price from"), "debit_memos": ("Debit memo", "Date", "Supplier"), "item_passes": ("Item pass", "Date", "Supplier")}
for login in ("owner", "accounting"):
    win = MainWindow(conn, auth.login(conn, login, "123"))
    win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    win.show()
    for size in ((1366, 768), (100, 100)):
        win.resize(*size)
        pump()
        for name, columns in KEY.items():
            page = getattr(win, name, None)
            if page is None or page not in win.nav_rows.values():
                continue
            win.nav.setCurrentRow(next(r for r, p in win.nav_rows.items() if p is page))
            pump()
            page.table.selectRow(0)
            pump()
            if size == (1366, 768):
                for box in [page.search] + ([page.external] if name == "debit_memos" else []):
                    need = box.fontMetrics().horizontalAdvance(box.placeholderText()) + 12 + (20 if box.isClearButtonEnabled() else 0)
                    if box.isVisible() and need > box.width():
                        out["placeholders"].append(f"{box.placeholderText()} needs {need} has {box.width()}")
            t = page.table
            cols = [c for c in range(t.columnCount()) if t.horizontalHeaderItem(c).text() in columns and not t.isColumnHidden(c)]
            out["cells"] += [f"{login} {win.width()} {name}: {t.item(r, c).text()}" for r in range(t.rowCount()) for c in cols if not fits(t, r, c)]
            if name == "debit_memos" and out["date"] is None:
                font = t.font()
                font.setBold(True)
                widest = max(QFontMetrics(font).horizontalAdvance(common.fmt_date(f"2026-{m:02d}-28")) for m in range(1, 13))
                out["date"] = [widest, t.columnWidth(1)]
    win.close()
print("RESULT " + json.dumps(out))
'''


@pytest.fixture(scope="module")
def measured(tmp_path_factory):
    """Text widths as Windows draws them, at 1366 x 768 and at the smallest window the program allows."""
    import json
    import subprocess
    import sys
    from pathlib import Path
    if sys.platform != "win32":
        pytest.skip("measures with the Windows fonts")
    script = tmp_path_factory.mktemp("measure") / "measure.py"
    script.write_text(MEASURE, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("QT_QPA_PLATFORM", "PO_DATA_DIR", "PO_SAMPLE_DIR")}
    env.update(QT_SCALE_FACTOR="1", QT_ENABLE_HIGHDPI_SCALING="0")
    done = subprocess.run([sys.executable, "-E", str(script), str(Path(__file__).resolve().parents[1])], capture_output=True, text=True, env=env, timeout=600)
    line = next((l for l in done.stdout.splitlines() if l.startswith("RESULT ")), None)
    assert line, done.stdout[-2000:] + done.stderr[-2000:]
    return json.loads(line[7:])


def test_placeholders_fit_their_boxes_at_1366(measured):
    """AUD-173: Debit memos search and external number, Item passes search and Selling prices search read whole at a laptop's width."""
    assert measured["placeholders"] == []


def test_the_debit_memo_date_has_room_for_any_date(measured):
    """AUD-006: the widest date, even bold, fits inside the cell's padding (18 px, measured with the style's text rectangle)."""
    widest, width = measured["date"]
    assert widest + 18 <= width, measured["date"]


def test_key_columns_show_whole_at_the_smallest_window(measured):
    """AUD-006: Selling prices OF number, Product, Color and Price from, and the identifying columns of Debit memos and Item passes, aren't cut off."""
    assert measured["cells"] == []


# ---------------------------------------------------------------- AUD-049, AUD-030, AUD-024 Item passes

def test_item_passes_have_an_all_tab_and_dates(app, sample):
    from atlaspo import item_passes
    from atlaspo.ui.common import set_date
    from atlaspo.ui.item_pass_page import ItemPassesPage
    admin = auth.login(sample, "owner", "123")
    page = ItemPassesPage(sample, admin)
    every = item_passes.search(sample, admin)
    texts = [page.tabs.tabText(i) for i in range(page.tabs.count())]
    assert texts[-1] == f"All  {len(every)}" and every
    page.tabs.setCurrentIndex(page.tabs.count() - 1)
    assert page.table.rowCount() == len(every)
    set_date(page.date_from, "2099-01-01")
    assert page.table.rowCount() == 0 and page.tabs.tabText(page.tabs.count() - 1) == "All  0"
    page.clear_filters()
    assert page.table.rowCount() == len(every)
    newest = max(r["ip_date"] for r in every)
    set_date(page.date_from, newest)
    assert page.table.rowCount() == sum(r["ip_date"] >= newest for r in every)
    page.close()


def test_forfeit_looks_like_a_step_that_cant_be_undone_and_collected_is_the_word(app, sample):
    from atlaspo.ui.item_pass_page import ItemPassesPage
    page = ItemPassesPage(sample, auth.login(sample, "owner", "123"))
    assert page.forfeit_btn.objectName() == "Danger"
    # AUD-024: the same word as claim stubs, which say Collected.
    assert page.collected_btn.text() == "Collected"
    assert not any("icked up" in b.text() for b in page.findChildren(QPushButton))
    assert not any("icked up" in page.tabs.tabText(i) for i in range(page.tabs.count()))
    page.close()


# ---------------------------------------------------------------- AUD-007, AUD-008 Selling prices with nothing matching

def test_selling_search_with_no_match_keeps_the_header_and_names_the_search(app, sample):
    from atlaspo.ui.selling_page import SellingPricesPage
    page = SellingPricesPage(sample, auth.login(sample, "owner", "123"))
    page.resize(1366, 700)
    page.show()
    app.processEvents()
    total = page.table.rowCount()
    y = page.search.mapTo(page, page.search.rect().topLeft()).y()
    page.search.setText("zzqqxx")
    app.processEvents()
    assert page.table.isHidden() and page.empty.isVisible()
    assert page.empty.text() == f"Nothing on Bought matches 'zzqqxx'. Clear the search to see all {total}."
    assert page.search.mapTo(page, page.search.rect().topLeft()).y() == y
    page.search.clear()
    page.only_flagged.setChecked(True)
    if page.table.isHidden():
        assert "Untick Only below SRP" in page.empty.text()
    page.close()


# ---------------------------------------------------------------- AUD-030 gap between Forfeit and Collected (changes the module's sample, so it runs last)

def test_forfeit_stands_apart_from_picked_up_on_an_overdue_pass(app, sample):
    from atlaspo import item_passes
    from atlaspo.ui.item_pass_page import ItemPassesPage
    admin = auth.login(sample, "owner", "123")
    ip = next((r for r in item_passes.search(sample, admin) if r["status"] == item_passes.PRINTED), None)
    assert ip, "the sample has an item pass waiting to come back"
    item_passes.arrived(sample, auth.login(sample, "warehouse", "123"), ip["id"], item_passes.as_on_paper(sample, ip["id"]))
    # Back-dated past the 90 days, so the pass is counted and overdue and both buttons show.
    sample.execute("UPDATE item_passes SET arrived_at = '2000-01-01 09:00:00' WHERE id = ?", (ip["id"],))
    page = ItemPassesPage(sample, admin)
    page.resize(1366, 700)
    page.show()
    page.tabs.setCurrentIndex(next(i for i in range(page.tabs.count()) if page.tabs.tabText(i).startswith("At ")))
    app.processEvents()
    page.table.selectRow(next(r for r in range(page.table.rowCount()) if page.table.item(r, 0).text() == ip["ip_number"]))
    app.processEvents()
    assert page.forfeit_btn.isVisible() and page.collected_btn.isVisible()
    gap = page.collected_btn.geometry().left() - page.forfeit_btn.geometry().right()
    assert gap >= 24, gap
    page.close()


def test_a_selling_price_typed_wrong_is_refused_plainly(world):
    """AUD-081 (selling part): text or an absurd number used to crash with TypeError or OverflowError."""
    from atlaspo import selling
    from atlaspo.db import POError
    of_id = world.approved(items=[world.item(cost=3000, alloc={NORTH: 6}, description="Tee", item_code="TEE-1")])
    key = next(r for r in selling.list_items(world.conn, "") if r["description"] == "Tee")["key"]
    for bad, message in ((10**19, "too large"), ("abc", "as a number"), ("1500", "as a number"), (True, "as a number")):
        with pytest.raises(POError, match=message):
            selling.set_price(world.conn, world.buyer, of_id, key, bad)
    assert world.conn.execute("SELECT COUNT(*) FROM selling_prices").fetchone()[0] == 0
    selling.set_price(world.conn, world.buyer, of_id, key, 5900)
    assert world.conn.execute("SELECT price_c FROM selling_prices").fetchone()[0] == 6000


def test_debit_memo_and_claim_stub_take_no_remarks_given_as_none(conn):
    """Remarks given as None used to crash with AttributeError; they are taken as blank, like a PO amendment's."""
    from atlaspo import claim_stubs
    w = _world(conn)
    short = Order(w, av=7)
    po_id = short.pos[NORTH]["id"]
    lines = debit_memos.outstanding(conn, w.buyer, po_id)
    memo = debit_memos.issue(conn, w.buyer, po_id, {(l["po_item_id"], l["reason"]): l["pieces_m"] // 1000 for l in lines}, None)
    assert conn.execute("SELECT remarks FROM debit_memos WHERE id = ?", (memo["id"],)).fetchone()[0] == ""
    damaged = Order(w, damaged="2")
    po_id = damaged.pos[NORTH]["id"]
    lines = claim_stubs.outstanding(conn, w.buyer, po_id)
    stub = claim_stubs.issue(conn, w.buyer, po_id, {(l["po_item_id"], l["reason"]): l["pieces_m"] // 1000 for l in lines}, None)
    assert conn.execute("SELECT remarks FROM claim_stubs WHERE id = ?", (stub["id"],)).fetchone()[0] == ""
