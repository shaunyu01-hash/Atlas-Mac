"""Payments page fixes from an audit: the check stays on the screen, Enter presses the action, reasons are asked for plainly,
the search waits for a pause in typing, one question to cancel a check, plain button names, empty lists, and room before Cancel check."""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from atlaspo import auth, db, debit_memos, order_forms, orders, payments, receiving, sample_data
from atlaspo.ui import payments_page, theme

from conftest import NORTH, PASSWORD, SOUTH


@pytest.fixture
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """Column choices and widths go to a file of this test's own."""
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(theme, "_settings", lambda: settings)


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    folder = tmp_path_factory.mktemp("payments_ui_sample")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PO_DATA_DIR", str(folder))
        conn = db.open_database(folder / "sample.sqlite")
        sample_data.build(conn)
        yield conn
        conn.close()


@pytest.fixture
def lorna(world):
    return auth.create_user(world.conn, world.admin, "lorna", "Lorna Tan", PASSWORD, ["ACCOUNTING"], must_change=False)


class Order:
    """One order delivered to two branches, 10 pieces each at 100.00, with two receivers at each (as in test_payments)."""

    def __init__(self, world, av=10, ros=10):
        self.w = world
        self.of_id = world.approved(items=[world.item(cost=10000, alloc={NORTH: 10, SOUTH: 10}, description="Socks", item_code="SOCK-1")])
        self.pos = {p["branch_code"]: p for p in order_forms.generate_pos(world.conn, world.buyer, self.of_id)}
        for p in self.pos.values():
            orders.issue(world.conn, world.buyer, p["id"])
        self.count(NORTH, av)
        self.count(SOUTH, ros)

    def count(self, branch, pieces):
        w, po = self.w, self.pos[branch]
        item = orders.get_po(w.conn, po["id"])["items"][0]["id"]
        for n in (1, 2):
            login = f"r{branch}x{n}"
            existing = w.conn.execute("SELECT id FROM users WHERE username = ?", (login,)).fetchone()
            who = auth.get_user(w.conn, existing["id"]) if existing else auth.create_user(w.conn, w.admin, login, f"Receiver {branch} {n}", PASSWORD, ["RECEIVER"], must_change=False, branch_code=branch)
            receiving.save_count(w.conn, who, po["id"], {item: str(pieces)})

    def short_memo(self, branch=NORTH):
        po_id = self.pos[branch]["id"]
        lines = debit_memos.outstanding(self.w.conn, self.w.buyer, po_id)
        return debit_memos.issue(self.w.conn, self.w.buyer, po_id, {(l["po_item_id"], l["reason"]): l["pieces_m"] // 1000 for l in lines}, "Short")

    def enter(self, user, off_by="0"):
        row = payments.order_row(self.w.conn, self.of_id)
        by_po = {p["po_id"]: p["delivered_c"] for p in row["pos"]}
        receipts = payments.receipts_to_enter(self.w.conn, user, self.of_id)
        payments.save_dr_entries(self.w.conn, user, self.of_id, {d["id"]: ("2026-10-28", f"{by_po[d['po_id']] / 100 + float(off_by):.2f}") for d in receipts})


def _printed_check(world, user, day, number):
    Order(world).enter(user)
    group = payments.checks_due(world.conn)[0]
    return payments.record_check(world.conn, user, [o["id"] for o in group["orders"]], number, day, f"{group['check_c'] / 100:.2f}")


def _shown(page, width=1154, height=700):
    """On the screen at the width a 1366 laptop leaves beside the sidebar, without a window appearing."""
    page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    page.resize(width, height)
    page.show()
    QApplication.processEvents()
    return page


# ---------------------------------------------------------------- AUD-004 the check amount is on the screen on the check tabs

def test_aud004_check_amount_is_on_screen_on_every_check_tab(app, sample):
    user = auth.login(sample, "accounting", "123")
    page = _shown(payments_page.PaymentsPage(sample, user))
    branch_names = [name for _, name in page.branches]
    for tab in (page.TO_PAY, page.FUTURE, page.DUE, page.CLAIMED, page.CLEARED, page.CANCELLED):
        page._set_tab(tab)
        QApplication.processEvents()
        page._fit_current()
        tree = page.tables[tab]
        headers = page.columns[tree]
        amount = headers.index("Check amount")
        assert not tree.isColumnHidden(amount)
        # Before the fix it sat after every branch and money column, about 2,100 px in, off a 1,112 px list.
        x = tree.header().sectionViewportPosition(amount)
        assert 0 <= x and x + tree.columnWidth(amount) <= tree.viewport().width(), (page.TABS[tab], x, tree.viewport().width())
        # It's right after Supplier.
        assert tree.header().visualIndex(amount) == tree.header().visualIndex(headers.index("Supplier")) + 1
        # The pieces under each branch are for comparing DRs: they start hidden here, and Columns still has them.
        assert all(tree.isColumnHidden(headers.index(name)) for name in branch_names)
        assert all(name.lower().replace(" ", "-") in page.choosers[tree].checks for name in branch_names)
        pieces = [tree.isColumnHidden(headers.index(h)) for h in ("Pcs ordered", "Pcs delivered")]
        assert pieces == ([False, False] if tab == page.TO_PAY else [True, True])
    # Check the DRs keeps the pieces under every branch.
    page._set_tab(page.TO_CHECK)
    tree = page.to_check
    assert not any(tree.isColumnHidden(page.columns[tree].index(name)) for name in branch_names)
    page.close()


def test_aud004_hscroll_repro_check_amount_in_view(app, sample):
    """The audit's repro measured the check amount off screen on all six check tabs; on Unclaimed nothing needs scrolling at 1,920."""
    user = auth.login(sample, "accounting", "123")
    page = _shown(payments_page.PaymentsPage(sample, user), 1708, 900)
    page._set_tab(page.DUE)
    QApplication.processEvents()
    page._fit_current()
    assert page.paid.horizontalScrollBar().maximum() == 0
    page.close()


# ---------------------------------------------------------------- AUD-005 Enter in a box presses the action, never Cancel

def _press_enter(dialog, field, text=None):
    dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    dialog.show()
    QApplication.processEvents()
    field.setFocus()
    if text is not None:
        QTest.keyClicks(field, text)
    QTest.keyClick(field, Qt.Key.Key_Return)
    QApplication.processEvents()
    return dialog.result()


def test_aud005_enter_in_payment_dialogs_does_not_throw_the_typing_away(app, world, lorna, monkeypatch):
    saved = []
    # The DRs are a panel on the Payments page now, not a dialog, so they're tested in test_rules_dr_lines.
    for name in ("record_check", "record_checks", "claim_check", "clear_check", "save_dr_entries", "mark_cleared_many"):
        monkeypatch.setattr(payments, name, lambda *a, _n=name, **k: saved.append(_n))
    monkeypatch.setattr(payments, "receipts_to_enter", lambda c, u, oid: [{"id": 1, "dr_number": "DR1", "branch": "NTH", "total_c": None, "dr_date": None, "suggested_date": "2026-11-02"}])
    from datetime import date
    group = {"supplier": "SUP", "supplier_id": 1, "orders": [{"id": 1, "of_number": "OF-X"}], "due": date(2026, 11, 7), "check_c": 100000}
    check = {"id": 1, "check_number": "100", "supplier": "SUP", "amount_c": 100000}
    cases = [
        (payments_page.CheckDialog(None, world.conn, lorna, group), lambda d: d.number, "123456", "record_check"),
        (payments_page.BatchCheckDialog(None, world.conn, lorna, [group]), lambda d: d.first, "123456", "record_checks"),
        (payments_page.ClaimDialog(None, world.conn, lorna, check), lambda d: d.who, "Juan", "claim_check"),
        (payments_page.ClearDialog(None, world.conn, lorna, check), lambda d: d.day, None, "clear_check"),
        (payments_page.ClearTickedDialog(None, world.conn, lorna, [check]), lambda d: d.day, None, "mark_cleared_many"),
    ]
    for dialog, field, text, call in cases:
        saved.clear()
        defaults = [b.text() for b in dialog.findChildren(payments_page.QPushButton) if b.isDefault()]
        assert defaults and defaults[0] not in ("Cancel", "Go back"), (type(dialog).__name__, defaults)
        # Before the fix Enter pressed Cancel: the dialog closed as rejected and nothing was saved.
        assert _press_enter(dialog, field(dialog), text) == QDialog.DialogCode.Accepted, type(dialog).__name__
        assert saved == [call]
        dialog.close()


def test_aud005_enter_in_the_difference_reason_accepts_it(app, world, lorna):
    o = Order(world)
    o.enter(lorna, off_by="100")
    dialog = payments_page.DifferenceDialog(None, payments.order_row(world.conn, o.of_id), ask_reason=True)
    assert _press_enter(dialog, dialog.why, "Delivery charge on the DR") == QDialog.DialogCode.Accepted
    assert dialog.reason == "Delivery charge on the DR"


# ---------------------------------------------------------------- AUD-039 an empty reason says what's missing

def test_aud039_check_for_a_different_amount_with_no_reason_says_so(app, world, lorna, monkeypatch):
    Order(world).enter(lorna)
    group = payments.checks_due(world.conn)[0]
    shown = []
    monkeypatch.setattr(payments_page, "ask_text", lambda *a, **k: "")
    monkeypatch.setattr(payments_page, "error", lambda parent, message: shown.append(message))
    dialog = payments_page.CheckDialog(None, world.conn, lorna, group)
    dialog.number.setText("999001")
    dialog.amount.setText("1.00")
    dialog.save()
    assert shown == ["Write why the amount is different."]
    assert dialog.result() == 0 and not world.conn.execute("SELECT 1 FROM payment_checks WHERE check_number = '999001'").fetchone()
    # Going back from the question says nothing.
    shown.clear()
    monkeypatch.setattr(payments_page, "ask_text", lambda *a, **k: None)
    dialog.save()
    assert shown == []


def test_aud039_accepting_a_difference_with_no_reason_says_so(app, world, lorna):
    o = Order(world)
    o.enter(lorna, off_by="100")
    dialog = payments_page.DifferenceDialog(None, payments.order_row(world.conn, o.of_id), ask_reason=True)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    dialog.show()
    assert dialog.missing.isHidden()
    dialog._ok()
    assert not dialog.result() and dialog.isVisible()
    assert not dialog.missing.isHidden() and dialog.missing.text() == "Write why the amount is different."
    dialog.close()


# ---------------------------------------------------------------- AUD-145 the search waits for a pause in typing

def test_aud145_payments_search_refreshes_once_typing_pauses(app, world, lorna, monkeypatch):
    calls = []
    real = payments_page.PaymentsPage.refresh
    monkeypatch.setattr(payments_page.PaymentsPage, "refresh", lambda self, *a: (calls.append(self.search.text()), real(self, *a)))
    page = _shown(payments_page.PaymentsPage(world.conn, lorna))
    calls.clear()
    page.search.setFocus()
    for ch in "S00":
        QTest.keyClick(page.search, ch)
    assert calls == []  # before the fix: one full refresh per key
    QTest.qWait(450)
    assert calls == ["S00"]
    # Text the program puts in, and clearing the box, show at once.
    page.search.setText("770002")
    assert calls[-1] == "770002"
    page.search.clear()
    assert calls[-1] == "" and len(calls) == 3
    page.close()


# ---------------------------------------------------------------- AUD-156 cancelling a check asks once

def test_aud156_cancel_check_asks_once_with_the_warning_in_the_reason(app, world, lorna, monkeypatch):
    _printed_check(world, lorna, "2030-06-01", "880001")
    asked, confirmed = [], []
    monkeypatch.setattr(payments_page, "confirm", lambda *a, **k: confirmed.append(a) or True)
    monkeypatch.setattr(payments_page, "ask_text", lambda parent, title, prompt: asked.append((title, prompt)) or "Spoiled")
    page = payments_page.PaymentsPage(world.conn, lorna)
    page._set_tab(page.FUTURE)
    assert page._selected_check()["check_number"] == "880001"
    page.cancel_check()
    assert confirmed == [] and len(asked) == 1
    title, prompt = asked[0]
    assert title == "Cancel check" and "Cancel check 880001?" in prompt and "Void the paper check too." in prompt and prompt.endswith("Why is this check being cancelled?")
    assert world.conn.execute("SELECT status, cancel_reason FROM payment_checks WHERE check_number = '880001'").fetchone()[:] == ("CANCELLED", "Spoiled")
    # Going back from the question changes nothing.
    _printed_check(world, lorna, "2030-06-01", "880002")
    monkeypatch.setattr(payments_page, "ask_text", lambda *a, **k: None)
    page.refresh()
    page.cancel_check()
    assert world.conn.execute("SELECT status FROM payment_checks WHERE check_number = '880002'").fetchone()[0] != "CANCELLED"


# ---------------------------------------------------------------- AUD-163, AUD-024 the buttons for the DRs say what they do

def test_aud163_after_a_differing_dr_the_button_left_says_the_other_drs_match(app, world, lorna):
    o = Order(world, av=8)
    o.short_memo()
    page = payments_page.PaymentsPage(world.conn, lorna)
    assert page.primary_btn.text() == "DRs match"
    # DR differs: only the North DR is typed, and it doesn't match.
    receipts = payments.receipts_to_enter(world.conn, lorna, o.of_id)
    north = next(d for d in receipts if d["po_id"] == o.pos[NORTH]["id"])
    payments.save_dr_entries(world.conn, lorna, o.of_id, {north["id"]: ("2026-10-28", "999.00")})
    page.refresh()
    order = page._selected_order()
    assert order["stage"] == payments.ENTER_DRS  # the stage logic is as it was
    assert page.primary_btn.text() == "The other DRs match"
    page._primary()
    assert payments.order_row(world.conn, o.of_id)["stage"] == payments.DIFFERENCE


def test_aud024_dr_differs_is_the_one_name_for_typing_the_drs(app, world, lorna):
    o = Order(world)
    o.enter(lorna, off_by="100")
    page = payments_page.PaymentsPage(world.conn, lorna)
    assert page.primary_btn.text() == "Accept the difference" and page.second_btn.text() == "DR differs"
    source = Path(payments_page.__file__).read_text(encoding="utf-8")
    assert "Change the DRs" not in source


# ---------------------------------------------------------------- AUD-007, AUD-008 an empty list keeps the page still and says why it's empty

def test_aud007_aud008_a_search_with_no_match_keeps_the_header_and_says_so(app, sample):
    user = auth.login(sample, "accounting", "123")
    page = _shown(payments_page.PaymentsPage(sample, user), 1400, 800)
    for tab in (page.TO_CHECK, page.TO_PAY, page.FUTURE, page.CANCELLED):
        page._set_tab(tab)
        QApplication.processEvents()
        n = int(page.tabs.tabText(list(page.TABS).index(tab)).split()[-1])
        assert n, page.TABS[tab]
        y = page.search.geometry().y()
        page.search.setText("zzqqxx")
        QApplication.processEvents()
        assert page.empty.isVisible()
        # Before the fix the search box dropped hundreds of pixels down the page.
        assert page.search.geometry().y() == y, page.TABS[tab]
        assert page.empty.text() == f"No match for 'zzqqxx'. Clear the search to see all {n}."
        page.search.clear()
        QApplication.processEvents()
    page.close()


def test_aud008_cleared_with_no_match_says_how_to_see_them(app, world, lorna):
    check = _printed_check(world, lorna, "2020-06-06", "770002")
    payments.claim_check(world.conn, lorna, check, "2020-06-08", "Juan")
    payments.clear_check(world.conn, lorna, check, "2020-06-10")
    page = payments_page.PaymentsPage(world.conn, lorna)
    page._set_tab(page.CLEARED)
    page.search.setText("zzqqxx")
    assert page.empty.text() == "No match for 'zzqqxx'. Clear the search and press Show all cleared to see all 1."
    # With nothing anywhere, the plain words stay.
    page._set_tab(page.CANCELLED)
    page.search.setText("zzqqxx")
    assert page.empty.text() == "No cancelled checks."


# ---------------------------------------------------------------- AUD-014 room before Cancel check

def test_aud014_cancel_check_stands_apart_from_mark_claimed(app, world, lorna):
    _printed_check(world, lorna, "2030-06-01", "880001")
    page = _shown(payments_page.PaymentsPage(world.conn, lorna), 1400, 800)
    page._set_tab(page.FUTURE)
    QApplication.processEvents()
    assert page.primary_btn.text() == "Mark claimed" and page.cancel_btn.isVisible() and page.primary_btn.isVisible()
    gap = page.cancel_btn.geometry().left() - page.primary_btn.geometry().right()
    assert gap >= 24, gap
    page.close()
