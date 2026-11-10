"""Owner decision, Nov 8 (Q33): Orders, Inquiry and Approvals > Rejected show one page at a time, newest first, with Previous and Next
and 50, 100 or 200 a page (50 unless chosen, and remembered). Search and filters still look through every order and go back to page 1;
tab counts are totals. Approvals > Waiting and Print stay complete."""
from __future__ import annotations

import os
import shutil
from datetime import date, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from atlaspo import auth, db, masters, order_forms, orders
from atlaspo.ui import common, of_page, pages

from conftest import NORTH, PASSWORD, SOUTH, World

TOTAL = 450
START = date(2025, 1, 1)


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    # Saved settings, the page size among them, go to a temp file (conftest), so every test starts at 50 a page.
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)


def pump(n=4):
    for _ in range(n):
        QApplication.processEvents()


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """450 approved OFs, one a day from 1 Jan 2025, even ones for North and odd ones for South, and 250 rejected OFs and 3 waiting, all for North in 2024.
    Orders > Open holds all 703. Built once, copied for each test."""
    folder = tmp_path_factory.mktemp("big")
    old = os.environ.get("PO_DATA_DIR")
    os.environ["PO_DATA_DIR"] = str(folder)
    try:
        conn = db.open_database()
        w = World(conn)
        ids = []
        for i in range(TOTAL):
            ids.append(w.approved(items=[w.item(alloc={NORTH if i % 2 == 0 else SOUTH: 1})], of_date=(START + timedelta(days=i)).isoformat()))
        # Rejected: a big total goes to the approver, who sends it back.
        rejected = []
        for i in range(250):
            # Dated in 2024, before every approved one. Rejected OFs go back to the purchaser as drafts, so they sit on Orders > Open too.
            of_id = w.draft(items=[w.item(cost=50_000_000)], of_date=(date(2024, 1, 1) + timedelta(days=i)).isoformat())
            assert order_forms.submit(conn, w.buyer, of_id) == orders.PENDING
            order_forms.reject(conn, w.admin, of_id, f"Too dear {i}")
            rejected.append(of_id)
        # Three still waiting for approval: Waiting always shows every one.
        waiting = []
        for i in range(3):
            of_id = w.draft(items=[w.item(cost=50_000_000)], of_date="2024-12-30")
            order_forms.submit(conn, w.buyer, of_id)
            waiting.append(of_id)
        numbers = {i: conn.execute("SELECT of_number FROM order_forms WHERE id = ?", (i,)).fetchone()[0] for i in ids}
        conn.close()
    finally:
        if old is None:
            os.environ.pop("PO_DATA_DIR", None)
        else:
            os.environ["PO_DATA_DIR"] = old
    return {"file": folder / "po.sqlite", "ids": ids, "numbers": numbers, "rejected": rejected, "waiting": waiting}


@pytest.fixture
def big(built, tmp_path, monkeypatch):
    monkeypatch.setenv("PO_DATA_DIR", str(tmp_path))
    shutil.copy(built["file"], tmp_path / "po.sqlite")
    conn = db.open_database()
    admin = auth.login(conn, "boss", PASSWORD)
    yield dict(built, conn=conn, admin=admin)
    conn.close()


def tops(tree) -> list:
    return [tuple(tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)) for i in range(tree.topLevelItemCount())]


def shown_label(pager) -> str:
    return pager.label.text() if not pager.isHidden() else ""


def page_text(page, pages, first, last, total) -> str:
    """The pager's words: 'Page 2 of 9 · 51–100 of 450'."""
    return f"Page {page:,} of {pages:,} · {first:,}–{last:,} of {total:,}"


def ofs(ids) -> list:
    return [("OF", i) for i in ids]


# ---------------------------------------------------------------- Orders

def test_orders_show_the_newest_page_of_703_and_next(app, big):
    page = of_page.OrdersPage(big["conn"], big["admin"])
    page.resize(1366, 768)
    page.show()
    pump()
    newest = list(reversed(big["ids"]))
    # Page 1: the newest 50, latest date at the top.
    assert tops(page.tree) == ofs(newest[:50])
    assert shown_label(page.pager) == page_text(1, 15, 1, 50, 703)
    # The tab count is the total, not what is on screen.
    assert "703" in page.tabs.tabText(0)
    page.pager.next_button.click()
    pump()
    assert tops(page.tree) == ofs(newest[50:100]) and shown_label(page.pager) == page_text(2, 15, 51, 100, 703)
    page.pager.go(-1)
    assert tops(page.tree) == ofs(newest[:50])
    # The Approved button narrows to the 450 approved, counted against all of them, from page 1.
    page.pager.go(1)
    page.count_buttons[orders.APPROVED].click()
    assert tops(page.tree) == ofs(newest[:50]) and shown_label(page.pager) == page_text(1, 9, 1, 50, 450)
    # The last page: the oldest 50.
    page.pager.go(8)
    pump()
    assert tops(page.tree) == ofs(newest[400:]) and shown_label(page.pager) == page_text(9, 9, 401, 450, 450)
    assert page.pager.prev_button._wanted and not page.pager.next_button._wanted
    # Export still takes every order on the list, not only those shown.
    assert len(page.rows) == 450


