"""The Order Form: the master document for one whole supplier order, across branches.

Draft -> Pending Approval -> Approved, or Draft -> Approved at once when no approval is required.
A rejected OF goes back to Draft; the rejection stays in of_approvals. The official number comes at approval.
An approved OF generates one PO per branch with a positive allocation, all in one transaction, once only.
Before generation the OF itself can be amended. After it, changes go through the branch POs and flow back.
"""
from __future__ import annotations

import json
from decimal import Decimal
from datetime import date, datetime, timedelta

from . import masters, orders
from .auth import User, require
from .db import POError, audit, get_setting, now, timestamp, today, tx
from .masters import clean_terms
from .money import (DISC_AMOUNT, DISC_NONE, DISC_PERCENT, VAT_EXCLUSIVE, VAT_NONE, compute_line, compute_order, fmt_peso, m_to_qty, parse_qty, round_centavos, split_by_weight,
                    sticker_deduction, qty_to_m)
from .orders import APPROVED, ARRANGEMENTS, CANCELLED, DRAFT, PENDING, STATUS_LABELS, clean_line, write_guard

OF_STATUSES = (DRAFT, PENDING, APPROVED, CANCELLED)
DRAFT_WARN_DAYS = 7
NEXT_SUBMIT, NEXT_APPROVE, NEXT_AMENDMENT, NEXT_PRINT = "Finish and submit", "Approve", "Decide the amendment", "Go to Print"
HEADER_FIELDS = ("of_date", "supplier_id", "expected_delivery_date", "delivery_arrangement", "payment_terms", "delivery_instructions",
                 "remarks", "warehouse_notes", "internal_notes", "overall_discount_type", "overall_discount_value", "vat_mode",
                 "vat_rate_bp", "other_charges_c", "other_charges_note")
ITEM_COLUMNS = orders.LINE_FIELDS + ("net_unit_c", "total_qty_m", "line_gross_c", "line_discount_c", "line_total_c")
# Safety limits only, far above any real order, so a typing slip or a bad import can't crash the program or reach the database.
MAX_AMOUNT_C = 10**13          # 100 billion pesos for one unit cost, charge or discount
MAX_TOTAL_C = 10**15           # for a line or a whole OF
MAX_PIECES = 10**9             # pieces of one product for one branch
MAX_PIECES_PER_UNIT = 10**6
MAX_FX_RATE_M = 10**12


def format_of_number(year: int, seq: int) -> str:
    return f"{year}-OF-{seq:05d}"


def _next_number(conn, year: int) -> tuple[str, int]:
    """Must run inside a write transaction, which BEGIN IMMEDIATE has already locked."""
    row = conn.execute("SELECT last_number FROM of_number_sequences WHERE year = ?", (year,)).fetchone()
    if row is None:
        seq = 1
        conn.execute("INSERT INTO of_number_sequences(year, last_number) VALUES (?, 1)", (year,))
    else:
        seq = row["last_number"] + 1
        conn.execute("UPDATE of_number_sequences SET last_number = ? WHERE year = ?", (seq, year))
    return format_of_number(year, seq), seq


def active_branches(conn) -> list:
    """The branches a new order can be allocated to."""
    return conn.execute("SELECT * FROM branches WHERE active = 1 AND kind = 'BRANCH' ORDER BY code").fetchall()


def _branch_names(conn) -> dict[int, str]:
    return {r["code"]: r["name"] for r in conn.execute("SELECT code, name FROM branches")}


def _check_branches_active(conn, codes: set[int]) -> None:
    known = {r["code"]: r for r in conn.execute("SELECT code, name, active, kind FROM branches")}
    for code in sorted(codes):
        branch = known.get(code)
        if branch is None:
            raise POError(f"Branch {code} doesn't exist.")
        if not branch["active"] or branch["kind"] != "BRANCH":
            raise POError(f"{branch['name']} is inactive, so it can't take an allocation. Clear its quantities, "
                          "or ask an administrator to reactivate the branch.")


def _clean_item(raw: dict) -> dict:
    try:
        item = clean_line(raw)
    except (TypeError, ValueError):
        raise POError(f"'{str(raw.get('description') or '').strip() or 'A row'}': the cost, discounts, pieces per unit and FX figures must be numbers.") from None
    who = item["description"]
    _check_line_numbers(item, who)
    allocations = {}
    try:
        if "allocations_m" in raw:
            allocations = {int(k): int(v) for k, v in raw["allocations_m"].items()}
            if any(v < 0 for v in allocations.values()):
                raise ValueError("Quantity can't be negative")
        else:
            allocations = {int(k): parse_qty(v, item["uom"]) for k, v in (raw.get("allocations") or {}).items()}
        ordered = raw.get("ordered_qty")
        ordered_m = parse_qty(ordered, item["uom"]) if str(ordered or "").strip() else None
    except ValueError as exc:
        raise POError(f"'{who}': {exc}") from None
    item["allocations_m"] = {k: v for k, v in allocations.items() if v > 0}
    if any(v * item["pieces_per_unit"] > MAX_PIECES * 1000 for v in item["allocations_m"].values()):
        raise POError(f"'{who}': a branch quantity is too large.")
    item["total_qty_m"] = sum(item["allocations_m"].values())
    # The order quantity is in pieces, like the total beside it. Branches are typed in the unit.
    pieces_m = item["total_qty_m"] * item["pieces_per_unit"]
    if ordered_m is not None and ordered_m != pieces_m:
        raise POError(f"'{who}': the branch quantities add up to {m_to_qty(pieces_m):,} pieces, "
                      f"but the order quantity is {m_to_qty(ordered_m):,}.")
    amounts = compute_line(1, item["unit_cost_c"], item["disc1_bp"], item["disc2_bp"])
    gross = discount = total = 0
    for qty_m in item["allocations_m"].values():
        line = compute_line(m_to_qty(qty_m), item["unit_cost_c"], item["disc1_bp"], item["disc2_bp"])
        gross, discount, total = gross + line.gross, discount + line.discount, total + line.total
    if gross > MAX_TOTAL_C:
        raise POError(f"'{who}': the row total is too large.")
    item.update(net_unit_c=amounts.net_unit, line_gross_c=gross, line_discount_c=discount, line_total_c=total)
    return item


def _check_line_numbers(item: dict, who: str) -> None:
    """Refuses a negative or absurd cost, price, unit size or FX figure before anything is saved."""
    if not 1 <= item["pieces_per_unit"] <= MAX_PIECES_PER_UNIT:
        raise POError(f"'{who}': pieces per unit must be a whole number from 1 to {MAX_PIECES_PER_UNIT:,}.")
    limits = (("unit_cost_c", "unit cost", MAX_AMOUNT_C), ("selling_c", "selling price", MAX_AMOUNT_C),
              ("fx_cost_m", "foreign cost", MAX_AMOUNT_C), ("fx_rate_m", "exchange rate", MAX_FX_RATE_M))
    for key, name, cap in limits:
        value = item.get(key)
        if value is None or value == "":
            continue
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise POError(f"'{who}': the {name} is not a number.") from None
        if value < 0:
            raise POError(f"'{who}': the {name} can't be negative.")
        if value > cap:
            raise POError(f"'{who}': the {name} is too large.")


