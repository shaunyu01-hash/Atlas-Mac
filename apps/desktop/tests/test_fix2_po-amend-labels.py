"""Second fix round: a PO amendment with no change is refused (AUD-086), and the PO screen uses the same words as the OF screen (AUD-024)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QPushButton

from atlaspo import order_forms, orders
from atlaspo.db import POError
from atlaspo.ui import common
from atlaspo.ui.main_window import MainWindow

from conftest import NORTH, SOUTH


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def quiet(monkeypatch):
    said = []
    for name in ("error", "info"):
        monkeypatch.setattr(common, name, lambda *a, _n=name: said.append((_n, a[-1])))
    return said


def _po(world, issue=True, uom=None):
    extra = {"uom": uom} if uom else {}
    of_id = world.approved(items=[world.item(cost=10000, alloc={NORTH: 6, SOUTH: 6}, description="Socks", **extra),
                                  world.item(cost=5000, alloc={NORTH: 3}, description="Caps")])
    po_id = {p["branch_code"]: p for p in order_forms.generate_pos(world.conn, world.buyer, of_id)}[NORTH]["id"]
    if issue:
        orders.issue(world.conn, world.buyer, po_id)
    return po_id


def _rows(world, po_id):
    return [{k: i[k] for k in orders.LINE_FIELDS} | {"id": i["id"], "qty_m": i["qty_m"]} for i in orders.get_po(world.conn, po_id)["items"]]


# ------------------------------------------------------------------ AUD-086: PO amendment with no change

@pytest.mark.parametrize("header", [{}, None, "same terms", "same terms typed another way"])
def test_po_amendment_with_nothing_changed_is_refused_and_the_issued_po_stays_issued(world, header):
    po_id = _po(world)
    before = orders.get_po(world.conn, po_id)
    if header == "same terms":
        header = {"payment_terms": before["payment_terms"]}
    elif header == "same terms typed another way":
        header = {"payment_terms": "30 days"}  # stored as 30D, which is what the PO already has
    with pytest.raises(POError, match="Nothing changed"):
        orders.amend(world.conn, world.buyer, po_id, header, _rows(world, po_id), "No change")
    after = orders.get_po(world.conn, po_id)
    assert (after["status"], after["revision"]) == (orders.ISSUED, before["revision"])
    assert not orders.amendments(world.conn, po_id)


def test_a_real_change_or_a_notes_only_change_still_goes_through(world):
    po_id = _po(world)
    res = orders.amend(world.conn, world.buyer, po_id, {"internal_notes": "Call before delivery"}, _rows(world, po_id), "Note")
    assert res["status"] == "APPLIED" and res["revision"] == 0 and orders.get_po(world.conn, po_id)["status"] == orders.ISSUED
    rows = _rows(world, po_id)
    rows[0]["qty_m"] -= 1000
    res = orders.amend(world.conn, world.buyer, po_id, {}, rows, "One less")
    assert res["status"] == "APPLIED" and res["revision"] == 1


def test_amend_on_the_po_screen_with_nothing_changed_says_so(app, world, quiet, monkeypatch):
    import atlaspo.ui.po_page as po_module
    po_id = _po(world)
    w = MainWindow(world.conn, world.buyer)
    w.open_po(po_id)
    page = w.po_page
    page.start_amendment()
    monkeypatch.setattr(po_module, "ask_text", lambda *a, **k: "No change")
    monkeypatch.setattr(po_module, "info", lambda *a: quiet.append(("info", a[-1])))
    common.guarded(page, page.save_amendment)
    assert ("error", "Nothing changed.") in quiet and not any(kind == "info" for kind, _ in quiet)
    po = orders.get_po(world.conn, po_id)
    assert po["revision"] == 0 and po["status"] == orders.ISSUED and not orders.amendments(world.conn, po_id)
    w.close()


# ------------------------------------------------------------------ AUD-024: PO screen words match the OF screen

def test_po_screen_uses_pieces_po_total_and_print_again(app, world, quiet):
    po_id = _po(world, uom="3 IN 1")
    w = MainWindow(world.conn, world.buyer)
    w.open_po(po_id)
    page = w.po_page
    headers = [page.dept_table.horizontalHeaderItem(c).text() for c in range(page.dept_table.columnCount())]
    assert headers == ["Department", "Pieces", "Amount"]
    from PySide6.QtWidgets import QLabel
    texts = {l.text() for l in page.findChildren(QLabel)}
    assert "PO total" in texts and "Grand total" not in texts
    po = orders.get_po(world.conn, po_id)
    pieces = sum(i["qty_m"] * (i["pieces_per_unit"] or 1) for i in po["items"]) // 1000
    packs = sum(i["qty_m"] for i in po["items"]) // 1000
    assert pieces != packs  # the socks come in packs of 3
    qty_text = page.rows["qty"][1].text()
    assert qty_text.startswith(f"{pieces:,} pcs") and f"{packs:,} packs" in qty_text
    assert sum(int(page.dept_table.item(r, 1).text().replace(",", "")) for r in range(page.dept_table.rowCount())) == pieces
    buttons = [b.text() for b in page.findChildren(QPushButton)]
    assert "Print again" in buttons and "Reprint" not in buttons
    w.close()
