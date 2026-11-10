"""Anyone can print a document again, but the copy says so, the screens show it, and it's recorded, so two copies can't be mistaken for two documents."""
from __future__ import annotations

import os
from io import BytesIO

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from pypdf import PdfReader
from PySide6.QtWidgets import QApplication

from atlaspo import auth, claim_stubs as cs, debit_memos as dm, order_forms, orders, pdf, receiving
from atlaspo.debit_memos import DAMAGED, SHORT
from atlaspo.ui.main_window import MainWindow

from conftest import NORTH, PASSWORD


@pytest.fixture
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_popups(monkeypatch):
    """Popups would wait for a click forever."""
    for module in ("atlaspo.ui.common", "atlaspo.ui.of_page", "atlaspo.ui.po_page", "atlaspo.ui.debit_memo_page"):
        monkeypatch.setattr(f"{module}.confirm", lambda *a, **k: True)
    monkeypatch.setattr(os, "startfile", lambda *a, **k: None, raising=False)


@pytest.fixture
def setup(world):
    of_id = world.approved(items=[world.item(cost=10000, alloc={NORTH: 10}, description="Socks", item_code="SOCK-1")])
    world.of_id = of_id
    world.po_id = order_forms.generate_pos(world.conn, world.buyer, of_id)[0]["id"]
    orders.issue(world.conn, world.buyer, world.po_id)
    world.item_id = orders.get_po(world.conn, world.po_id)["items"][0]["id"]
    make = lambda name: auth.create_user(world.conn, world.admin, name, name.title(), PASSWORD, ["RECEIVER"], must_change=False, branch_code=NORTH)
    world.r1, world.r2 = make("ana"), make("ben")
    return world


def _received(w, pieces="10", **kw):
    for who in (w.r1, w.r2):
        receiving.save_count(w.conn, who, w.po_id, {w.item_id: pieces}, **kw)


def _text(data: bytes, page: int = 0) -> str:
    return " ".join(PdfReader(BytesIO(data)).pages[page].extract_text().split())


def _flagged(data: bytes, copy: int) -> bool:
    return f"COPY {copy}. PRINTED AGAIN" in _text(data) and "BY BEA BUYER" in _text(data)


def _plain(data: bytes) -> bool:
    return "PRINTED AGAIN" not in _text(data)


def test_a_po_printed_again_says_so_and_the_kept_file_does_not_change(setup):
    w = setup
    original = orders.issued_pdf(w.conn, None, w.po_id)
    assert _plain(original)
    assert _flagged(orders.issued_pdf(w.conn, w.buyer, w.po_id), 2)
    assert _flagged(orders.issued_pdf(w.conn, w.buyer, w.po_id), 3)
    assert orders.issued_pdf(w.conn, None, w.po_id) == original
    assert "ABC Trading" in _text(orders.issued_pdf(w.conn, w.buyer, w.po_id))  # still the whole document


def test_the_print_set_printed_again_is_marked_on_every_page(setup):
    w = setup
    data = pdf.reprint_order_form(w.conn, w.buyer, w.of_id)
    reader = PdfReader(BytesIO(data))
    assert len(reader.pages) > 1 and all("COPY 2. PRINTED AGAIN" in " ".join(p.extract_text().split()) for p in reader.pages)
    assert pdf.reprint_counts(w.conn)[("OF", w.of_id)] == 1 and pdf.reprint_counts(w.conn)[("PO", w.po_id)] == 1


def test_a_part_printed_of_printing_its_other_pos_is_not_a_reprint(world):
    of_id = world.approved(items=[world.item(alloc={NORTH: 1, 4: 1})])
    pos = order_forms.generate_pos(world.conn, world.buyer, of_id)
    orders.issue(world.conn, world.buyer, pos[0]["id"])
    data, _ = pdf.print_order_form(world.conn, world.buyer, of_id)
    assert data and _plain(data) and ("OF", of_id) not in pdf.reprint_counts(world.conn)


def test_the_warehouse_copy_is_marked_from_its_second_print(setup, env):
    w = setup
    fresh = w.approved(items=[w.item(alloc={NORTH: 1}, description="Other")])
    assert _plain(pdf.warehouse_of(w.conn, w.buyer, fresh, env / "first.pdf").read_bytes())
    assert _flagged(pdf.warehouse_of(w.conn, w.buyer, fresh, env / "again.pdf").read_bytes(), 2)
    # Printing it with the POs counts as its first print too.
    third = w.approved(items=[w.item(alloc={NORTH: 1}, description="Third")])
    assert pdf.print_order_form(w.conn, w.buyer, third)[0]
    assert _flagged(pdf.warehouse_of(w.conn, w.buyer, third, env / "third.pdf").read_bytes(), 2)


def test_the_office_copy_is_marked_from_its_second_print(setup):
    w = setup
    _received(w)
    assert _plain(pdf.office_copy(w.conn, w.admin, w.of_id))
    assert _flagged(pdf.office_copy(w.conn, w.buyer, w.of_id), 2)


def test_a_blank_count_form_is_never_marked_as_a_copy_but_is_recorded(setup):
    w = setup
    assert _plain(pdf.blind_copy(w.conn, w.buyer, w.po_id)) and _plain(pdf.blind_copy(w.conn, w.buyer, w.po_id))
    assert w.conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action = 'BLIND_PRINT' AND po_id = ?", (w.po_id,)).fetchone()[0] == 2