def clean_date(value, what: str) -> str | None:
    """A real calendar date as YYYY-MM-DD, or None when blank. 20261101 becomes 2026-11-01; 'garbage' or 2026-02-30 is refused."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()).isoformat()
        except ValueError:
            pass
    raise POError(f"The {what} is not a real date. Use year-month-day, like 2026-11-07.")


def _clean_header(conn, raw: dict) -> dict:
    h = {k: raw.get(k) for k in HEADER_FIELDS}
    h["of_date"] = clean_date(h["of_date"], "OF date") or today().isoformat()
    h["expected_delivery_date"] = clean_date(h["expected_delivery_date"], "expected delivery date")
    h["delivery_arrangement"] = h["delivery_arrangement"] or "COMPLETE"
    if h["delivery_arrangement"] not in ARRANGEMENTS:
        raise POError("Choose Complete Delivery or Partial Delivery Allowed.")
    for key in ("payment_terms", "delivery_instructions", "remarks", "warehouse_notes", "internal_notes", "other_charges_note"):
        h[key] = str(h[key] or "").strip()
    h["overall_discount_type"] = h["overall_discount_type"] or DISC_NONE
    if h["overall_discount_type"] not in (DISC_NONE, DISC_PERCENT, DISC_AMOUNT):
        raise POError("Choose a valid overall discount type.")
    try:
        h["overall_discount_value"] = int(h["overall_discount_value"] or 0)
        h["other_charges_c"] = int(h["other_charges_c"] or 0)
        h["vat_rate_bp"] = int(h["vat_rate_bp"] if h["vat_rate_bp"] is not None else get_setting(conn, "tax.vat_rate_bp"))
    except (TypeError, ValueError):
        raise POError("The overall discount, other charges and VAT rate must be numbers.") from None
    for key, name in (("overall_discount_value", "overall discount"), ("other_charges_c", "other charges")):
        if h[key] < 0:
            raise POError(f"The {name} can't be negative.")
        if h[key] > MAX_AMOUNT_C:
            raise POError(f"The {name} is too large.")
    if not 0 <= h["vat_rate_bp"] <= 10000:
        raise POError("The VAT rate must be between 0% and 100%.")
    if not h["supplier_id"]:
        raise POError("Choose a supplier. One OF belongs to one supplier.")
    supplier = conn.execute("SELECT status, vat_mode, payment_terms FROM suppliers WHERE id=?", (h["supplier_id"],)).fetchone()
    if supplier is None:
        raise POError("That supplier no longer exists.")
    if supplier["status"] != "ACTIVE":
        raise POError("That supplier is inactive. Reactivate them first.")
    # For now: no order carries VAT. Suppliers who issue sales invoices are being checked first.
    h["vat_mode"] = VAT_NONE
    h["payment_terms"] = clean_terms(h["payment_terms"] or supplier["payment_terms"])
    return h


def calc_lines(items: list[dict]) -> list[dict]:
    return [{"department_id": i["department_id"], "pieces_per_unit": i["pieces_per_unit"], "unit_cost": i["unit_cost_c"],
             "disc1_bp": i["disc1_bp"], "disc2_bp": i["disc2_bp"],
             "allocations": {k: m_to_qty(v) for k, v in i["allocations_m"].items()}} for i in items]


def _totals(header: dict, items: list[dict]):
    try:
        totals = compute_order(calc_lines(items), header["overall_discount_type"], header["overall_discount_value"],
                               header["vat_mode"], header["vat_rate_bp"], header["other_charges_c"])
    except ValueError as exc:
        raise POError(str(exc)) from None
    if max(totals.gross, totals.grand_total) > MAX_TOTAL_C:
        raise POError("This OF's total is too large.")
    return totals


def approval_amount(conn, totals) -> int:
    """What the limit is compared with. By default the whole OF, so a big order can't slip through as small branch POs."""
    if orders.policy(conn)["basis"] == "LARGEST_BRANCH":
        return max((b.grand_total for b in totals.branches.values()), default=0)
    return totals.grand_total


def _store_totals(conn, of_id: int, t) -> None:
    conn.execute(
        "UPDATE order_forms SET total_qty_m=?, total_pieces_m=?, gross_c=?, item_discount_c=?, overall_discount_c=?, vat_c=?,"
        " grand_total_c=?, updated_at=? WHERE id=?",
        (qty_to_m(t.total_qty), qty_to_m(t.total_pieces), t.gross, t.item_discount, t.overall_discount, t.vat, t.grand_total, timestamp(), of_id))


def _insert_items(conn, of_id: int, items: list[dict]) -> None:
    marks = ", ".join("?" * len(ITEM_COLUMNS))
    sql = f"INSERT INTO order_form_items(of_id, line_no, {', '.join(ITEM_COLUMNS)}) VALUES (?, ?, {marks})"
    for n, item in enumerate(items, 1):
        cur = conn.execute(sql, [of_id, n, *[item[c] for c in ITEM_COLUMNS]])
        for code, qty_m in sorted(item["allocations_m"].items()):
            conn.execute("INSERT INTO order_form_allocations(of_item_id, branch_code, qty_m) VALUES (?,?,?)", (cur.lastrowid, code, qty_m))


def _delete_items(conn, of_id: int) -> None:
    conn.execute("DELETE FROM order_form_allocations WHERE of_item_id IN (SELECT id FROM order_form_items WHERE of_id = ?)", (of_id,))
    conn.execute("DELETE FROM order_form_items WHERE of_id = ?", (of_id,))


def _get(conn, of_id: int):
    row = conn.execute("SELECT * FROM order_forms WHERE id = ?", (of_id,)).fetchone()
    if row is None:
        raise POError("That OF no longer exists.")
    return row


def label(of) -> str:
    return of["of_number"] or f"Draft OF #{of['id']}"


def _load_items(conn, of_id: int) -> list[dict]:
    items = [dict(r) for r in conn.execute(
        "SELECT i.*, d.name AS department FROM order_form_items i JOIN departments d ON d.id = i.department_id"
        " WHERE i.of_id = ? ORDER BY i.department_id, i.line_no", (of_id,))]
    for item in items:
        rows = conn.execute("SELECT branch_code, qty_m FROM order_form_allocations WHERE of_item_id = ?", (item["id"],)).fetchall()
        item["allocations_m"] = {r["branch_code"]: r["qty_m"] for r in rows if r["qty_m"] > 0}
        item["allocations"] = {k: m_to_qty(v) for k, v in item["allocations_m"].items()}
        item["total_qty"] = m_to_qty(item["total_qty_m"])
    return items


def term_base(total_c: int, charges_c: int, vat_on_top_c: int, claim_stubs_c: int = 0) -> int:
    """What the term discount is taken on: the merchandise kept. Freight and other charges, VAT added on top, and goods sent back on claim stubs are left out."""
    return max(total_c - charges_c - vat_on_top_c - claim_stubs_c, 0)


