"""The agreed overall discount stays as agreed after a branch PO is amended or cancelled.

A percent OF keeps its percentage on every PO. A peso OF keeps the same peso total, shared again across the POs still live.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from atlaspo import auth, db, debit_memos, money, order_forms, orders, receiving
from atlaspo.db import POError

from conftest import NORTH, PASSWORD, SOUTH, EAST


@pytest.fixture
def w(world):
    db.set_settings(world.conn, world.admin, {"approval.enabled": "1", "approval.threshold_c": "10000000",
                                              "approval.approver_user_ids": str(world.admin.id)})
    return world


def _order(w, alloc, issue=True, **terms):
    """One product at 100.00 a piece, alloc {branch: pieces}. Returns (of_id, {branch: po_id})."""
    of_id = w.approved(items=[w.item(cost=10000, alloc=alloc, description="Socks", item_code="SOCK-1")], **terms)
    pos = order_forms.generate_pos(w.conn, w.buyer, of_id)
    if issue:
        for p in pos:
            orders.issue(w.conn, w.buyer, p["id"])
    return of_id, {p["branch_code"]: p["id"] for p in pos}


def _rows(w, pid, pieces):
    out = []
    for i in orders.get_po(w.conn, pid)["items"]:
        r = {k: i[k] for k in orders.LINE_FIELDS}
        r["id"], r["qty_m"] = i["id"], pieces * 1000
        out.append(r)
    return out


def _po(w, pid):
    return w.conn.execute("SELECT * FROM purchase_orders WHERE id = ?", (pid,)).fetchone()


def _of(w, of_id):
    return w.conn.execute("SELECT * FROM order_forms WHERE id = ?", (of_id,)).fetchone()


def _percent(w, **kw):
    return dict(overall_discount_type=money.DISC_PERCENT, **kw)


# ------------------------------------------------------------------ percent OF: the percentage stays

def test_a_ten_percent_po_cut_to_five_pieces_keeps_ten_percent(w):
    of_id, pos = _order(w, {NORTH: 10}, **_percent(w, overall_discount_value=1000))
    res = orders.amend(w.conn, w.buyer, pos[NORTH], {}, _rows(w, pos[NORTH], 5), "Supplier short")
    assert res["status"] == "APPLIED"  # less, so no approval
    po = _po(w, pos[NORTH])
    assert po["overall_discount_c"] == 5000 and po["grand_total_c"] == 45000  # 10% of 500.00, not 100.00 (20%)
    assert po["overall_discount_type"] == money.DISC_AMOUNT and po["overall_discount_value"] == 5000
    of = _of(w, of_id)
    assert (of["overall_discount_type"], of["overall_discount_value"]) == (money.DISC_PERCENT, 1000)  # the OF header is never changed
    assert of["overall_discount_c"] == 5000 and of["grand_total_c"] == 45000


def test_a_ten_percent_po_cut_to_one_piece_is_never_zero(w):
    _, pos = _order(w, {NORTH: 10}, **_percent(w, overall_discount_value=1000))
    orders.amend(w.conn, w.buyer, pos[NORTH], {}, _rows(w, pos[NORTH], 1), "Only one")
    po = _po(w, pos[NORTH])
    assert po["grand_total_c"] == 9000 and po["overall_discount_c"] == 1000


def test_a_twenty_percent_po_can_be_cut_to_one_piece(w):
    _, pos = _order(w, {NORTH: 10}, **_percent(w, overall_discount_value=2000))
    orders.amend(w.conn, w.buyer, pos[NORTH], {}, _rows(w, pos[NORTH], 1), "Only one")  # refused before: 200.00 off a 100.00 PO
    assert _po(w, pos[NORTH])["grand_total_c"] == 8000


def test_an_increase_keeps_ten_percent_and_still_needs_approval(w):
    _, pos = _order(w, {NORTH: 10}, **_percent(w, overall_discount_value=1000))
    res = orders.amend(w.conn, w.buyer, pos[NORTH], {}, _rows(w, pos[NORTH], 20), "More please")
    assert res["status"] == orders.PENDING
    assert _po(w, pos[NORTH])["grand_total_c"] == 90000  # nothing changes until it is approved
    orders.decide_amendment(w.conn, w.admin, res["amendment_id"], True)
    po = _po(w, pos[NORTH])
    assert po["overall_discount_c"] == 20000 and po["grand_total_c"] == 180000  # 10%, not 5%


def test_an_untouched_sibling_on_a_percent_of_keeps_its_revision_and_status(w):
    _, pos = _order(w, {NORTH: 10, SOUTH: 10}, **_percent(w, overall_discount_value=1000))
    before = dict(_po(w, pos[SOUTH]))
    orders.amend(w.conn, w.buyer, pos[NORTH], {}, _rows(w, pos[NORTH], 3), "Fewer")
    after = dict(_po(w, pos[SOUTH]))
    assert (after["status"], after["revision"], after["overall_discount_c"], after["grand_total_c"]) == \
           (orders.ISSUED, before["revision"], before["overall_discount_c"], before["grand_total_c"])
    assert not w.conn.execute("SELECT 1 FROM audit_logs WHERE po_id = ? AND action = 'PO_DISCOUNT_SHARE'", (pos[SOUTH],)).fetchone()


def test_a_short_piece_is_charged_at_the_new_share(w):
    _, pos = _order(w, {NORTH: 10}, **_percent(w, overall_discount_value=1000))
    pid = pos[NORTH]
    orders.amend(w.conn, w.buyer, pid, {}, _rows(w, pid, 5), "Fewer")
    orders.issue(w.conn, w.buyer, pid)
    item = orders.get_po(w.conn, pid)["items"][0]["id"]
    for n in (1, 2):
        who = auth.create_user(w.conn, w.admin, f"r{n}", f"R {n}", PASSWORD, ["RECEIVER"], must_change=False, branch_code=NORTH)
        receiving.save_count(w.conn, who, pid, {item: "4"})
    line = debit_memos.outstanding(w.conn, w.buyer, pid)[0]
    assert line["amount_c"] == 9000  # one piece at 100.00 less 10%; the old fixed share made it 80.00


# ------------------------------------------------------------------ peso OF: the same total, shared again

def _peso(w, issue=True):
    return _order(w, {NORTH: 10, SOUTH: 10, EAST: 10}, issue=issue,
                  overall_discount_type=money.DISC_AMOUNT, overall_discount_value=30000)


def _shares(w, pos):
    return {b: _po(w, p)["overall_discount_c"] for b, p in pos.items() if _po(w, p)["status"] != orders.CANCELLED}


def test_a_peso_of_amended_keeps_the_agreed_total_shared_by_merchandise(w):
    of_id, pos = _peso(w)
    assert _shares(w, pos) == {NORTH: 10000, SOUTH: 10000, EAST: 10000}
    orders.amend(w.conn, w.buyer, pos[NORTH], {}, _rows(w, pos[NORTH], 5), "Fewer")
    assert _shares(w, pos) == {NORTH: 6000, SOUTH: 12000, EAST: 12000}
    of = _of(w, of_id)
    assert of["overall_discount_c"] == 30000 and (of["overall_discount_type"], of["overall_discount_value"]) == (money.DISC_AMOUNT, 30000)
    assert of["grand_total_c"] == sum(_po(w, p)["grand_total_c"] for p in pos.values()) == 250000 - 30000


def test_an_issued_sibling_whose_share_changed_waits_to_be_issued_again(w):
    _, pos = _peso(w)
    before = _po(w, pos[SOUTH])
    orders.amend(w.conn, w.buyer, pos[NORTH], {}, _rows(w, pos[NORTH], 5), "Fewer")
    sib = _po(w, pos[SOUTH])
    assert sib["status"] == orders.APPROVED and sib["revision"] == before["revision"] + 1
    assert sib["grand_total_c"] == 100000 - 12000
    assert orders.to_issue(sib)
    log = w.conn.execute("SELECT summary FROM audit_logs WHERE po_id = ? AND action = 'PO_DISCOUNT_SHARE'", (pos[SOUTH],)).fetchall()
    assert len(log) == 1 and "shared again after" in log[0]["summary"] and "₱100.00 to ₱120.00" in log[0]["summary"]
    orders.issue(w.conn, w.buyer, pos[SOUTH])  # and it can go to the supplier again
    assert _po(w, pos[SOUTH])["issued_revision"] == before["revision"] + 1


def test_a_peso_of_increase_reshares_only_when_approved(w):
    _, pos = _peso(w)
    res = orders.amend(w.conn, w.buyer, pos[NORTH], {}, _rows(w, pos[NORTH], 20), "More")
    assert res["status"] == orders.PENDING
    assert _shares(w, pos) == {NORTH: 10000, SOUTH: 10000, EAST: 10000}
    orders.decide_amendment(w.conn, w.admin, res["amendment_id"], True)
    assert _shares(w, pos) == {NORTH: 15000, SOUTH: 7500, EAST: 7500}


def test_cancelling_one_po_moves_its_peso_share_onto_the_others(w):
    of_id, pos = _peso(w)
    orders.cancel(w.conn, w.buyer, pos[EAST], "Branch closed for repairs")
    assert _shares(w, pos) == {NORTH: 15000, SOUTH: 15000}
    of = _of(w, of_id)
    assert of["overall_discount_c"] == 30000 and of["grand_total_c"] == 200000 - 30000
    for b in (NORTH, SOUTH):
        assert _po(w, pos[b])["status"] == orders.APPROVED  # changed, so issued again


def test_cancelling_the_whole_of_is_unchanged(w):
    of_id, pos = _peso(w)
    order_forms.cancel(w.conn, w.buyer, of_id, "Supplier closed")
    assert all(_po(w, p)["overall_discount_c"] == 10000 for p in pos.values())
    assert not w.conn.execute("SELECT 1 FROM audit_logs WHERE action = 'PO_DISCOUNT_SHARE'").fetchone()


def test_an_agreed_amount_bigger_than_what_is_left_is_refused_and_nothing_saved(w):
    of_id = w.approved(items=[w.item(cost=10000, alloc={NORTH: 1, SOUTH: 3})],
                       overall_discount_type=money.DISC_AMOUNT, overall_discount_value=30000)
    pos = {p["branch_code"]: p["id"] for p in order_forms.generate_pos(w.conn, w.buyer, of_id)}
    before = [dict(_po(w, p)) for p in pos.values()]
    with pytest.raises(POError, match="agreed discount of ₱300.00.*more than what is left"):
        orders.cancel(w.conn, w.buyer, pos[SOUTH], "Not needed")
    with pytest.raises(POError, match="agreed discount of ₱300.00.*more than what is left"):
        orders.amend(w.conn, w.buyer, pos[SOUTH], {}, _rows(w, pos[SOUTH], 1), "Fewer")
    assert [dict(_po(w, p)) for p in pos.values()] == before
    assert not w.conn.execute("SELECT 1 FROM po_amendments").fetchone()


def test_an_of_without_a_discount_is_unchanged(w):
    of_id, pos = _order(w, {NORTH: 10, SOUTH: 10})
    before = dict(_po(w, pos[SOUTH]))
    orders.amend(w.conn, w.buyer, pos[NORTH], {}, _rows(w, pos[NORTH], 4), "Fewer")
    assert _po(w, pos[NORTH])["grand_total_c"] == 40000 and _po(w, pos[NORTH])["overall_discount_c"] == 0
    orders.cancel(w.conn, w.buyer, pos[NORTH], "Not needed")
    assert dict(_po(w, pos[SOUTH])) == before
    assert orders.agreed_discount_shares(w.conn, of_id, {pos[SOUTH]: []}) == {}
