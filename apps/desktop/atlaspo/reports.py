"""The reports and Excel export. Every report takes the same date filter."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .money import fmt_plain, fmt_qty
from .orders import STATUS_LABELS

MONEY, INT, QTY, TEXT = "money", "int", "qty", "text"
ORDERED = "p.status NOT IN ('DRAFT','PENDING_APPROVAL','CANCELLED')"
# Listed in a register, but not purchases, so not added to its totals.
NOT_BOUGHT = ("DRAFT", "PENDING_APPROVAL", "CANCELLED")
LEFT_OUT = "Cancelled ones are listed but not added to the totals."


@dataclass
class Column:
    title: str
    kind: str = TEXT


@dataclass
class Report:
    title: str
    columns: list[Column]
    rows: list[list]
    totals: bool = True
    note: str = ""
    # Rows (by position) that are listed but left out of the totals row, such as cancelled orders.
    skip_in_totals: set[int] = field(default_factory=set)


@dataclass
class Definition:
    key: str
    title: str
    fn: Callable
    needs: list[str] = field(default_factory=list)


def _dates(column: str, date_from, date_to) -> tuple[str, list]:
    sql, args = "", []
    if date_from:
        sql += f" AND {column} >= ?"
        args.append(date_from)
    if date_to:
        sql += f" AND {column} <= ?"
        args.append(date_to)
    return sql, args


def _status(code: str) -> str:
    return STATUS_LABELS.get(code, code)


def of_register(conn, date_from=None, date_to=None, **_) -> Report:
    from . import order_forms
    rows = order_forms.search(conn, date_from=date_from, date_to=date_to, limit=1000000)
    rows = [r for r in rows if r["of_number"]]
    rows.sort(key=lambda r: (r["of_year"], r["of_seq"]))
    return Report("OF Register", [Column("OF number"), Column("Date"), Column("Supplier"), Column("Products", INT), Column("Pieces", QTY),
                                  Column("Total", MONEY), Column("Branches", INT), Column("Approval"), Column("POs", INT), Column("Delivery")],
                  [[r["of_number"], r["of_date"], r["supplier"], r["product_count"], r["total_pieces_m"], r["grand_total_c"], r["branch_count"],
                    _status(r["status"]), r["po_count"], r["delivery"]] for r in rows],
                  note=LEFT_OUT, skip_in_totals={n for n, r in enumerate(rows) if r["status"] in NOT_BOUGHT})


def po_register(conn, date_from=None, date_to=None, **_) -> Report:
    where, args = _dates("p.po_date", date_from, date_to)
    rows = conn.execute(
        "SELECT p.po_number, f.of_number, p.legacy_po_number, p.po_date, p.branch_name, s.name, p.status, p.total_pieces_m,"
        " p.gross_c, p.item_discount_c + p.overall_discount_c AS disc, p.vat_c, p.grand_total_c, p.issued_at, p.expected_delivery_date"
        " FROM purchase_orders p JOIN suppliers s ON s.id=p.supplier_id LEFT JOIN order_forms f ON f.id=p.of_id"
        f" WHERE p.po_number IS NOT NULL{where} ORDER BY p.po_year, p.branch_code, p.po_seq", args).fetchall()
    return Report("PO Register", [Column("PO number"), Column("Parent OF"), Column("External PO"), Column("Date"), Column("Branch"), Column("Supplier"),
                                  Column("Status"), Column("Pieces", QTY), Column("Gross", MONEY), Column("Discounts", MONEY), Column("VAT", MONEY),
                                  Column("Total", MONEY), Column("Issued"), Column("Expected")],
                  [[r[0], r[1] or "", r[2], r[3], r[4], r[5], _status(r[6]), r[7], r[8], r[9], r[10], r[11], (r[12] or "")[:10], r[13] or ""] for r in rows],
                  note=LEFT_OUT, skip_in_totals={n for n, r in enumerate(rows) if r[6] in NOT_BOUGHT})


def by_supplier(conn, date_from=None, date_to=None, **_) -> Report:
    where, args = _dates("p.po_date", date_from, date_to)
    rows = conn.execute(
        "SELECT s.code, s.name, COUNT(*), SUM(p.total_pieces_m), SUM(p.gross_c), SUM(p.grand_total_c)"
        " FROM purchase_orders p JOIN suppliers s ON s.id=p.supplier_id"
        f" WHERE {ORDERED}{where} GROUP BY s.id ORDER BY SUM(p.grand_total_c) DESC", args).fetchall()
    return Report("Purchases by Supplier", [Column("Code"), Column("Supplier"), Column("POs", INT), Column("Pieces", QTY),
                                            Column("Gross", MONEY), Column("Total", MONEY)],
                  [list(r) for r in rows])


def by_branch(conn, date_from=None, date_to=None, **_) -> Report:
    where, args = _dates("p.po_date", date_from, date_to)
    rows = conn.execute(
        "SELECT b.code, b.name, COUNT(*), SUM(p.total_pieces_m), SUM(p.grand_total_c)"
        " FROM purchase_orders p JOIN branches b ON b.code=p.branch_code"
        f" WHERE {ORDERED}{where} GROUP BY b.code ORDER BY b.code", args).fetchall()
    return Report("Purchases by Branch", [Column("Code", INT), Column("Branch"), Column("POs", INT), Column("Pieces", QTY), Column("Total", MONEY)],
                  [list(r) for r in rows])


def by_department(conn, date_from=None, date_to=None, **_) -> Report:
    where, args = _dates("p.po_date", date_from, date_to)
    rows = conn.execute(
        "SELECT d.id, d.name, COUNT(DISTINCT p.id), SUM(i.qty_m * i.pieces_per_unit), SUM(i.line_gross_c), SUM(i.line_total_c)"
        " FROM purchase_order_items i JOIN purchase_orders p ON p.id=i.po_id JOIN departments d ON d.id=i.department_id"
        f" WHERE {ORDERED}{where} GROUP BY d.id ORDER BY d.id", args).fetchall()
    return Report("Purchases by Department", [Column("Dept", INT), Column("Department"), Column("POs", INT),
                                              Column("Pieces", QTY), Column("Gross", MONEY), Column("Net of item discounts", MONEY)],
                  [list(r) for r in rows], note="Amounts are per line, before overall discount, VAT added on top, and charges.")


def monthly(conn, date_from=None, date_to=None, **_) -> Report:
    where, args = _dates("p.po_date", date_from, date_to)
    rows = conn.execute(
        "SELECT substr(p.po_date,1,7) AS m, COUNT(*), SUM(p.total_pieces_m), SUM(p.gross_c),"
        " SUM(p.item_discount_c + p.overall_discount_c), SUM(p.grand_total_c) FROM purchase_orders p"
        f" WHERE {ORDERED}{where} GROUP BY m ORDER BY m", args).fetchall()
    return Report("Monthly Purchasing", [Column("Month"), Column("POs", INT), Column("Pieces", QTY),
                                         Column("Gross", MONEY), Column("Discounts", MONEY), Column("Total", MONEY)],
                  [list(r) for r in rows])


def annual(conn, date_from=None, date_to=None, **_) -> Report:
    where, args = _dates("p.po_date", date_from, date_to)
    rows = conn.execute(
        "SELECT substr(p.po_date,1,4) AS y, COUNT(*), SUM(p.total_pieces_m), SUM(p.gross_c),"
        " SUM(p.item_discount_c + p.overall_discount_c), SUM(p.grand_total_c) FROM purchase_orders p"
        f" WHERE {ORDERED}{where} GROUP BY y ORDER BY y", args).fetchall()
    return Report("Annual Purchasing", [Column("Year"), Column("POs", INT), Column("Pieces", QTY),
                                        Column("Gross", MONEY), Column("Discounts", MONEY), Column("Total", MONEY)],
                  [list(r) for r in rows])


def pending_approval(conn, date_from=None, date_to=None, **_) -> Report:
    where, args = _dates("f.of_date", date_from, date_to)
    rows = conn.execute(
        "SELECT f.id, f.of_date, s.name, u.full_name, f.total_pieces_m, f.grand_total_c, f.updated_at"
        " FROM order_forms f JOIN suppliers s ON s.id=f.supplier_id JOIN users u ON u.id=f.prepared_by"
        f" WHERE f.status='PENDING_APPROVAL'{where} ORDER BY f.updated_at", args).fetchall()
    return Report("Pending OF Approvals", [Column("Draft OF #", INT), Column("Date"), Column("Supplier"), Column("Prepared by"),
                                           Column("Pieces", QTY), Column("Total", MONEY), Column("Waiting since")],
                  [list(r) for r in rows])


def outstanding(conn, date_from=None, date_to=None, **_) -> Report:
    where, args = _dates("p.po_date", date_from, date_to)
    # Per line, in pieces. Received is what the branch agreed it got. When the Central Warehouse counted more than reached the branch,
    # the supplier delivered those pieces and they were lost on the way, so they aren't still to come from the supplier.
    lines = (
        "SELECT i.po_id, i.qty_m * i.pieces_per_unit AS ordered,"
        " COALESCE(NULLIF((SELECT SUM(r.pieces_m) FROM branch_received r WHERE r.po_item_id = i.id AND EXISTS (SELECT 1 FROM branch_counts b"
        "  WHERE b.po_id = r.po_id AND b.delivery_no = r.delivery_no AND b.attempt_no = r.attempt_no AND b.status = 'APPROVED')), 0), i.received_qty_m * i.pieces_per_unit) AS got,"
        " (SELECT COALESCE(SUM(w.pieces_m), 0) FROM warehouse_delivery_items w JOIN warehouse_deliveries d ON d.id = w.delivery_id"
        "  WHERE d.po_id = i.po_id AND w.po_item_id = i.id) AS central"
        " FROM purchase_order_items i")
    rows = conn.execute(
        "SELECT p.po_number, f.of_number, p.po_date, p.expected_delivery_date, s.name, p.branch_name, p.status, SUM(l.ordered), SUM(l.got),"
        " SUM(MAX(l.ordered - MAX(l.got, l.central), 0)) AS remaining, p.grand_total_c FROM purchase_orders p"
        f" JOIN ({lines}) l ON l.po_id=p.id JOIN suppliers s ON s.id=p.supplier_id LEFT JOIN order_forms f ON f.id=p.of_id"
        f" WHERE p.status IN ('APPROVED','ISSUED','PARTIALLY_DELIVERED'){where}"
        " GROUP BY p.id HAVING remaining > 0 ORDER BY p.expected_delivery_date IS NULL, p.expected_delivery_date",
        args).fetchall()
    return Report("Outstanding POs", [Column("PO number"), Column("Parent OF"), Column("Date"), Column("Expected"), Column("Supplier"),
                                      Column("Branch"), Column("Status"), Column("Pieces ordered", QTY), Column("Pieces received", QTY),
                                      Column("Pieces to come", QTY), Column("PO total", MONEY)],
                  [[r[0], r[1] or "", r[2], r[3], r[4], r[5], _status(r[6]), r[7], r[8], r[9], r[10]] for r in rows],
                  note="Pieces the warehouse sent on that never reached the branch aren't counted as still to come.")


def cancelled(conn, date_from=None, date_to=None, **_) -> Report:
    where_of, args_of = _dates("f.of_date", date_from, date_to)
    where_po, args_po = _dates("p.po_date", date_from, date_to)
    rows = conn.execute(
        "SELECT 'OF' AS kind, COALESCE(f.of_number, 'Draft OF #' || f.id) AS doc, f.of_date AS day, s.name AS sup, f.grand_total_c AS total,"
        " u.full_name AS who, f.cancelled_at AS at, f.cancel_reason AS why"
        " FROM order_forms f JOIN suppliers s ON s.id=f.supplier_id LEFT JOIN users u ON u.id=f.cancelled_by"
        f" WHERE f.status='CANCELLED'{where_of}"
        " UNION ALL "
        "SELECT 'PO', COALESCE(p.po_number, 'Draft #' || p.id), p.po_date, s.name, p.grand_total_c, u.full_name, p.cancelled_at, p.cancel_reason"
        " FROM purchase_orders p JOIN suppliers s ON s.id=p.supplier_id LEFT JOIN users u ON u.id=p.cancelled_by"
        f" WHERE p.status='CANCELLED'{where_po} ORDER BY at DESC", args_of + args_po).fetchall()
    return Report("Cancelled OFs and POs", [Column("Type"), Column("Document"), Column("Date"), Column("Supplier"), Column("Total", MONEY),
                                            Column("Cancelled by"), Column("Cancelled on"), Column("Reason")], [list(r) for r in rows], totals=False,
                  note="An OF and the POs under it are listed separately, so don't add them together.")


def supplier_history(conn, date_from=None, date_to=None, supplier_id=None, **_) -> Report:
    where, args = _dates("p.po_date", date_from, date_to)
    if supplier_id:
        where += " AND p.supplier_id = ?"
        args.append(supplier_id)
    rows = conn.execute(
        "SELECT s.code, s.name, COALESCE(p.po_number,'Draft #'||p.id), f.of_number, p.po_date, p.branch_name, p.status, p.total_pieces_m, p.grand_total_c"
        " FROM purchase_orders p JOIN suppliers s ON s.id=p.supplier_id LEFT JOIN order_forms f ON f.id=p.of_id"
        f" WHERE 1=1{where} ORDER BY s.name, p.po_date DESC", args).fetchall()
    return Report("Supplier Purchase History", [Column("Code"), Column("Supplier"), Column("PO"), Column("Parent OF"), Column("Date"), Column("Branch"),
                                                Column("Status"), Column("Pieces", QTY), Column("Total", MONEY)],
                  [[r[0], r[1], r[2], r[3] or "", r[4], r[5], _status(r[6]), r[7], r[8]] for r in rows],
                  note="Cancelled POs, and any not yet approved, are listed but not added to the totals.",
                  skip_in_totals={n for n, r in enumerate(rows) if r[6] in NOT_BOUGHT})


def item_history(conn, date_from=None, date_to=None, item_text=None, **_) -> Report:
    where, args = _dates("p.po_date", date_from, date_to)
    if item_text:
        where += " AND (i.item_code LIKE ? OR i.description LIKE ?)"
        args += [f"%{item_text}%"] * 2
    rows = conn.execute(
        "SELECT i.item_code, i.description, p.po_number, p.po_date, s.name, p.branch_name, i.qty_m, i.uom, i.unit_cost_c, i.net_unit_c, i.line_total_c"
        " FROM purchase_order_items i JOIN purchase_orders p ON p.id=i.po_id JOIN suppliers s ON s.id=p.supplier_id"
        f" WHERE {ORDERED}{where} ORDER BY i.item_code, p.po_date DESC LIMIT 20000", args).fetchall()
    return Report("Product Purchase History", [Column("SKU"), Column("Description"), Column("PO"), Column("Date"), Column("Supplier"),
                                               Column("Branch"), Column("Qty", QTY), Column("Unit"), Column("Unit cost", MONEY),
                                               Column("Net unit", MONEY), Column("Amount", MONEY)], [list(r) for r in rows], totals=False)


def cost_comparison(conn, date_from=None, date_to=None, item_text=None, **_) -> Report:
    """For each item bought more than once, or from more than one supplier: lowest, highest and latest cost."""
    where, args = _dates("p.po_date", date_from, date_to)
    if item_text:
        where += " AND (i.item_code LIKE ? OR i.description LIKE ?)"
        args += [f"%{item_text}%"] * 2
    # Latest within the same dates, so it falls between the lowest and highest shown.
    latest_where, latest_args = _dates("y.po_date", date_from, date_to)
    # One order split over several branches is one purchase: counted by OF, or by PO when there's no OF.
    rows = conn.execute(
        "SELECT i.item_code, MAX(i.description), COUNT(DISTINCT COALESCE('OF' || p.of_id, 'PO' || p.id)) AS times, COUNT(DISTINCT p.supplier_id), MIN(i.net_unit_c), MAX(i.net_unit_c),"
        " (SELECT x.net_unit_c FROM purchase_order_items x JOIN purchase_orders y ON y.id=x.po_id"
        f"  WHERE x.item_code=i.item_code AND y.status NOT IN ('DRAFT','PENDING_APPROVAL','CANCELLED'){latest_where} ORDER BY y.po_date DESC, y.id DESC LIMIT 1)"
        " FROM purchase_order_items i JOIN purchase_orders p ON p.id=i.po_id"
        f" WHERE {ORDERED} AND i.item_code <> ''{where} GROUP BY i.item_code HAVING times > 1"
        " ORDER BY MAX(i.net_unit_c) - MIN(i.net_unit_c) DESC LIMIT 20000", latest_args + args).fetchall()
    return Report("Purchase Cost Comparison", [Column("SKU"), Column("Description"), Column("Times bought", INT), Column("Suppliers", INT),
                                               Column("Lowest net", MONEY), Column("Highest net", MONEY), Column("Latest net", MONEY)],
                  [list(r) for r in rows], totals=False)


REPORTS = [
    Definition("of_register", "OF Register", of_register),
    Definition("register", "PO Register", po_register),
    Definition("supplier", "Purchases by Supplier", by_supplier),
    Definition("branch", "Purchases by Branch", by_branch),
    Definition("department", "Purchases by Department", by_department),
    Definition("monthly", "Monthly Purchasing", monthly),
    Definition("annual", "Annual Purchasing", annual),
    Definition("pending", "Pending OF Approvals", pending_approval),
    Definition("outstanding", "Outstanding POs", outstanding),
    Definition("cancelled", "Cancelled OFs and POs", cancelled),
    Definition("supplier_history", "Supplier Purchase History", supplier_history, ["supplier"]),
    Definition("item_history", "Product Purchase History", item_history, ["item"]),
    Definition("cost", "Purchase Cost Comparison", cost_comparison, ["item"]),
]


def totals_row(report: Report) -> list | None:
    if not report.totals or not report.rows:
        return None
    out = ["Total"] + [""] * (len(report.columns) - 1)
    for i, col in enumerate(report.columns):
        if col.kind in (MONEY, INT, QTY) and col.title not in ("Dept", "Draft OF #", "Code"):
            out[i] = sum(r[i] or 0 for n, r in enumerate(report.rows) if n not in report.skip_in_totals)
    return out


def display(value, kind: str) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):  # the "Total" label can land in a numeric first column
        return value
    if kind == MONEY:
        return fmt_plain(value)
    if kind == QTY:
        return fmt_qty(value)
    if kind == INT:
        return f"{value:,}"
    return str(value)


def to_excel(report: Report, path: str | Path, filters: str = "") -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = report.title[:31]
    ws["A1"] = report.title
    ws["A1"].font = Font(bold=True, size=13)
    ws["A2"] = f"Atlas PO ERP. Generated {datetime.now():%B %d, %Y %I:%M %p}" + (f". {filters}" if filters else "")
    ws["A2"].font = Font(color="5B6470", size=9)
    start = 4
    head = PatternFill("solid", fgColor="F3F4F6")
    for c, col in enumerate(report.columns, 1):
        cell = ws.cell(start, c, col.title)
        cell.font, cell.fill = Font(bold=True), head
        cell.alignment = Alignment(horizontal="right" if col.kind != TEXT else "left")

    def write(row, at, bold=False):
        for c, (value, col) in enumerate(zip(row, report.columns), 1):
            if col.kind == MONEY and value not in (None, "") and not isinstance(value, str):
                cell = ws.cell(at, c, value / 100)
                cell.number_format = '"₱"#,##0.00'
            elif col.kind == QTY and value not in (None, "") and not isinstance(value, str):
                cell = ws.cell(at, c, value / 1000)
                cell.number_format = "#,##0.###"
            elif col.kind == INT and value not in (None, "") and not isinstance(value, str):
                cell = ws.cell(at, c, value)
                cell.number_format = "#,##0"
            else:
                cell = ws.cell(at, c, value)
                if isinstance(value, str):
                    cell.data_type = "s"  # text starting with "=" stays text, never a live formula
            cell.font = Font(bold=bold)

    for r, row in enumerate(report.rows, start + 1):
        write(row, r)
    total = totals_row(report)
    if total:
        write(total, start + 1 + len(report.rows), bold=True)
    if report.note:
        ws.cell(start + 3 + len(report.rows), 1, report.note).font = Font(color="5B6470", size=9)
    for c, col in enumerate(report.columns, 1):
        width = max([len(col.title)] + [len(display(r[c - 1], col.kind)) for r in report.rows[:2000]])
        ws.column_dimensions[get_column_letter(c)].width = min(width + 3, 48)
    ws.freeze_panes = ws.cell(start + 1, 1)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def po_register_excel(rows: list, path: str | Path, filters: str = "") -> Path:
    report = Report("Purchase Orders", [Column("PO number"), Column("Parent OF"), Column("Branch"), Column("Supplier"), Column("PO date"),
                                        Column("Pieces", QTY), Column("Total", MONEY), Column("Status"), Column("Issued"), Column("Expected"),
                                        Column("External PO"), Column("Departments")],
                    [[r["po_number"] or f"Draft #{r['id']}", r["of_number"] or "", r["branch"], r["supplier"], r["po_date"], r["total_pieces_m"], r["grand_total_c"],
                      _status(r["status"]), (r["issued_at"] or "")[:10], r["expected_delivery_date"] or "", r["legacy_po_number"], r["depts"] or ""] for r in rows])
    return to_excel(report, path, filters)


def of_register_excel(rows: list, path: str | Path, filters: str = "") -> Path:
    report = Report("Order Forms", [Column("OF number"), Column("Date"), Column("Supplier"), Column("Products", INT), Column("Pieces", QTY), Column("Total", MONEY),
                                    Column("Branches", INT), Column("Approval"), Column("POs", INT), Column("Printed"), Column("Delivery")],
                    [[r["of_number"] or f"Draft OF #{r['id']}", r["of_date"], r["supplier"], r["product_count"], r["total_pieces_m"], r["grand_total_c"], r["branch_count"],
                      _status(r["status"]), r["po_count"], (r["printed_at"] or "")[:10] or r["printed_part"], r["delivery"]] for r in rows])
    return to_excel(report, path, filters)