def get_of(conn, of_id: int) -> dict:
    of = dict(_get(conn, of_id))
    of["items"] = _load_items(conn, of_id)
    if of["supplier_snapshot"]:
        of["supplier"] = json.loads(of["supplier_snapshot"])
    else:
        of["supplier"] = dict(conn.execute("SELECT * FROM suppliers WHERE id = ?", (of["supplier_id"],)).fetchone())
    for key in ("prepared_by", "approved_by"):
        who = conn.execute("SELECT full_name FROM users WHERE id = ?", (of[key],)).fetchone() if of[key] else None
        of[f"{key}_name"] = who["full_name"] if who else ""
    of["total_qty"] = m_to_qty(of["total_qty_m"])
    # What comes off the supplier's payment. It never changes the OF total or what the PO says, and a future CR works from pieces received.
    of["sticker_rate_c"] = of["supplier"].get("sticker_rate_c") or 0
    of["sticker_deduction_c"] = sticker_deduction(m_to_qty(of["total_pieces_m"]), of["sticker_rate_c"])
    of["term_discount_bp"] = of["supplier"].get("term_discount_bp") or 0
    # The term discount is on the merchandise only: never on freight or other charges, or on VAT added on top.
    of["term_discount_c"] = round_centavos(Decimal(term_base(of["grand_total_c"], of["other_charges_c"], of["vat_c"] if of["vat_mode"] == VAT_EXCLUSIVE else 0)) * of["term_discount_bp"] / 10000)
    of["net_payable_c"] = of["grand_total_c"] - of["sticker_deduction_c"] - of["term_discount_c"]
    of["pos"] = [dict(r) for r in conn.execute(
        "SELECT p.id, p.po_number, p.branch_code, p.branch_name, p.total_qty_m, p.total_pieces_m, p.grand_total_c, p.status, p.issued_at, p.revision, p.issued_revision, p.other_charges_c, p.vat_c, p.vat_mode"
        " FROM purchase_orders p WHERE p.of_id = ? ORDER BY p.branch_code", (of_id,))]
    # Columns on screen and on paper, at every stage: every active branch, ordered from or not, plus any other that holds a quantity or a PO on this OF.
    names = _branch_names(conn)
    codes = {r["code"] for r in active_branches(conn)}
    for item in of["items"]:
        codes |= set(item["allocations_m"])
    codes |= {p["branch_code"] for p in of["pos"]}
    of["branch_columns"] = [{"code": c, "name": names[c]} for c in sorted(codes)]
    return of


def create_draft(conn, user: User, header: dict, items: list[dict]) -> int:
    require(user, "of.edit")
    with tx(conn):
        h = _clean_header(conn, header)
        clean = [_clean_item(i) for i in items]
        _check_branches_active(conn, {c for i in clean for c in i["allocations_m"]})
        masters.check_skus(conn, h["supplier_id"], clean)
        totals = _totals(h, clean)
        stamp = timestamp()
        cols = ", ".join(HEADER_FIELDS)
        cur = conn.execute(f"INSERT INTO order_forms({cols}, prepared_by, created_at, updated_at)"
                           f" VALUES ({', '.join('?' * len(HEADER_FIELDS))}, ?, ?, ?)",
                           [h[k] for k in HEADER_FIELDS] + [user.id, stamp, stamp])
        of_id = cur.lastrowid
        _insert_items(conn, of_id, clean)
        _store_totals(conn, of_id, totals)
        audit(conn, user, "OF_CREATE", "order_form", of_id, of_id=of_id,
              summary=f"Draft created, {len(clean)} products, {fmt_peso(totals.grand_total)}")
    return of_id


def _snapshot(conn, of_id: int) -> dict:
    of = dict(_get(conn, of_id))
    keep = ("of_number", "status", "revision", "grand_total_c", "total_qty_m") + HEADER_FIELDS
    items = _load_items(conn, of_id)
    for item in items:
        item.pop("allocations", None)
        item.pop("total_qty", None)
    return {"header": {k: of[k] for k in keep}, "items": items}


def _allocation_view(items: list[dict]) -> list:
    return [(i["description"], sorted((int(k), v) for k, v in i["allocations_m"].items())) for i in items]


def update_draft(conn, user: User, of_id: int, header: dict, items: list[dict]) -> None:
    require(user, "of.edit")
    with tx(conn):
        old = _get(conn, of_id)
        if old["status"] != DRAFT:
            raise POError(f"{label(old)} is {STATUS_LABELS[old['status']]}. Use an amendment to change it.")
        before = _snapshot(conn, of_id)
        h = _clean_header(conn, header)
        clean = [_clean_item(i) for i in items]
        _check_branches_active(conn, {c for i in clean for c in i["allocations_m"]})
        masters.check_skus(conn, h["supplier_id"], clean)
        totals = _totals(h, clean)
        sets =", ".join(f"{k} = ?" for k in HEADER_FIELDS)
        conn.execute(f"UPDATE order_forms SET {sets} WHERE id = ?", [h[k] for k in HEADER_FIELDS] + [of_id])
        _delete_items(conn, of_id)
        _insert_items(conn, of_id, clean)
        _store_totals(conn, of_id, totals)
        after = _snapshot(conn, of_id)
        audit(conn, user, "OF_MODIFY", "order_form", of_id, of_id=of_id,
              summary=f"Draft changed, now {len(clean)} products, {fmt_peso(totals.grand_total)}", previous=before, new=after)
        if _allocation_view(before["items"]) != _allocation_view(after["items"]):
            audit(conn, user, "OF_ALLOCATION_CHANGE", "order_form", of_id, of_id=of_id, summary="Branch allocations changed",
                  previous=_allocation_view(before["items"]), new=_allocation_view(after["items"]))


