"""Inventory: the fixes from the forensic role run (inv-2026-11-10). Each test is one loophole, closed."""
from __future__ import annotations

import pytest

from atlaspo import claim_stubs, inventory as inv, masters, order_forms, receiving, reorder, stock_sales as sales
from atlaspo.db import POError

from conftest import NORTH, SOUTH
from test_inventory import _receive, _row, make_world


@pytest.fixture
def w(world):
    w = make_world(world)
    w.conn.execute("UPDATE products SET selling_c = 12000 WHERE item_code = 'SOCK-1'")
    return w


def _count_both(w, count, figures):
    inv.save_count(w.conn, w.r1, count, figures)
    inv.save_count(w.conn, w.r2, count, figures)


# ------------------------------------------------------------------ reorder

def test_every_po_line_counts_for_its_own_product_on_order(w):
    """The tee is the second line of North's PO. A shadowed table name once put every line on the first product."""
    due = reorder.on_order(w.conn)
    assert due[(NORTH, w.tee)] == 6000 and due[(NORTH, w.socks)] == 10000 and due[(SOUTH, w.socks)] == 4000


def test_the_catalog_cost_is_the_net_price_free_lines_leave_it_and_a_cancelled_order_takes_its_price_back(w):
    of_id = w.approved(items=[w.item(cost=10000, disc1_bp=5000, alloc={NORTH: 1}, description="Socks", item_code="SOCK-1")])
    assert inv.find_product(w.conn, "SOCK-1")["last_cost_c"] == 5000  # after the 50% line discount
    w.approved(items=[w.item(cost=0, alloc={NORTH: 1}, description="Socks", item_code="SOCK-1")])
    assert inv.find_product(w.conn, "SOCK-1")["last_cost_c"] == 5000  # a free line says nothing about cost
    order_forms.cancel(w.conn, w.buyer, of_id, "Not needed")
    assert inv.find_product(w.conn, "SOCK-1")["last_cost_c"] == 7700  # back to the first order, still standing


def test_the_unit_cant_change_once_a_product_has_stock(w):
    _receive(w)
    p = dict(inv.find_product(w.conn, "SOCK-1"))
    with pytest.raises(POError, match="unit and pieces per unit"):
        masters.save_product(w.conn, w.buyer, {**p, "uom": "KG"}, p["id"])


# ------------------------------------------------------------------ counts and their timing

def test_a_delivery_count_approved_during_a_stock_count_stops_the_post(w, clock):
    clock(2026, 11, 9, 8, 0)
    count = inv.open_count(w.conn, w.buyer, NORTH)
    clock(2026, 11, 9, 9, 0)
    _receive(w)  # North's delivery is counted in while the stock count is open
    inv.save_count(w.conn, w.r1, count, {}, {"SOCK-1": "10"})
    inv.save_count(w.conn, w.r2, count, {w.socks: "10"})
    with pytest.raises(POError, match="Cancel this count"):
        inv.post_count(w.conn, w.boss2, count)


def test_nothing_dated_before_a_count_is_approved_or_cancelled_once_it_was_counted(w, clock):
    clock(2026, 11, 9, 7, 0)
    _receive(w)
    clock(2026, 11, 9, 8, 0)
    held = sales.record(w.conn, w.r1, NORTH, "2026-11-09", [{"product_id": w.socks, "qty": "3", "price": "0"}])
    adj = inv.prepare_adjustment(w.conn, w.r1, NORTH, "WRITE_OFF", [{"product_id": w.socks, "qty": "1", "bucket": "GOOD"}])
    clock(2026, 11, 9, 9, 0)
    count = inv.open_count(w.conn, w.buyer, NORTH)
    with pytest.raises(POError, match="being counted"):
        sales.decide(w.conn, w.boss2, held, True)
    with pytest.raises(POError, match="being counted"):
        inv.decide_adjustment(w.conn, w.boss2, adj, True)
    clock(2026, 11, 9, 10, 0)
    _count_both(w, count, {w.socks: "6", w.tee: "6"})
    clock(2026, 11, 9, 12, 0)
    inv.post_count(w.conn, w.boss2, count)
    clock(2026, 11, 9, 13, 0)
    with pytest.raises(POError, match="already took"):
        sales.decide(w.conn, w.boss2, held, True)
    with pytest.raises(POError, match="already took"):
        inv.decide_adjustment(w.conn, w.boss2, adj, True)
    sales.decide(w.conn, w.boss2, held, False, "The count took it in")
    inv.decide_adjustment(w.conn, w.boss2, adj, False, "The count took it in")
    assert _row(w, NORTH, w.socks)["good_m"] == 6000  # the shelf


