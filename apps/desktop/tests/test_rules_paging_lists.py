"""Long lists show one page at a time, newest first, with Previous and Next and 50, 100 or 200 a page (50 unless chosen),
so they stay fast. Search and dates still look through everything and go back to page 1.

Paged: Received (Completed delivery, By receiver), Claim stubs (Collected, Forfeited, Cancelled, All, and the decided Branch reports), Debit memos (every tab),
Item passes (Collected, Forfeited, Cancelled, All), Central Warehouse (Shipped). Work still to do always shows in full: Received To check, claim stubs and item
passes coming back or at the warehouse, Branch reports waiting for approval, and the warehouse's To receive, To ship and Coming back.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QApplication

from atlaspo import auth, claim_stubs, db, debit_memos, item_passes, order_forms, orders, receiving, sample_data, warehouse
from atlaspo.ui.claim_stub_page import ALL, REPORTS, ClaimStubsPage
from atlaspo.ui.debit_memo_page import DebitMemosPage
from atlaspo.ui.item_pass_page import ItemPassesPage
from atlaspo.ui.receiving_page import ReceivedPage
from atlaspo.ui.warehouse_page import CentralWarehousePage

from conftest import NORTH, PASSWORD, SOUTH, World

OLD_CAP = 2000


@pytest.fixture
def app():
    # The saved page size goes to a temp settings file (conftest), so every test starts at 50 a page.
    return QApplication.instance() or QApplication([])


def _open(folder, sample=False):
    mp = pytest.MonkeyPatch()
    mp.setenv("PO_DATA_DIR", str(folder))
    if sample:
        sample_data.ensure(folder)
    return mp, db.open_database()


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    mp, conn = _open(tmp_path_factory.mktemp("sample"), sample=True)
    yield conn
    conn.close()
    mp.undo()


# ------------------------------------------------------------------ a business with more than 200 of everything, made once for the module

class _Copier:
    """Puts copies of rows straight into the file, lines first, so the checks that keep a finished paper unchanged don't see a parent yet.
    Foreign keys are checked when the transaction ends. Used as `with _Copier(conn) as copy:`, one transaction."""

    def __init__(self, conn):
        self.conn = conn
        self.next, self.columns = {}, {}

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute("PRAGMA defer_foreign_keys = ON")
        return self

    def __exit__(self, kind, *_):
        self.conn.execute("ROLLBACK" if kind else "COMMIT")

    def nid(self, table):
        if table not in self.next:
            self.next[table] = self.conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0] + 1
        self.next[table] += 1
        return self.next[table] - 1

    def rows(self, sql, args=()):
        return [dict(r) for r in self.conn.execute(sql, args)]

    def put(self, table, row):
        if table not in self.columns:  # generated columns can't be written
            self.columns[table] = {c[1] for c in self.conn.execute(f"PRAGMA table_xinfo({table})") if c[6] == 0}
        row = {k: v for k, v in row.items() if k in self.columns[table]}
        self.conn.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))

    def po(self, po_id, number, **over):
        """A copy of a PO with its lines, counts, what was agreed and what the warehouse recorded. Returns the new PO id."""
        new = self.nid("purchase_orders")
        items = self.rows("SELECT * FROM purchase_order_items WHERE po_id = ?", (po_id,))
        imap = {i["id"]: self.nid("purchase_order_items") for i in items}
        counts = self.rows("SELECT * FROM branch_counts WHERE po_id = ? ORDER BY id", (po_id,))
        cmap = {c["id"]: self.nid("branch_counts") for c in counts}
        deliveries = self.rows("SELECT * FROM warehouse_deliveries WHERE po_id = ?", (po_id,))
        dmap = {d["id"]: self.nid("warehouse_deliveries") for d in deliveries}
        for c in counts:
            for line in self.rows("SELECT * FROM branch_count_items WHERE count_id = ?", (c["id"],)):
                line.pop("id")
                self.put("branch_count_items", {**line, "count_id": cmap[c["id"]], "po_item_id": imap.get(line["po_item_id"], line["po_item_id"])})
        for c in counts:
            self.put("branch_counts", {**c, "id": cmap[c["id"]], "po_id": new, "recount_of": cmap.get(c["recount_of"])})
        for d in deliveries:
            for line in self.rows("SELECT * FROM warehouse_delivery_items WHERE delivery_id = ?", (d["id"],)):
                line.pop("id", None)
                self.put("warehouse_delivery_items", {**line, "delivery_id": dmap[d["id"]], "po_item_id": imap.get(line["po_item_id"], line["po_item_id"])})
            self.put("warehouse_deliveries", {**d, "id": dmap[d["id"]], "po_id": new})
        for i in items:
            self.put("purchase_order_items", {**i, "id": imap[i["id"]], "po_id": new})
        for r in self.rows("SELECT * FROM branch_received WHERE po_id = ?", (po_id,)):
            self.put("branch_received", {**r, "po_id": new, "po_item_id": imap.get(r["po_item_id"], r["po_item_id"])})
        self.put("purchase_orders", {**self.rows("SELECT * FROM purchase_orders WHERE id = ?", (po_id,))[0], "id": new, "po_number": number, **over})
        return new

    def of(self, of_id, number, k):
        """A copy of an order form's header and its branch POs. Returns the new OF id."""
        new = self.nid("order_forms")
        self.put("order_forms", {**self.rows("SELECT * FROM order_forms WHERE id = ?", (of_id,))[0], "id": new, "of_number": number, "of_seq": 900_000 + k})
        for p in self.rows("SELECT id, po_number FROM purchase_orders WHERE of_id = ?", (of_id,)):
            self.po(p["id"], f"{p['po_number']}-C{k:04d}", of_id=new, po_seq=900_000 + k)
        return new


