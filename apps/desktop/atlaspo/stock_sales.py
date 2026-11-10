"""Inventory, phase 2: sales and customer returns, and the reports that read them.

Rules:
- A sales paper is one place, one day: what was sold there, typed in or imported from a POS file, or what customers brought back.
  Quantities are in each product's stock unit (inventory.unit). Stock comes off the books the day it's recorded (inventory.MOVES reads it).
- Each line keeps what one unit sold for (typed, from the file, or else the catalog selling price) and what it cost then (the last purchase
  cost), so sales amounts and gross profit never change when prices do later.
- Customer returns go back to stock for sale, or to damaged stock. A return with no price typed is valued at what the product last sold
  for at that place, so it takes back what was taken in.
- Papers that could hide missing stock wait for management (ADMIN or APPROVER, never the person who recorded them) before they touch the
  books: a sale line below half the catalog price (or at nothing), and any customer return to stock for sale. The rest go straight through.
- A sale can't be dated before a stock count that has started at that place and covers the product: the count already took in what was
  sold up to then. Nor can it be dated in the future. For the same reason, sales a later count has taken in can't be cancelled.
- The same POS file can't be imported twice for the same place.
- Recorded sales are permanent. Management cancels a wrong paper with a reason, and it is recorded again. Nobody cancels their own.
- Branch staff record their own branch only and never see costs or profit.
"""
from __future__ import annotations

import csv
import hashlib
import io
from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from . import inventory as inv
from .auth import User, require
from .db import POError, audit, get_setting, timestamp, today, tx
from .money import fmt_peso, parse_pesos

SALE, RETURN = "SALE", "RETURN"
POSTED, CANCELLED = "POSTED", "CANCELLED"
WAITING, REJECTED = "WAITING", "REJECTED"  # a held paper's state, from sale_holds
KIND_LABELS = {SALE: "Sales", RETURN: "Customer return"}
SOURCE_LABELS = {"ENTRY": "Typed in", "IMPORT": "POS file"}
PREFIX = {SALE: "SL", RETURN: "RT"}
MAX_FILE_BYTES = 20 * 1024 * 1024

# Column headings a POS file may use, folded to lower case without spaces or punctuation.
_SKU = ("sku", "itemcode", "code", "productcode", "item", "barcode", "plu")
_QTY = ("qty", "quantity", "qtysold", "sold", "pcs", "pieces", "units")
_PRICE = ("price", "unitprice", "sellingprice", "srp")
_AMOUNT = ("amount", "total", "netamount", "sales", "lineamount", "netsales")


