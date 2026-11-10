"""Branch receiving: two blind counts, and a record nothing can erase."""
from __future__ import annotations

import os
import sqlite3
from datetime import date, timedelta
from io import BytesIO

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from pypdf import PdfReader
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from atlaspo import auth, db, item_passes, order_forms, orders, pdf, receiving, schema
from atlaspo.db import PermissionDenied, POError
from atlaspo.receiving import APPROVED, COMPLETED, MISMATCH, REPLACED, TO_CHECK, WAITING
from atlaspo.ui.main_window import MainWindow
from atlaspo.ui.receiving_page import C_DAMAGED, C_PIECES

from conftest import NORTH, PASSWORD, SOUTH


@pytest.fixture
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def setup(world):
    """One issued order across two branches: Socks, 10 at North, and Tee pack, 6 pieces at North. Two receivers at North and one at South."""
    of_id = world.approved(items=[world.item(cost=7777, alloc={NORTH: 10, SOUTH: 4}, description="Socks", item_code="SOCK-1"),
                                  world.item(cost=5000, uom="3 IN 1", alloc={NORTH: 2, SOUTH: 1}, description="Tee pack", item_code="TEE-3")])
    pos = order_forms.generate_pos(world.conn, world.buyer, of_id)
    for p in pos:
        orders.issue(world.conn, world.buyer, p["id"])
    make = lambda name, branch: auth.create_user(world.conn, world.admin, name, name.title(), PASSWORD, ["RECEIVER"], must_change=False, branch_code=branch)
    world.r1, world.r2, world.other = make("ana", NORTH), make("ben", NORTH), make("cara", SOUTH)
    world.of_id, world.north, world.south = of_id, pos[0], pos[1]
    world.socks, world.tee = [i["id"] for i in orders.get_po(world.conn, pos[0]["id"])["items"]]
    return world


def _po(world):
    return orders.get_po(world.conn, world.north["id"])


def _count(w, who, socks="10", tee="6", **kw):
    pieces = {k: v for k, v in ((w.socks, socks), (w.tee, tee)) if v is not None}
    return receiving.save_count(w.conn, who, w.north["id"], pieces, **kw)


def _agree(w, socks="10", tee="6", **kw):
    _count(w, w.r1, socks, tee, **kw)
    return _count(w, w.r2, socks, tee, **kw)


def _statuses(w):
    return [r["status"] for r in reversed(receiving.branch_counts(w.conn, w.r1))]


def test_either_number_finds_only_the_receivers_own_branch(setup):
    w = setup
    number = w.north["po_number"]
    of_number = order_forms.get_of(w.conn, w.of_id)["of_number"]
    assert [p["id"] for p in receiving.find_pos(w.conn, w.r1, number)] == [w.north["id"]]
    assert [p["id"] for p in receiving.find_pos(w.conn, w.r1, f"  {of_number.lower()} ")] == [w.north["id"]]
    assert [p["id"] for p in receiving.find_pos(w.conn, w.other, of_number)] == [w.south["id"]]
    # Another branch's PO looks exactly like a number that doesn't exist.
    with pytest.raises(POError, match="for your branch"):
        receiving.find_pos(w.conn, w.other, number)
    with pytest.raises(POError, match="for your branch"):
        receiving.find_pos(w.conn, w.other, "2026-9-99999")
    with pytest.raises(POError):
        receiving.sheet(w.conn, w.other, w.north["id"])
    # Only receivers count. An Admin can see what was ordered, so a count by them wouldn't be blind.
    with pytest.raises(PermissionDenied):
        receiving.find_pos(w.conn, w.admin, of_number)


def test_the_count_sheet_is_blind(setup):
    w = setup
    for line in receiving.sheet(w.conn, w.r1, w.north["id"]):
        assert set(line) == {"id", "line_no", "item_code", "description", "size", "color", "uom", "previous"} and line["previous"] is None
    count = _count(w, w.r1, "8", "6")["count_id"]
    # A round that didn't match shows each person only their own earlier numbers, never the other count.
    _count(w, w.r2, "9", "6")
    assert [l["previous"] for l in receiving.sheet(w.conn, w.r1, w.north["id"])] == [(8000, 0)]
    assert [l["previous"] for l in receiving.sheet(w.conn, w.r2, w.north["id"])] == [(9000, 0)]
    for line in receiving.count_lines(w.conn, w.r1, count):
        assert not {"qty_m", "ordered_m", "unit_cost_c", "net_unit_c", "line_total_c", "difference_m"} & set(line)
    for row in receiving.branch_counts(w.conn, w.r2):
        assert not {"ordered_m", "received_m", "difference_m", "grand_total_c", "pieces_m"} & set(row)


def test_the_second_person_can_not_see_the_first_count(setup):
    w = setup
    first = _count(w, w.r1, "8", "6")["count_id"]
    with pytest.raises(POError, match="Only the person who counted"):
        receiving.count_lines(w.conn, w.r2, first)
    assert receiving.count_lines(w.conn, w.r1, first)
    # Nor after they disagree.
    second = _count(w, w.r2, "9", "6")["count_id"]
    with pytest.raises(POError, match="Only the person who counted"):
        receiving.count_lines(w.conn, w.r1, second)
    # The home office sees everyone's numbers.
    assert [l["pieces_m"] for l in receiving.review_lines(w.conn, w.buyer, first)] == [8000, 6000]
    assert [l["pieces_m"] for l in receiving.review_lines(w.conn, w.buyer, second)] == [9000, 6000]
    with pytest.raises(db.PermissionDenied):
        receiving.review_lines(w.conn, w.r1, first)


def test_receivers_can_not_read_the_home_office_results(setup):
    w = setup
    for fn, args in ((receiving.po_results, ()), (receiving.po_lines, (w.north["id"],)), (receiving.po_history, (w.north["id"],)), (receiving.by_receiver, ())):
        with pytest.raises(db.PermissionDenied):
            fn(w.conn, w.r1, *args)
    with pytest.raises(db.PermissionDenied):
        receiving.save_count(w.conn, w.buyer, w.north["id"], {})


