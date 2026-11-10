"""Audit fixes in orders.py: amendments re-checked on approval, delivery status after an amendment, backend gates."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from atlaspo import auth, db, debit_memos, order_forms, orders, pdf, receiving
from atlaspo.db import PermissionDenied, POError

from conftest import NORTH, PASSWORD, SOUTH


@pytest.fixture
def w(world):
    """One issued order: Socks 10 and Caps 10 at North, 4 each at South. Two receivers at North, plus a viewer, accounting and a second approver."""
    conn = world.conn
    make = lambda name, roles, branch=None: auth.create_user(conn, world.admin, name, name.title(), PASSWORD, roles, must_change=False, branch_code=branch)
    world.r1, world.r2 = make("ana", ["RECEIVER"], NORTH), make("ben", ["RECEIVER"], NORTH)
    world.viewer, world.acct, world.mgr = make("vic", ["VIEWER"]), make("acc", ["ACCOUNTING"]), make("mgr", ["APPROVER"])
    db.set_settings(conn, world.admin, {"approval.enabled": "1", "approval.threshold_c": "10000000",
                                        "approval.approver_user_ids": f"{world.admin.id},{world.mgr.id}"})
    of_id = world.approved(items=[world.item(cost=10000, alloc={NORTH: 10, SOUTH: 4}, description="Socks", item_code="SOCK-1"),
                                  world.item(cost=10000, alloc={NORTH: 10, SOUTH: 4}, description="Caps", item_code="CAP-1")])
    pos = order_forms.generate_pos(conn, world.buyer, of_id)
    for p in pos:
        orders.issue(conn, world.buyer, p["id"])
    world.of_id, world.pid, world.south = of_id, pos[0]["id"], pos[1]["id"]
    world.a, world.b = [i["id"] for i in orders.get_po(conn, world.pid)["items"]]
    return world


def _rows(w, qty: dict, pid=None):
    out = []
    for i in orders.get_po(w.conn, pid or w.pid)["items"]:
        r = {k: i[k] for k in orders.LINE_FIELDS}
        r["id"], r["qty_m"] = i["id"], qty.get(i["id"], i["qty_m"])
        out.append(r)
    return out


def _agree(w, a, b):
    for who in (w.r1, w.r2):
        receiving.save_count(w.conn, who, w.pid, {w.a: a, w.b: b})


def _po(w, pid=None):
    return orders.get_po(w.conn, pid or w.pid)


# ------------------------------------------------------------------ AUD-056: approval re-checks what happened while it waited

def test_approval_is_refused_when_a_count_made_while_waiting_goes_above_the_new_quantity(w):
    res = orders.amend(w.conn, w.buyer, w.pid, {}, _rows(w, {w.a: 12000, w.b: 4000}), "rebalance")
    assert res["status"] == orders.PENDING
    _agree(w, "5", "10")  # 10 Caps arrive while the amendment waits
    with pytest.raises(POError, match="can't be applied any more.*below what was already received"):
        orders.decide_amendment(w.conn, w.mgr, res["amendment_id"], True)
    caps = next(i for i in _po(w)["items"] if i["id"] == w.b)
    assert caps["qty_m"] == 10000 and _po(w)["revision"] == 0
    # Still waiting, so it can be rejected.
    orders.decide_amendment(w.conn, w.mgr, res["amendment_id"], False, "counted since")


def test_approval_is_refused_when_a_debit_memo_made_while_waiting_charges_the_lowered_row(w):
    _agree(w, "10", "6")
    res = orders.amend(w.conn, w.buyer, w.pid, {}, _rows(w, {w.a: 11000, w.b: 8000}), "rebalance")
    assert res["status"] == orders.PENDING
    lines = debit_memos.outstanding(w.conn, w.buyer, w.pid)
    debit_memos.issue(w.conn, w.buyer, w.pid, {(l["po_item_id"], l["reason"]): str(l["pieces_m"] // 1000) for l in lines}, "Short")
    with pytest.raises(POError, match="debit memo or claim stub"):
        orders.decide_amendment(w.conn, w.mgr, res["amendment_id"], True)
    assert [i["qty_m"] for i in _po(w)["items"]] == [10000, 10000]


def test_approval_still_applies_when_nothing_got_in_the_way(w):
    res = orders.amend(w.conn, w.buyer, w.pid, {}, _rows(w, {w.a: 12000}), "more socks")
    orders.decide_amendment(w.conn, w.mgr, res["amendment_id"], True)
    assert _po(w)["items"][0]["qty_m"] == 12000 and _po(w)["revision"] == 1


# ------------------------------------------------------------------ AUD-075: delivery status after an amendment

def test_lowering_a_partly_delivered_po_to_what_arrived_makes_it_fully_delivered(w):
    _agree(w, "7", "10")
    assert _po(w)["status"] == orders.PARTIAL
    res = orders.amend(w.conn, w.buyer, w.pid, {}, _rows(w, {w.a: 7000}), "rest not coming")
    assert res["status"] == "APPLIED"
    assert _po(w)["status"] == orders.FULL


def test_lowering_but_not_to_what_arrived_stays_partly_delivered(w):
    _agree(w, "5", "10")
    orders.amend(w.conn, w.buyer, w.pid, {}, _rows(w, {w.a: 7000}), "fewer")
    assert _po(w)["status"] == orders.PARTIAL


# ------------------------------------------------------------------ AUD-089: amendment inputs

def test_blank_remarks_given_as_none_are_taken_as_blank(w):
    res = orders.amend(w.conn, w.buyer, w.pid, {"remarks": None, "delivery_instructions": None}, _rows(w, {w.a: 9000}), "one less")
    assert res["status"] == "APPLIED"
    po = _po(w)
    assert po["remarks"] == "" and po["delivery_instructions"] == "" and po["items"][0]["qty_m"] == 9000


def test_an_absurd_amendment_quantity_is_refused_plainly(w):
    """AUD-081 (PO amendment part): 1e16 used to crash with OverflowError; now a plain refusal and nothing saved."""
    for qty in (10**16, (10**9 + 1) * 1000):
        with pytest.raises(POError, match="Socks.*too large"):
            orders.amend(w.conn, w.buyer, w.pid, {}, _rows(w, {w.a: qty}), "typo")
    assert _po(w)["revision"] == 0 and _po(w)["items"][0]["qty_m"] == 10000
    # A large but possible quantity still goes through.
    res = orders.amend(w.conn, w.buyer, w.pid, {}, _rows(w, {w.a: 50000}), "more")
    assert res["status"] in ("APPLIED", orders.PENDING)


# ------------------------------------------------------------------ AUD-091: issue_batch

def test_issue_batch_reports_any_failure_per_po_and_goes_on(world, monkeypatch):
    of_id = world.approved(items=[world.item(alloc={NORTH: 1, SOUTH: 1})])
    pos = [p["id"] for p in order_forms.generate_pos(world.conn, world.buyer, of_id)]
    real = pdf.render_supplier_po

    def flaky(conn, po_id, *a, **kw):
        if po_id == pos[0]:
            raise RuntimeError("printer driver broke")
        return real(conn, po_id, *a, **kw)
    monkeypatch.setattr(pdf, "render_supplier_po", flaky)
    done, problems = orders.issue_batch(world.conn, world.buyer, pos)
    assert done == [pos[1]]
    assert len(problems) == 1 and "printer driver broke" in problems[0]
    assert orders.get_po(world.conn, pos[0])["status"] == orders.APPROVED  # rolled back, nothing half done


# ------------------------------------------------------------------ AUD-118: sync_parent_of

def test_sync_parent_of_needs_a_po_changing_permission(w):
    before = w.conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action = 'OF_SYNC'").fetchone()[0]
    for who in (w.viewer, w.r1, w.acct, w.warehouse):
        with pytest.raises(PermissionDenied), db.tx(w.conn):
            orders.sync_parent_of(w.conn, who, w.of_id, "probe")
    assert w.conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action = 'OF_SYNC'").fetchone()[0] == before
    for who in (w.buyer, w.mgr, w.admin):
        with db.tx(w.conn):
            orders.sync_parent_of(w.conn, who, w.of_id, "allowed")


# ------------------------------------------------------------------ AUD-133: write_guard

def test_write_guard_refuses_to_work_outside_a_transaction(w):
    assert not w.conn.in_transaction
    with pytest.raises(RuntimeError):
        with orders.write_guard(w.conn):
            pass
    assert w.conn.execute("SELECT COUNT(*) FROM po_write_guard").fetchone()[0] == 0
    with db.tx(w.conn), orders.write_guard(w.conn):
        assert w.conn.execute("SELECT COUNT(*) FROM po_write_guard").fetchone()[0] == 1
    assert w.conn.execute("SELECT COUNT(*) FROM po_write_guard").fetchone()[0] == 0


# ------------------------------------------------------------------ AUD-106 / AUD-140: issued_pdf

def test_issued_pdf_is_refused_to_receivers_and_the_warehouse(w):
    for who in (w.r1, w.warehouse):
        with pytest.raises(PermissionDenied):
            orders.issued_pdf(w.conn, who, w.pid)
    assert not w.conn.execute("SELECT 1 FROM audit_logs WHERE action = 'PO_REPRINT'").fetchone()
    for who in (w.buyer, w.viewer, w.acct, w.admin):
        assert orders.issued_pdf(w.conn, who, w.pid)[:4] == b"%PDF"
    assert orders.issued_pdf(w.conn, None, w.pid)[:4] == b"%PDF"  # the program itself, as on the first issue


def test_issued_pdf_counts_earlier_reprints_inside_its_transaction(w):
    seen = []
    w.conn.set_trace_callback(lambda sql: seen.append((sql, w.conn.in_transaction)))
    try:
        orders.issued_pdf(w.conn, w.buyer, w.pid)
    finally:
        w.conn.set_trace_callback(None)
    counts = [inside for sql, inside in seen if "PO_REPRINT" in sql and sql.lstrip().upper().startswith("SELECT")]
    assert counts and all(counts)


# ------------------------------------------------------------------ AUD-160: can_receive_outside

def test_receive_outside_is_not_offered_while_an_amendment_waits(w):
    assert orders.can_receive_outside(w.conn, w.pid)
    orders.amend(w.conn, w.buyer, w.pid, {}, _rows(w, {w.a: 12000}), "more")
    assert not orders.can_receive_outside(w.conn, w.pid)
    with pytest.raises(POError, match="waiting for approval"):
        orders.receive_outside(w.conn, w.admin, w.pid, "RR 1")


# ------------------------------------------------------------------ AUD-044 / AUD-013

def test_unused_approval_history_is_gone_from_orders():
    assert not hasattr(orders, "approval_history")
    assert hasattr(order_forms, "approval_history")  # the OF one is used by the OF screen


def test_status_labels_are_sentence_case():
    assert orders.STATUS_LABELS[orders.PARTIAL] == "Partially delivered"
    assert orders.STATUS_LABELS[orders.FULL] == "Fully delivered"
    assert orders.STATUS_LABELS[orders.PENDING] == "Pending approval"
    for label in orders.STATUS_LABELS.values():
        assert label[0].isupper() and label[1:] == label[1:].lower()
