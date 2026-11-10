"""The printed documents, kept apart because they do different jobs.

Supplier PO: one branch only. Never prints other branches' quantities, internal or warehouse notes, approval remarks, selling price or FX rate.
Warehouse OF: the whole order, every branch, with costs and any sticker deduction for the supplier to sign, which the final payment is based on, and spaces to write what was counted.
Office OF: the same order for accounting, with costs, totals, and any sticker deduction, debit memos and claim stubs. It only prints once the delivery is complete.
Delivery Receipt: one branch, no prices. The supplier writes in the pieces delivered, their unit price and total.
"""
from __future__ import annotations

import json
from datetime import date
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import delivery_receipts, payments, receiving
from .auth import require
from .db import POError, PermissionDenied, audit, data_dir, get_setting, today, tx
from .money import VAT_EXCLUSIVE, VAT_INCLUSIVE, fmt_peso, fmt_percent, fmt_plain, fmt_qty, per_piece
from .order_forms import generate_pos, get_of
from .orders import APPROVED, ARRANGEMENTS, CANCELLED, CLOSED, FULL, ISSUED, PARTIAL, STATUS_LABELS, get_po, issue, issued_pdf, to_issue

PRINTABLE = (APPROVED, ISSUED, PARTIAL, FULL, CLOSED, CANCELLED)
INK = colors.HexColor("#1D2329")
MUTED = colors.HexColor("#5B6470")
RULE = colors.HexColor("#C9CDD3")
HEAD = colors.HexColor("#F3F4F6")
ACCENT = colors.HexColor("#0F6E6E")
ACCENT_PALE = colors.HexColor("#E3F1F0")
RED = colors.HexColor("#B42318")


def stamp_copy(data: bytes, copy: int, who: str) -> bytes:
    """Marks every page of a document printed again, so it can't be taken for a second document. The kept file never changes."""
    st = Styles()
    text = f"COPY {copy}. PRINTED AGAIN {_long_date(today().isoformat()).upper()} BY {who.upper()}"
    writer = PdfWriter()
    for page in PdfReader(BytesIO(data)).pages:
        width, height = float(page.mediabox.width), float(page.mediabox.height)
        buffer = BytesIO()
        mark = canvas.Canvas(buffer, pagesize=(width, height))
        mark.setFillColor(colors.HexColor("#B42318"))
        mark.setFont(st.bold, 9)
        mark.drawCentredString(width / 2, height - 6.5 * mm, text)
        mark.save()
        page.merge_page(PdfReader(BytesIO(buffer.getvalue())).pages[0])
        writer.add_page(page)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def reprint_counts(conn) -> dict[tuple[str, int], int]:
    """How many extra times each OF and PO has printed, for the lists to flag."""
    counts = {("OF", r[0]): r[1] for r in conn.execute("SELECT of_id, COUNT(*) FROM audit_logs WHERE action = 'OF_REPRINT' GROUP BY of_id")}
    counts.update({("PO", r[0]): r[1] for r in conn.execute("SELECT po_id, COUNT(*) FROM audit_logs WHERE action = 'PO_REPRINT' GROUP BY po_id")})
    return counts


def combine(documents: list[bytes]) -> bytes:
    """Several documents as one file, to print in one go. Each keeps its exact pages."""
    writer = PdfWriter()
    for document in documents:
        writer.append(BytesIO(document))
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def _by_department(items: list) -> list[tuple[int, str, list]]:
    """(department id, name, its rows), in department order, each keeping its rows' order."""
    groups: dict[int, list] = {}
    for item in items:
        groups.setdefault(item["department_id"], []).append(item)
    return [(dept, group[0]["department"], group) for dept, group in sorted(groups.items())]


def _fonts() -> tuple[str, str]:
    """A font with the peso sign, embedded so it prints the same on any PC."""
    folder = Path(r"C:\Windows\Fonts")
    for regular, bold in (("segoeui.ttf", "segoeuib.ttf"), ("arial.ttf", "arialbd.ttf")):
        if (folder / regular).exists() and (folder / bold).exists():
            pdfmetrics.registerFont(TTFont("POBody", str(folder / regular)))
            pdfmetrics.registerFont(TTFont("POBold", str(folder / bold)))
            pdfmetrics.registerFontFamily("POBody", normal="POBody", bold="POBold", italic="POBody", boldItalic="POBold")
            return "POBody", "POBold"
    return "Helvetica", "Helvetica-Bold"


def _long_date(iso: str | None) -> str:
    if not iso:
        return ""
    d = date.fromisoformat(iso[:10])
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def esc(v) -> str:
    return str(v if v is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Styles:
    def __init__(self):
        self.body, self.bold = _fonts()
        body, bold = self.body, self.bold
        self.s = ParagraphStyle("s", fontName=body, fontSize=8.5, leading=11, textColor=INK, alignment=TA_LEFT)
        self.strong = ParagraphStyle("strong", parent=self.s, fontName=bold)
        self.small = ParagraphStyle("small", parent=self.s, fontSize=7.2, leading=9)
        self.right = ParagraphStyle("right", parent=self.small, alignment=TA_RIGHT)
        self.center = ParagraphStyle("center", parent=self.small, alignment=TA_CENTER)
        self.label = ParagraphStyle("label", parent=self.s, fontSize=7.2, leading=9.5, textColor=MUTED)
        self.th = ParagraphStyle("th", parent=self.small, fontName=bold)
        self.th_r = ParagraphStyle("thr", parent=self.th, alignment=TA_RIGHT)
        self.th_c = ParagraphStyle("thc", parent=self.th, alignment=TA_CENTER)
        self.company = ParagraphStyle("co", parent=self.s, fontName=bold, fontSize=15, leading=18, textColor=ACCENT)
        self.title = ParagraphStyle("t", parent=self.s, fontName=bold, fontSize=10, leading=13, alignment=TA_RIGHT, textColor=MUTED)
        self.number = ParagraphStyle("n", parent=self.s, fontName=bold, fontSize=17, leading=21, alignment=TA_RIGHT)
        self.h2 = ParagraphStyle("h2", parent=self.s, fontName=bold, fontSize=9, leading=12, spaceBefore=2, spaceAfter=3)
        self.peso = fmt_peso if body != "Helvetica" else (lambda c: "PHP " + fmt_plain(c))


def _logo(conn):
    name = get_setting(conn, "company.logo_file")
    path = data_dir() / name if name else None
    if not path or not path.is_file():
        return None
    try:
        logo = Image(str(path))
    except OSError:
        return None
    height = 15 * mm
    logo.drawWidth, logo.drawHeight = height * logo.imageWidth / logo.imageHeight, height
    logo.hAlign = "LEFT"
    return logo


def _company_block(conn, st: Styles) -> list:
    block = []
    logo = _logo(conn)
    if logo:
        block.append(logo)
    block.append(Paragraph(esc(get_setting(conn, "company.name") or "ATLAS PO ERP"), st.company))
    contact = " | ".join(x for x in (f"Tel: {get_setting(conn, 'company.telephone')}" if get_setting(conn, "company.telephone") else "",
                                     get_setting(conn, "company.email")) if x)
    tin = get_setting(conn, "company.tin")
    for text in (get_setting(conn, "company.address"), contact, f"TIN: {tin}" if tin else ""):
        if text:
            block.append(Paragraph(esc(text), st.small))
    return block


def _supplier_block(sup: dict, st: Styles) -> list:
    block = [Paragraph("Supplier", st.label), Paragraph(esc(sup.get("registered_name") or sup["name"]), st.strong)]
    if sup.get("registered_name") and sup["registered_name"] != sup["name"]:
        block.append(Paragraph(esc(sup["name"]), st.small))
    block.append(Paragraph(f"Code: {esc(sup['code'])}", st.small))
    for text in (sup.get("address"), f"Attn: {sup['contact_person']}" if sup.get("contact_person") else "",
                 f"Tel: {sup['telephone']}" if sup.get("telephone") else "", f"Email: {sup['email']}" if sup.get("email") else "",
                 f"TIN: {sup['tin']}" if sup.get("tin") else ""):
        if text:
            block.append(Paragraph(esc(text), st.small))
    return block


def _meta_table(rows: list[tuple[str, str]], st: Styles, width: float) -> Table:
    t = Table([[Paragraph(a, st.label), Paragraph(esc(b), st.s)] for a, b in rows], colWidths=[26 * mm, width - 26 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 0),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 2), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    return t


def _sign_row(st: Styles, width: float, signs: list[tuple[str, str]]) -> Table:
    def sign(title: str, name: str):
        return [Spacer(1, 20), Paragraph(esc(name) or "&nbsp;", ParagraphStyle("sn", parent=st.s, alignment=TA_CENTER)),
                Table([[""]], colWidths=[width / len(signs) - 10 * mm], rowHeights=[2], style=[("LINEABOVE", (0, 0), (-1, 0), 0.6, INK)]),
                Paragraph(title, ParagraphStyle("st", parent=st.label, alignment=TA_CENTER))]
    table = Table([[sign(*x) for x in signs]], colWidths=[width / len(signs)] * len(signs))
    table.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "BOTTOM")]))
    return table


