"""The practice database: every screen and report has something to show, and real data is never touched."""
from __future__ import annotations

import pytest

from atlaspo import auth, backup, db, order_forms, orders, reports, sample_data
from atlaspo.db import POError


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    folder = tmp_path_factory.mktemp("sample")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PO_DATA_DIR", str(folder))
        sample_data.ensure(folder)
        conn = db.open_database()
        yield conn
        conn.close()


def test_real_rules_still_hold(sample):
    assert backup.integrity_report(sample) == []
    assert sample_data.is_sample(sample)


@pytest.mark.parametrize("definition", reports.REPORTS, ids=lambda d: d.key)
def test_every_report_has_rows(sample, definition):
    assert definition.fn(sample).rows


def test_every_of_status_is_present(sample):
    found = {r[0] for r in sample.execute("SELECT DISTINCT status FROM order_forms")}
    assert found == {orders.DRAFT, orders.PENDING, orders.APPROVED, orders.CANCELLED}


def test_every_po_status_in_use_is_present(sample):
    found = {r[0] for r in sample.execute("SELECT DISTINCT status FROM purchase_orders")}
    assert found == {orders.APPROVED, orders.ISSUED, orders.PARTIAL, orders.FULL, orders.CLOSED, orders.CANCELLED}


def test_every_login_is_short_and_easy(sample):
    from atlaspo.db import POError
    names = [r["username"] for r in sample.execute("SELECT username FROM users WHERE active = 1")]
    assert sample_data.PASSWORD == "123" and {"owner", "admin", "nor1", "nor2", "sou1", "sou2", "eas1", "eas2", "wes1", "wes2"} <= set(names)
    for name in names:
        assert auth.login(sample, name, "123")
    assert auth.login(sample, "sou1", "123").branch_code == 4
    # The rule for logins made in the program stays as it was.
    admin = auth.login(sample, "owner", "123")
    with pytest.raises(POError, match="at least 8"):
        auth.create_user(sample, admin, "new", "New", "123", ["BUYER"])


def test_nor1_and_nor2_can_try_the_whole_flow(sample):
    from atlaspo import auth, receiving
    nor1, nor2 = auth.login(sample, "nor1", "123"), auth.login(sample, "nor2", "123")
    # The BBB delivery printed on Oct 26 is a second first count waiting at North.
    assert sorted(r["need"] for r in receiving.to_count(sample, nor2)) == [receiving.FIRST, receiving.FIRST, receiving.SECOND]
    assert [r["need"] for r in receiving.to_count(sample, nor1)] == [receiving.FIRST, receiving.FIRST]


def test_every_receiving_situation_is_present(sample):
    from atlaspo import auth, receiving, selling
    admin = auth.get_user(sample, sample.execute("SELECT id FROM users WHERE username = 'owner'").fetchone()[0])
    statuses = [r["status"] for r in sample.execute("SELECT status FROM branch_counts")]
    assert {receiving.APPROVED, receiving.WAITING, receiving.REPLACED, receiving.MISMATCH} == set(statuses)
    results = receiving.po_results(sample, admin)
    assert any(r["difference_m"] < 0 and not r["waiting"] for r in results)  # short
    assert any(r["difference_m"] > 0 for r in results)  # over
    assert any(not r["lines_off"] for r in results)  # exactly right
    assert any(r["delivery_count"] == 2 for r in sample.execute("SELECT delivery_count FROM purchase_orders").fetchall())
    assert any(r["progress"].startswith("Check") for r in results)  # two rounds that disagree
    # One receiver's mistakes show as a pattern, and nothing was accepted on one person's word.
    assert max(p["wrong"] for p in receiving.by_receiver(sample, admin)) == 2
    assert not sample.execute("SELECT 1 FROM branch_counts WHERE approved_by = counted_by OR (status = 'APPROVED' AND approved_by IS NULL)").fetchone()
    # Issued POs are left to practise on, at more than one branch.
    uncounted = sample.execute("SELECT DISTINCT branch_code FROM purchase_orders WHERE status = 'ISSUED' AND id NOT IN (SELECT po_id FROM branch_counts)").fetchall()
    assert uncounted
    sources = [r["source"] for r in selling.list_items(sample)]
    assert sources.count(selling.MANUAL) == 2 and selling.AUTO in sources
    assert sum(r["below_srp"] for r in selling.list_items(sample)) == 1 and all(r["price_c"] % 1000 == 0 for r in selling.list_items(sample))
    assert any(r["damaged_m"] for r in results)
    assert any(r["excess_left_m"] and not r["progress"] for r in results)  # extra pieces left to return
    assert sample.execute("SELECT 1 FROM branch_extra_items").fetchone()  # and a product not on the PO
    assert auth.login(sample, "purchaser", "123") and auth.login(sample, "purchaser2", "123") and auth.login(sample, "accounting", "123").roles == {"ACCOUNTING"}
    from atlaspo import payments
    assert {c["bucket"] for c in payments.checks(sample, payments.ACTIVE)} == {payments.FUTURE, payments.DUE, payments.CLAIMED, payments.CLEARED}
    assert payments.cash_flow(sample)
    # A stub at the warehouse waiting for the supplier's check before it can be picked up, one forfeited, and damage left to make one for.
    from atlaspo import claim_stubs
    assert [s["paid_on"] is None for s in claim_stubs.search(sample, admin, status=claim_stubs.AT_WAREHOUSE)] == [True]
    assert claim_stubs.search(sample, admin, status=claim_stubs.FORFEITED)
    assert any(claim_stubs.can_issue(sample, r["id"]) for r in results)
    # A debit memo has settled one delivery, and another is left to practise on.
    assert any(r["debit_memos"] and r["tab"] == receiving.COMPLETED for r in results)
    from atlaspo import debit_memos
    assert any(debit_memos.can_issue(sample, r["id"]) for r in results)


