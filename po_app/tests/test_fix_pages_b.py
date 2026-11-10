"""Audit 2026-11-07 fixes on the Suppliers, Product catalog, Branches and Reports pages
(AUD-006, 013, 017, 029, 031, 034, 080, 085, 177, 178, 185)."""
from __future__ import annotations

import dataclasses
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QDate, QSettings, Qt
from PySide6.QtGui import QValidator
from PySide6.QtWidgets import QApplication, QComboBox, QCheckBox, QFormLayout, QLineEdit, QMessageBox

from conftest import PASSWORD
from atlaspo import auth, masters, orders, reports
from atlaspo.db import POError
from atlaspo.ui import common, pages, theme
from atlaspo.ui.pages import BranchesPage, ProductsPage, ReportsPage, SuppliersPage


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def quiet(monkeypatch, tmp_path):
    """No popups, and the per-person preferences go to a temp file, never the Windows user's own."""
    seen = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: seen.append(a[-1]))
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: seen.append(a[-1]))
    monkeypatch.setattr(pages, "info", lambda parent, title, msg: seen.append(msg))
    monkeypatch.setattr(common, "error", lambda parent, msg: seen.append(msg))
    monkeypatch.setattr(theme, "_settings", lambda: QSettings(str(tmp_path / "prefs.ini"), QSettings.Format.IniFormat))
    return seen


def viewer(world):
    return auth.create_user(world.conn, world.admin, "vic", "Vic Viewer", PASSWORD, ["VIEWER"], must_change=False)


def product(world, code="A001", desc="Polo shirt"):
    return masters.save_product(world.conn, world.admin, {"item_code": code, "description": desc, "department_id": 1, "uom": "PC", "pieces_per_unit": 1,
                                                          "supplier_id": world.supplier, "last_cost_c": 10000, "active": True})


# ---------- AUD-029: a new database says what to do next
def test_fresh_database_says_what_to_do_next(app, conn):
    admin = auth.create_first_admin(conn, "boss", "Boss Person", PASSWORD)
    s = SuppliersPage(conn, admin)
    assert not s.empty.isHidden() and s.empty.text() == "No suppliers yet. Press New supplier to add one."
    p = ProductsPage(conn, admin)
    assert not p.empty.isHidden() and p.empty.text() == "No products yet. Press New product to add one."
    assert p.count.isHidden()
    v = auth.create_user(conn, admin, "vic", "Vic Viewer", PASSWORD, ["VIEWER"], must_change=False)
    # Someone who can't add one isn't told to press a button they don't have.
    assert SuppliersPage(conn, v).empty.text() == "No suppliers yet."
    assert ProductsPage(conn, v).empty.text() == "No products yet."


def test_a_list_with_rows_shows_no_empty_note(app, world):
    product(world)
    assert SuppliersPage(world.conn, world.admin).empty.isHidden()
    assert ProductsPage(world.conn, world.admin).empty.isHidden()


# ---------- AUD-185: a search that drops the selected record clears the form
def test_supplier_form_clears_when_the_search_drops_it(app, world):
    s = SuppliersPage(world.conn, world.admin)
    s.table.selectRow(0)
    assert s.current_id == world.supplier and s.f["name"].text() == "ABC Trading" and s.save_btn.isEnabled()
    s.search.setText("zzqqxx")
    assert s.table.rowCount() == 0
    assert s.current_id is None and s.f["name"].text() == "" and s.f["code"].text() == ""
    assert not s.save_btn.isEnabled()
    assert not s.empty.isHidden() and "zzqqxx" in s.empty.text()
    s.search.setText("")
    assert not s.save_btn.isEnabled()  # nothing chosen yet
    s.table.selectRow(0)
    assert s.save_btn.isEnabled() and s.current_id == world.supplier
    # A search that still lists it leaves the form as it was.
    s.search.setText("ABC")
    assert s.current_id == world.supplier and s.f["name"].text() == "ABC Trading"
    # New supplier makes the form ready again.
    s.search.setText("zzqqxx")
    s._new_pressed()
    assert s.save_btn.isEnabled()