def _footer(st: Styles, page, left: str, initials: bool = False):
    """Marks each page once the total is known: Page 2 of 3, and on later pages the document's number at the top, so a loose page still says what it belongs to.
    With initials, every page before the signed one has a place for the supplier to initial, so a page can't be swapped."""
    class Paged(canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.finished_pages = []

        def showPage(self):
            self.finished_pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            pages = self.finished_pages
            for state in pages:
                self.__dict__.update(state)
                self.mark(len(pages))
                super().showPage()
            super().save()

        def mark(self, total: int):
            number = self.getPageNumber()
            self.setFillColor(MUTED)
            self.setFont(st.body, 7)
            self.drawString(12 * mm, 7 * mm, left)
            self.drawRightString(page[0] - 12 * mm, 7 * mm, f"Page {number} of {total}")
            if initials and number < total:
                self.setFillColor(INK)
                self.setFont(st.body, 8)
                self.drawRightString(page[0] - 32 * mm, 7 * mm, "Supplier initials: ____________")
            if number > 1:
                self.setFillColor(INK)
                self.setFont(st.bold, 8)
                self.drawString(12 * mm, page[1] - 10 * mm, left)
    return Paged


def render_supplier_po(conn, po_id: int, preview: bool | None = None) -> bytes:
    """The supplier's PO as A4 PDF bytes. preview marks a copy that hasn't been issued yet."""
    po = get_po(conn, po_id)
    if po["status"] not in PRINTABLE:
        raise POError("Only an approved PO can be printed for a supplier.")
    if preview is None:
        # Not yet issued, or amended since: what the supplier holds is an earlier revision.
        preview = po["status"] == APPROVED or (po["issued_revision"] is not None and po["revision"] != po["issued_revision"])
    st = Styles()
    page = A4
    width = page[0] - 24 * mm

    rev = f"  Rev. {po['revision']}" if po["revision"] else ""
    head_right = [Paragraph("PURCHASE ORDER", st.title), Paragraph(esc(po["po_number"]) + esc(rev), st.number)]
    if po["of_number"]:
        head_right.append(Paragraph(f"Order Form reference: {esc(po['of_number'])}", ParagraphStyle("of", parent=st.small, alignment=TA_RIGHT, textColor=MUTED)))
    if po["status"] == CANCELLED:
        head_right.append(Paragraph("CANCELLED", ParagraphStyle("c", parent=st.title, textColor=RED)))
    elif preview:
        head_right.append(Paragraph("PREVIEW. NOT YET ISSUED", ParagraphStyle("p", parent=st.title, textColor=RED)))
    header = Table([[_company_block(conn, st), head_right]], colWidths=[width * 0.5, width * 0.5])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                                ("LINEBELOW", (0, 0), (-1, 0), 1.2, ACCENT)]))

    branch = po["branch"]
    # Every delivery goes to the main warehouse first. The branch named here is who it's for.
    heading = ParagraphStyle("dl", parent=st.label, textColor=ACCENT, fontName=st.bold)
    deliver = [Paragraph("DELIVER TO", heading),
               Paragraph(esc(get_setting(conn, "company.warehouse_name") or "Central Warehouse"), ParagraphStyle("dn", parent=st.s, fontName=st.bold, fontSize=13, leading=16))]
    if get_setting(conn, "company.warehouse_address"):
        deliver.append(Paragraph(esc(get_setting(conn, "company.warehouse_address")), st.small))
    deliver += [Spacer(1, 4), Paragraph("FOR BRANCH", heading), Paragraph(f"{esc(branch['name'])} ({branch['code']})", ParagraphStyle("fb", parent=st.s, fontName=st.bold))]
    meta = _meta_table([("PO date", _long_date(po["po_date"])),
                        ("Expected delivery", _long_date(po["expected_delivery_date"]) or "To be confirmed"),
                        ("Delivery", ARRANGEMENTS[po["delivery_arrangement"]]),
                        ("Payment terms", po["payment_terms"] or "As agreed")], st, width * 0.33 - 4 * mm)
    info = Table([[_supplier_block(po["supplier"], st), deliver, meta]], colWidths=[width * 0.37, width * 0.30, width * 0.33])
    info.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("LEFTPADDING", (1, 0), (1, 0), 7),
                              ("BACKGROUND", (1, 0), (1, 0), ACCENT_PALE), ("LEFTPADDING", (2, 0), (2, 0), 8),
                              ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))

    dr_number = delivery_receipts.format_number(po["po_year"], po["branch_code"], po["po_seq"])
    notice = Table([[Paragraph(f"The Delivery Receipt (DR) for this PO is <b>{esc(dr_number)}</b>. Please hand it over with the goods, with your pieces delivered, unit price and total written in.",
                               ParagraphStyle("dr", parent=st.s, fontName=st.body))]], colWidths=[width])
    notice.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.8, ACCENT), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                                ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6)]))

    heads = [("#", 0), ("SKU", 0), ("Description", 0), ("Brand", 0), ("Size", 0), ("Color", 0), ("UOM", 0),
             ("Qty", 1), ("Unit cost", 1), ("Disc.", 1), ("Net unit", 1), ("Line total", 1)]
    rows = [[Paragraph(t, st.th_r if r else st.th) for t, r in heads]]
    department_rows = []
    line = 0
    for dept, name, group in _by_department(po["items"]):
        department_rows.append(len(rows))
        rows.append([Paragraph(f"{dept} {esc(name)}", st.th)] + [""] * 6 + [Paragraph(f"{fmt_qty(sum(i['qty_m'] * i['pieces_per_unit'] for i in group))} pcs", st.th_r)] + [""] * 3
                    + [Paragraph(fmt_plain(sum(i["line_total_c"] for i in group)), st.th_r)])
        for it in group:
            line += 1
            discount = " + ".join(x for x in (fmt_percent(it["disc1_bp"]), fmt_percent(it["disc2_bp"])) if x)
            code = it["item_code"]
            if it["supplier_item_code"] and it["supplier_item_code"] not in code:
                code = f"{code} / {it['supplier_item_code']}" if code else it["supplier_item_code"]
            unit = it["uom"] + (f" (box of {it['box_size']})" if it["box_size"] else "")
            description = esc(it["description"])
            if it["remarks"]:
                description += "<br/>" + esc(it["remarks"])
            rows.append([Paragraph(str(line), st.small), Paragraph(esc(code), st.small),
                         Paragraph(description, st.small), Paragraph(esc(it["brand"]), st.small), Paragraph(esc(it["size"]), st.small),
                         Paragraph(esc(it["color"]), st.small), Paragraph(esc(unit), st.small), Paragraph(fmt_qty(it["qty_m"] * it["pieces_per_unit"]), st.right),
                         Paragraph(fmt_plain(it["unit_cost_c"]), st.right), Paragraph(discount, st.right),
                         Paragraph(fmt_plain(it["net_unit_c"]), st.right), Paragraph(fmt_plain(it["line_total_c"]), st.right)])
    w = [6, 23, 0, 15, 11, 12, 16, 11, 15, 12, 15, 18]
    col_w = [x * mm for x in w]
    col_w[2] = width - sum(w) * mm
    items = Table(rows, colWidths=col_w, repeatRows=1)
    items.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HEAD), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.3, RULE), ("LINEABOVE", (0, 0), (-1, 0), 0.6, INK),
        ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        *[c for r in department_rows for c in (("SPAN", (0, r), (6, r)), ("BACKGROUND", (0, r), (-1, r), ACCENT_PALE), ("NOSPLIT", (0, r), (-1, r + 1)))]]))

    t = po["totals"]
    names = {i["department_id"]: i["department"] for i in po["items"]}
    dept_rows = [[Paragraph("Dept", st.th), Paragraph("Department", st.th), Paragraph("Pieces", st.th_r), Paragraph("Amount", st.th_r)]]
    for did in sorted(t.departments):
        d = t.departments[did]
        dept_rows.append([Paragraph(str(did), st.small), Paragraph(esc(names[did]), st.small),
                          Paragraph(f"{d.pieces:,}", st.right), Paragraph(fmt_plain(d.amount), st.right)])
    dept_table = Table(dept_rows, colWidths=[10 * mm, 36 * mm, 14 * mm, 22 * mm])
    dept_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), HEAD), ("LINEBELOW", (0, 0), (-1, -1), 0.3, RULE),
                                    ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                                    ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))

    peso = st.peso
    sum_rows = [("Total pieces", f"{t.total_pieces:,}"), ("Subtotal", peso(t.gross))]
    if t.item_discount:
        sum_rows.append(("Item discounts", "-" + peso(t.item_discount)))
    if t.overall_discount:
        sum_rows.append(("Overall discount", "-" + peso(t.overall_discount)))
    if po["vat_mode"] == VAT_INCLUSIVE and po["supplier"].get("vat_registered"):
        sum_rows.append((f"VAT included ({fmt_percent(po['vat_rate_bp'])})", peso(t.vat)))
    elif po["vat_mode"] == VAT_EXCLUSIVE:
        sum_rows.append((f"VAT ({fmt_percent(po['vat_rate_bp'])})", peso(t.vat)))
    if t.other_charges:
        note = f" ({po['other_charges_note']})" if po["other_charges_note"] else ""
        sum_rows.append((f"Other charges{note}", peso(t.other_charges)))
    n = len(sum_rows)
    right_s = ParagraphStyle("r", parent=st.s, alignment=TA_RIGHT)
    summary = Table([[Paragraph(esc(a), st.s), Paragraph(esc(b), right_s)] for a, b in sum_rows]
                    + [[Paragraph("GRAND TOTAL", ParagraphStyle("gt", parent=st.s, fontName=st.bold, fontSize=10)),
                        Paragraph(peso(t.grand_total), ParagraphStyle("gtr", parent=st.s, fontName=st.bold, fontSize=10, alignment=TA_RIGHT))]],
                    colWidths=[48 * mm, 36 * mm])
    summary.setStyle(TableStyle([("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
                                 ("LINEABOVE", (0, n), (-1, n), 0.8, INK), ("TOPPADDING", (0, n), (-1, n), 4),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    totals = Table([[[Paragraph("By department", st.h2), dept_table], summary]], colWidths=[width - 88 * mm, 88 * mm])
    totals.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("LEFTPADDING", (1, 0), (1, 0), 4),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 8)]))

    notes = []
    for title, text in (("Payment terms", po["payment_terms"]), ("Delivery instructions", po["delivery_instructions"]), ("Remarks", po["remarks"])):
        if text:
            notes.append(Paragraph(f"<b>{title}:</b> {esc(text)}", st.s))

    approver = po["approved_by_name"] or ("Within approval limit" if po["auto_approved"] else "")
    # The supplier signs the Warehouse OF. The PO is only printed for them.
    signs = _sign_row(st, width, [("Prepared by", po["prepared_by_name"]), ("Approved by", approver)])

    story = [header, info, Spacer(1, 2), notice, Spacer(1, 6), items, KeepTogether([totals])]
    if notes:
        story += [Spacer(1, 6), *notes]
    story += [Spacer(1, 4), KeepTogether([signs])]
    buffer = BytesIO()
    footer = _footer(st, page, f"{po['po_number']}{rev}  |  {branch['name']}  |  DR {dr_number}")
    doc = SimpleDocTemplate(buffer, pagesize=page, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=10 * mm,
                            bottomMargin=12 * mm, title=f"Purchase Order {po['po_number']}", author="Atlas PO ERP")
    doc.build(story, canvasmaker=footer)
    return buffer.getvalue()


