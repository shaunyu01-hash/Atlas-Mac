"""Debit memos: what a supplier is charged for goods that never came. Damaged goods go on a claim stub instead, because they have to be collected.

Starts from what two receivers agreed on. The program works out what's still uncovered, and a purchaser approves it, which issues it, so management can see who approved money coming back. Issuing assigns a permanent number,
at the PO's net cost per piece, and keeps that exact PDF for good. A cancelled memo keeps its number. Deducting it from a payment belongs to payables, which
isn't built, so for now it's also entered in the accounting system, with its number saved here.
"""
from __future__ import annotations

import hashlib
from decimal import Decimal

from . import receiving
from .auth import User, require
from .db import POError, audit, timestamp, today, tx
from .money import fmt_peso, fmt_qty, parse_qty, round_centavos

SHORT, DAMAGED = "SHORT", "DAMAGED"
REASONS = {SHORT: "Short", DAMAGED: "Damaged"}
ISSUED, CANCELLED = "ISSUED", "CANCELLED"
STATUS_LABELS = {ISSUED: "Approved", CANCELLED: "Cancelled"}
CANCELLABLE = (ISSUED,)


def amount_c(pieces_m: int, net_unit_c: int, pieces_per_unit: int, share: tuple[int, int] = (1, 1)) -> int:
    """Pieces at the PO's net cost, after its overall discount when share gives one. Worked out from the pack's cost, so rounding a per-piece cost first can't change the total."""
    return round_centavos(Decimal(pieces_m) * net_unit_c * share[0] / (pieces_per_unit * 1000 * share[1]))