def test_a_debit_memo_is_marked_from_its_second_print(setup):
    w = setup
    _received(w, "8")
    made = dm.issue(w.conn, w.buyer, w.po_id, {(w.item_id, SHORT): "2"})
    kept = dm.document(w.conn, w.buyer, made["id"])
    assert _plain(kept) and dm.get(w.conn, w.buyer, made["id"])["prints"] == 1
    assert _flagged(dm.document(w.conn, w.buyer, made["id"]), 2)
    assert dm.get(w.conn, w.buyer, made["id"])["prints"] == 2
    assert w.conn.execute("SELECT pdf FROM debit_memos WHERE id = ?", (made["id"],)).fetchone()[0] == kept


def test_a_claim_stub_is_marked_from_its_second_print(setup):
    w = setup
    _received(w, damaged={w.item_id: "2"})
    made = cs.issue(w.conn, w.buyer, w.po_id, {(w.item_id, DAMAGED): "2"})
    kept = cs.document(w.conn, w.buyer, made["id"])
    assert _plain(kept)
    assert _flagged(cs.document(w.conn, w.buyer, made["id"]), 2)
    assert cs.get(w.conn, w.buyer, made["id"])["prints"] == 2
    assert w.conn.execute("SELECT pdf FROM claim_stubs WHERE id = ?", (made["id"],)).fetchone()[0] == kept


def test_it_asks_first_and_going_back_prints_nothing(app, setup, monkeypatch):
    w = setup
    opened, asked = [], []
    monkeypatch.setattr(os, "startfile", lambda p: opened.append(p), raising=False)
    monkeypatch.setattr("atlaspo.ui.common.confirm", lambda parent, title, message, *a: asked.append(message) or False)
    win = MainWindow(w.conn, w.buyer)
    page = win.po_page
    win.open_po(w.po_id)
    page.print_pdf()
    page.versions.selectRow(0)
    page.open_version()
    assert len(asked) == 2 and "marked as printed again" in asked[0] and not opened
    assert ("PO", w.po_id) not in pdf.reprint_counts(w.conn)
    monkeypatch.setattr("atlaspo.ui.common.confirm", lambda *a, **k: True)
    page.print_pdf()
    assert len(opened) == 1 and pdf.reprint_counts(w.conn)[("PO", w.po_id)] == 1
    win.close()


def test_previews_are_free_and_issuing_prints_without_asking(app, world, monkeypatch):
    opened = []
    monkeypatch.setattr(os, "startfile", lambda p: opened.append(p), raising=False)
    monkeypatch.setattr("atlaspo.ui.common.confirm", lambda *a, **k: pytest.fail("A first print asks nothing"))
    of_id = world.approved(items=[world.item(alloc={NORTH: 1})])
    po = order_forms.generate_pos(world.conn, world.buyer, of_id)[0]
    win = MainWindow(world.conn, world.buyer)
    win.open_po(po["id"])
    win.po_page.print_pdf()
    win.po_page.print_pdf()
    assert len(opened) == 2 and orders.get_po(world.conn, po["id"])["status"] == orders.APPROVED
    win.po_page.issue_po()
    assert len(opened) == 3 and orders.get_po(world.conn, po["id"])["status"] == orders.ISSUED
    assert not pdf.reprint_counts(world.conn)
    win.close()


def test_the_screens_show_what_has_printed_more_than_once(app, setup, monkeypatch):
    w = setup
    monkeypatch.setattr(os, "startfile", lambda *a, **k: None, raising=False)
    win = MainWindow(w.conn, w.buyer)
    orders_page, inquiry = win.orders_page, win.inquiry
    orders_page.tabs.setCurrentIndex(1)
    of_row = orders_page.item_for(("OF", w.of_id))
    assert "x)" not in of_row.text(orders_page.C_PRINTED)
    # Anyone who prints can print again, from the Print tab.
    page = win.print_pos
    page.tabs.setCurrentIndex(1)
    page.select_all()
    assert page.print_btn.isEnabled()
    page.print_selected()
    assert page.table.item(0, 7).text().endswith("(2x)")
    orders_page.refresh()
    of_row = orders_page.item_for(("OF", w.of_id))
    assert of_row.text(orders_page.C_PRINTED).endswith("(2x)") and of_row.child(0).text(orders_page.C_PRINTED).endswith("(2x)")
    inquiry.tabs.setCurrentIndex(1)
    assert inquiry.tree.topLevelItem(0).text(inquiry.C_PRINTED).endswith("(2x)")
    win.close()


def test_debit_memos_page_shows_times_printed(app, setup, monkeypatch):
    w = setup
    monkeypatch.setattr(os, "startfile", lambda *a, **k: None, raising=False)
    _received(w, "8")
    made = dm.issue(w.conn, w.buyer, w.po_id, {(w.item_id, SHORT): "2"})
    dm.document(w.conn, w.buyer, made["id"])
    win = MainWindow(w.conn, w.buyer)
    memos = win.debit_memos
    memos.refresh()
    assert memos.table.item(0, 10).text() == ""
    memos.table.selectRow(0)
    memos.print_again()
    memos.refresh()
    assert memos.table.item(0, 10).text() == "2 times"
    win.close()