def test_orders_search_finds_an_old_of_and_branch_counts_against_all(app, big):
    page = of_page.OrdersPage(big["conn"], big["admin"])
    oldest = big["ids"][0]
    page.pager.go(1)
    page.search.setText(big["numbers"][oldest])
    assert tops(page.tree) == [("OF", oldest)] and page.pager.isHidden()
    page.clear_filters()
    assert len(tops(page.tree)) == 50 and shown_label(page.pager) == page_text(1, 15, 1, 50, 703)
    page.pager.go(1)
    common.select_data(page.branch, SOUTH)
    # 225 South orders among all 450, not among the 50 shown; back to page 1.
    assert len(tops(page.tree)) == 50 and shown_label(page.pager) == page_text(1, 5, 1, 50, 225)
    assert "225" in page.tabs.tabText(0)
    assert all(i % 2 == 1 for i in (big["ids"].index(k[1]) for k in tops(page.tree)))


def test_orders_tab_switch_and_filters_go_back_to_page_1(app, big):
    page = of_page.OrdersPage(big["conn"], big["admin"])
    page.pager.go(1)
    assert shown_label(page.pager) == page_text(2, 15, 51, 100, 703)
    page.tabs.setCurrentIndex(2)  # Cancelled
    page.tabs.setCurrentIndex(0)
    assert shown_label(page.pager) == page_text(1, 15, 1, 50, 703)
    page.pager.go(1)
    common.select_data(page.year, "2025")
    # 365 orders in 2025, back to page 1.
    assert shown_label(page.pager) == page_text(1, 8, 1, 50, 365)
    # A refresh from elsewhere (after a save) stays on the page that was open.
    page.pager.go(1)
    page.pager.go(1)
    page.refresh()
    assert len(tops(page.tree)) == 50 and shown_label(page.pager) == page_text(3, 8, 101, 150, 365)


def test_a_new_page_starts_at_the_top_and_a_refresh_keeps_the_selected_row(app, big):
    page = of_page.OrdersPage(big["conn"], big["admin"])
    page.resize(1366, 768)
    page.show()
    pump()
    page.tree.setCurrentItem(page.tree.topLevelItem(49))
    page.tree.scrollToBottom()
    pump()
    assert page.tree.verticalScrollBar().value() > 0
    # Next: a new page, at its top, nothing selected.
    page.pager.go(1)
    pump()
    assert page.tree.currentItem() is None and page.tree.verticalScrollBar().value() == 0
    # A plain refresh keeps the selected row.
    item = page.tree.topLevelItem(30)
    page.tree.setCurrentItem(item)
    key = tuple(item.data(0, Qt.ItemDataRole.UserRole))
    page.refresh()
    pump()
    assert tuple(page.tree.currentItem().data(0, Qt.ItemDataRole.UserRole)) == key


def test_newest_first_merges_older_pos_by_date():
    ofs = [{"id": 1, "of_date": "2026-01-05", "of_number": "OF-1"}, {"id": 2, "of_date": "2026-01-01", "of_number": "OF-2"}]
    legacy = [{"id": 9, "po_date": "2026-01-03", "po_number": "P-9"}, {"id": 8, "po_date": "2026-01-05", "po_number": "P-8"}]
    got = [(k, r["id"]) for k, r in of_page.newest_first(ofs, legacy)]
    # Same day: the higher number first.
    assert got == [("PO", 8), ("OF", 1), ("PO", 9), ("OF", 2)]
    # A draft has no number yet: it is the newest of its day.
    got = [(k, r["id"]) for k, r in of_page.newest_first(ofs + [{"id": 3, "of_date": "2026-01-05", "of_number": None}], legacy)]
    assert got[0] == ("OF", 3)


# ---------------------------------------------------------------- Inquiry

