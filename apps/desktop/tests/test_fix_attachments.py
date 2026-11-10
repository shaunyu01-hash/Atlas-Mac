"""PO attachments and the PO screen: programs and empty or huge files are refused, a changed file is flagged before it opens,
receivers and the warehouse don't see a PO's files, the right file opens after sorting, and the read-only PO reads like the OF."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QApplication, QComboBox, QDateEdit, QLineEdit

from atlaspo import auth, orders
from atlaspo.db import PermissionDenied, POError, data_dir
from atlaspo.ui import common, po_page as po_module
from atlaspo.ui.main_window import MainWindow

from conftest import NORTH, PASSWORD


@pytest.fixture
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def opened(monkeypatch):
    """Every file the screen starts. Nothing is really started."""
    started = []
    monkeypatch.setattr(os, "startfile", lambda p, *a, **k: started.append(str(p)), raising=False)
    return started


@pytest.fixture
def errors(monkeypatch):
    shown = []
    monkeypatch.setattr("atlaspo.ui.common.error", lambda parent, message: shown.append(message))
    return shown


def _file(tmp_path, name, data=b"supplier quote"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _stored_files():
    folder = data_dir() / "attachments"
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def _plant(world, po_id, name, data=b"@echo off\n"):
    """An older row, from before programs were refused, written straight into the table."""
    import hashlib
    import uuid
    folder = data_dir() / "attachments"
    folder.mkdir(exist_ok=True)
    stored = uuid.uuid4().hex + os.path.splitext(name)[1]
    (folder / stored).write_bytes(data)
    with world.conn:
        world.conn.execute("INSERT INTO po_attachments(po_id, file_name, stored_name, sha256, size_bytes, added_by, added_at)"
                           " VALUES (?,?,?,?,?,?,?)", (po_id, name, stored, hashlib.sha256(data).hexdigest(), len(data), world.buyer.id,
                                                       "2026-11-01T09:00:00"))


# ------------------------------------------------------------ AUD-188: programs

@pytest.mark.parametrize("name", ["invoice.pdf.exe", "run.bat", "setup.MSI", "script.ps1", "link.lnk", "tool.js", "quote.pdf.exe.", "x.cmd ",
                                  "script.py", "site.url", "help.chm", "disk.ISO", "addin.xlam", "app.appref-ms"])
def test_programs_are_refused_and_leave_nothing_behind(world, tmp_path, name):
    po_id = world.po()
    with pytest.raises(POError, match="Programs can't be attached"):
        orders.add_attachment(world.conn, world.buyer, po_id, _file(tmp_path, name))
    assert orders.list_attachments(world.conn, po_id) == [] and _stored_files() == []


def test_documents_are_still_attached(world, tmp_path):
    po_id = world.po()
    for name in ("quote.pdf", "photo.JPG", "prices.xlsx", "notes.txt", "no-suffix"):
        orders.add_attachment(world.conn, world.buyer, po_id, _file(tmp_path, name))
    assert [r["file_name"] for r in orders.list_attachments(world.conn, po_id)] == ["quote.pdf", "photo.JPG", "prices.xlsx", "notes.txt", "no-suffix"]


def test_open_never_starts_an_older_program_row(app, world, opened, errors):
    po_id = world.po()
    _plant(world, po_id, "invoice.pdf.bat")
    w = MainWindow(world.conn, world.buyer)
    w.open_po(po_id)
    page = w.po_page
    page.attach_table.selectRow(0)
    common.guarded(page, page._open_attachment)
    assert opened == [] and errors and "is a program" in errors[0]
    w.close()


# ------------------------------------------------------------ AUD-189: size

def test_empty_file_is_refused(world, tmp_path):
    po_id = world.po()
    with pytest.raises(POError, match="empty"):
        orders.add_attachment(world.conn, world.buyer, po_id, _file(tmp_path, "blank.pdf", b""))
    assert _stored_files() == []


def test_too_large_file_is_refused(world, tmp_path, monkeypatch):
    po_id = world.po()
    monkeypatch.setattr(orders, "MAX_ATTACHMENT_BYTES", 10)
    with pytest.raises(POError, match="too large"):
        orders.add_attachment(world.conn, world.buyer, po_id, _file(tmp_path, "big.pdf", b"x" * 11))
    assert _stored_files() == []
    orders.add_attachment(world.conn, world.buyer, po_id, _file(tmp_path, "ok.pdf", b"x" * 10))
    assert len(orders.list_attachments(world.conn, po_id)) == 1


def test_hash_is_read_in_pieces(world, tmp_path, monkeypatch):
    import hashlib
    monkeypatch.setattr(orders, "_CHUNK", 7)
    data = bytes(range(256)) * 3
    po_id = world.po()
    orders.add_attachment(world.conn, world.buyer, po_id, _file(tmp_path, "q.pdf", data))
    assert orders.list_attachments(world.conn, po_id)[0]["sha256"] == hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------ AUD-128: no orphan file

def test_failed_save_leaves_no_file(world, tmp_path, monkeypatch):
    po_id = world.po()

    def broken_audit(*a, **k):
        raise RuntimeError("disk went away")
    monkeypatch.setattr(orders, "audit", broken_audit)
    with pytest.raises(RuntimeError):
        orders.add_attachment(world.conn, world.buyer, po_id, _file(tmp_path, "quote.pdf"))
    assert _stored_files() == []
    assert world.conn.execute("SELECT COUNT(*) FROM po_attachments").fetchone()[0] == 0


def test_missing_po_leaves_no_file(world, tmp_path):
    with pytest.raises(POError):
        orders.add_attachment(world.conn, world.buyer, 99999, _file(tmp_path, "quote.pdf"))
    assert _stored_files() == []


# ------------------------------------------------------------ AUD-193: changed file

def test_changed_file_warns_before_opening(app, world, tmp_path, opened, monkeypatch):
    po_id = world.po()
    orders.add_attachment(world.conn, world.buyer, po_id, _file(tmp_path, "quote.pdf"))
    row = orders.list_attachments(world.conn, po_id)[0]
    assert not orders.attachment_changed(row)
    orders.attachment_path(row).write_bytes(b"a swapped quote")
    assert orders.attachment_changed(row)
    asked = []
    answer = {"value": False}
    monkeypatch.setattr(po_module, "confirm", lambda parent, title, message, yes="Yes": (asked.append(message), answer["value"])[1])
    w = MainWindow(world.conn, world.buyer)
    w.open_po(po_id)
    page = w.po_page
    page.attach_table.selectRow(0)
    common.guarded(page, page._open_attachment)
    assert asked and "This file changed since it was attached." in asked[0] and opened == []
    answer["value"] = True
    common.guarded(page, page._open_attachment)
    assert len(opened) == 1
    w.close()


def test_unchanged_file_opens_without_asking(app, world, tmp_path, opened, monkeypatch):
    po_id = world.po()
    orders.add_attachment(world.conn, world.buyer, po_id, _file(tmp_path, "quote.pdf"))
    monkeypatch.setattr(po_module, "confirm", lambda *a, **k: pytest.fail("asked about an unchanged file"))
    w = MainWindow(world.conn, world.buyer)
    w.open_po(po_id)
    w.po_page.attach_table.selectRow(0)
    common.guarded(w.po_page, w.po_page._open_attachment)
    assert len(opened) == 1 and opened[0].endswith(".pdf")
    w.close()


# ------------------------------------------------------------ AUD-194: who sees the files

def test_receivers_and_warehouse_cannot_list_a_pos_files(world, tmp_path):
    po_id = world.po()
    orders.add_attachment(world.conn, world.buyer, po_id, _file(tmp_path, "quote.pdf"))
    receiver = auth.create_user(world.conn, world.admin, "rita", "Rita Receiver", PASSWORD, ["RECEIVER"], must_change=False, branch_code=NORTH)
    for user in (receiver, world.warehouse):
        with pytest.raises(PermissionDenied):
            orders.list_attachments(world.conn, po_id, user)
    viewer = auth.create_user(world.conn, world.admin, "vic", "Vic Viewer", PASSWORD, ["VIEWER"], must_change=False)
    accounting = auth.create_user(world.conn, world.admin, "aca", "Aca Accounting", PASSWORD, ["ACCOUNTING"], must_change=False)
    for user in (world.buyer, world.admin, viewer, accounting):
        assert len(orders.list_attachments(world.conn, po_id, user)) == 1
    assert len(orders.list_attachments(world.conn, po_id)) == 1  # the program itself, with no one looking


# ------------------------------------------------------------ AUD-064: sorted list opens the clicked file

def test_sorted_attachments_open_the_selected_file(app, world, tmp_path, opened):
    po_id = world.po()
    for name in ("aaa-quote.txt", "zzz-delivery.txt"):
        orders.add_attachment(world.conn, world.buyer, po_id, _file(tmp_path, name, name.encode() * 10))
    stored = {f["stored_name"]: f["file_name"] for f in orders.list_attachments(world.conn, po_id)}
    w = MainWindow(world.conn, world.buyer)
    w.open_po(po_id)
    t = w.po_page.attach_table
    t.horizontalHeader().sectionClicked.emit(0)
    t.horizontalHeader().sectionClicked.emit(0)
    assert t.item(0, 0).text() == "zzz-delivery.txt"  # the sort really changed the order
    for r in range(t.rowCount()):
        t.selectRow(r)
        opened.clear()
        common.guarded(w.po_page, w.po_page._open_attachment)
        assert stored[os.path.basename(opened[0])] == t.item(r, 0).text()
    w.close()


# ------------------------------------------------------------ AUD-063: add a row on a PO from before Order Forms

def _pre_of_po(world):
    po_id = world.po(total_pesos=100)
    conn = world.conn
    conn.execute("INSERT INTO po_write_guard(token) VALUES ('test')")
    conn.execute("UPDATE purchase_orders SET of_id = NULL, status = 'ISSUED' WHERE id = ?", (po_id,))
    conn.execute("DELETE FROM po_write_guard WHERE token = 'test'")
    conn.commit()
    return po_id


def test_add_row_on_a_po_from_before_order_forms(app, world):
    po_id = _pre_of_po(world)
    page = po_module.POPage(world.conn, world.admin)
    page.open_po(po_id)
    page.start_amendment()
    before = page.grid.rowCount()
    page.add_row()
    page.add_shortcut.activated.emit()
    assert page.grid.rowCount() == before + 2
    assert page.grid.item(before, po_module.C_D1).text() in ("", "0")
    page.close()


# ------------------------------------------------------------ AUD-011 and AUD-025: headings and the read-only header

def test_number_headings_fit_their_columns(app, world):
    po_id = world.po()
    w = MainWindow(world.conn, world.buyer)
    w.resize(1366, 768)
    w.show()
    w.open_po(po_id)
    grid = w.po_page.grid
    header = grid.horizontalHeader()
    fm = QFontMetrics(header.font())
    for col in (po_module.C_COST, po_module.C_NET):
        assert header.sectionSize(col) >= fm.horizontalAdvance(po_module.HEADERS[col]) + po_module.HEADING_PADDING, po_module.HEADERS[col]
    w.close()


def _description_width(world, po_id):
    """Column widths at 1366x768. The headings get the size they have with Windows' own font ('Cost per UOM' about 85 px),
    since the test platform's font is much wider and would cut every heading."""
    w = MainWindow(world.conn, world.buyer)
    w.resize(1366, 768)
    w.show()
    header = w.po_page.grid.horizontalHeader()
    font = header.font()
    scale = 85 / QFontMetrics(font).horizontalAdvance("Cost per UOM")
    if font.pointSizeF() > 0:
        font.setPointSizeF(font.pointSizeF() * scale)
    else:
        font.setPixelSize(max(1, round(font.pixelSize() * scale)))
    header.setFont(font)
    w.open_po(po_id)
    QApplication.processEvents()
    grid = w.po_page.grid
    widths = {c: grid.columnWidth(c) for c in range(grid.columnCount())}
    fm = QFontMetrics(header.font())
    widths["need"] = {c: fm.horizontalAdvance(po_module.HEADERS[c]) + po_module.HEADING_PADDING for c in range(grid.columnCount())}
    w.close()
    return widths


