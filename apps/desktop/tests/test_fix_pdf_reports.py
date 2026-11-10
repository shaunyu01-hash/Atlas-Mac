"""Audit fixes for the papers, their print gates and the reports (AUD-059, 101, 180, 115, 140, 096, 118, 073, 095, 092, 108, 174, 175)."""
from __future__ import annotations

import os
from io import BytesIO

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from openpyxl import load_workbook
from pypdf import PdfReader

from atlaspo import auth, debit_memos as dm, delivery_receipts as dr, masters, order_forms, orders, pdf, receiving, reports, warehouse
from atlaspo.db import PermissionDenied
from atlaspo.debit_memos import SHORT

from conftest import NORTH, PASSWORD, SOUTH


def _text(data: bytes) -> str:
    return " ".join(" ".join((p.extract_text() or "") for p in PdfReader(BytesIO(data)).pages).split())


def _user(world, name, role, branch=None):
    return auth.create_user(world.conn, world.admin, name, name.title(), PASSWORD, [role], must_change=False, branch_code=branch)


def _actions(world, action, of_id=None):
    sql, args = "SELECT COUNT(*) FROM audit_logs WHERE action = ?", [action]
    if of_id is not None:
        sql += " AND of_id = ?"
        args.append(of_id)
    return world.conn.execute(sql, args).fetchone()[0]


def _issued_of(world, **item):
    of_id = world.approved(items=[world.item(cost=10000, alloc={NORTH: 10}, description="Socks", item_code="SOCK-1", **item)])
    po = order_forms.generate_pos(world.conn, world.buyer, of_id)[0]
    orders.issue(world.conn, world.buyer, po["id"])
    return of_id, po["id"]


def _count(world, po_id, pieces, boxes=None):
    for n in (1, 2):
        who = _user(world, f"r{po_id}x{n}{pieces}", "RECEIVER", NORTH)
        sheet = receiving.sheet(world.conn, who, po_id)
        receiving.save_count(world.conn, who, po_id, {line["id"]: str(pieces) for line in sheet}, boxes=boxes)


# ------------------------------------------------------------------ AUD-059: a paper that can't be made issues nothing

def test_a_print_set_that_fails_to_render_issues_nothing_and_can_print_again(world, monkeypatch):
    of_id = world.approved(items=[world.item(alloc={NORTH: 3, SOUTH: 2})])

    def boom(*a, **k):
        raise RuntimeError("printer paper jam")

    real = pdf.render_cut_slips
    monkeypatch.setattr(pdf, "render_cut_slips", boom)
    with pytest.raises(RuntimeError):
        pdf.print_order_form(world.conn, world.buyer, of_id)
    statuses = {r[0] for r in world.conn.execute("SELECT status FROM purchase_orders WHERE of_id = ?", (of_id,))}
    assert statuses == {orders.APPROVED}
    assert _actions(world, "OF_PRINT_WAREHOUSE", of_id) == 0 and not pdf.warehouse_printed(world.conn, of_id)
    assert world.conn.execute("SELECT COUNT(*) FROM po_issued_versions v JOIN purchase_orders p ON p.id = v.po_id WHERE p.of_id = ?", (of_id,)).fetchone()[0] == 0
    assert not world.conn.in_transaction
    monkeypatch.setattr(pdf, "render_cut_slips", real)
    document, problems = pdf.print_order_form(world.conn, world.buyer, of_id)
    assert document and problems == []
    assert "PRINTED AGAIN" not in _text(document)  # the warehouse original is still the original
    assert {r[0] for r in world.conn.execute("SELECT status FROM purchase_orders WHERE of_id = ?", (of_id,))} == {orders.ISSUED}
    assert _actions(world, "OF_PRINT_WAREHOUSE", of_id) == 1


# ------------------------------------------------------------------ AUD-101 / 180 / 115 / 118: who may print what

