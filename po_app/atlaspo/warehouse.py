"""Central Warehouse: every order reaches the warehouse first, then goes on to the branches.

The warehouse works from the OF it was given, so it sees the quantities. For each branch PO it counts the boxes going to that branch, 0 when
nothing came for it. When the supplier's goods come without stickers, the ones with a sticker deduction, it also counts the pieces of each product
as it stickers them. Each branch ships on its own, and a supplier that delivers in more than one go gets one record per delivery.

The branch still makes its own two counts, and types the boxes that reached it. The warehouse's count is what the supplier delivered and what left
the warehouse; the branch's is what reached the branch. Pieces lost between the two are a transit loss for the home office, never a charge to the supplier.
"""
from __future__ import annotations

import json

from . import orders
from .auth import User, require
from .db import POError, audit, get_setting, timestamp, tx
from .money import fmt_qty, parse_qty

TO_RECEIVE, TO_SHIP, SHIPPED = "TO_RECEIVE", "TO_SHIP", "SHIPPED"
TABS = {TO_RECEIVE: "To receive", TO_SHIP: "To ship", SHIPPED: "Shipped"}
_ISSUED = (orders.ISSUED, orders.PARTIAL, orders.FULL, orders.CLOSED)
_OPEN = (orders.ISSUED, orders.PARTIAL)
# An amendment puts an issued PO back to Approved until it's printed again. Goods already at the warehouse for it stay on the list, but can't ship until then.
_LISTED = _ISSUED + (orders.APPROVED,)
REISSUE = "Changed by Purchasing. Waiting to be printed again."
# Far more than any delivery, so only a slip of the keyboard is refused.
MAX_COUNT = 10 ** 9


def live_since(conn) -> str:
    """When the Central Warehouse started in this database. Orders printed before then went straight to the branches."""
    return get_setting(conn, "warehouse.live_since") or ""


def stickers(conn, of_id: int) -> bool:
    """Whether this order's goods come without stickers. Taken from the supplier as it was when the OF was approved, as the payment is."""
    row = conn.execute("SELECT f.supplier_snapshot, s.sticker_rate_c FROM order_forms f JOIN suppliers s ON s.id = f.supplier_id WHERE f.id = ?", (of_id,)).fetchone()
    if row is None:
        return False
    if row["supplier_snapshot"]:
        return bool(json.loads(row["supplier_snapshot"]).get("sticker_rate_c"))
    return bool(row["sticker_rate_c"])