def test_description_keeps_its_width_when_headings_are_fitted(app, world, monkeypatch):
    po_id = world.po()
    fitted = _description_width(world, po_id)
    monkeypatch.setattr(po_module.POPage, "_fit_headers", lambda self: None)
    before = _description_width(world, po_id)
    assert fitted[po_module.C_DESC] >= before[po_module.C_DESC]
    # Columns whose headings already fit are not widened.
    for col in po_module.RIGHT:
        if col in po_module.MORE_COLUMNS:
            continue  # hidden until chosen
        if before["need"][col] <= before[col]:
            assert fitted[col] == before[col], po_module.HEADERS[col]
        else:
            assert fitted[col] == before["need"][col], po_module.HEADERS[col]


def test_a_cut_heading_takes_its_room_from_dept_and_sku(app, world, monkeypatch):
    """With headings wider than their columns, the extra comes out of Dept and SKU, not Description."""
    po_id = world.po()
    before = _description_width(world, po_id)
    monkeypatch.setattr(po_module, "HEADING_PADDING", 60)
    fitted = _description_width(world, po_id)
    assert fitted[po_module.C_COST] > before[po_module.C_COST]
    assert fitted[po_module.C_DEPT] + fitted[po_module.C_CODE] < before[po_module.C_DEPT] + before[po_module.C_CODE]
    assert min(fitted[po_module.C_DEPT], fitted[po_module.C_CODE]) >= po_module.DONOR_MIN_WIDTH