def _round(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def list_price_c(product) -> int:
    """The catalog selling price of one stock unit, or 0 when the product has none."""
    if product["selling_c"] is None:
        return 0
    return _round(Decimal(product["selling_c"]) / (product["pieces_per_unit"] or 1))


def _cost_c(product) -> int | None:
    cost = inv.piece_cost_c(product)
    return None if cost is None else _round(cost)


def _clean_date(day: str) -> str:
    try:
        when = date.fromisoformat(str(day))
    except (TypeError, ValueError):
        raise POError("Choose the day of the sales.") from None
    if when > today():
        raise POError("Sales can't be recorded for a day that hasn't come yet.")
    return when.isoformat()


def _later_count(conn, location: int, after: str, product_ids) -> dict | None:
    """A stock count at this place, not cancelled, started after `after` (a day or a moment), that covers any of these products."""
    depts = {r[0] for r in conn.execute(f"SELECT DISTINCT department_id FROM products WHERE id IN ({','.join('?' * len(product_ids))})", list(product_ids))} if product_ids else set()
    for c in conn.execute("SELECT count_number, department_id, substr(opened_at, 1, 10) AS day FROM stock_counts WHERE location_code = ? AND status <> 'CANCELLED'"
                          " AND opened_at > ? ORDER BY opened_at", (location, after)):
        if c["department_id"] is None or c["department_id"] in depts:
            return dict(c)
    return None


def _check_date(conn, location: int, day: str, product_ids) -> None:
    count = _later_count(conn, location, day + " 99", product_ids)
    if count:
        raise POError(f"Stock count {count['count_number']} at {inv.place_name(conn, location)} started on {count['day']}, so it already took in "
                      f"what was sold before then. Record sales from that day on.")


def _held_back(kind: str, lines: dict[int, tuple], products: dict) -> str:
    """Why a paper must wait for management, or '' when it goes straight through."""
    if kind == RETURN:
        return "Customer return to stock for sale" if any(r[0] == inv.GOOD for r in lines.values()) else ""
    for pid, (_bucket, _qty, price, _amount, _cost) in lines.items():
        listed = list_price_c(products[pid])
        if price == 0 or (listed and price * 2 < listed):
            return f"{products[pid]['item_code']} sold below half its catalog price"
    return ""


def _last_price_c(conn, location: int, product_id: int) -> int | None:
    row = conn.execute("SELECT l.price_c FROM stock_sale_lines l JOIN stock_sales s ON s.id = l.sale_id WHERE s.kind = 'SALE' AND s.status = 'POSTED'"
                       " AND s.location_code = ? AND l.product_id = ? ORDER BY s.sale_date DESC, s.id DESC LIMIT 1", (location, product_id)).fetchone()
    return row[0] if row else None


def _number(conn, kind: str) -> str:
    year = today().year
    conn.execute("INSERT INTO sale_number_sequences(kind, year, last_number) VALUES (?, ?, 1) ON CONFLICT(kind, year) DO UPDATE SET last_number = last_number + 1", (kind, year))
    seq = conn.execute("SELECT last_number FROM sale_number_sequences WHERE kind = ? AND year = ?", (kind, year)).fetchone()[0]
    return f"{year}-{PREFIX[kind]}-{seq:05d}"


def record(conn, user: User, location: int, day: str, lines: list[dict], kind: str = SALE, note: str = "",
           source: str = "ENTRY", file_name: str = "", file_sha256: str = "") -> int:
    """lines are {product_id, qty, price (optional, one unit), amount (optional, the whole line), bucket (returns only)}. Returns the paper's id."""
    require(user, "sales.record")
    if kind not in (SALE, RETURN):
        raise POError("Choose sales or a customer return.")
    with tx(conn):
        inv._check_place(conn, user, location)
        day = _clean_date(day)
        if file_sha256 and conn.execute("SELECT s.sale_number FROM stock_sales s WHERE s.file_sha256 = ? AND s.location_code = ? AND s.status = 'POSTED'"
                                        " AND NOT EXISTS (SELECT 1 FROM sale_holds h WHERE h.sale_id = s.id AND h.decision = 'REJECTED')",
                                        (file_sha256, location)).fetchone():
            raise POError(f"That file was already imported for {inv.place_name(conn, location)}.")
        rows: dict[int, tuple] = {}
        products: dict[int, object] = {}
        for line in lines:
            p = inv._product(conn, line["product_id"])
            products[p["id"]] = p
            qty = inv._qty(line.get("qty", ""), p)
            if not qty:
                continue
            if p["id"] in rows:
                raise POError(f"{p['item_code']} is on the paper twice.")
            bucket = line.get("bucket") or inv.GOOD
            if bucket not in (inv.GOOD, inv.DAMAGED) or (kind == SALE and bucket != inv.GOOD):
                raise POError("Sales come out of stock for sale. Choose stock for sale or damaged for a return.")
            try:
                amount_text, price_text = str(line.get("amount") or "").strip(), str(line.get("price") or "").strip()
                if amount_text:
                    amount = parse_pesos(amount_text)
                    price = _round(Decimal(amount) * 1000 / qty)
                else:
                    if price_text:
                        price = parse_pesos(price_text)
                    elif kind == RETURN:
                        last = _last_price_c(conn, location, p["id"])
                        price = list_price_c(p) if last is None else last
                    else:
                        price = list_price_c(p)
                    amount = _round(Decimal(price) * qty / 1000)
            except ValueError:
                raise POError(f"{p['item_code']}: the price must be an amount in pesos.") from None
            if price < 0 or amount < 0:
                raise POError(f"{p['item_code']}: a price can't be below zero.")
            rows[p["id"]] = (bucket, qty, price, amount, _cost_c(p))
        if not rows:
            raise POError("Type a quantity for at least one product.")
        _check_date(conn, location, day, rows)
        number = _number(conn, kind)
        sale_id = conn.execute("INSERT INTO stock_sales(sale_number, kind, location_code, sale_date, source, file_name, file_sha256, note, status, entered_by, entered_at)"
                               " VALUES (?,?,?,?,?,?,?,?,?,?,?)", (number, kind, location, day, source, file_name, file_sha256, (note or "").strip(), POSTED, user.id, timestamp())).lastrowid
        hold = _held_back(kind, rows, products)
        if hold:
            conn.execute("INSERT INTO sale_holds(sale_id, reason) VALUES (?, ?)", (sale_id, hold))
        conn.executemany("INSERT INTO stock_sale_lines(sale_id, product_id, bucket, qty_m, price_c, amount_c, cost_c) VALUES (?,?,?,?,?,?,?)",
                         [(sale_id, pid, *r) for pid, r in rows.items()])
        total = sum(r[3] for r in rows.values())
        audit(conn, user, "SALES_RECORD" if kind == SALE else "SALES_RETURN", "stock_sale", sale_id,
              summary=f"{number}: {KIND_LABELS[kind].lower()} at {inv.place_name(conn, location)} on {day}, {len(rows)} products, {fmt_peso(total)}"
              + (f", from {file_name}" if file_name else "") + (f". Waiting for approval: {hold}" if hold else ""))
    return sale_id


def is_waiting(conn, sale_id: int) -> bool:
    return conn.execute("SELECT 1 FROM sale_holds WHERE sale_id = ? AND decision IS NULL", (sale_id,)).fetchone() is not None


def decide(conn, user: User, sale_id: int, approve: bool, note: str = "") -> None:
    """Management decides a paper that is waiting. Approving puts it on the books; rejecting needs a reason and keeps it off for good."""
    require(user, "stock.post")
    note = (note or "").strip()
    if not approve and not note:
        raise POError("Give a reason for rejecting it.")
    with tx(conn):
        sale = _sale(conn, sale_id)
        inv._check_place(conn, user, sale["location_code"])
        hold = conn.execute("SELECT * FROM sale_holds WHERE sale_id = ?", (sale_id,)).fetchone()
        if hold is None or hold["decision"] is not None or sale["status"] != POSTED:
            raise POError("These sales aren't waiting for approval.")
        if sale["entered_by"] == user.id:
            raise POError("You recorded these, so someone else approves them.")
        if approve:
            _check_date(conn, sale["location_code"], sale["sale_date"], [r[0] for r in conn.execute("SELECT product_id FROM stock_sale_lines WHERE sale_id = ?", (sale_id,))])
        conn.execute("UPDATE sale_holds SET decision = ?, decided_by = ?, decided_at = ?, decision_note = ? WHERE sale_id = ?",
                     ("APPROVED" if approve else "REJECTED", user.id, timestamp(), note, sale_id))
        audit(conn, user, "SALES_APPROVE" if approve else "SALES_REJECT", "stock_sale", sale_id,
              summary=f"{sale['sale_number']} {'approved' if approve else 'rejected'}" + (f": {note}" if note else ""))


def waiting(conn, user: User) -> int:
    """How many papers wait for this person's approval."""
    if not user.can("stock.post"):
        return 0
    places = inv.scope(user)
    return sum(1 for r in conn.execute("SELECT s.location_code, s.entered_by FROM stock_sales s JOIN sale_holds h ON h.sale_id = s.id"
                                       " WHERE h.decision IS NULL AND s.status = 'POSTED'")
               if (places is None or r[0] in places) and r[1] != user.id)


def read_file(conn, data: bytes) -> dict:
    """A POS export (CSV) as {lines, unmatched, sha256}. Needs a SKU column and a quantity column; a price or amount column is used when there.
    Lines for the same SKU are added together. Products not in the catalog are listed, not guessed."""
    if len(data) > MAX_FILE_BYTES:
        raise POError("That file is too large to be a day's sales.")
    text = None
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise POError("That file can't be read as text. Export it from the POS as CSV.")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    if not rows:
        raise POError("That file is empty.")
    heads = ["".join(ch for ch in h.lower() if ch.isalnum()) for h in rows[0]]

    def col(names):
        return next((i for i, h in enumerate(heads) if h in names), None)
    sku, qty, price, amount = col(_SKU), col(_QTY), col(_PRICE), col(_AMOUNT)
    if sku is None or qty is None:
        raise POError("The file needs a column for the SKU (or item code) and one for the quantity, with headings on the first line.")
    merged: dict[int, dict] = {}
    unmatched: dict[str, Decimal] = defaultdict(Decimal)
    for n, row in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in row):
            continue
        cell = lambda i: row[i].strip() if i is not None and i < len(row) else ""  # noqa: E731
        code, q = cell(sku), cell(qty).replace(",", "")
        try:
            quantity = Decimal(q or "0")
        except ArithmeticError:
            raise POError(f"Line {n}: '{cell(qty)}' isn't a quantity.") from None
        if not code or quantity == 0:
            continue
        p = inv.find_product(conn, code)
        if p is None:
            unmatched[code] += quantity
            continue
        line = merged.setdefault(p["id"], {"product_id": p["id"], "item_code": p["item_code"], "description": p["description"], "unit": inv.unit(p),
                                           "qty": Decimal(0), "amount": Decimal(0), "priced": False})
        line["qty"] += quantity
        try:
            if amount is not None and cell(amount):
                line["amount"] += Decimal(cell(amount).replace(",", "").replace("₱", ""))
                line["priced"] = True
            elif price is not None and cell(price):
                line["amount"] += Decimal(cell(price).replace(",", "").replace("₱", "")) * quantity
                line["priced"] = True
        except ArithmeticError:
            raise POError(f"Line {n}: the price or amount isn't a number.") from None
    lines = []
    for line in merged.values():
        if line["qty"] < 0:
            raise POError(f"{line['item_code']}: the file sells less than nothing in total. Record customer returns on their own.")
        lines.append({**line, "qty": f"{line['qty']:f}", "amount": f"{line['amount']:.2f}" if line["priced"] else ""})
    return {"lines": lines, "unmatched": [{"code": c, "qty": f"{q:f}"} for c, q in unmatched.items()], "sha256": hashlib.sha256(data).hexdigest()}