def test_every_product_needs_a_number_and_zero_is_one(setup):
    w = setup
    with pytest.raises(POError, match="Tee pack"):
        _count(w, w.r1, "8", "")
    with pytest.raises((POError, ValueError)):
        _count(w, w.r1, "8", "-1")
    assert not receiving.branch_counts(w.conn, w.r1)
    assert _count(w, w.r1, "0", "0")["outcome"] == WAITING


def test_two_matching_counts_reach_the_po_and_one_does_not(setup):
    w = setup
    first = _count(w, w.r1, "10", "4")
    assert first == {"count_id": first["count_id"], "outcome": WAITING, "mismatched": 0}
    assert _po(w)["status"] == orders.ISSUED and sum(i["received_qty_m"] for i in _po(w)["items"]) == 0
    # The same person can't count twice, and another branch can't count it at all.
    with pytest.raises(POError, match="already counted"):
        _count(w, w.r1, "10", "4")
    with pytest.raises(POError, match="already counted"):
        receiving.sheet(w.conn, w.r1, w.north["id"])
    with pytest.raises(POError):
        _count(w, w.other, "10", "4")
    assert _count(w, w.r2, "10", "4")["outcome"] == APPROVED
    newest, oldest = receiving.branch_counts(w.conn, w.r1)
    assert (oldest["counted_name"], oldest["approved_name"], newest["counted_name"], newest["approved_name"]) == ("Ana", "Ben", "Ben", "Ana")
    assert {oldest["status"], newest["status"]} == {APPROVED}
    # 4 pieces of a 3 IN 1 is short of the 6 ordered, and counts don't have to make full packs.
    po = _po(w)
    assert po["status"] == orders.PARTIAL and po["delivery_count"] == 1
    assert [i["received_qty_m"] for i in po["items"]] == [10000, 1333]
    assert w.conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action = 'BRANCH_COUNT_MATCH' AND user_name = 'Ben'").fetchone()[0] == 1


def test_only_products_that_disagree_are_counted_again(setup):
    w = setup
    _count(w, w.r1, "10", "6")
    second = _count(w, w.r2, "9", "6")
    assert (second["outcome"], second["mismatched"]) == (MISMATCH, 1)
    # Nothing reaches the PO, not even the product that agreed, until the whole delivery has.
    assert _po(w)["status"] == orders.ISSUED and _po(w)["items"][1]["received_qty_m"] == 0
    assert [l["description"] for l in receiving.sheet(w.conn, w.r1, w.north["id"])] == ["Socks"]
    assert [r["need"] for r in receiving.to_count(w.conn, w.r1)] == [receiving.AGAIN]
    # The agreed product isn't asked for again, and whatever is sent for it is ignored.
    assert _count(w, w.r1, "10", None)["outcome"] == WAITING
    assert not receiving.to_count(w.conn, w.r1) and [r["need"] for r in receiving.to_count(w.conn, w.r2)] == [receiving.SECOND]
    assert _count(w, w.r2, "10", "99")["outcome"] == APPROVED
    po = _po(w)
    assert po["status"] == orders.FULL and [i["received_qty_m"] for i in po["items"]] == [10000, 2000] and po["delivery_count"] == 1
    assert _statuses(w) == [MISMATCH, MISMATCH, APPROVED, APPROVED]
    # All four counts stay, and the numbers show who was off.
    assert [h["pieces_m"] for h in receiving.po_history(w.conn, w.admin, w.north["id"])] == [16000, 15000, 10000, 10000]
    people = {p["full_name"]: p for p in receiving.by_receiver(w.conn, w.admin)}
    assert (people["Ana"]["wrong"], people["Ben"]["wrong"], people["Ben"]["mismatched"], people["Ben"]["recounts"]) == (0, 1, 1, 1)


def test_after_two_rounds_that_disagree_counting_stops_for_the_home_office(setup):
    w = setup
    _count(w, w.r1, "10", "6")
    assert receiving.po_results(w.conn, w.buyer)[0]["progress"] == receiving.COUNT_LABELS[WAITING]
    _count(w, w.r2, "9", "6")
    assert receiving.po_results(w.conn, w.buyer)[0]["progress"] == "Being recounted"
    _count(w, w.r1, "8", None)
    _count(w, w.r2, "7", None)
    assert receiving.po_results(w.conn, w.buyer)[0]["progress"] == "Check: 2 counts didn't match. Counting stopped"
    assert _po(w)["status"] == orders.ISSUED
    # Nobody at the branch can count it again, and it's off their list.
    assert not receiving.to_count(w.conn, w.r1) and not receiving.to_count(w.conn, w.r2)
    with pytest.raises(POError, match="counting has stopped"):
        _count(w, w.r2, "10", None)
    with pytest.raises(POError, match="permission"):
        receiving.reopen(w.conn, w.buyer, w.north["id"], "Try again")
    assert receiving.reopen_label(w.conn, w.north["id"]) == "Allow one more count"
    receiving.reopen(w.conn, w.admin, w.north["id"], "Called the branch: a carton was opened late")
    assert [r["need"] for r in receiving.to_count(w.conn, w.r1)] == [receiving.AGAIN]
    # One more round, and they settle it by agreeing.
    _count(w, w.r2, "10", None)
    assert _count(w, w.r1, "10", None)["outcome"] == APPROVED
    assert receiving.po_results(w.conn, w.buyer)[0]["progress"] == "" and _po(w)["status"] == orders.FULL


def test_damaged_has_to_match_too_and_stays_inside_the_count(setup):
    w = setup
    with pytest.raises(POError, match="more damaged than counted"):
        _count(w, w.r1, "2", "6", damaged={w.socks: "3"})
    _count(w, w.r1, "10", "6", damaged={w.socks: "3", w.tee: ""})
    # Same pieces but a different damaged figure is a mismatch.
    assert _count(w, w.r2, "10", "6", damaged={w.socks: "2"})["outcome"] == MISMATCH
    _count(w, w.r1, "10", None, damaged={w.socks: "3"})
    result = _count(w, w.r2, "10", None, damaged={w.socks: "3"})
    assert result["outcome"] == APPROVED and _po(w)["status"] == orders.FULL  # damaged pieces still arrived
    row = receiving.po_results(w.conn, w.admin)[0]
    assert (row["received_m"], row["difference_m"], row["damaged_m"]) == (16000, 0, 3000)
    assert [l["damaged_m"] for l in receiving.po_lines(w.conn, w.admin, w.north["id"])] == [3000, 0]
    assert [l["damaged_m"] for l in receiving.count_lines(w.conn, w.r1, result["count_id"] - 1)] == [3000]