def test_product_form_clears_when_the_search_drops_it(app, world):
    pid = product(world)
    p = ProductsPage(world.conn, world.admin)
    p.table.selectRow(0)
    assert p.current_id == pid and p.code.text() == "A001" and p.save_btn.isEnabled()
    p.search.setText("zzqqxx")
    assert p.table.rowCount() == 0
    assert p.current_id is None and p.code.text() == "" and p.desc.text() == ""
    assert not p.save_btn.isEnabled()
    assert not p.empty.isHidden() and p.empty.text().startswith("No product matches")
    p.search.setText("")
    p.table.selectRow(0)
    assert p.save_btn.isEnabled()
    p.clear_form()
    assert p.save_btn.isEnabled()


def test_supplier_just_saved_stays_on_the_form_when_the_search_hides_it(app, world, quiet):
    s = SuppliersPage(world.conn, world.admin)
    s.search.setText("ABC")
    s._new_pressed()
    s.f["code"].setText("QQ1")
    s.f["name"].setText("Quite Unusual Quilts")
    s.save()
    new_id = world.conn.execute("SELECT id FROM suppliers WHERE code='QQ1'").fetchone()[0]
    assert "QQ1" not in {s.table.item(r, 0).text() for r in range(s.table.rowCount())}  # the search still hides it
    assert s.current_id == new_id and s.f["name"].text() == "Quite Unusual Quilts" and s.save_btn.isEnabled()
    # A typo fixed straight away goes onto the same supplier.
    s.f["name"].setText("Quite Unusual Quilts Inc")
    s.save()
    assert world.conn.execute("SELECT name FROM suppliers WHERE id=?", (new_id,)).fetchone()[0] == "Quite Unusual Quilts Inc"
    assert world.conn.execute("SELECT COUNT(*) FROM suppliers WHERE code='QQ1'").fetchone()[0] == 1
    # Changing the search afterwards still clears it, as before.
    s.search.setText("ABCD")
    assert s.current_id is None and not s.save_btn.isEnabled()


def test_product_just_saved_stays_on_the_form_past_the_first_300(app, world, quiet):
    for i in range(320):
        product(world, code=f"B{i:04d}", desc=f"Bulk item {i}")
    p = ProductsPage(world.conn, world.admin)
    assert p.table.rowCount() == 300
    p.clear_form()
    p.code.setText("ZZNEW1")
    p.desc.setText("Brand new item")
    p.save()
    new_id = world.conn.execute("SELECT id FROM products WHERE item_code='ZZNEW1'").fetchone()[0]
    listed = {p.table.item(r, 1).text() for r in range(p.table.rowCount())}
    assert "ZZNEW1" not in listed  # not among the 300 shown
    assert p.current_id == new_id and p.code.text() == "ZZNEW1" and p.save_btn.isEnabled()
    p.desc.setText("Brand new item, blue")
    p.save()
    assert world.conn.execute("SELECT description FROM products WHERE id=?", (new_id,)).fetchone()[0] == "Brand new item, blue"
    assert world.conn.execute("SELECT COUNT(*) FROM products WHERE item_code='ZZNEW1'").fetchone()[0] == 1
    # A filter that drops it still clears the form.
    p.search.setText("zzqqxx")
    assert p.current_id is None and not p.save_btn.isEnabled()


# ---------- AUD-017: someone who can't save sees the forms read-only
def _writable(page, form_widgets):
    return [w for w in form_widgets if (isinstance(w, QLineEdit) and not w.isReadOnly()) or (isinstance(w, (QComboBox, QCheckBox)) and w.isEnabled())]


def test_viewer_sees_product_and_branch_forms_read_only(app, world):
    v = viewer(world)
    p = ProductsPage(world.conn, v)
    fields = [p.code, p.desc, p.brand, p.size, p.color, p.box, p.supplier_code, p.cost, p.selling, p.dept, p.uom, p.supplier, p.active]
    assert _writable(p, fields) == []
    assert not p.search.isReadOnly()  # searching still works
    b = BranchesPage(world.conn, v)
    assert _writable(b, [b.code, b.name, b.short, b.address, b.kind, b.active]) == []
    b.table.selectRow(0)
    assert b.name.text() and b.name.isReadOnly()


