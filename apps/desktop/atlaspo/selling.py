"""Selling prices of what was purchased, ready for the barcode stickers.

Each line on an approved OF gets the automatic price unless someone changes it by hand here. The price on the order is only
shown beside it, because catalog products carry their last price onto the order without anyone choosing it.
Stickers and the sticker deduction play no part in the cost used here. The supplier's PO never shows any of this.
"""
from __future__ import annotations

from .auth import User, require
from .db import POError, audit, timestamp, tx
from .money import ROUND_SELLING_C, fmt_plain, per_piece, selling_from_cost

MANUAL, AUTO = "Changed by hand", "Automatic"
BELOW_SRP = "Below SRP"


def round_up(price_c: int) -> int:
    """Every price ends in 0, so a price typed by hand goes up to the next multiple of 10 pesos, the same as the automatic one."""
    return -(-price_c // ROUND_SELLING_C) * ROUND_SELLING_C


def _flag(d: dict) -> None:
    """A price by hand under the automatic SRP is flagged, so selling below it is never an accident. Under what the piece cost is flagged harder: that's a loss on every sale."""
    d["below_srp"] = d["source"] == MANUAL and d["auto_c"] is not None and d["price_c"] < d["auto_c"]
    d["below_cost"] = d["price_c"] is not None and d.get("cost_c") is not None and d["price_c"] < d["cost_c"]


def item_key(item) -> str:
    """Survives an amendment, which replaces an OF's item rows."""
    return "|".join((item["item_code"] or "", item["description"] or "", item["size"] or "", item["color"] or "")).upper()


def list_items(conn, text: str = "", limit: int = 2000) -> list[dict]:
    """Every line on an approved OF, newest first. Drafts and cancelled OFs were never purchased."""
    where, args = ["f.status = 'APPROVED'"], []
    for word in text.split():
        where.append("(f.of_number LIKE ? OR s.name LIKE ? OR s.code LIKE ? OR i.item_code LIKE ? OR i.description LIKE ?)")
        args += [f"%{word}%"] * 5
    rows = conn.execute(
        "SELECT i.*, f.of_number, f.of_date, s.name AS supplier FROM order_form_items i JOIN order_forms f ON f.id = i.of_id"
        " JOIN suppliers s ON s.id = f.supplier_id WHERE " + " AND ".join(where) + " ORDER BY f.of_date DESC, f.id DESC, i.line_no LIMIT ?", [*args, limit]).fetchall()
    manual = {(r["of_id"], r["item_key"]): r["price_c"] for r in conn.execute("SELECT of_id, item_key, price_c FROM selling_prices")}
    out = []
    for row in rows:
        d = dict(row)
        d["key"] = item_key(row)
        d["cost_c"] = per_piece(d["net_unit_c"], d["pieces_per_unit"])
        d["auto_c"] = selling_from_cost(d["cost_c"])
        by_hand = manual.get((d["of_id"], d["key"]))
        d["price_c"], d["source"] = (by_hand, MANUAL) if by_hand else (d["auto_c"], AUTO)
        d["margin"] = (d["price_c"] - d["cost_c"]) / d["price_c"] if d["price_c"] else None
        _flag(d)
        out.append(d)
    return out


def set_price(conn, user: User, of_id: int, key: str, price_c: int | None) -> None:
    """A price by hand, per piece. None goes back to the automatic price."""
    require(user, "price.edit")
    if price_c is not None:
        if isinstance(price_c, bool) or not isinstance(price_c, int):
            raise POError("Type the selling price as a number.")
        # A safety limit, the same as other amounts (100 billion pesos), so a slip of the keyboard can't break the save.
        if price_c > 10**13:
            raise POError("That selling price is too large. Check the number.")
    if price_c is not None and price_c <= 0:
        raise POError("A selling price has to be more than zero. Clear it to go back to the automatic price.")
    if price_c is not None:
        price_c = round_up(price_c)
    with tx(conn):
        of = conn.execute("SELECT of_number, status FROM order_forms WHERE id = ?", (of_id,)).fetchone()
        if of is None or of["status"] != "APPROVED":
            raise POError("Selling prices are only kept for approved Order Forms.")
        old = conn.execute("SELECT price_c FROM selling_prices WHERE of_id = ? AND item_key = ?", (of_id, key)).fetchone()
        if price_c is None:
            conn.execute("DELETE FROM selling_prices WHERE of_id = ? AND item_key = ?", (of_id, key))
        else:
            conn.execute("INSERT INTO selling_prices(of_id, item_key, price_c, set_by, set_at) VALUES (?,?,?,?,?)"
                         " ON CONFLICT(of_id, item_key) DO UPDATE SET price_c = excluded.price_c, set_by = excluded.set_by, set_at = excluded.set_at",
                         (of_id, key, price_c, user.id, timestamp()))
        what = f"selling price set to {fmt_plain(price_c)}" if price_c else "selling price back to automatic"
        audit(conn, user, "SELLING_PRICE", "order_form", of_id, of_id=of_id, summary=f"{of['of_number']}, {key.strip('|')}: {what}",
              previous={"price_c": old["price_c"]} if old else None, new={"price_c": price_c})


CS, IP = "CS", "IP"


def forfeited_items(conn, text: str = "") -> list[dict]:
    """Goods the supplier didn't take back in 90 days, from claim stubs and item passes. They're ours to sell, so each gets a selling price.
    The automatic price comes from what the piece cost on its PO, the same way as everything bought."""
    # The pieces are what the warehouse counted when they came back, when it did. A product that wasn't on the PO has no cost, so its price is typed by hand.
    rows = conn.execute(
        "SELECT 'CS' AS kind, i.id AS item_id, c.id AS parent_id, c.cs_number AS number, c.closed_at, s.name AS supplier, s.code AS supplier_code, i.item_code, i.description, i.size, i.color,"
        " COALESCE(g.pieces_m, i.pieces_m) AS pieces_m, p.net_unit_c, p.pieces_per_unit"
        " FROM claim_stub_items i JOIN claim_stubs c ON c.id = i.cs_id JOIN suppliers s ON s.id = c.supplier_id LEFT JOIN purchase_order_items p ON p.id = i.po_item_id"
        " LEFT JOIN goods_arrivals g ON g.kind = 'CS' AND g.item_id = i.id WHERE c.status = 'FORFEITED'"
        " UNION ALL SELECT 'IP', i.id, x.id, x.ip_number, x.closed_at, s.name, s.code, i.item_code, i.description, i.size, i.color, COALESCE(g.pieces_m, i.pieces_m), p.net_unit_c, p.pieces_per_unit"
        " FROM item_pass_items i JOIN item_passes x ON x.id = i.ip_id JOIN suppliers s ON s.id = x.supplier_id LEFT JOIN purchase_order_items p ON p.id = i.po_item_id"
        " LEFT JOIN goods_arrivals g ON g.kind = 'IP' AND g.item_id = i.id WHERE x.status = 'FORFEITED'"
        " ORDER BY 5 DESC, 4").fetchall()
    manual = {(r["kind"], r["item_id"]): r["price_c"] for r in conn.execute("SELECT kind, item_id, price_c FROM forfeit_prices")}
    words = text.lower().split()
    out = []
    for row in rows:
        d = dict(row)
        if not all(w in f"{d['number']} {d['supplier']} {d['supplier_code']} {d['item_code']} {d['description']}".lower() for w in words):
            continue
        d["cost_c"] = per_piece(d["net_unit_c"], d["pieces_per_unit"]) if d["net_unit_c"] is not None else None
        d["auto_c"] = selling_from_cost(d["cost_c"]) if d["cost_c"] is not None else None
        by_hand = manual.get((d["kind"], d["item_id"]))
        d["price_c"], d["source"] = (by_hand, MANUAL) if by_hand else (d["auto_c"], AUTO)
        _flag(d)
        out.append(d)
    return out


def set_forfeit_price(conn, user: User, kind: str, item_id: int, price_c: int | None) -> None:
    """A price by hand, per piece, for forfeited goods. None goes back to the automatic price."""
    require(user, "price.edit")
    if kind not in (CS, IP):
        raise POError("That isn't a claim stub or item pass line.")
    if price_c is not None and price_c <= 0:
        raise POError("A selling price has to be more than zero. Clear it to go back to the automatic price.")
    if price_c is not None:
        price_c = round_up(price_c)
    with tx(conn):
        table, parent, status = ("claim_stub_items", "claim_stubs c ON c.id = i.cs_id", "c.status") if kind == CS else ("item_pass_items", "item_passes c ON c.id = i.ip_id", "c.status")
        number = "c.cs_number" if kind == CS else "c.ip_number"
        row = conn.execute(f"SELECT c.id AS parent_id, {number} AS number, {status} AS status, i.description FROM {table} i JOIN {parent} WHERE i.id = ?", (item_id,)).fetchone()
        if row is None or row["status"] != "FORFEITED":
            raise POError("Only forfeited goods get a selling price here.")
        old = conn.execute("SELECT price_c FROM forfeit_prices WHERE kind = ? AND item_id = ?", (kind, item_id)).fetchone()
        if price_c is None:
            conn.execute("DELETE FROM forfeit_prices WHERE kind = ? AND item_id = ?", (kind, item_id))
        else:
            conn.execute("INSERT INTO forfeit_prices(kind, item_id, price_c, set_by, set_at) VALUES (?,?,?,?,?)"
                         " ON CONFLICT(kind, item_id) DO UPDATE SET price_c = excluded.price_c, set_by = excluded.set_by, set_at = excluded.set_at", (kind, item_id, price_c, user.id, timestamp()))
        what = f"selling price set to {fmt_plain(price_c)}" if price_c else "selling price back to automatic"
        audit(conn, user, "FORFEIT_PRICE", "claim_stub" if kind == CS else "item_pass", row["parent_id"], summary=f"{row['number']}, {row['description']}: {what}",
              previous={"price_c": old["price_c"]} if old else None, new={"price_c": price_c, "line_id": item_id})