def test_several_deliveries_add_up_to_full(setup):
    w = setup
    _agree(w, "6", "6")
    assert _po(w)["status"] == orders.PARTIAL
    assert not receiving.to_count(w.conn, w.r1)  # a second delivery is reached by typing its number
    assert receiving.find_pos(w.conn, w.r1, w.north["po_number"])
    assert [l["description"] for l in receiving.sheet(w.conn, w.r1, w.north["id"])] == ["Socks", "Tee pack"]
    _agree(w, "4", "0")
    po = _po(w)
    assert po["status"] == orders.FULL and po["delivery_count"] == 2
    assert [r["delivery_no"] for r in receiving.branch_counts(w.conn, w.r1)] == [2, 2, 1, 1]
    of = next(r for r in order_forms.search(w.conn) if r["id"] == w.of_id)
    assert of["delivery"] == "Partially delivered"  # South hasn't arrived


def test_an_accepted_delivery_can_be_recounted_and_stands_until_that_agrees(setup):
    w = setup
    first = _agree(w, "10", "6")["count_id"]
    assert _po(w)["status"] == orders.FULL
    # Matched means finished: only the home office can reopen it.
    with pytest.raises(POError, match="Only the home office"):
        _count(w, w.r2, "7", "6", recount_of=first)
    receiving.reopen(w.conn, w.admin, w.north["id"], "3 socks were for South")
    assert receiving.progress(w.conn, w.north["id"]) == "Reopened for recount"
    assert [r["need"] for r in receiving.to_count(w.conn, w.r2)] == [receiving.RECOUNT]
    assert _count(w, w.r2, "7", "6", recount_of=first)["outcome"] == WAITING
    assert _po(w)["status"] == orders.FULL and _po(w)["items"][0]["received_qty_m"] == 10000
    # The other person counts it as the second count, with no reason asked of anyone.
    assert [r["need"] for r in receiving.to_count(w.conn, w.r1)] == [receiving.SECOND]
    assert _count(w, w.r1, "7", "6")["outcome"] == APPROVED
    po = _po(w)
    assert po["status"] == orders.PARTIAL and po["delivery_count"] == 1 and po["items"][0]["received_qty_m"] == 7000
    assert _statuses(w) == [REPLACED, REPLACED, APPROVED, APPROVED]
    assert [h["pieces_m"] for h in receiving.po_history(w.conn, w.admin, w.north["id"])] == [16000, 16000, 13000, 13000]
    assert [i["pieces_m"] for i in receiving.count_lines(w.conn, w.r2, first)] == [10000, 6000]  # still on record
    with pytest.raises(POError, match="already been replaced"):
        _count(w, w.r1, "1", "1", recount_of=first)


def test_a_recount_that_disagrees_leaves_the_accepted_figures_alone(setup):
    w = setup
    first = _agree(w, "10", "6")["count_id"]
    receiving.reopen(w.conn, w.admin, w.north["id"], "Recount")
    _count(w, w.r1, "7", "6", recount_of=first)
    assert _count(w, w.r2, "8", "6")["outcome"] == MISMATCH
    assert _po(w)["status"] == orders.FULL and receiving.po_results(w.conn, w.admin)[0]["received_m"] == 16000
    with pytest.raises(POError, match="Only the home office"):
        _count(w, w.r1, "1", "1", recount_of=first)
    _count(w, w.r1, "8", None)
    assert _count(w, w.r2, "8", None)["outcome"] == APPROVED
    assert _po(w)["items"][0]["received_qty_m"] == 8000 and _po(w)["delivery_count"] == 1


def test_someone_can_redo_their_own_count_before_the_second_person(setup):
    w = setup
    first = _count(w, w.r1, "9", "6")["count_id"]
    with pytest.raises(POError, match="waiting for yours"):
        _count(w, w.r2, "9", "6", recount_of=first)
    second = _count(w, w.r1, "10", "6", recount_of=first)
    assert second["outcome"] == WAITING and _statuses(w) == [REPLACED, WAITING]
    assert _count(w, w.r2, "10", "6")["outcome"] == APPROVED
    assert _po(w)["items"][0]["received_qty_m"] == 10000 and _po(w)["delivery_count"] == 1


def test_counts_can_not_be_altered_even_in_the_database(setup):
    w = setup
    _agree(w, damaged={w.socks: "1"})
    for sql in ("UPDATE branch_count_items SET pieces_m = 1", "DELETE FROM branch_count_items", "DELETE FROM branch_counts",
                f"UPDATE branch_counts SET counted_by = {w.r2.id}", "UPDATE branch_counts SET counted_at = 'x'", f"UPDATE branch_counts SET approved_by = {w.r1.id}",
                "UPDATE branch_counts SET round_no = 5", "UPDATE branch_counts SET attempt_no = 5",
                "UPDATE branch_received SET pieces_m = 1", "DELETE FROM branch_received",
                "UPDATE branch_count_damage SET damaged_m = 1", "DELETE FROM branch_count_damage"):
        with pytest.raises(sqlite3.Error):
            w.conn.execute(sql)
    # The received figures on a PO can't be typed in either.
    with pytest.raises(sqlite3.Error):
        w.conn.execute("UPDATE purchase_order_items SET received_qty_m = 5000")


def test_only_issued_pos_can_be_received(setup, monkeypatch):
    monkeypatch.setattr("atlaspo.receiving.live_since", lambda conn: "9999")  # as if issued before receiving existed, so it can close uncounted
    w = setup
    fresh = w.approved(items=[w.item(alloc={NORTH: 1}, description="Other")])
    po = order_forms.generate_pos(w.conn, w.buyer, fresh)[0]
    with pytest.raises(POError, match="can't be received"):
        receiving.find_pos(w.conn, w.r1, po["po_number"])
    orders.close(w.conn, w.buyer, w.north["id"], "Done")
    with pytest.raises(POError, match="can't be received"):
        receiving.find_pos(w.conn, w.r1, w.north["po_number"])