def test_every_debit_memo_and_claim_stub_tab_has_something_to_show(sample):
    from atlaspo import claim_stubs, debit_memos
    admin = auth.login(sample, "owner", "123")
    for status in (debit_memos.ISSUED, debit_memos.CANCELLED):
        assert debit_memos.search(sample, admin, status=status), status
    for status in (claim_stubs.ISSUED, claim_stubs.AT_WAREHOUSE, claim_stubs.COLLECTED, claim_stubs.FORFEITED, claim_stubs.CANCELLED):
        assert claim_stubs.search(sample, admin, status=status), status
    assert claim_stubs.for_sale(sample, admin)
    assert debit_memos.search(sample, admin, status=debit_memos.CANCELLED)[0]["cancel_reason"]
    assert claim_stubs.search(sample, admin, status=claim_stubs.COLLECTED)[0]["collected_by_name"] == "Rico Tan"


def test_every_sidebar_tab_opens_with_something_in_it(sample, tmp_path, monkeypatch):
    """Inquiry's first and third tabs and Payments' Cleared tab wait to be asked, on purpose. Everything else shows rows as soon as it's opened."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings, Qt
    from PySide6.QtWidgets import QApplication, QTabBar, QTableWidget, QTreeWidget
    from atlaspo.ui import theme
    from atlaspo.ui.main_window import MainWindow
    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(theme, "_settings", lambda: settings)
    win = MainWindow(sample, auth.login(sample, "owner", "123"))
    win.show()
    empty = []

    def rows(page):
        lists = page.findChildren(QTableWidget) + page.findChildren(QTreeWidget)
        return sum((w.rowCount() if isinstance(w, QTableWidget) else w.topLevelItemCount()) for w in lists if w.isVisible())

    for row, page in win.nav_rows.items():
        win.nav.setCurrentRow(row)
        name = win.nav.item(row).text().split("  ")[0]
        bars = page.findChildren(QTabBar)
        if not bars and not rows(page):
            empty.append(name)
        for bar in bars:
            for i in range(bar.count()):
                bar.setCurrentIndex(i)
                app.processEvents()
                tab = bar.tabText(i).split("  ")[0]
                if not rows(page) and tab not in ("Find an OF", "OFs by date", "Approval policy", "Company", "Order Form layout", "Cleared"):
                    empty.append(f"{name} / {tab}")
    win.close()
    assert empty == []


def test_the_sample_shows_a_clean_order_going_straight_to_its_check(sample):
    from atlaspo import payments
    rows = payments.orders_in_payment(sample)
    clean = [r for r in rows if not r["why"] and not r["entered"]]  # nobody had to type a DR for these
    assert clean and all(r["stage"] == payments.TO_PAY and r["entered"] == 0 for r in clean)
    assert any(r["stage"] == payments.ENTER_DRS for r in rows) and any(r["stage"] == payments.DIFFERENCE for r in rows)


def test_payment_lists_add_up_and_open_to_whats_inside(sample, tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings, Qt
    from PySide6.QtWidgets import QApplication
    from atlaspo import payments
    from atlaspo.ui import theme
    from atlaspo.ui.payments_page import MONEY, PaymentsPage
    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(theme, "_settings", lambda: settings)
    # The order, less what comes off, is the check. Delivered is only there to read.
    rows = payments.orders_in_payment(sample)
    assert all(any(r[k] and r["stage"] == payments.TO_PAY for r in rows) for k in ("dm_c", "cs_c", "sticker_c", "term_c"))
    for r in rows:
        assert r["total_c"] - r["dm_c"] - r["cs_c"] - r["unclaimed_c"] - r["sticker_c"] - r["term_c"] == r["pay_c"]
        assert sum(po["dm_c"] + po["cs_c"] for po in r["pos"]) == r["charged_c"]
        for po in r["pos"]:
            # Products add up to the PO, apart from VAT added on top, which has its own line.
            if r["supplier_code"] != "TAY":
                assert abs(sum(line["order_c"] for line in po["lines"]) - po["total_c"]) <= len(po["lines"])
    page = PaymentsPage(sample, auth.login(sample, "owner", "123"))
    page.show()
    app.processEvents()
    # A check opens to its orders, and an order to its products, each with its pieces under every branch.
    page._set_tab(page.TO_PAY)
    tree = page.to_pay
    assert set(MONEY) <= set(page.columns[tree])
    assert tree.topLevelItem(0).data(0, Qt.ItemDataRole.UserRole) == page.READY
    group, top = page.groups[0], tree.topLevelItem(1)
    assert top.childCount() == len(group["orders"])
    first = group["orders"][0]
    po = top.child(0).child(0)
    headers = page.columns[tree]
    assert top.child(0).text(0) == first["of_number"] and po.text(0) == first["pos"][0]["lines"][0]["item_code"]
    assert any(po.text(headers.index(name)) for _, name in page.branches)
    # Selecting a row inside still acts on the check, and a refresh keeps it open.
    tree.setCurrentItem(po)
    assert page.primary_btn.text() == "Record check" and page._selected_group() is group
    top.setExpanded(True)
    page.refresh()
    assert tree.topLevelItem(1).isExpanded()
    # A column nobody uses stays out of the way until a row needs it, and the check is always on the screen.
    headers = page.columns[tree]
    assert tree.isColumnHidden(headers.index("Short")) == (not any(g["orders"][0]["unclaimed_c"] for g in page.groups))
    page.resize(1100, 700)
    page._fit(tree)
    assert not tree.isColumnHidden(headers.index("Check amount"))
    # Pieces ordered and delivered are always there, to check the DRs against.
    assert not tree.isColumnHidden(headers.index("Pcs ordered")) and not tree.isColumnHidden(headers.index("Pcs delivered"))
    # Columns can be taken away, and the choice is kept for that login.
    chooser = page.choosers[tree]
    chooser.checks["stickers"].setChecked(False)
    assert tree.isColumnHidden(page.columns[tree].index("Stickers")) and "stickers" not in settings.value("columns/payments-to-pay-owner")
    chooser.reset()
    assert not tree.isColumnHidden(page.columns[tree].index("Stickers"))
    # A check already recorded opens the same way.
    page._set_tab(page.CLEARED)
    page.show_all_cleared()
    assert page.paid.topLevelItem(0).child(0).childCount() >= 1
    # Leaving the tab puts it back to waiting.
    page._set_tab(page.TO_PAY)
    page._set_tab(page.CLEARED)
    assert page.paid.topLevelItemCount() == 0
    page.close()


def test_checks_to_make_are_ready_on_top_and_batch_recorded_by_date(tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings, Qt
    from PySide6.QtWidgets import QApplication
    from atlaspo import db, payments
    from atlaspo.ui import common, payments_page, theme
    from atlaspo.ui.payments_page import BatchCheckDialog, PaymentsPage
    QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(theme, "_settings", lambda: settings)
    monkeypatch.setattr(payments_page, "open_for_printing", lambda documents, name: None)
    folder = tmp_path / "own"
    monkeypatch.setenv("PO_DATA_DIR", str(folder))
    sample_data.ensure(folder)
    conn = db.open_database()
    page = PaymentsPage(conn, auth.login(conn, "owner", "123"))
    page._set_tab(page.TO_PAY)
    tree = page.to_pay
    key = lambda item: item.data(0, Qt.ItemDataRole.UserRole)
    tops = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    headings = [key(i) for i in tops if isinstance(key(i), str)]
    assert headings == [page.READY, page.FINAL_FIRST]
    # Each section runs soonest first.
    for section in (page.READY, page.FINAL_FIRST):
        dates = [g["due"] for g in page.groups if page._section(g) == section]
        assert dates == sorted(dates) and dates
    # A check that needs a Final OF can't be recorded until it's printed, and then it moves up.
    waiting = next(g for g in page.groups if page._section(g) == page.FINAL_FIRST)
    item = next(i for i in tops if isinstance(key(i), int) and page.groups[key(i) - 1] is waiting)
    tree.setCurrentItem(item)
    assert page.primary_btn.text() == "Print Final OF" and not page.second_btn.isVisibleTo(page)
    # One supplier, so a Saturday's check can hold a clean order beside the one that needs its Final OF.
    of_id = next(o["id"] for o in waiting["orders"] if o["final_of_due"])
    page._primary()
    moved = next(g for g in page.groups if of_id in [o["id"] for o in g["orders"]])
    assert page._section(moved) == page.READY and next(o for o in moved["orders"] if o["id"] == of_id)["final_of_due"] is False
    # A deduction made after printing asks for a new one.
    row = payments.order_row(conn, of_id)
    money = dict(row, pay_c=row["pay_c"] - 100)
    assert payments.final_of_due(conn, of_id, money)
    # Ticking a section takes everything in it, and it opens no rows.
    head = next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount()) if key(tree.topLevelItem(i)) == page.READY)
    head.setCheckState(0, Qt.CheckState.Checked)
    ready = page._ticked_groups(page.READY)
    assert len(ready) == len(page._groups_in(page.READY)) >= 2 and page.primary_btn.text() == f"Record {len(ready)} checks"
    # Sorted by date, and the numbers run on from the first. The last has to agree.
    dialog = BatchCheckDialog(page, conn, page.user, list(reversed(ready)))
    assert [g["due"] for g in dialog.groups] == sorted(g["due"] for g in ready)
    # It offers to carry on from the last check recorded, so continuous printing needs nothing typed but the last, to check.
    assert dialog.first.text() == payments.next_check_number(conn) and dialog.numbers[0].text() == dialog.first.text()
    # Only the last typed: the list counts back from it.
    dialog.first.setText("")
    dialog.last.setText("0099010")
    assert dialog.first.text() == f"{99010 - len(ready) + 1:07d}" and dialog.numbers[-1].text() == "0099010" and not dialog._problem()
    dialog.last.setText("")
    # Or typed straight into the list, with nothing above.
    dialog.first.setText("")
    for n, box in enumerate(dialog.numbers):
        box.setText(f"55{n:03d}")
    assert not dialog._problem()
    dialog.first.setText("0099000")
    assert dialog.numbers[1].text() == "0099001"
    dialog.last.setText("0099999")
    assert "you typed 0099999" in dialog.status.text()
    with pytest.raises(POError, match="you typed"):
        dialog.save()
    # A spoiled check: the numbers in the list change to match the paper, and the last agrees.
    final = int("0099000") + len(ready)
    dialog.numbers[-1].setText(f"{final:07d}")
    dialog.last.setText(f"{final:07d}")
    assert not dialog._problem()
    # One number already used stops the whole batch, with nothing recorded.
    before = conn.execute("SELECT COUNT(*) FROM payment_checks").fetchone()[0]
    used = conn.execute("SELECT check_number FROM payment_checks LIMIT 1").fetchone()[0]
    dialog.numbers[1].setText(used)
    with pytest.raises(POError, match="already been used"):
        dialog.save()
    assert conn.execute("SELECT COUNT(*) FROM payment_checks").fetchone()[0] == before
    dialog.numbers[1].setText("0099001")
    dialog.save()
    assert conn.execute("SELECT COUNT(*) FROM payment_checks").fetchone()[0] == before + len(ready)
    assert not payments.check_numbers("7", 0)
    with pytest.raises(POError, match="digits"):
        payments.check_numbers("A12", 2)
    page.close()
    conn.close()


def test_cash_flow_totals_on_the_last_line_and_adds_up_by_month(sample, tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from atlaspo import payments
    from atlaspo.money import fmt_plain
    from atlaspo.ui import theme
    from atlaspo.ui.payments_page import PaymentsPage
    QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(theme, "_settings", lambda: settings)
    daily = payments.cash_flow(sample)
    monthly = payments.cash_flow_by_month(daily)
    assert len(monthly) < len(daily) and sum(m["total_c"] for m in monthly) == sum(d["total_c"] for d in daily) and monthly[-1]["running_c"] == daily[-1]["running_c"]
    assert all(m["date"].day == 1 for m in monthly)
    page = PaymentsPage(sample, auth.login(sample, "owner", "123"))
    page._set_tab(page.FLOW)
    table = page.flow
    assert table.rowCount() == len(daily) + 1 and table.item(len(daily), 0).text() == "Total"
    assert table.item(len(daily), 4).text() == fmt_plain(sum(d["total_c"] for d in daily)) == table.item(len(daily) - 1, 5).text()
    page.flow_buttons[True].click()
    assert table.rowCount() == len(monthly) + 1 and table.horizontalHeaderItem(0).text() == "Month" and table.item(0, 0).text() == monthly[0]["date"].strftime("%b %Y")
    assert table.item(len(monthly), 4).text() == fmt_plain(sum(d["total_c"] for d in daily))
    page.flow_buttons[False].click()
    assert table.rowCount() == len(daily) + 1 and table.horizontalHeaderItem(0).text() == "Check date"
    page._set_tab(page.TO_PAY)
    assert not page.flow_buttons[True].isVisibleTo(page)
    page.close()


def test_inquiry_shows_pieces_delivered_in_total_and_by_branch(sample, tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from atlaspo import masters, receiving
    from atlaspo.ui import theme
    from atlaspo.ui.of_page import EXTRA_COLUMNS, InquiryPage
    QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(theme, "_settings", lambda: settings)
    page = InquiryPage(sample, auth.login(sample, "owner", "123"))
    page.status.setCurrentIndex(0)
    page.tabs.setCurrentIndex(0)
    page._set_mode(page.MODES and list(page.MODES)[1])
    delivered = receiving.delivered_by_po(sample)
    # One of the sample's orders is short at its first branch and exact at the others.
    of_id = next(r["of_id"] for r in delivered.values() if r["counted"] and r["received_m"] < r["ordered_m"] and r["received_m"])
    of_number = sample.execute("SELECT of_number FROM order_forms WHERE id = ?", (of_id,)).fetchone()[0]
    item = next(page.tree.topLevelItem(i) for i in range(page.tree.topLevelItemCount()) if page.tree.topLevelItem(i).text(0) == of_number)
    first = len(page.HEADERS) + len(EXTRA_COLUMNS)
    mine = [r for r in delivered.values() if r["of_id"] == of_id]
    assert item.text(first).endswith(f"of {sum(r['ordered_m'] for r in mine) // 1000:,}")
    assert not page.tree.isColumnHidden(first)
    for offset, branch in enumerate(masters.list_branches(sample, branches_only=True), 1):
        assert page.tree.headerItem().text(first + offset) == branch["name"]
        rows = [r for r in mine if r["branch_code"] == branch["code"]]
        assert bool(item.text(first + offset)) == bool(rows)
    page.close()


def test_received_filters_by_branch_with_one_press(sample, tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from atlaspo.ui import theme
    from atlaspo.ui.receiving_page import ReceivedPage
    QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(theme, "_settings", lambda: settings)
    page = ReceivedPage(sample, auth.login(sample, "owner", "123"))
    everyone = len(page.rows)
    assert everyone and page.branch_buttons[None].text().endswith(str(everyone))
    code = page.rows[0]["branch_code"]
    page.branch_buttons[code].click()
    assert page.rows and all(r["branch_code"] == code for r in page.rows) and len(page.rows) == int(page.branch_buttons[code].text().split()[-1])
    # The other buttons still say how many they hold, and All brings everything back.
    page.branch_buttons[None].click()
    assert len(page.rows) == everyone
    page.close()


def test_every_list_lets_people_choose_columns(sample, tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from atlaspo.ui import theme
    from atlaspo.ui.common import ColumnChooser
    from atlaspo.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(theme, "_settings", lambda: settings)
    win = MainWindow(sample, auth.login(sample, "owner", "123"))
    win.pages, win.of_page, win.po_page  # pages are built when first opened; build them all
    chooser_on = {c.tree for c in win.findChildren(ColumnChooser)}
    assert len(chooser_on) >= 18
    # Taking a column off a plain list hides it, and the choice is kept.
    chooser = next(c for c in win.findChildren(ColumnChooser) if c.key == "debit-memos-owner")
    chooser.checks["external-dm"].setChecked(False)
    assert chooser.tree.isColumnHidden(8) and settings.value("columns/debit-memos-owner")
    chooser.reset()
    assert not chooser.tree.isColumnHidden(8)
    win.close()


def test_approvals_page_has_every_kind_of_request(sample):
    assert len(order_forms.pending(sample)) == 2
    assert len(order_forms.pending_amendments(sample)) == 1
    assert len(orders.pending_amendments(sample)) == 1


def test_limit_shown_on_both_sides(sample):
    decisions = {r["grand_total_c"]: r["status"] for r in sample.execute("SELECT grand_total_c, status FROM order_forms WHERE grand_total_c IN (10000000, 10000100)")}
    assert decisions == {10000000: orders.APPROVED, 10000100: orders.PENDING}


def test_preserved_versions_inactive_branch_and_history_span(sample):
    assert sample.execute("SELECT 1 FROM purchase_orders p JOIN po_issued_versions v ON v.po_id = p.id GROUP BY p.id HAVING COUNT(*) = 2").fetchone()
    closed = sample.execute("SELECT active FROM branches WHERE code = ?", (sample_data.CLOSED_BRANCH,)).fetchone()
    assert closed["active"] == 0
    assert sample.execute("SELECT 1 FROM purchase_orders WHERE branch_code = ?", (sample_data.CLOSED_BRANCH,)).fetchone()
    assert {r[0] for r in sample.execute("SELECT DISTINCT po_year FROM purchase_orders")} == {2025, 2026}


def test_only_invented_and_sb_branches(sample):
    assert {r[0] for r in sample.execute("SELECT code FROM branches")} == {2, 4, 6, 8, sample_data.CLOSED_BRANCH}


def test_master_login_approves_what_others_and_it_prepared(sample):
    owner_user = auth.login(sample, "owner", sample_data.PASSWORD)
    assert owner_user.roles == {"ADMIN", "APPROVER"}
    waiting = order_forms.pending(sample)[0]["id"]
    assert order_forms.approve(sample, owner_user, waiting)
    sup = sample.execute("SELECT id FROM suppliers WHERE status = 'ACTIVE'").fetchone()[0]
    own = order_forms.create_draft(sample, owner_user, {"supplier_id": sup}, [
        {"department_id": 1, "description": "Own order", "unit_cost_c": 20000000, "allocations": {sample_data.NORTH: 1}}])
    assert order_forms.submit(sample, owner_user, own) == orders.PENDING
    assert order_forms.approve(sample, owner_user, own)


def test_every_department_and_user_kind(sample):
    assert sample.execute("SELECT COUNT(DISTINCT department_id) FROM purchase_order_items").fetchone()[0] == 9
    assert auth.login(sample, "viewer", sample_data.PASSWORD).roles == {"VIEWER"}
    # Easy names: AAA, BBB and CCC, each with its own brand and item codes.
    assert [r[0] for r in sample.execute("SELECT name FROM suppliers ORDER BY code")] == ["AAA", "BBB", "CCC"]
    assert {r[0] for r in sample.execute("SELECT DISTINCT brand FROM products")} == {"Brand A", "Brand B", "Brand C"}
    assert sample.execute("SELECT 1 FROM products WHERE active = 0").fetchone()
    assert sample.execute("SELECT 1 FROM users WHERE active = 0").fetchone()


def test_clock_is_put_back(sample):
    assert db.now.__name__ == "now" and order_forms.now.__name__ == "now"


def test_refuses_a_database_with_users(world, conn):
    before = conn.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0]
    with pytest.raises(POError, match="won't be added"):
        sample_data.build(conn)
    assert conn.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0] == before


def test_real_database_never_shows_as_sample(world, conn):
    assert not sample_data.is_sample(conn)


def test_forfeited_goods_get_a_selling_price(sample):
    from atlaspo import selling
    from atlaspo.db import PermissionDenied
    rows = selling.forfeited_items(sample)
    assert {r["kind"] for r in rows} == {selling.CS, selling.IP} and all(r["price_c"] == r["auto_c"] > r["cost_c"] for r in rows)
    first = rows[0]
    with pytest.raises(PermissionDenied):
        selling.set_forfeit_price(sample, auth.login(sample, "accounting", "123"), first["kind"], first["item_id"], 99000)
    buyer = auth.login(sample, "purchaser", "123")
    selling.set_forfeit_price(sample, buyer, first["kind"], first["item_id"], 99000)
    again = next(r for r in selling.forfeited_items(sample) if (r["kind"], r["item_id"]) == (first["kind"], first["item_id"]))
    assert (again["price_c"], again["source"], again["below_srp"]) == (99000, selling.MANUAL, False)
    selling.set_forfeit_price(sample, buyer, first["kind"], first["item_id"], first["auto_c"] - 1500)  # forfeited goods sold cheaper are flagged too
    cheap = next(r for r in selling.forfeited_items(sample) if (r["kind"], r["item_id"]) == (first["kind"], first["item_id"]))
    assert cheap["below_srp"] and cheap["price_c"] % 1000 == 0
    selling.set_forfeit_price(sample, buyer, first["kind"], first["item_id"], None)
    assert next(r for r in selling.forfeited_items(sample) if (r["kind"], r["item_id"]) == (first["kind"], first["item_id"]))["source"] == selling.AUTO


def test_each_role_does_only_its_part(sample):
    """The warehouse receives, purchasers order and price, accounting pays, and Admin doesn't count."""
    warehouse = auth.login(sample, "warehouse", "123")
    assert warehouse.roles == {"WAREHOUSE"} and warehouse.can("warehouse.receive") and not warehouse.can("dm.issue") and not warehouse.can("payment.view")
    buyer = auth.login(sample, "purchaser", "123")
    assert buyer.can("price.edit") and buyer.can("po.close") and not buyer.can("warehouse.receive") and not buyer.can("payment.view")
    admin = auth.login(sample, "owner", "123")
    assert not admin.can("receiving.count") and admin.can("payment.signoff")
    assert auth.login(sample, "approver", "123").can("payment.signoff") and not auth.login(sample, "accounting", "123").can("payment.signoff")