def generate(conn, po_id: int, out_path: str | Path, preview: bool | None = None) -> Path:
    data = render_supplier_po(conn, po_id, preview)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    return out_path


def _of_sums(of: dict, st: Styles) -> list[tuple[str, str]]:
    peso = st.peso
    sums = [("Total pieces", fmt_qty(of["total_pieces_m"])), ("Gross amount", peso(of["gross_c"]))]
    if of["item_discount_c"]:
        sums.append(("Item discounts", "-" + peso(of["item_discount_c"])))
    if of["overall_discount_c"]:
        sums.append(("Overall discount", "-" + peso(of["overall_discount_c"])))
    if of["vat_mode"] == VAT_INCLUSIVE and of["supplier"].get("vat_registered"):
        sums.append((f"VAT included ({fmt_percent(of['vat_rate_bp'])})", peso(of["vat_c"])))
    elif of["vat_mode"] == VAT_EXCLUSIVE:
        sums.append((f"VAT ({fmt_percent(of['vat_rate_bp'])})", peso(of["vat_c"])))
    if of["other_charges_c"]:
        sums.append((esc(of["other_charges_note"]) or "Other charges", peso(of["other_charges_c"])))
    sums.append(("<b>OF total</b>", f"<b>{peso(of['grand_total_c'])}</b>"))
    if of["sticker_deduction_c"]:
        sums.append((f"Less stickers, {fmt_qty(of['total_pieces_m'])} pcs x {fmt_plain(of['sticker_rate_c'])}", "-" + peso(of["sticker_deduction_c"])))
    if of["term_discount_c"]:
        sums.append((f"Less term discount, {fmt_percent(of['term_discount_bp'])}", "-" + peso(of["term_discount_c"])))
    if of["sticker_deduction_c"] or of["term_discount_c"]:
        after = " and ".join(x for x in ("stickers" if of["sticker_deduction_c"] else "", "term discount" if of["term_discount_c"] else "") if x)
        sums.append((f"<b>To pay after {after}</b>", f"<b>{peso(of['net_payable_c'])}</b>"))
    return sums


def _tight_heads(st: Styles) -> tuple[ParagraphStyle, ParagraphStyle, ParagraphStyle]:
    """Column headings a little smaller, because a whole order's columns share an upright page."""
    tight = ParagraphStyle("tight", parent=st.th, fontSize=6.5, leading=8)
    return tight, ParagraphStyle("tightr", parent=tight, alignment=TA_RIGHT), ParagraphStyle("tightc", parent=tight, alignment=TA_CENTER)


def _product_text(it: dict) -> str:
    """The product, with its brand, size, color and unit on a line under it, so the numbers get the page's width."""
    details = " | ".join(esc(it[key]) for key in ("brand", "size", "color", "uom") if it[key])
    return esc(it["description"]) + (f'<br/><font size="6.3" color="#5B6470">{details}</font>' if details else "")


def _of_info(sup: dict, meta: Table, summary_rows: list, st: Styles, width: float) -> list:
    """Who and when side by side, with each branch's PO across the page below."""
    info = Table([[_supplier_block(sup, st), meta]], colWidths=[width * 0.5, width * 0.5])
    info.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                              ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    summary = Table(summary_rows, colWidths=[width * 0.3, width * 0.3, width * 0.15, width * 0.25])
    summary.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), HEAD), ("LINEBELOW", (0, 0), (-1, -1), 0.3, RULE),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                                 ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    return [info, summary]


def _beside_totals(left: list, right: list, width: float) -> Table:
    """Notes on the left and money on the right, which saves the height that used to push the signatures onto a page of their own."""
    block = Table([[left or "", right]], colWidths=[width - 100 * mm, 100 * mm])
    block.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (0, 0), 0), ("RIGHTPADDING", (0, 0), (0, 0), 8), ("RIGHTPADDING", (1, 0), (1, 0), 0), ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    return block


def _of_totals(of: dict, st: Styles) -> Table:
    totals = Table([[Paragraph(a, st.s), Paragraph(b, st.right)] for a, b in _of_sums(of, st)], colWidths=[62 * mm, 34 * mm], hAlign="RIGHT")
    totals.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.3, RULE), ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                                ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2)]))
    return totals


def render_warehouse_of(conn, of_id: int) -> bytes:
    """The warehouse's checking copy: everything arriving from this supplier, by branch, on A4. The supplier signs its costs, and the final payment is based on them."""
    of = get_of(conn, of_id)
    if of["status"] not in (APPROVED, CANCELLED) or not of["of_number"]:
        raise POError("Only an approved OF can be printed for the warehouse.")
    st = Styles()
    page = A4
    width = page[0] - 24 * mm
    sup = of["supplier"]

    rev = f"  Rev. {of['revision']}" if of["revision"] else ""
    head_right = [Paragraph("ORDER FORM. WAREHOUSE COPY", st.title), Paragraph(esc(of["of_number"]) + esc(rev), st.number),
                  Paragraph("For checking deliveries. The supplier signs this copy.", ParagraphStyle("nf", parent=st.small, alignment=TA_RIGHT, textColor=MUTED))]
    if of["status"] == CANCELLED:
        head_right.append(Paragraph("CANCELLED", ParagraphStyle("c", parent=st.title, textColor=RED)))
    header = Table([[_company_block(conn, st), head_right]], colWidths=[width * 0.5, width * 0.5])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                                ("LINEBELOW", (0, 0), (-1, 0), 1.2, ACCENT)]))

    approver = of["approved_by_name"] or ("Within approval limit" if of["auto_approved"] else "")
    meta = _meta_table([("OF date", _long_date(of["of_date"])),
                        ("Expected delivery", _long_date(of["expected_delivery_date"]) or "To be confirmed"),
                        ("Delivery", ARRANGEMENTS[of["delivery_arrangement"]]), ("Payment terms", of["payment_terms"] or "As agreed"),
                        ("Prepared by", of["prepared_by_name"]), ("Approved by", approver)], st, width * 0.5 - 4 * mm)

    live = {p["branch_code"]: p for p in of["pos"]}
    summary_rows = [[Paragraph("Branch", st.th), Paragraph("PO number", st.th), Paragraph("Total pieces", st.th_r), Paragraph("PO status", st.th)]]
    for b in of["branch_columns"]:
        po = live.get(b["code"])
        qty = sum(i["allocations_m"].get(b["code"], 0) * i["pieces_per_unit"] for i in of["items"])
        if po is None and not qty:
            continue
        summary_rows.append([Paragraph(f"{esc(b['name'])} ({b['code']})", st.small), Paragraph(esc(po["po_number"]) if po else "Not generated yet", st.small),
                             Paragraph(fmt_qty(qty), st.right), Paragraph(STATUS_LABELS[po["status"]] if po else "", st.small)])
    info = _of_info(sup, meta, summary_rows, st, width)

    branches = of["branch_columns"]
    tight, tight_r, tight_c = _tight_heads(st)
    heads = [Paragraph("#", tight), Paragraph("SKU", tight), Paragraph("Product", tight)]
    heads += [Paragraph(esc(b["name"]), tight_r) for b in branches]
    heads += [Paragraph(t, tight_r) for t in ("Total pcs", "Unit cost", "Disc.", "Amount")]
    heads += [Paragraph(t, tight_c) for t in ("Received", "Short", "Excess", "Damaged")]
    rows = [heads]
    branch_totals = {b["code"]: 0 for b in branches}
    department_rows = []
    line = 0
    for dept, name, group in _by_department(of["items"]):
        department_rows.append(len(rows))
        # Every quantity here is in pieces.
        subtotal = [sum(i["allocations_m"].get(b["code"], 0) * i["pieces_per_unit"] for i in group) for b in branches]
        rows.append([Paragraph(f"{dept} {esc(name)}", st.th)] + [""] * 2 + [Paragraph(fmt_qty(q) if q else "-", st.th_r) for q in subtotal]
                    + [Paragraph(fmt_qty(sum(subtotal)), st.th_r), "", "", Paragraph(fmt_plain(sum(i["line_total_c"] for i in group)), st.th_r)] + [""] * 4)
        for it in group:
            line += 1
            row = [Paragraph(str(line), st.small), Paragraph(esc(it["item_code"]), st.small), Paragraph(_product_text(it), st.small)]
            for b in branches:
                q = it["allocations_m"].get(b["code"], 0)
                branch_totals[b["code"]] += q * it["pieces_per_unit"]
                row.append(Paragraph(fmt_qty(q * it["pieces_per_unit"]) if q else "-", st.right))
            discount = " + ".join(x for x in (fmt_percent(it["disc1_bp"]), fmt_percent(it["disc2_bp"])) if x)
            row += [Paragraph(f"<b>{fmt_qty(it['total_qty_m'] * it['pieces_per_unit'])}</b>", st.right), Paragraph(fmt_plain(it["unit_cost_c"]), st.right), Paragraph(discount, st.right), Paragraph(fmt_plain(it["line_total_c"]), st.right)]
            row += [""] * 4
            rows.append(row)
    total_row = [Paragraph("Total", st.th)] + [""] * 2
    total_row += [Paragraph(fmt_qty(branch_totals[b["code"]]), st.th_r) for b in branches]
    total_row += [Paragraph(fmt_qty(sum(branch_totals.values())), st.th_r), "", "", Paragraph(fmt_plain(sum(i["line_total_c"] for i in of["items"])), st.th_r)] + [""] * 4
    rows.append(total_row)

    n = len(branches)
    tail = [12, 7.5, 9, 12]
    fixed = [5, 16, 0]
    branch_w = 11.5
    money = [8, 12, 7, 16]
    col_w = [x * mm for x in fixed] + [branch_w * mm] * n + [x * mm for x in money] + [x * mm for x in tail]
    col_w[2] = max(width - sum(col_w), 22 * mm)
    table = Table(rows, colWidths=col_w, repeatRows=1)
    first_branch = len(fixed)
    first_check = first_branch + n + len(money)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HEAD), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEABOVE", (0, 0), (-1, 0), 0.6, INK), ("LINEBELOW", (0, 0), (-1, -2), 0.3, RULE),
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, INK), ("LINEBELOW", (0, -1), (-1, -1), 0.8, INK),
        ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        # Room to write by hand in the checking columns. Each row grows with its product, so a long name can't run into the next row.
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("BACKGROUND", (first_branch, 1), (first_check - 1, -1), colors.HexColor("#F7FBFB")),
        ("LINEBEFORE", (first_branch, 0), (first_branch, -1), 0.6, ACCENT),
        ("GRID", (first_check, 0), (-1, -1), 0.5, MUTED),
        ("SPAN", (0, -1), (first_branch - 1, -1)),
        *[c for r in department_rows for c in (("SPAN", (0, r), (first_branch - 1, r)), ("BACKGROUND", (0, r), (first_check - 1, r), ACCENT_PALE), ("NOSPLIT", (0, r), (-1, r + 1)))],
    ]))

    notes = [Paragraph(f"<b>{title}:</b> {esc(text)}", ParagraphStyle("wn", parent=st.s, spaceAfter=3))
             for title, text in (("Delivery instructions", of["delivery_instructions"]), ("Remarks", of["remarks"]), ("Warehouse notes", of["warehouse_notes"])) if text]
    box = Table([[Paragraph("Warehouse remarks", st.label)]], colWidths=[width - 108 * mm], rowHeights=[18 * mm])
    box.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, MUTED), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    notes.append(box)
    # The signature stays with the totals it agrees to, on the same page.
    story = [header, *info, Spacer(1, 6), table, Spacer(1, 6), KeepTogether([_beside_totals(notes, [_of_totals(of, st)], width), Spacer(1, 2),
                                                              _sign_row(st, width, [("Boxes received", ""), ("Date received", ""), ("Received from supplier by", ""), ("Checked by", ""), ("Supplier agrees to costs", "")])])]
    buffer = BytesIO()
    footer = _footer(st, page, f"{of['of_number']}{rev}  |  Warehouse checking copy  |  {sup['name']}", initials=True)
    doc = SimpleDocTemplate(buffer, pagesize=page, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=10 * mm,
                            bottomMargin=12 * mm, title=f"Order Form {of['of_number']}", author="Atlas PO ERP")
    doc.build(story, canvasmaker=footer)
    return buffer.getvalue()