def _issued_of(w, alloc):
    of_id = w.approved(items=[w.item(cost=10000, alloc=alloc, description="Socks", item_code="SOCK-1")])
    pos = order_forms.generate_pos(w.conn, w.buyer, of_id)
    for p in pos:
        orders.issue(w.conn, w.buyer, p["id"])
    return of_id, {p["branch_code"]: p for p in pos}


@pytest.fixture(scope="module")
def big(tmp_path_factory):
    """208 POs whose delivery is complete and 208 still being counted, and 205 orders each with one branch shipped from the warehouse and one still to ship.
    One of each is made the normal way; the rest are copies put straight into the file, which is far quicker."""
    mp, conn = _open(tmp_path_factory.mktemp("big"))
    w = World(conn)
    people = {b: [auth.create_user(conn, w.admin, f"r{b}x{n}", f"Receiver {b} {n}", PASSWORD, ["RECEIVER"], must_change=False, branch_code=b) for n in (1, 2)]
              for b in (NORTH, SOUTH)}
    _, pos = _issued_of(w, {NORTH: 2, SOUTH: 2})
    done, counting = pos[NORTH], pos[SOUTH]
    for p, who in ((done, people[NORTH]), (counting, people[SOUTH][:1])):
        item = orders.get_po(conn, p["id"])["items"][0]["id"]
        for person in who:
            receiving.save_count(conn, person, p["id"], {item: "2"})
    of_id, pos = _issued_of(w, {NORTH: 1, SOUTH: 1})
    warehouse.receive(conn, w.warehouse, of_id, {pos[NORTH]["id"]: "1", pos[SOUTH]["id"]: "1"})
    warehouse.ship(conn, w.warehouse, of_id, [pos[NORTH]["id"]])
    with _Copier(conn) as copy:
        # Copies of a counted PO belong to no order form, so they don't clash with the original's branch.
        w.completed = [done["id"]] + [copy.po(done["id"], f"DONE-{k:04d}", of_id=None) for k in range(207)]
        w.counting = [counting["id"]] + [copy.po(counting["id"], f"OPEN-{k:04d}", of_id=None) for k in range(207)]
        w.shipped = [of_id] + [copy.of(of_id, f"OF-C{k:04d}", k) for k in range(204)]
    w.numbers = dict(conn.execute("SELECT id, po_number FROM purchase_orders").fetchall())
    yield w
    conn.close()
    mp.undo()


