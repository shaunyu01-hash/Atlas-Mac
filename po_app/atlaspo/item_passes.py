"""Item passes: excess pieces leaving a branch for the main warehouse.

Once two receivers agree on a delivery with pieces beyond the order, the branch prints an item pass, an original and a duplicate, so the goods can be signed out.
It prints once, because a pass that can be printed again could walk more goods out. If one is lost or spoiled, the home office cancels it with a reason, and the
branch prints a new one with its own number. The home office only reads and follows it. The supplier has 90 days to pick the goods up, counted from the day
they reach the main warehouse (not from printing or payment). After that they're forfeited and can be sold. Excess is never paid for, so a pass carries no money, and printing it settles the excess on the PO.
"""
from __future__ import annotations

import hashlib

from . import receiving
from .auth import User, require
from .claim_stubs import FORFEIT_AFTER_DAYS, counted, days_left, record_arrival, warehouse_name
from .db import POError, audit, timestamp, today, tx
from .money import fmt_qty

PRINTED, AT_WAREHOUSE, COLLECTED, FORFEITED, CANCELLED = "PRINTED", "AT_WAREHOUSE", "COLLECTED", "FORFEITED", "CANCELLED"


def status_labels(conn) -> dict:
    return {PRINTED: f"Coming back to {warehouse_name(conn)}", AT_WAREHOUSE: f"At {warehouse_name(conn)}", COLLECTED: "Collected by supplier", FORFEITED: "Forfeited: for sale", CANCELLED: "Cancelled"}


def _left(conn, po_id: int) -> list[dict]:
    return [r for r in receiving.excess_lines(conn, po_id) if r["left_m"]]


# What a pass says about each line. A receiver never gets what was ordered, so the count stays blind.
PASS_FIELDS = ("id", "item_code", "description", "size", "color", "uom", "not_on_po", "left_m")


def to_print(conn, user: User) -> list[dict]:
    """POs at this person's branch with agreed excess and no pass for it yet. Each line carries only what goes on the pass."""
    require(user, "receiving.count")
    where, args = "", []
    if user.branch_code is not None:
        where, args = " AND p.branch_code = ?", [user.branch_code]
    elif "ADMIN" not in user.roles:
        return []
    out = []
    for po in conn.execute("SELECT p.id, p.po_number, p.branch_code, p.branch_name, f.of_number, s.name AS supplier FROM purchase_orders p JOIN suppliers s ON s.id = p.supplier_id LEFT JOIN order_forms f ON f.id = p.of_id"
                           " WHERE p.status <> 'CANCELLED' AND EXISTS (SELECT 1 FROM branch_counts b WHERE b.po_id = p.id)" + where + " ORDER BY p.id", args).fetchall():
        if receiving._reachable(user, po["branch_code"]) and not receiving.progress(conn, po["id"]):
            left = _left(conn, po["id"])
            if left:
                out.append({**dict(po), "pieces_m": sum(r["left_m"] for r in left), "lines": [{k: r[k] for k in PASS_FIELDS} for r in left]})
    return out