def test_the_list_shows_what_this_person_can_count(setup):
    w = setup
    fresh = w.approved(items=[w.item(alloc={NORTH: 1, SOUTH: 1}, description="Other")])
    made = order_forms.generate_pos(w.conn, w.buyer, fresh)
    assert [r["id"] for r in receiving.to_count(w.conn, w.r1)] == [w.north["id"]]  # not printed yet, so not listed
    orders.issue(w.conn, w.buyer, made[0]["id"])
    assert [(r["id"], r["need"]) for r in receiving.to_count(w.conn, w.r1)] == [(w.north["id"], receiving.FIRST), (made[0]["id"], receiving.FIRST)]
    assert [r["id"] for r in receiving.to_count(w.conn, w.other)] == [w.south["id"]]
    with pytest.raises(PermissionDenied):
        receiving.to_count(w.conn, w.admin)
    for row in receiving.to_count(w.conn, w.r1):
        assert set(row) == {"id", "po_number", "branch_name", "expected_delivery_date", "supplier", "of_number", "need"}
    # Short or complete, an accepted PO leaves the list the same way, so the list can't give a shortage away.
    _agree(w, "1", "0")
    assert [r["id"] for r in receiving.to_count(w.conn, w.r1)] == [made[0]["id"]]
    with pytest.raises(db.PermissionDenied):
        receiving.to_count(w.conn, w.buyer)


def test_home_office_sees_the_difference(setup):
    w = setup
    po_id = w.north["id"]
    _count(w, w.r1, "8", "9")
    row = receiving.po_results(w.conn, w.buyer)[0]
    assert row["received_m"] == 0 and row["waiting"] == 1  # nothing counts until two agree
    _count(w, w.r2, "8", "9")
    row = receiving.po_results(w.conn, w.buyer)[0]
    assert (row["ordered_m"], row["received_m"], row["difference_m"], row["waiting"]) == (16000, 17000, 1000, 0)
    assert [(l["description"], l["difference_m"]) for l in receiving.po_lines(w.conn, w.buyer, po_id)] == [("Socks", -2000), ("Tee pack", 3000)]
    assert not receiving.po_results(w.conn, w.buyer, "nothing-like-this")
    assert receiving.signed_qty(-2000) == "-2" and receiving.signed_qty(3000) == "+3" and receiving.signed_qty(0) == "0"


def test_only_an_exact_match_is_a_completed_delivery(setup):
    w = setup
    tab = lambda: receiving.po_results(w.conn, w.buyer)[0]["tab"]
    _count(w, w.r1)
    assert tab() == TO_CHECK  # one count isn't enough
    _count(w, w.r2)
    assert tab() == COMPLETED
    assert [r["id"] for r in receiving.po_results(w.conn, w.buyer, tab=COMPLETED)] == [w.north["id"]] and not receiving.po_results(w.conn, w.buyer, tab=TO_CHECK)
    # A recount in progress puts it back until that agrees.
    newest = receiving.branch_counts(w.conn, w.r1)[0]["id"]
    receiving.reopen(w.conn, w.admin, w.north["id"], "Recount")
    assert tab() == TO_CHECK
    _count(w, w.r1, recount_of=newest)
    assert tab() == TO_CHECK
    _count(w, w.r2)
    assert tab() == COMPLETED
    # Matched twice now, so it can't be reopened again.
    assert not receiving.reopen_label(w.conn, w.north["id"])
    with pytest.raises(POError, match="already reopened once"):
        receiving.reopen(w.conn, w.admin, w.north["id"], "Again")


@pytest.mark.parametrize("kwargs", [dict(socks="9"), dict(socks="11"), dict(damaged="1")])
def test_short_over_or_damaged_stays_to_check_even_when_agreed(setup, kwargs):
    w = setup
    if "damaged" in kwargs:
        kwargs = {"damaged": {w.socks: kwargs["damaged"]}}
    _count(w, w.r1, **kwargs)
    _count(w, w.r2, **kwargs)
    assert receiving.po_results(w.conn, w.buyer)[0]["tab"] == TO_CHECK


def _extra(**over):
    return {"item_code": "XTRA", "description": "Unordered Cap", "size": "M", "color": "Red", "pieces": "5"} | over


def test_a_product_not_on_the_po_is_counted_by_both_and_is_excess(setup):
    w = setup
    # It needs a name and a count, and can't duplicate what's on the PO.
    with pytest.raises(POError, match="name of the product"):
        _count(w, w.r1, extras=[_extra(description=" ")])
    with pytest.raises(POError, match="Unordered Cap"):
        _count(w, w.r1, extras=[_extra(pieces="")])
    with pytest.raises(POError, match="already on this PO"):
        _count(w, w.r1, extras=[_extra(item_code="sock-1", size="", color="")])
    with pytest.raises(POError, match="more damaged"):
        _count(w, w.r1, extras=[_extra(damaged="9")])
    assert not w.conn.execute("SELECT 1 FROM branch_extra_items").fetchone()  # refused saves leave nothing behind

    _count(w, w.r1, extras=[_extra()])
    # The second person sees the product and not the number, and has to count it too.
    lines = receiving.sheet(w.conn, w.r2, w.north["id"])
    extra_id = lines[-1]["id"]
    assert [l["description"] for l in lines] == ["Socks", "Tee pack", "Unordered Cap"] and extra_id < 0
    assert set(lines[-1]) == {"id", "line_no", "item_code", "description", "size", "color", "uom", "previous"}
    with pytest.raises(POError, match="Unordered Cap"):
        _count(w, w.r2)
    with pytest.raises(POError, match="already listed"):
        _count(w, w.r2, extras=[_extra()])
    result = receiving.save_count(w.conn, w.r2, w.north["id"], {w.socks: "10", w.tee: "6", extra_id: "5"})
    assert result["outcome"] == APPROVED
    # It's excess, not part of the PO: the PO is exactly right, and nothing joined the catalog.
    po = _po(w)
    assert po["status"] == orders.FULL and [i["received_qty_m"] for i in po["items"]] == [10000, 2000]
    assert not w.conn.execute("SELECT 1 FROM products WHERE item_code = 'XTRA'").fetchone()
    row = receiving.po_results(w.conn, w.buyer)[0]
    assert (row["excess_m"], row["excess_left_m"], row["tab"]) == (5000, 5000, TO_CHECK)
    assert [(l["description"], l["ordered_m"], l["received_m"], l["difference_m"]) for l in receiving.po_lines(w.conn, w.buyer, w.north["id"])][-1] == ("Unordered Cap (not on PO)", 0, 5000, 5000)


