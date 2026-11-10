"""Reorder levels and suggested orders: what each branch is running low on, turned into draft Order Forms.

Rules:
- Each product at each branch has a minimum and a target, in its stock unit. The program works them out from sales: average daily sales
  over the last inventory.sales_days, times inventory.lead_days (the minimum), and times lead_days + cover_days (the target).
  A purchaser can set them by hand instead (reorder.edit); clearing them goes back to the program's figures.
- A product needs ordering when what the branch has for sale, plus what's on the way, what's still due on open POs (less shortages already
  charged on a debit memo) and what's on Order Forms not yet printed, is below the minimum. The suggestion brings it up to the target,
  rounded up to whole order units (packs) unless the unit can be split.
- Suggested orders become draft Order Forms, one per supplier, with each branch's quantity. They go through the usual approval.
  Products with no supplier, an inactive supplier, no cost or that are inactive are listed with the reason but can't be ordered here.
- Only branches are suggested for: Order Forms deliver to branches through the Central Warehouse.
"""
from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

from . import inventory as inv
from . import order_forms, orders
from .auth import User, require
from .db import POError, audit, get_setting, timestamp, today, tx
from .money import allows_decimal, fmt_qty
from .stock_sales import _setting_days, daily_sales

_OPEN_PO = (orders.APPROVED, orders.ISSUED, orders.PARTIAL)


def on_order(conn) -> dict[tuple[int, int], int]:
    """{(branch, product): stock units still coming}: what's due on POs that are approved, issued or partly delivered, less pieces already
    charged back on a shortage debit memo, plus each branch's quantity on Order Forms not yet turned into POs (drafts, waiting for
    approval, or approved but not printed), so the same goods are never suggested twice."""
    out: dict[tuple[int, int], int] = {}
    short = ("COALESCE((SELECT SUM(di.pieces_m) FROM debit_memo_items di JOIN debit_memos dm ON dm.id = di.dm_id"
             " WHERE di.po_item_id = i.id AND di.reason = 'SHORT' AND dm.status = 'ISSUED'), 0)")
    for r in conn.execute(f"SELECT p.branch_code, {inv._product_of('i.id')} AS pid,"
                          f" SUM(MAX(MAX(i.qty_m - i.received_qty_m, 0) * i.pieces_per_unit - {short}, 0)) AS due"
                          f" FROM purchase_order_items i JOIN purchase_orders p ON p.id = i.po_id WHERE p.status IN ({','.join('?' * len(_OPEN_PO))})"
                          " GROUP BY p.branch_code, pid", _OPEN_PO):
        if r["pid"] is not None and r["due"]:
            out[(r["branch_code"], r["pid"])] = out.get((r["branch_code"], r["pid"]), 0) + r["due"]
    for r in conn.execute("SELECT a.branch_code, COALESCE(i.product_id, (SELECT p.id FROM products p WHERE i.item_code <> '' AND p.item_code = i.item_code)) AS pid,"
                          " SUM(a.qty_m * i.pieces_per_unit) AS due FROM order_form_allocations a JOIN order_form_items i ON i.id = a.of_item_id"
                          " JOIN order_forms f ON f.id = i.of_id WHERE f.status IN ('DRAFT','PENDING_APPROVAL','APPROVED') AND f.pos_generated_at IS NULL"
                          " GROUP BY a.branch_code, pid"):
        if r["pid"] is not None and r["due"]:
            out[(r["branch_code"], r["pid"])] = out.get((r["branch_code"], r["pid"]), 0) + r["due"]
    return out


def levels(conn, user: User, location: int | None = None, only_low: bool = True) -> list[dict]:
    """Each product at each branch with its levels and what it has. only_low keeps just those below the minimum."""
    require(user, "inventory.view")
    days = _setting_days(conn, "inventory.sales_days")
    lead, cover = _setting_days(conn, "inventory.lead_days"), int(get_setting(conn, "inventory.cover_days") or 0)
    branches = [p for p in inv.locations(conn) if p["code"] != inv.WAREHOUSE and (location is None or p["code"] == location)]
    stock = {(r["location"], r["product_id"]): r for r in inv.on_hand(conn, user)}
    due = on_order(conn)
    manual = {(r["location_code"], r["product_id"]): r for r in conn.execute("SELECT r.*, u.full_name AS set_name FROM reorder_levels r JOIN users u ON u.id = r.set_by")}
    products = {r["id"]: r for r in conn.execute("SELECT p.*, s.name AS supplier, s.status AS supplier_status FROM products p LEFT JOIN suppliers s ON s.id = p.supplier_id")}
    out = []
    for b in branches:
        per_day = daily_sales(conn, b["code"], days)
        keys = {pid for (loc, pid) in list(stock) + list(due) + list(manual) if loc == b["code"]} | set(per_day)
        for pid in keys:
            p = products.get(pid)
            if p is None:
                continue
            daily = per_day.get(pid, Decimal(0))
            m = manual.get((b["code"], pid))
            minimum = m["min_m"] if m else int(daily * lead)
            target = m["target_m"] if m else int(daily * (lead + cover))
            s = stock.get((b["code"], pid))
            good, moving = (s["good_m"], s["transit_m"]) if s else (0, 0)
            coming = due.get((b["code"], pid), 0)
            have = good + moving + coming
            low = target > 0 and have < minimum
            if only_low and not low:
                continue
            need = max(target - have, 0) if low else 0
            ppu = p["pieces_per_unit"] or 1
            # Order units: whole packs, unless the unit itself can be split (a kilo, a metre).
            if allows_decimal(p["uom"]) and ppu == 1:
                units_m = need
            else:
                units_m = int((Decimal(need) / 1000 / ppu).to_integral_value(rounding=ROUND_CEILING)) * 1000
            why_not = ("No supplier in the catalog" if p["supplier_id"] is None else "Supplier is inactive" if p["supplier_status"] != "ACTIVE"
                       else "No purchase cost" if not p["last_cost_c"] else "Product is inactive" if not p["active"] else "")
            out.append({"location": b["code"], "location_name": b["name"], "product_id": pid, "item_code": p["item_code"], "description": p["description"],
                        "size": p["size"], "color": p["color"], "unit": inv.unit(p), "uom": p["uom"], "pieces_per_unit": ppu,
                        "supplier_id": p["supplier_id"], "supplier": p["supplier"] or "", "last_cost_c": p["last_cost_c"],
                        "good_m": good, "transit_m": moving, "on_order_m": coming, "per_day_m": int(daily), "min_m": minimum, "target_m": target,
                        "manual": m is not None, "set_by": m["set_name"] if m else "", "low": low, "need_m": need, "order_m": units_m,
                        "orderable": not why_not, "why_not": why_not})
    return sorted(out, key=lambda r: (r["supplier"].lower() or "~", r["item_code"].lower(), r["location"]))


