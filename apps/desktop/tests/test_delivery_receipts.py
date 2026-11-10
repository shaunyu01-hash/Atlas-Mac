"""Delivery receipts: numbered after the PO, no prices, printed here for suppliers we sticker for and emailed to the rest."""
from __future__ import annotations

import os
from io import BytesIO

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from pypdf import PdfReader
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from atlaspo import auth, db, delivery_receipts as dr, masters, order_forms, orders, pdf, schema
from atlaspo.db import POError
from atlaspo.ui import common, pages, theme
from atlaspo.ui.main_window import MainWindow
from atlaspo.ui.of_page import EXTRA_COLUMNS

from conftest import NORTH, PASSWORD, SOUTH


@pytest.fixture
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def store(tmp_path, monkeypatch):
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(theme, "_settings", lambda: settings)


@pytest.fixture
def no_popups(monkeypatch):
    opened, drafts = [], []
    monkeypatch.setattr(pages, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(pages, "confirm_reprint", lambda *a, **k: True)
    monkeypatch.setattr(common, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(os, "startfile", lambda p: opened.append(p), raising=False)
    monkeypatch.setattr(pages, "open_email_draft", lambda *a: drafts.append(a))
    return opened, drafts


def _text(data: bytes) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(BytesIO(data)).pages)


def _set_stickers(world, rate):
    masters.save_supplier(world.conn, world.admin, {"code": "ABC", "name": "ABC Trading", "email": "abc@example.test", "sticker_rate_c": rate}, world.supplier, confirm_duplicate=True)


def _issued(world, **over):
    of_id = world.approved(items=[world.item(cost=12345, alloc={NORTH: 6, SOUTH: 3}, description="Socks", item_code="SOCK-1", selling_c=99900)], **over)
    pos = order_forms.generate_pos(world.conn, world.buyer, of_id)
    for po in pos:
        orders.issue(world.conn, world.buyer, po["id"])
    return of_id, pos


def _first(world, po_id):
    return dr.get(world.conn, dr.ensure_first(world.conn, world.buyer, po_id))


def test_a_dr_is_numbered_after_its_po_and_only_once(world):
    _set_stickers(world, 0)
    _, pos = _issued(world)
    north, south = pos[0], pos[1]
    assert north["po_number"].endswith("-2-00001") and _first(world, north["id"])["dr_number"] == north["po_number"].replace("-2-", "-2-DR-")
    assert _first(world, south["id"])["dr_number"] == south["po_number"].replace("-4-", "-4-DR-")
    assert world.conn.execute("SELECT COUNT(*) FROM delivery_receipts").fetchone()[0] == 2
    # Issuing an amended PO again keeps the same DR.
    item = orders.get_po(world.conn, north["id"])["items"][0]
    items = [{k: item[k] for k in orders.ITEM_FIELDS} | {"id": item["id"], "qty_m": 5000}]
    orders.amend(world.conn, world.buyer, north["id"], {}, items, "Fewer")
    orders.issue(world.conn, world.buyer, north["id"])
    assert world.conn.execute("SELECT COUNT(*) FROM delivery_receipts WHERE po_id = ?", (north["id"],)).fetchone()[0] == 1


def test_a_dr_number_can_never_change_or_go_away(world):
    _, pos = _issued(world)
    d = _first(world, pos[0]["id"])
    with pytest.raises(Exception, match="never change"):
        world.conn.execute("UPDATE delivery_receipts SET dr_number = 'X' WHERE id = ?", (d["id"],))
    with pytest.raises(Exception, match="cannot be deleted"):
        world.conn.execute("DELETE FROM delivery_receipts WHERE id = ?", (d["id"],))


def test_the_dr_shows_no_prices_and_leaves_room_for_the_suppliers(world):
    _, pos = _issued(world)
    text = _text(pdf.render_delivery_receipt(world.conn, _first(world, pos[0]["id"])["id"]))
    for needed in ("DELIVERY RECEIPT", _first(world, pos[0]["id"])["dr_number"], pos[0]["po_number"], "Socks", "SOCK-1", "ordered", "delivered", "Unit price", "Total", "North", "Supplier (name, signature, date)"):
        assert needed in text, needed
    for hidden in ("123.45", "999.00", "740.70", "South"):
        assert hidden not in text, hidden


def test_suppliers_we_sticker_for_get_it_printed_with_their_po(world):
    _set_stickers(world, 50)
    of_id = world.approved(items=[world.item(alloc={NORTH: 3})])
    document, problems = pdf.print_order_form(world.conn, world.buyer, of_id)
    assert not problems and "DELIVERY RECEIPT" in _text(document)
    assert dr.waiting_to_email(world.conn) == []
    assert dr.search(world.conn)[0]["channel"] == dr.OFFICE and dr.search(world.conn)[0]["prints"] == 1


def test_other_suppliers_wait_to_be_emailed_and_not_in_the_printout(world, env):
    _set_stickers(world, 0)
    of_id = world.approved(items=[world.item(alloc={NORTH: 3, SOUTH: 3})])
    document, _ = pdf.print_order_form(world.conn, world.buyer, of_id)
    assert "DELIVERY RECEIPT" not in _text(document)
    group = dr.waiting_to_email(world.conn)[0]
    assert len(dr.waiting_to_email(world.conn)) == 1 and len(group["receipts"]) == 2 and group["email"] == "abc@example.test"
    # Saving twice gives the same unmarked file: it's for the supplier to print.
    path = pdf.receipts_for_email(world.conn, world.buyer, group)
    path = pdf.receipts_for_email(world.conn, world.buyer, group)
    assert path.parent == db.data_dir() / "email" and "COPY" not in _text(path.read_bytes()) and len(PdfReader(path).pages) == 2
    subject, body = dr.email_text(group)
    assert group["reference"] in subject and group["receipts"][0]["dr_number"] in body
    dr.mark_emailed(world.conn, world.buyer, [d["id"] for d in group["receipts"]])
    assert dr.waiting_to_email(world.conn) == []
    assert dr.search(world.conn)[0]["emailed_name"] == "Bea Buyer"


def test_an_amended_po_goes_back_on_the_email_list(world):
    _set_stickers(world, 0)
    _, pos = _issued(world)
    group = dr.waiting_to_email(world.conn)[0]
    dr.mark_emailed(world.conn, world.buyer, [d["id"] for d in group["receipts"]])
    item = orders.get_po(world.conn, pos[0]["id"])["items"][0]
    orders.amend(world.conn, world.buyer, pos[0]["id"], {}, [{k: item[k] for k in orders.ITEM_FIELDS} | {"id": item["id"], "qty_m": 5000}], "Fewer")
    # Not until it's issued again.
    assert dr.waiting_to_email(world.conn) == []
    orders.issue(world.conn, world.buyer, pos[0]["id"])
    assert [d["po_id"] for d in dr.waiting_to_email(world.conn)[0]["receipts"]] == [pos[0]["id"]]


def test_another_delivery_gets_its_own_number(world, monkeypatch):
    monkeypatch.setattr("atlaspo.receiving.live_since", lambda conn: "9999")  # as if issued before receiving existed, so it can close uncounted
    _, pos = _issued(world)
    first = _first(world, pos[0]["id"])
    second = dr.another_delivery(world.conn, world.buyer, pos[0]["id"])
    assert second["dr_number"] == first["dr_number"] + "-2" and second["delivery_no"] == 2
    assert "Delivery 2" in _text(pdf.render_delivery_receipt(world.conn, second["id"]))
    orders.close(world.conn, world.buyer, pos[0]["id"], "Done")
    with pytest.raises(POError, match="isn't waiting for more goods"):
        dr.another_delivery(world.conn, world.buyer, pos[0]["id"])


def test_a_cancelled_po_has_no_usable_dr_and_nothing_to_email(world):
    _set_stickers(world, 0)
    _, pos = _issued(world)
    d = _first(world, pos[0]["id"])
    orders.cancel(world.conn, world.buyer, pos[0]["id"], "Changed our minds")
    with pytest.raises(POError, match="cancelled"):
        pdf.render_delivery_receipt(world.conn, d["id"])
    assert [r["po_id"] for r in dr.waiting_to_email(world.conn)[0]["receipts"]] == [pos[1]["id"]]


def _signature_page(data: bytes, signature: str) -> str:
    pages = [page.extract_text() for page in PdfReader(BytesIO(data)).pages]
    return next(text for text in pages if signature in text)


@pytest.mark.parametrize("lines", [1, 4, 45])
def test_the_supplier_signs_on_the_page_with_the_totals(world, lines):
    _set_stickers(world, 50)
    of_id = world.approved(items=[world.item(alloc={NORTH: 3, SOUTH: 3}, description=f"Product {n}", item_code=f"SKU-{n}") for n in range(lines)], remarks="Check the seals", warehouse_notes="Dock B")
    order_forms.generate_pos(world.conn, world.buyer, of_id)
    page = _signature_page(pdf.render_warehouse_of(world.conn, of_id), "Supplier agrees to costs")
    assert "OF total" in page and "To pay after stickers" in page
    assert "To pay after stickers" in _signature_page(pdf.render_office_of(world.conn, of_id), "Received by accounting")


def test_a_long_order_says_what_each_page_belongs_to_and_gets_initialled(world):
    of_id = world.approved(items=[world.item(alloc={NORTH: 3}, description=f"Product {n}", item_code=f"SKU-{n}") for n in range(60)])
    order_forms.generate_pos(world.conn, world.buyer, of_id)
    number = order_forms.get_of(world.conn, of_id)["of_number"]
    pages = [page.extract_text() for page in PdfReader(BytesIO(pdf.render_warehouse_of(world.conn, of_id))).pages]
    assert len(pages) > 1
    for at, text in enumerate(pages, 1):
        assert number in text and f"Page {at} of {len(pages)}" in text
    # Initials on every page before the last, which has the full signature.
    assert all("Supplier initials" in text for text in pages[:-1]) and "Supplier initials" not in pages[-1] and "Supplier agrees" in pages[-1]
    # The office copy stays inside, so nobody from outside initials it.
    office = [page.extract_text() for page in PdfReader(BytesIO(pdf.render_office_of(world.conn, of_id))).pages]
    assert len(office) > 1 and number in office[-1] and f"of {len(office)}" in office[-1] and not any("Supplier initials" in text for text in office)


def test_a_one_page_order_asks_for_no_initials(world):
    of_id = world.approved(items=[world.item(alloc={NORTH: 3})])
    order_forms.generate_pos(world.conn, world.buyer, of_id)
    text = _text(pdf.render_warehouse_of(world.conn, of_id))
    assert "Page 1 of 1" in text and "Supplier initials" not in text


def test_every_document_prints_upright(world):
    _set_stickers(world, 50)
    of_id, pos = _issued(world)
    po_id = pos[0]["id"]
    documents = {"warehouse": pdf.render_warehouse_of(world.conn, of_id), "office": pdf.render_office_of(world.conn, of_id), "blind": pdf.render_blind_sheet(world.conn, po_id),
                 "slips": pdf.render_cut_slips(world.conn, [po_id]), "po": orders.issued_pdf(world.conn, None, po_id), "dr": pdf.render_delivery_receipt(world.conn, _first(world, po_id)["id"])}
    for name, data in documents.items():
        for page in PdfReader(BytesIO(data)).pages:
            assert float(page.mediabox.height) > float(page.mediabox.width), name


def test_the_po_names_its_dr_and_the_supplier_signs_only_the_of(world):
    _, pos = _issued(world)
    text = _text(orders.issued_pdf(world.conn, None, pos[0]["id"]))
    assert _first(world, pos[0]["id"])["dr_number"] in text and "acknowledgment" not in text


def test_printing_a_dr_again_marks_it_as_a_copy(world):
    _, pos = _issued(world)
    d = _first(world, pos[0]["id"])
    assert "PRINTED AGAIN" not in _text(pdf.delivery_receipt(world.conn, world.buyer, d["id"]))
    assert "COPY 2. PRINTED AGAIN" in _text(pdf.delivery_receipt(world.conn, world.buyer, d["id"]))


def test_a_viewer_cannot_mark_or_make_drs(world):
    _, pos = _issued(world)
    viewer = auth.create_user(world.conn, world.admin, "vic", "Vic Viewer", PASSWORD, ["VIEWER"], must_change=False)
    d = _first(world, pos[0]["id"])
    with pytest.raises(POError):
        dr.mark_emailed(world.conn, viewer, [d["id"]])
    with pytest.raises(POError):
        dr.another_delivery(world.conn, viewer, pos[0]["id"])


def test_a_dr_number_finds_its_order_and_shows_as_a_column(app, world, store):
    _set_stickers(world, 0)
    of_id, pos = _issued(world)
    number = _first(world, pos[1]["id"])["dr_number"]
    assert [r["id"] for r in orders.search(world.conn, text=number)] == [pos[1]["id"]]
    assert [r["id"] for r in order_forms.search(world.conn, text=number)] == [of_id]
    win = MainWindow(world.conn, world.buyer)
    page = win.orders_page
    page.tabs.setCurrentIndex(1)  # An issued PO's order is on Completed.
    page.search.setText(number)
    assert tuple(page.tree.currentItem().data(0, 256)) == ("PO", pos[1]["id"])
    col = len(page.HEADERS) + list(EXTRA_COLUMNS).index("dr")
    assert page.tree.currentItem().text(col) == number and page.tree.currentItem().parent().text(col) == ""
    win.close()


def test_the_print_page_takes_a_dr_from_email_to_sent(app, world, store, no_popups):
    opened, drafts = no_popups
    _set_stickers(world, 0)
    of_id, pos = _issued(world)
    win = MainWindow(world.conn, world.buyer)
    page = win.print_pos
    assert "1 order with DRs to email" in page.email_note.text()
    page.tabs.setCurrentIndex(2)
    assert page.email_table.rowCount() == 1 and page.email_table.item(0, 2).text() == "abc@example.test" and not page.table.isVisibleTo(page)
    assert page.print_btn.text() == "Save DRs and start email" and page.print_btn.isEnabled() and page.sent_btn.isVisibleTo(page)
    page.print_selected()
    path, to, subject, _body = drafts[0]
    assert to == "abc@example.test" and path.exists() and order_forms.get_of(world.conn, of_id)["of_number"] in subject
    assert dr.waiting_to_email(world.conn)  # Saving isn't sending.
    page.mark_sent()
    assert page.email_table.rowCount() == 0 and not page.empty.isHidden()

    page.tabs.setCurrentIndex(3)
    assert page.receipt_table.rowCount() == 2 and "Emailed" in page.receipt_table.item(0, 5).text() and page.print_btn.text() == "Print DR"
    page.print_selected()
    assert opened and page.receipt_table.item(0, 6).text() == "1"
    before = page.receipt_table.rowCount()
    page.another_delivery()
    assert page.receipt_table.rowCount() == before + 1
    # It's for an emailing supplier, so it waits to be emailed rather than printing.
    assert dr.waiting_to_email(world.conn) and page.receipt_table.item(0, 5).text() == "Supplier prints, to email"
    win.close()


def test_a_delivered_order_isnt_shown_as_still_to_email(app, world, store, monkeypatch):
    monkeypatch.setattr("atlaspo.receiving.live_since", lambda conn: "9999")  # as if issued before receiving existed, so it can close uncounted
    _set_stickers(world, 0)
    _, pos = _issued(world)
    orders.close(world.conn, world.buyer, pos[0]["id"], "Delivered")
    win = MainWindow(world.conn, world.buyer)
    page = win.print_pos
    page.tabs.setCurrentIndex(3)
    how = {page.receipt_table.item(r, 1).text(): page.receipt_table.item(r, 5).text() for r in range(page.receipt_table.rowCount())}
    assert how[pos[0]["po_number"]] == "Supplier prints, by email" and how[pos[1]["po_number"]] == "Supplier prints, to email"
    win.close()


def test_no_email_address_says_what_to_do(app, world, store, no_popups):
    masters.save_supplier(world.conn, world.admin, {"code": "ABC", "name": "ABC Trading", "email": "", "sticker_rate_c": 0}, world.supplier, confirm_duplicate=True)
    _issued(world)
    win = MainWindow(world.conn, world.buyer)
    page = win.print_pos
    page.tabs.setCurrentIndex(2)
    assert page.email_table.item(0, 2).text() == "No email on file"
    with pytest.raises(POError, match="Add it under Suppliers"):
        page.print_selected()
    win.close()


def test_a_database_from_before_drs_gains_them_without_losing_anything(env):
    path = env / "old.sqlite"
    c = db.open_database(path)
    boss = auth.create_first_admin(c, "boss", "Boss Man", PASSWORD)
    c.executescript("BEGIN;DROP TABLE delivery_receipts;PRAGMA user_version = 15;COMMIT;")
    c.close()
    conn = db.open_database(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
    assert list((env / "backups").glob("po-before-migration-v15-*.sqlite"))
    assert auth.login(conn, "boss", PASSWORD).id == boss.id and conn.execute("SELECT COUNT(*) FROM delivery_receipts").fetchone()[0] == 0
    conn.close()
