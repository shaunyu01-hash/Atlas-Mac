"""Claim stubs: damaged goods charged to the supplier and held for them to collect.

Starts from the damaged pieces two receivers agreed on. A purchaser approves the stub, which gives it a permanent number at the PO's net cost and keeps that exact
PDF. The goods come back from the branch to the main warehouse, where the warehouse counts them. The supplier's 90 days start on the day they received the check
that took the stub off, not on the day the goods arrived. They either collect, or after 90 days the goods are forfeited
and can be sold. Deducting it from a payment belongs to payables, which isn't built, so for now it's also entered in the accounting system, with its number saved here.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date

from . import receiving
from .auth import User, require
from .db import POError, audit, get_setting, timestamp, today, tx
from .debit_memos import DAMAGED, amount_c, discount_share, shown_cost
from .money import fmt_peso, fmt_qty, parse_qty
from .orders import paid_by, refuse_if_paid

ISSUED, AT_WAREHOUSE, COLLECTED, FORFEITED, CANCELLED = "ISSUED", "AT_WAREHOUSE", "COLLECTED", "FORFEITED", "CANCELLED"
FORFEIT_AFTER_DAYS = 90
CANCELLABLE = (ISSUED, AT_WAREHOUSE)


def warehouse_name(conn) -> str:
    return get_setting(conn, "company.warehouse_name") or "Central Warehouse"


def status_labels(conn) -> dict:
    return {ISSUED: f"Coming back to {warehouse_name(conn)}", AT_WAREHOUSE: f"At {warehouse_name(conn)}", COLLECTED: "Collected by supplier", FORFEITED: "Forfeited: for sale", CANCELLED: "Cancelled"}


def outstanding(conn, user: User, po_id: int) -> list[dict]:
    """Damaged pieces on a PO not on a claim stub yet. Refused while counting is open, because the figures could still change."""
    require(user, "dm.issue")
    if receiving.progress(conn, po_id):
        raise POError("This PO is still being counted. A claim stub has to wait until the counts agree.")
    return _outstanding(conn, po_id)


def _outstanding(conn, po_id: int) -> list[dict]:
    charged = receiving.dm_pieces(conn, po_id)
    items = {r["id"]: r for r in conn.execute("SELECT id, net_unit_c, pieces_per_unit FROM purchase_order_items WHERE po_id = ?", (po_id,))}
    share = discount_share(conn, po_id)
    out = []
    for line in receiving.lines_of(conn, po_id):
        item = items[line["id"]]
        left = line["damaged_m"] - charged.get((line["id"], DAMAGED), 0)
        if left > 0:
            out.append({"po_item_id": line["id"], "item_code": line["item_code"], "description": line["description"], "size": line["size"], "color": line["color"], "uom": line["uom"], "reason": DAMAGED,
                        "pieces_m": left, "net_unit_c": shown_cost(item["net_unit_c"], share), "cost_c": item["net_unit_c"], "share": share, "pieces_per_unit": item["pieces_per_unit"],
                        "amount_c": amount_c(left, item["net_unit_c"], item["pieces_per_unit"], share)})
    return out


def _agreed(conn, po_id: int) -> bool:
    """A charge only follows an agreed count on a PO that's still live."""
    return conn.execute("SELECT 1 FROM purchase_orders p WHERE p.id = ? AND p.status <> 'CANCELLED' AND EXISTS (SELECT 1 FROM branch_counts b WHERE b.po_id = p.id AND b.status = 'APPROVED')",
                        (po_id,)).fetchone() is not None


def can_issue(conn, po_id: int) -> bool:
    return _agreed(conn, po_id) and not receiving.progress(conn, po_id) and bool(_outstanding(conn, po_id))


