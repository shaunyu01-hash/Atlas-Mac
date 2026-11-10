"""Paying for a finished order: checking the supplier's DRs, working out what to pay and when, and recording the check.

An order comes here once every live PO has finished its delivery. When two receivers counted exactly what was ordered, at costs the supplier signed, it goes
straight to its check and nobody has to touch the DRs. Anything else waits for one press after someone compares the paper. Any single DR can still be corrected
until its check is recorded, and a total that doesn't match needs a written reason. The amount is the live POs less debit memos, claim stubs, any shortage
nobody charged, stickers on pieces received and the term discount. The check falls on the first Saturday on or after the terms, counted from the day the
supplier delivered to the Central Warehouse, the last delivery there for the order. An order from before the warehouse goes by its DR date, or the last agreed
count's date for a DR nobody typed. One check pays everything a supplier is due that day.

For now the real checks are printed outside the program, and their numbers are recorded here. A check number is never used twice, and an order with a live check can't be
paid again. So printing a check again always means cancelling the first one, which puts its orders back on the list.
"""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal

from . import delivery_receipts, order_forms, receiving
from .auth import User, require
from .db import POError, audit, timestamp, today, tx
from .debit_memos import SHORT, amount_c, discount_share, shown_cost
from .money import fmt_peso, fmt_qty, m_to_qty, parse_pesos, parse_qty, round_centavos, sticker_deduction

ENTER_DRS, DIFFERENCE, SIGN_OFF, TO_PAY, PAID, NOT_DONE = "ENTER_DRS", "DIFFERENCE", "SIGN_OFF", "TO_PAY", "PAID", "NOT_DONE"
# A shortage nobody put on a debit memo. The supplier gets a debit memo for anything short, so the order waits for one before it's paid.
DM_FIRST = "DM_FIRST"
ACTIVE, CANCELLED = "ACTIVE", "CANCELLED"
FUTURE, DUE, CLAIMED, CLEARED = "FUTURE", "DUE", "CLAIMED", "CLEARED"
TOLERANCE_C = 100  # Within a peso counts as the same.
SATURDAY = 5
# Terms the program can't turn into a check date, say 3,000,000 days typed by mistake. The order waits on the list instead of breaking it.
TERMS = "TERMS"
# Check numbers are digits. 20 is far longer than any paper check, and stops a typo that would break the next number.
MAX_CHECK_DIGITS = 20
# No check or DR comes near a hundred billion pesos. Anything bigger is a typo, and is refused before it reaches the database.
MAX_AMOUNT_C = 10**13
CREDIT_PREFIX = "CREDIT-"


class CheckDifference(POError):
    """The check's amount isn't what the program worked out. It can still be recorded, with a reason."""


# While a screen fills, each order's figures and each supplier's balance are worked out once. Nothing is written meanwhile, so they can't go stale.
_look: dict | None = None


@contextmanager
def one_look(conn):
    """Everything read inside this block shares one working-out of the figures. Only for reading: nothing may be saved inside it.

    The shared figures live in one module-level variable, which is safe only because the program has no threads and nothing is
    saved during a refresh. Never call a save from inside this block (or wrap a save in it): figures worked out before the save
    would be reused after it."""
    global _look
    if _look is not None:
        yield
        return
    _look = {"conn": conn, "settlement": {}, "balance": {}}
    try:
        yield
    finally:
        _look = None


def _memo(conn, kind: str) -> dict | None:
    return _look[kind] if _look is not None and _look["conn"] is conn else None


def _may_view(user: User | None) -> None:
    """A screen passes its login, so only people who may see payments get the figures. Called without a login, it's the program itself asking."""
    if user is not None:
        require(user, "payment.view")


def _day(text, what: str = "") -> str:
    """A real date, as YYYY-MM-DD. 20261101 is taken as 2026-11-01. Anything else is refused, so dates always compare correctly."""
    value = str(text if text is not None else "").strip()
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{8}", value):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            pass
    raise POError(f"{what}\"{value}\" isn't a date.")


def _amount(text, what: str = "") -> int:
    """An amount typed in pesos, in centavos. Refuses what isn't an amount, and anything too large to be real."""
    try:
        value = parse_pesos(text)
    except (ValueError, ArithmeticError):
        raise POError(f"{what}\"{text}\" isn't an amount.") from None
    if abs(value) >= MAX_AMOUNT_C:
        raise POError(f"{what}{fmt_peso(value)} is too large. Check the amount.")
    return value


def _check_number(number: str) -> str:
    number = str(number or "").strip()
    if not number:
        raise POError("Type the check number.")
    if number.upper().startswith(CREDIT_PREFIX):
        raise POError(f"Check numbers starting with {CREDIT_PREFIX} are kept for orders settled by the supplier's credit.")
    if not re.fullmatch(r"[0-9]+", number):
        raise POError(f"Check number \"{number}\" has to be digits only.")
    if len(number) > MAX_CHECK_DIGITS:
        raise POError(f"Check number {number[:12]}... is too long. A check number has at most {MAX_CHECK_DIGITS} digits.")
    return number


def _ask_signoff(conn, user: User, kind: str, ref_id: int, amount_c: int, of_id: int | None = None) -> None:
    """Accounting does the payment, and Admin or management gives the final check on an amount that differs. Only Admin's own is signed already:
    a login that is both Accounting and an Approver still needs someone else, so nobody signs off their own figures. Either way it's in the log."""
    signed = "ADMIN" in user.roles
    conn.execute("INSERT INTO payment_signoffs(kind, ref_id, amount_c, requested_by, requested_at, signed_by, signed_at) VALUES (?,?,?,?,?,?,?)",
                 (kind, ref_id, amount_c, user.id, timestamp(), user.id if signed else None, timestamp() if signed else None))
    entity = "payment_check" if kind == "CHECK" else "order_form"
    if signed:
        audit(conn, user, "PAYMENT_SIGNOFF", entity, ref_id, of_id=of_id, summary=f"Signed off by Admin when recorded, {fmt_peso(amount_c)}")
    else:
        audit(conn, user, "PAYMENT_SIGNOFF_ASKED", entity, ref_id, of_id=of_id, summary=f"Waiting for Admin or management to sign off, {fmt_peso(amount_c)}")


def waiting_signoffs(conn, rows: list[dict] | None = None) -> int:
    """How many differences and checks wait for Admin or management, so the sidebar can say so. rows are orders_in_payment() if already worked out."""
    stages = {r["id"]: r["stage"] for r in rows} if rows is not None else None
    # A check's status comes in the same query, so the badge needs no query per check.
    asked = conn.execute("SELECT s.kind, s.ref_id, k.status AS check_status FROM payment_signoffs s LEFT JOIN payment_checks k ON s.kind = 'CHECK' AND k.id = s.ref_id"
                         " WHERE s.signed_by IS NULL AND s.id = (SELECT MAX(t.id) FROM payment_signoffs t WHERE t.kind = s.kind AND t.ref_id = s.ref_id)").fetchall()
    n = 0
    for r in asked:
        if r["kind"] == "CHECK":
            n += r["check_status"] == ACTIVE
        elif stages is not None and r["ref_id"] in stages:
            n += stages[r["ref_id"]] == SIGN_OFF
        else:
            n += not _active_check(conn, r["ref_id"]) and order_row(conn, r["ref_id"])["stage"] == SIGN_OFF
    return n


def signoff(conn, kind: str, ref_id: int):
    """The latest sign-off asked for on this, or None."""
    return conn.execute("SELECT s.*, u.full_name AS signed_name, r.full_name AS requested_name FROM payment_signoffs s LEFT JOIN users u ON u.id = s.signed_by JOIN users r ON r.id = s.requested_by"
                        " WHERE s.kind = ? AND s.ref_id = ? ORDER BY s.id DESC LIMIT 1", (kind, ref_id)).fetchone()


def _sign(conn, user: User, kind: str, ref_id: int, what: str, of_id: int | None = None) -> None:
    require(user, "payment.signoff")
    with tx(conn):
        row = signoff(conn, kind, ref_id)
        if row is None or row["signed_by"] is not None:
            raise POError(f"{what} isn't waiting for a sign-off.")
        if row["requested_by"] == user.id:
            raise POError("Someone else has to sign off what you did yourself.")
        conn.execute("UPDATE payment_signoffs SET signed_by = ?, signed_at = ? WHERE id = ?", (user.id, timestamp(), row["id"]))
        audit(conn, user, "PAYMENT_SIGNOFF", "payment_check" if kind == "CHECK" else "order_form", ref_id, of_id=of_id, summary=f"{what} signed off, {fmt_peso(row['amount_c'])}")


def sign_off_difference(conn, user: User, of_id: int) -> None:
    """Admin or management agrees with the DR difference Accounting accepted, so the order can be paid."""
    row = order_row(conn, of_id)
    if row["stage"] != SIGN_OFF:
        raise POError(f"{row['of_number']} isn't waiting for a sign-off.")
    # Signing off says the DRs are right against what came, so there has to be a count to check them against.
    uncounted = [p["po_number"] for p in row["pos"] if not p["counted"]]
    if uncounted:
        raise POError(f"{row['of_number']} can't be signed off: {', '.join(uncounted)} has no delivery count to check the DRs against.")
    _sign(conn, user, "DIFFERENCE", of_id, f"The DR difference on {row['of_number']}", of_id)


def sign_off_check(conn, user: User, check_id: int) -> None:
    """Admin or management agrees with a check Accounting recorded at a different amount, so it can be handed to the supplier."""
    require(user, "payment.signoff")
    with tx(conn):
        check = conn.execute("SELECT check_number, status FROM payment_checks WHERE id = ?", (check_id,)).fetchone()
        if check is None:
            raise POError("That check no longer exists.")
        if check["status"] != ACTIVE:
            raise POError("This check was cancelled.")
        _sign(conn, user, "CHECK", check_id, f"Check {check['check_number']}")


def check_waiting(conn, check_id: int) -> bool:
    """Recorded at a different amount and not signed off yet, so it can't go to the supplier."""
    row = signoff(conn, "CHECK", check_id)
    return row is not None and row["signed_by"] is None


def next_saturday(day: date) -> date:
    return day + timedelta(days=(SATURDAY - day.weekday()) % 7)


def terms_days(terms: str) -> int | None:
    """Days from terms written the standard way. None for anything typed by hand, such as COD or a down payment, because the program can't read those."""
    match = re.fullmatch(r"(\d+)D", terms or "")
    if match:
        return int(match.group(1))
    return 0 if terms in ("", "None") else None


