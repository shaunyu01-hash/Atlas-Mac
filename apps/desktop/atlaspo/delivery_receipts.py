"""Delivery receipts: the paper a supplier hands over with a delivery. It shows no prices, because the supplier writes in their own.

Each issued PO gets one, numbered after it, so PO 2026-2-00017 has DR 2026-2-DR-00017. Another delivery on that PO gets 2026-2-DR-00017-2.
Suppliers we sticker for, who have a sticker deduction, get theirs printed at the office with the PO. The others print their own, from a copy sent by email.
"""
from __future__ import annotations

import json

from .auth import User, require
from .db import POError, audit, timestamp, tx

OFFICE, EMAIL = "OFFICE", "EMAIL"
CHANNELS = {OFFICE: "Printed at the office", EMAIL: "Supplier prints, by email"}
# Once goods have all arrived or the PO is over, nobody needs a DR sent.
_TO_SEND = ("ISSUED", "PARTIALLY_DELIVERED")
_SELECT = ("SELECT d.*, p.po_number, p.revision, p.status AS po_status, p.of_id, p.branch_name AS branch, f.of_number, s.name AS supplier, s.code AS supplier_code, s.email,"
           " e.full_name AS emailed_name,"
           " (SELECT COUNT(*) FROM audit_logs a WHERE a.action = 'DR_PRINT' AND a.entity_type = 'delivery_receipt' AND a.entity_id = d.id) AS prints"
           " FROM delivery_receipts d JOIN purchase_orders p ON p.id = d.po_id JOIN suppliers s ON s.id = p.supplier_id LEFT JOIN order_forms f ON f.id = p.of_id"
           " LEFT JOIN users e ON e.id = d.emailed_by")


def format_number(year: int, branch_code: int, seq: int, delivery_no: int = 1) -> str:
    return f"{year}-{branch_code}-DR-{seq:05d}" + (f"-{delivery_no}" if delivery_no > 1 else "")


def _channel(conn, po) -> str:
    """Taken from the PO's copy of the supplier, so a later change to the supplier doesn't change how an order already placed goes out."""
    supplier = json.loads(po["supplier_snapshot"]) if po["supplier_snapshot"] else {}
    if "sticker_rate_c" not in supplier:
        supplier = dict(conn.execute("SELECT sticker_rate_c FROM suppliers WHERE id = ?", (po["supplier_id"],)).fetchone())
    return OFFICE if supplier["sticker_rate_c"] else EMAIL


def _make(conn, user: User, po, delivery_no: int) -> int:
    number = format_number(po["po_year"], po["branch_code"], po["po_seq"], delivery_no)
    dr_id = conn.execute("INSERT INTO delivery_receipts(dr_number, po_id, delivery_no, channel, created_at, created_by) VALUES (?,?,?,?,?,?)",
                         (number, po["id"], delivery_no, _channel(conn, po), timestamp(), user.id)).lastrowid
    audit(conn, user, "DR_CREATE", "delivery_receipt", dr_id, po_id=po["id"], summary=f"{number} made for {po['po_number']}")
    return dr_id


def ensure_first(conn, user: User, po_id: int) -> int:
    """The PO's first DR, made if it doesn't have one. Runs inside the caller's transaction.
    Making one is part of issuing a PO, or of Accounting entering the DRs for a payment, so it needs one of those permissions."""
    if not user.can("payment.edit"):
        require(user, "po.issue")
    return _ensure_first(conn, user, po_id)


def _ensure_first(conn, user: User, po_id: int) -> int:
    """ensure_first without the permission check, for a caller that checked its own, such as printing an order again."""
    row = conn.execute("SELECT id FROM delivery_receipts WHERE po_id = ? AND delivery_no = 1", (po_id,)).fetchone()
    if row:
        return row["id"]
    return _make(conn, user, conn.execute("SELECT * FROM purchase_orders WHERE id = ?", (po_id,)).fetchone(), 1)