def test_receivers_get_no_priced_papers_through_the_backend(world, env):
    of_id, po_id = _issued_of(world)
    rec = _user(world, "nor1", "RECEIVER", NORTH)
    before = world.conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
    for call in (lambda: pdf.warehouse_of(world.conn, rec, of_id, env / "w.pdf"),
                 lambda: pdf.reprint_order_form(world.conn, rec, of_id),
                 lambda: pdf.blind_copy(world.conn, rec, po_id),
                 lambda: pdf.office_copy(world.conn, rec, of_id),
                 lambda: pdf.office_receipts(world.conn, rec, [po_id])):
        with pytest.raises(PermissionDenied):
            call()
    assert world.conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0] == before
    # The home office still prints them.
    accounting = _user(world, "acc", "ACCOUNTING")
    assert pdf.warehouse_of(world.conn, world.buyer, of_id, env / "w.pdf").exists()
    assert pdf.warehouse_of(world.conn, accounting, of_id, env / "w2.pdf").exists()
    assert pdf.reprint_order_form(world.conn, world.buyer, of_id)
    assert pdf.blind_copy(world.conn, accounting, po_id)
    assert isinstance(pdf.office_receipts(world.conn, world.buyer, [po_id]), list)


def test_only_purchasing_prints_a_dr_or_saves_them_for_email(world):
    _, po_id = _issued_of(world)
    viewer = _user(world, "viewer", "VIEWER")
    receipt = dr.get(world.conn, dr.ensure_first(world.conn, world.buyer, po_id))
    with pytest.raises(PermissionDenied):
        pdf.delivery_receipt(world.conn, viewer, receipt["id"])
    assert dr.get(world.conn, receipt["id"])["prints"] == 0
    group = {"reference": receipt["po_number"], "supplier_code": "ABC", "receipts": [receipt]}
    with pytest.raises(PermissionDenied):
        pdf.receipts_for_email(world.conn, viewer, group)
    assert _actions(world, "DR_SAVED_FOR_EMAIL") == 0
    # Purchasing's first print is the original.
    assert "PRINTED AGAIN" not in _text(pdf.delivery_receipt(world.conn, world.buyer, receipt["id"]))
    assert pdf.receipts_for_email(world.conn, world.buyer, group).exists()


def test_making_a_dr_needs_issue_or_payment_rights(world):
    of_id = world.approved(items=[world.item(alloc={NORTH: 1})])
    po_id = order_forms.generate_pos(world.conn, world.buyer, of_id)[0]["id"]
    for role in ("VIEWER", "RECEIVER", "WAREHOUSE", "APPROVER"):
        who = _user(world, f"x{role.lower()}", role, NORTH if role == "RECEIVER" else None)
        with pytest.raises(PermissionDenied):
            dr.ensure_first(world.conn, who, po_id)
    assert world.conn.execute("SELECT COUNT(*) FROM delivery_receipts WHERE po_id = ?", (po_id,)).fetchone()[0] == 0
    accounting = _user(world, "acc", "ACCOUNTING")
    first = dr.ensure_first(world.conn, accounting, po_id)
    assert dr.ensure_first(world.conn, world.buyer, po_id) == first


def test_a_view_only_reprint_still_works_as_before(world):
    """Whether a view-only reprint may make a DR is still to be decided, so it's unchanged."""
    of_id, _ = _issued_of(world)
    viewer = _user(world, "viewer", "VIEWER")
    assert "PRINTED AGAIN" in _text(pdf.reprint_order_form(world.conn, viewer, of_id))


# ------------------------------------------------------------------ AUD-140: "printed already?" read where the print is recorded