def test_inquiry_all_ofs_pages_and_searches_everything(app, big):
    page = of_page.InquiryPage(big["conn"], big["admin"])
    page.show_completed()  # All OFs
    ids = big["ids"]
    newest = list(reversed(ids))
    assert tops(page.tree) == ofs(newest[:50])
    assert shown_label(page.pager) == page_text(1, 9, 1, 50, 450) and page.count.text() == "450 found"
    page.pager.go(1)
    assert tops(page.tree) == ofs(newest[50:100]) and shown_label(page.pager) == page_text(2, 9, 51, 100, 450)
    # Any filter goes back to page 1, and looks through all 450.
    common.select_data(page.branch, NORTH)
    assert len(tops(page.tree)) == 50 and shown_label(page.pager) == page_text(1, 5, 1, 50, 225)
    page.pager.go(1)
    page.clear_filters()
    assert tops(page.tree) == ofs(newest[:50])
    # Search finds an old one, far beyond page 1.
    page.pager.go(1)
    page.search.setText(big["numbers"][ids[1]])
    assert tops(page.tree) == [("OF", ids[1])] and page.pager.isHidden()


def test_inquiry_every_tab_and_stage_pages_the_same(app, big):
    page = of_page.InquiryPage(big["conn"], big["admin"])
    # By date, a window holding 300 of them.
    page.tabs.setCurrentIndex(2)
    common.set_date(page.date_from, (START + timedelta(days=150)).isoformat())
    assert shown_label(page.pager) == page_text(1, 6, 1, 50, 300)
    page.pager.go(5)
    assert len(tops(page.tree)) == 50 and shown_label(page.pager) == page_text(6, 6, 251, 300, 300) and not page.pager.next_button._wanted
    # Changing tab goes back to page 1.
    page.tabs.setCurrentIndex(1)
    assert len(tops(page.tree)) == 50 and shown_label(page.pager) == page_text(1, 9, 1, 50, 450)
    # The Status (stage) filter runs on every order before the page is taken.
    page.pager.go(1)
    page.status.setCurrentIndex(page.status.findData(page.TO_PRINT))
    assert shown_label(page.pager) == page_text(1, 9, 1, 50, 450)
    assert all(page.tree.topLevelItem(i).text(page.C_STATUS) == "To print" for i in range(page.tree.topLevelItemCount()))
    page.status.setCurrentIndex(page.status.findData(orders.CANCELLED))
    assert tops(page.tree) == [] and page.pager.isHidden()
    # Find an OF by product: every order holds "Item dept 1".
    page.tabs.setCurrentIndex(0)
    page.product.setText("Item dept 1")
    assert shown_label(page.pager) == page_text(1, 9, 1, 50, 450)


# ---------------------------------------------------------------- Approvals and Print

def test_approvals_rejected_pages_and_waiting_shows_all(app, big):
    page = pages.ApprovalsPage(big["conn"], big["admin"])
    page.resize(1366, 768)
    page.show()
    pump()
    # Waiting: every OF waiting, never paged.
    assert page.ofs.topLevelItemCount() == 3 and not page.waiting.isHidden() and not page.rejected_pager.isVisibleTo(page) and page.rejected_box.isHidden()
    page.tabs.setCurrentIndex(1)
    pump()
    assert page.rejected.rowCount() == 50 and shown_label(page.rejected_pager) == page_text(1, 5, 1, 50, 250)
    assert "250" in page.tabs.tabText(1)
    reasons = lambda: [page.rejected.item(i, 7).text() for i in range(page.rejected.rowCount())]
    # Newest rejection first.
    assert reasons() == [f"Too dear {i}" for i in range(249, 199, -1)]
    page.rejected_pager.go(1)
    assert reasons() == [f"Too dear {i}" for i in range(199, 149, -1)] and shown_label(page.rejected_pager) == page_text(2, 5, 51, 100, 250)
    # The last page ends with the oldest.
    page.rejected_pager.go(3)
    pump()
    assert page.rejected.rowCount() == 50 and shown_label(page.rejected_pager) == page_text(5, 5, 201, 250, 250)
    assert page.rejected.item(49, 7).text() == "Too dear 0"
    # Back and forth goes back to page 1.
    page.tabs.setCurrentIndex(0)
    page.tabs.setCurrentIndex(1)
    assert page.rejected.rowCount() == 50 and page.rejected.item(0, 7).text() == "Too dear 249"
    # 200 a page, remembered for the next time the page opens.
    page.rejected_pager.size_box.setCurrentIndex(page.rejected_pager.size_box.findData(200))
    assert page.rejected.rowCount() == 200 and shown_label(page.rejected_pager) == page_text(1, 2, 1, 200, 250)
    again = pages.ApprovalsPage(big["conn"], big["admin"])
    again.tabs.setCurrentIndex(1)
    assert again.rejected_pager.page_size == 200 and again.rejected.rowCount() == 200
    again.close()


def test_print_page_is_not_paged(app, big):
    # Not in the owner's list: To print stays complete, every approved OF on one list.
    page = pages.PrintPage(big["conn"], big["admin"])
    assert page.table.rowCount() == TOTAL
    assert not any(isinstance(w, common.Pager) for w in page.findChildren(common.Pager))
