"""Fixes from an audit for branch counting and item passes (AUD-058, 162, 186, 183, 041, 145, 006, 149, 081, 148, 176, 117, 074)."""
from __future__ import annotations

import os
import re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from atlaspo import auth, db, item_passes, order_forms, orders, receiving, sample_data
from atlaspo.db import PermissionDenied, POError
from atlaspo.ui import receiving_page, theme
from atlaspo.ui.main_window import MainWindow
from atlaspo.ui.common import make_table, set_widths
from atlaspo.ui.receiving_page import C_DAMAGED, C_PIECES, C_REMARKS, ReceivedPage

from conftest import NORTH, PASSWORD, SOUTH


@pytest.fixture
def app(tmp_path, monkeypatch):
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(theme, "_settings", lambda: settings)
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def setup(world):
    """One issued order: Socks, 10 at North and 4 at South. Three receivers at North, one at South."""
    of_id = world.approved(items=[world.item(cost=7777, alloc={NORTH: 10, SOUTH: 4}, description="Socks", item_code="SOCK-1")])
    pos = order_forms.generate_pos(world.conn, world.buyer, of_id)
    for p in pos:
        orders.issue(world.conn, world.buyer, p["id"])
    make = lambda name, branch: auth.create_user(world.conn, world.admin, name, name.title(), PASSWORD, ["RECEIVER"], must_change=False, branch_code=branch)
    world.r1, world.r2, world.r3, world.other = make("ana", NORTH), make("ben", NORTH), make("cris", NORTH), make("cara", SOUTH)
    world.po = pos[0]["id"]
    world.po_number = pos[0]["po_number"]
    world.socks = orders.get_po(world.conn, world.po)["items"][0]["id"]
    return world


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    folder = tmp_path_factory.mktemp("sample")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PO_DATA_DIR", str(folder))
        sample_data.ensure(folder)
        conn = db.open_database()
        yield conn
        conn.close()


def _counts(w):
    return w.conn.execute("SELECT COUNT(*) FROM branch_counts WHERE po_id = ?", (w.po,)).fetchone()[0]


def _receiving(conn, who):
    """The receiving page inside a main window, the way receivers see it."""
    return MainWindow(conn, who).receiving


def _open(page, number):
    page.number.setText(number)
    page.open_number()


# ------------------------------------------------------------------ AUD-058 a stale sheet can't start a phantom delivery

def test_a_count_sheet_that_went_out_of_date_is_refused(setup):
    w = setup
    receiving.save_count(w.conn, w.r1, w.po, {w.socks: "10"}, expect=receiving.sheet_key(w.conn, w.r1, w.po))
    stale = receiving.sheet_key(w.conn, w.r3, w.po)  # a third receiver opens the sheet while the first count waits
    receiving.sheet(w.conn, w.r3, w.po)
    assert receiving.save_count(w.conn, w.r2, w.po, {w.socks: "10"}, expect=receiving.sheet_key(w.conn, w.r2, w.po))["outcome"] == receiving.APPROVED
    before = _counts(w)
    with pytest.raises(POError, match="out of date: a count was saved or the counting changed while you had it open. Open the PO again"):
        receiving.save_count(w.conn, w.r3, w.po, {w.socks: "10"}, expect=stale)
    assert _counts(w) == before and orders.get_po(w.conn, w.po)["items"][0]["received_qty_m"] == 10000
    assert receiving.excess_lines(w.conn, w.po) == []
    # Without the sheet's key it works as before (whether that's another delivery is still to be decided).
    assert receiving.save_count(w.conn, w.r3, w.po, {w.socks: "0"})["outcome"] == receiving.WAITING


def test_the_normal_two_counts_and_a_redo_still_work_with_the_key(setup):
    w = setup
    first = receiving.save_count(w.conn, w.r1, w.po, {w.socks: "9"}, expect=receiving.sheet_key(w.conn, w.r1, w.po))
    redo = receiving.save_count(w.conn, w.r1, w.po, {w.socks: "10"}, recount_of=first["count_id"], expect=receiving.sheet_key(w.conn, w.r1, w.po))
    assert redo["outcome"] == receiving.WAITING
    assert receiving.save_count(w.conn, w.r2, w.po, {w.socks: "10"}, expect=receiving.sheet_key(w.conn, w.r2, w.po))["outcome"] == receiving.APPROVED