def warehouse_printed(conn, of_id: int) -> bool:
    """Whether the warehouse copy of this OF's current revision was printed. The supplier signs it and the payment rests on it, so there's one original
    per revision: an amendment raises the revision and prints a new original, and anything else is a copy marked as one."""
    row = conn.execute("SELECT revision FROM order_forms WHERE id = ?", (of_id,)).fetchone()
    printed = {(json.loads(v) or {}).get("revision", 0) if v else 0 for (v,) in conn.execute("SELECT new_value FROM audit_logs WHERE of_id = ? AND action = 'OF_PRINT_WAREHOUSE'", (of_id,)).fetchall()}
    return row is not None and (row[0] or 0) in printed


def _warehouse_copy(conn, user, of_id: int, summary: str) -> bytes:
    """The warehouse copy, marked as a copy when this revision's original was printed already. Every print is recorded with its revision."""
    if user is None:
        return render_warehouse_of(conn, of_id)
    with tx(conn):
        # Read inside the transaction that records the print, so two PCs can't both take this revision's original.
        data = render_warehouse_of(conn, of_id)
        revision = conn.execute("SELECT revision FROM order_forms WHERE id = ?", (of_id,)).fetchone()[0]
        again = warehouse_printed(conn, of_id)
        if again:
            earlier = conn.execute("SELECT COUNT(*) FROM audit_logs WHERE of_id = ? AND action = 'OF_REPRINT'", (of_id,)).fetchone()[0]
            audit(conn, user, "OF_REPRINT", "order_form", of_id, of_id=of_id, summary="Warehouse copy printed again")
            data = stamp_copy(data, earlier + 2, user.display)
        audit(conn, user, "OF_PRINT_WAREHOUSE", "order_form", of_id, of_id=of_id, summary=summary, new={"revision": revision, "copy": again})
    return data


def _no_receivers(user) -> None:
    """Priced papers and print records are for the home office. Receivers count blind, so they never get them."""
    if "RECEIVER" in user.roles:
        raise PermissionDenied(f"{user.display} does not have permission for this (receiving.review).")
    require(user, "receiving.review")


def warehouse_of(conn, user, of_id: int, out_path: str | Path) -> Path:
    _no_receivers(user)
    data = _warehouse_copy(conn, user, of_id, "Warehouse copy printed")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    return out_path


def render_blind_sheet(conn, po_id: int) -> bytes:
    """A blank backup form, for a branch that can't reach the program. It lists what to look for and never how many, so nobody can just copy the PO."""
    po = get_po(conn, po_id)
    if po["status"] not in PRINTABLE:
        raise POError("Only an approved PO has a blind receiving sheet.")
    st = Styles()
    page = A4
    width = page[0] - 24 * mm
    branch = po["branch"]
    head_right = [Paragraph("BLIND RECEIVING SHEET", st.title), Paragraph(esc(po["po_number"]), st.number)]
    if po["of_number"]:
        head_right.append(Paragraph(f"Order Form: {esc(po['of_number'])}", ParagraphStyle("of", parent=st.small, alignment=TA_RIGHT, textColor=MUTED)))
    header = Table([[_company_block(conn, st), head_right]], colWidths=[width * 0.5, width * 0.5])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                                ("LINEBELOW", (0, 0), (-1, 0), 1.2, ACCENT)]))
    heading = ParagraphStyle("bh", parent=st.label, textColor=ACCENT, fontName=st.bold)
    info = Table([[[Paragraph("BRANCH", heading), Paragraph(f"{esc(branch['name'])} ({branch['code']})", ParagraphStyle("bn", parent=st.s, fontName=st.bold, fontSize=13, leading=16))],
                   [Paragraph("Supplier", st.label), Paragraph(esc(po["supplier"]["name"]), st.strong)],
                   [Paragraph("Date received", st.label), Spacer(1, 12), Paragraph("______________________", st.s)]]], colWidths=[width * 0.36, width * 0.36, width * 0.28])
    info.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (0, 0), ACCENT_PALE), ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    notice = Table([[Paragraph("<b>Backup form, only for when the program can't be reached.</b> Each person counts on their own form and doesn't show it to the other. <b>Type it into the program as soon as it's back. Nothing is recorded until then.</b><br/>"
                               "Count every piece, including damaged ones, and write how many you received. Write 0 if a product didn't arrive. Under Damaged, write how many of those are damaged. The expected quantity isn't shown on purpose.", st.s)]], colWidths=[width])
    notice.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.8, ACCENT), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))

    rows = [[Paragraph(t, st.th) for t in ("#", "SKU", "Product", "Size", "Color", "UOM")] + [Paragraph("Pieces received", st.th_c), Paragraph("Damaged", st.th_c), Paragraph("Remarks", st.th)]]
    department_rows, line = [], 0
    for dept, name, group in _by_department(po["items"]):
        department_rows.append(len(rows))
        rows.append([Paragraph(f"{dept} {esc(name)}", st.th)] + [""] * 8)
        for it in group:
            line += 1
            rows.append([Paragraph(str(line), st.small), Paragraph(esc(it["item_code"]), st.small), Paragraph(esc(it["description"]), st.small), Paragraph(esc(it["size"]), st.small),
                         Paragraph(esc(it["color"]), st.small), Paragraph(esc(it["uom"]), st.small), "", "", ""])
    w = [6, 24, 0, 13, 15, 15, 20, 16, 24]
    col_w = [x * mm for x in w]
    col_w[2] = width - sum(w) * mm
    table = Table(rows, colWidths=col_w, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HEAD), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LINEABOVE", (0, 0), (-1, 0), 0.6, INK), ("LINEBELOW", (0, 0), (-1, -1), 0.3, RULE),
        ("GRID", (6, 0), (-1, -1), 0.5, MUTED), ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        *[c for r in department_rows for c in (("SPAN", (0, r), (5, r)), ("BACKGROUND", (0, r), (5, r), ACCENT_PALE), ("NOSPLIT", (0, r), (-1, r + 1)))]]))
    # Room to write by hand, on the product rows.
    table._argH[1:] = [h if i in department_rows else max(h or 0, 9 * mm) for i, h in enumerate(table._argH[1:], start=1)]
    signs = _sign_row(st, width, [("Counted by", ""), ("Typed into the program on (date)", "")])
    story = [header, info, Spacer(1, 2), notice, Spacer(1, 6), table, Spacer(1, 6), KeepTogether([signs])]
    buffer = BytesIO()
    footer = _footer(st, page, f"{po['po_number']}  |  {branch['name']}  |  Blind receiving sheet")
    doc = SimpleDocTemplate(buffer, pagesize=page, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=10 * mm,
                            bottomMargin=12 * mm, title=f"Blind receiving {po['po_number']}", author="Atlas PO ERP")
    doc.build(story, canvasmaker=footer)
    return buffer.getvalue()