def _record_approval(conn, of_id, decision, amount, by=None, reason="", remarks="", amendment_id=None) -> None:
    p = orders.policy(conn)
    conn.execute(
        "INSERT INTO of_approvals(of_id, amendment_id, decision, decided_by, decided_at, amount_c, basis, threshold_c, policy_enabled, reason, remarks)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (of_id, amendment_id, decision, by, timestamp(), amount, p["basis"], p["threshold_c"], int(p["enabled"]), reason, remarks))


def _make_approved(conn, user: User, of, approver_id: int | None, auto: bool) -> str:
    _record_new_products(conn, user, of)
    moment = now()
    number, seq = _next_number(conn, moment.year)
    supplier = dict(conn.execute("SELECT * FROM suppliers WHERE id = ?", (of["supplier_id"],)).fetchone())
    conn.execute(
        "UPDATE order_forms SET status=?, of_number=?, of_year=?, of_seq=?, approved_by=?, approval_date=?, auto_approved=?,"
        " supplier_snapshot=?, updated_at=? WHERE id=?",
        (APPROVED, number, moment.year, seq, approver_id, moment.isoformat(sep=" ", timespec="seconds"), int(auto),
         json.dumps(supplier, ensure_ascii=False, default=str), timestamp(), of["id"]))
    return number


def _record_new_products(conn, user: User, of) -> None:
    """A SKU only joins the catalog once its order is approved, so a draft or a rejected OF leaves nothing behind."""
    items = _load_items(conn, of["id"])
    masters.record_products(conn, user, of["supplier_id"], items)
    with write_guard(conn):
        for item in items:
            if item["product_id"]:
                conn.execute("UPDATE order_form_items SET product_id = ? WHERE id = ? AND product_id IS NULL", (item["product_id"], item["id"]))


def submit_order(conn, user: User, of_id: int) -> list[tuple[int, str]]:
    """Send a draft on as one OF per department, each with its own number and POs. Returns (OF id, status) for each, lowest department first.

    All or nothing, in one transaction. Whether approval is needed is decided on the whole order, so a large order can't slip under the limit by being split.
    """
    require(user, "of.edit")
    with tx(conn):
        of = _get(conn, of_id)
        if of["status"] != DRAFT:
            raise POError(f"{label(of)} has already been submitted.")
        items = _load_items(conn, of_id)
        groups: dict[int, list[dict]] = {}
        for item in items:
            groups.setdefault(item["department_id"], []).append(item)
        if len(groups) < 2:
            return [(of_id, submit(conn, user, of_id))]
        departments = sorted(groups)
        whole_order = approval_amount(conn, _totals(of, items))
        # Split to the centavo by each department's value with the one rule used everywhere (largest remainder), so the parts add back up.
        values = {n: sum(i["line_total_c"] for i in groups[d]) for n, d in enumerate(departments)}
        amount_off = split_by_weight(of["overall_discount_value"], values) if of["overall_discount_type"] == DISC_AMOUNT else None
        charges = split_by_weight(of["other_charges_c"], values)
        ids = []
        for n, dept in enumerate(departments):
            header = {k: of[k] for k in HEADER_FIELDS} | {"other_charges_c": charges[n]}
            if amount_off:
                header["overall_discount_value"] = amount_off[n]
            if n == 0:
                update_draft(conn, user, of_id, header, groups[dept])
                ids.append(of_id)
            else:
                ids.append(create_draft(conn, user, header, groups[dept]))
        audit(conn, user, "OF_SPLIT", "order_form", of_id, of_id=of_id, summary=f"Split by department into {len(ids)} Order Forms", new={"draft_ids": ids})
        return [(i, submit(conn, user, i, whole_order)) for i in ids]


def submit(conn, user: User, of_id: int, judge_on: int | None = None) -> str:
    """Send a draft on. Returns the new status: Pending Approval, or Approved with its number. judge_on is the amount that decides approval, if not this OF's own."""
    require(user, "of.edit")
    with tx(conn):
        of = _get(conn, of_id)
        if of["status"] != DRAFT:
            raise POError(f"{label(of)} has already been submitted.")
        items = _load_items(conn, of_id)
        if not items:
            raise POError("Add at least one product before submitting.")
        for item in items:
            if item["total_qty_m"] <= 0:
                raise POError(f"'{item['description']}' has no branch quantities. Allocate it, or remove the row.")
            if item["total_qty_m"] != sum(item["allocations_m"].values()):
                raise POError(f"'{item['description']}': the total differs from its branch quantities.")
        _check_branches_active(conn, {c for i in items for c in i["allocations_m"]})
        totals = _totals(of, items)
        # Recorded is the amount actually compared with the limit: the whole order's when this OF is part of a split one.
        amount = approval_amount(conn, totals) if judge_on is None else judge_on
        # An OF that waited for approval before, because it was rejected back to Draft or is part of a split order, always waits again.
        # Judged on its own smaller amount it could otherwise slip through without the approver seeing it twice.
        waited = conn.execute("SELECT 1 FROM of_approvals WHERE of_id = ? AND decision = 'SUBMITTED' AND amendment_id IS NULL", (of_id,)).fetchone() is not None
        if waited or orders.needs_approval(conn, amount):
            if not orders.policy(conn)["approver_user_ids"]:
                raise POError("No approver is set. An administrator must choose one in Settings.")
            conn.execute("UPDATE order_forms SET status=?, updated_at=? WHERE id=?", (PENDING, timestamp(), of_id))
            _record_approval(conn, of_id, "SUBMITTED", amount, user.id)
            audit(conn, user, "OF_SUBMIT", "order_form", of_id, of_id=of_id, summary=f"Submitted for approval, {fmt_peso(amount)}")
            return PENDING
        number = _make_approved(conn, user, of, None, auto=True)
        _record_approval(conn, of_id, "AUTO_APPROVED", amount, None, remarks="No approval required by policy")
        audit(conn, user, "OF_AUTO_APPROVE", "order_form", of_id, of_id=of_id, summary=f"{number} approved automatically, {fmt_peso(amount)}")
        return APPROVED


def _submitted_amount(conn, of) -> int:
    """The amount this OF was judged on when it was last submitted, so the decision records the same figure."""
    row = conn.execute("SELECT amount_c FROM of_approvals WHERE of_id = ? AND decision = 'SUBMITTED' AND amendment_id IS NULL ORDER BY id DESC LIMIT 1",
                       (of["id"],)).fetchone()
    return row["amount_c"] if row else of["grand_total_c"]


def _check_approver(conn, user: User, requested_by: int, of_id: int | None = None) -> None:
    require(user, "of.approve")
    p = orders.policy(conn)
    orders.require_listed_approver(conn, user)
    if p["allow_self_approval"]:
        return
    if requested_by == user.id:
        raise POError("You prepared this, so someone else has to approve it.")
    # Editing or submitting someone else's draft counts as preparing it, or an approver could rewrite a draft and approve it themselves.
    if of_id is not None and conn.execute("SELECT 1 FROM audit_logs WHERE of_id = ? AND user_id = ? AND action IN ('OF_CREATE', 'OF_MODIFY', 'OF_ALLOCATION_CHANGE', 'OF_SUBMIT', 'OF_SPLIT')",
                                          (of_id, user.id)).fetchone():
        raise POError("You worked on this OF, so someone else has to approve it.")


def approve(conn, user: User, of_id: int, remarks: str = "") -> str:
    with tx(conn):
        of = _get(conn, of_id)
        if of["status"] != PENDING:
            raise POError(f"{label(of)} is not waiting for approval.")
        _check_approver(conn, user, of["prepared_by"], of_id)
        number = _make_approved(conn, user, of, user.id, auto=False)
        _record_approval(conn, of_id, "APPROVED", _submitted_amount(conn, of), user.id, remarks=remarks)
        audit(conn, user, "OF_APPROVE", "order_form", of_id, of_id=of_id, summary=f"{number} approved, {fmt_peso(of['grand_total_c'])}",
              new={"remarks": remarks})
    return number


def reject(conn, user: User, of_id: int, reason: str, remarks: str = "") -> None:
    if not reason.strip():
        raise POError("Write the reason for rejecting this OF.")
    with tx(conn):
        of = _get(conn, of_id)
        if of["status"] != PENDING:
            raise POError(f"{label(of)} is not waiting for approval.")
        _check_approver(conn, user, of["prepared_by"], of_id)
        conn.execute("UPDATE order_forms SET status=?, updated_at=? WHERE id=?", (DRAFT, timestamp(), of_id))
        _record_approval(conn, of_id, "REJECTED", _submitted_amount(conn, of), user.id, reason=reason.strip(), remarks=remarks)
        audit(conn, user, "OF_REJECT", "order_form", of_id, of_id=of_id, summary=f"Rejected and returned to draft: {reason.strip()}")


def can_cancel(conn, of_id: int) -> bool:
    """Whether cancel() would go ahead: every live PO under it could still be cancelled."""
    of = conn.execute("SELECT status FROM order_forms WHERE id = ?", (of_id,)).fetchone()
    if of is None or of["status"] == CANCELLED:
        return False
    return all(orders.can_cancel(conn, p["id"]) for p in conn.execute("SELECT id FROM purchase_orders WHERE of_id = ? AND status <> 'CANCELLED'", (of_id,)))


def generate_pos(conn, user: User, of_id: int) -> list[dict]:
    """One PO per branch with a positive allocation, each holding only that branch's quantities.

    All in one transaction. Running it again changes nothing and returns the POs that already exist,
    so a double click can't make duplicates; ux_po_of_branch in the database stands behind that too.
    """
    require(user, "of.generate")
    with tx(conn):
        of = _get(conn, of_id)
        if of["pos_generated_at"]:
            return get_of(conn, of_id)["pos"]
        if of["status"] != APPROVED:
            raise POError("Only an approved OF can generate branch POs.")
        if conn.execute("SELECT 1 FROM of_amendments WHERE of_id=? AND status='PENDING_APPROVAL'", (of_id,)).fetchone():
            raise POError("An amendment is waiting for approval. Decide on it first.")
        supplier = conn.execute("SELECT status FROM suppliers WHERE id=?", (of["supplier_id"],)).fetchone()
        if supplier["status"] != "ACTIVE":
            raise POError("This OF's supplier has been made inactive. Reactivate them first.")
        items = _load_items(conn, of_id)
        for item in items:
            if item["total_qty_m"] != sum(item["allocations_m"].values()):
                raise POError(f"'{item['description']}': the total differs from its branch quantities.")
        codes = {c for i in items for c in i["allocations_m"]}
        if not codes:
            raise POError("No branch has a quantity on this OF.")
        _check_branches_active(conn, codes)
        totals = _totals(of, items)
        moment = now()
        stamp = timestamp()
        day = moment.date().isoformat()
        branches = {r["code"]: r for r in conn.execute("SELECT * FROM branches")}
        made = []
        with write_guard(conn):
            for code in sorted(codes):
                number, seq = orders.next_po_number(conn, moment.year, code)
                share = totals.overall_shares[code]
                cur = conn.execute(
                    "INSERT INTO purchase_orders(po_number, po_year, po_seq, po_date, of_id, supplier_id, supplier_snapshot, branch_code, branch_name,"
                    " branch_address, expected_delivery_date, delivery_arrangement, payment_terms, delivery_instructions, status, remarks,"
                    " overall_discount_type, overall_discount_value, vat_mode, vat_rate_bp, other_charges_c, other_charges_note,"
                    " prepared_by, approved_by, approval_date, auto_approved, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (number, moment.year, seq, day, of_id, of["supplier_id"], of["supplier_snapshot"], code, branches[code]["name"],
                     branches[code]["address"], of["expected_delivery_date"], of["delivery_arrangement"], of["payment_terms"],
                     of["delivery_instructions"], APPROVED, of["remarks"], DISC_AMOUNT if share else DISC_NONE, share, of["vat_mode"],
                     of["vat_rate_bp"], totals.charge_shares[code], of["other_charges_note"] if totals.charge_shares[code] else "",
                     of["prepared_by"], of["approved_by"], of["approval_date"], of["auto_approved"], stamp, stamp))
                po_id = cur.lastrowid
                lines = []
                for item in items:
                    qty_m = item["allocations_m"].get(code)
                    if not qty_m:
                        continue
                    amounts = compute_line(m_to_qty(qty_m), item["unit_cost_c"], item["disc1_bp"], item["disc2_bp"])
                    allocation = conn.execute("SELECT id FROM order_form_allocations WHERE of_item_id=? AND branch_code=?",
                                              (item["id"], code)).fetchone()["id"]
                    lines.append({**{k: item[k] for k in orders.LINE_FIELDS}, "qty_m": qty_m, "net_unit_c": amounts.net_unit,
                                  "line_gross_c": amounts.gross, "line_discount_c": amounts.discount, "line_total_c": amounts.total,
                                  "of_item_id": item["id"], "of_allocation_id": allocation})
                orders.insert_items(conn, po_id, lines)
                orders.store_totals(conn, po_id, totals.branches[code])
                decision = "AUTO_APPROVED" if of["auto_approved"] else "APPROVED"
                orders._record_approval(conn, po_id, decision, totals.branches[code].grand_total, of["approved_by"],
                                        remarks=f"Inherited from {of['of_number']}")
                audit(conn, user, "PO_GENERATE", "purchase_order", po_id, po_id=po_id,
                      summary=f"{number} generated from {of['of_number']} for {branches[code]['name']}, {fmt_peso(totals.branches[code].grand_total)}")
                made.append(number)
            conn.execute("UPDATE order_forms SET pos_generated_at=?, pos_generated_by=?, updated_at=? WHERE id=?", (stamp, user.id, stamp, of_id))
        audit(conn, user, "OF_GENERATE_POS", "order_form", of_id, of_id=of_id, summary=f"Generated {len(made)} branch POs: {', '.join(made)}", new=made)
        return get_of(conn, of_id)["pos"]