def test_read_only_po_shows_a_fact_line_not_grey_boxes(app, world):
    long_text = "Deliver to the back gate before 10 in the morning, ring the bell twice and ask for the guard on duty " * 2
    po_id = world.po(delivery_instructions=long_text)
    orders.issue(world.conn, world.buyer, po_id)
    w = MainWindow(world.conn, world.buyer)
    w.resize(1366, 768)
    w.show()
    w.open_po(po_id)
    page = w.po_page
    assert page.facts.isVisible() and "|" in page.facts.text() and "ABC Trading" in page.facts.text()
    assert page.notes_line.isVisible() and page.notes_line.text().startswith("Delivery: Deliver to the back gate")
    boxes = [x for kind in (QLineEdit, QComboBox, QDateEdit) for x in page.findChildren(kind)
             if not isinstance(x.parentWidget(), (QComboBox, QDateEdit))]
    assert [x for x in boxes if x.isVisibleTo(page) and not x.isEnabled()] == []
    for box in (page.supplier, page.branch, page.parent_of, page.instructions, page.remarks, page.internal):
        assert not box.isVisibleTo(page)
    # The external PO number can still be typed and saved on an issued PO.
    assert page.legacy.isVisibleTo(page) and page.legacy.isEnabled()
    # Amending brings the boxes back, each showing the start of its text.
    page.start_amendment()
    assert not page.facts.isVisible() and page.instructions.isVisible() and page.instructions.isEnabled()
    assert page.instructions.cursorPosition() == 0
    w.close()