def render_cut_slips(conn, po_ids: list[int]) -> bytes:
    """Four to a page, to cut apart and send ahead, so each branch knows which PO is being delivered to it. Says nothing about what or how many."""
    st = Styles()
    page = A4
    width = page[0] - 24 * mm
    heading = ParagraphStyle("sh", parent=st.label, textColor=ACCENT, fontName=st.bold)
    big = ParagraphStyle("sb", parent=st.s, fontName=st.bold, fontSize=20, leading=25)
    note = "This PO is being delivered to your branch. Keep this slip and use the PO number when you receive the delivery."
    rows = []
    for po_id in po_ids:
        po = get_po(conn, po_id)
        branch = po["branch"]
        left = [Paragraph("FOR BRANCH", heading), Paragraph(f"{esc(branch['name'])} ({branch['code']})", big), Spacer(1, 6),
                Paragraph("PO NUMBER", heading), Paragraph(esc(po["po_number"]), big)]
        right = [Paragraph(esc(get_setting(conn, "company.name")), ParagraphStyle("sc", parent=st.strong, textColor=ACCENT)), Paragraph("Branch delivery slip", st.label), Spacer(1, 6)]
        for title, value in (("Supplier", po["supplier"]["name"]), ("Order Form", po["of_number"]), ("Expected delivery", _long_date(po["expected_delivery_date"]) or "To be confirmed")):
            right += [Paragraph(title, st.label), Paragraph(esc(value), st.strong), Spacer(1, 3)]
        body = Table([[left, right]], colWidths=[width * 0.55, width * 0.45])
        body.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (0, 0), ACCENT_PALE), ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
        rows.append([[body, Spacer(1, 6), Paragraph(note, st.s)]])
    table = Table(rows, colWidths=[width], rowHeights=[67 * mm] * len(rows))
    table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                               ("LINEBELOW", (0, 0), (-1, -1), 0.7, MUTED, None, (4, 3))]))
    buffer = BytesIO()
    footer = _footer(st, page, "Cut along the dashed lines and send each slip to its branch")
    doc = SimpleDocTemplate(buffer, pagesize=page, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=10 * mm, bottomMargin=12 * mm,
                            title="Branch delivery slips", author="Atlas PO ERP")
    doc.build([table], canvasmaker=footer)
    return buffer.getvalue()


def render_debit_memo(conn, dm_id: int) -> bytes:
    """What the supplier is charged and why. Issued once, and that exact file is kept."""
    dm = conn.execute("SELECT d.*, u.full_name AS prepared_name FROM debit_memos d JOIN users u ON u.id = d.prepared_by WHERE d.id = ?", (dm_id,)).fetchone()
    lines = conn.execute("SELECT * FROM debit_memo_items WHERE dm_id = ? ORDER BY line_no", (dm_id,)).fetchall()
    po = get_po(conn, dm["po_id"])
    st = Styles()
    page = A4
    width = page[0] - 24 * mm
    head_right = [Paragraph("DEBIT MEMO", st.title), Paragraph(esc(dm["dm_number"]), st.number)]
    header = Table([[_company_block(conn, st), head_right]], colWidths=[width * 0.5, width * 0.5])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("LINEBELOW", (0, 0), (-1, 0), 1.2, ACCENT)]))
    meta = _meta_table([("Date", _long_date(dm["dm_date"])), ("PO number", po["po_number"]), ("Order Form", po["of_number"] or ""), ("For branch", f"{po['branch']['name']} ({po['branch']['code']})")], st, width * 0.45)
    info = Table([[_supplier_block(po["supplier"], st), meta]], colWidths=[width * 0.55, width * 0.45])
    info.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 6)]))
    notice = Table([[Paragraph(f"This amount will be deducted from your payment. Please quote this number and PO {esc(po['po_number'])} on any question about it.", st.s)]], colWidths=[width])
    notice.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.8, ACCENT), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    labels = {"SHORT": "Short", "DAMAGED": "Damaged"}
    rows = [[Paragraph(t, st.th) for t in ("#", "SKU", "Product", "Size", "Color", "Reason")] + [Paragraph(t, st.th_r) for t in ("Pieces", "Cost per pc", "Amount")]]
    for n, line in enumerate(lines, 1):
        rows.append([Paragraph(str(n), st.small), Paragraph(esc(line["item_code"]), st.small), Paragraph(esc(line["description"]), st.small), Paragraph(esc(line["size"]), st.small), Paragraph(esc(line["color"]), st.small),
                     Paragraph(labels[line["reason"]], st.small), Paragraph(fmt_qty(line["pieces_m"]), st.right), Paragraph(fmt_plain(per_piece(line["net_unit_c"], line["pieces_per_unit"])), st.right), Paragraph(fmt_plain(line["amount_c"]), st.right)])
    w = [6, 26, 0, 13, 16, 16, 14, 19, 22]
    col_w = [x * mm for x in w]
    col_w[2] = width - sum(w) * mm
    table = Table(rows, colWidths=col_w, repeatRows=1)
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), HEAD), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LINEABOVE", (0, 0), (-1, 0), 0.6, INK), ("LINEBELOW", (0, 0), (-1, -1), 0.3, RULE),
                               ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    total = Table([[Paragraph("TOTAL CHARGED", st.h2), Paragraph(st.peso(dm["total_c"]), ParagraphStyle("dmt", parent=st.h2, alignment=TA_RIGHT, fontSize=12, leading=15))]], colWidths=[width * 0.75, width * 0.25])
    story = [header, info, Spacer(1, 4), notice, Spacer(1, 8), table, Spacer(1, 6), total]
    if dm["remarks"]:
        story += [Spacer(1, 4), Paragraph(f"<b>Remarks:</b> {esc(dm['remarks'])}", st.s)]
    story += [Spacer(1, 10), KeepTogether([_sign_row(st, width, [("Approved by", dm["prepared_name"]), ("Supplier acknowledgment (name, signature, date)", "")])])]
    buffer = BytesIO()
    footer = _footer(st, page, f"{dm['dm_number']}  |  {po['po_number']}  |  Debit memo")
    doc = SimpleDocTemplate(buffer, pagesize=page, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=10 * mm, bottomMargin=12 * mm, title=f"Debit memo {dm['dm_number']}", author="Atlas PO ERP")
    doc.build(story, canvasmaker=footer)
    return buffer.getvalue()


def render_claim_stub(conn, cs_id: int) -> bytes:
    """Damaged goods the supplier is charged for and has to collect. Approved once, and that exact file is kept."""
    cs = conn.execute("SELECT c.*, u.full_name AS approved_name FROM claim_stubs c JOIN users u ON u.id = c.approved_by WHERE c.id = ?", (cs_id,)).fetchone()
    lines = conn.execute("SELECT * FROM claim_stub_items WHERE cs_id = ? ORDER BY line_no", (cs_id,)).fetchall()
    po = get_po(conn, cs["po_id"])
    place = get_setting(conn, "company.warehouse_name") or "Central Warehouse"
    address = get_setting(conn, "company.warehouse_address")
    st = Styles()
    page = A4
    width = page[0] - 24 * mm
    head_right = [Paragraph("CLAIM STUB", st.title), Paragraph(esc(cs["cs_number"]), st.number)]
    header = Table([[_company_block(conn, st), head_right]], colWidths=[width * 0.5, width * 0.5])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("LINEBELOW", (0, 0), (-1, 0), 1.2, ACCENT)]))
    meta = _meta_table([("Date", _long_date(cs["cs_date"])), ("PO number", po["po_number"]), ("Order Form", po["of_number"] or ""), ("From branch", f"{po['branch']['name']} ({po['branch']['code']})")], st, width * 0.45)
    info = Table([[_supplier_block(po["supplier"], st), meta]], colWidths=[width * 0.55, width * 0.45])
    info.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 6)]))
    where = esc(place) + (f", {esc(address)}" if address else "")
    notice = Table([[Paragraph(f"These damaged goods are charged to you and will be deducted from your payment. Collect them from <b>{where}</b> using this stub. Goods not collected within 90 days of reaching the warehouse are forfeited.", st.s)]], colWidths=[width])
    notice.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.8, ACCENT), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    rows = [[Paragraph(t, st.th) for t in ("#", "SKU", "Product", "Size", "Color")] + [Paragraph(t, st.th_r) for t in ("Pieces", "Cost per pc", "Amount")]]
    for n, line in enumerate(lines, 1):
        rows.append([Paragraph(str(n), st.small), Paragraph(esc(line["item_code"]), st.small), Paragraph(esc(line["description"]), st.small), Paragraph(esc(line["size"]), st.small), Paragraph(esc(line["color"]), st.small),
                     Paragraph(fmt_qty(line["pieces_m"]), st.right), Paragraph(fmt_plain(per_piece(line["net_unit_c"], line["pieces_per_unit"])), st.right), Paragraph(fmt_plain(line["amount_c"]), st.right)])
    w = [6, 26, 0, 13, 16, 14, 19, 22]
    col_w = [x * mm for x in w]
    col_w[2] = width - sum(w) * mm
    table = Table(rows, colWidths=col_w, repeatRows=1)
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), HEAD), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LINEABOVE", (0, 0), (-1, 0), 0.6, INK), ("LINEBELOW", (0, 0), (-1, -1), 0.3, RULE),
                               ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    total = Table([[Paragraph("TOTAL CHARGED", st.h2), Paragraph(st.peso(cs["total_c"]), ParagraphStyle("cst", parent=st.h2, alignment=TA_RIGHT, fontSize=12, leading=15))]], colWidths=[width * 0.75, width * 0.25])
    story = [header, info, Spacer(1, 4), notice, Spacer(1, 8), table, Spacer(1, 6), total]
    if cs["remarks"]:
        story += [Spacer(1, 4), Paragraph(f"<b>Remarks:</b> {esc(cs['remarks'])}", st.s)]
    story += [Spacer(1, 10), KeepTogether([_sign_row(st, width, [("Approved by", cs["approved_name"]), ("Goods collected by (name, signature, date)", "")])])]
    buffer = BytesIO()
    footer = _footer(st, page, f"{cs['cs_number']}  |  {po['po_number']}  |  Claim stub")
    doc = SimpleDocTemplate(buffer, pagesize=page, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=10 * mm, bottomMargin=12 * mm, title=f"Claim stub {cs['cs_number']}", author="Atlas PO ERP")
    doc.build(story, canvasmaker=footer)
    return buffer.getvalue()


