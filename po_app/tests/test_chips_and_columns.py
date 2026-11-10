"""A chip for each person on what they counted, and Inquiry's choice of columns."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from atlaspo import auth, db, masters, order_forms, orders, receiving, schema
from atlaspo.db import POError
from atlaspo.ui import theme
from atlaspo.ui.common import CHIP_ROLE, PersonDelegate, fmt_date
from atlaspo.ui.main_window import MainWindow
from atlaspo.ui.of_page import EXTRA_COLUMNS
from atlaspo.ui.settings_page import UserDialog

from conftest import NORTH, PASSWORD, SOUTH


@pytest.fixture
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Nothing here may touch this PC's saved choices."""
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(theme, "_settings", lambda: settings)
    return settings


def _receiver(world, login, name, branch=NORTH, **kw):
    return auth.create_user(world.conn, world.admin, login, name, PASSWORD, ["RECEIVER"], must_change=False, branch_code=branch, **kw)


def _chip(world, user):
    return auth.chips(world.conn)[user.id]


def test_initials_come_from_the_first_and_last_name(world):
    assert _chip(world, _receiver(world, "a", "Sam Yu")).initials == "SY"
    assert _chip(world, _receiver(world, "b", "Maria Dela Cruz")).initials == "MC"
    assert _chip(world, _receiver(world, "c", "Dee")).initials == "DE"
    assert _chip(world, _receiver(world, "d", "Pedro Santos Jr.")).initials == "PS"


def test_two_people_at_one_branch_never_look_alike(world):
    first, second = _receiver(world, "a", "Maria Reyes"), _receiver(world, "b", "Mario Rosales")
    elsewhere = _receiver(world, "c", "Marco Ramos", SOUTH)
    assert (_chip(world, first).initials, _chip(world, second).initials, _chip(world, elsewhere).initials) == ("MR", "MRO", "MR")
    # Someone joining later doesn't change anyone's chip.
    _receiver(world, "d", "Mina Ruiz")
    assert (_chip(world, first).initials, _chip(world, second).initials) == ("MR", "MRO")


def test_each_branch_uses_the_same_two_colors(world):
    first, second = _receiver(world, "a1", "Ana Cruz"), _receiver(world, "a2", "Ben Lim")
    other_first, other_second = _receiver(world, "r1", "Cy Tan", SOUTH), _receiver(world, "r2", "Di Uy", SOUTH)
    assert (_chip(world, first).color, _chip(world, second).color) == auth.RECEIVER_COLORS
    assert (_chip(world, other_first).color, _chip(world, other_second).color) == auth.RECEIVER_COLORS
    # Saving someone leaves their color alone.
    auth.update_user(world.conn, world.admin, first.id, "Renamed Person", ["RECEIVER"], True, NORTH)
    assert _chip(world, first).color == auth.RECEIVER_COLORS[0]


def test_a_replacement_takes_the_color_of_who_left_and_a_move_takes_whats_free(world):
    first, second = _receiver(world, "a1", "Ana Cruz"), _receiver(world, "a2", "Ben Lim")
    auth.update_user(world.conn, world.admin, first.id, "Ana Cruz", ["RECEIVER"], False, NORTH)
    assert _chip(world, _receiver(world, "a3", "Cy Tan")).color == auth.RECEIVER_COLORS[0]
    # South's first receiver has the first color, so someone moving there takes the second.
    _receiver(world, "r1", "Di Uy", SOUTH)
    auth.update_user(world.conn, world.admin, first.id, "Ana Cruz", ["RECEIVER"], True, SOUTH)
    assert _chip(world, first).color == auth.RECEIVER_COLORS[1]


def test_people_without_a_branch_keep_colors_apart_from_receivers(world):
    colors = [auth.chips(world.conn)[world.admin.id].color, _chip(world, auth.create_user(world.conn, world.admin, "off1", "Office One", "longenough1", ["BUYER"], must_change=False)).color]
    assert not set(colors) & set(auth.RECEIVER_COLORS)