def issue(conn, user: User, po_id: int) -> dict:
    """Gives every excess piece left on the PO a pass with a permanent number. Only someone at the branch can, and only once for those pieces."""
    from . import pdf
    require(user, "receiving.count")
    with tx(conn):
        po = conn.execute("SELECT * FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
        if po is None:
            raise POError("That PO no longer exists.")
        if not receiving._reachable(user, po["branch_code"]):
            raise POError("That PO belongs to another branch.")
        if receiving.progress(conn, po_id):
            raise POError("This PO is still being counted. An item pass has to wait until the counts agree.")
        left = _left(conn, po_id)
        if not left:
            raise POError("There's no excess left on this PO. Its item pass has already been printed.")
        when = today()
        conn.execute("INSERT INTO ip_number_sequences(year, last_number) VALUES (?, 1) ON CONFLICT(year) DO UPDATE SET last_number = last_number + 1", (when.year,))
        seq = conn.execute("SELECT last_number FROM ip_number_sequences WHERE year = ?", (when.year,)).fetchone()[0]
        number = f"{when.year}-IP-{seq:05d}"
        ip_id = conn.execute("INSERT INTO item_passes(ip_number, ip_year, ip_seq, ip_date, po_id, supplier_id, status, printed_by, printed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                             (number, when.year, seq, when.isoformat(), po_id, po["supplier_id"], PRINTED, user.id, timestamp())).lastrowid
        conn.executemany("INSERT INTO item_pass_items(ip_id, line_no, po_item_id, item_code, description, size, color, uom, pieces_m) VALUES (?,?,?,?,?,?,?,?,?)",
                         [(ip_id, n, r["id"], r["item_code"], r["description"], r["size"], r["color"], r["uom"], r["left_m"]) for n, r in enumerate(left, 1)])
        document_bytes = pdf.render_item_pass(conn, ip_id)
        conn.execute("UPDATE item_passes SET pdf = ?, sha256 = ? WHERE id = ?", (document_bytes, hashlib.sha256(document_bytes).hexdigest(), ip_id))
        total = sum(r["left_m"] for r in left)
        audit(conn, user, "IP_PRINT", "item_pass", ip_id, po_id=po_id, summary=f"{number} printed on {po['po_number']} for {fmt_qty(total)} excess pieces")
    return {"id": ip_id, "ip_number": number, "pdf": document_bytes}


def _row(conn, ip_id: int):
    row = conn.execute("SELECT * FROM item_passes WHERE id = ?", (ip_id,)).fetchone()
    if row is None:
        raise POError("That item pass no longer exists.")
    return row


def _lines(conn, ip_id: int) -> list[dict]:
    return [dict(r) | {"parent_id": ip_id} for r in conn.execute("SELECT id, description, uom, pieces_m FROM item_pass_items WHERE ip_id = ? ORDER BY line_no", (ip_id,))]


def is_counted(conn, ip_id: int) -> bool:
    return counted(conn, "IP", [line["id"] for line in _lines(conn, ip_id)])


def count_arrived(conn, user: User, ip_id: int, pieces: dict[int, str]) -> None:
    """Counts goods that reached the warehouse before counting was asked for, so they can be collected."""
    require(user, "warehouse.receive")
    with tx(conn):
        ip = _row(conn, ip_id)
        if ip["status"] != AT_WAREHOUSE or is_counted(conn, ip_id):
            raise POError(f"{ip['ip_number']} has nothing waiting to be counted.")
        record_arrival(conn, user, "IP", ip["ip_number"], ip["po_id"], _lines(conn, ip_id), pieces)


def arrived(conn, user: User, ip_id: int, pieces: dict[int, str] | None = None) -> None:
    """The goods reached the warehouse, and the supplier's 90 days count from this day. The warehouse records it and counts every line, because they received them.
    pieces is {item pass line id: pieces counted}."""
    require(user, "warehouse.receive")
    with tx(conn):
        ip = _row(conn, ip_id)
        if ip["status"] != PRINTED:
            raise POError(f"{ip['ip_number']} isn't waiting to come back to the warehouse.")
        record_arrival(conn, user, "IP", ip["ip_number"], ip["po_id"], _lines(conn, ip_id), pieces or {})
        conn.execute("UPDATE item_passes SET status = ?, arrived_by = ?, arrived_at = ? WHERE id = ?", (AT_WAREHOUSE, user.id, timestamp(), ip_id))
        audit(conn, user, "IP_ARRIVED", "item_pass", ip_id, po_id=ip["po_id"], summary=f"{ip['ip_number']} goods are at {warehouse_name(conn)}")


def collected(conn, user: User, ip_id: int, collected_by: str) -> None:
    require(user, "goods.release")
    if not collected_by.strip():
        raise POError("Write who collected the goods for the supplier.")
    with tx(conn):
        ip = _row(conn, ip_id)
        if ip["status"] != AT_WAREHOUSE:
            raise POError(f"{ip['ip_number']} can only be collected once its goods are at {warehouse_name(conn)}.")
        if not is_counted(conn, ip_id):
            raise POError(f"{ip['ip_number']}: the warehouse has to count the goods before the supplier collects them.")
        conn.execute("UPDATE item_passes SET status = ?, closed_by = ?, closed_at = ?, collected_by_name = ? WHERE id = ?", (COLLECTED, user.id, timestamp(), collected_by.strip(), ip_id))
        audit(conn, user, "IP_COLLECTED", "item_pass", ip_id, po_id=ip["po_id"], summary=f"{ip['ip_number']} collected by {collected_by.strip()}")


def forfeit(conn, user: User, ip_id: int) -> None:
    """The supplier didn't pick the goods up in time, so they can be sold. Nobody can do this before 90 days have passed since the goods reached the warehouse."""
    require(user, "dm.issue")
    with tx(conn):
        ip = _row(conn, ip_id)
        if ip["status"] != AT_WAREHOUSE:
            raise POError(f"{ip['ip_number']} can only be forfeited while its goods are waiting at {warehouse_name(conn)}.")
        if not is_counted(conn, ip_id):
            raise POError(f"{ip['ip_number']}: the warehouse has to count the goods before they're forfeited.")
        left = days_left(ip["arrived_at"])
        if left >= 0:
            raise POError(f"{ip['ip_number']} can't be forfeited yet. The supplier has until {left + 1} more days have passed.")
        conn.execute("UPDATE item_passes SET status = ?, closed_by = ?, closed_at = ? WHERE id = ?", (FORFEITED, user.id, timestamp(), ip_id))
        audit(conn, user, "IP_FORFEIT", "item_pass", ip_id, po_id=ip["po_id"], summary=f"{ip['ip_number']} not collected after {FORFEIT_AFTER_DAYS} days. Goods forfeited, for sale")


def cancel(conn, user: User, ip_id: int, reason: str) -> None:
    """A pass lost or spoiled before the goods reach the warehouse. Its pieces go back to needing a pass, so the branch prints a new one.
    Management only, because a second pass lets the same goods leave twice."""
    require(user, "dm.cancel")
    if not reason.strip():
        raise POError("Write the reason for cancelling this item pass.")
    with tx(conn):
        ip = _row(conn, ip_id)
        if ip["status"] != PRINTED:
            raise POError(f"{ip['ip_number']} has already reached the warehouse, so it can't be cancelled.")
        conn.execute("UPDATE item_passes SET status = ?, closed_by = ?, closed_at = ?, cancel_reason = ? WHERE id = ?", (CANCELLED, user.id, timestamp(), reason.strip(), ip_id))
        audit(conn, user, "IP_CANCEL", "item_pass", ip_id, po_id=ip["po_id"], summary=f"{ip['ip_number']} cancelled: {reason.strip()}")


def print_failed(conn, user: User, ip_id: int, problem: str) -> None:
    """The printer failed, so no paper exists. The person who just printed it cancels it straight away, with the printer's error as the reason, and can print a new one."""
    require(user, "receiving.count")
    with tx(conn):
        ip = _row(conn, ip_id)
        if ip["status"] != PRINTED or ip["printed_by"] != user.id or ip["arrived_at"]:
            raise POError(f"{ip['ip_number']} can only be cancelled by the home office now.")
        reason = f"Didn't print: {problem.strip() or 'printer error'}"
        conn.execute("UPDATE item_passes SET status = ?, closed_by = ?, closed_at = ?, cancel_reason = ? WHERE id = ?", (CANCELLED, user.id, timestamp(), reason, ip_id))
        audit(conn, user, "IP_CANCEL", "item_pass", ip_id, po_id=ip["po_id"], summary=f"{ip['ip_number']} cancelled: {reason}")


_SELECT = ("SELECT i.id, i.ip_number, i.ip_date, i.po_id, i.status, i.printed_at, i.arrived_at, i.closed_at, i.collected_by_name, i.cancel_reason,"
           " p.po_number, p.branch_code, p.branch_name, f.of_number, s.name AS supplier, u.full_name AS printed_name, x.full_name AS closed_name,"
           " (SELECT SUM(l.pieces_m) FROM item_pass_items l WHERE l.ip_id = i.id) AS pieces_m"
           " FROM item_passes i JOIN purchase_orders p ON p.id = i.po_id JOIN suppliers s ON s.id = i.supplier_id LEFT JOIN order_forms f ON f.id = p.of_id"
           " JOIN users u ON u.id = i.printed_by LEFT JOIN users x ON x.id = i.closed_by")


def _with_days(row) -> dict:
    d = dict(row)
    d["days_left"] = days_left(d["arrived_at"]) if d["status"] == AT_WAREHOUSE else None
    return d


def search(conn, user: User, text: str = "", status: str | None = None, limit: int = 2000) -> list[dict]:
    """For the home office, which sees every branch's passes."""
    require(user, "receiving.review")
    where, args = [], []
    if status:
        where.append("i.status = ?")
        args.append(status)
    for word in text.split():
        where.append("(i.ip_number LIKE ? OR p.po_number LIKE ? OR f.of_number LIKE ? OR s.name LIKE ? OR s.code LIKE ? OR p.branch_name LIKE ?)")
        args += [f"%{word}%"] * 6
    return [_with_days(r) for r in conn.execute(_SELECT + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY i.id DESC LIMIT ?", [*args, limit])]


def for_branch(conn, user: User) -> list[dict]:
    """The passes this person's branch has printed. There's no way to print them again from here."""
    require(user, "receiving.count")
    rows = conn.execute(_SELECT + " ORDER BY i.id DESC LIMIT 200").fetchall()
    return [_with_days(r) for r in rows if receiving._reachable(user, r["branch_code"])]


def items(conn, user: User, ip_id: int) -> list[dict]:
    """A pass's lines. Only the home office, or someone at the pass's own branch, may read them (AUD-117: a user is always needed,
    the same way as debit memo and claim stub lines)."""
    branch = conn.execute("SELECT p.branch_code FROM item_passes i JOIN purchase_orders p ON p.id = i.po_id WHERE i.id = ?", (ip_id,)).fetchone()
    if not (user.can("receiving.count") and branch is not None and receiving._reachable(user, branch["branch_code"])):
        require(user, "receiving.review")
    return [dict(r) for r in conn.execute("SELECT * FROM item_pass_items WHERE ip_id = ? ORDER BY line_no", (ip_id,))]


def as_on_paper(conn, ip_id: int) -> dict[int, str]:
    """Each line's pieces as the pass says, for when the warehouse counted exactly that."""
    return {line["id"]: fmt_qty(line["pieces_m"]) for line in _lines(conn, ip_id)}