def test_two_windows_on_screen_the_late_one_is_refused(app, setup, tmp_path):
    w = setup
    path = w.conn.execute("PRAGMA database_list").fetchone()[2]
    pc_a, pc_b, pc_c = db.open_database(path), db.open_database(path), db.open_database(path)
    first = _receiving(pc_a, w.r1)
    _open(first, w.po_number)
    first.grid.item(0, C_PIECES).setText("10")
    first.save_count()
    late = _receiving(pc_c, w.r3)
    _open(late, w.po_number)  # opened while the first count waits
    second = _receiving(pc_b, w.r2)
    _open(second, w.po_number)
    second.grid.item(0, C_PIECES).setText("10")
    second.save_count()
    assert "Both counts match" in second.prompt.text()
    before = _counts(w)
    late.grid.item(0, C_PIECES).setText("10")
    with pytest.raises(POError, match="out of date"):
        late.save_count()
    assert _counts(w) == before
    for page in (first, late, second):
        page.window().close()


# ------------------------------------------------------------------ AUD-162 / AUD-186 what can't be saved is shown before the confirm, cell by cell

def test_a_blank_line_is_refused_before_the_confirm(app, setup, monkeypatch):
    w = setup
    asked = []
    monkeypatch.setattr(receiving_page, "confirm", lambda *a, **k: asked.append(a) or True)
    page = _receiving(w.conn, w.r1)
    _open(page, w.po_number)
    with pytest.raises(POError, match="Type how many pieces of Socks you counted. Type 0 if it didn't arrive."):
        page._save_pressed()
    assert asked == [] and _counts(w) == 0
    assert page.grid.item(0, C_PIECES).background().color() == QColor(theme.current.RED_PALE)
    page.grid.item(0, C_PIECES).setText("10")
    page._save_pressed()
    assert len(asked) == 1 and "Left blank" not in asked[0][2] and _counts(w) == 1
    page.window().close()


def test_every_bad_cell_is_named_with_its_product_and_column_and_marked(app, setup):
    w = setup
    page = _receiving(w.conn, w.r1)
    _open(page, w.po_number)
    page.grid.item(0, C_PIECES).setText("abc")
    page.grid.item(0, C_DAMAGED).setText("-3")
    with pytest.raises(POError) as caught:
        page.save_count()
    message = str(caught.value)
    assert "Pieces counted for Socks: 'abc' is not a valid quantity" in message
    assert "Damaged for Socks: quantity can't be negative" in message
    red = QColor(theme.current.RED_PALE)
    assert page.grid.item(0, C_PIECES).background().color() == red and page.grid.item(0, C_DAMAGED).background().color() == red
    assert _counts(w) == 0
    # Fixed, the marks go and the count saves.
    page.grid.item(0, C_PIECES).setText("10")
    page.grid.item(0, C_DAMAGED).setText("1")
    page.save_count()
    assert _counts(w) == 1
    page.window().close()


def test_the_service_names_the_product_and_column_too(setup):
    w = setup
    with pytest.raises(POError, match="Pieces counted for Socks: 'abc'"):
        receiving.save_count(w.conn, w.r1, w.po, {w.socks: "abc"})
    with pytest.raises(POError, match="Damaged for Socks: quantity can't be negative"):
        receiving.save_count(w.conn, w.r1, w.po, {w.socks: "5"}, damaged={w.socks: "-3"})
    assert _counts(w) == 0


# ------------------------------------------------------------------ AUD-081 absurd numbers are refused, naming the product

def test_numbers_no_delivery_can_have_are_refused(setup):
    w = setup
    for pieces, damaged, column in (("1e16", None, "Pieces counted"), ("99999999999999999999", None, "Pieces counted"), ("10", "2000000000", "Damaged")):
        with pytest.raises(POError, match=f"{column} for Socks: .* far more than a delivery can have"):
            receiving.save_count(w.conn, w.r1, w.po, {w.socks: pieces}, damaged={w.socks: damaged} if damaged else None)
    with pytest.raises(POError, match="boxes is far more"):
        receiving.save_count(w.conn, w.r1, w.po, {w.socks: "10"}, boxes="9" * 20)
    with pytest.raises(POError, match="whole number"):
        receiving.save_count(w.conn, w.r1, w.po, {w.socks: "10"}, boxes="²")
    assert _counts(w) == 0
    # A billion pieces is still allowed: the limit is only there to catch slips.
    assert receiving.save_count(w.conn, w.r1, w.po, {w.socks: "1000000000"})["outcome"] == receiving.WAITING