def cancel(conn, user: User, of_id: int, reason: str) -> None:
    """Cancels the OF and every branch PO under it. Numbers stay used. Refused if any goods have arrived."""
    require(user, "of.cancel")
    if not reason.strip():
        raise POError("Write the reason for cancelling this OF.")
    with tx(conn):
        of = _get(conn, of_id)
        if of["status"] == CANCELLED:
            raise POError(f"{label(of)} is already cancelled.")
        if conn.execute("SELECT 1 FROM of_amendments WHERE of_id = ? AND status = 'PENDING_APPROVAL' UNION ALL SELECT 1 FROM po_amendments a JOIN purchase_orders p ON p.id = a.po_id"
                        " WHERE p.of_id = ? AND a.status = 'PENDING_APPROVAL'", (of_id, of_id)).fetchone():
            raise POError("An amendment is waiting for approval. The approver has to decide on it, or reject it, before this OF can be cancelled.")
        pos = conn.execute("SELECT id, po_number, status FROM purchase_orders WHERE of_id=?", (of_id,)).fetchall()
        for po in pos:
            if po["status"] in (orders.PARTIAL, orders.FULL, orders.CLOSED):
                raise POError(f"{po['po_number']} is {STATUS_LABELS[po['status']]}, so this OF can't be cancelled as a whole.")
        for po in pos:
            if po["status"] != CANCELLED:
                orders.cancel(conn, user, po["id"], f"OF cancelled: {reason.strip()}", sync_of=False)
        stamp = timestamp()
        conn.execute("UPDATE order_forms SET status=?, cancelled_at=?, cancelled_by=?, cancel_reason=?, updated_at=? WHERE id=?",
                     (CANCELLED, stamp, user.id, reason.strip(), stamp, of_id))
        audit(conn, user, "OF_CANCEL", "order_form", of_id, of_id=of_id, summary=f"{label(of)} cancelled: {reason.strip()}",
              previous={"status": of["status"]})
        if of["status"] == APPROVED:
            # Its prices set the catalog cost on approval; a cancelled order leaves none behind.
            masters.recost_after_cancel(conn, user, [r[0] for r in conn.execute("SELECT product_id FROM order_form_items WHERE of_id = ? AND product_id IS NOT NULL", (of_id,))])


