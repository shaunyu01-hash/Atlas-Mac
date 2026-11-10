"""Audit fixes in order_forms.py: one splitting rule, matching approval amounts, the branch filter,
amendments that change nothing or only internal notes, and number and date checks."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from atlaspo import db, money, order_forms, orders
from atlaspo.db import POError

from conftest import NORTH, SOUTH, EAST


def _approvals(conn, of_id):
    return [(r["decision"], r["amount_c"]) for r in conn.execute(
        "SELECT decision, amount_c FROM of_approvals WHERE of_id = ? AND amendment_id IS NULL ORDER BY id", (of_id,))]


# ------------------------------------------------------------------ AUD-043 one splitting rule

def test_split_order_uses_the_largest_remainder_rule(world):
    assert not hasattr(order_forms, "_shares")
    items = [world.item(dept=d, cost=c, description=f"Dept {d}") for d, c in ((1, 200), (2, 200), (3, 100), (4, 100))]
    of_id = world.draft(items=items, other_charges_c=5, overall_discount_type=money.DISC_AMOUNT, overall_discount_value=5)
    ids = [i for i, _ in order_forms.submit_order(world.conn, world.buyer, of_id)]
    rows = [world.conn.execute("SELECT other_charges_c, overall_discount_value FROM order_forms WHERE id = ?", (i,)).fetchone() for i in ids]
    # The old rule gave every leftover centavo to the largest: [4, 1, 0, 0].
    assert [r["other_charges_c"] for r in rows] == [2, 1, 1, 1]
    assert [r["overall_discount_value"] for r in rows] == [2, 1, 1, 1]
    assert list(money.split_by_weight(5, dict(enumerate([2, 2, 1, 1]))).values()) == [2, 1, 1, 1]


# ------------------------------------------------------------------ AUD-072 the same amount on SUBMIT and APPROVE

def test_split_order_records_the_whole_order_on_submit_and_approve(world):
    conn = world.conn
    # 600.00 + 500.00 = 1,100.00 as one order; each half alone is under the 1,000.00 limit.
    db.set_settings(conn, world.admin, {"approval.threshold_c": "100000"})
    items = [world.item(dept=1, cost=60000, description="Shirt"), world.item(dept=2, cost=50000, description="Blouse")]
    of_id = world.draft(items=items)
    results = order_forms.submit_order(conn, world.buyer, of_id)
    assert [s for _, s in results] == [orders.PENDING, orders.PENDING]
    for i, _ in results:
        order_forms.approve(conn, world.admin, i)
        assert _approvals(conn, i) == [("SUBMITTED", 110000), ("APPROVED", 110000)]


def test_largest_branch_basis_records_the_same_amount_on_submit_and_approve(world):
    conn = world.conn
    db.set_settings(conn, world.admin, {"approval.threshold_c": "100", "approval.basis": "LARGEST_BRANCH"})
    of_id = world.draft(items=[world.item(cost=10000, alloc={NORTH: 3, SOUTH: 1})])
    assert order_forms.submit(conn, world.buyer, of_id) == orders.PENDING
    order_forms.approve(conn, world.admin, of_id)
    # Before the fix: SUBMITTED 30000 (the largest branch) but APPROVED 40000 (the whole OF), both labelled LARGEST_BRANCH.
    assert _approvals(conn, of_id) == [("SUBMITTED", 30000), ("APPROVED", 30000)]


# ------------------------------------------------------------------ AUD-144 branch filter

def test_branch_filter_finds_the_same_orders_without_a_correlated_branch_scan(world):
    conn = world.conn
    made = {NORTH: set(), SOUTH: set()}
    for n in range(12):
        alloc = {NORTH: 1} if n % 3 == 0 else {SOUTH: 2} if n % 3 == 1 else {NORTH: 1, SOUTH: 1}
        of_id = world.draft(items=[world.item(alloc=alloc, description=f"Row {n}")])
        for code in alloc:
            made[code].add(of_id)
    for code, want in made.items():
        assert {r["id"] for r in order_forms.search(conn, branch_code=code)} == want
    assert order_forms.search(conn, branch_code=EAST) == []
    seen = []
    conn.set_trace_callback(seen.append)
    try:
        order_forms.search(conn, branch_code=NORTH)
    finally:
        conn.set_trace_callback(None)
    sql = next(s for s in seen if "FROM order_forms f" in s)
    plan = {r[0]: (r[1], r[3]) for r in conn.execute("EXPLAIN QUERY PLAN " + sql)}
    for node, (parent, detail) in plan.items():
        if "idx_of_alloc_branch" not in detail:
            continue
        while parent in plan:
            parent, above = plan[parent]
            assert "CORRELATED" not in above, "the branch index is used once per OF"


# ------------------------------------------------------------------ AUD-172, AUD-086 amendments

def test_of_amendment_of_internal_notes_only_makes_no_revision(world):
    conn = world.conn
    item = world.item(alloc={NORTH: 3})
    of_id = world.approved(items=[item])
    result = order_forms.amend(conn, world.buyer, of_id, {"internal_notes": "Call Mr. Cruz first"}, [item], "note")
    of = order_forms.get_of(conn, of_id)
    assert result == {"amendment_id": None, "status": "APPLIED", "revision": 0}
    assert of["revision"] == 0 and of["internal_notes"] == "Call Mr. Cruz first"
    assert conn.execute("SELECT COUNT(*) FROM of_amendments WHERE of_id = ?", (of_id,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM audit_logs WHERE of_id = ? AND action = 'OF_NOTES'", (of_id,)).fetchone()[0] == 1


def test_of_amendment_with_no_change_is_refused(world):
    conn = world.conn
    item = world.item(alloc={NORTH: 3, SOUTH: 2}, item_code="SKU-1", description="Polo")
    of_id = world.approved(items=[item])
    with pytest.raises(POError, match="Nothing changed"):
        order_forms.amend(conn, world.buyer, of_id, {}, [item], "no change")
    # The rows as the screen loads them back count as unchanged too.
    with pytest.raises(POError, match="Nothing changed"):
        order_forms.amend(conn, world.buyer, of_id, {}, order_forms.get_of(conn, of_id)["items"], "no change")
    assert order_forms.get_of(conn, of_id)["revision"] == 0
    assert conn.execute("SELECT COUNT(*) FROM of_amendments WHERE of_id = ?", (of_id,)).fetchone()[0] == 0
    # A real change still makes a revision.
    result = order_forms.amend(conn, world.buyer, of_id, {}, [dict(item, allocations={NORTH: 2, SOUTH: 2})], "less")
    assert result["status"] == "APPLIED" and result["revision"] == 1


# ------------------------------------------------------------------ AUD-081 numbers out of range

@pytest.mark.parametrize("over, words", [
    (dict(cost=10**17), "unit cost is too large"),
    (dict(cost=-1), "can't be negative"),
    (dict(fx_rate_m=-1000), "exchange rate can't be negative"),
    (dict(fx_cost_m=-1), "foreign cost can't be negative"),
    (dict(selling_c=-100), "selling price can't be negative"),
    (dict(selling_c=10**14), "selling price is too large"),
    (dict(pieces_per_unit=-3), "pieces per unit"),
    (dict(alloc={NORTH: "10000000000"}), "branch quantity is too large"),
    (dict(cost="abc"), "must be numbers"),
])
def test_out_of_range_item_numbers_are_refused_before_saving(world, over, words):
    kw = {k: over.pop(k) for k in ("cost", "alloc") if k in over}
    before = world.conn.execute("SELECT COUNT(*) FROM order_forms").fetchone()[0]
    with pytest.raises(POError, match=words):
        world.draft(items=[world.item(**kw, **over)])
    assert world.conn.execute("SELECT COUNT(*) FROM order_forms").fetchone()[0] == before


def test_unit_cost_of_1e17_on_5_pieces_is_not_saved_as_a_draft(world):
    with pytest.raises(POError):
        world.draft(items=[world.item(cost=10**17, alloc={NORTH: 5})])
    assert world.conn.execute("SELECT COUNT(*) FROM order_forms").fetchone()[0] == 0


@pytest.mark.parametrize("over, words", [
    (dict(other_charges_c=-1), "other charges can't be negative"),
    (dict(other_charges_c=10**19), "other charges is too large"),
    (dict(other_charges_c="abc"), "must be numbers"),
    (dict(vat_rate_bp=-1), "VAT rate"),
])
def test_out_of_range_header_numbers_are_refused(world, over, words):
    with pytest.raises(POError, match=words):
        world.draft(**over)
    assert world.conn.execute("SELECT COUNT(*) FROM order_forms").fetchone()[0] == 0


def test_large_but_sane_amounts_still_save(world):
    of_id = world.draft(items=[world.item(cost=10**9, alloc={NORTH: 1000})], other_charges_c=10**8)
    assert world.conn.execute("SELECT grand_total_c FROM order_forms WHERE id = ?", (of_id,)).fetchone()[0] == 10**12 + 10**8


# ------------------------------------------------------------------ AUD-087 dates

@pytest.mark.parametrize("value", ["garbage", "2026-02-30", "2026-13-01", "2026-11-07T10:00", 20261107, "tomorrow"])
def test_bad_of_dates_are_refused(world, value):
    with pytest.raises(POError, match="not a real date"):
        world.draft(of_date=value)
    with pytest.raises(POError, match="not a real date"):
        world.draft(expected_delivery_date=value)
    assert world.conn.execute("SELECT COUNT(*) FROM order_forms").fetchone()[0] == 0


def test_of_dates_are_stored_as_year_month_day(world):
    of_id = world.draft(of_date="20261101", expected_delivery_date=" 2026-12-05 ")
    row = world.conn.execute("SELECT of_date, expected_delivery_date FROM order_forms WHERE id = ?", (of_id,)).fetchone()
    assert (row["of_date"], row["expected_delivery_date"]) == ("2026-11-01", "2026-12-05")
    blank = world.draft(of_date="", expected_delivery_date="")
    row = world.conn.execute("SELECT of_date, expected_delivery_date FROM order_forms WHERE id = ?", (blank,)).fetchone()
    assert row["of_date"] == db.today().isoformat() and row["expected_delivery_date"] is None
    assert world.conn.execute("SELECT of_date FROM order_forms WHERE id = ?", (world.draft(of_date="2028-02-29"),)).fetchone()[0] == "2028-02-29"


def test_of_screen_amendment_with_no_change_or_notes_only(world, monkeypatch):
    """The rows the OF screen collects back count as unchanged, so the office sees the message instead of a needless revision."""
    from PySide6.QtWidgets import QApplication
    from atlaspo.ui import of_page as of_module
    from atlaspo.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(of_module, "ask_text", lambda *a, **k: "check")
    monkeypatch.setattr(of_module, "info", lambda *a, **k: None)
    of_id = world.approved(items=[world.item(cost=12345, alloc={NORTH: 3, SOUTH: 2}, item_code="SKU-9", description="Polo", size="M", disc1_bp=500)],
                           overall_discount_type=money.DISC_AMOUNT, overall_discount_value=100, other_charges_c=250, remarks="Rush")
    w = MainWindow(world.conn, world.buyer)
    try:
        w.open_of(of_id)
        page = w.of_page
        page.start_amendment()
        with pytest.raises(POError, match="Nothing changed"):
            page.save_amendment()
        page.start_amendment()
        page.internal.setText("Ask for Mr. Cruz")
        page.save_amendment()
        of = order_forms.get_of(world.conn, of_id)
        assert of["revision"] == 0 and of["internal_notes"] == "Ask for Mr. Cruz"
    finally:
        w.close()