def discount_share(conn, po_id: int) -> tuple[int, int]:
    """What's left of each peso of merchandise after the PO's overall discount, as (kept, whole). A discounted item is charged back at the discounted price,
    so a shortage or damage never takes back more than was paid for it."""
    whole = conn.execute("SELECT COALESCE(SUM(line_total_c), 0) FROM purchase_order_items WHERE po_id = ?", (po_id,)).fetchone()[0]
    off = conn.execute("SELECT overall_discount_c FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
    if not off or not off[0] or not whole:
        return (1, 1)
    return (whole - off[0], whole)


def shown_cost(net_unit_c: int, share: tuple[int, int]) -> int:
    """The pack cost printed on a memo or stub: after the overall discount, so pieces times cost matches the amount."""
    return round_centavos(Decimal(net_unit_c) * share[0] / share[1])


def outstanding(conn, user: User, po_id: int) -> list[dict]:
    """What's short on a PO and not on a debit memo yet. Refused while counting is open, because the figures could still change."""
    require(user, "dm.issue")
    if receiving.progress(conn, po_id):
        raise POError("This PO is still being counted. A debit memo has to wait until the counts agree.")
    return _outstanding(conn, po_id)


def _outstanding(conn, po_id: int) -> list[dict]:
    charged = receiving.dm_pieces(conn, po_id)
    items = {r["id"]: r for r in conn.execute("SELECT id, net_unit_c, pieces_per_unit FROM purchase_order_items WHERE po_id = ?", (po_id,))}
    share = discount_share(conn, po_id)
    out = []
    for line in receiving.lines_of(conn, po_id):
        item = items[line["id"]]
        # Pieces lost between the warehouse and the branch aren't the supplier's, so a shortage is measured against what the warehouse counted, when it counted.
        left = max(line["ordered_m"] - line["supplier_m"], 0) - charged.get((line["id"], SHORT), 0)
        if left > 0:
            out.append({"po_item_id": line["id"], "item_code": line["item_code"], "description": line["description"], "size": line["size"], "color": line["color"], "uom": line["uom"],
                        "reason": SHORT, "pieces_m": left, "net_unit_c": shown_cost(item["net_unit_c"], share), "cost_c": item["net_unit_c"], "share": share, "pieces_per_unit": item["pieces_per_unit"],
                        "amount_c": amount_c(left, item["net_unit_c"], item["pieces_per_unit"], share)})
    return out


def _agreed(conn, po_id: int) -> bool:
    """A charge only follows an agreed count on a PO that's still live."""
    return conn.execute("SELECT 1 FROM purchase_orders p WHERE p.id = ? AND p.status <> 'CANCELLED' AND EXISTS (SELECT 1 FROM branch_counts b WHERE b.po_id = p.id AND b.status = 'APPROVED')",
                        (po_id,)).fetchone() is not None


def can_issue(conn, po_id: int) -> bool:
    return _agreed(conn, po_id) and not receiving.progress(conn, po_id) and bool(_outstanding(conn, po_id))


def issue(conn, user: User, po_id: int, pieces: dict[tuple[int, str], str], remarks: str = "") -> dict:
    """Issues a memo for the pieces given, keyed by (PO item, reason). Blank or zero leaves that line off. Nobody can charge more than is outstanding."""
    from . import pdf
    require(user, "dm.issue")
    with tx(conn):
        po = conn.execute("SELECT * FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
        if po is None:
            raise POError("That PO no longer exists.")
        if receiving.progress(conn, po_id):
            raise POError("This PO is still being counted. A debit memo has to wait until the counts agree.")
        if not _agreed(conn, po_id):
            raise POError("A debit memo needs a count agreed at the branch, on a PO that isn't cancelled.")
        chosen = []
        for line in _outstanding(conn, po_id):
            text = str(pieces.get((line["po_item_id"], line["reason"]), "")).strip()
            wanted = parse_qty(text, line["uom"]) if text else 0
            if wanted > line["pieces_m"]:
                raise POError(f"{line['description']} ({REASONS[line['reason']]}): only {line['pieces_m'] // 1000:,} pieces can be charged.")
            if wanted:
                chosen.append((line, wanted))
        if not chosen:
            raise POError("There's nothing to charge on this debit memo.")
        date = today()
        conn.execute("INSERT INTO dm_number_sequences(year, last_number) VALUES (?, 1) ON CONFLICT(year) DO UPDATE SET last_number = last_number + 1", (date.year,))
        seq = conn.execute("SELECT last_number FROM dm_number_sequences WHERE year = ?", (date.year,)).fetchone()[0]
        number = f"{date.year}-DM-{seq:05d}"
        rows = [(line, wanted, amount_c(wanted, line["cost_c"], line["pieces_per_unit"], line["share"])) for line, wanted in chosen]
        total = sum(r[2] for r in rows)
        stamp = timestamp()
        dm_id = conn.execute("INSERT INTO debit_memos(dm_number, dm_year, dm_seq, dm_date, po_id, supplier_id, status, total_c, remarks, prepared_by, prepared_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (number, date.year, seq, date.isoformat(), po_id, po["supplier_id"], ISSUED, total, (remarks or "").strip(), user.id, stamp)).lastrowid
        conn.executemany("INSERT INTO debit_memo_items(dm_id, line_no, po_item_id, reason, item_code, description, size, color, uom, pieces_m, net_unit_c, pieces_per_unit, amount_c) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         [(dm_id, n, line["po_item_id"], line["reason"], line["item_code"], line["description"], line["size"], line["color"], line["uom"], wanted, line["net_unit_c"], line["pieces_per_unit"], amount)
                          for n, (line, wanted, amount) in enumerate(rows, 1)])
        document = pdf.render_debit_memo(conn, dm_id)
        conn.execute("UPDATE debit_memos SET pdf = ?, sha256 = ? WHERE id = ?", (document, hashlib.sha256(document).hexdigest(), dm_id))
        audit(conn, user, "DM_ISSUE", "debit_memo", dm_id, po_id=po_id, summary=f"{number} issued on {po['po_number']} for {fmt_peso(total)}")
    return get(conn, user, dm_id)


def cancel(conn, user: User, dm_id: int, reason: str) -> None:
    """Management only, because it puts money back on the supplier's side. A late delivery is fine as long as it came: when the pieces arrive after the
    check, management cancels the memo, and what it took off is owed to the supplier and added to their next check. Any memo
    cancelled after the order was paid is paid back on that next check, with its number and the cancel reason shown there."""
    require(user, "dm.cancel")
    if not reason.strip():
        raise POError("Write the reason for cancelling this debit memo.")
    with tx(conn):
        dm = _row(conn, dm_id)
        if dm["status"] != ISSUED:
            raise POError(f"{dm['dm_number']} is already cancelled.")
        # Worked out before the memo stops counting, so the record says what really happened to its pieces.
        on_memo = conn.execute("SELECT COALESCE(SUM(pieces_m), 0) FROM debit_memo_items WHERE dm_id = ? AND reason = ?", (dm_id, SHORT)).fetchone()[0]
        came = min(arrived_since(conn, dm), on_memo)
        # A memo cancelled after its order was paid is paid back on the supplier's next check. The check that had paid
        # the order is kept with the memo, in the same change that cancels it, so the payback is recorded once.
        paid = conn.execute("SELECT c.id, c.check_number FROM payment_checks c JOIN payment_check_ofs o ON o.check_id = c.id WHERE o.of_id = ? AND c.status = 'ACTIVE'",
                            (_of_id(conn, dm["po_id"]),)).fetchone()
        conn.execute("UPDATE debit_memos SET status = ?, cancelled_by = ?, cancelled_at = ?, cancel_reason = ?, paid_back_check_id = ? WHERE id = ?",
                     (CANCELLED, user.id, timestamp(), reason.strip(), paid["id"] if paid else None, dm_id))
        check = paid["check_number"] if paid else ""
        summary = f"{dm['dm_number']} cancelled: {reason.strip()}"
        if check:
            summary += f". Check {check} already paid the order; {fmt_peso(dm['total_c'])} goes on the supplier's next check"
        if came:
            summary += f". {fmt_qty(came)} of its pieces {'was' if came == 1000 else 'were'} delivered since"
        short = on_memo - came
        # Paid back, so its pieces still short are not charged again as short with no debit memo.
        if short > 0 and not check:
            one = short == 1000
            summary += (f". {fmt_qty(short)} {'piece on it counts' if one else 'pieces on it count'} as short again" if came
                        else f". Its {fmt_qty(short)} {'piece counts' if one else 'pieces count'} as short again")
        audit(conn, user, "DM_CANCEL", "debit_memo", dm_id, po_id=dm["po_id"], summary=summary)


def save_external_number(conn, user: User, dm_id: int, number: str) -> None:
    require(user, "dm.issue")
    with tx(conn):
        dm = _row(conn, dm_id)
        conn.execute("UPDATE debit_memos SET legacy_dm_number = ? WHERE id = ?", (number.strip(), dm_id))
        audit(conn, user, "DM_EXTERNAL_NUMBER", "debit_memo", dm_id, po_id=dm["po_id"], summary=f"{dm['dm_number']} external number {number.strip() or 'cleared'}")


def _of_id(conn, po_id: int):
    return conn.execute("SELECT of_id FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()[0]


def can_cancel(conn, user: User, row) -> bool:
    """Whether cancel() would go ahead for this person, so the button only shows when it can."""
    return user.can("dm.cancel") and row["status"] in CANCELLABLE


def arrived_since(conn, dm) -> int:
    """Pieces on this memo the supplier has delivered since, say in a late delivery. Management can cancel the memo, and the purchaser issues one for what's still short."""
    if dm["status"] != ISSUED:
        return 0
    lines = {line["id"]: line for line in receiving.lines_of(conn, dm["po_id"])}
    charged = receiving.dm_pieces(conn, dm["po_id"])
    over = 0
    for (item_id,) in conn.execute("SELECT DISTINCT po_item_id FROM debit_memo_items WHERE dm_id = ?", (dm["id"],)).fetchall():
        line = lines.get(item_id)
        if line:
            over += max(charged.get((item_id, SHORT), 0) - max(line["ordered_m"] - line["supplier_m"], 0), 0)
    return over


def _row(conn, dm_id: int):
    row = conn.execute("SELECT * FROM debit_memos WHERE id = ?", (dm_id,)).fetchone()
    if row is None:
        raise POError("That debit memo no longer exists.")
    return row


_SELECT = ("SELECT d.id, d.dm_number, d.dm_date, d.po_id, d.status, d.total_c, d.remarks, d.prepared_at, d.cancelled_at, d.cancel_reason, d.legacy_dm_number,"
           " p.po_number, p.branch_name, f.of_number, s.name AS supplier, u.full_name AS prepared_name, c.full_name AS cancelled_name,"
           " (SELECT SUM(i.pieces_m) FROM debit_memo_items i WHERE i.dm_id = d.id) AS pieces_m,"
           " (SELECT COUNT(*) FROM audit_logs a WHERE a.action = 'DM_PRINT' AND a.entity_id = d.id) AS prints"
           " FROM debit_memos d JOIN purchase_orders p ON p.id = d.po_id JOIN suppliers s ON s.id = d.supplier_id LEFT JOIN order_forms f ON f.id = p.of_id"
           " JOIN users u ON u.id = d.prepared_by LEFT JOIN users c ON c.id = d.cancelled_by")


def get(conn, user: User, dm_id: int) -> dict:
    require(user, "receiving.review")
    row = conn.execute(_SELECT + " WHERE d.id = ?", (dm_id,)).fetchone()
    if row is None:
        raise POError("That debit memo no longer exists.")
    return dict(row)


def search(conn, user: User, text: str = "", status: str | None = None, limit: int = 2000) -> list[dict]:
    require(user, "receiving.review")
    where, args = [], []
    if status:
        where.append("d.status = ?")
        args.append(status)
    for word in text.split():
        where.append("(d.dm_number LIKE ? OR p.po_number LIKE ? OR f.of_number LIKE ? OR s.name LIKE ? OR s.code LIKE ? OR d.legacy_dm_number LIKE ?)")
        args += [f"%{word}%"] * 6
    return [dict(r) for r in conn.execute(_SELECT + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY d.id DESC LIMIT ?", [*args, limit])]


def items(conn, user: User, dm_id: int) -> list[dict]:
    require(user, "receiving.review")
    return [dict(r) for r in conn.execute("SELECT * FROM debit_memo_items WHERE dm_id = ? ORDER BY line_no", (dm_id,))]


def document(conn, user: User, dm_id: int) -> bytes:
    """The exact PDF kept on its first print. Any later print is a copy marked as one, and every print is recorded."""
    from . import pdf
    require(user, "receiving.review")
    dm = _row(conn, dm_id)
    earlier = conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action = 'DM_PRINT' AND entity_id = ?", (dm_id,)).fetchone()[0]
    with tx(conn):
        audit(conn, user, "DM_PRINT", "debit_memo", dm_id, po_id=dm["po_id"], summary=f"{dm['dm_number']} printed")
    return pdf.stamp_copy(dm["pdf"], earlier + 1, user.display) if earlier else dm["pdf"]