# ------------------------------------------------------------------ amendments, before POs exist

def amend(conn, user: User, of_id: int, header: dict, items: list[dict], reason: str) -> dict:
    """Formal change to an approved OF whose POs aren't generated yet. Once they are, amend the branch POs.

    More quantity at any branch, a new row, or a higher total goes to the approver while the policy is on.
    Returns {'amendment_id', 'status', 'revision'}.
    """
    require(user, "of.amend")
    if not reason.strip():
        raise POError("Write the reason for this amendment.")
    with tx(conn):
        of = _get(conn, of_id)
        if of["status"] != APPROVED:
            raise POError(f"An OF that is {STATUS_LABELS[of['status']]} can't be amended.")
        if of["pos_generated_at"]:
            raise POError("The branch POs already exist. Amend the PO of the branch that is changing.")
        if conn.execute("SELECT 1 FROM of_amendments WHERE of_id=? AND status='PENDING_APPROVAL'", (of_id,)).fetchone():
            raise POError("An earlier amendment is still waiting for approval.")
        # The OF date never changes on an amendment, so one saved before dates were checked doesn't block it.
        h = _clean_header(conn, {**dict(of), **header, "supplier_id": of["supplier_id"], "of_date": None}) | {"of_date": of["of_date"]}
        clean = [_clean_item(i) for i in items]
        # An OF is for one department. An older OF with several can keep those, but none can gain another.
        kept = {i["department_id"] for i in _load_items(conn, of_id)}
        if {i["department_id"] for i in clean} - kept:
            raise POError("An approved OF can't take on another department. Put those products on a new OF.")
        for item in clean:
            if item["total_qty_m"] <= 0:
                raise POError(f"'{item['description']}' has no branch quantities. Allocate it, or remove the row.")
        if not clean:
            raise POError("An OF needs at least one product. To drop everything, cancel the OF.")
        _check_branches_active(conn, {c for i in clean for c in i["allocations_m"]})
        masters.check_skus(conn, h["supplier_id"], clean)
        totals = _totals(h, clean)
        before = _snapshot(conn, of_id)
        moved = {k for k in HEADER_FIELDS if k not in ("supplier_id", "of_date") and (h.get(k) or None) != (of[k] or None)}
        same_items = _item_view(clean) == _item_view(before["items"])
        if same_items and not moved:
            raise POError("Nothing changed.")
        if same_items and moved <= set(UNPRINTED_HEADER):
            # Internal notes never print, so changing only them makes no new revision, the same as on a PO.
            with write_guard(conn):
                for k in moved:
                    conn.execute(f"UPDATE order_forms SET {k} = ?, updated_at = ? WHERE id = ?", (h[k], timestamp(), of_id))
            audit(conn, user, "OF_NOTES", "order_form", of_id, of_id=of_id, summary=f"{of['of_number']}: internal notes changed: {reason.strip()}",
                  previous={k: of[k] for k in moved}, new={k: h[k] for k in moved})
            return {"amendment_id": None, "status": "APPLIED", "revision": of["revision"]}
        old = {(i["item_code"], i["description"], i["size"], i["color"]): i for i in before["items"]}
        increased = False
        for item in clean:
            prior = old.get((item["item_code"], item["description"], item["size"], item["color"]))
            # Quantities and costs are compared per piece, so changing the unit, say a dozen to a piece at the same money, can't hide a change.
            ppu, prior_ppu = item["pieces_per_unit"] or 1, (prior or {}).get("pieces_per_unit") or 1
            if prior is None or any(q * ppu > prior["allocations_m"].get(c, 0) * prior_ppu for c, q in item["allocations_m"].items()):
                increased = True
            # A higher price or a smaller discount costs more, even when a quantity drops elsewhere.
            elif (item["unit_cost_c"] or 0) * prior_ppu > (prior["unit_cost_c"] or 0) * ppu or (item["disc1_bp"] or 0) < (prior["disc1_bp"] or 0) or (item["disc2_bp"] or 0) < (prior["disc2_bp"] or 0):
                increased = True
        # Shorter terms bring the check date earlier, and the order-level money fields change what's paid.
        # Dropping VAT under the no-VAT rule never costs more, so it alone doesn't need approval.
        if any((h.get(k) or None) != (of[k] or None) for k in ("payment_terms", "overall_discount_type", "overall_discount_value", "other_charges_c")):
            increased = True
        after = {"header": h, "items": clean, "grand_total_c": totals.grand_total}
        cur = conn.execute(
            "INSERT INTO of_amendments(of_id, status, reason, before_json, after_json, requested_by, requested_at) VALUES (?,?,?,?,?,?,?)",
            (of_id, PENDING, reason.strip(), json.dumps(before, default=str), json.dumps(after, default=str), user.id, timestamp()))
        amendment_id = cur.lastrowid
        p = orders.policy(conn)
        if p["enabled"] and (increased or totals.grand_total > of["grand_total_c"]):
            if not p["approver_user_ids"]:
                raise POError("No approver is set. An administrator must choose one in Settings.")
            _record_approval(conn, of_id, "SUBMITTED", totals.grand_total, user.id, amendment_id=amendment_id, reason=reason.strip())
            audit(conn, user, "OF_AMEND_REQUEST", "order_form", of_id, of_id=of_id, summary=f"Amendment requested: {reason.strip()}",
                  previous=before["header"], new=h)
            return {"amendment_id": amendment_id, "status": PENDING, "revision": of["revision"]}
        revision = _apply_amendment(conn, user, of, amendment_id, after, before)
        _record_approval(conn, of_id, "AUTO_APPROVED", totals.grand_total, None, amendment_id=amendment_id,
                         remarks="Amendment needs no approval: no increase, or approval policy off")
        return {"amendment_id": amendment_id, "status": "APPLIED", "revision": revision}


UNPRINTED_HEADER = ("internal_notes",)


def _item_view(items: list[dict]) -> list:
    """The rows as a comparable value, ignoring their order and the catalog link that approval fills in."""
    view = []
    for i in items:
        fields = tuple((c, i.get(c) or None) for c in ITEM_COLUMNS if c != "product_id")
        view.append(repr((fields, sorted((int(k), v) for k, v in i["allocations_m"].items() if v))))
    return sorted(view)


def _apply_amendment(conn, user, of, amendment_id: int, after: dict, before: dict) -> int:
    of_id = of["id"]
    for item in after["items"]:  # JSON turned branch codes into text
        item["allocations_m"] = {int(k): v for k, v in item["allocations_m"].items()}
    masters.record_products(conn, user, of["supplier_id"], after["items"])
    with write_guard(conn):
        fields = [k for k in HEADER_FIELDS if k not in ("supplier_id", "of_date")]
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE order_forms SET {sets} WHERE id = ?", [after["header"][k] for k in fields] + [of_id])
        _delete_items(conn, of_id)
        _insert_items(conn, of_id, after["items"])
        totals = _totals(after["header"], after["items"])
        _store_totals(conn, of_id, totals)
        revision = of["revision"] + 1
        conn.execute("UPDATE order_forms SET revision = ? WHERE id = ?", (revision, of_id))
        conn.execute("UPDATE of_amendments SET status='APPLIED', revision=?, decided_at=?, decided_by=? WHERE id=?",
                     (revision, timestamp(), user.id, amendment_id))
        audit(conn, user, "OF_AMEND", "order_form", of_id, of_id=of_id,
              summary=f"{of['of_number']} revision {revision}, now {fmt_peso(totals.grand_total)}", previous=before, new=after)
        previous, current = _allocation_view(before["items"]), _allocation_view(after["items"])
        if previous != current:
            audit(conn, user, "OF_ALLOCATION_CHANGE", "order_form", of_id, of_id=of_id, summary="Branch allocations changed by amendment",
                  previous=previous, new=current)
    return revision