# ------------------------------------------------------------------ AUD-183 the product name stays readable on a small window

def _sized(page, room, headers, widths, hidden=()):
    """A table set up like one of the page's, at a given width, with the page's own rule for sharing the room."""
    table = make_table(headers, stretch=room.stretch)
    set_widths(table, widths)
    for c in hidden:
        table.setColumnHidden(c, True)
    copy = receiving_page._Room(table, room.stretch, room.room, room.shrink)
    return table, copy


def test_the_product_column_keeps_its_room_on_the_smallest_window(app, setup):
    w = setup
    page = _receiving(w.conn, w.r1)
    _open(page, w.po_number)
    assert page.grid.item(0, 1).toolTip() == "Socks" and page.rooms == (page.grid_room, page.lines_room, page.pass_done_room)
    # The count sheet at the receiver's smallest window (1074 x 692 leaves the table 820 wide) and at a big one.
    headers = [page.grid.horizontalHeaderItem(c).text() for c in range(page.grid.columnCount())]
    widths = {c: page.grid.columnWidth(c) for c in range(page.grid.columnCount()) if c != 1}
    widths.update({C_REMARKS: 220, 8: 150})
    table, room = _sized(page, page.grid_room, headers, widths, hidden=(8,))
    table.show()
    for wide, product, remarks in ((822, 160, 120), (1400, 600, 220)):
        table.resize(wide, 300)
        QApplication.processEvents()
        room.fit()
        QApplication.processEvents()
        assert table.columnWidth(1) >= product and table.columnWidth(C_REMARKS) == remarks, (wide, table.columnWidth(1), table.columnWidth(C_REMARKS))
    # Already printed: where the goods are keeps 200.
    headers = [page.pass_done.horizontalHeaderItem(c).text() for c in range(page.pass_done.columnCount())]
    table, room = _sized(page, page.pass_done_room, headers, {0: 120, 1: 96, 2: 120, 3: 220, 4: 70, 5: 150})
    table.show()
    table.resize(822, 300)
    QApplication.processEvents()
    room.fit()
    QApplication.processEvents()
    assert table.columnWidth(6) >= 200 and table.columnWidth(5) == 100, [table.columnWidth(c) for c in range(7)]
    table.close()
    page.window().close()


def _pump(n=6):
    for _ in range(n):
        QApplication.processEvents()


def test_the_real_count_sheet_gives_the_product_its_room(app, setup):
    w = setup
    page = receiving_page.ReceivingPage(w.conn, w.r1)
    page.resize(1300, 700)
    page.show()
    _pump()
    page._load(w.po)
    for wide, product, remarks in ((822, 160, 120), (1400, 600, 220)):
        page.grid.setFixedWidth(wide)
        _pump()
        page._fit_rooms()
        _pump(3)
        assert page.grid.columnWidth(1) >= product and page.grid.columnWidth(C_REMARKS) == remarks, (wide, page.grid.columnWidth(1), page.grid.columnWidth(C_REMARKS))
    page.close()


def test_a_width_the_person_set_on_already_printed_is_kept(app, setup):
    w = setup
    theme._settings().setValue(f"widths/passes-printed-{w.r1.username}", "3:300")
    page = receiving_page.ReceivingPage(w.conn, w.r1)
    page.resize(1700, 900)
    page.show()
    page._set_tab(page.PASSES)
    _pump()
    page._fit_rooms()
    _pump(3)
    assert page.pass_done.columnWidth(3) == 300, [page.pass_done.columnWidth(c) for c in range(page.pass_done.columnCount())]
    # On a narrow window it still gives way so where the goods are stays readable.
    page.pass_done.setFixedWidth(700)
    _pump()
    page._fit_rooms()
    _pump(3)
    assert page.pass_done.columnWidth(3) < 300 and page.pass_done.columnWidth(6) >= 200 or page.pass_done.columnWidth(3) == 110
    page.close()


def test_long_typed_numbers_get_a_short_plain_message(setup):
    w = setup
    with pytest.raises(POError) as caught:
        receiving.save_count(w.conn, w.r1, w.po, {w.socks: "10"}, boxes="9" * 5000)
    assert "far more than a delivery can have" in str(caught.value) and len(str(caught.value)) < 120
    with pytest.raises(POError) as caught:
        receiving.save_count(w.conn, w.r1, w.po, {w.socks: "9" * 5000})
    assert "far more than a delivery can have" in str(caught.value) and len(str(caught.value)) < 160
    with pytest.raises(POError) as caught:
        receiving.save_count(w.conn, w.r1, w.po, {w.socks: "x" * 5000})
    assert "is not a valid quantity" in str(caught.value) and len(str(caught.value)) < 160
    assert receiving.save_count(w.conn, w.r1, w.po, {w.socks: "10"}, boxes="0001000000000")["outcome"] == receiving.WAITING
    assert _counts(w) == 1


