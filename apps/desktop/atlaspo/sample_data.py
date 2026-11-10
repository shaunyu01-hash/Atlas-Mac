"""A separate practice database with every screen and report filled in. Everything is invented.

It goes through the real rules, so numbers, approvals, versions and the audit log are all genuine. It never touches
the real database: build() refuses a database that already has users, and sample orders would use up numbers for good.
"""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from . import item_passes, auth, backup, claim_stubs, db, debit_memos, delivery_receipts, masters, order_forms, orders, payments, pdf, receiving, selling
from .db import POError, get_setting

FOLDER = Path(os.environ["PO_SAMPLE_DIR"]) if os.environ.get("PO_SAMPLE_DIR") else Path.home() / "Documents" / "Atlas PO ERP Sample"
PASSWORD = "123"  # Every sample login, so anyone checking can get straight in. The real program's password rule doesn't change.
_BUILDING = "sample-build-only"  # Meets that rule while the logins are made through the real code. _easy_logins replaces it.
FLAG = "sample.data"
NORTH, SOUTH, EAST, WEST, CLOSED_BRANCH = 2, 4, 6, 8, 10

# Easy-to-remember suppliers: AAA, BBB and CCC, Company A, B and C, each with its own Brand A, B or C and item codes
# starting with its letter. AAA carries every situation: it has a term discount, and agrees to a sticker deduction from 30 June 2026 until 1 August,
# so orders in July show the warehouse stickering, with DRs printed at the office, and the rest come stickered, with DRs emailed.
# BBB and CCC add plain business around it, so lists and reports have more than one supplier.
SUPPLIERS = {
    "AAA": {"name": "AAA", "registered_name": "Company A Trading (Sample)", "contact_person": "Contact A", "telephone": "0917 555 0101",
            "email": "sales@aaa-sample.example", "tin": "100-000-001-000", "payment_terms": "30D", "sticker_rate_c": 0, "term_discount_bp": 300,
            "term_discount_days": 14, "default_discount_bp": 500, "vat_registered": False, "vat_mode": "NONE", "address": "SAMPLE, Metro City"},
    "BBB": {"name": "BBB", "registered_name": "Company B Trading (Sample)", "contact_person": "Contact B", "telephone": "0917 555 0202",
            "email": "sales@bbb-sample.example", "tin": "200-000-002-000", "payment_terms": "45D", "sticker_rate_c": 0, "term_discount_bp": 0,
            "term_discount_days": 0, "default_discount_bp": 0, "vat_registered": False, "vat_mode": "NONE", "address": "SAMPLE, Metro City"},
    "CCC": {"name": "CCC", "registered_name": "Company C Kids and Bags (Sample)", "contact_person": "Contact C", "telephone": "0917 555 0303",
            "email": "sales@ccc-sample.example", "tin": "300-000-003-000", "payment_terms": "60D", "sticker_rate_c": 0, "term_discount_bp": 200,
            "term_discount_days": 10, "default_discount_bp": 0, "vat_registered": False, "vat_mode": "NONE", "address": "SAMPLE, Metro City"},
}

# Simple item codes for now, A001 on. Item code, department, description, brand, size, color, unit, supplier, last cost, selling price (pesos).
PRODUCTS = [
    ("A001", 5, "Claw Hammer", "Brand A", "16 oz", "Black", "PC", "AAA", 1200, 2195),
    ("A002", 5, "Screwdriver Set", "Brand A", "6 pc", "Red", "PC", "AAA", 1100, 1995),
    ("A003", 6, "LED Bulb", "Brand A", "9 W", "Daylight", "PC", "AAA", 650, 1295),
    ("A004", 6, "Extension Cord", "Brand A", "3 m", "White", "PC", "AAA", 780, 1495),
    ("A005", 7, "Spiral Notebook", "Brand A", "80 leaves", "Assorted", "PC", "AAA", 480, 895),
    ("A006", 1, "Rice", "Brand A", "5 kg", "Jasmine", "PC", "AAA", 250, 499),
    ("A007", 1, "Instant Noodles", "Brand A", "60 g", "Chicken", "PC", "AAA", 120, 249),
    ("A008", 1, "Cooking Oil", "Brand A", "1 L", "Palm", "PC", "AAA", 340, 699),
    ("A009", 2, "Bottled Water", "Brand A", "500 ml", "Still", "PC", "AAA", 290, 599),
    ("A010", 2, "Orange Juice", "Brand A", "1 L", "Orange", "PC", "AAA", 160, 349),
    ("A011", 2, "Ground Coffee", "Brand A", "250 g", "Medium Roast", "PC", "AAA", 520, 1099),
    ("A012", 3, "Dishwashing Liquid", "Brand A", "500 ml", "Lemon", "PC", "AAA", 480, 999),
    ("A013", 3, "Laundry Detergent", "Brand A", "1 kg", "Original", "PC", "AAA", 420, 899),
    ("A014", 4, "Shampoo", "Brand A", "340 ml", "Herbal", "PC", "AAA", 460, 949),
    ("A015", 4, "Bath Soap", "Brand A", "135 g", "Aloe", "PC", "AAA", 330, 699),
    ("A016", 8, "Building Blocks", "Brand A", "50 pc", "Assorted", "PC", "AAA", 95, 199),
    ("A017", 8, "Baby Wipes", "Brand A", "80 sheets", "Unscented", "PC", "AAA", 110, 229),
    ("A018", 8, "Toy Cars, dozen", "Brand A", "Mini", "Assorted", "1 DOZEN", "AAA", 240, 49),
    ("A019", 9, "Umbrella", "Brand A", "Free", "Black", "PC", "AAA", 150, 349),
    ("A020", 9, "Reusable Shopping Bag", "Brand A", "Free", "Natural", "PC", "AAA", 85, 199),
    ("A021", 9, "AA Batteries, pack of 4", "Brand A", "Free", "Assorted", "4 IN 1", "AAA", 60, 129),
    ("A022", 9, "Gift Ribbon", "Brand A", "", "Red", "PC", "AAA", 18, 0),
    ("A023", 9, "Flashlight", "Brand A", "Free", "Black", "PC", "AAA", 310, 799),
    ("A024", 9, "Wall Clock", "Brand A", "Free", "Wood", "PC", "AAA", 140, 449),
    ("A025", 5, "Discontinued Hand Saw", "Brand A", "18 in", "Steel", "PC", "AAA", 300, 599),
    # Company B: groceries and household. Company C: toys, school and general goods.
    ("B001", 1, "Canned Tuna", "Brand B", "155 g", "Flakes in Oil", "PC", "BBB", 110, 229),
    ("B002", 1, "Corned Beef", "Brand B", "260 g", "Regular", "PC", "BBB", 240, 499),
    ("B003", 2, "Soda", "Brand B", "1.5 L", "Cola", "PC", "BBB", 105, 219),
    ("B004", 2, "Iced Tea", "Brand B", "1 L", "Lemon", "PC", "BBB", 280, 579),
    ("B005", 3, "Floor Cleaner", "Brand B", "1 L", "Pine", "PC", "BBB", 450, 949),
    ("B006", 4, "Toothpaste", "Brand B", "150 g", "Mint", "PC", "BBB", 440, 929),
    ("C001", 8, "Coloring Book", "Brand C", "A4", "Assorted", "PC", "CCC", 90, 189),
    ("C002", 8, "Stuffed Toy", "Brand C", "30 cm", "Brown", "PC", "CCC", 130, 279),
    ("C003", 7, "Ballpoint Pens, box", "Brand C", "0.7 mm", "Black", "PC", "CCC", 380, 799),
    ("C004", 9, "Storage Box", "Brand C", "Free", "Clear", "PC", "CCC", 260, 599),
    ("C005", 9, "Keychain", "Brand C", "Free", "Red", "PC", "CCC", 35, 89),
]