def render_item_pass(conn, ip_id: int) -> bytes:
    """Excess pieces signed out of a branch: an original that goes with the goods and a duplicate that stays. No money, because excess is never paid for."""
    ip = conn.execute("SELECT i.*, u.full_name AS printed_name FROM item_passes i JOIN users u ON u.id = i.printed_by WHERE i.id = ?", (ip_id,)).fetchone()
    lines = conn.execute("SELECT * FROM item_pass_items WHERE ip_id = ? ORDER BY line_no", (ip_id,)).fetchall()
    place = get_setting(conn, "company.warehouse_name") or "Central Warehouse"
    return _goods_pass(conn, "ITEM PASS", ip["ip_number"], ip["ip_date"], ip["po_id"], lines, ip["printed_name"], "Item pass",
                       f"These pieces arrived beyond the order, so they aren't paid for. They go back to <b>{esc(place)}</b> for the supplier to collect. Goods not collected within 90 days of reaching the warehouse are forfeited. This pass prints only once.")


def render_claim_stub_pass(conn, cs_id: int, printed_by: str) -> bytes:
    """Damaged goods on a claim stub, signed out of the branch for the warehouse. Pieces only: no costs, because receivers never see them."""
    cs = conn.execute("SELECT * FROM claim_stubs WHERE id = ?", (cs_id,)).fetchone()
    lines = conn.execute("SELECT * FROM claim_stub_items WHERE cs_id = ? ORDER BY line_no", (cs_id,)).fetchall()
    place = get_setting(conn, "company.warehouse_name") or "Central Warehouse"
    return _goods_pass(conn, "DAMAGED GOODS PASS", cs["cs_number"], today().isoformat(), cs["po_id"], lines, printed_by, "Damaged goods pass",
                       f"These pieces are on claim stub {esc(cs['cs_number'])}. They go back to <b>{esc(place)}</b>, where the warehouse counts them, and the supplier collects them there.")


def _goods_pass(conn, title: str, number: str, day: str, po_id: int, lines, printed_name: str, kind: str, notice_text: str) -> bytes:
    po = get_po(conn, po_id)
    place = get_setting(conn, "company.warehouse_name") or "Central Warehouse"
    st = Styles()
    page = A4
    width = page[0] - 24 * mm

    def one(copy: str, where: str) -> bytes:
        head_right = [Paragraph(title, st.title), Paragraph(esc(number), st.number), Paragraph(f"<b>{copy}</b>: {where}", ParagraphStyle("ipc", parent=st.s, alignment=TA_RIGHT))]
        header = Table([[_company_block(conn, st), head_right]], colWidths=[width * 0.5, width * 0.5])
        header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("LINEBELOW", (0, 0), (-1, 0), 1.2, ACCENT)]))
        meta = _meta_table([("Date", _long_date(day)), ("PO number", po["po_number"]), ("Order Form", po["of_number"] or ""), ("From branch", f"{po['branch']['name']} ({po['branch']['code']})"), ("Going to", place)], st, width * 0.45)
        info = Table([[_supplier_block(po["supplier"], st), meta]], colWidths=[width * 0.55, width * 0.45])
        info.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 6)]))
        notice = Table([[Paragraph(notice_text, st.s)]], colWidths=[width])
        notice.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.8, ACCENT), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
        rows = [[Paragraph(t, st.th) for t in ("#", "SKU", "Product", "Size", "Color", "UOM")] + [Paragraph("Pieces", st.th_r)]]
        for n, line in enumerate(lines, 1):
            rows.append([Paragraph(str(n), st.small), Paragraph(esc(line["item_code"]), st.small), Paragraph(esc(line["description"]), st.small), Paragraph(esc(line["size"]), st.small), Paragraph(esc(line["color"]), st.small),
                         Paragraph(esc(line["uom"]), st.small), Paragraph(fmt_qty(line["pieces_m"]), st.right)])
        rows.append(["", "", Paragraph("<b>Total pieces</b>", st.small), "", "", "", Paragraph(f"<b>{fmt_qty(sum(line['pieces_m'] for line in lines))}</b>", st.right)])
        w = [6, 26, 0, 16, 22, 16, 18]
        col_w = [x * mm for x in w]
        col_w[2] = width - sum(w) * mm
        table = Table(rows, colWidths=col_w, repeatRows=1)
        table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), HEAD), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LINEABOVE", (0, 0), (-1, 0), 0.6, INK), ("LINEBELOW", (0, 0), (-1, -1), 0.3, RULE),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
        story = [header, info, Spacer(1, 4), notice, Spacer(1, 8), table, Spacer(1, 10),
                 KeepTogether([_sign_row(st, width, [("Printed by", printed_name), ("Signed out by (name, signature, date)", ""), (f"Received at {esc(place)} (name, signature, date)", "")])])]
        buffer = BytesIO()
        footer = _footer(st, page, f"{number}  |  {po['po_number']}  |  {kind}, {copy.lower()}")
        doc = SimpleDocTemplate(buffer, pagesize=page, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=10 * mm, bottomMargin=12 * mm, title=f"{kind} {number}", author="Atlas PO ERP")
        doc.build(story, canvasmaker=footer)
        return buffer.getvalue()

    return combine([one("ORIGINAL", "goes with the goods"), one("DUPLICATE", "stays at the branch")])