def import_file(conn, user: User, location: int, day: str, data: bytes, file_name: str, note: str = "") -> dict:
    """Records a POS file's matched lines as one sales paper. Returns {sale_id, unmatched}."""
    read = read_file(conn, data)
    if not read["lines"]:
        raise POError("Nothing in that file matches a catalog product.")
    sale_id = record(conn, user, location, day, read["lines"], SALE, note, "IMPORT", file_name, read["sha256"])
    return {"sale_id": sale_id, "unmatched": read["unmatched"]}


def cancel(conn, user: User, sale_id: int, reason: str) -> None:
    """Management only. The stock goes back as if the paper was never recorded; record it again correctly."""
    require(user, "stock.post")
    reason = (reason or "").strip()
    if not reason:
        raise POError("Give a reason for cancelling.")
    with tx(conn):
        sale = _sale(conn, sale_id)
        inv._check_place(conn, user, sale["location_code"])
        if sale["status"] != POSTED:
            raise POError("These sales are already cancelled.")
        hold = conn.execute("SELECT decision FROM sale_holds WHERE sale_id = ?", (sale_id,)).fetchone()
        if hold is not None and hold[0] != "APPROVED":
            raise POError("These sales never reached the books. Reject them instead." if hold[0] is None else "These sales were rejected, so they never reached the books.")
        if sale["entered_by"] == user.id:
            raise POError("You recorded these, so someone else cancels them.")
        moment = min(sale["sale_date"] + " 23:59:59", sale["entered_at"])
        count = _later_count(conn, sale["location_code"], moment, [r[0] for r in conn.execute("SELECT product_id FROM stock_sale_lines WHERE sale_id = ?", (sale_id,))])
        if count:
            raise POError(f"Stock count {count['count_number']} has started at {inv.place_name(conn, sale['location_code'])} since, and it already took these "
                          "sales in. Put the books right with an adjustment instead.")
        conn.execute("UPDATE stock_sales SET status = ?, cancelled_by = ?, cancelled_at = ?, cancel_reason = ? WHERE id = ?", (CANCELLED, user.id, timestamp(), reason, sale_id))
        audit(conn, user, "SALES_CANCEL", "stock_sale", sale_id, summary=f"{sale['sale_number']} cancelled: {reason}")


