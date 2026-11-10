"""Audit fixes on the Orders and Inquiry lists and Orders' Go to Print (AUD-007, 008, 013, 049, 099, 144, 145, 158, 173, 187)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtGui import QShortcut
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

from atlaspo import auth, order_forms, orders, receiving, warehouse
from atlaspo.ui import common, of_page, pages, theme
from atlaspo.ui.common import set_date
from atlaspo.ui.main_window import MainWindow

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
    for module in (common, of_page, pages):
        monkeypatch.setattr(module, "info", lambda parent, title, msg, *a: seen.append(("info", msg)))
        monkeypatch.setattr(module, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(common, "error", lambda parent, msg: seen.append(("error", msg)))
    monkeypatch.setattr(os, "startfile", lambda *a, **k: None, raising=False)
    return seen


def pump(n=6):
    for _ in range(n):
        QApplication.processEvents()


def page_alone(page, size=(1066, 768)):
    page.resize(*size)
    page.show()
    pump()
    return page


def _type(box, text):
    box.setFocus()
    for ch in text:
        QTest.keyClick(box, ch)


# ---------------------------------------------------------------- AUD-145 search boxes wait for a pause

@pytest.mark.parametrize("make, boxes", [(of_page.OrdersPage, ("search",)), (of_page.InquiryPage, ("search", "product"))])
def test_typing_waits_for_a_pause_before_refreshing(app, world, monkeypatch, make, boxes):
    world.approved()
    page = page_alone(make(world.conn, world.admin))
    # The timer calls the refresh it was given at build time; the box looks refresh up when it changes, so that one is counted as "now".
    calls = []
    page.search_timer.timeout.connect(lambda: calls.append("timer"))
    original = page.refresh
    monkeypatch.setattr(page, "refresh", lambda *a: (calls.append("now"), original(*a))[1])
    for name in boxes:
        box = getattr(page, name)
        calls.clear()
        _type(box, "ABC")
        assert calls == [], f"{name}: refreshed on a key press: {calls}"
        assert page.search_timer.isActive()
        QTest.qWait(450)
        # One refresh once typing pauses (the timer's own, which may reach the counted refresh too).
        assert calls.count("timer") == 1 and calls.count("now") <= 1, f"{name}: {calls}"
        # Text the program puts in shows at once.
        calls.clear()
        box.setText("")
        assert calls == ["now"]


def test_typed_search_still_finds_the_order(app, world):
    of_id = world.approved()
    number = order_forms.get_of(world.conn, of_id)["of_number"] or ""
    page = page_alone(of_page.OrdersPage(world.conn, world.admin))
    _type(page.search, "zzqq")
    QTest.qWait(450)
    assert page.tree.topLevelItemCount() == 0
    page.search.clear()
    pump()
    assert page.item_for(("OF", of_id)) is not None
    if number:
        _type(page.search, number)
        QTest.qWait(450)
        assert page.item_for(("OF", of_id)) is not None


# ---------------------------------------------------------------- AUD-144 the branch filter runs once per Orders refresh

BRANCH_SQL = "f.id IN (SELECT i.of_id FROM order_form_allocations"


def _rows(page):
    return [(page.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole), page.tree.topLevelItem(i).text(page.C_STATUS))
            for i in range(page.tree.topLevelItemCount())]


def test_orders_branch_filter_runs_once_and_lists_the_same(app, world):
    a = world.approved(items=[world.item(alloc={NORTH: 3})])
    r = world.approved(items=[world.item(alloc={SOUTH: 2})])
    d = world.draft(items=[world.item(alloc={NORTH: 1, SOUTH: 1})])
    page = page_alone(of_page.OrdersPage(world.conn, world.admin))
    seen = []
    world.conn.set_trace_callback(seen.append)
    try:
        page.branch.setCurrentIndex(page.branch.findData(NORTH))
        pump()
    finally:
        world.conn.set_trace_callback(None)
    assert sum(BRANCH_SQL in s for s in seen) == 1, sum(BRANCH_SQL in s for s in seen)
    ids = {k[1] for k, _ in _rows(page) if k[0] == "OF"}
    assert ids == {a, d} and r not in ids
    # The counts and the amount still read as the service works them out.
    counts = order_forms.counts(world.conn, branch_code=NORTH)
    assert page.count_buttons[None].text().endswith(str(counts["by_tab"][orders.OPEN]))
    assert page.count_buttons[orders.DRAFT].text().endswith(str(counts["open_by_status"][orders.DRAFT]))
    # A status chip narrows it as before.
    page._set_status(orders.DRAFT)
    assert {k[1] for k, _ in _rows(page) if k[0] == "OF"} == {d}


# ---------------------------------------------------------------- AUD-049 Clear filters on Orders, Ctrl+O on Inquiry

def test_orders_clear_filters_puts_every_filter_back(app, world):
    world.approved()
    page = page_alone(of_page.OrdersPage(world.conn, world.admin))
    assert "Clear filters" in {b.text() for b in page.findChildren(QPushButton) if b.isVisibleTo(page)}
    page.search.setText("zzqqxx")
    page.branch.setCurrentIndex(page.branch.findData(SOUTH))
    page.department.setCurrentIndex(1)
    set_date(page.date_from, "2030-01-01")
    page._set_status(orders.DRAFT)
    page.count_buttons[orders.DRAFT].setChecked(True)
    assert page.tree.topLevelItemCount() == 0
    next(b for b in page.findChildren(QPushButton) if b.text() == "Clear filters").click()
    pump()
    assert page.search.text() == "" and page.branch.currentData() is None and page.department.currentData() is None and page.year.currentData() is None
    assert page.date_from.date() == page.date_from.minimumDate() or not of_page.date_value(page.date_from)
    assert page.status_filter is None and page.count_buttons[None].isChecked()
    assert page.tree.topLevelItemCount() == 1


def test_inquiry_has_ctrl_o(app, world, monkeypatch):
    world.po()
    page = page_alone(of_page.InquiryPage(world.conn, world.admin))
    assert "Ctrl+O" in {s.key().toString() for s in page.findChildren(QShortcut)}
    page.show_completed()
    pump()
    opened = []
    page.open_of.connect(opened.append)
    page.open_po.connect(opened.append)
    page.tree.setCurrentItem(page.tree.topLevelItem(0))
    page.open_shortcut.activated.emit()
    assert opened == [page._key()[1]]


# ---------------------------------------------------------------- AUD-158 Go to Print ticks the OF on Print

def test_go_to_print_arrives_with_that_of_ticked(app, world):
    other = world.approved()
    of_id = world.approved()
    w = MainWindow(world.conn, world.admin)
    w.show()
    pump()
    w.print_pos.search.setText("zzqqxx")  # left over from earlier; the OF must still be found
    op = w.orders_page
    w.nav.setCurrentRow(next(r for r, p in w.nav_rows.items() if p is op))
    pump()
    op.tree.setCurrentItem(op.item_for(("OF", of_id)))
    pump()
    assert op.next_btn.text() == "Go to Print"
    op.next_btn.click()
    QTest.qWait(450)
    pp = w.print_pos
    assert w.stack.currentWidget() is pp
    assert pp.tab == pp.TO_PRINT and pp.ticked == {of_id} and other not in pp.ticked
    row = [r["id"] for r in pp.rows].index(of_id)
    assert pp.table.item(row, 0).checkState() == Qt.CheckState.Checked and pp.table.currentRow() == row
    assert pp.print_btn.isEnabled()


def test_tick_of_switches_to_to_print(app, world):
    of_id = world.approved()
    pp = page_alone(pages.PrintPage(world.conn, world.admin))
    pp.tabs.setCurrentIndex(list(pp.TABS).index(pp.PRINTED))
    pp.tick_of(of_id)
    assert pp.tab == pp.TO_PRINT and pp.ticked == {of_id}
    # An OF that isn't waiting to print ticks nothing.
    pp.tick_of(999999)
    assert pp.ticked == set()


# ---------------------------------------------------------------- AUD-099 delivered reads against the order, extra and lost separately

def test_delivered_text_caps_at_the_order_and_shows_extra_and_lost():
    base = {"counted": 1, "status": orders.FULL, "ordered_m": 10000}
    assert of_page._delivered_text([dict(base, received_m=15000, shown_m=10000, extra_m=5000, lost_m=0)]) == "10 of 10, 5 extra"
    assert of_page._delivered_text([dict(base, received_m=8000, shown_m=10000, extra_m=0, lost_m=2000)]) == "10 of 10, 2 lost on the way"
    assert of_page._delivered_text([dict(base, received_m=6000, shown_m=6000, extra_m=0, lost_m=0)]) == "6 of 10"
    # Without the detail it reads as before.
    assert of_page._delivered_text([dict(base, received_m=6000)]) == "6 of 10"


@pytest.fixture
def issued(world, request):
    if getattr(request, "param", False):
        # Goods that come without stickers are counted piece by piece at the Central Warehouse.
        world.conn.execute("UPDATE suppliers SET sticker_rate_c = 50 WHERE id = ?", (world.supplier,))
    of_id = world.approved(items=[world.item(cost=1000, alloc={NORTH: 10}, description="Socks", item_code="SOCK-1")])
    pos = order_forms.generate_pos(world.conn, world.buyer, of_id)
    for p in pos:
        orders.issue(world.conn, world.buyer, p["id"])
    make = lambda name: auth.create_user(world.conn, world.admin, name, name.title(), PASSWORD, ["RECEIVER"], must_change=False, branch_code=NORTH)
    world.r1, world.r2 = make("ana"), make("ben")
    world.of_id, world.po_id = of_id, pos[0]["id"]
    world.socks = orders.get_po(world.conn, world.po_id)["items"][0]["id"]
    return world


def _delivered_cell(app, world, page_class):
    page = page_alone(page_class(world.conn, world.admin))
    if page_class is of_page.InquiryPage:
        page.show_completed()
    else:
        page._set_tab(orders.COMPLETED)
    pump()
    item = page.item_for(("OF", world.of_id)) if hasattr(page, "item_for") else next(
        page.tree.topLevelItem(i) for i in range(page.tree.topLevelItemCount()) if page.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)[1] == world.of_id)
    return item.text(len(page.HEADERS) + len(of_page.EXTRA_COLUMNS))


@pytest.mark.parametrize("page_class", [of_page.OrdersPage, of_page.InquiryPage])
def test_excess_and_not_on_po_read_as_extra(app, issued, page_class):
    w = issued
    receiving.save_count(w.conn, w.r1, w.po_id, {w.socks: "12"}, extras=[{"item_code": "X6", "description": "Not ordered", "pieces": "3"}])
    extra_id = receiving.sheet(w.conn, w.r2, w.po_id)[-1]["id"]
    receiving.save_count(w.conn, w.r2, w.po_id, {w.socks: "12", extra_id: "3"})
    assert receiving.delivered_by_po(w.conn)[w.po_id]["received_m"] == 15000  # the service is unchanged
    assert _delivered_cell(app, w, page_class) == "10 of 10, 5 extra"


@pytest.mark.parametrize("issued", [True], indirect=True)
def test_transit_loss_reads_separately(app, issued):
    w = issued
    warehouse.receive(w.conn, w.warehouse, w.of_id, {w.po_id: "1"}, stickered={w.socks: "10"})
    warehouse.ship(w.conn, w.warehouse, w.of_id)
    assert warehouse.left_central(w.conn, w.po_id) == {w.socks: 10000}
    for who in (w.r1, w.r2):
        receiving.save_count(w.conn, who, w.po_id, {w.socks: "8"}, boxes="1")
    assert receiving.delivered_by_po(w.conn)[w.po_id]["received_m"] == 8000
    detail = of_page.delivered_with_detail(w.conn)[w.po_id]
    assert (detail["shown_m"], detail["extra_m"], detail["lost_m"]) == (10000, 0, 2000)
    # It agrees with what Payments works out for the line.
    line = receiving.lines_of(w.conn, w.po_id)[0]
    assert (min(line["supplier_m"], line["ordered_m"]), line["transit_m"]) == (10000, 2000)
    assert _delivered_cell(app, w, of_page.InquiryPage) == "10 of 10, 2 lost on the way"


def _warehouse_counted_and_shipped(w, pieces="10", another=False):
    warehouse.receive(w.conn, w.warehouse, w.of_id, {w.po_id: "1"}, stickered={w.socks: pieces}, another=another)
    warehouse.ship(w.conn, w.warehouse, w.of_id)


def _text(w):
    return of_page._delivered_text([of_page.delivered_with_detail(w.conn)[w.po_id]])


@pytest.mark.parametrize("issued", [True], indirect=True)
def test_count_waiting_for_second_receiver_is_not_lost(app, issued):
    w = issued
    _warehouse_counted_and_shipped(w)
    receiving.save_count(w.conn, w.r1, w.po_id, {w.socks: "10"}, boxes="1")
    assert _text(w) == "0 of 10"


@pytest.mark.parametrize("issued", [True], indirect=True)
def test_counts_that_disagree_are_not_lost(app, issued):
    w = issued
    _warehouse_counted_and_shipped(w)
    receiving.save_count(w.conn, w.r1, w.po_id, {w.socks: "10"}, boxes="1")
    receiving.save_count(w.conn, w.r2, w.po_id, {w.socks: "9"}, boxes="1")
    assert receiving._state(w.conn, w.po_id)["need"] == receiving.AGAIN
    assert _text(w) == "0 of 10"


@pytest.mark.parametrize("issued", [True], indirect=True)
def test_held_count_is_not_lost(app, issued):
    w = issued
    _warehouse_counted_and_shipped(w)
    n = 0
    while receiving._state(w.conn, w.po_id)["need"] != receiving.HELD:
        n += 1
        assert n < 20
        receiving.save_count(w.conn, w.r1 if n % 2 else w.r2, w.po_id, {w.socks: "10" if n % 2 else "9"}, boxes="1")
    assert _text(w) == "0 of 10"


@pytest.mark.parametrize("issued", [True], indirect=True)
def test_second_delivery_not_yet_agreed_is_not_lost(app, issued):
    w = issued
    _warehouse_counted_and_shipped(w, "6")
    for who in (w.r1, w.r2):
        receiving.save_count(w.conn, who, w.po_id, {w.socks: "6"}, boxes="1")
    assert _text(w) == "6 of 10"
    _warehouse_counted_and_shipped(w, "4", another=True)
    receiving.save_count(w.conn, w.r1, w.po_id, {w.socks: "4"}, boxes="1")
    assert _text(w) == "6 of 10"


# ---------------------------------------------------------------- AUD-173 the Orders placeholder fits at 1366

MEASURE = r'''
import os, sys, tempfile, json
os.environ["PO_DATA_DIR"] = tempfile.mkdtemp(prefix="ofl-data-")
os.environ["PO_SAMPLE_DIR"] = tempfile.mkdtemp(prefix="ofl-sample-")
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from PySide6.QtCore import Qt, QSettings
from PySide6.QtWidgets import QApplication, QLineEdit
app = QApplication([])
from atlaspo import auth, db, sample_data
from atlaspo.ui import theme
theme._settings = lambda: QSettings(str(Path(os.environ["PO_DATA_DIR"]) / "q.ini"), QSettings.Format.IniFormat)
from atlaspo.ui.main_window import MainWindow
assert os.environ["PO_DATA_DIR"] in str(db.data_dir())
conn = db.open_database(Path(os.environ["PO_DATA_DIR"]) / "m.sqlite")
sample_data.build(conn)
theme.apply("light")
pump = lambda: [app.processEvents() for _ in range(4)]
bad, seen = [], 0
for login in ("admin", "viewer"):
    win = MainWindow(conn, auth.login(conn, login, "123"))
    win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    win.show()
    win.resize(1366, 768)
    pump()
    for name in ("orders_page", "inquiry"):
        page = getattr(win, name)
        win.nav.setCurrentRow(next(r for r, p in win.nav_rows.items() if p is page))
        pump()
        if name == "inquiry":
            page.show_completed()
            pump()
        for e in page.findChildren(QLineEdit):
            if e.isVisible() and e.placeholderText():
                seen += 1
                need = e.fontMetrics().horizontalAdvance(e.placeholderText()) + 12 + (20 if e.isClearButtonEnabled() else 0)
                if need > e.width():
                    bad.append(f"{login} {name}: {e.placeholderText()} needs {need} has {e.width()}")
    win.close()
print("RESULT " + json.dumps({"bad": bad, "seen": seen}))
'''


def test_orders_and_inquiry_placeholders_fit_at_1366(tmp_path):
    """Measured with the Windows fonts, as the audit did: the offscreen test platform has none."""
    import json
    import subprocess
    import sys
    from pathlib import Path
    if sys.platform != "win32":
        pytest.skip("measures with the Windows fonts")
    script = tmp_path / "measure.py"
    script.write_text(MEASURE, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("QT_QPA_PLATFORM", "PO_DATA_DIR", "PO_SAMPLE_DIR")}
    env.update(QT_SCALE_FACTOR="1", QT_ENABLE_HIGHDPI_SCALING="0")
    done = subprocess.run([sys.executable, "-E", str(script), str(Path(__file__).resolve().parents[1])], capture_output=True, text=True, env=env, timeout=600)
    line = next((l for l in done.stdout.splitlines() if l.startswith("RESULT ")), None)
    assert line, done.stdout[-2000:] + done.stderr[-2000:]
    result = json.loads(line[7:])
    assert result["seen"] >= 6 and result["bad"] == []


# ---------------------------------------------------------------- AUD-187 the empty text names New order form only for those who have it

def test_empty_orders_text_mentions_new_order_form_only_with_of_edit(app, world):
    viewer = auth.create_user(world.conn, world.admin, "vic", "Vic Viewer", PASSWORD, ["VIEWER"], must_change=False)
    for user, expected in ((world.buyer, True), (viewer, False)):
        page = page_alone(of_page.OrdersPage(world.conn, user))
        assert page.tree.topLevelItemCount() == 0 and page.empty.isVisible()
        assert ("New order form" in page.empty.text()) is expected, page.empty.text()
        assert page.new_btn.isVisibleTo(page) is expected


def test_orders_no_match_names_the_search(app, world):
    world.approved()
    page = page_alone(of_page.OrdersPage(world.conn, world.admin))
    page.search.setText("zzqqxx")
    assert page.empty.isVisible() and "zzqqxx" in page.empty.text()


# ---------------------------------------------------------------- AUD-007 / AUD-008 Inquiry keeps its header in place, and says what didn't match

def _y(page, w):
    return w.mapTo(page, QPoint(0, 0)).y()


def test_inquiry_by_date_with_no_date_keeps_the_filters_in_place(app, world):
    world.po()
    page = page_alone(of_page.InquiryPage(world.conn, world.admin))
    page.show_completed()
    pump()
    before = _y(page, page.search)
    page.tabs.setCurrentIndex(list(page.MODES).index(page.DATES))
    pump()
    assert page.empty.isVisible() and not page.tree.isVisible()
    assert _y(page, page.search) == before
    assert _y(page, page.date_from) < 200


def test_orders_header_stays_put_when_nothing_matches(app, world):
    world.approved()
    page = page_alone(of_page.OrdersPage(world.conn, world.admin))
    before = _y(page, page.search)
    page.search.setText("zzqqxx")
    pump()
    assert page.empty.isVisible() and _y(page, page.search) == before


def test_inquiry_no_match_names_the_search(app, world):
    world.po()
    page = page_alone(of_page.InquiryPage(world.conn, world.admin))
    page.search.setText("zzqqxx")
    assert page.empty.isVisible() and "No match for 'zzqqxx'" in page.empty.text()
    page.search.setText("")
    page.product.setText("nothing-like-this")
    assert "No match for 'nothing-like-this'" in page.empty.text()


# ---------------------------------------------------------------- AUD-013 one spelling for partly delivered on Inquiry

def test_inquiry_spells_partially_delivered_as_the_po_status(app, world):
    of_id = world.approved(items=[world.item(alloc={NORTH: 2, SOUTH: 2})])
    pos = order_forms.generate_pos(world.conn, world.buyer, of_id)
    for p in pos:
        orders.issue(world.conn, world.buyer, p["id"])
    make = lambda name: auth.create_user(world.conn, world.admin, name, name.title(), PASSWORD, ["RECEIVER"], must_change=False, branch_code=NORTH)
    r1, r2 = make("ana"), make("ben")
    item = orders.get_po(world.conn, pos[0]["id"])["items"][0]["id"]
    for who in (r1, r2):
        receiving.save_count(world.conn, who, pos[0]["id"], {item: "2"})
    page = page_alone(of_page.InquiryPage(world.conn, world.admin))
    page.show_completed()
    pump()
    top = next(page.tree.topLevelItem(i) for i in range(page.tree.topLevelItemCount()) if page.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole) == ["OF", of_id]
               or tuple(page.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)) == ("OF", of_id))
    texts = {top.text(page.C_STATUS)} | {top.child(c).text(page.C_STATUS) for c in range(top.childCount())}
    assert "Partly delivered" not in texts
    assert orders.STATUS_LABELS[orders.PARTIAL] in texts and texts & {orders.STATUS_LABELS[orders.PARTIAL]}


# ---------------------------------------------------------------- AUD-024 the Print page is called what the sidebar calls it

def test_print_heading_matches_sidebar(app, world):
    from PySide6.QtWidgets import QLabel
    pp = page_alone(pages.PrintPage(world.conn, world.admin))
    texts = [l.text() for l in pp.findChildren(QLabel)]
    assert "Print" in texts and "Print OF and POs" not in texts