def test_an_admin_can_set_initials_and_color_and_go_back_to_automatic(world):
    ana = _receiver(world, "ana", "Ana Cruz")
    auth.update_user(world.conn, world.admin, ana.id, "Ana Cruz", ["RECEIVER"], True, NORTH, "acz", 9)
    assert (_chip(world, ana).initials, _chip(world, ana).color) == ("ACZ", 9)
    # Saving something else leaves them alone.
    auth.update_user(world.conn, world.admin, ana.id, "Ana Cruz", ["RECEIVER"], True, NORTH)
    assert (_chip(world, ana).initials, _chip(world, ana).color) == ("ACZ", 9)
    auth.update_user(world.conn, world.admin, ana.id, "Ana Cruz", ["RECEIVER"], True, NORTH, "")
    assert _chip(world, ana).initials == "AC"
    logged = world.conn.execute("SELECT new_value FROM audit_logs WHERE action = 'USER_UPDATE' ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert '"initials": ""' in logged


def test_typed_initials_are_checked(world):
    ana, ben = _receiver(world, "ana", "Ana Cruz"), _receiver(world, "ben", "Ben Lim", SOUTH)
    auth.update_user(world.conn, world.admin, ana.id, "Ana Cruz", ["RECEIVER"], True, NORTH, "XX")
    for bad in ("TOOLONG", "A-B"):
        with pytest.raises(POError, match="letters or numbers"):
            auth.update_user(world.conn, world.admin, ben.id, "Ben Lim", ["RECEIVER"], True, SOUTH, bad)
    with pytest.raises(POError, match="color"):
        auth.update_user(world.conn, world.admin, ben.id, "Ben Lim", ["RECEIVER"], True, SOUTH, color=auth.CHIP_COLORS)
    auth.update_user(world.conn, world.admin, ben.id, "Ben Lim", ["RECEIVER"], True, SOUTH, "XX")  # another branch is fine
    # Moving onto a branch where those initials are taken is refused, and nothing changes.
    with pytest.raises(POError, match="Ana Cruz already uses"):
        auth.update_user(world.conn, world.admin, ben.id, "Ben Lim", ["RECEIVER"], True, NORTH)
    assert auth.get_user(world.conn, ben.id).branch_code == SOUTH
    with pytest.raises(POError, match="already uses"):
        _receiver(world, "cy", "Cy Tan", initials="XX")
    assert "cy" not in [u["username"] for u in auth.list_users(world.conn)]


def test_every_color_reads_with_white_initials():
    def luminance(value):
        channels = [c / 255 for c in QColor(value).getRgb()[:3]]
        r, g, b = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    assert len(theme.PERSON_COLORS) == auth.CHIP_COLORS
    assert len({value for _, value in theme.PERSON_COLORS}) == auth.CHIP_COLORS
    for _, value in theme.PERSON_COLORS:
        assert 1.05 / (luminance(value) + 0.05) >= 4.5, value


def test_people_from_before_chips_get_one_without_losing_anything(env):
    path = env / "old.sqlite"
    c = db.open_database(path)
    boss = auth.create_first_admin(c, "boss", "Boss Man", PASSWORD)
    ana = auth.create_user(c, boss, "ana", "Ana Cruz", PASSWORD, ["BUYER"], must_change=False)
    c.executescript("BEGIN;DROP TABLE user_chips;PRAGMA user_version = 14;COMMIT;")
    c.close()
    conn = db.open_database(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
    assert list((env / "backups").glob("po-before-migration-v14-*.sqlite"))
    assert auth.login(conn, "ana", PASSWORD).id == ana.id
    everyone = auth.chips(conn)
    assert (everyone[boss.id].initials, everyone[ana.id].initials) == ("BM", "AC") and everyone[boss.id].color != everyone[ana.id].color
    conn.close()


def _counted(world):
    of_id = world.approved(items=[world.item(cost=7777, alloc={NORTH: 10}, description="Socks", item_code="SOCK-1")])
    po = order_forms.generate_pos(world.conn, world.buyer, of_id)[0]
    orders.issue(world.conn, world.buyer, po["id"])
    item = orders.get_po(world.conn, po["id"])["items"][0]["id"]
    ana, ben = _receiver(world, "ana", "Ana Cruz"), _receiver(world, "ben", "Ben Lim")
    receiving.save_count(world.conn, ana, po["id"], {item: "10"})
    receiving.save_count(world.conn, ben, po["id"], {item: "10"})
    return ana, ben


def test_received_and_receiving_mark_who_counted_with_chips(app, world, store):
    ana, ben = _counted(world)
    chips = auth.chips(world.conn)
    win = MainWindow(world.conn, world.buyer)
    page = win.received
    page.tabs.setCurrentIndex(1)
    page.table.selectRow(0)
    counted, matched = page.history.item(0, 4), page.history.item(0, 6)
    assert isinstance(page.history.itemDelegateForColumn(4), PersonDelegate)
    assert counted.data(CHIP_ROLE) == (chips[ana.id].initials, chips[ana.id].color) and counted.toolTip() == "Ana Cruz"
    assert matched.data(CHIP_ROLE) == (chips[ben.id].initials, chips[ben.id].color) and matched.toolTip() == "Ben Lim"
    assert page.history.item(1, 6).data(CHIP_ROLE) == (chips[ana.id].initials, chips[ana.id].color)
    assert page.history.item(1, 4).toolTip() == "Ben Lim"
    page.tabs.setCurrentIndex(2)
    assert page.receivers.item(0, 0).text() in ("Ana Cruz", "Ben Lim") and page.receivers.item(0, 0).data(CHIP_ROLE) is not None
    assert page.receivers.itemDelegateForColumn(0).named
    page.grab()
    win.close()

    win = MainWindow(world.conn, ben)
    page = win.receiving
    page.tabs.setCurrentIndex(1)
    assert page.counts.item(0, 5).data(CHIP_ROLE) == (chips[ben.id].initials, chips[ben.id].color)
    # A chip says who, never how many: the other person's numbers stay out of reach.
    page.counts.selectRow(1)
    assert "only Ana Cruz can see" in page.selected_note.text() and page.lines.rowCount() == 0
    win.close()


def test_a_count_nobody_has_matched_yet_has_no_second_chip(app, world, store):
    of_id = world.approved(items=[world.item(cost=7777, alloc={NORTH: 10}, item_code="SOCK-1")])
    po = order_forms.generate_pos(world.conn, world.buyer, of_id)[0]
    orders.issue(world.conn, world.buyer, po["id"])
    ana = _receiver(world, "ana", "Ana Cruz")
    receiving.save_count(world.conn, ana, po["id"], {orders.get_po(world.conn, po["id"])["items"][0]["id"]: "10"})
    win = MainWindow(world.conn, ana)
    counts = win.receiving.counts
    assert counts.item(0, 5).data(CHIP_ROLE) is not None
    assert counts.item(0, 7).data(CHIP_ROLE) is None and counts.item(0, 7).text() == ""
    win.close()


def test_the_user_screen_sets_a_chip(app, world, store):
    ana = _receiver(world, "ana", "Ana Cruz")
    win = MainWindow(world.conn, world.admin)
    page = win.settings
    row = next(r for r in range(page.users.rowCount()) if page.users.item(r, 0).text() == "ana")
    assert page.users.item(row, 5).data(CHIP_ROLE) == ("AC", _chip(world, ana).color)
    current = next(u for u in auth.list_users(world.conn) if u["id"] == ana.id)
    edit = UserDialog(page, masters.list_branches(world.conn), current)
    assert "AC" in edit.initials.placeholderText() and edit.initials.text() == "" and edit.color.currentData() == current["color"]
    new = UserDialog(page, masters.list_branches(world.conn))
    assert new.color.currentData() is None and new.color.count() == auth.CHIP_COLORS + 1
    win.close()


def _inquiry(world, username_user=None):
    supplier = masters.save_supplier(world.conn, world.admin, {"code": "MRK", "name": "Marikina Footwear Trading"})
    of_id = world.approved(supplier_id=supplier, items=[world.item(dept=5, cost=9000, alloc={NORTH: 2, SOUTH: 1}, description="Shoe", item_code="SHOE-1")], expected_delivery_date="2026-12-01", remarks="Rush")
    order_forms.generate_pos(world.conn, world.buyer, of_id)
    win = MainWindow(world.conn, username_user or world.buyer)
    win.inquiry.tabs.setCurrentIndex(1)
    return win


def test_inquiry_shows_the_supplier_code_with_the_name_on_hover(app, world, store):
    win = _inquiry(world)
    page = win.inquiry
    top = page.tree.topLevelItem(0)
    assert top.text(page.C_WHO) == "MRK" and top.toolTip(page.C_WHO) == "Marikina Footwear Trading"
    # A PO under it still says which branch, and an OF's own line is the only place the code stands for a supplier.
    assert top.child(0).text(page.C_WHO).startswith(str(NORTH)) and top.child(0).toolTip(page.C_WHO) == top.child(0).text(page.C_WHO)
    assert "Marikina Footwear Trading" in page.summary.text()
    win.close()


def test_columns_can_be_hidden_added_and_stay_that_way(app, world, store):
    win = _inquiry(world)
    page = win.inquiry
    tree, checks = page.tree, page.chooser.checks
    extra = {name: col for col, name in enumerate(EXTRA_COLUMNS, len(page.HEADERS))}
    # The usual columns show, the extras wait until chosen, and the number can't be turned off.
    assert not any(tree.isColumnHidden(c) for c in range(len(page.HEADERS))) and all(tree.isColumnHidden(c) for c in extra.values())
    assert "number" not in checks and tree.headerItem().text(page.C_NUMBER) not in [b.text() for b in checks.values()]
    checks["printed"].setChecked(False)
    checks["total"].setChecked(False)
    checks["name"].setChecked(True)
    checks["departments"].setChecked(True)
    assert tree.isColumnHidden(page.C_PRINTED) and tree.isColumnHidden(page.C_TOTAL) and not tree.isColumnHidden(extra["name"])
    top = tree.topLevelItem(0)
    assert top.text(extra["name"]) == "Marikina Footwear Trading" and top.text(extra["delivery"]) == fmt_date("2026-12-01")
    assert top.text(extra["remarks"]) == "Rush" and top.text(extra["products"]) == "1" and top.text(extra["branches"]) == "2" and top.text(extra["prepared"]) == "Bea Buyer"
    assert top.text(extra["departments"]) and top.child(0).text(extra["products"]) == "1" and top.child(0).text(extra["branches"]) == ""
    win.close()

    # Back the next time this person opens the program.
    win = MainWindow(world.conn, world.buyer)
    page = win.inquiry
    assert page.tree.isColumnHidden(page.C_PRINTED) and not page.tree.isColumnHidden(extra["name"]) and page.chooser.checks["name"].isChecked()
    # Someone else on this PC has their own.
    other = MainWindow(world.conn, world.admin)
    assert not other.inquiry.tree.isColumnHidden(other.inquiry.C_PRINTED) and other.inquiry.tree.isColumnHidden(extra["name"])
    other.close()
    page.chooser.reset()
    assert not page.tree.isColumnHidden(page.C_PRINTED) and page.tree.isColumnHidden(extra["name"]) and not page.chooser.checks["name"].isChecked()
    win.close()
    win = MainWindow(world.conn, world.buyer)
    assert not win.inquiry.tree.isColumnHidden(win.inquiry.C_PRINTED)
    win.close()


def test_hiding_every_column_still_leaves_the_order_number(app, world, store):
    win = _inquiry(world)
    page = win.inquiry
    for box in page.chooser.checks.values():
        box.setChecked(False)
    assert not page.tree.isColumnHidden(page.C_NUMBER) and page.tree.topLevelItemCount() == 1
    win.close()
    win = MainWindow(world.conn, world.buyer)
    assert all(not b.isChecked() for b in win.inquiry.chooser.checks.values())
    win.close()


def _orders(world):
    supplier = masters.save_supplier(world.conn, world.admin, {"code": "MRK", "name": "Marikina Footwear Trading"})
    of_id = world.approved(supplier_id=supplier, items=[world.item(dept=5, cost=9000, alloc={NORTH: 2, SOUTH: 1}, description="Shoe", item_code="SHOE-1")], expected_delivery_date="2026-12-01", remarks="Rush")
    order_forms.generate_pos(world.conn, world.buyer, of_id)
    return MainWindow(world.conn, world.buyer)


def test_orders_shows_the_supplier_code_with_the_name_on_hover(app, world, store):
    win = _orders(world)
    page = win.orders_page
    top = page.tree.topLevelItem(0)
    assert top.text(page.C_WHO) == "MRK" and top.toolTip(page.C_WHO) == "Marikina Footwear Trading"
    page.tree.setCurrentItem(top)
    assert "Marikina Footwear Trading" in page.selected_note.text()
    page.tree.setCurrentItem(top.child(0))
    assert "North" in top.child(0).text(page.C_WHO) and "North" in page.selected_note.text()
    win.close()


def test_orders_columns_are_chosen_per_login_and_stay_that_way(app, world, store):
    win = _orders(world)
    page = win.orders_page
    tree, checks = page.tree, page.chooser.checks
    extra = {name: col for col, name in enumerate(EXTRA_COLUMNS, len(page.HEADERS))}
    assert not any(tree.isColumnHidden(c) for c in range(len(page.HEADERS))) and all(tree.isColumnHidden(c) for c in extra.values())
    assert "number" not in checks and page.C_NUMBER not in page.chooser.columns.values()
    checks["printed"].setChecked(False)
    checks["delivery"].setChecked(True)
    checks["remarks"].setChecked(True)
    assert tree.isColumnHidden(page.C_PRINTED) and not tree.isColumnHidden(extra["delivery"])
    top = tree.topLevelItem(0)
    assert top.text(extra["delivery"]) == fmt_date("2026-12-01") and top.text(extra["remarks"]) == "Rush" and top.text(extra["name"]) == "Marikina Footwear Trading"
    # The action above the list doesn't depend on the Next step column.
    checks["next"].setChecked(False)
    assert tree.isColumnHidden(page.C_NEXT) and not page.next_btn.isHidden()
    win.close()

    win = MainWindow(world.conn, world.buyer)
    page = win.orders_page
    assert page.tree.isColumnHidden(page.C_PRINTED) and not page.tree.isColumnHidden(extra["delivery"])
    # Inquiry's choice and Orders' choice are separate, and so is another person's.
    assert not win.inquiry.tree.isColumnHidden(win.inquiry.C_PRINTED)
    other = MainWindow(world.conn, world.admin)
    assert not other.orders_page.tree.isColumnHidden(page.C_PRINTED) and other.orders_page.tree.isColumnHidden(extra["delivery"])
    other.close()
    page.chooser.reset()
    assert not page.tree.isColumnHidden(page.C_PRINTED) and page.tree.isColumnHidden(extra["delivery"])
    win.close()


def test_orders_columns_fill_the_window_whichever_are_showing(app, world, store):
    win = _orders(world)
    win.resize(1400, 860)
    win.show()
    app.processEvents()
    page = win.orders_page
    tree, checks, header = page.tree, page.chooser.checks, page.tree.header()
    def filled():
        app.processEvents()
        return sum(tree.columnWidth(c) for c in range(tree.columnCount())) >= tree.viewport().width()
    assert filled() and tree.columnWidth(page.C_WHO) > 170
    # An extra opens at its full width, not as a sliver.
    checks["name"].setChecked(True)
    assert filled() and tree.columnWidth(len(page.HEADERS)) >= EXTRA_COLUMNS["name"][1]
    assert tree.columnWidth(page.C_WHO) >= tree.fontMetrics().horizontalAdvance("MRK")
    # When the list runs past the window it scrolls, rather than cutting off the next step.
    for name in EXTRA_COLUMNS:
        if name in checks:
            checks[name].setChecked(True)
    top = tree.topLevelItem(0)
    assert tree.columnWidth(page.C_NEXT) >= tree.fontMetrics().horizontalAdvance(top.text(page.C_NEXT))
    for name in EXTRA_COLUMNS:
        if name in checks:
            checks[name].setChecked(False)
    checks["name"].setChecked(True)
    checks["name"].setChecked(False)
    assert filled() and tree.columnWidth(page.C_WHO) > 170
    # With the stretching column itself hidden, another takes the room.
    checks["who"].setChecked(False)
    assert filled()
    page.chooser.reset()
    assert filled() and not header.stretchLastSection()
    win.close()


def test_choosing_columns_changes_no_records(app, world, store):
    win = _inquiry(world)
    before = world.conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
    win.inquiry.chooser.checks["terms"].setChecked(True)
    win.inquiry.chooser.reset()
    assert world.conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0] == before
    win.close()