def another_delivery(conn, user: User, po_id: int) -> dict:
    """A new number for a further delivery on the same PO, so each delivery has its own."""
    require(user, "po.issue")
    with tx(conn):
        po = conn.execute("SELECT * FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
        if po is None:
            raise POError("That PO no longer exists.")
        if po["status"] not in _TO_SEND:
            raise POError(f"{po['po_number']} isn't waiting for more goods, so it can't take another DR.")
        ensure_first(conn, user, po_id)
        latest = conn.execute("SELECT MAX(delivery_no) FROM delivery_receipts WHERE po_id = ?", (po_id,)).fetchone()[0]
        dr_id = _make(conn, user, po, latest + 1)
    return get(conn, dr_id)


def get(conn, dr_id: int) -> dict:
    row = conn.execute(_SELECT + " WHERE d.id = ?", (dr_id,)).fetchone()
    if row is None:
        raise POError("That DR no longer exists.")
    return dict(row)


def search(conn, text: str = "", limit: int = 2000) -> list[dict]:
    where, args = [], []
    for word in text.split():
        where.append("(d.dr_number LIKE ? OR p.po_number LIKE ? OR f.of_number LIKE ? OR s.name LIKE ? OR s.code LIKE ?)")
        args += [f"%{word}%"] * 5
    return [dict(r) for r in conn.execute(_SELECT + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY d.id DESC LIMIT ?", [*args, limit])]


def first_office_ids(conn, po_ids: list[int]) -> list[int]:
    """The DRs that print at the office along with these POs."""
    marks = ",".join("?" * len(po_ids))
    return [r[0] for r in conn.execute(f"SELECT id FROM delivery_receipts WHERE channel = ? AND delivery_no = 1 AND po_id IN ({marks}) ORDER BY po_id", [OFFICE, *po_ids])]


def needs_email(d: dict) -> bool:
    """A PO amended and issued again needs it sent again, because the supplier's copy is out of date."""
    return d["channel"] == EMAIL and d["po_status"] in _TO_SEND and not (d["emailed_at"] and d["emailed_revision"] >= d["revision"])


def waiting_to_email(conn, text: str = "") -> list[dict]:
    """What still has to go out, one email per order."""
    groups: dict[tuple, dict] = {}
    for d in sorted(search(conn, text, 1000000), key=lambda r: r["id"]):
        if not needs_email(d):
            continue
        key = ("OF", d["of_id"]) if d["of_id"] else ("PO", d["po_id"])
        group = groups.setdefault(key, {"key": key, "reference": d["of_number"] or d["po_number"], "supplier": d["supplier"], "supplier_code": d["supplier_code"], "email": d["email"], "receipts": []})
        group["receipts"].append(d)
    return list(groups.values())


def email_text(group: dict) -> tuple[str, str]:
    """Subject and message for the email, for a person to read over and send."""
    numbers = ", ".join(d["dr_number"] for d in group["receipts"])
    return (f"Delivery Receipts for {group['reference']}",
            f"Dear {group['supplier']},\n\nAttached are the Delivery Receipts for {group['reference']}: {numbers}.\n\n"
            "Please print one for each delivery, write in the pieces delivered, your unit price and total, and sign it. Hand it over with the goods.\n\nThank you.")


def mark_emailed(conn, user: User, dr_ids: list[int]) -> None:
    """A person confirms it went out. The program can't send email yet, so it can't know by itself."""
    require(user, "po.issue")
    with tx(conn):
        for dr_id in dr_ids:
            d = get(conn, dr_id)
            conn.execute("UPDATE delivery_receipts SET emailed_at = ?, emailed_by = ?, emailed_revision = ? WHERE id = ?", (timestamp(), user.id, d["revision"], dr_id))
            audit(conn, user, "DR_EMAILED", "delivery_receipt", dr_id, po_id=d["po_id"], summary=f"{d['dr_number']} emailed to {d['email'] or d['supplier']}")