# ------------------------------------------------------------------ AUD-006 Received: Status and Counting keep their width, and say it all on hover

def test_received_status_and_counting_are_readable(app, sample):
    page = ReceivedPage(sample, auth.login(sample, "owner", "123"))
    page.resize(1366, 768)
    page.show()
    QApplication.processEvents()
    assert page.table.columnWidth(10) >= 190 and page.table.columnWidth(9) >= 176
    assert [page.table.horizontalHeaderItem(c).toolTip() for c in (8, 9, 10)] == ["DM / CS / item pass", "Counting", "Status"]
    assert page.table.rowCount() and all(page.table.item(r, c).toolTip() == page.table.item(r, c).text() for r in range(page.table.rowCount()) for c in (9, 10))
    page.close()


# ------------------------------------------------------------------ AUD-145 Received search waits for a pause in typing

def test_received_search_refreshes_once_typing_pauses(app, setup, monkeypatch):
    w = setup
    calls = []
    real = ReceivedPage.refresh
    monkeypatch.setattr(ReceivedPage, "refresh", lambda self, *a: (calls.append(1), real(self, *a)))
    page = ReceivedPage(w.conn, w.buyer)
    calls.clear()
    for ch in "S00":
        QTest.keyClick(page.search, ch)
    assert calls == []
    QTest.qWait(450)
    assert len(calls) == 1
    page.close()


# ------------------------------------------------------------------ AUD-041 printing a damaged goods pass tells the other pages

def test_printing_a_damaged_goods_pass_emits_changed_even_if_the_file_does_not_open(app, world, monkeypatch):
    from test_payments import Order
    o = Order(world, damaged="3")
    o.stub()
    who = auth.get_user(world.conn, world.conn.execute("SELECT id FROM users WHERE username = ?", (f"r{NORTH}x1",)).fetchone()[0])
    monkeypatch.setattr(receiving_page, "confirm", lambda *a, **k: True)

    def fail(docs, name):
        raise OSError("No application is associated with the specified file")
    monkeypatch.setattr(receiving_page, "open_for_printing", fail)
    page = _receiving(world.conn, who)
    emitted = []
    page.changed.connect(lambda: emitted.append(1))
    with pytest.raises(OSError):
        page.print_cs_pass()
    # The file never opened, so the pass isn't recorded as printed (AUD-041, second round).
    assert emitted == [1] and not page.cs_rows[0]["printed_at"]
    monkeypatch.setattr(receiving_page, "open_for_printing", lambda docs, name: None)
    page.print_cs_pass()
    assert emitted == [1, 1] and page.cs_rows[0]["printed_at"]
    page.window().close()


# ------------------------------------------------------------------ AUD-149 the Received list reads documents once, with the same results

def test_po_results_batches_lookups_and_gives_the_same_rows(sample):
    admin = auth.login(sample, "owner", "123")
    statements = []
    sample.set_trace_callback(statements.append)
    try:
        new = receiving.po_results(sample, admin)
    finally:
        sample.set_trace_callback(None)
    assert new
    # The same rows worked out one PO at a time, the way it used to be done.
    for r in new:
        po_id = r["id"]
        assert r["debit_memos"] == ", ".join(x[0] for x in sample.execute("SELECT dm_number FROM debit_memos WHERE po_id = ? AND status = 'ISSUED' ORDER BY id", (po_id,)))
        assert r["claim_stubs"] == ", ".join(x[0] for x in sample.execute("SELECT cs_number FROM claim_stubs WHERE po_id = ? AND status <> 'CANCELLED' ORDER BY id", (po_id,)))
        assert r["item_passes"] == ", ".join(x[0] for x in sample.execute("SELECT ip_number FROM item_passes WHERE po_id = ? AND status <> 'CANCELLED' ORDER BY id", (po_id,)))
        excess = receiving.excess_lines(sample, po_id)
        assert (r["excess_m"], r["excess_left_m"]) == (sum(x["excess_m"] for x in excess), sum(x["left_m"] for x in excess))
        assert r["tab"] == (receiving.COMPLETED if not r["progress"] and receiving._settled(sample, po_id) else receiving.TO_CHECK)
    assert any(r["debit_memos"] for r in new) and any(r["claim_stubs"] for r in new) and any(r["item_passes"] for r in new)
    # Before the fix: three document lookups per PO, and the lines read twice. Now the document numbers take three statements in all.
    per_po_lookups = [s for s in statements if re.search(r"FROM (debit_memos|claim_stubs|item_passes) WHERE po_id = \d+", s)]
    assert per_po_lookups == [] and statements
    assert len(statements) < 14 * len(new), len(statements)


