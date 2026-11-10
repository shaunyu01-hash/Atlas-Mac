"""The purchase order lifecycle. Every change is one transaction with its audit entries.

A PO is born Approved, from an approved Order Form (order_forms.generate_pos), and inherits that approval.
Approved -> Issued -> (Partially/Fully Delivered, reserved for receiving) -> Closed
Amending an issued PO makes a new revision that goes back to Approved until it is issued again; every issued
revision stays in po_issued_versions exactly as the supplier received it.
POs made before Order Forms existed keep their old numbers and can still be amended, issued, cancelled and closed.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from . import delivery_receipts
from .auth import User, require
from .db import PermissionDenied, POError, audit, data_dir, get_setting, timestamp, tx
from .masters import clean_terms
from .money import (DISC_AMOUNT, DISC_NONE, DISC_PERCENT, compute_line, compute_totals, fmt_peso, m_to_qty, parse_qty, pieces_per_unit,
                    qty_to_m, requires_approval, round_centavos, split_by_weight)

DRAFT, PENDING, APPROVED, ISSUED = "DRAFT", "PENDING_APPROVAL", "APPROVED", "ISSUED"
PARTIAL, FULL, CLOSED, CANCELLED = "PARTIALLY_DELIVERED", "FULLY_DELIVERED", "CLOSED", "CANCELLED"

STATUS_LABELS = {
    DRAFT: "Draft", PENDING: "Pending approval", APPROVED: "Approved", ISSUED: "Issued",
    PARTIAL: "Partially delivered", FULL: "Fully delivered", CLOSED: "Closed", CANCELLED: "Cancelled",
}
# The register tabs. None is the All tab. Purchasing's part is done once a PO is issued, which is its official print.
OPEN, COMPLETED = "OPEN", "COMPLETED"
REGISTER_TABS = {OPEN: "Open", COMPLETED: "Completed", CANCELLED: "Cancelled", None: "All"}
PO_TAB_STATUSES = {OPEN: (DRAFT, PENDING, APPROVED), COMPLETED: (ISSUED, PARTIAL, FULL, CLOSED), CANCELLED: (CANCELLED,)}
ARRANGEMENTS = {"COMPLETE": "Complete Delivery", "PARTIAL": "Partial Delivery Allowed"}

AMENDABLE_HEADER = ("expected_delivery_date", "delivery_arrangement", "payment_terms", "delivery_instructions", "remarks",
                    "internal_notes", "legacy_po_number", "overall_discount_type", "overall_discount_value", "vat_mode",
                    "vat_rate_bp", "other_charges_c", "other_charges_note")
# On a PO from an Order Form, these belong to the whole order and stay as the OF set them.
ORDER_LEVEL_HEADER = ("overall_discount_type", "overall_discount_value", "vat_mode", "vat_rate_bp", "other_charges_c", "other_charges_note")
LINE_FIELDS = ("department_id", "product_id", "item_code", "supplier_item_code", "description", "brand", "size", "color",
               "uom", "pieces_per_unit", "box_size", "unit_cost_c", "disc1_bp", "disc2_bp", "selling_c",
               "fx_currency", "fx_rate_m", "fx_cost_m", "remarks")
ITEM_FIELDS = LINE_FIELDS + ("qty_m",)
ORDER_LEVEL_LINE = ("department_id", "product_id", "item_code", "description", "size", "color", "uom", "pieces_per_unit", "unit_cost_c", "disc1_bp", "disc2_bp")
# Never on the supplier's paper, so changing only these saves without a new revision and the supplier's copy still stands.
UNPRINTED_HEADER = ("internal_notes", "legacy_po_number")
APPROVAL_BASES = {"OF_TOTAL": "Total OF amount across all branches", "LARGEST_BRANCH": "Largest single branch PO"}


def format_po_number(year: int, branch_code: int, seq: int) -> str:
    return f"{year}-{branch_code}-{seq:05d}"


def next_po_number(conn, year: int, branch_code: int) -> tuple[str, int]:
    """Must run inside a write transaction, which BEGIN IMMEDIATE has already locked."""
    row = conn.execute("SELECT last_number FROM po_number_sequences WHERE year = ? AND branch_code = ?", (year, branch_code)).fetchone()
    if row is None:
        seq = 1
        conn.execute("INSERT INTO po_number_sequences(year, branch_code, last_number) VALUES (?, ?, 1)", (year, branch_code))
    else:
        seq = row["last_number"] + 1
        conn.execute("UPDATE po_number_sequences SET last_number = ? WHERE year = ? AND branch_code = ?", (seq, year, branch_code))
    return format_po_number(year, branch_code, seq), seq


@contextmanager
def write_guard(conn):
    """Lets a recorded operation change a locked document. Lives only inside its transaction."""
    if not conn.in_transaction:
        # Outside a transaction the token would be committed on its own and could be left behind, unlocking every document.
        raise RuntimeError("write_guard needs an open transaction")
    token = uuid.uuid4().hex
    conn.execute("INSERT INTO po_write_guard(token) VALUES (?)", (token,))
    try:
        yield
    finally:
        conn.execute("DELETE FROM po_write_guard WHERE token = ?", (token,))


def clean_line(raw: dict) -> dict:
    """The product part of a row, shared by OF items and PO items."""
    item = {k: raw.get(k) for k in LINE_FIELDS}
    for key in ("item_code", "supplier_item_code", "brand", "size", "color", "fx_currency", "remarks"):
        item[key] = str(item[key] or "").strip()
    item["description"] = str(item["description"] or "").strip()
    item["uom"] = str(item["uom"] or "PC").strip().upper()
    item["pieces_per_unit"] = int(item["pieces_per_unit"] or pieces_per_unit(item["uom"]))
    for key in ("unit_cost_c", "disc1_bp", "disc2_bp", "fx_rate_m", "fx_cost_m"):
        item[key] = int(item[key] or 0)
    if not item["department_id"]:
        raise POError(f"Choose a department for '{item['description'] or item['item_code'] or 'this row'}'.")
    if not item["description"]:
        raise POError("Every row needs a description.")
    return item


def _clean_item(raw: dict) -> dict:
    item = clean_line(raw)
    try:
        item["qty_m"] = int(raw["qty_m"]) if raw.get("qty_m") is not None else parse_qty(raw.get("qty"), item["uom"])
    except ValueError as exc:
        raise POError(f"'{item['description']}': {exc}") from None
    if item["qty_m"] <= 0:
        raise POError(f"Enter a quantity for '{item['description']}'.")
    # A safety limit, the same one an OF has per branch: a billion pieces of one product.
    if item["qty_m"] * item["pieces_per_unit"] > 10**9 * 1000:
        raise POError(f"'{item['description']}': the quantity is too large.")
    amounts = compute_line(m_to_qty(item["qty_m"]), item["unit_cost_c"], item["disc1_bp"], item["disc2_bp"])
    item.update(net_unit_c=amounts.net_unit, line_gross_c=amounts.gross, line_discount_c=amounts.discount,
                line_total_c=amounts.total)
    return item


def calc_line(item: dict) -> dict:
    return {"department_id": item["department_id"], "qty": m_to_qty(item["qty_m"]), "pieces_per_unit": item["pieces_per_unit"],
            "unit_cost": item["unit_cost_c"], "disc1_bp": item["disc1_bp"], "disc2_bp": item["disc2_bp"]}


def _totals(header: dict, items: list[dict]):
    try:
        return compute_totals([calc_line(i) for i in items], header["overall_discount_type"], header["overall_discount_value"],
                              header["vat_mode"], header["vat_rate_bp"], header["other_charges_c"])
    except ValueError as exc:
        raise POError(str(exc)) from None


def store_totals(conn, po_id: int, t) -> None:
    conn.execute(
        "UPDATE purchase_orders SET total_qty_m=?, total_pieces_m=?, gross_c=?, item_discount_c=?, overall_discount_c=?,"
        " vat_c=?, grand_total_c=?, updated_at=? WHERE id=?",
        (qty_to_m(t.total_qty), qty_to_m(t.total_pieces), t.gross, t.item_discount, t.overall_discount, t.vat, t.grand_total,
         timestamp(), po_id))


def insert_items(conn, po_id: int, items: list[dict], start: int = 1) -> None:
    cols = ITEM_FIELDS + ("net_unit_c", "line_gross_c", "line_discount_c", "line_total_c", "of_item_id", "of_allocation_id")
    sql = (f"INSERT INTO purchase_order_items(po_id, line_no, {', '.join(cols)})"
           f" VALUES (?, ?, {', '.join('?' * len(cols))})")
    for n, item in enumerate(items, start):
        conn.execute(sql, [po_id, n, *[item.get(c) for c in cols]])


def _get_po(conn, po_id: int):
    row = conn.execute("SELECT * FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
    if row is None:
        raise POError("That PO no longer exists.")
    return row


def _label(po) -> str:
    return po["po_number"] or f"Draft #{po['id']}"


def get_po(conn, po_id: int) -> dict:
    """The PO as issued: supplier and branch come from the copies taken when it was made, not today's master records."""
    po = dict(_get_po(conn, po_id))
    po["items"] = [dict(r) for r in conn.execute(
        "SELECT i.*, d.name AS department FROM purchase_order_items i JOIN departments d ON d.id = i.department_id"
        " WHERE i.po_id = ? ORDER BY i.line_no", (po_id,))]
    for item in po["items"]:
        item["qty"] = m_to_qty(item["qty_m"])
        item["received_qty"] = m_to_qty(item["received_qty_m"])
        item["remaining_qty"] = m_to_qty(item["remaining_qty_m"])
    po["total_qty"] = m_to_qty(po["total_qty_m"])
    if po["supplier_snapshot"]:
        po["supplier"] = json.loads(po["supplier_snapshot"])
    else:
        po["supplier"] = dict(conn.execute("SELECT * FROM suppliers WHERE id = ?", (po["supplier_id"],)).fetchone())
    po["branch"] = {"code": po["branch_code"], "name": po["branch_name"], "address": po["branch_address"]}
    parent = conn.execute("SELECT of_number FROM order_forms WHERE id = ?", (po["of_id"],)).fetchone() if po["of_id"] else None
    po["of_number"] = parent["of_number"] if parent else ""
    for key in ("prepared_by", "approved_by"):
        who = conn.execute("SELECT full_name FROM users WHERE id = ?", (po[key],)).fetchone() if po[key] else None
        po[f"{key}_name"] = who["full_name"] if who else ""
    po["totals"] = _totals(po, po["items"])
    return po


