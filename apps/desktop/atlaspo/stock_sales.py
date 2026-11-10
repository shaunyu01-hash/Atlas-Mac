"""Inventory, phase 2: sales and customer returns, and the reports that read them.

Rules:
- A sales paper is one place, one day: what was sold there, typed in or imported from a POS file, or what customers brought back.
  Quantities are in each product's stock unit (inventory.unit). Stock comes off the books the day it's recorded (inventory.MOVES reads it).
- Each line keeps what one unit sold for (typed, from the file, or else the catalog selling price) and what it cost then (the last purchase
  cost), so sales amounts and gross profit never change when prices do later.
- Customer returns go back to stock for sale, or to damaged stock.
- A sale can't be dated before a stock count that has started at that place: the count already took in what was sold up to then.
  Nor can it be dated in the future.
- The same POS file can't be imported twice for the same place.
- Recorded sales are permanent. Management cancels a wrong paper with a reason, and it is recorded again.
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


def _check_date(conn, location: int, day: str) -> str:
    try:
        when = date.fromisoformat(str(day))
    except (TypeError, ValueError):
        raise POError("Choose the day of the sales.") from None
    if when > today():
        raise POError("Sales can't be recorded for a day that hasn't come yet.")
    count = conn.execute("SELECT count_number, substr(opened_at, 1, 10) AS day FROM stock_counts WHERE location_code = ? AND status <> 'CANCELLED'"
                         " AND substr(opened_at, 1, 10) > ? ORDER BY opened_at LIMIT 1", (location, when.isoformat())).fetchone()
    if count:
        raise POError(f"Stock count {count['count_number']} at {inv.place_name(conn, location)} started on {count['day']}, so it already took in "
                      f"what was sold before then. Record sales from that day on.")
    return when.isoformat()


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
        day = _check_date(conn, location, day)
        if file_sha256 and conn.execute("SELECT sale_number FROM stock_sales WHERE file_sha256 = ? AND location_code = ? AND status = 'POSTED'",
                                        (file_sha256, location)).fetchone():
            raise POError(f"That file was already imported for {inv.place_name(conn, location)}.")
        rows: dict[int, tuple] = {}
        for line in lines:
            p = inv._product(conn, line["product_id"])
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
                    price = parse_pesos(price_text) if price_text else list_price_c(p)
                    amount = _round(Decimal(price) * qty / 1000)
            except ValueError:
                raise POError(f"{p['item_code']}: the price must be an amount in pesos.") from None
            if price < 0 or amount < 0:
                raise POError(f"{p['item_code']}: a price can't be below zero.")
            rows[p["id"]] = (bucket, qty, price, amount, _cost_c(p))
        if not rows:
            raise POError("Type a quantity for at least one product.")
        number = _number(conn, kind)
        sale_id = conn.execute("INSERT INTO stock_sales(sale_number, kind, location_code, sale_date, source, file_name, file_sha256, note, status, entered_by, entered_at)"
                               " VALUES (?,?,?,?,?,?,?,?,?,?,?)", (number, kind, location, day, source, file_name, file_sha256, (note or "").strip(), POSTED, user.id, timestamp())).lastrowid
        conn.executemany("INSERT INTO stock_sale_lines(sale_id, product_id, bucket, qty_m, price_c, amount_c, cost_c) VALUES (?,?,?,?,?,?,?)",
                         [(sale_id, pid, *r) for pid, r in rows.items()])
        total = sum(r[3] for r in rows.values())
        audit(conn, user, "SALES_RECORD" if kind == SALE else "SALES_RETURN", "stock_sale", sale_id,
              summary=f"{number}: {KIND_LABELS[kind].lower()} at {inv.place_name(conn, location)} on {day}, {len(rows)} products, {fmt_peso(total)}"
              + (f", from {file_name}" if file_name else ""))
    return sale_id


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
        if sale["status"] != POSTED:
            raise POError("These sales are already cancelled.")
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
                        " (SELECT COALESCE(SUM(amount_c), 0) FROM stock_sale_lines l WHERE l.sale_id = s.id) AS total_c"
                        " FROM stock_sales s JOIN users u ON u.id = s.entered_by LEFT JOIN users c ON c.id = s.cancelled_by"
                        + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY s.sale_date DESC, s.id DESC", args).fetchall()
    return [{**dict(r), "location_name": inv.place_name(conn, r["location_code"]), "kind_label": KIND_LABELS[r["kind"]], "source_label": SOURCE_LABELS[r["source"]]}
            for r in rows if places is None or r["location_code"] in places]


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
    where = "s.status = 'POSTED' AND s.sale_date BETWEEN ? AND ?" + (" AND s.location_code = ?" if location is not None else "")
    args = [date_from, date_to] + ([location] if location is not None else [])
    return conn.execute("SELECT s.location_code, l.product_id,"
                        " SUM(CASE s.kind WHEN 'SALE' THEN l.qty_m ELSE -l.qty_m END) AS qty_m,"
                        " SUM(CASE s.kind WHEN 'SALE' THEN l.amount_c ELSE -l.amount_c END) AS amount_c,"
                        " SUM(CASE s.kind WHEN 'SALE' THEN 1 ELSE -1 END * COALESCE(l.cost_c, 0) * l.qty_m / 1000.0) AS cost_c,"
                        " SUM(l.cost_c IS NULL) AS no_cost, MAX(CASE s.kind WHEN 'SALE' THEN s.sale_date END) AS last_sold"
                        f" FROM stock_sales s JOIN stock_sale_lines l ON l.sale_id = s.id WHERE {where} GROUP BY s.location_code, l.product_id", args).fetchall()


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
        o["cost_c"] += _round(Decimal(str(r["cost_c"] or 0)))
        o["no_cost"] += r["no_cost"]
    for o in out.values():
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
    out = []
    for key in set(sold) | set(stock):
        loc, pid = key
        p = products[pid]
        qty = sold[key]["qty_m"] if key in sold else 0
        per_day = Decimal(max(qty, 0)) / days
        good = stock[key]["good_m"] if key in stock else 0
        last_day = last.get(key)
        idle = (end - date.fromisoformat(last_day)).days if last_day else None
        out.append({"location": loc, "location_name": inv.place_name(conn, loc), "product_id": pid, "item_code": p["item_code"], "description": p["description"],
                    "unit": inv.unit(p), "sold_m": qty, "per_day_m": _round(per_day), "good_m": good, "value_c": inv.value_c(good, p),
                    "days_left": int(good / per_day) if per_day > 0 and good > 0 else None, "last_sold": last_day,
                    "not_moving": good > 0 and (idle is None or idle >= dead)})
    return sorted(out, key=lambda o: (-o["sold_m"], o["item_code"]))


def shrinkage(conn, user: User, date_from: str, date_to: str) -> list[dict]:
    """What posted stock counts found missing or over the books, per place and product, valued at the last purchase cost."""
    require(user, "inventory.view")
    products = {r["id"]: r for r in conn.execute("SELECT * FROM products")}
    out = []
    for r in conn.execute("SELECT m.location_code, m.product_id, SUM(CASE WHEN m.qty_m < 0 THEN -m.qty_m ELSE 0 END) AS missing_m,"
                          " SUM(CASE WHEN m.qty_m > 0 THEN m.qty_m ELSE 0 END) AS over_m, COUNT(*) AS counts"
                          " FROM stock_movements m WHERE m.kind = 'COUNT' AND substr(m.moved_at, 1, 10) BETWEEN ? AND ? GROUP BY m.location_code, m.product_id",
                          (date_from, date_to)):
        p = products[r["product_id"]]
        out.append({"location": r["location_code"], "location_name": inv.place_name(conn, r["location_code"]), "item_code": p["item_code"],
                    "description": p["description"], "unit": inv.unit(p), "missing_m": r["missing_m"], "over_m": r["over_m"],
                    "missing_c": inv.value_c(r["missing_m"], p), "over_c": inv.value_c(r["over_m"], p), "counts": r["counts"]})
    places = inv.scope(user)
    return sorted([o for o in out if places is None or o["location"] in places], key=lambda o: -o["missing_c"])


def value_by_department(conn, user: User, location: int | None = None) -> list[dict]:
    """Stock value per department at the last purchase cost: for sale, and damaged or held."""
    out: dict[str, dict] = {}
    for r in inv.on_hand(conn, user, location):
        o = out.setdefault(r["department"], {"department": r["department"], "products": 0, "good_value_c": 0, "other_value_c": 0})
        o["products"] += 1
        o["good_value_c"] += r["good_value_c"]
        o["other_value_c"] += r["value_c"] - r["good_value_c"]
    return sorted(out.values(), key=lambda o: -o["good_value_c"])


def fmt_margin(bp: int | None) -> str:
    return "" if bp is None else f"{Decimal(bp) / 100:.1f}%"