def charges(conn, of_id: int) -> list:
    """Debit memos and claim stubs that come off this order. Cancelled ones don't."""
    return conn.execute("SELECT 'Debit memo' AS kind, d.dm_number AS number, d.total_c, d.po_id FROM debit_memos d JOIN purchase_orders p ON p.id = d.po_id WHERE p.of_id = ? AND d.status = 'ISSUED' AND p.status <> 'CANCELLED'"
                        " UNION ALL SELECT 'Claim stub', c.cs_number, c.total_c, c.po_id FROM claim_stubs c JOIN purchase_orders p ON p.id = c.po_id WHERE p.of_id = ? AND c.status <> 'CANCELLED' AND p.status <> 'CANCELLED' ORDER BY 1 DESC, 2",
                        (of_id, of_id)).fetchall()


def _po_lines(conn, po_id: int) -> tuple[bool, list[dict]]:
    """A PO's products, with what each came to and what's short, on a debit memo or not.
    A closed PO issued before receiving was in use, or received in full outside the program by Admin, has no count to go by, so it's taken as delivered.
    Any other PO with nothing agreed is short in full."""
    counted = conn.execute("SELECT 1 FROM branch_counts WHERE po_id = ?", (po_id,)).fetchone() is not None
    taken_as_delivered = receiving.taken_as_delivered(conn, po_id)
    items = {r["id"]: r for r in conn.execute("SELECT id, net_unit_c, pieces_per_unit FROM purchase_order_items WHERE po_id = ?", (po_id,))}
    charged = receiving.dm_pieces(conn, po_id)
    paid_back = _paid_back_pieces(conn, po_id)
    # Shortages are valued at what the company pays after the PO's overall discount, the same as the debit memo charges them.
    share = discount_share(conn, po_id)
    lines = []
    for line in receiving.lines_of(conn, po_id):
        item = items[line["id"]]
        short = 0 if taken_as_delivered else max(line["ordered_m"] - line["supplier_m"], 0)
        on_memo = min(charged.get((line["id"], SHORT), 0), short)
        # Pieces on a debit memo cancelled after the order was paid are paid back, so they no longer count as short with no
        # debit memo. Only up to what is still short: pieces delivered since are paid as delivered, never twice.
        back = min(paid_back.get(line["id"], 0), short - on_memo)
        short_dm_c = amount_c(on_memo, item["net_unit_c"], item["pieces_per_unit"], share)
        paid_back_c = amount_c(back, item["net_unit_c"], item["pieces_per_unit"], share)
        unclaimed_c = amount_c(short - on_memo, item["net_unit_c"], item["pieces_per_unit"], share) - paid_back_c
        order_c = amount_c(line["ordered_m"], item["net_unit_c"], item["pieces_per_unit"], share)
        lines.append({**line, "order_c": order_c, "short_dm_c": short_dm_c, "unclaimed_c": unclaimed_c, "paid_back_c": paid_back_c, "delivered_c": order_c - short_dm_c - unclaimed_c - paid_back_c,
                      "net_unit_c": item["net_unit_c"], "pieces_per_unit": item["pieces_per_unit"], "share": share,
                      "sticker_m": line["ordered_m"] if taken_as_delivered else min(line["supplier_m"], line["ordered_m"])})
    return counted, lines


def _paid_back_pieces(conn, po_id: int) -> dict[int, int]:
    """Short pieces on this PO's debit memos that were cancelled after the order was paid, by product."""
    return {r[0]: r[1] for r in conn.execute(
        "SELECT i.po_item_id, SUM(i.pieces_m) FROM debit_memo_items i JOIN debit_memos d ON d.id = i.dm_id"
        " WHERE d.po_id = ? AND d.status = 'CANCELLED' AND d.paid_back_check_id IS NOT NULL AND i.reason = ? GROUP BY i.po_item_id", (po_id, SHORT))}


def settlement(conn, of_id: int) -> dict:
    """What this order comes to now. Pieces beyond the order are returned, so they're never paid for or stickered."""
    memo = _memo(conn, "settlement")
    if memo is None:
        return _settlement(conn, of_id)
    if of_id not in memo:
        memo[of_id] = _settlement(conn, of_id)
    return memo[of_id]


def _settlement(conn, of_id: int) -> dict:
    of = order_forms.get_of(conn, of_id)
    live = [p for p in of["pos"] if p["status"] != "CANCELLED"]
    charge_rows = charges(conn, of_id)
    pos, total_c, unclaimed_c, sticker_m, short_dm_c, paid_back_c = [], 0, 0, 0, 0, 0
    for p in live:
        counted, lines = _po_lines(conn, p["id"])
        po_short = sum(line["short_dm_c"] for line in lines)
        po_unclaimed = sum(line["unclaimed_c"] for line in lines)
        po_back = sum(line["paid_back_c"] for line in lines)
        sticker_m += sum(line["sticker_m"] for line in lines)
        total_c += p["grand_total_c"]
        unclaimed_c += po_unclaimed
        short_dm_c += po_short
        paid_back_c += po_back
        pos.append({"po_id": p["id"], "po_number": p["po_number"], "branch": p["branch_name"], "branch_code": p["branch_code"], "total_c": p["grand_total_c"], "delivered_c": p["grand_total_c"] - po_short - po_unclaimed - po_back, "unclaimed_c": po_unclaimed,
                    "dm_c": sum(c["total_c"] for c in charge_rows if c["kind"] == "Debit memo" and c["po_id"] == p["id"]), "cs_c": sum(c["total_c"] for c in charge_rows if c["kind"] == "Claim stub" and c["po_id"] == p["id"]), "counted": counted, "lines": lines})
    charged_c = sum(c["total_c"] for c in charge_rows)
    # What was paid back is still short, so the term discount and stickers stay as they were: only the memo stops coming off.
    delivered_c = total_c - short_dm_c - unclaimed_c - paid_back_c
    sticker_c = sticker_deduction(m_to_qty(sticker_m), of["sticker_rate_c"])
    # On the merchandise kept: after shortages and claim stubs, without freight or VAT added on top.
    cs_c = sum(po["cs_c"] for po in pos)
    term_c = round_centavos(Decimal(order_forms.term_base(delivered_c, sum(p["other_charges_c"] for p in live), sum(p["vat_c"] for p in live if p["vat_mode"] == "EXCLUSIVE"), cs_c)) * of["term_discount_bp"] / 10000)
    dr_lower_c = _dr_lower(conn, of_id, pos)
    pay_c = total_c - charged_c - unclaimed_c - sticker_c - term_c - dr_lower_c
    return {"total_c": total_c, "charges": charge_rows, "charged_c": charged_c, "dm_c": sum(po["dm_c"] for po in pos), "cs_c": cs_c, "unclaimed_c": unclaimed_c, "delivered_c": delivered_c, "sticker_m": sticker_m, "sticker_c": sticker_c, "term_c": term_c,
            "paid_back_c": paid_back_c, "paid_back": _paid_back_rows(conn, of_id) if paid_back_c else [], "dr_lower_c": dr_lower_c,
            "pay_c": pay_c, "signed_c": of["net_payable_c"], "pos": pos, "cancelled_pos": [{"po_number": p["po_number"], "branch": p["branch_name"], "branch_code": p["branch_code"]} for p in of["pos"] if p["status"] == "CANCELLED"],
            # What the supplier signed still stands only if nothing about the order changed.
            "changed": pay_c != of["net_payable_c"] or len(live) != len(of["pos"])}


def _typed_by_po(conn, of_id: int) -> dict[int, int]:
    """The supplier's typed DR totals, added up per live PO."""
    return {r[0]: r[1] for r in conn.execute(
        "SELECT d.po_id, SUM(e.total_c) FROM dr_entries e JOIN delivery_receipts d ON d.id = e.dr_id JOIN purchase_orders p ON p.id = d.po_id"
        " WHERE p.of_id = ? AND p.status <> 'CANCELLED' GROUP BY d.po_id", (of_id,))}


def _lower_part(typed: dict[int, int], pos: list[dict]) -> int:
    """How much lower than what each PO came to its typed DRs are, beyond a peso, added up. A PO whose DRs are higher adds nothing."""
    return sum(p["delivered_c"] - typed[p["po_id"]] for p in pos if p["po_id"] in typed and p["delivered_c"] - typed[p["po_id"]] > TOLERANCE_C)


def _dr_lower(conn, of_id: int, pos: list[dict]) -> int:
    """A supplier's DR lower than the program's figure is paid at the DR's amount at once, with no acceptance and no sign-off.
    The products typed on the DR show why, and the DR's save is in the log. Read from the DRs as they stand, so a DR corrected later pays what it says now.
    Higher DRs keep today's rule: Accounting accepts, Admin or management signs off, and the program's lower figure is paid.
    Once a check pays the order, the lower part it paid stays (kept when the check is recorded), so pieces counted later are paid as delivered and never
    taken off again. A difference accepted before version 28 has none kept, so it's read from the DRs as they stand."""
    if _active_check(conn, of_id):
        row = conn.execute("SELECT dr_lower_c FROM payment_differences WHERE of_id = ?", (of_id,)).fetchone()
        if row is None:
            return 0
        if row["dr_lower_c"] is not None:
            return row["dr_lower_c"]
    return _lower_part(_typed_by_po(conn, of_id), pos)


def _paid_back_rows(conn, of_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT d.dm_number, d.total_c, d.cancel_reason FROM debit_memos d JOIN purchase_orders p ON p.id = d.po_id"
        " WHERE p.of_id = ? AND d.status = 'CANCELLED' AND d.paid_back_check_id IS NOT NULL ORDER BY d.dm_number", (of_id,))]


def needs_final_of(conn, of_id: int) -> bool:
    """A clean delivery needs no second paper: the signed Warehouse OF already says what's paid."""
    return receiving.of_delivery_complete(conn, of_id) and settlement(conn, of_id)["changed"]


def final_of_due(conn, of_id: int, money: dict) -> bool:
    """A Final OF is needed and the latest one printed doesn't show what's paid now, so a deduction made after printing asks for another."""
    if not money["changed"]:
        return False
    latest = conn.execute("SELECT new_value FROM audit_logs WHERE of_id = ? AND action = 'OF_PRINT_OFFICE' ORDER BY id DESC LIMIT 1", (of_id,)).fetchone()
    return not (latest and latest[0] and json.loads(latest[0]).get("pay_c") == money["pay_c"])