def render_office_of(conn, of_id: int) -> bytes:
    """Accounting's copy of the whole order: costs, per-piece costs, totals, and what stickers take off the payment."""
    of = get_of(conn, of_id)
    if of["status"] not in (APPROVED, CANCELLED) or not of["of_number"]:
        raise POError("Only an approved OF can be printed for the office.")
    st = Styles()
    page = A4
    width = page[0] - 24 * mm
    sup = of["supplier"]

    rev = f"  Rev. {of['revision']}" if of["revision"] else ""
    head_right = [Paragraph("ORDER FORM. FINAL COPY", st.title), Paragraph(esc(of["of_number"]) + esc(rev), st.number),
                  Paragraph("The final figures, for accounting.", ParagraphStyle("nf", parent=st.small, alignment=TA_RIGHT, textColor=MUTED))]
    if of["status"] == CANCELLED:
        head_right.append(Paragraph("CANCELLED", ParagraphStyle("c", parent=st.title, textColor=RED)))
    header = Table([[_company_block(conn, st), head_right]], colWidths=[width * 0.5, width * 0.5])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                                ("LINEBELOW", (0, 0), (-1, 0), 1.2, ACCENT)]))

    approver = of["approved_by_name"] or ("Within approval limit" if of["auto_approved"] else "")
    meta = _meta_table([("OF date", _long_date(of["of_date"])), ("Expected delivery", _long_date(of["expected_delivery_date"]) or "To be confirmed"),
                        ("Payment terms", of["payment_terms"] or "As agreed"), ("Delivery", ARRANGEMENTS[of["delivery_arrangement"]]),
                        ("Prepared by", of["prepared_by_name"]), ("Approved by", approver)], st, width * 0.5 - 4 * mm)
    live = {p["branch_code"]: p for p in of["pos"]}
    summary_rows = [[Paragraph("Branch", st.th), Paragraph("PO number", st.th), Paragraph("Pieces", st.th_r), Paragraph("Amount", st.th_r)]]
    for b in of["branch_columns"]:
        po = live.get(b["code"])
        qty = sum(i["allocations_m"].get(b["code"], 0) * i["pieces_per_unit"] for i in of["items"])
        if po is None and not qty:
            continue
        summary_rows.append([Paragraph(f"{esc(b['name'])} ({b['code']})", st.small), Paragraph(esc(po["po_number"]) if po else "Not generated yet", st.small),
                             Paragraph(fmt_qty(qty), st.right), Paragraph(fmt_plain(po["grand_total_c"]) if po else "", st.right)])
    info = _of_info(sup, meta, summary_rows, st, width)

    branches = of["branch_columns"]
    leading = 3
    tight, tight_r, _ = _tight_heads(st)
    heads = [Paragraph(t, tight) for t in ("#", "SKU", "Product")] + [Paragraph(esc(b["name"]), tight_r) for b in branches]
    heads += [Paragraph(t, tight_r) for t in ("Total pcs", "Unit cost", "Disc.", "Per pc", "Amount")]
    rows = [heads]
    department_rows = []
    line = 0
    for dept, name, group in _by_department(of["items"]):
        department_rows.append(len(rows))
        subtotal = [sum(i["allocations_m"].get(b["code"], 0) * i["pieces_per_unit"] for i in group) for b in branches]
        rows.append([Paragraph(f"{dept} {esc(name)}", st.th)] + [""] * (leading - 1) + [Paragraph(fmt_qty(q) if q else "-", st.th_r) for q in subtotal]
                    + [Paragraph(fmt_qty(sum(subtotal)), st.th_r), "", "", "", Paragraph(fmt_plain(sum(i["line_total_c"] for i in group)), st.th_r)])
        for it in group:
            line += 1
            row = [Paragraph(str(line), st.small), Paragraph(esc(it["item_code"]), st.small), Paragraph(_product_text(it), st.small)]
            for b in branches:
                q = it["allocations_m"].get(b["code"], 0)
                row.append(Paragraph(fmt_qty(q * it["pieces_per_unit"]) if q else "-", st.right))
            discount = " + ".join(x for x in (fmt_percent(it["disc1_bp"]), fmt_percent(it["disc2_bp"])) if x)
            row += [Paragraph(f"<b>{fmt_qty(it['total_qty_m'] * it['pieces_per_unit'])}</b>", st.right), Paragraph(fmt_plain(it["unit_cost_c"]), st.right), Paragraph(discount, st.right),
                    Paragraph(fmt_plain(per_piece(it["net_unit_c"], it["pieces_per_unit"])), st.right), Paragraph(fmt_plain(it["line_total_c"]), st.right)]
            rows.append(row)
    fixed = [5, 17, 0]
    col_w = [x * mm for x in fixed] + [11 * mm] * len(branches) + [x * mm for x in (10, 14, 9, 13, 18)]
    col_w[2] = max(width - sum(col_w), 22 * mm)
    table = Table(rows, colWidths=col_w, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HEAD), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LINEABOVE", (0, 0), (-1, 0), 0.6, INK), ("LINEBELOW", (0, 0), (-1, -1), 0.3, RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        *[c for r in department_rows for c in (("SPAN", (0, r), (leading - 1, r)), ("BACKGROUND", (0, r), (-1, r), ACCENT_PALE), ("NOSPLIT", (0, r), (-1, r + 1)))]]))

    peso = st.peso
    money, side = [_of_totals(of, st)], []
    complete = receiving.of_delivery_complete(conn, of_id)
    final = payments.settlement(conn, of_id)
    settle = [[Paragraph(f"{c['kind']} {esc(c['number'])}", st.s), Paragraph("-" + peso(c["total_c"]), st.right)] for c in final["charges"]]
    if complete and final["unclaimed_c"]:
        settle.append([Paragraph("Short, with no debit memo", st.s), Paragraph("-" + peso(final["unclaimed_c"]), st.right)])
    # Stickers and the term discount were worked out on what was ordered, so they move with what arrived.
    if complete and final["sticker_c"] != of["sticker_deduction_c"]:
        settle.append([Paragraph(f"Stickers on the {fmt_qty(final['sticker_m'])} pcs received", st.s), Paragraph(peso(of["sticker_deduction_c"] - final["sticker_c"]), st.right)])
    if complete and final["term_c"] != of["term_discount_c"]:
        settle.append([Paragraph("Term discount on what was delivered", st.s), Paragraph(peso(of["term_discount_c"] - final["term_c"]), st.right)])
    # A DR lower than ours is what is paid, no sign-off needed.
    if complete and final.get("dr_lower_c"):
        settle.append([Paragraph("DR total lower", st.s), Paragraph("-" + peso(final["dr_lower_c"]), st.right)])
    # A debit memo cancelled after payment no longer comes off, and is paid back on the supplier's next check.
    for memo in final.get("paid_back", []) if complete else []:
        settle.append([Paragraph(f"Debit memo {esc(memo['dm_number'])} cancelled after payment, paid back ({peso(memo['total_c'])})", st.s), Paragraph("not deducted", st.right)])
    if settle:
        # One final figure, so accounting isn't left with two totals to choose from.
        # Named after what the figure takes off, the term discount and any pieces short with no debit memo included.
        stickers = final["sticker_c"] if complete else of["sticker_deduction_c"]
        term = final["term_c"] if complete else of["term_discount_c"]
        short = complete and final["unclaimed_c"]
        if stickers or term or short or (complete and final.get("dr_lower_c")):
            parts = (["stickers"] if stickers else []) + (["term discount"] if term else []) + ["debit memos", "claim stubs"] + (["pieces short"] if short else []) + (["the lower DRs"] if complete and final.get("dr_lower_c") else [])
            last = "To pay after " + ", ".join(parts[:-1]) + " and " + parts[-1]
        else:
            last = "OF total less debit memos and claim stubs"
        amount = final["pay_c"] if complete else of["net_payable_c"] - final["charged_c"]
        settle.append([Paragraph(f"<b>{last}</b>", st.s), Paragraph(f"<b>{peso(amount)}</b>", st.right)])
        block = Table(settle, colWidths=[62 * mm, 34 * mm], hAlign="RIGHT")
        block.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.3, RULE), ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2), ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2)]))
        money += [Spacer(1, 6), block]
    if of["sticker_deduction_c"] and not complete:
        # Worked out on what was ordered, until the delivery is complete.
        room = width - 108 * mm
        stickers = Table([[Paragraph(text, st.s)] for text in ("<b>Stickers, on delivery</b>", "Received: ____________ pcs", "Final deduction: ____________", "Final to pay: ____________")], colWidths=[room])
        stickers.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.8, ACCENT), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
        side += [stickers, Spacer(1, 6)]
    side += [Paragraph(f"<b>{title}:</b> {esc(text)}", ParagraphStyle("on", parent=st.s, spaceAfter=3)) for title, text in (("Remarks", of["remarks"]), ("Internal notes", of["internal_notes"])) if text]
    story = [header, *info, Spacer(1, 6), table, Spacer(1, 6), KeepTogether([_beside_totals(side, money, width), Spacer(1, 2),
                                                              _sign_row(st, width, [("Prepared by", of["prepared_by_name"]), ("Approved by", approver), ("Received by accounting", "")])])]
    buffer = BytesIO()
    footer = _footer(st, page, f"{of['of_number']}{rev}  |  Final copy  |  {sup['name']}")
    doc = SimpleDocTemplate(buffer, pagesize=page, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=10 * mm,
                            bottomMargin=12 * mm, title=f"Order Form {of['of_number']}, final copy", author="Atlas PO ERP")
    doc.build(story, canvasmaker=footer)
    return buffer.getvalue()


def office_ready(conn, of_id: int) -> bool:
    """Accounting's copy waits for the final figures. A cancelled OF has none coming, so its copy is just a record."""
    return get_of(conn, of_id)["status"] == CANCELLED or receiving.of_delivery_complete(conn, of_id)


def office_copy(conn, user, of_id: int) -> bytes:
    _no_receivers(user)
    if not office_ready(conn, of_id):
        raise POError(f"{get_of(conn, of_id)['of_number']}: the Office OF prints once its delivery is complete. Check Received, To check.")
    with tx(conn):
        earlier = conn.execute("SELECT COUNT(*) FROM audit_logs WHERE of_id = ? AND action = 'OF_PRINT_OFFICE'", (of_id,)).fetchone()[0]
        # The first print lets the check go ahead, so it's Accounting's step. Copies after that are open to anyone who can see the order.
        if not earlier and not user.can("payment.edit"):
            raise PermissionDenied("Only Accounting prints the first Final OF.")
        data = render_office_of(conn, of_id)
        audit(conn, user, "OF_PRINT_OFFICE", "order_form", of_id, of_id=of_id, summary="Office copy printed", new={"pay_c": payments.settlement(conn, of_id)["pay_c"]})
    data = stamp_copy(data, earlier + 1, user.display) if earlier else data
    # The debit memos on it come after, each printed and recorded like any print of a memo.
    from . import debit_memos
    memos = [r["id"] for r in conn.execute("SELECT d.id FROM debit_memos d JOIN purchase_orders p ON p.id = d.po_id WHERE p.of_id = ? AND d.status = 'ISSUED' ORDER BY d.id", (of_id,))]
    return combine([data, *[debit_memos.document(conn, user, dm_id) for dm_id in memos]]) if memos else data


def office_print_count(conn, of_id: int) -> int:
    # Not used by the program; kept only because audit scripts under po_app/audit import it. Remove when the audit closes.
    return conn.execute("SELECT COUNT(*) FROM audit_logs WHERE of_id = ? AND action = 'OF_PRINT_OFFICE'", (of_id,)).fetchone()[0]


def office_printed_at(conn, of_id: int) -> str | None:
    return conn.execute("SELECT MAX(occurred_at) FROM audit_logs WHERE of_id = ? AND action = 'OF_PRINT_OFFICE'", (of_id,)).fetchone()[0]


def office_of(conn, user, of_id: int, out_path: str | Path) -> Path:
    data = office_copy(conn, user, of_id)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    return out_path