# ------------------------------------------------------------------ approval policy

def policy(conn) -> dict:
    return {
        "enabled": get_setting(conn, "approval.enabled") == "1",
        "threshold_c": int(get_setting(conn, "approval.threshold_c") or 0),
        "comparison": get_setting(conn, "approval.comparison") or "GT",
        "approver_user_ids": [int(x) for x in get_setting(conn, "approval.approver_user_ids").split(",") if x.strip()],
        "allow_self_approval": get_setting(conn, "approval.allow_self_approval") == "1",
        "basis": get_setting(conn, "approval.basis") or "OF_TOTAL",
    }


def approver_names(conn) -> str:
    """Everyone who can approve right now, as a sentence. Any one of them is enough."""
    ids = policy(conn)["approver_user_ids"]
    marks = ",".join("?" * len(ids))
    names = [r["full_name"] for r in conn.execute(f"SELECT full_name FROM users WHERE active = 1 AND id IN ({marks}) ORDER BY full_name", ids)]
    return " or ".join(filter(None, [", ".join(names[:-1]), names[-1] if names else ""])) or "nobody yet"


def require_listed_approver(conn, user: User) -> None:
    if user.id not in policy(conn)["approver_user_ids"]:
        raise POError(f"Only {approver_names(conn)} can approve this. An administrator can change who approves under Settings, Approval policy.")


def needs_approval(conn, amount_c: int) -> bool:
    p = policy(conn)
    return requires_approval(amount_c, p["enabled"], p["threshold_c"], p["comparison"])


