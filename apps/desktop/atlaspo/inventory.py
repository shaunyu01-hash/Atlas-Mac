"""Inventory, phase 1: stock on hand at every branch and at the Central Warehouse.

Rules:
- Stock is kept per catalog product (one SKU, size and color) in its base unit: pieces when it is bought in packs (pieces per unit above 1),
  otherwise the product's own unit, so goods sold by the kilo or metre keep their fractions.
- Purchasing already records every piece that moves, so its stock is read straight from those permanent records (MOVES below) and never copied:
  agreed branch counts bring goods in, damage found later moves them to damaged, claim stubs and item passes take them to the warehouse,
  the supplier collects them there, and goods not collected in time become stock for sale at the warehouse. A recount that replaces an
  accepted count replaces its stock too, because only counts in force are read.
- Inventory itself writes only stock_movements, and only from three papers: a posted stock count, an approved adjustment and a transfer.
  Movements are permanent. A mistake is put right with another movement.
- Buckets: GOOD is for sale. DAMAGED is at a branch, waiting for a claim stub or a write-off. HELD is at the warehouse, waiting for the
  supplier to collect it. Goods on the way (item passes printed, transfers sent) are shown as in transit at the place they're going to.
- Purchasing movements dated before inventory.live_since stay out of the books, each judged by its own date, so a later delivery on an
  older PO still counts. The first stock count at each place sets its opening stock.
- Sales and customer returns (stock_sales.py) take stock off and put it back the day they're recorded. A posted stock count moves the
  books to what was counted, so its difference is what went missing, or was sold without being recorded.
- Stock may go below zero (sold before the delivery was counted, say). It is flagged, never blocked.
- Stock counts are blind and double, like receiving: two people count without seeing the books or each other's figures. Lines they agree on
  are fixed; the rest are counted again by two people. Management posts the agreed count. Each product's difference is worked out against
  the books as they stood when it was counted, so anything recorded after that (a sale typed in that evening) isn't counted twice.
  A count can't start while a transfer is on its way there, and a transfer can't be counted in while its place is being counted.
- Whoever prepares an adjustment never approves it, and whoever sends a transfer never receives it. Receivers work at their own branch only,
  the warehouse login at the warehouse only, and neither sees the books, costs or what was sent to them.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from . import masters
from .auth import User, require
from .db import POError, audit, get_setting, timestamp, today, tx
from .money import fmt_qty, parse_qty
from .receiving import _IN_FORCE

WAREHOUSE = 0
GOOD, DAMAGED, HELD = "GOOD", "DAMAGED", "HELD"
BUCKETS = (GOOD, DAMAGED, HELD)

# Stock counts
COUNTING, AGREED, POSTED, CANCELLED = "COUNTING", "AGREED", "POSTED", "CANCELLED"
COUNT_LABELS = {COUNTING: "Counting", AGREED: "Agreed, to post", POSTED: "Posted", CANCELLED: "Cancelled"}
# Adjustments
PENDING, APPROVED, REJECTED = "PENDING", "APPROVED", "REJECTED"
ADJUST_LABELS = {PENDING: "Waiting for approval", APPROVED: "Approved", REJECTED: "Rejected"}
REASONS = {"WRITE_OFF": "Write off", "DAMAGED": "Found damaged", "FOUND": "Found", "INTERNAL_USE": "Used by the business",
           "RETURNED": "Returned to supplier", "OTHER": "Other"}
# Transfers
SENT, RECEIVED = "SENT", "RECEIVED"
TRANSFER_LABELS = {SENT: "On the way", RECEIVED: "Received", CANCELLED: "Cancelled"}

KINDS = {"RECEIVED": "Delivery received", "DAMAGE_FOUND": "Damage found later", "CS_SENT": "Damaged goods sent back",
         "CS_ARRIVED": "Claim stub goods arrived", "IP_SENT": "Excess sent back", "IP_ARRIVED": "Item pass goods arrived",
         "COLLECTED": "Collected by supplier", "FORFEITED": "Forfeited by the supplier", "CS_CANCELLED": "Claim stub cancelled, kept as damaged", "RETURNED": "Returned to supplier",
         "WRITTEN_OFF": "Written off on a debit memo", "SOLD": "Sold", "CUSTOMER_RETURN": "Customer return", "COUNT": "Stock count difference", "ADJUSTMENT": "Adjustment",
         "TRANSFER_OUT": "Transfer sent", "TRANSFER_IN": "Transfer received", "TRANSFER_BACK": "Transfer cancelled"}

MAX_QTY_M = 10**12
NEED_FIGURE = "Type a number for every product, 0 if there are none."
NOT_YOURS = "That isn't your branch."


# ------------------------------------------------------------------ where, and what

def scope(user: User) -> set[int] | None:
    """The places this person works at, or None for every place. Receivers have their branch; the warehouse login has the warehouse.
    A receiver with no branch set gets no place at all, never every place."""
    if user.branch_code is not None:
        return {user.branch_code}
    if "RECEIVER" in user.roles:
        return set()
    if "WAREHOUSE" in user.roles:
        return {WAREHOUSE}
    return None


def _check_place(conn, user: User, code: int) -> None:
    places = scope(user)
    if places is not None and code not in places:
        raise POError(NOT_YOURS)
    if code != WAREHOUSE and conn.execute("SELECT 1 FROM branches WHERE code = ? AND kind = 'BRANCH'", (code,)).fetchone() is None:
        raise POError("There's no branch with that code.")


def warehouse_name(conn) -> str:
    return get_setting(conn, "company.warehouse_name") or "Central Warehouse"


def locations(conn, user: User | None = None, active_only: bool = True) -> list[dict]:
    """The Central Warehouse, then every branch, as {code, name}. Limited to this person's places when a user is given."""
    rows = [{"code": WAREHOUSE, "name": warehouse_name(conn)}]
    rows += [{"code": b["code"], "name": b["name"]} for b in masters.list_branches(conn, active_only=active_only, branches_only=True)]
    places = scope(user) if user is not None else None
    return [r for r in rows if places is None or r["code"] in places]


def place_name(conn, code: int) -> str:
    if code == WAREHOUSE:
        return warehouse_name(conn)
    row = conn.execute("SELECT name FROM branches WHERE code = ?", (code,)).fetchone()
    return row["name"] if row else f"Branch {code}"


def unit(product) -> str:
    """The unit stock is kept in: pieces for goods bought in packs, else the product's own unit."""
    return "PC" if (product["pieces_per_unit"] or 1) > 1 else (product["uom"] or "PC")


def piece_cost_c(product) -> Decimal | None:
    """The last purchase cost of one base unit, or None when the product has never had a cost."""
    if product["last_cost_c"] is None:
        return None
    return Decimal(product["last_cost_c"]) / (product["pieces_per_unit"] or 1)


