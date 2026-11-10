"""Blind receiving at the branches, counted twice.

A receiver presses a PO or types its number, sees only their branch's products and types what they counted. A second receiver then counts on their
own, without seeing the first count. If the two agree on every product, the delivery is accepted and reaches the PO. Products that don't agree come
back to be counted once more, by two people. If that round doesn't agree either, counting stops until the home office checks and allows one more round.

Each person's count is permanent. A receiver can only ever see their own numbers, and everything they can reach leaves out ordered quantities, costs
and prices. Only the home office sees differences. A receiver may redo their own count once while it waits for the second. A delivery that matched
is finished: only the home office can reopen it, once, with a reason, and never once it's paid or has a debit memo, claim stub or item pass.
It's then recounted the same way, and the accepted count stands until the recount agrees.

Anything beyond the order is excess delivery, which leaves the branch on an item pass: extra pieces of a PO product, or a product receivers add because it wasn't on the PO at all.
An added product shows on the other receiver's sheet like any other line, without the number, so it's counted twice too.
"""
from __future__ import annotations

import json

from .auth import User, require
from .db import POError, audit, timestamp, tx
from .masters import code_key
from .money import fmt_qty, parse_qty
from .orders import CANCELLED, CLOSED, FULL, ISSUED, PARTIAL, write_guard
from .warehouse import left_central

WAITING, APPROVED, MISMATCH, REPLACED = "WAITING", "APPROVED", "MISMATCH", "REPLACED"
COUNT_LABELS = {WAITING: "Waiting for second count", APPROVED: "Matched", MISMATCH: "Didn't match", REPLACED: "Replaced by a recount"}
COUNTABLE = (ISSUED, PARTIAL, FULL)
FIRST, SECOND, AGAIN = "First count", "Second count", "Count again: the counts didn't match"
RECOUNT, HELD = "Recount: the home office reopened it", "HELD"
CHECK_AFTER = 2  # rounds that didn't agree before counting stops for the home office
# No branch counts a billion pieces of one product. Anything bigger is a typing slip, and too big for the database to hold safely.
MAX_PIECES_M = 10 ** 9 * 1000
MAX_BOXES = 10 ** 9
OUT_OF_DATE = "This count sheet is out of date: a count was saved or the counting changed while you had it open. Open the PO again."
TO_CHECK, COMPLETED = "TO_CHECK", "COMPLETED"

# Only what two people agreed on, in a delivery's latest attempt to be fully agreed, counts as received.
_IN_FORCE = ("EXISTS (SELECT 1 FROM branch_counts b WHERE b.po_id = r.po_id AND b.delivery_no = r.delivery_no AND b.attempt_no = r.attempt_no AND b.status = 'APPROVED')")


def _reachable(user: User, branch_code: int) -> bool:
    return user.branch_code is None and "ADMIN" in user.roles or user.branch_code == branch_code