def issue(conn, user: User, po_id: int, pieces: dict[tuple[int, str], str], remarks: str = "") -> dict:
    """Approves a stub for the pieces given, keyed by (PO item, reason). Blank or zero leaves that line off. Nobody can charge more than is outstanding."""
    from . import pdf
    require(user, "dm.issue")
    with tx(conn):
        po = conn.execute("SELECT * FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
        if po is None:
            raise POError("That PO no longer exists.")
        if receiving.progress(conn, po_id):
            raise POError("This PO is still being counted. A claim stub has to wait until the counts agree.")
        if not _agreed(conn, po_id):
            raise POError("A claim stub needs a count agreed at the branch, on a PO that isn't cancelled.")
        chosen = []
        for line in _outstanding(conn, po_id):
            text = str(pieces.get((line["po_item_id"], DAMAGED), "")).strip()
            wanted = parse_qty(text, line["uom"]) if text else 0
            if wanted > line["pieces_m"]:
                raise POError(f"{line['description']}: only {line['pieces_m'] // 1000:,} damaged pieces can be charged.")
            if wanted:
                chosen.append((line, wanted, amount_c(wanted, line["cost_c"], line["pieces_per_unit"], line["share"])))
        if not chosen:
            raise POError("There's nothing to charge on this claim stub.")
        cs_id = _create(conn, user, po, chosen, remarks)
    return get(conn, user, cs_id)


def _create(conn, user: User, po, chosen: list, remarks: str, late_reason: str = "") -> int:
    """Numbers, stores and prints a stub for the chosen (line, pieces, amount). With late_reason, it's damage found after the delivery was settled."""
    from . import pdf
    po_id = po["id"]
    with tx(conn):
        when = today()
        conn.execute("INSERT INTO cs_number_sequences(year, last_number) VALUES (?, 1) ON CONFLICT(year) DO UPDATE SET last_number = last_number + 1", (when.year,))
        seq = conn.execute("SELECT last_number FROM cs_number_sequences WHERE year = ?", (when.year,)).fetchone()[0]
        number = f"{when.year}-CS-{seq:05d}"
        total = sum(c[2] for c in chosen)
        cs_id = conn.execute("INSERT INTO claim_stubs(cs_number, cs_year, cs_seq, cs_date, po_id, supplier_id, status, total_c, remarks, approved_by, approved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (number, when.year, seq, when.isoformat(), po_id, po["supplier_id"], ISSUED, total, (remarks or "").strip(), user.id, timestamp())).lastrowid
        conn.executemany("INSERT INTO claim_stub_items(cs_id, line_no, po_item_id, item_code, description, size, color, uom, pieces_m, net_unit_c, pieces_per_unit, amount_c) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         [(cs_id, n, line["po_item_id"], line["item_code"], line["description"], line["size"], line["color"], line["uom"], wanted, line["net_unit_c"], line["pieces_per_unit"], amount)
                          for n, (line, wanted, amount) in enumerate(chosen, 1)])
        if late_reason:
            check = paid_by(conn, po["of_id"])
            check_id = conn.execute("SELECT id FROM payment_checks WHERE check_number = ?", (check,)).fetchone()[0] if check else None
            conn.execute("INSERT INTO late_damage(cs_id, reason, found_by, found_at, paid_before) VALUES (?,?,?,?,?)", (cs_id, late_reason, user.id, timestamp(), check_id))
        document_bytes = pdf.render_claim_stub(conn, cs_id)
        conn.execute("UPDATE claim_stubs SET pdf = ?, sha256 = ? WHERE id = ?", (document_bytes, hashlib.sha256(document_bytes).hexdigest(), cs_id))
        audit(conn, user, "CS_ISSUE", "claim_stub", cs_id, po_id=po_id, summary=f"{number} approved on {po['po_number']} for {fmt_peso(total)}" + (f", damage found after delivery: {late_reason}" if late_reason else ""))
    return cs_id


def late_lines(conn, user: User, number: str, sku: str = "", branch_code: int | None = None, check: bool = True, leaving_out: int | None = None) -> list[dict]:
    """The products on an OF or PO that damage can still be found on, by OF or PO number and, if given, part of the stock code.
    A product can take as many pieces as were received, less those the branch already counted damaged, any damage already found later,
    and any a branch has reported and is waiting for approval. With branch_code, only that branch's PO."""
    if check:
        require(user, "dm.issue")
    number, sku = number.strip().upper(), sku.strip().upper()
    if not number:
        raise POError("Type the OF number or PO number.")
    pos = conn.execute("SELECT p.*, f.of_number FROM purchase_orders p LEFT JOIN order_forms f ON f.id = p.of_id WHERE (upper(p.po_number) = ? OR upper(f.of_number) = ?)"
                       " AND p.status <> 'CANCELLED' ORDER BY p.branch_code", (number, number)).fetchall()
    if not pos:
        raise POError(f"There's no OF or PO numbered {number}.")
    out = []
    for po in pos:
        if branch_code is not None and po["branch_code"] != branch_code:
            continue
        if not _agreed(conn, po["id"]) or receiving.progress(conn, po["id"]):
            continue
        late = {r[0]: r[1] for r in conn.execute("SELECT i.po_item_id, SUM(i.pieces_m) FROM claim_stub_items i JOIN claim_stubs c ON c.id = i.cs_id JOIN late_damage l ON l.cs_id = c.id"
                                                 " WHERE c.po_id = ? AND c.status <> 'CANCELLED' GROUP BY i.po_item_id", (po["id"],))}
        for item_id, pending in conn.execute("SELECT i.po_item_id, SUM(i.pieces_m) FROM claim_request_items i JOIN claim_requests r ON r.id = i.request_id"
                                             " WHERE r.po_id = ? AND r.status = 'PENDING' AND r.id IS NOT ? GROUP BY i.po_item_id", (po["id"], leaving_out)):
            late[item_id] = late.get(item_id, 0) + pending
        items = {r["id"]: r for r in conn.execute("SELECT id, net_unit_c, pieces_per_unit FROM purchase_order_items WHERE po_id = ?", (po["id"],))}
        share = discount_share(conn, po["id"])
        for line in receiving.lines_of(conn, po["id"]):
            if sku and sku not in (line["item_code"] or "").upper():
                continue
            room = line["received_m"] - line["damaged_m"] - late.get(line["id"], 0)
            if room <= 0:
                continue
            item = items[line["id"]]
            out.append({"po_id": po["id"], "po_number": po["po_number"], "of_number": po["of_number"], "branch_name": po["branch_name"], "po_item_id": line["id"],
                        "item_code": line["item_code"], "description": line["description"], "size": line["size"], "color": line["color"], "uom": line["uom"],
                        "received_m": line["received_m"], "room_m": room, "net_unit_c": shown_cost(item["net_unit_c"], share), "cost_c": item["net_unit_c"],
                        "share": share, "pieces_per_unit": item["pieces_per_unit"]})
    if not out:
        raise POError(f"Nothing on {number} can take damage now: it isn't counted and agreed at the branch yet, or every piece received is already charged.")
    return out


def issue_late(conn, user: User, number: str, pieces: dict[int, str], reason: str) -> list[dict]:
    """Claim stubs for damage found after the delivery was settled, one per branch PO, for {PO item id: pieces}. If the order is already paid,
    the stub makes it worth less than its check, and the difference comes off the supplier's next check."""
    require(user, "dm.issue")
    if not reason.strip():
        raise POError("Write how the damage was found, for example: found torn when unpacked.")
    made = []
    with tx(conn):
        lines = late_lines(conn, user, number)
        by_po: dict[int, list] = {}
        for line in lines:
            text = str(pieces.get(line["po_item_id"], "")).strip()
            wanted = parse_qty(text, "PC") if text else 0
            if wanted > line["room_m"]:
                raise POError(f"{line['branch_name']}, {line['description']}: only {fmt_qty(line['room_m'])} pieces can be damaged.")
            if wanted:
                by_po.setdefault(line["po_id"], []).append((line, wanted, amount_c(wanted, line["cost_c"], line["pieces_per_unit"], line["share"])))
        if not by_po:
            raise POError("Type the damaged pieces for at least one product.")
        for po_id, chosen in by_po.items():
            po = conn.execute("SELECT * FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
            made.append(_create(conn, user, po, chosen, f"Damage found after delivery: {reason.strip()}", reason.strip()))
    return [get(conn, user, cs_id) for cs_id in made]


def is_late(conn, cs_id: int):
    return conn.execute("SELECT * FROM late_damage WHERE cs_id = ?", (cs_id,)).fetchone()


def _of_id(conn, po_id: int):
    return conn.execute("SELECT of_id FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()[0]


def can_cancel(conn, user: User, row) -> bool:
    """Whether cancel() would go ahead for this person, so the button only shows when it can."""
    return user.can("dm.cancel") and row["status"] in CANCELLABLE and not paid_by(conn, _of_id(conn, row["po_id"]))


def is_paid(conn, po_id: int) -> bool:
    """Whether the PO's order already has its check, so a stub on it comes off the supplier's next check and can't be cancelled."""
    return bool(paid_by(conn, _of_id(conn, po_id)))


def _row(conn, cs_id: int):
    row = conn.execute("SELECT * FROM claim_stubs WHERE id = ?", (cs_id,)).fetchone()
    if row is None:
        raise POError("That claim stub no longer exists.")
    return row


def record_arrival(conn, user: User, kind: str, number: str, po_id: int, lines: list, pieces: dict[int, str]) -> str:
    """The warehouse counts every line of goods coming back before anyone collects them, and the count stays on record. Returns a note of any line that came short or over.
    lines are the stub's or pass's own lines, each with its id, description, uom and pieces_m. Only the warehouse counts goods in."""
    require(user, "warehouse.receive")
    counted, notes = [], []
    for line in lines:
        text = str(pieces.get(line["id"], "")).strip()
        if not text:
            raise POError(f"{line['description']}: type how many pieces came back, 0 if none.")
        try:
            got = parse_qty(text, "PC")
        except ValueError as e:
            raise POError(f"{line['description']}: {e}") from None
        if got > line["pieces_m"]:
            # Goods that came back can only be what the branch sent; more would invent stock held for the supplier.
            raise POError(f"{line['description']}: that's more than the branch sent. Count again; anything extra isn't on this paper, so set it apart and tell the office.")
        counted.append((line, got))
        if got != line["pieces_m"]:
            notes.append(f"{line['description']}: {fmt_qty(got)} came, {fmt_qty(line['pieces_m'])} on the paper")
    stamp = timestamp()
    conn.executemany("INSERT INTO goods_arrivals(kind, item_id, pieces_m, counted_by, counted_at) VALUES (?,?,?,?,?)", [(kind, line["id"], got, user.id, stamp) for line, got in counted])
    audit(conn, user, f"{kind}_COUNTED", "claim_stub" if kind == "CS" else "item_pass", lines[0]["parent_id"] if lines else None, po_id=po_id,
          summary=f"{number}: {fmt_qty(sum(g for _, g in counted))} pieces counted at {warehouse_name(conn)}" + (". " + "; ".join(notes) if notes else ""),
          new={"pieces": {line["description"]: fmt_qty(got) for line, got in counted}})
    return "; ".join(notes)


def arrival_counts(conn, kind: str, item_ids: list[int]) -> dict[int, int]:
    """What the warehouse counted of each line when it came back. Missing for goods that arrived before counting was asked for."""
    if not item_ids:
        return {}
    return {r[0]: r[1] for r in conn.execute(f"SELECT item_id, pieces_m FROM goods_arrivals WHERE kind = ? AND item_id IN ({','.join('?' * len(item_ids))})", [kind, *item_ids])}


def counted(conn, kind: str, item_ids: list[int]) -> bool:
    return bool(item_ids) and len(arrival_counts(conn, kind, item_ids)) == len(item_ids)


def _lines(conn, cs_id: int) -> list[dict]:
    return [dict(r) | {"parent_id": cs_id} for r in conn.execute("SELECT id, description, uom, pieces_m FROM claim_stub_items WHERE cs_id = ? ORDER BY line_no", (cs_id,))]


def arrived(conn, user: User, cs_id: int, pieces: dict[int, str] | None = None) -> None:
    """The goods reached the warehouse. The warehouse records it and counts every line, because they received them. The supplier's 90 days don't start
    here: they run from the day the supplier received the check that took the stub off (paid_on). pieces is {claim stub line id: pieces counted}. Goods already at the warehouse from before counting was asked for are counted with count_arrived."""
    require(user, "warehouse.receive")
    with tx(conn):
        cs = _row(conn, cs_id)
        if cs["status"] != ISSUED:
            raise POError(f"{cs['cs_number']} isn't waiting to come back to the warehouse.")
        record_arrival(conn, user, "CS", cs["cs_number"], cs["po_id"], _lines(conn, cs_id), pieces or {})
        conn.execute("UPDATE claim_stubs SET status = ?, arrived_by = ?, arrived_at = ? WHERE id = ?", (AT_WAREHOUSE, user.id, timestamp(), cs_id))
        audit(conn, user, "CS_ARRIVED", "claim_stub", cs_id, po_id=cs["po_id"], summary=f"{cs['cs_number']} goods are at {warehouse_name(conn)}")


def count_arrived(conn, user: User, cs_id: int, pieces: dict[int, str]) -> None:
    """Counts goods that reached the warehouse before counting was asked for, so they can be collected."""
    require(user, "warehouse.receive")
    with tx(conn):
        cs = _row(conn, cs_id)
        lines = _lines(conn, cs_id)
        if cs["status"] != AT_WAREHOUSE or counted(conn, "CS", [line["id"] for line in lines]):
            raise POError(f"{cs['cs_number']} has nothing waiting to be counted.")
        record_arrival(conn, user, "CS", cs["cs_number"], cs["po_id"], lines, pieces)


def is_counted(conn, cs_id: int) -> bool:
    return counted(conn, "CS", [line["id"] for line in _lines(conn, cs_id)])


def collected(conn, user: User, cs_id: int, collected_by: str) -> None:
    require(user, "goods.release")
    if not collected_by.strip():
        raise POError("Write who collected the goods for the supplier.")
    with tx(conn):
        cs = _row(conn, cs_id)
        if cs["status"] != AT_WAREHOUSE:
            raise POError(f"{cs['cs_number']} can only be collected once its goods are at {warehouse_name(conn)}.")
        if not is_counted(conn, cs_id):
            raise POError(f"{cs['cs_number']}: the warehouse has to count the goods before the supplier collects them.")
        # Damaged goods are collected at the warehouse only once the supplier has the check that took the claim stub off.
        if not paid_on(conn, cs["po_id"], cs_id):
            raise POError(f"{cs['cs_number']}: the supplier collects these once they've received the check that takes this claim stub off. That hasn't happened yet.")
        conn.execute("UPDATE claim_stubs SET status = ?, closed_by = ?, closed_at = ?, collected_by_name = ? WHERE id = ?", (COLLECTED, user.id, timestamp(), collected_by.strip(), cs_id))
        audit(conn, user, "CS_COLLECTED", "claim_stub", cs_id, po_id=cs["po_id"], summary=f"{cs['cs_number']} collected by {collected_by.strip()}")


def days_left(arrived_at: str | None) -> int | None:
    """Days the supplier still has, counted from the day given: for a claim stub that's paid_on, the day they received the check that took it off.
    Below zero means the 90 days have passed."""
    if not arrived_at:
        return None
    return FORFEIT_AFTER_DAYS - (today() - date.fromisoformat(arrived_at[:10])).days


def forfeit(conn, user: User, cs_id: int) -> None:
    """The supplier didn't collect in time, so the goods are forfeited and can be sold. Nobody can do this before the 90 days are over,
    counted from paid_on: the day the supplier received the check that took the stub off, not the day the goods arrived."""
    require(user, "dm.issue")
    with tx(conn):
        cs = _row(conn, cs_id)
        if cs["status"] != AT_WAREHOUSE:
            raise POError(f"{cs['cs_number']} can only be forfeited while its goods are waiting at {warehouse_name(conn)}.")
        if not is_counted(conn, cs_id):
            raise POError(f"{cs['cs_number']}: the warehouse has to count the goods before they're forfeited.")
        # The supplier's 90 days run from when they received the check, since that's when they may collect the goods.
        paid = paid_on(conn, cs["po_id"], cs_id)
        if not paid:
            raise POError(f"{cs['cs_number']} can't be forfeited: the supplier hasn't received the check that takes it off, so their {FORFEIT_AFTER_DAYS} days haven't started.")
        left = days_left(paid)
        if left >= 0:
            raise POError(f"{cs['cs_number']} can't be forfeited yet. The supplier has until {left + 1} more days have passed.")
        conn.execute("UPDATE claim_stubs SET status = ?, closed_by = ?, closed_at = ? WHERE id = ?", (FORFEITED, user.id, timestamp(), cs_id))
        audit(conn, user, "CS_FORFEIT", "claim_stub", cs_id, po_id=cs["po_id"], summary=f"{cs['cs_number']} not collected after {FORFEIT_AFTER_DAYS} days. Goods forfeited, for sale")


def cancel(conn, user: User, cs_id: int, reason: str) -> None:
    """Management only, because it puts money back on the supplier's side. Never once the order's check is recorded."""
    require(user, "dm.cancel")
    if not reason.strip():
        raise POError("Write the reason for cancelling this claim stub.")
    with tx(conn):
        cs = _row(conn, cs_id)
        if cs["status"] not in (ISSUED, AT_WAREHOUSE):
            raise POError(f"{cs['cs_number']} is finished, so it can't be cancelled.")
        refuse_if_paid(conn, _of_id(conn, cs["po_id"]), "changed by cancelling " + cs["cs_number"])
        conn.execute("UPDATE claim_stubs SET status = ?, closed_by = ?, closed_at = ?, cancel_reason = ? WHERE id = ?", (CANCELLED, user.id, timestamp(), reason.strip(), cs_id))
        audit(conn, user, "CS_CANCEL", "claim_stub", cs_id, po_id=cs["po_id"], summary=f"{cs['cs_number']} cancelled: {reason.strip()}")


def save_external_number(conn, user: User, cs_id: int, number: str) -> None:
    require(user, "dm.issue")
    with tx(conn):
        cs = _row(conn, cs_id)
        conn.execute("UPDATE claim_stubs SET legacy_cs_number = ? WHERE id = ?", (number.strip(), cs_id))
        audit(conn, user, "CS_EXTERNAL_NUMBER", "claim_stub", cs_id, po_id=cs["po_id"], summary=f"{cs['cs_number']} external number {number.strip() or 'cleared'}")


_SELECT = ("SELECT c.id, c.cs_number, c.cs_date, c.po_id, c.status, c.total_c, c.remarks, c.approved_at, c.arrived_at, c.closed_at, c.collected_by_name, c.cancel_reason, c.legacy_cs_number,"
           " p.po_number, p.branch_code, p.branch_name, f.of_number, s.name AS supplier, u.full_name AS approved_name, x.full_name AS closed_name,"
           " (SELECT SUM(i.pieces_m) FROM claim_stub_items i WHERE i.cs_id = c.id) AS pieces_m,"
           " (SELECT COUNT(*) FROM audit_logs a WHERE a.action = 'CS_PRINT' AND a.entity_id = c.id) AS prints"
           " FROM claim_stubs c JOIN purchase_orders p ON p.id = c.po_id JOIN suppliers s ON s.id = c.supplier_id LEFT JOIN order_forms f ON f.id = p.of_id"
           " JOIN users u ON u.id = c.approved_by LEFT JOIN users x ON x.id = c.closed_by")


def paid_on(conn, po_id: int, cs_id: int | None = None) -> str | None:
    """The day the supplier received the check that took this PO's claim stubs off: when they claimed it, or the day a settlement by credit was recorded.
    None until then. The goods can only be collected from that day, and the supplier's 90 days start then. Damage found after the order was paid
    comes off the supplier's next check instead, so for that stub it's the first check after it was found."""
    late = conn.execute("SELECT * FROM late_damage WHERE cs_id = ?", (cs_id,)).fetchone() if cs_id else None
    if late is not None and late["paid_before"] is not None:
        nxt = conn.execute("SELECT c.source, c.amount_c, c.check_date, k.claimed_on FROM payment_checks c JOIN claim_stubs s ON s.supplier_id = c.supplier_id"
                           " LEFT JOIN check_claims k ON k.check_id = c.id WHERE s.id = ? AND c.status = 'ACTIVE' AND c.id <> ? AND c.recorded_at >= ? ORDER BY c.recorded_at, c.id LIMIT 1",
                           (cs_id, late["paid_before"], late["found_at"])).fetchone()
        if nxt is None:
            return None
        return nxt["claimed_on"] or (nxt["check_date"] if nxt["source"] == "PROGRAM" and nxt["amount_c"] == 0 else None)
    row = conn.execute(
        "SELECT c.source, c.amount_c, c.check_number, c.check_date, k.claimed_on FROM payment_checks c JOIN payment_check_ofs o ON o.check_id = c.id"
        " JOIN purchase_orders p ON p.of_id = o.of_id LEFT JOIN check_claims k ON k.check_id = c.id WHERE p.id = ? AND c.status = 'ACTIVE'", (po_id,)).fetchone()
    if row is None:
        return None
    if row["claimed_on"]:
        return row["claimed_on"]
    return row["check_date"] if row["source"] == "PROGRAM" and row["amount_c"] == 0 else None


def _paid_on_many(conn, rows) -> dict[int, str | None]:
    """paid_on() for every stub on a list that's at the warehouse, in two statements however long the list is (AUD-149)."""
    want = [r for r in rows if r["status"] == AT_WAREHOUSE]
    if not want:
        return {}
    ids, pos = json.dumps([r["id"] for r in want]), json.dumps(sorted({r["po_id"] for r in want}))
    late = {r["cs_id"]: r for r in conn.execute(
        "SELECT l.cs_id, c.id AS check_id, c.source, c.amount_c, c.check_date, k.claimed_on FROM late_damage l JOIN claim_stubs s ON s.id = l.cs_id"
        " LEFT JOIN payment_checks c ON c.id = (SELECT n.id FROM payment_checks n WHERE n.supplier_id = s.supplier_id AND n.status = 'ACTIVE' AND n.id <> l.paid_before"
        "   AND n.recorded_at >= l.found_at ORDER BY n.recorded_at, n.id LIMIT 1)"
        " LEFT JOIN check_claims k ON k.check_id = c.id WHERE l.paid_before IS NOT NULL AND l.cs_id IN (SELECT value FROM json_each(?))", (ids,))}
    by_po: dict = {}
    for r in conn.execute(
            "SELECT p.id AS po_id, c.source, c.amount_c, c.check_date, k.claimed_on FROM payment_checks c JOIN payment_check_ofs o ON o.check_id = c.id"
            " JOIN purchase_orders p ON p.of_id = o.of_id LEFT JOIN check_claims k ON k.check_id = c.id WHERE p.id IN (SELECT value FROM json_each(?)) AND c.status = 'ACTIVE'", (pos,)):
        by_po.setdefault(r["po_id"], r)
    out = {}
    for r in want:
        if r["id"] in late:
            check = late[r["id"]] if late[r["id"]]["check_id"] is not None else None
        else:
            check = by_po.get(r["po_id"])
        out[r["id"]] = None if check is None else check["claimed_on"] or (check["check_date"] if check["source"] == "PROGRAM" and check["amount_c"] == 0 else None)
    return out


def _with_days(row, conn=None, paid: dict | None = None) -> dict:
    d = dict(row)
    if paid is not None:
        d["paid_on"] = paid.get(d["id"]) if d["status"] == AT_WAREHOUSE else None
    else:
        d["paid_on"] = paid_on(conn, d["po_id"], d["id"]) if conn is not None and d["status"] == AT_WAREHOUSE else None
    d["days_left"] = days_left(d["paid_on"]) if d["paid_on"] else None
    return d


def get(conn, user: User, cs_id: int) -> dict:
    require(user, "receiving.review")
    row = conn.execute(_SELECT + " WHERE c.id = ?", (cs_id,)).fetchone()
    if row is None:
        raise POError("That claim stub no longer exists.")
    return _with_days(row, conn)


def search(conn, user: User, text: str = "", status: str | None = None, limit: int = 2000) -> list[dict]:
    require(user, "receiving.review")
    where, args = [], []
    if status:
        where.append("c.status = ?")
        args.append(status)
    for word in text.split():
        where.append("(c.cs_number LIKE ? OR p.po_number LIKE ? OR f.of_number LIKE ? OR s.name LIKE ? OR s.code LIKE ? OR c.legacy_cs_number LIKE ?)")
        args += [f"%{word}%"] * 6
    rows = conn.execute(_SELECT + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY c.id DESC LIMIT ?", [*args, limit]).fetchall()
    paid = _paid_on_many(conn, rows)
    return [_with_days(r, paid=paid) for r in rows]


def items(conn, user: User, cs_id: int) -> list[dict]:
    require(user, "receiving.review")
    return [dict(r) for r in conn.execute("SELECT * FROM claim_stub_items WHERE cs_id = ? ORDER BY line_no", (cs_id,))]


def for_sale(conn, user: User) -> list[dict]:
    """Forfeited goods, ready to be sold."""
    require(user, "receiving.review")
    return [dict(r) for r in conn.execute(
        "SELECT i.item_code, i.description, i.size, i.color, i.pieces_m, i.net_unit_c, i.pieces_per_unit, c.cs_number, c.closed_at, s.name AS supplier FROM claim_stub_items i JOIN claim_stubs c ON c.id = i.cs_id"
        " JOIN suppliers s ON s.id = c.supplier_id WHERE c.status = ? ORDER BY c.closed_at DESC, i.line_no", (FORFEITED,))]


def document(conn, user: User, cs_id: int) -> bytes:
    """The exact PDF kept on its first print. Any later print is a copy marked as one, and every print is recorded."""
    from . import pdf
    require(user, "receiving.review")
    cs = _row(conn, cs_id)
    # Read inside the write lock, so two PCs printing at once can't both get the unmarked original.
    with tx(conn):
        earlier = conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action = 'CS_PRINT' AND entity_id = ?", (cs_id,)).fetchone()[0]
        audit(conn, user, "CS_PRINT", "claim_stub", cs_id, po_id=cs["po_id"], summary=f"{cs['cs_number']} printed")
    return pdf.stamp_copy(cs["pdf"], earlier + 1, user.display) if earlier else cs["pdf"]


def as_on_paper(conn, cs_id: int) -> dict[int, str]:
    """Each line's pieces as the stub says, for when the warehouse counted exactly that."""
    return {line["id"]: fmt_qty(line["pieces_m"]) for line in _lines(conn, cs_id)}


def late_numbers(conn, user: User) -> list[str]:
    """OF and PO numbers whose deliveries the branches agreed on, newest first, so typing part of one offers the rest."""
    require(user, "dm.issue")
    rows = conn.execute(
        "SELECT DISTINCT f.of_number, p.po_number, p.id FROM purchase_orders p LEFT JOIN order_forms f ON f.id = p.of_id WHERE p.status <> 'CANCELLED'"
        " AND EXISTS (SELECT 1 FROM branch_counts b WHERE b.po_id = p.id AND b.status = 'APPROVED') ORDER BY p.id DESC").fetchall()
    # dict.fromkeys keeps the first of each, in order, without searching the list for every number.
    return list(dict.fromkeys(number for r in rows for number in (r["of_number"], r["po_number"]) if number))


def passes_for_branch(conn, user: User) -> list[dict]:
    """The claim stubs whose damaged goods are still at this receiver's branch, waiting to go to the warehouse, with whether their pass was printed."""
    require(user, "receiving.count")
    if user.branch_code is None:
        return []
    rows = [dict(r) for r in conn.execute(
        "SELECT c.id, c.cs_number, c.cs_date, c.status, p.po_number, s.name AS supplier, (SELECT SUM(i.pieces_m) FROM claim_stub_items i WHERE i.cs_id = c.id) AS pieces_m,"
        " (SELECT MAX(a.occurred_at) FROM audit_logs a WHERE a.action = 'CS_PASS_PRINT' AND a.entity_id = c.id) AS printed_at"
        " FROM claim_stubs c JOIN purchase_orders p ON p.id = c.po_id JOIN suppliers s ON s.id = c.supplier_id WHERE p.branch_code = ? AND c.status = ? ORDER BY c.id",
        (user.branch_code, ISSUED))]
    for r in rows:
        r["lines"] = [dict(x) for x in conn.execute("SELECT item_code, description, size, color, pieces_m FROM claim_stub_items WHERE cs_id = ? ORDER BY line_no", (r["id"],))]
    return rows


def print_pass(conn, user: User, cs_id: int, deliver=None) -> bytes:
    """The branch prints the pass its damaged goods leave on: an original with the goods, a duplicate kept. A later print is marked as a copy. Every print is recorded.
    With deliver, the pass is handed to it (for example to open it for printing) before the print is recorded, and is only recorded if that worked,
    so a pass whose file never opened isn't shown as printed, and the next one is still the original."""
    from . import pdf
    require(user, "receiving.count")
    cs = _row(conn, cs_id)
    branch = conn.execute("SELECT branch_code FROM purchase_orders WHERE id = ?", (cs["po_id"],)).fetchone()[0]
    if user.branch_code != branch:
        raise POError("That claim stub isn't for your branch.")
    if cs["status"] != ISSUED:
        raise POError(f"{cs['cs_number']}'s goods have already left for the warehouse.")
    # Read inside the write lock, so two receivers printing at once can't both get the unmarked original.
    with tx(conn):
        earlier = conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action = 'CS_PASS_PRINT' AND entity_id = ?", (cs_id,)).fetchone()[0]
        data = pdf.render_claim_stub_pass(conn, cs_id, user.display)
        if earlier:
            data = pdf.stamp_copy(data, earlier + 1, user.display)
        if deliver is not None:
            deliver(data)  # if this fails, nothing is recorded
        audit(conn, user, "CS_PASS_PRINT", "claim_stub", cs_id, po_id=cs["po_id"], summary=f"Damaged goods pass for {cs['cs_number']} printed at the branch")
    return data



# ------------------------------------------------------------------ damage reported by a branch

PENDING, APPROVED, REJECTED = "PENDING", "APPROVED", "REJECTED"
REQUEST_LABELS = {PENDING: "Waiting for approval", APPROVED: "Approved: print the pass", REJECTED: "Rejected"}


def _receiver_branch(user: User) -> int:
    require(user, "receiving.count")
    if user.branch_code is None:
        raise POError("Your login has no branch yet. Ask the home office to set it.")
    return user.branch_code


def report_numbers(conn, user: User) -> list[str]:
    """The receiver's own branch's PO and OF numbers whose deliveries were agreed, newest first, so typing part of one offers the rest."""
    branch = _receiver_branch(user)
    rows = conn.execute("SELECT f.of_number, p.po_number FROM purchase_orders p LEFT JOIN order_forms f ON f.id = p.of_id WHERE p.branch_code = ? AND p.status <> 'CANCELLED'"
                        " AND EXISTS (SELECT 1 FROM branch_counts b WHERE b.po_id = p.id AND b.status = 'APPROVED') ORDER BY p.id DESC", (branch,)).fetchall()
    return list(dict.fromkeys(number for r in rows for number in (r["po_number"], r["of_number"]) if number))


def report_lines(conn, user: User, number: str) -> list[dict]:
    """The products on the receiver's branch PO that damage can be reported on, with pieces only: no costs or prices reach a receiver."""
    branch = _receiver_branch(user)
    keep = ("po_id", "po_number", "of_number", "branch_name", "po_item_id", "item_code", "description", "size", "color", "uom", "received_m", "room_m")
    return [{k: line[k] for k in keep} for line in late_lines(conn, user, number, branch_code=branch, check=False)]


def send_report(conn, user: User, number: str, pieces: dict[int, str], reason: str) -> list[int]:
    """One receiver reports damage found after the count, for Accounting or management to approve. Returns the reports made, one per PO."""
    branch = _receiver_branch(user)
    if not reason.strip():
        raise POError("Write how the damage was found, for example: found torn when unpacked.")
    made = []
    with tx(conn):
        lines = late_lines(conn, user, number, branch_code=branch, check=False)
        by_po: dict[int, list] = {}
        for line in lines:
            text = str(pieces.get(line["po_item_id"], "")).strip()
            wanted = parse_qty(text, "PC") if text else 0
            if wanted > line["room_m"]:
                raise POError(f"{line['description']}: only {fmt_qty(line['room_m'])} pieces can be reported.")
            if wanted:
                by_po.setdefault(line["po_id"], []).append((line, wanted))
        if not by_po:
            raise POError("Type the damaged pieces for at least one product.")
        for po_id, chosen in by_po.items():
            request_id = conn.execute("INSERT INTO claim_requests(po_id, reason, status, requested_by, requested_at) VALUES (?,?,?,?,?)",
                                      (po_id, reason.strip(), PENDING, user.id, timestamp())).lastrowid
            _add_report_lines(conn, request_id, [(line["po_item_id"], wanted) for line, wanted in chosen])
            audit(conn, user, "CS_REQUEST", "claim_request", request_id, po_id=po_id,
                  summary=f"Damage reported by the branch on {chosen[0][0]['po_number']}: {fmt_qty(sum(w for _, w in chosen))} pieces, {reason.strip()}")
            made.append(request_id)
    return made


def _add_report_lines(conn, request_id: int, lines: list[tuple[int, int]]) -> None:
    """The only way the program puts lines on a damage report: once, as the branch sends it. A report that was approved or rejected,
    or that already has its lines, takes no more, so what the branch sent stays exactly as sent (AUD-130)."""
    row = conn.execute("SELECT status FROM claim_requests WHERE id = ?", (request_id,)).fetchone()
    if row is None:
        raise POError("That damage report no longer exists.")
    if row["status"] != PENDING:
        raise POError("That damage report was already decided, so nothing can be added to it.")
    if conn.execute("SELECT 1 FROM claim_request_items WHERE request_id = ?", (request_id,)).fetchone():
        raise POError("That damage report was already sent, so nothing can be added to it. Send a new report instead.")
    conn.executemany("INSERT INTO claim_request_items(request_id, po_item_id, pieces_m) VALUES (?,?,?)", [(request_id, item, pieces) for item, pieces in lines])


def reports(conn, user: User, status: str | None = None) -> list[dict]:
    """Damage reports, newest first. A receiver sees only their own branch's, and only pieces. The office sees every branch, with what each would come to."""
    where, args = [], []
    if user.can("receiving.count") and not user.can("receiving.review"):
        where.append("p.branch_code = ?")
        args.append(_receiver_branch(user))
    if status:
        where.append("r.status = ?")
        args.append(status)
    rows = [dict(r) for r in conn.execute(
        "SELECT r.*, p.po_number, p.branch_code, p.branch_name, f.of_number, s.name AS supplier, u.full_name AS requested_name, d.full_name AS decided_name, c.cs_number"
        " FROM claim_requests r JOIN purchase_orders p ON p.id = r.po_id JOIN suppliers s ON s.id = p.supplier_id LEFT JOIN order_forms f ON f.id = p.of_id"
        " JOIN users u ON u.id = r.requested_by LEFT JOIN users d ON d.id = r.decided_by LEFT JOIN claim_stubs c ON c.id = r.cs_id"
        + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY r.id DESC", args)]
    money = user.can("receiving.review")
    for r in rows:
        r["lines"] = [dict(x) for x in conn.execute(
            "SELECT i.po_item_id, i.pieces_m, o.item_code, o.description, o.size, o.color, o.net_unit_c, o.pieces_per_unit FROM claim_request_items i"
            " JOIN purchase_order_items o ON o.id = i.po_item_id WHERE i.request_id = ? ORDER BY o.line_no", (r["id"],))]
        r["pieces_m"] = sum(x["pieces_m"] for x in r["lines"])
        if money:
            share = discount_share(conn, r["po_id"])
            r["amount_c"] = sum(amount_c(x["pieces_m"], x["net_unit_c"], x["pieces_per_unit"], share) for x in r["lines"])
        else:
            for x in r["lines"]:
                x.pop("net_unit_c")
    return rows


def _report(conn, request_id: int):
    row = conn.execute("SELECT * FROM claim_requests WHERE id = ?", (request_id,)).fetchone()
    if row is None:
        raise POError("That damage report no longer exists.")
    if row["status"] != PENDING:
        raise POError("That damage report was already decided.")
    return row


def approve_report(conn, user: User, request_id: int) -> dict:
    """Accounting or management agrees: the claim stub is made, exactly as the branch reported it, and its pass is ready for the branch to print.
    If the order is already paid, what it takes off comes off the supplier's next check."""
    require(user, "cs.approve_request")
    with tx(conn):
        req = _report(conn, request_id)
        if conn.execute("SELECT requested_by FROM claim_requests WHERE id = ?", (request_id,)).fetchone()[0] == user.id:
            raise POError("Someone else has to approve damage you reported yourself.")
        items = {r["po_item_id"]: r["pieces_m"] for r in conn.execute("SELECT po_item_id, pieces_m FROM claim_request_items WHERE request_id = ?", (request_id,))}
        po = conn.execute("SELECT * FROM purchase_orders WHERE id = ?", (req["po_id"],)).fetchone()
        # This report's own pieces are left out of what's waiting, so the room is what it would take.
        try:
            lines = {line["po_item_id"]: line for line in late_lines(conn, user, po["po_number"], branch_code=po["branch_code"], check=False, leaving_out=request_id)}
        except POError:
            lines = {}
        chosen = []
        for item_id, wanted in items.items():
            line = lines.get(item_id)
            if line is None or wanted > line["room_m"]:
                raise POError("Those pieces have been charged another way since the branch reported them, so this report can't be approved. Reject it, and the branch can send a new one.")
            chosen.append((line, wanted, amount_c(wanted, line["cost_c"], line["pieces_per_unit"], line["share"])))
        cs_id = _create(conn, user, po, chosen, f"Damage found after delivery: {req['reason']}", req["reason"])
        conn.execute("UPDATE claim_requests SET status = ?, decided_by = ?, decided_at = ?, cs_id = ? WHERE id = ?", (APPROVED, user.id, timestamp(), cs_id, request_id))
        audit(conn, user, "CS_REQUEST_APPROVE", "claim_stub", cs_id, po_id=po["id"], summary=f"Branch damage report approved as claim stub {get(conn, user, cs_id)['cs_number']}")
    return get(conn, user, cs_id)


def reject_report(conn, user: User, request_id: int, reason: str) -> None:
    require(user, "cs.approve_request")
    if not reason.strip():
        raise POError("Write why this damage report is rejected. The branch will see it.")
    with tx(conn):
        req = _report(conn, request_id)
        conn.execute("UPDATE claim_requests SET status = ?, decided_by = ?, decided_at = ?, decision_reason = ? WHERE id = ?", (REJECTED, user.id, timestamp(), reason.strip(), request_id))
        audit(conn, user, "CS_REQUEST_REJECT", "purchase_order", req["po_id"], po_id=req["po_id"], summary=f"Branch damage report rejected: {reason.strip()}")


def reports_waiting(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM claim_requests WHERE status = 'PENDING'").fetchone()[0]