def test_a_product_the_second_person_adds_comes_back_to_both(setup):
    w = setup
    _count(w, w.r1)
    result = _count(w, w.r2, extras=[_extra()])
    assert (result["outcome"], result["mismatched"]) == (MISMATCH, 1)
    assert _po(w)["status"] == orders.ISSUED
    # What they agreed on isn't asked again, and the new product is, with no number from the other person.
    lines = receiving.sheet(w.conn, w.r1, w.north["id"])
    assert [l["description"] for l in lines] == ["Unordered Cap"]
    extra_id = lines[0]["id"]
    assert receiving.save_count(w.conn, w.r1, w.north["id"], {extra_id: "4"})["outcome"] == WAITING
    assert receiving.save_count(w.conn, w.r2, w.north["id"], {extra_id: "4"})["outcome"] == APPROVED
    assert _po(w)["status"] == orders.FULL and receiving.po_results(w.conn, w.buyer)[0]["excess_m"] == 4000


def test_a_product_added_by_mistake_is_settled_by_both_counting_zero(setup):
    w = setup
    _count(w, w.r1, extras=[_extra()])
    extra_id = receiving.sheet(w.conn, w.r2, w.north["id"])[-1]["id"]
    assert receiving.save_count(w.conn, w.r2, w.north["id"], {w.socks: "10", w.tee: "6", extra_id: "0"})["outcome"] == MISMATCH
    assert receiving.save_count(w.conn, w.r1, w.north["id"], {extra_id: "0"})["outcome"] == WAITING
    assert receiving.save_count(w.conn, w.r2, w.north["id"], {extra_id: "0"})["outcome"] == APPROVED
    row = receiving.po_results(w.conn, w.buyer)[0]
    assert (row["excess_m"], row["tab"]) == (0, COMPLETED)


def test_an_item_pass_prints_once_and_completes_the_delivery(setup):
    w = setup
    with pytest.raises(POError, match="no excess"):
        item_passes.issue(w.conn, w.r1, w.north["id"])
    _count(w, w.r1, "12", "6")
    with pytest.raises(POError, match="still being counted"):
        item_passes.issue(w.conn, w.r1, w.north["id"])
    assert not item_passes.to_print(w.conn, w.r1)
    _count(w, w.r2, "12", "6")
    row = receiving.po_results(w.conn, w.buyer)[0]
    assert (row["excess_m"], row["excess_left_m"], row["tab"]) == (2000, 2000, TO_CHECK)
    assert [r["pieces_m"] for r in item_passes.to_print(w.conn, w.r2)] == [2000] and not item_passes.to_print(w.conn, w.other)
    # Printed at the branch, not by the home office or another branch.
    with pytest.raises(db.PermissionDenied):
        item_passes.issue(w.conn, w.buyer, w.north["id"])
    with pytest.raises(POError, match="another branch"):
        item_passes.issue(w.conn, w.other, w.north["id"])
    made = item_passes.issue(w.conn, w.r1, w.north["id"])
    assert made["ip_number"].endswith("-IP-00001") and len(PdfReader(BytesIO(made["pdf"])).pages) == 2
    text = [page.extract_text() for page in PdfReader(BytesIO(made["pdf"])).pages]
    assert "ORIGINAL" in text[0] and "DUPLICATE" in text[1] and "Socks" in text[0] and "SOCK-1" in text[1]
    row = receiving.po_results(w.conn, w.buyer)[0]
    assert (row["excess_m"], row["excess_left_m"], row["tab"], row["item_passes"]) == (2000, 0, COMPLETED, made["ip_number"])
    # Nobody can print it again, either person.
    assert not item_passes.to_print(w.conn, w.r2)
    for who in (w.r1, w.r2):
        with pytest.raises(POError, match="already been printed"):
            item_passes.issue(w.conn, who, w.north["id"])
    # More excess later needs its own pass, for only the new pieces.
    _agree(w, "1", "0")
    row = receiving.po_results(w.conn, w.buyer)[0]
    assert (row["excess_m"], row["excess_left_m"], row["tab"]) == (3000, 1000, TO_CHECK)
    assert item_passes.items(w.conn, w.r2, item_passes.issue(w.conn, w.r2, w.north["id"])["id"])[0]["pieces_m"] == 1000


def test_a_lost_pass_is_cancelled_and_the_branch_prints_a_new_number(setup):
    w = setup
    _agree(w, "12", "6")
    first = item_passes.issue(w.conn, w.r1, w.north["id"])
    with pytest.raises(db.PermissionDenied):
        item_passes.cancel(w.conn, w.r1, first["id"], "Lost")
    with pytest.raises(POError, match="reason"):
        item_passes.cancel(w.conn, w.admin, first["id"], " ")
    item_passes.cancel(w.conn, w.admin, first["id"], "Lost on the way")
    row = receiving.po_results(w.conn, w.buyer)[0]
    assert (row["excess_left_m"], row["tab"], row["item_passes"]) == (2000, TO_CHECK, "")
    second = item_passes.issue(w.conn, w.r2, w.north["id"])
    assert second["ip_number"] != first["ip_number"]
    # Once the goods are at the warehouse, the pass stands.
    item_passes.arrived(w.conn, w.warehouse, second["id"], item_passes.as_on_paper(w.conn, second["id"]))
    with pytest.raises(POError, match="already reached"):
        item_passes.cancel(w.conn, w.admin, second["id"], "Too late")