def _sale(conn, sale_id: int):
    row = conn.execute("SELECT * FROM stock_sales WHERE id = ?", (sale_id,)).fetchone()
    if row is None:
        raise POError("Those sales don't exist.")
    return row


def papers(conn, user: User, status: str | None = None, location: int | None = None, date_from: str | None = None, date_to: str | None = None) -> list[dict]:
    """Sales and return papers, newest first, at this person's places."""
    if not (user.can("sales.record") or user.can("inventory.view")):
        require(user, "sales.record")
    places = inv.scope(user)
    where, args = [], []
    for cond, value in (("s.status = ?", status), ("s.location_code = ?", location), ("s.sale_date >= ?", date_from), ("s.sale_date <= ?", date_to)):
        if value is not None:
            where.append(cond)
            args.append(value)
    rows = conn.execute("SELECT s.*, u.full_name AS entered_name, c.full_name AS cancelled_name,"
                        " (SELECT COUNT(*) FROM stock_sale_lines l WHERE l.sale_id = s.id) AS lines,"
                        " (SELECT COALESCE(SUM(amount_c), 0) FROM stock_sale_lines l WHERE l.sale_id = s.id) AS total_c,"
                        " h.reason AS hold_reason, h.decision AS hold_decision, h.decision_note AS hold_note, h.decided_at AS hold_decided_at, d.full_name AS hold_decided_name"
                        " FROM stock_sales s JOIN users u ON u.id = s.entered_by LEFT JOIN users c ON c.id = s.cancelled_by"
                        " LEFT JOIN sale_holds h ON h.sale_id = s.id LEFT JOIN users d ON d.id = h.decided_by"
                        + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY s.sale_date DESC, s.id DESC", args).fetchall()
    out = []
    for r in rows:
        if places is not None and r["location_code"] not in places:
            continue
        state = (CANCELLED if r["status"] == CANCELLED else WAITING if r["hold_reason"] is not None and r["hold_decision"] is None
                 else REJECTED if r["hold_decision"] == "REJECTED" else POSTED)
        out.append({**dict(r), "location_name": inv.place_name(conn, r["location_code"]), "kind_label": KIND_LABELS[r["kind"]],
                    "source_label": SOURCE_LABELS[r["source"]], "state": state, "on_books": state == POSTED})
    return out


def paper_lines(conn, user: User, sale_id: int) -> list[dict]:
    if not (user.can("sales.record") or user.can("inventory.view")):
        require(user, "sales.record")
    sale = _sale(conn, sale_id)
    places = inv.scope(user)
    if places is not None and sale["location_code"] not in places:
        raise POError(inv.NOT_YOURS)
    show_cost = user.can("inventory.view")
    out = []
    for r in conn.execute("SELECT l.*, p.item_code, p.description, p.size, p.color, p.uom, p.pieces_per_unit FROM stock_sale_lines l JOIN products p ON p.id = l.product_id"
                          " WHERE l.sale_id = ? ORDER BY p.item_code", (sale_id,)):
        cost = None if r["cost_c"] is None else _round(Decimal(r["cost_c"]) * r["qty_m"] / 1000)
        out.append({"item_code": r["item_code"], "description": r["description"], "size": r["size"], "color": r["color"], "unit": inv.unit(r),
                    "bucket": r["bucket"], "qty_m": r["qty_m"], "price_c": r["price_c"], "amount_c": r["amount_c"],
                    "cost_c": cost if show_cost else None, "profit_c": (r["amount_c"] - cost) if show_cost and cost is not None else None})
    return out


# ------------------------------------------------------------------ reports

def _sold(conn, date_from: str, date_to: str, location: int | None = None) -> list:
    """Net sales per place and product between two days: sold less returned, with amounts and cost."""
    where = (f"s.status = 'POSTED' AND {inv._NOT_HELD_BACK} AND s.sale_date BETWEEN ? AND ?" + (" AND s.location_code = ?" if location is not None else ""))
    args = [date_from, date_to] + ([location] if location is not None else [])
    out: dict[tuple[int, int], dict] = {}
    for r in conn.execute(f"SELECT s.location_code, s.kind, s.sale_date, l.product_id, l.qty_m, l.amount_c, l.cost_c FROM stock_sales s JOIN stock_sale_lines l ON l.sale_id = s.id WHERE {where}", args):
        o = out.setdefault((r["location_code"], r["product_id"]), {"location_code": r["location_code"], "product_id": r["product_id"], "qty_m": 0, "amount_c": 0,
                                                                   "cost": Decimal(0), "no_cost": 0, "last_sold": None})
        sign = 1 if r["kind"] == SALE else -1
        o["qty_m"] += sign * r["qty_m"]
        o["amount_c"] += sign * r["amount_c"]
        if r["cost_c"] is None:
            o["no_cost"] += 1
        else:
            o["cost"] += sign * Decimal(r["cost_c"]) * r["qty_m"] / 1000
        if r["kind"] == SALE and (o["last_sold"] is None or r["sale_date"] > o["last_sold"]):
            o["last_sold"] = r["sale_date"]
    for o in out.values():
        o["cost_c"] = _round(o.pop("cost"))
    return list(out.values())


def sales_report(conn, user: User, date_from: str, date_to: str, location: int | None = None, by: str = "product") -> list[dict]:
    """Net sales between two days, by product or by place: quantity, amount, cost and gross profit."""
    require(user, "inventory.view")
    products = {r["id"]: r for r in conn.execute("SELECT p.*, d.name AS department FROM products p JOIN departments d ON d.id = p.department_id")}
    out: dict = {}
    for r in _sold(conn, date_from, date_to, location):
        p = products[r["product_id"]]
        key = r["location_code"] if by == "place" else r["product_id"]
        o = out.setdefault(key, {"location": r["location_code"], "location_name": inv.place_name(conn, r["location_code"]), "product_id": p["id"],
                                 "item_code": p["item_code"], "description": p["description"], "department": p["department"], "unit": inv.unit(p),
                                 "qty_m": 0, "amount_c": 0, "cost_c": 0, "no_cost": 0})
        o["qty_m"] += r["qty_m"]
        o["amount_c"] += r["amount_c"]
        o["cost_c"] += r["cost_c"]
        o["no_cost"] += r["no_cost"]
    for o in out.values():
        # Without a cost for every line, profit and margin would be overstated, so they're left blank and out of the totals.
        if o["no_cost"]:
            o["cost_c"] = o["profit_c"] = o["margin_bp"] = None
        else:
            o["profit_c"] = o["amount_c"] - o["cost_c"]
            o["margin_bp"] = _round(Decimal(o["profit_c"]) * 10000 / o["amount_c"]) if o["amount_c"] else None
    return sorted(out.values(), key=lambda o: -o["amount_c"])


def daily_sales(conn, location: int, days: int, until: str | None = None) -> dict[int, Decimal]:
    """{product_id: average units sold a day} at one place over the last `days` days. No permission check: services use it."""
    end = date.fromisoformat(until) if until else today()
    start = end - timedelta(days=days - 1)
    return {r["product_id"]: Decimal(max(r["qty_m"], 0)) / days for r in _sold(conn, start.isoformat(), end.isoformat(), location)}


def _setting_days(conn, key: str) -> int:
    try:
        return max(int(get_setting(conn, key) or 0), 1)
    except ValueError:
        return 1


def movers(conn, user: User, location: int | None = None) -> list[dict]:
    """Every product in stock or sold lately, per place: what sold over the sales window, per day, what's left and how many days it lasts.
    Fastest first; the slowest and those not moving are at the end."""
    require(user, "inventory.view")
    days, dead = _setting_days(conn, "inventory.sales_days"), _setting_days(conn, "inventory.dead_days")
    end = today()
    sold = {(r["location_code"], r["product_id"]): r for r in _sold(conn, (end - timedelta(days=days - 1)).isoformat(), end.isoformat(), location)}
    last = {(r["location_code"], r["product_id"]): r["last_sold"] for r in _sold(conn, "0000-01-01", end.isoformat(), location)}
    stock = {(r["location"], r["product_id"]): r for r in inv.on_hand(conn, user, location)}
    products = {r["id"]: r for r in conn.execute("SELECT * FROM products")}
    # When good stock first arrived at each place: a product that has never sold is idle from then, not for ever.
    first_in = {(r[0], r[1]): r[2] for r in conn.execute(inv._moves_sql("bucket = 'GOOD' AND qty_m > 0 AND product_id IS NOT NULL", "location, product_id, MIN(moved_at)")
                                                         + " GROUP BY location, product_id", inv._params(conn))}
    out = []
    for key in set(sold) | set(stock):
        loc, pid = key
        p = products[pid]
        qty = sold[key]["qty_m"] if key in sold else 0
        per_day = Decimal(max(qty, 0)) / days
        good = stock[key]["good_m"] if key in stock else 0
        last_day = last.get(key)
        since = last_day or (first_in.get(key) or "")[:10]
        idle = (end - date.fromisoformat(since)).days if since else None
        out.append({"location": loc, "location_name": inv.place_name(conn, loc), "product_id": pid, "item_code": p["item_code"], "description": p["description"],
                    "unit": inv.unit(p), "sold_m": qty, "per_day_m": _round(per_day), "good_m": good, "value_c": inv.value_c(good, p),
                    "days_left": int(good / per_day) if per_day > 0 and good > 0 else None, "last_sold": last_day,
                    "not_moving": good > 0 and (idle is None or idle >= dead)})
    return sorted(out, key=lambda o: (-o["sold_m"], o["item_code"]))


def shrinkage(conn, user: User, date_from: str, date_to: str, location: int | None = None) -> list[dict]:
    """What went missing, per place, product and how: found missing or over by posted stock counts, and lost on transfers (sent but not
    counted in at the other end, shown at the place it was going to). Valued at the last purchase cost."""
    require(user, "inventory.view")
    products = {r["id"]: r for r in conn.execute("SELECT * FROM products")}
    found = conn.execute("SELECT 'COUNT' AS how, m.location_code, m.product_id, SUM(CASE WHEN m.qty_m < 0 THEN -m.qty_m ELSE 0 END) AS missing_m,"
                         " SUM(CASE WHEN m.qty_m > 0 THEN m.qty_m ELSE 0 END) AS over_m"
                         " FROM stock_movements m WHERE m.kind = 'COUNT' AND substr(m.moved_at, 1, 10) BETWEEN ? AND ? GROUP BY m.location_code, m.product_id"
                         " UNION ALL"
                         " SELECT 'TRANSFER', t.to_code, l.product_id, SUM(MAX(l.sent_m - l.received_m, 0)), SUM(MAX(l.received_m - l.sent_m, 0))"
                         " FROM stock_transfers t JOIN stock_transfer_lines l ON l.transfer_id = t.id"
                         " WHERE t.status = 'RECEIVED' AND l.received_m <> l.sent_m AND substr(t.closed_at, 1, 10) BETWEEN ? AND ? GROUP BY t.to_code, l.product_id",
                         (date_from, date_to, date_from, date_to)).fetchall()
    places = inv.scope(user)
    out = []
    for r in found:
        if (places is not None and r["location_code"] not in places) or (location is not None and r["location_code"] != location):
            continue
        p = products[r["product_id"]]
        out.append({"location": r["location_code"], "location_name": inv.place_name(conn, r["location_code"]), "how": r["how"],
                    "how_label": "Stock count" if r["how"] == "COUNT" else "Lost on transfer", "item_code": p["item_code"],
                    "description": p["description"], "unit": inv.unit(p), "missing_m": r["missing_m"], "over_m": r["over_m"],
                    "missing_c": inv.value_c(r["missing_m"], p), "over_c": inv.value_c(r["over_m"], p)})
    return sorted(out, key=lambda o: -o["missing_c"])


def value_by_department(conn, user: User, location: int | None = None) -> list[dict]:
    """Stock value per department at the last purchase cost: for sale, and damaged or held."""
    out: dict[str, dict] = {}
    for r in inv.on_hand(conn, user, location):
        o = out.setdefault(r["department"], {"department": r["department"], "ids": set(), "good_value_c": 0, "other_value_c": 0})
        o["ids"].add(r["product_id"])
        o["good_value_c"] += r["good_value_c"]
        o["other_value_c"] += r["value_c"] - r["good_value_c"]
    for o in out.values():
        o["products"] = len(o.pop("ids"))
    return sorted(out.values(), key=lambda o: -o["good_value_c"])


def fmt_margin(bp: int | None) -> str:
    return "" if bp is None else f"{Decimal(bp) / 100:.1f}%"