def set_levels(conn, user: User, location: int, product_id: int, minimum: str | None, target: str | None) -> None:
    """Sets a product's minimum and target at a branch by hand. Both blank goes back to the program's figures."""
    require(user, "reorder.edit")
    with tx(conn):
        inv._check_place(conn, user, location)
        p = inv._product(conn, product_id)
        old = conn.execute("SELECT min_m, target_m FROM reorder_levels WHERE location_code = ? AND product_id = ?", (location, product_id)).fetchone()
        if not str(minimum or "").strip() and not str(target or "").strip():
            conn.execute("DELETE FROM reorder_levels WHERE location_code = ? AND product_id = ?", (location, product_id))
            audit(conn, user, "REORDER_LEVELS", "product", product_id, summary=f"{p['item_code']} at {inv.place_name(conn, location)}: back to the program's levels",
                  previous=dict(old) if old else None)
            return
        lo, hi = inv._qty(minimum, p), inv._qty(target if str(target or "").strip() else minimum, p)
        if hi < lo:
            raise POError("The target can't be below the minimum.")
        conn.execute("INSERT INTO reorder_levels(location_code, product_id, min_m, target_m, set_by, set_at) VALUES (?,?,?,?,?,?)"
                     " ON CONFLICT(location_code, product_id) DO UPDATE SET min_m = excluded.min_m, target_m = excluded.target_m, set_by = excluded.set_by, set_at = excluded.set_at",
                     (location, product_id, lo, hi, user.id, timestamp()))
        audit(conn, user, "REORDER_LEVELS", "product", product_id,
              summary=f"{p['item_code']} at {inv.place_name(conn, location)}: minimum {fmt_qty(lo)}, target {fmt_qty(hi)}",
              previous=dict(old) if old else None, new={"min_m": lo, "target_m": hi})


def make_orders(conn, user: User, picks: list[tuple[int, int]], quantities: dict[tuple[int, int], str] | None = None) -> list[int]:
    """Draft Order Forms, one per supplier, for the (branch, product) picks. quantities may change the suggested order units. Returns the OF ids."""
    require(user, "of.edit")
    require(user, "inventory.view")
    quantities = quantities or {}
    rows = {(r["location"], r["product_id"]): r for r in levels(conn, user)}
    by_supplier: dict[int, dict[int, dict]] = {}
    for key in picks:
        r = rows.get(key)
        if r is None:
            raise POError("That product no longer needs ordering there. Refresh the list.")
        if not r["orderable"]:
            raise POError(f"{r['item_code']} can't be ordered from here: {r['why_not'].lower()}" + (f" ({r['supplier']})." if r["supplier"] else "."))
        text = quantities.get(key)
        units_m = r["order_m"] if text is None or not str(text).strip() else inv._qty(text, {"item_code": r["item_code"], "uom": r["uom"], "pieces_per_unit": 1})
        if units_m <= 0:
            continue
        item = by_supplier.setdefault(r["supplier_id"], {}).setdefault(r["product_id"], {"allocations_m": {}})
        item["allocations_m"][r["location"]] = units_m
    if not by_supplier:
        raise POError("Choose at least one product with a quantity to order.")
    of_ids = []
    with tx(conn):
        for supplier_id, items in by_supplier.items():
            supplier = conn.execute("SELECT * FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()
            lines = []
            for pid, item in items.items():
                p = inv._product(conn, pid)
                lines.append({"department_id": p["department_id"], "product_id": pid, "item_code": p["item_code"], "supplier_item_code": p["supplier_item_code"],
                              "description": p["description"], "brand": p["brand"], "size": p["size"], "color": p["color"], "uom": p["uom"],
                              "pieces_per_unit": p["pieces_per_unit"], "box_size": p["box_size"], "selling_c": p["selling_c"], "unit_cost_c": p["last_cost_c"],
                              "allocations_m": item["allocations_m"]})
            of_id = order_forms.create_draft(conn, user, {"supplier_id": supplier_id, "of_date": today().isoformat(), "payment_terms": supplier["payment_terms"],
                                                          "remarks": "Suggested by inventory: products below their minimum."}, lines)
            audit(conn, user, "REORDER_DRAFT", "order_form", of_id, of_id=of_id, summary=f"Draft OF for {supplier['name']} from the reorder list, {len(lines)} products")
            of_ids.append(of_id)
    return of_ids
