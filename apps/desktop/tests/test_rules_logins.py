"""Logins.

Q36 (AUD-181, part): Accounting doesn't buy stock, so one login can't hold Buying and Accounting together. Approver combinations are not decided and stay as they are.
Q21 (AUD-120): warehouse staff may see costs, as they already see them on the OF. Nothing may hide them.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialogButtonBox

from atlaspo import auth, claim_stubs as cs, db, debit_memos as dm, order_forms, orders, pdf, receiving, warehouse
from atlaspo.db import POError
from atlaspo.debit_memos import DAMAGED, SHORT
from atlaspo.ui import common, settings_page

from conftest import NORTH, PASSWORD

CONFLICT = "Buying and Accounting can't be on the same login. Accounting doesn't buy stock. Remove one of the two."


@pytest.fixture
def app():
    yield QApplication.instance() or QApplication([])


def _counts(conn):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("users", "user_roles", "user_chips", "user_titles", "audit_logs")}


def _roles(conn, user_id):
    return sorted(r[0] for r in conn.execute("SELECT role FROM user_roles WHERE user_id = ?", (user_id,)))


def _conflicting_row(world, username="mixed"):
    """A login saved before the rule: made as a purchaser, Accounting added straight into the database."""
    u = auth.create_user(world.conn, world.admin, username, "Mia Mixed", PASSWORD, ["BUYER"], must_change=False)
    with db.tx(world.conn):
        world.conn.execute("INSERT INTO user_roles(user_id, role) VALUES (?, 'ACCOUNTING')", (u.id,))
    return u


# ---------- Q36: the rule itself
def test_role_conflict_only_for_buying_with_accounting():
    assert auth.role_conflict(["BUYER", "ACCOUNTING"]) == CONFLICT
    assert auth.role_conflict({"ADMIN", "BUYER", "ACCOUNTING"}) == CONFLICT
    for fine in (["ACCOUNTING", "APPROVER"], ["BUYER", "APPROVER"], ["ADMIN", "APPROVER"], ["ADMIN", "BUYER"], ["ADMIN", "ACCOUNTING"],
                 ["BUYER"], ["ACCOUNTING"], ["VIEWER", "ACCOUNTING"], [], None):
        assert auth.role_conflict(fine) is None, fine


def test_create_refuses_buying_with_accounting_and_writes_nothing(world):
    before = _counts(world.conn)
    for roles in (["BUYER", "ACCOUNTING"], ["ACCOUNTING", "BUYER", "APPROVER"], ["ADMIN", "BUYER", "ACCOUNTING"]):
        with pytest.raises(POError) as refused:
            auth.create_user(world.conn, world.admin, "both", "Bo Both", PASSWORD, roles, must_change=False)
        assert str(refused.value) == CONFLICT
    assert _counts(world.conn) == before
    assert world.conn.execute("SELECT 1 FROM users WHERE username = 'both'").fetchone() is None


def test_update_refuses_adding_accounting_to_a_buyer_and_writes_nothing(world):
    before = _counts(world.conn)
    with pytest.raises(POError, match="Accounting doesn't buy stock"):
        auth.update_user(world.conn, world.admin, world.buyer.id, "Bea Buyer Changed", ["BUYER", "ACCOUNTING"], True, title="Purchasing Head")
    assert _counts(world.conn) == before
    assert _roles(world.conn, world.buyer.id) == ["BUYER"]
    assert world.conn.execute("SELECT full_name FROM users WHERE id = ?", (world.buyer.id,)).fetchone()[0] == "Bea Buyer"
    assert auth.get_title(world.conn, world.buyer.id) == ""


def test_approver_combinations_still_save(world):
    acap = auth.create_user(world.conn, world.admin, "acap", "Acct Approver", PASSWORD, ["ACCOUNTING", "APPROVER"], must_change=False)
    assert acap.roles == {"ACCOUNTING", "APPROVER"}
    buap = auth.create_user(world.conn, world.admin, "buap", "Buyer Approver", PASSWORD, ["BUYER", "APPROVER"], must_change=False)
    assert buap.roles == {"BUYER", "APPROVER"}
    # The owner's own login: admin, approver and purchaser together keeps saving.
    auth.update_user(world.conn, world.admin, world.admin.id, "Boss Approver", ["ADMIN", "APPROVER", "BUYER"], True)
    assert _roles(world.conn, world.admin.id) == ["ADMIN", "APPROVER", "BUYER"]
    auth.update_user(world.conn, world.admin, acap.id, "Acct Approver", ["ACCOUNTING", "APPROVER", "VIEWER"], True)
    assert _roles(world.conn, acap.id) == ["ACCOUNTING", "APPROVER", "VIEWER"]
    # The receiver and warehouse "alone" rules are unchanged.
    with pytest.raises(POError, match="A warehouse login can't have another role"):
        auth.create_user(world.conn, world.admin, "wa", "W A", PASSWORD, ["WAREHOUSE", "ACCOUNTING"])
    with pytest.raises(POError, match="A receiver can't have another role"):
        auth.create_user(world.conn, world.admin, "ra", "R A", PASSWORD, ["RECEIVER", "BUYER"], branch_code=NORTH)


# ---------- Q36: a login saved before the rule
def test_existing_conflicting_login_is_left_alone_by_opening_the_program(world, env):
    mixed = _conflicting_row(world)
    world.conn.close()
    again = db.open_database()
    try:
        assert _roles(again, mixed.id) == ["ACCOUNTING", "BUYER"]
        assert auth.login(again, "mixed", PASSWORD).roles == {"BUYER", "ACCOUNTING"}
    finally:
        again.close()


def test_existing_conflicting_login_can_be_turned_off_then_fixed(world):
    mixed = _conflicting_row(world)
    # Still refused while it stays active with both roles.
    with pytest.raises(POError, match="Remove one of the two"):
        auth.update_user(world.conn, world.admin, mixed.id, "Mia Mixed", ["BUYER", "ACCOUNTING"], True)
    # Turning it off always works, even before the roles are fixed.
    auth.update_user(world.conn, world.admin, mixed.id, "Mia Mixed", ["BUYER", "ACCOUNTING"], False)
    assert not world.conn.execute("SELECT active FROM users WHERE id = ?", (mixed.id,)).fetchone()[0]
    # A password reset never checks roles.
    auth.reset_password(world.conn, world.admin, mixed.id, "another-password")
    # Removing one role saves in one go, and turns it back on.
    auth.update_user(world.conn, world.admin, mixed.id, "Mia Mixed", ["ACCOUNTING"], True)
    assert _roles(world.conn, mixed.id) == ["ACCOUNTING"]
    assert world.conn.execute("SELECT active FROM users WHERE id = ?", (mixed.id,)).fetchone()[0] == 1


def _users_row(page, user_id):
    t = page.users
    return next(r for r in range(t.rowCount()) if t.item(r, 0).data(Qt.ItemDataRole.UserRole) == user_id)


def test_users_list_flags_conflicting_login_and_one_save_fixes_it(world, app, monkeypatch):
    mixed = _conflicting_row(world)
    page = settings_page.SettingsPage(world.conn, world.admin)
    assert not page.role_conflicts.isHidden()
    assert page.role_conflicts.text() == "1 login has Buying and Accounting together. Open it and remove one role."
    row = _users_row(page, mixed.id)
    cell = page.users.item(row, 3)
    assert "Buying and Accounting together: remove one" in cell.text()
    assert cell.foreground().color().name().lower() == settings_page.theme.current.RED.lower()
    assert cell.toolTip() == CONFLICT
    # Other logins are not flagged.
    plain = page.users.item(_users_row(page, world.buyer.id), 3)
    assert "remove one" not in plain.text()

    shown = []
    monkeypatch.setattr(common, "error", lambda parent, msg: shown.append(msg))
    results = []

    def run(dialog):
        save = dialog.buttons.button(QDialogButtonBox.StandardButton.Save)
        save.click()  # Both roles still ticked: refused, the dialog stays open.
        results.append(dialog.result())
        dialog.roles["ACCOUNTING"].setChecked(False)
        save.click()
        return dialog.result()

    monkeypatch.setattr(settings_page.UserDialog, "exec", run)
    page.users.selectRow(row)
    page.users.setCurrentCell(row, 0)
    page.edit_user()
    assert shown == [CONFLICT] and results == [0]
    assert _roles(world.conn, mixed.id) == ["BUYER"]
    assert page.role_conflicts.isHidden()
    assert "remove one" not in page.users.item(_users_row(page, mixed.id), 3).text()


def test_users_list_counts_several_and_new_user_dialog_refuses(world, app, monkeypatch):
    _conflicting_row(world, "mixed1")
    _conflicting_row(world, "mixed2")
    page = settings_page.SettingsPage(world.conn, world.admin)
    assert page.role_conflicts.text() == "2 logins have Buying and Accounting together. Open each one and remove one role."
    shown = []
    monkeypatch.setattr(common, "error", lambda parent, msg: shown.append(msg))
    before = _counts(world.conn)

    def run(dialog):
        dialog.username.setText("newbie")
        dialog.full_name.setText("New Bee")
        dialog.password.setText(PASSWORD)
        dialog.roles["BUYER"].setChecked(True)
        dialog.roles["ACCOUNTING"].setChecked(True)
        dialog.buttons.button(QDialogButtonBox.StandardButton.Save).click()
        return dialog.result()

    monkeypatch.setattr(settings_page.UserDialog, "exec", run)
    page.new_user()
    assert shown == [CONFLICT]
    assert _counts(world.conn) == before


def test_settings_shows_the_data_folder_read_only(world, app, env):
    page = settings_page.SettingsPage(world.conn, world.admin)
    assert page.data_folder.isReadOnly()
    assert page.data_folder.text() == str(db.data_dir())


# ---------- Q21 (AUD-120): the warehouse still sees costs
@pytest.fixture
def papers(world):
    """Socks at 77.77 a piece for North: 2 short on a debit memo, 3 damaged on a claim stub."""
    of_id = world.approved(items=[world.item(cost=7777, alloc={NORTH: 10}, description="Socks", item_code="SOCK-1")])
    po = order_forms.generate_pos(world.conn, world.buyer, of_id)[0]
    orders.issue(world.conn, world.buyer, po["id"])
    item = orders.get_po(world.conn, po["id"])["items"][0]["id"]
    for name in ("ana", "ben"):
        r = auth.create_user(world.conn, world.admin, name, name.title(), PASSWORD, ["RECEIVER"], must_change=False, branch_code=NORTH)
        receiving.save_count(world.conn, r, po["id"], {item: "8"}, damaged={item: "3"})
    world.of_id, world.po_id = of_id, po["id"]
    world.dm = dm.issue(world.conn, world.buyer, po["id"], {(item, SHORT): "2"})
    world.cs = cs.issue(world.conn, world.buyer, po["id"], {(item, DAMAGED): "3"})
    return world


def test_warehouse_login_still_reads_costs(papers, env):
    w, wh = papers, papers.warehouse
    assert wh.roles == {"WAREHOUSE"}
    memo = dm.get(w.conn, wh, w.dm["id"])
    assert memo["total_c"] == 2 * 7777
    assert [(i["net_unit_c"], i["pieces_m"]) for i in dm.items(w.conn, wh, w.dm["id"])] == [(7777, 2000)]
    assert any(m["id"] == w.dm["id"] and m["total_c"] == 2 * 7777 for m in dm.search(w.conn, wh))
    stub = cs.get(w.conn, wh, w.cs["id"])
    assert stub["total_c"] == 3 * 7777
    assert [(i["net_unit_c"], i["pieces_m"]) for i in cs.items(w.conn, wh, w.cs["id"])] == [(7777, 3000)]
    assert any(s["id"] == w.cs["id"] and s["total_c"] == 3 * 7777 for s in cs.search(w.conn, wh))
    assert dm.document(w.conn, wh, w.dm["id"]).startswith(b"%PDF")
    assert cs.document(w.conn, wh, w.cs["id"]).startswith(b"%PDF")
    # An order still on its way to the warehouse: its lines show there.
    on_way = w.approved(items=[w.item(cost=4500, alloc={NORTH: 6}, description="Caps", item_code="CAP-1")])
    orders.issue(w.conn, w.buyer, order_forms.generate_pos(w.conn, w.buyer, on_way)[0]["id"])
    lines = warehouse.lines(w.conn, wh, on_way)
    assert [(l["description"], l["ordered_m"]) for l in lines] == [("Caps", 6000)]
    assert any(o["id"] == on_way for o in warehouse.search(w.conn, wh))
    # The warehouse copy of the OF, with its costs, still prints for them.
    assert pdf.warehouse_of(w.conn, wh, w.of_id, env / "wh-of.pdf").read_bytes().startswith(b"%PDF")
    for permission in ("receiving.review", "warehouse.receive"):
        assert wh.can(permission)