def render_delivery_receipt(conn, dr_id: int) -> bytes:
    """One branch's products, with blanks for the supplier's own pieces delivered, unit price and total. No prices print, however the supplier gets it."""
    dr = delivery_receipts.get(conn, dr_id)
    po = get_po(conn, dr["po_id"])
    if po["status"] == CANCELLED:
        raise POError(f"{po['po_number']} is cancelled, so its DR can't be used.")
    st = Styles()
    page = A4
    width = page[0] - 24 * mm
    branch = po["branch"]
    head_right = [Paragraph("DELIVERY RECEIPT", st.title), Paragraph(esc(dr["dr_number"]), st.number),
                  Paragraph(f"PO: {esc(po['po_number'])}" + (f"  |  Order Form: {esc(po['of_number'])}" if po["of_number"] else ""), ParagraphStyle("of", parent=st.small, alignment=TA_RIGHT, textColor=MUTED))]
    header = Table([[_company_block(conn, st), head_right]], colWidths=[width * 0.5, width * 0.5])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                                ("LINEBELOW", (0, 0), (-1, 0), 1.2, ACCENT)]))
    heading = ParagraphStyle("dl", parent=st.label, textColor=ACCENT, fontName=st.bold)
    deliver = [Paragraph("DELIVER TO", heading),
               Paragraph(esc(get_setting(conn, "company.warehouse_name") or "Central Warehouse"), ParagraphStyle("dn", parent=st.s, fontName=st.bold, fontSize=13, leading=16))]
    if get_setting(conn, "company.warehouse_address"):
        deliver.append(Paragraph(esc(get_setting(conn, "company.warehouse_address")), st.small))
    deliver += [Spacer(1, 4), Paragraph("FOR BRANCH", heading), Paragraph(f"{esc(branch['name'])} ({branch['code']})", ParagraphStyle("fb", parent=st.s, fontName=st.bold))]
    delivered = [Paragraph("Delivery", st.label), Paragraph(f"Delivery {dr['delivery_no']}" if dr["delivery_no"] > 1 else "First delivery", st.s), Spacer(1, 4),
                 Paragraph("Date delivered", st.label), Spacer(1, 12), Paragraph("______________________", st.s)]
    info = Table([[_supplier_block(po["supplier"], st), deliver, delivered]], colWidths=[width * 0.37, width * 0.33, width * 0.30])
    info.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("LEFTPADDING", (1, 0), (1, 0), 7), ("BACKGROUND", (1, 0), (1, 0), ACCENT_PALE),
                              ("LEFTPADDING", (2, 0), (2, 0), 8), ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    notice = Table([[Paragraph("Write in the pieces delivered, your unit price and the total for each product, then sign below. Hand this over with the goods.", st.s)]], colWidths=[width])
    notice.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.8, ACCENT), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))

    rows = [[Paragraph(t, st.th) for t in ("#", "SKU", "Product", "Size", "Color", "UOM")] + [Paragraph(t, st.th_c) for t in ("Pieces ordered", "Pieces delivered", "Unit price", "Total")]]
    department_rows, line = [], 0
    for dept, name, group in _by_department(po["items"]):
        department_rows.append(len(rows))
        rows.append([Paragraph(f"{dept} {esc(name)}", st.th)] + [""] * 9)
        for it in group:
            line += 1
            rows.append([Paragraph(str(line), st.small), Paragraph(esc(it["item_code"]), st.small), Paragraph(esc(it["description"]), st.small), Paragraph(esc(it["size"]), st.small),
                         Paragraph(esc(it["color"]), st.small), Paragraph(esc(it["uom"]), st.small), Paragraph(fmt_qty(it["qty_m"] * it["pieces_per_unit"]), st.center), "", "", ""])
    rows.append([Paragraph("Total", st.th)] + [""] * 5 + [Paragraph(fmt_qty(sum(i["qty_m"] * i["pieces_per_unit"] for i in po["items"])), ParagraphStyle("tc", parent=st.th_c))] + [""] * 3)
    w = [6, 22, 0, 12, 14, 14, 15, 17, 19, 23]
    col_w = [x * mm for x in w]
    col_w[2] = width - sum(w) * mm
    table = Table(rows, colWidths=col_w, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HEAD), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LINEABOVE", (0, 0), (-1, 0), 0.6, INK), ("LINEBELOW", (0, 0), (-1, -1), 0.3, RULE),
        ("GRID", (7, 0), (-1, -1), 0.5, MUTED), ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("SPAN", (0, -1), (5, -1)), ("LINEABOVE", (0, -1), (-1, -1), 0.8, INK),
        *[c for r in department_rows for c in (("SPAN", (0, r), (6, r)), ("BACKGROUND", (0, r), (6, r), ACCENT_PALE), ("NOSPLIT", (0, r), (-1, r + 1)))]]))
    # Room to write by hand, on the product rows and the total.
    table._argH[1:] = [h if i in department_rows else max(h or 0, 9 * mm) for i, h in enumerate(table._argH[1:], start=1)]
    signs = _sign_row(st, width, [("Supplier (name, signature, date)", ""), ("Received by", ""), ("Date received", "")])
    buffer = BytesIO()
    footer = _footer(st, page, f"{dr['dr_number']}  |  {po['po_number']}  |  {branch['name']}")
    doc = SimpleDocTemplate(buffer, pagesize=page, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=10 * mm,
                            bottomMargin=12 * mm, title=f"Delivery Receipt {dr['dr_number']}", author="Atlas PO ERP")
    doc.build([header, info, Spacer(1, 2), notice, Spacer(1, 6), table, Spacer(1, 6), KeepTogether([signs])], canvasmaker=footer)
    return buffer.getvalue()


def delivery_receipt(conn, user, dr_id: int) -> bytes:
    """A DR to print here. Any print after the first is marked as a copy, and every print is recorded.
    The first print is the original, so it takes the right to issue POs. Printing it again is open to the home office,
    never to a receiver, who counts blind."""
    _no_receivers(user)
    with tx(conn):
        # Read inside the transaction that records the print, so two PCs can't both take the original.
        earlier = delivery_receipts.get(conn, dr_id)["prints"]
        if not earlier:
            require(user, "po.issue")
        data = render_delivery_receipt(conn, dr_id)
        _record_dr_print(conn, user, dr_id)
    return stamp_copy(data, earlier + 1, user.display) if earlier else data


def _record_dr_print(conn, user, dr_id: int) -> None:
    dr = delivery_receipts.get(conn, dr_id)
    with tx(conn):
        audit(conn, user, "DR_PRINT", "delivery_receipt", dr_id, po_id=dr["po_id"], summary=f"{dr['dr_number']} printed")


def office_receipts(conn, user, po_ids: list[int]) -> list[bytes]:
    """The DRs that print here with their POs, for suppliers we sticker for. Copies are marked by whoever prints the whole set again."""
    _no_receivers(user)
    documents = []
    for dr_id in delivery_receipts.first_office_ids(conn, po_ids):
        documents.append(render_delivery_receipt(conn, dr_id))
        _record_dr_print(conn, user, dr_id)
    return documents


def receipts_for_email(conn, user, group: dict) -> Path:
    """One order's DRs as one file to attach. These aren't marked as copies: the supplier prints them, and saving twice doesn't make a second document."""
    require(user, "po.issue")
    folder = data_dir() / "email"
    folder.mkdir(exist_ok=True)
    path = folder / f"DR {group['reference']} {group['supplier_code']}.pdf"
    path.write_bytes(combine([render_delivery_receipt(conn, d["id"]) for d in group["receipts"]]))
    with tx(conn):
        for d in group["receipts"]:
            audit(conn, user, "DR_SAVED_FOR_EMAIL", "delivery_receipt", d["id"], po_id=d["po_id"], summary=f"{d['dr_number']} saved to email")
    return path


def _print_set(conn, user, of_id: int, po_ids: list[int], po_documents: list[bytes], paper_forms: bool = False, reprint: bool = False) -> bytes:
    """What a delivery needs: the warehouse copy, then the branch slips, POs and any DRs printed here. Rendered last so they show the PO numbers.
    Receivers count in the program, so paper forms only print when asked for: two per branch, because each counter needs their own to keep the count blind.
    The office copy isn't here, because accounting gets it once the delivery is complete. A reprint is stamped as a whole by the caller."""
    summary = f"Warehouse copy{', paper count forms' if paper_forms else ''} and branch slips printed with its POs"
    if reprint:
        with tx(conn):
            warehouse = render_warehouse_of(conn, of_id)
            revision = conn.execute("SELECT revision FROM order_forms WHERE id = ?", (of_id,)).fetchone()[0]
            audit(conn, user, "OF_PRINT_WAREHOUSE", "order_form", of_id, of_id=of_id, summary=summary, new={"revision": revision, "copy": True})
    else:
        warehouse = _warehouse_copy(conn, user, of_id, summary)
    forms = [render_blind_sheet(conn, po_id) for po_id in po_ids for _ in range(2)] if paper_forms else []
    return combine([warehouse, *forms, render_cut_slips(conn, po_ids), *po_documents, *office_receipts(conn, user, po_ids)])


def print_order_form(conn, user, of_id: int, paper_forms: bool = False) -> tuple[bytes | None, list[str]]:
    """Print an approved OF for the first time: makes its POs if they don't exist yet, and issues any not yet issued.

    All of an OF's branch POs go together or none do, so the supplier and the warehouse never hold part of an order. If one can't go, nothing is issued
    and the reason comes back. Returns the file, and what stopped it.
    """
    of = get_of(conn, of_id)
    try:
        pos = generate_pos(conn, user, of_id)
    except POError as exc:
        return None, [f"{of['of_number']}: {exc}"]
    wanted = [p for p in pos if to_issue(p)]
    if not wanted:
        return None, [f"{of['of_number']}: it has no POs left to print."]
    done = [p["id"] for p in wanted]
    try:
        # The papers are made in the same transaction as the issue. If any of them can't be made, nothing is issued or recorded as printed,
        # so the POs stay ready to print and the warehouse original is still there to take.
        with tx(conn):
            for p in wanted:
                try:
                    issue(conn, user, p["id"])
                except POError as exc:
                    raise POError(f"{of['of_number']}: {p['po_number']} can't be issued, so none of its POs were. {exc}") from None
            document = _print_set(conn, user, of_id, done, [issued_pdf(conn, None, po_id) for po_id in done], paper_forms)
    except POError as exc:
        return None, [str(exc)]
    return document, []


def blind_copy(conn, user, po_id: int) -> bytes:
    """One blank form. It isn't marked as a copy, because each counter needs their own and it holds nothing until they write on it. It's still recorded."""
    _no_receivers(user)
    data = render_blind_sheet(conn, po_id)
    with tx(conn):
        audit(conn, user, "BLIND_PRINT", "purchase_order", po_id, po_id=po_id, summary="Paper count form printed")
    return data


def reprint_order_form(conn, user, of_id: int, paper_forms: bool = False) -> bytes:
    """The warehouse copy and slips again, with the original issued PDF of each PO, every page marked as printed again. Each is recorded."""
    _no_receivers(user)
    # One transaction for the records and the papers, so the copy number is read where it's recorded and a paper that can't be made records nothing.
    with tx(conn):
        live = [p["id"] for p in get_of(conn, of_id)["pos"] if p["status"] != CANCELLED]
        documents = [issued_pdf(conn, None, po_id) for po_id in live]
        earlier = conn.execute("SELECT COUNT(*) FROM audit_logs WHERE of_id = ? AND action = 'OF_REPRINT'", (of_id,)).fetchone()[0]
        audit(conn, user, "OF_REPRINT", "order_form", of_id, of_id=of_id, summary="Warehouse copy, slips and POs printed again")
        for po_id in live:
            audit(conn, user, "PO_REPRINT", "purchase_order", po_id, po_id=po_id, summary="Reprinted with its Order Form")
            # A PO issued before DRs existed gets its DR now. Whether a view-only reprint should do this is still to be decided, so it stays as it was.
            delivery_receipts._ensure_first(conn, user, po_id)
        data = _print_set(conn, user, of_id, live, [d for d in documents if d], paper_forms, reprint=True)
    return stamp_copy(data, earlier + 2, user.display)