def is_sample(conn) -> bool:
    return get_setting(conn, FLAG) == "1"


@contextmanager
def _clock():
    """Back-dates what build() does, so reports have months and years to show. Only ever runs during build()."""
    saved = db.now, order_forms.now
    try:
        yield
    finally:
        db.now, order_forms.now = saved


def _at(y: int, m: int, d: int, hour: int = 10) -> None:
    moment = datetime(y, m, d, hour)
    db.now = order_forms.now = lambda: moment


class _Seed:
    def __init__(self, conn):
        self.conn = conn
        self.sup, self.prod = {}, {}
        self.admin = self.buyer = self.buyer2 = self.approver = None
        self.for_claims = []
        self.for_clean = []
        self.for_deductions = []
        self.for_passes = []
        self.people = {}

    def line(self, code: str, alloc: dict, cost: float | None = None, **over) -> dict:
        p = self.prod[code]
        return {"product_id": p["id"], "department_id": p["department_id"], "item_code": code, "description": p["description"], "brand": p["brand"],
                "size": p["size"], "color": p["color"], "uom": p["uom"], "selling_c": p["selling_c"],
                "unit_cost_c": p["last_cost_c"] if cost is None else round(cost * 100), "allocations": alloc, **over}

    def draft(self, day, supplier: str, lines: list, by=None, **header) -> int:
        _at(*day)
        return order_forms.create_draft(self.conn, by or self.buyer, {"supplier_id": self.sup[supplier], **header}, lines)

    def approved(self, day, supplier: str, lines: list, by=None, remarks="Reviewed against the supplier's quote.", **header) -> int:
        of_id = self.draft(day, supplier, lines, by, **header)
        if order_forms.submit(self.conn, by or self.buyer, of_id) == orders.PENDING:
            order_forms.approve(self.conn, self.approver, of_id, remarks)
        return of_id

    def generated(self, day, supplier: str, lines: list, by=None, **header) -> list[dict]:
        of_id = self.approved(day, supplier, lines, by, **header)
        return order_forms.generate_pos(self.conn, by or self.buyer, of_id)

    def closed(self, day, supplier: str, lines: list, close_day, reason: str = "", short: int = 0, **header) -> list[dict]:
        """A finished order: printed, then counted at every branch by its two receivers, because nothing is paid before that.
        With short, the first branch gets that many pieces fewer of its first product, and the POs are closed with the reason, since no more is coming."""
        pos = self.generated(day, supplier, lines, **header)
        for po in pos:
            orders.issue(self.conn, self.buyer, po["id"])
        _at(*close_day)
        for n, po in enumerate(pos):
            items = self.conn.execute("SELECT id, qty_m, pieces_per_unit FROM purchase_order_items WHERE po_id = ? ORDER BY line_no", (po["id"],)).fetchall()
            pieces = {i["id"]: i["qty_m"] * i["pieces_per_unit"] // 1000 for i in items}
            if short and n == 0:
                pieces[items[0]["id"]] -= short
            for who in self.people[po["branch_code"]]:
                receiving.save_count(self.conn, who, po["id"], {k: str(v) for k, v in pieces.items()})
        if reason:
            for po in pos:
                orders.close(self.conn, self.buyer, po["id"], reason)
        return pos

    def amend_po(self, po_id: int, changes: dict, reason: str, **header) -> dict:
        """changes is {item_code: new quantity} for the rows that change."""
        po = orders.get_po(self.conn, po_id)
        items = [{k: i[k] for k in orders.ITEM_FIELDS} | {"id": i["id"]} for i in po["items"]]
        for item in items:
            if item["item_code"] in changes:
                item["qty_m"] = changes[item["item_code"]] * 1000
        return orders.amend(self.conn, self.buyer, po_id, header, items, reason)


def _masters(s: _Seed) -> None:
    conn = s.conn
    _at(2025, 8, 1, 9)
    s.admin = auth.create_first_admin(conn, "admin", "Maria Santos", _BUILDING)
    s.approver = auth.create_user(conn, s.admin, "approver", "Ramon Villanueva", _BUILDING, ["APPROVER"], must_change=False, title="Operations Manager")
    s.buyer = auth.create_user(conn, s.admin, "purchaser", "Bea Reyes", _BUILDING, ["BUYER"], must_change=False, title="Purchasing Head")
    s.buyer2 = auth.create_user(conn, s.admin, "purchaser2", "Carlo Mendoza", _BUILDING, ["BUYER"], must_change=False, title="Purchasing Staff")
    auth.create_user(conn, s.admin, "viewer", "Vicky Aquino", _BUILDING, ["VIEWER"], must_change=False)
    s.warehouse = auth.create_user(conn, s.admin, "warehouse", "Nestor Garcia", _BUILDING, ["WAREHOUSE"], must_change=False, title="Warehouse Head")
    # The practice data lets nor1 and nor2 count without the warehouse shipping first, so every situation can be tried. Real data waits for shipping.
    conn.execute("INSERT OR REPLACE INTO system_settings(key, value, updated_at) VALUES ('receiving.after_shipping', '0', datetime('now'))")
    auth.create_user(conn, s.admin, "warehouse2", "Rudy Manalo", _BUILDING, ["WAREHOUSE"], must_change=False, title="Warehouse Receiver")
    s.accounting = auth.create_user(conn, s.admin, "accounting", "Lorna Tan", _BUILDING, ["ACCOUNTING"], must_change=False, title="Accounting Staff")
    former = auth.create_user(conn, s.admin, "former", "Dan Flores", _BUILDING, ["BUYER"], must_change=False)
    auth.update_user(conn, s.admin, former.id, former.full_name, ["BUYER"], False)
    for code, login, first, second in ((NORTH, "nor", "Liza Ramos", "Noel Garcia"), (SOUTH, "sou", "Tess Cruz", "Jun Bautista"),
                                       (EAST, "eas", "Mina Lopez", "Rey Santos"), (WEST, "wes", "Gina Torres", "Paolo Diaz")):
        s.people[code] = [auth.create_user(conn, s.admin, f"{login}{n}", name, _BUILDING, ["RECEIVER"], must_change=False, branch_code=code) for n, name in enumerate((first, second), 1)]
    db.set_settings(conn, s.admin, {
        FLAG: "1",
        "company.name": "ATLAS PO ERP (SAMPLE DATA)",
        "company.address": "SAMPLE ONLY, not a real address",
        "company.telephone": "(02) 8555 0100",
        "company.email": "purchasing@sample.example",
        "company.tin": "000-000-000-000",
        "approval.approver_user_ids": str(s.approver.id),
    })
    addresses = {NORTH: "SAMPLE, 1 North Road", SOUTH: "SAMPLE, 2 South Road", EAST: "SAMPLE, 3 East Road",
                 WEST: "SAMPLE, 4 West Road"}
    for code, address in addresses.items():
        b = conn.execute("SELECT * FROM branches WHERE code=?", (code,)).fetchone()
        masters.save_branch(conn, s.admin, {"short_code": b["short_code"], "name": b["name"], "kind": b["kind"], "address": address, "active": True}, code)
    # An invented branch trades until the end of 2025, so an inactive branch has history to show.
    masters.save_branch(conn, s.admin, {"code": CLOSED_BRANCH, "short_code": "OLD", "name": "Closed Branch (Sample)", "address": "SAMPLE, Metro City", "active": True})
    s.people[CLOSED_BRANCH] = [auth.create_user(conn, s.admin, f"old{n}", name, _BUILDING, ["RECEIVER"], must_change=False, branch_code=CLOSED_BRANCH)
                               for n, name in enumerate(("Lito Cruz", "Remy Santos"), 1)]
    for code, data in SUPPLIERS.items():
        s.sup[code] = masters.save_supplier(conn, s.admin, {"code": code, **data})
    for code, dept, description, brand, size, color, uom, supplier, cost, selling in PRODUCTS:
        pid = masters.save_product(conn, s.buyer, {
            "item_code": code, "department_id": dept, "description": description, "brand": brand, "size": size, "color": color, "uom": uom,
            "supplier_id": s.sup[supplier], "supplier_item_code": f"{supplier}-{code}", "last_cost_c": cost * 100, "selling_c": selling * 100})
        s.prod[code] = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()


def _history(s: _Seed) -> None:
    """Finished business, 2025 to mid 2026: closed POs in every department, with costs that move over time."""
    four = {NORTH: 60, SOUTH: 30, EAST: 24, WEST: 18}
    s.closed((2025, 9, 8), "AAA", [s.line("A006", {NORTH: 40, SOUTH: 20, CLOSED_BRANCH: 30}, 235), s.line("A007", {NORTH: 60, CLOSED_BRANCH: 40}, 110)], (2025, 10, 20))
    s.closed((2025, 10, 6), "AAA", [s.line("A001", {NORTH: 24, EAST: 12, CLOSED_BRANCH: 12}, 1120, disc1_bp=500)], (2025, 11, 14))
    s.closed((2025, 11, 10), "AAA", [s.line("A016", four, 88), s.line("A017", four, 105)], (2025, 12, 12))
    s.closed((2025, 12, 1), "AAA", [s.line("A012", {NORTH: 80, SOUTH: 40, EAST: 30, WEST: 30, CLOSED_BRANCH: 20}, 450)], (2026, 1, 15))
    _at(2025, 12, 31, 17)
    b = s.conn.execute("SELECT * FROM branches WHERE code=?", (CLOSED_BRANCH,)).fetchone()
    masters.save_branch(s.conn, s.admin, {"short_code": b["short_code"], "name": b["name"], "kind": b["kind"], "address": b["address"], "active": False}, CLOSED_BRANCH)
    for who in s.people[CLOSED_BRANCH]:
        auth.update_user(s.conn, s.admin, who.id, who.full_name, ["RECEIVER"], False, CLOSED_BRANCH)

    s.closed((2026, 1, 12), "AAA", [s.line("A009", four, 275), s.line("A010", four, 150)], (2026, 2, 18))
    s.closed((2026, 2, 9), "AAA", [s.line("A003", four, 620, disc1_bp=500), s.line("A005", {NORTH: 30, EAST: 24, WEST: 24}, 460)], (2026, 3, 20))
    s.closed((2026, 3, 16), "AAA", [s.line("A019", {NORTH: 48, SOUTH: 24}, 140), s.line("A021", {NORTH: 36, SOUTH: 12, WEST: 12}, 55)], (2026, 4, 2))
    s.closed((2026, 4, 13), "AAA", [s.line("A014", four, 445), s.line("A013", four, 410)], (2026, 5, 22),
             reason="Short delivery of 6 pieces accepted. Balance will not be delivered.", short=6, overall_discount_type="PERCENT", overall_discount_value=300)
    s.closed((2026, 5, 11), "AAA", [s.line("A016", four, 95), s.line("A018", {NORTH: 10, SOUTH: 5, EAST: 5, WEST: 5})], (2026, 6, 12))
    s.closed((2026, 6, 8), "AAA", [s.line("A006", {NORTH: 90, SOUTH: 50, EAST: 40, WEST: 30}), s.line("A007", four, 120)], (2026, 7, 10))


def _attachment(s: _Seed, po_id: int) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        quote = Path(tmp) / "sample-supplier-quote.txt"
        quote.write_text("SAMPLE supplier quotation. Invented for practice.\n", encoding="utf-8")
        orders.add_attachment(s.conn, s.buyer, po_id, quote)


def _open_work(s: _Seed) -> None:
    """Orders in every state, ending at 2 November 2026."""
    conn, four = s.conn, {NORTH: 60, SOUTH: 30, EAST: 24, WEST: 18}
    delivery = "Deliver Mon-Fri, 8 AM to 4 PM. Present this PO at receiving."

    # One PO per branch, delivered with damage in July. _claim_stub_stages takes each to a different end.
    s.for_claims = [po["id"] for po in s.generated((2026, 6, 22), "AAA", [s.line("A019", four), s.line("A020", four)], expected_delivery_date="2026-07-06", remarks="Damaged in transit.")]

    # Everything arrives exactly as ordered, so it goes straight to its check.
    s.for_clean = [po["id"] for po in s.generated((2026, 6, 29), "AAA", [s.line("A010", four), s.line("A007", four)], expected_delivery_date="2026-10-20")]

    _at(2026, 6, 30, 8)
    masters.save_supplier(conn, s.admin, {"code": "AAA", **SUPPLIERS["AAA"], "sticker_rate_c": 50}, s.sup["AAA"], confirm_duplicate=True)
    # One product short at a branch and damaged at another, now that the supplier's goods come without stickers, so every column on Checks to make has something in it.
    s.for_deductions = [po["id"] for po in s.generated((2026, 6, 30), "AAA", [s.line("A004", four), s.line("A005", four)], expected_delivery_date="2026-10-20")]

    # Extra pieces at every branch, so every stage of an item pass has something to show.
    s.for_passes = [po["id"] for po in s.generated((2026, 6, 30), "AAA", [s.line("A016", four), s.line("A017", four)], expected_delivery_date="2026-07-06", remarks="More pieces came than were ordered.")]

    # Issued and overdue: fills Outstanding POs, with an external PO number and an attachment.
    pos = s.generated((2026, 7, 13), "AAA", [
        s.line("A001", {NORTH: 120, SOUTH: 50, EAST: 40, WEST: 40}, disc1_bp=500),
        s.line("A002", {NORTH: 60, SOUTH: 30, EAST: 20, WEST: 10}, disc1_bp=500),
        s.line("A003", {NORTH: 36, SOUTH: 24, EAST: 24, WEST: 12}),
        s.line("A005", {NORTH: 24, EAST: 12, WEST: 12}),
    ], expected_delivery_date="2026-08-31", delivery_instructions=delivery, remarks="Please deliver complete sizes.",
        warehouse_notes="Check every carton against the branch quantities.", internal_notes="Negotiated 5% on the hardware lines.")
    for po in pos:
        orders.issue(conn, s.buyer, po["id"])
    orders.set_legacy_number(conn, s.buyer, pos[0]["id"], "118204")
    orders.set_legacy_number(conn, s.buyer, pos[1]["id"], "118205")
    _attachment(s, pos[0]["id"])
    orders.issued_pdf(conn, s.buyer, pos[0]["id"])

    # From August the supplier stickers its own goods again, so later orders come stickered and their DRs are emailed.
    _at(2026, 8, 1, 8)
    masters.save_supplier(conn, s.admin, {"code": "AAA", **SUPPLIERS["AAA"]}, s.sup["AAA"], confirm_duplicate=True)
    # An issued PO amended and issued again, so it has two preserved versions. South's PO in the same OF is cancelled.
    pos = s.generated((2026, 8, 10), "AAA", [s.line("A014", four), s.line("A015", {NORTH: 36, SOUTH: 18, EAST: 12})],
                      expected_delivery_date="2026-11-20", delivery_arrangement="PARTIAL", other_charges_c=450000, other_charges_note="Freight")
    for po in pos:
        orders.issue(conn, s.buyer, po["id"])
    _at(2026, 8, 24)
    s.amend_po(pos[0]["id"], {"A014": 48}, "North reduced after August sell-through.")
    orders.issue(conn, s.buyer, pos[0]["id"])
    _at(2026, 8, 28)
    orders.cancel(conn, s.buyer, pos[1]["id"], "South stockroom under renovation until January.")

    # An amendment that was rejected, and one still waiting in Approvals.
    pos = s.generated((2026, 9, 7), "AAA", [s.line("A011", {NORTH: 40, SOUTH: 20, EAST: 20, WEST: 20}), s.line("A009", four)],
                      expected_delivery_date="2026-11-28", delivery_instructions=delivery)
    for po in pos:
        orders.issue(conn, s.buyer, po["id"])
    _at(2026, 9, 21)
    rejected = s.amend_po(pos[1]["id"], {"A011": 35}, "South asked for more dresses.")
    orders.decide_amendment(conn, s.approver, rejected["amendment_id"], False, "South's September sales don't support this.")
    _at(2026, 10, 26)
    s.amend_po(pos[2]["id"], {"A011": 30}, "East sold out early. Ten more dresses.")

    # A whole OF cancelled after its POs were made: numbers stay used and visible.
    pos = s.generated((2026, 9, 14), "AAA", [
        s.line("A023", {NORTH: 100, SOUTH: 50, EAST: 50, WEST: 50}, fx_currency="USD", fx_rate_m=57500, fx_cost_m=5400),
        s.line("A024", {NORTH: 80, EAST: 40}, fx_currency="USD", fx_rate_m=57500, fx_cost_m=2430),
    ], expected_delivery_date="2026-12-15", remarks="Imported. Shipment by sea.")
    _at(2026, 9, 30)
    order_forms.cancel(conn, s.buyer, orders.get_po(conn, pos[0]["id"])["of_id"], "Supplier could not meet the shipment date.")

    # A draft cancelled before it was ever numbered.
    draft = s.draft((2026, 10, 1), "AAA", [s.line("A020", {NORTH: 50})])
    order_forms.cancel(conn, s.buyer, draft, "Duplicate of another order.")

    # Printed, so every branch PO went out together, the way Print does it.
    pos = s.generated((2026, 10, 5), "AAA", [s.line("A016", four), s.line("A017", four), s.line("A018", {NORTH: 10, SOUTH: 5})],
                      by=s.buyer2, expected_delivery_date="2026-11-30", delivery_instructions=delivery)
    for po in pos:
        orders.issue(conn, s.buyer2, po["id"])

    # Exactly the limit: approved without the approver. One peso more waits for them below. Its PO is made and waits on Print.
    s.generated((2026, 10, 12), "AAA", [s.line("A008", {NORTH: 100}, 1000)], remarks="", expected_delivery_date="2026-12-01")

    # Approved, POs not made yet: one amended down (revision 1), one with an increase waiting for approval.
    amended = s.approved((2026, 10, 15), "AAA", [s.line("A012", four), s.line("A013", {NORTH: 30, SOUTH: 15})], expected_delivery_date="2026-12-10")
    of = order_forms.get_of(conn, amended)
    items = [{k: i[k] for k in orders.LINE_FIELDS} | {"allocations_m": i["allocations_m"]} for i in of["items"]]
    items[1]["allocations_m"] = {NORTH: 24000, SOUTH: 15000}
    _at(2026, 10, 19)
    order_forms.amend(conn, s.buyer, amended, {}, items, "Supplier short on North's chinos.")
    waiting = s.approved((2026, 10, 20), "AAA", [s.line("A004", four, disc1_bp=500)], by=s.buyer2, expected_delivery_date="2026-12-12")
    of = order_forms.get_of(conn, waiting)
    items = [{k: i[k] for k in orders.LINE_FIELDS} | {"allocations_m": i["allocations_m"]} for i in of["items"]]
    items[0]["allocations_m"] = {**items[0]["allocations_m"], EAST: 36000}
    _at(2026, 10, 27)
    order_forms.amend(conn, s.buyer2, waiting, {}, items, "East wants 12 more heels for Christmas.")

    # Waiting for the approver: one peso over the limit, and a large order.
    one_over = s.draft((2026, 10, 28), "AAA", [s.line("A008", {NORTH: 100}, 1000.01)], expected_delivery_date="2026-12-05", remarks="One peso over the limit.")
    order_forms.submit(conn, s.buyer, one_over)
    big = s.draft((2026, 10, 29), "AAA", [
        s.line("A001", {NORTH: 150, SOUTH: 70, EAST: 60, WEST: 50}, 1250, disc1_bp=500),
        s.line("A004", {NORTH: 80, SOUTH: 40, EAST: 30, WEST: 30}, 800),
    ], by=s.buyer2, expected_delivery_date="2026-12-18", remarks="Christmas order.", internal_notes="Check the cost increase with the supplier.")
    order_forms.submit(conn, s.buyer2, big)

    # Rejected and back in Draft with the reason showing.
    rejected_of = s.draft((2026, 10, 30), "AAA", [s.line("A012", {NORTH: 200, SOUTH: 100, EAST: 80, WEST: 80}, 530)])
    order_forms.submit(conn, s.buyer, rejected_of)
    _at(2026, 10, 31)
    order_forms.reject(conn, s.approver, rejected_of, "Cost is 10% above the last order. Ask for a better price.")

    # Drafts in progress: one typed product with decimals in metres, one only partly allocated.
    s.draft((2026, 11, 2), "AAA", [
        s.line("A022", {NORTH: 12, WEST: 7}),
        {"department_id": 9, "description": "Gift Boxes (new, not in catalog)", "unit_cost_c": 3500, "allocations": {NORTH: 100, SOUTH: 50}},
    ], remarks="Christmas packaging.")
    s.draft((2026, 11, 2), "AAA", [s.line("A017", {NORTH: 24}), s.line("A016", {})], by=s.buyer2)


def build(conn) -> None:
    """Fill an empty database. Refuses one that already has users, so it can't mix sample orders into real records."""
    if auth.has_users(conn):
        raise POError("This database already has users, so sample data won't be added to it.")
    with _clock():
        s = _Seed(conn)
        _masters(s)
        _history(s)
        _open_work(s)
        _at(2026, 11, 2, 12)
        masters.save_product(conn, s.admin, {**dict(s.prod["A025"]), "active": 0}, s.prod["A025"]["id"])
        # The owner's master account does everything, including approving what it prepared.
        owner_user = auth.create_user(conn, s.admin, "owner", "Sam Yu", _BUILDING, ["ADMIN", "APPROVER"], must_change=False, title="Owner")
        db.set_settings(conn, s.admin, {"approval.approver_user_ids": f"{owner_user.id},{s.approver.id}", "approval.allow_self_approval": "1"})
        _receiving(s)
        _other_suppliers(s)
        _easy_logins(conn)
        backup.manual_backup(conn, s.admin)


def _other_suppliers(s: _Seed) -> None:
    """Company B and Company C: finished orders over the year, and a little open work, so every list shows more than one supplier.
    Added last, so AAA's situations above stay exactly as they were."""
    four = {NORTH: 40, SOUTH: 20, EAST: 20, WEST: 20}
    two = {NORTH: 30, SOUTH: 15}
    # Finished orders, all counted by both receivers at every branch.
    s.closed((2026, 1, 19), "BBB", [s.line("B001", four), s.line("B003", four)], (2026, 2, 20))
    s.closed((2026, 3, 2), "BBB", [s.line("B005", two), s.line("B006", two)], (2026, 4, 10))
    s.closed((2026, 5, 4), "BBB", [s.line("B002", four)], (2026, 6, 5))
    s.closed((2026, 7, 6), "BBB", [s.line("B004", four), s.line("B003", two, 100)], (2026, 8, 7))
    s.closed((2026, 2, 16), "CCC", [s.line("C001", four), s.line("C002", four)], (2026, 3, 18))
    s.closed((2026, 4, 20), "CCC", [s.line("C003", two)], (2026, 5, 25))
    s.closed((2026, 8, 17), "CCC", [s.line("C004", two), s.line("C005", {NORTH: 100, SOUTH: 50})], (2026, 9, 18))
    # Open work: BBB printed and waiting at the branches, CCC approved and waiting for its POs, and one CCC draft not sent yet.
    for po in s.generated((2026, 10, 26), "BBB", [s.line("B001", four), s.line("B002", two)], expected_delivery_date="2026-11-16"):
        orders.issue(s.conn, s.buyer, po["id"])
    s.approved((2026, 10, 27), "CCC", [s.line("C001", four), s.line("C003", two)], expected_delivery_date="2026-11-30")
    s.draft((2026, 10, 30), "CCC", [s.line("C004", {NORTH: 20})], remarks="Still checking the color with Company C.")


def _easy_logins(conn) -> None:
    """Written directly because 123 is too short for the real rule, which only applies to logins made in the program."""
    conn.execute("UPDATE users SET password_hash = ?", (auth.hash_password(PASSWORD),))
    conn.commit()


def _receiving(s) -> None:
    """Two receivers per branch and a delivery in every situation the home office has to read. A few issued POs stay uncounted, to practise on."""
    conn, people = s.conn, s.people

    def ordered(po) -> dict:
        lines = conn.execute("SELECT id, qty_m, pieces_per_unit FROM purchase_order_items WHERE po_id = ? ORDER BY line_no", (po["id"],)).fetchall()
        return {i["id"]: i["qty_m"] * i["pieces_per_unit"] // 1000 for i in lines}

    def counted(po, person: int, change: int = 0, **kw) -> dict:
        """One receiver's count: everything as ordered, except the first product, which is off by change."""
        pieces = ordered(po)
        pieces[next(iter(pieces))] += change
        return receiving.save_count(conn, people[po["branch_code"]][person], po["id"], pieces, **kw)

    def both(po, change: int = 0, **kw) -> dict:
        counted(po, 0, change, **kw)
        return counted(po, 1, change, **kw)

    issued = conn.execute("SELECT id, branch_code FROM purchase_orders WHERE status = 'ISSUED' ORDER BY id").fetchall()
    short, complete, over, split, nothing, stuck, *rest = issued
    # The next two share a branch, so the same receiver's mistakes show as a pattern on the By receiver tab.
    wrong = rest.pop(0)
    wrong_again = rest.pop(next(i for i, po in enumerate(rest) if po["branch_code"] == wrong["branch_code"]))
    # At North, so nor1 and nor2 alone can try everything: one PO for a second count and one still to count.
    waiting = next(po for po in rest if po["branch_code"] == NORTH)

    _at(2026, 10, 26, 9)
    both(short, -2)
    # Everything arrived, with a few pieces damaged. The claim stub is old enough that the supplier's 90 days have passed.
    _at(2026, 7, 20, 14)
    first_line = next(iter(ordered(complete)))
    both(complete, 0, remarks={first_line: "Crushed carton"}, damaged={first_line: 3})
    _at(2026, 7, 21, 10)
    overdue = claim_stubs.issue(conn, s.buyer, complete["id"], {(first_line, debit_memos.DAMAGED): 3}, "Crushed carton.")
    _at(2026, 7, 25, 10)
    claim_stubs.arrived(conn, s.warehouse, overdue["id"], claim_stubs.as_on_paper(conn, overdue["id"]))
    # Agreed as over, then counted again and agreed lower: the first two counts stay on record as replaced.
    _at(2026, 10, 27, 10)
    accepted = both(over, 5)
    _at(2026, 10, 27, 15)
    receiving.reopen(conn, s.approver, over["id"], "Branch found 2 cartons were for another store. Count it again.")
    _at(2026, 10, 27, 16)
    counted(over, 0, 3, recount_of=accepted["count_id"])
    counted(over, 1, 3)
    # Half now and the rest later, as two deliveries.
    _at(2026, 10, 27, 15)
    half = {k: v // 2 for k, v in ordered(split).items()}
    for person in (0, 1):
        receiving.save_count(conn, people[split["branch_code"]][person], split["id"], half)
    # Its second delivery has its own DR.
    delivery_receipts.another_delivery(conn, s.buyer, split["id"])
    _at(2026, 10, 30, 11)
    for person in (0, 1):
        receiving.save_count(conn, people[split["branch_code"]][person], split["id"], {k: v - half[k] for k, v in ordered(split).items()})
    # One product never arrived.
    _at(2026, 10, 28, 9)
    pieces = ordered(nothing)
    last = list(pieces)[-1]
    pieces[last] = 0
    for person in (0, 1):
        receiving.save_count(conn, people[nothing["branch_code"]][person], nothing["id"], pieces, {last: "Not in the delivery"}, damaged={next(iter(pieces)): 2})
    # The first receiver is wrong twice. Each time the two disagree, and a recount settles it against them.
    for po, day, extra in ((wrong, 28, 10), (wrong_again, 29, 6)):
        _at(2026, 10, day, 10)
        counted(po, 0, extra)
        counted(po, 1)
        _at(2026, 10, day, 16)
        both(po)
    # Two rounds that still disagree, so counting stops until the home office checks.
    _at(2026, 10, 31, 10)
    counted(stuck, 0, -4)
    counted(stuck, 1, -1)
    _at(2026, 11, 2, 9)
    counted(stuck, 0, -4)
    counted(stuck, 1, -2)
    # A first count waiting for its second person, who'll see a product that wasn't on the PO but not how many.
    _at(2026, 11, 2, 11)
    counted(waiting, 0, extras=[{"item_code": "XTRA 01", "description": "Unordered Paper Towels", "size": "2 rolls", "color": "White", "pieces": "5", "remarks": "Came in the same carton"}])

    # A shortage settled with a debit memo, so it shows on Completed delivery. Other short deliveries are left to practise on.
    _at(2026, 10, 29, 11)
    lines = debit_memos.outstanding(conn, s.buyer, short["id"])
    debit_memos.issue(conn, s.buyer, short["id"], {(l["po_item_id"], l["reason"]): l["pieces_m"] // 1000 for l in lines}, "Short on delivery. Counted by two receivers.")
    # Two days later the missing pieces come after all, as a second delivery. The memo now says so, for management to cancel.
    _at(2026, 10, 31, 10)
    delivery_receipts.another_delivery(conn, s.buyer, short["id"])
    late = {k: 0 for k in ordered(short)}
    late[next(iter(late))] = 2
    for person in (0, 1):
        receiving.save_count(conn, people[short["branch_code"]][person], short["id"], late)
    _at(2026, 10, 29, 11)
    # A memo cancelled the next day, which frees its pieces, so this shortage is still there to practise on.
    lines = debit_memos.outstanding(conn, s.buyer, nothing["id"])
    memo = debit_memos.issue(conn, s.buyer, nothing["id"], {(l["po_item_id"], l["reason"]): l["pieces_m"] // 1000 for l in lines}, "Product never arrived.")
    _at(2026, 10, 30, 9)
    debit_memos.cancel(conn, s.approver, memo["id"], "Supplier says the missing product is on its way. Wait for it.")

    _claim_stub_stages(s, both, ordered)

    marks = ",".join("?" * len(s.for_clean))
    for po in conn.execute(f"SELECT id, branch_code FROM purchase_orders WHERE id IN ({marks})", s.for_clean).fetchall():
        _at(2026, 10, 20)
        orders.issue(conn, s.buyer, po["id"])
        _at(2026, 10, 26)
        both(po)

    marks = ",".join("?" * len(s.for_deductions))
    short_po, damaged_po, *exact = conn.execute(f"SELECT id, branch_code FROM purchase_orders WHERE id IN ({marks}) ORDER BY branch_code", s.for_deductions).fetchall()
    for po in (short_po, damaged_po, *exact):
        _at(2026, 10, 20)
        orders.issue(conn, s.buyer, po["id"])
    _at(2026, 10, 26)
    both(short_po, -4)
    damaged_line = next(iter(ordered(damaged_po)))
    both(damaged_po, 0, remarks={damaged_line: "Scratched"}, damaged={damaged_line: 2})
    for po in exact:
        both(po)
    _at(2026, 10, 28)
    debit_memos.issue(conn, s.buyer, short_po["id"], {(l["po_item_id"], l["reason"]): l["pieces_m"] // 1000 for l in debit_memos.outstanding(conn, s.buyer, short_po["id"])}, "Short on delivery. Counted by two receivers.")
    claim_stubs.issue(conn, s.buyer, damaged_po["id"], {(damaged_line, debit_memos.DAMAGED): 2}, "Scratched.")

    _item_pass_stages(s, both, people, over)

    # A South receiver found damage after the count, when unpacking, and reported it. It waits for Accounting or management.
    _at(2026, 11, 1, 10)
    south = conn.execute(f"SELECT po_number FROM purchase_orders WHERE id IN ({','.join('?' * len(s.for_clean))}) AND branch_code = ?", [*s.for_clean, SOUTH]).fetchone()[0]
    lines = claim_stubs.report_lines(conn, people[SOUTH][0], south)
    claim_stubs.send_report(conn, people[SOUTH][0], south, {lines[0]["po_item_id"]: "2"}, "Torn seams found when unpacking for display.")

    # One order's DRs have been emailed, and the rest still wait.
    _at(2026, 11, 2, 10)
    delivery_receipts.mark_emailed(conn, s.buyer, [d["id"] for d in delivery_receipts.waiting_to_email(conn)[0]["receipts"]])

    _payment_stages(s)
    _warehouse_stages(s)

    # A few prices changed by hand, one above and one below the automatic price, and one back where it started.
    lines = selling.list_items(conn)
    for line, change in ((lines[0], 2000), (lines[2], -1000), (lines[4], 5000)):
        selling.set_price(conn, s.admin, line["of_id"], line["key"], line["auto_c"] + change)
    selling.set_price(conn, s.admin, lines[4]["of_id"], lines[4]["key"], None)
    _at(2026, 11, 2, 12)


def _claim_stub_stages(s, both, ordered) -> None:
    """A stub at each stage that isn't already shown: forfeited, collected, cancelled, and still coming back."""
    conn = s.conn
    marks = ",".join("?" * len(s.for_claims))
    pos = conn.execute(f"SELECT id, branch_code FROM purchase_orders WHERE id IN ({marks}) ORDER BY branch_code", s.for_claims).fetchall()
    stubs = []
    for po, pieces, remark in zip(pos, (6, 4, 2, 5), ("Scuffed and bent.", "Buckles broken.", "Torn stitching.", "Water damage.")):
        _at(2026, 7, 1)
        orders.issue(conn, s.buyer, po["id"])
        line = next(iter(ordered(po)))
        _at(2026, 7, 6)
        both(po, 0, remarks={line: remark}, damaged={line: pieces})
        _at(2026, 7, 22)
        stubs.append(claim_stubs.issue(conn, s.buyer, po["id"], {(line, debit_memos.DAMAGED): pieces}, remark)["id"])
    forfeited, collected, cancelled, _coming_back = stubs
    _at(2026, 7, 23)
    claim_stubs.cancel(conn, s.approver, cancelled, "Stub made on the wrong product line. Made again.")
    # Made again, so the whole order finishes with deductions and needs a Final OF.
    east = pos[2]
    line = next(iter(ordered(east)))
    claim_stubs.issue(conn, s.buyer, east["id"], {(line, debit_memos.DAMAGED): 2}, "Torn stitching.")
    # Paid in July with the claim stubs taken off. The supplier has the check, so the goods can be collected from then.
    of_id = conn.execute("SELECT of_id FROM purchase_orders WHERE id = ?", (pos[0]["id"],)).fetchone()[0]
    _at(2026, 7, 24)
    payments.drs_match(conn, s.accounting, of_id)
    expected = payments._expected(conn, [payments.order_row(conn, of_id)])[2]
    check = payments.record_check(conn, s.accounting, [of_id], "880050", "2026-07-25", f"{expected / 100:.2f}")
    _at(2026, 7, 26)
    payments.claim_check(conn, s.accounting, check, "2026-07-26", "Rico Tan")
    _at(2026, 7, 27)
    claim_stubs.arrived(conn, s.warehouse, forfeited, claim_stubs.as_on_paper(conn, forfeited))
    claim_stubs.arrived(conn, s.warehouse, collected, claim_stubs.as_on_paper(conn, collected))
    _at(2026, 8, 3)
    claim_stubs.collected(conn, s.buyer, collected, "Rico Tan")
    _at(2026, 11, 1)
    claim_stubs.forfeit(conn, s.buyer, forfeited)


def _item_pass_stages(s, both, people, over) -> None:
    """A pass at each stage. North's was spoiled and cancelled, so nor1 and nor2 can print the new one themselves."""
    conn = s.conn
    marks = ",".join("?" * len(s.for_passes))
    pos = conn.execute(f"SELECT id, branch_code FROM purchase_orders WHERE id IN ({marks}) ORDER BY branch_code", s.for_passes).fetchall()
    passes = []
    for po, extra in zip(pos, (3, 2, 4, 5)):
        _at(2026, 7, 1)
        orders.issue(conn, s.buyer, po["id"])
        _at(2026, 7, 6)
        both(po, extra)
        _at(2026, 7, 7)
        passes.append(item_passes.issue(conn, people[po["branch_code"]][0], po["id"])["id"])
    spoiled, _on_its_way, picked_up, forfeited = passes
    _at(2026, 7, 8)
    item_passes.cancel(conn, s.approver, spoiled, "Printer jammed and the pass was torn.")
    _at(2026, 7, 15)
    item_passes.arrived(conn, s.warehouse, picked_up, item_passes.as_on_paper(conn, picked_up))
    item_passes.arrived(conn, s.warehouse, forfeited, item_passes.as_on_paper(conn, forfeited))
    _at(2026, 8, 3)
    item_passes.collected(conn, s.buyer, picked_up, "Ana Lim")
    _at(2026, 10, 28)
    late_pass = item_passes.issue(conn, people[over["branch_code"]][0], over["id"])["id"]
    item_passes.arrived(conn, s.warehouse, late_pass, item_passes.as_on_paper(conn, late_pass))
    _at(2026, 11, 1)
    item_passes.forfeit(conn, s.buyer, forfeited)


def _payment_stages(s) -> None:
    """Finished orders at every stage: DRs to compare, a total that differs, a difference waiting for its sign-off, checks to make on more than one Saturday,
    and checks recorded at every stage, one cancelled and made again and one waiting for a sign-off. There's one supplier, so each Saturday is one check."""
    conn = s.conn
    of_of = lambda po_id: conn.execute("SELECT of_id FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()[0]
    deductions, passes, clean = of_of(s.for_deductions[0]), of_of(s.for_passes[0]), of_of(s.for_clean[0])
    # The order with every deduction is compared and stays on Checks to make, so its columns can be read. The clean one went straight there.
    _at(2026, 11, 2, 11)
    payments.drs_match(conn, s.accounting, deductions)
    # The short delivery that was closed gets its debit memo, as every shortage does before it's paid.
    for row in payments.orders_in_payment(conn):
        if row["stage"] == payments.DM_FIRST:
            for p in row["pos"]:
                if p["unclaimed_c"]:
                    _at(2026, 5, 23)
                    lines = debit_memos.outstanding(conn, s.buyer, p["po_id"])
                    debit_memos.issue(conn, s.buyer, p["po_id"], {(l["po_item_id"], l["reason"]): l["pieces_m"] // 1000 for l in lines}, "Short delivery, closed.")
    _at(2026, 11, 2, 11)
    everything = [r for r in payments.orders_in_payment(conn) if r["id"] not in (deductions, passes, clean)]
    # The short delivery that was closed still waits for its DRs to be compared.
    untyped = next(r for r in everything if r["stage"] == payments.ENTER_DRS)
    history = sorted((r for r in everything if r["stage"] == payments.TO_PAY and r["id"] != untyped["id"]), key=lambda r: r["id"])
    differs, signoff = history.pop(), history.pop()

    def enter(row, day, off_by=0):
        by_po = {p["po_id"]: p["delivered_c"] for p in row["pos"]}
        entries, seen = {}, set()
        for d in payments.receipts_to_enter(conn, s.accounting, row["id"]):
            if d["po_id"] not in seen:
                seen.add(d["po_id"])
                entries[d["id"]] = (day, f"{(by_po[d['po_id']] + off_by) / 100:.2f}")
        payments.save_dr_entries(conn, s.accounting, row["id"], entries)

    # untyped still waits for its DRs to be compared.
    enter(differs, "2026-10-28", 50000)
    enter(signoff, "2026-10-28", 30000)
    payments.accept_difference(conn, s.accounting, signoff["id"], "The supplier billed the delivery charge on the DR. Agreed by phone.")
    # Two more wait on Checks to make, on Saturdays of their own. The rest were paid outside the program, a week apart.
    waiting, paid = history[-2:], history[:-2]
    for row, day in zip(waiting, ("2026-10-13", "2026-10-20")):
        enter(row, day)
    days = ["2026-07-06", "2026-07-20", "2026-08-03", "2026-08-17", "2026-08-31", "2026-09-14", "2026-09-28"]
    for row, day in zip(paid, days):
        enter(row, day)
    paid_ids = {r["id"] for r in paid}
    groups = [[o for o in g["orders"] if o["id"] in paid_ids] for g in payments.checks_due(conn)]
    groups = [g for g in groups if g]
    # Dated around the day the sample was built, so every kind of check shows.
    real_today = datetime.now().date()
    number, recorded = 880100, []
    for n, chosen in enumerate(groups):
        number += 1
        day = (real_today + timedelta(days=(-28, -21, -7)[n] if n < 3 else 7 * (n - 2))).isoformat()
        ids, amount = [o["id"] for o in chosen], f"{sum(o['pay_c'] for o in chosen) / 100:.2f}"
        reason = ""
        # The last is for less than the program worked out, so it waits for Admin or management to sign it off.
        if n == len(groups) - 1 and n >= 3:
            amount, reason = f"{float(amount) - 500:.2f}", "Supplier agreed to take 500 off for a late delivery"
        check = payments.record_check(conn, s.accounting, ids, str(number), day, amount, reason)
        if n == 0:
            payments.cancel_check(conn, s.accounting, check, "Printed with the wrong date. Voided.")
            number += 1
            check = payments.record_check(conn, s.accounting, ids, str(number), day, amount)
        recorded.append((check, day))
    # The oldest has cleared the bank, the next has only gone to the supplier, and the others wait to be claimed. Dates stay within the sample's own clock.
    for check, day in recorded[:2]:
        payments.claim_check(conn, s.accounting, check, min(day, "2026-11-02"), "Juan Dela Cruz")
    payments.clear_check(conn, s.accounting, recorded[0][0], min(recorded[0][1], "2026-11-02"))


def _warehouse_stages(s) -> None:
    """The sample's orders were printed before its build, so the Central Warehouse is treated as running since January."""
    from . import warehouse
    conn = s.conn
    conn.execute("INSERT OR REPLACE INTO system_settings(key, value, updated_at) VALUES ('warehouse.live_since', '2026-01-01', '2026-01-01 08:00:00')")
    # Orders the branches already counted went through the warehouse first. Their history is written as it happened: received and shipped the day before the
    # first count, with the pieces stickered matching what the branch agreed, so no figure the payments rest on changes.
    from . import receiving
    from datetime import datetime, timedelta
    counted = conn.execute("SELECT p.id, p.of_id, MIN(b.counted_at) AS first, (SELECT SUM(i.qty_m * i.pieces_per_unit) FROM purchase_order_items i WHERE i.po_id = p.id) AS ordered_m"
                           " FROM purchase_orders p JOIN branch_counts b ON b.po_id = p.id AND b.status = 'APPROVED' WHERE p.of_id IS NOT NULL AND p.issued_at >= '2026-01-01' GROUP BY p.id ORDER BY p.id").fetchall()
    for r in counted:
        day = (datetime.fromisoformat(r["first"]) - timedelta(days=1)).date().isoformat()
        delivery = conn.execute("INSERT INTO warehouse_deliveries(po_id, delivery_no, boxes, received_by, received_at) VALUES (?,1,?,?,?)",
                                (r["id"], max(1, r["ordered_m"] // 1000 // 24), s.warehouse.id, f"{day} 09:00:00")).lastrowid
        if warehouse.stickers(conn, r["of_id"]):
            conn.executemany("INSERT INTO warehouse_delivery_items(delivery_id, po_item_id, pieces_m) VALUES (?,?,?)",
                             [(delivery, line["id"], line["received_m"]) for line in receiving.lines_of(conn, r["id"])])
        conn.execute("UPDATE warehouse_deliveries SET shipped_by = ?, shipped_at = ? WHERE id = ?", (s.warehouse.id, f"{day} 15:00:00", delivery))
    # Of the orders still on their way, one is received and waiting to ship, and the rest are still to receive.
    rows = sorted(warehouse.search(conn, s.admin), key=lambda r: r["id"])
    waiting = [r for r in rows if warehouse.TO_RECEIVE in r["stages"]]
    row = next((r for r in waiting if sum(p["stage"] == warehouse.TO_RECEIVE for p in r["pos"]) > 1), waiting[0])
    first, *others = [p for p in row["pos"] if p["stage"] == warehouse.TO_RECEIVE]
    stickered = {i["po_item_id"]: str(i["ordered_m"] // 1000) for i in warehouse.lines(conn, s.admin, row["id"]) if i["po_id"] == first["id"]} if row["stickers"] else None
    # Only the first branch's goods came. The others are 0 boxes, and come in a second delivery.
    _at(2026, 10, 30, 9)
    warehouse.receive(conn, s.warehouse, row["id"], {first["id"]: "3", **{p["id"]: "0" for p in others}}, stickered,
                      remarks="Only the first branch's cartons were on the truck." if others else "")
    if not others:
        return
    _at(2026, 10, 30, 15)
    warehouse.ship(conn, s.warehouse, row["id"])
    # The branch counts 2 boxes of the 3 the warehouse shipped, and 2 pieces short, so Received shows the missing box.
    _at(2026, 10, 31, 10)
    receivers = [auth.get_user(conn, r[0]) for r in conn.execute(
        "SELECT u.id FROM users u JOIN user_branches b ON b.user_id = u.id JOIN user_roles r ON r.user_id = u.id WHERE r.role = 'RECEIVER' AND b.branch_code = ? ORDER BY u.id",
        (first["branch_code"],))][:2]
    lines = conn.execute("SELECT id, qty_m, pieces_per_unit FROM purchase_order_items WHERE po_id = ? ORDER BY line_no", (first["id"],)).fetchall()
    pieces = {i["id"]: i["qty_m"] * i["pieces_per_unit"] // 1000 for i in lines}
    pieces[lines[0]["id"]] -= 2
    for who in receivers:
        receiving.save_count(conn, who, first["id"], pieces, boxes="2")
    # The rest arrive at the warehouse, received and waiting to ship.
    _at(2026, 11, 2, 9)
    warehouse.receive(conn, s.warehouse, row["id"], {p["id"]: "2" for p in others}, remarks="Second delivery.", another=True)


def ensure(folder: Path = FOLDER) -> None:
    """Point the program at the sample folder and build the database once. A failed build leaves nothing behind."""
    os.environ["PO_DATA_DIR"] = str(folder)
    live = db.db_path()
    if live.exists():
        return
    building = live.with_name("po.building.sqlite")
    building.unlink(missing_ok=True)
    conn = db.open_database(building)
    try:
        build(conn)
    finally:
        conn.close()
    os.replace(building, live)
