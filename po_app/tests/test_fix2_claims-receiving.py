"""Second fix round: claims and receiving (AUD-041, AUD-130 A7, AUD-122, AUD-024 claim stub wording)."""
from __future__ import annotations

import inspect
import os
from io import BytesIO

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from pypdf import PdfReader

from atlaspo import auth, claim_stubs as cs, order_forms, orders, receiving
from atlaspo.db import POError, tx
from atlaspo.ui import claim_stub_page

from conftest import NORTH, SOUTH
from test_payments import Order


def _receiver(world, branch=NORTH):
    return auth.get_user(world.conn, world.conn.execute("SELECT id FROM users WHERE username = ?", (f"r{branch}x1",)).fetchone()[0])


def _pass_prints(world, cs_id):
    return world.conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action = 'CS_PASS_PRINT' AND entity_id = ?", (cs_id,)).fetchone()[0]


def _text(data: bytes) -> str:
    return " ".join(p.extract_text() for p in PdfReader(BytesIO(data)).pages)


# ------------------------------------------------------------------ AUD-041: a damaged goods pass is recorded only once its file opened

def test_a_damaged_goods_pass_whose_file_did_not_open_is_not_recorded_and_the_next_is_the_original(world):
    o = Order(world, damaged="2")
    stub = o.stub()
    who = _receiver(world)

    def fail(data):
        raise OSError("No application is associated with the specified file")
    with pytest.raises(OSError):
        cs.print_pass(world.conn, who, stub["id"], deliver=fail)
    assert _pass_prints(world, stub["id"]) == 0
    assert not cs.passes_for_branch(world.conn, who)[0]["printed_at"]
    opened = []
    data = cs.print_pass(world.conn, who, stub["id"], deliver=opened.append)
    assert opened == [data] and "COPY" not in _text(data).upper()  # still the original
    assert _pass_prints(world, stub["id"]) == 1
    # A second print is still a copy, and what is opened is that copy.
    again = cs.print_pass(world.conn, who, stub["id"], deliver=opened.append)
    assert opened[-1] == again and "COPY" in _text(again).upper() and _pass_prints(world, stub["id"]) == 2


# ------------------------------------------------------------------ AUD-130 A7: lines go on a damage report only as it is sent

def _report(world, o, branch=NORTH):
    item = world.conn.execute("SELECT id FROM purchase_order_items WHERE po_id = ?", (o.pos[branch]["id"],)).fetchone()[0]
    made = cs.send_report(world.conn, _receiver(world, branch), o.pos[branch]["po_number"], {item: "1"}, "Torn seams")
    return made[0], item


def test_no_lines_can_be_added_to_a_report_once_it_is_decided_or_sent(world):
    o = Order(world)
    request_id, item = _report(world, o)
    assert world.conn.execute("SELECT COUNT(*) FROM claim_request_items WHERE request_id = ?", (request_id,)).fetchone()[0] == 1
    # Still pending, but already sent with its lines: no more lines.
    with pytest.raises(POError, match="already sent"):
        with tx(world.conn):
            cs._add_report_lines(world.conn, request_id, [(item, 1000)])
    cs.reject_report(world.conn, world.admin, request_id, "Not damaged")
    with pytest.raises(POError, match="already decided"):
        with tx(world.conn):
            cs._add_report_lines(world.conn, request_id, [(item, 1000)])
    # Approved too.
    second, item = _report(world, o, SOUTH)
    cs.approve_report(world.conn, world.admin, second)
    with pytest.raises(POError, match="already decided"):
        with tx(world.conn):
            cs._add_report_lines(world.conn, second, [(item, 1000)])
    assert world.conn.execute("SELECT COUNT(*) FROM claim_request_items").fetchone()[0] == 2


# ------------------------------------------------------------------ AUD-122: extra lines compare codes in any language's case

def _po_with(world, code):
    of_id = world.approved(items=[world.item(cost=10000, alloc={NORTH: 10}, description="Rhea plush", item_code=code)])
    po = order_forms.generate_pos(world.conn, world.buyer, of_id)[0]
    orders.issue(world.conn, world.buyer, po["id"])
    return po["id"]


def test_an_extra_line_matching_a_po_product_in_another_case_is_refused(world):
    po_id = _po_with(world, "ñandu-1")
    with pytest.raises(POError, match="already on this PO"):
        with tx(world.conn):
            receiving._add_extra(world.conn, world.buyer, po_id, 1, {"item_code": "ÑANDU-1", "description": "Rhea plush"})
    assert not world.conn.execute("SELECT 1 FROM branch_extra_items").fetchone()


def test_the_same_extra_twice_in_another_case_is_refused(world):
    po_id = _po_with(world, "SOCK-9")
    with tx(world.conn):
        receiving._add_extra(world.conn, world.buyer, po_id, 1, {"item_code": "ñandu-2", "description": "Gorra niño", "color": "Añil"})
    with pytest.raises(POError, match="already listed"):
        with tx(world.conn):
            receiving._add_extra(world.conn, world.buyer, po_id, 1, {"item_code": "ÑANDU-2", "description": "GORRA NIÑO", "color": "AÑIL"})
    # A different product is still taken.
    with tx(world.conn):
        receiving._add_extra(world.conn, world.buyer, po_id, 1, {"item_code": "ñandu-3", "description": "Gorra niño"})
    assert world.conn.execute("SELECT COUNT(*) FROM branch_extra_items").fetchone()[0] == 2


# ------------------------------------------------------------------ AUD-024: claim stubs say "collect", as the README and the Collected button do

def test_claim_stubs_say_collect_not_pick_up(world):
    o = Order(world, damaged="2")
    stub = o.stub()
    cs.arrived(world.conn, world.warehouse, stub["id"], cs.as_on_paper(world.conn, stub["id"]))
    with pytest.raises(POError) as refused:
        cs.collected(world.conn, world.buyer, stub["id"], "Juan")
    assert "collects these" in str(refused.value) and "pick" not in str(refused.value).lower()
    assert cs.status_labels(world.conn)[cs.COLLECTED] == "Collected by supplier"
    page_text = inspect.getsource(claim_stub_page).lower()
    assert "picks these up" not in page_text and "picked up" not in page_text and "pick up" not in page_text