def test_the_supplier_has_90_days_to_pick_up_excess(setup, monkeypatch):
    w = setup
    _agree(w, "12", "6")
    made = item_passes.issue(w.conn, w.r1, w.north["id"])
    with pytest.raises(POError, match="can only be collected"):
        item_passes.collected(w.conn, w.buyer, made["id"], "Rico")
    with pytest.raises(POError, match="can only be forfeited"):
        item_passes.forfeit(w.conn, w.buyer, made["id"])
    item_passes.arrived(w.conn, w.warehouse, made["id"], item_passes.as_on_paper(w.conn, made["id"]))
    assert item_passes.search(w.conn, w.buyer, status=item_passes.AT_WAREHOUSE)[0]["days_left"] == 90
    with pytest.raises(POError, match="can't be forfeited yet"):
        item_passes.forfeit(w.conn, w.buyer, made["id"])
    w.conn.execute("UPDATE item_passes SET arrived_at = ? WHERE id = ?", ((date.today() - timedelta(days=91)).isoformat(), made["id"]))
    item_passes.forfeit(w.conn, w.buyer, made["id"])
    assert item_passes.search(w.conn, w.buyer, status=item_passes.FORFEITED)[0]["ip_number"] == made["ip_number"]
    # A printed pass can't be altered or removed, and its pieces stay on record.
    for sql in ("UPDATE item_passes SET ip_number = 'X'", "DELETE FROM item_passes", "UPDATE item_pass_items SET pieces_m = 1", "DELETE FROM item_pass_items", "UPDATE item_passes SET pdf = x'00'"):
        with pytest.raises(sqlite3.Error):
            w.conn.execute(sql)


def test_a_pass_goes_straight_to_the_printer_with_one_copy(app, setup, tmp_path, monkeypatch):
    from PySide6.QtPrintSupport import QPrinter
    from atlaspo.ui.common import print_once
    w = setup
    _agree(w, "12", "6")
    made = item_passes.issue(w.conn, w.r1, w.north["id"])
    printer = QPrinter()
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(tmp_path / "printed.pdf"))
    printer.setCopyCount(5)
    print_once(printer, made["pdf"])
    assert printer.copyCount() == 1 and len(PdfReader(tmp_path / "printed.pdf").pages) == 2


def test_added_products_can_be_recounted_after_acceptance(setup):
    w = setup
    _count(w, w.r1, extras=[_extra()])
    extra_id = receiving.sheet(w.conn, w.r2, w.north["id"])[-1]["id"]
    accepted = receiving.save_count(w.conn, w.r2, w.north["id"], {w.socks: "10", w.tee: "6", extra_id: "5"})["count_id"]
    assert [l["id"] for l in receiving.sheet(w.conn, w.r1, w.north["id"], accepted)] == [w.socks, w.tee, extra_id]
    receiving.reopen(w.conn, w.admin, w.north["id"], "Recount the extra")
    receiving.save_count(w.conn, w.r1, w.north["id"], {w.socks: "10", w.tee: "6", extra_id: "3"}, recount_of=accepted)
    assert receiving.po_results(w.conn, w.buyer)[0]["excess_m"] == 5000  # stands until the recount agrees
    receiving.save_count(w.conn, w.r2, w.north["id"], {w.socks: "10", w.tee: "6", extra_id: "3"})
    assert receiving.po_results(w.conn, w.buyer)[0]["excess_m"] == 3000