def test_one_check_waits_for_a_sign_off(sample):
    from atlaspo import payments
    waiting = [c for c in payments.checks(sample, payments.ACTIVE) if c["waiting_signoff"]]
    assert len(waiting) == 1 and waiting[0]["bucket"] == payments.FUTURE and waiting[0]["difference_reason"]


def test_the_central_warehouse_has_every_stage(sample):
    from atlaspo import warehouse
    rows = warehouse.search(sample, auth.login(sample, "warehouse", "123"))
    assert {r["stage"] for r in rows} == {warehouse.TO_RECEIVE, warehouse.TO_SHIP, warehouse.SHIPPED}
    assert any(r["stickers"] and r["stage"] == warehouse.SHIPPED for r in rows)



def test_the_sample_shows_what_was_built_on_november_6(sample):
    """Job titles, the warehouse's 0 boxes and second delivery, a box missing at a branch, a late delivery on a memo, and a difference waiting for its sign-off."""
    from atlaspo import debit_memos, payments, receiving, warehouse
    assert {u["username"]: u["title"] for u in auth.list_users(sample)}["warehouse"] == "Warehouse Head"
    rows = warehouse.search(sample, auth.login(sample, "warehouse", "123"))
    assert any(r["stages"] == {warehouse.SHIPPED, warehouse.TO_SHIP} and any(len(p["deliveries"]) == 2 for p in r["pos"]) for r in rows)
    assert any((receiving.boxes_check(sample, p) or {}).get("missing") for (p,) in sample.execute("SELECT DISTINCT po_id FROM warehouse_deliveries"))
    assert any(debit_memos.arrived_since(sample, d) for d in debit_memos.search(sample, auth.login(sample, "admin", "123")))
    assert any(r["stage"] == payments.SIGN_OFF for r in payments.orders_in_payment(sample))