def decide_amendment(conn, user: User, amendment_id: int, approved: bool, reason: str = "") -> None:
    with tx(conn):
        a = conn.execute("SELECT * FROM of_amendments WHERE id = ?", (amendment_id,)).fetchone()
        if a is None or a["status"] != PENDING:
            raise POError("That amendment is not waiting for approval.")
        of = _get(conn, a["of_id"])
        _check_approver(conn, user, a["requested_by"])
        after = json.loads(a["after_json"])
        if approved and (of["status"] != APPROVED or of["pos_generated_at"]):
            raise POError(f"{label(of)} is {STATUS_LABELS[of['status']].lower() if of['status'] != APPROVED else 'already printed as branch POs'}, so this amendment can't be applied. Reject it.")
        if approved:
            _apply_amendment(conn, user, of, amendment_id, after, json.loads(a["before_json"]))
            _record_approval(conn, of["id"], "APPROVED", after["grand_total_c"], user.id, amendment_id=amendment_id, remarks=reason)
            return
        if not reason.strip():
            raise POError("Write the reason for rejecting this amendment.")
        conn.execute("UPDATE of_amendments SET status='REJECTED', decided_by=?, decided_at=? WHERE id=?", (user.id, timestamp(), amendment_id))
        _record_approval(conn, of["id"], "REJECTED", after["grand_total_c"], user.id, amendment_id=amendment_id, reason=reason.strip())
        audit(conn, user, "OF_AMEND_REJECT", "order_form", of["id"], of_id=of["id"], summary=f"Amendment rejected: {reason.strip()}")


# ------------------------------------------------------------------ lists

def delivery_status(of_status: str, po_statuses: list[str], any_received: bool) -> str:
    """Derived from the branch POs. A recorded DR alone never changes this; only warehouse counts will."""
    if of_status == CANCELLED:
        return "Cancelled"
    if not po_statuses:
        return "POs not generated"
    live = [s for s in po_statuses if s != CANCELLED]
    if not live:
        return "All POs cancelled"
    if all(s in (orders.FULL, orders.CLOSED) for s in live):
        return "Completed"
    if any_received or orders.PARTIAL in live:
        return "Partially delivered"
    if orders.ISSUED in live:
        return "Awaiting delivery"
    return "Not yet issued"


def draft_is_old(of) -> bool:
    """A warning only: an old draft is never changed or removed. Saving it, or having it rejected back to Draft, restarts the count."""
    return of["status"] == DRAFT and now() - datetime.fromisoformat(of["updated_at"]) > timedelta(days=DRAFT_WARN_DAYS)


def register_tab(of_status: str, po_statuses: list[str], unsent: int = 0) -> str:
    """An OF is Completed once every branch PO that wasn't cancelled has been issued, and every amended one issued again."""
    live = [s for s in po_statuses if s != CANCELLED]
    if of_status == CANCELLED or (po_statuses and not live):
        return CANCELLED
    if unsent:
        return orders.OPEN
    return orders.COMPLETED if live and all(s in orders.PO_TAB_STATUSES[orders.COMPLETED] for s in live) else orders.OPEN


def next_step(of_status: str, tab: str, amendments_waiting: int, delivery: str) -> str:
    """What has to happen next to this order, in words. Anyone reading a list, person or program, needs nothing else to decide."""
    if of_status == CANCELLED or tab == CANCELLED:
        return ""
    if amendments_waiting:
        return NEXT_AMENDMENT
    if of_status == DRAFT:
        return NEXT_SUBMIT
    if of_status == PENDING:
        return NEXT_APPROVE
    return NEXT_PRINT if tab == orders.OPEN else delivery