def test_editors_keep_writable_forms(app, world):
    p = ProductsPage(world.conn, world.buyer)
    assert not p.code.isReadOnly() and p.dept.isEnabled() and p.save_btn.isEnabled()
    b = BranchesPage(world.conn, world.admin)
    assert not b.name.isReadOnly() and b.kind.isEnabled()


# ---------- AUD-031: form labels line up the same way on the three pages
def test_form_labels_align_the_same_on_suppliers_products_branches(app, world):
    for cls in (SuppliersPage, ProductsPage, BranchesPage):
        page = cls(world.conn, world.admin)
        forms = page.findChildren(QFormLayout)
        assert forms, cls.__name__
        for form in forms:
            assert form.labelAlignment() & Qt.AlignmentFlag.AlignRight, cls.__name__


# ---------- AUD-006: the purchase-history status chip reads in full
def test_purchase_history_status_column_fits_the_longest_status(app, world):
    s = SuppliersPage(world.conn, world.admin)
    fm = s.history.fontMetrics()
    # The chip is the text plus 14, drawn 4 in from the left and kept 8 inside the cell.
    need = max(fm.horizontalAdvance(t) for t in orders.STATUS_LABELS.values()) + 22
    assert s.history.columnWidth(3) >= need


# ---------- AUD-013: status words in sentence case, as on every other screen
def test_status_words_are_sentence_case(app, world):
    world.po()
    masters.save_supplier(world.conn, world.admin, {"code": "OLD", "name": "Old Trading Post", "status": "INACTIVE"}, confirm_duplicate=True)
    s = SuppliersPage(world.conn, world.admin)
    assert {s.table.item(r, 3).text() for r in range(s.table.rowCount())} == {"Active", "Inactive"}
    s.table.selectRow(next(r for r in range(s.table.rowCount()) if s.table.item(r, 0).text() == "ABC"))
    shown = {s.history.item(r, 3).text() for r in range(s.history.rowCount())}
    assert shown and all(t == t[0] + t[1:].lower() for t in shown), shown
    b = BranchesPage(world.conn, world.admin)
    assert {b.table.item(r, 4).text() for r in range(b.table.rowCount())} <= {"Active", "Inactive"}


# ---------- AUD-085 / AUD-080: the supplier form's number fields
def _new_supplier(s, code="NEW"):
    s.clear_form()
    s.f["code"].setText(code)
    s.f["name"].setText("Wholly Unlike Anything Else Inc")


def test_supplier_term_fields_give_the_plain_message(app, world):
    s = SuppliersPage(world.conn, world.admin)
    _new_supplier(s)
    s.term_days.setText("1.5")
    with pytest.raises(POError, match="Type a whole number of days, like 30."):
        s.save()
    s.term_days.setText("")
    s.term_discount.setText("2.5.1")
    with pytest.raises(POError, match="Type the term discount as a number, like 2%."):
        s.save()
    s.term_discount.setText("")
    s.discount.setText("abc")
    with pytest.raises(POError, match="Type the discount as a number, like 5%."):
        s.save()
    s.discount.setText("")
    s.sticker.setChecked(True)
    s.sticker_rate.setText("1..2")
    with pytest.raises(POError, match="Type the sticker deduction as an amount, like 0.50."):
        s.save()
    assert world.conn.execute("SELECT 1 FROM suppliers WHERE code='NEW'").fetchone() is None
    s.sticker_rate.setText("0.50")
    s.term_discount.setText("2%")
    s.term_days.setText("15")
    s.save()
    row = world.conn.execute("SELECT term_discount_bp, term_discount_days, sticker_rate_c FROM suppliers WHERE code='NEW'").fetchone()
    assert tuple(row) == (200, 15, 50)