def value_c(qty_m: int, product) -> int:
    cost = piece_cost_c(product)
    if cost is None:
        return 0
    return int((Decimal(qty_m) * cost / 1000).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _qty(text, product, signed: bool = False) -> int:
    """A typed figure in the product's stock unit, in thousandths."""
    s = str(text if text is not None else "").strip()
    negative = signed and s.startswith("-")
    if negative:
        s = s[1:]
    try:
        qty = parse_qty(s, unit(product))
    except ValueError as exc:
        raise POError(f"{product['item_code']}: {exc}") from None
    if qty > MAX_QTY_M:
        raise POError(f"{product['item_code']}: that figure is too large.")
    return -qty if negative else qty


def _product(conn, product_id: int):
    row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if row is None:
        raise POError("That product isn't in the catalog.")
    return row


def find_product(conn, code: str):
    """A catalog product by its SKU, any case, or None."""
    code = (code or "").strip()
    return conn.execute("SELECT * FROM products WHERE item_code = ?", (code,)).fetchone() if code else None


def _number(conn, kind: str) -> str:
    year = today().year
    conn.execute("INSERT INTO stock_number_sequences(kind, year, last_number) VALUES (?, ?, 1) ON CONFLICT(kind, year) DO UPDATE SET last_number = last_number + 1", (kind, year))
    seq = conn.execute("SELECT last_number FROM stock_number_sequences WHERE kind = ? AND year = ?", (kind, year)).fetchone()[0]
    return f"{year}-{kind}-{seq:05d}"


def live_since(conn) -> str:
    return get_setting(conn, "inventory.live_since")


# ------------------------------------------------------------------ every movement, from purchasing and from inventory

def _product_of(col: str) -> str:
    """The catalog product a PO line or an extra line (negative id) stands for: its own link, or the catalog product with the same SKU.
    Its own table names are unusual on purpose, so a caller's `i` or `p` never shadows them (that once matched every line to one product)."""
    return (f"(CASE WHEN {col} > 0 THEN (SELECT COALESCE(_poi.product_id, (SELECT _prd.id FROM products _prd WHERE _poi.item_code <> '' AND _prd.item_code = _poi.item_code))"
            f" FROM purchase_order_items _poi WHERE _poi.id = {col})"
            f" ELSE (SELECT _prx.id FROM branch_extra_items _ext JOIN products _prx ON _prx.item_code = _ext.item_code WHERE _ext.id = -{col} AND _ext.item_code <> '') END)")


_CS_COUNT = "COALESCE((SELECT g.pieces_m FROM goods_arrivals g WHERE g.kind = 'CS' AND g.item_id = ci.id), ci.pieces_m)"
_IP_COUNT = "COALESCE((SELECT g.pieces_m FROM goods_arrivals g WHERE g.kind = 'IP' AND g.item_id = pi.id), pi.pieces_m)"
_COLS = "moved_at, location, product_id, bucket, qty_m, kind, ref, po_id, po_item_id"


def _first_received(item_col: str) -> str:
    """When damaged pieces of a PO line first reached the branch's books: the earliest receipt in force with damage on it. Goods that later
    leave damaged stock on a claim stub or a debit memo count only when that is on or after the start date."""
    return (f"(SELECT MIN(r.accepted_at) FROM branch_received r WHERE r.po_id = po.id AND r.po_item_id = {item_col} AND r.damaged_m > 0"
            f" AND {_IN_FORCE})")


# When the pieces on a claim stub reached the branch's damaged stock: when the damage was found later, or when the line was counted in.
_CS_DAMAGED_FROM = (f"(CASE WHEN EXISTS (SELECT 1 FROM late_damage ld WHERE ld.cs_id = cs.id) THEN cs.approved_at"
                    f" ELSE COALESCE({_first_received('ci.po_item_id')}, cs.approved_at) END)")
# Damaged pieces counted in on a line that wasn't on the PO (an extra, negative id). Its item pass sends every piece back, damaged ones too.
_IP_DAMAGED = (f"(CASE WHEN pi.po_item_id < 0 THEN MIN(pi.pieces_m, COALESCE((SELECT SUM(r.damaged_m) FROM branch_received r"
               f" WHERE r.po_id = po.id AND r.po_item_id = pi.po_item_id AND {_IN_FORCE}), 0)) ELSE 0 END)")
# The same, counting only damage received on or after the start date: what the books hold in damaged stock to take off.
_IP_DAMAGED_LIVE = _IP_DAMAGED.replace(f"AND {_IN_FORCE}), 0)", f"AND {_IN_FORCE} AND r.accepted_at >= :since), 0)")
# Sales waiting for approval, or rejected, never touch the books.
_NOT_HELD_BACK = "NOT EXISTS (SELECT 1 FROM sale_holds h WHERE h.sale_id = s.id AND h.decision IS NOT 'APPROVED')"

# One row per movement: (moved_at, location, product_id, bucket, qty_m, kind, ref, po_id, po_item_id). product_id is NULL for a purchased
# line that isn't in the catalog; those are listed by not_in_catalog and kept off the books. Bound parameter: :since.
# Purchasing movements before :since (inventory.live_since) are left out one by one, each by its own date. Goods leaving damaged or held
# stock are left out when they entered it before :since, so what never reached the books never leaves them.
MOVES = f"""
moves({_COLS}) AS (
    SELECT r.accepted_at, po.branch_code, {_product_of('r.po_item_id')}, 'GOOD', r.pieces_m - r.damaged_m, 'RECEIVED', po.po_number, po.id, r.po_item_id
      FROM branch_received r JOIN purchase_orders po ON po.id = r.po_id WHERE {_IN_FORCE} AND r.accepted_at >= :since
    UNION ALL
    SELECT r.accepted_at, po.branch_code, {_product_of('r.po_item_id')}, 'DAMAGED', r.damaged_m, 'RECEIVED', po.po_number, po.id, r.po_item_id
      FROM branch_received r JOIN purchase_orders po ON po.id = r.po_id WHERE r.damaged_m > 0 AND {_IN_FORCE} AND r.accepted_at >= :since
    UNION ALL
    SELECT cs.approved_at, po.branch_code, {_product_of('ci.po_item_id')}, b.bucket, CASE b.bucket WHEN 'GOOD' THEN -ci.pieces_m ELSE ci.pieces_m END,
           'DAMAGE_FOUND', cs.cs_number, po.id, ci.po_item_id
      FROM claim_stubs cs JOIN late_damage ld ON ld.cs_id = cs.id JOIN claim_stub_items ci ON ci.cs_id = cs.id JOIN purchase_orders po ON po.id = cs.po_id
      JOIN (SELECT 'GOOD' AS bucket UNION ALL SELECT 'DAMAGED') b
     WHERE NOT (cs.status = 'CANCELLED' AND cs.arrived_at IS NULL) AND cs.approved_at >= :since
    UNION ALL
    SELECT cs.arrived_at, po.branch_code, {_product_of('ci.po_item_id')}, 'DAMAGED', -ci.pieces_m, 'CS_SENT', cs.cs_number, po.id, ci.po_item_id
      FROM claim_stubs cs JOIN claim_stub_items ci ON ci.cs_id = cs.id JOIN purchase_orders po ON po.id = cs.po_id
     WHERE cs.arrived_at IS NOT NULL AND {_CS_DAMAGED_FROM} >= :since
    UNION ALL
    SELECT cs.arrived_at, 0, {_product_of('ci.po_item_id')}, 'HELD', {_CS_COUNT}, 'CS_ARRIVED', cs.cs_number, po.id, ci.po_item_id
      FROM claim_stubs cs JOIN claim_stub_items ci ON ci.cs_id = cs.id JOIN purchase_orders po ON po.id = cs.po_id WHERE cs.arrived_at >= :since
    UNION ALL
    SELECT cs.closed_at, 0, {_product_of('ci.po_item_id')}, b.bucket, CASE WHEN b.bucket = 'HELD' THEN -{_CS_COUNT} ELSE {_CS_COUNT} END,
           CASE cs.status WHEN 'CANCELLED' THEN 'CS_CANCELLED' ELSE cs.status END, cs.cs_number, po.id, ci.po_item_id
      FROM claim_stubs cs JOIN claim_stub_items ci ON ci.cs_id = cs.id JOIN purchase_orders po ON po.id = cs.po_id
      JOIN (SELECT 'HELD' AS bucket UNION ALL SELECT 'GOOD' UNION ALL SELECT 'DAMAGED') b
     WHERE cs.status IN ('COLLECTED','FORFEITED','CANCELLED') AND cs.arrived_at IS NOT NULL
       AND ((b.bucket = 'HELD' AND cs.arrived_at >= :since)
            OR (b.bucket = 'GOOD' AND cs.status = 'FORFEITED' AND cs.closed_at >= :since)
            OR (b.bucket = 'DAMAGED' AND cs.status = 'CANCELLED' AND cs.closed_at >= :since))
    UNION ALL
    SELECT ip.printed_at, po.branch_code, {_product_of('pi.po_item_id')}, b.bucket,
           CASE b.bucket WHEN 'GOOD' THEN {_IP_DAMAGED} - pi.pieces_m ELSE -{_IP_DAMAGED_LIVE} END, 'IP_SENT', ip.ip_number, po.id, pi.po_item_id
      FROM item_passes ip JOIN item_pass_items pi ON pi.ip_id = ip.id JOIN purchase_orders po ON po.id = ip.po_id
      JOIN (SELECT 'GOOD' AS bucket UNION ALL SELECT 'DAMAGED') b
     WHERE ip.status <> 'CANCELLED' AND ip.printed_at >= :since
    UNION ALL
    SELECT ip.arrived_at, 0, {_product_of('pi.po_item_id')}, 'HELD', {_IP_COUNT}, 'IP_ARRIVED', ip.ip_number, po.id, pi.po_item_id
      FROM item_passes ip JOIN item_pass_items pi ON pi.ip_id = ip.id JOIN purchase_orders po ON po.id = ip.po_id
     WHERE ip.arrived_at >= :since AND ip.status <> 'CANCELLED'
    UNION ALL
    SELECT ip.closed_at, 0, {_product_of('pi.po_item_id')}, b.bucket, CASE b.bucket WHEN 'HELD' THEN -{_IP_COUNT} ELSE {_IP_COUNT} END,
           ip.status, ip.ip_number, po.id, pi.po_item_id
      FROM item_passes ip JOIN item_pass_items pi ON pi.ip_id = ip.id JOIN purchase_orders po ON po.id = ip.po_id
      JOIN (SELECT 'HELD' AS bucket UNION ALL SELECT 'GOOD') b
     WHERE ip.status IN ('COLLECTED','FORFEITED') AND ip.arrived_at IS NOT NULL
       AND ((b.bucket = 'HELD' AND ip.arrived_at >= :since) OR (b.bucket = 'GOOD' AND ip.status = 'FORFEITED' AND ip.closed_at >= :since))
    UNION ALL
    SELECT x.returned_at, po.branch_code, {_product_of('x.po_item_id')}, 'GOOD', -x.pieces_m, 'RETURNED', po.po_number, po.id, x.po_item_id
      FROM excess_returns x JOIN purchase_orders po ON po.id = x.po_id WHERE x.returned_at >= :since
    UNION ALL
    SELECT dm.prepared_at, po.branch_code, {_product_of('di.po_item_id')}, 'DAMAGED', -di.pieces_m, 'WRITTEN_OFF', dm.dm_number, po.id, di.po_item_id
      FROM debit_memos dm JOIN debit_memo_items di ON di.dm_id = dm.id JOIN purchase_orders po ON po.id = dm.po_id
     WHERE dm.status = 'ISSUED' AND di.reason = 'DAMAGED' AND COALESCE({_first_received('di.po_item_id')}, dm.prepared_at) >= :since
    UNION ALL
    SELECT MIN(s.sale_date || ' 23:59:59', s.entered_at), s.location_code, l.product_id, l.bucket,
           CASE s.kind WHEN 'SALE' THEN -l.qty_m ELSE l.qty_m END, CASE s.kind WHEN 'SALE' THEN 'SOLD' ELSE 'CUSTOMER_RETURN' END, s.sale_number, NULL, NULL
      FROM stock_sales s JOIN stock_sale_lines l ON l.sale_id = s.id WHERE s.status = 'POSTED' AND {_NOT_HELD_BACK}
    UNION ALL
    SELECT m.moved_at, m.location_code, m.product_id, m.bucket, m.qty_m, m.kind,
           CASE WHEN m.kind = 'COUNT' THEN (SELECT count_number FROM stock_counts WHERE id = m.ref_id)
                WHEN m.kind = 'ADJUSTMENT' THEN (SELECT adj_number FROM stock_adjustments WHERE id = m.ref_id)
                ELSE (SELECT transfer_number FROM stock_transfers WHERE id = m.ref_id) END, NULL, NULL
      FROM stock_movements m
)"""


def _moves_sql(where: str = "", select: str = "*") -> str:
    return f"WITH {MOVES} SELECT {select} FROM moves WHERE qty_m <> 0" + (f" AND {where}" if where else "")


def _params(conn, **more) -> dict:
    return {"since": live_since(conn), **more}


def book(conn, location: int, bucket: str = GOOD) -> dict[int, int]:
    """{product_id: quantity} on the books at one place, in one bucket. No permission check: services use it."""
    return {r[0]: r[1] for r in conn.execute(_moves_sql("location = :loc AND bucket = :bucket AND product_id IS NOT NULL", "product_id, SUM(qty_m)") + " GROUP BY product_id",
                                             _params(conn, loc=location, bucket=bucket))}


def _in_transit(conn) -> dict[tuple[int, int], int]:
    """{(location, product_id): quantity} on the way there: excess going back to the warehouse, and transfers sent."""
    out: dict[tuple[int, int], int] = defaultdict(int)
    for loc, pid, qty in conn.execute(
            f"SELECT 0, {_product_of('pi.po_item_id')} AS pid, SUM(pi.pieces_m) FROM item_passes ip JOIN item_pass_items pi ON pi.ip_id = ip.id"
            f" JOIN purchase_orders po ON po.id = ip.po_id WHERE ip.status = 'PRINTED' AND ip.printed_at >= :since GROUP BY pid", _params(conn)):
        if pid is not None:
            out[(loc, pid)] += qty
    for loc, pid, qty in conn.execute("SELECT t.to_code, l.product_id, SUM(l.sent_m) FROM stock_transfers t JOIN stock_transfer_lines l ON l.transfer_id = t.id"
                                      " WHERE t.status = 'SENT' GROUP BY t.to_code, l.product_id"):
        out[(loc, pid)] += qty
    return out


# ------------------------------------------------------------------ what's on hand

def on_hand(conn, user: User, location: int | None = None, search: str = "", department_id: int | None = None,
            below_zero: bool = False, include_zero: bool = False) -> list[dict]:
    """Stock per place and product: good, damaged, held for suppliers and in transit, with its value at the last purchase cost."""
    require(user, "inventory.view")
    sums: dict[tuple[int, int], dict] = defaultdict(lambda: {GOOD: 0, DAMAGED: 0, HELD: 0})
    for loc, pid, bucket, qty in conn.execute(_moves_sql("product_id IS NOT NULL", "location, product_id, bucket, SUM(qty_m)") + " GROUP BY location, product_id, bucket",
                                              _params(conn)):
        sums[(loc, pid)][bucket] = qty
    transit = _in_transit(conn)
    for key in transit:
        sums[key]  # noqa: B018 (makes the row)
    products = {r["id"]: r for r in conn.execute("SELECT p.*, d.name AS department FROM products p JOIN departments d ON d.id = p.department_id")}
    names = {r["code"]: r["name"] for r in locations(conn, active_only=False)}
    words = (search or "").strip().lower().split()
    rows = []
    for (loc, pid), b in sums.items():
        p = products.get(pid)
        if p is None or (location is not None and loc != location) or (department_id is not None and p["department_id"] != department_id):
            continue
        haystack = f"{p['item_code']} {p['description']} {p['brand']} {p['size']} {p['color']}".lower()
        if words and not all(w in haystack for w in words):
            continue
        moving = transit.get((loc, pid), 0)
        if not include_zero and not (b[GOOD] or b[DAMAGED] or b[HELD] or moving):
            continue
        if below_zero and min(b.values()) >= 0:
            continue
        rows.append({"location": loc, "location_name": names.get(loc, f"Branch {loc}"), "product_id": pid, "item_code": p["item_code"],
                     "description": p["description"], "brand": p["brand"], "size": p["size"], "color": p["color"], "department": p["department"],
                     "unit": unit(p), "good_m": b[GOOD], "damaged_m": b[DAMAGED], "held_m": b[HELD], "transit_m": moving,
                     "below_zero": min(b.values()) < 0, "has_cost": p["last_cost_c"] is not None,
                     "good_value_c": value_c(b[GOOD], p), "value_c": value_c(b[GOOD] + b[DAMAGED] + b[HELD], p)})
    rows.sort(key=lambda r: (r["location"], r["item_code"].lower()))
    return rows


def valuation(conn, user: User) -> list[dict]:
    """Per place: products in stock, units, value for sale, value damaged or held, and how many products are below zero or have no cost."""
    rows = on_hand(conn, user)
    out = {r["code"]: {"location": r["code"], "location_name": r["name"], "products": 0, "good_value_c": 0, "other_value_c": 0, "below_zero": 0, "no_cost": 0}
           for r in locations(conn)}
    for r in rows:
        o = out.setdefault(r["location"], {"location": r["location"], "location_name": r["location_name"], "products": 0, "good_value_c": 0,
                                           "other_value_c": 0, "below_zero": 0, "no_cost": 0})
        o["products"] += 1
        o["good_value_c"] += r["good_value_c"]
        o["other_value_c"] += r["value_c"] - r["good_value_c"]
        o["below_zero"] += r["below_zero"]
        o["no_cost"] += not r["has_cost"]
    return list(out.values())


def stock_card(conn, user: User, location: int, product_id: int, date_from: str | None = None, date_to: str | None = None) -> dict:
    """Every movement of one product at one place, oldest first, with what was left after each. Balances before date_from are carried in."""
    require(user, "inventory.view")
    p = _product(conn, product_id)
    rows = [dict(r) for r in conn.execute(_moves_sql("location = :loc AND product_id = :pid", "moved_at, bucket, qty_m, kind, ref") + " ORDER BY moved_at, kind, bucket DESC",
                                          _params(conn, loc=location, pid=product_id))]
    balance = {GOOD: 0, DAMAGED: 0, HELD: 0}
    opening = None
    out = []
    for r in rows:
        day = (r["moved_at"] or "")[:10]
        if date_to and day > date_to:
            break
        if date_from and day < date_from:
            balance[r["bucket"]] += r["qty_m"]
            continue
        if opening is None:
            opening = dict(balance)
        balance[r["bucket"]] += r["qty_m"]
        out.append({**r, "kind_label": KINDS.get(r["kind"], r["kind"]), "good_m": balance[GOOD], "damaged_m": balance[DAMAGED], "held_m": balance[HELD]})
    return {"product": dict(p), "unit": unit(p), "location_name": place_name(conn, location), "opening": opening or dict(balance),
            "rows": out, "closing": dict(balance)}


def movement_summary(conn, user: User, date_from: str, date_to: str, location: int | None = None) -> list[dict]:
    """Totals per kind of movement between two dates (inclusive), in units and at the last purchase cost."""
    require(user, "inventory.view")
    where = "product_id IS NOT NULL AND substr(moved_at, 1, 10) BETWEEN :a AND :b" + (" AND location = :loc" if location is not None else "")
    products = {r["id"]: r for r in conn.execute("SELECT * FROM products")}
    out: dict[tuple, dict] = {}
    for kind, bucket, pid, qty in conn.execute(_moves_sql(where, "kind, bucket, product_id, SUM(qty_m)") + " GROUP BY kind, bucket, product_id",
                                               _params(conn, a=date_from, b=date_to, loc=location)):
        o = out.setdefault((kind, bucket), {"kind": kind, "kind_label": KINDS.get(kind, kind), "bucket": bucket, "in_m": 0, "out_m": 0, "value_c": 0, "products": 0})
        o["in_m" if qty > 0 else "out_m"] += abs(qty)
        o["value_c"] += value_c(qty, products[pid])
        o["products"] += 1
    return sorted(out.values(), key=lambda o: (list(KINDS).index(o["kind"]) if o["kind"] in KINDS else 99, o["bucket"]))


def not_in_catalog(conn, user: User) -> list[dict]:
    """Purchased lines that moved stock but match no catalog product, so they're kept off the books until the product is added."""
    require(user, "inventory.view")
    rows = conn.execute(_moves_sql("product_id IS NULL", "location, po_id, po_item_id, SUM(qty_m) AS qty_m, MAX(moved_at) AS last_at")
                        + " GROUP BY location, po_id, po_item_id", _params(conn)).fetchall()
    out = []
    for r in rows:
        if r["po_item_id"] > 0:
            line = conn.execute("SELECT item_code, description, size, color FROM purchase_order_items WHERE id = ?", (r["po_item_id"],)).fetchone()
        else:
            line = conn.execute("SELECT item_code, description, size, color FROM branch_extra_items WHERE id = ?", (-r["po_item_id"],)).fetchone()
        po = conn.execute("SELECT po_number FROM purchase_orders WHERE id = ?", (r["po_id"],)).fetchone()
        out.append({"location": r["location"], "location_name": place_name(conn, r["location"]), "po_number": po["po_number"] if po else "",
                    "item_code": line["item_code"] if line else "", "description": line["description"] if line else "",
                    "size": line["size"] if line else "", "color": line["color"] if line else "", "qty_m": r["qty_m"], "last_at": r["last_at"]})
    return out


def set_live_since(conn, user: User, day: str) -> None:
    """Purchasing movements before this day stay off the books. Changing it changes every book, so only Admin can, and only before the first
    stock count is posted: after that, counts and sales already rest on the books as they were."""
    require(user, "settings.edit")
    from datetime import date
    try:
        date.fromisoformat(day)
    except (TypeError, ValueError):
        raise POError("Choose a date.") from None
    with tx(conn):
        if conn.execute("SELECT 1 FROM stock_counts WHERE status = 'POSTED' LIMIT 1").fetchone():
            raise POError("A stock count has been posted, so the start date can't change any more: every book since rests on it.")
        old = live_since(conn)
        conn.execute("INSERT INTO system_settings(key, value, updated_at, updated_by) VALUES ('inventory.live_since', ?, ?, ?)"
                     " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                     (day, timestamp(), user.id))
        audit(conn, user, "SETTING_CHANGE", "system_setting", summary=f"Inventory counts orders received from {day}", previous=old, new=day)


# ------------------------------------------------------------------ stock counts

def count_in_the_way(conn, location: int, moment: str, product_ids=None) -> dict | None:
    """A stock count at this place, not cancelled, covering any of these products (every product when None), that is still open or was
    posted after `moment`. Approving, cancelling or recounting something dated at `moment` would then change stock that count already saw,
    and it would be counted twice."""
    depts = None
    if product_ids is not None:
        ids = list(product_ids)
        depts = {r[0] for r in conn.execute(f"SELECT DISTINCT department_id FROM products WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else set()
    for c in conn.execute("SELECT count_number, department_id, status, substr(opened_at, 1, 10) AS day FROM stock_counts WHERE location_code = ?"
                          " AND status <> 'CANCELLED' AND (closed_at IS NULL OR closed_at > ?) ORDER BY opened_at", (location, moment)):
        if depts is None or c["department_id"] is None or c["department_id"] in depts:
            return dict(c)
    return None


def _count_words(conn, count: dict, location: int) -> str:
    if count["status"] in (COUNTING, AGREED):
        return f"{place_name(conn, location)} is being counted on {count['count_number']}. Post or cancel the count first."
    return f"Stock count {count['count_number']} at {place_name(conn, location)} already took this in. Put the books right with an adjustment instead."


def _count(conn, count_id: int):
    row = conn.execute("SELECT * FROM stock_counts WHERE id = ?", (count_id,)).fetchone()
    if row is None:
        raise POError("That stock count doesn't exist.")
    return row


def open_count(conn, user: User, location: int, department_id: int | None = None, note: str = "") -> int:
    """Starts a count at one place, of one department or everything. Lists every product the books show there; the counters add what else they find."""
    require(user, "count.manage")
    with tx(conn):
        _check_place(conn, user, location)
        if department_id is not None and conn.execute("SELECT 1 FROM departments WHERE id = ?", (department_id,)).fetchone() is None:
            raise POError("There's no such department.")
        busy = conn.execute("SELECT count_number FROM stock_counts WHERE location_code = ? AND status IN ('COUNTING','AGREED')", (location,)).fetchone()
        if busy:
            raise POError(f"{place_name(conn, location)} is already being counted on {busy[0]}. Post or cancel it first.")
        coming = conn.execute("SELECT transfer_number FROM stock_transfers WHERE to_code = ? AND status = 'SENT' ORDER BY id LIMIT 1", (location,)).fetchone()
        if coming:
            raise POError(f"Transfer {coming[0]} is on its way to {place_name(conn, location)}. Count it in, or cancel it, before starting a count.")
        number = _number(conn, "SC")
        count_id = conn.execute("INSERT INTO stock_counts(count_number, location_code, department_id, status, note, opened_by, opened_at) VALUES (?,?,?,?,?,?,?)",
                                (number, location, department_id, COUNTING, (note or "").strip(), user.id, timestamp())).lastrowid
        books = book(conn, location)
        in_dept = {r[0] for r in conn.execute("SELECT id FROM products" + (" WHERE department_id = ?" if department_id is not None else ""),
                                              (department_id,) if department_id is not None else ())}
        lines = [(count_id, pid, qty) for pid, qty in books.items() if qty and pid in in_dept]
        conn.executemany("INSERT INTO stock_count_lines(count_id, product_id, book_m) VALUES (?,?,?)", lines)
        audit(conn, user, "STOCK_COUNT_OPEN", "stock_count", count_id,
              summary=f"{number} started at {place_name(conn, location)}" + (f", {_dept_name(conn, department_id)} only" if department_id else "") + f", {len(lines)} products listed")
    return count_id


def _dept_name(conn, department_id: int | None) -> str:
    if department_id is None:
        return "Every department"
    row = conn.execute("SELECT name FROM departments WHERE id = ?", (department_id,)).fetchone()
    return row["name"] if row else ""


def counts(conn, user: User, status: str | None = None) -> list[dict]:
    """Stock counts, newest first, at this person's places."""
    if not (user.can("count.manage") or user.can("stock.count") or user.can("stock.post")):
        require(user, "count.manage")
    places = scope(user)
    rows = conn.execute("SELECT c.*, u.full_name AS opened_name, x.full_name AS closed_name FROM stock_counts c JOIN users u ON u.id = c.opened_by"
                        " LEFT JOIN users x ON x.id = c.closed_by" + (" WHERE c.status = ?" if status else "") + " ORDER BY c.id DESC", (status,) if status else ()).fetchall()
    out = []
    for r in rows:
        if places is not None and r["location_code"] not in places:
            continue
        state = _state(conn, r["id"])
        out.append({**dict(r), "location_name": place_name(conn, r["location_code"]), "department": _dept_name(conn, r["department_id"]),
                    "status_label": COUNT_LABELS[r["status"]], "round_no": state["round"], "counted_this_round": state["counters"],
                    "lines": conn.execute("SELECT COUNT(*) FROM stock_count_lines WHERE count_id = ?", (r["id"],)).fetchone()[0],
                    "open_lines": len(state["open"])})
    return out


def _state(conn, count_id: int) -> dict:
    """Which round is being counted, who has counted it, and which products are still to agree."""
    last = conn.execute("SELECT MAX(round_no) FROM stock_count_entries WHERE count_id = ?", (count_id,)).fetchone()[0] or 1
    counters = [r[0] for r in conn.execute("SELECT DISTINCT counted_by FROM stock_count_entries WHERE count_id = ? AND round_no = ? ORDER BY counted_by", (count_id, last))]
    if len(counters) >= 2:
        last, counters = last + 1, []
    open_ids = [r[0] for r in conn.execute("SELECT product_id FROM stock_count_lines WHERE count_id = ? AND agreed_m IS NULL", (count_id,))]
    return {"round": last, "counters": counters, "open": open_ids}


def _counter_check(conn, user: User, count) -> dict:
    require(user, "stock.count")
    _check_place(conn, user, count["location_code"])
    if count["status"] != COUNTING:
        raise POError("This stock count is finished.")
    state = _state(conn, count["id"])
    if user.id in state["counters"]:
        raise POError("You've counted this round. The other count must be done by someone else.")
    return state


_SHEET = "SELECT p.id AS product_id, p.item_code, p.description, p.brand, p.size, p.color, p.uom, p.pieces_per_unit FROM stock_count_lines l JOIN products p ON p.id = l.product_id"


def count_sheet(conn, user: User, count_id: int) -> dict:
    """What a counter counts this round: the products only, never the books or anyone's figures. Later rounds list only what didn't agree."""
    count = _count(conn, count_id)
    state = _counter_check(conn, user, count)
    rows = conn.execute(_SHEET + " WHERE l.count_id = ? AND l.agreed_m IS NULL ORDER BY p.item_code", (count_id,)).fetchall()
    return {"count_number": count["count_number"], "location_name": place_name(conn, count["location_code"]), "round": state["round"],
            "department": _dept_name(conn, count["department_id"]), "first": not state["counters"],
            "lines": [{**dict(r), "unit": unit(r)} for r in rows]}


def save_count(conn, user: User, count_id: int, figures: dict[int, str], found: dict[str, str] | None = None) -> dict:
    """One person's figures for this round. found is {SKU: figure} for products not on the sheet. The second person's save compares the two."""
    found = {k.strip(): v for k, v in (found or {}).items() if k.strip()}
    with tx(conn):
        count = _count(conn, count_id)
        state = _counter_check(conn, user, count)
        sheet = {r["product_id"]: r for r in conn.execute(_SHEET + " WHERE l.count_id = ? AND l.agreed_m IS NULL", (count_id,))}
        missing = [p for pid, p in sheet.items() if str(figures.get(pid, "")).strip() == ""]
        if missing:
            raise POError(NEED_FIGURE + f" {missing[0]['item_code']} has none.")
        extra = set(figures) - set(sheet)
        if extra:
            raise POError("The count changed while you were counting. Open it again.")
        typed = {pid: _qty(figures[pid], sheet[pid]) for pid in sheet}
        books = book(conn, count["location_code"]) if found else {}
        for code, text in found.items():
            p = find_product(conn, code)
            if p is None:
                raise POError(f"{code} isn't in the product catalog. Ask the office to add it first.")
            if count["department_id"] is not None and p["department_id"] != count["department_id"]:
                raise POError(f"{p['item_code']} isn't in {_dept_name(conn, count['department_id'])}, so it isn't part of this count.")
            if conn.execute("SELECT 1 FROM stock_count_lines WHERE count_id = ? AND product_id = ?", (count_id, p["id"])).fetchone():
                raise POError(f"{p['item_code']} is already on the count.")
            if p["id"] in typed:
                raise POError(f"{p['item_code']} is listed twice.")
            typed[p["id"]] = _qty(text, p)
            conn.execute("INSERT INTO stock_count_lines(count_id, product_id, book_m, added_by) VALUES (?,?,?,?)", (count_id, p["id"], books.get(p["id"], 0), user.id))
        if not typed:
            raise POError("Nothing is listed on this count. Add each product you find by its SKU.")
        stamp = timestamp()
        rnd = state["round"]
        conn.executemany("INSERT INTO stock_count_entries(count_id, round_no, counted_by, product_id, qty_m, counted_at) VALUES (?,?,?,?,?,?)",
                         [(count_id, rnd, user.id, pid, qty, stamp) for pid, qty in typed.items()])
        audit(conn, user, "STOCK_COUNT", "stock_count", count_id, summary=f"{count['count_number']} round {rnd}: {len(typed)} products counted")
        if not state["counters"]:
            return {"outcome": "WAITING", "mismatched": 0}
        other = state["counters"][0]
        theirs = {r[0]: r[1] for r in conn.execute("SELECT product_id, qty_m FROM stock_count_entries WHERE count_id = ? AND round_no = ? AND counted_by = ?", (count_id, rnd, other))}
        open_now = [r[0] for r in conn.execute("SELECT product_id FROM stock_count_lines WHERE count_id = ? AND agreed_m IS NULL", (count_id,))]
        agreed = [(typed[pid], count_id, pid) for pid in open_now if pid in typed and pid in theirs and typed[pid] == theirs[pid]]
        conn.executemany("UPDATE stock_count_lines SET agreed_m = ? WHERE count_id = ? AND product_id = ?", agreed)
        left = len(open_now) - len(agreed)
        if left:
            audit(conn, user, "STOCK_COUNT_MISMATCH", "stock_count", count_id, summary=f"{count['count_number']} round {rnd}: {left} products to count again")
            return {"outcome": "AGAIN", "mismatched": left}
        conn.execute("UPDATE stock_counts SET status = ? WHERE id = ?", (AGREED, count_id))
        audit(conn, user, "STOCK_COUNT_MATCH", "stock_count", count_id, summary=f"{count['count_number']}: both counts agree")
        return {"outcome": AGREED, "mismatched": 0}


def my_figures(conn, user: User, count_id: int) -> list[dict]:
    """A counter's own figures on this count, every round. Never anyone else's."""
    require(user, "stock.count")
    count = _count(conn, count_id)
    _check_place(conn, user, count["location_code"])
    return [dict(r) for r in conn.execute("SELECT e.round_no, p.item_code, p.description, p.size, p.color, e.qty_m FROM stock_count_entries e JOIN products p ON p.id = e.product_id"
                                          " WHERE e.count_id = ? AND e.counted_by = ? ORDER BY e.round_no, p.item_code", (count_id, user.id))]


def count_results(conn, user: User, count_id: int) -> dict:
    """The office's view: the books when the count started, each person's figures, what was agreed and the difference, valued at the last cost."""
    if not user.can("stock.post"):
        require(user, "count.manage")
    count = _count(conn, count_id)
    _check_place(conn, user, count["location_code"])
    show_cost = user.can("inventory.view")
    entries: dict[int, list] = defaultdict(list)
    for r in conn.execute("SELECT e.product_id, e.round_no, u.full_name, e.qty_m FROM stock_count_entries e JOIN users u ON u.id = e.counted_by WHERE e.count_id = ? ORDER BY e.round_no, e.counted_at",
                          (count_id,)):
        entries[r["product_id"]].append(f"R{r['round_no']} {r['full_name']}: {fmt_qty(r['qty_m'])}")
    if count["status"] == POSTED:
        posted = {r[0]: r[1] for r in conn.execute("SELECT product_id, qty_m FROM stock_movements WHERE kind = 'COUNT' AND ref_id = ?", (count_id,))}
        books = None
    else:
        posted = {}
        books = _books_when_counted(conn, count) if count["status"] in (COUNTING, AGREED) else None
    lines = []
    for r in conn.execute("SELECT l.*, p.item_code, p.description, p.size, p.color, p.uom, p.pieces_per_unit, p.last_cost_c FROM stock_count_lines l JOIN products p ON p.id = l.product_id"
                          " WHERE l.count_id = ? ORDER BY p.item_code", (count_id,)):
        if count["status"] == POSTED:
            book_m = r["agreed_m"] - posted.get(r["product_id"], 0)
        else:
            book_m = books.get(r["product_id"], 0) if books is not None else r["book_m"]
        diff = None if r["agreed_m"] is None else r["agreed_m"] - book_m
        warning = _not_for_sale(conn, count["location_code"], r["product_id"]) if r["added_by"] is not None else ""
        lines.append({"warning": warning, "product_id": r["product_id"], "item_code": r["item_code"], "description": r["description"], "size": r["size"], "color": r["color"],
                      "unit": unit(r), "book_m": book_m, "start_m": r["book_m"], "agreed_m": r["agreed_m"], "diff_m": diff, "found": r["added_by"] is not None,
                      "counts": "; ".join(entries.get(r["product_id"], [])), "diff_value_c": value_c(diff or 0, r) if show_cost else None})
    return {**dict(count), "location_name": place_name(conn, count["location_code"]), "department": _dept_name(conn, count["department_id"]),
            "status_label": COUNT_LABELS[count["status"]], "lines": lines, "moved_since": moved_since(conn, count_id),
            "short_value_c": sum(-l["diff_value_c"] for l in lines if show_cost and (l["diff_m"] or 0) < 0),
            "over_value_c": sum(l["diff_value_c"] for l in lines if show_cost and (l["diff_m"] or 0) > 0)}


def _not_for_sale(conn, location: int, product_id: int) -> str:
    """Why goods found at the warehouse may not be stock for sale: some are held for a supplier, or a delivery of them waits to ship."""
    if location != WAREHOUSE:
        return ""
    notes = []
    held = book(conn, WAREHOUSE, HELD).get(product_id, 0)
    if held > 0:
        notes.append(f"{fmt_qty(held)} held for a supplier")
    waiting = conn.execute(f"SELECT COUNT(*) FROM warehouse_deliveries d JOIN purchase_order_items i ON i.po_id = d.po_id"
                           f" WHERE d.shipped_at IS NULL AND {_product_of('i.id')} = ?", (product_id,)).fetchone()[0]
    if waiting:
        notes.append("a delivery of it waits to ship")
    return ("Found here, but " + " and ".join(notes) + ". Check these aren't the same goods.") if notes else ""


def moved_since(conn, count_id: int) -> int:
    """How many movements reached this place's good stock after the count started. Posting allows for them; the office sees how many."""
    count = _count(conn, count_id)
    return conn.execute(_moves_sql("location = :loc AND bucket = 'GOOD' AND moved_at > :at AND NOT (kind = 'COUNT')", "COUNT(*)"),
                        _params(conn, loc=count["location_code"], at=count["opened_at"])).fetchone()[0]


def _purchasing_during(conn, count) -> str:
    """What purchasing changed in this place's good stock after the count opened: a delivery count or recount approved, damage found
    later approved, or claim stub or item pass goods forfeited into stock at the warehouse. '' when nothing."""
    loc, at = count["location_code"], count["opened_at"]
    row = conn.execute("SELECT po.po_number FROM branch_counts b JOIN purchase_orders po ON po.id = b.po_id WHERE po.branch_code = ? AND b.approved_at > ?"
                       " ORDER BY b.approved_at LIMIT 1", (loc, at)).fetchone()
    if row:
        return f"A delivery count on {row[0]} was approved at {place_name(conn, loc)}"
    row = conn.execute("SELECT cs.cs_number FROM claim_stubs cs JOIN late_damage ld ON ld.cs_id = cs.id JOIN purchase_orders po ON po.id = cs.po_id"
                       " WHERE po.branch_code = ? AND cs.approved_at > ? LIMIT 1", (loc, at)).fetchone()
    if row:
        return f"Damage found later ({row[0]}) was approved at {place_name(conn, loc)}"
    if loc == WAREHOUSE:
        row = conn.execute("SELECT cs_number FROM claim_stubs WHERE status = 'FORFEITED' AND closed_at > ? UNION ALL"
                           " SELECT ip_number FROM item_passes WHERE status = 'FORFEITED' AND closed_at > ? LIMIT 1", (at, at)).fetchone()
        if row:
            return f"Goods on {row[0]} were forfeited into stock for sale"
    return ""


def _books_when_counted(conn, count) -> dict[int, int]:
    """{product_id: good stock on the books when that product was last counted on this count}, read now, so movements recorded later
    but dated before the counting are in, and those dated after it are out. A product not counted yet is taken as of now."""
    when = {r[0]: r[1] for r in conn.execute(
        "SELECT e.product_id, MAX(e.counted_at) FROM stock_count_entries e WHERE e.count_id = ?"
        " AND e.round_no = (SELECT MAX(x.round_no) FROM stock_count_entries x WHERE x.count_id = e.count_id AND x.product_id = e.product_id) GROUP BY e.product_id",
        (count["id"],))}
    ids = [r[0] for r in conn.execute("SELECT product_id FROM stock_count_lines WHERE count_id = ?", (count["id"],))]
    books = dict.fromkeys(ids, 0)
    for pid, moved_at, qty in conn.execute(_moves_sql("location = :loc AND bucket = 'GOOD' AND product_id IS NOT NULL", "product_id, moved_at, qty_m"),
                                           _params(conn, loc=count["location_code"])):
        if pid in books and (pid not in when or (moved_at or "") <= when[pid]):
            books[pid] += qty
    return books


def post_count(conn, user: User, count_id: int) -> int:
    """Moves the books to what was counted: each product's agreed figure less the books when it was counted. Returns how many products changed."""
    require(user, "stock.post")
    with tx(conn):
        count = _count(conn, count_id)
        _check_place(conn, user, count["location_code"])
        if count["status"] != AGREED:
            raise POError("Only a count both people agree on can be posted.")
        if conn.execute("SELECT 1 FROM stock_count_entries WHERE count_id = ? AND counted_by = ?", (count_id, user.id)).fetchone():
            raise POError("You counted this stock, so someone else posts it.")
        moved = _purchasing_during(conn, count)
        if moved:
            raise POError(f"{moved} while {count['count_number']} was open, so the count can't tell which goods it saw. Cancel this count and count again.")
        stamp = timestamp()
        books = _books_when_counted(conn, count)
        diffs = [(count["location_code"], r["product_id"], GOOD, r["agreed_m"] - books[r["product_id"]], "COUNT", count_id, user.id, stamp)
                 for r in conn.execute("SELECT * FROM stock_count_lines WHERE count_id = ?", (count_id,)) if r["agreed_m"] != books[r["product_id"]]]
        conn.executemany("INSERT INTO stock_movements(location_code, product_id, bucket, qty_m, kind, ref_id, moved_by, moved_at) VALUES (?,?,?,?,?,?,?,?)", diffs)
        conn.execute("UPDATE stock_counts SET status = ?, closed_by = ?, closed_at = ? WHERE id = ?", (POSTED, user.id, stamp, count_id))
        short = sum(-d[3] for d in diffs if d[3] < 0)
        over = sum(d[3] for d in diffs if d[3] > 0)
        audit(conn, user, "STOCK_COUNT_POST", "stock_count", count_id,
              summary=f"{count['count_number']} posted: {len(diffs)} products changed, {fmt_qty(short)} missing, {fmt_qty(over)} more than the books")
    return len(diffs)


def cancel_count(conn, user: User, count_id: int, reason: str) -> None:
    require(user, "count.manage")
    reason = (reason or "").strip()
    if not reason:
        raise POError("Give a reason for cancelling the count.")
    with tx(conn):
        count = _count(conn, count_id)
        _check_place(conn, user, count["location_code"])
        if count["status"] not in (COUNTING, AGREED):
            raise POError("This stock count is already finished.")
        if count["status"] == AGREED and not user.can("stock.post"):
            raise POError("Both counts agree on this one, so only management can cancel it.")
        conn.execute("UPDATE stock_counts SET status = ?, closed_by = ?, closed_at = ?, cancel_reason = ? WHERE id = ?", (CANCELLED, user.id, timestamp(), reason, count_id))
        audit(conn, user, "STOCK_COUNT_CANCEL", "stock_count", count_id, summary=f"{count['count_number']} cancelled: {reason}")


# ------------------------------------------------------------------ adjustments

def _adjustment_rows(reason: str, product, qty_text: str, bucket: str | None) -> list[tuple[str, int]]:
    """(bucket, signed quantity) for one product on an adjustment, from the reason: removing, adding or moving good stock to damaged."""
    if reason == "OTHER":
        qty = _qty(qty_text, product, signed=True)
        if bucket not in (GOOD, DAMAGED):
            raise POError("Choose good or damaged stock.")
        return [(bucket, qty)] if qty else []
    qty = _qty(qty_text, product)
    if not qty:
        return []
    if reason == "FOUND":
        return [(GOOD, qty)]
    if reason == "DAMAGED":
        return [(GOOD, -qty), (DAMAGED, qty)]
    chosen = bucket or (DAMAGED if reason == "WRITE_OFF" else GOOD)
    if chosen not in (GOOD, DAMAGED):
        raise POError("Choose good or damaged stock.")
    return [(chosen, -qty)]


def prepare_adjustment(conn, user: User, location: int, reason: str, lines: list[dict], note: str = "") -> int:
    """lines are {product_id, qty, bucket}. Waits for management's approval before it changes the books."""
    require(user, "adjust.prepare")
    if reason not in REASONS:
        raise POError("Choose a reason.")
    note = (note or "").strip()
    if reason == "OTHER" and not note:
        raise POError("Say what happened, since the reason is Other.")
    with tx(conn):
        _check_place(conn, user, location)
        rows: dict[tuple[int, str], int] = {}
        for line in lines:
            p = _product(conn, line["product_id"])
            for bucket, qty in _adjustment_rows(reason, p, line.get("qty", ""), line.get("bucket")):
                if (p["id"], bucket) in rows:
                    raise POError(f"{p['item_code']} is on the adjustment twice.")
                rows[(p["id"], bucket)] = qty
        if not rows:
            raise POError("Type a quantity for at least one product.")
        number = _number(conn, "SA")
        adj_id = conn.execute("INSERT INTO stock_adjustments(adj_number, location_code, reason, note, status, prepared_by, prepared_at) VALUES (?,?,?,?,?,?,?)",
                              (number, location, reason, note, PENDING, user.id, timestamp())).lastrowid
        conn.executemany("INSERT INTO stock_adjustment_lines(adj_id, product_id, bucket, qty_m) VALUES (?,?,?,?)", [(adj_id, pid, b, q) for (pid, b), q in rows.items()])
        audit(conn, user, "STOCK_ADJUST_PREPARE", "stock_adjustment", adj_id,
              summary=f"{number} at {place_name(conn, location)}: {REASONS[reason]}, {len({k[0] for k in rows})} products" + (f". {note}" if note else ""))
    return adj_id


def _adjustment(conn, adj_id: int):
    row = conn.execute("SELECT * FROM stock_adjustments WHERE id = ?", (adj_id,)).fetchone()
    if row is None:
        raise POError("That adjustment doesn't exist.")
    return row


def decide_adjustment(conn, user: User, adj_id: int, approve: bool, note: str = "") -> None:
    """Approving puts the adjustment on the books. Rejecting needs a reason, which the person who prepared it sees."""
    require(user, "stock.post")
    note = (note or "").strip()
    if not approve and not note:
        raise POError("Give a reason for rejecting it.")
    with tx(conn):
        adj = _adjustment(conn, adj_id)
        _check_place(conn, user, adj["location_code"])
        if adj["status"] != PENDING:
            raise POError("This adjustment was already decided.")
        if adj["prepared_by"] == user.id:
            raise POError("You prepared this adjustment, so someone else approves it.")
        if approve:
            in_way = count_in_the_way(conn, adj["location_code"], adj["prepared_at"],
                                      [r[0] for r in conn.execute("SELECT product_id FROM stock_adjustment_lines WHERE adj_id = ?", (adj_id,))])
            if in_way:
                raise POError(_count_words(conn, in_way, adj["location_code"]) if in_way["status"] in (COUNTING, AGREED) else
                              f"Stock count {in_way['count_number']} at {place_name(conn, adj['location_code'])} has counted these goods since this "
                              "adjustment was prepared, so it already took the change in. Reject this one.")
        stamp = timestamp()
        conn.execute("UPDATE stock_adjustments SET status = ?, decided_by = ?, decided_at = ?, decision_note = ? WHERE id = ?",
                     (APPROVED if approve else REJECTED, user.id, stamp, note, adj_id))
        if approve:
            conn.executemany("INSERT INTO stock_movements(location_code, product_id, bucket, qty_m, kind, ref_id, moved_by, moved_at) VALUES (?,?,?,?,?,?,?,?)",
                             [(adj["location_code"], r["product_id"], r["bucket"], r["qty_m"], "ADJUSTMENT", adj_id, user.id, stamp)
                              for r in conn.execute("SELECT * FROM stock_adjustment_lines WHERE adj_id = ?", (adj_id,))])
        audit(conn, user, "STOCK_ADJUST_APPROVE" if approve else "STOCK_ADJUST_REJECT", "stock_adjustment", adj_id,
              summary=f"{adj['adj_number']} {'approved' if approve else 'rejected'}" + (f": {note}" if note else ""))


def adjustments(conn, user: User, status: str | None = None) -> list[dict]:
    if not (user.can("adjust.prepare") or user.can("stock.post") or user.can("inventory.view")):
        require(user, "adjust.prepare")
    places = scope(user)
    rows = conn.execute("SELECT a.*, u.full_name AS prepared_name, d.full_name AS decided_name FROM stock_adjustments a JOIN users u ON u.id = a.prepared_by"
                        " LEFT JOIN users d ON d.id = a.decided_by" + (" WHERE a.status = ?" if status else "") + " ORDER BY a.id DESC", (status,) if status else ()).fetchall()
    return [{**dict(r), "location_name": place_name(conn, r["location_code"]), "reason_label": REASONS[r["reason"]], "status_label": ADJUST_LABELS[r["status"]]}
            for r in rows if places is None or r["location_code"] in places]


def adjustment_lines(conn, user: User, adj_id: int) -> list[dict]:
    adj = _adjustment(conn, adj_id)
    if not (user.can("adjust.prepare") or user.can("stock.post") or user.can("inventory.view")):
        require(user, "adjust.prepare")
    _check_place(conn, user, adj["location_code"])
    show_cost = user.can("inventory.view")
    return [{"item_code": r["item_code"], "description": r["description"], "size": r["size"], "color": r["color"], "unit": unit(r), "bucket": r["bucket"],
             "qty_m": r["qty_m"], "value_c": value_c(r["qty_m"], r) if show_cost else None}
            for r in conn.execute("SELECT l.bucket, l.qty_m, p.* FROM stock_adjustment_lines l JOIN products p ON p.id = l.product_id WHERE l.adj_id = ? ORDER BY p.item_code, l.bucket", (adj_id,))]


def adjustments_waiting(conn, user: User) -> int:
    if not user.can("stock.post"):
        return 0
    places = scope(user)
    return sum(1 for r in conn.execute("SELECT location_code, prepared_by FROM stock_adjustments WHERE status = 'PENDING'")
               if (places is None or r[0] in places) and r[1] != user.id)


# ------------------------------------------------------------------ transfers

def _transfer(conn, transfer_id: int):
    row = conn.execute("SELECT * FROM stock_transfers WHERE id = ?", (transfer_id,)).fetchone()
    if row is None:
        raise POError("That transfer doesn't exist.")
    return row


def send_transfer(conn, user: User, from_code: int, to_code: int, lines: dict[int, str], note: str = "") -> int:
    """Good stock leaving one place for another. It leaves the books here now and reaches the other place's books when they count it in."""
    require(user, "transfer.send")
    with tx(conn):
        _check_place(conn, user, from_code)
        if to_code == from_code:
            raise POError("Choose where the goods are going.")
        if to_code != WAREHOUSE and conn.execute("SELECT 1 FROM branches WHERE code = ? AND kind = 'BRANCH' AND active = 1", (to_code,)).fetchone() is None:
            raise POError("Choose an open branch to send to.")
        rows = []
        books = book(conn, from_code)
        for pid, text in lines.items():
            p = _product(conn, pid)
            qty = _qty(text, p)
            if qty:
                if qty > books.get(pid, 0):
                    raise POError(f"{p['item_code']}: that's more than {place_name(conn, from_code)} has for sale on the books. Count it first, or send less.")
                rows.append((pid, qty))
        if not rows:
            raise POError("Type a quantity for at least one product.")
        number = _number(conn, "ST")
        stamp = timestamp()
        transfer_id = conn.execute("INSERT INTO stock_transfers(transfer_number, from_code, to_code, status, note, sent_by, sent_at) VALUES (?,?,?,?,?,?,?)",
                                   (number, from_code, to_code, SENT, (note or "").strip(), user.id, stamp)).lastrowid
        conn.executemany("INSERT INTO stock_transfer_lines(transfer_id, product_id, sent_m) VALUES (?,?,?)", [(transfer_id, pid, q) for pid, q in rows])
        conn.executemany("INSERT INTO stock_movements(location_code, product_id, bucket, qty_m, kind, ref_id, moved_by, moved_at) VALUES (?,?,?,?,?,?,?,?)",
                         [(from_code, pid, GOOD, -q, "TRANSFER_OUT", transfer_id, user.id, stamp) for pid, q in rows])
        audit(conn, user, "STOCK_TRANSFER_SEND", "stock_transfer", transfer_id,
              summary=f"{number}: {len(rows)} products from {place_name(conn, from_code)} to {place_name(conn, to_code)}")
    return transfer_id


def receive_transfer(conn, user: User, transfer_id: int, figures: dict[int, str]) -> dict:
    """The place it went to counts what arrived, without seeing what was sent. What didn't arrive is a loss on the way, shown in the audit."""
    require(user, "transfer.receive")
    with tx(conn):
        t = _transfer(conn, transfer_id)
        if scope(user) is None:
            raise POError("Only someone working at the place it went to counts a transfer in.")
        _check_place(conn, user, t["to_code"])
        if t["status"] != SENT:
            raise POError("This transfer is already finished.")
        if t["sent_by"] == user.id:
            raise POError("You sent this transfer, so someone at the other end counts it in.")
        busy = conn.execute("SELECT count_number FROM stock_counts WHERE location_code = ? AND status IN ('COUNTING','AGREED')", (t["to_code"],)).fetchone()
        if busy:
            raise POError(f"{place_name(conn, t['to_code'])} is being counted on {busy[0]}. Keep these goods apart and count them in once the count is posted.")
        lines = {r["product_id"]: r for r in conn.execute("SELECT l.*, p.item_code, p.uom, p.pieces_per_unit FROM stock_transfer_lines l JOIN products p ON p.id = l.product_id WHERE l.transfer_id = ?",
                                                         (transfer_id,))}
        missing = [r["item_code"] for pid, r in lines.items() if str(figures.get(pid, "")).strip() == ""]
        if missing:
            raise POError(NEED_FIGURE + f" {missing[0]} has none.")
        stamp = timestamp()
        got = {pid: _qty(figures[pid], r) for pid, r in lines.items()}
        for pid, qty in got.items():
            conn.execute("UPDATE stock_transfer_lines SET received_m = ? WHERE transfer_id = ? AND product_id = ?", (qty, transfer_id, pid))
        # What arrived beyond what was sent isn't added: no paper made it, so the next count finds it, and the office sees it as over.
        conn.executemany("INSERT INTO stock_movements(location_code, product_id, bucket, qty_m, kind, ref_id, moved_by, moved_at) VALUES (?,?,?,?,?,?,?,?)",
                         [(t["to_code"], pid, GOOD, min(q, lines[pid]["sent_m"]), "TRANSFER_IN", transfer_id, user.id, stamp) for pid, q in got.items() if q])
        conn.execute("UPDATE stock_transfers SET status = ?, closed_by = ?, closed_at = ? WHERE id = ?", (RECEIVED, user.id, stamp, transfer_id))
        short = sum(max(r["sent_m"] - got[pid], 0) for pid, r in lines.items())
        over = sum(max(got[pid] - r["sent_m"], 0) for pid, r in lines.items())
        audit(conn, user, "STOCK_TRANSFER_RECEIVE", "stock_transfer", transfer_id,
              summary=f"{t['transfer_number']} received at {place_name(conn, t['to_code'])}"
              + (f", {fmt_qty(short)} lost on the way" if short else "") + (f", {fmt_qty(over)} more than sent" if over else ""))
    return {"short_m": short, "over_m": over}


def cancel_transfer(conn, user: User, transfer_id: int, reason: str) -> None:
    """Management only, while it's still on the way: the goods go back on the sender's books."""
    require(user, "stock.post")
    reason = (reason or "").strip()
    if not reason:
        raise POError("Give a reason for cancelling the transfer.")
    with tx(conn):
        t = _transfer(conn, transfer_id)
        if t["status"] != SENT:
            raise POError("Only a transfer still on the way can be cancelled.")
        if t["sent_by"] == user.id:
            raise POError("You sent this transfer, so someone else cancels it.")
        in_way = count_in_the_way(conn, t["from_code"], t["sent_at"],
                                  [r[0] for r in conn.execute("SELECT product_id FROM stock_transfer_lines WHERE transfer_id = ?", (transfer_id,))])
        if in_way:
            raise POError(_count_words(conn, in_way, t["from_code"]) if in_way["status"] in (COUNTING, AGREED) else
                          f"Stock count {in_way['count_number']} at {place_name(conn, t['from_code'])} has counted since this was sent. "
                          "Have the other place count it in (0 if nothing came) instead.")
        stamp = timestamp()
        conn.executemany("INSERT INTO stock_movements(location_code, product_id, bucket, qty_m, kind, ref_id, moved_by, moved_at) VALUES (?,?,?,?,?,?,?,?)",
                         [(t["from_code"], r["product_id"], GOOD, r["sent_m"], "TRANSFER_BACK", transfer_id, user.id, stamp)
                          for r in conn.execute("SELECT * FROM stock_transfer_lines WHERE transfer_id = ?", (transfer_id,))])
        conn.execute("UPDATE stock_transfers SET status = ?, closed_by = ?, closed_at = ?, cancel_reason = ? WHERE id = ?", (CANCELLED, user.id, stamp, reason, transfer_id))
        audit(conn, user, "STOCK_TRANSFER_CANCEL", "stock_transfer", transfer_id, summary=f"{t['transfer_number']} cancelled: {reason}")


def transfers(conn, user: User, status: str | None = None) -> list[dict]:
    if not (user.can("transfer.send") or user.can("transfer.receive") or user.can("inventory.view")):
        require(user, "transfer.send")
    places = scope(user)
    rows = conn.execute("SELECT t.*, u.full_name AS sent_name, c.full_name AS closed_name FROM stock_transfers t JOIN users u ON u.id = t.sent_by"
                        " LEFT JOIN users c ON c.id = t.closed_by" + (" WHERE t.status = ?" if status else "") + " ORDER BY t.id DESC", (status,) if status else ()).fetchall()
    return [{**dict(r), "from_name": place_name(conn, r["from_code"]), "to_name": place_name(conn, r["to_code"]), "status_label": TRANSFER_LABELS[r["status"]],
             "to_receive": r["status"] == SENT and (places is None or r["to_code"] in places) and r["sent_by"] != user.id and user.can("transfer.receive")}
            for r in rows if places is None or r["from_code"] in places or r["to_code"] in places]


def transfer_lines(conn, user: User, transfer_id: int) -> list[dict]:
    """The products on a transfer. Whoever counts it in doesn't see what was sent until they've saved their count."""
    if not (user.can("transfer.send") or user.can("transfer.receive") or user.can("inventory.view")):
        require(user, "transfer.send")
    t = _transfer(conn, transfer_id)
    places = scope(user)
    if places is not None and t["from_code"] not in places and t["to_code"] not in places:
        raise POError(NOT_YOURS)
    blind = t["status"] == SENT and not user.can("inventory.view") and t["sent_by"] != user.id and (places is None or t["from_code"] not in places)
    return [{"product_id": r["product_id"], "item_code": r["item_code"], "description": r["description"], "size": r["size"], "color": r["color"], "unit": unit(r),
             "sent_m": None if blind else r["sent_m"], "received_m": r["received_m"]}
            for r in conn.execute("SELECT l.*, p.item_code, p.description, p.size, p.color, p.uom, p.pieces_per_unit FROM stock_transfer_lines l JOIN products p ON p.id = l.product_id"
                                  " WHERE l.transfer_id = ? ORDER BY p.item_code", (transfer_id,))]


def products_for(conn, search: str, limit: int = 30) -> list[dict]:
    """Active catalog products matching every word typed, for picking lines on an adjustment or transfer."""
    words = (search or "").strip().split()
    sql = "SELECT id, item_code, description, size, color, uom, pieces_per_unit FROM products WHERE active = 1"
    args: list = []
    for w in words:
        sql += " AND (item_code LIKE ? OR description LIKE ? OR brand LIKE ?)"
        args += [f"%{w}%"] * 3
    return [{**dict(r), "unit": unit(r)} for r in conn.execute(sql + " ORDER BY item_code LIMIT ?", (*args, limit))]