def _deliveries(conn, po_ids: list[int]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {i: [] for i in po_ids}
    if not po_ids:
        return out
    for r in conn.execute(
            "SELECT d.*, r.full_name AS received_name, x.full_name AS shipped_name FROM warehouse_deliveries d JOIN users r ON r.id = d.received_by LEFT JOIN users x ON x.id = d.shipped_by"
            f" WHERE d.po_id IN ({','.join('?' * len(po_ids))}) ORDER BY d.po_id, d.delivery_no", po_ids):
        out[r["po_id"]].append(dict(r))
    return out


def _mark(conn, d: dict) -> tuple:
    """What a delivery still at the warehouse looks like now: its id, when it was last saved and its figures. A screen keeps this to tell whether
    someone else saved over it since, even within the same second."""
    pieces = tuple(conn.execute("SELECT po_item_id, pieces_m FROM warehouse_delivery_items WHERE delivery_id = ? ORDER BY po_item_id", (d["id"],)).fetchall())
    return (d["id"], d["corrected_at"] or d["received_at"], d["boxes"], d["remarks"], tuple(tuple(r) for r in pieces))


def _pos(conn, of_id: int | None = None) -> list[dict]:
    """Printed branch POs since the warehouse started, each with its deliveries and where it stands.

    A PO the warehouse never recorded is only asked for while nothing else has happened to it: once a branch counted it, Admin received it outside
    the program, it was closed or its order was paid, it's no longer on its way to the warehouse."""
    where, args = ["p.of_id IS NOT NULL", f"p.status IN ({','.join('?' * len(_LISTED))})",
                   "COALESCE((SELECT MIN(v.issued_at) FROM po_issued_versions v WHERE v.po_id = p.id), p.issued_at) >= ?"], [*_LISTED, live_since(conn)]
    if of_id is not None:
        where.append("p.of_id = ?")
        args.append(of_id)
    rows = [dict(r) for r in conn.execute(
        "SELECT p.id, p.of_id, p.po_number, p.branch_code, p.branch_name, p.status,"
        " EXISTS (SELECT 1 FROM branch_counts b WHERE b.po_id = p.id) AS counted,"
        " EXISTS (SELECT 1 FROM outside_receipts o WHERE o.po_id = p.id) AS outside,"
        " EXISTS (SELECT 1 FROM payment_check_ofs k JOIN payment_checks c ON c.id = k.check_id WHERE k.of_id = p.of_id AND c.status = 'ACTIVE') AS paid,"
        " (SELECT COALESCE(SUM(i.qty_m * i.pieces_per_unit), 0) FROM purchase_order_items i WHERE i.po_id = p.id) AS ordered_m"
        " FROM purchase_orders p WHERE " + " AND ".join(where) + " ORDER BY p.of_id, p.branch_code", args)]
    deliveries = _deliveries(conn, [r["id"] for r in rows])
    out = []
    for p in rows:
        p["deliveries"] = deliveries[p["id"]]
        p["current"] = next((d for d in p["deliveries"] if d["shipped_at"] is None), None)
        if p["current"]:
            p["current"]["mark"] = _mark(conn, p["current"])
        p["reissue"] = p["status"] == orders.APPROVED
        still_open = p["status"] in _OPEN and not p["paid"] and not p["outside"]
        if p["current"]:
            p["stage"] = TO_SHIP
        elif p["deliveries"]:
            p["stage"] = SHIPPED
        elif still_open and p["status"] == orders.ISSUED and not p["counted"]:
            p["stage"] = TO_RECEIVE
        else:
            continue
        # Another delivery can still come for a PO that isn't finished or paid.
        p["can_more"] = p["stage"] == SHIPPED and still_open
        latest = p["current"] or (p["deliveries"][-1] if p["deliveries"] else None)
        p["delivery_no"] = latest["delivery_no"] if latest else 1
        p["boxes"] = latest["boxes"] if latest else None
        p["remarks"] = latest["remarks"] if latest else ""
        p["received_at"] = latest["received_at"] if latest else None
        p["received_name"] = latest["received_name"] if latest else ""
        p["shipped_at"] = latest["shipped_at"] if latest else None
        p["shipped_name"] = latest["shipped_name"] if latest else ""
        p["boxes_shipped"] = sum(d["boxes"] for d in p["deliveries"] if d["shipped_at"])
        out.append(p)
    return out


def _of_stage(stages: set[str]) -> str:
    for stage in (TO_RECEIVE, TO_SHIP, SHIPPED):
        if stage in stages:
            return stage
    return SHIPPED


def search(conn, user: User, text: str = "") -> list[dict]:
    """One row per order, newest first. An order shows under every tab one of its branches is in, so a branch waiting to ship isn't hidden by one still to come."""
    require(user, "receiving.review")
    by_of: dict[int, list] = {}
    for p in _pos(conn):
        by_of.setdefault(p["of_id"], []).append(p)
    if not by_of:
        return []
    heads = {r["id"]: dict(r) for r in conn.execute(
        f"SELECT f.id, f.of_number, f.of_date, s.name AS supplier, s.code AS supplier_code FROM order_forms f JOIN suppliers s ON s.id = f.supplier_id WHERE f.id IN ({','.join('?' * len(by_of))})", list(by_of))}
    words = text.lower().split()
    out = []
    for of_id, pos in by_of.items():
        head = heads[of_id]
        if not all(w in f"{head['of_number']} {head['supplier']} {head['supplier_code']} {' '.join(p['po_number'] for p in pos)}".lower() for w in words):
            continue
        stages = {p["stage"] for p in pos}
        out.append({**head, "stickers": stickers(conn, of_id), "pos": pos, "stages": stages, "stage": _of_stage(stages), "ordered_m": sum(p["ordered_m"] for p in pos),
                    "boxes": sum(p["boxes"] or 0 for p in pos), "can_more": any(p["can_more"] for p in pos),
                    "received_at": max((p["received_at"] for p in pos if p["received_at"]), default=None),
                    "shipped_at": max((p["shipped_at"] for p in pos if p["shipped_at"]), default=None)})
    return sorted(out, key=lambda r: r["id"], reverse=True)


def lines(conn, user: User, of_id: int) -> list[dict]:
    """Each product on each branch PO: what was ordered, the pieces stickered in the delivery still at the warehouse, and what earlier deliveries took."""
    require(user, "receiving.review")
    pos = {p["id"]: p for p in _pos(conn, of_id)}
    if not pos:
        return []
    out = []
    for r in conn.execute(
            "SELECT i.id AS po_item_id, i.po_id, p.po_number, p.branch_name, i.item_code, i.description, i.size, i.color, i.uom, i.qty_m * i.pieces_per_unit AS ordered_m"
            f" FROM purchase_order_items i JOIN purchase_orders p ON p.id = i.po_id WHERE i.po_id IN ({','.join('?' * len(pos))}) ORDER BY p.branch_code, i.line_no", list(pos)):
        line = dict(r)
        current = pos[line["po_id"]]["current"]
        counts = {d: m for d, m in conn.execute(
            "SELECT d.id, w.pieces_m FROM warehouse_delivery_items w JOIN warehouse_deliveries d ON d.id = w.delivery_id WHERE w.po_item_id = ?", (line["po_item_id"],))}
        line["stickered_m"] = counts.get(current["id"]) if current else None
        line["before_m"] = sum(m for d, m in counts.items() if not current or d != current["id"])
        out.append(line)
    return out


def _pieces(text, line) -> int:
    text = str(text or "").strip()
    if not text:
        raise POError(f"{line['branch_name']}, {line['description']}: type the pieces stickered, 0 if none came.")
    try:
        got = parse_qty(text, "PC")
    except ValueError as e:
        raise POError(f"{line['branch_name']}, {line['description']}: {e}") from None
    if got > MAX_COUNT * 1000:
        raise POError(f"{line['branch_name']}, {line['description']}: {text} pieces is far too many. Check the figure.")
    return got


def _boxes(text, po) -> int:
    text = str(text).strip()
    if not text.isdigit():
        raise POError(f"{po['branch_name']}: type how many boxes go to the branch, 0 if nothing came for it.")
    try:
        count = int(text)
    except ValueError:
        raise POError(f"{po['branch_name']}: type how many boxes go to the branch, 0 if nothing came for it.") from None
    if count > MAX_COUNT:
        raise POError(f"{po['branch_name']}: {text} boxes is far too many. Check the figure.")
    return count


def receive(conn, user: User, of_id: int, boxes: dict[int, str], stickered: dict[int, str] | None = None, remarks: str | None = None, another: bool = False,
            only: list[int] | None = None, expect_delivery: dict[int, tuple | None] | None = None) -> None:
    """Goods reached the warehouse. boxes is {PO id: boxes for that branch}, and stickered is {PO item id: pieces stickered} when the goods come without stickers.

    The first time, every branch needs a figure, 0 when nothing came for it. Until a branch ships, its figures can be corrected: whoever received it stays on
    record and the log keeps the earlier figures. With another, it's a later delivery for branches already shipped, and only the branches typed are in it.
    only limits it to those PO ids, when the screen shows one branch. expect_delivery is {PO id: the delivery's "mark" from _pos the screen showed, None for
    none}: when someone else saved, corrected or shipped it since, nothing is saved."""
    require(user, "warehouse.receive")
    stickered = stickered or {}
    with tx(conn):
        everything = _pos(conn, of_id)
        pos = [p for p in everything if only is None or p["id"] in only]
        if expect_delivery is not None:
            listed = {p["id"]: p for p in everything}
            for po_id, expected in expect_delivery.items():
                now = listed.get(po_id)
                current = now["current"] if now else None
                # The mark, not just the id: a correction keeps the same delivery id. Anything else given (a bare id) never matches, so nothing is overwritten.
                if (current["mark"] if current else None) != (tuple(expected) if isinstance(expected, (tuple, list)) else expected):
                    raise POError("Someone else saved this delivery. Open it again.")
        targets = []
        for p in pos:
            text = str(boxes.get(p["id"], "")).strip()
            if p["current"] or p["stage"] == TO_RECEIVE:
                if not text and not another:
                    raise POError(f"{p['branch_name']}: type how many boxes go to the branch, 0 if nothing came for it.")
                if text:
                    targets.append((p, _boxes(text, p)))
            elif another and p["can_more"] and text:
                targets.append((p, _boxes(text, p)))
        if not targets:
            raise POError("Nothing on this order is waiting at the warehouse." if not another else "Type the boxes for each branch in this delivery.")
        sticker = stickers(conn, of_id)
        by_po: dict[int, list] = {}
        if sticker:
            target_ids = {p["id"] for p, _ in targets}
            for line in lines(conn, user, of_id):
                if line["po_id"] in target_ids:
                    by_po.setdefault(line["po_id"], []).append((line, _pieces(stickered.get(line["po_item_id"]), line)))
        number = conn.execute("SELECT of_number FROM order_forms WHERE id = ?", (of_id,)).fetchone()[0]
        stamp = timestamp()
        for p, count in targets:
            current = p["current"]
            note = (remarks if remarks is not None else (current["remarks"] if current else "")).strip()
            if current:
                old = {line["po_item_id"]: line["stickered_m"] for line, _ in by_po.get(p["id"], [])}
                new = {line["po_item_id"]: got for line, got in by_po.get(p["id"], [])}
                if current["boxes"] == count and old == new and current["remarks"] == note:
                    continue
                conn.execute("UPDATE warehouse_deliveries SET boxes = ?, remarks = ?, corrected_by = ?, corrected_at = ? WHERE id = ?", (count, note, user.id, stamp, current["id"]))
                delivery_id, delivery_no = current["id"], current["delivery_no"]
                changed = {f"{line['item_code']} {line['description']}".strip(): (fmt_qty(line["stickered_m"]) if line["stickered_m"] is not None else "", fmt_qty(got))
                           for line, got in by_po.get(p["id"], []) if line["stickered_m"] != got}
                audit(conn, user, "WAREHOUSE_CORRECT", "purchase_order", p["id"], po_id=p["id"], of_id=of_id,
                      summary=f"{p['po_number']} delivery {delivery_no} corrected at the Central Warehouse: {count} boxes for {p['branch_name']}",
                      previous={"boxes": current["boxes"], "remarks": current["remarks"], "pieces": {k: v[0] for k, v in changed.items()}},
                      new={"boxes": count, "remarks": note, "pieces": {k: v[1] for k, v in changed.items()}})
            else:
                delivery_no = len(p["deliveries"]) + 1
                delivery_id = conn.execute("INSERT INTO warehouse_deliveries(po_id, delivery_no, boxes, remarks, received_by, received_at) VALUES (?,?,?,?,?,?)",
                                           (p["id"], delivery_no, count, note, user.id, stamp)).lastrowid
                audit(conn, user, "WAREHOUSE_RECEIVE", "purchase_order", p["id"], po_id=p["id"], of_id=of_id,
                      summary=f"{p['po_number']} at the Central Warehouse" + (f", delivery {delivery_no}" if delivery_no > 1 else "") + f": {count} boxes for {p['branch_name']}",
                      new={"boxes": count, "delivery_no": delivery_no})
            for line, got in by_po.get(p["id"], []):
                conn.execute("INSERT INTO warehouse_delivery_items(delivery_id, po_item_id, pieces_m) VALUES (?,?,?) ON CONFLICT(delivery_id, po_item_id) DO UPDATE SET pieces_m = excluded.pieces_m",
                             (delivery_id, line["po_item_id"], got))
        stickered_total = sum(got for rows in by_po.values() for _, got in rows)
        if by_po:
            audit(conn, user, "WAREHOUSE_STICKERED", "order_form", of_id, of_id=of_id, summary=f"{number}: {fmt_qty(stickered_total)} pieces stickered at the Central Warehouse")


def ship(conn, user: User, of_id: int, po_ids: list[int] | None = None) -> None:
    """The boxes for these branches left for the branches, which count them again when they arrive. Without po_ids, every branch ready to ship."""
    require(user, "warehouse.receive")
    with tx(conn):
        ready = [p for p in _pos(conn, of_id) if p["current"] and (po_ids is None or p["id"] in po_ids)]
        held = [p for p in ready if p["reissue"]]
        # Asked for by name, a changed PO is refused; shipping the whole order leaves it behind for later.
        if held and (po_ids is not None or len(held) == len(ready)):
            raise POError(f"{', '.join(p['po_number'] for p in held)}: {REISSUE} It can ship once it's printed again.")
        ready = [p for p in ready if not p["reissue"]]
        if not ready:
            raise POError("Nothing on this order is waiting to ship.")
        for p in ready:
            conn.execute("UPDATE warehouse_deliveries SET shipped_by = ?, shipped_at = ? WHERE id = ?", (user.id, timestamp(), p["current"]["id"]))
            audit(conn, user, "WAREHOUSE_SHIP", "purchase_order", p["id"], po_id=p["id"], of_id=of_id, summary=f"{p['po_number']}: {p['current']['boxes']} boxes shipped to {p['branch_name']}")


def shipped_on(conn, po_id: int) -> str | None:
    """When the latest delivery for this PO left the warehouse, if any did."""
    row = conn.execute("SELECT MAX(shipped_at) FROM warehouse_deliveries WHERE po_id = ?", (po_id,)).fetchone()
    return row[0] if row else None


def left_central(conn, po_id: int) -> dict[int, int]:
    """Pieces of each product the warehouse counted for this PO, over every delivery. Empty when it only counted boxes."""
    return {r[0]: r[1] for r in conn.execute(
        "SELECT w.po_item_id, SUM(w.pieces_m) FROM warehouse_delivery_items w JOIN warehouse_deliveries d ON d.id = w.delivery_id WHERE d.po_id = ? GROUP BY w.po_item_id", (po_id,))}


def boxes_sent(conn, po_id: int) -> int | None:
    """Boxes shipped to this branch over every delivery, or None if the warehouse never shipped it."""
    row = conn.execute("SELECT SUM(boxes), COUNT(*) FROM warehouse_deliveries WHERE po_id = ? AND shipped_at IS NOT NULL", (po_id,)).fetchone()
    return row[0] if row[1] else None