@pytest.fixture(scope="module")
def papers(tmp_path_factory):
    """The sample business with 2,100 more debit memos, claim stubs and item passes put straight into the file, the first of each dated 2020-01-15
    with a number of its own, so it's the oldest and far past the old 2,000 cap. Plus 250 claim stubs and item passes still coming back, older than the
    2,099 newest, so the old cap hid them too."""
    mp, conn = _open(tmp_path_factory.mktemp("papers"), sample=True)
    out = {}
    with _Copier(conn) as copy:
        for table, kind, number, date_col, (ctable, ccol), history, waiting in (
                ("debit_memos", "dm", "dm_number", "dm_date", ("debit_memo_items", "dm_id"), debit_memos.ISSUED, None),
                ("claim_stubs", "cs", "cs_number", "cs_date", ("claim_stub_items", "cs_id"), claim_stubs.COLLECTED, claim_stubs.ISSUED),
                ("item_passes", "ip", "ip_number", "ip_date", ("item_pass_items", "ip_id"), item_passes.COLLECTED, item_passes.PRINTED)):
            src = copy.rows(f"SELECT * FROM {table} ORDER BY id LIMIT 1")[0]
            lines = copy.rows(f"SELECT * FROM {ctable} WHERE {ccol} = ?", (src["id"],))
            # The oldest first, then the ones still coming back, then 2,099 newer ones that pushed both past the old cap.
            changes = [{number: f"OLDEST-{kind.upper()}", date_col: "2020-01-15", "status": history}]
            if waiting:
                changes += [{number: f"WAIT-{kind.upper()}-{i:05d}", date_col: "2026-10-01", "status": waiting} for i in range(250)]
            changes += [{number: f"BULK-{kind.upper()}-{i:05d}", date_col: "2026-10-02", "status": history} for i in range(2099)]
            ids = []
            for k, change in enumerate(changes):
                if f"{kind}_seq" in src:  # each paper's number in its year is its own
                    change[f"{kind}_seq"] = 900_000 + k
                new = copy.nid(table)
                for line in lines:
                    copy.put(ctable, {**{k: v for k, v in line.items() if k != "id"}, ccol: new})
                copy.put(table, {**src, **change, "id": new})
                ids.append(new)
            out[kind] = ids[0]
    yield SimpleNamespace(conn=conn, oldest=out, owner_user=auth.login(conn, "owner", "123"))
    conn.close()
    mp.undo()


def _tab_text(page, key):
    return next(page.tabs.tabText(i) for i in range(page.tabs.count()) if page.tabs.tabData(i) == key)


def _pager_text(page_pager):
    return page_pager.label.text() if not page_pager.isHidden() else ""


def _page_text(page, pages, first, last, total):
    """The pager's words: 'Page 2 of 9 · 51–100 of 450'."""
    return f"Page {page:,} of {pages:,} · {first:,}–{last:,} of {total:,}"