# ------------------------------------------------------------------ AUD-148 / AUD-176 item passes to print: branch filtered in SQL, lines carry only the pass

def test_item_passes_to_print_filter_by_branch_and_carry_only_the_pass(sample):
    nor1 = auth.login(sample, "nor1", "123")
    statements = []
    sample.set_trace_callback(statements.append)
    try:
        rows = item_passes.to_print(sample, nor1)
    finally:
        sample.set_trace_callback(None)
    assert rows and all(r["branch_code"] == nor1.branch_code for r in rows)
    for r in rows:
        assert sum(line["left_m"] for line in r["lines"]) == r["pieces_m"]
        for line in r["lines"]:
            assert set(line) == set(item_passes.PASS_FIELDS)
            assert not {"ordered_m", "received_m", "left_central_m", "supplier_m", "transit_m", "difference_m", "excess_m"} & set(line)
        # The same products and pieces the full lines give.
        assert [(line["id"], line["left_m"]) for line in r["lines"]] == [(x["id"], x["left_m"]) for x in receiving.excess_lines(sample, r["id"]) if x["left_m"]]
    # Only this branch's POs are looked at.
    branch_pos = sample.execute("SELECT COUNT(*) FROM purchase_orders p WHERE p.status <> 'CANCELLED' AND p.branch_code = ? AND EXISTS (SELECT 1 FROM branch_counts b WHERE b.po_id = p.id)", (nor1.branch_code,)).fetchone()[0]
    every_pos = sample.execute("SELECT COUNT(*) FROM purchase_orders p WHERE p.status <> 'CANCELLED' AND EXISTS (SELECT 1 FROM branch_counts b WHERE b.po_id = p.id)").fetchone()[0]
    assert branch_pos < every_pos
    assert re.search(r"p\.branch_code = \d+", statements[0]), statements[0]  # the branch is chosen in the query, not afterwards
    looked_at = sum(1 for s in statements if s.startswith("SELECT * FROM branch_counts WHERE po_id = ") and "status = 'WAITING'" in s)
    assert looked_at <= branch_pos
    # Someone with no branch and no Admin role sees nothing, as before.
    nobody = auth.User(0, "x", "X", frozenset({"RECEIVER"}), False, None)
    assert item_passes.to_print(sample, nobody) == []


# ------------------------------------------------------------------ AUD-117 reading a pass's lines needs a user with permission

def test_item_pass_lines_need_permission(sample):
    ip = sample.execute("SELECT i.id, p.branch_code FROM item_passes i JOIN purchase_orders p ON p.id = i.po_id WHERE p.branch_code = ? ORDER BY i.id LIMIT 1", (NORTH,)).fetchone()
    assert ip
    lines = item_passes.items(sample, auth.login(sample, "owner", "123"), ip["id"])  # the home office
    assert lines
    assert item_passes.items(sample, auth.login(sample, "nor1", "123"), ip["id"]) == lines  # the branch's own receiver
    stranger = sample.execute("SELECT u.username FROM users u JOIN user_branches b ON b.user_id = u.id JOIN user_roles r ON r.user_id = u.id"
                              " WHERE r.role = 'RECEIVER' AND b.branch_code <> ? ORDER BY u.id LIMIT 1", (NORTH,)).fetchone()[0]
    with pytest.raises(PermissionDenied):
        item_passes.items(sample, auth.login(sample, stranger, "123"), ip["id"])


# ------------------------------------------------------------------ AUD-074 the item pass docstrings say the 90 days start at arrival

def test_item_pass_docstrings_count_the_90_days_from_arrival():
    assert "counted from the day\nthey reach the main warehouse" in item_passes.__doc__
    assert "since the goods reached the warehouse" in item_passes.forfeit.__doc__
    assert "count from this day" in item_passes.arrived.__doc__