def _receipts(conn, of_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT d.id, d.dr_number, d.po_id, d.delivery_no, p.po_number, p.branch_name AS branch, e.dr_date, e.total_c FROM delivery_receipts d JOIN purchase_orders p ON p.id = d.po_id"
        " LEFT JOIN dr_entries e ON e.dr_id = d.id WHERE p.of_id = ? AND p.status <> 'CANCELLED' ORDER BY p.branch_code, d.delivery_no", (of_id,))]


def _last_count_day(conn, po_id: int) -> str:
    """When this PO's delivery was last agreed. A PO closed before receiving was in use goes by when it was closed."""
    counted = conn.execute("SELECT MAX(COALESCE(approved_at, counted_at)) FROM branch_counts WHERE po_id = ? AND status = 'APPROVED'", (po_id,)).fetchone()[0]
    if counted:
        return counted[:10]
    po = conn.execute("SELECT closed_at, issued_at, po_date FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
    return (po["closed_at"] or po["issued_at"] or po["po_date"])[:10]


def central_day(conn, po_id: int) -> str | None:
    """When the supplier delivered this PO to the Central Warehouse: the last delivery received there. The terms start that day.
    None for a PO that never went through the warehouse, such as one printed before the warehouse started."""
    row = conn.execute("SELECT MAX(received_at) FROM warehouse_deliveries WHERE po_id = ?", (po_id,)).fetchone()
    return row[0][:10] if row and row[0] else None


def counted_by_branches(conn, of_id: int) -> bool:
    """Every live PO under this OF has a delivery the branch's two receivers agreed on. Nothing is paid before the branches counted it,
    so an order closed before counting began, or received outside the program, is never paid here. Those are paid outside the program."""
    return conn.execute(
        "SELECT NOT EXISTS (SELECT 1 FROM purchase_orders p WHERE p.of_id = ? AND p.status <> 'CANCELLED'"
        " AND NOT EXISTS (SELECT 1 FROM branch_counts b WHERE b.po_id = p.id AND b.status = 'APPROVED'))"
        " AND EXISTS (SELECT 1 FROM purchase_orders p WHERE p.of_id = ? AND p.status <> 'CANCELLED')", (of_id, of_id)).fetchone()[0] == 1


def receipts_to_enter(conn, user: User, of_id: int) -> list[dict]:
    """The order's DRs, each with a date to start from. A PO issued before DRs existed gets its DR now, so its paper can still be entered."""
    require(user, "payment.edit")
    with tx(conn):
        for p in conn.execute("SELECT id FROM purchase_orders WHERE of_id = ? AND status <> 'CANCELLED'", (of_id,)).fetchall():
            delivery_receipts.ensure_first(conn, user, p["id"])
    return [d | {"suggested_date": _last_count_day(conn, d["po_id"])} for d in _receipts(conn, of_id)]


def _active_check(conn, of_id: int):
    return conn.execute("SELECT c.* FROM payment_checks c JOIN payment_check_ofs o ON o.check_id = c.id WHERE o.of_id = ? AND c.status = ?", (of_id, ACTIVE)).fetchone()


def save_dr_entries(conn, user: User, of_id: int, entries: dict[int, tuple[str | None, str]]) -> None:
    """entries is {DR id: (date on the DR, the supplier's total as typed)}. A DR with no total is left off, so only the one that differs needs typing.
    The Payments screen types the products that differ instead (save_dr_lines), and works the total out from them. This stays for a total
    typed on its own, such as DRs match filling in the program's figure."""
    require(user, "payment.edit")
    with tx(conn):
        if _active_check(conn, of_id):
            raise POError("This order already has a check. Cancel the check before changing its DRs.")
        if not receiving.of_delivery_complete(conn, of_id):
            raise POError("This order isn't fully delivered and settled yet, so its DRs can't be entered.")
        known = {d["id"]: d for d in _receipts(conn, of_id)}
        for dr_id, (day, text) in entries.items():
            receipt = known.get(dr_id)
            if receipt is None:
                raise POError("That DR isn't on this order.")
            text = str(text or "").strip()
            if not text:
                if receipt["total_c"] is not None:
                    conn.execute("DELETE FROM dr_entries WHERE dr_id = ?", (dr_id,))
                    _end_lines(conn, user, dr_id)
                    audit(conn, user, "DR_ENTRY_CLEARED", "delivery_receipt", dr_id, po_id=receipt["po_id"], of_id=of_id,
                          summary=f"{receipt['dr_number']}: typed total cleared, so it's taken as matching", previous={"date": receipt["dr_date"], "total_c": receipt["total_c"]})
                continue
            if not day:
                raise POError(f"{receipt['dr_number']}: type the date on the DR.")
            day = _day(day, f"{receipt['dr_number']}: ")
            if date.fromisoformat(day) > today():
                raise POError(f"{receipt['dr_number']}: the date on the DR can't be in the future.")
            total = _amount(text, f"{receipt['dr_number']}: ")
            if total < 0:
                raise POError(f"{receipt['dr_number']}: the supplier's total can't be below nothing.")
            if (receipt["dr_date"], receipt["total_c"]) == (day, total):
                continue
            conn.execute("INSERT INTO dr_entries(dr_id, dr_date, total_c, entered_by, entered_at) VALUES (?,?,?,?,?) ON CONFLICT(dr_id) DO UPDATE SET dr_date = excluded.dr_date, total_c = excluded.total_c,"
                         " entered_by = excluded.entered_by, entered_at = excluded.entered_at", (dr_id, day, total, user.id, timestamp()))
            # A total typed on its own replaces any product differences saved for this DR before.
            if total != receipt["total_c"]:
                _end_lines(conn, user, dr_id)
            audit(conn, user, "DR_ENTRY", "delivery_receipt", dr_id, po_id=receipt["po_id"], of_id=of_id, summary=f"{receipt['dr_number']} dated {day}, supplier's total {fmt_peso(total)}",
                  previous={"date": receipt["dr_date"], "total_c": receipt["total_c"]}, new={"date": day, "total_c": total})


# ---- A DR's products. Pick the DR that differs, and on each product that differs type what the supplier's DR says, pieces
# and/or unit price. The DR's total is worked out from them: a product left blank is the same as ours. Kept product by product, so the
# difference and its sign-off show which products differ and by how much.

def current_lines(conn, dr_id: int) -> dict[int, dict]:
    """What the supplier's DR says on each product that differs, as last saved: {PO item id: {pieces_m, unit_c}}. None in either means the same as ours."""
    save_no = conn.execute("SELECT MAX(save_no) FROM dr_line_differences WHERE dr_id = ?", (dr_id,)).fetchone()[0]
    if save_no is None:
        return {}
    return {r["po_item_id"]: {"pieces_m": r["pieces_m"], "unit_c": r["unit_c"]} for r in conn.execute(
        "SELECT po_item_id, pieces_m, unit_c FROM dr_line_differences WHERE dr_id = ? AND save_no = ? AND po_item_id IS NOT NULL", (dr_id, save_no))}


def _write_lines(conn, user: User, dr_id: int, diffs: list[tuple[int, int | None, int | None]]) -> None:
    """A new numbered set for the DR. Nothing saved before is changed: the newest set is what counts. No differences writes one empty row."""
    save_no = (conn.execute("SELECT MAX(save_no) FROM dr_line_differences WHERE dr_id = ?", (dr_id,)).fetchone()[0] or 0) + 1
    stamp = timestamp()
    conn.executemany("INSERT INTO dr_line_differences(dr_id, save_no, po_item_id, pieces_m, unit_c, created_by, created_at) VALUES (?,?,?,?,?,?,?)",
                     [(dr_id, save_no, item, pieces, unit, user.id, stamp) for item, pieces, unit in (diffs or [(None, None, None)])])


def _end_lines(conn, user: User, dr_id: int) -> None:
    if current_lines(conn, dr_id):
        _write_lines(conn, user, dr_id, [])


def line_amount(line: dict, pieces_m: int | None, unit_c: int | None) -> int:
    """A product's amount on the supplier's DR. Whatever isn't given is ours. A unit price is per unit as ordered (a dozen for DOZ),
    after the PO's overall discount like the unit cost shown beside it."""
    pieces = line["pieces_m"] if pieces_m is None else pieces_m
    if unit_c is None:
        return amount_c(pieces, line["net_unit_c"], line["pieces_per_unit"], line["share"])
    return round_centavos(Decimal(pieces) * unit_c / (line["pieces_per_unit"] * 1000))


def _product_text(line: dict) -> str:
    return ", ".join(part for part in (line["description"], line["size"], line["color"]) if part)


def _dr_lines(conn, of_id: int, receipt: dict) -> dict:
    money = settlement(conn, of_id)
    po = next((p for p in money["pos"] if p["po_id"] == receipt["po_id"]), None)
    if po is None:
        raise POError("That DR isn't on this order.")
    saved = current_lines(conn, receipt["id"])
    # The OF line each product came from, so the same product can be found on the order's other DRs.
    of_items = {r["id"]: r["of_item_id"] for r in conn.execute("SELECT id, of_item_id FROM purchase_order_items WHERE po_id = ?", (receipt["po_id"],))}
    lines = []
    for line in po["lines"]:
        # The pieces paid for: what the branch counted, up to what was ordered. Pieces beyond the order go back on an item pass.
        ours = {"po_item_id": line["id"], "of_item_id": of_items.get(line["id"]), "item_code": line["item_code"], "product": _product_text(line), "uom": line["uom"], "pieces_m": line["sticker_m"],
                "net_unit_c": line["net_unit_c"], "pieces_per_unit": line["pieces_per_unit"], "share": line["share"], "unit_c": shown_cost(line["net_unit_c"], line["share"])}
        ours["amount_c"] = line_amount(ours, None, None)
        said = saved.get(line["id"], {})
        ours["dr_pieces_m"], ours["dr_unit_c"] = said.get("pieces_m"), said.get("unit_c")
        ours["dr_amount_c"] = line_amount(ours, ours["dr_pieces_m"], ours["dr_unit_c"])
        lines.append(ours)
    # What this DR stands for at our figures: the PO's delivery, less any total typed on the PO's other DRs.
    others = conn.execute("SELECT COALESCE(SUM(e.total_c), 0) FROM dr_entries e JOIN delivery_receipts d ON d.id = e.dr_id WHERE d.po_id = ? AND d.id <> ?",
                          (receipt["po_id"], receipt["id"])).fetchone()[0]
    return {"dr": receipt, "base_c": po["delivered_c"] - others, "lines": lines}


def dr_total(info: dict, diffs: dict[int, tuple[int | None, int | None]]) -> int:
    """The supplier's total for a DR: ours, plus what each product that differs comes to on their DR less what it comes to on ours."""
    by_id = {line["po_item_id"]: line for line in info["lines"]}
    return info["base_c"] + sum(line_amount(by_id[item], pieces, unit) - by_id[item]["amount_c"] for item, (pieces, unit) in diffs.items())


def dr_lines(conn, user: User, of_id: int, dr_id: int) -> dict:
    """One DR's products for the screen: SKU, product, pieces the branch counted, unit cost and amount at the order's cost, and what the supplier's DR
    says on any product that differs, as saved. base_c is what the DR comes to at our figures."""
    require(user, "payment.edit")
    receipt = next((d for d in _receipts(conn, of_id) if d["id"] == dr_id), None)
    if receipt is None:
        raise POError("That DR isn't on this order.")
    return _dr_lines(conn, of_id, receipt)


def save_dr_lines(conn, user: User, of_id: int, entries: dict[int, tuple[str | None, dict[int, tuple[str, str]]]]) -> None:
    """entries is {DR id: (date on the DR, {PO item id: (pieces on their DR, unit price on their DR)})}, as typed. Blank is the same as ours.
    The DR's total is worked out from its products. A DR whose products are all the same again is taken as matching, as a blank total always was."""
    require(user, "payment.edit")
    with tx(conn):
        if _active_check(conn, of_id):
            raise POError("This order already has a check. Cancel the check before changing its DRs.")
        if not receiving.of_delivery_complete(conn, of_id):
            raise POError("This order isn't fully delivered and settled yet, so its DRs can't be entered.")
        known = {d["id"]: d for d in _receipts(conn, of_id)}
        for dr_id, (day, typed) in entries.items():
            receipt = known.get(dr_id)
            if receipt is None:
                raise POError("That DR isn't on this order.")
            info = _dr_lines(conn, of_id, receipt)
            by_id = {line["po_item_id"]: line for line in info["lines"]}
            if set(typed) - set(by_id):
                raise POError(f"{receipt['dr_number']}: that product isn't on this DR.")
            diffs: dict[int, tuple[int | None, int | None]] = {}
            for item, (pieces_text, unit_text) in typed.items():
                line = by_id[item]
                name = f"{receipt['dr_number']}, {line['item_code'] or line['product']}: "
                pieces_text, unit_text = str(pieces_text or "").strip(), str(unit_text or "").strip()
                try:
                    pieces = parse_qty(pieces_text, "PC") if pieces_text else None
                except ValueError:
                    raise POError(f"{name}\"{pieces_text}\" isn't a number of pieces.") from None
                unit = _amount(unit_text, name) if unit_text else None
                if unit is not None and unit < 0:
                    raise POError(f"{name}the unit price can't be below nothing.")
                pieces = None if pieces == line["pieces_m"] else pieces
                unit = None if unit == line["unit_c"] else unit
                if pieces is not None or unit is not None:
                    diffs[item] = (pieces, unit)
            before = {k: (v["pieces_m"], v["unit_c"]) for k, v in current_lines(conn, dr_id).items()}
            if day:
                day = _day(day, f"{receipt['dr_number']}: ")
                if date.fromisoformat(day) > today():
                    raise POError(f"{receipt['dr_number']}: the date on the DR can't be in the future.")
            if not diffs:
                if before:
                    _write_lines(conn, user, dr_id, [])
                    if receipt["total_c"] is not None:
                        conn.execute("DELETE FROM dr_entries WHERE dr_id = ?", (dr_id,))
                    audit(conn, user, "DR_ENTRY_CLEARED", "delivery_receipt", dr_id, po_id=receipt["po_id"], of_id=of_id,
                          summary=f"{receipt['dr_number']}: no product differs any more, so it's taken as matching", previous={"date": receipt["dr_date"], "total_c": receipt["total_c"]})
                elif receipt["total_c"] is not None and day and day != receipt["dr_date"]:
                    conn.execute("UPDATE dr_entries SET dr_date = ?, entered_by = ?, entered_at = ? WHERE dr_id = ?", (day, user.id, timestamp(), dr_id))
                    audit(conn, user, "DR_ENTRY", "delivery_receipt", dr_id, po_id=receipt["po_id"], of_id=of_id, summary=f"{receipt['dr_number']} dated {day}",
                          previous={"date": receipt["dr_date"], "total_c": receipt["total_c"]}, new={"date": day, "total_c": receipt["total_c"]})
                continue
            if not day:
                raise POError(f"{receipt['dr_number']}: type the date on the DR.")
            total = dr_total(info, diffs)
            if total < 0:
                raise POError(f"{receipt['dr_number']}: the supplier's total can't be below nothing.")
            if total >= MAX_AMOUNT_C:
                raise POError(f"{receipt['dr_number']}: {fmt_peso(total)} is too large. Check the amounts.")
            if diffs == before and (receipt["dr_date"], receipt["total_c"]) == (day, total):
                continue
            conn.execute("INSERT INTO dr_entries(dr_id, dr_date, total_c, entered_by, entered_at) VALUES (?,?,?,?,?) ON CONFLICT(dr_id) DO UPDATE SET dr_date = excluded.dr_date, total_c = excluded.total_c,"
                         " entered_by = excluded.entered_by, entered_at = excluded.entered_at", (dr_id, day, total, user.id, timestamp()))
            if diffs != before:
                _write_lines(conn, user, dr_id, [(item, pieces, unit) for item, (pieces, unit) in sorted(diffs.items())])
            said = [_said(by_id[item], pieces, unit) for item, (pieces, unit) in sorted(diffs.items())]
            audit(conn, user, "DR_LINES", "delivery_receipt", dr_id, po_id=receipt["po_id"], of_id=of_id,
                  summary=f"{receipt['dr_number']} dated {day}, supplier's total {fmt_peso(total)}: " + "; ".join(said),
                  previous={"date": receipt["dr_date"], "total_c": receipt["total_c"], "lines": {str(k): list(v) for k, v in before.items()}},
                  new={"date": day, "total_c": total, "lines": {str(k): list(v) for k, v in diffs.items()}})


def _said(line: dict, pieces: int | None, unit: int | None) -> str:
    """One product that differs, in words: what their DR says beside ours."""
    parts = []
    if pieces is not None:
        parts.append(f"{fmt_qty(pieces)} pcs (ours {fmt_qty(line['pieces_m'])})")
    if unit is not None:
        parts.append(f"{fmt_peso(unit)} a unit (ours {fmt_peso(line['unit_c'])})")
    off = line_amount(line, pieces, unit) - line["amount_c"]
    return f"{line['item_code'] or line['product']} " + ", ".join(parts) + f", {'+' if off > 0 else ''}{fmt_peso(off)}"


def line_differences(conn, of_id: int, pos: list[dict] | None = None) -> dict[int, dict]:
    """Each product whose supplier's DR differs from ours, across the order's typed DRs: {PO item id: {dr_number, pieces and unit on both sides,
    both amounts, off_c}}. pos are settlement()['pos'] if already worked out."""
    rows = conn.execute(
        "SELECT l.po_item_id, l.pieces_m, l.unit_c, d.dr_number, d.po_id FROM dr_line_differences l JOIN delivery_receipts d ON d.id = l.dr_id JOIN purchase_orders p ON p.id = d.po_id"
        " JOIN dr_entries e ON e.dr_id = l.dr_id WHERE p.of_id = ? AND p.status <> 'CANCELLED' AND l.po_item_id IS NOT NULL"
        " AND l.save_no = (SELECT MAX(x.save_no) FROM dr_line_differences x WHERE x.dr_id = l.dr_id) ORDER BY l.id", (of_id,)).fetchall()
    if not rows:
        return {}
    lines = {line["id"]: line for p in (settlement(conn, of_id)["pos"] if pos is None else pos) for line in p["lines"]}
    out = {}
    for r in rows:
        line = lines.get(r["po_item_id"])
        if line is None:
            continue
        ours = {"pieces_m": line["sticker_m"], "net_unit_c": line["net_unit_c"], "pieces_per_unit": line["pieces_per_unit"], "share": line["share"],
                "unit_c": shown_cost(line["net_unit_c"], line["share"]), "item_code": line["item_code"], "product": _product_text(line)}
        ours["amount_c"] = line_amount(ours, None, None)
        dr_amount = line_amount(ours, r["pieces_m"], r["unit_c"])
        out[r["po_item_id"]] = {"dr_number": r["dr_number"], "po_id": r["po_id"], "item_code": line["item_code"], "product": ours["product"], "pieces_m": ours["pieces_m"], "unit_c": ours["unit_c"], "amount_c": ours["amount_c"],
                                "dr_pieces_m": ours["pieces_m"] if r["pieces_m"] is None else r["pieces_m"], "dr_unit_c": ours["unit_c"] if r["unit_c"] is None else r["unit_c"],
                                "dr_amount_c": dr_amount, "off_c": dr_amount - ours["amount_c"], "text": _said(ours, r["pieces_m"], r["unit_c"])}
    return out


def drs_match(conn, user: User, of_id: int) -> None:
    """Someone compared the paper DRs and they say what the screen says, so nobody has to type them. Their name is recorded."""
    require(user, "payment.edit")
    row = order_row(conn, of_id)
    if row["stage"] != ENTER_DRS:
        raise POError(f"{row['of_number']} isn't waiting for its DRs to be compared.")
    typed = {d["po_id"] for d in row["receipts"] if d["dr_date"]}
    entries = {}
    receipts = receipts_to_enter(conn, user, of_id)
    for p in row["pos"]:
        if p["po_id"] not in typed:
            first = next(d for d in receipts if d["po_id"] == p["po_id"])
            entries[first["id"]] = (first["suggested_date"], f"{p['delivered_c'] / 100:.2f}")
    with tx(conn):
        save_dr_entries(conn, user, of_id, entries)
        audit(conn, user, "DRS_MATCH", "order_form", of_id, of_id=of_id, summary=f"{row['of_number']}: DRs compared and they match the order")


def _why_to_compare(conn, of: dict, money: dict) -> list[str]:
    """What makes this order more than two people counting exactly what was ordered. Nothing here means nobody needs to look at its DRs."""
    why = []
    if money["charges"]:
        why.append("deductions")
    if any(p["status"] == "CANCELLED" for p in of["pos"]):
        why.append("PO cancelled")
    for p in of["pos"]:
        if p["status"] == "CANCELLED":
            continue
        if p["status"] == "CLOSED":
            why.append("PO closed")
        if p["revision"]:
            why.append("PO amended")
        if not conn.execute("SELECT 1 FROM branch_counts WHERE po_id = ?", (p["id"],)).fetchone():
            why.append("no count")
            continue
        lines = receiving.lines_of(conn, p["id"])
        if any(line["supplier_m"] < line["ordered_m"] for line in lines):
            why.append("short")
        if any(line["transit_m"] for line in lines):
            why.append("lost on the way to the branch")
        if any(line["damaged_m"] for line in lines):
            why.append("damaged")
        if receiving.excess_lines(conn, p["id"]):
            why.append("excess")
    return list(dict.fromkeys(why))


def dr_differs(conn, po_id: int) -> int | None:
    """How far the supplier's typed DRs for this PO are from what it came to, when it's more than a peso. None when they agree or nothing was typed."""
    po = conn.execute("SELECT of_id FROM purchase_orders WHERE id = ? AND status <> 'CANCELLED'", (po_id,)).fetchone()
    if po is None or po["of_id"] is None:
        return None
    typed = conn.execute("SELECT SUM(e.total_c) FROM dr_entries e JOIN delivery_receipts d ON d.id = e.dr_id WHERE d.po_id = ?", (po_id,)).fetchone()[0]
    if typed is None:
        return None
    line = next((p for p in settlement(conn, po["of_id"])["pos"] if p["po_id"] == po_id), None)
    if line is None:
        return None
    off = typed - line["delivered_c"]
    return off if abs(off) > TOLERANCE_C else None


def accept_difference(conn, user: User, of_id: int, reason: str) -> None:
    """The supplier's DRs don't add up to what the order says, and someone looked into it. If the figures change later, it has to be looked at again."""
    require(user, "payment.edit")
    if not reason.strip():
        raise POError("Write why this difference is acceptable.")
    row = order_row(conn, of_id)
    if row["stage"] != DIFFERENCE:
        raise POError(f"{row['of_number']} has no difference to accept.")
    # Only the DRs higher than ours are accepted and signed off. A lower DR is already paid as it says; its part is kept for the record.
    lower = _lower_part(_typed_by_po(conn, of_id), row["pos"])
    higher = row["higher_c"]
    with tx(conn):
        before = conn.execute("SELECT difference_c, reason, dr_lower_c FROM payment_differences WHERE of_id = ?", (of_id,)).fetchone()
        conn.execute("INSERT INTO payment_differences(of_id, difference_c, reason, accepted_by, accepted_at, dr_lower_c) VALUES (?,?,?,?,?,?) ON CONFLICT(of_id) DO UPDATE SET difference_c = excluded.difference_c,"
                     " reason = excluded.reason, accepted_by = excluded.accepted_by, accepted_at = excluded.accepted_at, dr_lower_c = excluded.dr_lower_c",
                     (of_id, higher, reason.strip(), user.id, timestamp(), lower))
        summary = f"DRs higher than the order by {fmt_peso(higher)}: {reason.strip()}. The order's figure is paid"
        if lower:
            summary += f"; DRs lower by {fmt_peso(lower)} are paid as they say"
        audit(conn, user, "PAYMENT_DIFFERENCE", "order_form", of_id, of_id=of_id, summary=summary,
              previous=dict(before) if before else None, new={"difference_c": higher, "reason": reason.strip(), "dr_lower_c": lower})
        _ask_signoff(conn, user, "DIFFERENCE", of_id, higher, of_id)


def order_row(conn, of_id: int, complete: bool | None = None, counted: bool | None = None) -> dict:
    """Where one finished order stands, with what the screen needs to say the next step. An order not fully delivered and settled can't go further.
    complete and counted are of_delivery_complete() and counted_by_branches() when the caller already knows them."""
    of = order_forms.get_of(conn, of_id)
    money = settlement(conn, of_id)
    receipts = _receipts(conn, of_id)
    entered = [d for d in receipts if d["dr_date"]]
    why = _why_to_compare(conn, of, money)
    live_pos = {p["po_id"] for p in money["pos"]}
    typed_pos = {d["po_id"] for d in entered}
    all_in = bool(live_pos) and live_pos <= typed_pos
    dr_total = sum(d["total_c"] for d in entered)
    # Only the DRs someone typed are compared, because the rest of a clean order is taken as matching.
    difference = dr_total - sum(p["delivered_c"] for p in money["pos"] if p["po_id"] in typed_pos)
    # Each PO is compared on its own, so a DR over at one branch can't hide one under at another.
    typed_by_po: dict[int, int] = {}
    for d in entered:
        typed_by_po[d["po_id"]] = typed_by_po.get(d["po_id"], 0) + d["total_c"]
    per_po = [typed_by_po[p["po_id"]] - p["delivered_c"] for p in money["pos"] if p["po_id"] in typed_by_po]
    off = sum(abs(v) for v in per_po if abs(v) > TOLERANCE_C)
    # Only DRs higher than ours wait for Accounting and a sign-off. A lower DR is simply paid as it says.
    # A difference accepted or signed off before this rule holds the whole gap, so it still counts while nothing has changed.
    higher = sum(v for v in per_po if v > TOLERANCE_C)
    lower = off - higher
    agreed = {higher, off}
    accepted = conn.execute("SELECT difference_c, reason FROM payment_differences WHERE of_id = ?", (of_id,)).fetchone()
    if all_in or not why:
        # The terms start when the supplier delivered to the Central Warehouse. Only a PO that never went through the warehouse goes by its DR date, or the day its count was agreed.
        starts = []
        for po_id in live_pos:
            day = central_day(conn, po_id)
            if day is None:
                typed_days = [d["dr_date"] for d in entered if d["po_id"] == po_id]
                day = max(typed_days) if typed_days else _last_count_day(conn, po_id)
            starts.append(day)
    else:
        starts = []
    latest = max(starts, default=None)
    bp, discount_days = of["term_discount_bp"], of["supplier"].get("term_discount_days") or 0
    # Terms or a date the program can't work with, say 3,000,000 days typed by mistake, hold up only this order and never the whole list.
    # Which value the date comes from, so the screen can name it if it gives no check date.
    if bp and discount_days:
        terms_cause, terms_value = "term discount days", str(discount_days)
    else:
        terms_cause, terms_value = "payment terms", str(of["payment_terms"])
    try:
        days = discount_days if bp and discount_days else terms_days(of["payment_terms"])
        due = next_saturday(date.fromisoformat(latest) + timedelta(days=days or 0)) if latest else None
        unreadable = False
    except (OverflowError, ValueError, TypeError):
        days, due, unreadable = None, None, True
    check = _active_check(conn, of_id)
    if check:
        stage = PAID
    elif not (receiving.of_delivery_complete(conn, of_id) if complete is None else complete) or not (counted_by_branches(conn, of_id) if counted is None else counted):
        stage = NOT_DONE
    elif money["unclaimed_c"] > 0:
        stage = DM_FIRST
    elif why and not all_in:
        stage = ENTER_DRS
    elif higher and not (accepted and accepted["difference_c"] in agreed):
        stage = DIFFERENCE
    elif higher and not ((sign := signoff(conn, "DIFFERENCE", of_id)) and sign["amount_c"] in agreed and sign["signed_by"] is not None):
        stage = SIGN_OFF
    else:
        stage = TERMS if unreadable else TO_PAY
    return {"id": of_id, "of_number": of["of_number"], "supplier_id": of["supplier_id"], "supplier": of["supplier"]["name"], "supplier_code": of["supplier"]["code"], "terms": of["payment_terms"], "receipts": receipts,
            "entered": len(entered), "why": why, "dr_total_c": dr_total, "difference_c": difference, "off_c": off, "higher_c": higher, "lower_c": lower, "accepted_reason": accepted["reason"] if accepted else "", "stage": stage, "latest_dr": latest,
            "terms_by_hand": days is None and not unreadable, "terms_unreadable": unreadable,
            "terms_cause": terms_cause if unreadable else "", "terms_value": terms_value if unreadable else "", "due": due, "check_number": check["check_number"] if check else "", "final_of_due": final_of_due(conn, of_id, money),
            "line_diffs": line_differences(conn, of_id, money["pos"]) if entered else {}, **money}


def orders_in_payment(conn, text: str = "", user: User | None = None) -> list[dict]:
    _may_view(user)
    words = text.split()
    # An order with a live check is finished here, so it's left out before anything is worked out for it.
    ids = conn.execute("SELECT f.id FROM order_forms f WHERE f.status = 'APPROVED' AND f.of_number IS NOT NULL AND NOT EXISTS"
                       " (SELECT 1 FROM payment_check_ofs o JOIN payment_checks c ON c.id = o.check_id WHERE o.of_id = f.id AND c.status = 'ACTIVE') ORDER BY f.id").fetchall()
    # Only what the branches counted: an order with a PO nobody counted never comes here.
    # Which orders the branches counted is read in one query for the whole list, as counted_by_branches() decides it for one order.
    counted = {r[0] for r in conn.execute(
        "SELECT p.of_id FROM purchase_orders p WHERE p.of_id IS NOT NULL AND p.status <> 'CANCELLED' GROUP BY p.of_id"
        " HAVING SUM(NOT EXISTS (SELECT 1 FROM branch_counts b WHERE b.po_id = p.id AND b.status = 'APPROVED')) = 0")}
    rows = [order_row(conn, r["id"], complete=True, counted=True) for r in ids if r["id"] in counted and receiving.of_delivery_complete(conn, r["id"])]
    return [r for r in rows if all(w.lower() in f"{r['of_number']} {r['supplier']} {r['supplier_code']} {' '.join(d['dr_number'] for d in r['receipts'])}".lower() for w in words)]


def supplier_balance(conn, supplier_id: int, user: User | None = None) -> int:
    """One running balance per supplier: what live checks actually paid, less what the orders they paid come to now.

    Above zero, we paid too much, say a debit memo came after the check or a check was typed high, and it comes off their next check.
    Below zero, we owe them, say a check was typed low, and it's added to their next check. A cancelled check drops out on both sides,
    so a credit is never taken twice and never lost. Within a peso counts as nothing, as everywhere else.
    """
    _may_view(user)
    memo = _memo(conn, "balance")
    if memo is None:
        return _supplier_balance(conn, supplier_id)
    if supplier_id not in memo:
        memo[supplier_id] = _supplier_balance(conn, supplier_id)
    return memo[supplier_id]


def agreed_deduction(conn, check) -> int:
    """A check recorded lower than the program's figure, with a reason, and signed off by Admin or management, is final.
    The difference is an agreed deduction, never carried to the next check as "We owe". check is a payment_checks row or its id.
    Nothing for a higher check, one still waiting for its sign-off, a cancelled one, or one recorded before the program kept its figure."""
    if isinstance(check, int):
        check = conn.execute("SELECT * FROM payment_checks WHERE id = ?", (check,)).fetchone()
        if check is None:
            return 0
    if check["status"] != ACTIVE or check["expected_c"] is None or not str(check["difference_reason"] or "").strip():
        return 0
    off = check["expected_c"] - check["amount_c"]
    if off <= TOLERANCE_C:
        return 0
    sign = signoff(conn, "CHECK", check["id"])
    return off if sign is not None and sign["signed_by"] is not None else 0


def _supplier_balance(conn, supplier_id: int) -> int:
    paid = conn.execute("SELECT COALESCE(SUM(amount_c), 0) FROM payment_checks WHERE supplier_id = ? AND status = ?", (supplier_id, ACTIVE)).fetchone()[0]
    # A lower check signed off is final: its difference counts as paid, so it's never owed again.
    agreed = sum(agreed_deduction(conn, c) for c in conn.execute(
        "SELECT * FROM payment_checks WHERE supplier_id = ? AND status = ? AND expected_c IS NOT NULL AND expected_c - amount_c > ?", (supplier_id, ACTIVE, TOLERANCE_C)).fetchall())
    orders = conn.execute("SELECT DISTINCT o.of_id FROM payment_check_ofs o JOIN payment_checks c ON c.id = o.check_id WHERE c.supplier_id = ? AND c.status = ?", (supplier_id, ACTIVE)).fetchall()
    balance = paid + agreed - sum(settlement(conn, r["of_id"])["pay_c"] for r in orders)
    return 0 if abs(balance) <= TOLERANCE_C else balance


def supplier_credit(conn, supplier_id: int, user: User | None = None) -> int:
    """What comes off the supplier's next check because we paid too much before. Nothing when we owe them instead."""
    _may_view(user)
    return max(supplier_balance(conn, supplier_id), 0)


def credit_without_orders(conn, groups: list[dict] | None = None, user: User | None = None) -> list[dict]:
    """Suppliers who owe us from earlier checks, say a debit memo came after the check, with nothing due to take it off. It waits for their next order."""
    _may_view(user)
    due = {g["supplier_id"] for g in (checks_due(conn) if groups is None else groups)}
    out = []
    for s in conn.execute("SELECT DISTINCT s.id, s.code, s.name FROM payment_checks c JOIN suppliers s ON s.id = c.supplier_id WHERE c.status = ? ORDER BY s.name", (ACTIVE,)).fetchall():
        if s["id"] not in due:
            balance = supplier_balance(conn, s["id"])
            if balance > 0:
                out.append({"supplier_id": s["id"], "code": s["code"], "supplier": s["name"], "credit_c": balance})
    return out


def late_claims(conn, supplier_id: int, user: User | None = None) -> list[dict]:
    """Claim stubs for damage found after the order was paid that no later check has taken off yet. They're why the supplier's next check is lower."""
    _may_view(user)
    return [dict(r) for r in conn.execute(
        "SELECT c.cs_number, c.total_c, l.found_at, k.check_number AS paid_by FROM late_damage l JOIN claim_stubs c ON c.id = l.cs_id JOIN payment_checks k ON k.id = l.paid_before"
        " WHERE c.supplier_id = ? AND c.status <> 'CANCELLED' AND NOT EXISTS (SELECT 1 FROM payment_checks n WHERE n.supplier_id = c.supplier_id AND n.status = 'ACTIVE' AND n.id <> l.paid_before AND n.recorded_at >= l.found_at)"
        " ORDER BY c.cs_number", (supplier_id,))]


def paid_back_memos(conn, supplier_id: int, user: User | None = None) -> list[dict]:
    """Debit memos cancelled after their order was paid, that no later check has carried yet. They're why the supplier's
    next check is higher, shown with the memo number and the reason it was cancelled, so Accounting can judge them."""
    _may_view(user)
    return [dict(r) for r in conn.execute(
        "SELECT d.id, d.dm_number, d.total_c, d.cancel_reason, d.cancelled_at, u.full_name AS cancelled_name, k.check_number AS paid_by, p.po_number, f.of_number"
        " FROM debit_memos d JOIN payment_checks k ON k.id = d.paid_back_check_id JOIN purchase_orders p ON p.id = d.po_id LEFT JOIN order_forms f ON f.id = p.of_id"
        " LEFT JOIN users u ON u.id = d.cancelled_by"
        " WHERE d.supplier_id = ? AND d.status = 'CANCELLED' AND NOT EXISTS (SELECT 1 FROM payment_checks n WHERE n.supplier_id = d.supplier_id AND n.status = 'ACTIVE'"
        " AND n.id <> d.paid_back_check_id AND n.recorded_at >= d.cancelled_at)"
        " ORDER BY d.dm_number", (supplier_id,))]


def owed_without_orders(conn, groups: list[dict] | None = None, user: User | None = None) -> list[dict]:
    """Suppliers we owe from earlier checks who have nothing due to add it to, so it isn't forgotten. groups are checks_due() if already worked out."""
    _may_view(user)
    due = {g["supplier_id"] for g in (checks_due(conn) if groups is None else groups)}
    out = []
    for s in conn.execute("SELECT DISTINCT s.id, s.code, s.name FROM payment_checks c JOIN suppliers s ON s.id = c.supplier_id WHERE c.status = ? ORDER BY s.name", (ACTIVE,)).fetchall():
        if s["id"] not in due:
            balance = supplier_balance(conn, s["id"])
            if balance < 0:
                out.append({"supplier_id": s["id"], "code": s["code"], "supplier": s["name"], "owed_c": -balance})
    return out


def checks_due(conn, text: str = "", rows: list[dict] | None = None, user: User | None = None) -> list[dict]:
    """One line per check to make: a supplier and a Saturday, soonest first. The supplier's balance from earlier checks goes on their first one:
    a credit comes off it, and what we owe them is added."""
    _may_view(user)
    groups: dict[tuple, dict] = {}
    for row in orders_in_payment(conn, text) if rows is None else rows:
        # An order without a check date (its terms can't be read) waits on Check the DRs instead.
        if row["stage"] == TO_PAY and row["due"] is not None:
            group = groups.setdefault((row["due"], row["supplier"], row["supplier_id"]), {"due": row["due"], "supplier": row["supplier"], "supplier_id": row["supplier_id"], "orders": [], "amount_c": 0})
            group["orders"].append(row)
            group["amount_c"] += row["pay_c"]
    balances: dict[int, int] = {}
    for key in sorted(groups):
        group = groups[key]
        # Worked out only the first time a supplier comes up. A default given to setdefault would work it out for every Saturday.
        if group["supplier_id"] not in balances:
            balances[group["supplier_id"]] = supplier_balance(conn, group["supplier_id"])
        left = balances[group["supplier_id"]]
        group["credit_c"] = min(left, max(group["amount_c"], 0)) if left > 0 else 0
        group["owed_c"] = -left if left < 0 else 0
        balances[group["supplier_id"]] = left - group["credit_c"] + group["owed_c"]
        group["check_c"] = group["amount_c"] - group["credit_c"] + group["owed_c"]
    return [groups[key] for key in sorted(groups)]


def _ready_rows(conn, of_ids: list[int]) -> list[dict]:
    rows = [order_row(conn, of_id) for of_id in of_ids]
    if not rows:
        raise POError("Choose what this check pays.")
    for row in rows:
        if row["stage"] == PAID:
            raise POError(f"{row['of_number']} already has check {row['check_number']}. To print it again, cancel that check first.")
        if row["stage"] == NOT_DONE:
            if receiving.of_delivery_complete(conn, row["id"]) and not counted_by_branches(conn, row["id"]):
                raise POError(f"{row['of_number']} has a PO the branch receivers never counted, so it isn't paid here.")
            raise POError(f"{row['of_number']} isn't fully delivered and settled yet, so it can't be paid.")
        if row["stage"] == DM_FIRST:
            raise POError(f"{row['of_number']} is short with no debit memo. The purchaser makes the debit memo first, so the supplier has the paper for it.")
        if row["stage"] == TERMS:
            raise POError(f"{row['of_number']} has terms that give no check date, so it can't be paid until they are corrected.")
        if row["stage"] != TO_PAY:
            raise POError(f"{row['of_number']} isn't ready to pay. Finish checking its DRs first.")
    if len({r["supplier_id"] for r in rows}) > 1:
        raise POError("One check can only pay one supplier.")
    return rows


def _expected(conn, rows: list[dict], groups: list[dict] | None = None) -> tuple[int, int, int]:
    """(what the orders come to, the credit taken or owed added, what the check should be). The credit is stored as one figure: below zero means it was added.

    It's the credit Checks to make gave these orders' line, so recording a later Saturday's check first doesn't take the credit meant for an earlier one.
    groups are checks_due() if already worked out, so a batch works them out once."""
    supplier = rows[0]["supplier_id"]
    total = sum(r["pay_c"] for r in rows)
    ids = {r["id"] for r in rows}
    group = next((g for g in (checks_due(conn) if groups is None else groups) if g["supplier_id"] == supplier and ids <= {o["id"] for o in g["orders"]}), None)
    if group is not None:
        credit = min(group["credit_c"], max(total, 0)) - group["owed_c"]
    else:
        balance = supplier_balance(conn, supplier)
        credit = min(balance, max(total, 0)) if balance > 0 else balance
    return total, credit, total - credit


def record_check(conn, user: User, of_ids: list[int], number: str, check_date: str, amount_text: str, reason: str = "", *, groups: list[dict] | None = None) -> int:
    """Records a printed check. It has to pay what the program worked out, or say why not. groups are checks_due() if a batch already worked them out."""
    require(user, "payment.edit")
    number = _check_number(number)
    amount = _amount(amount_text)
    if amount <= 0:
        raise POError("A check has to be for more than nothing. If the supplier's credit covers these orders, press Settle by credit instead.")
    check_date = _day(check_date)
    # AUD-146: several PCs share one file, so the long working-out happens before the write lock, while others can still save.
    # Inside the lock it's done again only if another PC saved in between (data_version changes then). A batch already holds the lock.
    worked, seen = None, None
    if not conn.in_transaction:
        seen = conn.execute("PRAGMA data_version").fetchone()[0]
        with one_look(conn):
            rows = _ready_rows(conn, of_ids)
            worked = (rows, _expected(conn, rows, groups))
    with tx(conn):
        if worked is None or conn.execute("PRAGMA data_version").fetchone()[0] != seen:
            rows = _ready_rows(conn, of_ids)
            worked = (rows, _expected(conn, rows, groups))
        rows, (total, credit, expected) = worked
        if conn.execute("SELECT 1 FROM payment_checks WHERE check_number = ?", (number,)).fetchone():
            raise POError(f"Check number {number} has already been used. A number is never used twice, even if its check was cancelled.")
        supplier = rows[0]["supplier_id"]
        if abs(amount - expected) > TOLERANCE_C and not reason.strip():
            raise CheckDifference(f"This check is {fmt_peso(amount)}, but the program worked out {fmt_peso(expected)}. Find out why before recording it.")
        # What the program worked out stays with the check: a lower check signed off is measured against it.
        check_id = conn.execute("INSERT INTO payment_checks(check_number, source, check_date, supplier_id, amount_c, credit_applied_c, difference_reason, status, recorded_by, recorded_at, expected_c) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (number, "EXTERNAL", check_date, supplier, amount, credit, reason.strip(), ACTIVE, user.id, timestamp(), expected)).lastrowid
        _link_orders(conn, check_id, rows)
        if abs(amount - expected) > TOLERANCE_C:
            _ask_signoff(conn, user, "CHECK", check_id, amount, rows[0]["id"])
        for row in rows:
            audit(conn, user, "CHECK_RECORD", "payment_check", check_id, of_id=row["id"], summary=f"Check {number} dated {check_date}, {fmt_peso(amount)}",
                  new={"amount_c": amount, "expected_c": expected, "credit_c": credit, "reason": reason.strip()})
    return check_id


def _link_orders(conn, check_id: int, rows: list[dict]) -> None:
    """Writes which orders a check pays. It happens once, while the check is being recorded and before anything is logged for it.
    Afterwards the check's orders are fixed: an order is never added to a check already recorded (the database refuses it too),
    and an order already paid by a live check is never put on a second one."""
    if conn.execute("SELECT 1 FROM payment_check_ofs WHERE check_id = ?", (check_id,)).fetchone():
        raise POError("This check already has its orders. To pay another order, record a new check.")
    for row in rows:
        other = _active_check(conn, row["id"])
        if other is not None and other["id"] != check_id:
            raise POError(f"{row['of_number']} already has check {other['check_number']}. To print it again, cancel that check first.")
        conn.execute("INSERT INTO payment_check_ofs(check_id, of_id, amount_c) VALUES (?,?,?)", (check_id, row["id"], row["pay_c"]))
        _keep_dr_lower(conn, check_id, row)


def _keep_dr_lower(conn, check_id: int, row: dict) -> None:
    """A lower DR is paid as it says with no sign-off. What the check paid for it is kept with the order, so a later count or
    debit memo can't take it off again. An order with nothing lower and nothing accepted needs no row."""
    if "dr_lower_c" not in row:
        return
    lower = row["dr_lower_c"] or 0
    if conn.execute("SELECT 1 FROM payment_differences WHERE of_id = ?", (row["id"],)).fetchone():
        conn.execute("UPDATE payment_differences SET dr_lower_c = ? WHERE of_id = ?", (lower, row["id"]))
    elif lower:
        # Nothing was accepted (only DRs higher than ours are), so the row says only what the lower DRs took off: no difference, no reason,
        # kept by whoever recorded the check.
        conn.execute("INSERT INTO payment_differences(of_id, difference_c, reason, accepted_by, accepted_at, dr_lower_c) SELECT ?, 0, '', recorded_by, ?, ? FROM payment_checks WHERE id = ?",
                     (row["id"], timestamp(), lower, check_id))


def settle_by_credit(conn, user: User, of_ids: list[int]) -> int:
    """The supplier's credit from earlier checks covers these orders, so no check is printed. It's recorded as a nothing-to-pay settlement under its own number,
    counts as cleared since no money moves, and waits for Admin or management to sign it off like any amount that isn't a plain check."""
    require(user, "payment.edit")
    # AUD-146: worked out before the write lock, and again inside only if another PC saved in between.
    seen = conn.execute("PRAGMA data_version").fetchone()[0]
    with one_look(conn):
        rows = _ready_rows(conn, of_ids)
        worked = (rows, _expected(conn, rows))
    with tx(conn):
        if conn.execute("PRAGMA data_version").fetchone()[0] != seen:
            rows = _ready_rows(conn, of_ids)
            worked = (rows, _expected(conn, rows))
        rows, (total, credit, expected) = worked
        if expected > TOLERANCE_C:
            raise POError(f"The supplier's credit doesn't cover these orders: {fmt_peso(expected)} is still to pay, so it needs a check.")
        number = _next_credit_number(conn)
        check_id = conn.execute("INSERT INTO payment_checks(check_number, source, check_date, supplier_id, amount_c, credit_applied_c, difference_reason, status, recorded_by, recorded_at, expected_c) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (number, "PROGRAM", today().isoformat(), rows[0]["supplier_id"], 0, total, "Settled by the supplier's credit", ACTIVE, user.id, timestamp(), max(expected, 0))).lastrowid
        _link_orders(conn, check_id, rows)
        for row in rows:
            audit(conn, user, "CHECK_CREDIT", "payment_check", check_id, of_id=row["id"], summary=f"{row['of_number']} settled by the supplier's credit, {number}", new={"credit_c": total})
        _ask_signoff(conn, user, "CHECK", check_id, total, rows[0]["id"])
    return check_id


def _next_credit_number(conn) -> str:
    """The number after the highest CREDIT- number used, cancelled ones too, so a number is never used twice."""
    used = [str(r[0])[len(CREDIT_PREFIX):] for r in conn.execute("SELECT check_number FROM payment_checks WHERE check_number LIKE 'CREDIT-%'")]
    top = max((int(n) for n in used if re.fullmatch(r"[0-9]{1,18}", n)), default=0)
    return f"{CREDIT_PREFIX}{top + 1:05d}"


def settled_by_credit(check) -> bool:
    return check["source"] == "PROGRAM" and check["amount_c"] == 0 and str(check["check_number"]).startswith("CREDIT-")


def next_check_number(conn) -> str:
    """The number after the highest printed check recorded, keeping its zeros in front, so a batch can carry on from it. Nothing if none was recorded yet."""
    # Only plain digits count. An odd number recorded before they were checked, say "²", is passed over rather than breaking the next one.
    numbers = [str(r[0]) for r in conn.execute("SELECT check_number FROM payment_checks WHERE source = 'EXTERNAL'") if re.fullmatch(r"[0-9]{1,%d}" % MAX_CHECK_DIGITS, str(r[0]))]
    if not numbers:
        return ""
    top = max(numbers, key=int)
    return str(int(top) + 1).zfill(len(top))


def check_numbers(first: str, count: int) -> list[str]:
    """Numbers running on from the first, keeping any zeros in front."""
    first = first.strip()
    if not re.fullmatch(r"[0-9]{1,%d}" % MAX_CHECK_DIGITS, first):
        raise POError("Type the first check number, in digits only.")
    return [str(int(first) + n).zfill(len(first)) for n in range(count)]


def record_checks(conn, user: User, checks: list[dict]) -> list[int]:
    """Records a printed batch, each as {of_ids, number, date, amount}. Either all of them are recorded or none are, so a wrong number never leaves half a batch."""
    require(user, "payment.edit")
    if not checks:
        raise POError("Tick the checks to record.")
    with tx(conn):
        # Checks to make is worked out once for the batch. Each check pays a different supplier and Saturday, and the line's credit was already shared out.
        with one_look(conn):
            groups = checks_due(conn)
        return [record_check(conn, user, c["of_ids"], c["number"], c["date"], c["amount"], groups=groups) for c in checks]


def cancel_check(conn, user: User, check_id: int, reason: str) -> None:
    """The only way to print a check again. Its orders go back on the list, so nothing is paid twice and nothing is forgotten."""
    require(user, "payment.edit")
    if not reason.strip():
        raise POError("Write why this check is being cancelled.")
    with tx(conn):
        check = conn.execute("SELECT * FROM payment_checks WHERE id = ?", (check_id,)).fetchone()
        if check is None:
            raise POError("That check no longer exists.")
        if check["status"] != ACTIVE:
            raise POError(f"Check {check['check_number']} is already cancelled.")
        if conn.execute("SELECT 1 FROM check_claims WHERE check_id = ?", (check_id,)).fetchone():
            raise POError(f"The supplier already has check {check['check_number']}. Undo the claim first if it came back.")
        conn.execute("UPDATE payment_checks SET status = ?, cancelled_by = ?, cancelled_at = ?, cancel_reason = ? WHERE id = ?", (CANCELLED, user.id, timestamp(), reason.strip(), check_id))
        for link in conn.execute("SELECT of_id FROM payment_check_ofs WHERE check_id = ?", (check_id,)).fetchall():
            audit(conn, user, "CHECK_CANCEL", "payment_check", check_id, of_id=link["of_id"], summary=f"Check {check['check_number']} cancelled: {reason.strip()}")


def claim_check(conn, user: User, check_id: int, claimed_on: str | None = None, claimed_by: str = "") -> None:
    """The supplier took the check. It can happen before the check's date, because a post-dated check can be collected early."""
    require(user, "payment.edit")
    day = _day(claimed_on) if claimed_on else today().isoformat()
    if date.fromisoformat(day) > today():
        raise POError("The date claimed can't be in the future.")
    with tx(conn):
        check = conn.execute("SELECT * FROM payment_checks WHERE id = ?", (check_id,)).fetchone()
        if check is None or check["status"] != ACTIVE:
            raise POError("A cancelled check can't be claimed.")
        if conn.execute("SELECT 1 FROM check_claims WHERE check_id = ?", (check_id,)).fetchone():
            raise POError(f"Check {check['check_number']} is already claimed.")
        if check_waiting(conn, check_id):
            raise POError(f"Check {check['check_number']} is for a different amount than the program worked out. Admin or management has to sign it off before the supplier gets it.")
        conn.execute("INSERT INTO check_claims(check_id, claimed_on, claimed_by_name, recorded_by, recorded_at) VALUES (?,?,?,?,?)", (check_id, day, claimed_by.strip(), user.id, timestamp()))
        audit(conn, user, "CHECK_CLAIM", "payment_check", check_id, summary=f"Check {check['check_number']} claimed {day}" + (f" by {claimed_by.strip()}" if claimed_by.strip() else ""))


def undo_claim(conn, user: User, check_id: int, reason: str) -> None:
    """Marked claimed by mistake, or the check came back."""
    require(user, "payment.edit")
    if not reason.strip():
        raise POError("Write why the claim is being undone.")
    with tx(conn):
        claim = conn.execute("SELECT * FROM check_claims WHERE check_id = ?", (check_id,)).fetchone()
        if claim is None:
            raise POError("That check isn't claimed.")
        if conn.execute("SELECT 1 FROM check_clears WHERE check_id = ?", (check_id,)).fetchone():
            raise POError("That check has cleared the bank. Undo that first.")
        number = conn.execute("SELECT check_number FROM payment_checks WHERE id = ?", (check_id,)).fetchone()[0]
        conn.execute("DELETE FROM check_claims WHERE check_id = ?", (check_id,))
        audit(conn, user, "CHECK_UNCLAIM", "payment_check", check_id, summary=f"Claim on check {number} undone: {reason.strip()}", previous={"claimed_on": claim["claimed_on"], "claimed_by": claim["claimed_by_name"]})


def _clear_day(cleared_on: str | None) -> str:
    day = _day(cleared_on) if cleared_on else today().isoformat()
    if date.fromisoformat(day) > today():
        raise POError("The date cleared can't be in the future.")
    return day


def _stored_day(value: str) -> str:
    """A date saved before dates were checked, say 20261101, read the same way as one saved now."""
    try:
        return _day(value)
    except POError:
        return value


def _clear(conn, user: User, check_id: int, day: str) -> None:
    check = conn.execute("SELECT c.check_number, k.claimed_on FROM payment_checks c LEFT JOIN check_claims k ON k.check_id = c.id WHERE c.id = ?", (check_id,)).fetchone()
    if check is None or not check["claimed_on"]:
        raise POError("A check has to be claimed before it can clear the bank." if check is None else f"Check {check['check_number']} has to be claimed before it can clear the bank.")
    if day < _stored_day(check["claimed_on"]):
        raise POError(f"Check {check['check_number']} was claimed on {check['claimed_on']}, so it can't have cleared before that.")
    if conn.execute("SELECT 1 FROM check_clears WHERE check_id = ?", (check_id,)).fetchone():
        raise POError(f"Check {check['check_number']} has already cleared.")
    conn.execute("INSERT INTO check_clears(check_id, cleared_on, recorded_by, recorded_at) VALUES (?,?,?,?)", (check_id, day, user.id, timestamp()))
    audit(conn, user, "CHECK_CLEAR", "payment_check", check_id, summary=f"Check {check['check_number']} cleared the bank {day}")


def clear_check(conn, user: User, check_id: int, cleared_on: str | None = None) -> None:
    """The bank paid it, so the money has left the account. A check has to be claimed before it can clear."""
    require(user, "payment.edit")
    day = _clear_day(cleared_on)
    with tx(conn):
        _clear(conn, user, check_id, day)


def mark_cleared_many(conn, user: User, check_ids: list[int], cleared_on: str | None = None) -> None:
    """Several checks from one bank statement, all cleared the same day. Each follows the same rules as one, and either all are marked or none are."""
    require(user, "payment.edit")
    ids = list(dict.fromkeys(check_ids))
    if not ids:
        raise POError("Tick the checks to mark cleared.")
    day = _clear_day(cleared_on)
    with tx(conn):
        for check_id in ids:
            _clear(conn, user, check_id, day)


def undo_clear(conn, user: User, check_id: int, reason: str) -> None:
    """Marked cleared by mistake."""
    require(user, "payment.edit")
    if not reason.strip():
        raise POError("Write why this is being undone.")
    with tx(conn):
        clear = conn.execute("SELECT * FROM check_clears WHERE check_id = ?", (check_id,)).fetchone()
        if clear is None:
            raise POError("That check hasn't cleared.")
        number = conn.execute("SELECT check_number FROM payment_checks WHERE id = ?", (check_id,)).fetchone()[0]
        conn.execute("DELETE FROM check_clears WHERE check_id = ?", (check_id,))
        audit(conn, user, "CHECK_UNCLEAR", "payment_check", check_id, summary=f"Clearing of check {number} undone: {reason.strip()}", previous={"cleared_on": clear["cleared_on"]})


def check_bucket(check_date: str, claimed_on: str | None, cleared_on: str | None = None) -> str:
    """Cleared once the bank paid it, and claimed once the supplier has it. Otherwise it's due from its date, and in the future until then."""
    if cleared_on:
        return CLEARED
    if claimed_on:
        return CLAIMED
    return FUTURE if date.fromisoformat(check_date) > today() else DUE


def orders_on_check(conn, check_id: int, user: User | None = None) -> list[dict]:
    """The orders a check pays, each with what it comes to now and what this check paid towards it."""
    _may_view(user)
    return [{"id": r["of_id"], "of_number": r["of_number"], "paid_c": r["amount_c"], **settlement(conn, r["of_id"])} for r in conn.execute(
        "SELECT o.of_id, o.amount_c, f.of_number FROM payment_check_ofs o JOIN order_forms f ON f.id = o.of_id WHERE o.check_id = ? ORDER BY f.of_number", (check_id,)).fetchall()]


FLOW_KEYS = ("printed_n", "printed_c", "claimed_n", "claimed_c", "to_make_n", "to_make_c", "total_c", "waiting_n", "waiting_c", "passed_n", "passed_c")


def cash_flow(conn, rows: list[dict] | None = None, groups: list[dict] | None = None, user: User | None = None) -> list[dict]:
    """What has to be covered, by date, soonest first: every check that hasn't cleared the bank, and checks still to make. The running total is the cash needed by that date.
    groups are checks_due() if already worked out. Checks still waiting for a sign-off, and unclaimed checks whose date has passed, are counted as well,
    so a date's amount is never less than it could be, and each line says how many of them it holds."""
    _may_view(user)
    days: dict[date, dict] = {}

    def line(day: date) -> dict:
        return days.setdefault(day, {"date": day, **dict.fromkeys(FLOW_KEYS, 0)})

    for check in checks(conn, ACTIVE):
        if check["bucket"] != CLEARED:
            row = line(date.fromisoformat(check["check_date"]))
            kind = "claimed" if check["bucket"] == CLAIMED else "printed"
            row[f"{kind}_n"] += 1
            row[f"{kind}_c"] += check["amount_c"]
            if check["waiting_signoff"]:
                row["waiting_n"] += 1
                row["waiting_c"] += check["amount_c"]
            if check["bucket"] == DUE and check["days"] < 0:
                row["passed_n"] += 1
                row["passed_c"] += check["amount_c"]
    for group in (checks_due(conn, rows=rows) if groups is None else groups):
        row = line(group["due"])
        row["to_make_n"] += 1
        row["to_make_c"] += group["check_c"]
    running = 0
    out = []
    for day in sorted(days):
        row = days[day]
        row["total_c"] = row["printed_c"] + row["claimed_c"] + row["to_make_c"]
        running += row["total_c"]
        row["running_c"] = running
        out.append(row)
    return out


def cash_flow_by_month(rows: list[dict]) -> list[dict]:
    """The same figures added up by month. The running total is where the month ends."""
    months: dict[date, dict] = {}
    for row in rows:
        month = months.setdefault(row["date"].replace(day=1), {"date": row["date"].replace(day=1), **dict.fromkeys(FLOW_KEYS, 0)})
        for key in FLOW_KEYS:
            month[key] += row.get(key, 0)
        month["running_c"] = row["running_c"]
    return [months[m] for m in sorted(months)]


def checks(conn, status: str | None = None, text: str = "", user: User | None = None) -> list[dict]:
    _may_view(user)
    where, args = [], []
    if status:
        where.append("c.status = ?")
        args.append(status)
    for word in text.split():
        where.append("(c.check_number LIKE ? OR s.name LIKE ? OR s.code LIKE ? OR EXISTS (SELECT 1 FROM payment_check_ofs o JOIN order_forms f ON f.id = o.of_id WHERE o.check_id = c.id AND f.of_number LIKE ?))")
        args += [f"%{word}%"] * 4
    rows = conn.execute("SELECT c.*, s.name AS supplier, s.code AS supplier_code, r.full_name AS recorded_name, x.full_name AS cancelled_name, k.claimed_on, k.claimed_by_name, b.cleared_on,"
                        " (SELECT GROUP_CONCAT(f.of_number, ', ') FROM payment_check_ofs o JOIN order_forms f ON f.id = o.of_id WHERE o.check_id = c.id) AS of_numbers,"
                        # The latest sign-off asked for on the check, as check_waiting() reads it, so the list needs no query per check.
                        " (SELECT g.signed_by IS NULL FROM payment_signoffs g WHERE g.kind = 'CHECK' AND g.ref_id = c.id ORDER BY g.id DESC LIMIT 1) AS waiting_signoff"
                        " FROM payment_checks c JOIN suppliers s ON s.id = c.supplier_id JOIN users r ON r.id = c.recorded_by LEFT JOIN users x ON x.id = c.cancelled_by LEFT JOIN check_claims k ON k.check_id = c.id LEFT JOIN check_clears b ON b.check_id = c.id"
                        + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY c.check_date DESC, c.id DESC", args).fetchall()
    out = [dict(r) for r in rows]
    for c in out:
        # A settlement by credit moves no money, so it's done the day it's recorded.
        c["bucket"] = CLEARED if settled_by_credit(c) else check_bucket(c["check_date"], c["claimed_on"], c["cleared_on"])
        c["days"] = (date.fromisoformat(c["check_date"]) - today()).days
        c["waiting_signoff"] = bool(c["waiting_signoff"])
    return out