def search(conn, *, text: str = "", supplier_id=None, department_id=None, branch_code=None, status=None, year=None, date_from=None, date_to=None, tab=None,
           limit: int = 2000, product: str = "") -> list[dict]:
    where, args = [], []
    if product:
        where.append("EXISTS (SELECT 1 FROM order_form_items i WHERE i.of_id = f.id AND (i.item_code LIKE ? OR i.description LIKE ?))")
        args += [f"%{product}%"] * 2
    if branch_code:
        # Not a correlated EXISTS: without table statistics SQLite drove that from the branch index, quadratic in the number of OFs.
        where.append("f.id IN (SELECT i.of_id FROM order_form_allocations a JOIN order_form_items i ON i.id = a.of_item_id WHERE a.branch_code = ? AND a.qty_m > 0)")
        args.append(branch_code)
    for like in (f"%{word}%" for word in text.split()):
        where.append("(f.of_number LIKE ? OR s.name LIKE ? OR s.code LIKE ? OR EXISTS (SELECT 1 FROM purchase_orders p WHERE p.of_id = f.id AND p.po_number LIKE ?)"
                     " OR EXISTS (SELECT 1 FROM delivery_receipts d JOIN purchase_orders p ON p.id = d.po_id WHERE p.of_id = f.id AND d.dr_number LIKE ?))")
        args += [like] * 5
    if supplier_id:
        where.append("f.supplier_id = ?")
        args.append(supplier_id)
    if status:
        where.append("f.status = ?")
        args.append(status)
    if year:
        where.append("substr(f.of_date, 1, 4) = ?")
        args.append(str(year))
    if date_from:
        where.append("f.of_date >= ?")
        args.append(date_from)
    if date_to:
        where.append("f.of_date <= ?")
        args.append(date_to)
    if department_id:
        where.append("EXISTS (SELECT 1 FROM order_form_items i WHERE i.of_id = f.id AND i.department_id = ?)")
        args.append(department_id)
    sql = ("SELECT f.*, s.name AS supplier, s.code AS supplier_code,"
           " (SELECT COUNT(*) FROM order_form_items i WHERE i.of_id = f.id) AS product_count,"
           " (SELECT GROUP_CONCAT(DISTINCT i.department_id) FROM order_form_items i WHERE i.of_id = f.id) AS depts,"
           " (SELECT COUNT(DISTINCT a.branch_code) FROM order_form_allocations a JOIN order_form_items i ON i.id = a.of_item_id"
           "   WHERE i.of_id = f.id AND a.qty_m > 0) AS branch_count,"
           " (SELECT COUNT(*) FROM purchase_orders p WHERE p.of_id = f.id) AS po_count,"
           " (SELECT GROUP_CONCAT(p.status) FROM purchase_orders p WHERE p.of_id = f.id) AS po_statuses,"
           " (SELECT MAX(p.issued_at) FROM purchase_orders p WHERE p.of_id = f.id"
           "   AND p.status IN ('ISSUED','PARTIALLY_DELIVERED','FULLY_DELIVERED','CLOSED')) AS last_issued_at,"
           " (SELECT COALESCE(SUM(i.received_qty_m),0) FROM purchase_order_items i JOIN purchase_orders p ON p.id = i.po_id WHERE p.of_id = f.id) AS received_m,"
           " (SELECT COUNT(*) FROM purchase_orders p WHERE p.of_id = f.id AND p.status IN ('ISSUED','PARTIALLY_DELIVERED') AND p.issued_revision IS NOT NULL AND p.revision > p.issued_revision) AS unsent,"
           " (SELECT COUNT(*) FROM of_amendments a WHERE a.of_id = f.id AND a.status = 'PENDING_APPROVAL')"
           " + (SELECT COUNT(*) FROM po_amendments a JOIN purchase_orders p ON p.id = a.po_id WHERE p.of_id = f.id AND a.status = 'PENDING_APPROVAL') AS amendments_waiting"
           " FROM order_forms f JOIN suppliers s ON s.id = f.supplier_id"
           + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY f.of_date DESC, f.id DESC LIMIT ?")
    out = []
    # The tab comes from the POs, so it's applied after the query and the limit must count only what's kept.
    for row in conn.execute(sql, [*args, 1000000 if tab else limit]).fetchall():
        d = dict(row)
        po_statuses = (d["po_statuses"] or "").split(",") if d["po_statuses"] else []
        d["old_draft"] = draft_is_old(d)
        d["delivery"] = f"Over {DRAFT_WARN_DAYS} days old" if d["old_draft"] else delivery_status(d["status"], po_statuses, bool(d["received_m"]))
        d["tab"] = register_tab(d["status"], po_statuses, d["unsent"])
        # Printed: the date the last branch PO was printed once all are, or how far along it is until then.
        live = [s for s in po_statuses if s != CANCELLED]
        done = max(sum(s in orders.PO_TAB_STATUSES[orders.COMPLETED] for s in live) - d["unsent"], 0)
        d["printed_at"] = d["last_issued_at"] if d["tab"] == orders.COMPLETED else None
        d["printed_part"] = f"{done} of {len(live)}" if d["tab"] == orders.OPEN and done else ""
        d["next_step"] = next_step(d["status"], d["tab"], d["amendments_waiting"], d["delivery"])
        if not tab or d["tab"] == tab:
            out.append(d)
    return out[:limit]


def counts(conn, **filters) -> dict:
    rows = search(conn, **filters, limit=1000000)
    by_status = {s: 0 for s in OF_STATUSES}
    by_tab = {t: 0 for t in orders.PO_TAB_STATUSES}
    open_by_status = {s: 0 for s in OF_STATUSES}
    amount = 0
    for r in rows:
        by_status[r["status"]] += 1
        by_tab[r["tab"]] += 1
        if r["tab"] == orders.OPEN:
            open_by_status[r["status"]] += 1
        if r["status"] == APPROVED:
            amount += r["grand_total_c"]
    return {"total": len(rows), "by_status": by_status, "by_tab": by_tab, "open_by_status": open_by_status, "approved_amount_c": amount}


def pending(conn) -> list:
    return conn.execute(
        "SELECT f.*, s.name AS supplier, u.full_name AS prepared_name FROM order_forms f JOIN suppliers s ON s.id=f.supplier_id"
        " JOIN users u ON u.id=f.prepared_by WHERE f.status='PENDING_APPROVAL' ORDER BY f.updated_at").fetchall()


def pending_amendments(conn) -> list:
    return conn.execute(
        "SELECT a.*, f.of_number, f.grand_total_c, s.name AS supplier, u.full_name AS requested_name"
        " FROM of_amendments a JOIN order_forms f ON f.id=a.of_id JOIN suppliers s ON s.id=f.supplier_id"
        " JOIN users u ON u.id=a.requested_by WHERE a.status='PENDING_APPROVAL' ORDER BY a.requested_at").fetchall()


def approval_history(conn, of_id: int) -> list:
    return conn.execute(
        "SELECT a.*, u.full_name AS decided_name FROM of_approvals a LEFT JOIN users u ON u.id = a.decided_by"
        " WHERE a.of_id = ? ORDER BY a.id", (of_id,)).fetchall()


def audit_trail(conn, of_id: int) -> list:
    """The OF's own entries and those of every branch PO under it, in order."""
    return conn.execute(
        "SELECT * FROM audit_logs WHERE of_id = ? OR po_id IN (SELECT id FROM purchase_orders WHERE of_id = ?) ORDER BY id",
        (of_id, of_id)).fetchall()


def amendments(conn, of_id: int) -> list:
    return conn.execute(
        "SELECT a.*, u.full_name AS requested_name FROM of_amendments a JOIN users u ON u.id=a.requested_by"
        " WHERE a.of_id = ? ORDER BY a.id", (of_id,)).fetchall()


def rejections(conn) -> list[dict]:
    """Every OF and amendment the approver rejected, newest first, with the reason and where the order stands now. Kept for good, so the Rejected tab is a history."""
    rows = [dict(r) | {"kind": "OF"} for r in conn.execute(
        "SELECT a.of_id AS doc_id, a.decided_at, a.reason, u.full_name AS rejected_by, f.id, f.status, f.of_number AS number, f.grand_total_c, s.name AS supplier, p.full_name AS prepared_name"
        " FROM of_approvals a JOIN order_forms f ON f.id = a.of_id JOIN suppliers s ON s.id = f.supplier_id LEFT JOIN users u ON u.id = a.decided_by JOIN users p ON p.id = f.prepared_by"
        " WHERE a.decision = 'REJECTED' AND a.amendment_id IS NULL")]
    rows += [dict(r) | {"kind": "OF amendment"} for r in conn.execute(
        "SELECT m.of_id AS doc_id, a.decided_at, a.reason, u.full_name AS rejected_by, f.id, f.status, f.of_number AS number, f.grand_total_c, s.name AS supplier, p.full_name AS prepared_name"
        " FROM of_approvals a JOIN of_amendments m ON m.id = a.amendment_id JOIN order_forms f ON f.id = m.of_id JOIN suppliers s ON s.id = f.supplier_id LEFT JOIN users u ON u.id = a.decided_by"
        " JOIN users p ON p.id = m.requested_by WHERE a.decision = 'REJECTED'")]
    rows += [dict(r) | {"kind": "PO amendment"} for r in conn.execute(
        "SELECT m.po_id AS doc_id, a.decided_at, a.reason, u.full_name AS rejected_by, o.id, o.status, o.po_number AS number, o.grand_total_c, s.name AS supplier, p.full_name AS prepared_name"
        " FROM po_approvals a JOIN po_amendments m ON m.id = a.amendment_id JOIN purchase_orders o ON o.id = m.po_id JOIN suppliers s ON s.id = o.supplier_id LEFT JOIN users u ON u.id = a.decided_by"
        " JOIN users p ON p.id = m.requested_by WHERE a.decision = 'REJECTED'")]
    return sorted(rows, key=lambda r: r["decided_at"] or "", reverse=True)
