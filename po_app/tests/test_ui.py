"""Drives the real screens with no window on display, including the whole path a buyer follows from OF to issued PO."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import timedelta
from pathlib import Path

import pytest
from openpyxl import load_workbook
from PySide6.QtCore import QDate, QPoint, QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from atlaspo import auth, db, masters, of_layout, order_forms, orders, reports
from atlaspo.db import POError
from atlaspo.ui import common, of_page as of_module, po_page as po_module, theme
from atlaspo.ui.main_window import MainWindow
from atlaspo.ui.of_page import C_COST, C_D1, C_DEPT, C_DESC, C_SIZE
from atlaspo.ui.po_page import C_QTY

from conftest import NORTH, PASSWORD, SOUTH, WEST, EAST


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_popups(monkeypatch):
    """Popups would wait for a click forever. Collect them instead."""
    seen = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: seen.append(a[-1]))
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: seen.append(a[-1]))
    for module in (common, po_module, of_module):
        monkeypatch.setattr(module, "info", lambda parent, title, msg: seen.append(msg))
    monkeypatch.setattr(common, "error", lambda parent, msg: seen.append(msg))
    monkeypatch.setattr(of_module, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(po_module, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(common, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(os, "startfile", lambda *a, **k: None, raising=False)
    return seen


def fill_row(page, r, dept="1 Grocery", desc="Polo", cost="100", size="S-2XL", d1="", alloc=None):
    for col, text in ((C_DEPT, dept), (C_DESC, desc), (C_COST, cost), (C_SIZE, size), (C_D1, d1)):
        page.grid.item(r, col).setText(text)
    for code, qty in (alloc or {NORTH: "10"}).items():
        page.grid.item(r, page.branch_cols[code]).setText(qty)


def test_first_run_login_and_logout_cycle(app, env, monkeypatch):
    import atlaspo.__main__ as entry
    from atlaspo import auth, db
    from atlaspo.ui.main_window import FirstRunDialog, LoginDialog

    def first_exec(dialog):
        dialog.username.setText("boss")
        dialog.full_name.setText("Boss")
        dialog.password.setText("correct-horse")
        dialog.again.setText("wrong-horse!")
        with pytest.raises(db.POError):
            dialog._create()
        dialog.again.setText("correct-horse")
        dialog._create()
        return True

    def login_exec(dialog):
        dialog.username.setText("boss")
        dialog.password.setText("nope-nope-nope")
        dialog._login()
        assert dialog.user is None and "Wrong" in dialog.problem.text()
        dialog.password.setText("correct-horse")
        dialog._login()
        return True

    monkeypatch.setattr(FirstRunDialog, "exec", first_exec)
    monkeypatch.setattr(LoginDialog, "exec", login_exec)
    session = entry.Session(app)
    assert session.start() and session.window.user.username == "boss"
    assert (db.data_dir() / "backups").exists() and list((db.data_dir() / "backups").glob("po-auto-*"))
    session.restart()
    assert session.window.user.username == "boss" and app.quitOnLastWindowClosed()
    assert auth.has_users(session.conn)
    session.window.close()
    session.conn.close()


def test_new_of_shows_only_the_four_active_branches(app, world):
    w = MainWindow(world.conn, world.buyer)
    w.new_of()
    page = w.of_page
    heads = [page.grid.horizontalHeaderItem(c).text() for c in range(page.grid.columnCount())]
    assert [h for h in heads if h in ("North", "South", "East", "West", "Old Town", "Hillside", "Lakeside", "Bayside")] == ["North", "South", "East", "West"]
    w.close()


def test_supplier_default_discount_fills_new_rows(app, world):
    disc = masters.save_supplier(world.conn, world.admin, {"code": "DIS", "name": "Discount House", "default_discount_bp": 500})
    w = MainWindow(world.conn, world.buyer)
    w.new_of()
    page = w.of_page
    common.select_data(page.supplier, disc)
    page.add_row()
    assert page.grid.item(1, C_D1).text() == "5"
    w.close()


def test_leaving_warns_about_unsaved_header_change(app, world):
    of_id = world.draft(10)
    w = MainWindow(world.conn, world.buyer)
    w.open_of(of_id)
    assert not w.of_page._dirty()
    w.of_page.remarks.setText("changed")
    assert w.of_page._dirty()
    w.close()


def test_every_page_opens(app, world):
    w = MainWindow(world.conn, world.admin)
    for row, page in w.nav_rows.items():
        w.nav.setCurrentRow(row)
        assert w.stack.currentWidget() is page
    assert len(w.pages) == 16
    assert not any(text in ("Purchase orders", "Receiving") for text, _ in w.pages)  # Admin doesn't count deliveries
    w.close()


def test_selling_prices_fill_in_and_can_be_changed_by_hand(app, world, monkeypatch):
    from atlaspo import auth, selling
    from atlaspo.db import POError
    from atlaspo.ui.selling_page import C_PRICE
    world.draft(items=[world.item(cost=9900, description="Still a draft")])
    of_id = world.approved(items=[world.item(cost=3000, alloc={NORTH: 6}, description="Tee", item_code="TEE-1"),
                                  world.item(cost=90000, uom="1 DOZEN", alloc={NORTH: 24}, description="Socks", item_code="SOCK-1"),
                                  world.item(cost=5000, alloc={NORTH: 1}, description="Cap", item_code="CAP-1", selling_c=12900)])
    w = MainWindow(world.conn, world.buyer)
    page = w.selling
    assert w.pages[[t for t, _ in w.pages].index("Selling prices")][1] is page
    by_name = {r["description"]: r for r in page.rows}
    assert set(by_name) == {"Tee", "Socks", "Cap"}  # a draft wasn't purchased
    tee = by_name["Tee"]
    assert (tee["cost_c"], tee["auto_c"], tee["price_c"], tee["source"]) == (3000, 5000, 5000, selling.AUTO)
    # A dozen at 900.00 is 75.00 a piece. The price is per piece, whatever the pack.
    assert (by_name["Socks"]["cost_c"], by_name["Socks"]["price_c"]) == (7500, 11000)
    # A price carried on the order, like an older one, is shown but doesn't replace the automatic one.
    cap = by_name["Cap"]
    assert (cap["selling_c"], cap["price_c"], cap["source"]) == (12900, 8000, selling.AUTO)

    row = _selling_row(page, "Tee")[0]
    assert not page.table.item(row, 0).flags() & Qt.ItemFlag.ItemIsEditable
    # Every price ends in 0, so 59 goes up to 60.
    page.table.item(row, C_PRICE).setText("59")
    app.processEvents()
    tee = _selling_row(page, "Tee")[1]
    assert (tee["price_c"], tee["source"], tee["below_srp"]) == (6000, selling.MANUAL, False) and round(tee["margin"], 3) == 0.5
    assert world.conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action='SELLING_PRICE'").fetchone()[0] == 1
    # Under the automatic SRP of 50 is flagged.
    page.table.item(_selling_row(page, "Tee")[0], C_PRICE).setText("41")
    app.processEvents()
    row, tee = _selling_row(page, "Tee")
    assert (tee["price_c"], tee["below_srp"]) == (5000, False)  # 41 rounds up to 50, which is the SRP itself
    page.table.item(row, C_PRICE).setText("35")
    app.processEvents()
    row, tee = _selling_row(page, "Tee")
    assert (tee["price_c"], tee["below_srp"]) == (4000, True) and page.flagged.text() == "1 below SRP on Bought" and "Below SRP, SRP 50.00" in page.table.item(row, 11).text()
    page.table.item(row, C_PRICE).setText("59")
    app.processEvents()

    # An amendment replaces the item rows, and the price by hand stays with the product.
    amended = [world.item(cost=3000, alloc={NORTH: 5}, description="Tee", item_code="TEE-1"), world.item(cost=5000, alloc={NORTH: 1}, description="Cap", item_code="CAP-1", selling_c=12900)]
    assert order_forms.amend(world.conn, world.buyer, of_id, {"remarks": "Cut"}, amended, "Fewer tees")["status"] == "APPLIED"
    page.refresh()
    assert _selling_row(page, "Tee")[1]["price_c"] == 6000

    # Nonsense is refused and changes nothing, and clearing a price goes back.
    refused = []
    monkeypatch.setattr(common, "error", lambda parent, message: refused.append(message))
    page.table.item(_selling_row(page, "Tee")[0], C_PRICE).setText("-5")
    app.processEvents()
    assert refused and _selling_row(page, "Tee")[1]["price_c"] == 6000
    page.table.item(_selling_row(page, "Tee")[0], C_PRICE).setText("")
    app.processEvents()
    tee = _selling_row(page, "Tee")[1]
    assert (tee["price_c"], tee["source"]) == (5000, selling.AUTO)
    w.close()

    # View-only users see prices and can't change them.
    viewer = auth.create_user(world.conn, world.admin, "vic", "Vic Viewer", PASSWORD, ["VIEWER"], must_change=False)
    with pytest.raises(POError):
        selling.set_price(world.conn, viewer, of_id, tee["key"], 100)
    w = MainWindow(world.conn, viewer)
    cell = w.selling.table.item(_selling_row(w.selling, "Tee")[0], C_PRICE)
    assert cell.text() == "50.00" and not cell.flags() & Qt.ItemFlag.ItemIsEditable
    w.close()


def _selling_row(page, description):
    return next((i, r) for i, r in enumerate(page.rows) if r["description"] == description)


def test_orders_columns_fit_what_they_show(app, world):
    of_id = world.approved(items=[world.item(alloc={NORTH: 1, SOUTH: 1})], supplier_id=world.supplier)
    order_forms.generate_pos(world.conn, world.buyer, of_id)
    w = MainWindow(world.conn, world.buyer)
    w.resize(1400, 860)
    w.show()
    app.processEvents()
    page, tree = w.orders_page, w.orders_page.tree
    assert tree.topLevelItemCount() == 1
    # Nothing is cut off and nothing runs past the window.
    fm = tree.fontMetrics()
    item = tree.topLevelItem(0)
    for col in range(tree.columnCount()):
        if col != page.C_WHO and not tree.isColumnHidden(col):
            assert tree.columnWidth(col) >= fm.horizontalAdvance(item.text(col)), col
    assert not tree.horizontalScrollBar().isVisible()
    # They follow the content: a longer next step widens its column.
    before = tree.columnWidth(page.C_NEXT)
    item.setText(page.C_NEXT, "A much longer next step than any of them really has")
    page._fit_columns()
    assert tree.columnWidth(page.C_NEXT) > before
    # When there isn't room for everything, Next step gives way, and Supplier keeps its room.
    item.setText(page.C_NEXT, "An impossibly long next step " * 12)
    page._fit_columns()
    assert tree.columnWidth(page.C_NEXT) < fm.horizontalAdvance(item.text(page.C_NEXT))
    others = sum(tree.columnWidth(c) for c in range(tree.columnCount()) if c != page.C_WHO)
    assert tree.viewport().width() - others >= 170
    w.close()


def test_buyer_has_no_settings(app, world):
    w = MainWindow(world.conn, world.buyer)
    assert w.settings is None and len(w.pages) == 14
    w.close()


def test_buyer_runs_the_whole_of_to_issued_po_path(app, world, no_popups):
    w = MainWindow(world.conn, world.buyer)
    w.new_of()
    page = w.of_page
    common.select_data(page.supplier, world.supplier)
    assert page.terms.currentText() == "30D"
    fill_row(page, 0, d1="10", alloc={NORTH: "6", SOUTH: "4"})
    assert page.grand.text() == "₱900.00"
    assert page.grid.item(0, page.after["Total"]).text() == "10"
    page.add_row()
    fill_row(page, 1, dept="5 Hardware", desc="Boot", cost="500", alloc={NORTH: "1", WEST: "1"})
    assert page.grand.text() == "₱1,900.00"
    assert page.dept_table.rowCount() == 2
    assert page.footer.item(0, page.branch_cols[NORTH]).text() == "7"
    assert page.footer.item(0, page.after["Total"]).text() == "12"
    page.submit_of()
    of = order_forms.get_of(world.conn, page.of_id)
    # Two departments make two OFs, and the screen lands on the first.
    assert of["status"] == "APPROVED" and of["of_number"] and of["grand_total_c"] == 90000
    shoes = next(o for o in order_forms.search(world.conn) if o["id"] != page.of_id)
    assert shoes["of_number"] and shoes["grand_total_c"] == 100000
    assert of["items"][0]["size"] == "S-2XL" and of["items"][0]["disc1_bp"] == 1000
    assert not page.grid.editTriggers() & page.grid.EditTrigger.DoubleClicked
    # A second press after the first must do nothing more.
    page.generate()
    page.generate()
    of = order_forms.get_of(world.conn, page.of_id)
    assert [p["branch_code"] for p in of["pos"]] == [NORTH, SOUTH]
    assert page.po_table.rowCount() == 2
    assert not any(b.text().startswith("Generate") for b in page.findChildren(common.QPushButton) if b.parent() is page and b.isVisible())
    w.stack.setCurrentWidget(w.orders_page)  # a page is brought up to date as it's shown
    assert w.orders_page.item_for(("OF", page.of_id)).childCount() == 2
    # On to the first PO, and issue it.
    w.open_po(of["pos"][0]["id"])
    po_page = w.po_page
    assert of["of_number"] in po_page.parent_of.text() and "North" in po_page.branch.text()
    po_page.issue_po()
    po = orders.get_po(world.conn, of["pos"][0]["id"])
    assert po["status"] == "ISSUED" and orders.issued_versions(world.conn, po["id"])
    assert po_page.versions.rowCount() == 1
    w.close()


def test_enter_walks_the_branch_columns_and_adds_a_row(app, world):
    w = MainWindow(world.conn, world.buyer)
    w.new_of()
    page = w.of_page
    fill_row(page, 0, alloc={NORTH: "5"})
    first, last = page.branch_cols[NORTH], page.branch_cols[WEST]
    page.grid.setCurrentCell(0, first)
    page.advance()
    assert page.grid.currentColumn() == page.branch_cols[SOUTH]
    page.grid.setCurrentCell(0, last)
    page.advance()
    assert page.grid.rowCount() == 2 and page.grid.currentRow() == 1
    w.close()


def test_threshold_note_changes_with_total(app, world):
    w = MainWindow(world.conn, world.buyer)
    w.new_of()
    page = w.of_page
    common.select_data(page.supplier, world.supplier)
    fill_row(page, 0, cost="1000", alloc={NORTH: "60", SOUTH: "40"})
    assert "No approval needed" in page.approval_note.text()
    fill_row(page, 0, cost="1000.01", alloc={NORTH: "60", SOUTH: "40"})
    assert "Needs approval" in page.approval_note.text()
    w.close()


def test_bad_quantity_keeps_draft_unsaved_with_message(app, world, no_popups):
    w = MainWindow(world.conn, world.buyer)
    w.new_of()
    page = w.of_page
    common.select_data(page.supplier, world.supplier)
    fill_row(page, 0, alloc={NORTH: "1.5"})
    common.guarded(page, page.save_draft)
    assert any("whole number" in str(m) for m in no_popups)
    fill_row(page, 0, alloc={NORTH: "-2"})
    common.guarded(page, page.save_draft)
    assert any("negative" in str(m) for m in no_popups)
    assert world.conn.execute("SELECT COUNT(*) FROM order_forms").fetchone()[0] == 0
    w.close()


def test_order_quantity_mismatch_shows_on_the_row_and_blocks_saving(app, world, no_popups):
    w = MainWindow(world.conn, world.buyer)
    w.new_of()
    page = w.of_page
    page.more.setChecked(True)
    common.select_data(page.supplier, world.supplier)
    fill_row(page, 0, alloc={NORTH: "5"})
    page.grid.item(0, page.after["Order qty"]).setText("6")
    assert "add up" in page.grid.item(0, page.after["Total"]).toolTip()
    common.guarded(page, page.save_draft)
    assert any("add up" in str(m) for m in no_popups)
    w.close()


def test_po_reopens_read_only_and_amends_quantity_only(app, world, monkeypatch):
    of_id = world.approved(items=[world.item(cost=10000, alloc={NORTH: 10, SOUTH: 2})])
    po_id = order_forms.generate_pos(world.conn, world.buyer, of_id)[0]["id"]
    w = MainWindow(world.conn, world.buyer)
    w.open_po(po_id)
    page = w.po_page
    assert page.supplier.isReadOnly() and not page.terms.isEnabled()
    page.start_amendment()
    assert page.terms.isEnabled() and not page.vat_mode.isEnabled() and page.add_btn.isHidden()
    assert not page.grid.item(0, po_module.C_COST).flags() & Qt.ItemFlag.ItemIsEditable
    assert page.grid.item(0, C_QTY).flags() & Qt.ItemFlag.ItemIsEditable
    page.grid.item(0, C_QTY).setText("6")
    monkeypatch.setattr(po_module, "ask_text", lambda *a: "Supplier short")
    page.save_amendment()
    after = orders.get_po(world.conn, po_id)
    assert after["revision"] == 1 and after["items"][0]["qty"] == 6 and after["po_number"]
    assert order_forms.get_of(world.conn, of_id)["items"][0]["allocations"] == {NORTH: 6, SOUTH: 2}
    w.close()


def test_approver_decides_from_of_screen(app, world, monkeypatch):
    of_id = world.draft(150000)
    order_forms.submit(world.conn, world.buyer, of_id)
    w = MainWindow(world.conn, world.admin)
    assert w.approvals.ofs.topLevelItemCount() == 1 and "(1)" in w.nav.item(w.approvals_row).text()
    w.open_of(of_id)
    monkeypatch.setattr(of_module, "ask_approve", lambda *a, **k: "Fine")
    w.of_page.approve_of()
    assert order_forms.get_of(world.conn, of_id)["status"] == "APPROVED"
    w.stack.setCurrentWidget(w.approvals)  # a page is brought up to date as it's shown
    assert w.approvals.ofs.topLevelItemCount() == 0
    w.close()


def test_of_with_a_cancelled_branch_shows_the_pos_figures(app, world):
    """The screen shows the live POs' figures after a branch is cancelled, and an approved OF must not read as needing approval.
    A peso discount stays as agreed, so the whole P500 moves onto North's P1,000 (a cancel that would leave
    less than the discount is refused, see test_rules_discount)."""
    of_id = world.approved(items=[world.item(cost=10000, alloc={NORTH: 10, SOUTH: 90})], overall_discount_type="AMOUNT", overall_discount_value=50000)
    pos = order_forms.generate_pos(world.conn, world.buyer, of_id)
    orders.cancel(world.conn, world.buyer, pos[1]["id"], "South doesn't need it")
    assert world.conn.execute("SELECT overall_discount_c FROM purchase_orders WHERE id = ?", (pos[0]["id"],)).fetchone()[0] == 50000
    w = MainWindow(world.conn, world.buyer)
    w.open_of(of_id)
    page = w.of_page
    stored = order_forms.get_of(world.conn, of_id)["grand_total_c"]
    assert page.grand.text() == of_module.fmt_peso(stored)
    assert "Approved" in page.approval_note.text() and "Needs" not in page.approval_note.text()
    assert page.footer.item(1, page.branch_cols[SOUTH]).text() == "0"
    w.close()


def test_catalog_pick_fills_a_row_and_manual_row_can_be_saved(app, world):
    masters.save_product(world.conn, world.admin, {"item_code": "1ABC 100", "department_id": 8, "description": "Kid Tee", "last_cost_c": 12300, "uom": "DOZ", "pieces_per_unit": 12})
    w = MainWindow(world.conn, world.buyer)
    w.new_of()
    page = w.of_page
    common.select_data(page.supplier, world.supplier)
    page._catalog_search("kid")
    page._catalog_pick(next(iter(page._catalog_rows)))
    assert page.grid.rowCount() == 1
    assert page.grid.item(0, C_DESC).text() == "Kid Tee" and page.grid.item(0, C_COST).text() == "123.00"
    assert page.grid.item(0, C_DEPT).text() == "8 Toys and Baby"
    assert page.grid.currentColumn() == page.branch_cols[NORTH]
    page.grid.item(0, page.branch_cols[NORTH]).setText("24")
    assert page.grand.text() == "₱246.00" and "24 pcs" in page.rows["qty"][1].text()
    page.add_row()
    fill_row(page, 1, desc="Typed by hand")
    page.grid.item(1, of_module.C_CODE).setText("NEW-1")
    before = world.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    page.save_draft()
    # A draft adds nothing to the catalog. Approval does.
    assert world.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == before
    w.close()


def test_new_skus_join_the_catalog_only_when_the_of_is_approved(app, world):
    other = masters.save_supplier(world.conn, world.admin, {"code": "XYZ", "name": "Other Works"})
    masters.save_product(world.conn, world.admin, {"item_code": "THEIRS", "department_id": 1, "description": "Their item", "supplier_id": other, "last_cost_c": 500})
    masters.save_product(world.conn, world.admin, {"item_code": "MINE", "department_id": 1, "description": "Keep this name", "supplier_id": world.supplier, "last_cost_c": 500})
    count = lambda: world.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    # Another supplier's SKU is refused straight away, on a draft and on saving it again.
    with pytest.raises(POError, match="theirs belongs to XYZ Other Works"):
        world.draft(items=[world.item(item_code="theirs", description="Clash")])
    big = dict(cost=20000000)
    of_id = world.draft(items=[world.item(item_code="NEW-A", description="New A", size="S-L", color="Red", uom="1 DOZEN", selling_c=9900, **big),
                               world.item(item_code="new-a", description="Same SKU typed again"), world.item(item_code="MINE", description="Changed name"),
                               world.item(description="No SKU, a one-off")])
    assert count() == 2
    assert order_forms.submit(world.conn, world.buyer, of_id) == orders.PENDING
    assert count() == 2  # waiting isn't approved
    order_forms.reject(world.conn, world.admin, of_id, "Not now")
    assert count() == 2  # and a rejected order leaves nothing behind
    order_forms.submit(world.conn, world.buyer, of_id)
    # An approver who can't edit the catalog still adds them, because it's the approved order doing it.
    ana = auth.create_user(world.conn, world.admin, "ana", "Ana", "correct-horse", ["APPROVER"], must_change=False)
    db.set_settings(world.conn, world.admin, {"approval.approver_user_ids": str(ana.id)})
    order_forms.approve(world.conn, ana, of_id)
    products = {r["item_code"]: r for r in world.conn.execute("SELECT * FROM products")}
    assert set(products) == {"NEW-A", "THEIRS", "MINE"}  # nothing without a SKU, and nothing twice
    new = products["NEW-A"]
    assert (new["supplier_id"], new["department_id"], new["size"], new["color"], new["uom"], new["pieces_per_unit"], new["last_cost_c"], new["selling_c"]) == (world.supplier, 1, "S-L", "Red", "1 DOZEN", 12, 20000000, 9900)
    assert products["MINE"]["description"] == "Keep this name"  # never overwritten
    links = {i["description"]: i["product_id"] for i in order_forms.get_of(world.conn, of_id)["items"]}
    assert links["New A"] == new["id"] and links["Same SKU typed again"] == new["id"] and links["Changed name"] == products["MINE"]["id"] and links["No SKU, a one-off"] is None
    # Within the limit it's approved on submit, so it's recorded then.
    auto = world.draft(items=[world.item(item_code="NEW-B", description="New B")])
    assert "NEW-B" not in {r[0] for r in world.conn.execute("SELECT item_code FROM products")}
    assert order_forms.submit(world.conn, world.buyer, auto) == orders.APPROVED
    assert "NEW-B" in {r[0] for r in world.conn.execute("SELECT item_code FROM products")}

    w = MainWindow(world.conn, world.admin)
    catalog = w.products
    catalog.refresh()
    # Organized by supplier, and one supplier can be picked out.
    suppliers = [catalog.table.item(r, 0).text() for r in range(catalog.table.rowCount())]
    assert suppliers == sorted(suppliers) and suppliers[0].startswith("ABC")
    common.select_data(catalog.supplier_filter, other)
    assert [catalog.table.item(r, 1).text() for r in range(catalog.table.rowCount())] == ["THEIRS"]
    w.close()


def test_units_are_pieces_or_packs_and_totals_count_pieces(app, world):
    w = MainWindow(world.conn, world.buyer)
    w.new_of()
    page = w.of_page
    assert list(of_module.OFFERED_UNITS) == ["PC", "3 IN 1", "4 IN 1", "6 IN 1", "1 DOZEN"]
    common.select_data(page.supplier, world.supplier)
    fill_row(page, 0, desc="Socks", cost="30", alloc={NORTH: "30", SOUTH: "6"})
    page.grid.item(0, of_module.C_UNIT).setText("3 IN 1")
    # Typed in pieces: 36 pieces is 12 packs of 3, and the cost is per pack.
    total = page.grid.item(0, page.after["Total"])
    assert total.text() == "36" and page.grid.item(0, page.after["Amount"]).text() == "360.00"
    assert page.grid.item(0, page.after["Per pc"]).text() == "10.00"  # 30.00 a pack of 3, worked out by the program
    assert not page.grid.item(0, page.after["Per pc"]).flags() & Qt.ItemFlag.ItemIsEditable and not page.grid.isColumnHidden(page.after["Per pc"])
    assert "36 pcs" in page.rows["qty"][1].text() and "12 packs" in page.rows["qty"][1].text()
    assert page.footer.item(0, page.branch_cols[NORTH]).text() == "30" and page.footer.item(0, page.after["Total"]).text() == "36"
    assert page.dept_table.item(0, 1).text() == "36"
    # The order quantity is pieces too.
    page.grid.item(0, page.after["Order qty"]).setText("36")
    assert not total.toolTip()
    page.grid.item(0, page.after["Order qty"]).setText("12")
    assert "12 pieces" in total.toolTip()
    page.grid.item(0, page.after["Order qty"]).setText("")
    # What was typed comes back as typed after saving, and a broken pack is refused.
    page.save_draft()
    north, south = [page.grid.item(0, page.branch_cols[b]).text() for b in (NORTH, SOUTH)]
    assert (north, south) == ("30", "6")
    saved = order_forms.get_of(world.conn, page.of_id)
    assert saved["total_pieces_m"] == 36000 and saved["grand_total_c"] == 36000
    page.grid.item(0, page.branch_cols[NORTH]).setText("10")
    with pytest.raises(POError, match="multiple of 3"):
        page._collect_items()
    w.close()


def test_save_and_submit_splits_by_department_and_says_so(app, world, no_popups):
    w = MainWindow(world.conn, world.buyer)
    w.new_of()
    page = w.of_page
    common.select_data(page.supplier, world.supplier)
    _pick_department(page, 5)
    _type_row(page, page.grid.currentRow(), "Shoe")
    _pick_department(page, 1)
    _type_row(page, page.grid.currentRow(), "Shirt")
    assert "2 Order Forms" in page.approval_note.text()
    page.submit_of()
    ofs = order_forms.search(world.conn, tab=orders.OPEN)
    assert len(ofs) == 2 and all(o["of_number"] for o in ofs)
    assert page.of["items"][0]["description"] == "Shirt"  # lands on the first one
    message = no_popups[-1]
    assert all(o["of_number"] in message for o in ofs) and "Grocery" in message and "Hardware" in message
    assert "Order Forms" not in page.approval_note.text()
    w.close()


def date_cleared(page) -> bool:
    return not common.date_value(page.date_from) and not common.date_value(page.date_to)


def test_inquiry_finds_past_orders_by_anything_and_changes_nothing(app, world):
    other = masters.save_supplier(world.conn, world.admin, {"code": "XYZ", "name": "Other Works"})
    first = world.approved(items=[world.item(item_code="SOCK-1", description="Ankle Socks", uom="3 IN 1", cost=9000, alloc={NORTH: 2, SOUTH: 1})], of_date="2026-03-01")
    pos = order_forms.generate_pos(world.conn, world.buyer, first)
    orders.issue(world.conn, world.buyer, pos[0]["id"])
    second = world.approved(items=[world.item(dept=5, description="Boots", alloc={EAST: 1})], supplier_id=other, of_date="2026-09-01")
    draft = world.draft(description="Not numbered yet")
    w = MainWindow(world.conn, world.buyer)
    page = w.inquiry
    names = [w.nav.item(r).text() for r in sorted(w.nav_rows)]
    assert names.index("Inquiry") == names.index("Suppliers") - 1  # under Records, for looking things up
    w.nav.setCurrentRow(next(r for r, p in w.nav_rows.items() if p is page))
    assert w.stack.currentWidget() is page
    found = lambda: [page.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)[1] for i in range(page.tree.topLevelItemCount())]
    # It opens showing nothing, and asks. Typing brings up only what matches.
    assert page.mode == page.ONE and found() == [] and page.more.isHidden() and "Type an OF, PO or DR number" in page.empty.text() and not page.open_btn.isEnabled()
    page.search.setText(order_forms.get_of(world.conn, first)["of_number"])
    assert found() == [first] and page.empty.isHidden()
    page.search.clear()
    assert found() == []
    # By date waits for a date, which is the only thing it needs.
    page.tabs.setCurrentIndex(2)
    assert page.mode == page.DATES and found() == [] and not page.more.isHidden() and "date" in page.empty.text()
    common.set_date(page.date_from, "2026-06-01")
    assert found() == [second]
    # All OFs: everything numbered, newest first. A draft belongs on Orders.
    page.tabs.setCurrentIndex(1)
    assert date_cleared(page) and found() == [second, first] and draft not in found() and page.count.text() == "2 found"
    # By product, PO number, supplier, department, branch, status and date.
    page.product.setText("sock")
    assert found() == [first]
    page.product.setText("nothing-like-this")
    assert found() == [] and page.tree.isHidden() and not page.empty.isHidden() and not page.open_btn.isEnabled()
    page.clear_filters()
    assert found() == [second, first] and not page.tree.isHidden()
    page.search.setText(pos[1]["po_number"])
    assert found() == [first]
    page.clear_filters()
    common.select_data(page.supplier, other)
    assert found() == [second]
    page.clear_filters()
    common.select_data(page.department, 5)
    assert found() == [second]
    page.clear_filters()
    common.select_data(page.branch, SOUTH)
    assert found() == [first] and page.tree.topLevelItem(0).childCount() == 1
    page.clear_filters()
    common.select_data(page.status, orders.CANCELLED)
    assert found() == []
    page.clear_filters()
    common.set_date(page.date_to, "2026-06-30")
    assert found() == [first]
    page.clear_filters()
    # Clicking shows the products in place, in pieces, with the cost per piece.
    top = page.tree.topLevelItem(1)
    page.tree.setCurrentItem(top)
    assert w.stack.currentWidget() is page
    assert page.items.rowCount() == 1 and page.items.item(0, 1).text() == "SOCK-1" and page.items.item(0, 6).text() == "9"
    assert page.items.item(0, 7).text() == "90.00" and page.items.item(0, 9).text() == "30.00" and page.items.item(0, 0).text() == "1 Grocery"
    assert "ABC Trading" in page.summary.text() and "270.00" in page.summary.text()
    page._clicked(top, 0)
    assert top.isExpanded()
    page.tree.setCurrentItem(top.child(0))
    assert page.items.item(0, 6).text() == "6" and "North" in page.summary.text()
    # A look around leaves no mark, and Open goes to the document and back here.
    before = world.conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
    page.refresh()
    assert world.conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0] == before
    page.tree.setCurrentItem(page.tree.topLevelItem(0))
    page._open_selected()
    assert w.stack.currentWidget() is w.of_page and w.of_page.of_id == second
    w._back_from_document()
    assert w.stack.currentWidget() is page
    w.close()


def test_inquiry_works_for_view_only_and_on_older_pos(app, world):
    world.conn.execute("PRAGMA foreign_keys = OFF")
    stamp = "2026-10-01 09:00:00"
    world.conn.execute("INSERT INTO purchase_orders(id, po_number, po_year, po_seq, po_date, supplier_id, branch_code, branch_name, status, prepared_by, created_at, updated_at)"
                       " VALUES (900, '2026PO-00001', 2026, 1, '2026-10-01', ?, 2, 'North', 'ISSUED', 1, ?, ?)", (world.supplier, stamp, stamp))
    viewer = auth.create_user(world.conn, world.admin, "vic", "Vic Viewer", "correct-horse", ["VIEWER"], must_change=False)
    w = MainWindow(world.conn, viewer)
    page = w.inquiry
    page.tabs.setCurrentIndex(1)
    assert page.tree.topLevelItemCount() == 1 and page.tree.topLevelItem(0).text(0) == "2026PO-00001" and page._key() == ("PO", 900)
    w.close()


def test_no_order_carries_vat_for_now(app, world):
    made = masters.save_supplier(world.conn, world.admin, {"code": "PLN", "name": "Plain Supplies Ltd"})
    assert world.conn.execute("SELECT vat_mode FROM suppliers WHERE id=?", (made,)).fetchone()[0] == "NONE"
    w = MainWindow(world.conn, world.admin)
    w.suppliers.clear_form()
    assert w.suppliers.vat_mode.currentData() == "NONE"
    w.new_of()
    assert w.of_page.vat_mode.currentData() == "NONE"
    with_vat = masters.save_supplier(world.conn, world.admin, {"code": "VAT", "name": "Registered Trading", "vat_registered": True, "vat_mode": "INCLUSIVE"})
    w.of_page._load_suppliers()
    common.select_data(w.of_page.supplier, with_vat)
    # For now: no VAT on any order, even for a VAT-registered supplier. There's nothing to choose on screen.
    assert w.of_page.vat_mode.currentData() == "NONE" and not w.of_page.vat_mode.isVisibleTo(w.of_page) and not w.of_page.rows["vat"][0].isVisibleTo(w.of_page)
    of_id = order_forms.create_draft(world.conn, world.buyer, {"supplier_id": with_vat, "of_date": "2026-11-02", "vat_mode": "EXCLUSIVE"}, [world.item()])
    assert order_forms.get_of(world.conn, of_id)["vat_mode"] == "NONE" and order_forms.get_of(world.conn, of_id)["vat_c"] == 0
    w.close()


def test_a_quantity_that_is_not_a_full_pack_says_so_and_never_goes_blank(app, world):
    w = MainWindow(world.conn, world.buyer)
    w.new_of()
    page = w.of_page
    common.select_data(page.supplier, world.supplier)
    assert page.row_problem.isHidden()
    fill_row(page, 0, desc="Socks", cost="120", alloc={NORTH: "30", SOUTH: "24"})
    page.grid.item(0, of_module.C_UNIT).setText("1 DOZEN")
    north, south = page.grid.item(0, page.branch_cols[NORTH]), page.grid.item(0, page.branch_cols[SOUTH])
    # It shows what was typed, marks the one that's wrong, and says what would work.
    assert page.grid.item(0, page.after["Total"]).text() == "54"
    assert not page.row_problem.isHidden() and "Row 1" in page.row_problem.text() and "1 DOZEN comes in 12s" in page.row_problem.text() and "24 or 36" in page.row_problem.text()
    assert north.foreground().color().name().lower() == theme.current.RED.lower() and "24 or 36" in north.toolTip()
    assert not south.toolTip()
    with pytest.raises(POError, match="24 or 36"):
        page._collect_items()
    # Fixed, and everything is worked out: 48 pieces is 4 dozen.
    north.setText("24")
    assert page.row_problem.isHidden() and not north.toolTip()
    assert page.grid.item(0, page.after["Total"]).text() == "48"
    assert page.grid.item(0, page.after["Amount"]).text() == "480.00" and page.grid.item(0, page.after["Per pc"]).text() == "10.00"
    w.close()


def test_sticker_supplier_shows_what_comes_off_the_payment(app, world):
    masters.save_supplier(world.conn, world.admin, {"code": "ABC", "name": "ABC Trading", "sticker_rate_c": 50}, world.supplier, confirm_duplicate=True)
    w = MainWindow(world.conn, world.buyer)
    w.new_of()
    page = w.of_page
    assert page.rows["sticker"][1].isHidden()
    common.select_data(page.supplier, world.supplier)
    fill_row(page, 0, desc="Socks", cost="30", alloc={NORTH: "120"})
    page.grid.item(0, of_module.C_UNIT).setText("1 DOZEN")
    assert page.grand.text() == "₱300.00"  # the OF total itself doesn't change
    assert not page.rows["sticker"][1].isHidden() and page.rows["sticker"][1].text() == "-₱60.00"  # 120 pieces at 0.50
    assert page.rows["net"][1].text() == "₱240.00"
    page.save_draft()
    o = order_forms.get_of(world.conn, page.of_id)
    assert (o["grand_total_c"], o["sticker_deduction_c"], o["net_payable_c"]) == (30000, 6000, 24000)
    w.close()


def test_supplier_form_sets_the_sticker_rate(app, world):
    w = MainWindow(world.conn, world.admin)
    s = w.suppliers
    s.clear_form()
    assert not s.sticker.isChecked() and not s.sticker_rate.isEnabled()
    s.f["code"].setText("STK")
    s.f["name"].setText("Completely Different Sticker Co")
    s.sticker.setChecked(True)
    assert s.sticker_rate.text() == "0.50"
    s.save()
    rate = lambda: world.conn.execute("SELECT sticker_rate_c FROM suppliers WHERE code='STK'").fetchone()[0]
    assert rate() == 50
    s.sticker_rate.setText("0.75")
    s.save()
    assert rate() == 75
    s.sticker.setChecked(False)
    s.save()
    assert rate() == 0
    w.close()


def test_unset_dates_never_show_the_year_2000(app):
    today = QDate.currentDate()
    d = common.date_edit(optional=True)
    assert d.is_unset() and d.text() == "Not set" and common.date_value(d) is None
    # A real click on the arrow opens the calendar on this month, and looking at it changes nothing.
    d.resize(160, 30)
    d.show()
    arrow = QPoint(d.width() - 6, d.height() // 2)
    QTest.mouseClick(d, Qt.MouseButton.LeftButton, pos=arrow)
    calendar = d.calendarWidget()
    assert calendar.isVisible()
    assert (calendar.yearShown(), calendar.monthShown()) == (today.year(), today.month())
    calendar.window().hide()
    assert d.is_unset()
    # An arrow or a typed digit starts from today.
    d.stepBy(1)
    assert d.date() == today and common.date_value(d) == today.toString("yyyy-MM-dd")
    # Delete goes back to not set.
    QTest.keyClick(d, Qt.Key.Key_Delete)
    assert d.is_unset()
    QTest.keyClicks(d, "1")
    assert d.date().year() == today.year()
    # A date already chosen opens where it is, and a required date starts today.
    common.set_date(d, "2027-03-15")
    QTest.mouseClick(d, Qt.MouseButton.LeftButton, pos=arrow)
    assert (calendar.yearShown(), calendar.monthShown()) == (2027, 3)
    calendar.window().hide()
    d.close()
    assert common.date_edit().date() == today


def test_registers_filter_and_count(app, world):
    of_id = world.approved(items=[world.item(dept=3, alloc={NORTH: 1, EAST: 1})])
    order_forms.generate_pos(world.conn, world.buyer, of_id)
    world.draft(items=[world.item(dept=4)])
    w = MainWindow(world.conn, world.admin)
    r = w.orders_page
    assert r.tree.topLevelItemCount() == 2 and r.count_buttons[None].text() == "All open  2"
    r._set_status("DRAFT")
    assert r.tree.topLevelItemCount() == 1
    r._set_status(None)
    common.select_data(r.department, 3)
    assert r.tree.topLevelItemCount() == 1
    # A branch shows only that branch's PO under its order.
    assert r.item_for(("OF", of_id)).childCount() == 2
    common.select_data(r.branch, EAST)
    top = r.item_for(("OF", of_id))
    assert top.childCount() == 1 and "East" in top.child(0).text(r.C_WHO)
    r.search.setText("nothing-like-this")
    assert r.tree.topLevelItemCount() == 0 and r.tree.isHidden() and not r.empty.isHidden()
    w.close()


def test_every_active_branch_stays_on_the_of_at_every_stage(app, world):
    of_id = world.draft(200000, items=[world.item(cost=20000000, alloc={NORTH: 1})])
    w = MainWindow(world.conn, world.buyer)

    def columns():
        w.open_of(of_id)
        return sorted(w.of_page.branch_cols)

    four = [NORTH, SOUTH, EAST, WEST]
    assert columns() == four
    order_forms.submit(world.conn, world.buyer, of_id)
    assert columns() == four
    order_forms.approve(world.conn, world.admin, of_id)
    assert columns() == four
    order_forms.generate_pos(world.conn, world.buyer, of_id)
    assert columns() == four
    # Only North was ordered, so it's still the only PO.
    assert [p["branch_code"] for p in order_forms.get_of(world.conn, of_id)["pos"]] == [NORTH]
    # A branch closed later stays out unless this OF used it.
    masters.save_branch(world.conn, world.admin, {"name": "South", "active": False}, SOUTH)
    assert columns() == [NORTH, EAST, WEST]
    w.close()


def test_rows_group_by_department_on_screen(app, world):
    of_id = world.draft(items=[world.item(dept=5, description="Shoe A"), world.item(dept=1, description="Shirt A"), world.item(dept=5, description="Shoe B"), world.item(dept=1, description="Shirt B")])
    w = MainWindow(world.conn, world.buyer)
    w.open_of(of_id)
    page = w.of_page
    assert [page.grid.item(r, C_DESC).text() for r in range(4)] == ["Shirt A", "Shirt B", "Shoe A", "Shoe B"]
    assert page.grid.group_starts == {2}
    assert page.grid.item(0, C_DEPT).font().bold() and not page.grid.item(1, C_DEPT).font().bold() and page.grid.item(2, C_DEPT).font().bold()
    # The department stays in every row's data, so saving keeps each row right.
    assert [i["department_id"] for i in page._collect_items()] == [1, 1, 5, 5]
    w.close()


def _pick_department(page, dept):
    page.dept_tabs.setCurrentIndex(next(i for i in range(page.dept_tabs.count()) if page.dept_tabs.tabData(i) == dept))


def _type_row(page, r, desc, cost="100", qty="2"):
    page.grid.item(r, C_DESC).setText(desc)
    page.grid.item(r, C_COST).setText(cost)
    page.grid.item(r, page.branch_cols[NORTH]).setText(qty)


def _visible(page):
    return [page.grid.item(r, C_DESC).text() for r in range(page.grid.rowCount()) if not page.grid.isRowHidden(r)]


def test_choose_a_department_then_just_type_the_stocks(app, world):
    w = MainWindow(world.conn, world.buyer)
    w.new_of()
    page = w.of_page
    common.select_data(page.supplier, world.supplier)
    assert not page.grid.isColumnHidden(C_DEPT)  # All shows the department on each row
    _pick_department(page, 5)
    assert page.grid.isColumnHidden(C_DEPT) and page.footer.isColumnHidden(C_DEPT)
    assert page.grid.currentRow() == 0 and page.grid.currentColumn() != C_DEPT
    _type_row(page, 0, "Shoe A")
    # Enter past the last branch makes the next row, already in this department.
    page.grid.setCurrentCell(0, max(page.branch_cols.values()))
    page.advance()
    assert page.grid.currentRow() == 1
    _type_row(page, 1, "Shoe B")
    assert page.dept_tabs.tabText(5) == "5 Hardware  2"

    _pick_department(page, 1)
    assert _visible(page) == [""]  # a fresh row, ready to type in
    _type_row(page, page.grid.currentRow(), "Shirt")
    assert page.dept_tabs.tabText(0) == "All  3" and page.dept_tabs.tabText(1) == "1 Grocery  1"

    _pick_department(page, None)
    assert not page.grid.isColumnHidden(C_DEPT) and "Shoe A" in _visible(page) and "Shirt" in _visible(page)
    assert sorted(i["department_id"] for i in page._collect_items()) == [1, 5, 5]
    # Saving from inside a department stays there, and everything is kept.
    _pick_department(page, 5)
    page.save_draft()
    assert page.dept_filter == 5 and _visible(page)[:2] == ["Shoe A", "Shoe B"]
    assert [i["description"] for i in order_forms.get_of(world.conn, page.of_id)["items"]] == ["Shirt", "Shoe A", "Shoe B"]
    # Reopening starts from All again.
    w.open_of(page.of_id)
    assert page.dept_filter is None and page.dept_tabs.currentIndex() == 0
    w.close()


def test_enter_in_a_department_skips_rows_from_other_departments(app, world):
    of_id = world.draft(items=[world.item(dept=1, description="Shirt A"), world.item(dept=5, description="Shoe"), world.item(dept=1, description="Shirt B")])
    w = MainWindow(world.conn, world.buyer)
    w.open_of(of_id)
    page = w.of_page
    _pick_department(page, 5)
    assert _visible(page) == ["Shoe"]
    row = next(r for r in range(page.grid.rowCount()) if page.grid.item(r, C_DESC).text() == "Shoe")
    page.grid.setCurrentCell(row, max(page.branch_cols.values()))
    page.advance()
    assert page.grid.currentRow() == page.grid.rowCount() - 1 and not page.grid.isRowHidden(page.grid.currentRow())
    assert page.grid.item(page.grid.currentRow(), C_DEPT).text() == "5 Hardware"
    w.close()


def test_catalog_only_offers_the_chosen_department(app, world):
    for code, dept in (("SHOE1", 5), ("SHIRT1", 1)):
        masters.save_product(world.conn, world.admin, {"item_code": code, "department_id": dept, "description": f"Item {code}", "supplier_id": world.supplier})
    w = MainWindow(world.conn, world.buyer)
    w.new_of()
    page = w.of_page
    _pick_department(page, 5)
    page._catalog_search("Item")
    assert [p["item_code"] for p in page._catalog_rows.values()] == ["SHOE1"]
    page._catalog_pick(next(iter(page._catalog_rows)))
    assert _visible(page) == ["Item SHOE1"]
    _pick_department(page, None)
    page._catalog_search("Item")
    assert len(page._catalog_rows) == 2
    w.close()


def test_a_finished_of_can_be_looked_at_one_department_at_a_time(app, world):
    of_id = world.approved(items=[world.item(dept=1, description="Shirt"), world.item(dept=5, description="Shoe")])
    w = MainWindow(world.conn, world.buyer)
    w.open_of(of_id)
    page = w.of_page
    rows = page.grid.rowCount()
    _pick_department(page, 5)
    assert _visible(page) == ["Shoe"] and page.grid.rowCount() == rows  # looking never adds a row
    assert page.grand.text() == "₱200.00"  # the total stays the whole order
    w.close()


def _move_to_top(widget, key):
    row = next(i for i in range(widget.count()) if widget.item(i).data(Qt.ItemDataRole.UserRole) == key)
    widget.insertItem(0, widget.takeItem(row))


def test_admin_rearranges_the_order_form_and_only_admin_can(app, world, monkeypatch):
    monkeypatch.setattr("atlaspo.ui.settings_page.info", lambda *a: None)
    w = MainWindow(world.conn, world.admin)
    page, settings = w.of_page, w.settings
    grid_header = page.grid.horizontalHeader()
    w.new_of()
    assert grid_header.visualIndex(C_DEPT) == 0
    _move_to_top(settings.layout_columns, of_layout.BRANCHES)
    _move_to_top(settings.layout_fields, "terms")
    settings.save_layout()
    # On screen at once, and the footer stays lined up under the grid.
    first_branch = min(page.branch_cols.values())
    assert grid_header.visualIndex(first_branch) == 0 and grid_header.visualIndex(C_DEPT) == 4
    assert page.footer.horizontalHeader().visualIndex(first_branch) == 0
    assert page.field_grid.getItemPosition(page.field_grid.indexOf(page.terms))[:2] == (1, 0)
    # Remembered for the next person, on a new login.
    buyer_window = MainWindow(world.conn, world.buyer)
    buyer_window.new_of()
    assert buyer_window.of_page.grid.horizontalHeader().visualIndex(first_branch) == 0
    assert buyer_window.settings is None  # no Settings page, so no way in
    with pytest.raises(auth.PermissionDenied):
        of_layout.save(world.conn, world.buyer, [], [])
    assert world.conn.execute("SELECT 1 FROM audit_logs WHERE summary LIKE 'layout.of_columns%'").fetchone()
    # Nothing about the order changes.
    fill_row(page, 0, alloc={NORTH: "3"})
    assert [i["allocations_m"] for i in page._collect_items()] == [{NORTH: 3000}]
    buyer_window.close()
    settings.reset_layout()
    assert grid_header.visualIndex(C_DEPT) == 0 and of_layout.column_order(world.conn) == of_layout.COLUMNS
    w.close()


def test_enter_follows_the_columns_as_arranged(app, world):
    columns = [of_layout.BRANCHES, *[c for c in of_layout.COLUMNS if c != of_layout.BRANCHES]]
    of_layout.save(world.conn, world.admin, list(of_layout.FIELDS), columns)
    w = MainWindow(world.conn, world.buyer)
    w.new_of()
    page = w.of_page
    branches = sorted(page.branch_cols.values())
    page.grid.setCurrentCell(0, branches[0])
    page.advance()
    assert page.grid.currentColumn() == branches[1]
    page.grid.setCurrentCell(0, branches[-1])
    fill_row(page, 0, alloc={NORTH: "1"})
    page.grid.setCurrentCell(0, branches[-1])
    page.advance()
    assert page.grid.currentRow() == 1
    w.close()


def test_a_stale_layout_setting_loses_nothing(world):
    db.set_settings(world.conn, world.admin, {"layout.of_fields": "terms,gone,terms", "layout.of_columns": "Total,Not a column"})
    assert of_layout.field_order(world.conn)[0] == "terms" and sorted(of_layout.field_order(world.conn)) == sorted(of_layout.FIELDS)
    assert of_layout.column_order(world.conn)[0] == "Total" and sorted(of_layout.column_order(world.conn)) == sorted(of_layout.COLUMNS)


def test_a_long_branch_name_does_not_widen_the_orders_page(app, world):
    w = MainWindow(world.conn, world.admin)
    r = w.orders_page
    before = r.branch.minimumSizeHint().width()
    masters.save_branch(world.conn, world.admin, {"code": 20, "name": "A Very Long Branch Name Indeed, Far Longer Than Any Real One", "active": False})
    r.reload_choices()
    assert r.branch.count() == 6 and r.branch.minimumSizeHint().width() == before
    w.close()


def test_clicking_an_order_shows_its_pos_on_the_same_page(app, world):
    of_id = world.approved(items=[world.item(alloc={NORTH: 1, EAST: 1})])
    order_forms.generate_pos(world.conn, world.buyer, of_id)
    world.draft()
    w = MainWindow(world.conn, world.buyer)
    r = w.orders_page
    top = r.item_for(("OF", of_id))
    assert top.childCount() == 2 and not top.isExpanded()
    r._clicked(top, 0)
    assert top.isExpanded() and w.stack.currentWidget() is r
    # Stays open when the list refreshes, and closes on the next click.
    r.refresh()
    top = r.item_for(("OF", of_id))
    assert top.isExpanded()
    r._clicked(top, 0)
    assert not top.isExpanded()
    # A draft has no POs, so there's nothing to open.
    draft = r.tree.topLevelItem(0)
    assert draft.childCount() == 0
    r._clicked(draft, 0)
    assert not draft.isExpanded()
    w.close()


def test_double_click_opens_the_right_document(app, world):
    of_id = world.approved(items=[world.item(alloc={NORTH: 1})])
    po = order_forms.generate_pos(world.conn, world.buyer, of_id)[0]
    w = MainWindow(world.conn, world.buyer)
    r = w.orders_page
    # Double-click only shows it in the panel on the right. The panel's button opens the whole OF.
    item = r.item_for(("OF", of_id))
    r.tree.setCurrentItem(item)
    r.tree.itemActivated.emit(item, 0)
    assert w.stack.currentWidget() is w.orders_page and r.panel.key == ("OF", of_id) and r.split.orientation() == Qt.Orientation.Horizontal
    assert r.panel.branches.isVisibleTo(r.panel) and r.panel.branches.item(0, 1).text() == po["po_number"]
    r._open_selected()
    assert w.stack.currentWidget() is w.of_page and w.of_page.of_id == of_id
    r.tree.setCurrentItem(r.item_for(("PO", po["id"])))
    r._open_selected()
    assert w.stack.currentWidget() is w.po_page and w.po_page.po_id == po["id"]
    w.close()


def test_searching_a_po_number_opens_its_order_and_lands_on_it(app, world):
    of_id = world.approved(items=[world.item(alloc={NORTH: 1, EAST: 1})])
    second = order_forms.generate_pos(world.conn, world.buyer, of_id)[1]
    world.draft()
    w = MainWindow(world.conn, world.buyer)
    r = w.orders_page
    r.search.setText(second["po_number"])
    assert r.item_for(("OF", of_id)).isExpanded()
    assert r._key() == ("PO", second["id"]) and r.next_btn.text() == "Go to Print"
    w.close()


def _button_for(page, key):
    page.tree.setCurrentItem(page.item_for(key))
    return page.next_btn.text() if not page.next_btn.isHidden() else None, page.next_btn.isEnabled()


def test_every_order_says_its_next_step_and_one_button_does_it(app, world, env, monkeypatch):
    draft = world.draft(100)
    waiting = world.draft(200000)
    order_forms.submit(world.conn, world.buyer, waiting)
    ready = world.approved(items=[world.item(alloc={NORTH: 1, EAST: 1})])
    opened = []
    monkeypatch.setattr("atlaspo.ui.common.os.startfile", lambda p: opened.append(p), raising=False)
    monkeypatch.setattr("atlaspo.ui.common.confirm", lambda *a, **k: True)
    w = MainWindow(world.conn, world.buyer)
    r = w.orders_page
    steps = {x["id"]: x["next_step"] for x in r.rows}
    assert steps == {draft: "Finish and submit", waiting: "Approve", ready: "Go to Print"}
    assert r.item_for(("OF", ready)).text(r.C_NEXT) == "Go to Print"
    # Always something selected. The list runs newest first now (paging, Nov 8), so that is the OF waiting for approval, which a buyer
    # can't press: its button stays hidden, as checked below.
    assert r._key() == ("OF", waiting) and r.next_btn.isHidden()

    assert _button_for(r, ("OF", draft)) == ("Open and finish", True)
    # The buyer isn't an approver, so sees it's waiting and can't press it.
    # Someone who can't approve has nothing to press, so no button shows. The Next step column still says it's waiting.
    assert _button_for(r, ("OF", waiting)) == (None, False)
    r.do_next_step()
    assert w.stack.currentWidget() is not w.of_page

    # Printing has one place, the Print tab, so the button goes there.
    assert _button_for(r, ("OF", ready)) == ("Go to Print", True)
    r.do_next_step()
    assert w.stack.currentWidget() is w.print_pos
    w.print_pos.ticked = {ready}
    w.print_pos.print_selected()
    w.stack.setCurrentWidget(r)
    of = order_forms.get_of(world.conn, ready)
    assert {p["status"] for p in of["pos"]} == {orders.ISSUED} and len(opened) == 1
    assert ready not in [x["id"] for x in r.rows]
    _tab(r, orders.COMPLETED)
    assert r.item_for(("OF", ready)).text(r.C_NEXT) == "Awaiting delivery"
    assert _button_for(r, ("OF", ready))[0] is None
    w.close()


def test_a_draft_can_be_cancelled_from_the_list_and_is_kept(app, world, monkeypatch):
    draft = world.draft(100)
    waiting = world.draft(200000)
    order_forms.submit(world.conn, world.buyer, waiting)
    ready = world.approved(items=[world.item(alloc={NORTH: 1})])
    monkeypatch.setattr(of_module, "ask_text", lambda *a, **k: "Supplier ran out")
    w = MainWindow(world.conn, world.buyer)
    r = w.orders_page
    _button_for(r, ("OF", draft))
    assert r.cancel_btn.isVisibleTo(r)
    _button_for(r, ("OF", waiting))
    assert r.cancel_btn.isVisibleTo(r)
    _button_for(r, ("OF", ready))
    assert not r.cancel_btn.isVisibleTo(r)  # an approved OF is cancelled from its own screen, where its POs show
    r.tree.setCurrentItem(r.item_for(("OF", draft)))
    r._cancel_selected()
    assert order_forms.get_of(world.conn, draft)["status"] == orders.CANCELLED and draft not in [x["id"] for x in r.rows]
    assert _tab(r, orders.CANCELLED) == "Cancelled  1" and [x["id"] for x in r.rows] == [draft]
    assert r.item_for(("OF", draft)).text(r.C_STATUS) == "Cancelled"
    w.close()
    # A view-only login has no Cancel button.
    viewer = auth.create_user(world.conn, world.admin, "vic", "Vic", PASSWORD, ["VIEWER"], must_change=False)
    w = MainWindow(world.conn, viewer)
    _button_for(w.orders_page, ("OF", waiting))
    assert not w.orders_page.cancel_btn.isVisibleTo(w.orders_page)
    w.close()


def world_tmp_ini(world):
    import os, tempfile
    return os.path.join(tempfile.mkdtemp(), "ui.ini")


def test_double_clicking_an_order_shows_it_on_the_right_with_a_button_for_the_whole_thing(app, world):
    of_id = world.approved(items=[world.item(alloc={NORTH: 2, EAST: 1}, description="Socks", item_code="SOCK-1")], remarks="Christmas order")
    po = order_forms.generate_pos(world.conn, world.buyer, of_id)[0]
    w = MainWindow(world.conn, world.buyer)
    r = w.orders_page
    panel = r.panel
    item = r.item_for(("OF", of_id))
    r.tree.setCurrentItem(item)
    assert not panel.isVisibleTo(r)  # the list keeps its width until someone double-clicks
    r.tree.itemActivated.emit(item, 0)
    assert panel.isVisibleTo(r)
    assert panel.title.text() == order_forms.get_of(world.conn, of_id)["of_number"] and panel.items.rowCount() == 1 and "Socks" in panel.items.item(0, 0).text()
    assert "Christmas order" in panel.notes.text() and panel.open_btn.text() == "Open the whole OF"
    assert w.stack.currentWidget() is r  # a quick look stays on the list
    r.item_for(("OF", of_id)).setExpanded(True)
    r.tree.setCurrentItem(r.item_for(("PO", po["id"])))
    assert panel.title.text() == po["po_number"] and "North" in panel.facts.text() and panel.open_btn.text() == "Open the whole PO"
    # Pieces per branch, when wanted: a column for each branch the OF sends to.
    r.tree.setCurrentItem(r.item_for(("OF", of_id)))
    from atlaspo.ui import theme as theme_module
    settings = QSettings(str(world_tmp_ini(world)), QSettings.Format.IniFormat)
    theme_module_settings = theme_module._settings
    theme_module._settings = lambda: settings
    try:
        panel.by_branch.setChecked(True)
        headers = [panel.items.horizontalHeaderItem(c).text() for c in range(panel.items.columnCount())]
        assert headers == ["Product", "North", "East", "Pieces", "Per pc", "Amount"] and panel.items.item(0, 1).text() == "2" and panel.items.item(0, 2).text() == "1"
        panel.by_branch.setChecked(False)
        assert panel.items.columnCount() == 4
    finally:
        theme_module._settings = theme_module_settings
    r.item_for(("OF", of_id)).setExpanded(True)
    r.tree.setCurrentItem(r.item_for(("PO", po["id"])))
    assert not panel.by_branch.isVisibleTo(panel)
    panel.closed.emit()
    assert not panel.isVisibleTo(r)
    r.tree.itemActivated.emit(r.tree.currentItem(), 0)
    panel.open_btn.click()
    assert w.stack.currentWidget() is w.po_page and w.po_page.po_id == po["id"]
    w.close()


def test_approver_gets_the_approve_button(app, world):
    waiting = world.draft(200000)
    order_forms.submit(world.conn, world.buyer, waiting)
    w = MainWindow(world.conn, world.admin)
    r = w.orders_page
    assert _button_for(r, ("OF", waiting)) == ("Review and approve", True)
    r.do_next_step()
    assert w.stack.currentWidget() is w.of_page and w.of_page.of_id == waiting
    w.close()


def test_waiting_amendment_sends_the_approver_to_approvals(app, world):
    of_id = world.approved(items=[world.item(alloc={NORTH: 1})])
    po = order_forms.generate_pos(world.conn, world.buyer, of_id)[0]
    detail = orders.get_po(world.conn, po["id"])
    items = [{k: detail["items"][0][k] for k in (*orders.ITEM_FIELDS, "id")}]
    items[0]["qty_m"] = 5000
    assert orders.amend(world.conn, world.buyer, po["id"], {}, items, "More")["status"] == orders.PENDING
    w = MainWindow(world.conn, world.admin)
    r = w.orders_page
    assert r.item_for(("OF", of_id)).text(r.C_NEXT) == "Decide the amendment"
    assert _button_for(r, ("OF", of_id)) == ("Decide the amendment", True)
    r.do_next_step()
    assert w.nav.currentRow() == w.approvals_row and w.stack.currentWidget() is w.approvals
    w.close()


def test_a_po_row_sends_printing_to_the_print_tab(app, world, env):
    of_id = world.approved(items=[world.item(alloc={NORTH: 1, EAST: 1})])
    first, second = order_forms.generate_pos(world.conn, world.buyer, of_id)
    w = MainWindow(world.conn, world.buyer)
    r = w.orders_page
    assert _button_for(r, ("PO", first["id"])) == ("Go to Print", True)
    r.do_next_step()
    assert w.stack.currentWidget() is w.print_pos and orders.get_po(world.conn, first["id"])["status"] == orders.APPROVED
    # Reprints are on the Print tab too, so a printed PO has no button here.
    orders.issue(world.conn, world.buyer, first["id"])
    r.refresh()
    assert r.item_for(("OF", of_id)).text(r.C_PRINTED) == "1 of 2" and _button_for(r, ("PO", first["id"]))[0] is None
    # The PO screen doesn't print a PO from an OF either. The Print tab prints it with its warehouse copy and slips.
    w.open_po(second["id"])
    assert "Print and issue" not in [b.text() for b in w.po_page.findChildren(common.QPushButton)]
    w.close()


def test_po_without_an_of_is_its_own_row(app, world):
    world.conn.execute("PRAGMA foreign_keys = OFF")
    world.conn.execute("DROP TRIGGER IF EXISTS po_needs_approved_of")
    stamp = "2026-10-01 09:00:00"
    world.conn.execute("INSERT INTO purchase_orders(id, po_number, po_year, po_seq, po_date, supplier_id, branch_code, branch_name, status, prepared_by, created_at, updated_at)"
                       " VALUES (900, '2026PO-00001', 2026, 1, '2026-10-01', ?, 2, 'North', 'APPROVED', 1, ?, ?)", (world.supplier, stamp, stamp))
    w = MainWindow(world.conn, world.buyer)
    r = w.orders_page
    assert r.item_for(("PO", 900)).parent() is None and r.tabs.tabText(0) == "Open  1"
    assert _button_for(r, ("PO", 900))[0] == "Open and print"  # no OF to print it with, so it prints from its own screen
    _tab(r, orders.COMPLETED)
    assert r.item_for(("PO", 900)) is None
    w.close()


def _tab(register, key):
    bar = register.tabs
    bar.setCurrentIndex(next(i for i in range(bar.count()) if bar.tabData(i) == key))
    return bar.tabText(bar.currentIndex())


def test_completed_and_cancelled_orders_have_their_own_tabs(app, world):
    done = world.approved(items=[world.item(alloc={NORTH: 1, EAST: 1})])
    closed, also_closed = order_forms.generate_pos(world.conn, world.buyer, done)
    half = world.approved(items=[world.item(alloc={NORTH: 1, EAST: 1})])
    still_open, stopped = order_forms.generate_pos(world.conn, world.buyer, half)
    gone = world.approved(items=[world.item(alloc={NORTH: 1})])
    only = order_forms.generate_pos(world.conn, world.buyer, gone)[0]
    draft = world.draft(items=[world.item()])
    import pytest as _pytest
    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr("atlaspo.receiving.live_since", lambda conn: "9999")  # as if issued before receiving existed, so they can close uncounted
        for po in (closed, also_closed):
            orders.issue(world.conn, world.buyer, po["id"])
            orders.close(world.conn, world.buyer, po["id"], "Supplier finished")
    orders.cancel(world.conn, world.buyer, stopped["id"], "Not needed")
    orders.cancel(world.conn, world.buyer, only["id"], "Not needed")

    w = MainWindow(world.conn, world.admin)
    r = w.orders_page
    # Opens on what's still to print: the draft and the OF with one PO not yet issued.
    assert {x["id"] for x in r.rows} == {half, draft}
    assert r.tabs.tabText(0) == "Open  2"
    assert _tab(r, orders.COMPLETED) == "Recently completed  1" and [x["id"] for x in r.rows] == [done]
    assert r.count_buttons[None].isHidden()
    # Cancelled orders keep their own tab, so nothing is lost. Older completed ones are Inquiry's.
    assert r.tabs.count() == 3 and gone not in [x["id"] for x in r.rows]
    assert _tab(r, orders.CANCELLED) == "Cancelled  1" and [x["id"] for x in r.rows] == [gone]
    _tab(r, orders.OPEN)
    r._set_status("DRAFT")
    assert [x["id"] for x in r.rows] == [draft]
    _tab(r, orders.COMPLETED)
    assert r.status_filter is None and [x["id"] for x in r.rows] == [done]
    # Every tab's count follows the search, so an order on another tab can still be found.
    r.search.setText("nothing-like-this")
    assert r.tabs.tabText(1) == "Recently completed  0" and not r.empty.isHidden() and "Inquiry" in r.empty.text()
    # Each PO is under its order, whatever became of it.
    r.search.clear()
    _tab(r, orders.OPEN)
    assert r.item_for(("PO", still_open["id"])).parent() is r.item_for(("OF", half))
    assert r.item_for(("PO", stopped["id"])).text(r.C_STATUS) == "Cancelled" and r.item_for(("PO", stopped["id"])).text(r.C_NEXT) == ""
    _tab(r, orders.COMPLETED)
    assert r.item_for(("PO", closed["id"])).text(r.C_STATUS) == "Closed"
    # Completed a while ago, it moves off the work list.
    world.conn.execute("UPDATE purchase_orders SET issued_at = '2020-01-01 09:00:00' WHERE of_id = ?", (done,))
    r.refresh()
    assert r.tabs.tabText(1) == "Recently completed  0"
    w.close()


def _age(world, of_id, **age):
    stamp = (db.now() - timedelta(**age)).isoformat(sep=" ", timespec="seconds")
    world.conn.execute("UPDATE order_forms SET updated_at=? WHERE id=?", (stamp, of_id))


def test_an_old_draft_is_flagged_but_never_changed(app, world, tmp_path, monkeypatch):
    # The mode switch below must not touch this PC's saved choice.
    store = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(theme, "_settings", lambda: store)
    old, fresh = world.draft(items=[world.item()]), world.draft(items=[world.item()])
    waiting = world.approved(items=[world.item()])
    _age(world, old, days=7, hours=1)
    _age(world, fresh, days=6, hours=23)
    _age(world, waiting, days=30)
    w = MainWindow(world.conn, world.buyer)
    r = w.orders_page
    by_id = {x["id"]: x for x in r.rows}
    assert by_id[old]["delivery"] == "Over 7 days old" and by_id[old]["status"] == orders.DRAFT
    assert by_id[fresh]["delivery"] == "POs not generated" and by_id[waiting]["delivery"] == "POs not generated"
    assert r.item_for(("OF", old)).text(r.C_NEXT) == "Finish and submit, over 7 days old"
    assert r.item_for(("OF", fresh)).text(r.C_NEXT) == "Finish and submit"
    amber = r.item_for(("OF", old)).foreground(r.C_NEXT).color().name().lower()
    assert amber == theme.current.AMBER.lower()
    # Still on Open with its rows intact, and nothing about it is recorded as changed.
    assert order_forms.get_of(world.conn, old)["status"] == orders.DRAFT and old in [x["id"] for x in order_forms.search(world.conn, tab=orders.OPEN)]
    # Repaints with the mode.
    w._toggle_theme()
    amber_now = r.item_for(("OF", old)).foreground(r.C_NEXT).color().name().lower()
    assert amber_now == theme.current.AMBER.lower() and amber_now != amber
    w._toggle_theme()

    page = w.of_page
    w.open_of(fresh)
    assert page.message.isHidden()
    w.open_of(old)
    assert not page.message.isHidden() and "over 7 days" in page.message.text()
    # Saving restarts the count.
    page.save_draft()
    assert page.message.isHidden() or "over 7 days" not in page.message.text()
    assert order_forms.search(world.conn, tab=orders.OPEN) and not order_forms.draft_is_old(order_forms.get_of(world.conn, old))
    w.close()


def test_a_rejected_draft_restarts_the_count(app, world):
    of_id = world.draft(250000)
    assert order_forms.submit(world.conn, world.buyer, of_id) == orders.PENDING
    _age(world, of_id, days=20)
    # While it waits, the delay is the approver's, so it isn't flagged.
    assert not order_forms.draft_is_old(order_forms.get_of(world.conn, of_id))
    order_forms.reject(world.conn, world.admin, of_id, "No budget")
    assert not order_forms.draft_is_old(order_forms.get_of(world.conn, of_id))
    _age(world, of_id, days=8)
    assert order_forms.draft_is_old(order_forms.get_of(world.conn, of_id))
    # Left too long after that, it shows both the reason it came back and the reminder.
    w = MainWindow(world.conn, world.buyer)
    w.open_of(of_id)
    assert "No budget" in w.of_page.message.text() and "over 7 days" in w.of_page.message.text()
    w.close()


def test_printing_for_issue_completes_a_po_but_a_preview_does_not(app, world):
    of_id = world.approved(items=[world.item(alloc={NORTH: 1, EAST: 1})])
    first, second = order_forms.generate_pos(world.conn, world.buyer, of_id)
    w = MainWindow(world.conn, world.buyer)
    r = w.orders_page
    page = w.po_page
    w.open_po(first["id"])
    texts = [b.text() for b in page.findChildren(common.QPushButton)]
    assert "Preview" in texts and "Print and issue" not in texts and "Issue to supplier" not in texts  # it prints from the Print tab

    page.print_pdf()
    assert orders.get_po(world.conn, first["id"])["status"] == orders.APPROVED
    assert r.tabs.tabText(0) == "Open  1"

    # One branch printed: that PO is done, but the OF still has one to go.
    orders.issue(world.conn, world.buyer, first["id"])
    w.refresh_all()
    w.stack.setCurrentWidget(r)  # a page is brought up to date as it's shown
    assert r.tabs.tabText(0) == "Open  1" and r.tabs.tabText(1) == "Recently completed  0"
    assert r.item_for(("OF", of_id)).text(r.C_PRINTED) == "1 of 2"

    orders.issue(world.conn, world.buyer, second["id"])
    w.refresh_all()
    w.stack.setCurrentWidget(r)
    assert r.tabs.tabText(0) == "Open  0" and r.tabs.tabText(1) == "Recently completed  1"
    # Printed isn't delivered, and the status the next step relies on stays Issued.
    row = order_forms.search(world.conn, tab=orders.COMPLETED)[0]
    assert row["id"] == of_id and row["delivery"] == "Awaiting delivery"
    _tab(r, orders.COMPLETED)
    printed = orders.get_po(world.conn, second["id"])["issued_at"]
    assert r.item_for(("OF", of_id)).text(r.C_PRINTED) == common.fmt_date(printed)
    path = Path(db.data_dir()) / "printed.xlsx"
    reports.of_register_excel(r.rows, path)
    sheet = load_workbook(path).active
    cells = [c.value for row_cells in sheet.iter_rows() for c in row_cells]
    assert "Printed" in cells and printed[:10] in [str(c)[:10] for c in cells]
    assert orders.get_po(world.conn, second["id"])["status"] == orders.ISSUED
    w.close()


def test_a_cancelled_branch_does_not_hold_an_of_back(world):
    of_id = world.approved(items=[world.item(alloc={NORTH: 1, EAST: 1})])
    first, second = order_forms.generate_pos(world.conn, world.buyer, of_id)
    orders.issue(world.conn, world.buyer, first["id"])
    assert order_forms.search(world.conn, tab=orders.OPEN)[0]["id"] == of_id
    orders.cancel(world.conn, world.buyer, second["id"], "Branch doesn't need it")
    assert order_forms.search(world.conn, tab=orders.COMPLETED)[0]["id"] == of_id


def test_branch_screen_reactivates_and_renames_but_never_recodes(app, world):
    masters.save_branch(world.conn, world.admin, {"code": 20, "name": "Old Branch", "active": False})
    w = MainWindow(world.conn, world.admin)
    b = w.branches
    row = next(r for r in range(b.table.rowCount()) if b.table.item(r, 1).text() == "Old Branch")
    assert b.table.item(row, 4).text() == "Inactive"
    b.table.setCurrentCell(row, 0)
    assert not b.code.isEnabled()
    b.active.setChecked(True)
    b.save()
    assert world.conn.execute("SELECT active FROM branches WHERE code = 20").fetchone()[0] == 1
    w.new_of()
    assert 20 in w.of_page.branch_cols
    w.close()


def test_supplier_form_limits_code_and_offers_only_standard_terms(app, world):
    w = MainWindow(world.conn, world.admin)
    s = w.suppliers
    s.clear_form()
    assert s.f["code"].maxLength() == 4
    s.f["code"].insert("A-")
    assert s.f["code"].text() == ""  # a dash can't be typed or pasted
    assert [s.terms.itemText(i) for i in range(s.terms.count())] == ["7D", "30D", "45D", "60D", "90D", "None"]
    assert s.terms.currentText() == "None"
    s.f["code"].setText("NEW1")
    s.f["name"].setText("Completely Different Supplier")
    s.terms.setCurrentIndex(s.terms.findText("45D"))
    s.save()
    assert world.conn.execute("SELECT payment_terms FROM suppliers WHERE code='NEW1'").fetchone()[0] == "45D"
    # Or type a custom one. It comes back when the supplier is selected again, and stays off the list for others.
    s.terms.lineEdit().setText("120 days")
    s.save()
    assert world.conn.execute("SELECT payment_terms FROM suppliers WHERE code='NEW1'").fetchone()[0] == "120D"
    s.clear_form()
    assert s.terms.currentText() == "None" and s.terms.count() == 6
    row = next(r for r in range(s.table.rowCount()) if s.table.item(r, 0).text() == "NEW1")
    s.table.setCurrentCell(row, 0)
    assert s.terms.currentText() == "120D" and s.terms.count() == 6
    w.close()


def test_typing_a_term_is_not_autocompleted_into_another(app, world):
    w = MainWindow(world.conn, world.buyer)
    w.new_of()
    terms = w.of_page.terms
    terms.setFocus()
    terms.lineEdit().clear()
    from PySide6.QtTest import QTest
    QTest.keyClicks(terms.lineEdit(), "4")
    assert terms.currentText() == "4"
    w.close()


def test_older_order_form_shows_its_old_terms_only_while_open(app, world):
    of_id = world.draft()
    world.conn.execute("UPDATE order_forms SET payment_terms='3.0% + 30 D' WHERE id=?", (of_id,))
    w = MainWindow(world.conn, world.buyer)
    w.open_of(of_id)
    assert w.of_page.terms.currentText() == "3.0% + 30 D"
    w.new_of()
    assert w.of_page.terms.currentText() == "None"
    assert [w.of_page.terms.itemText(i) for i in range(w.of_page.terms.count())] == ["7D", "30D", "45D", "60D", "90D", "None"]
    w.close()


def test_settings_tick_several_approvers(app, world, monkeypatch):
    second = auth.create_user(world.conn, world.admin, "ana", "Ana Approver", "correct-horse", ["APPROVER"], must_change=False)
    w = MainWindow(world.conn, world.admin)
    page = w.settings
    page.refresh()
    assert page.p_approvers.count() == 2  # the buyer can't approve, so isn't offered
    for i in range(page.p_approvers.count()):
        page.p_approvers.item(i).setCheckState(Qt.CheckState.Checked)
    monkeypatch.setattr("atlaspo.ui.settings_page.info", lambda *a: None)
    page.save_policy()
    assert set(orders.policy(world.conn)["approver_user_ids"]) == {world.admin.id, second.id}
    for i in range(page.p_approvers.count()):
        page.p_approvers.item(i).setCheckState(Qt.CheckState.Unchecked)
    with pytest.raises(POError, match="at least one"):
        page.save_policy()
    w.close()


def _approved_ofs(world, count=2):
    return [world.approved(items=[world.item(alloc={NORTH: 2, SOUTH: 3})]) for _ in range(count)]


def _print_pages(path):
    """Which document each page of a printed file belongs to, in order."""
    from pypdf import PdfReader
    return [page.extract_text() for page in PdfReader(path).pages]


def test_print_tab_comes_after_approvals_with_a_count(app, world):
    ids = _approved_ofs(world)
    w = MainWindow(world.conn, world.buyer)
    names = [w.nav.item(r).text() for r in sorted(w.nav_rows)]
    place = names.index(f"Print  ({len(ids)})")
    assert names[place - 1].startswith("Approvals")
    w.close()


def test_print_tab_prints_each_of_then_its_pos_as_one_file(app, world, env, monkeypatch):
    ids = _approved_ofs(world, 3)
    opened = []
    monkeypatch.setattr("atlaspo.ui.common.os.startfile", lambda p: opened.append(p), raising=False)
    monkeypatch.setattr("atlaspo.ui.common.confirm", lambda *a, **k: True)
    w = MainWindow(world.conn, world.buyer)
    page = w.print_pos
    page.refresh()
    assert [r["id"] for r in page.rows] == ids  # oldest first
    assert not page.print_btn.isEnabled()
    page._row_clicked(0, 1)
    page.table.item(2, 0).setCheckState(Qt.CheckState.Checked)
    assert page.ticked == {ids[0], ids[2]} and page.print_btn.isEnabled() and page.selected_note.text() == "2 selected"
    page.print_selected()
    first, third = order_forms.get_of(world.conn, ids[0]), order_forms.get_of(world.conn, ids[2])
    assert {p["status"] for p in first["pos"] + third["pos"]} == {orders.ISSUED} and len(first["pos"]) == 2
    assert not order_forms.get_of(world.conn, ids[1])["pos"]  # not ticked, so no numbers used
    assert len(opened) == 1
    expected = [first["of_number"], first["pos"][1]["po_number"], first["pos"][0]["po_number"], first["pos"][1]["po_number"],
                third["of_number"], third["pos"][1]["po_number"], third["pos"][0]["po_number"], third["pos"][1]["po_number"]]
    pages = _print_pages(opened[0])
    assert len(pages) == 8 and "WAREHOUSE COPY" in pages[0] and "Branch delivery slip" in pages[1] and first["pos"][0]["po_number"] in pages[1]
    assert "Branch delivery slip" not in pages[2]
    assert not any("BLIND RECEIVING SHEET" in text for text in pages)  # receivers count in the program
    assert not any("FINAL COPY" in text for text in pages)  # accounting's copy waits for the delivery
    assert all(number in text for number, text in zip(expected, pages))
    assert [r["id"] for r in page.rows] == [ids[1]] and not page.ticked
    assert w.nav.item(w.print_row).text() == "Print  (1)"
    page.tabs.setCurrentIndex(1)
    assert {r["id"] for r in page.rows} == {ids[0], ids[2]} and page.print_btn.text() == "Print again"
    page.select_all()
    page.print_selected()
    assert len(opened) == 2 and len(_print_pages(opened[1])) == 8
    assert world.conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action='PO_REPRINT'").fetchone()[0] == 4
    w.close()


def test_paper_count_forms_print_only_when_asked_and_one_for_each_counter(app, world, env, monkeypatch):
    ids = _approved_ofs(world, 2)
    opened = []
    monkeypatch.setattr("atlaspo.ui.common.os.startfile", lambda p: opened.append(p), raising=False)
    monkeypatch.setattr("atlaspo.ui.common.confirm", lambda *a, **k: True)
    w = MainWindow(world.conn, world.buyer)
    page = w.print_pos
    page.refresh()
    assert not page.paper_forms.isChecked() and page.paper_forms.isVisibleTo(page)
    page.ticked = {ids[0]}
    page.paper_forms.setChecked(True)
    page.print_selected()
    pos = order_forms.get_of(world.conn, ids[0])["pos"]
    forms = [text for text in _print_pages(opened[0]) if "BLIND RECEIVING SHEET" in text]
    assert len(forms) == 2 * len(pos) and all(sum(p["po_number"] in text for text in forms) == 2 for p in pos)
    assert all("Backup form" in " ".join(text.split()) and "PRINTED AGAIN" not in text for text in forms)
    # It doesn't stay on for the next order by accident.
    assert not page.paper_forms.isChecked()
    page.ticked = {ids[1]}
    page.print_selected()
    assert not any("BLIND RECEIVING SHEET" in text for text in _print_pages(opened[1]))
    for index, shown in ((1, True), (2, False), (3, False), (4, False)):
        page.tabs.setCurrentIndex(index)
        assert page.paper_forms.isVisibleTo(page) == shown
    w.close()


def test_print_tab_prints_only_whats_left_of_a_part_printed_of(app, world, env, monkeypatch):
    of_id = _approved_ofs(world, 1)[0]
    pos = order_forms.generate_pos(world.conn, world.buyer, of_id)
    orders.issue(world.conn, world.buyer, pos[0]["id"])
    opened = []
    monkeypatch.setattr("atlaspo.ui.common.os.startfile", lambda p: opened.append(p), raising=False)
    monkeypatch.setattr("atlaspo.ui.common.confirm", lambda *a, **k: True)
    w = MainWindow(world.conn, world.buyer)
    page = w.print_pos
    page.refresh()
    assert page.table.item(0, 7).text() == "1 of 2"
    page.select_all()
    page.print_selected()
    pages = _print_pages(opened[0])
    assert len(pages) == 3 and "WAREHOUSE COPY" in pages[0] and pos[1]["po_number"] in pages[2]
    # Only the PO going out now gets a slip.
    assert pos[1]["po_number"] in pages[1] and pos[0]["po_number"] not in pages[1]
    w.close()


def test_print_tab_reports_an_of_it_cant_print_and_prints_the_rest(app, world, env, monkeypatch):
    stuck, fine = _approved_ofs(world)
    masters.save_supplier(world.conn, world.admin, {"code": "ABC", "name": "ABC Trading", "status": "INACTIVE"}, world.supplier, confirm_duplicate=True)
    opened, told = [], []
    monkeypatch.setattr("atlaspo.ui.common.os.startfile", lambda p: opened.append(p), raising=False)
    monkeypatch.setattr("atlaspo.ui.common.confirm", lambda *a, **k: True)
    monkeypatch.setattr("atlaspo.ui.common.error", lambda parent, message: told.append(message))
    w = MainWindow(world.conn, world.buyer)
    page = w.print_pos
    page.refresh()
    page.select_all()
    page.print_selected()
    assert not opened and order_forms.get_of(world.conn, stuck)["of_number"] in told[0] and order_forms.get_of(world.conn, fine)["of_number"] in told[0]
    w.close()


def test_print_tab_going_back_changes_nothing(app, world, monkeypatch):
    ids = _approved_ofs(world, 1)
    monkeypatch.setattr("atlaspo.ui.common.confirm", lambda *a, **k: False)
    w = MainWindow(world.conn, world.buyer)
    page = w.print_pos
    page.refresh()
    page.select_all()
    page.print_selected()
    assert not order_forms.get_of(world.conn, ids[0])["pos"]
    w.close()


def test_view_only_can_reprint_but_not_issue(app, world):
    _approved_ofs(world, 1)
    viewer = auth.create_user(world.conn, world.admin, "vic", "Vic Viewer", "correct-horse", ["VIEWER"], must_change=False)
    w = MainWindow(world.conn, viewer)
    page = w.print_pos
    page.refresh()
    page.select_all()
    assert page.ticked and not page.print_btn.isEnabled()
    w.close()


def test_reports_page_runs_all(app, world, env):
    for dept in (1, 5):
        world.po(items=[world.item(dept=dept, item_code="A1")])
    order_forms.cancel(world.conn, world.buyer, world.draft(10), "test")
    order_forms.submit(world.conn, world.buyer, world.draft(250000))
    w = MainWindow(world.conn, world.admin)
    for row in range(w.reports.list.count()):
        w.reports.list.setCurrentRow(row)
        assert w.reports.report is not None
        assert reports.to_excel(w.reports.report, env / f"r{row}.xlsx").exists()
    w.close()


def test_approvals_open_to_their_products_approve_on_the_row_and_keep_rejections(app, world, monkeypatch):
    from atlaspo.ui import pages as pages_module
    quick = world.draft(items=[world.item(cost=15000000, alloc={NORTH: 1}, description="Big coats", item_code="COAT-1")])
    order_forms.submit(world.conn, world.buyer, quick)
    other = world.draft(150000)
    order_forms.submit(world.conn, world.buyer, other)
    w = MainWindow(world.conn, world.admin)
    page = w.approvals
    top = page.ofs.topLevelItem(0)
    assert top.childCount() == 1 and top.child(0).text(3) == "COAT-1" and "Big coats" in top.child(0).text(4) and top.child(0).text(7) == "1" and top.child(0).text(6) == "150,000.00" and top.text(5) == "BB" and page.ofs.itemWidget(top, page.ofs.columnCount() - 1) is not None
    monkeypatch.setattr(pages_module, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(pages_module, "info", lambda *a, **k: None)
    monkeypatch.setattr(pages_module, "ask_approve", lambda *a, **k: "")
    page._quick_approve(quick)
    assert order_forms.get_of(world.conn, quick)["status"] == orders.APPROVED and page.ofs.topLevelItemCount() == 1
    monkeypatch.setattr(pages_module, "ask_text", lambda *a, **k: "Too dear")
    page._reject(other)
    assert page.tabs.tabText(1) == "Rejected  1"
    page.tabs.setCurrentIndex(1)
    assert page.rejected.isVisibleTo(page) and page.rejected.item(0, 7).text() == "Too dear" and page.rejected.item(0, 8).text() == "Back with the purchaser"
    w.close()


def test_ctrl_o_opens_the_selected_order_and_print_shows_it_on_the_right(app, world):
    of_id = world.approved(items=[world.item(alloc={NORTH: 2})])
    w = MainWindow(world.conn, world.buyer)
    r = w.orders_page
    r.tree.setCurrentItem(r.item_for(("OF", of_id)))
    r.open_shortcut.activated.emit()
    assert w.stack.currentWidget() is w.of_page and w.of_page.of_id == of_id
    w.nav.setCurrentRow(w.print_row)
    page = w.print_pos
    page._open(0, 1)
    assert page.panel.isVisibleTo(page) and page.panel.key == ("OF", of_id) and w.stack.currentWidget() is page
    page.table.setCurrentCell(0, 1)
    page.open_shortcut.activated.emit()
    assert w.stack.currentWidget() is w.of_page
    w.close()


def test_esc_goes_back_and_closes_the_panel(app, world):
    of_id = world.approved(items=[world.item(alloc={NORTH: 2})])
    w = MainWindow(world.conn, world.buyer)
    r = w.orders_page
    item = r.item_for(("OF", of_id))
    r.tree.setCurrentItem(item)
    r.tree.itemActivated.emit(item, 0)
    r.close_shortcut.activated.emit()
    assert not r.panel.isVisibleTo(r)
    w.open_of(of_id)
    w.of_page.escape_shortcut.activated.emit()
    assert w.stack.currentWidget() is r
    w.close()


def test_print_issues_all_of_an_ofs_pos_or_none(world):
    from atlaspo import pdf
    of_id = world.approved(items=[world.item(alloc={NORTH: 2, SOUTH: 1})])
    pos = order_forms.generate_pos(world.conn, world.buyer, of_id)
    # One branch PO waits on an amendment, so it can't go, and neither can the other.
    po = orders.get_po(world.conn, pos[1]["id"])
    orders.amend(world.conn, world.buyer, po["id"], {}, [{**i, "qty": "5", "qty_m": 5000} for i in po["items"]], "More")
    document, problems = pdf.print_order_form(world.conn, world.buyer, of_id)
    assert document is None and "none of its POs" in problems[0]
    assert all(orders.get_po(world.conn, p["id"])["status"] == orders.APPROVED for p in pos)


def test_inquiry_is_under_records_and_orders_opens_it_on_completed(app, world):
    w = MainWindow(world.conn, world.buyer)
    texts = [w.nav.item(i).text() for i in range(w.nav.count())]
    assert texts.index("Records") < texts.index("Inquiry") < texts.index("Suppliers")
    w.orders_page.go_completed.emit()
    assert w.stack.currentWidget() is w.inquiry and w.inquiry.mode == w.inquiry.ALL
    w.close()