def _pages(total, size=50):
    return -(-total // size)


def _ids(rows):
    return [r["id"] for r in rows]


# ------------------------------------------------------------------ Received: the rows shown are the same as before

def _old_po_results(conn, text=""):
    """po_results exactly as it was before Nov 8, kept here to compare with."""
    where, args = ["EXISTS (SELECT 1 FROM branch_counts b WHERE b.po_id = p.id)"], []
    for word in text.split():
        where.append("(p.po_number LIKE ? OR f.of_number LIKE ? OR s.name LIKE ? OR s.code LIKE ? OR p.branch_name LIKE ?)")
        args += [f"%{word}%"] * 5
    _IN_FORCE = receiving._IN_FORCE
    rows = conn.execute(
        "SELECT p.id, p.po_number, p.status, p.branch_name, p.branch_code, f.of_number, s.name AS supplier,"
        " (SELECT SUM(i.qty_m * i.pieces_per_unit) FROM purchase_order_items i WHERE i.po_id = p.id) AS ordered_m,"
        f" (SELECT COALESCE(SUM(r.pieces_m), 0) FROM branch_received r WHERE r.po_id = p.id AND {_IN_FORCE}) AS received_m,"
        f" (SELECT COALESCE(SUM(r.damaged_m), 0) FROM branch_received r WHERE r.po_id = p.id AND {_IN_FORCE}) AS damaged_m,"
        " (SELECT COUNT(*) FROM branch_counts b WHERE b.po_id = p.id AND b.status = 'WAITING') AS waiting,"
        " (SELECT COUNT(*) FROM purchase_order_items i WHERE i.po_id = p.id AND i.qty_m * i.pieces_per_unit <> (SELECT COALESCE(SUM(r.pieces_m), 0)"
        f"    FROM branch_received r WHERE r.po_item_id = i.id AND {_IN_FORCE})) AS lines_off"
        " FROM purchase_orders p JOIN suppliers s ON s.id = p.supplier_id LEFT JOIN order_forms f ON f.id = p.of_id WHERE " + " AND ".join(where) + " ORDER BY p.id DESC LIMIT 2000", args).fetchall()
    out = [dict(r) for r in rows]
    numbers = {kind: {} for kind in ("debit_memos", "claim_stubs", "item_passes")}
    if out:
        for kind, sql in (("debit_memos", "SELECT po_id, dm_number AS n FROM debit_memos WHERE status = 'ISSUED' ORDER BY id"),
                          ("claim_stubs", "SELECT po_id, cs_number AS n FROM claim_stubs WHERE status <> 'CANCELLED' ORDER BY id"),
                          ("item_passes", "SELECT po_id, ip_number AS n FROM item_passes WHERE status <> 'CANCELLED' ORDER BY id")):
            for row in conn.execute(sql):
                numbers[kind].setdefault(row["po_id"], []).append(row["n"])
    ids = [r["id"] for r in out]
    words = receiving._progress_many(conn, out)
    lines_by, extra_by = receiving._lines_many(conn, ids), receiving._extra_lines_many(conn, ids)
    returned_by, charged_by = receiving._returned_many(conn, ids), receiving._charged_many(conn, ids)
    for r in out:
        r["difference_m"] = r["received_m"] - r["ordered_m"]
        r["progress"] = words[r["id"]]
        for kind, by_po in numbers.items():
            r[kind] = ", ".join(by_po.get(r["id"], []))
        lines = lines_by.get(r["id"], [])
        excess = receiving._excess_from(lines, extra_by.get(r["id"], []), returned_by.get(r["id"], {}))
        r["excess_m"], r["excess_left_m"] = sum(x["excess_m"] for x in excess), sum(x["left_m"] for x in excess)
        r["tab"] = receiving.COMPLETED if not r["progress"] and receiving._settled_from(charged_by.get(r["id"], {}), lines, excess) else receiving.TO_CHECK
    return out


def test_received_rows_shown_are_the_same_as_the_old_output(sample):
    admin = auth.login(sample, "owner", "123")
    old = _old_po_results(sample)
    assert old and {r["tab"] for r in old} == {receiving.TO_CHECK, receiving.COMPLETED}
    assert any(r["debit_memos"] for r in old) and any(r["claim_stubs"] for r in old) and any(r["item_passes"] for r in old) and any(r["progress"] for r in old)
    assert receiving.po_results(sample, admin) == old
    branches = sorted({r["branch_code"] for r in old})
    for tab in (receiving.TO_CHECK, receiving.COMPLETED):
        on_tab = [r for r in old if r["tab"] == tab]
        for code in [None, *branches]:
            rows = [r for r in on_tab if code is None or r["branch_code"] == code]
            for limit in (None, 1, 3):
                found = receiving.received_list(sample, admin, "", tab, code, limit)
                assert found["rows"] == (rows if limit is None else rows[:limit])
                assert found["total"] == len(rows)
                chosen = [r for r in old if code is None or r["branch_code"] == code]
                assert found["counts"] == {t: sum(r["tab"] == t for r in chosen) for t in (receiving.TO_CHECK, receiving.COMPLETED)}
                assert found["branches"][None] == len(on_tab) and all(found["branches"].get(b, 0) == sum(r["branch_code"] == b for r in on_tab) for b in branches)
    # A search gives the same rows too.
    word = old[-1]["po_number"]
    assert receiving.received_list(sample, admin, word, old[-1]["tab"])["rows"] == [r for r in _old_po_results(sample, word) if r["tab"] == old[-1]["tab"]]


def test_received_documents_are_read_only_for_the_rows_shown(sample):
    admin = auth.login(sample, "owner", "123")
    statements = []
    sample.set_trace_callback(statements.append)
    try:
        receiving.received_list(sample, admin, "", receiving.COMPLETED, None, 1)
    finally:
        sample.set_trace_callback(None)
    docs = [s for s in statements if "dm_number AS n" in s or "cs_number AS n" in s or "ip_number AS n" in s]
    assert len(docs) == 3 and all("json_each" in s for s in docs)  # before: every memo, stub and pass in the file, whatever was shown


def test_received_completed_pages_and_to_check_shows_everything(app, big):
    w = big
    page = ReceivedPage(w.conn, w.admin)
    # To check is work to do: every PO, no pages.
    assert page.tab == receiving.TO_CHECK and page.table.rowCount() == 208 and page.pager.isHidden()
    assert page.tabs.tabText(0).endswith(" 208") and page.tabs.tabText(1).endswith(" 208")  # tab counts are totals
    page._set_tab(receiving.COMPLETED)
    newest = sorted(w.completed, reverse=True)
    # Page 1: the newest 50.
    assert page.table.rowCount() == 50 and _pager_text(page.pager) == _page_text(1, 5, 1, 50, 208)
    assert _ids(page.rows) == newest[:50]
    assert not page.pager.prev_button._wanted and page.pager.next_button._wanted
    assert page.branch_buttons[None].text().endswith(" 208") and page.branch_buttons[NORTH].text().endswith(" 208") and page.branch_buttons[SOUTH].text().endswith(" 0")
    # Next: the next 50, starting at the top with nothing selected.
    page.table.selectRow(10)
    page.pager.next_button.click()
    assert _ids(page.rows) == newest[50:100] and _pager_text(page.pager) == _page_text(2, 5, 51, 100, 208)
    assert not page.table.selectedItems() and page.pager.prev_button._wanted
    # A plain refresh (after a save) stays on this page and keeps the selected PO.
    page.table.selectRow(5)
    picked = page.rows[5]["id"]
    page.refresh()
    assert _ids(page.rows) == newest[50:100] and page.rows[page.table.currentRow()]["id"] == picked
    # The last page holds what's left.
    for _ in range(3):
        page.pager.go(1)
    assert _ids(page.rows) == newest[200:] and _pager_text(page.pager) == _page_text(5, 5, 201, 208, 208) and not page.pager.next_button._wanted
    page.pager.go(-1)
    assert _ids(page.rows) == newest[150:200]
    # Search looks through every PO, not only this page: the oldest completed one is found.
    oldest = min(w.completed)
    assert oldest not in _ids(receiving.received_list(w.conn, w.admin, "", receiving.COMPLETED, None, page.pager.limit)["rows"])
    page.search.setText(w.numbers[oldest])
    page.refresh()
    assert _ids(page.rows) == [oldest] and page.pager.isHidden()
    # Clearing the search goes back to page 1.
    page.search.setText("")
    page.refresh()
    assert _ids(page.rows) == newest[:50] and _pager_text(page.pager) == _page_text(1, 5, 1, 50, 208)
    # 100 a page.
    page.pager.size_box.setCurrentIndex(page.pager.size_box.findData(100))
    assert _ids(page.rows) == newest[:100] and _pager_text(page.pager) == _page_text(1, 3, 1, 100, 208)
    page.pager.size_box.setCurrentIndex(page.pager.size_box.findData(50))
    # A branch with none completed: nothing to page. Choosing a branch goes back to page 1.
    page.pager.go(1)
    page.branch_buttons[SOUTH].click()
    assert not page.rows and page.pager.isHidden()
    page.branch_buttons[NORTH].click()
    assert _ids(page.rows) == newest[:50] and _pager_text(page.pager) == _page_text(1, 5, 1, 50, 208)
    page.close()


# ------------------------------------------------------------------ Debit memos, claim stubs, item passes: beyond the old 2,000 cap

def test_services_capped_at_2000_which_hid_old_papers(papers):
    # Why the pages ask for every row: the services' default stops at the newest 2,000, so the oldest paper was invisible to a search with no words.
    assert all(r["id"] != papers.oldest["dm"] for r in debit_memos.search(papers.conn, papers.owner_user))
    assert len(debit_memos.search(papers.conn, papers.owner_user)) == OLD_CAP


def test_debit_memos_page_a_page_at_a_time_and_finds_the_oldest(app, papers):
    page = DebitMemosPage(papers.conn, papers.owner_user)
    issued = _ids(debit_memos.search(papers.conn, papers.owner_user, status=debit_memos.ISSUED, limit=10 ** 9))  # newest first
    total = len(issued)
    assert total > OLD_CAP + 100
    assert _ids(page.rows) == issued[:50] and _pager_text(page.pager) == _page_text(1, _pages(total), 1, 50, total)
    assert page.tabs.tabText(0).endswith(f" {total}")  # the count is the total, not what's shown
    page.pager.go(1)
    assert _ids(page.rows) == issued[50:100] and _pager_text(page.pager) == _page_text(2, _pages(total), 51, 100, total)
    # The tab change goes back to page 1; All is paged too.
    page._set_tab("ALL")
    every = papers.conn.execute("SELECT COUNT(*) FROM debit_memos").fetchone()[0]
    assert page.table.rowCount() == 50 and page.tabs.tabText(2).endswith(f" {every}") and _pager_text(page.pager) == _page_text(1, _pages(every), 1, 50, every)
    # Search and dates look through everything, beyond page 1 and the old 2,000.
    page.pager.go(1)
    page.search.setText("OLDEST-DM")
    assert _ids(page.rows) == [papers.oldest["dm"]] and page.pager.isHidden()
    page.clear_filters()
    assert page.table.rowCount() == 50 and _pager_text(page.pager).startswith("Page 1 of ")
    page.pager.go(1)
    page.date_from.setDate(QDate(2020, 1, 1))
    page.date_to.setDate(QDate(2020, 1, 31))
    assert _ids(page.rows) == [papers.oldest["dm"]] and page.pager.isHidden()
    page.close()


def test_claim_stubs_page_pages_history_and_shows_every_stub_still_to_do(app, papers):
    page = ClaimStubsPage(papers.conn, papers.owner_user)
    coming = papers.conn.execute("SELECT COUNT(*) FROM claim_stubs WHERE status = 'ISSUED'").fetchone()[0]
    assert coming >= 250
    # Coming back to the warehouse is work to do: every stub, no pages.
    assert page.tab == claim_stubs.ISSUED and page.table.rowCount() == coming and page.pager.isHidden()
    page._set_tab(claim_stubs.AT_WAREHOUSE)
    assert page.pager.isHidden() and page.table.rowCount() == papers.conn.execute("SELECT COUNT(*) FROM claim_stubs WHERE status = 'AT_WAREHOUSE'").fetchone()[0]
    page._set_tab(claim_stubs.COLLECTED)
    collected = _ids(claim_stubs.search(papers.conn, papers.owner_user, status=claim_stubs.COLLECTED, limit=10 ** 9))  # newest first
    n = len(collected)
    assert n > OLD_CAP and _ids(page.rows) == collected[:50] and _pager_text(page.pager) == _page_text(1, _pages(n), 1, 50, n)
    page.pager.go(1)
    assert _ids(page.rows) == collected[50:100] and _pager_text(page.pager) == _page_text(2, _pages(n), 51, 100, n)
    # A tab change goes back to page 1.
    page._set_tab(ALL)
    assert page.table.rowCount() == 50 and _pager_text(page.pager).startswith("Page 1 of ")
    page.pager.go(1)
    page.search.setText("OLDEST-CS")
    assert _ids(page.rows) == [papers.oldest["cs"]] and page.pager.isHidden()
    page.clear_filters()
    assert _pager_text(page.pager).startswith("Page 1 of ")
    page.date_from.setDate(QDate(2020, 1, 1))
    page.date_to.setDate(QDate(2020, 1, 31))
    assert _ids(page.rows) == [papers.oldest["cs"]]
    page.close()


def test_branch_reports_waiting_always_show_and_decided_ones_page(app, papers, monkeypatch):
    page = ClaimStubsPage(papers.conn, papers.owner_user)
    reports = claim_stubs.reports(papers.conn, papers.owner_user)
    # 300 reports: 210 decided and 90 waiting, the waiting ones the oldest. With pages of 50, every waiting one still shows on every page.
    fake = [{**reports[0], "id": 10_000 - i, "status": claim_stubs.REJECTED if i < 210 else claim_stubs.PENDING} for i in range(300)]
    monkeypatch.setattr(claim_stubs, "reports", lambda conn, user, status=None: [dict(r) for r in fake])
    waiting = {r["id"] for r in fake if r["status"] == claim_stubs.PENDING}
    decided = [r["id"] for r in fake if r["status"] != claim_stubs.PENDING]  # newest first
    page._set_tab(REPORTS)

    def shown():
        keys = [page.reports.item(i, 0).data(Qt.ItemDataRole.UserRole) for i in range(page.reports.rowCount())]
        return {k for k in keys if k in waiting}, {k for k in keys if k not in waiting}

    pending_label = claim_stubs.REQUEST_LABELS[claim_stubs.PENDING]
    assert page.reports.rowCount() == 140 and [page.reports.item(i, 9).text() for i in range(140)].count(pending_label) == 90
    assert shown() == (waiting, set(decided[:50]))
    # The pager counts only the decided ones, the ones it pages.
    assert _pager_text(page.reports_pager) == _page_text(1, 5, 1, 50, 210)
    assert _tab_text(page, REPORTS).endswith(" 90")
    page.reports_pager.go(1)
    assert page.reports.rowCount() == 140 and shown() == (waiting, set(decided[50:100]))
    assert _pager_text(page.reports_pager) == _page_text(2, 5, 51, 100, 210)
    # The last page: the 10 decided ones left, and still every waiting one.
    page.reports_pager.go(3)
    assert page.reports.rowCount() == 100 and shown() == (waiting, set(decided[200:]))
    assert _pager_text(page.reports_pager) == _page_text(5, 5, 201, 210, 210)
    # A tab change goes back to page 1.
    page._set_tab(ALL)
    page._set_tab(REPORTS)
    assert shown() == (waiting, set(decided[:50]))
    page.close()


def test_item_passes_page_pages_history_and_shows_every_pass_still_to_do(app, papers):
    page = ItemPassesPage(papers.conn, papers.owner_user)
    coming = papers.conn.execute("SELECT COUNT(*) FROM item_passes WHERE status = 'PRINTED'").fetchone()[0]
    assert coming >= 250 and page.tab == item_passes.PRINTED and page.table.rowCount() == coming and page.pager.isHidden()
    page._set_tab(item_passes.COLLECTED)
    collected = _ids(item_passes.search(papers.conn, papers.owner_user, status=item_passes.COLLECTED, limit=10 ** 9))  # newest first
    n = len(collected)
    assert _ids(page.rows) == collected[:50] and _pager_text(page.pager) == _page_text(1, _pages(n), 1, 50, n)
    assert _tab_text(page, item_passes.COLLECTED).endswith(f" {n}")
    page.pager.go(1)
    assert _ids(page.rows) == collected[50:100] and _pager_text(page.pager) == _page_text(2, _pages(n), 51, 100, n)
    # A tab change goes back to page 1.
    page._set_tab(ALL)
    assert page.table.rowCount() == 50 and _pager_text(page.pager).startswith("Page 1 of ")
    page.pager.go(1)
    page.search.setText("OLDEST-IP")
    assert _ids(page.rows) == [papers.oldest["ip"]] and page.pager.isHidden()
    page.clear_filters()
    assert _pager_text(page.pager).startswith("Page 1 of ")
    page.date_from.setDate(QDate(2020, 1, 1))
    page.date_to.setDate(QDate(2020, 1, 31))
    assert _ids(page.rows) == [papers.oldest["ip"]]
    page.close()


def test_warehouse_coming_back_shows_every_paper_even_past_2000(app, papers):
    page = CentralWarehousePage(papers.conn, papers.owner_user)
    page._set_tab("BACK")
    waiting = papers.conn.execute("SELECT (SELECT COUNT(*) FROM claim_stubs WHERE status = 'ISSUED') + (SELECT COUNT(*) FROM item_passes WHERE status = 'PRINTED')").fetchone()[0]
    # Every one: the 2,099 newer collected copies used to push the waiting ones past the 2,000 cap, off this list.
    assert waiting >= 500 and page.returns.rowCount() == waiting
    page.close()


# ------------------------------------------------------------------ Central Warehouse: Shipped is paged, the rest is work to do

def test_warehouse_shipped_pages_and_to_ship_shows_everything(app, big):
    w = big
    page = CentralWarehousePage(w.conn, w.warehouse)
    page._set_tab(warehouse.TO_SHIP)
    assert page.table.rowCount() == 205 and page.pager.isHidden()
    page._set_tab(warehouse.SHIPPED)
    newest = sorted(w.shipped, reverse=True)
    assert _ids(page.rows) == newest[:50] and _pager_text(page.pager) == _page_text(1, 5, 1, 50, 205)
    assert page.tabs.tabText(2).endswith(" 205")
    # Next: the next 50, nothing selected.
    page.table.selectRow(20)
    page.pager.next_button.click()
    assert _ids(page.rows) == newest[50:100] and _pager_text(page.pager) == _page_text(2, 5, 51, 100, 205) and not page.table.selectedItems()
    # A plain refresh stays on this page and keeps the selected order.
    page.table.selectRow(30)
    picked = page.rows[30]["id"]
    page.refresh()
    assert _ids(page.rows) == newest[50:100] and page._row()["id"] == picked
    page.pager.go(3)
    assert _ids(page.rows) == newest[200:] and _pager_text(page.pager) == _page_text(5, 5, 201, 205, 205)
    # Search finds the oldest, beyond page 1.
    oldest = min(w.shipped)
    number = w.conn.execute("SELECT of_number FROM order_forms WHERE id = ?", (oldest,)).fetchone()[0]
    page.search.setText(number)
    page.refresh()
    assert _ids(page.rows) == [oldest] and page.pager.isHidden()
    page.search.setText("")
    page.refresh()
    assert _ids(page.rows) == newest[:50] and _pager_text(page.pager) == _page_text(1, 5, 1, 50, 205)  # a new search goes back to page 1
    # A tab change goes back to page 1 too, and To ship still shows every order.
    page.pager.go(1)
    page._set_tab(warehouse.TO_SHIP)
    assert page.table.rowCount() == 205 and page.pager.isHidden()
    page._set_tab(warehouse.SHIPPED)
    assert _ids(page.rows) == newest[:50]
    page.close()