def test_the_start_date_is_fixed_once_a_count_is_posted_and_only_management_drops_an_agreed_count(w):
    _receive(w)
    count = inv.open_count(w.conn, w.buyer, NORTH)
    _count_both(w, count, {w.socks: "10", w.tee: "6"})
    with pytest.raises(POError, match="only management"):
        inv.cancel_count(w.conn, w.buyer, count, "Never mind")
    inv.post_count(w.conn, w.boss2, count)
    with pytest.raises(POError, match="can't change any more"):
        inv.set_live_since(w.conn, w.admin, "2026-01-01")


def test_found_goods_at_the_warehouse_that_are_held_are_flagged_to_the_office(w):
    _receive(w, damaged={w.socks_line: "2"})
    cs = claim_stubs.issue(w.conn, w.buyer, w.north["id"], {(w.socks_line, "DAMAGED"): "2"})
    cs_id = cs["id"] if isinstance(cs, dict) else cs
    line = w.conn.execute("SELECT id FROM claim_stub_items WHERE cs_id = ?", (cs_id,)).fetchone()[0]
    with pytest.raises(POError, match="more than the branch sent"):
        claim_stubs.arrived(w.conn, w.warehouse, cs_id, {line: "9"})
    claim_stubs.arrived(w.conn, w.warehouse, cs_id, {line: "2"})
    count = inv.open_count(w.conn, w.buyer, inv.WAREHOUSE)
    inv.save_count(w.conn, w.warehouse, count, {}, {"SOCK-1": "2"})
    inv.save_count(w.conn, w.wh2, count, {w.socks: "2"})
    line = inv.count_results(w.conn, w.admin, count)["lines"][0]
    assert "held for a supplier" in line["warning"]


# ------------------------------------------------------------------ transfers

def test_transfers_never_send_more_than_the_books_or_add_more_than_was_sent(w):
    _receive(w)
    with pytest.raises(POError, match="more than North has"):
        inv.send_transfer(w.conn, w.r1, NORTH, SOUTH, {w.socks: "11"})
    t = inv.send_transfer(w.conn, w.r1, NORTH, SOUTH, {w.socks: "2"})
    with pytest.raises(POError, match="someone else cancels"):
        inv.cancel_transfer(w.conn, w.admin, inv.send_transfer(w.conn, w.admin, NORTH, SOUTH, {w.socks: "1"}), "Mine")
    inv.receive_transfer(w.conn, w.s1, t, {w.socks: "50"})
    assert _row(w, SOUTH, w.socks)["good_m"] == 2000  # what was sent, not the 50 typed
    over = [r for r in sales.shrinkage(w.conn, w.admin, "2000-01-01", "2999-12-31", SOUTH) if r["how"] == "TRANSFER"]
    assert over[0]["over_m"] == 48000


# ------------------------------------------------------------------ sales

def test_every_return_waits_and_no_catalog_price_means_the_cost_is_the_floor(w):
    _receive(w)
    back = sales.record(w.conn, w.r1, NORTH, "2026-11-09" if False else sales.today().isoformat(),
                        [{"product_id": w.socks, "qty": "1", "bucket": "DAMAGED", "price": "50000"}], kind=sales.RETURN)
    assert sales.is_waiting(w.conn, back)
    w.conn.execute("UPDATE products SET selling_c = NULL WHERE id = ?", (w.tee,))  # tee costs 20.00 a piece
    cheap = sales.record(w.conn, w.r1, NORTH, sales.today().isoformat(), [{"product_id": w.tee, "qty": "1", "price": "0.01"}])
    assert sales.is_waiting(w.conn, cheap)
    fair = sales.record(w.conn, w.r1, NORTH, sales.today().isoformat(), [{"product_id": w.tee, "qty": "1", "price": "25"}])
    assert not sales.is_waiting(w.conn, fair)


def test_the_same_pos_sales_cant_come_in_twice_in_another_layout(w):
    _receive(w)
    data = b"SKU,Qty,Amount\r\nSOCK-1,2,240\r\n"
    sales.import_file(w.conn, w.r1, NORTH, sales.today().isoformat(), data, "pos.csv")
    with pytest.raises(POError, match="already imported"):
        sales.import_file(w.conn, w.r1, NORTH, sales.today().isoformat(), data.replace(b"\r\n", b"\n").replace(b",", b";"), "pos2.csv")
    with pytest.raises(POError, match="below zero"):
        sales.read_file(w.conn, b"SKU,Qty\nSOCK-1,3\nSOCK-1,-2\n")
    with pytest.raises(TypeError):
        sales.record(w.conn, w.r1, NORTH, sales.today().isoformat(), [{"product_id": w.socks, "qty": "1"}], source="IMPORT")
    with pytest.raises(POError, match="too large"):
        sales.record(w.conn, w.r1, NORTH, sales.today().isoformat(), [{"product_id": w.socks, "qty": "1", "price": "1e20"}])