def test_printed_already_is_read_inside_the_recording_transaction(world, env, monkeypatch):
    of_id, _ = _issued_of(world)
    seen = []
    real = pdf.warehouse_printed

    def spy(conn, of):
        seen.append(conn.in_transaction)
        return real(conn, of)

    monkeypatch.setattr(pdf, "warehouse_printed", spy)
    pdf.warehouse_of(world.conn, world.buyer, of_id, env / "a.pdf")
    pdf.warehouse_of(world.conn, world.buyer, of_id, env / "b.pdf")
    assert seen == [True, True]
    rows = [r[0] for r in world.conn.execute("SELECT new_value FROM audit_logs WHERE of_id = ? AND action = 'OF_PRINT_WAREHOUSE' ORDER BY id", (of_id,))]
    assert ['"copy": false' in r for r in rows] == [True, False]


def test_a_reprint_that_fails_to_render_records_nothing(world, monkeypatch):
    of_id, _ = _issued_of(world)

    def boom(*a, **k):
        raise RuntimeError("printer paper jam")

    real = pdf.render_cut_slips
    monkeypatch.setattr(pdf, "render_cut_slips", boom)
    with pytest.raises(RuntimeError):
        pdf.reprint_order_form(world.conn, world.buyer, of_id)
    assert _actions(world, "OF_REPRINT", of_id) == 0 and not world.conn.in_transaction
    monkeypatch.setattr(pdf, "render_cut_slips", real)
    assert "COPY 2. PRINTED AGAIN" in _text(pdf.reprint_order_form(world.conn, world.buyer, of_id))
    assert "COPY 3. PRINTED AGAIN" in _text(pdf.reprint_order_form(world.conn, world.buyer, of_id))


# ------------------------------------------------------------------ AUD-096: the Final OF's last line says what it takes off

def test_final_of_last_line_names_the_term_discount(world):
    masters.save_supplier(world.conn, world.admin, {"code": "ABC", "name": "ABC Trading", "payment_terms": "60D", "term_discount_bp": 300, "term_discount_days": 14},
                          world.supplier, confirm_duplicate=True)
    of_id, po_id = _issued_of(world)
    _count(world, po_id, 9)
    item = orders.get_po(world.conn, po_id)["items"][0]["id"]
    dm.issue(world.conn, world.buyer, po_id, {(item, SHORT): "1"})
    text = _text(pdf.render_office_of(world.conn, of_id))
    assert "To pay after term discount, debit memos and claim stubs" in text
    assert "OF total less debit memos" not in text


# ------------------------------------------------------------------ AUD-073 / 095: pieces, and cancelled orders listed but not totalled

def test_registers_report_pieces_and_leave_cancelled_out_of_the_totals(world):
    _issued_of(world, pieces_per_unit=12)  # 10 dozen = 120 pieces
    gone = world.po(50)
    orders.cancel(world.conn, world.buyer, gone, "test")
    reg = reports.po_register(world.conn)
    titles = [c.title for c in reg.columns]
    assert "Qty" not in titles and "Pieces" in titles
    pieces = titles.index("Pieces")
    total = titles.index("Total")
    assert len(reg.rows) == 2  # the cancelled one is still listed
    totals = reports.totals_row(reg)
    supplier = reports.by_supplier(world.conn)
    assert totals[pieces] == 120000 == supplier.rows[0][3]
    assert totals[total] == supplier.rows[0][5] == 100000  # 10 dozen at 100.00 a dozen
    assert "Pieces" in [c.title for c in supplier.columns]
    ofs = reports.of_register(world.conn)
    assert reports.totals_row(ofs)[[c.title for c in ofs.columns].index("Total")] == supplier.rows[0][5]
    history = reports.supplier_history(world.conn)
    assert len(history.rows) == 2 and reports.totals_row(history)[[c.title for c in history.columns].index("Total")] == supplier.rows[0][5]
    assert "Received stays 0" not in reports.outstanding(world.conn).note


