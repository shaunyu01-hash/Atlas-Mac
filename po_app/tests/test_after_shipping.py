"""A branch counts a delivery only once the Central Warehouse shipped it there, and the warehouse may record a supplier collecting."""
from __future__ import annotations

import pytest

from atlaspo import auth, claim_stubs, item_passes, order_forms, orders, receiving, warehouse
from atlaspo.db import POError, PermissionDenied

from conftest import NORTH, PASSWORD


@pytest.fixture
def w(world):
    world.conn.execute("UPDATE system_settings SET value = '1' WHERE key = 'receiving.after_shipping'")
    world.of_id = world.approved(items=[world.item(cost=10000, alloc={NORTH: 10}, description="Socks", item_code="SOCK-1")])
    po = order_forms.generate_pos(world.conn, world.buyer, world.of_id)[0]
    orders.issue(world.conn, world.buyer, po["id"])
    world.po_id = po["id"]
    world.socks = orders.get_po(world.conn, po["id"])["items"][0]["id"]
    make = lambda name: auth.create_user(world.conn, world.admin, name, name.title(), PASSWORD, ["RECEIVER"], must_change=False, branch_code=NORTH)
    world.r1, world.r2 = make("ana"), make("ben")
    return world


def _ship(w, pieces="10", another=False):
    warehouse.receive(w.conn, w.warehouse, w.of_id, {w.po_id: "1"}, stickered={w.socks: pieces}, another=another)
    warehouse.ship(w.conn, w.warehouse, w.of_id)


def test_a_branch_counts_only_what_the_warehouse_shipped(w):
    assert not receiving.to_count(w.conn, w.r1)
    with pytest.raises(POError, match="hasn't been shipped"):
        receiving.sheet(w.conn, w.r1, w.po_id)
    with pytest.raises(POError, match="hasn't been shipped"):
        receiving.save_count(w.conn, w.r1, w.po_id, {w.socks: "10"})
    _ship(w)
    assert [r["id"] for r in receiving.to_count(w.conn, w.r1)] == [w.po_id]
    receiving.save_count(w.conn, w.r1, w.po_id, {w.socks: "6"}, boxes="1")
    # The second count is of goods already there, so it's never held.
    assert receiving.save_count(w.conn, w.r2, w.po_id, {w.socks: "6"}, boxes="1")["outcome"] == receiving.APPROVED
    # The rest is a new delivery, and waits for its own shipment.
    with pytest.raises(POError, match="hasn't been shipped"):
        receiving.save_count(w.conn, w.r1, w.po_id, {w.socks: "4"})


def test_without_a_warehouse_login_branches_count_as_before(w):
    auth.update_user(w.conn, w.admin, w.warehouse.id, w.warehouse.full_name, ["WAREHOUSE"], False)
    assert auth.warehouse_login_missing(w.conn)
    assert receiving.save_count(w.conn, w.r1, w.po_id, {w.socks: "10"})["outcome"] == receiving.WAITING


def test_the_warehouse_may_record_a_supplier_collecting(world):
    assert world.warehouse.can("goods.release") and world.buyer.can("goods.release")
    viewer = auth.create_user(world.conn, world.admin, "vic", "Vic", PASSWORD, ["VIEWER"], must_change=False)
    for fn in (claim_stubs.collected, item_passes.collected):
        with pytest.raises(PermissionDenied):
            fn(world.conn, viewer, 1, "Rico")