def test_supplier_form_only_takes_what_could_be_right(app, world):
    s = SuppliersPage(world.conn, world.admin)
    state = lambda w, text: w.validator().validate(text, len(text))[0]
    ok = (QValidator.State.Acceptable, QValidator.State.Intermediate)
    for w in (s.discount, s.term_discount):
        assert state(w, "2.5%") in ok and state(w, "100") in ok
        assert state(w, "1000") == QValidator.State.Invalid and state(w, "abc") == QValidator.State.Invalid and state(w, "-1") == QValidator.State.Invalid
    assert state(s.term_days, "3650") in ok
    assert state(s.term_days, "100000") == QValidator.State.Invalid and state(s.term_days, "1.5") == QValidator.State.Invalid
    assert state(s.sticker_rate, "1,000.00") in ok and state(s.sticker_rate, "0.50") in ok
    assert state(s.sticker_rate, "10,000.00") == QValidator.State.Invalid and state(s.sticker_rate, "-1") == QValidator.State.Invalid


def test_over_the_limits_from_the_form_gives_the_service_message(app, world):
    s = SuppliersPage(world.conn, world.admin)
    _new_supplier(s, "LIM")
    s.term_discount.setText("150")
    with pytest.raises(POError, match="more than 100%"):
        s.save()
    s.term_discount.setText("")
    s.sticker.setChecked(True)
    s.sticker_rate.setText("1,500.00")
    with pytest.raises(POError, match="sticker deduction can be up to"):
        s.save()


# ---------- AUD-034: the Total row stays last when a report is sorted
def _report_with_totals(rp, conn):
    for i, d in enumerate(reports.REPORTS):
        rep = d.fn(conn)
        if reports.totals_row(rep) is not None and len(rep.rows) >= 2:
            rp.list.setCurrentRow(i)
            return i
    raise AssertionError("no report with totals and two rows")


def test_report_total_row_stays_last_when_sorted(app, world):
    for cost in (100, 900, 300):
        world.po(cost)
    rp = ReportsPage(world.conn, world.admin)
    _report_with_totals(rp, world.conn)
    last = lambda: [rp.table.item(rp.table.rowCount() - 1, c).text() for c in range(rp.table.columnCount())]
    before = last()
    assert before[0] == "Total"
    header = rp.table.horizontalHeader()
    for col in range(rp.table.columnCount()):
        header.sectionClicked.emit(col)
        assert last() == before, col
        header.sectionClicked.emit(col)
        assert last() == before, col
    # Run again while sorted: the Total is still last, and it's the bold one.
    rp.run()
    assert last() == before
    assert rp.table.item(rp.table.rowCount() - 1, 0).font().bold()
    assert not any(rp.table.item(r, 0).font().bold() for r in range(rp.table.rowCount() - 1))


# ---------- AUD-177: an empty report says so, as well as its note
def test_empty_report_shows_its_note_and_says_nothing_matches(app, world):
    rp = ReportsPage(world.conn, world.admin)
    idx = next(i for i, d in enumerate(reports.REPORTS) if d.title == "Cancelled OFs and POs")
    rp.list.setCurrentRow(idx)
    rp.date_from.setDate(QDate(2030, 1, 1))
    rp.date_to.setDate(QDate(2030, 12, 31))
    note = reports.REPORTS[idx].fn(world.conn).note
    assert rp.table.rowCount() == 0
    assert note and note in rp.note.text() and "Nothing matches these dates." in rp.note.text()


# ---------- AUD-178: From after To runs nothing and says why
def test_from_after_to_says_so_and_runs_nothing(app, world, monkeypatch):
    world.po()
    rp = ReportsPage(world.conn, world.admin)
    rp.list.setCurrentRow(0)
    rp.date_to.setDate(QDate(2026, 1, 1))
    ran = []
    real = reports.REPORTS[0]
    counted = dataclasses.replace(real, fn=lambda *a, **k: ran.append(1) or real.fn(*a, **k))
    monkeypatch.setattr(reports, "REPORTS", [counted, *reports.REPORTS[1:]])
    rp.date_from.setDate(QDate(2026, 10, 1))
    assert rp.note.text() == "From is after To."
    assert rp.table.rowCount() == 0 and rp.report is None and rp.title_note.text() == ""
    assert ran == []
    rp.export()  # nothing to export, and no error
    rp.date_from.setDate(QDate(2025, 1, 1))
    assert rp.note.text() != "From is after To." and rp.report is not None