def test_cancelled_rows_stay_out_of_the_excel_totals(world, env):
    world.po(20)
    gone = world.po(50)
    orders.cancel(world.conn, world.buyer, gone, "test")
    rep = reports.po_register(world.conn)
    ws = load_workbook(reports.to_excel(rep, env / "r.xlsx")).active
    total_col = [c.title for c in rep.columns].index("Total") + 1
    assert ws.cell(5 + len(rep.rows), 1).value == "Total" and ws.cell(5 + len(rep.rows), total_col).value == 20


# ------------------------------------------------------------------ AUD-092: pieces lost on the way aren't still to come

def test_pieces_lost_on_the_way_are_not_outstanding_from_the_supplier(world):
    masters.save_supplier(world.conn, world.admin, {"code": "ABC", "name": "ABC Trading", "payment_terms": "30 D", "sticker_rate_c": 50}, world.supplier, confirm_duplicate=True)
    of_id = world.approved(items=[world.item(cost=10000, alloc={NORTH: 10}, description="Socks", item_code="SOCK-1")])
    po = order_forms.generate_pos(world.conn, world.buyer, of_id)[0]
    orders.issue(world.conn, world.buyer, po["id"])
    lines = warehouse.lines(world.conn, world.warehouse, of_id)
    warehouse.receive(world.conn, world.warehouse, of_id, {po["id"]: "1"}, {lines[0]["po_item_id"]: "10"})
    warehouse.ship(world.conn, world.warehouse, of_id)
    _count(world, po["id"], 8, boxes="1")
    assert orders.get_po(world.conn, po["id"])["status"] == orders.PARTIAL  # the PO itself is unchanged
    assert [r for r in reports.outstanding(world.conn).rows if r[0] == po["po_number"]] == []
    # A PO the supplier really still owes stays on, in pieces.
    _, other = _issued_of(world, pieces_per_unit=2)
    row = next(r for r in reports.outstanding(world.conn).rows if r[0] == orders.get_po(world.conn, other)["po_number"])
    assert row[7:10] == [20000, 0, 20000]


# ------------------------------------------------------------------ AUD-108: text in a workbook stays text

def test_text_starting_with_equals_stays_text_in_excel(world, env):
    masters.save_supplier(world.conn, world.admin, {"code": "ABC", "name": "=1+1", "payment_terms": "30 D"}, world.supplier, confirm_duplicate=True)
    world.po(10)
    rep = reports.po_register(world.conn)
    ws = load_workbook(reports.to_excel(rep, env / "f.xlsx")).active
    col = [c.title for c in rep.columns].index("Supplier") + 1
    cell = ws.cell(5, col)
    assert cell.value == "=1+1" and cell.data_type == "s"


# ------------------------------------------------------------------ AUD-174 / 175: cost comparison

def _bought(world, clock, day, cost, alloc):
    clock(*map(int, day.split("-")), 9)
    of_id = world.approved(items=[world.item(cost=cost, alloc=alloc, description="Shirt", item_code="SH-1")], of_date=day)
    order_forms.generate_pos(world.conn, world.buyer, of_id)


def test_cost_comparison_counts_orders_and_keeps_latest_in_the_window(world, clock):
    _bought(world, clock, "2026-10-01", 10000, {NORTH: 1, SOUTH: 1, 6: 1, 8: 1})
    # One OF over four branches is bought once, so it's not listed.
    assert [r for r in reports.cost_comparison(world.conn).rows if r[0] == "SH-1"] == []
    _bought(world, clock, "2026-10-05", 12000, {NORTH: 1})
    _bought(world, clock, "2026-10-20", 15000, {NORTH: 1})
    row = next(r for r in reports.cost_comparison(world.conn).rows if r[0] == "SH-1")
    assert row[2] == 3 and row[6] == 15000
    # Within early October, the latest is from early October, between the lowest and highest shown.
    row = next(r for r in reports.cost_comparison(world.conn, date_from="2026-10-01", date_to="2026-10-10").rows if r[0] == "SH-1")
    assert row[2] == 2 and (row[4], row[5], row[6]) == (10000, 12000, 12000)