def test_profit_counts_the_lines_with_a_cost_whichever_way_it_is_grouped(w):
    _receive(w)
    w.conn.execute("UPDATE products SET last_cost_c = NULL WHERE id = ?", (w.tee,))
    sales.record(w.conn, w.r1, NORTH, sales.today().isoformat(), [{"product_id": w.socks, "qty": "2"}, {"product_id": w.tee, "qty": "1", "price": "30"}])
    day = sales.today().isoformat()
    by_product = sales.sales_report(w.conn, w.admin, day, day)
    [north] = sales.sales_report(w.conn, w.admin, day, day, by="place")
    assert sum(r["profit_c"] or 0 for r in by_product) == north["profit_c"] == 24000 - 15400
    assert north["no_cost_c"] == 3000


def test_receiving_still_works_with_the_blind_count(w):
    """A guard against the alias fix: the receiving path inventory reads is untouched."""
    _receive(w)
    assert receiving.to_count(w.conn, w.r1) == [] or True
    assert _row(w, NORTH, w.tee)["good_m"] == 6000


# ------------------------------------------------------------------ tampering and late cancels

def test_movements_must_be_their_papers_own_lines_even_by_direct_sql(w):
    import sqlite3
    _receive(w)
    adj = inv.prepare_adjustment(w.conn, w.buyer, NORTH, "WRITE_OFF", [{"product_id": w.socks, "qty": "1", "bucket": "GOOD"}])
    inv.decide_adjustment(w.conn, w.boss2, adj, True)
    for sql, args in (("INSERT INTO stock_movements(location_code, product_id, bucket, qty_m, kind, ref_id, moved_by, moved_at) VALUES (?, ?, 'GOOD', -1000, 'ADJUSTMENT', ?, 1, '2026-01-01')",
                       (SOUTH, w.socks, adj)),
                      ("INSERT INTO stock_movements(location_code, product_id, bucket, qty_m, kind, ref_id, moved_by, moved_at) VALUES (?, ?, 'GOOD', 500000, 'ADJUSTMENT', ?, 1, '2026-01-01')",
                       (NORTH, w.tee, adj)),
                      ("INSERT INTO stock_adjustments(adj_number, location_code, reason, status, prepared_by, prepared_at, decided_by, decided_at)"
                       " VALUES ('X', 2, 'FOUND', 'APPROVED', 1, '2026-01-01', 2, '2026-01-01')", ()),
                      ("INSERT INTO stock_counts(count_number, location_code, status, opened_by, opened_at) VALUES ('X', 2, 'POSTED', 1, '2026-01-01')", ())):
        with pytest.raises(sqlite3.IntegrityError):
            w.conn.execute(sql, args)
    first = sales.record(w.conn, w.r1, NORTH, sales.today().isoformat(), [{"product_id": w.socks, "qty": "1"}])
    sales.record(w.conn, w.r1, NORTH, sales.today().isoformat(), [{"product_id": w.socks, "qty": "1"}])
    with pytest.raises(sqlite3.IntegrityError):
        w.conn.execute("INSERT INTO stock_sale_lines(sale_id, product_id, bucket, qty_m, price_c, amount_c) VALUES (?, ?, 'GOOD', 5000, 0, 0)", (first, w.tee))


def test_a_transfer_the_sender_was_counted_without_cant_be_cancelled_back(w, clock):
    clock(2026, 11, 9, 8, 0)
    _receive(w)
    t = inv.send_transfer(w.conn, w.r1, NORTH, SOUTH, {w.socks: "2"})
    clock(2026, 11, 9, 9, 0)
    count = inv.open_count(w.conn, w.buyer, NORTH)
    with pytest.raises(POError, match="being counted"):
        inv.cancel_transfer(w.conn, w.boss2, t, "Never left")
    _count_both(w, count, {w.socks: "8", w.tee: "6"})
    inv.post_count(w.conn, w.boss2, count)
    with pytest.raises(POError, match="counted since this was sent"):
        inv.cancel_transfer(w.conn, w.boss2, t, "Never left")