def _po(conn, user: User, po_id: int):
    po = conn.execute("SELECT * FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
    if po is None or not _reachable(user, po["branch_code"]):
        raise POError("That PO isn't for your branch.")
    return po


def _count(conn, user: User, count_id: int):
    count = conn.execute("SELECT * FROM branch_counts WHERE id = ?", (count_id,)).fetchone()
    if count is None:
        raise POError("That count no longer exists.")
    _po(conn, user, count["po_id"])
    return count


def _all_items(conn, po_id: int) -> list[int]:
    return [r["id"] for r in conn.execute("SELECT id FROM purchase_order_items WHERE po_id = ? ORDER BY line_no", (po_id,))]


def _delivery_lines(conn, po_id: int, delivery: int) -> list[int]:
    """A delivery's lines: the PO's products, and any added to it, as minus their ids."""
    return _all_items(conn, po_id) + [-r["id"] for r in conn.execute("SELECT id FROM branch_extra_items WHERE po_id = ? AND delivery_no = ? ORDER BY id", (po_id, delivery))]


def _line_rows(conn, ids: list[int]) -> list:
    """The same fields whichever kind of line it is, PO products first."""
    products, extras = [i for i in ids if i > 0], [-i for i in ids if i < 0]
    rows = conn.execute(f"SELECT id, line_no, item_code, description, size, color, uom FROM purchase_order_items WHERE id IN ({','.join('?' * len(products))}) ORDER BY line_no", products).fetchall()
    return rows + conn.execute(f"SELECT -id AS id, 100000 + id AS line_no, item_code, description, size, color, 'PC' AS uom FROM branch_extra_items WHERE id IN ({','.join('?' * len(extras))}) ORDER BY id", extras).fetchall()


def _add_extra(conn, user: User, po_id: int, delivery: int, extra: dict) -> int:
    """Records a product that arrived but wasn't on the PO. Returns its line id."""
    code, description = str(extra.get("item_code") or "").strip(), str(extra.get("description") or "").strip()
    if not description:
        raise POError("Type the name of the product that wasn't on the PO.")
    size, color = str(extra.get("size") or "").strip(), str(extra.get("color") or "").strip()
    # Compared in Python, like the product codes in masters: SQLite's upper() only folds A to Z, so 'ñandu-1' and 'ÑANDU-1' would look different.
    want = tuple(code_key(v) for v in (code, size, color))
    if code and any(tuple(code_key(r[k]) for k in ("item_code", "size", "color")) == want
                    for r in conn.execute("SELECT item_code, size, color FROM purchase_order_items WHERE po_id = ?", (po_id,))):
        raise POError(f"{code} is already on this PO. Count it on its own line.")
    want = tuple(code_key(v) for v in (code, description, size, color))
    if any(tuple(code_key(r[k]) for k in ("item_code", "description", "size", "color")) == want
           for r in conn.execute("SELECT item_code, description, size, color FROM branch_extra_items WHERE po_id = ? AND delivery_no = ?", (po_id, delivery))):
        raise POError(f"{description} is already listed for this delivery. Count it on its own line.")
    return -conn.execute("INSERT INTO branch_extra_items(po_id, delivery_no, item_code, description, size, color, added_by, added_at) VALUES (?,?,?,?,?,?,?,?)",
                         (po_id, delivery, code, description, size, color, user.id, timestamp())).lastrowid


def _state(conn, po_id: int) -> dict:
    """Where a PO's counting stands: who's needed next, and for which products."""
    waiting = conn.execute("SELECT * FROM branch_counts WHERE po_id = ? AND status = ? ORDER BY id DESC LIMIT 1", (po_id, WAITING)).fetchone()
    if waiting:
        lines = [r["po_item_id"] for r in conn.execute("SELECT po_item_id FROM branch_count_items WHERE count_id = ?", (waiting["id"],))]
        return {"need": SECOND, "delivery": waiting["delivery_no"], "attempt": waiting["attempt_no"], "round": waiting["round_no"], "lines": lines, "waiting": waiting}
    latest = conn.execute("SELECT * FROM branch_counts WHERE po_id = ? ORDER BY id DESC LIMIT 1", (po_id,)).fetchone()
    if latest and latest["status"] == MISMATCH:
        delivery, attempt, next_round = latest["delivery_no"], latest["attempt_no"], latest["round_no"] + 1
        # Two rounds, then one more for each time the home office allowed it.
        allowed = CHECK_AFTER + conn.execute("SELECT COUNT(*) FROM count_reopens WHERE po_id = ? AND delivery_no = ? AND attempt_no = ? AND kind = 'ROUND'", (po_id, delivery, attempt)).fetchone()[0]
        return {"need": AGAIN if next_round <= allowed else HELD, "delivery": delivery, "attempt": attempt, "round": next_round,
                "lines": _still_open(conn, po_id, delivery, attempt), "waiting": None}
    reopened = open_reopen(conn, po_id, "RECOUNT")
    if reopened:
        accepted = conn.execute("SELECT MAX(id) FROM branch_counts WHERE po_id = ? AND delivery_no = ? AND status = ?", (po_id, reopened["delivery_no"], APPROVED)).fetchone()[0]
        return {"need": RECOUNT, "delivery": reopened["delivery_no"], "attempt": reopened["attempt_no"], "round": 1,
                "lines": _delivery_lines(conn, po_id, reopened["delivery_no"]), "waiting": None, "recount_of": accepted, "reopen": reopened}
    delivery = conn.execute("SELECT COALESCE(MAX(delivery_no), 0) + 1 FROM branch_counts WHERE po_id = ?", (po_id,)).fetchone()[0]
    return {"need": FIRST if not latest else None, "delivery": delivery, "attempt": 1, "round": 1, "lines": _all_items(conn, po_id), "waiting": None}


def open_reopen(conn, po_id: int, kind: str | None = None):
    """A count the home office allowed and nobody has started yet."""
    return conn.execute("SELECT * FROM count_reopens WHERE po_id = ? AND used_by_count_id IS NULL" + (" AND kind = ?" if kind else "") + " ORDER BY id DESC LIMIT 1",
                        (po_id, kind) if kind else (po_id,)).fetchone()


def _still_open(conn, po_id: int, delivery: int, attempt: int) -> list[int]:
    """The products of an attempt that two people haven't agreed on yet."""
    return [r["po_item_id"] for r in conn.execute(
        "SELECT DISTINCT c.po_item_id FROM branch_count_items c JOIN branch_counts b ON b.id = c.count_id WHERE b.po_id = ? AND b.delivery_no = ? AND b.attempt_no = ?"
        " AND c.po_item_id NOT IN (SELECT po_item_id FROM branch_received WHERE po_id = ? AND delivery_no = ? AND attempt_no = ?) ORDER BY c.id", (po_id, delivery, attempt, po_id, delivery, attempt))]


def find_pos(conn, user: User, number: str) -> list[dict]:
    """The POs to count for an OF or PO number. Nothing here says whether the number exists for another branch."""
    require(user, "receiving.count")
    if user.branch_code is None and "ADMIN" not in user.roles:
        raise POError("Your login has no branch yet. Ask the home office to set it.")
    number = number.strip().upper()
    if not number:
        raise POError("Type the OF number or PO number.")
    rows = conn.execute(
        "SELECT p.id, p.po_number, p.status, p.branch_code, p.branch_name, s.name AS supplier, f.of_number FROM purchase_orders p"
        " JOIN suppliers s ON s.id = p.supplier_id LEFT JOIN order_forms f ON f.id = p.of_id"
        " WHERE upper(p.po_number) = ? OR upper(f.of_number) = ? ORDER BY p.branch_code", (number, number)).fetchall()
    rows = [dict(r) for r in rows if _reachable(user, r["branch_code"])]
    if not rows:
        raise POError(f"There's no PO with the number {number} for your branch. Check the number on your slip.")
    usable = [r for r in rows if r["status"] in COUNTABLE]
    if not usable:
        raise POError(f"{rows[0]['po_number']} can't be received: it isn't printed for delivery yet, or it's closed or cancelled. Ask the home office.")
    return usable


def to_count(conn, user: User) -> list[dict]:
    """What this person can count now, oldest first. An accepted PO leaves the list whether it was short or complete, so the list can't give a shortage away."""
    require(user, "receiving.count")
    where, args = "", []
    if user.branch_code is not None:
        where, args = " AND p.branch_code = ?", [user.branch_code]
    elif "ADMIN" not in user.roles:
        return []
    marks = ",".join("?" * len(COUNTABLE))
    rows = conn.execute(
        "SELECT p.id, p.po_number, p.branch_name, p.expected_delivery_date, s.name AS supplier, f.of_number FROM purchase_orders p JOIN suppliers s ON s.id = p.supplier_id"
        " LEFT JOIN order_forms f ON f.id = p.of_id WHERE p.status IN (" + marks + ")" + where + " ORDER BY p.id", [*COUNTABLE, *args]).fetchall()
    out = []
    for row in rows:
        state = _state(conn, row["id"])
        if state["need"] and state["need"] != HELD and not (state["waiting"] and state["waiting"]["counted_by"] == user.id) and not not_shipped_yet(conn, row["id"], state):
            out.append(dict(row) | {"need": state["need"]})
    return out


def sheet(conn, user: User, po_id: int, recount_of: int | None = None) -> list[dict]:
    """What to look for and never how many. New fields only go here if a receiver may see them."""
    require(user, "receiving.count")
    _po(conn, user, po_id)
    state = _state(conn, po_id)
    if state["waiting"] and state["waiting"]["counted_by"] == user.id and recount_of != state["waiting"]["id"]:
        raise POError("You've already counted this. Another receiver has to count it too, without seeing yours.")
    if state["need"] == HELD:
        raise POError(STOPPED)
    if recount_of is None and not_shipped_yet(conn, po_id, state):
        raise POError(_not_shipped_message(conn, _po(conn, user, po_id)))
    if recount_of is None or (state["waiting"] and recount_of == state["waiting"]["id"]):
        lines, delivery = state["lines"], state["delivery"]
    else:
        delivery = _count(conn, user, recount_of)["delivery_no"]
        lines = _delivery_lines(conn, po_id, delivery)
    before = previous_counts(conn, user, po_id, delivery)
    return [dict(r) | {"previous": before.get(r["id"])} for r in _line_rows(conn, lines)]


STOPPED = "The two counts didn't match twice, so counting has stopped. The home office will check and tell you when to count again."


def previous_counts(conn, user: User, po_id: int, delivery: int) -> dict[int, tuple[int, int]]:
    """What this person last counted of each product in this delivery, as (pieces, damaged). Only ever their own numbers, never the other count."""
    out = {}
    for r in conn.execute("SELECT c.po_item_id, c.pieces_m, COALESCE(d.damaged_m, 0) AS damaged_m FROM branch_count_items c JOIN branch_counts b ON b.id = c.count_id"
                          " LEFT JOIN branch_count_damage d ON d.count_id = c.count_id AND d.po_item_id = c.po_item_id"
                          " WHERE b.po_id = ? AND b.delivery_no = ? AND b.counted_by = ? ORDER BY b.id", (po_id, delivery, user.id)):
        out[r["po_item_id"]] = (r["pieces_m"], r["damaged_m"])
    return out


def sheet_key(conn, user: User, po_id: int) -> tuple[int, int, int]:
    """Which delivery, attempt and round a count sheet is for. Read it before the sheet, and hand it back with the count, so a sheet that went
    out of date meanwhile, say because two others finished the delivery, can't be saved as the start of another delivery."""
    require(user, "receiving.count")
    _po(conn, user, po_id)
    state = _state(conn, po_id)
    return state["delivery"], state["attempt"], state["round"]


def need_label(conn, po_id: int) -> str:
    need = _state(conn, po_id)["need"]
    return "Stopped: the home office is checking" if need == HELD else need or FIRST


def save_count(conn, user: User, po_id: int, pieces: dict[int, str], remarks: dict[int, str] | None = None, recount_of: int | None = None, damaged: dict[int, str] | None = None, extras: list[dict] | None = None, boxes: str | None = None,
               expect: tuple[int, int, int] | None = None) -> dict:
    """Records one person's count. If it's the second count, compares the two and accepts whatever they agree on.

    Every product asked for needs a number, 0 included, so a blank can't pass as nothing arrived. Damaged is how many of those counted pieces are damaged,
    so it's already inside the count, and blank means none. Extras are products that weren't on the PO, each with its own pieces, and damaged and remarks if any.
    Boxes is how many boxes reached the branch. It's needed on a count of the whole delivery once the Central Warehouse shipped this PO, so a box lost on the way shows.
    Expect is what sheet_key said when the sheet was opened. If counting has moved on since, the count is refused and nothing is saved.
    Returns the count's id, the outcome, and how many products didn't match.
    """
    require(user, "receiving.count")
    pieces, remarks, damaged = dict(pieces), dict(remarks or {}), dict(damaged or {})
    with tx(conn):
        po = _po(conn, user, po_id)
        if po["status"] not in COUNTABLE:
            raise POError(f"{po['po_number']} can't be received now. Ask the home office.")
        state = _state(conn, po_id)
        if state["need"] == HELD:
            raise POError(STOPPED)
        if recount_of is None and not_shipped_yet(conn, po_id, state):
            raise POError(_not_shipped_message(conn, po))
        if expect is not None and tuple(expect) != (state["delivery"], state["attempt"], state["round"]):
            raise POError(OUT_OF_DATE)
        waiting = state["waiting"]
        delivery, attempt, round_no, asked = state["delivery"], state["attempt"], state["round"], state["lines"]
        redo = False
        if recount_of is None and state["need"] == RECOUNT:
            recount_of = state["recount_of"]
        if recount_of is not None:
            old = _count(conn, user, recount_of)
            if old["po_id"] != po_id:
                raise POError("That count is for another PO.")
            if old["status"] == WAITING:
                if old["counted_by"] != user.id:
                    raise POError("That count is waiting for yours. Count it as the second count.")
                if _redone(conn, old):
                    raise POError("You've already counted this again once. Another receiver has to count it now, without seeing yours.")
                redo = True
            elif old["status"] != APPROVED:
                raise POError("That count has already been replaced. Recount the latest one.")
            elif state["need"] != RECOUNT or state["delivery"] != old["delivery_no"]:
                raise POError("Both counts matched, so this delivery is finished. Only the home office can reopen it for a recount.")
            else:
                delivery = old["delivery_no"]
                attempt = conn.execute("SELECT MAX(attempt_no) FROM branch_counts WHERE po_id = ? AND delivery_no = ?", (po_id, delivery)).fetchone()[0] + 1
                round_no, asked = 1, _delivery_lines(conn, po_id, delivery)
        elif waiting and waiting["counted_by"] == user.id:
            raise POError("You've already counted this. Another receiver has to count it too, without seeing yours.")
        asked = list(asked)
        for extra in extras or []:
            line_id = _add_extra(conn, user, po_id, delivery, extra)
            asked.append(line_id)
            pieces[line_id], damaged[line_id], remarks[line_id] = extra.get("pieces", ""), extra.get("damaged", ""), extra.get("remarks", "")
        lines = []
        for item in _line_rows(conn, asked):
            text = str(pieces.get(item["id"], "")).strip()
            if not text:
                raise POError(f"Type how many pieces of {item['description']} you counted. Type 0 if it didn't arrive.")
            counted = read_quantity(text, item["description"], item["uom"], "Pieces counted")
            broken = read_quantity(str(damaged.get(item["id"], "")).strip(), item["description"], item["uom"], "Damaged")
            if broken > counted:
                raise POError(f"{item['description']} has more damaged than counted. Count every piece, including the damaged ones.")
            lines.append((item, counted, broken))
        box_text = str(boxes if boxes is not None else "").strip()
        whole_delivery = state["need"] in (FIRST, SECOND, RECOUNT, None) and round_no == 1
        if box_text and not box_text.isdecimal():
            raise POError("Type the boxes that arrived as a whole number, 0 if none.")
        # Compared as text first: a number thousands of digits long can't even be turned into a number.
        if box_text and (len(box_text.lstrip("0")) > len(str(MAX_BOXES)) or int(box_text) > MAX_BOXES):
            raise POError(f"{_short(box_text)} boxes is far more than a delivery can have. Check the number.")
        if not box_text and whole_delivery and _shipped_from_central(conn, po_id):
            raise POError("Type how many boxes arrived from the Central Warehouse.")
        count_id = conn.execute("INSERT INTO branch_counts(po_id, delivery_no, attempt_no, round_no, status, counted_by, counted_at, recount_of) VALUES (?,?,?,?,?,?,?,?)",
                                (po_id, delivery, attempt, round_no, WAITING, user.id, timestamp(), recount_of)).lastrowid
        conn.executemany("INSERT INTO branch_count_items(count_id, po_item_id, item_code, description, size, color, uom, pieces_m, remarks) VALUES (?,?,?,?,?,?,?,?,?)",
                         [(count_id, i["id"], i["item_code"], i["description"], i["size"], i["color"], i["uom"], m, str(remarks.get(i["id"], "")).strip()) for i, m, _ in lines])
        conn.executemany("INSERT INTO branch_count_damage(count_id, po_item_id, damaged_m) VALUES (?,?,?)", [(count_id, i["id"], d) for i, _, d in lines if d])
        if box_text:
            conn.execute("INSERT INTO branch_count_boxes(count_id, boxes) VALUES (?,?)", (count_id, int(box_text)))
        audit(conn, user, "BRANCH_COUNT", "purchase_order", po_id, po_id=po_id, summary=f"{po['po_number']} delivery {delivery} counted")
        # The home office's permission is used up by the first count it lets happen.
        if not redo and (state["need"] == RECOUNT or (state["need"] == AGAIN and round_no > CHECK_AFTER)):
            allowance = open_reopen(conn, po_id, "RECOUNT" if state["need"] == RECOUNT else "ROUND")
            if allowance:
                conn.execute("UPDATE count_reopens SET used_by_count_id = ? WHERE id = ?", (count_id, allowance["id"]))
        if redo:
            conn.execute("UPDATE branch_counts SET status = ? WHERE id = ?", (REPLACED, recount_of))
        if redo or not waiting or recount_of is not None:
            return {"count_id": count_id, "outcome": WAITING, "mismatched": 0}
        return {"count_id": count_id, **_compare(conn, po, waiting, count_id, user)}


def read_quantity(text: str, description: str, uom: str, column: str) -> int:
    """One typed number of a count. Blank is 0. A number that can't be right is refused with a message naming the product and the column."""
    try:
        value = parse_qty(text, uom)
    except ValueError as exc:
        why = str(exc).replace(f"'{text}'", f"'{_short(text)}'")
        raise POError(f"{column} for {description}: {why[:1].lower() + why[1:] if why.startswith('Quantity') else why}.") from None
    if value > MAX_PIECES_M:
        raise POError(f"{column} for {description}: {_short(text)} is far more than a delivery can have. Check the number.")
    return value


def _short(text) -> str:
    """What was typed, cut short so a long slip doesn't fill the message."""
    text = str(text).strip()
    return text if len(text) <= 20 else text[:20] + "..."


def not_shipped_yet(conn, po_id: int, state: dict | None = None) -> bool:
    """A new delivery can't be counted at the branch until the Central Warehouse has shipped it there.
    Only once the warehouse has a login: until then nobody can mark anything shipped, so branches count as before.
    Second counts, rounds that didn't match and recounts are of a delivery that's already there, so they're never held."""
    from .auth import warehouse_login_missing
    from .db import get_setting
    state = state or _state(conn, po_id)
    if state["waiting"] or state["need"] not in (FIRST, None) or get_setting(conn, "receiving.after_shipping") != "1" or warehouse_login_missing(conn):
        return False
    shipped = conn.execute("SELECT COUNT(*) FROM warehouse_deliveries WHERE po_id = ? AND shipped_at IS NOT NULL", (po_id,)).fetchone()[0]
    return shipped < state["delivery"]


def _not_shipped_message(conn, po) -> str:
    from .claim_stubs import warehouse_name
    return f"{po['po_number']} hasn't been shipped to your branch from {warehouse_name(conn)} yet. Count it when it arrives. If it's already here, ask the warehouse to mark it shipped."


def _shipped_from_central(conn, po_id: int) -> bool:
    return conn.execute("SELECT 1 FROM warehouse_deliveries WHERE po_id = ? AND shipped_at IS NOT NULL", (po_id,)).fetchone() is not None


def boxes_counted(conn, po_id: int) -> list[dict]:
    """The boxes each receiver typed, by delivery. For the home office only: receivers never see the warehouse's figure or each other's."""
    return [dict(r) for r in conn.execute(
        "SELECT b.delivery_no, b.id AS count_id, x.boxes, u.full_name AS counted_name FROM branch_count_boxes x JOIN branch_counts b ON b.id = x.count_id JOIN users u ON u.id = b.counted_by"
        " WHERE b.po_id = ? AND b.status <> 'REPLACED' ORDER BY b.delivery_no, b.id", (po_id,))]


def boxes_check(conn, po_id: int) -> dict | None:
    """Boxes the Central Warehouse shipped against boxes the branch says arrived, when both are known. Each delivery counts what the receivers agreed, or the lower figure if they didn't."""
    from .warehouse import boxes_sent
    sent = boxes_sent(conn, po_id)
    typed = boxes_counted(conn, po_id)
    if sent is None or not typed:
        return None
    by_delivery: dict[int, list[int]] = {}
    for row in typed:
        by_delivery.setdefault(row["delivery_no"], []).append(row["boxes"])
    arrived = sum(min(v) for v in by_delivery.values())
    return {"sent": sent, "arrived": arrived, "missing": max(sent - arrived, 0), "disagree": [d for d, v in by_delivery.items() if len(set(v)) > 1]}


def _redone(conn, count) -> bool:
    """Whether this waiting count already replaced the same person's earlier count of the same round. Each person may redo theirs once."""
    if not count["recount_of"]:
        return False
    earlier = conn.execute("SELECT counted_by, status, delivery_no, attempt_no, round_no FROM branch_counts WHERE id = ?", (count["recount_of"],)).fetchone()
    return bool(earlier) and earlier["status"] == REPLACED and earlier["counted_by"] == count["counted_by"] and \
        (earlier["delivery_no"], earlier["attempt_no"], earlier["round_no"]) == (count["delivery_no"], count["attempt_no"], count["round_no"])


def can_redo(conn, user: User, count_id: int) -> bool:
    """A person may count again once while their count waits for the second one."""
    count = conn.execute("SELECT * FROM branch_counts WHERE id = ?", (count_id,)).fetchone()
    return bool(count) and count["status"] == WAITING and count["counted_by"] == user.id and not _redone(conn, count)


def _matched_delivery(conn, po_id: int):
    return conn.execute("SELECT delivery_no, attempt_no FROM branch_counts WHERE po_id = ? AND status = ? ORDER BY id DESC LIMIT 1", (po_id, APPROVED)).fetchone()


def _why_not_reopen(conn, po) -> str:
    """Why a matched delivery can't be reopened now, or nothing if it can."""
    from .orders import live_charges, paid_by
    if po["status"] not in COUNTABLE:
        return f"{po['po_number']} is closed or cancelled, so it can't be counted again."
    state = _state(conn, po["id"])
    if state["need"] == HELD:
        return ""
    if state["need"]:
        return f"{po['po_number']} is still being counted. Wait until the counts are in."
    latest = _matched_delivery(conn, po["id"])
    if latest is None:
        return f"{po['po_number']} has no matched count to reopen."
    if conn.execute("SELECT 1 FROM count_reopens WHERE po_id = ? AND delivery_no = ? AND kind = 'RECOUNT'", (po["id"], latest["delivery_no"])).fetchone():
        return f"Delivery {latest['delivery_no']} of {po['po_number']} was already reopened once. It can't be recounted again."
    check = paid_by(conn, po["of_id"])
    if check:
        return f"Check {check} already pays this order, so its delivery can't be recounted. Anything wrong now goes on a debit memo or claim stub, which comes off the supplier's next check."
    charges = live_charges(conn, po["id"])
    if charges:
        return f"{po['po_number']} has {charges}. Management has to cancel those first, with a reason, before it can be recounted."
    return ""


def reopen(conn, user: User, po_id: int, reason: str) -> None:
    """The home office lets a delivery be counted once more: an accepted one, for a recount, or one whose counts didn't match twice, for one more round.

    A matched delivery can be reopened only once, and not after it's paid or once a debit memo, claim stub or item pass rests on it.
    Those have to be cancelled first, by management, so the reason is on record twice.
    """
    require(user, "receiving.reopen")
    reason = reason.strip()
    if not reason:
        raise POError("Write why this delivery has to be counted again.")
    with tx(conn):
        po = conn.execute("SELECT * FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
        if po is None:
            raise POError("That PO no longer exists.")
        problem = _why_not_reopen(conn, po)
        if problem:
            raise POError(problem)
        state = _state(conn, po_id)
        if state["need"] == HELD:
            kind, delivery, attempt = "ROUND", state["delivery"], state["attempt"]
            what = f"one more count of delivery {delivery}"
        else:
            latest = _matched_delivery(conn, po_id)
            kind, delivery, attempt = "RECOUNT", latest["delivery_no"], latest["attempt_no"] + 1
            what = f"a recount of delivery {delivery}"
        conn.execute("INSERT INTO count_reopens(po_id, delivery_no, attempt_no, kind, reason, opened_by, opened_at) VALUES (?,?,?,?,?,?,?)",
                     (po_id, delivery, attempt, kind, reason, user.id, timestamp()))
        audit(conn, user, "COUNT_REOPEN", "purchase_order", po_id, po_id=po_id, summary=f"{po['po_number']}: allowed {what}. {reason}")


def reopen_label(conn, po_id: int) -> str:
    """What the home office's button says for this PO, or nothing when it can't be pressed."""
    po = conn.execute("SELECT * FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
    if po is None or _why_not_reopen(conn, po):
        return ""
    return "Allow one more count" if _state(conn, po_id)["need"] == HELD else "Reopen for recount"


def _figures(conn, count_id: int) -> dict[int, tuple[int, int]]:
    return {r["po_item_id"]: (r["pieces_m"], r["damaged_m"]) for r in conn.execute(
        "SELECT c.po_item_id, c.pieces_m, COALESCE(d.damaged_m, 0) AS damaged_m FROM branch_count_items c"
        " LEFT JOIN branch_count_damage d ON d.count_id = c.count_id AND d.po_item_id = c.po_item_id WHERE c.count_id = ?", (count_id,))}


def _compare(conn, po, first, second_id: int, user: User) -> dict:
    """Both counts of a round are in. What agrees is accepted, and the delivery only reaches the PO once every product has been agreed."""
    po_id, delivery, attempt = po["id"], first["delivery_no"], first["attempt_no"]
    one, two = _figures(conn, first["id"]), _figures(conn, second_id)
    stamp = timestamp()
    agreed = [(item, figures) for item, figures in one.items() if two.get(item) == figures]
    conn.executemany("INSERT INTO branch_received(po_id, delivery_no, attempt_no, po_item_id, pieces_m, damaged_m, accepted_at) VALUES (?,?,?,?,?,?,?)",
                     [(po_id, delivery, attempt, item, figures[0], figures[1], stamp) for item, figures in agreed])
    # A line only one of them has, such as a product the second person added, doesn't agree yet.
    mismatched = len(set(one) | set(two)) - len(agreed)
    if mismatched:
        conn.execute("UPDATE branch_counts SET status = ? WHERE id IN (?, ?)", (MISMATCH, first["id"], second_id))
        audit(conn, user, "BRANCH_COUNT_MISMATCH", "purchase_order", po_id, po_id=po_id, summary=f"{po['po_number']} delivery {delivery}: {mismatched} products didn't match")
        return {"outcome": MISMATCH, "mismatched": mismatched}
    # Fully agreed. An earlier attempt at this delivery stops counting now, and stays on record.
    conn.execute("UPDATE branch_counts SET status = ? WHERE po_id = ? AND delivery_no = ? AND attempt_no < ? AND status = ?", (REPLACED, po_id, delivery, attempt, APPROVED))
    conn.execute("UPDATE branch_counts SET status = ?, approved_by = ?, approved_at = ? WHERE id = ?", (APPROVED, first["counted_by"], stamp, second_id))
    conn.execute("UPDATE branch_counts SET status = ?, approved_by = ?, approved_at = ? WHERE id = ?", (APPROVED, user.id, stamp, first["id"]))
    _apply_totals(conn, po_id)
    audit(conn, user, "BRANCH_COUNT_MATCH", "purchase_order", po_id, po_id=po_id, summary=f"{po['po_number']} delivery {delivery}: both counts match")
    return {"outcome": APPROVED, "mismatched": 0}


def _apply_totals(conn, po_id: int) -> None:
    """The PO's received quantities and status, always worked out again from what was agreed."""
    items = conn.execute(f"SELECT i.id, i.qty_m, i.pieces_per_unit, (SELECT COALESCE(SUM(r.pieces_m), 0) FROM branch_received r WHERE r.po_item_id = i.id AND {_IN_FORCE}) AS got"
                         " FROM purchase_order_items i WHERE i.po_id = ?", (po_id,)).fetchall()
    deliveries = conn.execute("SELECT COUNT(DISTINCT delivery_no) FROM branch_counts WHERE po_id = ? AND status = ?", (po_id, APPROVED)).fetchone()[0]
    status = FULL if all(i["got"] >= i["qty_m"] * i["pieces_per_unit"] for i in items) else PARTIAL if any(i["got"] for i in items) else ISSUED
    with write_guard(conn):
        for i in items:
            conn.execute("UPDATE purchase_order_items SET received_qty_m = ? WHERE id = ?", ((i["got"] + i["pieces_per_unit"] // 2) // i["pieces_per_unit"], i["id"]))
        conn.execute("UPDATE purchase_orders SET delivery_count = ?, status = CASE WHEN status IN (?, ?) THEN status ELSE ? END, updated_at = ? WHERE id = ?",
                     (deliveries, CLOSED, CANCELLED, status, timestamp(), po_id))


# ------------------------------------------------------------------ what a branch sees

def branch_counts(conn, user: User) -> list[dict]:
    """This branch's counts, newest first, with who did what and nothing about what was ordered."""
    require(user, "receiving.count")
    where, args = ("", []) if user.branch_code is None else (" WHERE p.branch_code = ?", [user.branch_code])
    rows = conn.execute(
        "SELECT b.id, b.po_id, b.delivery_no, b.attempt_no, b.round_no, b.status, b.counted_by, b.approved_by, b.counted_at, b.approved_at, b.recount_of, p.po_number, p.branch_name, s.name AS supplier,"
        " c.full_name AS counted_name, a.full_name AS approved_name FROM branch_counts b JOIN purchase_orders p ON p.id = b.po_id JOIN suppliers s ON s.id = p.supplier_id"
        " JOIN users c ON c.id = b.counted_by LEFT JOIN users a ON a.id = b.approved_by" + where + " ORDER BY b.id DESC LIMIT 500", args).fetchall()
    return [dict(r) for r in rows]


def _lines(conn, count_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT c.item_code, c.description, c.size, c.color, c.uom, c.pieces_m, c.remarks, COALESCE(d.damaged_m, 0) AS damaged_m FROM branch_count_items c"
        " LEFT JOIN branch_count_damage d ON d.count_id = c.count_id AND d.po_item_id = c.po_item_id WHERE c.count_id = ? ORDER BY c.id", (count_id,))]


def count_lines(conn, user: User, count_id: int) -> list[dict]:
    """A person's own numbers. Nobody at a branch can read anyone else's, or the second count wouldn't be blind."""
    require(user, "receiving.count")
    count = _count(conn, user, count_id)
    if count["counted_by"] != user.id and "ADMIN" not in user.roles:
        raise POError("Only the person who counted can see these numbers.")
    return _lines(conn, count_id)


# ------------------------------------------------------------------ what the home office sees

def progress(conn, po_id: int) -> str:
    """Where counting stands, in words. Empty once nothing is open."""
    state = _state(conn, po_id)
    if state["need"] == SECOND:
        return COUNT_LABELS[WAITING]
    if state["need"] == HELD:
        return f"Check: {state['round'] - 1} counts didn't match. Counting stopped"
    if state["need"] == AGAIN:
        return "Being recounted"
    if state["need"] == RECOUNT:
        return "Reopened for recount"
    return ""


def delivered_by_po(conn) -> dict[int, dict]:
    """How much of each live PO has arrived, counting only what two people agreed on. A PO closed before receiving was in use has no count to go by."""
    return {r["id"]: dict(r) for r in conn.execute(
        "SELECT p.id, p.of_id, p.branch_code, p.status, (SELECT COALESCE(SUM(i.qty_m * i.pieces_per_unit), 0) FROM purchase_order_items i WHERE i.po_id = p.id) AS ordered_m,"
        f" (SELECT COALESCE(SUM(r.pieces_m), 0) FROM branch_received r WHERE r.po_id = p.id AND {_IN_FORCE}) AS received_m,"
        " EXISTS (SELECT 1 FROM branch_counts b WHERE b.po_id = p.id) AS counted FROM purchase_orders p WHERE p.status <> 'CANCELLED'")}


def po_results(conn, user: User, text: str = "", tab: str | None = None) -> list[dict]:
    """Every PO with a count, ordered against received. Nothing counts as received until two people have agreed on a whole delivery.

    A delivery is Completed when no counting is open and every product arrived exactly as ordered with none damaged, or debit memos charge exactly
    what's short and claim stubs exactly what's damaged, and any excess has left on an item pass. Anything else stays To check.
    Every PO the search finds is listed: there's no cap that would hide older ones (the Received page shows them a page at a time, see received_list).
    """
    require(user, "receiving.review")
    out = _classified(conn, text, every_line=True)
    _finish_rows(conn, out)
    return [r for r in out if r["tab"] == tab] if tab else out


def received_list(conn, user: User, text: str = "", tab: str = TO_CHECK, branch_code: int | None = None, limit: int | None = None) -> dict:
    """What the Received page shows: the POs on one tab for one branch (None for all), newest first, with the full rows built only for the first limit
    (None for every one). The rows shown are the same as po_results gives for them. Also how many are on each tab for that branch, and on this tab per branch,
    so the counts are totals and not just what's shown. Long lists show a page at a time so they stay fast, and search still looks through everything."""
    require(user, "receiving.review")
    every = _classified(conn, text)
    chosen = [r for r in every if branch_code is None or r["branch_code"] == branch_code]
    on_tab = [r for r in every if r["tab"] == tab]
    branches: dict = {None: len(on_tab)}
    for r in on_tab:
        branches[r["branch_code"]] = branches.get(r["branch_code"], 0) + 1
    rows = [r for r in on_tab if branch_code is None or r["branch_code"] == branch_code]
    shown = rows if limit is None else rows[:limit]
    _finish_rows(conn, shown)
    return {"rows": shown, "total": len(rows), "counts": {t: sum(r["tab"] == t for r in chosen) for t in (TO_CHECK, COMPLETED)}, "branches": branches}


def _classified(conn, text: str, every_line: bool = False) -> list[dict]:
    """Every PO with a count that the search finds, newest first, with where its counting stands and which tab it's on.
    A PO still being counted is To check whatever its figures, so its lines are only read when it's shown, unless every_line asks for them all at once
    (AUD-149 reads them once for the whole list)."""
    where, args = ["EXISTS (SELECT 1 FROM branch_counts b WHERE b.po_id = p.id)"], []
    for word in text.split():
        where.append("(p.po_number LIKE ? OR f.of_number LIKE ? OR s.name LIKE ? OR s.code LIKE ? OR p.branch_name LIKE ?)")
        args += [f"%{word}%"] * 5
    rows = conn.execute(
        "SELECT p.id, p.po_number, p.status, p.branch_name, p.branch_code, f.of_number, s.name AS supplier,"
        " (SELECT SUM(i.qty_m * i.pieces_per_unit) FROM purchase_order_items i WHERE i.po_id = p.id) AS ordered_m,"
        f" (SELECT COALESCE(SUM(r.pieces_m), 0) FROM branch_received r WHERE r.po_id = p.id AND {_IN_FORCE}) AS received_m,"
        f" (SELECT COALESCE(SUM(r.damaged_m), 0) FROM branch_received r WHERE r.po_id = p.id AND {_IN_FORCE}) AS damaged_m,"
        " (SELECT COUNT(*) FROM branch_counts b WHERE b.po_id = p.id AND b.status = 'WAITING') AS waiting,"
        " (SELECT COUNT(*) FROM purchase_order_items i WHERE i.po_id = p.id AND i.qty_m * i.pieces_per_unit <> (SELECT COALESCE(SUM(r.pieces_m), 0)"
        f"    FROM branch_received r WHERE r.po_item_id = i.id AND {_IN_FORCE})) AS lines_off"
        " FROM purchase_orders p JOIN suppliers s ON s.id = p.supplier_id LEFT JOIN order_forms f ON f.id = p.of_id WHERE " + " AND ".join(where) + " ORDER BY p.id DESC", args).fetchall()
    out = [dict(r) for r in rows]
    if not out:
        return out
    words = _progress_many(conn, out)
    for r in out:
        r["progress"] = words[r["id"]]
    _add_excess(conn, out if every_line else [r for r in out if not r["progress"]])
    for r in out:
        r["tab"] = TO_CHECK if r["progress"] or not r.pop("_settled") else COMPLETED
    return out


def _add_excess(conn, rows: list[dict]) -> None:
    """Excess and whether it's all settled, for these POs, reading counting, lines, extras, returns and charges once for all of them (AUD-149)."""
    if not rows:
        return
    ids = [r["id"] for r in rows]
    lines_by, extra_by = _lines_many(conn, ids), _extra_lines_many(conn, ids)
    returned_by, charged_by = _returned_many(conn, ids), _charged_many(conn, ids)
    for r in rows:
        lines = lines_by.get(r["id"], [])
        excess = _excess_from(lines, extra_by.get(r["id"], []), returned_by.get(r["id"], {}))
        r["excess_m"], r["excess_left_m"] = sum(x["excess_m"] for x in excess), sum(x["left_m"] for x in excess)
        r["_settled"] = _settled_from(charged_by.get(r["id"], {}), lines, excess)


def _finish_rows(conn, rows: list[dict]) -> None:
    """The rest of each row for the POs that are shown: the difference, the excess of those still being counted, and the documents' numbers."""
    if not rows:
        return
    _add_excess(conn, [r for r in rows if "excess_m" not in r])
    ids = json.dumps([r["id"] for r in rows])
    # Each document kind is read once for every PO shown, not once per PO.
    numbers = {kind: {} for kind in ("debit_memos", "claim_stubs", "item_passes")}
    for kind, sql in (("debit_memos", f"SELECT po_id, dm_number AS n FROM debit_memos WHERE status = 'ISSUED' AND po_id IN {_IDS} ORDER BY id"),
                      ("claim_stubs", f"SELECT po_id, cs_number AS n FROM claim_stubs WHERE status <> 'CANCELLED' AND po_id IN {_IDS} ORDER BY id"),
                      ("item_passes", f"SELECT po_id, ip_number AS n FROM item_passes WHERE status <> 'CANCELLED' AND po_id IN {_IDS} ORDER BY id")):
        for row in conn.execute(sql, (ids,)):
            numbers[kind].setdefault(row["po_id"], []).append(row["n"])
    for r in rows:
        r.pop("_settled", None)
        r["difference_m"] = r["received_m"] - r["ordered_m"]
        for kind, by_po in numbers.items():
            r[kind] = ", ".join(by_po.get(r["id"], []))


_IDS = "(SELECT value FROM json_each(?))"  # a list of PO ids passed as one JSON value, so a long list is still one statement


def _progress_many(conn, rows: list[dict]) -> dict[int, str]:
    """progress() for every PO on a list at once. rows carry the PO id and how many counts are waiting for their second person."""
    ids = json.dumps([r["id"] for r in rows])
    latest = {r["po_id"]: r for r in conn.execute(
        "SELECT b.po_id, b.status, b.delivery_no, b.attempt_no, b.round_no FROM branch_counts b"
        f" WHERE b.id = (SELECT MAX(x.id) FROM branch_counts x WHERE x.po_id = b.po_id) AND b.po_id IN {_IDS}", (ids,))}
    rounds = {(r[0], r[1], r[2]): r[3] for r in conn.execute(
        f"SELECT po_id, delivery_no, attempt_no, COUNT(*) FROM count_reopens WHERE kind = 'ROUND' AND po_id IN {_IDS} GROUP BY po_id, delivery_no, attempt_no", (ids,))}
    recount = {r[0] for r in conn.execute(f"SELECT DISTINCT po_id FROM count_reopens WHERE used_by_count_id IS NULL AND kind = 'RECOUNT' AND po_id IN {_IDS}", (ids,))}
    out = {}
    for r in rows:
        last = latest.get(r["id"])
        if r["waiting"]:
            out[r["id"]] = COUNT_LABELS[WAITING]
        elif last is not None and last["status"] == MISMATCH:
            next_round = last["round_no"] + 1
            allowed = CHECK_AFTER + rounds.get((r["id"], last["delivery_no"], last["attempt_no"]), 0)
            out[r["id"]] = "Being recounted" if next_round <= allowed else f"Check: {next_round - 1} counts didn't match. Counting stopped"
        elif r["id"] in recount:
            out[r["id"]] = "Reopened for recount"
        else:
            out[r["id"]] = ""
    return out


def _lines_many(conn, ids: list[int]) -> dict[int, list[dict]]:
    """lines_of() for every PO on a list at once."""
    by_po: dict[int, list[dict]] = {}
    for r in conn.execute(_LINES_SELECT.replace("SELECT i.id, ", "SELECT i.po_id, i.id, ", 1) + f" WHERE i.po_id IN {_IDS} ORDER BY i.po_id, i.line_no", (json.dumps(ids),)):
        line = dict(r)
        by_po.setdefault(line.pop("po_id"), []).append(line)
    central: dict[int, dict[int, int]] = {}
    for po_id, item, pieces in conn.execute(
            "SELECT d.po_id, w.po_item_id, SUM(w.pieces_m) FROM warehouse_delivery_items w JOIN warehouse_deliveries d ON d.id = w.delivery_id"
            f" WHERE d.po_id IN {_IDS} GROUP BY d.po_id, w.po_item_id", (json.dumps(ids),)):
        central.setdefault(po_id, {})[item] = pieces
    for po_id, rows in by_po.items():
        _finish_lines(rows, central.get(po_id, {}))
    return by_po


def _extra_lines_many(conn, ids: list[int]) -> dict[int, list[dict]]:
    """_extra_lines() for every PO on a list at once."""
    by_po: dict[int, list[dict]] = {}
    for r in conn.execute(_EXTRA_SELECT.replace("SELECT -e.id", "SELECT e.po_id, -e.id", 1) + f" WHERE e.po_id IN {_IDS} ORDER BY e.id", (json.dumps(ids),)):
        line = dict(r)
        line["difference_m"] = line["received_m"]
        by_po.setdefault(line.pop("po_id"), []).append(line)
    return by_po


def _returned_many(conn, ids: list[int]) -> dict[int, dict[int, int]]:
    """What excess_lines() counts as already gone back, for every PO on a list at once."""
    out: dict[int, dict[int, int]] = {}
    ids_json = json.dumps(ids)
    for po_id, item, pieces in conn.execute(
            f"SELECT po_id, po_item_id, SUM(pieces_m) FROM (SELECT po_id, po_item_id, pieces_m FROM excess_returns WHERE po_id IN {_IDS}"
            f" UNION ALL SELECT p.po_id, i.po_item_id, i.pieces_m FROM item_pass_items i JOIN item_passes p ON p.id = i.ip_id WHERE p.po_id IN {_IDS} AND p.status <> 'CANCELLED')"
            " GROUP BY po_id, po_item_id", (ids_json, ids_json)):
        out.setdefault(po_id, {})[item] = pieces
    return out


def _charged_many(conn, ids: list[int]) -> dict[int, dict[tuple[int, str], int]]:
    """dm_pieces() for every PO on a list at once."""
    out: dict[int, dict[tuple[int, str], int]] = {}
    ids_json = json.dumps(ids)
    for po_id, item, reason, pieces in conn.execute(
            "SELECT po_id, po_item_id, reason, SUM(pieces_m) FROM ("
            f" SELECT d.po_id, i.po_item_id, i.reason, i.pieces_m FROM debit_memo_items i JOIN debit_memos d ON d.id = i.dm_id WHERE d.po_id IN {_IDS} AND d.status = 'ISSUED'"
            f" UNION ALL SELECT c.po_id, i.po_item_id, 'DAMAGED', i.pieces_m FROM claim_stub_items i JOIN claim_stubs c ON c.id = i.cs_id WHERE c.po_id IN {_IDS} AND c.status <> 'CANCELLED'"
            " AND c.id NOT IN (SELECT cs_id FROM late_damage)"
            ") GROUP BY po_id, po_item_id, reason", (ids_json, ids_json)):
        out.setdefault(po_id, {})[(item, reason)] = pieces
    return out


def excess_lines(conn, po_id: int, lines: list[dict] | None = None) -> list[dict]:
    """Everything beyond the order that two people agreed on, with how much has left on an item pass. Returns recorded before item passes still count.
    lines is lines_of for this PO when the caller already has it."""
    returned = {r["po_item_id"]: r["pieces_m"] for r in conn.execute(
        "SELECT po_item_id, SUM(pieces_m) AS pieces_m FROM (SELECT po_item_id, pieces_m FROM excess_returns WHERE po_id = ?"
        " UNION ALL SELECT i.po_item_id, i.pieces_m FROM item_pass_items i JOIN item_passes p ON p.id = i.ip_id WHERE p.po_id = ? AND p.status <> 'CANCELLED') GROUP BY po_item_id", (po_id, po_id))}
    return _excess_from(lines_of(conn, po_id) if lines is None else lines, _extra_lines(conn, po_id), returned)


def _excess_from(lines: list[dict], extra: list[dict], returned: dict[int, int]) -> list[dict]:
    rows = [dict(line, excess_m=line["received_m"] - line["ordered_m"], not_on_po=False) for line in lines if line["received_m"] > line["ordered_m"]]
    rows += [dict(line, excess_m=line["received_m"], not_on_po=True) for line in extra if line["received_m"] > 0]
    for r in rows:
        r["returned_m"] = returned.get(r["id"], 0)
        r["left_m"] = max(r["excess_m"] - r["returned_m"], 0)
    return rows


def taken_as_delivered(conn, po_id: int) -> bool:
    """A closed PO with no count that's paid as delivered in full: issued before branch receiving existed, or received outside the program by Admin."""
    po = conn.execute("SELECT status, issued_at FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
    if po is None or po["status"] != CLOSED or conn.execute("SELECT 1 FROM branch_counts WHERE po_id = ?", (po_id,)).fetchone():
        return False
    return (po["issued_at"] or "") < live_since(conn) or conn.execute("SELECT 1 FROM outside_receipts WHERE po_id = ?", (po_id,)).fetchone() is not None


def live_since(conn) -> str:
    """When branch receiving started in this database. A PO issued before then may close with no count, and is then taken as delivered."""
    row = conn.execute("SELECT MIN(occurred_at) FROM audit_logs WHERE action = 'MIGRATE_V9'").fetchone()
    return row[0] or ""


def of_delivery_complete(conn, of_id: int) -> bool:
    """Every live PO under an OF has finished its delivery: counted, agreed and settled, or closed. Nothing counts as finished before anything was counted.
    A closed PO that was counted still needs its damage on a claim stub and its excess on an item pass. Its shortage is simply never coming."""
    pos = conn.execute("SELECT id, status FROM purchase_orders WHERE of_id = ? AND status <> ?", (of_id, CANCELLED)).fetchall()
    for po in pos:
        if po["status"] == CLOSED:
            if conn.execute("SELECT 1 FROM branch_counts WHERE po_id = ?", (po["id"],)).fetchone() and (progress(conn, po["id"]) or not _settled(conn, po["id"], include_short=False)):
                return False
            continue
        if not conn.execute("SELECT 1 FROM branch_counts WHERE po_id = ?", (po["id"],)).fetchone() or progress(conn, po["id"]) or not _settled(conn, po["id"]):
            return False
    return bool(pos)


def dm_pieces(conn, po_id: int) -> dict[tuple[int, str], int]:
    """What's been charged on a PO, by product and reason: shortages on debit memos, and damage on claim stubs. Cancelled ones don't count."""
    return {(r["po_item_id"], r["reason"]): r["pieces_m"] for r in conn.execute(
        "SELECT po_item_id, reason, SUM(pieces_m) AS pieces_m FROM ("
        " SELECT i.po_item_id, i.reason, i.pieces_m FROM debit_memo_items i JOIN debit_memos d ON d.id = i.dm_id WHERE d.po_id = ? AND d.status = 'ISSUED'"
        " UNION ALL SELECT i.po_item_id, 'DAMAGED', i.pieces_m FROM claim_stub_items i JOIN claim_stubs c ON c.id = i.cs_id WHERE c.po_id = ? AND c.status <> 'CANCELLED'"
        " AND c.id NOT IN (SELECT cs_id FROM late_damage)"
        ") GROUP BY po_item_id, reason", (po_id, po_id))}


def _settled(conn, po_id: int, include_short: bool = True, lines: list[dict] | None = None, excess: list[dict] | None = None) -> bool:
    """Every product is exact, or its shortage and damage are charged exactly, and any excess has left on an item pass. If a later delivery changes the figures, a charge no longer matches.
    lines and excess are lines_of and excess_lines for this PO when the caller already has them."""
    lines = lines_of(conn, po_id) if lines is None else lines
    return _settled_from(dm_pieces(conn, po_id), lines, excess_lines(conn, po_id, lines) if excess is None else excess, include_short)


def _settled_from(charged: dict, lines: list[dict], excess: list[dict], include_short: bool = True) -> bool:
    for line in lines:
        short = max(line["ordered_m"] - line["supplier_m"], 0)
        if (include_short and charged.get((line["id"], "SHORT"), 0) != short) or charged.get((line["id"], "DAMAGED"), 0) != line["damaged_m"]:
            return False
    return not any(r["left_m"] for r in excess)


_EXTRA_SELECT = ("SELECT -e.id AS id, e.item_code, e.description, e.size, e.color, 'PC' AS uom, 0 AS ordered_m,"
                 f" (SELECT COALESCE(SUM(r.pieces_m), 0) FROM branch_received r WHERE r.po_item_id = -e.id AND {_IN_FORCE}) AS received_m,"
                 f" (SELECT COALESCE(SUM(r.damaged_m), 0) FROM branch_received r WHERE r.po_item_id = -e.id AND {_IN_FORCE}) AS damaged_m"
                 " FROM branch_extra_items e")


def _extra_lines(conn, po_id: int) -> list[dict]:
    """Products that weren't on the PO, as the home office reads them."""
    rows = [dict(r) for r in conn.execute(_EXTRA_SELECT + " WHERE e.po_id = ? ORDER BY e.id", (po_id,))]
    for r in rows:
        r["difference_m"] = r["received_m"]
    return rows


def po_lines(conn, user: User, po_id: int) -> list[dict]:
    require(user, "receiving.review")
    return lines_of(conn, po_id) + [dict(r, description=f"{r['description']} (not on PO)") for r in _extra_lines(conn, po_id)]


_LINES_SELECT = ("SELECT i.id, i.item_code, i.description, i.size, i.color, i.uom, i.qty_m * i.pieces_per_unit AS ordered_m,"
                 f" (SELECT COALESCE(SUM(r.pieces_m), 0) FROM branch_received r WHERE r.po_item_id = i.id AND {_IN_FORCE}) AS received_m,"
                 f" (SELECT COALESCE(SUM(r.damaged_m), 0) FROM branch_received r WHERE r.po_item_id = i.id AND {_IN_FORCE}) AS damaged_m"
                 " FROM purchase_order_items i")


def lines_of(conn, po_id: int) -> list[dict]:
    rows = [dict(r) for r in conn.execute(_LINES_SELECT + " WHERE i.po_id = ? ORDER BY i.line_no", (po_id,))]
    return _finish_lines(rows, left_central(conn, po_id))


def _finish_lines(rows: list[dict], central: dict[int, int]) -> list[dict]:
    for r in rows:
        r["difference_m"] = r["received_m"] - r["ordered_m"]
        # When the Central Warehouse counted the pieces, that's what the supplier delivered. Fewer reaching the branch is a transit loss for the home office,
        # never a shortage charged to the supplier. More reaching the branch means the warehouse undercounted, so the supplier gets the benefit.
        r["left_central_m"] = central.get(r["id"])
        r["supplier_m"] = r["received_m"] if r["left_central_m"] is None else max(r["received_m"], r["left_central_m"])
        r["transit_m"] = 0 if r["left_central_m"] is None else max(r["left_central_m"] - r["received_m"], 0)
    return rows


def po_history(conn, user: User, po_id: int) -> list[dict]:
    """Every count ever made on a PO, oldest first, including those that didn't match or were replaced."""
    require(user, "receiving.review")
    return [dict(r) for r in conn.execute(
        "SELECT b.id, b.delivery_no, b.attempt_no, b.round_no, b.status, b.counted_by, b.approved_by, b.counted_at, b.approved_at, b.recount_of, c.full_name AS counted_name, a.full_name AS approved_name,"
        " (SELECT SUM(x.pieces_m) FROM branch_count_items x WHERE x.count_id = b.id) AS pieces_m,"
        " (SELECT COALESCE(SUM(d.damaged_m), 0) FROM branch_count_damage d WHERE d.count_id = b.id) AS damaged_m FROM branch_counts b JOIN users c ON c.id = b.counted_by LEFT JOIN users a ON a.id = b.approved_by"
        " WHERE b.po_id = ? ORDER BY b.id", (po_id,))]


def review_lines(conn, user: User, count_id: int) -> list[dict]:
    """Any one person's numbers, for the home office."""
    require(user, "receiving.review")
    return _lines(conn, count_id)


def by_receiver(conn, user: User) -> list[dict]:
    """How each person's counts turned out, so a pattern shows. Once two people agree on a figure, anyone who counted something else on that product was wrong."""
    require(user, "receiving.review")
    rows = conn.execute(
        "SELECT u.id, u.full_name, u.username, ub.branch_code, br.name AS branch,"
        " (SELECT COUNT(*) FROM branch_counts b WHERE b.counted_by = u.id) AS counted,"
        " (SELECT COUNT(*) FROM branch_counts b WHERE b.counted_by = u.id AND b.status = 'MISMATCH') AS mismatched,"
        " (SELECT COUNT(*) FROM branch_count_items c JOIN branch_counts b ON b.id = c.count_id JOIN branch_received r ON r.po_id = b.po_id AND r.delivery_no = b.delivery_no AND r.attempt_no = b.attempt_no"
        "   AND r.po_item_id = c.po_item_id LEFT JOIN branch_count_damage d ON d.count_id = c.count_id AND d.po_item_id = c.po_item_id"
        "   WHERE b.counted_by = u.id AND (c.pieces_m <> r.pieces_m OR COALESCE(d.damaged_m, 0) <> r.damaged_m)) AS wrong,"
        " (SELECT COUNT(*) FROM branch_counts b WHERE b.counted_by = u.id AND b.status = 'REPLACED') AS replaced,"
        " (SELECT COUNT(*) FROM branch_counts b WHERE b.counted_by = u.id AND (b.round_no > 1 OR b.attempt_no > 1)) AS recounts"
        " FROM users u LEFT JOIN user_branches ub ON ub.user_id = u.id LEFT JOIN branches br ON br.code = ub.branch_code"
        " WHERE EXISTS (SELECT 1 FROM branch_counts b WHERE b.counted_by = u.id) ORDER BY wrong DESC, mismatched DESC, u.full_name").fetchall()
    return [dict(r) for r in rows]


def signed_qty(m: int) -> str:
    return "0" if not m else ("+" if m > 0 else "-") + fmt_qty(abs(m))