def _record_approval(conn, po_id, decision, amount, by=None, reason="", remarks="", amendment_id=None) -> None:
    p = policy(conn)
    conn.execute(
        "INSERT INTO po_approvals(po_id, amendment_id, decision, decided_by, decided_at, amount_c, threshold_c,"
        " policy_enabled, reason, remarks) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (po_id, amendment_id, decision, by, timestamp(), amount, p["threshold_c"], int(p["enabled"]), reason, remarks))


# ------------------------------------------------------------------ the agreed overall discount

# The agreed overall discount stays as agreed after a branch PO is amended or cancelled.
# A percent OF keeps its percentage on every PO. A peso OF keeps the same peso total, shared again across the POs still live.
# The OF header is never changed by this, and each PO keeps storing its share as pesos, so debit memos and payments follow it.
# Freight and other charges are not part of this rule (not decided).

def _merchandise(items) -> int:
    """What a PO's rows come to after their own item discounts, before the overall discount."""
    return sum(compute_line(m_to_qty(i["qty_m"]), i["unit_cost_c"], (i.get("disc1_bp") if isinstance(i, dict) else i["disc1_bp"]) or 0,
                            (i.get("disc2_bp") if isinstance(i, dict) else i["disc2_bp"]) or 0).total for i in items)


def _share_header(share: int) -> dict:
    """A PO's share is kept as pesos, the way generate_pos stores it."""
    return {"overall_discount_type": DISC_AMOUNT if share else DISC_NONE, "overall_discount_value": share}


def agreed_discount_shares(conn, of_id: int | None, changed: dict | None = None, leaving=()) -> dict[int, int]:
    """Each PO's share of the OF's agreed overall discount, from the OF's own terms. Returns {po_id: centavos} for the POs to set.

    changed holds {po_id: rows} for a PO whose rows are about to change; leaving holds POs about to be cancelled.
    Percent OF: only the changed POs, each at the OF percentage of its own merchandise; the others keep what they have.
    Peso OF: the agreed amount shared across every live PO by merchandise, adding up to the agreed amount.
    Nothing for a PO not from an OF, or an OF with no overall discount. Refused when the agreed amount no longer fits.
    """
    changed = changed or {}
    if not of_id:
        return {}
    of = conn.execute("SELECT of_number, overall_discount_type, overall_discount_value FROM order_forms WHERE id = ?", (of_id,)).fetchone()
    if of is None or not of["overall_discount_value"] or of["overall_discount_type"] not in (DISC_PERCENT, DISC_AMOUNT):
        return {}
    agreed = of["overall_discount_value"]
    if of["overall_discount_type"] == DISC_PERCENT:
        # The same rounding compute_order uses for the percentage.
        return {po_id: round_centavos(Decimal(_merchandise(items)) * agreed / 10000) for po_id, items in changed.items()}
    live = conn.execute("SELECT id, po_number FROM purchase_orders WHERE of_id = ? AND status <> 'CANCELLED' ORDER BY id", (of_id,)).fetchall()
    weights, numbers = {}, {}
    for row in live:
        if row["id"] in leaving:
            continue
        numbers[row["id"]] = row["po_number"]
        if row["id"] in changed:
            weights[row["id"]] = _merchandise(changed[row["id"]])
        else:
            weights[row["id"]] = _merchandise(conn.execute("SELECT * FROM purchase_order_items WHERE po_id = ?", (row["id"],)).fetchall())
    if not weights:
        return {}
    left = sum(weights.values())
    if agreed >= left:
        how = "more than" if agreed > left else "all of"
        raise POError(f"The agreed discount of {fmt_peso(agreed)} on {of['of_number']} is {how} what is left on this order ({fmt_peso(left)}). "
                      "Cancel the order instead, or ask the supplier.")
    shares = split_by_weight(agreed, weights)
    for po_id, share in shares.items():
        if weights[po_id] and share >= weights[po_id]:
            raise POError(f"The agreed discount of {fmt_peso(agreed)} on {of['of_number']} would leave {numbers[po_id]} at {fmt_peso(0)}. "
                          "Cancel the order instead, or ask the supplier.")
    return shares


def _reshare_siblings(conn, user, shares: dict[int, int], skip: int | None, why: str) -> None:
    """The other POs on a peso OF take their new share. One whose share didn't move is left alone.
    Called inside write_guard, before sync_parent_of."""
    for sid, share in shares.items():
        if sid == skip:
            continue
        sib = _get_po(conn, sid)
        if sib["overall_discount_c"] == share and sib["overall_discount_value"] == share:
            continue
        items = [dict(r) for r in conn.execute("SELECT * FROM purchase_order_items WHERE po_id = ? ORDER BY line_no", (sid,))]
        header = {k: sib[k] for k in AMENDABLE_HEADER} | _share_header(share)
        totals = _totals(header, items)
        revision = sib["revision"] + 1
        conn.execute("UPDATE purchase_orders SET overall_discount_type = ?, overall_discount_value = ?, revision = ? WHERE id = ?",
                     (header["overall_discount_type"], header["overall_discount_value"], revision, sid))
        store_totals(conn, sid, totals)
        # Like any changed PO, it is no longer what the supplier holds, so it waits to be issued again.
        if sib["status"] == ISSUED:
            conn.execute("UPDATE purchase_orders SET status = ? WHERE id = ?", (APPROVED, sid))
        elif sib["status"] in (PARTIAL, FULL):
            _refresh_delivery(conn, sid)
        audit(conn, user, "PO_DISCOUNT_SHARE", "purchase_order", sid, po_id=sid,
              summary=f"{sib['po_number']} revision {revision}: overall discount shared again after {why}: "
                      f"{fmt_peso(sib['overall_discount_c'])} to {fmt_peso(share)}, now {fmt_peso(totals.grand_total)}",
              previous={"overall_discount_c": sib["overall_discount_c"], "grand_total_c": sib["grand_total_c"], "revision": sib["revision"]},
              new={"overall_discount_c": share, "grand_total_c": totals.grand_total, "revision": revision})


# ------------------------------------------------------------------ keeping the parent OF in step

def sync_parent_of(conn, user, of_id: int | None, why: str) -> None:
    """After a PO changes, the OF's allocations and totals follow, so the warehouse copy always shows live quantities.
    Only someone who may cancel, amend or approve a PO changes one, so only they may set this off."""
    if user is None or not any(user.can(p) for p in ("po.cancel", "po.amend", "po.approve")):
        raise PermissionDenied(f"{user.display if user else 'Nobody'} does not have permission for this (po.amend).")
    if not of_id:
        return
    before = conn.execute("SELECT total_qty_m, grand_total_c FROM order_forms WHERE id=?", (of_id,)).fetchone()
    with write_guard(conn):
        conn.execute(
            "UPDATE order_form_allocations SET qty_m = COALESCE((SELECT SUM(i.qty_m) FROM purchase_order_items i"
            " JOIN purchase_orders p ON p.id = i.po_id WHERE i.of_allocation_id = order_form_allocations.id AND p.status <> 'CANCELLED'), 0)"
            " WHERE of_item_id IN (SELECT id FROM order_form_items WHERE of_id = ?)", (of_id,))
        conn.execute(
            "UPDATE order_form_items SET"
            " total_qty_m = (SELECT COALESCE(SUM(a.qty_m),0) FROM order_form_allocations a WHERE a.of_item_id = order_form_items.id),"
            " line_gross_c = (SELECT COALESCE(SUM(i.line_gross_c),0) FROM purchase_order_items i JOIN purchase_orders p ON p.id=i.po_id"
            "   WHERE i.of_item_id = order_form_items.id AND p.status <> 'CANCELLED'),"
            " line_discount_c = (SELECT COALESCE(SUM(i.line_discount_c),0) FROM purchase_order_items i JOIN purchase_orders p ON p.id=i.po_id"
            "   WHERE i.of_item_id = order_form_items.id AND p.status <> 'CANCELLED'),"
            " line_total_c = (SELECT COALESCE(SUM(i.line_total_c),0) FROM purchase_order_items i JOIN purchase_orders p ON p.id=i.po_id"
            "   WHERE i.of_item_id = order_form_items.id AND p.status <> 'CANCELLED')"
            " WHERE of_id = ?", (of_id,))
        s = conn.execute(
            "SELECT COALESCE(SUM(total_qty_m),0) q, COALESCE(SUM(total_pieces_m),0) pc, COALESCE(SUM(gross_c),0) g, COALESCE(SUM(item_discount_c),0) d,"
            " COALESCE(SUM(overall_discount_c),0) o, COALESCE(SUM(vat_c),0) v, COALESCE(SUM(grand_total_c),0) t, COALESCE(SUM(other_charges_c),0) c"
            " FROM purchase_orders WHERE of_id=? AND status<>'CANCELLED'", (of_id,)).fetchone()
        conn.execute(
            "UPDATE order_forms SET total_qty_m=?, total_pieces_m=?, gross_c=?, item_discount_c=?, overall_discount_c=?, vat_c=?,"
            " grand_total_c=?, other_charges_c=?, updated_at=? WHERE id=?",
            (s["q"], s["pc"], s["g"], s["d"], s["o"], s["v"], s["t"], s["c"], timestamp(), of_id))
    audit(conn, user, "OF_SYNC", "order_form", of_id, of_id=of_id, summary=f"Updated from its branch POs: {why}",
          previous=dict(before), new={"total_qty_m": s["q"], "grand_total_c": s["t"]})


# ------------------------------------------------------------------ issuing

def unsent(po) -> bool:
    """Amended after it was issued, and the new revision hasn't gone to the supplier yet. A partly delivered PO keeps its status, so this is what says so."""
    return po["status"] in (ISSUED, PARTIAL) and po["issued_revision"] is not None and po["revision"] > po["issued_revision"]


def to_issue(po) -> bool:
    return po["status"] == APPROVED or unsent(po)


def issue(conn, user: User, po_id: int) -> None:
    """Hand the PO to the supplier: records who and when, and keeps the exact PDF of this revision for good."""
    from . import pdf
    require(user, "po.issue")
    with tx(conn):
        po = _get_po(conn, po_id)
        if not to_issue(po):
            raise POError("Only an approved PO, or an amended one not sent yet, can be issued to a supplier.")
        if conn.execute("SELECT 1 FROM po_amendments WHERE po_id=? AND status='PENDING_APPROVAL'", (po_id,)).fetchone():
            raise POError("An amendment is waiting for approval. Decide on it first.")
        document = pdf.render_supplier_po(conn, po_id, preview=False)
        snapshot = get_po(conn, po_id)
        snapshot.pop("totals")
        stamp = timestamp()
        conn.execute(
            "INSERT INTO po_issued_versions(po_id, revision, issued_at, issued_by, snapshot_json, pdf, sha256) VALUES (?,?,?,?,?,?,?)",
            (po_id, po["revision"], stamp, user.id, json.dumps(snapshot, ensure_ascii=False, default=str), document,
             hashlib.sha256(document).hexdigest()))
        received = conn.execute("SELECT COALESCE(SUM(received_qty_m), 0) FROM purchase_order_items WHERE po_id = ?", (po_id,)).fetchone()[0]
        status = po["status"] if po["status"] in (ISSUED, PARTIAL) else (PARTIAL if received else ISSUED)
        conn.execute("UPDATE purchase_orders SET status=?, issued_at=?, issued_by=?, issued_revision=?, updated_at=? WHERE id=?",
                     (status, stamp, user.id, po["revision"], stamp, po_id))
        rev = f" revision {po['revision']}" if po["revision"] else ""
        audit(conn, user, "PO_ISSUE", "purchase_order", po_id, po_id=po_id, summary=f"{po['po_number']}{rev} issued to the supplier")
        delivery_receipts.ensure_first(conn, user, po_id)


def issue_batch(conn, user: User, po_ids: list[int]) -> tuple[list[int], list[str]]:
    """Issue each PO on its own, so one that can't go doesn't hold back the rest. Returns those issued, and what stopped the others."""
    done, problems = [], []
    for po_id in po_ids:
        try:
            issue(conn, user, po_id)
            done.append(po_id)
        except Exception as exc:  # noqa: BLE001 - one PO's failure, of any kind, must not stop the rest
            row = conn.execute("SELECT po_number FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
            problems.append(f"{(row['po_number'] if row else None) or f'PO #{po_id}'}: {exc}")
    return done, problems


def issued_versions(conn, po_id: int) -> list:
    return conn.execute(
        "SELECT v.id, v.revision, v.issued_at, v.sha256, length(v.pdf) AS size_bytes, u.full_name AS issued_name FROM po_issued_versions v"
        " JOIN users u ON u.id = v.issued_by WHERE v.po_id = ? ORDER BY v.revision", (po_id,)).fetchall()


def issued_pdf(conn, user: User | None, po_id: int, revision: int | None = None) -> bytes | None:
    """The original bytes of an issued revision, the latest by default. None if it was never issued.
    With no user, it's the exact kept file. A person asking for it is printing it again, so they get a copy marked as one, and it's recorded."""
    from . import pdf
    # Receivers count blind and the warehouse has no PO page, so neither gets the supplier's priced PO.
    # With no user, it's the program itself fetching the kept file, for example on the first issue.
    if user is not None and user.roles & {"RECEIVER", "WAREHOUSE"}:
        raise PermissionDenied(f"{user.display} does not have permission to see supplier POs.")
    sql = "SELECT revision, pdf FROM po_issued_versions WHERE po_id = ?"
    args = [po_id]
    if revision is not None:
        sql += " AND revision = ?"
        args.append(revision)
    row = conn.execute(sql + " ORDER BY revision DESC LIMIT 1", args).fetchone()
    if row is None:
        return None
    if user is None:
        return bytes(row["pdf"])
    with tx(conn):
        # Counted inside the same transaction as the record, so two PCs can't both get the same copy number.
        earlier = conn.execute("SELECT COUNT(*) FROM audit_logs WHERE po_id = ? AND action = 'PO_REPRINT'", (po_id,)).fetchone()[0]
        audit(conn, user, "PO_REPRINT", "purchase_order", po_id, po_id=po_id, summary=f"Reprinted issued revision {row['revision']}")
    return pdf.stamp_copy(bytes(row["pdf"]), earlier + 2, user.display)


def cancel(conn, user: User, po_id: int, reason: str, sync_of: bool = True) -> None:
    """sync_of is off when the whole OF is being cancelled, so the OF keeps showing what was ordered."""
    require(user, "po.cancel")
    if not reason.strip():
        raise POError("Write the reason for cancelling this PO.")
    with tx(conn):
        po = _get_po(conn, po_id)
        if po["status"] not in (DRAFT, PENDING, APPROVED, ISSUED):
            raise POError(f"A PO that is {STATUS_LABELS[po['status']]} can't be cancelled.")
        if conn.execute("SELECT 1 FROM po_amendments WHERE po_id = ? AND status = 'PENDING_APPROVAL'", (po_id,)).fetchone():
            raise POError("An amendment is waiting for approval. The approver has to decide on it first.")
        if conn.execute("SELECT 1 FROM branch_counts WHERE po_id = ?", (po_id,)).fetchone():
            raise POError("This PO has been counted at the branch, so it can't be cancelled. If nothing more is coming, close it instead.")
        if live_charges(conn, po_id):
            raise POError(f"This PO has {live_charges(conn, po_id)}. Cancel those first.")
        refuse_if_paid(conn, po["of_id"], "cancelled")
        received = conn.execute("SELECT COALESCE(SUM(received_qty_m),0) FROM purchase_order_items WHERE po_id=?",
                                (po_id,)).fetchone()[0]
        if received:
            raise POError("Goods were already received on this PO. Close it instead.")
        # A peso discount agreed for the whole OF moves onto the POs still live. Checked before anything is saved.
        shares = agreed_discount_shares(conn, po["of_id"], leaving={po_id}) if sync_of else {}
        stamp = timestamp()
        conn.execute("UPDATE purchase_orders SET status=?, cancelled_at=?, cancelled_by=?, cancel_reason=?, updated_at=?"
                     " WHERE id=?", (CANCELLED, stamp, user.id, reason.strip(), stamp, po_id))
        audit(conn, user, "PO_CANCEL", "purchase_order", po_id, po_id=po_id,
              summary=f"{_label(po)} cancelled: {reason.strip()}", previous={"status": po["status"]})
        if sync_of:
            if shares:
                with write_guard(conn):
                    _reshare_siblings(conn, user, shares, po_id, f"{_label(po)} was cancelled")
            sync_parent_of(conn, user, po["of_id"], f"{_label(po)} cancelled")


def close(conn, user: User, po_id: int, reason: str) -> None:
    """End a PO that won't receive anything more, including a short delivery."""
    require(user, "po.close")
    if not reason.strip():
        raise POError("Write the reason for closing this PO.")
    with tx(conn):
        po = _get_po(conn, po_id)
        if po["status"] not in (ISSUED, PARTIAL, FULL):
            raise POError("Only an issued or delivered PO can be closed.")
        if conn.execute("SELECT 1 FROM po_amendments WHERE po_id = ? AND status = 'PENDING_APPROVAL'", (po_id,)).fetchone():
            raise POError("An amendment is waiting for approval. The approver has to decide on it first.")
        # Closing with nothing counted used to pay the PO in full. Only a PO issued before branch receiving existed may still close that way.
        # Otherwise: if nothing came, cancel it; if a count is open, finish it first.
        from . import receiving
        legacy = (po["issued_at"] or "") < receiving.live_since(conn)
        if not legacy and not conn.execute("SELECT 1 FROM branch_counts WHERE po_id = ? AND status = 'APPROVED'", (po_id,)).fetchone():
            raise POError("Nothing on this PO has been counted and agreed at the branch. If nothing will come, cancel it instead.")
        if receiving.progress(conn, po_id):
            raise POError("This PO is still being counted at the branch. Wait until the counts agree, then close it.")
        stamp = timestamp()
        conn.execute("UPDATE purchase_orders SET status=?, closed_at=?, closed_by=?, close_reason=?, updated_at=?"
                     " WHERE id=?", (CLOSED, stamp, user.id, reason.strip(), stamp, po_id))
        audit(conn, user, "PO_CLOSE", "purchase_order", po_id, po_id=po_id,
              summary=f"{po['po_number']} closed: {reason.strip()}", previous={"status": po["status"]})


def can_cancel(conn, po_id: int) -> bool:
    """Whether cancel() would go ahead, so the button only shows when it can."""
    po = conn.execute("SELECT status FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
    return (po is not None and po["status"] in (DRAFT, PENDING, APPROVED, ISSUED)
            and not conn.execute("SELECT 1 FROM po_amendments WHERE po_id = ? AND status = 'PENDING_APPROVAL'", (po_id,)).fetchone()
            and not conn.execute("SELECT 1 FROM branch_counts WHERE po_id = ?", (po_id,)).fetchone()
            and not conn.execute("SELECT 1 FROM purchase_order_items WHERE po_id = ? AND received_qty_m > 0", (po_id,)).fetchone()
            and not live_charges(conn, po_id))


def can_close(conn, po_id: int) -> bool:
    """Whether close() would go ahead."""
    from . import receiving
    po = conn.execute("SELECT status, issued_at FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
    if po is None or po["status"] not in (ISSUED, PARTIAL, FULL) or receiving.progress(conn, po_id):
        return False
    if conn.execute("SELECT 1 FROM po_amendments WHERE po_id = ? AND status = 'PENDING_APPROVAL'", (po_id,)).fetchone():
        return False
    return (po["issued_at"] or "") < receiving.live_since(conn) or conn.execute("SELECT 1 FROM branch_counts WHERE po_id = ? AND status = 'APPROVED'", (po_id,)).fetchone() is not None


def paid_by(conn, of_id: int | None) -> str:
    """The number of the live check that pays this order, or nothing. Once a check is recorded, nothing may change what the order is worth."""
    if not of_id:
        return ""
    row = conn.execute("SELECT c.check_number FROM payment_checks c JOIN payment_check_ofs o ON o.check_id = c.id WHERE o.of_id = ? AND c.status = 'ACTIVE'", (of_id,)).fetchone()
    return row[0] if row else ""


def refuse_if_paid(conn, of_id: int | None, doing: str) -> None:
    check = paid_by(conn, of_id)
    if check:
        raise POError(f"Check {check} already pays this order, so it can't be {doing}. Accounting has to cancel the check first, if the supplier hasn't claimed it.")


def receive_outside(conn, user: User, po_id: int, reason: str) -> None:
    """Until branches count in the program, Admin records a delivery received in full outside the program, and the PO closes as delivered.
    If not everything came, lower the PO's quantities first. The order's DRs are then compared before it's paid, like any closed PO."""
    require(user, "po.receive_outside")
    reason = reason.strip()
    if not reason:
        raise POError("Write where this delivery was recorded, for example the receiving report number.")
    with tx(conn):
        po = _get_po(conn, po_id)
        if po["status"] not in (ISSUED, PARTIAL, FULL):
            raise POError("Only an issued PO can be received.")
        if conn.execute("SELECT 1 FROM branch_counts WHERE po_id = ?", (po_id,)).fetchone():
            raise POError(f"{po['po_number']} has been counted at the branch, so it goes by that count.")
        if conn.execute("SELECT 1 FROM po_amendments WHERE po_id = ? AND status = 'PENDING_APPROVAL'", (po_id,)).fetchone():
            raise POError("An amendment to this PO is still waiting for approval. Decide it first.")
        stamp = timestamp()
        conn.execute("INSERT INTO outside_receipts(po_id, reason, recorded_by, recorded_at) VALUES (?,?,?,?)", (po_id, reason, user.id, stamp))
        with write_guard(conn):
            conn.execute("UPDATE purchase_order_items SET received_qty_m = qty_m WHERE po_id = ?", (po_id,))
        conn.execute("UPDATE purchase_orders SET status=?, closed_at=?, closed_by=?, close_reason=?, updated_at=? WHERE id=?",
                     (CLOSED, stamp, user.id, f"Received in full outside the program: {reason}", stamp, po_id))
        audit(conn, user, "PO_RECEIVED_OUTSIDE", "purchase_order", po_id, po_id=po_id, of_id=po["of_id"],
              summary=f"{po['po_number']} received in full outside the program: {reason}", previous={"status": po["status"]})


def can_receive_outside(conn, po_id: int) -> bool:
    """Whether receive_outside() would go ahead."""
    po = conn.execute("SELECT status FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
    return (po is not None and po["status"] in (ISSUED, PARTIAL, FULL)
            and not conn.execute("SELECT 1 FROM branch_counts WHERE po_id = ?", (po_id,)).fetchone()
            and not conn.execute("SELECT 1 FROM po_amendments WHERE po_id = ? AND status = 'PENDING_APPROVAL'", (po_id,)).fetchone())


def live_charges(conn, po_id: int) -> str:
    """Debit memos, claim stubs and item passes still in force on a PO, by number. Empty when there are none."""
    rows = conn.execute("SELECT dm_number FROM debit_memos WHERE po_id = ? AND status = 'ISSUED'"
                        " UNION ALL SELECT cs_number FROM claim_stubs WHERE po_id = ? AND status <> 'CANCELLED'"
                        " UNION ALL SELECT ip_number FROM item_passes WHERE po_id = ? AND status <> 'CANCELLED'", (po_id, po_id, po_id)).fetchall()
    return ", ".join(r[0] for r in rows)


def set_legacy_number(conn, user: User, po_id: int, number: str) -> None:
    """Record the external PO number. It's a cross-reference, known only after the approved PO is entered in your accounting system,
    so it can be set at any status without an amendment."""
    require(user, "po.edit")
    number = number.strip()
    with tx(conn):
        po = _get_po(conn, po_id)
        if po["legacy_po_number"] == number:
            return
        if number:
            other = conn.execute(
                "SELECT po_number FROM purchase_orders WHERE legacy_po_number=? AND branch_code=? AND id<>? AND status<>'CANCELLED'",
                (number, po["branch_code"], po_id)).fetchone()
            if other:
                raise POError(f"External PO {number} is already recorded on {other['po_number'] or 'another draft'} for this branch.")
        conn.execute("UPDATE purchase_orders SET legacy_po_number=?, updated_at=? WHERE id=?", (number, timestamp(), po_id))
        audit(conn, user, "PO_LEGACY_NUMBER", "purchase_order", po_id, po_id=po_id,
              summary=f"External PO number set to {number or 'blank'}", previous={"legacy_po_number": po["legacy_po_number"]},
              new={"legacy_po_number": number})


# ------------------------------------------------------------------ amendments

def _snapshot(conn, po_id: int) -> dict:
    po = dict(_get_po(conn, po_id))
    items = [dict(r) for r in conn.execute("SELECT * FROM purchase_order_items WHERE po_id=? ORDER BY line_no", (po_id,))]
    keep = ("po_number", "status", "revision", "grand_total_c", "total_qty_m", "supplier_id", "branch_code", "po_date") + AMENDABLE_HEADER
    return {"header": {k: po[k] for k in keep}, "items": items}


# Header fields kept as text that may be blank. A missing value (None) means blank.
_TEXT_HEADER = ("payment_terms", "delivery_instructions", "remarks", "internal_notes", "legacy_po_number", "other_charges_note")


def _refuse_kept_row(item: dict, prior, charged: dict) -> None:
    """What a kept row can't become, given what has happened to the PO since. Checked on request and again on approval."""
    if item["qty_m"] < prior["received_qty_m"]:
        raise POError(f"'{item['description']}': quantity can't go below what was already received.")
    if item["qty_m"] < prior["qty_m"] and any(charged.get((prior["id"], why)) for why in ("SHORT", "DAMAGED")):
        raise POError(f"'{item['description']}' is on a debit memo or claim stub, so its quantity can't be lowered. Cancel that first.")


def _refuse_dropped_row(conn, gone, charged: dict) -> None:
    gone_id = gone["id"]
    if gone["received_qty_m"]:
        raise POError(f"'{gone['description']}' has received goods, so it can't be removed.")
    if any(key[0] == gone_id for key in charged):
        raise POError(f"'{gone['description']}' is on a debit memo or claim stub, so it can't be removed. Cancel that first.")
    if conn.execute("SELECT 1 FROM warehouse_delivery_items WHERE po_item_id = ? UNION ALL SELECT 1 FROM warehouse_stickered WHERE po_item_id = ?",
                    (gone_id, gone_id)).fetchone():
        raise POError(f"'{gone['description']}' was already counted and stickered at the Central Warehouse, so it can't be removed. Lower its quantity instead.")


def _recheck_waiting(conn, po_id: int, after: dict) -> None:
    """Counts, debit memos and claim stubs can be made while an amendment waits, so approval checks the rows again against today's PO."""
    from . import receiving
    old_items = {r["id"]: r for r in conn.execute("SELECT * FROM purchase_order_items WHERE po_id=?", (po_id,))}
    charged = receiving.dm_pieces(conn, po_id)
    kept = set()
    try:
        for item in after["items"]:
            if item.get("id") is None:
                continue
            prior = old_items.get(item["id"])
            if prior is None:
                raise POError("A row on this amendment is no longer on this PO.")
            kept.add(item["id"])
            _refuse_kept_row(item, prior, charged)
        for gone_id, gone in old_items.items():
            if gone_id not in kept:
                _refuse_dropped_row(conn, gone, charged)
    except POError as exc:
        raise POError(f"This amendment can't be applied any more. {exc} Reject it, and ask for a new one if it's still needed.") from None


def amend(conn, user: User, po_id: int, header_changes: dict, items: list[dict], reason: str) -> dict:
    """Formal change to an approved PO. Number stays. Items carry their id when they are existing rows.

    On a PO from an OF, only quantities and terms can change, and rows can be dropped; the OF's allocations follow.
    Prices and new products mean a new OF, because they belong to the whole supplier order.
    Any quantity increase, any new row, or a higher total goes through approval while the policy is on.
    Otherwise it applies at once. Returns {'amendment_id', 'status', 'revision'}.
    """
    require(user, "po.amend")
    if not (reason or "").strip():
        raise POError("Write the reason for this amendment.")
    header_changes = {k: ("" if v is None and k in _TEXT_HEADER else v) for k, v in (header_changes or {}).items()}
    with tx(conn):
        po = _get_po(conn, po_id)
        if po["status"] not in (APPROVED, ISSUED, PARTIAL):
            raise POError(f"A PO that is {STATUS_LABELS[po['status']]} can't be amended.")
        refuse_if_paid(conn, po["of_id"], "amended")
        if conn.execute("SELECT 1 FROM po_amendments WHERE po_id=? AND status='PENDING_APPROVAL'", (po_id,)).fetchone():
            raise POError("An earlier amendment is still waiting for approval.")
        unknown = set(header_changes) - set(AMENDABLE_HEADER)
        if unknown:
            raise POError("The supplier, branch and PO date can't be amended. Cancel and create a new OF.")
        # An older PO keeps its terms exactly as printed unless someone changes them.
        if header_changes.get("payment_terms", po["payment_terms"]) != po["payment_terms"]:
            header_changes = header_changes | {"payment_terms": clean_terms(header_changes["payment_terms"])}
        linked = po["of_id"] is not None
        if linked and set(header_changes) & set(ORDER_LEVEL_HEADER):
            raise POError("VAT, overall discount and other charges belong to the whole OF, so they can't change on one branch PO.")
        old_items = {r["id"]: r for r in conn.execute("SELECT * FROM purchase_order_items WHERE po_id=?", (po_id,))}
        from . import receiving
        charged = receiving.dm_pieces(conn, po_id)
        clean = []
        increased = False
        kept_ids = set()
        for raw in items:
            item = _clean_item(raw)
            item_id = raw.get("id")
            item["id"] = item_id
            if item_id is not None:
                prior = old_items.get(item_id)
                if prior is None:
                    raise POError("A row on this amendment doesn't belong to this PO.")
                kept_ids.add(item_id)
                if item["qty_m"] < prior["received_qty_m"]:
                    raise POError(f"'{item['description']}': quantity can't go below what was already received.")
                if linked and any((item[k] or None) != (prior[k] or None) for k in ORDER_LEVEL_LINE):
                    raise POError(f"'{item['description']}': on a PO from an OF, only the quantity can change. "
                                  "A new price or product needs a new OF.")
                _refuse_kept_row(item, prior, charged)
                # A higher price or a smaller discount costs more, even when a quantity drops elsewhere. Compared per piece, so a change of unit can't hide either.
                ppu, prior_ppu = item["pieces_per_unit"] or 1, prior["pieces_per_unit"] or 1
                dearer = (item["unit_cost_c"] or 0) * prior_ppu > (prior["unit_cost_c"] or 0) * ppu or (item["disc1_bp"] or 0) < (prior["disc1_bp"] or 0) or (item["disc2_bp"] or 0) < (prior["disc2_bp"] or 0)
                increased = increased or item["qty_m"] * ppu > prior["qty_m"] * prior_ppu or dearer
            else:
                if linked:
                    raise POError("A PO from an OF can't take new products. Create a new OF for them.")
                increased = True
            clean.append(item)
        for gone_id, gone in old_items.items():
            if gone_id not in kept_ids:
                _refuse_dropped_row(conn, gone, charged)
        if not clean:
            raise POError("A PO needs at least one item. To drop everything, cancel the PO.")
        new_header = {k: po[k] for k in AMENDABLE_HEADER} | header_changes
        moved = {k for k in AMENDABLE_HEADER if (new_header.get(k) or None) != (po[k] or None)}
        same_items = len(clean) == len(old_items) and all(i["id"] in old_items and all((i[c] or None) == (old_items[i["id"]][c] or None) for c in ITEM_FIELDS) for i in clean)
        if same_items and not moved:
            # Same as an OF amendment: no new revision, and an issued PO isn't sent back to be issued again for nothing.
            raise POError("Nothing changed.")
        if same_items and moved and moved <= set(UNPRINTED_HEADER):
            # The supplier's copy doesn't show these, so it still stands: no new revision, and nothing to print or issue again.
            with write_guard(conn):
                for k in moved:
                    conn.execute(f"UPDATE purchase_orders SET {k} = ?, updated_at = ? WHERE id = ?", (new_header[k], timestamp(), po_id))
            audit(conn, user, "PO_NOTES", "purchase_order", po_id, po_id=po_id, summary=f"{po['po_number']}: internal notes changed: {reason.strip()}",
                  previous={k: po[k] for k in moved}, new={k: new_header[k] for k in moved})
            return {"amendment_id": None, "status": "APPLIED", "revision": po["revision"]}
        # Shorter terms bring the check date earlier, and the order-level money fields change what's paid, so any change there needs approval too.
        money_header = ("payment_terms", "overall_discount_type", "overall_discount_value", "vat_mode", "vat_rate_bp", "other_charges_c")
        header_moved = any((new_header.get(k) or None) != (po[k] or None) for k in money_header)
        # The PO's share of the OF's agreed discount follows its new rows. Worked out again when it is applied.
        shares = agreed_discount_shares(conn, po["of_id"], {po_id: clean}) if linked else {}
        if po_id in shares:
            new_header = new_header | _share_header(shares[po_id])
        totals = _totals(new_header, clean)
        after = {"header": {k: new_header[k] for k in AMENDABLE_HEADER}, "items": clean,
                 "grand_total_c": totals.grand_total}
        before = _snapshot(conn, po_id)
        stamp = timestamp()
        cur = conn.execute(
            "INSERT INTO po_amendments(po_id, status, reason, before_json, after_json, requested_by, requested_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (po_id, PENDING, reason.strip(), json.dumps(before, default=str), json.dumps(after, default=str),
             user.id, stamp))
        amendment_id = cur.lastrowid
        p = policy(conn)
        grows = increased or header_moved or totals.grand_total > po["grand_total_c"]
        if p["enabled"] and grows:
            if not p["approver_user_ids"]:
                raise POError("No approver is set. An administrator must choose one in Settings.")
            _record_approval(conn, po_id, "SUBMITTED", totals.grand_total, user.id, amendment_id=amendment_id,
                             reason=reason.strip())
            audit(conn, user, "PO_AMEND_REQUEST", "purchase_order", po_id, po_id=po_id,
                  summary=f"Amendment requested: {reason.strip()}", previous=before["header"], new=after["header"])
            return {"amendment_id": amendment_id, "status": PENDING, "revision": po["revision"]}
        revision = _apply_amendment(conn, user, po, amendment_id, after, before)
        _record_approval(conn, po_id, "AUTO_APPROVED", totals.grand_total, None, amendment_id=amendment_id,
                         remarks="Amendment needs no approval: no increase, or approval policy off")
        return {"amendment_id": amendment_id, "status": "APPLIED", "revision": revision}


def _normalize_stored(after: dict) -> dict:
    """An amendment saved before version 2 holds whole-number qty and fewer header fields."""
    for item in after["items"]:
        if "qty_m" not in item:
            item["qty_m"] = int(item["qty"]) * 1000
    after["header"].setdefault("delivery_instructions", "")
    return after


def _apply_amendment(conn, user, po, amendment_id: int, after: dict, before: dict) -> int:
    """Guarded write: the only path that changes items once a PO is past Draft."""
    po_id = po["id"]
    # The agreed discount is shared again on today's figures, so an amendment approved later still keeps the agreed amount.
    shares = agreed_discount_shares(conn, po["of_id"], {po_id: after["items"]})
    if po_id in shares:
        after["header"] = after["header"] | _share_header(shares[po_id])
    with write_guard(conn):
        sets = ", ".join(f"{k} = ?" for k in AMENDABLE_HEADER)
        conn.execute(f"UPDATE purchase_orders SET {sets} WHERE id = ?",
                     [after["header"][k] for k in AMENDABLE_HEADER] + [po_id])
        keep = {i["id"] for i in after["items"] if i.get("id")}
        for row in conn.execute("SELECT id FROM purchase_order_items WHERE po_id=?", (po_id,)).fetchall():
            if row["id"] not in keep:
                conn.execute("DELETE FROM purchase_order_items WHERE id=?", (row["id"],))
        # Move every line number out of the way first, so reordering can't collide with UNIQUE(po_id, line_no).
        conn.execute("UPDATE purchase_order_items SET line_no = line_no + 100000 WHERE po_id=?", (po_id,))
        cols = ITEM_FIELDS + ("net_unit_c", "line_gross_c", "line_discount_c", "line_total_c")
        for n, item in enumerate(after["items"], 1):
            if item.get("id"):
                sets = ", ".join(f"{c} = ?" for c in cols)
                conn.execute(f"UPDATE purchase_order_items SET line_no = ?, {sets} WHERE id = ?",
                             [n, *[item[c] for c in cols], item["id"]])
            else:
                insert_items(conn, po_id, [item], start=n)
        revision = po["revision"] + 1
        conn.execute("UPDATE purchase_orders SET revision = ? WHERE id = ?", (revision, po_id))
        if po["of_id"]:
            conn.execute("UPDATE order_forms SET revision = revision + 1 WHERE id = ?", (po["of_id"],))
        totals = _totals(after["header"], after["items"])
        store_totals(conn, po_id, totals)
        after["grand_total_c"] = totals.grand_total
        _reshare_siblings(conn, user, shares, po_id, f"{po['po_number']} changed")
        # A changed PO is no longer what the supplier holds. It waits to be issued again; the earlier version is kept.
        if po["status"] == ISSUED:
            conn.execute("UPDATE purchase_orders SET status = ? WHERE id = ?", (APPROVED, po_id))
        elif po["status"] in (PARTIAL, FULL):
            _refresh_delivery(conn, po_id)
        conn.execute("UPDATE po_amendments SET status='APPLIED', revision=?, decided_at=?, decided_by=? WHERE id=?",
                     (revision, timestamp(), user.id, amendment_id))
        audit(conn, user, "PO_AMEND", "purchase_order", po_id, po_id=po_id,
              summary=f"{po['po_number']} revision {revision}, now {fmt_peso(totals.grand_total)}",
              previous=before, new=after)
    sync_parent_of(conn, user, po["of_id"], f"{po['po_number']} revision {revision}")
    return revision


def _refresh_delivery(conn, po_id: int) -> None:
    """A delivered PO whose quantities changed: its delivery status worked out again, as a count would."""
    from . import receiving
    if conn.execute("SELECT 1 FROM branch_counts WHERE po_id = ? AND status = 'APPROVED'", (po_id,)).fetchone():
        receiving._apply_totals(conn, po_id)
        return
    # Received before branch counting existed: only the received quantities on the rows say what came.
    rows = conn.execute("SELECT qty_m, received_qty_m FROM purchase_order_items WHERE po_id = ?", (po_id,)).fetchall()
    status = FULL if all(r["received_qty_m"] >= r["qty_m"] for r in rows) else PARTIAL if any(r["received_qty_m"] for r in rows) else ISSUED
    conn.execute("UPDATE purchase_orders SET status = ? WHERE id = ?", (status, po_id))


def decide_amendment(conn, user: User, amendment_id: int, approved: bool, reason: str = "") -> None:
    with tx(conn):
        a = conn.execute("SELECT * FROM po_amendments WHERE id = ?", (amendment_id,)).fetchone()
        if a is None or a["status"] != PENDING:
            raise POError("That amendment is not waiting for approval.")
        po = _get_po(conn, a["po_id"])
        if approved:
            refuse_if_paid(conn, po["of_id"], "amended")
            if po["status"] not in (APPROVED, ISSUED, PARTIAL):
                raise POError(f"{_label(po)} is {STATUS_LABELS[po['status']].lower()}, so this amendment can't be applied. Reject it.")
        require(user, "po.approve")
        p = policy(conn)
        require_listed_approver(conn, user)
        if a["requested_by"] == user.id and not p["allow_self_approval"]:
            raise POError("You requested this amendment, so someone else has to approve it.")
        after = _normalize_stored(json.loads(a["after_json"]))
        if approved:
            _recheck_waiting(conn, po["id"], after)
            _apply_amendment(conn, user, po, amendment_id, after, json.loads(a["before_json"]))
            _record_approval(conn, po["id"], "APPROVED", after["grand_total_c"], user.id, amendment_id=amendment_id,
                             remarks=reason)
            return
        if not reason.strip():
            raise POError("Write the reason for rejecting this amendment.")
        conn.execute("UPDATE po_amendments SET status='REJECTED', decided_by=?, decided_at=? WHERE id=?",
                     (user.id, timestamp(), amendment_id))
        _record_approval(conn, po["id"], "REJECTED", after["grand_total_c"], user.id, amendment_id=amendment_id,
                         reason=reason.strip())
        audit(conn, user, "PO_AMEND_REJECT", "purchase_order", po["id"], po_id=po["id"],
              summary=f"Amendment rejected: {reason.strip()}")


# ------------------------------------------------------------------ lists

def search(conn, *, text: str = "", supplier_id=None, department_id=None, branch_code=None, status=None, year=None,
           date_from=None, date_to=None, of_number: str = "", po_number: str = "", approval=None, of_id=None, tab=None,
           limit: int = 2000, product: str = "") -> list:
    """approval: AUTO (within the limit), APPROVED (by the approver), or PENDING."""
    where, args = [], []
    if product:
        where.append("EXISTS (SELECT 1 FROM purchase_order_items i WHERE i.po_id = p.id AND (i.item_code LIKE ? OR i.description LIKE ?))")
        args += [f"%{product}%"] * 2
    for word in text.split():
        where.append("(p.po_number LIKE ? OR p.legacy_po_number LIKE ? OR f.of_number LIKE ? OR s.name LIKE ? OR s.code LIKE ?"
                     " OR EXISTS (SELECT 1 FROM delivery_receipts d WHERE d.po_id = p.id AND d.dr_number LIKE ?))")
        args += [f"%{word}%"] * 6
    for column, value in (("p.po_number", po_number), ("f.of_number", of_number)):
        if value and value.strip():
            where.append(f"{column} LIKE ?")
            args.append(f"%{value.strip()}%")
    for column, value in (("p.supplier_id", supplier_id), ("p.branch_code", branch_code), ("p.status", status), ("p.of_id", of_id)):
        if value:
            where.append(f"{column} = ?")
            args.append(value)
    if tab:
        where.append(f"p.status IN ({','.join('?' * len(PO_TAB_STATUSES[tab]))})")
        args += PO_TAB_STATUSES[tab]
    if approval == "AUTO":
        where.append("p.auto_approved = 1")
    elif approval == "APPROVED":
        where.append("p.auto_approved = 0 AND p.approved_by IS NOT NULL")
    elif approval == "PENDING":
        where.append("p.status = 'PENDING_APPROVAL'")
    if year:
        where.append("substr(p.po_date, 1, 4) = ?")
        args.append(str(year))
    if date_from:
        where.append("p.po_date >= ?")
        args.append(date_from)
    if date_to:
        where.append("p.po_date <= ?")
        args.append(date_to)
    if department_id:
        where.append("EXISTS (SELECT 1 FROM purchase_order_items i WHERE i.po_id = p.id AND i.department_id = ?)")
        args.append(department_id)
    sql = ("SELECT p.*, s.name AS supplier, s.code AS supplier_code, p.branch_name AS branch, f.of_number,"
           " (SELECT GROUP_CONCAT(DISTINCT i.department_id) FROM purchase_order_items i WHERE i.po_id = p.id) AS depts,"
           " (SELECT COUNT(*) FROM purchase_order_items i WHERE i.po_id = p.id) AS product_count,"
           " (SELECT GROUP_CONCAT(d.dr_number, ', ') FROM delivery_receipts d WHERE d.po_id = p.id) AS dr_numbers"
           " FROM purchase_orders p JOIN suppliers s ON s.id = p.supplier_id LEFT JOIN order_forms f ON f.id = p.of_id"
           + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY p.po_date DESC, p.id DESC LIMIT ?")
    return conn.execute(sql, [*args, limit]).fetchall()


def dashboard_counts(conn, **filters) -> dict:
    rows = search(conn, **filters, limit=1000000)
    counts = {s: 0 for s in STATUS_LABELS}
    amount = 0
    for r in rows:
        counts[r["status"]] += 1
        if r["status"] not in (DRAFT, PENDING, CANCELLED):
            amount += r["grand_total_c"]
    by_tab = {tab: sum(counts[s] for s in statuses) for tab, statuses in PO_TAB_STATUSES.items()}
    return {"total": len(rows), "by_status": counts, "by_tab": by_tab, "purchasing_amount_c": amount}


def next_step(po) -> str:
    """What has to happen next to this PO, in words."""
    return {APPROVED: "Print and issue", ISSUED: "Awaiting delivery", PARTIAL: "Awaiting delivery", DRAFT: "Finish and submit", PENDING: "Approve"}.get(po["status"], "")


def pending_amendments(conn) -> list:
    return conn.execute(
        "SELECT a.*, p.po_number, p.grand_total_c, s.name AS supplier, u.full_name AS requested_name"
        " FROM po_amendments a JOIN purchase_orders p ON p.id=a.po_id JOIN suppliers s ON s.id=p.supplier_id"
        " JOIN users u ON u.id=a.requested_by WHERE a.status='PENDING_APPROVAL' ORDER BY a.requested_at").fetchall()


def audit_trail(conn, po_id: int) -> list:
    return conn.execute("SELECT * FROM audit_logs WHERE po_id = ? ORDER BY id", (po_id,)).fetchall()


def amendments(conn, po_id: int) -> list:
    return conn.execute(
        "SELECT a.*, u.full_name AS requested_name FROM po_amendments a JOIN users u ON u.id=a.requested_by"
        " WHERE a.po_id = ? ORDER BY a.id", (po_id,)).fetchall()


# ------------------------------------------------------------------ attachments

# Files that run as programs when opened. They are never attached, and an older one is never started from the PO screen.
# Which document types are allowed, and how large, is still to be decided; until then only programs are refused.
PROGRAM_SUFFIXES = frozenset((".exe", ".com", ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh",
                              ".hta", ".scr", ".pif", ".lnk", ".msi", ".msp", ".cpl", ".jar", ".reg",
                              ".py", ".pyw", ".pyz", ".chm", ".hlp", ".url", ".website", ".msc", ".appref-ms", ".application",
                              ".gadget", ".scf", ".sct", ".wsc", ".ws", ".vb", ".inf", ".settingcontent-ms", ".xll", ".xlam", ".ppam",
                              ".iso", ".img", ".vhd", ".vhdx", ".library-ms", ".search-ms", ".diagcab", ".mst", ".psc1", ".psd1", ".cab"))
# A generous ceiling so one file can't fill the disk or the memory. An assumption until decided otherwise.
MAX_ATTACHMENT_BYTES = 200 * 1024 * 1024
_CHUNK = 1024 * 1024


def is_program(name: str) -> bool:
    """Whether Windows would run this file. Windows ignores trailing dots and spaces, so 'quote.pdf.exe. ' is a program too."""
    return Path(str(name).rstrip(" .")).suffix.lower() in PROGRAM_SUFFIXES


def file_sha256(path: str | Path) -> str:
    """Read in pieces, so a large file never has to fit in memory."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def add_attachment(conn, user: User, po_id: int, source: str | Path) -> int:
    require(user, "po.edit")
    source = Path(source)
    if not source.is_file():
        raise POError("That file can't be found.")
    if is_program(source.name):
        raise POError("Programs can't be attached to a PO. Attach the document itself, such as a PDF or a photo.")
    size = source.stat().st_size
    if size == 0:
        raise POError("That file is empty, so there's nothing to attach.")
    if size > MAX_ATTACHMENT_BYTES:
        raise POError(f"That file is too large to attach. The limit is {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB.")
    folder = data_dir() / "attachments"
    folder.mkdir(exist_ok=True)
    stored = f"{uuid.uuid4().hex}{Path(source.name.rstrip(' .')).suffix}"
    target = folder / stored
    try:
        shutil.copy2(source, target)
        digest = file_sha256(target)
        size = target.stat().st_size
        with tx(conn):
            _get_po(conn, po_id)
            cur = conn.execute(
                "INSERT INTO po_attachments(po_id, file_name, stored_name, sha256, size_bytes, added_by, added_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (po_id, source.name, stored, digest, size, user.id, timestamp()))
            audit(conn, user, "PO_ATTACH", "purchase_order", po_id, po_id=po_id, summary=f"Attached {source.name}")
    except BaseException:
        # Nothing was recorded, so the copy would only be left behind with no PO pointing at it.
        target.unlink(missing_ok=True)
        raise
    return cur.lastrowid


def list_attachments(conn, po_id: int, user: User | None = None) -> list:
    """With a user, it's someone looking: receivers count blind and the warehouse has no PO page, so neither sees a PO's files."""
    if user is not None and user.roles & {"RECEIVER", "WAREHOUSE"}:
        raise PermissionDenied(f"{user.display} does not have permission to see a PO's files.")
    return conn.execute("SELECT * FROM po_attachments WHERE po_id=? AND removed_at IS NULL ORDER BY id", (po_id,)).fetchall()


def attachment_path(row) -> Path:
    return data_dir() / "attachments" / row["stored_name"]


def attachment_changed(row) -> bool:
    """Whether the kept file is no longer the one that was attached. A missing file counts as changed."""
    path = attachment_path(row)
    if not path.is_file():
        return True
    return file_sha256(path) != row["sha256"]