def test_adding_a_product_and_returning_excess_on_screen(app, setup, monkeypatch):
    w = setup
    monkeypatch.setattr("atlaspo.ui.receiving_page.confirm", lambda *a, **k: True)
    monkeypatch.setattr("atlaspo.ui.item_pass_page.confirm", lambda *a, **k: True)
    win = MainWindow(w.conn, w.r1)
    page = win.receiving
    assert page.add_btn.isHidden()
    page.po_list.cellClicked.emit(0, 0)
    assert not page.add_btn.isHidden() and page.grid.rowCount() == 2
    page.add_extra_row()
    assert page.grid.rowCount() == 3 and page.grid.item(2, 1).flags() & Qt.ItemFlag.ItemIsEditable and not page.grid.item(2, 4).flags() & Qt.ItemFlag.ItemIsEditable
    page.grid.item(2, 0).setText("XTRA")
    page.grid.item(2, 1).setText("Unordered Cap")
    page.grid.item(2, C_PIECES).setText("5")
    # A blank line someone added and didn't use doesn't count.
    page.add_extra_row()
    _type(page, ("10", ""), ("6", ""))
    page.save_count()
    assert "Another receiver" in page.prompt.text()
    win.close()

    win = MainWindow(w.conn, w.r2)
    page = win.receiving
    page.po_list.cellClicked.emit(0, 0)
    assert page.grid.rowCount() == 3 and page.grid.item(2, 1).text() == "Unordered Cap (not on PO)" and page.grid.item(2, C_PIECES).text() == ""
    assert not page.grid.item(2, 1).flags() & Qt.ItemFlag.ItemIsEditable
    _type(page, ("10", ""), ("6", ""), ("5", ""))
    page.save_count()
    assert "Both counts match" in page.prompt.text()
    win.close()

    win = MainWindow(w.conn, w.buyer)
    results = win.received
    assert results.table.item(0, 10).text() == "Excess: branch to print item pass"
    results.table.selectRow(0)
    assert not results.dm_btn.isEnabled() and results.lines.item(2, 1).text() == "Unordered Cap (not on PO)" and not hasattr(results, "return_btn")
    win.close()

    # The branch prints it, and it leaves the list, so there's nothing left to print again.
    printed = []
    monkeypatch.setattr("atlaspo.ui.receiving_page.choose_printer", lambda parent: "printer")
    monkeypatch.setattr("atlaspo.ui.receiving_page.print_once", lambda printer, document: printed.append((printer, document)))
    win = MainWindow(w.conn, w.r2)
    page = win.receiving
    assert page.tabs.tabText(2) == "Item passes  (1)" and page.pass_waiting.item(0, 3).text() == "5" and page.print_pass_btn.isEnabled()
    page.print_pass()
    assert [p[0] for p in printed] == ["printer"] and page.tabs.tabText(2) == "Item passes" and page.pass_waiting.rowCount() == 0 and not page.print_pass_btn.isEnabled()
    assert page.pass_done.rowCount() == 1 and page.pass_done.item(0, 0).text().endswith("-IP-00001")
    win.close()

    # The home office sees it and follows it, with nothing to print.
    win = MainWindow(w.conn, w.buyer)
    results = win.received
    assert results.tabs.tabText(1) == "Completed delivery  1"
    results.tabs.setCurrentIndex(1)
    assert results.table.item(0, 10).text() == "Completed with item pass" and results.table.item(0, 8).text().endswith("-IP-00001")
    passes = win.item_passes
    assert passes.table.rowCount() == 1 and passes.table.item(0, 6).text() == "5"
    passes.table.selectRow(0)
    assert passes.arrived_btn.parent() is None and not passes.collected_btn.isEnabled() and not any(b.text().startswith("Print") for b in passes.findChildren(type(passes.arrived_btn)))
    yard = MainWindow(w.conn, w.warehouse)
    yard.item_passes.table.selectRow(0)
    assert yard.item_passes.arrived_btn.isEnabled()
    import atlaspo.ui.claim_stub_page as stub_page
    stub_page_count, stub_page.count_goods = stub_page.count_goods, lambda parent, number, lines: {i["id"]: str(i["pieces_m"] // 1000) for i in lines}
    yard.item_passes.arrived()
    stub_page.count_goods = stub_page_count
    yard.close()
    passes.refresh()
    assert passes.tabs.tabText(1).endswith("1")
    win.close()


def test_purchaser_is_the_role_name(app, world):
    assert auth.ROLES["BUYER"] == "Purchaser"
    assert "Buyer" not in auth.ROLES.values()


def test_a_receiver_needs_a_branch(world):
    with pytest.raises(POError, match="branch"):
        auth.create_user(world.conn, world.admin, "dee", "Dee", PASSWORD, ["RECEIVER"])
    dee = auth.create_user(world.conn, world.admin, "dee", "Dee", PASSWORD, ["RECEIVER"], branch_code=NORTH)
    assert dee.branch_code == NORTH
    assert next(u for u in auth.list_users(world.conn) if u["id"] == dee.id)["branch_code"] == NORTH
    with pytest.raises(POError, match="branch"):
        auth.update_user(world.conn, world.admin, dee.id, "Dee", ["RECEIVER"], True)
    auth.update_user(world.conn, world.admin, dee.id, "Dee", ["RECEIVER"], True, branch_code=SOUTH)
    assert auth.get_user(world.conn, dee.id).branch_code == SOUTH
    auth.update_user(world.conn, world.admin, dee.id, "Dee", ["BUYER"], True)
    assert auth.get_user(world.conn, dee.id).branch_code is None


def test_update_keeps_existing_users_and_roles(env):
    path = env / "old.sqlite"
    c = db.open_database(path)
    admin = auth.create_first_admin(c, "boss", "Boss", PASSWORD)
    c.execute("PRAGMA user_version = 8")
    c.close()
    conn = db.open_database(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
    assert auth.get_user(conn, admin.id).roles == {"ADMIN", "APPROVER"}
    assert auth.create_user(conn, admin, "r1", "R One", PASSWORD, ["RECEIVER"], branch_code=NORTH).roles == {"RECEIVER"}
    conn.close()


def test_counts_approved_the_old_way_carry_over(setup):
    """Written as version 10 stored them: one person counted, another approved. A count waiting then becomes a first count."""
    w = setup
    conn, po_id, stamp = w.conn, w.north["id"], "2026-11-03 10:00:00"
    ids = []
    for delivery, status, counted_by, approved_by, pieces in ((1, REPLACED, w.r1.id, w.r2.id, 16000), (1, APPROVED, w.r2.id, w.r1.id, 10000), (2, WAITING, w.r1.id, None, 3000)):
        cid = conn.execute("INSERT INTO branch_counts(po_id, delivery_no, status, counted_by, counted_at, approved_by, approved_at) VALUES (?,?,?,?,?,?,?)",
                           (po_id, delivery, status, counted_by, stamp, approved_by, stamp if approved_by else None)).lastrowid
        conn.execute("INSERT INTO branch_count_items(count_id, po_item_id, description, pieces_m) VALUES (?,?,?,?)", (cid, w.socks, "Socks", pieces))
        conn.execute("INSERT INTO branch_count_items(count_id, po_item_id, description, pieces_m) VALUES (?,?,?,?)", (cid, w.tee, "Tee pack", 0))
        ids.append(cid)
    conn.execute("INSERT INTO branch_count_damage(count_id, po_item_id, damaged_m) VALUES (?,?,?)", (ids[1], w.socks, 1000))
    # The file is already at the newest version, and _migrate skips a step the file has passed (another PC may have made it).
    # Without its version line the V11 rebuild runs on these rows as it did on a version 10 file, and the file keeps its version.
    db._migrate(conn, schema.MIGRATE_V11.replace("PRAGMA user_version = 11;", ""))
    rows = conn.execute("SELECT id, delivery_no, attempt_no, round_no, status FROM branch_counts ORDER BY id").fetchall()
    assert [tuple(r)[1:] for r in rows] == [(1, 1, 1, REPLACED), (1, 2, 1, APPROVED), (2, 1, 1, WAITING)]
    assert [tuple(r) for r in conn.execute("SELECT delivery_no, attempt_no, po_item_id, pieces_m, damaged_m FROM branch_received ORDER BY po_item_id")] == [(1, 2, w.socks, 10000, 1000), (1, 2, w.tee, 0, 0)]
    assert receiving.po_results(conn, w.admin)[0]["received_m"] == 10000
    assert receiving.count_lines(conn, w.r2, ids[1])
    assert [r["need"] for r in receiving.to_count(conn, w.r2)] == [receiving.SECOND]


def test_the_blind_sheet_has_a_place_for_damaged_and_still_hides_the_quantity(setup):
    text = PdfReader(BytesIO(pdf.render_blind_sheet(setup.conn, setup.north["id"]))).pages[0].extract_text()
    assert "Damaged" in text and "Pieces received" in text and "SOCK-1" in text and "77.77" not in text


def test_a_receiver_only_sees_receiving(app, setup):
    w = setup
    win = MainWindow(w.conn, w.r1)
    assert [text for text, _ in win.pages] == ["Receiving"]
    assert win.stack.currentWidget() is win.receiving
    win.new_of()
    win.refresh_all()
    assert win.stack.currentWidget() is win.receiving
    win.close()
    office = MainWindow(w.conn, w.admin)
    assert {"Received", "Orders"} <= {text for text, _ in office.pages} and "Receiving" not in {text for text, _ in office.pages}  # Admin doesn't count
    office.close()
    buyer = MainWindow(w.conn, w.buyer)
    names = {text for text, _ in buyer.pages}
    assert "Received" in names and "Receiving" not in names
    buyer.close()


def _type(page, *rows):
    for r, (pieces, damaged) in enumerate(rows):
        page.grid.item(r, C_PIECES).setText(pieces)
        page.grid.item(r, C_DAMAGED).setText(damaged)


def test_pressing_a_po_opens_it_for_counting(app, setup):
    w = setup
    win = MainWindow(w.conn, w.r1)
    page = win.receiving
    assert page.po_list.rowCount() == 1 and page.po_list.item(0, 0).text() == w.north["po_number"] and page.po_list.item(0, 5).text() == receiving.FIRST
    assert page.tabs.tabText(0) == "Count a delivery  (1)"
    assert not page.po_list.isHidden() and page.grid.isHidden() and page.save_btn.isHidden()
    page.po_list.cellClicked.emit(0, 0)
    assert page.po_id == w.north["id"] and page.grid.rowCount() == 2
    assert page.po_list.isHidden() and not page.grid.isHidden() and not page.save_btn.isHidden()
    # Clear goes back to the list, with nothing saved.
    page._clear()
    assert not page.po_list.isHidden() and page.grid.isHidden() and not receiving.branch_counts(w.conn, w.r1)
    win.close()


def test_two_people_count_on_screen(app, setup):
    w = setup
    win = MainWindow(w.conn, w.r1)
    page = win.receiving
    page.number.setText(w.north["po_number"])
    page.open_number()
    assert page.grid.rowCount() == 2 and page.save_btn.isEnabled()
    headers = [page.grid.horizontalHeaderItem(c).text() for c in range(page.grid.columnCount())]
    assert headers == ["SKU", "Product", "Size", "Color", "UOM", "Pieces counted", "Damaged", "Remarks", "You counted before"]
    # Nothing counted before, so that column stays out of the way, and Back returns to the list.
    assert page.grid.isColumnHidden(8) and page.back_btn.isVisibleTo(page)
    # Nothing on the screen says how many were ordered, and only the count can be typed in.
    assert not page.grid.item(0, 0).flags() & Qt.ItemFlag.ItemIsEditable and page.grid.item(0, C_PIECES).flags() & Qt.ItemFlag.ItemIsEditable
    assert page.grid.item(0, C_PIECES).text() == ""
    _type(page, ("9", "2"), ("6", ""))
    page.save_count()
    assert page.grid.rowCount() == 0 and "Another receiver" in page.prompt.text() and page.po_list.rowCount() == 0
    # Their own numbers are there for them, and they can redo them. There's no approving.
    page.tabs.setCurrentIndex(1)
    page.counts.selectRow(0)
    assert page.lines.item(0, C_PIECES).text() == "9" and page.lines.item(0, C_DAMAGED).text() == "2" and page.recount_btn.isEnabled()
    assert not hasattr(page, "approve_btn")
    win.close()

    win = MainWindow(w.conn, w.r2)
    page = win.receiving
    assert page.po_list.item(0, 5).text() == receiving.SECOND
    # The second person sees the row and whose it is, and no numbers.
    page.tabs.setCurrentIndex(1)
    page.counts.selectRow(0)
    assert page.lines.rowCount() == 0 and "only Ana" in page.selected_note.text() and not page.recount_btn.isEnabled()
    page.tabs.setCurrentIndex(0)
    page.po_list.cellClicked.emit(0, 0)
    assert page.grid.item(0, C_PIECES).text() == "" and "Second count" in page.heading.text()
    _type(page, ("10", "2"), ("6", ""))
    page.save_count()
    assert "don't match on 1 products" in page.prompt.text() and _po(w)["status"] == orders.ISSUED
    # Only the product that disagreed comes back.
    assert page.po_list.item(0, 5).text() == receiving.AGAIN
    page.po_list.cellClicked.emit(0, 0)
    assert page.grid.rowCount() == 1 and page.grid.item(0, 1).text() == "Socks" and "didn't match" in page.prompt.text()
    _type(page, ("10", "2"))
    page.save_count()
    win.close()

    win = MainWindow(w.conn, w.r1)
    page = win.receiving
    page.po_list.cellClicked.emit(0, 0)
    assert page.grid.rowCount() == 1
    _type(page, ("10", "2"))
    page.save_count()
    assert "Both counts match" in page.prompt.text() and _po(w)["status"] == orders.FULL
    # Recount from the same screen, with no question asked.
    page.tabs.setCurrentIndex(1)
    page.counts.selectRow(0)
    page.recount()
    assert page.views.currentIndex() == 0 and "Recount" in page.heading.text() and page.grid.rowCount() == 2 and page.grid.item(0, C_PIECES).text() == ""
    page._clear()
    assert page.counts.item(0, 9).text() == "Recount" and page.counts.item(3, 9).text() == "Count"
    win.close()

    win = MainWindow(w.conn, w.admin)
    results = win.received
    # Matched in full but with damage, so it stays to check.
    assert results.tabs.tabText(0) == "To check  1" and results.tabs.tabText(1) == "Completed delivery  0"
    assert results.table.rowCount() == 1 and results.table.item(0, 6).text() == "0" and results.table.item(0, 7).text() == "2"
    results.table.selectRow(0)
    assert results.lines.rowCount() == 2 and results.history.rowCount() == 4 and results.detail.isHidden()
    # Any one person's numbers.
    results.history.selectRow(0)
    assert not results.detail.isHidden() and results.detail.item(0, 5).text() == "9"
    results.tabs.setCurrentIndex(1)
    assert results.table.isHidden() and "No delivery" in results.empty.text()
    results.tabs.setCurrentIndex(2)
    assert results.tabs.tabText(2) == "By receiver" and results.views.currentIndex() == 1
    assert results.receivers.rowCount() == 2 and results.receivers.item(0, 0).text() == "Ana" and results.receivers.item(0, 5).text() == "1"
    win.close()


def test_a_matching_delivery_moves_to_its_own_tab_on_screen(app, setup):
    w = setup
    _agree(w)
    win = MainWindow(w.conn, w.buyer)
    results = win.received
    assert results.tabs.tabText(0) == "To check  0" and results.tabs.tabText(1) == "Completed delivery  1" and results.table.isHidden()
    results.tabs.setCurrentIndex(1)
    assert results.table.rowCount() == 1 and results.table.item(0, 0).text() == w.north["po_number"] and results.table.item(0, 10).text() == "Completed delivery"
    win.close()
